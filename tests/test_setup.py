"""`handoff setup`: config merges that are idempotent and keep everything else, against real-looking configs."""
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from handoff import hosts
from handoff.hosts import SetupError, merge_codex, merge_desktop

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

CODEX_SAMPLE = textwrap.dedent('''\
    # My Codex settings
    model = "example-model"
    approval_policy = "on-request"
    sandbox_mode = "workspace-write"

    [sandbox_workspace_write]
    network_access = false

    [mcp_servers.github]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-github"]

    [mcp_servers.github.env]
    GITHUB_TOKEN_ENV = "GH_TOKEN"

    [profiles.fast]
    model = "example-mini"
    notes = """
    Be brief.
    [mcp_servers.handoff]
    """
''')

CODEX_WITH_OLD_ENTRY = textwrap.dedent('''\
    model = "example-model"

    [mcp_servers.handoff]
    command = "/old/venv/bin/handoff"
    args = [
      "mcp",
      "--as", "codex",
    ]
    startup_timeout_sec = 20   # mine: keep it
    tool_timeout_sec = 60

    [mcp_servers.handoff.env]
    FOO = "bar"

    [mcp_servers.other]
    command = "other"
''')

DESKTOP_SAMPLE = json.dumps({
    "mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem",
                                                            "/Users/me/Desktop"]}},
    "globalShortcut": "Ctrl+Space",
}, indent=2)

LINES = ['command = "/opt/handoff/.venv/bin/handoff"', 'args = ["mcp", "--as", "codex"]', "tool_timeout_sec = 960"]


# ── Codex ────────────────────────────────────────────────────────────────────

def test_codex_added_to_a_real_config_keeping_everything(tmp_path):
    new = merge_codex(CODEX_SAMPLE, LINES, tmp_path / "config.toml")
    assert new.startswith(CODEX_SAMPLE)  # appended; the rest untouched, comments and all
    before, after = tomllib.loads(CODEX_SAMPLE), tomllib.loads(new)
    assert after["mcp_servers"]["handoff"] == {"command": "/opt/handoff/.venv/bin/handoff",
                                               "args": ["mcp", "--as", "codex"], "tool_timeout_sec": 960}
    after["mcp_servers"].pop("handoff")
    assert after == before
    assert merge_codex(new, LINES, tmp_path / "config.toml") == new  # idempotent


def test_codex_update_replaces_only_our_keys(tmp_path):
    new = merge_codex(CODEX_WITH_OLD_ENTRY, LINES, tmp_path / "config.toml")
    data = tomllib.loads(new)
    assert data["mcp_servers"]["handoff"] == {"command": "/opt/handoff/.venv/bin/handoff",
                                              "args": ["mcp", "--as", "codex"], "tool_timeout_sec": 960,
                                              "startup_timeout_sec": 20, "env": {"FOO": "bar"}}
    assert data["mcp_servers"]["other"] == {"command": "other"} and data["model"] == "example-model"
    assert "# mine: keep it" in new and "/old/venv" not in new
    assert merge_codex(new, LINES, tmp_path / "config.toml") == new


def test_codex_new_file(tmp_path):
    new = merge_codex(None, LINES, tmp_path / "config.toml")
    assert tomllib.loads(new) == {"mcp_servers": {"handoff": {"command": "/opt/handoff/.venv/bin/handoff",
                                                              "args": ["mcp", "--as", "codex"],
                                                              "tool_timeout_sec": 960}}}


@pytest.mark.parametrize("text,message", [
    ('model = "x"\n[mcp_servers\n', "isn't valid TOML"),
    ('[mcp_servers]\nhandoff = { command = "mine" }\n', "Couldn't safely edit"),
    ('mcp_servers.handoff.command = "mine"\n', "Couldn't safely edit"),
])
def test_codex_refuses_what_it_cannot_edit_safely(tmp_path, text, message):
    with pytest.raises(SetupError, match=message):
        merge_codex(text, LINES, tmp_path / "config.toml")


def test_windows_apps_start_the_signed_python_not_handoff_exe(monkeypatch):
    # Smart App Control blocks pip's unsigned handoff.exe, and an update has to replace it while apps hold it open
    monkeypatch.setattr(hosts.sys, "platform", "win32")
    monkeypatch.setattr(hosts.sys, "prefix", "C:\\Users\\Ada\\AppData\\Local\\Handoff\\.venv")
    monkeypatch.setattr(hosts.sys, "base_prefix", "C:\\Python313")
    command, args = hosts.server_args("codex")
    assert command == hosts.sys.executable and args == ["-I", "-m", "handoff", "mcp", "--as", "codex"]


def test_installed_apps_are_found_on_path_only(tmp_path, monkeypatch):
    planted = tmp_path / "claude"
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", "."]))
    assert not hosts.installed("claude-code")
    from handoff import ixel
    (tmp_path / "ixel").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "ixel").chmod(0o755)
    assert ixel.find_ixel() is None


def test_codex_windows_paths_are_escaped():
    command = "C:\\Users\\Ada Lovelace\\AppData\\Local\\Handoff\\.venv\\Scripts\\handoff.exe"
    line = f"command = {hosts._toml_string(command)}"
    assert tomllib.loads(line)["command"] == command


# ── Claude Desktop ───────────────────────────────────────────────────────────

def test_desktop_added_keeping_other_servers_and_settings(tmp_path):
    entry = {"command": "/opt/handoff", "args": ["mcp", "--as", "claude", "--project", "/work/app"]}
    new = merge_desktop(DESKTOP_SAMPLE, entry, tmp_path / "c.json")
    data = json.loads(new)
    assert data["mcpServers"]["handoff"] == entry
    assert data["mcpServers"]["filesystem"] == json.loads(DESKTOP_SAMPLE)["mcpServers"]["filesystem"]
    assert data["globalShortcut"] == "Ctrl+Space"
    assert merge_desktop(new, entry, tmp_path / "c.json") == new


@pytest.mark.parametrize("text,message", [
    ("{ not json", "isn't valid JSON"),
    ("[]", "doesn't hold a JSON object"),
    ('{"mcpServers": []}', "isn't an object"),
])
def test_desktop_refuses_broken_configs(tmp_path, text, message):
    with pytest.raises(SetupError, match=message):
        merge_desktop(text, {}, tmp_path / "c.json")


# ── Writing for real, in a fake home ─────────────────────────────────────────

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(hosts, "home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    return home


def test_write_codex_and_desktop_twice(fake_home, tmp_path):
    codex = fake_home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(CODEX_SAMPLE, encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(codex, 0o640)
    desktop = hosts.desktop_config_path()
    desktop.parent.mkdir(parents=True)
    desktop.write_text(DESKTOP_SAMPLE, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    assert hosts.write("codex", project).status == "added"
    assert hosts.write("claude-desktop", project).status == "added"
    assert hosts.write("codex", project).status == "unchanged"
    assert hosts.write("claude-desktop", project).status == "unchanged"

    data = tomllib.loads(codex.read_text(encoding="utf-8"))
    command, args = hosts.server_args("codex")
    assert data["mcp_servers"]["handoff"]["command"] == command and Path(command).is_absolute()
    assert data["mcp_servers"]["handoff"]["args"] == args and args[-2:] == ["--as", "codex"]
    assert data["mcp_servers"]["github"]["env"] == {"GITHUB_TOKEN_ENV": "GH_TOKEN"}
    if sys.platform != "win32":
        assert stat.S_IMODE(codex.stat().st_mode) == 0o640  # the file keeps its permissions
    entry = json.loads(desktop.read_text(encoding="utf-8"))["mcpServers"]["handoff"]
    assert entry["args"][-4:] == ["--as", "claude", "--project", str(project)]
    assert hosts.configured("codex")[0] and hosts.configured("claude-desktop")[0]


def test_claude_desktop_installed_as_an_app_package_gets_the_config_it_reads(fake_home, tmp_path, monkeypatch):
    """Anthropic's Windows installer makes Claude Desktop an app package, with its own copy of %APPDATA%:
    once it has a config file there, it ignores %APPDATA%\\Claude's."""
    monkeypatch.setattr(hosts.sys, "platform", "win32")
    plain = fake_home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    own = (fake_home / "AppData" / "Local" / "Packages" / hosts.CLAUDE_DESKTOP_PACKAGE / "LocalCache" / "Roaming"
           / "Claude" / "claude_desktop_config.json")
    assert not hosts.installed("claude-desktop") and hosts.desktop_config_path() == plain
    own.parent.mkdir(parents=True)  # installed and opened, with no config file yet: it reads its own first
    assert hosts.installed("claude-desktop") and hosts.desktop_config_path() == own
    plain.parent.mkdir(parents=True)
    plain.write_text(DESKTOP_SAMPLE, encoding="utf-8")  # from the app before packaging: it reads this while
    assert hosts.desktop_config_path() == plain         # it has none of its own
    own.write_text("{}\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    assert hosts.desktop_config_path() == own and hosts.write("claude-desktop", project).status == "added"
    assert "handoff" in json.loads(own.read_text(encoding="utf-8"))["mcpServers"]
    assert plain.read_text(encoding="utf-8") == DESKTOP_SAMPLE
    # --remove also takes out one an earlier Handoff left in %APPDATA%: the app reads it if its own copy goes
    old = json.loads(DESKTOP_SAMPLE)
    old.setdefault("mcpServers", {})["handoff"] = {"command": "old"}
    plain.write_text(json.dumps(old), encoding="utf-8")
    assert hosts.remove_desktop().status == "removed"
    assert all("handoff" not in json.loads(p.read_text(encoding="utf-8")).get("mcpServers", {}) for p in (own, plain))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_a_config_is_never_readable_by_others_even_while_it_is_written(fake_home, tmp_path, monkeypatch):
    seen = {}
    chmod = os.chmod

    def spy(path, mode, *args, **kwargs):  # how the new file was made, before setup sets its permissions
        seen[Path(path).name] = stat.S_IMODE(os.stat(path).st_mode)
        return chmod(path, mode, *args, **kwargs)
    monkeypatch.setattr(hosts.os, "chmod", spy)
    old_umask = os.umask(0)  # nothing taken away: a file made with open() would be 0o666
    try:
        outside = tmp_path / "outside.toml"
        outside.write_text("mine\n", encoding="utf-8")
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / ".config.toml.handoff-tmp").symlink_to(outside)  # a link where the temp file goes
        assert hosts.write("codex", None).status == "added"
    finally:
        os.umask(old_umask)
    assert seen == {".config.toml.handoff-tmp": 0o600}
    assert outside.read_text(encoding="utf-8") == "mine\n"  # never written through the link
    config = fake_home / ".codex" / "config.toml"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert "[mcp_servers.handoff]" in config.read_text(encoding="utf-8")


def test_a_longer_codex_timeout_you_set_is_kept(fake_home):
    from handoff import ixel
    assert hosts.CODEX_TOOL_TIMEOUT > ixel.TIMEOUT_SEC  # Codex mustn't give up on a panel review
    codex = fake_home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text("[mcp_servers.handoff]\ncommand = \"old\"\ntool_timeout_sec = 1800\n", encoding="utf-8")
    assert hosts.write("codex", None).status == "updated"
    assert tomllib.loads(codex.read_text(encoding="utf-8"))["mcp_servers"]["handoff"]["tool_timeout_sec"] == 1800
    assert hosts.write("codex", None).status == "unchanged"
    for shorter in ("60", "true", '"2000"'):  # shorter, or not a number: ours instead
        codex.write_text(f"[mcp_servers.handoff]\ntool_timeout_sec = {shorter}\n", encoding="utf-8")
        hosts.write("codex", None)
        server = tomllib.loads(codex.read_text(encoding="utf-8"))["mcp_servers"]["handoff"]
        assert server["tool_timeout_sec"] == hosts.CODEX_TOOL_TIMEOUT


def test_desktop_needs_a_project(fake_home):
    assert hosts.write("claude-desktop", None).status == "skipped"


def test_codex_home_is_honored(fake_home, tmp_path, monkeypatch):
    custom = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    hosts.write("codex", None)
    assert (custom / "config.toml").exists() and not (fake_home / ".codex").exists()


def _fake_claude(bin_dir: Path, home: Path, log: Path) -> None:
    """A stand-in `claude` CLI: logs its arguments and keeps ~/.claude.json like the real one."""
    script = bin_dir / "fake_claude.py"
    script.write_text(textwrap.dedent(f'''
        import json, sys
        from pathlib import Path
        args = sys.argv[1:]
        with open({str(log)!r}, "a", encoding="utf-8") as f:
            f.write(json.dumps(args) + "\\n")
        state = Path({str(home / ".claude.json")!r})
        data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {{}}
        servers = data.setdefault("mcpServers", {{}})
        if args[:2] == ["mcp", "remove"]:
            if servers.pop(args[-1], None) is None:
                sys.exit(1)
        elif args[:2] == ["mcp", "add"]:
            name = args[args.index("--") - 1]
            rest = args[args.index("--") + 1:]
            servers[name] = {{"type": "stdio", "command": rest[0], "args": rest[1:]}}
        state.write_text(json.dumps(data), encoding="utf-8")
    '''), encoding="utf-8")
    if sys.platform == "win32":
        (bin_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        wrapper = bin_dir / "claude"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)


def test_write_claude_code_uses_its_cli(fake_home, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude.log"
    _fake_claude(bin_dir, fake_home, log)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    assert hosts.write("claude-code", None).status == "added"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    command, args = hosts.server_args("claude")
    assert calls[-1] == ["mcp", "add", "--scope", "user", "handoff", "--", command, *args]
    assert "--project" not in calls[-1]  # Claude Code starts it in the project you open

    assert hosts.write("claude-code", None).status == "unchanged"
    assert len(log.read_text(encoding="utf-8").splitlines()) == len(calls)  # nothing run the second time
    assert hosts.configured("claude-code")[0]


def test_write_claude_code_without_claude(fake_home, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(SetupError, match="isn't on PATH"):
        hosts.write("claude-code", None)


# ── The command ──────────────────────────────────────────────────────────────

def _env(home: Path, **extra) -> dict:
    return {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "AppData"),
            "PYTHONIOENCODING": "utf-8", "COLUMNS": "200", **extra}


def _run(*args, home: Path, cwd: Path, **extra):
    return subprocess.run([sys.executable, "-m", "handoff", *args], env=_env(home, **extra), cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_setup_prints_snippets_with_absolute_paths(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    proc = _run("setup", home=tmp_path, cwd=project)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "claude mcp add --scope user handoff --" in out and "--as claude" in out
    assert "[mcp_servers.handoff]" in out and "tool_timeout_sec = 960" in out
    assert '"mcpServers"' in out and str(project.resolve()).replace("\\", "\\\\") in out
    assert os.path.dirname(sys.executable) in out or os.path.dirname(sys.executable).replace("\\", "\\\\") in out


def test_setup_outside_a_project_explains_desktop(tmp_path):
    proc = _run("setup", home=tmp_path, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "isn't inside a git repository" in proc.stdout
    assert "Claude Desktop: it has no project folder of its own" in proc.stdout


def test_setup_write_codex_from_the_command_line(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    codex_home = tmp_path / "codex"
    proc = _run("setup", "--write", "--host", "codex", home=tmp_path, cwd=project, CODEX_HOME=str(codex_home))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "Codex: added" in proc.stdout
    again = _run("setup", "--write", "--host", "codex", home=tmp_path, cwd=project, CODEX_HOME=str(codex_home))
    assert "Codex: unchanged" in again.stdout
    assert tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]["handoff"]


def test_setup_write_with_no_app_installed_says_so(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    proc = _run("setup", "--write", home=tmp_path, cwd=project, PATH=str(empty), CODEX_HOME=str(tmp_path / "none"))
    assert proc.returncode == 1, proc.stdout
    assert "None of the apps is installed, so nothing was set up" in proc.stdout
    assert "Then, in any of them" not in proc.stdout


def test_setup_write_reports_a_config_it_wont_touch(tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("not = [valid\n", encoding="utf-8")
    proc = _run("setup", "--write", "--host", "codex", home=tmp_path, cwd=tmp_path, CODEX_HOME=str(codex_home))
    assert proc.returncode == 1
    assert "isn't valid TOML" in proc.stdout
    assert (codex_home / "config.toml").read_text(encoding="utf-8") == "not = [valid\n"


# ── The Handoff plugin ───────────────────────────────────────────────────────

def test_the_plugin_counts_as_set_up(fake_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"other@x": True, "handoff@ixelai": True}}), encoding="utf-8")
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "config.toml").write_text('[plugins."handoff@ixelai"]\nenabled = true\n',
                                                     encoding="utf-8")
    assert hosts.plugin_enabled("claude-code") == "handoff@ixelai"
    assert hosts.configured("claude-code") == (True, "through the plugin (handoff@ixelai)")
    assert hosts.configured("codex") == (True, "through the plugin (handoff@ixelai)")
    assert hosts.plugin_enabled("claude-desktop") is None


def test_a_disabled_or_missing_plugin_is_not_set_up(fake_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"handoff@ixelai": False}}),
                                                       encoding="utf-8")
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "config.toml").write_text('[plugins."handoff@ixelai"]\nenabled = false\n',
                                                     encoding="utf-8")
    assert hosts.plugin_enabled("claude-code") is None and hosts.plugin_enabled("codex") is None
    assert hosts.configured("codex")[0] is False


def test_the_plugin_and_the_config_together_are_reported(fake_home):
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "config.toml").write_text(
        '[plugins."handoff@ixelai"]\nenabled = true\n\n[mcp_servers.handoff]\ncommand = "handoff"\n'
        'args = ["mcp", "--as", "codex"]\n', encoding="utf-8")
    ok, detail = hosts.configured("codex")
    assert ok and detail.startswith("through the plugin (handoff@ixelai) and also its config")
    assert hosts.in_config("codex")[0] is True


# ── Taking it out again ──────────────────────────────────────────────────────

def test_codex_entry_is_removed_and_nothing_else(tmp_path):
    path = tmp_path / "config.toml"
    added = merge_codex(CODEX_SAMPLE, LINES, path)
    assert hosts.unmerge_codex(added, path) == CODEX_SAMPLE  # back to exactly what it was
    removed = hosts.unmerge_codex(CODEX_WITH_OLD_ENTRY, path)  # with keys and a sub-table of yours
    data = tomllib.loads(removed)
    assert "handoff" not in data["mcp_servers"] and data["mcp_servers"]["other"] == {"command": "other"}
    assert data["model"] == "example-model" and "FOO" not in removed
    assert hosts.unmerge_codex(merge_codex(None, LINES, path), path) == ""
    with pytest.raises(SetupError, match="Remove the \\[mcp_servers.handoff\\] entry by hand"):
        hosts.unmerge_codex('mcp_servers.handoff.command = "mine"\n', path)


def test_setup_remove_takes_out_everything_and_is_safe_twice(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_claude(bin_dir, home, tmp_path / "claude.log")
    env = {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), "CODEX_HOME": str(home / ".codex")}
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(CODEX_SAMPLE, encoding="utf-8")
    desktop = home / ".config" / "Claude" / "claude_desktop_config.json"  # (on Linux)
    desktop.parent.mkdir(parents=True)
    desktop.write_text(DESKTOP_SAMPLE, encoding="utf-8")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"theme": "dark"}', encoding="utf-8")
    written = _run("setup", "--write", home=home, cwd=project, **env)
    assert written.returncode == 0, written.stdout + written.stderr
    assert "handoff" in json.loads((home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "session-start" in settings.read_text(encoding="utf-8")

    removed = _run("setup", "--remove", home=home, cwd=project, **env)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    for line in ("Claude Code: removed", "Claude Code session hook: removed", "Codex: removed"):
        assert line in removed.stdout, line
    if sys.platform.startswith("linux"):
        assert "Claude Desktop: removed" in removed.stdout
        assert json.loads(desktop.read_text(encoding="utf-8")) == json.loads(DESKTOP_SAMPLE)
    assert json.loads((home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"] == {}
    assert json.loads(settings.read_text(encoding="utf-8")) == {"theme": "dark"}
    assert codex.read_text(encoding="utf-8") == CODEX_SAMPLE
    assert "settings.json.handoff-bak" in removed.stdout  # the copy setup made, to delete when you like

    again = _run("setup", "--remove", home=home, cwd=project, **env)
    assert again.returncode == 0 and "removed" not in again.stdout
    for line in ("Claude Code: not there", "Claude Code session hook: not there", "Codex: not there",
                 "Claude Desktop: not there"):
        assert line in again.stdout, line


def test_setup_remove_without_claude_code_edits_its_file(fake_home, monkeypatch, tmp_path):
    state = fake_home / ".claude.json"
    state.write_text(json.dumps({"mcpServers": {"handoff": {"command": "x"}, "other": {"command": "y"}},
                                 "numStartups": 3}), encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # Claude Code itself is gone
    assert hosts.remove("claude-code").status == "removed"
    assert json.loads(state.read_text(encoding="utf-8")) == {"mcpServers": {"other": {"command": "y"}},
                                                              "numStartups": 3}
    assert hosts.remove("claude-code").status == "not there"


def test_setup_remove_goes_on_its_own(tmp_path):
    proc = _run("setup", "--remove", "--write", home=tmp_path, cwd=tmp_path)
    assert proc.returncode == 2 and "--remove goes on its own" in proc.stderr


# ── Gemini CLI, OpenCode and Grok Build ──────────────────────────────────────

GEMINI_SAMPLE = json.dumps({
    "theme": "Default Dark",
    "mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
                   "handoff": {"command": "/old/handoff", "args": ["mcp"], "trust": True, "timeout": 2_000_000}},
}, indent=2)

OPENCODE_SAMPLE = json.dumps({
    "$schema": "https://opencode.ai/config.json",
    "model": "xai/grok-code",
    "mcp": {"docs": {"type": "remote", "url": "https://example.com/mcp"}},
}, indent=2)


@pytest.fixture
def three_apps(fake_home, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    gemini = fake_home / ".gemini" / "settings.json"
    opencode = fake_home / ".config" / "opencode" / "opencode.json"
    grok = fake_home / ".grok" / "config.toml"
    for path, text in ((gemini, GEMINI_SAMPLE), (opencode, OPENCODE_SAMPLE), (grok, CODEX_SAMPLE)):
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    return gemini, opencode, grok


def test_gemini_opencode_and_grok_are_added_keeping_everything_else(three_apps):
    gemini, opencode, grok = three_apps
    assert [hosts.write(h, None).status for h in ("gemini", "opencode", "grok")] == ["updated", "added", "added"]
    assert [hosts.write(h, None).status for h in ("gemini", "opencode", "grok")] == ["unchanged"] * 3
    assert all(hosts.configured(h)[0] for h in ("gemini", "opencode", "grok"))

    data = json.loads(gemini.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["handoff"]
    assert (entry["command"], entry["args"]) == hosts.server_args("gemini") and entry["args"][-2:] == ["--as", "gemini"]
    assert entry["trust"] is True and entry["timeout"] == 2_000_000  # yours: kept, and the longer timeout
    assert data["theme"] == "Default Dark" and data["mcpServers"]["github"]["command"] == "npx"

    data = json.loads(opencode.read_text(encoding="utf-8"))
    command, args = hosts.server_args("opencode")
    assert data["mcp"]["handoff"] == {"type": "local", "command": [command, *args], "enabled": True,
                                      "timeout": hosts.JSON_TOOL_TIMEOUT_MS}  # per call: a panel review takes minutes
    assert args[-2:] == ["--as", "opencode"] and Path(command).is_absolute()
    assert data["model"] == "xai/grok-code" and data["mcp"]["docs"]["type"] == "remote"

    data = tomllib.loads(grok.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["handoff"]["args"][-2:] == ["--as", "grok"]
    assert data["mcp_servers"]["handoff"]["tool_timeout_sec"] == hosts.CODEX_TOOL_TIMEOUT
    assert data["mcp_servers"]["github"]["env"] == {"GITHUB_TOKEN_ENV": "GH_TOKEN"}

    assert [hosts.remove(h).status for h in ("gemini", "opencode", "grok")] == ["removed"] * 3
    assert [hosts.remove(h).status for h in ("gemini", "opencode", "grok")] == ["not there"] * 3
    assert "handoff" not in json.loads(gemini.read_text(encoding="utf-8"))["mcpServers"]
    assert json.loads(opencode.read_text(encoding="utf-8")) == json.loads(OPENCODE_SAMPLE)
    assert grok.read_text(encoding="utf-8") == CODEX_SAMPLE


def test_json_with_comments_is_left_alone(three_apps, fake_home):
    gemini, _, _ = three_apps
    commented = '{\n  // my settings\n  "theme": "Default Dark"\n}\n'
    gemini.write_text(commented, encoding="utf-8")
    with pytest.raises(SetupError, match="has comments"):
        hosts.write("gemini", None)
    assert gemini.read_text(encoding="utf-8") == commented
    assert hosts.remove("gemini").status == "not there"  # nothing of ours in it, comments or not
    # OpenCode's opencode.jsonc, when that's its config, is read the same way
    folder = fake_home / ".config" / "opencode"
    (folder / "opencode.json").unlink()
    (folder / "opencode.jsonc").write_text(commented, encoding="utf-8")
    assert hosts.opencode_config_path() == folder / "opencode.jsonc"
    with pytest.raises(SetupError, match="has comments"):
        hosts.write("opencode", None)
    (folder / "opencode.jsonc").write_text('{"model": "x"}', encoding="utf-8")
    assert hosts.write("opencode", None).status == "added"
    assert "handoff" in json.loads((folder / "opencode.jsonc").read_text(encoding="utf-8"))["mcp"]


def test_opencode_turned_off_is_not_set_up_until_setup_turns_it_on(three_apps):
    _, opencode, _ = three_apps
    data = json.loads(OPENCODE_SAMPLE)
    data["mcp"]["handoff"] = {"type": "local", "command": ["handoff", "mcp", "--as", "opencode"], "enabled": False,
                              "environment": {"FOO": "bar"}}
    opencode.write_text(json.dumps(data), encoding="utf-8")
    ok, detail = hosts.configured("opencode")
    assert not ok and "turned off" in detail
    assert hosts.write("opencode", None).status == "updated"
    entry = json.loads(opencode.read_text(encoding="utf-8"))["mcp"]["handoff"]
    assert entry["enabled"] is True and entry["environment"] == {"FOO": "bar"} and hosts.configured("opencode")[0]


def test_opencode_honors_xdg_config_home(fake_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert hosts.opencode_config_path() == tmp_path / "xdg" / "opencode" / "opencode.json"
    assert not hosts.installed("opencode") or hosts.find_on_path("opencode")
    (tmp_path / "xdg" / "opencode").mkdir(parents=True)
    assert hosts.installed("opencode")
    assert hosts.write("opencode", None).status == "added"


def test_setup_write_connects_gemini_opencode_and_grok(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    for folder in (home / ".gemini", home / ".config" / "opencode", home / ".grok"):
        folder.mkdir(parents=True)
    (home / ".grok" / "config.toml").write_text('model = "grok-code"\n', encoding="utf-8")
    hosts_args = ["--host", "gemini", "--host", "opencode", "--host", "grok"]
    proc = _run("setup", "--write", *hosts_args, home=home, cwd=project, XDG_CONFIG_HOME="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for line in ("Gemini CLI: added", "OpenCode: added", "Grok Build: added", "Check your handoffs"):
        assert line in proc.stdout, line
    again = _run("setup", "--write", *hosts_args, home=home, cwd=project, XDG_CONFIG_HOME="")
    assert all(f"{label}: unchanged" in again.stdout for label in ("Gemini CLI", "OpenCode", "Grok Build"))
    removed = _run("setup", "--remove", *hosts_args, home=home, cwd=project, XDG_CONFIG_HOME="")
    assert removed.returncode == 0 and removed.stdout.count(": removed") == 3
    assert tomllib.loads((home / ".grok" / "config.toml").read_text(encoding="utf-8")) == {"model": "grok-code"}


def test_setup_prints_gemini_opencode_and_grok(tmp_path):
    proc = _run("setup", home=tmp_path, cwd=tmp_path, XDG_CONFIG_HOME="")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert 'Gemini CLI: add this server inside "mcpServers"' in out and '"--as",' in out and '"gemini"' in out
    assert 'OpenCode: add this server inside "mcp"' in out and '"type": "local"' in out and '"opencode"' in out
    assert "Grok Build: add to" in out and '"--as", "grok"' in out


def test_a_jsonc_file_with_handoff_in_it_is_read_but_not_rewritten(three_apps):
    gemini, opencode, _ = three_apps
    command, args = hosts.server_args("opencode")
    jsonc = ('{\n  // mine\n  "mcp": {\n    /* Handoff */ "handoff": {"type": "local", "command": %s, '
             '"enabled": true,},\n  },\n}\n' % json.dumps([command, *args]))
    opencode.write_text(jsonc, encoding="utf-8")
    ok, detail = hosts.configured("opencode")
    assert ok and detail.endswith("--as opencode")  # doctor reads it
    with pytest.raises(SetupError, match='delete "handoff" under "mcp" in it by hand'):
        hosts.remove("opencode")
    assert opencode.read_text(encoding="utf-8") == jsonc


def test_a_url_in_a_setting_isnt_taken_for_a_comment(three_apps):
    gemini, _, _ = three_apps
    gemini.write_text('{"theme": "x", "docs": "https://example.com/a//b", "broken": }', encoding="utf-8")
    with pytest.raises(SetupError, match="isn't valid JSON"):
        hosts.write("gemini", None)
    assert hosts._without_comments('{"a": "http://x/*y*/", "b": [1, 2,], // c\n "d": "\\"//"}') == \
        '{"a": "http://x/*y*/", "b": [1, 2], \n "d": "\\"//"}'


@pytest.mark.parametrize("entry", [None, 3, "handoff", {"command": None}, {"command": "handoff", "args": None},
                                   {"command": "handoff", "args": 7}, {"command": ["handoff", 2]}, {"command": []}])
def test_doctor_reads_an_odd_entry_without_falling_over(three_apps, entry):
    gemini, opencode, grok = three_apps
    gemini.write_text(json.dumps({"mcpServers": {"handoff": entry}}), encoding="utf-8")
    opencode.write_text(json.dumps({"mcp": {"handoff": entry}}), encoding="utf-8")
    assert not hosts.configured("gemini")[0] and not hosts.configured("opencode")[0]
    for text in ('[mcp_servers]\nhandoff = 3\n', 'mcp_servers = 3\n',
                 '[mcp_servers.handoff]\ncommand = "handoff"\nargs = 7\n'):
        grok.write_text(text, encoding="utf-8")
        assert not hosts.configured("grok")[0]
    gemini.write_text(json.dumps({"mcpServers": ["handoff"]}), encoding="utf-8")
    assert not hosts.configured("gemini")[0]


def test_an_entry_pointed_at_a_url_is_pointed_at_handoff_instead(three_apps):
    gemini, opencode, _ = three_apps
    gemini.write_text(json.dumps({"mcpServers": {"handoff": {"httpUrl": "http://localhost:9/mcp", "url": "x",
                                                             "trust": True}}}), encoding="utf-8")
    opencode.write_text(json.dumps({"mcp": {"handoff": {"type": "remote", "url": "https://x", "headers": {"a": "b"},
                                                        "oauth": {}, "environment": {"K": "v"}}}}),
                        encoding="utf-8")
    assert hosts.write("gemini", None).status == hosts.write("opencode", None).status == "updated"
    entry = json.loads(gemini.read_text(encoding="utf-8"))["mcpServers"]["handoff"]
    assert "url" not in entry and "httpUrl" not in entry and entry["trust"] is True
    entry = json.loads(opencode.read_text(encoding="utf-8"))["mcp"]["handoff"]
    assert entry["type"] == "local" and not {"url", "headers", "oauth"} & set(entry)
    assert entry["environment"] == {"K": "v"}


def test_each_app_s_settings_are_read_the_way_that_app_reads_them(three_apps, fake_home, monkeypatch):
    gemini, opencode, _ = three_apps
    entry = {"command": "/v/bin/handoff", "args": ["mcp"]}
    with_comma = '{"mcpServers": {"handoff": %s,},}' % json.dumps(entry)
    with_comment = '{\n  // mine\n  "mcpServers": {"handoff": %s}\n}' % json.dumps(entry)
    gemini.write_text(with_comment, encoding="utf-8")
    assert hosts.configured("gemini")[0]  # Gemini CLI takes comments
    gemini.write_text(with_comma, encoding="utf-8")
    assert not hosts.configured("gemini")[0]  # but not trailing commas
    opencode.write_text(with_comma.replace("mcpServers", "mcp").replace('"args"', '"type": "local", "args"'),
                        encoding="utf-8")
    desktop = fake_home / "desktop.json"
    monkeypatch.setattr(hosts, "desktop_config_path", lambda: desktop)
    for text in (with_comma, with_comment):  # Claude Desktop takes neither
        desktop.write_text(text, encoding="utf-8")
        ok, detail = hosts.configured("claude-desktop")
        assert not ok and "can't read" in detail


def test_a_comma_inside_a_string_is_kept(three_apps):
    assert json.loads(hosts._without_comments('{"a": "x,}", "b": ["y, ]", ], // c\n}')) == {"a": "x,}", "b": ["y, ]"]}
    assert json.loads(hosts._without_comments('[1, /* c */ ]')) == [1]
    with pytest.raises(json.JSONDecodeError):
        json.loads(hosts._without_comments('[1, ]', commas=False))


def test_codex_and_grok_entries_turned_off_or_pointed_at_a_url_are_pointed_at_handoff(fake_home):
    for host, path in (("codex", fake_home / ".codex" / "config.toml"), ("grok", fake_home / ".grok" / "config.toml")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('model = "m"\n\n[mcp_servers.handoff]\nurl = "http://localhost:9/mcp"\n'
                        'bearer_token_env_var = "T"\nenabled = false\nstartup_timeout_sec = 20\n', encoding="utf-8")
        assert not hosts.configured(host)[0]
        assert hosts.write(host, None).status == "updated"
        entry = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["handoff"]
        assert not {"url", "bearer_token_env_var", "enabled"} & set(entry) and entry["startup_timeout_sec"] == 20
        assert hosts.configured(host)[0] and hosts.write(host, None).status == "unchanged"


def test_gemini_and_opencode_drop_what_would_connect_them_elsewhere(three_apps):
    gemini, opencode, _ = three_apps
    gemini.write_text(json.dumps({"mcpServers": {"handoff": {"httpUrl": "https://x/mcp", "trust": True,
                                                             "authProviderType": "google_credentials",
                                                             "targetAudience": "a", "headers": {"a": "b"}}}}),
                      encoding="utf-8")
    opencode.write_text(json.dumps({"mcp": {"handoff": {"type": "local", "command": ["x"], "disabled": True}}}),
                        encoding="utf-8")
    hosts.write("gemini", None), hosts.write("opencode", None)
    entry = json.loads(gemini.read_text(encoding="utf-8"))["mcpServers"]["handoff"]
    assert set(entry) == {"command", "args", "timeout", "trust"}
    entry = json.loads(opencode.read_text(encoding="utf-8"))["mcp"]["handoff"]
    assert set(entry) == {"type", "command", "enabled", "timeout"} and entry["enabled"] is True
