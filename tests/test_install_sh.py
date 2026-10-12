"""install.sh's offer of bubblewrap and socat on Linux. The function is cut out of install.sh and run under
/bin/sh, on a PATH of fake uname, sudo, bwrap and socat and with a made-up os-release, so nothing depends on
the machine the tests run on. Checked: the command printed for each distro, that nothing is installed unless
you're at a terminal and say yes, and that it never fails an install that worked."""
import os
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"
BOTH = "sudo apt install bubblewrap socat"
shell_only = pytest.mark.skipif(os.name == "nt", reason="runs install.sh's shell")

DISTROS = [
    ("ubuntu", "debian", "sudo apt install bubblewrap socat"),
    ("debian", "", "sudo apt install bubblewrap socat"),
    ("linuxmint", "ubuntu debian", "sudo apt install bubblewrap socat"),
    ("fedora", "", "sudo dnf install bubblewrap socat"),
    ("rhel", "fedora", "sudo dnf install bubblewrap socat"),
    ("centos", "rhel fedora", "sudo dnf install bubblewrap socat"),
    ("arch", "", "sudo pacman -S bubblewrap socat"),
    ("manjaro", "arch", "sudo pacman -S bubblewrap socat"),
    ("opensuse-leap", "suse opensuse", "sudo zypper install bubblewrap socat"),
    ("opensuse-tumbleweed", "", "sudo zypper install bubblewrap socat"),
    ("sles", "suse", "sudo zypper install bubblewrap socat"),
    ("alpine", "", "sudo apk add bubblewrap socat"),
]


def _function() -> str:
    """The offer_sandbox_tools function, as written in install.sh."""
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    start = lines.index("offer_sandbox_tools() {")
    return "\n".join(lines[start:lines.index("}", start) + 1]) + "\n"


def _at_a_terminal(argv, env, typed):
    """Runs argv with a terminal (a pty) as its stdin, where `typed` has already been typed."""
    import pty
    master, slave = pty.openpty()
    try:
        os.write(master, typed.encode("ascii"))
        return subprocess.run(argv, env=env, stdin=slave, capture_output=True, text=True, encoding="utf-8",
                              timeout=30)
    finally:
        os.close(master)
        os.close(slave)


@pytest.fixture
def offer(tmp_path):
    """Runs the function on a made-up machine and returns (the finished process, what sudo was asked to run).
    By default stdin is /dev/null; `typed` makes it a terminal with that typed, `piped` a pipe with that in it."""
    fakes, log, script = tmp_path / "fakes", tmp_path / "sudo.log", tmp_path / "run.sh"
    release, userns_file = tmp_path / "os-release", tmp_path / "userns"
    fakes.mkdir()
    script.write_bytes(("set -eu\n" + _function() + 'offer_sandbox_tools "$1" "$2"\n').encode("ascii"))

    def program(name, body):
        (fakes / name).write_bytes(f"#!/bin/sh\n{body}\n".encode("ascii"))
        (fakes / name).chmod(0o755)

    def run(distro="ubuntu", like="debian", have=(), kernel="Linux", userns=None, typed=None, piped=None,
            sudo_fails=False, **env):
        program("uname", f"echo {kernel}")
        program("sudo", f'echo "$*" >> "{log}"' + ("\nexit 1" if sudo_fails else ""))
        for name in have:
            program(name, "exit 0")
        if distro is None:
            release.unlink(missing_ok=True)
        else:
            text = f'NAME="Some Linux"\nID={distro}\n' + (f'ID_LIKE="{like}"\n' if like else "") + 'VERSION_ID="1"\n'
            release.write_bytes(text.encode("ascii"))
        if userns is None:
            userns_file.unlink(missing_ok=True)
        else:
            userns_file.write_bytes(userns.encode("ascii"))
        argv = ["/bin/sh", str(script), str(release), str(userns_file)]
        env = {"PATH": str(fakes), "HOME": str(tmp_path), **env}
        if typed is not None:
            done = _at_a_terminal(argv, env, typed)
        elif piped is not None:
            done = subprocess.run(argv, env=env, input=piped, capture_output=True, text=True, encoding="utf-8",
                                  timeout=30)
        else:
            done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", timeout=30)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return done, calls
    return run


@shell_only
@pytest.mark.parametrize("distro, like, command", DISTROS)
def test_each_distro_is_told_its_own_install_command(offer, distro, like, command):
    done, calls = offer(distro, like)
    assert done.returncode == 0, done.stderr
    assert f"  Install them with:  {command}\n" in done.stdout
    assert "[y/N]" not in done.stdout and calls == []  # nobody to ask: it only prints


@shell_only
@pytest.mark.parametrize("have, line, left_out", [(("bwrap",), "sudo apt install socat\n", "bubblewrap"),
                                                  (("socat",), "sudo apt install bubblewrap\n", "socat")])
def test_only_what_is_missing_is_named(offer, have, line, left_out):
    done, calls = offer(have=have)
    assert done.returncode == 0, done.stderr
    assert line in done.stdout and left_out not in done.stdout
    assert calls == []


@shell_only
def test_nothing_is_said_when_both_are_there(offer):
    done, calls = offer(have=("bwrap", "socat"), userns="1\n")
    assert (done.returncode, done.stdout, done.stderr, calls) == (0, "", "", [])


@shell_only
def test_it_does_nothing_off_linux(offer):
    done, calls = offer(kernel="Darwin", typed="y\n")
    assert (done.returncode, done.stdout, done.stderr, calls) == (0, "", "", [])


@shell_only
@pytest.mark.parametrize("distro", ["gentoo", None])  # a distro it has no command for; no os-release at all
def test_an_unknown_distro_gets_a_generic_line_and_no_question(offer, distro):
    done, calls = offer(distro, like="", typed="y\n")
    assert done.returncode == 0, done.stderr
    assert "Install bubblewrap and socat with your system's package manager." in done.stdout
    assert "sudo" not in done.stdout and "[y/N]" not in done.stdout and calls == []


@shell_only
@pytest.mark.parametrize("userns, said", [("1\n", True), ("0\n", False), (None, False)])
def test_ubuntu_2404s_apparmor_is_pointed_at_handoff_doctor(offer, userns, said):
    done, calls = offer(userns=userns)
    assert done.returncode == 0, done.stderr
    assert ("AppArmor" in done.stdout and "handoff doctor" in done.stdout) is said
    assert BOTH in done.stdout and calls == []


@shell_only
@pytest.mark.parametrize("how", ["piped", "switched off"])
def test_nothing_runs_without_a_terminal_or_when_the_question_is_off(offer, how):
    if how == "piped":  # curl ... | sh: stdin is the script, even with a y in it
        done, calls = offer(piped="y\n")
    else:
        done, calls = offer(typed="y\n", HANDOFF_INSTALL_YES="0")
    assert done.returncode == 0, done.stderr
    assert BOTH in done.stdout
    assert "[y/N]" not in done.stdout and calls == []


@shell_only
@pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", "Yes\n", "YES\n"])
def test_yes_at_a_terminal_runs_the_command(offer, answer):
    done, calls = offer(typed=answer)
    assert done.returncode == 0, done.stderr
    assert "Install them now? [y/N]" in done.stdout
    assert calls == ["apt install bubblewrap socat"]


@shell_only
@pytest.mark.parametrize("answer", ["\n", "n\n", "no\n", "yess\n", "sure\n", "\x04"])  # \x04: Ctrl-D, no answer
def test_anything_but_yes_at_a_terminal_runs_nothing(offer, answer):
    done, calls = offer(typed=answer)
    assert done.returncode == 0, done.stderr
    assert "Install them now? [y/N]" in done.stdout and "Skipped." in done.stdout
    assert BOTH in done.stdout and calls == []


@shell_only
def test_a_command_that_fails_is_said_and_does_not_fail_the_install(offer):
    done, calls = offer(typed="y\n", sudo_fails=True)
    assert done.returncode == 0, done.stderr
    assert calls == ["apt install bubblewrap socat"]
    assert "didn't finish" in done.stdout
    assert done.stdout.count(BOTH) == 2  # shown once, and again for you to run


@shell_only
def test_the_function_is_plain_sh_and_runs_last():
    code = "\n".join(line for line in _function().splitlines() if not line.lstrip().startswith("#"))
    for bashism in ("[[", "local ", "==", "=~", "<<<", "$'", "declare "):
        assert bashism not in code, bashism
    assert INSTALL_SH.read_text(encoding="utf-8").rstrip().splitlines()[-1] == "offer_sandbox_tools"
