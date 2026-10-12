"""The real OpenCode CLI as the worker runs it, against a stand-in model server that tries to break out.

Skipped where `opencode` isn't installed. The model is a local HTTP server speaking the OpenAI chat API (as
Ollama does), set up as a provider in a private OpenCode config; it asks OpenCode's tools to read a file
outside the worktree, write one outside it, and write HELLO.txt inside it. The run must offer no shell, web or
subagent tool at all, refuse both escapes, and commit only HELLO.txt. OpenCode 2 offers those tools (its free
models refuse a run without them), so there it also tries each one: every call must be rejected. Its execute
tool (JavaScript that can make HTTP requests) mustn't be offered at all.
"""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from handoff import adapters
from handoff.board import HUMAN, Board
from handoff.proc import find_on_path
from handoff.worker import AGENTS, Worker

pytestmark = pytest.mark.skipif(find_on_path("opencode") is None, reason="opencode isn't installed")


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)


def opencode_2() -> bool:
    """OpenCode 2 (no --pure): its other tools are offered but rejected, rather than taken away."""
    help_text = subprocess.run([find_on_path("opencode"), "run", "--help"], capture_output=True, text=True,
                               encoding="utf-8", timeout=60)
    return "--pure" not in help_text.stdout + help_text.stderr


class Model:
    def __init__(self, outside: Path, v2: bool = False):
        self.requests = []
        self.fetched = []  # (anything that reached this server other than the model's own API)
        self.httpd = HTTPServer(("127.0.0.1", 0), self.handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        key = "path" if v2 else "filePath"
        self.steps = [("read", {key: str(outside / "secret.txt")}),
                      ("write", {key: str(outside / "escaped.txt"), "content": "out"})]
        if v2:  # (each one needs your say-so, which a run with no one to ask can't get)
            here = self.url.removesuffix("/v1")
            self.steps += [("shell", {"command": "touch SHELL_RAN.txt", "description": "make a file"}),
                           ("webfetch", {"url": f"{here}/webfetch"}),
                           ("subagent", {"agent": "general", "description": "x",
                                         "prompt": "Write SUBAGENT_RAN.txt."})]
        self.steps.append(("write", {key: "HELLO.txt", "content": "hi\n"}))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.fetched.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fetched")

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(request)
                if "messages" not in request:  # (a title request, say)
                    body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "t"}}]})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body.encode())
                    return
                done = sum(1 for m in request["messages"] if m.get("role") == "tool")
                tools = {t["function"]["name"] for t in request.get("tools") or []}
                steps = outer.steps
                if tools and done < len(steps):
                    name, args = steps[done]
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"c{done}", "type": "function",
                                                                  "function": {"name": name,
                                                                               "arguments": json.dumps(args)}}]}
                    finish = "tool_calls"
                else:
                    delta = {"role": "assistant", "content": "DONE: wrote HELLO.txt\nLEFT: nothing\nVERIFY: cat it"}
                    finish = "stop"
                chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(b"data: " + json.dumps({"id": "x", "object": "chat.completion.chunk",
                                                             "model": "stub", **chunk}).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
        return Handler


def test_opencode_edits_only_inside_its_worktree(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("the secret", encoding="utf-8")
    v2 = opencode_2()
    model = Model(outside, v2)
    # A private home: OpenCode's config (the stand-in provider) and its data, nothing of yours
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps({
        "provider": {"stub": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": model.url},
                              "models": {"coder": {}}}},
        "permission": {"bash": "allow"},  # (yours can't give it a shell: the run's own permissions win)
    }), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("# Shop\n", encoding="utf-8")
    (repo / "opencode.json").write_text(json.dumps({"permission": {"bash": "allow", "task": "allow"}}),
                                        encoding="utf-8")  # (nor can the project's)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-q", "-m", "init")
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Say hi", "Write HELLO.txt.")
    AGENTS["opencode"] = adapters.presets()["opencode"]
    board.approve(HUMAN, task.id, "opencode", models=["stub/coder"])
    worker = Worker(board, repo, "opencode", timeout_sec=180)
    worker.check()
    [result] = worker.run_pending()
    assert result.ok, result.message
    chats = [r for r in model.requests if r.get("tools")]
    offered = {t["function"]["name"] for r in chats for t in r["tools"]}
    results = [m for m in chats[-1]["messages"] if m.get("role") == "tool"]
    assert len(results) == len(model.steps), results  # (every step was tried)
    if v2:
        assert {"shell", "webfetch", "subagent"} <= offered and "execute" not in offered, offered
        assert all("permission.rejected" in m["content"] for m in results[:-1]), results
    else:
        assert offered and not offered & {"bash", "task", "webfetch", "websearch", "skill"}, offered
    assert not (outside / "escaped.txt").exists()
    said = json.dumps(results)
    assert "the secret" not in said
    assert model.fetched == []
    assert git(repo, "show", f"handoff/{task.ref}:HELLO.txt").stdout == "hi\n"
    changed = git(repo, "diff", "--name-only", f"HEAD..handoff/{task.ref}").stdout.split()
    assert changed == ["HELLO.txt"]
    assert result.used and "stub/coder" in result.used["models"]
    assert os.environ.get("HANDOFF_DEPTH") is None
