"""
`handoff hook session-start --as NAME`: what Claude Code hears when a session starts in a project whose
board has work for it, so "is anything waiting for me?" doesn't depend on someone asking.

It only reads, and it says only task refs and counts: no title, note or anything else an agent, a person or a
cloned repository wrote reaches the model this way, so a task can't put instructions into a session. It says
nothing at all when there's no project, no board or nothing waiting, nothing inside an agent Handoff
started (that agent has its one task), and nothing to Grok Build, which runs Claude Code's hooks too (what waits
for claude isn't Grok's). Whatever goes wrong, it exits 0 quietly: a session must always start.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

EVENTS = ("session-start",)
MAX_STDIN = 64 * 1024
MAX_REFS = 10


def _refs(tasks: list) -> str:
    refs = [t.ref for t in tasks[:MAX_REFS]]
    more = len(tasks) - len(refs)
    return ", ".join(refs) + (f" and {more} more" if more > 0 else "")


def session_start_text(root: Path, me: str) -> str:
    """The lines for `me`, or "" when nothing's waiting. Only reads: it never makes, sets up or changes a board."""
    from handoff.board import Board
    from handoff.project import board_path
    if not board_path(root).is_file():
        return ""
    inbox = Board.open_read_only(root).inbox(me)
    parts = []
    if inbox.mine:
        n = len(inbox.mine)
        parts.append(f"{n} task{'s are' if n > 1 else ' is'} assigned to you ({me}): {_refs(inbox.mine)}.")
    if inbox.blocked_on_me:
        n = len(inbox.blocked_on_me)
        parts.append(f"{n} task{'s are' if n > 1 else ' is'} blocked waiting on you: {_refs(inbox.blocked_on_me)}.")
    if not parts:
        return ""
    first = (inbox.mine or inbox.blocked_on_me)[0].ref
    return ("Handoff, this project's shared task board: " + " ".join(parts) + " Read them with the handoff_inbox "
            f"tool (or `handoff show {first}`) when the user asks what's waiting, and mention them before starting "
            "unrelated work.")


def _start_folder(raw: bytes) -> Path:
    """The session's folder: `cwd` from what Claude Code sends on stdin, else where this runs."""
    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError):
        data = {}
    cwd = data.get("cwd") if isinstance(data, dict) else None
    if isinstance(cwd, str) and cwd and "\x00" not in cwd and os.path.isabs(cwd) and os.path.isdir(cwd):
        return Path(cwd)
    return Path.cwd()


def main(argv: list[str]) -> int:
    from handoff.board import BoardError, check_name
    if not argv or argv[0] not in EVENTS:
        sys.stderr.write(f"handoff hook takes one of: {', '.join(EVENTS)}\n")
        return 1
    me = "claude"
    rest = argv[1:]
    if rest[:1] == ["--as"] and len(rest) == 2:
        try:
            me = check_name(rest[1], "--as")
        except BoardError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
    elif rest:
        sys.stderr.write("usage: handoff hook session-start [--as NAME]\n")
        return 1

    from handoff.ixel import depth
    if depth() > 0:  # an agent Handoff started: it has its one task
        return 0
    if os.environ.get("GROK_HOOK_EVENT"):  # set on every hook Grok Build runs, Claude Code's included
        return 0
    try:
        stdin = getattr(sys.stdin, "buffer", None)
        raw = stdin.read(MAX_STDIN) if stdin is not None and not sys.stdin.isatty() else b""
        from handoff.project import find_project_root
        root = find_project_root(_start_folder(raw))
        text = session_start_text(root, me) if root is not None else ""
    except Exception:  # noqa: BLE001 — a hook that fails must not get in the way of the session
        return 0
    if text:
        out = (text + "\n").encode("utf-8")
        stdout = getattr(sys.stdout, "buffer", None)
        if stdout is not None:
            stdout.write(out)
            stdout.flush()
        else:
            sys.stdout.write(out.decode("utf-8"))
    return 0
