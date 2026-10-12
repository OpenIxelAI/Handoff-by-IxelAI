"""The worker's other agents (adapters.py) and the model on your own computer (local_model.py).

OpenCode, Hermes Agent and the rest are stood in for by a fake program and a fake runner, as in
tests/test_worker.py; tests/test_worker_opencode_live.py runs the real OpenCode against a stand-in model server.
The local model is a real HTTP server here (a stand-in for Ollama), playing a script of replies.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from handoff import adapters, api, local_model, usage, worker
from handoff.board import HUMAN, Board
from handoff.worker import AGENTS, Worker, WorkerUnavailable

REPLY = "DONE: added src/hello.py\nLEFT: nothing\nVERIFY: python src/hello.py"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("# Shop\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def board(repo):
    return Board.open(repo)


@pytest.fixture
def config():
    """agents.toml for this test (next to its approval key), and the worker's agents read again from it."""
    path = adapters.config_path()

    def write(text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        worker.load_agents()
        return path
    return write


def fake_program(tmp_path, name, help_text):
    """A program on disk that prints `help_text` (the worker checks an agent's --help for the flags it needs)."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(f"#!{sys.executable}\nprint({help_text!r})\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class FakeAgent:
    def __init__(self, action=None, stdout=REPLY, code=0):
        self.calls, self.action, self.stdout, self.code = [], action, stdout, code

    def __call__(self, argv, stdin, env, timeout, cwd):
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": env, "cwd": cwd})
        if self.action:
            self.action(Path(cwd), list(argv))
        return subprocess.CompletedProcess(argv, self.code, self.stdout, "")


def write_hello(folder, argv=None):
    (folder / "src").mkdir(exist_ok=True)
    (folder / "src" / "hello.py").write_text("print('hi')\n", encoding="utf-8")


def approve(board, agent, **kwargs):
    task, _ = board.create(HUMAN, "Add a hello script", "Write src/hello.py that prints hi.")
    board.approve(HUMAN, task.id, agent, **kwargs)
    return task


# ── Which agents there are ───────────────────────────────────────────────────

def test_only_claude_and_codex_when_nothing_else_is_here():
    assert list(AGENTS) == ["claude", "codex"]


def test_the_agents_handoff_knows_are_offered_once_they_are_on_path():
    found, problems = adapters.build({}, on_path=lambda name: f"/usr/bin/{name}" if name in ("opencode", "ollama",
                                                                                           "kimi") else None)
    assert list(found) == ["opencode", "kimi", "ollama"] and problems == []
    assert found["opencode"].access == "edit-only" and not found["opencode"].needs_ok
    assert found["kimi"].access == "full" and found["kimi"].needs_ok
    assert found["ollama"].kind == "local" and found["ollama"].url == adapters.OLLAMA_URL


def test_agents_toml_adds_model_servers_and_agents_of_your_own(config):
    config('[agents.ollama]\nmodel = "qwen3:4b"\n\n'
           '[agents.studio]\nurl = "http://192.168.1.20:1234/v1/"\nmodel = "qwen3-coder"\nkey_env = "STUDIO_KEY"\n\n'
           '[agents.mine]\ncommand = "mine"\nargs = ["run"]\nmodel_args = ["--model", "{model}"]\n'
           'effort_args = ["--effort", "{effort}"]\nefforts = ["low", "high"]\nenv = ["MINE_"]\n')
    assert {"ollama", "studio", "mine"} <= set(AGENTS)
    assert AGENTS["ollama"].default_model == "qwen3:4b"
    studio = AGENTS["studio"]
    assert studio.kind == "local" and studio.url == "http://192.168.1.20:1234/v1" and studio.key_env == "STUDIO_KEY"
    mine = AGENTS["mine"]
    assert mine.custom and mine.access == "full" and mine.needs_ok and mine.model_flag == "--model"
    assert mine.effort_levels == ("low", "high") and mine.env_prefixes == ("MINE_",)


@pytest.mark.parametrize("text, problem", [
    ("[agents.claude]\ncommand = 'x'\n", "an agent's name"),
    ("[agents.Big]\ncommand = 'x'\n", "an agent's name"),
    ("[agents.x]\nargs = ['a']\n", "must say which program"),
    ("[agents.x]\ncommand = 'x'\nefforts = ['low']\n", "where the level goes"),
    ("[agents.x]\ncommand = 'x'\nmodel_args = ['--model']\n", "a flag and"),
    ("[agents.x]\ncommand = 'x'\nenv = ['A B']\n", "names of environment variables"),
    ("[agents.x]\nurl = 'file:///etc/passwd'\n", "http:// or https://"),
    ("[agents.x]\nurl = 'http://127.0.0.1:1/v1'\nmodel = 'bad model name'\n", "isn't a model name"),
    ("[agents\n", "can't be read"),
])
def test_what_is_wrong_in_agents_toml_is_said_not_fatal(config, text, problem):
    config(text)
    assert list(AGENTS)[:2] == ["claude", "codex"]
    assert any(problem in p for p in worker.AGENT_PROBLEMS), worker.AGENT_PROBLEMS


def test_enabling_an_agent_keeps_the_rest_of_the_file(config):
    path = config("# mine\n[agents.ollama]\nmodel = \"qwen3:4b\"  # small\n\n[agents.kimi]\nenabled = false\n")
    adapters.set_enabled("kimi", True)
    adapters.set_enabled("hermes", True)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# mine\n[agents.ollama]\nmodel = \"qwen3:4b\"  # small\n")
    assert adapters.enabled("kimi") and adapters.enabled("hermes") and not adapters.enabled("cline")
    adapters.set_enabled("kimi", False)
    assert not adapters.enabled("kimi") and adapters.enabled("hermes")
    with pytest.raises(adapters.ConfigError):
        adapters.set_enabled("../evil", True)


def test_a_file_that_does_not_parse_is_never_rewritten(config):
    path = config("[agents\n")
    with pytest.raises(adapters.ConfigError):
        adapters.set_enabled("kimi", True)
    assert path.read_text(encoding="utf-8") == "[agents\n"


# ── OpenCode: edit-only, its permissions set for the run ─────────────────────

OPENCODE_HELP = "opencode run [message..]\n --format  --pure  -m, --model  --variant"


def opencode_events(text=REPLY, cost=0.0012):
    """What `opencode run --format json` prints (its shape, from a real run)."""
    lines = [
        {"type": "step_start", "part": {"messageID": "m1", "type": "step-start"}},
        {"type": "tool_use", "part": {"messageID": "m1", "type": "tool", "tool": "write"}},
        {"type": "step_finish", "part": {"messageID": "m1", "type": "step-finish", "cost": 0,
                                         "tokens": {"input": 1200, "output": 300, "reasoning": 20,
                                                    "cache": {"read": 50, "write": 0}}}},
        {"type": "text", "part": {"messageID": "m2", "type": "text", "text": "Working on it.\n"}},
        {"type": "text", "part": {"messageID": "m2", "type": "text", "text": text}},
        {"type": "step_finish", "part": {"messageID": "m2", "type": "step-finish", "cost": cost,
                                         "tokens": {"input": 1500, "output": 100, "reasoning": 0,
                                                    "cache": {"read": 0, "write": 0}}}},
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def opencode_worker(tmp_path, board, repo, agent):
    worker.load_agents()
    AGENTS["opencode"] = adapters.presets()["opencode"]
    return Worker(board, repo, "opencode", runner=agent, command=fake_program(tmp_path, "opencode", OPENCODE_HELP))


def test_opencode_runs_edit_only_on_the_model_you_approved(tmp_path, board, repo):
    agent = FakeAgent(write_hello, stdout=opencode_events())
    task = approve(board, "opencode", models=["opencode/step-5-preview-free"], efforts={"opencode/step-5-preview-free":
                                                                                       "high"})
    [result] = opencode_worker(tmp_path, board, repo, agent).run_pending()
    assert result.ok, result.message
    [call] = agent.calls
    argv = call["argv"]
    assert argv[1:5] == ["run", "--format", "json", "--pure"]
    assert argv[argv.index("--model") + 1] == "opencode/step-5-preview-free"
    assert argv[argv.index("--variant") + 1] == "high"
    assert "Add a hello script" in call["stdin"] and "Add a hello script" not in " ".join(argv)
    permissions = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])["permission"]
    assert permissions["edit"] == "allow"
    assert all(permissions[tool] == "deny" for tool in ("bash", "webfetch", "websearch", "task",
                                                        "external_directory"))
    assert call["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert call["env"]["HANDOFF_DEPTH"] == "1"
    finished = board.get(task.id)
    assert finished.status == "handed_off" and finished.branch == f"handoff/{task.ref}"
    assert git(repo, "show", f"handoff/{task.ref}:src/hello.py").stdout == "print('hi')\n"
    used = result.used["models"]["opencode/step-5-preview-free"]
    assert used == {"in": 2700, "out": 420, "cache_read": 50, "cache_write": 0, "cost": 0.0012}
    message = git(repo, "log", "-1", "--format=%B", f"handoff/{task.ref}").stdout
    assert "opencode, opencode/step-5-preview-free, high effort" in message


def test_opencode_takes_only_its_own_effort_levels(tmp_path, board, repo):
    from handoff.worker import effort_problem
    AGENTS["opencode"] = adapters.presets()["opencode"]
    assert effort_problem("opencode", {"": "high"}) is None
    assert "not ultra" in effort_problem("opencode", {"": "ultra"})


def test_an_error_opencode_reports_fails_the_run(tmp_path, board, repo):
    error = json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": "model not found"}}})
    task = approve(board, "opencode")
    [result] = opencode_worker(tmp_path, board, repo, FakeAgent(stdout=error + "\n")).run_pending()
    assert not result.ok and "model not found" in result.message
    assert board.get(task.id).status != "in_progress"


def test_parse_opencode_keeps_only_the_last_message():
    reply, used, error = usage.parse_opencode(opencode_events(), "opencode/x")
    assert reply == "Working on it.\n" + REPLY and error is None
    assert usage.parse_opencode("not json\n", "opencode/x") == ("", None, None)


# ── Agents that wait for your OK ─────────────────────────────────────────────

HERMES_HELP = "usage: hermes  -z, --oneshot PROMPT  --usage-file PATH  -t, --toolsets  --ignore-rules  -m MODEL"


def test_hermes_waits_until_you_enable_it_then_runs_with_its_file_tools_only(tmp_path, board, repo, config):
    config("")
    AGENTS["hermes"] = adapters.presets()["hermes"]
    report = {"input_tokens": 900, "output_tokens": 80, "cache_read_tokens": 0, "cache_write_tokens": 0,
              "estimated_cost_usd": 0.0021, "model": "nous/hermes-5", "failed": False}

    def act(folder, argv):
        write_hello(folder)
        Path(argv[argv.index("--usage-file") + 1]).write_text(json.dumps(report), encoding="utf-8")
    agent = FakeAgent(act)
    command = fake_program(tmp_path, "hermes", HERMES_HELP)
    task = approve(board, "hermes")
    hermes = Worker(board, repo, "hermes", runner=agent, command=command)
    with pytest.raises(WorkerUnavailable, match="handoff agents enable hermes"):
        hermes.check()
    [waiting] = hermes.run_pending()
    assert not waiting.ok and "handoff agents enable hermes" in waiting.waiting and agent.calls == []
    assert board.pending_runs("hermes"), "the approval is kept for when you enable it"

    adapters.set_enabled("hermes", True)
    hermes.check()
    [result] = hermes.run_pending()
    assert result.ok, result.message
    argv = agent.calls[0]["argv"]
    assert argv[argv.index("-t") + 1] == "file" and "--ignore-rules" in argv
    prompt = argv[argv.index("-z") + 1]
    assert prompt.startswith("You're hermes") and "Add a hello script" in prompt and agent.calls[0]["stdin"] == ""
    assert not Path(argv[argv.index("--usage-file") + 1]).exists(), "its scratch folder is removed"
    assert result.used["models"]["nous/hermes-5"]["cost"] == 0.0021
    assert board.get(task.id).status == "handed_off"


def test_openclaw_reads_the_task_from_a_private_file_and_works_in_the_worktree(tmp_path, board, repo, config):
    config("[agents.openclaw]\nenabled = true\n")
    seen = {}

    def act(folder, argv):
        path = Path(argv[argv.index("--message-file") + 1])
        seen["text"], seen["mode"] = path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777
        write_hello(folder)
    agent = FakeAgent(act)
    command = fake_program(tmp_path, "openclaw", "openclaw agent --local --message-file --model --thinking")
    approve(board, "openclaw", models=["anthropic/claude-haiku-5-5"], efforts={"anthropic/claude-haiku-5-5": "low"})
    [result] = Worker(board, repo, "openclaw", runner=agent, command=command).run_pending()
    assert result.ok, result.message
    assert "Add a hello script" in seen["text"] and seen["mode"] == 0o600
    call = agent.calls[0]
    assert call["argv"][1:3] == ["agent", "--local"] and call["stdin"] == ""
    assert call["env"]["OPENCLAW_WORKSPACE_DIR"] == call["cwd"]


def test_an_agent_of_your_own_gets_only_the_environment_it_names(tmp_path, board, repo, config, monkeypatch):
    config('[agents.mine]\ncommand = "mine"\nargs = ["go"]\nenv = ["MINE_", "EXTRA"]\nenabled = true\n')
    monkeypatch.setenv("MINE_HOME", "/x")
    monkeypatch.setenv("EXTRA", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-the-test")
    agent = FakeAgent(write_hello)
    approve(board, "mine")
    [result] = Worker(board, repo, "mine", runner=agent, command=fake_program(tmp_path, "mine", "")).run_pending()
    assert result.ok, result.message
    env = agent.calls[0]["env"]
    assert env["MINE_HOME"] == "/x" and env["EXTRA"] == "1" and "GITHUB_TOKEN" not in env
    assert agent.calls[0]["argv"][1:] == ["go"] and "Add a hello script" in agent.calls[0]["stdin"]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe shims are Windows'")
def test_a_task_on_the_command_line_never_goes_through_cmd_exe(tmp_path, board, repo, config):
    config("[agents.kimi]\nenabled = true\n")
    shim = tmp_path / "kimi.cmd"
    shim.write_text("@echo --prompt --output-format --model\n", encoding="utf-8")
    with pytest.raises(WorkerUnavailable, match="cmd.exe"):
        Worker(board, repo, "kimi", command=str(shim)).check()


# ── A model on your own computer ─────────────────────────────────────────────

class Server:
    """A stand-in for Ollama's OpenAI-style API: lists its models, and answers each chat request with the next
    reply in its script (a tool call or a final answer), keeping every request it got."""

    def __init__(self, script, models=("qwen3:4b",)):
        self.script, self.models, self.requests = list(script), list(models), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, {"object": "list", "data": [{"id": m} for m in outer.models]})

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(request)
                step = outer.script.pop(0) if outer.script else "Nothing more to do."
                if isinstance(step, tuple) and step[0] == "error":
                    return self._send(step[1], {"error": {"message": step[2]}})
                if isinstance(step, str):
                    message = {"role": "assistant", "content": step}
                else:
                    message = {"role": "assistant", "content": "", "tool_calls": [
                        {"id": f"call_{n}", "type": "function",
                         "function": {"name": name, "arguments": json.dumps(args)}}
                        for n, (name, args) in enumerate(step)]}
                self._send(200, {"choices": [{"index": 0, "message": message}],
                                 "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made = []

    def make(script, **kwargs):
        made.append(Server(script, **kwargs))
        return made[-1]
    yield make
    for s in made:
        s.close()


def local_worker(board, repo, server, config, name="ollama"):
    config(f'[agents.{name}]\nurl = "{server.url}"\n')
    return Worker(board, repo, name)


def test_a_local_model_edits_files_through_handoffs_tools_and_costs_nothing(board, repo, server, config, tmp_path):
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    s = server([
        [("list_files", {"path": "."})],
        [("read_file", {"path": "../secret.txt"}), ("read_file", {"path": str(tmp_path / "secret.txt")}),
         ("write_file", {"path": ".git/hooks/post-commit", "content": "evil"}), ("run_command", {"cmd": "id"})],
        [("read_file", {"path": "README.md"})],
        [("write_file", {"path": "src/hello.py", "content": "print('hi')\n"})],
        [("replace_in_file", {"path": "README.md", "old": "# Shop", "new": "# Shop\n\nSays hi."})],
        "<think>done?</think>" + REPLY,
    ])
    task = approve(board, "ollama", models=["qwen3:4b"])
    ollama = local_worker(board, repo, s, config)
    ollama.check()
    [result] = ollama.run_pending()
    assert result.ok, result.message
    tools = [m for m in s.requests[-1]["messages"] if m["role"] == "tool"]
    said = [m["content"] for m in tools]
    assert said[0] == "README.md"
    assert "outside the project" in said[1] and "outside the project" in said[2]
    assert ".git folder is off limits" in said[3] and "no tool 'run_command'" in said[4]
    assert said[5] == "# Shop\n" and said[6].startswith("Created src/hello.py") and said[7] == "Changed README.md."
    first = s.requests[0]
    assert first["model"] == "qwen3:4b" and first["reasoning_effort"] == "none" and first["think"] is False
    assert first["messages"][0]["content"].endswith("/no_think")
    assert {t["function"]["name"] for t in first["tools"]} == set(local_model.TOOL_RUNNERS)
    assert git(repo, "show", f"handoff/{task.ref}:src/hello.py").stdout == "print('hi')\n"
    assert "Says hi." in git(repo, "show", f"handoff/{task.ref}:README.md").stdout
    assert not (repo / ".git" / "hooks" / "post-commit").exists()
    assert result.used == {"models": {"qwen3:4b": {"in": 600, "out": 120, "cache_read": 0, "cache_write": 0,
                                                    "cost": 0.0}}}
    finished = board.get(task.id)
    assert finished.status == "handed_off"
    events = board.events(task.id)
    handback = events[-1].data
    assert "<think>" not in json.dumps(handback) and "added src/hello.py" in json.dumps(handback)


def test_a_local_model_runs_the_servers_first_model_when_none_was_named(board, repo, server, config):
    s = server([REPLY], models=["llama3.2:3b", "qwen3:4b"])
    approve(board, "ollama", efforts={"": "high"})
    [result] = local_worker(board, repo, s, config).run_pending()
    assert result.ok, result.message
    assert s.requests[0]["model"] == "llama3.2:3b" and s.requests[0]["reasoning_effort"] == "high"
    assert "think" not in s.requests[0]


def test_a_server_that_is_not_running_is_said_before_anything_starts(board, repo, config):
    config('[agents.ollama]\nurl = "http://127.0.0.1:9/v1"\n')
    with pytest.raises(WorkerUnavailable, match="isn't answering at http://127.0.0.1:9/v1.*ollama serve"):
        Worker(board, repo, "ollama").check()


def test_a_server_with_no_models_says_how_to_get_one(board, repo, server, config):
    s = server([], models=[])
    with pytest.raises(WorkerUnavailable, match="ollama pull"):
        local_worker(board, repo, s, config).check()


def test_a_server_error_fails_the_run_and_says_what_the_server_said(board, repo, server, config):
    s = server([("error", 400, "registry.ollama.ai/library/qwen3:4b does not support tools")])
    task = approve(board, "ollama", models=["qwen3:4b"])
    [result] = local_worker(board, repo, s, config).run_pending()
    assert not result.ok and "does not support tools" in result.message
    assert board.get(task.id).status != "in_progress"


def test_a_model_that_never_stops_asking_for_tools_is_stopped(board, repo, server, config, monkeypatch):
    monkeypatch.setattr(local_model, "MAX_TURNS", 3)
    s = server([[("list_files", {})]] * 5)
    approve(board, "ollama", models=["qwen3:4b"])
    [result] = local_worker(board, repo, s, config).run_pending()
    assert not result.ok and "after 3 turns" in result.message and len(s.requests) == 3


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../../x", ".git/config", "", "C:\\x" if
                                  sys.platform == "win32" else "/x"])
def test_the_tools_refuse_paths_that_leave_the_worktree(tmp_path, path):
    assert local_model.run_tool(tmp_path, "read_file", json.dumps({"path": path})).startswith("Error")
    assert local_model.run_tool(tmp_path, "write_file", {"path": path, "content": "x"}).startswith("Error")


def test_the_tools_do_not_follow_links(tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "work").mkdir()
    os.symlink(tmp_path / "outside", tmp_path / "work" / "out")
    said = local_model.run_tool(tmp_path / "work", "write_file", {"path": "out/x.txt", "content": "x"})
    assert "link" in said and not (tmp_path / "outside" / "x.txt").exists()
    assert "out" not in local_model.run_tool(tmp_path / "work", "list_files", {})


def test_replace_in_file_needs_exactly_one_match(tmp_path):
    (tmp_path / "a.txt").write_text("x x", encoding="utf-8")
    assert "appears 2 times" in local_model.run_tool(tmp_path, "replace_in_file", {"path": "a.txt", "old": "x",
                                                                                  "new": "y"})


# ── What the Ixel app sees ───────────────────────────────────────────────────

def call(op, project=None, **args):
    request = {"schema": 1, "op": op, "args": args}
    if project:
        request["project"] = str(project)
    reply, _ = api.handle(json.dumps(request).encode())
    return reply


def test_the_board_lists_every_worker_with_what_it_can_reach(repo, server, config):
    s = server([], models=["qwen3:4b", "llama3.2:3b"])
    config(f'[agents.ollama]\nurl = "{s.url}"\n\n[agents.kimi]\nenabled = false\n')
    assert call("init", repo)["ok"]
    assert call("add", repo, title="Fix the footer")["ok"]
    workers = call("task", repo, task="T-1")["data"]["task"]["workers"]
    assert workers["claude"]["access"] == "sandbox" and workers["claude"]["enabled"]
    assert workers["ollama"] == {"efforts": ["none", "low", "medium", "high"], "chain": False, "label": "Ollama",
                                 "access": "edit-only", "local": True, "needs_ok": False, "enabled": True,
                                 "default_model": None}
    assert workers["kimi"]["access"] == "full" and workers["kimi"]["needs_ok"] and not workers["kimi"]["enabled"]
    assert "any-worker" in call("hello")["data"]["features"]
    models = call("models", repo, agent="ollama")["data"]
    assert models == {"agent": "ollama", "models": ["qwen3:4b", "llama3.2:3b"], "default": "qwen3:4b"}
    assert call("models", repo, agent="claude")["data"]["models"] == []

    enabled = call("agents.enable", repo, agent="kimi", on=True)
    assert enabled["ok"] and enabled["data"]["workers"]["kimi"]["enabled"]
    assert adapters.enabled("kimi")
    assert not call("agents.enable", repo, agent="claude")["ok"]


def test_nothing_handoff_started_can_enable_an_agent(repo, config, monkeypatch):
    config("[agents.kimi]\n")
    monkeypatch.setenv("HANDOFF_DEPTH", "1")
    reply = call("agents.enable", repo, agent="kimi", on=True)
    assert not reply["ok"] and reply["error"]["code"] == "forbidden" and not adapters.enabled("kimi")


def test_an_edit_can_be_approved_for_any_worker_through_the_api(repo, server, config):
    s = server([])
    config(f'[agents.ollama]\nurl = "{s.url}"\n')
    assert call("init", repo)["ok"]
    assert call("add", repo, title="Fix the footer")["ok"]
    reply = call("approve", repo, task="T-1", agent="ollama", models=["qwen3:4b"], efforts={"qwen3:4b": "none"})
    assert reply["ok"], reply
    bad = call("approve", repo, task="T-1", agent="ollama", models=["qwen3:4b"], efforts={"qwen3:4b": "max"})
    assert not bad["ok"]
