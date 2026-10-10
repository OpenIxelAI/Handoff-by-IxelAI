"""The worker's OS sandbox, and the agents' private logins: pure logic, on every platform.

tests/test_worker_codex_live.py and tests/test_worker_claude_live.py run the real
thing (codex and claude inside bubblewrap) on Linux.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from handoff import sandbox
from handoff import worker as worker_module
from handoff.board import HUMAN, Board
from handoff.worker import (
    CLAUDE,
    CLAUDE_KEY_FILE,
    CODEX,
    ClaudeLogin,
    CodexLogin,
    Worker,
    WorkerUnavailable,
    _looks_secret,
    claude_sandbox_settings,
)

# ── The sandbox's command line ───────────────────────────────────────────────

def _pairs(argv, flag):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def test_wrap_shows_only_the_worktree_the_install_and_the_private_home(tmp_path):
    worktree, private, install = tmp_path / "wt", tmp_path / "private", tmp_path / "install"
    for folder in (worktree, private, install):
        folder.mkdir()
    home = Path("/home/ada")
    argv = sandbox.wrap("bwrap", ["codex", "exec", "-"], worktree=worktree, home=home,
                        private={private: home / ".codex"}, read_only=[install, Path("/usr/lib/x")], env={})
    assert argv[0] == "bwrap" and argv[-3:] == ["codex", "exec", "-"] and argv[-4] == "--"
    tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    for hidden in ("/tmp", "/home", "/root", "/run", "/var", "/mnt", "/media"):
        assert hidden in tmpfs, hidden
    assert (str(install), str(install)) in _pairs(argv, "--ro-bind")
    assert ("/usr/lib/x", "/usr/lib/x") not in _pairs(argv, "--ro-bind")  # /usr is already there
    assert (str(private), str(home / ".codex")) in _pairs(argv, "--bind")
    assert (str(worktree), str(worktree)) in _pairs(argv, "--bind")
    # the binds come after the tmpfs that hides their parents
    last_tmpfs = max(i for i, a in enumerate(argv) if a == "--tmpfs")
    assert all(i > last_tmpfs for i, a in enumerate(argv) if a == "--bind")
    assert ("HOME", str(home)) in _pairs(argv, "--setenv") and ("TMPDIR", "/tmp") in _pairs(argv, "--setenv")
    assert argv[argv.index("--chdir") + 1] == str(worktree)
    assert "--unshare-net" not in argv  # the agent reaches its API; its own sandbox cuts its commands off
    assert "--die-with-parent" in argv and "--unshare-pid" in argv


def test_wrap_lets_the_network_settings_through(tmp_path):
    ca = tmp_path / "proxy-ca.pem"
    ca.write_text("cert", encoding="utf-8")
    argv = sandbox.wrap("bwrap", ["x"], worktree=tmp_path, home=Path("/home/ada"), private={}, read_only=[],
                        env={"SSL_CERT_FILE": str(ca), "NODE_EXTRA_CA_CERTS": str(tmp_path / "missing.pem")})
    assert (str(ca.resolve()), str(ca.resolve())) in _pairs(argv, "--ro-bind")
    assert not any("missing.pem" in a for a in argv)


def test_install_dirs_for_a_node_package(tmp_path, monkeypatch):
    package = tmp_path / "prefix" / "lib" / "node_modules" / "@openai" / "codex" / "bin"
    package.mkdir(parents=True)
    script = package / "codex.js"
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    node = tmp_path / "prefix" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("", encoding="utf-8")
    monkeypatch.setattr(sandbox, "find_on_path", lambda name: str(node) if name == "node" else None)
    assert sandbox.install_dirs(str(script)) == [(tmp_path / "prefix" / "lib" / "node_modules").resolve(),
                                                  (tmp_path / "prefix").resolve()]


def test_install_dirs_for_a_single_binary(tmp_path):
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text("", encoding="utf-8")
    assert sandbox.install_dirs(str(binary)) == [binary.parent.resolve()]


def test_check_explains_what_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    with pytest.raises(sandbox.SandboxUnavailable, match="Linux-only for now"):
        sandbox.check()
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable, match="sudo apt install bubblewrap"):
        sandbox.check()


# ── Folders the person shares ────────────────────────────────────────────────

def _home_with_tools(tmp_path):
    home = tmp_path / "home"
    tools = home / ".toolbox"
    (tools / "bin").mkdir(parents=True)
    (tools / "bin" / "hello").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (tools / "credentials.toml").write_text("token = 'x'\n", encoding="utf-8")
    (tools / "certs" / "deep").mkdir(parents=True)
    (tools / "certs" / "deep" / "server.pem").write_text("key\n", encoding="utf-8")
    for folder in (".ssh", ".config/gh", ".local/share/keyrings", ".local/bin"):
        (home / folder).mkdir(parents=True)
    project = home / "shop"
    (project / ".handoff" / "worktrees" / "T-1").mkdir(parents=True)
    return home, tools, project


def test_a_shared_folder_is_shown_with_its_credentials_hidden(tmp_path):
    home, tools, project = _home_with_tools(tmp_path)
    folders, hidden = sandbox.shared_folders([str(tools)], project=project, home=home)
    real = Path(os.path.realpath(tools))
    assert folders == [real]
    assert set(hidden) == {real / "credentials.toml", real / "certs" / "deep" / "server.pem"}


def test_a_deeply_nested_credential_in_a_shared_folder_is_hidden(tmp_path):
    home, tools, project = _home_with_tools(tmp_path)
    deep = tools / "one" / "two" / "three" / "four" / "five"
    deep.mkdir(parents=True)
    credential = deep / "credentials.json"
    credential.write_text('{"token": "secret"}', encoding="utf-8")

    folders, hidden = sandbox.shared_folders([tools], project=project, home=home)
    assert credential in hidden
    argv = sandbox.wrap("bwrap", ["run"], worktree=tmp_path / "wt", home=tmp_path / "h", private={},
                        read_only=folders, env={}, hidden=hidden)
    mask = argv.index(str(credential))
    assert argv[mask - 2:mask] == ["--ro-bind", "/dev/null"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_links_to_credentials_in_a_shared_folder_are_hidden(tmp_path):
    home, tools, project = _home_with_tools(tmp_path)
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("[default]\n", encoding="utf-8")
    (tools / "data").mkdir()
    (tools / "data" / "tokens.txt").write_text("secret\n", encoding="utf-8")
    (tools / "versions" / "1").mkdir(parents=True)
    sdk = home / ".sdk"  # another folder shared, after this one
    sdk.mkdir()
    (sdk / "token.txt").write_text("secret\n", encoding="utf-8")
    (project / "settings.txt").write_text("secret\n", encoding="utf-8")
    (tools / "credentials.json").symlink_to(tools / "data" / "tokens.txt")  # named like one
    (tools / "auth.json").symlink_to(sdk / "token.txt")                     # into the other shared folder
    (tools / ".env").symlink_to(project / "settings.txt")                   # into the project
    (tools / "config").symlink_to(home / ".aws" / "credentials")            # points at one the sandbox lacks
    (tools / "aws").symlink_to(home / ".aws", target_is_directory=True)     # a credential folder, likewise
    (tools / "current").symlink_to(tools / "versions" / "1", target_is_directory=True)
    (tools / "gone.pem").symlink_to(tools / "missing.pem")

    folders, hidden = sandbox.shared_folders([tools, sdk], project=project, home=home)
    real, real_sdk, real_project = (Path(os.path.realpath(p)) for p in (tools, sdk, project))
    aws = Path(os.path.realpath(home / ".aws"))
    assert {real / "data" / "tokens.txt", real_sdk / "token.txt", real_project / "settings.txt"} <= set(hidden)
    assert real / "versions" / "1" not in hidden and real / "missing.pem" not in hidden
    # The rest of your home isn't in the sandbox at all (it's an empty folder there), so there's nothing to hide
    assert aws not in hidden and aws / "credentials" not in hidden
    argv = sandbox.wrap("bwrap", ["run"], worktree=tmp_path / "wt", home=tmp_path / "h", private={},
                        read_only=folders, env={}, hidden=hidden)
    mask = argv.index(str(real / "data" / "tokens.txt"))
    assert argv[mask - 2:mask] == ["--ro-bind", "/dev/null"]
    assert not any(str(aws) in a for a in argv)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_to_the_systems_ca_bundle_is_not_hidden(tmp_path):
    """A cacert.pem that points at the CA bundle stays readable: hiding the bundle would break the agent's HTTPS."""
    home, tools, project = _home_with_tools(tmp_path)
    system = "/etc/ssl/certs/ca-certificates.crt"  # Debian's and Ubuntu's (hidden before, where it's there)
    proxy = tmp_path / "proxy" / "ca.crt"          # one SSL_CERT_FILE names, outside the shared folders
    proxy.parent.mkdir()
    proxy.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    (tools / "cacert.pem").symlink_to(system)
    (tools / "proxy-ca.pem").symlink_to(proxy)

    folders, hidden = sandbox.shared_folders([tools], project=project, home=home)
    bundles = [Path(os.path.realpath(system)), Path(os.path.realpath(proxy))]
    assert not set(bundles) & set(hidden)
    argv = sandbox.wrap("bwrap", ["run"], worktree=tmp_path / "wt", home=tmp_path / "h", private={},
                        read_only=folders, env={"SSL_CERT_FILE": str(tools / "proxy-ca.pem")}, hidden=hidden)
    assert (str(bundles[1]), str(bundles[1])) in _pairs(argv, "--ro-bind")
    assert not {("/dev/null", str(bundle)) for bundle in bundles} & set(_pairs(argv, "--ro-bind"))


def test_what_the_network_needs_is_never_hidden(tmp_path):
    """What the network settings name stays readable, even in a folder you share and named like a credential,
    or inside a credential folder there (a linked ~/.config, say)."""
    (tmp_path / "tools" / "certs").mkdir(parents=True)
    (tmp_path / "tools" / "config" / "corp").mkdir(parents=True)
    tools = Path(os.path.realpath(tmp_path / "tools"))  # as shared_folders gives it
    for name in ("proxy-ca.pem", "credentials.toml", "certs/extra.pem", "config/corp/ca.pem"):
        (tools / name).write_text("x", encoding="utf-8")
    hidden = [tools / "proxy-ca.pem", tools / "credentials.toml", tools / "certs" / "extra.pem", tools / "config"]
    argv = sandbox.wrap("bwrap", ["run"], worktree=tmp_path / "wt", home=tmp_path / "h", private={},
                        read_only=[tools], hidden=hidden,
                        env={"SSL_CERT_FILE": str(tools / "proxy-ca.pem"), "SSL_CERT_DIR": str(tools / "certs"),
                             "NODE_EXTRA_CA_CERTS": str(tools / "config" / "corp" / "ca.pem")})
    masks = {target for source, target in _pairs(argv, "--ro-bind") if source == "/dev/null"}
    assert masks == {str(tools / "credentials.toml")}
    # The folder is hidden, and the file the network needs in it shown again after
    folder_mask, ca = argv.index(str(tools / "config")), str(tools / "config" / "corp" / "ca.pem")
    assert argv[folder_mask - 1] == "--tmpfs"
    shown = [i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == ca]
    assert shown and shown[-1] > folder_mask


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="symlinks, and permissions (which root ignores)")
def test_a_link_into_a_folder_you_cannot_read_is_left_alone(tmp_path):
    """The agent runs as you, so it can't read there either: there's nothing to hide, and sharing still works."""
    home, tools, project = _home_with_tools(tmp_path)
    elsewhere, in_project = tmp_path / "root", project / "private"  # like /root; and a folder of the project
    for folder in (elsewhere, in_project):
        folder.mkdir()
        (folder / "token.txt").write_text("secret\n", encoding="utf-8")
    (tools / "credentials.json").symlink_to(elsewhere / "token.txt")
    (tools / "auth.json").symlink_to(in_project / "token.txt")
    elsewhere.chmod(0)
    in_project.chmod(0)
    try:
        folders, hidden = sandbox.shared_folders([tools], project=project, home=home)
    finally:
        elsewhere.chmod(0o700)
        in_project.chmod(0o700)
    assert folders == [Path(os.path.realpath(tools))]
    locked = [Path(os.path.realpath(elsewhere)), Path(os.path.realpath(in_project))]
    assert not any(folder in path.parents for path in hidden for folder in locked)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="permissions (which root ignores)")
@pytest.mark.parametrize("which,why", [("inside a folder you can't open", "isn't a folder you can open"),
                                       ("holding a folder you can only list", "couldn't check it for credential")])
def test_a_folder_you_cannot_open_is_refused_not_a_crash(tmp_path, which, why):
    home, tools, project = _home_with_tools(tmp_path)
    locked = tmp_path / "root"  # like /root
    (locked / "tools").mkdir(parents=True)
    listed = tools / "listed"
    listed.mkdir()
    (listed / "token.txt").write_text("secret\n", encoding="utf-8")
    locked.chmod(0)
    listed.chmod(0o444)  # its names can be read, but nothing about them
    try:
        with pytest.raises(sandbox.ShareRefused, match=why):
            sandbox.shared_folders([locked / "tools" if which.startswith("inside") else tools], project=project,
                                   home=home)
    finally:
        locked.chmod(0o700)
        listed.chmod(0o700)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="permissions (which root ignores)")
def test_a_certificate_file_you_cannot_read_is_left_out(tmp_path):
    """Network settings that name a file you can't read (the agent can't either) don't stop the task starting."""
    locked = tmp_path / "root"
    locked.mkdir()
    (locked / "ca.pem").write_text("cert", encoding="utf-8")
    locked.chmod(0)
    try:
        argv = sandbox.wrap("bwrap", ["x"], worktree=tmp_path, home=Path("/home/ada"), private={}, read_only=[],
                            env={"SSL_CERT_FILE": str(locked / "ca.pem")})
    finally:
        locked.chmod(0o700)
    assert not any("ca.pem" in a for a in argv)


def test_a_shared_folder_is_refused_when_credential_scan_fails(tmp_path, monkeypatch):
    home, tools, project = _home_with_tools(tmp_path)

    def unreadable(folder, *, onerror):
        onerror(PermissionError("cannot read a nested folder"))

    monkeypatch.setattr(sandbox.os, "walk", unreadable)
    with pytest.raises(sandbox.ShareRefused, match="couldn't check it for credential files"):
        sandbox.shared_folders([tools], project=project, home=home)


def test_a_credentials_folder_inside_a_shared_folder_is_hidden(tmp_path):
    home, tools, project = _home_with_tools(tmp_path)
    folders, hidden = sandbox.shared_folders([home / ".local"], project=project, home=home)
    assert Path(os.path.realpath(home / ".local" / "share" / "keyrings")) in hidden
    assert Path(os.path.realpath(home / ".local" / "bin")) not in hidden


@pytest.mark.parametrize("which,why", [
    ("home", "whole home folder"), ("above home", "whole home folder"), ("project", "project stays hidden"),
    ("inside project", "project stays hidden"), (".ssh", "holds credentials"), (".config/gh", "holds credentials"),
    ("a file", "isn't a folder"), ("missing", "isn't a folder"),
])
def test_what_cannot_be_shared(tmp_path, which, why):
    home, tools, project = _home_with_tools(tmp_path)
    path = {"home": home, "above home": tmp_path, "project": project,
            "inside project": project / ".handoff" / "worktrees" / "T-1", ".ssh": home / ".ssh",
            ".config/gh": home / ".config" / "gh", "a file": tools / "bin" / "hello",
            "missing": home / "missing"}[which]
    with pytest.raises(sandbox.ShareRefused, match=why):
        sandbox.shared_folders([str(path)], project=project, home=home)


def test_wrap_hides_credentials_inside_shared_folders(tmp_path):
    tools = tmp_path / "tools"
    (tools / "vault").mkdir(parents=True)
    (tools / "credentials.toml").write_text("x", encoding="utf-8")
    argv = sandbox.wrap("bwrap", ["run"], worktree=tmp_path / "wt", home=tmp_path / "h", private={},
                        read_only=[tools], env={}, hidden=[tools / "credentials.toml", tools / "vault"])
    shown = argv.index(str(tools))
    assert argv[shown - 1] == "--ro-bind"
    file_mask = argv.index(str(tools / "credentials.toml"))
    assert argv[file_mask - 2:file_mask] == ["--ro-bind", "/dev/null"] and file_mask > shown
    folder_mask = argv.index(str(tools / "vault"))
    assert argv[folder_mask - 1] == "--tmpfs" and folder_mask > shown


@pytest.mark.skipif(os.name == "nt", reason="uses sh")
def test_check_names_what_the_agents_own_sandbox_needs(monkeypatch, tmp_path):
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: str(fake))
    assert sandbox.check() == str(fake)
    monkeypatch.setattr(sandbox, "find_on_path", lambda name: None)
    with pytest.raises(sandbox.SandboxUnavailable, match="needs socat.*sudo apt install socat"):
        sandbox.check(("socat",))


@pytest.mark.skipif(os.name == "nt", reason="uses sh")
@pytest.mark.parametrize("restricted", ["1", "0", None])
def test_check_explains_a_bwrap_that_cannot_start(monkeypatch, tmp_path, restricted):
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    setting = tmp_path / "apparmor_restrict_unprivileged_userns"
    if restricted is not None:
        setting.write_text(restricted + "\n", encoding="ascii")
    monkeypatch.setattr(sandbox, "APPARMOR_USERNS", setting)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "find_bwrap", lambda: str(fake))
    with pytest.raises(sandbox.SandboxUnavailable, match="uid map: Permission denied") as caught:
        sandbox.check()
    message = str(caught.value)
    if restricted == "1":  # Ubuntu 24.04 and later: the message ends with the command that fixes it
        assert "AppArmor" in message and message.endswith(sandbox.apparmor_fix(str(fake)))
    else:
        assert "AppArmor" not in message and "user namespaces" in message


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_the_apparmor_fix_writes_and_loads_the_profile(tmp_path):
    real = tmp_path / "usr" / "bin" / "bwrap"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")
    (tmp_path / "bwrap").symlink_to(real)
    words = shlex.split(sandbox.apparmor_fix(str(tmp_path / "bwrap")))
    # AppArmor attaches a profile to the program's real path, not to the link on PATH
    assert words == ["echo", f"abi <abi/4.0>, profile bwrap {real.resolve()} flags=(unconfined) {{ userns, }}", "|",
                     "sudo", "tee", "/etc/apparmor.d/bwrap", "&&", "sudo", "apparmor_parser", "-r",
                     "/etc/apparmor.d/bwrap"]


# ── Codex's private login ────────────────────────────────────────────────────

@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _auth(account="acct-1", access="tok-1"):
    return json.dumps({"OPENAI_API_KEY": None, "tokens": {"account_id": account, "access_token": access}})


def test_the_login_is_copied_into_a_private_home(codex_home):
    (codex_home / "auth.json").write_text(_auth(), encoding="utf-8")
    (codex_home / "config.toml").write_text("[mcp_servers.x]\n", encoding="utf-8")
    (codex_home / "sessions").mkdir()
    login = CodexLogin({})
    login.prepare()
    assert sorted(p.name for p in login.home.iterdir()) == ["auth.json"]  # nothing else of ~/.codex
    if sys.platform != "win32":
        assert (login.home / "auth.json").stat().st_mode & 0o777 == 0o600
        assert login.home.stat().st_mode & 0o777 == 0o700
    login.cleanup()
    assert not login.home.exists()


def test_an_api_key_in_the_environment_becomes_the_private_login(codex_home):
    login = CodexLogin({"OPENAI_API_KEY": "sk-the-key"})
    login.prepare()
    assert json.loads((login.home / "auth.json").read_text(encoding="utf-8")) == {"OPENAI_API_KEY": "sk-the-key"}
    assert not (codex_home / "auth.json").exists()  # yours is untouched
    login.cleanup()


def test_no_login_says_how_to_sign_in(codex_home):
    with pytest.raises(WorkerUnavailable, match="codex login.*cli_auth_credentials_store"):
        CodexLogin({}).prepare()


def test_a_refreshed_login_goes_back(codex_home):
    (codex_home / "auth.json").write_text(_auth(), encoding="utf-8")
    login = CodexLogin({})
    login.prepare()
    (login.home / "auth.json").write_text(_auth(access="tok-2"), encoding="utf-8")  # codex refreshed it
    assert login.finish() is None
    assert json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))["tokens"]["access_token"] == "tok-2"
    login.cleanup()


def test_a_login_for_another_account_does_not_go_back(codex_home):
    (codex_home / "auth.json").write_text(_auth(), encoding="utf-8")
    login = CodexLogin({})
    login.prepare()
    (login.home / "auth.json").write_text(_auth(account="attacker"), encoding="utf-8")
    assert "didn't copy back" in login.finish()
    assert json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))["tokens"]["account_id"] == "acct-1"
    login.cleanup()


def test_a_login_you_changed_meanwhile_is_kept(codex_home):
    (codex_home / "auth.json").write_text(_auth(), encoding="utf-8")
    login = CodexLogin({})
    login.prepare()
    (codex_home / "auth.json").write_text(_auth(access="yours-new"), encoding="utf-8")  # you signed in again
    (login.home / "auth.json").write_text(_auth(access="tok-2"), encoding="utf-8")
    assert "didn't copy back" in login.finish()
    assert "yours-new" in (codex_home / "auth.json").read_text(encoding="utf-8")
    login.cleanup()


def test_a_broken_login_file_does_not_go_back(codex_home):
    (codex_home / "auth.json").write_text(_auth(), encoding="utf-8")
    login = CodexLogin({})
    login.prepare()
    (login.home / "auth.json").write_text("{not json", encoding="utf-8")
    assert "didn't copy back" in login.finish()
    assert json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))["tokens"]["access_token"] == "tok-1"
    login.cleanup()


# ── Claude's private login ───────────────────────────────────────────────────

@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def _credentials(access="tok-1", refresh="ref-1"):
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": 1,
                                         "scopes": ["user:inference"]}})


def test_claudes_login_is_copied_into_a_private_home(claude_home):
    (claude_home / ".credentials.json").write_text(_credentials(), encoding="utf-8")
    (claude_home / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    (claude_home / "projects").mkdir()
    login = ClaudeLogin({})
    login.prepare()
    assert sorted(p.name for p in login.home.iterdir()) == [".credentials.json"]  # nothing else of ~/.claude
    if sys.platform != "win32":
        assert (login.home / ".credentials.json").stat().st_mode & 0o777 == 0o600
    settings = json.loads(login.values(Path("/h/.claude"))["SETTINGS"])
    assert settings["sandbox"]["filesystem"]["denyRead"] == [str(Path("/h/.claude"))]
    assert "apiKeyHelper" not in settings
    login.cleanup()
    assert not login.home.exists()


def test_an_api_key_goes_in_a_file_claude_reads_itself(claude_home):
    (claude_home / ".credentials.json").write_text(_credentials(), encoding="utf-8")  # the key wins, as in Claude
    login = ClaudeLogin({"ANTHROPIC_API_KEY": "sk-ant-the-key"})
    login.prepare()
    assert sorted(p.name for p in login.home.iterdir()) == [CLAUDE_KEY_FILE]
    assert (login.home / CLAUDE_KEY_FILE).read_text(encoding="utf-8") == "sk-ant-the-key"
    inside = Path("/h/.claude")
    helper = json.loads(login.values(inside)["SETTINGS"])["apiKeyHelper"]
    assert shlex.split(helper) == ["cat", str(inside / CLAUDE_KEY_FILE)]
    assert login.finish() is None and (claude_home / ".credentials.json").read_text(encoding="utf-8") == _credentials()
    login.cleanup()


def test_a_setup_token_becomes_a_login_that_never_goes_back(claude_home):
    login = ClaudeLogin({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-token"})
    login.prepare()
    saved = json.loads((login.home / ".credentials.json").read_text(encoding="utf-8"))
    assert saved["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-token"
    (login.home / ".credentials.json").write_text(_credentials(access="changed"), encoding="utf-8")
    assert login.finish() is None and not (claude_home / ".credentials.json").exists()
    login.cleanup()


def test_no_claude_login_says_how_to_sign_in(claude_home):
    with pytest.raises(WorkerUnavailable, match="Sign in with `claude`.*ANTHROPIC_API_KEY"):
        ClaudeLogin({}).prepare()


def test_a_refreshed_claude_login_goes_back_unless_you_changed_yours(claude_home):
    (claude_home / ".credentials.json").write_text(_credentials(), encoding="utf-8")
    login = ClaudeLogin({})
    login.prepare()
    (login.home / ".credentials.json").write_text(_credentials(access="tok-2", refresh="ref-2"), encoding="utf-8")
    assert login.finish() is None
    assert "tok-2" in (claude_home / ".credentials.json").read_text(encoding="utf-8")
    login.cleanup()

    login = ClaudeLogin({})
    login.prepare()
    (claude_home / ".credentials.json").write_text(_credentials(access="yours-new"), encoding="utf-8")
    (login.home / ".credentials.json").write_text(_credentials(access="tok-3"), encoding="utf-8")
    assert "didn't copy back" in login.finish()
    assert "yours-new" in (claude_home / ".credentials.json").read_text(encoding="utf-8")
    login.cleanup()


@pytest.mark.parametrize("login,env,file", [
    (CodexLogin, {"CODEX_API_KEY": "sk-x"}, "auth.json"), (CodexLogin, {"OPENAI_API_KEY": "sk-x"}, "auth.json"),
    (ClaudeLogin, {"ANTHROPIC_API_KEY": "sk-ant-x"}, ".credentials.json"),
    (ClaudeLogin, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}, ".credentials.json"),
])
def test_a_missing_login_is_seen_without_copying_anything(login, env, file, codex_home, claude_home, monkeypatch):
    home = codex_home if login is CodexLogin else claude_home
    monkeypatch.setattr(worker_module.tempfile, "mkdtemp", lambda **kw: pytest.fail("made a private home"))
    assert "isn't signed in with a login Handoff can pass to it" in login.missing({})
    assert login.missing(env) is None
    (home / file).write_text("{}", encoding="utf-8")
    assert login.missing({}) is None


def _sandboxed_worker(tmp_path, monkeypatch, agent):
    """A worker whose sandbox and agent CLI pass every check: only its login is left to look at."""
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("# Shop\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "-m", "init")
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "check", lambda *needs: "bwrap")
    spec = worker_module.AGENTS[agent]
    if sys.platform == "win32":  # Windows can't run a #!/bin/sh script
        fake = tmp_path / f"{agent}.cmd"
        fake.write_text(f"@echo {' '.join(spec.required_flags)}\r\n", encoding="utf-8")
    else:
        fake = tmp_path / agent
        fake.write_text(f"#!/bin/sh\necho {shlex.quote(' '.join(spec.required_flags))}\n", encoding="utf-8")
        fake.chmod(0o755)
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Compute the answer")
    board.approve(HUMAN, task.id, agent)
    ran = []
    return board, task, Worker(board, repo, agent, runner=lambda *a: ran.append(a), command=str(fake)), ran


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_a_worker_with_no_login_isn_t_ready_and_keeps_the_approval(agent, tmp_path, monkeypatch, codex_home,
                                                                   claude_home):
    board, task, worker, ran = _sandboxed_worker(tmp_path, monkeypatch, agent)
    approved = board.get(task.id).status
    with pytest.raises(WorkerUnavailable, match="isn't signed in"):  # what `handoff doctor` says
        worker.check()
    [result] = worker.run_pending()  # a worker started without a check (handoff run checks first)
    assert not result.ok and "isn't signed in" in result.message
    assert f"It's still approved; run it later with: handoff run {task.ref}" in result.message
    assert ran == [] and board.get(task.id).status == approved
    assert [t.id for t, _ in board.pending_runs(agent)] == [task.id]  # signing in is all it takes now


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_a_worker_waiting_for_a_login_says_so_once_and_starts_when_you_sign_in(agent, tmp_path, monkeypatch,
                                                                                codex_home, claude_home):
    """Not ▶ and ✗ every few seconds: the task is passed by until its approval or the login changes."""
    board, task, worker, ran = _sandboxed_worker(tmp_path, monkeypatch, agent)
    worker.bwrap = "bwrap"  # checked when it started, signed in; the login went since
    started = []

    def run_pending():
        return worker.run_pending(on_start=lambda t, kind: started.append(t.ref))
    [first] = run_pending()
    assert not first.ok and "isn't signed in" in first.waiting
    for _ in range(3):
        assert run_pending() == []
    assert started == [task.ref] and ran == []
    board.revoke_approval(HUMAN, task.id)
    board.approve(HUMAN, task.id, agent)  # approved again: it's tried again, once
    [again] = run_pending()
    assert again.waiting and run_pending() == [] and len(started) == 2
    login = worker_module.AGENTS[agent].login
    (login.real_home() / login.login_file).write_text(_auth() if agent == "codex" else _credentials(),
                                                      encoding="utf-8")  # you sign in
    worker.runner = lambda argv, stdin, env, timeout, cwd: subprocess.CompletedProcess(
        argv, 0, "DONE: done\nLEFT: nothing\nVERIFY: nothing", "")
    [ran_now] = run_pending()
    assert ran_now.ok and not ran_now.waiting and len(started) == 3


@pytest.mark.skipif(sys.platform == "win32", reason="the fake agent is a shell script")
def test_handoff_worker_says_once_that_a_task_waits_for_a_login(tmp_path, monkeypatch, claude_home, capsys):
    from rich.console import Console

    from handoff import cli
    board, task, _, _ = _sandboxed_worker(tmp_path, monkeypatch, "claude")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")  # the fake claude
    monkeypatch.setattr(Worker, "check", lambda self: setattr(self, "bwrap", "bwrap"))  # (signed in then)
    monkeypatch.setattr(cli, "console", Console(width=400, highlight=False))
    monkeypatch.chdir(tmp_path / "shop")
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 4:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["worker", "--agent", "claude"]) == 0
    out = capsys.readouterr().out
    assert out.count("▶") == 1 and out.count(f"{task.ref} is waiting: Claude Code isn't signed in") == 1
    assert "this worker starts it once you sign in" in out and "✗" not in out


def test_claudes_sandbox_settings():
    """What the attack test relies on; a change here needs tests/test_worker_claude_live.py run again."""
    settings = json.loads(claude_sandbox_settings(Path("/h/.claude")))
    assert settings["disableAllHooks"] is True
    box = settings["sandbox"]
    assert box["enabled"] is True and box["failIfUnavailable"] is True and box["allowUnsandboxedCommands"] is False
    assert box["network"] == {"allowedDomains": [], "strictAllowlist": True}
    assert box["filesystem"] == {"denyRead": [str(Path("/h/.claude"))]}


@pytest.mark.parametrize("name,secret", [
    ("OPENAI_API_KEY", True), ("CODEX_API_KEY", True), ("GITHUB_TOKEN", True), ("AWS_SECRET_ACCESS_KEY", True),
    ("DB_PASSWORD", True), ("OPENAI_BASE_URL", False), ("PATH", False), ("HTTPS_PROXY", False), ("LANG", False),
])
def test_looks_secret(name, secret):
    assert _looks_secret(name) is secret


# ── The Codex worker, with a fake codex ──────────────────────────────────────

def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)


def test_codex_runs_inside_the_sandbox_with_its_private_login(tmp_path, monkeypatch, codex_home):
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("# Shop\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "-m", "init")
    (codex_home / "auth.json").write_text(_auth(), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-the-sandbox")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_for_the_sandbox")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Compute the answer")
    board.approve(HUMAN, task.id, "codex")
    seen = {}

    def fake_bwrap(argv, stdin, env, timeout, cwd):
        seen.update(argv=argv, stdin=stdin, env=env)
        private = next(Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--bind" and argv[i + 1] != cwd)
        seen["private"] = sorted(p.name for p in private.iterdir())
        (Path(cwd) / "answer.txt").write_text("42\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "DONE: wrote answer.txt\nLEFT: nothing\nVERIFY: cat it", "")

    tools = tmp_path / "tools"
    (tools / "bin").mkdir(parents=True)
    (tools / ".npmrc").write_text("//registry/:_authToken=x\n", encoding="utf-8")
    worker = Worker(board, repo, "codex", runner=fake_bwrap, command=str(tmp_path / "codex"), share=[str(tools)])
    worker.check = lambda: None  # the sandbox itself is faked here
    worker.bwrap = "bwrap"
    worker.shared, worker.hidden = sandbox.shared_folders(worker.share, project=repo)
    [result] = worker.run_pending()
    assert result.ok, result.message

    argv = seen["argv"]
    inner = argv[argv.index("--") + 1:]
    assert argv[0] == "bwrap" and inner[0] == os.path.realpath(str(tmp_path / "codex"))
    real_tools = os.path.realpath(tools)
    assert argv[argv.index(real_tools) - 1] == "--ro-bind"
    assert argv[argv.index(os.path.join(real_tools, ".npmrc")) - 1] == "/dev/null"
    assert inner[1] == "exec" and inner[-1] == "-" and "--ignore-user-config" in inner
    inside = str(Path.home() / ".codex")
    assert f"permissions.handoff.filesystem={{{json.dumps(inside)}=\"none\"}}" in inner
    assert seen["private"] == ["auth.json"]
    env = seen["env"]
    assert env["CODEX_HOME"] == inside and env["OPENAI_BASE_URL"] == "https://example.test/v1"
    assert "OPENAI_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert "run commands here" in seen["stdin"] and "no network" in seen["stdin"]
    assert "Git commands don't work here" in seen["stdin"]  # (it can't see the repository)
    assert git(repo, "show", "handoff/T-1:answer.txt").stdout == "42\n"
    left = board.events(task.id)[-1].data["left"]
    assert CODEX.note in left and "You shared these folders with it, read-only" in left


def test_a_login_refreshed_before_a_timeout_still_goes_back(tmp_path, monkeypatch, codex_home):
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("# Shop\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "-m", "init")
    (codex_home / "auth.json").write_text(_auth(), encoding="utf-8")
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Slow task")
    board.approve(HUMAN, task.id, "codex")

    def refresh_then_time_out(argv, stdin, env, timeout, cwd):
        private = next(Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--bind" and argv[i + 1] != cwd)
        (private / "auth.json").write_text(_auth(access="tok-refreshed"), encoding="utf-8")
        raise subprocess.TimeoutExpired(argv, timeout)

    worker = Worker(board, repo, "codex", runner=refresh_then_time_out, command=str(tmp_path / "codex"))
    worker.bwrap = "bwrap"
    [result] = worker.run_pending()
    assert not result.ok and "stopped after" in result.message
    assert "tok-refreshed" in (codex_home / "auth.json").read_text(encoding="utf-8")


def test_codex_on_a_system_without_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    with pytest.raises(WorkerUnavailable, match="Linux-only for now"):
        Worker(None, tmp_path, "codex", command="codex").check()  # type: ignore[arg-type]
    assert worker_module.AGENTS["codex"] is CODEX


def test_claude_runs_inside_the_sandbox_with_its_private_login(tmp_path, monkeypatch, claude_home):
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("# Shop\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "-m", "init")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-the-sandbox")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_for_the_sandbox")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.test")
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Compute the answer")
    board.approve(HUMAN, task.id, "claude")
    seen = {}

    def fake_bwrap(argv, stdin, env, timeout, cwd):
        seen.update(argv=argv, stdin=stdin, env=env)
        private = next(Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--bind" and argv[i + 1] != cwd)
        seen["private"] = sorted(p.name for p in private.iterdir())
        (Path(cwd) / "answer.txt").write_text("42\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "DONE: wrote answer.txt\nLEFT: nothing\nVERIFY: cat it", "")

    worker = Worker(board, repo, "claude", runner=fake_bwrap, command=str(tmp_path / "claude"))
    worker.bwrap = "bwrap"
    [result] = worker.run_pending()
    assert result.ok, result.message

    argv = seen["argv"]
    inner = argv[argv.index("--") + 1:]
    assert argv[0] == "bwrap" and inner[0] == os.path.realpath(str(tmp_path / "claude"))
    assert "--restricted" in inner and "--strict-mcp-config" in inner
    assert inner[inner.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep,Bash"
    assert inner[inner.index("--allowedTools") + 1] == "Bash"
    inside = Path.home() / ".claude"
    settings = json.loads(inner[inner.index("--settings") + 1])
    assert settings["sandbox"]["filesystem"]["denyRead"] == [str(inside)]
    assert shlex.split(settings["apiKeyHelper"]) == ["cat", str(inside / CLAUDE_KEY_FILE)]
    assert seen["private"] == [CLAUDE_KEY_FILE]
    env = seen["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(inside) and env["ANTHROPIC_BASE_URL"] == "https://example.test"
    assert "ANTHROPIC_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert "run commands here" in seen["stdin"] and "no network" in seen["stdin"]
    assert "Git commands don't work here" in seen["stdin"]  # (it can't see the repository)
    assert git(repo, "show", "handoff/T-1:answer.txt").stdout == "42\n"
    assert CLAUDE.note in board.events(task.id)[-1].data["left"]
