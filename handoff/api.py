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

The shapes of `board` and `task` are board_summary() and task_detail(), which `handoff board --json` and
`handoff show --json` print too, so a script reads the same data the window does.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from itertools import chain, combinations
from pathlib import Path

from handoff import __version__, approvals, usage
from handoff.board import (
    ACTIVE,
    HUMAN,
    RUN_KINDS,
    STATE_EVENTS,
    TERMINAL,
    Board,
    BoardError,
    Changed,
    Claim,
    Event,
    Forbidden,
    NoBoard,
    NotFound,
    Task,
    check_name,
    content_of,
    parse_task_id,
    run_kind,
    safe_name,
    task_ref,
)
from handoff.ixel import DEPTH_VAR, depth
from handoff.project import BOARD_DIR, OUTPUTS_DIR, ProjectError, board_path
from handoff.sanitize import clean_line

SCHEMA = 1
MAX_REQUEST_BYTES = 256 * 1024
DONE_SHOWN = 20             # finished tasks on the board, newest first, unless asked for more
MAX_DONE_SHOWN = 500
MAX_OUTPUTS = 50
_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
WRITE_OPS = ("init", "add", "assign", "note", "review", "status", "delete", "approve", "revoke", "run.start",
             "merge", "agents.enable")
READ_OPS = ("hello", "revision", "board", "task", "agents", "inbox", "models", "fallbacks", "rankings")
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
             overlaps: int, used: tuple[float, dict | None]) -> dict:
    cost, models = used
    return {"ref": task.ref, "id": task.id, "title": task.title, "status": task.status, "assignee": task.assignee,
            "waiting_on": task.waiting_on, "branch": task.branch,
            "parent": task_ref(task.parent_id) if task.parent_id else None, "created_by": task.created_by,
            "created_at": task.created_at, "updated_at": task.updated_at, "last_event": _when(last),
            "run": _run_state(task, state, approved), "overlaps": overlaps,
            "cost": cost, "usage": models}  # (what its runs used: their cost, and model by model)


def _overlap_counts(board: Board, tasks: list[Task]) -> dict[int, int]:
    """How many other active tasks' claims each task's own claims overlap. Each pair of claims is compared
    once (an overlap counts for both), not once per task, and only pairs that can overlap at all: claims that
    start with the same plain folder (src/api.py and src/db.py, not src and docs), and the ones that start
    with a wildcard against every claim (globs.plain_head). A board of hundreds of claims stays quick."""
    from handoff.globs import overlaps, plain_head
    live = {t.id for t in board.tasks(statuses=sorted(ACTIVE))}
    claims = [c for c in board.active_claims() if c.task_id in live]
    counts: dict[int, int] = {task.id: 0 for task in tasks}
    by_head: dict[str, list[Claim]] = {}
    wild: list[Claim] = []
    for claim in claims:
        head = plain_head(claim.path_glob)
        if head is None:
            wild.append(claim)
        else:
            by_head.setdefault(head, []).append(claim)
    pairs = chain((pair for group in by_head.values() for pair in combinations(group, 2)),
                  combinations(wild, 2),
                  ((mine, other) for mine in wild for group in by_head.values() for other in group))
    for mine, other in pairs:
        if mine.task_id == other.task_id or (mine.task_id not in counts and other.task_id not in counts):
            continue
        if overlaps(mine.path_glob, other.path_glob):
            for task_id in (mine.task_id, other.task_id):
                if task_id in counts:
                    counts[task_id] += 1
    return counts


def _summaries(board: Board, tasks: list[Task], used: dict[int, tuple[float, dict | None]] | None = None) -> list[dict]:
    ids = [t.id for t in tasks]
    last = board.last_events(ids, skip=("claim_paths", "release_paths"))
    states = board.last_events(ids, kinds=STATE_EVENTS)
    approved = {task.id: approval for task, approval in board.pending_runs(None, ids)}
    counts = _overlap_counts(board, tasks)
    used = board.run_usage(ids) if used is None else used
    return [_summary(t, last.get(t.id), states.get(t.id), approved, counts.get(t.id, 0), used.get(t.id, (0.0, None)))
            for t in tasks]


def actions(task: Task, run: dict | None, merge_into: str | None = None) -> list[dict]:
    """What the person can do with a task now, as the window's buttons, best first. Each names the op it
    runs; `needs` is what the person has to type or pick first. The board checks each again when it's
    pressed, so this list is a guide, not the rule. `merge_into`: the branch the project has out, when the
    task's branch has a run's work that isn't merged yet."""
    found: list[dict] = []

    def add(op: str, label: str, primary: bool = False, **extra) -> None:
        found.append({"op": op, "label": label, "primary": primary, **extra})

    if merge_into:
        add("merge", f"Merge into {merge_into}", confirm=True)
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
    folder = root / BOARD_DIR / OUTPUTS_DIR / task.ref
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
    elif event.kind == "worker" and usage.check(event.data.get("usage")):
        detail = "\n".join(usage.lines(event.data["usage"]))
    panel = event.data.get("panel")
    used = usage.check(event.data.get("usage")) if event.kind == "worker" else None
    return {"id": event.id, "at": event.at, "actor": event.actor, "kind": event.kind, "summary": describe(event, _who),
            "text": event.text, "detail": detail,
            "panel": panel_text(panel) if isinstance(panel, dict) else "", "usage": used}


def board_summary(board: Board, root: Path, tasks: list[Task], revision: str | None = None) -> dict:
    """The board as the window and `handoff board --json` show it: `tasks` are the ones the caller chose, in the
    order it wants them. `revision` is one the caller read before it fetched the tasks (so a change in between
    shows up on its next poll); left out, it's read now."""
    # counts has only the statuses Handoff knows; a task it can't read is only counted, never listed. spent and
    # usage are what every run on the board used, the tasks it doesn't list included (at API prices, as the
    # agents said): spent in all, usage model by model
    used = board.run_usage()
    return {"project": str(root), "revision": board.revision() if revision is None else revision,
            "counts": board.counts(), "unreadable": board.unreadable(), "done_rule": board.done_rule,
            "spent": round(min(sum(cost for cost, _ in used.values()), usage.MAX_COST), 6),
            "usage": usage.merge(*(models for _, models in used.values())), "tasks": _summaries(board, tasks, used)}


def task_detail(board: Board, root: Path, task_id: int) -> dict:
    """One task as the window and `handoff show --json` show it: the task with its body, acceptance checks and
    what its runs were approved for, its history, claims, overlaps, subtasks, results, and what the person can
    do with it next."""
    task, events, claims, children = board.task_with_history(task_id)
    summary = _summaries(board, [task])[0]
    # Per kind of run, what approving it again is of (Board.approve keeps it, while its approval checks out)
    targets = board.approval_targets(task.id)
    used = usage.merge(*(e.data.get("usage") for e in events if e.kind == "worker"))
    return {"task": {**summary, "body": task.body, "acceptance": task.acceptance, "targets": targets,
                     "content": content_of(task, events),  # (approve's `shown`)
                     "usage": used,  # (every run's, model by model, as the summary's, from this snapshot)
                     "workers": _workers()},
            "events": [_event(e) for e in events], "claims": [c.path_glob for c in claims],
            "overlaps": [o.describe() for o in board.overlaps_for(task.id)],
            "children": _summaries(board, children), "outputs": _outputs(root, task),
            "actions": actions(task, summary["run"], _merge_into(root, task, events))}


def _merge_into(root: Path, task: Task, events: list[Event]) -> str | None:
    """The branch the project has out, when the task's branch has a finished run's work that isn't merged yet
    (what the merge action's label names); else None."""
    if not task.branch or Board.merge_blocker(task, events) is not None:
        return None
    commit = Board.run_commit(events)
    if commit is None or Board.merged_commit(events) == commit:
        return None
    from handoff import gitwork
    # Offered only when it could go through: the branch is still at the run's commit, the checkout is on a branch
    # of its own and lacks it (merged by hand with git, it isn't offered any more)
    return gitwork.merge_ready(root, task.branch, commit)


# ── The ops ───────────────────────────────────────────────────────────────────

# What this Handoff can do beyond the ops, for a window that needs it. run-target: a review or a change can
# be approved for a pull request's commits (approve's args.target), which an older Handoff would ignore.
# approve-shown: approve's args.shown (task's `content`) refuses an approval of a task that changed since.
# run-models: an edit can name its models (approve's args.models: one model, or a chain, biggest first, where the
# first plans and the others do the steps). run-usage: worker runs say what they used (events' and tasks' usage
# and `cost`, on the board too, and the board's `usage` and `spent`: what all its runs used and cost).
# run-effort: an edit can say how hard each model thinks (approve's args.efforts: {model: level}, or {"": level}
# for the agent's own model), in the levels each worker takes (a task's `workers`). run-merge: the person can merge a
# task's branch into their current branch (op merge: args.task, and optional args.shown as approve's).
# any-worker: the worker runs more agents than claude and codex (a board's `workers`, each with its `label`,
# `access`, `local` and `needs_ok`/`enabled`: adapters.py), op models lists the models one can run, and op
# agents.enable lets one run, or stops it (args.agent, args.on).
FEATURES = ("run-target", "approve-shown", "run-models", "run-usage", "run-effort", "run-merge", "any-worker",
            "run-fallback")


def _workers() -> dict:
    """What each worker's edits can be given: its effort levels, least first, and whether it runs a chain; and for
    people, its name, what it can reach (adapters.ACCESS), whether it's a model on this computer (free), and
    whether it waits for you to enable it. Nothing here starts a program or asks a server."""
    from handoff import adapters
    from handoff.worker import AGENTS, CHAIN_AGENTS, access, label
    try:
        config = adapters.load_config()
    except adapters.ConfigError:
        config = {}
    return {name: {"efforts": list(spec.effort_levels), "chain": name in CHAIN_AGENTS, "label": label(spec),
                   "access": access(spec), "local": spec.kind == "local", "needs_ok": spec.needs_ok,
                   "enabled": (not spec.needs_ok) or adapters.enabled(name, config),
                   "default_model": spec.default_model or None}
            for name, spec in AGENTS.items()}


def op_models(request: dict) -> dict:
    """The models a worker can run, as it lists them (a local server's, or `opencode models`): `models`, and
    `default`, the one it runs when an approval names none. Empty where the agent can't say (claude and codex
    take their own names and aliases, like claude-opus-5-5 or haiku)."""
    from handoff.worker import AGENTS, Worker, WorkerUnavailable
    agent = _name(request.get("args", {}).get("agent"), "agent")
    if agent is None or agent not in AGENTS:
        raise ApiError("usage", "args.agent must be one of the workers: " + ", ".join(AGENTS) + ".")
    try:
        worker = Worker(None, Path("."), agent)  # type: ignore[arg-type]  (only lists, never runs)
    except WorkerUnavailable as exc:
        raise BoardError(str(exc)) from None
    models = worker.list_models()
    default = AGENTS[agent].default_model or (models[0] if models and AGENTS[agent].kind == "local" else None)
    return {"agent": agent, "models": models, "default": default}


def op_agents_enable(request: dict) -> dict:
    """Let a worker that runs with its own permissions (or reads outside its worktree) change files, or stop
    letting it: `enabled` in agents.toml, which only the person writes (refused for anything Handoff started)."""
    from handoff import adapters
    from handoff.worker import AGENTS, BUILT_IN, load_agents
    args = request.get("args", {})
    agent = _name(args.get("agent"), "agent")
    on = _arg(args, "on", bool, required=False, default=True)
    if agent is None:
        raise ApiError("usage", "Say which agent: args.agent.")
    if agent in BUILT_IN:
        raise BoardError(f"{agent} doesn't need enabling.")
    if agent not in AGENTS and agent not in adapters.presets():
        raise BoardError(f"Handoff doesn't know {agent}; add it in {adapters.config_path()} first.")
    try:
        adapters.set_enabled(agent, on)
    except adapters.ConfigError as exc:
        raise BoardError(str(exc)) from None
    load_agents()
    return {"agent": agent, "enabled": on, "workers": _workers()}


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
    revision = board.revision()  # before the tasks, so a change between the two shows up on the next poll
    tasks = board.tasks(statuses=sorted(ACTIVE)) + board.tasks(statuses=sorted(TERMINAL), limit=shown)
    return {"exists": True, **board_summary(board, root, tasks, revision)}


def op_task(request: dict) -> dict:
    root = _project(request)
    return task_detail(_board(root), root, _task_arg(request.get("args", {})))


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
    """Delete a task for good, with its history and results. `left`: what of it is still there, in words for the
    person (as a rule, nothing): results that couldn't be removed, or the worker's worktree and branch, which hold
    the agent's work, so Handoff leaves them."""
    root = _project(request)
    board = _board(root)
    task_id = _task_arg(request.get("args", {}))
    left = board.delete(HUMAN, task_id)
    return {"deleted": task_ref(task_id), "left": left + _work_left(root, task_ref(task_id))}


def _work_left(root: Path, ref: str) -> list[str]:
    """The worker's worktree and branch for the deleted task `ref`, if they're still there, in words, with the git
    commands that remove them (as `handoff delete` says it)."""
    from handoff import gitwork
    said = []
    for _, worktree, branch in gitwork.work_left(root, lambda gone: gone == ref):
        both = worktree and branch
        things = " and ".join(word for word, have in (("worktree", worktree), ("branch", branch)) if have)
        commands = [command for command, have in ((f"git worktree remove {(gitwork.WORKTREES / ref).as_posix()}",
                                                   worktree), (f"git branch -D handoff/{ref}", branch)) if have]
        said.append(f"{ref}'s {things} {'are' if both else 'is'} still there, with the agent's work (its commits "
                    f"carry the task's title). If you don't need {'them' if both else 'it'} any more: "
                    f"{', then '.join(commands)}")
    return said


def op_approve(request: dict) -> dict:
    from handoff.worker import AGENTS, CHAIN_AGENTS, effort_problem, names, step_agent
    board = _board(_project(request))
    args = request.get("args", {})
    agent = _name(args.get("agent"), "agent")
    kind = _arg(args, "kind", str, required=False, default="edit")
    if agent is None:
        raise ApiError("usage", "Say which agent runs it.")
    if kind not in RUN_KINDS:
        raise ApiError("usage", f"args.kind must be one of: {', '.join(RUN_KINDS)}.")
    if kind == "edit" and agent not in AGENTS:
        raise BoardError(f"Only {names(AGENTS)} can change files. {agent} can answer, review your changes "
                         "or make pictures instead.")
    return_to = _name(args.get("return_to"), "return_to") or HUMAN
    target = args.get("target")
    if target is not None and not isinstance(target, dict):
        raise ApiError("usage", "args.target must be an object: base, head and label.")
    models = args.get("models")
    if models is not None and not isinstance(models, list):
        raise ApiError("usage", "args.models must be a list of model names, biggest first.")
    if models is not None and len(models) > 1 and agent not in CHAIN_AGENTS:
        raise BoardError(f"A chain of models runs on {' or '.join(CHAIN_AGENTS)}; {agent} takes one model.")
    if models and len(models) > 1 and isinstance(models[0], str) and step_agent(models[0], agent)[0] != agent:
        raise BoardError(f"A chain's first model plans, on {agent}; the models after it can run on other agents.")
    efforts = args.get("efforts")
    if efforts is not None and not isinstance(efforts, dict):
        raise ApiError("usage", 'args.efforts must be an object: a level for each model, like {"claude-opus-5-5": '
                                '"high"}, or {"": "high"} for the agent\'s own model.')
    if efforts and effort_problem(agent, efforts):
        raise BoardError(effort_problem(agent, efforts))
    fallbacks = args.get("fallbacks")  # (the ones `fallbacks` suggested and the window showed you)
    if fallbacks is not None and not isinstance(fallbacks, list):
        raise ApiError("usage", 'args.fallbacks must be a list, like ["claude:claude-sonnet-5-5", "codex:gpt-6-sol"].')
    shown = _arg(args, "shown", str, required=False, default=None)  # the `content` of the task you showed
    # An empty list of models is refused (Board.approve), not taken as no choice: the run would go to the agent's
    # own model, which may cost more than the ones the window meant to name
    return _task_reply(board, board.approve(HUMAN, _task_arg(args), agent, return_to, kind, target, shown,
                                            models, efforts or None, fallbacks or None))


def op_fallbacks(request: dict) -> dict:
    """Who would take over a run, in turn, when a model is out of usage (rankings.py), for the window to show
    before you approve it: `fallbacks` ("agent:model", best first; pass them back to approve) and `words`.
    Nothing is approved and no model is asked; for an answer or a review, Ixel MAT is asked who it has."""
    from handoff.cli import suggested_fallbacks
    from handoff.rankings import shown
    args = request.get("args", {})
    agent = _name(args.get("agent"), "agent")
    kind = _arg(args, "kind", str, required=False, default="edit")
    if agent is None or kind not in RUN_KINDS:
        raise ApiError("usage", f"args.agent is the agent, and args.kind one of: {', '.join(RUN_KINDS)}.")
    models = args.get("models")
    if models is not None and not (isinstance(models, list) and approvals.check_models(models)):
        raise ApiError("usage", "args.models must be a list of model names, biggest first.")
    found = suggested_fallbacks(kind, agent, models, _project(request))
    return {"fallbacks": found, "words": shown(found)}


def op_rankings(request: dict) -> dict:
    """Which models are best at what (rankings.py): `ranks` per job, best first, what each job is, where each
    model fits in a chain (`tiers`: top, mid, low, open models with what it takes to run them), how current they
    are, and where they come from."""
    from handoff import rankings
    ranks, updated = rankings.load()
    return {"updated": updated, "jobs": dict(rankings.JOBS), "ranks": ranks, "tiers": rankings.TIERS,
            "tier_words": dict(rankings.TIER_WORDS), "hardware": rankings.HARDWARE,
            "sources": [{"name": n, "url": u} for n, u in rankings.SOURCES]}


def op_revoke(request: dict) -> dict:
    board = _board(_project(request))
    return _task_reply(board, board.revoke_approval(HUMAN, _task_arg(request.get("args", {}))))


def op_merge(request: dict) -> dict:
    """Merge the task's branch into the branch the project has out, as one merge commit (never pushed), then
    mark the task done and remove its worktree (the branch stays). Refused, changing nothing, when the branch
    isn't the commit the worker's run recorded, or the checkout isn't ready (gitwork.merge_branch). `left`: what
    of the worktree is still there, in words (as a rule, nothing)."""
    from handoff import gitwork
    root = _project(request)
    board = _board(root)
    args = request.get("args", {})
    task_id = _task_arg(args)
    shown = _arg(args, "shown", str, required=False, default=None)  # the `content` of the task you showed
    task, events, _, _ = board.task_with_history(task_id)
    if shown is not None and shown != content_of(task, events):
        raise Changed(f"{task.ref} changed while you were reading it. Look at it again, then merge what's there "
                      "now.")
    if task.branch is None:
        raise BoardError(f"{task.ref} has no branch: nothing to merge.")
    blocked = Board.merge_blocker(task, events)
    if blocked:
        raise BoardError(blocked)
    expected = Board.run_commit(events)
    if expected is None:
        raise BoardError(f"No worker run recorded a commit for {task.ref}, so there's nothing of its to merge.")
    if Board.merged_commit(events) == expected:
        raise BoardError(f"{task.branch} is already merged: the board has it merged at the run's commit.")
    try:
        into, sha, short = gitwork.merge_branch(root, task.branch, f"Merge {task.ref}: {task.title}", expected)
    except gitwork.MergeConflict as exc:
        raise BoardError(f"Merging {task.branch} conflicts in: {', '.join(exc.files[:50])}. "
                         "The merge was aborted, so nothing was changed.") from None
    except gitwork.GitError as exc:  # (AlreadyMerged too)
        raise BoardError(str(exc)) from None
    # Read the board again where it is: the merge changed the checkout it lives in
    board = _board(root)
    task, settled = board.record_merge(HUMAN, task_id, into, sha, short, expected)
    note = gitwork.remove_worktree(root, task_id) if settled else MOVED_WHILE_MERGING.format(ref=task.ref)
    return {**_task_reply(board, task), "merged": {"branch": task.branch, "into": into, "sha": short},
            "left": [note] if note else []}


MOVED_WHILE_MERGING = ("{ref} changed while git merged it (a run was approved or started, or a newer run finished), "
                       "so it stays as it is and its worktree is kept. The merge itself stands.")


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
    "merge": op_merge, "models": op_models, "agents.enable": op_agents_enable, "fallbacks": op_fallbacks,
    "rankings": op_rankings,
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
    except NoBoard as exc:
        code, message = "no_board", str(exc)
    except NotFound as exc:
        code, message = "not_found", str(exc)
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

