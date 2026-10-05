"""`handoff update`: pull the checkout Handoff was installed from, then run its installer again."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from handoff import cli, proc, update


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", name)


@pytest.fixture
def installed(tmp_path):
    """GitHub (a bare repo), a clone of it that Handoff was installed from, and a way to publish a change."""
    origin, work, source = tmp_path / "origin.git", tmp_path / "work", tmp_path / "source"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-b", "main")
    commit(work, "one")
    git(work, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(source)], check=True, capture_output=True)
    info = {"source": str(source), "install_root": str(tmp_path / "root"), "bin_dir": str(tmp_path / "bin"),
            "installer": "install.sh"}

    def publish(name):
        commit(work, name)
        git(work, "push", "-q", "origin", "main")

    return info, source, publish


def run(info, **kw):
    said, reinstalled = [], []
    code = update.update(say=said.append, info=info,
                         reinstall=lambda i, say: reinstalled.append(i) or 0, **kw)
    return code, said, reinstalled


def test_the_installers_record_where_they_installed_from(tmp_path):
    path = tmp_path / "install.json"
    path.write_text(json.dumps({"source": "/src", "installer": "install.sh"}), encoding="utf-8")
    assert update.install_info(path)["source"] == "/src"
    assert update.install_info(tmp_path / "missing.json") is None
    path.write_text("not json", encoding="utf-8")
    assert update.install_info(path) is None
    for script in ("install.sh", "install.ps1"):
        assert "install.json" in (Path(__file__).resolve().parents[1] / script).read_text(encoding="utf-8")


def _venv_python(path: Path, script: str = "") -> None:
    """A stand-in for the environment's python: this Python, unless `script` (sh) answers first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\n{script}\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def install_sh(tmp_path):
    """A checkout with this install.sh, a uv that makes an environment of this Python, and a way to run it."""
    checkout, root, bin_dir, fakes = tmp_path / "checkout", tmp_path / "root", tmp_path / "bin", tmp_path / "fakes"
    (checkout / "handoff").mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[1] / "install.sh", checkout / "install.sh")
    git(checkout.parent, "init", "-q", str(checkout))
    commit(checkout, "pyproject.toml")
    fakes.mkdir()
    # uv: `uv venv ... DIR` makes DIR/bin/python (this Python, with Handoff) unless it's there; `uv pip install`
    # does nothing. Each call is written to fakes/calls. FAKE_UV_FAILS=venv or pip makes that step fail, and
    # =import makes an environment whose Handoff doesn't load.
    (fakes / "uv").write_text(f"""#!{sys.executable}
import os, sys
from pathlib import Path
args, fails = sys.argv[1:], os.environ.get("FAKE_UV_FAILS", "")
with open({str(fakes / "calls")!r}, "a", encoding="utf-8") as calls:
    calls.write(" ".join(args) + "\\n")
if args[0] == fails:
    sys.exit("uv: it didn't work")
python = Path(args[-1]) / "bin" / "python"
if args[0] == "venv" and not python.exists():
    python.parent.mkdir(parents=True)
    broken = 'case "$2" in import\\ handoff*) echo "ImportError: broken" >&2; exit 1;; esac\\n'
    python.write_text("#!/bin/sh\\n" + (broken if fails == "import" else "") + 'exec "{sys.executable}" "$@"\\n')
    python.chmod(0o755)
""", encoding="utf-8")
    (fakes / "uv").chmod(0o755)
    env = {**os.environ, "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}", "HANDOFF_USE_UV": "1",
           "HANDOFF_PYTHON": sys.executable, "HANDOFF_INSTALL_ROOT": str(root), "HANDOFF_BIN_DIR": str(bin_dir),
           "HANDOFF_SKIP_PATH_UPDATE": "1"}

    def install(**extra):  # from this checkout, so that Python finds Handoff whether or not it's installed
        return subprocess.run(["bash", str(checkout / "install.sh")], env={**env, **extra}, capture_output=True,
                              text=True, cwd=Path(__file__).resolve().parents[1])
    return checkout, root, bin_dir, install


@pytest.mark.skipif(os.name == "nt", reason="runs install.sh")
def test_install_sh_records_the_commit_it_installed_once_it_all_worked(install_sh):
    checkout, root, bin_dir, install = install_sh
    _venv_python(root / ".venv" / "bin" / "python")  # the environment it would make: this Python, with Handoff
    done = install()
    assert done.returncode == 0, done.stderr
    assert "Next:    cd into a project, then run: handoff setup --write" in done.stdout
    first = git(checkout, "rev-parse", "HEAD")
    assert json.loads((root / "install.json").read_text(encoding="utf-8")) == {
        "source": str(checkout), "install_root": str(root), "bin_dir": str(bin_dir), "installer": "install.sh",
        "commit": first, "python": sys.executable}  # the Python you chose, for handoff update
    commit(checkout, "two")
    (bin_dir / "handoff").unlink()
    (bin_dir / "handoff").mkdir()  # so writing the command fails, after the environment was updated
    assert install().returncode != 0
    assert json.loads((root / "install.json").read_text(encoding="utf-8"))["commit"] == first


@pytest.mark.skipif(os.name == "nt", reason="runs install.sh")
def test_install_sh_remakes_an_environment_made_with_another_python(install_sh):
    checkout, root, bin_dir, install = install_sh
    venv_python = root / ".venv" / "bin" / "python"
    _venv_python(venv_python, 'case "$2" in *base_prefix*) echo "3.9.0 /some/other/python"; exit 0;; esac')
    (root / ".venv" / "marker").write_text("", encoding="utf-8")
    done = install()
    assert done.returncode == 0, done.stderr
    assert f"Making a new environment with {sys.executable} (the last install used another Python)" in done.stdout
    assert not (root / ".venv" / "marker").exists() and "other" not in venv_python.read_text(encoding="utf-8")
    assert [p.name for p in root.iterdir() if p.name.startswith(".venv")] == [".venv"]  # the old one is gone
    (root / ".venv" / "marker").write_text("", encoding="utf-8")
    done = install()  # the same Python again: the environment stays
    assert "Making a new environment" not in done.stdout and (root / ".venv" / "marker").exists()
    assert "in the environment from the last install" in done.stdout
    done = install(HANDOFF_PYTHON="")  # no choice now: the one you made last time still holds
    assert json.loads((root / "install.json").read_text(encoding="utf-8"))["python"] == sys.executable


@pytest.mark.skipif(os.name == "nt", reason="runs install.sh")
def test_install_sh_keeps_the_environment_s_python_unless_you_choose_another(install_sh):
    """An install from before install.json kept the Python (so handoff update passes none) keeps its own."""
    checkout, root, bin_dir, install = install_sh
    venv_python = root / ".venv" / "bin" / "python"
    _venv_python(venv_python, 'case "$2" in *base_prefix*) echo "3.12.0 /the/one/you/chose"; exit 0;; esac')
    (root / ".venv" / "marker").write_text("", encoding="utf-8")
    done = install(HANDOFF_PYTHON="")
    assert done.returncode == 0, done.stderr
    assert "Making a new environment" not in done.stdout and (root / ".venv" / "marker").exists()
    assert "/the/one/you/chose" in venv_python.read_text(encoding="utf-8")
    calls = (root.parent / "fakes" / "calls").read_text(encoding="utf-8").splitlines()
    assert [c.split()[0] for c in calls] == ["pip"]  # nothing made over it with another Python
    assert "python" not in json.loads((root / "install.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(os.name == "nt", reason="runs install.sh")
@pytest.mark.parametrize("step", ["venv", "pip", "import", "no venv module"])
def test_a_new_environment_that_fails_leaves_the_last_one_working(install_sh, step):
    """No python3-venv, no network, a library that doesn't load: Handoff (and handoff update) still work."""
    checkout, root, bin_dir, install = install_sh
    venv_python = root / ".venv" / "bin" / "python"
    _venv_python(venv_python, 'case "$2" in *base_prefix*) echo "3.9.0 /some/other/python"; exit 0;; esac')
    (root / ".venv" / "marker").write_text("", encoding="utf-8")
    without_venv = root.parent / "fakes" / "python-without-venv"
    _venv_python(without_venv, '[ "$1 $2" = "-m venv" ] && exit 1')  # Debian without python3-venv
    failed = install(FAKE_UV_FAILS=step) if step != "no venv module" else install(
        HANDOFF_USE_UV="0", HANDOFF_PYTHON=str(without_venv))
    assert failed.returncode != 0
    assert "Put the last install's environment back, so Handoff works as it did before." in failed.stderr
    assert (root / ".venv" / "marker").exists() and "other" in venv_python.read_text(encoding="utf-8")
    assert [p.name for p in root.iterdir() if p.name.startswith(".venv")] == [".venv"]
    assert install().returncode == 0  # and it can be made later


@pytest.mark.skipif(os.name == "nt", reason="runs install.sh")
def test_install_sh_shows_why_handoff_did_not_load(install_sh):
    checkout, root, bin_dir, install = install_sh
    _venv_python(root / ".venv" / "bin" / "python", 'case "$2" in import\\ handoff*) '
                 'printf "Traceback (most recent call last):\\nImportError: no module named pydantic_core\\n" >&2; '
                 'exit 1;; esac')
    failed = install()
    assert failed.returncode == 1 and "didn't finish installing" in failed.stderr
    assert "  ImportError: no module named pydantic_core" in failed.stderr
    assert f'bash "{checkout}/install.sh"' in failed.stderr
    assert not (root / "install.json").exists()


@pytest.mark.skipif(os.name == "nt" or any(os.path.exists(p) for p in ("/opt/homebrew/bin/python3",
                                                                         "/usr/local/bin/python3")),
                    reason="runs install.sh with only the Pythons it's given")
@pytest.mark.parametrize("version", ["Python 3.14.0rc1", ""])
def test_install_sh_says_a_python_isn_t_usable_without_calling_it_old(tmp_path, version):
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "python3").write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "{version}" && exit 0\nexit 1\n',
                                   encoding="utf-8")
    (fakes / "python3").chmod(0o755)
    (fakes / "uname").symlink_to(shutil.which("uname"))
    env = {"PATH": str(fakes), "HOME": str(tmp_path)}
    failed = subprocess.run(["/bin/bash", str(Path(__file__).resolve().parents[1] / "install.sh")], env=env,
                            capture_output=True, text=True, cwd=tmp_path)
    assert failed.returncode == 1 and "too old" not in failed.stderr.replace("(too old, or a pre-release)", "")
    if version:
        assert f"Found {version} at {fakes}/python3, which isn't usable (too old, or a pre-release)." in failed.stderr
    else:
        assert "Found" not in failed.stderr


def test_update_reuses_the_python_you_installed_with(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(proc, "WINDOWS", False)
    monkeypatch.setattr(update, "_program", lambda name: name)  # install.sh's bash, which Windows may not have
    monkeypatch.setattr(update.subprocess, "run", lambda command, env: seen.append(env.get("HANDOFF_PYTHON"))
                        or subprocess.CompletedProcess(command, 0))
    monkeypatch.delenv("HANDOFF_PYTHON", raising=False)
    info = {"source": str(tmp_path), "python": sys.executable}
    said = []
    update._reinstall(info, said.append)
    monkeypatch.setenv("HANDOFF_PYTHON", "/your/choice/now")
    update._reinstall(info, said.append)
    monkeypatch.delenv("HANDOFF_PYTHON")
    update._reinstall({**info, "python": str(tmp_path / "gone" / "python3")}, said.append)
    assert seen == [sys.executable, "/your/choice/now", None]
    assert "isn't there any more, so the installer picks one" in said[-2]


def test_install_ps1_records_the_commit_it_installed_once_it_all_worked():
    script = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8")
    record = script.index("Invoke-Checked 'Recording the install'")
    for step in ("$ImportError = & $VenvPython -c 'import handoff.cli", "Copy-Item -Force $HandoffExe $BinExe",
                 "\nEnsure-UserPath $BinDir"):
        assert script.index(step) < record, step
    line = next(line for line in script[record:].splitlines() if "json.dump" in line)
    assert "commit=" in line and "$Commit" in line and '"' not in line  # Windows PowerShell 5.1 drops "
    # never an empty argument (5.1 drops those too): each of the last two says what it is
    assert line.endswith("('commit=' + $Commit) ('python=' + $ChosenPython)")
    assert "rev-parse HEAD" in script


def test_install_ps1_keeps_the_python_you_choose():
    script = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8")
    remake = script[script.index("$VenvWorks = "):script.index("if ($VenvWorks) {")]
    assert "$env:HANDOFF_PYTHON -and (Get-PythonId @($VenvPython)) -ne (Get-PythonId $Python)" in remake
    # moved aside first: Windows refuses that while an app runs Handoff from it, and nothing is half deleted
    assert "Move-Item -Path $VenvDir -Destination $Aside -ErrorAction Stop" in remake
    assert "$VenvWorks = $false" in remake and "Remove-Item" not in remake
    # deleted only once the new one works; otherwise (an error, Ctrl+C) it's put back
    build = script[script.index("$Works = $false\ntry {"):script.index("# A copy an app is running")]
    proven = build.index("$Works = $true\n} finally {")
    assert build.index("$ImportError = & $VenvPython -c 'import handoff.cli") < proven
    put_back = build[proven:]
    assert "if ($OldVenv -and $Works) {\n        Remove-Item -Recurse -Force $OldVenv" in put_back
    assert "Move-Item -Path $OldVenv -Destination $VenvDir" in put_back
    assert "isn't usable (too old, a pre-release, or it doesn't run)" in script
    assert "didn't finish installing" in script and "Join-Path $SourceDir 'install.ps1'" in script
    assert 'Write-Host "Next:    cd into a project, then run: handoff setup --write"' in script


def test_install_ps1_runs_the_signed_python_where_smart_app_control_is_on():
    """Smart App Control blocks pip's unsigned handoff.exe; python.exe is signed."""
    script = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8")
    assert script.isascii()  # Windows PowerShell 5.1 reads a .ps1 with no BOM in the ANSI code page
    assert "'VerifiedAndReputablePolicyState'" in script and "return $state -eq 1" in script
    choose = script[script.index("if (Test-Path $BinExe) {"):script.index("Ensure-UserPath $BinDir")]
    line = next(ln.strip() for ln in choose.splitlines() if ln.strip().startswith("Set-Content -Path $BinCmd"))
    assert line == ('Set-Content -Path $BinCmd -Encoding ASCII -Value '
                    '"@echo off`r`n`"$WrapperPython`" -I -m handoff %*"')
    assert "$WrapperPython = $VenvPython" in choose and "Copy-Item -Force $HandoffExe $BinExe" in choose
    # Whichever it writes, the other one goes, so `handoff` in a terminal is the one just installed
    assert "Remove-Item -Force $BinCmd" in choose and choose.index("Move-Item -Force $BinExe $Stale") < choose.index(
        "Set-Content -Path $BinCmd")


def test_a_copy_the_installer_did_not_make_says_how_to_update(tmp_path):
    with pytest.raises(update.UpdateError, match="Run the installer from a fresh clone once"):
        update.update(info=None)


def test_check_counts_the_new_changes(installed):
    info, source, publish = installed
    assert run(info, check_only=True)[1] == ["Handoff is up to date."]
    publish("two")
    publish("three")
    code, said, reinstalled = run(info, check_only=True)
    assert code == 0 and said == ["2 new changes available. Run: handoff update"] and not reinstalled


def test_update_pulls_then_reinstalls(installed):
    info, source, publish = installed
    publish("two")
    code, said, reinstalled = run(info)
    assert code == 0 and reinstalled == [info]
    assert (source / "two").exists()


def test_nothing_new_means_no_reinstall_unless_forced(installed):
    info, _, _ = installed
    code, said, reinstalled = run(info)
    assert code == 0 and said[-1] == "Handoff is already up to date." and not reinstalled
    assert run(info, force=True)[2] == [info]


@pytest.mark.parametrize("recorded,reinstalls", [("head", False), ("older", True), (None, False), ("", False)])
def test_a_reinstall_that_failed_runs_again(installed, recorded, reinstalls):
    """The installers record the commit they installed, as their last step. So when an earlier update pulled
    but its reinstall failed, the next one reinstalls even though the pull brings nothing new. Without a
    record (an install from before they kept one), only a pull that brings something reinstalls."""
    info, source, publish = installed
    older = git(source, "rev-parse", "HEAD")
    publish("two")
    git(source, "pull", "-q", "--ff-only")  # the earlier update's pull
    head = git(source, "rev-parse", "HEAD")
    if recorded is not None:
        info = {**info, "commit": {"head": head, "older": older}.get(recorded, recorded)}
    code, said, reinstalled = run(info)
    assert code == 0 and reinstalled == ([info] if reinstalls else [])
    if not reinstalls:
        assert said[-1] == "Handoff is already up to date."


@pytest.mark.parametrize("recorded,pending", [("head", False), ("older", True), (None, False)])
def test_check_says_when_what_was_pulled_is_not_installed_yet(installed, recorded, pending):
    """After an update that pulled but didn't finish installing, --check says an update is waiting, as update
    would install it."""
    info, source, publish = installed
    older = git(source, "rev-parse", "HEAD")
    publish("two")
    git(source, "pull", "-q", "--ff-only")  # the earlier update's pull
    if recorded is not None:
        info = {**info, "commit": {"head": git(source, "rev-parse", "HEAD"), "older": older}[recorded]}
    code, said, reinstalled = run(info, check_only=True)
    assert code == 0 and not reinstalled
    assert said == ["An update is downloaded but not installed yet. Run: handoff update" if pending
                    else "Handoff is up to date."]
    assert run(info)[2] == ([info] if pending else [])


def test_local_changes_are_never_overwritten(installed):
    info, source, publish = installed
    publish("two")
    (source / "one").write_text("my edit", encoding="utf-8")
    with pytest.raises(update.UpdateError, match=r"has local changes. Put them aside with: git -C .* stash"):
        run(info)
    assert not (source / "two").exists()


def test_a_checkout_that_is_gone_is_explained(installed, tmp_path):
    info, _, _ = installed
    with pytest.raises(update.UpdateError, match="isn't a git checkout any more"):
        run({**info, "source": str(tmp_path / "gone")})


def test_the_command_reports_a_problem_and_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(update, "INSTALL_INFO", tmp_path / "install.json")
    assert cli.main(["update"]) == 1
    assert "install.sh or install.ps1" in capsys.readouterr().err


# ── The programs update runs ─────────────────────────────────────────────────

def _program(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\ntouch ran-from-the-project\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH rules")
def test_update_never_runs_a_program_from_the_current_folder(installed, tmp_path, monkeypatch):
    """handoff update is usually run from inside a project, and its programs come from PATH alone."""
    info, source, publish = installed
    publish("two")
    project = tmp_path / "cloned-project"
    _program(project, "git")
    _program(project / "bin", "git")
    monkeypatch.chdir(project)
    # On POSIX, "" and "." on PATH mean the current folder (Windows looks there first anyway)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin", os.path.dirname(shutil.which("git"))]))
    code, said, reinstalled = run(info)
    assert code == 0 and reinstalled == [info] and (source / "two").exists()
    assert not (project / "ran-from-the-project").exists()


def test_programs_come_from_path_never_the_current_folder(tmp_path, monkeypatch):
    project, tools = tmp_path / "cloned-project", tmp_path / "tools"
    for folder in (project, project / "bin", tools):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "git.exe").write_text("", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setattr(proc, "WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin", f'"{tools}"']))
    assert proc.find_on_path("git") == str(tools / "git.exe")
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin"]))
    assert proc.find_on_path("git") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_on_posix_only_programs_count(tmp_path, monkeypatch):
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "git").write_text("not a program", encoding="utf-8")
    real = _program(tmp_path / "bin", "git")
    monkeypatch.setattr(proc, "WINDOWS", False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "plain"), str(tmp_path / "bin")]))
    assert proc.find_on_path("git") == str(real)


def test_on_windows_only_what_windows_can_start_counts(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "git.py").write_text("", encoding="utf-8")  # python.org adds .PY to PATHEXT
    (second / "git.exe").write_text("", encoding="utf-8")
    (second / "bash.cmd").write_text("", encoding="utf-8")
    monkeypatch.setattr(proc, "WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.PY;.PYW")
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))
    assert proc.find_on_path("git") == str(second / "git.exe")
    assert proc.find_on_path("git.exe") == str(second / "git.exe")
    assert proc.find_on_path("bash") == str(second / "bash.cmd")
    assert proc.find_on_path("powershell") is None


def _installer_run(monkeypatch, returncode=0):
    ran = []

    def fake_run(command, env):
        ran.append(command)
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    return ran


def _windows(tmp_path, monkeypatch) -> Path:
    """Windows, as far as update can tell, with Windows PowerShell where Windows keeps it."""
    builtin = tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    builtin.parent.mkdir(parents=True)
    builtin.write_text("", encoding="utf-8")
    monkeypatch.setattr(proc, "WINDOWS", True)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    return builtin


def test_on_windows_the_installer_runs_in_windows_powershell(tmp_path, monkeypatch):
    builtin = _windows(tmp_path, monkeypatch)
    on_path = tmp_path / "bin" / "powershell.exe"
    on_path.parent.mkdir()
    on_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(on_path.parent))
    ran = _installer_run(monkeypatch)
    info = {"source": str(tmp_path / "source")}
    assert update._reinstall(info, say=lambda line: None) == 0
    assert ran[-1][0] == str(builtin) and ran[-1][-1] == str(tmp_path / "source" / "install.ps1")
    builtin.unlink()  # not where Windows keeps it: the one on PATH
    update._reinstall(info, say=lambda line: None)
    assert ran[-1][0] == str(on_path)
    on_path.unlink()
    with pytest.raises(update.UpdateError, match="Windows PowerShell isn't installed"):
        update._reinstall(info, say=lambda line: None)
    assert len(ran) == 2


@pytest.mark.parametrize("case", ["pulled", "forced", "unrecorded"])
def test_the_advice_after_a_failed_reinstall_works(installed, tmp_path, monkeypatch, case):
    """On Windows a failed reinstall says how to finish it; doing what it says runs the installer again: after
    an update that pulled something, after --force with the latest installed already, and for an install from
    before the installers recorded their commit."""
    info, source, publish = installed
    if case != "unrecorded":
        info = {**info, "commit": git(source, "rev-parse", "HEAD")}  # the failed install doesn't record its own
    if case != "forced":
        publish("two")
    said = []

    def fails_on_windows(info, say):
        with monkeypatch.context() as windows:
            _windows(tmp_path, windows)
            _installer_run(windows, returncode=1)
            return update._reinstall(info, say)

    assert update.update(force=case == "forced", say=said.append, info=info, reinstall=fails_on_windows) == 1
    advice = said[-1].rsplit(" and run ", 1)[1]
    assert advice in ("handoff update again.", "handoff update --force.")
    assert run(info, force=advice == "handoff update --force.")[2] == [info]


@pytest.mark.parametrize("name", ["git", "bash"])
def test_a_missing_program_is_named(installed, monkeypatch, name):
    info, _, publish = installed
    publish("two")
    found = {"git": proc.find_on_path("git"), "bash": "/bin/bash"}
    monkeypatch.setattr(proc, "find_on_path", lambda program: None if program == name else found[program])
    monkeypatch.setattr(proc, "WINDOWS", False)
    with pytest.raises(update.UpdateError, match=f"Can't update: {name} isn't installed"):
        update.update(say=lambda line: None, info=info)
