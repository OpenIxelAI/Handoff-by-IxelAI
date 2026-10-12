"""The real OpenCode CLI as the worker runs it, against a stand-in model server that tries to break out.

Skipped where `opencode` isn't installed. The model is a local HTTP server speaking the OpenAI chat API (as
Ollama does), set up as a provider in a private OpenCode config; it asks OpenCode's tools to read a file
outside the worktree, write one outside it, and write HELLO.txt inside it. The run must offer no shell, web or
subagent tool at all, refuse both escapes, and commit only HELLO.txt.
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


class Model:
    def __init__(self, outside: Path):
        self.requests = []
        steps = [("read", {"filePath": str(outside / "secret.txt")}),
                 ("write", {"filePath": str(outside / "escaped.txt"), "content": "out"}),
                 ("write", {"filePath": "HELLO.txt", "content": "hi\n"})]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

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

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def test_opencode_edits_only_inside_its_worktree(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("the secret", encoding="utf-8")
    model = Model(outside)
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
    assert offered and not offered & {"bash", "task", "webfetch", "websearch", "skill"}, offered
    assert not (outside / "escaped.txt").exists()
    said = json.dumps([m for m in chats[-1]["messages"] if m.get("role") == "tool"])
    assert "the secret" not in said
    assert git(repo, "show", f"handoff/{task.ref}:HELLO.txt").stdout == "hi\n"
    changed = git(repo, "diff", "--name-only", f"HEAD..handoff/{task.ref}").stdout.split()
    assert changed == ["HELLO.txt"]
    assert result.used and "stub/coder" in result.used["models"]
    assert os.environ.get("HANDOFF_DEPTH") is None
