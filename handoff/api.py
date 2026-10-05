"""
`handoff api`: how the Ixel window reads and changes a board. One JSON request on stdin, one JSON
reply on stdout, and nothing else on stdout:

    {"schema": 1, "op": "board", "project": "/abs/path/to/repo", "args": {...}}
    -> {"schema": 1, "ok": true, "op": "board", "data": {...}}                      exit 0
    -> {"schema": 1, "ok": false, "op": "board", "error": {"code": ..., "message": ...}}  exit 1

The window is the person's own, so everything here acts as the person (`human`), with the same rules
the CLI has: the board still refuses what it refuses. Reading never makes a board; only `init` does.
Approval seals never leave Handoff. Every input comes on stdin (the command line is always just
`api`), so nothing a task says passes through a shell. Bytes: stdin is UTF-8 (a BOM is fine) and
stdout is ASCII-only JSON, so a Windows code page can't mangle either one.

Within schema 1, replies only ever gain fields. Error codes: usage (a malformed request), no_project,
no_board, not_found, forbidden, invalid (the board refused it), changed (approve's `shown` isn't the task
any more: show it again), busy, internal.

Approving what the person saw: `task` gives the task's `content`, a hash of its text and history as the
agent would get them. Pass it back as approve's `shown`, and the approval is refused (changed) if an agent
changed the task in between, instead of sealing something the person never saw.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from handoff import __version__
from handoff.board import (ACTIVE, HUMAN, RUN_KINDS, STATE_EVENTS, TERMINAL, Board, BoardError, Changed, Event,
                           Forbidden, NotFound, Task, check_name, content_of, parse_task_id, run_kind, safe_name,
                           task_ref)
from handoff.ixel import DEPTH_VAR, depth
from handoff.project import BOARD_DIR, ProjectError, board_path
from handoff.sanitize import clean_line

SCHEMA = 1
MAX_REQUEST_BYTES = 256 * 1024
DONE_SHOWN = 20             # finished tasks on the board, newest first, unless asked for more
MAX_DONE_SHOWN = 500
MAX_OUTPUTS = 50
_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
WRITE_OPS = ("init", "add", "assign", "note", "review", "status", "delete", "approve", "revoke", "run.start")
READ_OPS = ("hello", "revision", "board", "task", "agents", "inbox")
OPS = READ_OPS + WRITE_OPS


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── Reading the request ───────────────────────────────────────────────────────

def read_request(raw: bytes) -> dict:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ApiError("usage", f"The request is bigger than {MAX_REQUEST_BYTES // 1024} KB.")
    try:
        request = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError("usage", f"The request isn't JSON: {exc}") from None
    if not isinstance(request, dict):
        raise ApiError("usage", "The request must be a JSON object.")
    if request.get("schema") != SCHEMA:
        raise ApiError("usage", f"This Handoff speaks schema {SCHEMA}; the request asked for "
                                f"{request.get('schema')!r}. Update Ixel or Handoff so they match.")
    if request.get("op") not in OPS:
        raise ApiError("usage", f"Unknown op {str(request.get('op'))[:40]!r}.")
    if not isinstance(request.get("args", {}), dict):
        raise ApiError("usage", "args must be a JSON object.")
    return request


def _project(request: dict) -> Path:
    value = request.get("project")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ApiError("no_project", "Say which project: the folder of a git repository.")
    path = Path(value)
    if not path.is_absolute():
        raise ApiError("no_project", "The project must be a full path.")
    from handoff.project import find_project_root
    try:
        found = path.is_dir()
    except OSError:  # a path too long for the system, say
        found = False
    if not found:
        raise ApiError("no_project", f"There's no folder at {value}.")
    root = find_project_root(path)
    if root is None:
        raise ApiError("no_project", f"{value} isn't in a git repository, and Handoff keeps its board in one.")
    return root


def _existing_board(root: Path) -> Board | None:
    """The project's board, or None if it has none yet (never made here)."""
    if not board_path(root).exists():
        return None
    return Board.open(root, create=False)


def _board(root: Path) -> Board:
    board = _existing_board(root)
    if board is None:
        raise ApiError("no_board", f"There's no board in {root} yet.")
    return board


def _arg(args: dict, name: str, kind: type, required: bool = True, default=None):
    value = args.get(name, default)
    if value is None and not required:
        return default
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise ApiError("usage", f"args.{name} must be {kind.__name__}.")
    return value


def _task_arg(args: dict) -> int:
    value = args.get("task")
    if isinstance(value, bool) or not isinstance(value, (int, str)) or isinstance(value, int) and value < 1:
        raise ApiError("usage", "args.task must be a task, like T-12.")
    return parse_task_id(str(value))


def _name(value: object, what: str) -> str | None:
    """A name from the window: an agent, "human" (the person), or None (nobody)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ApiError("usage", f"{what} must be a name.")
    return check_name(value, what)


# ── What the window shows ─────────────────────────────────────────────────────

def _who(name: str) -> str:
    """Names as the person reads them in their window: `human` is them."""
    return "you" if name == HUMAN else name


def _when(event: Event | None) -> dict | None:
    if event is None:
        return None
    from handoff.describe import describe
    return {"at": event.at, "actor": event.actor, "kind": event.kind, "summary": describe(event, _who)}


def _run_state(task: Task, state: Event | None, approved: dict[int, Event]) -> dict | None:
    """running: a worker has it now; approved: it waits for `handoff run`."""
    if state is not None and state.kind == "worker" and state.data.get("state") == "started" \
            and task.status not in TERMINAL:
        return {"state": "running", "agent": safe_name(state.actor), "kind": str(state.data.get("kind", "edit"))}
    if task.id in approved:
        approval = approved[task.id]
        found = {"state": "approved", "agent": safe_name(approval.data.get("worker")),
                 "kind": str(approval.data.get("kind", "edit"))}
        target = approval.data.get("target")
        if isinstance(target, dict) and isinstance(target.get("label"), str):
            found["of"] = clean_line(target["label"])[:300]  # a review of a pull request, say
        return found
    return None


def _summary(task: Task, last: Event | None, state: Event | None, approved: dict[int, Event],
             overlaps: int) -> dict:
    return {"ref": task.ref, "id": task.id, "title": task.title, "status": task.status, "assignee": task.assignee,
            "waiting_on": task.waiting_on, "branch": task.branch,
            "parent": task_ref(task.parent_id) if task.parent_id else None, "created_by": task.created_by,
            "created_at": task.created_at, "updated_at": task.updated_at, "last_event": _when(last),
            "run": _run_state(task, state, approved), "overlaps": overlaps}


def _overlap_counts(board: Board, tasks: list[Task]) -> dict[int, int]:
    from handoff.globs import overlaps
    live = {t.id for t in board.tasks(statuses=sorted(ACTIVE))}
    claims = [c for c in board.active_claims() if c.task_id in live]
    counts: dict[int, int] = {}
    for task in tasks:
        mine = [c.path_glob for c in claims if c.task_id == task.id]
        counts[task.id] = sum(1 for path in mine for c in claims
                              if c.task_id != task.id and overlaps(path, c.path_glob))
    return counts


def _summaries(board: Board, tasks: list[Task]) -> list[dict]:
    ids = [t.id for t in tasks]
    last = board.last_events(ids, skip=("claim_paths", "release_paths"))
    states = board.last_events(ids, kinds=STATE_EVENTS)
    approved = {task.id: approval for task, approval in board.pending_runs(None, ids)}
    counts = _overlap_counts(board, tasks)
    return [_summary(t, last.get(t.id), states.get(t.id), approved, counts.get(t.id, 0)) for t in tasks]


def actions(task: Task, run: dict | None) -> list[dict]:
    """What the person can do with a task now, as the window's buttons, best first. Each names the op it
    runs; `needs` is what the person has to type or pick first. The board checks each again when it's
    pressed, so this list is a guide, not the rule."""
    found: list[dict] = []

    def add(op: str, label: str, primary: bool = False, **extra) -> None:
        found.append({"op": op, "label": label, "primary": primary, **extra})

    who = task.assignee
    if task.status in TERMINAL:
        add("status", "Reopen", True, args={"to": "open"})
        add("delete", "Delete", confirm=True)
        return found
    if run is not None and run["state"] == "running":
        add("note", "Add a note", needs="text")
        add("status", "Cancel", args={"to": "cancelled"}, confirm=True)
        return found
    if run is not None and run["state"] == "approved":
        add("run.start", "Run it now", True, confirm=True)
        add("revoke", "Withdraw the approval")
    if task.status == "in_review":
        add("review", "Approve", who == HUMAN, args={"verdict": "approve"})
        add("review", "Send back", who == HUMAN, args={"verdict": "changes"}, needs="text")
    elif task.status == "blocked":
        add("status", "Reopen", True, args={"to": "open"})
    elif run is None and who not in (None, HUMAN):
        from handoff.asks import IxelRoster, image_provider
        from handoff.worker import AGENTS
        kinds = list(RUN_KINDS) if who in AGENTS else [k for k in RUN_KINDS if k != "edit"]
        # Pictures only from an agent that stands for a picture service (grok is xAI, gpt is OpenAI). This is
        # read from the name alone: asking Ixel would start a program every time the window looks at a task
        if image_provider(who, IxelRoster([], [])) is None:
            kinds.remove("image")
        add("approve", "Run it now", task.status in ("open", "handed_off"), args={"agent": who, "kind": kinds[0]},
            kinds=kinds, needs="kind", confirm=True)
    if task.status != "in_review":
        add("status", "Mark done", who == HUMAN, args={"to": "done"})
        add("assign", "Assign", who is None, needs="agent")
        if task.status != "blocked":
            add("status", "Block", args={"to": "blocked"}, needs="text")
    add("note", "Add a note", needs="text")
    add("status", "Cancel", args={"to": "cancelled"}, confirm=True)
    add("delete", "Delete", confirm=True)
    return found


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows junction (which is_symlink() doesn't see)."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == 0xA0000003


def _outputs(root: Path, task: Task) -> list[dict]:
    """The files a run left in .handoff/outputs/T-N (answers, pictures): plain files only, never a link or a
    file with another name elsewhere (a hard link)."""
    folder = root / BOARD_DIR / "outputs" / task.ref
    found = []
    try:
        if any(_is_link(f) for f in (folder.parent.parent, folder.parent, folder)) or not folder.is_dir():
            return []
        with os.scandir(folder) as entries:
            for entry in entries:
                if len(found) >= MAX_OUTPUTS:
                    break
                if _OUTPUT_NAME.match(entry.name) and entry.is_file(follow_symlinks=False):
                    st = os.lstat(entry.path)  # not entry.stat(): on Windows that has no link count
                    if st.st_nlink == 1 and getattr(st, "st_reparse_tag", 0) == 0:
                        found.append({"name": entry.name, "size": st.st_size})
    except OSError:
        return found
    return sorted(found, key=lambda item: item["name"])


def _event(event: Event) -> dict:
    from handoff.describe import checks_text, describe, handoff_text, panel_text, string_list
    detail = ""
    if event.kind == "handoff":
        detail = handoff_text(event.data)
    elif event.kind in ("claim_paths", "release_paths"):
        detail = "\n".join(string_list(event.data.get("paths")))
    elif event.kind == "review" and checks_text(event.data.get("checks")):
        detail = checks_text(event.data["checks"])
    panel = event.data.get("panel")
    return {"id": event.id, "at": event.at, "actor": event.actor, "kind": event.kind, "summary": describe(event, _who),
            "text": event.text, "detail": detail,
            "panel": panel_text(panel) if isinstance(panel, dict) else ""}


# ── The ops ───────────────────────────────────────────────────────────────────

# What this Handoff can do beyond the ops, for a window that needs it. run-target: a review or a change can
# be approved for a pull request's commits (approve's args.target), which an older Handoff would ignore.
# approve-shown: approve's args.shown (task's `content`) refuses an approval of a task that changed since.
FEATURES = ("run-target", "approve-shown")


def op_hello(request: dict) -> dict:
    return {"handoff_version": __version__, "schema": SCHEMA, "ops": list(OPS), "features": list(FEATURES)}


def op_revision(request: dict) -> dict:
    board = _existing_board(_project(request))
    return {"exists": board is not None, "revision": board.revision() if board else ""}


def op_board(request: dict) -> dict:
    root = _project(request)
    board = _existing_board(root)
    if board is None:
        return {"exists": False, "revision": "", "project": str(root), "counts": {}, "tasks": []}
    args = request.get("args", {})
    shown = _arg(args, "done", int, required=False, default=DONE_SHOWN)
    if shown < 0:
        raise ApiError("usage", "args.done must be 0 or more.")
    shown = min(shown, MAX_DONE_SHOWN)
    revision = board.revision()
    tasks = board.tasks(statuses=sorted(ACTIVE)) + board.tasks(statuses=sorted(TERMINAL), limit=shown)
    # counts has only the statuses Handoff knows; a task it can't read is only counted, never listed
    return {"exists": True, "revision": revision, "project": str(root), "counts": board.counts(),
            "unreadable": board.unreadable(), "done_rule": board.done_rule, "tasks": _summaries(board, tasks)}


def op_task(request: dict) -> dict:
    root = _project(request)
    board = _board(root)
    task_id = _task_arg(request.get("args", {}))
    task, events, claims, children = board.task_with_history(task_id)
    summary = _summaries(board, [task])[0]
    # Per kind of run, what approving it again is of (Board.approve keeps it, while its approval checks out)
    targets = board.approval_targets(task.id)
    return {"task": {**summary, "body": task.body, "acceptance": task.acceptance, "targets": targets,
                     "content": content_of(task, events)},  # (approve's `shown`)
            "events": [_event(e) for e in events], "claims": [c.path_glob for c in claims],
            "overlaps": [o.describe() for o in board.overlaps_for(task.id)],
            "children": _summaries(board, children), "outputs": _outputs(root, task),
            "actions": actions(task, summary["run"])}


def op_agents(request: dict) -> dict:
    from handoff import crew
    root = _project(request)
    board = _existing_board(root)
    roster = crew.build_roster(board or Board(board_path(root)), root, check_edits=board is not None)
    return {"agents": [{"name": m.name, "label": m.label, "kinds": m.kinds, "notes": m.notes}
                       for m in roster.members.values()], "problems": roster.problems}


def op_inbox(request: dict) -> dict:
    """Who's waiting on `agent`, by task ref only: no titles or text (a hook shows this to a model)."""
    board = _existing_board(_project(request))
    agent = check_name(_arg(request.get("args", {}), "agent", str), "agent")
    if board is None:
        return {"exists": False, "refs": [], "counts": {}}
    inbox = board.inbox(agent)
    return {"exists": True, "refs": [t.ref for t in inbox.mine],
            "counts": {"mine": len(inbox.mine), "blocked_on_me": len(inbox.blocked_on_me),
                       "unassigned": len(inbox.unassigned)}}


def op_init(request: dict) -> dict:
    root = _project(request)
    Board.open(root, create=True)
    return {"exists": True, "project": str(root)}


def _task_reply(board: Board, task: Task) -> dict:
    return {"task": _summaries(board, [task])[0]}


def op_add(request: dict) -> dict:
    board = _board(_project(request))
    args = request.get("args", {})
    acceptance = _arg(args, "acceptance", list, required=False, default=[])
    if not all(isinstance(check, str) for check in acceptance):
        raise ApiError("usage", "args.acceptance must be a list of strings.")
    task, _ = board.create(HUMAN, _arg(args, "title", str), _arg(args, "body", str, required=False, default=""),
                           acceptance, _name(args.get("assignee"), "assignee"))
    return _task_reply(board, task)


def op_assign(request: dict) -> dict:
    board = _board(_project(request))
    args = request.get("args", {})
    return _task_reply(board, board.assign(HUMAN, _task_arg(args), _name(args.get("to"), "to")))


def op_note(request: dict) -> dict:
    board = _board(_project(request))
    args = request.get("args", {})
    return _task_reply(board, board.note(HUMAN, _task_arg(args), _arg(args, "text", str)))


def op_review(request: dict) -> dict:
    board = _board(_project(request))
    args = request.get("args", {})
    checks = _arg(args, "checks", list, required=False, default=[])
    task = board.review(HUMAN, _task_arg(args), _arg(args, "verdict", str),
                        _arg(args, "note", str, required=False, default=""), checks=checks)
    return _task_reply(board, task)


def op_status(request: dict) -> dict:
    board = _board(_project(request))
    args = request.get("args", {})
    task_id, to = _task_arg(args), _arg(args, "to", str)
    reason = _arg(args, "reason", str, required=False, default="")
    if to == "cancelled":
        return _task_reply(board, board.cancel(HUMAN, task_id, reason))
    if to not in ("open", "blocked", "done"):
        raise ApiError("usage", 'args.to must be "open", "blocked", "done" or "cancelled".')
    task = board.set_status(HUMAN, task_id, to, reason, _name(args.get("waiting_on"), "waiting_on"))
    return _task_reply(board, task)


def op_delete(request: dict) -> dict:
    board = _board(_project(request))
    task_id = _task_arg(request.get("args", {}))
    board.delete(HUMAN, task_id)
    return {"deleted": task_ref(task_id)}


def op_approve(request: dict) -> dict:
    from handoff.worker import AGENTS
    board = _board(_project(request))
    args = request.get("args", {})
    agent = _name(args.get("agent"), "agent")
    kind = _arg(args, "kind", str, required=False, default="edit")
    if agent is None:
        raise ApiError("usage", "Say which agent runs it.")
    if kind not in RUN_KINDS:
        raise ApiError("usage", f"args.kind must be one of: {', '.join(RUN_KINDS)}.")
    if kind == "edit" and agent not in AGENTS:
        raise BoardError(f"Only {' and '.join(AGENTS)} can change files. {agent} can answer, review your changes "
                         "or make pictures instead.")
    return_to = _name(args.get("return_to"), "return_to") or HUMAN
    target = args.get("target")
    if target is not None and not isinstance(target, dict):
        raise ApiError("usage", "args.target must be an object: base, head and label.")
    shown = _arg(args, "shown", str, required=False, default=None)  # the `content` of the task you showed
    return _task_reply(board, board.approve(HUMAN, _task_arg(args), agent, return_to, kind, target, shown))


def op_revoke(request: dict) -> dict:
    board = _board(_project(request))
    return _task_reply(board, board.revoke_approval(HUMAN, _task_arg(request.get("args", {}))))


def run_command(root: Path, ref: str) -> list[str]:
    return [sys.executable, "-I", "-m", "handoff", "run", ref, "--project", str(root), "--json"]


def op_run_start(request: dict) -> dict:
    """Start `handoff run T-N` on its own: it keeps going when the window closes, and its result lands
    on the board, which the window is watching."""
    from handoff.ixel import DEPTH_VAR, depth
    if depth() > 0:
        raise Forbidden(f"This was started by Handoff ({DEPTH_VAR} is set), and an agent Handoff started can't "
                        "start more agents.")
    root = _project(request)
    board = _board(root)
    task_id = _task_arg(request.get("args", {}))
    pending = board.pending_runs(None, [task_id])
    if not pending:
        raise BoardError(f"{task_ref(task_id)} isn't approved to run (or it changed since it was approved).")
    task, approval = pending[0]
    if run_kind(approval) == "edit":
        # The checks `handoff run` makes before an edit run starts, made here: the run below has nowhere to
        # say why it couldn't start (an answer that can't start says so on the board itself)
        from handoff.worker import Worker, WorkerUnavailable
        try:
            Worker(board, root, task.assignee or "").check()
        except WorkerUnavailable as exc:
            raise ApiError("invalid", f"{exc} It's still approved, so it can run once that's fixed.") from None
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # the person started it, as `handoff run` at a prompt
    if os.name == "nt":
        # A hidden console of its own, which the agents it starts share (no windows flash up), in its own
        # group, so it keeps going when the window that asked for it closes
        extra: dict = {"creationflags": 0x00000200 | 0x08000000}  # NEW_PROCESS_GROUP | NO_WINDOW
    else:
        extra = {"start_new_session": True}
    proc = subprocess.Popen(run_command(root, task_ref(task_id)), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(root), env=env,
                            close_fds=True, **extra)
    return {"started": task_ref(task_id), "pid": proc.pid}


HANDLERS = {
    "hello": op_hello, "revision": op_revision, "board": op_board, "task": op_task, "agents": op_agents,
    "inbox": op_inbox, "init": op_init, "add": op_add, "assign": op_assign, "note": op_note, "review": op_review,
    "status": op_status, "delete": op_delete, "approve": op_approve, "revoke": op_revoke, "run.start": op_run_start,
}
assert set(HANDLERS) == set(OPS)


def handle(raw: bytes) -> tuple[dict, int]:
    """The reply to one request, and the exit code."""
    op = None
    try:
        request = read_request(raw)
        op = request["op"]
        if op in WRITE_OPS and depth() > 0:  # the window is the person's; an agent Handoff started isn't
            raise ApiError("forbidden", f"This was started by Handoff ({DEPTH_VAR} is set), so it can't change "
                                        "the board as the person.")
        data = HANDLERS[op](request)
        return {"schema": SCHEMA, "ok": True, "op": op, "data": data}, 0
    except ApiError as exc:
        code, message = exc.code, str(exc)
    except NotFound as exc:
        code, message = ("no_board" if "no board" in str(exc) else "not_found"), str(exc)
    except Forbidden as exc:
        code, message = "forbidden", str(exc)
    except Changed as exc:
        code, message = "changed", str(exc)
    except BoardError as exc:
        code, message = "invalid", str(exc)
    except ProjectError as exc:
        code, message = "no_project", str(exc)
    except sqlite3.OperationalError as exc:
        busy = "locked" in str(exc) or "busy" in str(exc)
        code, message = ("busy", "The board is busy; try again.") if busy else ("internal", f"The board: {exc}")
    except Exception as exc:  # noqa: BLE001 — the window always gets an answer it can show
        code, message = "internal", f"{type(exc).__name__}: {exc}"
    return {"schema": SCHEMA, "ok": False, "op": op, "error": {"code": code, "message": message[:2000]}}, 1


def main(argv: list[str]) -> int:
    if argv:
        sys.stderr.write("handoff api takes no arguments: it reads one JSON request on stdin.\n")
        return 2
    stdin = getattr(sys.stdin, "buffer", None)
    raw = stdin.read(MAX_REQUEST_BYTES + 1) if stdin is not None else sys.stdin.read().encode("utf-8")
    reply, code = handle(raw)
    out = (json.dumps(reply, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    stdout = getattr(sys.stdout, "buffer", None)
    if stdout is not None:
        stdout.write(out)
        stdout.flush()
    else:
        sys.stdout.write(out.decode("ascii"))
    return code

