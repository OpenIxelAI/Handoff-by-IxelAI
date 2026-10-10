"""
The Handoff plugin for Claude Code and Codex (plugins/handoff), and the marketplaces that list it.

The manifests are checked on every platform. The live tests install the plugin
into the real `claude` and `codex` the way a person would, then have a fake model
create a task through it: that proves the app starts `handoff mcp` from the
plugin, as the right agent, on the board of the project it was opened in. They
skip unless the CLI is installed; CI sets HANDOFF_REQUIRE_CLIS=1.
"""
import json
import os
import re
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest
from cli_capture import CaptureServer, offered_tools

from handoff import __version__, hosts, ixel
from handoff.board import Board

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "handoff"
# what the apps need to run; on Windows also its system folders and how it finds programs
KEEP_ENV = {"PATH", "LANG", "LC_ALL", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
            "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS", "OS"}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── The manifests ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("app,manifest", [("claude", ".claude-plugin"), ("codex", ".codex-plugin")])
def test_each_app_starts_handoff_as_itself(app, manifest):
    data = _json(PLUGIN / manifest / "plugin.json")
    assert data["name"] == "handoff" and data["version"] == __version__
    # no --project: the app starts it in the project it's working on, and each project has its own board
    server = data["mcpServers"]["handoff"]
    assert list(data["mcpServers"]) == ["handoff"]
    assert server["command"] == "handoff" and server["args"] == ["mcp", "--as", app]
    if app == "codex":  # as `handoff setup` gives it: a review with the Ixel panel can take minutes
        assert server["tool_timeout_sec"] == hosts.CODEX_TOOL_TIMEOUT > ixel.TIMEOUT_SEC


def test_no_shared_mcp_config():
    # a .mcp.json would be read by both apps, and one of them would join the board under the other's name
    assert not (PLUGIN / ".mcp.json").exists()


def test_the_marketplaces_list_the_plugin():
    claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
    [entry] = claude["plugins"]
    assert entry["name"] == "handoff" and (ROOT / entry["source"]).resolve() == PLUGIN
    assert entry["version"] == __version__
    codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    [entry] = codex["plugins"]
    assert entry["name"] == "handoff" and entry["source"]["source"] == "local"
    assert (ROOT / entry["source"]["path"]).resolve() == PLUGIN


def test_the_skill_says_approvals_are_the_persons():
    text = (PLUGIN / "skills" / "handoff" / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    assert re.search(r"^name: handoff$", front, re.M) and re.search(r"^description: .{40,}", front, re.M)
    assert "Only the person approves the worker" in text
    assert "data, not instructions" in text
    for tool in ("handoff_inbox", "handoff_claim", "handoff_pass", "handoff_request_review", "handoff_review"):
        assert tool in text


# ── Live: install it in the real apps ────────────────────────────────────────

def _project(tmp_path: Path) -> Path:
    project = tmp_path / "shop"
    project.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", *args], cwd=project, check=True,
                       capture_output=True)
    return project


@pytest.fixture(autouse=True)
def _this_handoff_first(monkeypatch):
    # The apps start `handoff` from PATH: this checkout's (pip install -e . puts it next to the Python
    # running the tests), even when its folder isn't on PATH, as with .venv\Scripts\python -m pytest
    scripts = sysconfig.get_path("scripts")
    monkeypatch.setenv("PATH", scripts + os.pathsep + os.environ.get("PATH", ""))


def _needs(command: str) -> str:
    found = shutil.which(command)
    if not found:
        if os.environ.get("HANDOFF_REQUIRE_CLIS") == "1":
            pytest.fail(f"{command} is not installed")
        pytest.skip(f"{command} is not installed")
    if not shutil.which("handoff"):
        pytest.fail("the handoff command isn't on PATH (pip install -e .)")
    return found


def _env(tmp_path: Path, **extra) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    keep = {k: v for k, v in os.environ.items() if k.upper() in KEEP_ENV}
    # Windows keeps settings in AppData too: the test's own, like its home
    appdata = {"APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local")} \
        if os.name == "nt" else {}
    return {**keep, "HOME": str(home), "USERPROFILE": str(home), **appdata, "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost", **extra}


def _run(argv, env, cwd, stdin=""):
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=cwd, timeout=300)


def test_claude_code_uses_the_board_through_the_plugin(tmp_path):
    claude = _needs("claude")
    project = _project(tmp_path)
    with CaptureServer() as fake:
        env = _env(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "claude"), ANTHROPIC_BASE_URL=fake.url,
                   ANTHROPIC_API_KEY="sk-ant-test", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", MCP_TIMEOUT="60000")
        # installed as a person would: add the marketplace (this checkout), then the plugin
        added = _run([claude, "plugin", "marketplace", "add", str(ROOT)], env, project)
        assert added.returncode == 0, added.stdout + added.stderr
        installed = _run([claude, "plugin", "install", "handoff@ixelai"], env, project)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        tool = "mcp__plugin_handoff_handoff__handoff_create"
        fake.tool_calls = [{"name": tool, "args": {"title": "Made through the Claude Code plugin"}}]
        proc = _run([claude, "-p", "--permission-mode", "acceptEdits", "--allowedTools", tool, "--output-format",
                     "text"], env, project, "Create the task.")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        offered = {t for r in fake.requests if r["body"] for t in offered_tools(r["body"])}
    assert tool in offered, sorted(offered)
    [task] = Board.open(project, create=False).tasks()
    assert task.title == "Made through the Claude Code plugin" and task.created_by == "claude"


def test_codex_uses_the_board_through_the_plugin(tmp_path):
    codex = _needs("codex")
    project = _project(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-test"}), encoding="utf-8")
    env = _env(tmp_path, CODEX_HOME=str(codex_home))
    added = _run([codex, "plugin", "marketplace", "add", str(ROOT)], env, project)
    assert added.returncode == 0, added.stdout + added.stderr
    installed = _run([codex, "plugin", "add", "handoff@ixelai"], env, project)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    with CaptureServer() as fake:
        # Codex groups an MCP server's tools under one namespace, and the model calls into it
        fake.tool_calls = [{"name": "handoff_create", "namespace": "mcp__handoff",
                            "args": {"title": "Made through the Codex plugin"}}]
        proc = _run([codex, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
                     "-c", 'approval_policy="never"', "-c", 'model_provider="fake"', "-c", 'model="gpt-5"',
                     "-c", f'model_providers.fake={{name="fake",base_url="{fake.url}/v1",wire_api="responses"}}',
                     "-"], env, project, "Create the task.")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        offered = {t for r in fake.requests if r["body"] for t in offered_tools(r["body"])}
    assert "mcp__handoff" in offered, sorted(offered)
    [task] = Board.open(project, create=False).tasks()
    assert task.title == "Made through the Codex plugin" and task.created_by == "codex"
