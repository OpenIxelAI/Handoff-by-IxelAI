"""
The worker's model on your own computer: Ollama, LM Studio, or any server with an OpenAI-style API.

Such a model is only a model, not an agent, so Handoff drives it: it sends the brief and the task, and gives
the model four tools, which Handoff runs itself, only on files in the run's worktree:

- list_files(path): the files under a folder (".git" and links left out);
- read_file(path): a text file, up to READ_LIMIT bytes;
- write_file(path, content): makes or replaces a file;
- replace_in_file(path, old, new): changes the one place `old` appears.

A path that leaves the worktree (an absolute path, "..", a link) or goes into .git is refused, and the model
is told so. Nothing runs commands. The run stops when the model answers without asking for a tool, after
MAX_TURNS turns, or at its time limit.

Small models think out loud (Qwen3's <think> blocks): that's taken out of the reply, and effort "none" asks the
server not to think at all (Ollama's think = false, and Qwen3's /no_think). A thinking-only model (Qwen3-4B-Thinking-2507,
say) thinks anyway; its thinking comes back apart from the reply.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from handoff.proc import Stopped, stopping

MAX_TURNS = 40
READ_LIMIT = 100_000
WRITE_LIMIT = 400_000
LIST_LIMIT = 400
RESPONSE_LIMIT = 8_000_000
LIST_TIMEOUT_SEC = 2.0

SYSTEM = ("You're a careful coding assistant working through tools on files in one folder. Use the tools to read "
          "what you need and to make the change. Keep changes small and exactly what the task asks. When you're "
          "done, stop calling tools and reply.")

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List the files under a folder of the project (\".\" for all of it).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file of the project.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create a file, or replace a file's whole content.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "replace_in_file",
        "description": "Replace the one place `old` appears in a file with `new` (old must appear exactly once).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                                        "new": {"type": "string"}},
                       "required": ["path", "old", "new"]}}},
]

_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


class ServerError(Exception):
    """The model server can't be reached or answered with an error; the message says which."""


def _loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def _open(url: str, body: dict | None, key: str | None, timeout: float) -> dict:
    """GET (body None) or POST JSON to the server; its JSON reply. A server on this computer is reached directly,
    never through a proxy."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    handlers = [urllib.request.ProxyHandler({})] if _loopback(url) else []
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with opener.open(request, timeout=max(timeout, 0.1)) as response:
            raw = response.read(RESPONSE_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2000).decode("utf-8", "replace")
            detail = json.loads(detail).get("error", detail)
            detail = detail.get("message", detail) if isinstance(detail, dict) else detail
        except (OSError, ValueError, AttributeError):
            pass
        raise ServerError(f"the server said {exc.code}" + (f": {str(detail)[:300]}" if detail else "")) from None
    except TimeoutError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise reason from None
        raise ServerError(f"it can't be reached ({reason})") from None
    if len(raw) > RESPONSE_LIMIT:
        raise ServerError("its answer was too big")
    try:
        return json.loads(raw)
    except ValueError:
        raise ServerError("its answer wasn't JSON") from None


def list_models(url: str, key: str | None = None, timeout: float = LIST_TIMEOUT_SEC) -> list[str]:
    """The models the server has (GET /models). ServerError when it isn't there."""
    try:
        data = _open(f"{url}/models", None, key, timeout)
    except TimeoutError:
        raise ServerError("it didn't answer in time") from None
    found = data.get("data") if isinstance(data, dict) else None
    if not isinstance(found, list):
        raise ServerError("it didn't list its models")
    return [m["id"] for m in found if isinstance(m, dict) and isinstance(m.get("id"), str)][:200]


# ── The tools, on files in the worktree only ─────────────────────────────────

class _Refused(Exception):
    pass


def _resolve(root: Path, path: object) -> Path:
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise _Refused("give a path, relative to the project's folder")
    rel = Path(path.strip().replace("\\", "/"))
    if rel.is_absolute() or rel.anchor or ".." in rel.parts:  # (on Windows, /etc/x has a root but no drive)
        raise _Refused(f"{path} is outside the project: give a path inside it, like src/app.py")
    if ".git" in rel.parts:
        raise _Refused("the .git folder is off limits")
    current = root
    for part in rel.parts:
        if part in ("", "."):
            continue
        current = current / part
        if current.is_symlink():
            raise _Refused(f"{path} goes through a link, which the tools don't follow")
    return current


def _list(root: Path, args: dict) -> str:
    folder = _resolve(root, args.get("path") or ".")
    if not folder.is_dir():
        raise _Refused(f"{args.get('path')} isn't a folder")
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            full = Path(dirpath, name)
            if name != ".git" and not full.is_symlink():  # (a worktree's .git is a file)
                found.append(full.relative_to(root).as_posix())
            if len(found) >= LIST_LIMIT:
                return "\n".join(found) + "\n(more files not shown: list a folder inside it)"
    return "\n".join(found) or "(no files)"


def _read(root: Path, args: dict) -> str:
    path = _resolve(root, args.get("path"))
    if not path.is_file():
        raise _Refused(f"there's no file {args.get('path')}")
    data = path.read_bytes()
    if b"\0" in data[:8000]:
        raise _Refused(f"{args.get('path')} isn't a text file")
    text = data[:READ_LIMIT].decode("utf-8", "replace")
    return text + (f"\n(cut at {READ_LIMIT} bytes)" if len(data) > READ_LIMIT else "")


def _write(root: Path, args: dict) -> str:
    path = _resolve(root, args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise _Refused("give the file's content as text")
    data = content.encode("utf-8")
    if len(data) > WRITE_LIMIT:
        raise _Refused(f"that's over {WRITE_LIMIT} bytes")
    if path.is_dir():
        raise _Refused(f"{args.get('path')} is a folder")
    path.parent.mkdir(parents=True, exist_ok=True)
    _resolve(root, args.get("path"))  # (no link made meanwhile)
    existed = path.exists()
    path.write_bytes(data)
    return f"{'Replaced' if existed else 'Created'} {args.get('path')} ({len(data)} bytes)."


def _replace(root: Path, args: dict) -> str:
    path = _resolve(root, args.get("path"))
    old, new = args.get("old"), args.get("new")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise _Refused("give old (the text to find, exactly) and new")
    if not path.is_file():
        raise _Refused(f"there's no file {args.get('path')}")
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise _Refused(f"old appears {count} times in {args.get('path')}; it must appear exactly once (read the file, "
                       "and include more of the lines around it)")
    updated = text.replace(old, new)
    if len(updated.encode("utf-8")) > WRITE_LIMIT:
        raise _Refused(f"the file would be over {WRITE_LIMIT} bytes")
    path.write_text(updated, encoding="utf-8")
    return f"Changed {args.get('path')}."


TOOL_RUNNERS = {"list_files": _list, "read_file": _read, "write_file": _write, "replace_in_file": _replace}


def run_tool(root: Path, name: str, raw_args: object) -> str:
    """One tool call, as the model asked for it; what to tell the model."""
    runner = TOOL_RUNNERS.get(name)
    if runner is None:
        return f"Error: there's no tool {name!r}. The tools are: {', '.join(TOOL_RUNNERS)}."
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise _Refused("the arguments must be a JSON object")
        return runner(Path(os.path.realpath(root)), args)
    except ValueError:
        return "Error: the arguments weren't valid JSON."
    except _Refused as exc:
        return f"Error: {exc}."
    except (OSError, UnicodeDecodeError) as exc:
        return f"Error: {type(exc).__name__}: {exc}"


# ── The run ──────────────────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # (the chat template opened it, so only its end is in the reply)
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def run(url: str, model: str, prompt: str, cwd: str, timeout: float, effort: str | None = None,
        key: str | None = None) -> "subprocess.CompletedProcess[str]":
    """Drive the model on the task, in `cwd`. Its result as an agent's would be: stdout holds JSON with its reply,
    what it used ({"in", "out"} tokens) and what went wrong, if anything (returncode 1 then).
    subprocess.TimeoutExpired at the time limit; Stopped if you stopped the runs (Ctrl+C)."""
    deadline = time.monotonic() + timeout
    system = SYSTEM + (" /no_think" if effort == "none" and model.lower().startswith("qwen3") else "")
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    used = {"in": 0, "out": 0}
    reply, error, changed = "", None, 0

    def result(code: int) -> "subprocess.CompletedProcess[str]":
        out = json.dumps({"reply": reply, "usage": used, "error": error, "model": model, "changed": changed})
        return subprocess.CompletedProcess([url, model], code, out, "")

    for _turn in range(MAX_TURNS):
        if stopping():
            raise Stopped("stopped")
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired([url, model], timeout)
        body: dict = {"model": model, "messages": messages, "tools": TOOLS, "stream": False}
        if effort == "none":
            # Ollama's own switch (other servers ignore it). Not reasoning_effort "none": Ollama's OpenAI API takes
            # that to mean "don't split the thinking out", and a model that thinks anyway puts it in the reply
            body["think"] = False
        elif effort:
            body["reasoning_effort"] = effort
        try:
            data = _open(f"{url}/chat/completions", body, key, left)
        except TimeoutError:
            raise subprocess.TimeoutExpired([url, model], timeout) from None
        except ServerError as exc:
            error = f"The model server at {url} failed: {exc}"
            return result(1)
        counts = data.get("usage") if isinstance(data, dict) else None
        if isinstance(counts, dict):
            for ours, theirs in (("in", "prompt_tokens"), ("out", "completion_tokens")):
                value = counts.get(theirs)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    used[ours] += value
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) \
            else None
        if not isinstance(message, dict):
            error = "The model server's answer had no message."
            return result(1)
        calls = message.get("tool_calls") or []
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        if not calls:
            reply = strip_thinking(content)
            return result(0)
        messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
        for n, call in enumerate(calls if isinstance(calls, list) else []):
            fn = call.get("function") if isinstance(call, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            output = run_tool(Path(cwd), name if isinstance(name, str) else "", fn.get("arguments") if fn else None)
            if name in ("write_file", "replace_in_file") and not output.startswith("Error"):
                changed += 1
            messages.append({"role": "tool", "tool_call_id": str(call.get("id") or f"call_{n}"), "content": output})
    error = f"The model was still asking for tools after {MAX_TURNS} turns, so the run stopped there."
    reply = strip_thinking(content)
    return result(1)
