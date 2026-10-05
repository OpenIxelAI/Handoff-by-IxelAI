"""The task board: one SQLite file per project, shared by every Handoff process.

Each host app starts its own `handoff mcp` process, and the person runs the
`handoff` CLI, so several processes write to the same file. SQLite in WAL
mode handles that: every change is one short `BEGIN IMMEDIATE` transaction
(read, check, write), so two writers can't both act on the same stale state.

Rules are enforced here, not trusted from the caller:
- Only a task's assignee (or the person, as `human`) can note, pass, request
  a review or change its status; only the named reviewer can review.
- Agents can't delete tasks or edit history. `events` is append-only, and
  SQLite triggers refuse updates and deletes while the task exists.
- Sizes are capped, control characters and bidi overrides are stripped, and
  obvious secrets are refused instead of being stored.
- Deleting is for good. Every connection that writes overwrites deleted rows
  with zeros and keeps SQLite's scratch copies in memory, and after a delete
  the board is rebuilt and its -wal file emptied, so no file keeps the text
  (_clear_deleted). A task's results in .handoff/outputs go with it. Only the
  person deletes, or sets how long finished tasks stay (`handoff keep`), and
  then any open for writing removes the ones past it.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from handoff import approvals
from handoff.globs import PathError, normalize_path, overlaps
from handoff.project import BOARD_DIR, BOARD_FILE, OUTPUTS_DIR
from handoff.sanitize import clean_line, clean_text, find_secret
from handoff.timefmt import parse as parse_time

SCHEMA_VERSION = 1

STATUSES = ("open", "claimed", "in_progress", "handed_off", "in_review", "blocked", "done", "cancelled")
ACTIVE = frozenset(STATUSES[:6])
TERMINAL = frozenset({"done", "cancelled"})
EVENT_KINDS = ("created", "claimed", "note", "handoff", "review_requested", "review", "status", "assigned",
               "claim_paths", "release_paths", "approved", "worker")
# Events that change who has a task or where it stands. An approval for the worker
# holds only while it's the latest of these: any handoff, review, reassignment or
# status change after it means the person approved something that's no longer there.
STATE_EVENTS = ("claimed", "handoff", "review_requested", "review", "status", "assigned", "approved", "worker")
WORKER_STATES = ("started", "finished", "failed", "revoked")
# What an approved run does: edit (the worker runs claude or codex in its own worktree), or, through
# Ixel MAT, answer (one model replies), review (one model reads your changes) or image (pictures)
RUN_KINDS = ("edit", "answer", "review", "image")
VERDICTS = ("approve", "changes")
CHECK_RESULTS = ("met", "not_met", "not_checked")  # a reviewer's mark on one acceptance check

HUMAN = "human"  # the person, through the CLI
PERSON_WORDS = ("me", "you", "myself")  # what the person types for themselves, so never an agent's name
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

MAX_TITLE_CHARS = 200
MAX_TEXT_BYTES = 20 * 1024
MAX_OPEN_TASKS = 500
MAX_ACCEPTANCE = 50
MAX_ACCEPTANCE_CHARS = 500
MAX_CHECK_NOTE_CHARS = 500
MAX_FILES = 200
MAX_PATHS = 50
MAX_BRANCH_CHARS = 200
MAX_NAME_CHARS = 32
MAX_JSON_DEPTH = 32  # lists and objects inside each other; what Handoff stores is a few deep at most

# When may an assignee mark a task done?
DONE_RULES = {
    "review_or_reason": "after an approving review, or with a reason",
    "review": "only after an approving review",
    "any": "whenever the assignee says so",
}
DEFAULT_DONE_RULE = "review_or_reason"

# How long a board keeps finished tasks unless the person sets it (handoff keep): None keeps them until they're
# deleted or cleaned; a number of days removes each one that long after its last change
DEFAULT_KEEP_DAYS: int | None = None
MAX_KEEP_DAYS = 36_500  # a hundred years; past that, it's forever
CLEAR_WAIT_MS = 2_000   # how long a delete waits for a busy board before leaving the clearing for later

_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '[]',
    assignee TEXT,
    status TEXT NOT NULL CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    branch TEXT,
    waiting_on TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    parent_id INTEGER REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS tasks_assignee ON tasks(assignee, status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    actor TEXT NOT NULL,
    path_glob TEXT NOT NULL,
    at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS claims_active ON claims(released_at, task_id);
CREATE TRIGGER IF NOT EXISTS events_append_only_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS events_append_only_delete BEFORE DELETE ON events
WHEN EXISTS (SELECT 1 FROM tasks WHERE id = OLD.task_id)
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
"""


# ── Errors ────────────────────────────────────────────────────────────────────

class BoardError(Exception):
    """A request the board refuses. The message is written for the agent or person to act on."""


class NotFound(BoardError):
    pass


class Forbidden(BoardError):
    pass


class Changed(BoardError):
    """The task isn't what the person was shown any more (approve's `shown`)."""


# ── Records ───────────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: int
    title: str
    body: str
    acceptance: list[str]
    assignee: str | None
    status: str
    branch: str | None
    waiting_on: str | None
    created_by: str
    created_at: str
    updated_at: str
    parent_id: int | None

    @property
    def ref(self) -> str:
        return task_ref(self.id)


@dataclass
class Event:
    id: int
    task_id: int
    at: str
    actor: str
    kind: str
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class Claim:
    id: int
    task_id: int
    actor: str
    path_glob: str
    at: str
    released_at: str | None


@dataclass
class Overlap:
    """A path this task claimed that another active task has claimed too."""
    path: str
    other_task_id: int
    other_path: str
    other_assignee: str | None

    def describe(self) -> str:
        who = f", assigned to {self.other_assignee}" if self.other_assignee else ""
        if self.path == self.other_path:
            return f"{self.path} is also claimed by {task_ref(self.other_task_id)}{who}"
        return f"{self.path} overlaps {self.other_path}, claimed by {task_ref(self.other_task_id)}{who}"


@dataclass
class Inbox:
    mine: list[Task]            # assigned to me and still active, newest first
    blocked_on_me: list[Task]   # someone else's task, blocked waiting on me
    unassigned: list[Task]      # open tasks nobody has taken


# ── Helpers ───────────────────────────────────────────────────────────────────

def task_ref(task_id: int) -> str:
    return f"T-{task_id}"


def task_of_name(name: str) -> int | None:
    """The task a folder or branch named T-N is for, when the name is exactly what Handoff writes (T-12; not
    T-012, t-12 or T-١٢); else None."""
    digits = name[2:]
    if name.startswith("T-") and digits.isascii() and digits.isdigit() and len(digits) <= 12:
        task_id = int(digits)
        if task_id > 0 and task_ref(task_id) == name:
            return task_id
    return None


def parse_task_id(value: object) -> int:
    """Accept T-12, t-12, T12, #12 or 12."""
    text = clean_line(value)
    if text[:1] in ("T", "t", "#"):
        text = text[1:]
    if text[:1] == "-":
        text = text[1:]
    if not text.isdigit() or not text.isascii() or len(text) > 12:
        raise BoardError(f"{clean_line(value)[:40]!r} isn't a task id; they look like T-12.")
    return int(text)


def check_name(value: object, what: str = "name") -> str:
    """Agent names: lowercase letters, digits, - and _; like claude, codex or human."""
    name = clean_line(value).lower()
    if not name or len(name) > MAX_NAME_CHARS or not set(name) <= _NAME_CHARS or not name[0].isalpha():
        raise BoardError(f"{what} {clean_line(value)[:40]!r} isn't valid: use a short lowercase name "
                         "such as claude, codex or human.")
    if name in PERSON_WORDS:
        raise BoardError(f"{what} {name!r} isn't a name an agent can have: at the command line, {name} means the "
                         "person. Use human for the person, or the agent's own name, such as claude or codex.")
    return name


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refuse_secrets(*texts: str) -> None:
    for text in texts:
        what = find_secret(text)
        if what:
            raise BoardError(f"Refused: this looks like it contains {what}. Secrets don't belong on the "
                             "board, where every agent can read them. Say where the secret is kept "
                             "instead (for example, the name of an environment variable).")


def _text(value: object, what: str, *, required: bool = False) -> str:
    text = clean_text(value)
    if required and not text:
        raise BoardError(f"{what} is empty.")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise BoardError(f"{what} is longer than {MAX_TEXT_BYTES // 1024} KB; shorten it or point to a file.")
    _refuse_secrets(text)
    return text


def _title(value: object) -> str:
    title = clean_line(value)
    if not title:
        raise BoardError("The title is empty.")
    if len(title) > MAX_TITLE_CHARS:
        raise BoardError(f"The title is longer than {MAX_TITLE_CHARS} characters; put the detail in the body.")
    _refuse_secrets(title)
    return title


def _acceptance(items: Iterable[object] | None) -> list[str]:
    checks = [clean_line(item) for item in (items or [])]
    checks = [c for c in checks if c]
    if len(checks) > MAX_ACCEPTANCE:
        raise BoardError(f"More than {MAX_ACCEPTANCE} acceptance checks; group some together.")
    for check in checks:
        if len(check) > MAX_ACCEPTANCE_CHARS:
            raise BoardError(f"An acceptance check is longer than {MAX_ACCEPTANCE_CHARS} characters.")
    _refuse_secrets(*checks)
    return checks


def _check_results(items: Iterable[object] | None, task: Task) -> list[dict]:
    """A reviewer's marks, one per acceptance check, in order. No marks, nothing recorded: the review makes no
    claim about the checks. Once they mark any, the checks they didn't mark are recorded as not_checked."""
    marks = list(items or [])
    if not marks:
        return []
    if not task.acceptance:
        raise BoardError(f"{task.ref} has no acceptance checks to mark; leave checks out and use the notes.")
    total = len(task.acceptance)
    found: dict[int, dict] = {}
    for mark in marks:
        if not isinstance(mark, dict):
            raise BoardError("Each check needs a number, a result and optionally a note.")
        number, result = mark.get("check"), mark.get("result")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= total:
            raise BoardError(f"{clean_line(number)[:20]!r} isn't a check on {task.ref}; "
                             f"its checks are numbered 1 to {total}.")
        if number in found:
            raise BoardError(f"Check {number} is marked twice; mark each check once.")
        if result not in CHECK_RESULTS:
            raise BoardError(f"Check {number}: the result must be {', '.join(CHECK_RESULTS)}.")
        note = clean_line(mark.get("note") or "")
        if len(note) > MAX_CHECK_NOTE_CHARS:
            raise BoardError(f"The note on check {number} is longer than {MAX_CHECK_NOTE_CHARS} characters; "
                             "put the detail in the review notes.")
        _refuse_secrets(note)
        found[number] = {"result": result, "note": note}
    return [{"check": n, "text": text, **found.get(n, {"result": "not_checked", "note": ""})}
            for n, text in enumerate(task.acceptance, 1)]


def _paths(items: Iterable[object] | None) -> list[str]:
    raw = list(items or [])
    if len(raw) > MAX_PATHS:
        raise BoardError(f"More than {MAX_PATHS} paths in one claim; claim a folder instead.")
    paths: list[str] = []
    for item in raw:
        try:
            path = normalize_path(str(item))
        except PathError as exc:
            raise BoardError(f"Can't claim that path: {exc}.") from None
        if path not in paths:
            paths.append(path)
    _refuse_secrets(*paths)
    return paths


def _files(items: Iterable[object] | None) -> list[str]:
    files = [clean_line(item) for item in (items or [])]
    files = [f for f in files if f]
    if len(files) > MAX_FILES:
        raise BoardError(f"More than {MAX_FILES} files; list the folders instead.")
    for name in files:
        if len(name) > 256:
            raise BoardError("A file name is longer than 256 characters.")
    _refuse_secrets(*files)
    return files


def _branch(value: object) -> str | None:
    branch = clean_line(value)
    if not branch:
        return None
    if len(branch) > MAX_BRANCH_CHARS:
        raise BoardError(f"The branch name is longer than {MAX_BRANCH_CHARS} characters.")
    _refuse_secrets(branch)
    return branch


# Reading rows back. Handoff cleans text on the way in, but a board can also come from somewhere else (a
# repository can ship a crafted .handoff/board.db), so every field is checked again on the way out: names
# must be names, text is sanitized, JSON must have the right shape. Everything shown to an agent or
# printed in a terminal comes through here.

def run_kind(approval: "Event") -> str:
    """What an approval lets the worker do (approvals made before there were kinds are edits)."""
    kind = approval.data.get("kind", "edit")
    return kind if kind in RUN_KINDS else "unknown"


def safe_name(value: object) -> str | None:
    """A stored name if it's a valid one, 'unknown' if it isn't, None if there's none."""
    if value is None:
        return None
    name = str(value)
    valid = 0 < len(name) <= MAX_NAME_CHARS and set(name) <= _NAME_CHARS and name[0].isalpha()
    return name if valid else "unknown"


def _json(value: object, kind: type):
    try:
        data = json.loads(value) if isinstance(value, str) else None
    except (ValueError, RecursionError):  # (nested thousands deep, json may give up)
        data = None
    return data if isinstance(data, kind) and not _too_deep(data) else kind()


def _too_deep(data: object) -> bool:
    """Nested past MAX_JSON_DEPTH. Python 3.14 parses thousands deep when the stack has room, where older ones
    always gave up, and printing or sending a field like that would then hit the limit instead."""
    stack = [(data, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            if depth >= MAX_JSON_DEPTH:
                return True
            stack.extend((item, depth + 1) for item in (value.values() if isinstance(value, dict) else value))
    return False


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _task_from_row(row: sqlite3.Row) -> Task:
    if row["status"] not in STATUSES or not isinstance(row["id"], int):
        raise BoardError(f"The board has a task it can't read (status {clean_line(row['status'])[:20]!r}); "
                         "the file may be damaged or made by another tool.")
    try:
        checks = [clean_line(c) for c in _json(row["acceptance"], list) if isinstance(c, str)]
        return Task(id=row["id"], title=clean_line(row["title"]), body=clean_text(row["body"]),
                    acceptance=[c for c in checks if c], assignee=safe_name(row["assignee"]), status=row["status"],
                    branch=clean_line(row["branch"]) or None, waiting_on=safe_name(row["waiting_on"]),
                    created_by=safe_name(row["created_by"]) or "unknown", created_at=clean_line(row["created_at"]),
                    updated_at=clean_line(row["updated_at"]), parent_id=_int_or_none(row["parent_id"]))
    except Exception as exc:  # whatever a crafted row can do, it's one task Handoff can't read, not a crash
        raise BoardError(f"The board has a task it can't read ({task_ref(row['id'])}); the file may be damaged "
                         "or made by another tool.") from exc


def _readable(rows: Iterable[sqlite3.Row]) -> list[Task]:
    """The tasks among `rows` that can be read: one damaged row is left out (Board.unreadable counts them),
    so it can't hide the rest of the board."""
    tasks = []
    for row in rows:
        try:
            tasks.append(_task_from_row(row))
        except BoardError:
            continue
    return tasks


def _event_from_row(row: sqlite3.Row) -> Event:
    kind = row["kind"] if row["kind"] in EVENT_KINDS else "unknown"
    return Event(id=row["id"], task_id=row["task_id"], at=clean_line(row["at"]),
                 actor=safe_name(row["actor"]) or "unknown", kind=kind, text=clean_text(row["text"]),
                 data=_json(row["data"], dict))


def content_of(task: Task, history: Iterable[Event]) -> str:
    """The hash of what an agent is given for `task` with this history: what Board.approve seals, and what
    a preview passes back to it as `shown`."""
    return approvals.content_hash(task.title, task.body, task.acceptance,
                                  [{"id": e.id, "at": e.at, "actor": e.actor, "kind": e.kind, "text": e.text,
                                    "data": e.data} for e in history])


def _claim_from_row(row: sqlite3.Row) -> Claim:
    return Claim(id=row["id"], task_id=row["task_id"], actor=safe_name(row["actor"]) or "unknown",
                 path_glob=clean_line(row["path_glob"]), at=clean_line(row["at"]),
                 released_at=row["released_at"])


def _is_link(path: Path) -> bool:
    """A symbolic link, or a Windows junction (which is_symlink() doesn't see)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == 0xA0000003


def link_problem(root: Path) -> str | None:
    """Why Handoff won't use this project's board, if part of it is a link: a cloned repository could ship
    a .handoff folder, or a board.db (or its -wal, -shm or -journal file) in a real one, that leads elsewhere,
    and SQLite would make or change the file there."""
    folder = Path(root) / BOARD_DIR
    if _is_link(folder):
        return f"{folder} is a symbolic link; Handoff won't follow it. Remove it and try again."
    for name in (BOARD_FILE, f"{BOARD_FILE}-wal", f"{BOARD_FILE}-shm", f"{BOARD_FILE}-journal"):
        if _is_link(folder / name):
            return (f"{folder / name} is a symbolic link; Handoff won't follow it, so it won't use this board. "
                    "Remove it and try again.")
    return None


def _secure(path: Path, mode: int) -> None:
    """Owner-only permissions on macOS and Linux (Windows keeps the folder's own ACLs)."""
    if sys.platform == "win32":
        return
    try:
        if path.stat().st_mode & 0o777 != mode:
            os.chmod(path, mode)
    except OSError:
        pass  # not ours to change; `handoff doctor` reports it


def _keep_days(value: str | None) -> int | None:
    """A board's keep setting as stored: a number of days, or None for forever. Not set, or not one Handoff
    wrote: the default."""
    if value == "forever":
        return None
    if value and value.isascii() and value.isdigit() and 1 <= int(value) <= MAX_KEEP_DAYS:
        return int(value)
    return DEFAULT_KEEP_DAYS


def _check_days(days: object, least: int = 0) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or not least <= days <= MAX_KEEP_DAYS:
        raise BoardError(f"Say a number of days from {least} to {MAX_KEEP_DAYS}.")
    return days


# ── The board ─────────────────────────────────────────────────────────────────

class Board:
    """One project's board. Cheap to create; each operation opens its own connection."""

    BUSY_TIMEOUT_MS = 10_000

    def __init__(self, path: Path, clock: Callable[[], str] = utc_now, read_only: bool = False):
        self.path = Path(path)
        self.clock = clock
        self.read_only = read_only
        self.expired: list[Task] = []  # what opening the board removed because of its keep setting
        self.expired_left: list[str] = []  # what of those it couldn't remove (results in use), in words

    # ── Opening ──

    @classmethod
    def open(cls, root: Path, *, create: bool = True, clock: Callable[[], str] = utc_now,
             expire: bool = True) -> "Board":
        """The board for project `root`, creating `.handoff/board.db` (0700 folder, 0600 file) if asked. Opening
        it also removes finished tasks it keeps no longer (_upkeep; they're in `expired`), unless `expire` is
        False: for `handoff keep`, which is about to change how long that is."""
        folder = Path(root) / BOARD_DIR
        path = folder / BOARD_FILE
        problem = link_problem(root)
        if problem:
            raise BoardError(problem)
        if folder.exists() and not folder.is_dir():
            raise BoardError(f"{folder} is a file, not a folder; Handoff keeps its board there. Move it away.")
        if not path.exists():
            if not create:
                raise NotFound(f"There's no board in {root} yet. Run `handoff board` there to start one.")
            try:
                folder.mkdir(mode=0o700, exist_ok=True)
                ignore = folder / ".gitignore"
                if not os.path.lexists(ignore):  # keeps the board out of git even without a .gitignore line
                    with open(os.open(ignore, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW, 0o600), "w",
                              encoding="utf-8") as f:
                        f.write("# Handoff's board: never commit it\n*\n")
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW, 0o600))
            except FileExistsError:
                pass  # another process just made it
            except OSError as exc:
                raise BoardError(f"Can't create the board in {folder}: {exc}") from exc
        _secure(folder, 0o700)
        _secure(path, 0o600)
        board = cls(path, clock=clock)
        board._init_schema()
        board._upkeep(expire)
        return board

    @classmethod
    def open_read_only(cls, root: Path) -> "Board":
        """The project's board for reading only, as it is: nothing is made, set up, tightened or written, and
        SQLite refuses any write. NotFound if there's no board."""
        path = Path(root) / BOARD_DIR / BOARD_FILE
        problem = link_problem(root)
        if problem:
            raise BoardError(problem)
        if not path.is_file():
            raise NotFound(f"There's no board in {root} yet.")
        return cls(path, read_only=True)

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            # With no -wal file, everything is in board.db, and `immutable` reads it without making the -wal
            # and -shm files SQLite would leave behind; with one, a plain read-only connection sees it too
            wal = self.path.with_name(self.path.name + "-wal")
            mode = "mode=ro" if os.path.lexists(wal) else "immutable=1"
            conn = sqlite3.connect(f"{self.path.absolute().as_uri()}?{mode}", uri=True,
                                   timeout=self.BUSY_TIMEOUT_MS / 1000, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA temp_store = MEMORY")  # (below)
            return conn
        conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        # A deleted row, and the old copy of a changed one, is overwritten with zeros, not just marked free (some
        # SQLite builds do that anyway, but not all)
        conn.execute("PRAGMA secure_delete = ON")
        # What SQLite sets aside while it works stays in memory: the rows a delete takes out (in case it has to
        # undo it), a big sort, VACUUM's copy of the board. In a temporary file, the text would stay on the disk,
        # in the system's temp folder, after the board itself is clean.
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nCOMMIT;")
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                             (str(SCHEMA_VERSION),))
                # A new board has nothing deleted to clear (an older one is cleared once: _upkeep)
                conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('deletions_cleared', '0')")
            else:
                try:
                    version = int(row[0])
                except (TypeError, ValueError):
                    raise BoardError(f"The board at {self.path} looks damaged or made by another tool (its "
                                     f"version is {clean_line(row[0])[:20]!r}), so Handoff won't use it.") from None
                if version > SCHEMA_VERSION:
                    raise BoardError(f"This board was made by a newer version of Handoff (schema {version}); "
                                     "update Handoff to use it.")
        except sqlite3.DatabaseError as exc:
            raise BoardError(f"Can't open the board at {self.path}: {exc}") from exc
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")  # one consistent snapshot for multi-query reads
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    # ── Settings ──

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self._read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    @property
    def done_rule(self) -> str:
        rule = self.setting("done_rule", DEFAULT_DONE_RULE)
        return rule if rule in DONE_RULES else DEFAULT_DONE_RULE

    def set_done_rule(self, rule: str) -> None:
        if rule not in DONE_RULES:
            raise BoardError(f"The done rule must be one of: {', '.join(DONE_RULES)}.")
        self.set_setting("done_rule", rule)

    @property
    def keep_days(self) -> int | None:
        """How many days a finished task stays on this board after its last change; None: until it's deleted
        or cleaned by hand."""
        return _keep_days(self.setting("keep_days"))

    def set_keep_days(self, actor: str, days: int | None) -> None:
        """Keep finished tasks `days` days (None: forever). From the next time the board opens for writing, older
        ones are removed with their history and outputs, as `clean` would. The person only."""
        actor = check_name(actor, "actor")
        self._require_human(actor, "set how long finished tasks stay")
        self.set_setting("keep_days", "forever" if days is None else str(_check_days(days, least=1)))

    # ── Reading ──

    def get(self, task_id: int) -> Task:
        with self._read() as conn:
            return self._get(conn, task_id)

    def _get(self, conn: sqlite3.Connection, task_id: int) -> Task:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"There's no task {task_ref(task_id)} on this board.")
        return _task_from_row(row)

    def events(self, task_id: int) -> list[Event]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [_event_from_row(r) for r in rows]

    def task_with_history(self, task_id: int) -> tuple[Task, list[Event], list[Claim], list[Task]]:
        """A task, its events, its active claims and its subtasks, from one snapshot."""
        with self._read() as conn:
            task = self._get(conn, task_id)
            events = [_event_from_row(r) for r in conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,))]
            claims = [_claim_from_row(r) for r in conn.execute(
                "SELECT * FROM claims WHERE task_id = ? AND released_at IS NULL ORDER BY id", (task_id,))]
            children = _readable(conn.execute("SELECT * FROM tasks WHERE parent_id = ? ORDER BY id", (task_id,)))
        return task, events, claims, children

    def tasks(self, statuses: Iterable[str] | None = None, assignee: str | None = None,
              limit: int | None = None) -> list[Task]:
        """Tasks, most recently updated first."""
        query, args = "SELECT * FROM tasks", []
        where = []
        if statuses is not None:
            wanted = list(statuses)
            if not wanted:
                return []
            where.append(f"status IN ({', '.join('?' for _ in wanted)})")
            args += wanted
        if assignee is not None:
            where.append("assignee = ?")
            args.append(assignee)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        with self._read() as conn:
            return _readable(conn.execute(query, args))

    def last_events(self, task_ids: Iterable[int], skip: Iterable[str] = (),
                    kinds: Iterable[str] | None = None) -> dict[int, Event]:
        """Each task's latest event, ignoring the kinds in `skip` (or, with `kinds`, of those kinds only)."""
        ids, skipped = list(task_ids), list(skip)
        only = list(kinds) if kinds is not None else None
        if not ids or only == []:
            return {}
        marks = ", ".join("?" for _ in ids)
        where = f"task_id IN ({marks})"
        if skipped:
            where += f" AND kind NOT IN ({', '.join('?' for _ in skipped)})"
        if only:
            where += f" AND kind IN ({', '.join('?' for _ in only)})"
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM events WHERE id IN (SELECT MAX(id) FROM events "
                                f"WHERE {where} GROUP BY task_id)", ids + skipped + (only or [])).fetchall()
        return {r["task_id"]: _event_from_row(r) for r in rows}

    def revision(self) -> str:
        """Changes whenever anything on the board does, and never comes back to an earlier value (ids only
        grow, even past a deleted task), for a window that redraws only when there's something new. Opaque:
        compare it, don't read it."""
        with self._read() as conn:
            row = conn.execute(
                "SELECT (SELECT IFNULL(MAX(id), 0) FROM events), (SELECT COUNT(*) FROM tasks), "
                "(SELECT IFNULL(MAX(seq), 0) FROM sqlite_sequence WHERE name = 'tasks'), "
                "(SELECT IFNULL(MAX(updated_at), '') FROM tasks), (SELECT IFNULL(MAX(id), 0) FROM claims), "
                "(SELECT COUNT(*) FROM claims WHERE released_at IS NOT NULL), "
                "(SELECT IFNULL(MAX(value), '') FROM meta WHERE key = 'done_rule')").fetchone()
        return "/".join(str(value) for value in row)

    def last_event_of(self, task_id: int, kinds: Iterable[str]) -> Event | None:
        wanted = list(kinds)
        with self._read() as conn:
            row = conn.execute(f"SELECT * FROM events WHERE task_id = ? AND kind IN "
                               f"({', '.join('?' for _ in wanted)}) ORDER BY id DESC LIMIT 1",
                               [task_id, *wanted]).fetchone()
        return _event_from_row(row) if row else None

    def active_claims(self) -> list[Claim]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM claims WHERE released_at IS NULL ORDER BY id").fetchall()
        return [_claim_from_row(r) for r in rows]

    def inbox(self, me: str, unassigned_limit: int = 10) -> Inbox:
        active = sorted(ACTIVE)
        marks = ", ".join("?" for _ in active)
        order = " ORDER BY updated_at DESC, id DESC"
        with self._read() as conn:
            mine = conn.execute(f"SELECT * FROM tasks WHERE assignee = ? AND status IN ({marks})" + order,
                                [me, *active]).fetchall()
            blocked = conn.execute("SELECT * FROM tasks WHERE status = 'blocked' AND waiting_on = ? "
                                   "AND (assignee IS NULL OR assignee != ?)" + order, (me, me)).fetchall()
            free = conn.execute("SELECT * FROM tasks WHERE status = 'open' AND assignee IS NULL" + order
                                + " LIMIT ?", (unassigned_limit,)).fetchall()
        return Inbox(_readable(mine), _readable(blocked), _readable(free))

    def counts(self) -> dict[str, int]:
        """How many tasks have each status. A task it can't read isn't counted here; see unreadable()."""
        marks = ", ".join("?" for _ in STATUSES)
        with self._read() as conn:
            rows = conn.execute(f"SELECT status, COUNT(*) FROM tasks WHERE status IN ({marks}) AND "
                                f"typeof(id) = 'integer' GROUP BY status", list(STATUSES)).fetchall()
        return {status: n for status, n in rows}

    def unreadable(self) -> int:
        """How many tasks the board has that can't be read (a status Handoff doesn't know, say), and so are
        left out of every list. Only a damaged board, or one made by another tool, has any."""
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM tasks").fetchall()
        return len(rows) - len(_readable(rows))

    # ── Writing: building blocks ──

    def _event(self, conn: sqlite3.Connection, task_id: int, actor: str, kind: str, text: str = "",
               data: dict | None = None, at: str | None = None) -> None:
        assert kind in EVENT_KINDS, kind
        conn.execute("INSERT INTO events (task_id, at, actor, kind, text, data) VALUES (?, ?, ?, ?, ?, ?)",
                     (task_id, at or self.clock(), actor, kind, text, json.dumps(data or {}, ensure_ascii=False)))

    def _update(self, conn: sqlite3.Connection, task: Task, **changes) -> Task:
        changes["updated_at"] = self.clock()
        sets = ", ".join(f"{key} = ?" for key in changes)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*changes.values(), task.id])
        return self._get(conn, task.id)

    @staticmethod
    def _require_assignee(task: Task, actor: str, doing: str) -> None:
        if actor == HUMAN or task.assignee == actor:
            return
        if task.status == "in_review":  # its reviewer has it; handing it to its author would let them review it
            raise Forbidden(f"{task.ref} is in review with {task.assignee}; only its reviewer can {doing}. It goes "
                            "back to its author with the verdict.")
        owner = f"assigned to {task.assignee}" if task.assignee else "not assigned to anyone"
        raise Forbidden(f"{task.ref} is {owner}; only its assignee can {doing}. "
                        f"The person can reassign it with: handoff assign {task.ref} {actor}")

    @staticmethod
    def _require_active(task: Task, doing: str) -> None:
        if task.status in TERMINAL:
            raise BoardError(f"{task.ref} is {task.status}, so you can't {doing}. "
                             f"The person can reopen it with: handoff status {task.ref} open")

    @staticmethod
    def _require_human(actor: str, doing: str) -> None:
        if actor != HUMAN:
            raise Forbidden(f"Only the person can {doing}, from the handoff command line.")

    def _add_claims(self, conn: sqlite3.Connection, task: Task, actor: str, paths: list[str]) -> list[Overlap]:
        if not paths:
            return []
        rows = conn.execute("SELECT c.*, t.assignee FROM claims c JOIN tasks t ON t.id = c.task_id "
                            f"WHERE c.released_at IS NULL AND t.status IN ({', '.join('?' for _ in ACTIVE)})",
                            sorted(ACTIVE)).fetchall()
        mine = {r["path_glob"] for r in rows if r["task_id"] == task.id}
        new = [p for p in paths if p not in mine]
        found: list[Overlap] = []
        for path in new:
            for r in rows:
                if r["task_id"] != task.id and overlaps(path, r["path_glob"]):
                    found.append(Overlap(path, r["task_id"], r["path_glob"], r["assignee"]))
        if not new:
            return found
        at = self.clock()
        conn.executemany("INSERT INTO claims (task_id, actor, path_glob, at) VALUES (?, ?, ?, ?)",
                         [(task.id, actor, p, at) for p in new])
        self._event(conn, task.id, actor, "claim_paths", data={
            "paths": new,
            "overlaps": [{"path": o.path, "task_id": o.other_task_id, "other_path": o.other_path}
                         for o in found]})
        return found

    def overlaps_for(self, task_id: int) -> list[Overlap]:
        """Other active tasks' claims that overlap this task's own, whichever was claimed first: the agent
        that claimed first hears about a later overlap here (its inbox, the task, and its next note)."""
        active = sorted(ACTIVE)
        with self._read() as conn:
            rows = conn.execute("SELECT c.task_id, c.path_glob, t.assignee FROM claims c JOIN tasks t ON t.id = c.task_id "
                                f"WHERE c.released_at IS NULL AND t.status IN ({', '.join('?' for _ in active)})",
                                active).fetchall()
        mine = [clean_line(r["path_glob"]) for r in rows if r["task_id"] == task_id]
        return [Overlap(path, r["task_id"], clean_line(r["path_glob"]), safe_name(r["assignee"]))
                for path in mine for r in rows
                if r["task_id"] != task_id and overlaps(path, clean_line(r["path_glob"]))]

    def _release_claims(self, conn: sqlite3.Connection, task: Task, actor: str) -> None:
        rows = conn.execute("SELECT path_glob FROM claims WHERE task_id = ? AND released_at IS NULL",
                            (task.id,)).fetchall()
        if rows:
            conn.execute("UPDATE claims SET released_at = ? WHERE task_id = ? AND released_at IS NULL",
                         (self.clock(), task.id))
            self._event(conn, task.id, actor, "release_paths", data={"paths": [r[0] for r in rows]})

    @staticmethod
    def _approved(events: list[Event]) -> bool:
        """Is the latest review an approval, with no handoff since?"""
        approved = False
        for event in events:
            if event.kind == "review":
                approved = event.data.get("verdict") == "approve"
            elif event.kind == "handoff":
                approved = False
        return approved

    # ── Writing: the operations ──

    def create(self, actor: str, title: str, body: str = "", acceptance: Iterable[str] | None = None,
               assignee: str | None = None, parent_id: int | None = None,
               paths: Iterable[str] | None = None, branch: str | None = None) -> tuple[Task, list[Overlap]]:
        actor = check_name(actor, "actor")
        title = _title(title)
        body = _text(body, "The body")
        checks = _acceptance(acceptance)
        assignee = check_name(assignee, "assignee") if assignee else None
        claimed = _paths(paths)
        branch = _branch(branch)
        with self._write() as conn:
            active = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE status IN "
                                  f"({', '.join('?' for _ in ACTIVE)})", sorted(ACTIVE)).fetchone()[0]
            if active >= MAX_OPEN_TASKS:
                raise BoardError(f"The board already has {MAX_OPEN_TASKS} open tasks. "
                                 "Finish or cancel some before adding more.")
            if parent_id is not None:
                self._get(conn, parent_id)
            # an agent that assigns a task to itself has taken it
            status = "claimed" if assignee == actor and actor != HUMAN else "open"
            now = self.clock()
            cur = conn.execute(
                "INSERT INTO tasks (title, body, acceptance, assignee, status, branch, created_by, created_at, "
                "updated_at, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, body, json.dumps(checks, ensure_ascii=False), assignee, status, branch, actor, now, now,
                 parent_id))
            task = self._get(conn, cur.lastrowid)
            self._event(conn, task.id, actor, "created", data={"assignee": assignee, "parent_id": parent_id})
            if status == "claimed":
                self._event(conn, task.id, actor, "claimed")
            found = self._add_claims(conn, task, actor, claimed)
            return task, found

    def claim(self, actor: str, task_id: int, paths: Iterable[str] | None = None) -> tuple[Task, list[Overlap]]:
        actor = check_name(actor, "actor")
        claimed = _paths(paths)
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_active(task, "claim it")
            if task.assignee not in (None, actor):
                raise Forbidden(f"{task.ref} is assigned to {task.assignee}. "
                                f"The person can reassign it with: handoff assign {task.ref} {actor}")
            if task.status == "in_review":
                raise BoardError(f"{task.ref} is waiting for your review; use handoff_review.")
            if task.status in ("open", "handed_off"):
                task = self._update(conn, task, status="claimed", assignee=actor)
                self._event(conn, task.id, actor, "claimed")
            elif not claimed:
                return task, []  # already yours: nothing to change
            found = self._add_claims(conn, task, actor, claimed)
            return task, found

    def note(self, actor: str, task_id: int, text: str) -> Task:
        actor = check_name(actor, "actor")
        text = _text(text, "The note", required=True)
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_assignee(task, actor, "add notes")
            if actor != HUMAN:
                self._require_active(task, "add notes")
            before = task.status
            if task.assignee == actor and task.status in ("open", "claimed", "handed_off"):
                task = self._update(conn, task, status="in_progress")  # the assignee has started
            else:
                task = self._update(conn, task)
            # the history shows the change too (in the note: a status event would make an approval lapse)
            self._event(conn, task.id, actor, "note", text,
                        {"from": before, "to": task.status} if task.status != before else None)
            return task

    @staticmethod
    def _handoff_fields(actor: str, to: str, done: str, left: str, verify: str, files: Iterable[str] | None,
                        branch: str | None) -> dict:
        to = check_name(to, "`to`")
        if to == actor:
            raise BoardError("You can't hand a task to yourself; name who takes it next (claude, codex or human).")
        done = _text(done, "`done`", required=True)
        left = _text(left, "`left`")
        verify = _text(verify, "`verify`")
        if len("\n".join((done, left, verify)).encode("utf-8")) > MAX_TEXT_BYTES:
            raise BoardError(f"The handoff is longer than {MAX_TEXT_BYTES // 1024} KB in total; shorten it.")
        return {"to": to, "done": done, "left": left, "verify": verify, "files": _files(files),
                "branch": _branch(branch)}

    def _hand_off(self, conn: sqlite3.Connection, task: Task, actor: str, fields: dict) -> Task:
        task = self._update(conn, task, status="handed_off", assignee=fields["to"], waiting_on=None,
                            branch=fields["branch"] or task.branch)
        self._event(conn, task.id, actor, "handoff", data=fields)
        return task

    def pass_task(self, actor: str, task_id: int, to: str, done: str, left: str = "", verify: str = "",
                  files: Iterable[str] | None = None, branch: str | None = None) -> Task:
        actor = check_name(actor, "actor")
        fields = self._handoff_fields(actor, to, done, left, verify, files, branch)
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_assignee(task, actor, "hand it off")
            self._require_active(task, "hand it off")
            if task.status == "in_review" and actor != HUMAN:
                raise BoardError(f"{task.ref} is waiting for your review; use handoff_review to approve it "
                                 "or send it back.")
            return self._hand_off(conn, task, actor, fields)

    def request_review(self, actor: str, task_id: int, reviewer: str, what: str) -> Task:
        actor = check_name(actor, "actor")
        reviewer = check_name(reviewer, "reviewer")
        if reviewer == actor:
            raise BoardError("You can't review your own work; ask someone else (claude, codex or human).")
        what = _text(what, "`what`", required=True)
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_assignee(task, actor, "ask for a review")
            self._require_active(task, "ask for a review")
            if task.status == "in_review":
                raise BoardError(f"{task.ref} is already in review with {task.assignee}.")
            author = task.assignee if actor == HUMAN and task.assignee else actor
            if reviewer == author:
                raise BoardError(f"{author} can't review their own work; ask someone else.")
            if reviewer in self._workers(conn, task):
                raise BoardError(f"{reviewer} worked on {task.ref}, so it can't review it; ask someone else.")
            task = self._update(conn, task, status="in_review", assignee=reviewer, waiting_on=None)
            self._event(conn, task.id, actor, "review_requested", what,
                        data={"reviewer": reviewer, "author": author})
            return task

    def check_review(self, actor: str, task_id: int, verdict: str, notes: str = "",
                     checks: Iterable[object] | None = None) -> tuple[Task, list[Event], list[dict]]:
        """Would this review be accepted? Raises like review() would; for checking before slow work."""
        actor = check_name(actor, "actor")
        self._check_verdict(verdict, notes)
        with self._read() as conn:
            task = self._get(conn, task_id)
            self._review_allowed(conn, task, actor)
            results = self._review_marks(task, verdict, checks)
            events = [_event_from_row(r) for r in conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task.id,))]
        return task, events, results

    @staticmethod
    def _check_verdict(verdict: str, notes: str) -> str:
        if verdict not in VERDICTS:
            raise BoardError('The verdict must be "approve" or "changes".')
        return _text(notes, "The review's text", required=verdict == "changes")

    @staticmethod
    def _review_marks(task: Task, verdict: str, checks: Iterable[object] | None) -> list[dict]:
        results = _check_results(checks, task)
        missed = [str(r["check"]) for r in results if r["result"] == "not_met"]
        if verdict == "approve" and missed:
            which = f"check {missed[0]}" if len(missed) == 1 else f"checks {', '.join(missed)}"
            raise BoardError(f"You marked {which} not met, so this can't be an approval. Send it back with "
                             '"changes" and say what to fix.')
        return results

    @staticmethod
    def _review_author(conn: sqlite3.Connection, task: Task) -> str:
        """Whose work a review is of: who asked for it (or, for the person, whose task it was)."""
        asked = conn.execute("SELECT data FROM events WHERE task_id = ? AND kind = 'review_requested' "
                             "ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
        author = safe_name(_json(asked[0], dict).get("author")) if asked else None
        return author or task.created_by

    def _workers(self, conn: sqlite3.Connection, task: Task) -> set[str]:
        """The agents whose work a review would be of, so the one that built it can't review it, whoever asks:
        every agent that took the task on, handed it over, or started a worker run on it (an edit run's commits
        stay on its branch even if it fails), and, while a review is open, whoever asked for it. A worker run
        that only reviewed the task doesn't count."""
        names: set[str] = set()
        runs: dict[str, str] = {}  # the kind of run each agent last started
        previous: tuple[str, str, str] | None = None
        for actor, kind, data in conn.execute(
                "SELECT actor, kind, data FROM events WHERE task_id = ? AND kind IN ('claimed', 'handoff', 'worker') "
                "ORDER BY id", (task.id,)):
            info = _json(data, dict)
            if kind == "worker":
                state = str(info.get("state") or "")
                if state == "started":
                    runs[actor] = "edit" if "branch" in info else str(info.get("kind") or "edit")
                    if runs[actor] != "review":
                        names.add(actor)
                previous = (kind, actor, state)
                continue
            # A review run hands its verdict over too; that isn't work on the task
            if not (kind == "handoff" and previous == ("worker", actor, "finished") and runs.get(actor) == "review"):
                names.add(actor)
            previous = (kind, actor, "")
        if task.status == "in_review":
            names.add(self._review_author(conn, task))
        return {name for name in (safe_name(n) for n in names) if name and name != HUMAN}

    def _review_allowed(self, conn: sqlite3.Connection, task: Task, actor: str) -> None:
        if task.status != "in_review":
            raise BoardError(f"{task.ref} isn't waiting for a review (it's {task.status}).")
        # Nobody reviews their own work, even when the task was later reassigned to them. The person may: they
        # can close any task without a review anyway
        if actor != HUMAN and actor in self._workers(conn, task):
            raise Forbidden(f"{task.ref} is your own work, so you can't review it. Its reviewer does, and it comes "
                            "back to you with their verdict.")
        self._require_assignee(task, actor, "review it")

    def review(self, actor: str, task_id: int, verdict: str, notes: str = "",
               panel: dict | None = None, checks: Iterable[object] | None = None) -> Task:
        actor = check_name(actor, "actor")
        notes = self._check_verdict(verdict, notes)
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._review_allowed(conn, task, actor)
            results = self._review_marks(task, verdict, checks)
            author = self._review_author(conn, task)
            task = self._update(conn, task, status="handed_off", assignee=author)
            data = {"verdict": verdict, "author": author}
            if results:
                data["checks"] = results
            if panel is not None:
                data["panel"] = panel
            self._event(conn, task.id, actor, "review", notes, data=data)
            return task

    def set_status(self, actor: str, task_id: int, status: str, reason: str = "",
                   waiting_on: str | None = None) -> Task:
        actor = check_name(actor, "actor")
        if status not in ("open", "blocked", "done"):
            raise BoardError('The status must be "open", "blocked" or "done".')
        reason = _text(reason, "The reason", required=status == "blocked")
        waiting_on = check_name(waiting_on, "waiting_on") if waiting_on else None
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_assignee(task, actor, "change its status")
            if task.status in TERMINAL and not (actor == HUMAN and status == "open"):
                self._require_active(task, f"set it to {status}")
            if task.status == "in_review" and actor != HUMAN:
                raise BoardError(f"{task.ref} is waiting for review: approve it or send it back with "
                                 "handoff_review, and its author takes it from there.")
            if status == "done":
                if actor != HUMAN:
                    self._check_done_rule(conn, task, reason)
                self._release_claims(conn, task, actor)
            before = task.status
            task = self._update(conn, task, status=status,
                                waiting_on=waiting_on if status == "blocked" else None)
            self._event(conn, task.id, actor, "status", reason,
                        data={"from": before, "to": status, "waiting_on": task.waiting_on})
            return task

    def _check_done_rule(self, conn: sqlite3.Connection, task: Task, reason: str) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key = 'done_rule'").fetchone()
        rule = row[0] if row and row[0] in DONE_RULES else DEFAULT_DONE_RULE
        if rule == "any":
            return
        events = [_event_from_row(r) for r in conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task.id,))]
        if self._approved(events):
            return
        if rule == "review_or_reason" and reason:
            return
        how = "ask for a review first (handoff_request_review)"
        if rule == "review_or_reason":
            how += ", or give the reason it's done without one"
        raise BoardError(f"{task.ref} hasn't had an approving review. This board marks tasks done "
                         f"{DONE_RULES[rule]}: {how}.")

    def assign(self, actor: str, task_id: int, assignee: str | None) -> Task:
        actor = check_name(actor, "actor")
        self._require_human(actor, "reassign tasks")
        assignee = check_name(assignee, "assignee") if assignee else None
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_active(task, "reassign it")
            if assignee == task.assignee:
                return task
            if task.status == "in_review" and assignee not in (None, HUMAN) \
                    and assignee in self._workers(conn, task):
                raise BoardError(f"{task.ref} is waiting for a review of {assignee}'s own work, so {assignee} can't "
                                 f"be its reviewer. Give it to another reviewer, or review it yourself: "
                                 f"handoff review {task.ref}")
            changes: dict = {"assignee": assignee}
            if task.status in ("claimed", "in_progress"):
                changes["status"] = "open"  # the new assignee claims it when they start
            before = task.assignee
            task = self._update(conn, task, **changes)
            self._event(conn, task.id, actor, "assigned", data={"from": before, "to": assignee})
            return task

    def cancel(self, actor: str, task_id: int, reason: str = "") -> Task:
        actor = check_name(actor, "actor")
        self._require_human(actor, "cancel tasks")
        reason = _text(reason, "The reason")
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_active(task, "cancel it")
            self._release_claims(conn, task, actor)
            before = task.status
            task = self._update(conn, task, status="cancelled", waiting_on=None)
            self._event(conn, task.id, actor, "status", reason, data={"from": before, "to": "cancelled"})
            return task

    # ── The worker: the person approves a task for a headless agent ──

    def _last_state_event(self, conn: sqlite3.Connection, task: Task) -> Event | None:
        row = conn.execute(f"SELECT * FROM events WHERE task_id = ? AND kind IN "
                           f"({', '.join('?' for _ in STATE_EVENTS)}) ORDER BY id DESC LIMIT 1",
                           [task.id, *STATE_EVENTS]).fetchone()
        return _event_from_row(row) if row else None

    def _latest_approval(self, conn: sqlite3.Connection, task: Task, worker: str | None = None) -> Event | None:
        """The task's approval for the worker, if the task hasn't moved since it was given."""
        if task.status in TERMINAL or task.status == "in_review":
            return None
        event = self._last_state_event(conn, task)
        if event is None or event.kind != "approved":
            return None
        approved_for = safe_name(event.data.get("worker"))
        if task.assignee != approved_for or (worker is not None and approved_for != worker):
            return None
        return event

    def _sealed(self, event: Event) -> bool:
        """Was this approval made on this computer, for this board, by a Handoff that seals what it approves?
        (See approvals.py.)"""
        if not isinstance(event.data.get("content"), str):
            return False
        try:
            key = approvals.load_key()
        except OSError:
            return False
        return approvals.is_sealed(key, self.path, event.task_id, event.data, event.at)

    def _content(self, conn: sqlite3.Connection, task: Task, before: int | None = None) -> str:
        """The hash of what the agent is given: the task's text and its history (before event `before`)."""
        return content_of(task, self._history(conn, task.id, before))

    def _history(self, conn: sqlite3.Connection, task_id: int, before: int | None = None) -> list[Event]:
        query, args = "SELECT * FROM events WHERE task_id = ?", [task_id]
        if before is not None:
            query += " AND id < ?"
            args.append(before)
        return [_event_from_row(r) for r in conn.execute(query + " ORDER BY id", args)]

    def _pending_approval(self, conn: sqlite3.Connection, task: Task, worker: str | None = None) -> Event | None:
        """The approval the worker may act on: the person gave it here, the task hasn't moved since, what the
        agent is given is as it was approved, and it hasn't been used for a run yet."""
        event = self._latest_approval(conn, task, worker)
        if event is None or not self._sealed(event) or event.data["content"] != self._content(conn, task, event.id):
            return None
        try:
            used = approvals.is_used(str(event.data.get("nonce")))
        except OSError:
            return None  # (it can't tell, so it doesn't run)
        return None if used else event

    def approve(self, actor: str, task_id: int, worker: str, return_to: str = HUMAN, kind: str = "edit",
                target: Mapping | None = None, shown: str | None = None) -> Task:
        """The person lets the worker run `worker` on this task, once, for one kind of run (RUN_KINDS).
        Results go to `return_to`. A review or a change can be of two given commits (`target`:
        approvals.check_target), such as a pull request fetched into the project: a review reads them
        instead of your own changes, and a change starts from the head instead of your current commit.
        Approved again without one, the task keeps the commits it was last approved for (a pull request's
        review run again reads that pull request, not your own changes). `shown` is content_of() the task
        and history the person was shown: if anything changed since (a note added while they read it), it's
        refused with Changed, so what's sealed is what they saw."""
        actor = check_name(actor, "actor")
        self._require_human(actor, "approve tasks for the worker")
        worker = check_name(worker, "worker")
        return_to = check_name(return_to, "return_to")
        if kind not in RUN_KINDS:
            raise BoardError(f"A run is one of: {', '.join(RUN_KINDS)}.")
        if worker == HUMAN:
            raise BoardError("The worker runs an agent; name the agent (claude, codex, gemini…).")
        if return_to == worker:
            raise BoardError(f"The results need to go to someone other than {worker}; the default is human.")
        if target is not None:
            if kind not in approvals.TARGET_KINDS:
                raise BoardError("Only a review or a change can be of given commits.")
            target = approvals.check_target(target)
            if target is None:
                raise BoardError("The commits are two full commit ids and a few words saying what they are.")
        try:
            key = approvals.load_key(create=True)
        except OSError as exc:
            raise BoardError(f"Can't make this computer's approval key at {approvals.key_path()}: {exc}") from exc
        if key is None:
            raise BoardError(f"This computer's approval key at {approvals.key_path()} isn't one Handoff made. "
                             "Delete it and approve again.")
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_active(task, "approve it for the worker")
            if task.status == "in_review":
                raise BoardError(f"{task.ref} is in review; finish the review before approving it for the worker.")
            if shown is not None and self._content(conn, task) != shown:
                raise Changed(f"{task.ref} changed while you were reading it. Look at it again, then approve "
                              "what's there now.")
            if target is None and kind in approvals.TARGET_KINDS:
                target = self._earlier_target(conn, task, kind, key)
            at = self.clock()
            if task.assignee != worker:
                changes: dict = {"assignee": worker}
                if task.status in ("claimed", "in_progress"):
                    changes["status"] = "open"
                before = task.assignee
                task = self._update(conn, task, **changes)
                self._event(conn, task.id, actor, "assigned", data={"from": before, "to": worker})
            else:
                task = self._update(conn, task)
            content = self._content(conn, task)  # the task and its history, as the agent will get them
            sealed = approvals.seal(key, self.path, task.id, worker, return_to, at, kind, target, content)
            data = {"worker": worker, "kind": kind, "return_to": return_to, "content": content, **sealed}
            if target is not None:
                data["target"] = target
            self._event(conn, task.id, actor, "approved", data=data, at=at)
            return task

    def _earlier_target(self, conn: sqlite3.Connection, task: Task, kind: str, key: bytes | None) -> dict | None:
        """The commits the task's last approval for this kind of run was for. The newest approval of that kind
        that was made here decides; one that can't be checked any more but names commits is refused rather
        than dropped, so a pull request's review never quietly becomes a review of your own changes."""
        rows = conn.execute("SELECT * FROM events WHERE task_id = ? AND kind = 'approved' ORDER BY id DESC",
                            (task.id,)).fetchall()
        for row in rows:
            event = _event_from_row(row)
            if event.data.get("kind", "edit") != kind:
                continue
            if approvals.is_sealed(key, self.path, task.id, event.data, event.at):
                return approvals.check_target(event.data["target"]) if "target" in event.data else None
            if "target" in event.data:
                target = approvals.check_target(event.data["target"])
                what = f" ({clean_line(target['label'])})" if target else ""
                raise BoardError(f"{task.ref}'s last {kind} was approved for given commits{what}, but that approval "
                                 "can't be checked here any more (the project moved, or this computer's approval "
                                 "key changed). Start it again from where it came from, such as the Board's pull "
                                 "requests.")
        return None

    def approval_targets(self, task_id: int) -> dict[str, str]:
        """What approving the task again for each kind of run would be of (the commits' label), where that's
        given commits and the approval still checks out."""
        try:
            key = approvals.load_key()
        except OSError:
            key = None
        out: dict[str, str] = {}
        with self._read() as conn:
            task = self._get(conn, task_id)
            for kind in approvals.TARGET_KINDS:
                try:
                    target = self._earlier_target(conn, task, kind, key)
                except BoardError:
                    continue
                if target is not None:
                    out[kind] = clean_line(target["label"])[:300]
        return out

    def revoke_approval(self, actor: str, task_id: int) -> Task:
        actor = check_name(actor, "actor")
        self._require_human(actor, "revoke a worker approval")
        with self._write() as conn:
            task = self._get(conn, task_id)
            approval = self._pending_approval(conn, task)
            if approval is None:
                raise BoardError(f"{task.ref} isn't waiting for the worker.")
            task = self._update(conn, task)
            self._event(conn, task.id, actor, "worker", data={"state": "revoked", "approval": approval.id})
            return task

    def pending_runs(self, worker: str | None = None, task_ids: Iterable[int] | None = None) -> list[tuple[Task, Event]]:
        """Tasks approved for `worker` (None: for anyone) and still as the person approved them, oldest approval
        first. `task_ids` narrows it to those tasks."""
        worker = check_name(worker, "worker") if worker is not None else None
        runnable = sorted(ACTIVE - {"in_review"})
        query = f"SELECT * FROM tasks WHERE status IN ({', '.join('?' for _ in runnable)})"
        params: list = list(runnable)
        if worker is not None:
            query += " AND assignee = ?"
            params.append(worker)
        wanted = None if task_ids is None else set(task_ids)
        with self._read() as conn:
            tasks = [t for t in _readable(conn.execute(query, params)) if wanted is None or t.id in wanted]
            found = [(t, a) for t in tasks
                     if t.assignee and (a := self._pending_approval(conn, t, worker)) is not None]
        return sorted(found, key=lambda pair: pair[1].id)

    def unsealed_approvals(self, worker: str) -> list[Task]:
        """Tasks approved for `worker` somewhere else (a board that came with a cloned repo), or before
        approvals were sealed. The worker never runs them; `handoff approve` here makes them runnable."""
        worker = check_name(worker, "worker")
        runnable = sorted(ACTIVE - {"in_review"})
        with self._read() as conn:
            tasks = _readable(conn.execute(
                f"SELECT * FROM tasks WHERE assignee = ? AND status IN ({', '.join('?' for _ in runnable)})",
                [worker, *runnable]))
            return [t for t in tasks if (a := self._latest_approval(conn, t, worker)) is not None and not self._sealed(a)]

    def start_run(self, worker: str, task_id: int, approval_id: int, run: dict) -> tuple[Task, list[Event]]:
        """Take an approved task for one run. Two workers can't both start the same approval. The task and
        its history as it was approved: what the agent is given, read where the approval was checked."""
        worker = check_name(worker, "worker")
        with self._write() as conn:
            task = self._get(conn, task_id)
            approval = self._pending_approval(conn, task, worker)
            if approval is None or approval.id != approval_id:
                raise BoardError(f"{task.ref} is no longer approved for the {worker} worker.")
            history = self._history(conn, task.id, approval.id)
            try:  # used up, for good: outside the board, so a copy of it put back never runs
                approvals.mark_used(str(approval.data.get("nonce")))
            except (OSError, ValueError) as exc:
                raise BoardError(f"Couldn't record that {task.ref}'s approval was used ({exc}), so it didn't "
                                 "run.") from exc
            task = self._update(conn, task, status="in_progress", waiting_on=None)
            self._event(conn, task.id, worker, "worker", data={"state": "started", "approval": approval_id, **run})
            return task, history

    def _require_running(self, conn: sqlite3.Connection, task: Task, worker: str) -> None:
        event = self._last_state_event(conn, task)
        if not (event and event.kind == "worker" and event.actor == worker and event.data.get("state") == "started"
                and task.assignee == worker):
            raise BoardError(f"{task.ref} changed while the {worker} worker was running, so its result wasn't "
                             "recorded on the board.")

    def finish_run(self, worker: str, task_id: int, return_to: str, done: str, left: str = "", verify: str = "",
                   files: Iterable[str] | None = None, branch: str | None = None,
                   extra: dict | None = None) -> Task:
        """Record a finished run and hand the task on. `extra` keeps the run's base and commit (ids only)."""
        worker = check_name(worker, "worker")
        fields = self._handoff_fields(worker, return_to, done, left, verify, files, branch)
        ids = {k: v for k, v in (extra or {}).items()
               if k in ("base", "commit") and isinstance(v, str) and v.isalnum() and len(v) <= 64}
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_running(conn, task, worker)
            self._event(conn, task.id, worker, "worker", data={"state": "finished", **ids})
            return self._hand_off(conn, task, worker, fields)

    def fail_run(self, worker: str, task_id: int, return_to: str, reason: str) -> Task:
        worker = check_name(worker, "worker")
        return_to = check_name(return_to, "return_to")
        reason = _text(reason, "The reason", required=True)
        with self._write() as conn:
            task = self._get(conn, task_id)
            self._require_running(conn, task, worker)
            self._event(conn, task.id, worker, "worker", data={"state": "failed"})
            before = task.status
            task = self._update(conn, task, status="blocked", waiting_on=return_to)
            self._event(conn, task.id, worker, "status", reason,
                        data={"from": before, "to": "blocked", "waiting_on": return_to})
            return task

    # ── Removing tasks for good ──

    def delete(self, actor: str, task_id: int) -> list[str]:
        """Remove a task, its history and its results in .handoff/outputs for good. The person only. Returns what
        of its outputs couldn't be removed, in words (as a rule, nothing)."""
        actor = check_name(actor, "actor")
        self._require_human(actor, "delete tasks")
        with self._write() as conn:
            task = self._get(conn, task_id)
            children = conn.execute("SELECT id FROM tasks WHERE parent_id = ? ORDER BY id", (task.id,)).fetchall()
            if children:
                refs = ", ".join(task_ref(r[0]) for r in children)
                raise BoardError(f"{task.ref} has subtasks ({refs}); delete those first.")
            self._remove(conn, [task.id])
        left = self._remove_outputs([task.ref])
        self._clear_deleted()
        return left

    def cleanable(self, days: int) -> tuple[list[Task], list[Task]]:
        """What clean(days) would remove now, oldest first: finished tasks whose last change is more than `days`
        days ago. And the ones it would keep for now, because a subtask of theirs isn't finished, or is newer."""
        with self._read() as conn:
            return self._finished_before(conn, self._cutoff(_check_days(days)))

    def clean(self, actor: str, days: int, only: Iterable[int] | None = None) -> tuple[list[Task], list[str]]:
        """Remove finished tasks (done or cancelled) whose last change is more than `days` days ago, with their
        history, claims and results in .handoff/outputs, for good. A task goes with its subtasks, once they can
        all go. The person only. `only`: of these tasks (the ones the person was shown), so one that changed
        since stays. Returns what was removed, and what of its outputs couldn't be, in words."""
        actor = check_name(actor, "actor")
        self._require_human(actor, "clean the board")
        cutoff = self._cutoff(_check_days(days))
        return self._remove_finished(cutoff, None if only is None else set(only))

    def leftover_outputs(self) -> list[str]:
        """The folders in .handoff/outputs whose task isn't on the board, oldest task first (T-N): the results of
        tasks deleted with an older Handoff, which left them, or of a run that finished after its task was
        deleted. Only folders named the way Handoff names them, and never a link."""
        outputs = self.path.parent / OUTPUTS_DIR
        found: dict[int, str] = {}
        try:
            if _is_link(outputs) or not outputs.is_dir():
                return []
            with os.scandir(outputs) as entries:
                for entry in entries:
                    task_id = task_of_name(entry.name)
                    if task_id is not None and entry.is_dir(follow_symlinks=False) and not _is_link(Path(entry.path)):
                        found[task_id] = entry.name
        except OSError:
            return []
        if not found:
            return []
        with self._read() as conn:
            on_board = {row[0] for row in conn.execute("SELECT id FROM tasks")}
        return [found[i] for i in sorted(found) if i not in on_board]

    def clean_outputs(self, actor: str, refs: Iterable[str]) -> tuple[list[str], list[str]]:
        """Remove folders in .handoff/outputs whose task isn't on the board (leftover_outputs), of `refs`: the ones
        the person was shown. The person only. Returns the ones removed, and what couldn't be, in words."""
        actor = check_name(actor, "actor")
        self._require_human(actor, "clean the board")
        leftover = set(self.leftover_outputs())
        chosen = [ref for ref in refs if ref in leftover]
        left = self._remove_outputs(chosen)
        outputs = self.path.parent / OUTPUTS_DIR
        return [ref for ref in chosen if not os.path.lexists(outputs / ref)], left

    def _cutoff(self, days: int) -> str:
        """The time `days` days ago, as the board writes times: a task last changed by then is that old."""
        now = parse_time(self.clock()) or datetime.now(timezone.utc)
        return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _finished_before(conn: sqlite3.Connection, cutoff: str,
                         only: set[int] | None = None) -> tuple[list[Task], list[Task]]:
        """Finished tasks last changed by `cutoff`: the ones that can go now, and the ones that wait for a
        subtask that isn't finished, or changed since. A task only ever goes with all of its subtasks, so no
        task is left pointing at one that's gone."""
        finished = sorted(TERMINAL)
        old = [t for t in _readable(conn.execute(
                   f"SELECT * FROM tasks WHERE status IN ({', '.join('?' for _ in finished)}) AND updated_at <= ? "
                   "ORDER BY id", [*finished, cutoff]))
               if parse_time(t.updated_at) is not None and (only is None or t.id in only)]
        children: dict[int, list[int]] = {}
        for child, parent in conn.execute("SELECT id, parent_id FROM tasks WHERE parent_id IS NOT NULL"):
            children.setdefault(parent, []).append(child)
        going = {t.id for t in old}
        while True:
            waiting = {i for i in going if any(c not in going for c in children.get(i, ()))}
            if not waiting:
                break
            going -= waiting
        return [t for t in old if t.id in going], [t for t in old if t.id not in going]

    def _remove_finished(self, cutoff: str, only: set[int] | None = None,
                         wait_ms: int | None = None) -> tuple[list[Task], list[str]]:
        """Remove the finished tasks that can go by `cutoff` (of `only`, if given) and their outputs, then clear
        their text from the files."""
        try:
            with self._write() as conn:
                going, _ = self._finished_before(conn, cutoff, only)
                if going:
                    self._remove(conn, [t.id for t in going])
        except sqlite3.IntegrityError as exc:  # only a board Handoff didn't write: its subtasks don't add up
            raise BoardError("Nothing was removed: the board has a subtask whose task isn't there. The file may be "
                             "damaged or made by another tool.") from exc
        if not going:
            return [], []
        left = self._remove_outputs(t.ref for t in going)
        self._clear_deleted(wait_ms)
        return going, left

    @staticmethod
    def _remove(conn: sqlite3.Connection, task_ids: Iterable[int]) -> None:
        """Delete tasks with their claims and history. Subtasks go in the same transaction, so the check that no
        task points at a removed one waits for the commit."""
        conn.execute("PRAGMA defer_foreign_keys = ON")  # (back off at the commit)
        for task_id in task_ids:
            conn.execute("DELETE FROM claims WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.execute("DELETE FROM events WHERE task_id = ?", (task_id,))  # allowed once the task is gone
        # Counted, so that clearing the text from the files, if it has to wait, isn't forgotten (_upkeep)
        conn.execute("INSERT INTO meta (key, value) VALUES ('deletions', '1') "
                     "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")

    def _remove_outputs(self, refs: Iterable[str]) -> list[str]:
        """Remove each task's folder in .handoff/outputs (an answer, a review, pictures). Never through a link: a
        repository can ship one, and what it leads to isn't Handoff's to remove. Returns what's left, in words."""
        outputs = self.path.parent / OUTPUTS_DIR
        shown = f"{BOARD_DIR}/{OUTPUTS_DIR}"
        if not os.path.lexists(outputs):
            return []
        if _is_link(outputs) or not outputs.is_dir():
            return [f"{shown} isn't a folder Handoff made (it's a link or a file), so nothing in it was removed."]
        left = []
        for ref in refs:
            folder = outputs / ref
            try:
                if not os.path.lexists(folder):
                    continue
                if _is_link(folder) or not folder.is_dir():
                    left.append(f"{shown}/{ref} isn't a folder Handoff made (it's a link or a file), so it's still "
                                "there.")
                    continue
                shutil.rmtree(folder, ignore_errors=True)  # (it removes a link inside, never what it leads to)
                gone = not os.path.lexists(folder)
            except OSError:
                gone = False
            if not gone:
                left.append(f"Some of {shown}/{ref} couldn't be removed; a file in it may be open in another "
                            "program. Delete the folder yourself.")
        return left

    def _clear_deleted(self, wait_ms: int | None = None) -> bool:
        """Clear deleted tasks' text out of the board's files: board.db is rebuilt from what's still on it
        (VACUUM), and the -wal file, which keeps earlier copies of changed pages, is copied in and emptied.
        Best effort: while another program is reading the board, the -wal file can't be emptied, so it waits for
        the next time the board opens for writing, and nothing fails over it. True when it's done. `wait_ms`: how
        long to wait for a busy board (CLEAR_WAIT_MS unless said)."""
        conn = self._connect()  # (VACUUM's copy of the board stays in memory: _connect)
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(CLEAR_WAIT_MS if wait_ms is None else wait_ms)}")
            row = conn.execute("SELECT value FROM meta WHERE key = 'deletions'").fetchone()
            deletions = row[0] if row else "0"
            # Emptying the -wal file first is the cheap way to find out whether anyone is reading
            if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                return False
            conn.execute("VACUUM")
            if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                return False
            # (a delete since `deletions` was read counted one more, so that one is still to clear)
            conn.execute("INSERT INTO meta (key, value) VALUES ('deletions_cleared', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (deletions,))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return True
        except sqlite3.DatabaseError:  # busy, or another writer has it
            return False
        finally:
            conn.close()

    def _upkeep(self, expire: bool = True) -> None:
        """What opening a board for writing also does, quietly and without waiting on a busy board: with `expire`,
        remove the finished tasks its keep setting says have had their time, and clear deleted text that an older
        Handoff, or a board that was busy at the time, left in its files. Neither stops the board opening."""
        try:
            with self._read() as conn:
                meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN "
                                         "('keep_days', 'deletions', 'deletions_cleared')").fetchall())
                days = _keep_days(meta.get("keep_days"))
                finished = sorted(TERMINAL)
                due = expire and days is not None and conn.execute(
                    f"SELECT EXISTS (SELECT 1 FROM tasks WHERE status IN ({', '.join('?' for _ in finished)}) "
                    "AND updated_at <= ?)", [*finished, self._cutoff(days)]).fetchone()[0]
            if due:
                self.expired, self.expired_left = self._remove_finished(self._cutoff(days), wait_ms=0)
                if self.expired:
                    return  # (and that cleared the files, as far as it could)
            if meta.get("deletions_cleared") != meta.get("deletions", "0"):
                self._clear_deleted(wait_ms=0)
        except (BoardError, sqlite3.DatabaseError, OSError):
            pass  # the board still opens; handoff clean says what's wrong
