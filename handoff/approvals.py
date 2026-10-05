"""
Approvals for the worker carry a seal made with a key that lives in your user folder, outside every
project, so a board that arrives with a cloned repo can't carry one. The worker runs a task only if its
approval was made on this computer, by `handoff approve`, for this board.

The key is 32 random bytes, made the first time you approve a task:

    Windows   %APPDATA%\\Handoff\\approval.key
    macOS     ~/Library/Application Support/Handoff/approval.key
    Linux     $XDG_CONFIG_HOME/handoff/approval.key (~/.config/handoff/approval.key)

HANDOFF_APPROVAL_KEY names another file (the tests use it). The seal is an HMAC-SHA256 of the board's
own path, the task, who runs it, what kind of run it is (edit, answer, review or image), who gets it
back, when it was approved and a random nonce, so an approval can't be moved to another board or task,
or turned from an answer into an edit, either. It also seals a hash of what the agent will be given (the
task's title, body and checks, and its history up to the approval), so that can't be changed afterwards
by writing to the board. A review or a change of a pull request also seals its two commits (its
`target`: a review reads them, a change starts from its head), so it can't be pointed at other code.

Each approval is good for one run. When a run starts, its nonce is added to a list next to the key
(approvals-used), and an approval whose nonce is on it never runs again, even if it's copied back into
the board.

None of this stops a program that runs as you and can read the key (an agent with your file access could
approve itself whatever it likes anyway); it stops approvals you never made from reaching the worker.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Mapping

KEY_BYTES = 32
KEY_NAME = "approval.key"
USED_NAME = "approvals-used"
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_HEX = re.compile(r"^[0-9a-f]{32,64}$")


def key_path(env: Mapping[str, str] = os.environ, system: str = sys.platform, home: Path | None = None) -> Path:
    if env.get("HANDOFF_APPROVAL_KEY"):
        return Path(env["HANDOFF_APPROVAL_KEY"]).expanduser()
    home = home or Path.home()
    if system == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming", "Handoff", KEY_NAME)
    if system == "darwin":
        return home / "Library" / "Application Support" / "Handoff" / KEY_NAME
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config", "handoff", KEY_NAME)


def load_key(create: bool = False, path: Path | None = None) -> bytes | None:
    """This computer's approval key; None if there isn't one (nothing was ever approved here)."""
    path = path or key_path()
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            return load_key(create=False, path=path)  # another approve just made it
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(KEY_BYTES))
        key = path.read_bytes()
    return key if len(key) == KEY_BYTES else None


def used_path() -> Path:
    """The list of approvals that already ran, next to the key: outside every board, so writing a board
    can't take one off it."""
    return key_path().with_name(USED_NAME)


def mark_used(nonce: str) -> None:
    """Record that an approval's run started. OSError if it can't be recorded (then the run doesn't start)."""
    if not _HEX.fullmatch(nonce):
        raise ValueError("not an approval's nonce")
    path = used_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "a", encoding="ascii", newline="\n") as handle:
        handle.write(nonce + "\n")


def is_used(nonce: str) -> bool:
    try:
        text = used_path().read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return False
    return nonce in text.split()


def content_hash(title: str, body: str, acceptance: list[str], history: list[Mapping]) -> str:
    """What the agent is given for a task, as one hash: its text and its history (each event's time, who,
    what and its text and data)."""
    data = {"title": title, "body": body, "acceptance": list(acceptance), "history": list(history)}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True, default=str).encode("ascii")).hexdigest()


def board_identity(board_path: Path) -> str:
    return os.path.normcase(os.path.realpath(board_path))


TARGET_KINDS = ("review", "edit")  # the runs that can be of given commits


def check_target(target: object) -> dict[str, str] | None:
    """A run's target as an approval keeps it: {"base", "head"} (full commit ids) and "label" (what it is,
    in words). A review reads what's on `head` since it left `base`; a change starts from `head`. None when
    it isn't one."""
    if not isinstance(target, Mapping) or set(target) != {"base", "head", "label"}:
        return None
    base, head, label = target["base"], target["head"], target["label"]
    if not (isinstance(base, str) and _SHA.fullmatch(base) and isinstance(head, str) and _SHA.fullmatch(head)
            and isinstance(label, str) and 0 < len(label) <= 300 and label.isprintable()):
        return None
    return {"base": base, "head": head, "label": label}


def _message(board_path: Path, task_id: int, worker: str, kind: str, return_to: str, at: str, nonce: str,
             target: Mapping | None = None, content: str | None = None) -> bytes:
    fields = {"board": board_identity(board_path), "task": task_id, "worker": worker, "kind": kind,
              "return_to": return_to, "at": at, "nonce": nonce}
    if target is not None:  # (approvals without one seal what they always did)
        fields["target"] = dict(target)
    if content is not None:  # (approvals from before Handoff sealed it don't run: see Board._sealed)
        fields["content"] = content
    return json.dumps(fields, sort_keys=True, ensure_ascii=True).encode("ascii")


def seal(key: bytes, board_path: Path, task_id: int, worker: str, return_to: str, at: str,
         kind: str = "edit", target: Mapping | None = None, content: str | None = None) -> dict[str, str]:
    """The fields an approval event stores: a fresh nonce and the seal over everything it approves."""
    nonce = secrets.token_hex(16)
    mac = hmac.new(key, _message(board_path, task_id, worker, kind, return_to, at, nonce, target, content),
                   hashlib.sha256).hexdigest()
    return {"nonce": nonce, "seal": mac}


def is_sealed(key: bytes | None, board_path: Path, task_id: int, data: Mapping, at: str) -> bool:
    """Was this approval made here, by `handoff approve`, for exactly this board, task and kind of run (and
    the commits it's of, and the content it sealed)?"""
    nonce, mac, worker, return_to = (data.get(k) for k in ("nonce", "seal", "worker", "return_to"))
    kind = data.get("kind", "edit")  # approvals for an edit didn't always say so
    if key is None or not all(isinstance(v, str) and v for v in (nonce, mac, worker, return_to, kind)):
        return False
    target = None
    if "target" in data:
        target = check_target(data["target"])
        if target is None or kind not in TARGET_KINDS:
            return False
    content = data.get("content")
    if content is not None and not (isinstance(content, str) and _HEX.fullmatch(content)):
        return False
    expected = hmac.new(key, _message(board_path, task_id, worker, kind, return_to, at, nonce, target, content),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, mac)
