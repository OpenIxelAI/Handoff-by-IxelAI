"""`handoff`: the command line for the person.

The agents talk to the board through `handoff mcp`; everything else here is
for you: see the board, add and assign tasks, and connect your apps. At the
command line you are `human` on the board (shown as "you"), and the board lets
you do what agents can't: reassign, cancel, delete, clean, reopen, and close
without a review.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from pathlib import Path
from typing import Callable

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from handoff import __version__

# Ixel palette, so both tools look alike
C = {
    "moon": "#c8d8e8",
    "blue": "#7eb8d4",
    "violet": "#9b7fc7",
    "gold": "#d4af37",
    "dim": "#6b7d94",
    "green": "#4ade80",
    "red": "#e05252",
}
STATUS_STYLE = {
    "in_review": C["violet"], "handed_off": C["gold"], "blocked": C["red"], "in_progress": C["blue"],
    "claimed": C["blue"], "open": C["moon"], "done": C["green"], "cancelled": C["dim"],
}
STATUS_ORDER = list(STATUS_STYLE)

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

# (usage, what it does). `handoff help` shows them under GROUPS
COMMANDS = [
    ("handoff board [--all] [--status S] [--assignee NAME] [--watch]", "Who's doing what, and what's blocked"),
    ("handoff show T-12", "One task with its full history (or just: handoff T-12)"),
    ('handoff add "title" [--to NAME] [--body TEXT] [--accept CHECK]...', "Add a task (and assign it)"),
    ("handoff assign T-12 NAME", "Give a task to claude, codex, me… (or nobody)"),
    ('handoff note T-12 "text"', "Add a note to a task"),
    ("handoff done T-12 [--reason R]", "Mark a task done (no review needed)"),
    ("handoff reopen T-12", "Open a done, cancelled or blocked task again"),
    ('handoff block T-12 "why" [--waiting-on NAME]', "Mark a task blocked, and say why"),
    ("handoff cancel T-12 [--reason R]", "Cancel a task"),
    ("handoff delete T-12 [--yes]", "Delete a task, its history and its results for good"),
    ("handoff clean [--older-than DAYS] [--yes]", "Remove finished tasks that haven't changed in 30 days (or DAYS)"),
    ("handoff status T-12 open|blocked|done [--reason R]", "Set a task's status directly"),
    ('handoff review T-12 [approve|changes ["notes"]] [--check N=RESULT] [--panel]',
     "Review a task that's waiting for you (with no verdict, it walks you through it)"),
    ('handoff dispatch "codex review my changes, gemini list …" [--yes]',
     "Split one request across your agents and run the parts side by side"),
    ("handoff agents", "Who can take tasks here: changing files, answering, reviewing, pictures"),
    ("handoff approve T-12 AGENT [--kind edit|answer|review|image]", "Let an agent run a task, once"),
    ("handoff run [T-12 ...]", "Run approved tasks now, side by side"),
    ("handoff worker --agent claude|codex [--once] [--share FOLDER]", "Run approved tasks headlessly, each in its own worktree"),
    ("handoff setup [--write|--remove] [--host HOST]", "Connect Claude Code, Codex and Claude Desktop (or take "
                                                        "Handoff out of them)"),
    ("handoff doctor", "Check the board, the project and the apps"),
    ("handoff done-rule [review_or_reason|review|any]", "When agents may mark tasks done"),
    ("handoff keep [DAYS|forever]", "How long this board keeps finished tasks"),
    ("handoff update [--check]", "Get the latest Handoff and reinstall it"),
    ("handoff version", "Show the version"),
    ("handoff help [COMMAND]", "Show this help, or one command's options and examples"),
    ("handoff mcp --as NAME [--project PATH]", "Serve the board to an app over MCP (the app starts this)"),
]

GROUPS = [
    ("See what's happening", ("board", "show")),
    ("Make and change tasks", ("add", "assign", "note", "done", "reopen", "block", "cancel", "delete", "clean",
                               "status")),
    ("Review work", ("review",)),
    ("Hand work to agents", ("dispatch", "agents", "approve", "run", "worker")),
    ("Set up", ("setup", "doctor", "done-rule", "keep", "update", "version", "help")),
    ("For apps", ("mcp",)),
]

# Other names people reach for. `handoff help` mentions them in one line
ALIASES = {
    "list": "board", "ls": "board", "tasks": "board",
    "view": "show", "info": "show", "get": "show",
    "new": "add", "create": "add",
    "comment": "note",
    "rm": "delete", "remove": "delete",
    "close": "done", "finish": "done", "complete": "done",
    "unblock": "reopen",
}
FLAGS = {"--version": "version", "-v": "version", "--help": "help", "-h": "help"}

# What each command's --help ends with; the first one is also the example a usage error shows
EXAMPLES = {
    "board": ["handoff board", "handoff board --all", "handoff board --assignee codex --watch"],
    "show": ["handoff show T-3", "handoff T-3"],
    "add": ['handoff add "Fix the login page" --to codex',
            'handoff add "Write the release notes" --accept "Mentions the new API"'],
    "assign": ["handoff assign T-3 codex", "handoff assign T-3 me", "handoff assign T-3 nobody"],
    "note": ['handoff note T-3 "The bug only happens on Safari"'],
    "done": ["handoff done T-3", 'handoff done T-3 --reason "Fixed in the last release"'],
    "reopen": ["handoff reopen T-3"],
    "block": ['handoff block T-3 "Waiting for the API keys"',
              'handoff block T-3 "Needs a decision on the design" --waiting-on me'],
    "cancel": ["handoff cancel T-3", 'handoff cancel T-3 --reason "Not needed any more"'],
    "delete": ["handoff delete T-3", "handoff delete T-3 --yes"],
    "clean": ["handoff clean", "handoff clean --older-than 90", "handoff clean --older-than 7 --yes"],
    "status": ["handoff status T-3 open", 'handoff status T-3 blocked --reason "Waiting for the API keys"'],
    "review": ["handoff review T-3", "handoff review T-3 approve",
               'handoff review T-3 changes "Handle an empty cart" --check 2=not_met'],
    "dispatch": ['handoff dispatch "codex review my changes, claude fix the footer"',
                 'handoff dispatch --plan "gemini list five ideas for the shop"'],
    "agents": ["handoff agents"],
    "approve": ["handoff approve T-3 claude", "handoff approve T-3 gemini --kind answer",
                "handoff approve T-3 --revoke"],
    "run": ["handoff run", "handoff run T-3 T-4"],
    "worker": ["handoff worker --agent claude", "handoff worker --agent codex --once"],
    "setup": ["handoff setup", "handoff setup --write", "handoff setup --remove"],
    "doctor": ["handoff doctor"],
    "done-rule": ["handoff done-rule", "handoff done-rule review"],
    "keep": ["handoff keep 30", "handoff keep forever", "handoff keep"],
    "update": ["handoff update --check", "handoff update"],
    "version": ["handoff version"],
    "help": ["handoff help", "handoff help show"],
    "mcp": ["handoff mcp --as claude"],
}
HELP_EXAMPLES = [
    'handoff add "Fix the login page" --to codex',
    "handoff T-1",
    "handoff approve T-1 codex",
    "handoff review T-1 approve",
    'handoff dispatch "codex review my changes, claude fix the footer"',
]
# The first commands to know, for `handoff` outside a board
START = [
    ("handoff board", "Who's doing what, and what's blocked"),
    ('handoff add "Fix the login page" --to codex', "Add a task and give it to an agent"),
    ("handoff T-3", "One task with its history (the same as handoff show T-3)"),
    ("handoff review T-3", "Review work that's waiting for you"),
    ('handoff dispatch "codex review my changes, claude fix the footer"', "Hand out work to several agents at once"),
    ("handoff setup", "Connect Claude Code, Codex and Claude Desktop"),
]

CLEAN_DAYS = 30  # handoff clean removes finished tasks older than this, unless told otherwise
PROJECT_HELP = "the project folder (default: the git repository containing this folder)"
YOU = ("me", "you", "myself")  # names for the person, wherever they type a name
NOBODY = ("nobody", "anyone", "none", "-")  # no assignee: anyone can claim it
# What a choice is, for "'xyz' isn't a status you can set; use open, blocked or done"
CHOICES_SAID = {"status": "a status you can set", "verdict": "a verdict", "kind": "a kind of run",
                "host": "an app Handoff can set up", "rule": "a done rule"}
# Flags people bring from elsewhere, and the one this command has
FLAG_SYNONYMS = {"--assignee": "--to", "--assign": "--to", "--for": "--to", "--agent": "--worker",
                 "--worker": "--agent", "--description": "--body", "--message": "--reason", "--why": "--reason",
                 "--force": "--yes"}
_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")


class CommandError(Exception):
    """A problem to report to the person; exit code 1."""


# ── Usage errors in plain words ──────────────────────────────────────────────

class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Wraps the description as argparse does, and keeps the Examples block's lines as they're written."""

    def _fill_text(self, text, width, indent):
        if text.startswith("Examples:"):
            return super()._fill_text(text, width, indent)
        return argparse.HelpFormatter._fill_text(self, text, width, indent)


def _or(items) -> str:
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} or {items[-1]}"


def _and(items, most: int | None = None) -> str:
    """T-1, T-2 and T-3; past `most` of them, the rest as a count."""
    items = [str(i) for i in items]
    if most is not None and len(items) > most:
        items = items[:most] + [f"{len(items) - most} more"]
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


class _Parser(argparse.ArgumentParser):
    """argparse, with its errors in plain words: what's missing or wrong, an example to copy, and where to read
    more. Still exit code 2."""

    def __init__(self, command: str, description: str, needs: dict[str, str] | None = None,
                 choices_said: dict[str, str] | None = None, task_statuses: list[str] | None = None):
        examples = EXAMPLES.get(command, [])
        super().__init__(prog=f"handoff {command}".strip(), description=description, formatter_class=_HelpFormatter,
                         epilog="Examples:\n" + "\n".join(f"  {e}" for e in examples) if examples else None)
        self.command = command
        self.examples = examples
        # what to say when an argument is missing, by its dest; {task} becomes the task they named
        self.needs = {"task": f"Say which task, like: handoff {command} T-3", **(needs or {})}
        self.choices_said = {**CHOICES_SAID, **(choices_said or {})}
        self.task_statuses = task_statuses  # which tasks to list when the task is missing (default: active)
        self.argv: list[str] = []
        self._bad_choice = None
        self._missing = ""

    def parse_args(self, args=None, namespace=None):
        self.argv = list(sys.argv[1:] if args is None else args)
        flag = self._unknown_flag()
        if flag:
            self.fail(self._unknown_flag_said(flag))
        # intermixed: `review T-3 --check 1=met approve` and `add Fix --to codex the footer` read as people mean them.
        # After `--` there are no flags to intermix, and before Python 3.13 intermixing loses what follows it
        if "--" in self.argv:
            parsed, extra = self.parse_known_args(self.argv, namespace)
        else:
            parsed, extra = self.parse_known_intermixed_args(self.argv, namespace)
        if extra:
            self.fail(f"{self.prog} didn't expect {' '.join(extra)!r}.")
        return parsed

    def _check_value(self, action, value):
        if action.choices is not None and value not in action.choices:
            self._bad_choice = (action, value)
        return super()._check_value(action, value)

    def error(self, message: str):
        self.fail(self._plain(message))

    def fail(self, message: str):
        """Say what's wrong and exit 2. A message without a command to copy (": handoff …") gets an example."""
        _say_problem(message)
        if "--json" in self.argv and "--json" in self._option_string_actions:
            self.exit(2)  # a program is calling (the Ixel app shows the last line), so the reason stays last
        if self._missing == "task":
            tasks = self._some_tasks()
            if tasks:
                err_console.print(f"  [{C['dim']}]On this board:[/]")
                for task in tasks:
                    err_console.print(Text.assemble(("    ", ""), (task.ref, C["blue"]), ("  " + task.title, "")),
                                      soft_wrap=True)
        if not re.search(r":\s*handoff ", message) and self.examples:
            err_console.print(f"  [{C['dim']}]For example:[/] {escape(self._example())}", soft_wrap=True)
        err_console.print(f"  [{C['dim']}]More: {escape(f'handoff help {self.command}'.strip())}[/]", soft_wrap=True)
        self.exit(2)

    def _example(self) -> str:
        """An example to copy, with the task they named; after a wrong choice, one that shows a right one."""
        examples = self.examples
        if self._bad_choice is not None:
            right = [e for e in examples if any(f" {c}" in e for c in self._bad_choice[0].choices)]
            examples = right or examples
        return examples[0].replace("T-3", self._typed_task())

    def _plain(self, message: str) -> str:
        """argparse's message, said for a person."""
        from handoff.sanitize import clean_line

        if self._bad_choice is not None:
            action, value = self._bad_choice
            what = self.choices_said.get(action.dest, f"one of the choices for {action.dest}")
            return f"{clean_line(value)[:40]!r} isn't {what}; use {_or(action.choices)}."
        found = re.match(r"the following arguments are required: (.+)", message)
        if found:
            names = found.group(1).split(", ")
            self._missing = self._dest(names[0])
            said = self.needs.get(self._missing) or f"{self.prog} needs {_or(names)}."
            return said.replace("{task}", self._typed_task())
        found = re.match(r"argument ([^:]+): expected one argument", message)
        if found:
            return f"{found.group(1).split('/')[0]} needs a value after it."
        found = re.match(r"argument ([^:]+): invalid (?:float|int) value: (.+)", message)
        if found:
            return f"{found.group(1).split('/')[0]} needs a number, not {found.group(2)}."
        found = re.match(r"ambiguous option: (\S+?)(?:=\S*)? could match (.+)", message)
        if found:
            return f"{found.group(1)} could be {_or(found.group(2).split(', '))}; type more of it."
        message = message[:1].upper() + message[1:]
        return message if message.endswith((".", "?", ")")) else message + "."

    def _dest(self, name: str) -> str:
        for action in self._actions:
            if name in (action.dest, action.metavar, "/".join(action.option_strings)):
                return action.dest
        return name

    def _takes_value(self, token: str) -> bool:
        action = self._option_string_actions.get(token.split("=", 1)[0])
        return action is not None and action.nargs != 0 and "=" not in token

    def _typed_task(self) -> str:
        """The task they named, for the example (T-3 if they named none)."""
        from handoff.board import BoardError, parse_task_id, task_ref

        skip = False
        for token in self.argv:
            if skip:
                skip = False
                continue
            if token.startswith("-"):
                skip = self._takes_value(token)  # its value isn't the task
                continue
            try:
                return task_ref(parse_task_id(token))
            except BoardError:
                continue
        return "T-3"

    def _value_of(self, flag: str) -> str | None:
        for n, token in enumerate(self.argv):
            if token == flag and n + 1 < len(self.argv):
                return self.argv[n + 1]
            if token.startswith(flag + "="):
                return token.split("=", 1)[1]
        return None

    def _some_tasks(self) -> list:
        """Up to five tasks from this project's board, if it already has one (this never makes one, and removes
        nothing its keep setting would: it's only for a message)."""
        from handoff.board import ACTIVE, Board, BoardError
        from handoff.project import ProjectError, board_path, resolve_project

        try:
            root = resolve_project(self._value_of("--project"))
            if not board_path(root).exists():
                return []
            board = Board.open(root, create=False, expire=False)
            tasks = board.tasks(statuses=self.task_statuses or sorted(ACTIVE), limit=5)
        except (BoardError, ProjectError, OSError):
            return []
        return sorted(tasks, key=lambda t: t.id)

    def _unknown_flag(self) -> str | None:
        """The first --flag this command doesn't have (argparse would only say so after other problems)."""
        known = self._option_string_actions
        for token in self.argv:
            if token == "--":
                return None
            if not token.startswith("-") or token == "-" or " " in token or _NUMBER.match(token):
                continue
            flag = token.split("=", 1)[0]
            if flag in known or (flag.startswith("--") and any(o.startswith(flag) for o in known)):
                continue
            return flag
        return None

    def _unknown_flag_said(self, flag: str) -> str:
        options = [o for o in self._option_string_actions if o.startswith("--")]
        close = [FLAG_SYNONYMS[flag]] if FLAG_SYNONYMS.get(flag) in options else \
            difflib.get_close_matches(flag, options, n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]}?" if close else ""
        return f"{self.prog} doesn't take {flag}.{hint}"


# ── Shared helpers ───────────────────────────────────────────────────────────

_opened = None  # the board this command opened, so a task that isn't there can be answered with the ones that are


def print_banner() -> None:
    console.print(f"\n  [{C['gold']}]Handoff[/] [{C['dim']}]v{__version__}[/]  "
                  f"[{C['dim']}]— hand work between your AI agents[/]")
    console.print(f"  [{C['dim']}]{'─' * 44}[/]\n")


def _parser(command: str, description: str, project: bool = True, **kwargs) -> _Parser:
    parser = _Parser(command, description, **kwargs)
    if project:
        parser.add_argument("--project", metavar="PATH", help=PROJECT_HELP)
    return parser


def _root(args: argparse.Namespace) -> Path:
    from handoff.project import resolve_project
    return resolve_project(args.project)


def _with_project(command: str, args: argparse.Namespace) -> str:
    """A command to copy, with the --project they gave (as they typed it)."""
    path = getattr(args, "project", None)
    if not path:
        return command
    return f'{command} --project "{path}"' if " " in path else f"{command} --project {path}"


def _board(args: argparse.Namespace, expire: bool = True):
    """The project's board, made if it isn't there yet. With `expire`, opening it removes the finished tasks its
    keep setting says have had their time, and this says which."""
    global _opened
    from handoff.board import Board
    root = _root(args)
    board = _opened = Board.open(root, expire=expire)
    # With --json, what's printed is the JSON alone: this goes to stderr
    _say_expired(board, err_console if getattr(args, "json", False) else console)
    _maybe_ask_gitignore(board, root)
    return board


def _say_expired(board, out: Console | None = None) -> None:
    """Which finished tasks opening the board just removed, because of how long it keeps them (handoff keep)."""
    if board.expired:
        days = board.keep_days
        how_long = f"{_plural(days, 'day')} after their last change" if days else "as its keep setting says"
        message = (f"Removed {_and([t.ref for t in board.expired], most=10)}: this board removes finished tasks "
                   f"{how_long} (handoff keep).")
        (out or console).print(f"  [{C['dim']}]{escape(message)}[/]", soft_wrap=True)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _keeps_itself_out(root: Path) -> bool:
    """Does `.handoff` still hold the .gitignore Handoff made there, which ignores everything in it?"""
    from handoff.project import BOARD_DIR
    try:
        return "*" in (root / BOARD_DIR / ".gitignore").read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return False


def _maybe_ask_gitignore(board, root: Path) -> None:
    """Ask once whether to add `.handoff/` to the project's .gitignore, when `.handoff` no longer keeps itself
    out of git (its own .gitignore is gone or changed)."""
    from handoff.project import add_board_to_gitignore, gitignore_has_board
    if board.setting("gitignore_asked") or gitignore_has_board(root) or _keeps_itself_out(root) \
            or not _interactive():
        return
    answer = console.input(f"  [{C['dim']}]Keep the board out of git (add[/] .handoff/ [{C['dim']}]to "
                           f".gitignore)? [Y/n][/] ")
    yes = not answer.strip().lower().startswith("n")
    if yes:
        add_board_to_gitignore(root)
        console.print(f"  [{C['green']}]✓[/] Added .handoff/ to .gitignore")
    board.set_setting("gitignore_asked", "yes" if yes else "no")


def _task_id(value: str) -> int:
    from handoff.board import parse_task_id
    return parse_task_id(value)


def _is_task_id(value: str) -> bool:
    from handoff.board import BoardError
    if value.startswith("-"):
        return False
    try:
        _task_id(value)
    except BoardError:
        return False
    return True


def _person(name: str | None) -> str | None:
    """What you type for a name: "me" (or you, myself) is you, `human` on the board."""
    from handoff.board import HUMAN
    return HUMAN if name and name.strip().lower() in YOU else name


def _who(name: str | None) -> str | None:
    """A name on the board as you read it: `human` is you, and an agent an app named "me" or "you" says so."""
    from handoff.board import HUMAN
    if name == HUMAN:
        return "you"
    return f"{name} (an agent)" if name in YOU else name


def _for_you(message: str) -> str:
    """The board's messages are written for agents ("The person can reopen it with: handoff status T-1 open");
    at the command line, the person is you."""
    message = re.sub(r"The person can reopen it with: handoff status (T-\d+) open",
                     r"You can reopen it with: handoff reopen \1", message)
    message = message.replace("; the default is human.", "; the default is you.")
    # A name that isn't valid: said with the flag you typed, and a name you'd type ("me", or an agent's)
    message = re.sub(r"^(return_to|waiting_on) ", lambda m: "--" + m.group(1).replace("_", "-") + " ", message)
    message = re.sub(r"^worker (.*) such as claude, codex or human\.$", r"The agent \1 such as claude or codex.",
                     message)
    message = message.replace("such as claude, codex or human.", "such as claude, codex or me.")
    return message.replace("The person can ", "You can ")


def _ran(message: str) -> str:
    """A run's result as you read it ("handed to human" is handed back to you); the JSON keeps the original."""
    return re.sub(r"^handed to human\b", "handed back to you", message)


def _shown_path(path) -> str:
    """A path as people read it: your home folder as ~."""
    path = Path(path)
    try:
        homes = {Path.home(), Path.home().resolve()}
    except (RuntimeError, OSError):
        return str(path)
    for home in homes:
        if home == Path(home.anchor):  # HOME=/ (a service account): every path would look like ~/…
            continue
        try:
            rest = path.relative_to(home)
        except ValueError:
            continue
        return str(Path("~", rest)) if rest.parts else "~"
    return str(path)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _which_tasks(board) -> str:
    """Which tasks this board has, for when the one you named isn't there."""
    from handoff.board import task_ref
    ids = sorted(t.id for t in board.tasks(statuses=None))
    if not ids:
        return 'The board is empty; add one with: handoff add "title"'
    first, last = task_ref(ids[0]), task_ref(ids[-1])
    if len(ids) <= 2:
        has = f"only {first}" if len(ids) == 1 else f"{first} and {last}"
    elif ids[-1] - ids[0] + 1 == len(ids):
        has = f"{first} to {last}"
    else:
        has = f"{len(ids)} tasks, from {first} to {last}"
    return f"This board has {has}; handoff board --all lists them."


def _say_problem(message: str) -> None:
    """On stderr: the first line in red, any more lines (what to do instead) as they are."""
    first, *rest = message.split("\n")
    err_console.print(f"  [{C['red']}]{escape(first)}[/]", soft_wrap=True)  # soft_wrap: a command pastes whole
    for line in rest:
        err_console.print(f"  {escape(line)}", soft_wrap=True)


def _ok(message: str) -> None:
    console.print(f"  [{C['green']}]✓[/] {escape(message)}", soft_wrap=True)


def _hint(message: str) -> None:
    console.print(f"  [{C['dim']}]{escape(message)}[/]", soft_wrap=True)


def _prose(message: str, width: int = 78) -> None:
    """A dim paragraph with no command in it, wrapped between words and indented, on a terminal or in a pipe."""
    import textwrap
    for line in textwrap.wrap(message, width, initial_indent="  ", subsequent_indent="  ", break_long_words=False,
                              break_on_hyphens=False):
        console.print(f"[{C['dim']}]{escape(line)}[/]", soft_wrap=True)


def _ask(prompt: str) -> str:
    """One line from the person; the prompt is plain text, so [a/c] stays as written."""
    return console.input(Text(prompt)).strip()


def _warn_overlaps(found) -> None:
    for overlap in found:
        text = overlap.describe().replace(", assigned to human", ", assigned to you")
        console.print(f"  [{C['gold']}]⚠[/] {escape(text)}", soft_wrap=True)


def _status_text(status: str) -> Text:
    return Text(status, style=STATUS_STYLE.get(status, ""))


def _print_text(item) -> None:
    """A task's own text: wrapped between words on a terminal; whole lines in a pipe, so a URL or path isn't
    cut in two."""
    console.print(item, soft_wrap=not console.is_terminal)


def _print_table(table) -> None:
    """A table at its natural width when the output isn't a terminal, so a pipe or a log gets whole rows."""
    if console.is_terminal:
        console.print(table)
        return
    from rich.measure import Measurement
    width = Measurement.get(console, console.options.update(width=10_000), table).maximum
    saved = console.width
    console.width = max(saved, width)
    try:
        console.print(table)
    finally:
        console.width = saved


# ── board ────────────────────────────────────────────────────────────────────

def _board_parts(board, root: Path, statuses: list[str] | None,
                 assignee: str | None) -> tuple[list[Text], Table | None]:
    """The board's header lines, and its table (None when no task matches)."""
    from handoff.board import ACTIVE, STATUSES
    from handoff.describe import describe
    from handoff.sanitize import clean_line
    from handoff.timefmt import ago

    tasks = board.tasks(statuses=statuses, assignee=assignee)
    tasks.sort(key=lambda t: STATUS_ORDER.index(t.status))  # stable: newest first within a status
    last = board.last_events((t.id for t in tasks), skip=("claim_paths", "release_paths"))
    counts = board.counts()
    active = sum(n for s, n in counts.items() if s in ACTIVE)
    summary = " · ".join(f"{counts[s]} {s}" for s in STATUSES if counts.get(s)) or "empty"

    lines = [Text.assemble(("  Handoff board", C["gold"]), (f"  {active} active · {summary}", C["dim"])),
             Text(f"  {_shown_path(root)}", style=C["dim"]), Text("")]
    unreadable = board.unreadable()
    if unreadable:
        lines[-1:-1] = [Text(f"  ⚠ {_plural(unreadable, 'task')} on this board can't be read, so "
                             f"{'it isn' if unreadable == 1 else 'they aren'}'t shown. The file may be damaged "
                             "or made by another tool.", style=C["gold"])]
    if not tasks:
        hint = "No tasks match."
        if not counts:
            hint = 'No tasks yet. Add one with: handoff add "title" --to codex'
        elif not active and statuses == sorted(ACTIVE) and assignee is None:
            hint = "Nothing is active right now. To see finished tasks too: handoff board --all"
        return lines + [Text(f"  {hint}", style=C["dim"])], None

    table = Table(box=box.SIMPLE_HEAD, header_style=C["dim"], pad_edge=False, expand=False)
    table.add_column("Task", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Assignee", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Updated", no_wrap=True, style=C["dim"])
    table.add_column("Last", style=C["dim"], overflow="fold")
    for task in tasks:
        event = last.get(task.id)
        table.add_row(Text(task.ref, style=C["blue"]), _status_text(task.status),
                      Text(_who(task.assignee) or "—"), Text(clean_line(task.title)), ago(task.updated_at),
                      Text(f"{_who(event.actor)} {describe(event, _who)}" if event else ""))
    return lines, table


def render_board(board, root: Path, statuses: list[str] | None, assignee: str | None):
    lines, table = _board_parts(board, root, statuses, assignee)
    return Group(*lines, *([table] if table is not None else []))


def _print_board(board, root: Path, statuses: list[str] | None, assignee: str | None) -> None:
    lines, table = _board_parts(board, root, statuses, assignee)
    console.print()
    for line in lines:
        console.print(line, soft_wrap=True)  # a long path stays whole
    if table is not None:
        _print_table(table)
    console.print()


def cmd_board(argv: list[str]) -> int:
    from handoff.board import ACTIVE, STATUSES, check_name

    parser = _parser("board", "Show the board: who's doing what, what's blocked, what's waiting for review.",
                     choices_said={"status": "a status"})
    parser.add_argument("--all", action="store_true", help="include done and cancelled tasks")
    parser.add_argument("--status", choices=STATUSES, metavar="STATUS",
                        help=f"only tasks with this status: {_or(STATUSES)}")
    parser.add_argument("--assignee", metavar="NAME", help="only tasks assigned to NAME (me for yours)")
    parser.add_argument("--watch", action="store_true", help="keep refreshing (Ctrl+C to stop)")
    args = parser.parse_args(argv)
    statuses = [args.status] if args.status else (None if args.all else sorted(ACTIVE))
    assignee = check_name(_person(args.assignee), "--assignee") if args.assignee else None
    root = _root(args)
    board = _board(args)
    if not args.watch:
        _print_board(board, root, statuses, assignee)
        return 0
    if not console.is_terminal:  # piped or logged, where a live view draws nothing: a copy each time it changes
        shown = None
        try:
            while True:
                if board.revision() != shown:
                    shown = board.revision()
                    _print_board(board, root, statuses, assignee)
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        return 0

    from rich.live import Live
    try:
        with Live(render_board(board, root, statuses, assignee), console=console, auto_refresh=False) as live:
            while True:
                time.sleep(2)
                live.update(render_board(board, root, statuses, assignee), refresh=True)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_home(argv: list[str]) -> int:
    """`handoff` on its own: this project's board, if it has one; else how to start. It never makes a board."""
    from handoff.board import ACTIVE, Board
    from handoff.project import ProjectError, board_path, resolve_project

    global _opened
    parser = _parser("", "See this project's board, or how to start.")
    args = parser.parse_args(argv)
    try:
        root = resolve_project(args.project)
    except ProjectError:
        if args.project:
            raise
        root = None
    if root is not None and board_path(root).exists():
        board = _opened = Board.open(root, create=False)
        _say_expired(board)
        _print_board(board, root, sorted(ACTIVE), None)
        _hint("Every command: handoff help · One task: handoff T-N")
        console.print()
        return 0
    print_banner()
    for usage, what in START:
        console.print(Text("    " + usage, style=C["blue"]), soft_wrap=True)
        console.print(Text("        " + what, style=C["dim"]), soft_wrap=True)
    console.print()
    _hint("handoff help lists every command.")
    if root is None:
        _hint("Run Handoff in your project's folder (a git repository), or add --project PATH.")
    else:
        _hint(f"This project has no board yet; {_with_project('handoff board', args)} starts one.")
    console.print()
    return 0


# ── show ─────────────────────────────────────────────────────────────────────

def _indented(text: str, style: str = "") -> Text:
    from handoff.sanitize import clean_text
    return Text("\n".join("      " + line for line in clean_text(text).splitlines()), style=style)


def _next_steps(board, task) -> list[str]:
    """What you can do with a task now, as commands that would work as they are. Each line ends with its
    command, so it copies whole."""
    from handoff.board import HUMAN, STATE_EVENTS, TERMINAL
    from handoff.worker import AGENTS

    ref, who = task.ref, task.assignee
    if task.status in TERMINAL:
        return [f"Next, to work on it again: handoff reopen {ref}"]
    if task.status == "blocked":
        return [f"Next, when it can go on: handoff reopen {ref}"]
    if task.status == "in_review":
        if who == HUMAN:
            return [f"Next, to accept the work: handoff review {ref} approve",
                    f"Or send it back: handoff review {ref} changes \"what to change\""]
        return [f"Next: {_who(who)} reviews it in its app when you say \"check your handoffs\"."]
    last = board.last_event_of(task.id, STATE_EVENTS)
    if last is not None and last.kind == "worker" and last.data.get("state") == "started":
        # approving it again now would throw away what the run brings back
        return [f"Next: the {_who(last.actor)} worker is running it now; the result shows here when it finishes."]
    if any(t.id == task.id for t, _ in board.pending_runs(None, [task.id])):
        return [f"Next: it's approved for {_who(who)}. Run it now: handoff run {ref}"]
    if who is None:
        return [f"Next, to give it to an agent: handoff assign {ref} codex",
                f"Or let one run it now: handoff approve {ref} claude"]
    if who == HUMAN:
        return [f"Next: it's with you. When it's finished: handoff done {ref}",
                f"To hand it on: handoff assign {ref} codex"]
    if task.status in ("claimed", "in_progress"):
        return [f"Next: {_who(who)} is working on it; it shows here when {_who(who)} hands it over."]
    run = f"handoff approve {ref} {who}" + ("" if who in AGENTS else " --kind answer")
    return [f"Next: {_who(who)} picks it up in its app when you say \"check your handoffs\".", f"Or run it now: {run}"]


def _history(events) -> list:
    """A task's history as `handoff show` prints it, with what each one wrote."""
    from handoff.describe import checks_text, describe, handoff_text, panel_text
    from handoff.timefmt import stamp

    out: list = []
    for event in events:
        out.append(Text.assemble(("  " + stamp(event.at), C["dim"]), ("  ", ""), (_who(event.actor), C["blue"]),
                                 (" " + describe(event, _who), "")))
        if event.kind == "handoff":
            out.append(_indented(handoff_text(event.data)))
        elif event.kind in ("claim_paths", "release_paths"):
            paths = event.data.get("paths")
            out.append(_indented("\n".join(p for p in paths if isinstance(p, str))
                                 if isinstance(paths, list) else "", C["dim"]))
        elif event.text:
            out.append(_indented(event.text))
        if event.kind == "review" and checks_text(event.data.get("checks")):
            out.append(_indented(checks_text(event.data["checks"])))
        if isinstance(event.data.get("panel"), dict):
            out.append(_indented("Ixel panel: " + panel_text(event.data["panel"]), C["violet"]))
    return out


def cmd_show(argv: list[str]) -> int:
    from handoff.board import task_ref
    from handoff.project import board_path
    from handoff.sanitize import clean_line, clean_text
    from handoff.timefmt import ago

    parser = _parser("show", "Show one task with its full history. handoff T-12 on its own does the same.")
    parser.add_argument("task", metavar="T-12", help="the task: T-12, t12 or just 12")
    args = parser.parse_args(argv)
    task_id = _task_id(args.task)
    if not board_path(_root(args)).exists():  # looking at a task never makes a board
        raise CommandError(f"This project has no board yet, so there's no task {task_ref(task_id)}.\nAdd one with: "
                           + _with_project('handoff add "title"', args))
    board = _board(args)
    task, events, claims, children = board.task_with_history(task_id)

    console.print()
    console.print(Text.assemble(("  ", ""), (task.ref, C["blue"]), ("  ", ""), _status_text(task.status),
                                (f"  assigned to {_who(task.assignee) or 'nobody'}", C["dim"])), soft_wrap=True)
    out: list = [Text(f"  {clean_line(task.title)}", style="bold")]
    meta = [f"created by {_who(task.created_by)} {ago(task.created_at)}", f"updated {ago(task.updated_at)}"]
    if task.branch:
        meta.append(f"branch {clean_line(task.branch)}")
    if task.parent_id:
        meta.append(f"parent {task_ref(task.parent_id)}")
    if children:
        meta.append("subtasks " + ", ".join(c.ref for c in children))
    out.append(Text("  " + " · ".join(meta), style=C["dim"]))
    if claims:
        out.append(Text("  claims " + ", ".join(c.path_glob for c in claims), style=C["dim"]))
    if task.body:
        out += [Text(""), Text("\n".join("  " + line for line in clean_text(task.body).splitlines()))]
    if task.acceptance:
        out += [Text(""), Text("  Acceptance", style=C["dim"])]
        out += [Text(f"  ☐ {n}. {clean_line(check)}") for n, check in enumerate(task.acceptance, 1)]
    out += [Text(""), Text("  History", style=C["dim"]), *_history(events), Text("")]
    for item in out:
        _print_text(item)
    for line in _next_steps(board, task):
        _hint(line)
    console.print()
    return 0


# ── The person's controls ────────────────────────────────────────────────────

def cmd_add(argv: list[str]) -> int:
    from handoff.board import HUMAN

    parser = _parser("add", "Add a task to the board. Quotes around the title are optional.")
    parser.add_argument("title", nargs="*", help="what needs doing")
    parser.add_argument("--to", metavar="NAME", help="who does it: claude, codex, me… (default: anyone)")
    parser.add_argument("--body", default="", help='what needs doing, in detail ("-" reads it from stdin)')
    parser.add_argument("--accept", action="append", default=[], metavar="CHECK",
                        help="an acceptance check (repeat for more)")
    parser.add_argument("--parent", metavar="T-N", help="make it a subtask of this task")
    parser.add_argument("--path", action="append", default=[], metavar="PATH",
                        help="a file or folder it will change (repeat for more)")
    parser.add_argument("--branch", help="the git branch the work goes on")
    args = parser.parse_args(argv)
    title, to = " ".join(args.title).strip(), args.to
    if not title and not _interactive():
        parser.fail('Say what needs doing, like: handoff add "Fix the login page" --to codex')
    body = sys.stdin.read() if args.body == "-" else args.body
    board = _board(args)
    if not title:
        title = _ask("  What needs doing? ")
        if not title:
            _hint("Nothing was added.")
            return 1
        if to is None:
            to = _ask("  Who should do it? (claude, codex… or Enter for anyone) ") or None
    to = None if to and to.strip().lower() in NOBODY else _person(to)
    parent = _task_id(args.parent) if args.parent else None
    task, found = board.create(HUMAN, title, body, args.accept, to, parent, args.path, args.branch)
    who = f" for {_who(task.assignee)}" if task.assignee else " (anyone can claim it)"
    _ok(f"Added {task.ref}{who}.")
    _warn_overlaps(found)
    if task.assignee and task.assignee != HUMAN:
        _hint(f"Tell {task.assignee} to check its handoffs.")
    elif not task.assignee:
        _hint(f"Give it to an agent with: handoff assign {task.ref} codex")
    return 0


def cmd_assign(argv: list[str]) -> int:
    from handoff.board import HUMAN

    parser = _parser("assign", "Give a task to someone (claude, codex, you…), or to nobody.",
                     needs={"task": "Say which task and who gets it, like: handoff assign T-3 codex",
                            "name": "Say who gets it: handoff assign {task} codex, or nobody"})
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("name", help='who gets it: claude, codex, me… or "nobody"')
    args = parser.parse_args(argv)
    name = None if args.name.strip().lower() in NOBODY else _person(args.name)
    task = _board(args).assign(HUMAN, _task_id(args.task), name)
    _ok(f"{task.ref} is assigned to {_who(task.assignee) or 'nobody'} ({task.status}).")
    if task.assignee and task.assignee != HUMAN:
        _hint(f"Tell {task.assignee} to check its handoffs.")
    return 0


def cmd_note(argv: list[str]) -> int:
    from handoff.board import HUMAN, task_ref

    parser = _parser("note", "Add a note to a task. Quotes around the note are optional.",
                     needs={"task": 'Say which task and what to note, like: handoff note T-3 "The bug only happens '
                                    'on Safari"'})
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("text", nargs="*", help='the note ("-" reads it from stdin)')
    args = parser.parse_args(argv)
    task_id = _task_id(args.task)
    text = " ".join(args.text).strip()
    if not text:
        parser.fail(f'Say what to note, like: handoff note {task_ref(task_id)} "The bug only happens on Safari"')
    text = sys.stdin.read() if text == "-" else text
    task = _board(args).note(HUMAN, task_id, text)
    _ok(f"Noted on {task.ref}.")
    return 0


def _check_mark(value: str) -> dict:
    """`--check 2=met`, `2=not_met` or `2=not_checked`, optionally with a note: `2=not_met:fails when empty`."""
    number, _, rest = value.partition("=")
    result, _, note = rest.partition(":")
    number = number.strip()
    if not (number.isascii() and number.isdecimal()) or not result.strip():
        raise CommandError(f"--check {value!r}: mark a check by its number, like 2=met, 2=not_met or "
                           "2=not_checked (add :note for a note).")
    return {"check": int(number), "result": result.strip().lower().replace("-", "_"), "note": note.strip()}


def _ask_review(board, task, marks: list[dict]) -> tuple[str, str, list[dict]] | None:
    """Walk the person through a review: what came back, the checks, then their verdict. None: nothing to record."""
    from handoff.describe import handoff_text
    from handoff.timefmt import ago

    events = board.events(task.id)
    handed = next((e for e in reversed(events) if e.kind == "handoff"), None)
    asked = next((e for e in reversed(events) if e.kind == "review_requested"), None)
    console.print()
    console.print(Text.assemble(("  ", ""), (task.ref, C["blue"]), ("  ", ""), (task.title, "bold")), soft_wrap=True)
    if handed is not None:
        _hint(f"{_who(handed.actor)} handed it over {ago(handed.at)}:")
        _print_text(_indented(handoff_text(handed.data)))
    if asked is not None and asked.text:
        _hint(f"{_who(asked.actor)} asked for a review {ago(asked.at)}:")
        _print_text(_indented(asked.text))
    if task.acceptance:
        _hint("Acceptance checks:")
        for n, check in enumerate(task.acceptance, 1):
            console.print(Text(f"      {n}. {check}"), soft_wrap=True)
    console.print()
    answer = _ask("  Approve it, or send it back with changes? [a/c] ").lower()
    if answer in ("a", "approve", "y", "yes"):
        verdict = "approve"
    elif answer in ("c", "changes", "change"):
        verdict = "changes"
    else:
        _hint("Nothing was recorded.")
        return None
    if task.acceptance and not marks:
        _hint("Mark each check: y if it's met, n if it isn't, Enter to skip it.")
        for n, check in enumerate(task.acceptance, 1):
            mark = _ask(f"    {n}. {check} [y/n] ").lower()
            if mark[:1] in ("y", "n"):
                marks.append({"check": n, "result": "met" if mark[:1] == "y" else "not_met", "note": ""})
    if verdict == "approve" and any(m["result"] == "not_met" for m in marks):
        again = _ask("  A check isn't met, so send it back with changes instead? [Y/n] ")
        if again.lower().startswith("n"):
            _hint("Nothing was recorded.")
            return None
        verdict = "changes"
    notes = _ask("  What should change? " if verdict == "changes" else "  Any notes? (Enter for none) ")
    if verdict == "changes" and not notes:
        _hint("Nothing was recorded: say what should change, so they know what to fix.")
        return None
    return verdict, notes, marks


def cmd_review(argv: list[str]) -> int:
    from handoff.board import HUMAN, task_ref

    parser = _parser("review", "Review a task that's waiting for you: approve it, or send it back with changes. "
                               "With no verdict, it shows you what came back and asks.",
                     needs={"task": "Say which task to review, like: handoff review T-3"}, task_statuses=["in_review"])
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("verdict", nargs="?", choices=["approve", "changes"], metavar="approve|changes",
                        help="approve it, or send it back with changes (leave it out to be asked)")
    parser.add_argument("notes", nargs="*", help="what you checked, and what to change")
    parser.add_argument("--check", action="append", default=[], metavar="N=RESULT",
                        help="mark acceptance check N as met, not_met or not_checked, optionally with a note "
                             "(2=not_met:fails when empty); repeat for each check. Once you mark one, the "
                             "checks you don't mark are recorded as not_checked")
    parser.add_argument("--panel", action="store_true",
                        help="also ask your Ixel panel (other AI models) and keep their verdict with yours")
    args = parser.parse_args(argv)
    marks = [_check_mark(value) for value in args.check]
    verdict, notes = args.verdict, " ".join(args.notes)
    task_id = _task_id(args.task)
    if verdict == "changes" and not notes.strip():
        parser.fail(f'Say what to change: handoff review {task_ref(task_id)} changes "what to change"')
    board = _board(args)
    if verdict is None:
        task = board.get(task_id)
        if task.status != "in_review":
            raise CommandError(f"{task.ref} isn't waiting for a review (it's {task.status}).")
        if not _interactive():
            ref = task_ref(task_id)
            parser.fail(f'Say approve or changes: handoff review {ref} approve, or handoff review {ref} changes '
                        '"what to change"')
        answer = _ask_review(board, task, marks)
        if answer is None:
            return 1
        verdict, notes, marks = answer
    panel = None
    if args.panel:
        from handoff import ixel
        from handoff.describe import panel_text

        task, events, results = board.check_review(HUMAN, task_id, verdict, notes, marks)
        try:
            with console.status("  Asking the Ixel panel (this can take a few minutes)…"):
                panel = ixel.run_panel(ixel.review_prompt(task, events, HUMAN, notes, results))
        except ixel.PanelUnavailable as exc:
            raise CommandError(f"{exc} Nothing was recorded.") from exc
        console.print(Text("  Ixel panel: ", style=C["violet"]) + Text(panel_text(panel)))
    task = board.review(HUMAN, task_id, verdict, notes, panel=panel, checks=marks)
    verb = "Approved" if verdict == "approve" else "Sent back"
    _ok(f"{verb} {task.ref}; " + ("it's back with you." if task.assignee == HUMAN else
                                  f"it's with {task.assignee} again."))
    return 0


def cmd_status(argv: list[str]) -> int:
    from handoff.board import HUMAN

    parser = _parser("status", "Change a task's status. As the person, you can also reopen finished tasks. "
                               "handoff done, reopen and block do the same in fewer words.",
                     needs={"task": "Say which task and its new status, like: handoff status T-3 open",
                            "status": "Say the new status: handoff status {task} open, blocked or done"})
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("status", choices=["open", "blocked", "done"], metavar="open|blocked|done")
    parser.add_argument("--reason", default="", help="why (required for blocked)")
    parser.add_argument("--waiting-on", metavar="NAME", help="for blocked: who needs to act (me for you)")
    args = parser.parse_args(argv)
    task_id = _task_id(args.task)
    if args.status == "blocked" and not args.reason.strip():
        from handoff.board import task_ref
        parser.fail(f'Say why it\'s blocked: handoff status {task_ref(task_id)} blocked --reason "Waiting for the '
                    'API keys"')
    task = _board(args).set_status(HUMAN, task_id, args.status, args.reason, _person(args.waiting_on))
    _ok(f"{task.ref} is {task.status}.")
    return 0


def cmd_done(argv: list[str]) -> int:
    from handoff.board import HUMAN

    parser = _parser("done", "Mark a task done. You don't need a review for this, whatever the done rule says.")
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("--reason", default="", help="why it's done (kept in its history)")
    args = parser.parse_args(argv)
    board = _board(args)
    task = board.get(_task_id(args.task))
    if task.status == "done":
        _hint(f"{task.ref} is already done.")
        return 0
    task = board.set_status(HUMAN, task.id, "done", args.reason)
    _ok(f"{task.ref} is done.")
    return 0


def cmd_reopen(argv: list[str]) -> int:
    from handoff.board import HUMAN

    parser = _parser("reopen", "Open a done, cancelled or blocked task again. It keeps its assignee and history.",
                     task_statuses=["done", "cancelled", "blocked"])
    parser.add_argument("task", metavar="T-12")
    args = parser.parse_args(argv)
    board = _board(args)
    task = board.get(_task_id(args.task))
    if task.status == "open":
        _hint(f"{task.ref} is already open.")
        return 0
    if task.status == "in_review":
        raise CommandError(f"{task.ref} is waiting for a review, so there's nothing to reopen.\nReview it with: "
                           f"handoff review {task.ref}")
    if task.status not in ("done", "cancelled", "blocked"):
        raise CommandError(f"{task.ref} is {task.status}, not done, cancelled or blocked, so there's nothing to "
                           f"reopen.\nTo set it back to open anyway: handoff status {task.ref} open")
    task = board.set_status(HUMAN, task.id, "open")
    who = f"assigned to {_who(task.assignee)}." if task.assignee else "anyone can claim it."
    _ok(f"{task.ref} is open again, {who}")
    return 0


def cmd_block(argv: list[str]) -> int:
    from handoff.board import HUMAN, task_ref

    parser = _parser("block", "Mark a task blocked, with why. handoff reopen T-12 opens it again.",
                     needs={"task": 'Say which task and why it\'s blocked, like: handoff block T-3 "Waiting for the '
                                    'API keys"'})
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("why", nargs="*", help="why it's blocked")
    parser.add_argument("--reason", default="", help="the same as WHY")
    parser.add_argument("--waiting-on", metavar="NAME", help="who needs to act: claude, codex, me…")
    args = parser.parse_args(argv)
    task_id = _task_id(args.task)
    why = " ".join(args.why).strip()
    if why and args.reason:
        parser.fail(f'Say why once: handoff block {task_ref(task_id)} "{why}"')
    reason = why or args.reason
    if not reason.strip():
        parser.fail(f'Say why {task_ref(task_id)} is blocked, like: handoff block {task_ref(task_id)} "Waiting for '
                    'the API keys"')
    task = _board(args).set_status(HUMAN, task_id, "blocked", reason, _person(args.waiting_on))
    _ok(f"{task.ref} is blocked" + (f", waiting on {_who(task.waiting_on)}." if task.waiting_on else "."))
    _hint(f"When it can go on: handoff reopen {task.ref}")
    return 0


def cmd_cancel(argv: list[str]) -> int:
    from handoff.board import HUMAN

    parser = _parser("cancel", "Cancel a task (it stays on the board, with its history).")
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("--reason", default="", help="why (kept in its history)")
    args = parser.parse_args(argv)
    task = _board(args).cancel(HUMAN, _task_id(args.task), args.reason)
    _ok(f"{task.ref} is cancelled.")
    return 0


def cmd_delete(argv: list[str]) -> int:
    from handoff.board import HUMAN, task_ref

    parser = _parser("delete", "Delete a task, its history and its results in .handoff/outputs for good. handoff "
                               "cancel keeps the history. The worker's worktree and branch for it stay, since they "
                               "hold the agent's work; it says how to remove them.")
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args(argv)
    board = _board(args)
    task = board.get(_task_id(args.task))
    if not args.yes:
        if not _interactive():
            raise CommandError(f"Deleting {task.ref} can't be undone; pass --yes to confirm: "
                               f"handoff delete {task.ref} --yes")
        answer = console.input(f"  Delete {task.ref}, its history and its results for good? Type {task.ref} to "
                               "confirm: ")
        if answer.strip().upper() != task.ref.upper():
            _hint(f"Kept {task.ref}.")
            return 1
    left = board.delete(HUMAN, task.id)
    _ok(f"Deleted {task_ref(task.id)}.")
    for problem in left:
        _warn(problem)
    _say_work_left(_root(args), lambda ref: ref == task.ref)
    leftover = board.leftover_outputs()
    if leftover:  # (from deletes before they took the results too)
        _hint(f"The results of {_and(leftover, most=10)}, which are no longer on the board, are still in "
              ".handoff/outputs. handoff clean lists them and asks before removing them.")
    return 0


def _warn(message: str) -> None:
    console.print(f"  [{C['gold']}]⚠[/] {escape(message)}", soft_wrap=True)


def _say_work_left(root: Path, gone: Callable[[str], bool]) -> None:
    """Removing a task leaves its worktree and branch from the worker alone, since they hold an agent's work: say
    which are still there for the tasks that are gone (by `gone`), and how to remove them."""
    from handoff import gitwork

    found = gitwork.work_left(root, gone)
    if not found:
        return
    refs = [ref for ref, _, _ in found]
    worktree, branch = any(w for _, w, _ in found), any(b for _, _, b in found)
    if len(refs) == 1:
        things = " and ".join(word for word, have in (("worktree", worktree), ("branch", branch)) if have)
        console.print(f"  {refs[0]}'s {things} {'are' if worktree and branch else 'is'} still there, with the agent's "
                      f"work (its commits carry the task's title). If you don't need "
                      f"{'them' if worktree and branch else 'it'} any more:", soft_wrap=True)
    else:
        things = " and ".join(word for word, have in (("worktrees", worktree), ("branches", branch)) if have)
        console.print(f"  The {things} of {_and(refs)} are still there, with the agents' work (their commits carry "
                      "the tasks' titles). If you don't need them any more:", soft_wrap=True)
    for ref, has_worktree, has_branch in found[:3]:
        if has_worktree:
            _hint(f"  git worktree remove {(gitwork.WORKTREES / ref).as_posix()}")
        if has_branch:
            _hint(f"  git branch -D handoff/{ref}")
    if len(refs) > 3:
        _hint(f"  and the same for the other {len(refs) - 3}")


def _gone_from(board) -> Callable[[str], bool]:
    """Is a task (T-N) gone from the board? Every task that is: also ones deleted, or removed by the board's keep
    setting, before."""
    on_board = {t.ref for t in board.tasks(statuses=None)}
    return lambda ref: ref not in on_board


def _confirm_removal(going, waiting, heading: str, command: str, args, leftover=()) -> bool:
    """Show the tasks, and the results of tasks already gone (`leftover`), about to be removed for good, and ask
    (unless --yes). False if the person says no."""
    from handoff.timefmt import ago

    if going:
        console.print(f"  {heading}", soft_wrap=True)
        width = max(len(task.ref) for task in going)
        for task in going:
            console.print(Text.assemble(("    ", ""), (task.ref.ljust(width), C["blue"]), ("  ", ""),
                                        (task.status.ljust(len("cancelled")), STATUS_STYLE[task.status]),
                                        ("  " + task.title, ""), (f"  changed {ago(task.updated_at)}", C["dim"])),
                          soft_wrap=True)
    _say_waiting(waiting)
    if leftover:
        console.print(f"  Results in .handoff/outputs of tasks that are no longer on the board: {_and(leftover)}",
                      soft_wrap=True)
    if args.yes:
        return True
    if not _interactive():
        raise CommandError(f"Removing them can't be undone; pass --yes to confirm: {_with_project(command, args)}")
    if going and leftover:
        question = "Remove all of this for good?"
    elif going:
        them, their = ("it", "its") if len(going) == 1 else (f"these {len(going)}", "their")
        question = f"Remove {them}, with {their} history and results, for good?"
    else:
        question = f"Remove {'these results' if len(leftover) > 1 else 'them'} for good?"
    return _ask(f"  {question} [y/N] ").lower() in ("y", "yes")


def _say_waiting(waiting) -> None:
    if waiting:
        _hint(f"Staying for now, until their subtasks can go too: {', '.join(t.ref for t in waiting)}")


def _say_removed(removed, left, shown: int) -> None:
    _ok(f"Removed {_plural(len(removed), 'task')}.")
    stayed = shown - len(removed)
    if stayed:
        _hint(f"{_plural(stayed, 'task')} changed since you were shown {'it' if stayed == 1 else 'them'}, so "
              f"{'it' if stayed == 1 else 'they'} stayed.")
    for problem in left:
        _warn(problem)


def cmd_clean(argv: list[str]) -> int:
    from handoff.board import HUMAN, MAX_KEEP_DAYS

    parser = _parser("clean", "Remove finished tasks (done or cancelled) that haven't changed for a while, with "
                              "their history and their results in .handoff/outputs, for good. A task goes with its "
                              "subtasks, once they can all go. It also removes the folders in .handoff/outputs "
                              "whose task is no longer on the board, such as the results of tasks deleted with an "
                              "older Handoff. It lists what it will remove and asks first. The worker's worktrees "
                              "and branches stay, since they hold the agents' work; it says how to remove them.")
    parser.add_argument("--older-than", type=int, default=CLEAN_DAYS, metavar="DAYS",
                        help=f"tasks last changed more than DAYS days ago (default: {CLEAN_DAYS}; 0 for every "
                             "finished task)")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args(argv)
    days = args.older_than
    if not 0 <= days <= MAX_KEEP_DAYS:
        parser.fail(f"--older-than needs a number of days from 0 to {MAX_KEEP_DAYS}.")
    root, board = _root(args), _board(args)
    going, waiting = board.cleanable(days)
    leftover = board.leftover_outputs()
    if not going and not leftover:
        _hint(f"Nothing to clean: no finished task was last changed more than {_plural(days, 'day')} ago."
              if days else "Nothing to clean: there are no finished tasks.")
        _say_waiting(waiting)
        _say_work_left(root, _gone_from(board))
        return 0
    heading = (f"Finished tasks last changed more than {_plural(days, 'day')} ago:" if days else
               "Every finished task:")
    if not _confirm_removal(going, waiting, heading, f"handoff clean --older-than {days} --yes", args, leftover):
        _hint("Nothing was removed.")
        return 1
    if going:
        _say_removed(*board.clean(HUMAN, days, [t.id for t in going]), len(going))
    if leftover:
        removed, left = board.clean_outputs(HUMAN, leftover)
        if removed:
            _ok(f"Removed the results of {_and(removed, most=10)}.")
        for problem in left:
            _warn(problem)
    _say_work_left(root, _gone_from(board))
    if going and days and board.keep_days is None:
        _hint(f"To have this done by itself from now on: handoff keep {days}")
    return 0


def cmd_keep(argv: list[str]) -> int:
    from handoff.board import HUMAN, MAX_KEEP_DAYS

    parser = _parser("keep", "Show or set how long this board keeps finished tasks. With a number of days, a done or "
                             "cancelled task is removed that long after its last change, with its history and its "
                             "results in .handoff/outputs, whenever Handoff opens the board. With forever, finished "
                             "tasks stay until you delete them or run handoff clean. Setting a number of days lists "
                             "the tasks that go now and asks first. Showing the setting, or setting forever, removes "
                             "nothing.")
    parser.add_argument("days", nargs="?", metavar="DAYS",
                        help="a number of days, or forever (leave it out to see the setting now)")
    parser.add_argument("--yes", action="store_true", help="don't ask before removing tasks that are already older")
    args = parser.parse_args(argv)
    # Opened without removing what the setting now in place says has had its time: looking at the setting
    # removes nothing, and changing it removes only what the new one says, after asking
    if args.days is None:
        board = _board(args, expire=False)
        default = " (the default)" if board.setting("keep_days") is None else ""
        console.print(f"  {_keeping(board.keep_days)}{default}.", soft_wrap=True)
        _hint("To change it: handoff keep 30, or handoff keep forever")
        return 0
    word = args.days.strip().lower()
    if word != "forever" and not (word.isascii() and word.isdigit() and 1 <= int(word) <= MAX_KEEP_DAYS):
        parser.fail(f"{args.days[:40]!r} isn't how long to keep finished tasks; use a number of days from 1 to "
                    f"{MAX_KEEP_DAYS}, or forever.")
    days = None if word == "forever" else int(word)
    root, board = _root(args), _board(args, expire=False)
    going, waiting = board.cleanable(days) if days is not None else ([], [])
    if going and not _confirm_removal(going, waiting, f"Keeping finished tasks {_plural(days, 'day')} removes these "
                                      "now:", f"handoff keep {days} --yes", args):
        _hint("Nothing changed.")
        return 1
    board.set_keep_days(HUMAN, days)
    _ok(f"{_keeping(days)}.")
    if going:
        _say_removed(*board.clean(HUMAN, days, [t.id for t in going]), len(going))
        _say_work_left(root, _gone_from(board))
    if days is not None:
        _hint("Handoff removes them whenever it opens the board. To keep them: handoff keep forever")
    return 0


def _keeping(days: int | None) -> str:
    if days is None:
        return "This board keeps finished tasks until you delete them or run handoff clean"
    return f"This board removes each finished task {_plural(days, 'day')} after its last change"


def cmd_done_rule(argv: list[str]) -> int:
    from handoff.board import DONE_RULES

    parser = _parser("done-rule", "Show or set when agents may mark a task done on this board. You can always "
                                  "close a task yourself.")
    parser.add_argument("rule", nargs="?", choices=list(DONE_RULES), metavar="RULE",
                        help=f"{_or(DONE_RULES)} (leave it out to see the rule now)")
    args = parser.parse_args(argv)
    board = _board(args)
    if args.rule:
        board.set_done_rule(args.rule)
        _ok(f"Agents may now mark tasks done {DONE_RULES[args.rule]}.")
    else:
        rule = board.done_rule
        console.print(f"  Agents may mark tasks done {escape(DONE_RULES[rule])} [{C['dim']}]({rule})[/].",
                      soft_wrap=True)
        _hint("You can always close a task yourself: handoff done T-12")
    return 0


# ── The worker ───────────────────────────────────────────────────────────────

def _sandbox_word(agent: str) -> str | None:
    """How an edit by `agent` runs here: in its sandbox, edit-only where that can't run, or None (it can't)."""
    from handoff import sandbox
    from handoff.worker import AGENTS
    spec = AGENTS[agent]
    try:
        sandbox.check(spec.needs)
        return "in its sandbox"
    except sandbox.SandboxUnavailable:
        return "edit-only (no commands, since its sandbox can't run here)" if spec.fallback else None


def cmd_approve(argv: list[str]) -> int:
    from handoff.board import HUMAN, RUN_KINDS, TERMINAL
    from handoff.crew import board_name
    from handoff.sanitize import clean_line
    from handoff.worker import AGENTS

    parser = _parser("approve", "Approve a task for an agent to run, once: claude or codex change files in their "
                                "own git worktree, in a sandbox (where it can't run, Claude only edits files); "
                                "other models answer, review or make pictures through Ixel MAT. Then handoff run "
                                "starts it (a running handoff worker --agent claude or codex also picks up what "
                                "you approve for it).",
                     needs={"task": "Say which task, like: handoff approve T-3 claude"})
    parser.add_argument("task", metavar="T-12")
    parser.add_argument("agent", nargs="?", metavar="AGENT",
                        help="the agent to run: claude or codex for an edit; any model in Ixel MAT for the other "
                             "kinds (handoff agents)")
    parser.add_argument("--worker", metavar="AGENT", help="the same as AGENT")
    parser.add_argument("--kind", choices=RUN_KINDS, default="edit", metavar="KIND",
                        help="edit: change files in its own worktree (claude, codex); answer, review (your changes) "
                             "or image: through Ixel MAT, nothing in the project changes (default: edit)")
    parser.add_argument("--return-to", default=HUMAN, metavar="NAME",
                        help="who gets the task back when the run ends (default: you)")
    parser.add_argument("--revoke", action="store_true", help="withdraw an approval the worker hasn't used yet")
    parser.add_argument("--yes", action="store_true", help="don't ask; approve the task as shown")
    args = parser.parse_args(argv)
    if args.agent and args.worker and args.agent.lower() != args.worker.lower():
        parser.fail(f"Name the agent once, like: handoff approve {args.task} {args.agent}")
    agent = args.agent or args.worker
    return_to = _person(args.return_to)
    board = _board(args)
    task_id = _task_id(args.task)
    if args.revoke:
        task = board.revoke_approval(HUMAN, task_id)
        _ok(f"{task.ref} is no longer approved for the worker.")
        return 0
    task = board.get(task_id)
    ref = task.ref
    # The board refuses these too; saying so first spares you a task you can't approve
    if task.status in TERMINAL:
        raise CommandError(f"{ref} is {task.status}, so there's nothing to run. You can reopen it with: "
                           f"handoff reopen {ref}")
    if task.status == "in_review":
        if task.assignee == HUMAN and not agent:
            raise CommandError(f"{ref} is waiting for your review.\nTo accept the work: handoff review {ref} approve\n"
                               f'To send it back for more work: handoff review {ref} changes "what to change"')
        raise CommandError(f"{ref} is waiting for a review; an agent can run it once the review is done.\n"
                           f"See it with: handoff show {ref}")
    asked = False
    if not agent:
        assignee = task.assignee
        fits = assignee in AGENTS if args.kind == "edit" else assignee not in (None, HUMAN)
        if fits and _interactive() and not args.yes:
            agent, asked = assignee, True  # you confirm it below, with the task in front of you
        elif fits:
            parser.fail(f"Say which agent runs {ref}, like: handoff approve {ref} {assignee}\nHandoff only picks "
                        f"the assignee ({assignee}) for you when it can ask you first, at a terminal.")
        else:
            parser.fail(f"Say which agent runs {ref}, like: handoff approve {ref} claude (or codex)\n"
                        "To see who can take tasks here: handoff agents")
    if args.kind == "edit" and agent not in AGENTS:
        raise CommandError(f"Only {' and '.join(AGENTS)} can change files through the worker. To have "
                           f"{agent} answer, review your changes or make pictures, add --kind answer, "
                           "review or image.")
    if args.kind != "edit" and board_name(agent):  # (asks Ixel MAT who it has; no model is asked anything)
        from handoff import asks
        problem = asks.unrunnable(board_name(agent), args.kind, asks.ixel_roster(_root(args)))
        if problem:
            raise CommandError(f"{problem}\nTo see who can do what here: handoff agents")
    # What the agent will be told is the task's text, which another agent may have written: show it first.
    # What's sealed is what was shown: if an agent changes the task while you read it, you see it again.
    from handoff.board import Changed, content_of
    whose = f" ({ref}'s assignee)" if asked else ""
    for attempt in range(1, 6):
        task, events, _, _ = board.task_with_history(task_id)
        console.print(f"\n  [{C['dim']}]The {escape(agent)} worker{whose} gets {ref} as it is now, with its "
                      "history:[/]", soft_wrap=True)
        _print_text(Text(f"  {clean_line(task.title)}", style="bold"))
        if task.body:
            _print_text(_indented(task.body))
        for n, check in enumerate(task.acceptance, 1):
            _print_text(Text(f"      ☐ {n}. {clean_line(check)}"))
        for line in [Text("  History", style=C["dim"]), *_history(events)]:
            _print_text(line)  # (notes and handoffs, which agents may have written, go to the agent too)
        if not args.yes and _interactive():
            answer = console.input(f"  Approve {ref} for the {escape(agent)} worker? [Y/n] ")
            if answer.strip().lower().startswith("n"):
                _hint("Not approved.")
                return 1
        try:
            task = board.approve(HUMAN, task_id, agent, return_to, args.kind, shown=content_of(task, events))
            break
        except Changed:
            if attempt == 5:
                raise CommandError(f"{ref} keeps changing (an agent may be working on it), so it wasn't approved. "
                                   "Try again in a moment.") from None
            console.print(f"\n  [{C['gold']}]⚠[/] The task changed while you were reading it; here it is again.",
                          soft_wrap=True)
    if args.kind != "edit":
        _ok(f"Approved {task.ref} for {agent} to {KIND_VERBS[args.kind]}, once, through Ixel MAT.")
        _hint(f"Nothing in the project changes; the result goes to .handoff/outputs/{task.ref} and the task to "
              f"{_who(return_to)}.")
        console.print(f"  [{C['dim']}]Run it with:[/] handoff run {task.ref}", soft_wrap=True)
        return 0
    _ok(f"Approved {task.ref} for the {agent} worker.")
    how = _sandbox_word(agent)
    if how is None:
        console.print(f"  [{C['gold']}]⚠[/] {escape(agent)} only runs inside its sandbox, which can't run here "
                      "(handoff doctor says why). It stays approved until it can.", soft_wrap=True)
        how = "in its sandbox"
    _hint(f"It runs once, {how}, in .handoff/worktrees/{task.ref} on branch handoff/{task.ref}, then hands "
          f"the task to {_who(return_to)}. Nothing is pushed.")
    console.print(f"  [{C['dim']}]Start it with:[/] handoff run {task.ref}  [{C['dim']}](or keep a worker "
                  f"running: handoff worker --agent {escape(agent)})[/]", soft_wrap=True)
    return 0


KIND_VERBS = {"edit": "change files", "answer": "answer", "review": "review your changes", "image": "make pictures"}


def cmd_worker(argv: list[str]) -> int:
    from handoff.worker import DEFAULT_TIMEOUT_MIN, Worker, WorkerUnavailable

    parser = _parser("worker", "Run an agent headlessly on the tasks you approved (handoff approve), one at a "
                               "time, each in its own git worktree. The agent can read and edit files there and run "
                               "commands, inside a sandbox with no network (Linux, with bubblewrap); where the "
                               "sandbox can't run, Claude only reads and edits and Codex doesn't run. In the "
                               "sandbox, nothing of yours outside the worktree is visible (system folders such as "
                               "/usr and /etc are, read-only). Nothing is pushed. Answers, reviews and pictures you "
                               "approved for it go through Ixel MAT.",
                     needs={"agent": "Say which agent runs the tasks, like: handoff worker --agent claude"})
    parser.add_argument("--agent", required=True, metavar="AGENT", help="the agent to run: claude or codex")
    parser.add_argument("--once", action="store_true", help="run what's approved now, then stop")
    parser.add_argument("--share", action="append", default=[], metavar="FOLDER",
                        help="show the agent a folder read-only, such as tools installed in your home (~/.cargo, "
                             "~/.nvm); repeat for more. Folders with credentials are refused, and credential "
                             "files inside are shown empty")
    parser.add_argument("--poll", type=float, default=5.0, metavar="SECONDS",
                        help="how often to look for work (default 5)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_MIN, metavar="MINUTES",
                        help=f"stop a run after this long (default {DEFAULT_TIMEOUT_MIN})")
    args = parser.parse_args(argv)
    if args.poll <= 0 or args.timeout <= 0:
        parser.error("--poll and --timeout must be more than 0")
    root = _root(args)
    board = _board(args)
    try:
        worker = Worker(board, root, args.agent, timeout_sec=args.timeout * 60, share=args.share)
        worker.check()
    except WorkerUnavailable as exc:
        raise CommandError(str(exc)) from exc

    def starting(task, kind: str) -> None:
        what = (f"running {escape(args.agent)} in .handoff/worktrees/{task.ref}" if kind == "edit" else
                f"{escape(args.agent)} {_kind_text(kind)}, through Ixel MAT")
        console.print(f"  [{C['blue']}]▶[/] {task.ref} [{C['dim']}]{what}…[/]", soft_wrap=True)

    def done(result) -> None:  # each one as it comes: the next may take half an hour
        if result.waiting and not args.once:  # said once: it's tried again when you sign in
            console.print(f"  [{C['gold']}]⚠[/] {result.task.ref} is waiting: {escape(result.waiting)} It's still "
                          "approved, and this worker starts it once you sign in.", soft_wrap=True)
        elif result.ok:
            what = f"{result.task.ref} {_ran(result.message)}"
            what += f"; the work is on {result.branch}" if result.branch else ""
            console.print(f"  [{C['green']}]✓[/] {escape(what)}", soft_wrap=True)
        else:
            console.print(f"  [{C['red']}]✗[/] {result.task.ref}: {escape(result.message)}", soft_wrap=True)

    console.print(f"\n  [{C['gold']}]Handoff worker[/] [{C['dim']}]· {escape(args.agent)} · "
                  f"{escape(_shown_path(root))}[/]", soft_wrap=True)
    if worker.fallback_reason:
        console.print(f"  [{C['gold']}]⚠[/] {escape(args.agent)} runs edit-only here (no commands): "
                      f"[{C['dim']}]{escape(worker.fallback_reason)}[/]", soft_wrap=True)
    elif worker.shared:
        hidden = f"; {len(worker.hidden)} credential files and folders in them are shown empty" if worker.hidden else ""
        console.print(f"  [{C['dim']}]Shared with the agent, read-only: "
                      f"{escape(', '.join(_shown_path(p) for p in worker.shared))}{hidden}[/]", soft_wrap=True)
    unsealed = board.unsealed_approvals(args.agent)
    if unsealed:
        refs = ", ".join(t.ref for t in unsealed)
        console.print(f"  [{C['gold']}]⚠[/] {refs} {'was' if len(unsealed) == 1 else 'were'} approved on another "
                      f"computer or by an older Handoff, so the worker won't run "
                      f"{'it' if len(unsealed) == 1 else 'them'}. To run one, approve it again here: "
                      f"handoff approve {unsealed[0].ref} {escape(args.agent)}", soft_wrap=True)
    if not args.once:
        _hint(f"Waiting for tasks you approve (handoff approve T-12 {args.agent}). Ctrl+C to stop.")
    results = []
    try:
        while True:
            results += worker.run_pending(on_start=starting, on_done=done)
            if args.once:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        _hint("Stopped.")
    if args.once and not results:
        _hint(f"Nothing is approved for the {args.agent} worker.")
    return 1 if args.once and any(not r.ok for r in results) else 0


# ── dispatch, run, agents ────────────────────────────────────────────────────

def _depth_guard() -> None:
    from handoff.ixel import DEPTH_VAR, depth
    if depth() > 0:
        raise CommandError(f"This was started by Handoff ({DEPTH_VAR} is set), and an agent Handoff started can't "
                           "start more agents.")


def _kind_text(kind: str) -> str:
    return {"edit": "changes files", "answer": "answers", "review": "reviews", "image": "makes pictures"}.get(kind, kind)


def _result_line(result, agent: str) -> None:
    if result is None:
        return
    if result.ok:
        where = f"; the work is on {result.branch}" if result.branch else ""
        console.print(f"  [{C['green']}]✓[/] {result.task.ref} {escape(agent)}: "
                      f"{escape(_ran(result.message))}{escape(where)}", soft_wrap=True)
    else:
        console.print(f"  [{C['red']}]✗[/] {result.task.ref} {escape(agent)}: {escape(result.message)}", soft_wrap=True)


def _run_and_report(board, root: Path, pairs, roster=None, as_json: bool = False) -> list[dict]:
    """Run the approved tasks side by side; each result as JSON-ready data, with who ran it."""
    from handoff import crew

    agents = {task.id: task.assignee or "" for task, _ in pairs}  # once it's handed back, the assignee is you

    def starting(task, kind: str) -> None:
        if not as_json:
            console.print(f"  [{C['blue']}]▶[/] {task.ref} [{C['dim']}]{escape(task.assignee or '')} "
                          f"{_kind_text(kind)}…[/]", soft_wrap=True)

    def done(result) -> None:
        if not as_json:
            _result_line(result, agents.get(result.task.id, ""))

    results = crew.run_all(board, root, pairs, roster=roster, on_start=starting, on_done=done)
    if not as_json:
        refs = " ".join(r.task.ref for r in results if r is not None)
        console.print(f"\n  [{C['dim']}]See each result with:[/] handoff show T-N  [{C['dim']}]({refs})[/]",
                      soft_wrap=True)
    return [{"task": r.task.ref, "agent": agents.get(r.task.id, ""), "ok": r.ok, "message": r.message,
             **({"branch": r.branch} if r.branch else {})} for r in results if r is not None] + \
        [{"task": task.ref, "agent": agents[task.id], "ok": False, "message": "It was still running when Handoff "
          "stopped."} for (task, _), r in zip(pairs, results) if r is None]


def cmd_dispatch(argv: list[str]) -> int:
    import json

    from handoff import crew
    from handoff.board import HUMAN
    from handoff.sanitize import clean_line

    parser = _parser("dispatch", "Split one request across your agents and run the parts side by side. Start each "
                                 "part with who does it: \"codex review my changes, grok make pictures of my app, "
                                 "claude finish the next step, and gemini make me a list of projects to check "
                                 "out\". You see the plan first; nothing runs until you say yes.")
    parser.add_argument("request", nargs="*", help='what to do; "-" reads it from stdin')
    parser.add_argument("--yes", action="store_true", help="don't ask; add, approve and run the plan as shown")
    parser.add_argument("--plan", action="store_true",
                        help="only show the plan; add nothing (it still checks which agents can run here, which "
                             "asks no model)")
    parser.add_argument("--no-run", action="store_true",
                        help="add and approve the tasks, but don't run them (handoff run does, later)")
    parser.add_argument("--return-to", default=HUMAN, metavar="NAME",
                        help="who gets each task back when its run ends (default: you)")
    parser.add_argument("--json", action="store_true", help="print the plan (and the results) as JSON")
    args = parser.parse_args(argv)
    request = " ".join(args.request)
    if request.strip() == "-" or (not request.strip() and not sys.stdin.isatty()):
        # UTF-8 whatever this computer's code page (Ixel sends it that way); -I ignores PYTHONIOENCODING
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        request = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    if not request.strip():
        parser.error("say what to do, starting each part with who does it")
    _depth_guard()
    from handoff.board import check_name
    return_to = check_name(_person(args.return_to), "return_to")  # before anything is planned or added
    root = _root(args)
    only_plan = args.plan or (args.json and not args.yes)
    board = None if only_plan else _board(args)  # (showing a plan makes no board)
    roster = crew.build_roster(board, root)
    context, steps = crew.plan(request, roster, root, return_to)
    runnable = [s for s in steps if s.ok]
    plan = [{"agent": s.agent, "label": s.label, "kind": s.kind, "text": s.text, "title": s.title(),
             "detail": s.detail, "note": s.note, "problem": s.problem} for s in steps]
    if args.json and only_plan:
        print(json.dumps({"context": context, "steps": plan, "problems": roster.problems}, indent=2))
        return 0 if runnable else 1
    if not args.json:
        if context:
            console.print(f"\n  [{C['dim']}]For all of them:[/] {escape(clean_line(context))}", soft_wrap=True)
        console.print()
        for n, step in enumerate(steps, 1):
            if step.ok:
                detail = f" · {step.detail}" if step.detail else ""
                console.print(f"  [{C['gold']}]{n}.[/] [{C['blue']}]{escape(step.label)}[/] "
                              f"[{C['dim']}]{_kind_text(step.kind)}{escape(detail)}[/]", soft_wrap=True)
                console.print(Text(f"     {clean_line(step.title())}"), soft_wrap=True)
                if step.note:
                    console.print(f"     [{C['gold']}]⚠[/] [{C['dim']}]{escape(step.note)}[/]", soft_wrap=True)
            else:
                console.print(f"  [{C['red']}]✗ {n}.[/] [{C['blue']}]{escape(step.label)}[/] "
                              f"[{C['dim']}]{escape(step.text[:80])}[/]", soft_wrap=True)
                console.print(f"     [{C['red']}]Won't run:[/] {escape(step.problem)}", soft_wrap=True)
        for problem in roster.problems:
            console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{escape(problem)}[/]", soft_wrap=True)
    if not steps:
        raise CommandError("Start each part with who does it, like: handoff dispatch \"codex review my changes, "
                           "gemini make me a list of projects to check out\". To see who's here: handoff agents")
    if not runnable:
        raise CommandError("None of these can run here; the lines above say why.")
    if args.plan:
        return 0
    edits = [s for s in runnable if s.kind == "edit"]
    if not args.yes:
        if not _interactive():
            raise CommandError("Add --yes to run the plan above without being asked.")
        if edits:
            _hint("Edits happen in their own worktree and branch (handoff/T-N); nothing is pushed. Answers, reviews "
                  "and pictures change nothing in the project.")
        verb = "Add and approve" if args.no_run else "Run"
        answer = console.input(f"  {verb} {'these' if len(runnable) > 1 else 'this'} {len(runnable)} "
                               f"task{'s' if len(runnable) > 1 else ''}? [Y/n] ")
        if answer.strip().lower().startswith("n"):
            _hint("Nothing was added.")
            return 1
    pairs = crew.add_and_approve(board, request, runnable, return_to)
    if args.no_run:
        refs = " ".join(task.ref for task, _ in pairs)
        if args.json:
            print(json.dumps({"context": context, "steps": plan, "tasks": [t.ref for t, _ in pairs]}, indent=2))
        else:
            _ok(f"Added and approved {refs}.")
            console.print(f"  [{C['dim']}]Run them with:[/] handoff run {refs}", soft_wrap=True)
        return 0
    if not args.json:
        console.print()
    results = _run_and_report(board, root, pairs, roster.ixel, as_json=args.json)
    if args.json:
        print(json.dumps({"context": context, "steps": plan, "results": results}, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


def _approval_words(board, task) -> str:
    """Who to approve a task for, and for what, as you'd type it: as it was last approved, else its assignee."""
    from handoff.board import HUMAN, run_kind, safe_name
    last = next((e for e in reversed(board.events(task.id)) if e.kind == "approved"), None)
    agent = safe_name(last.data.get("worker")) if last else None
    if agent:
        kind = run_kind(last)
        return agent + (f" --kind {kind}" if kind not in ("edit", "unknown") else "")
    return task.assignee if task.assignee not in (None, HUMAN) else "claude"


def cmd_run(argv: list[str]) -> int:
    import json

    parser = _parser("run", "Run approved tasks now, side by side: edits in their own worktree, answers, reviews "
                            "and pictures through Ixel MAT. With no tasks named, runs everything that's approved.")
    parser.add_argument("tasks", nargs="*", metavar="T-N")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    _depth_guard()
    root = _root(args)
    board = _board(args)
    ids = [_task_id(t) for t in args.tasks] or None
    pairs = board.pending_runs(None, ids)
    if ids:
        missing = sorted(set(ids) - {task.id for task, _ in pairs})
        if missing:
            from handoff.board import HUMAN, task_ref
            refs = ", ".join(task_ref(i) for i in missing)
            first = board.get(missing[0])
            approve = f"handoff approve {first.ref} {_approval_words(board, first)}"
            if first.assignee not in (None, HUMAN) and \
                    first.id in {t.id for t in board.unsealed_approvals(first.assignee)}:
                raise CommandError(f"{first.ref} was approved on another computer or by an older Handoff, so it "
                                   f"won't run here. To run it, approve it again here: {approve}")
            raise CommandError(f"{refs} {'isn' if len(missing) == 1 else 'aren'}'t approved to run (or changed since "
                               f"{'it was' if len(missing) == 1 else 'they were'} approved).\nApprove it first, "
                               f"like: {approve}")
    if not pairs:
        if args.json:
            print(json.dumps({"results": []}))
        else:
            _hint("Nothing is approved to run. Approve a task with: handoff approve T-N AGENT")
        return 0
    results = _run_and_report(board, root, pairs, as_json=args.json)
    if args.json:
        print(json.dumps({"results": results}, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


def cmd_agents(argv: list[str]) -> int:
    import json

    from handoff import crew

    parser = _parser("agents", "Who can take tasks here, and what kind: claude and codex change files (the worker); "
                               "any model you set up in Ixel MAT answers and reviews; Grok (xAI) and GPT (OpenAI) "
                               "make pictures.")
    parser.add_argument("--json", action="store_true", help="print it as JSON")
    args = parser.parse_args(argv)
    root = _root(args)
    roster = crew.build_roster(None, root)  # (only looks: no board needed)
    if args.json:
        print(json.dumps({"agents": [{"name": m.name, "label": m.label, "kinds": m.kinds, "notes": m.notes}
                                     for m in roster.members.values()], "problems": roster.problems}, indent=2))
        return 0
    print_banner()
    for member in roster.members.values():
        ready = [k for k in ("edit", "answer", "review", "image") if member.kinds.get(k) == ""]
        label = "" if member.label.lower() == member.name.lower() else f" [{C['dim']}]{escape(member.label)}[/]"
        console.print(f"  [{C['blue']}]{escape(member.name)}[/]{label}  "
                      + (", ".join(_kind_text(k) for k in ready) if ready else f"[{C['dim']}]nothing yet[/]"),
                      soft_wrap=True)
        for kind, note in member.notes.items():
            console.print(f"      [{C['gold']}]⚠[/] [{C['dim']}]{escape(note)}[/]", soft_wrap=True)
        for kind, why in member.kinds.items():
            if why:
                console.print(f"      [{C['dim']}]can't {KIND_VERBS.get(kind, kind)}: {escape(why)}[/]", soft_wrap=True)
    for problem in roster.problems:
        console.print(f"\n  [{C['gold']}]⚠[/] [{C['dim']}]{escape(problem)}[/]", soft_wrap=True)
    example = _dispatch_example(roster)
    if example:
        console.print(f"\n  [{C['dim']}]Give them work with:[/] handoff dispatch \"{escape(example)}\"\n",
                      soft_wrap=True)
    else:
        console.print(f"\n  [{C['dim']}]Nobody can take tasks here yet; handoff doctor says what each one "
                      "needs.[/]\n", soft_wrap=True)
    return 0


def _dispatch_example(roster) -> str:
    """A request that would run here, with the names that can take each part."""
    def ready(kind: str) -> list[str]:
        return [m.name for m in roster.members.values() if m.kinds.get(kind) == ""]
    parts = []
    reviewers, answerers, editors = ready("review"), ready("answer"), ready("edit")
    if reviewers:
        parts.append(f"{reviewers[0]} review my changes")
    others = [n for n in answerers if n not in reviewers[:1]] or answerers
    if others:
        parts.append(f"{others[0]} make me a list of …")
    if len(parts) < 2 and editors:
        parts.append(f"{editors[0]} fix the footer")
    return ", ".join(parts[:2])


# ── setup ────────────────────────────────────────────────────────────────────

def cmd_setup(argv: list[str]) -> int:
    import json

    from handoff import hosts
    from handoff.project import ProjectError, resolve_project

    parser = _parser("setup", "Connect your apps to Handoff: prints the MCP config for Claude Code, Codex, "
                              "Claude Desktop, Gemini CLI, OpenCode and Grok Build, and with --write adds it "
                              "for you.")
    parser.add_argument("--write", action="store_true", help="add Handoff to each app's config")
    parser.add_argument("--host", action="append", choices=hosts.HOSTS, metavar="HOST",
                        help=f"only this app: {_or(hosts.HOSTS)} (repeat for more; default: every app "
                             "that's installed)")
    parser.add_argument("--no-hook", action="store_true",
                        help="don't add the session hook that tells Claude Code which tasks wait for it")
    parser.add_argument("--hook", action="store_true",
                        help="only add that hook (for the plugin, which doesn't bring it)")
    parser.add_argument("--remove-hook", action="store_true", help="take that hook out of Claude Code's settings")
    parser.add_argument("--remove", action="store_true",
                        help="take out everything setup added (before you uninstall Handoff)")
    args = parser.parse_args(argv)
    if args.write or args.remove or args.hook or args.remove_hook:  # (as parsed: --wri is --write)
        _not_started_by_handoff()
    if args.hook and args.remove_hook:
        parser.error("--hook and --remove-hook don't go together")
    if args.remove and (args.write or args.hook or args.remove_hook or args.no_hook):
        parser.error("--remove goes on its own (with --host to choose apps)")
    if args.remove:
        return _setup_remove(args.host or list(hosts.HOSTS))
    if args.hook or args.remove_hook:
        try:
            result = hosts.write_claude_hook(add=args.hook)
        except (hosts.SetupError, OSError) as exc:
            console.print(f"  [{C['red']}]✗[/] {escape(str(exc))}", soft_wrap=True)
            return 1
        console.print(f"  [{C['green']}]✓[/] Session hook: {result.status} [{C['dim']}]({escape(result.detail)})[/]",
                      soft_wrap=True)
        return 0
    try:
        project = resolve_project(args.project)
    except ProjectError as exc:
        if args.project:
            raise
        project, why = None, str(exc)
    else:
        why = ""

    print_banner()
    if project is not None:
        console.print(f"  [{C['dim']}]Project:[/] {escape(_shown_path(project))}\n", soft_wrap=True)
    else:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{escape(why)}[/]\n", soft_wrap=True)

    if not args.write:
        console.print(hosts.snippets(project), markup=False, highlight=False, soft_wrap=True)
        hook = json.dumps({"hooks": {hosts.HOOK_EVENT: [hosts.hook_entry()]}}, indent=2)
        console.print(f"Claude Code's session hook (tells it which tasks wait for it, by number only): add to "
                      f"{hosts.claude_code_settings_path()}", markup=False, highlight=False, soft_wrap=True)
        console.print("\n".join(f"  {line}" for line in hook.splitlines()) + "\n", markup=False, highlight=False,
                      soft_wrap=True)
        console.print(f"  [{C['dim']}]Run[/] handoff setup --write [{C['dim']}]to add these for you, then "
                      f"restart the apps.[/]\n", soft_wrap=True)
        return 0

    wanted = args.host or list(hosts.HOSTS)
    failed, connected = False, 0
    for host in wanted:
        label = hosts.HOST_LABELS[host]
        if not args.host and not hosts.installed(host):
            _hint(f"– {label}: not installed, skipped")
            continue
        connected += 1
        try:
            result = hosts.write(host, project)
        except (hosts.SetupError, OSError, ValueError) as exc:
            failed = True
            console.print(f"  [{C['red']}]✗[/] {label}: {escape(str(exc))}", soft_wrap=True)
            continue
        mark = f"[{C['dim']}]–[/]" if result.status == "skipped" else f"[{C['green']}]✓[/]"
        console.print(f"  {mark} {label}: {result.status} [{C['dim']}]({escape(result.detail)})[/]", soft_wrap=True)
        if host == "claude-code" and not args.no_hook:
            try:
                hook = hosts.write_claude_hook()
            except (hosts.SetupError, OSError) as exc:
                failed = True
                console.print(f"  [{C['red']}]✗[/] {label} session hook: {escape(str(exc))}", soft_wrap=True)
                continue
            console.print(f"  [{C['green']}]✓[/] {label} session hook: {hook.status} "
                          f"[{C['dim']}]({escape(hook.detail)})[/]", soft_wrap=True)
    if not connected:
        console.print(f"\n  [{C['red']}]None of the apps is installed, so nothing was set up.[/] Handoff needs at "
                      f"least one of {_or([hosts.HOST_LABELS[h] for h in hosts.HOSTS])} (two of them to pass "
                      "work between). Install one, then run handoff setup --write again.\n", soft_wrap=True)
        return 1
    console.print(f"\n  [{C['dim']}]Then, in any of them:[/] \"Split this feature: hand the tests to Codex, "
                  f"and you do the endpoint.\" [{C['dim']}]In the other: \"Check your handoffs.\"[/]\n",
                  soft_wrap=True)
    return 1 if failed else 0


def _setup_remove(wanted: list[str]) -> int:
    """`handoff setup --remove`: Handoff's entry in each app's config, and the session hook. Safe to run twice."""
    import subprocess

    from handoff import hosts

    failed = False

    def show(label: str, result) -> None:
        mark = f"[{C['green']}]✓[/]" if result.status == "removed" else f"[{C['dim']}]–[/]"
        console.print(f"  {mark} {label}: {result.status} [{C['dim']}]({escape(result.detail)})[/]", soft_wrap=True)

    console.print()
    for host in wanted:
        label = hosts.HOST_LABELS[host]
        try:
            show(label, hosts.remove(host))
            if host == "claude-code":
                hook = hosts.write_claude_hook(add=False)
                show(f"{label} session hook",
                     hook if hook.status == "removed" else hosts.Result(hook.host, "not there", "nothing in "
                                                                        f"{hosts.claude_code_settings_path()}"))
        except (hosts.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            failed = True
            console.print(f"  [{C['red']}]✗[/] {label}: {escape(str(exc))}", soft_wrap=True)
            continue
        plugin = hosts.plugin_enabled(host)
        if plugin:
            console.print(f"  [{C['gold']}]⚠[/] The Handoff plugin ({escape(plugin)}) is still on in {label}. "
                          "Uninstall it there too.", soft_wrap=True)
    console.print()
    backup = hosts.claude_code_settings_path().with_name("settings.json.handoff-bak")
    if "claude-code" in wanted and backup.exists():
        _hint(f"A copy of Claude Code's settings from before setup changed them is {_shown_path(backup)}. "
              "Delete it when you don't need it.")
    _hint("Boards stay in each project's .handoff folder until you delete them.")
    console.print()
    return 1 if failed else 0


# ── doctor ───────────────────────────────────────────────────────────────────

def cmd_doctor(argv: list[str]) -> int:
    import os
    import platform
    import sqlite3

    from handoff import hosts
    from handoff.board import Board, BoardError, link_problem
    from handoff.project import ProjectError, board_path, gitignore_has_board, resolve_project

    parser = _parser("doctor", "Check the project, the board and which apps have Handoff set up.")
    args = parser.parse_args(argv)
    problems = 0

    def line(mark: str, what: str, detail: str = "") -> None:
        color = {"✓": C["green"], "⚠": C["gold"], "✗": C["red"], "–": C["dim"]}[mark]
        # soft_wrap: the terminal wraps long lines, so a command in `detail` pastes as one line
        console.print(f"  [{color}]{mark}[/] {escape(what)}" + (f"  [{C['dim']}]{escape(detail)}[/]" if detail else ""),
                      soft_wrap=True)

    print_banner()
    line("✓", f"Handoff {__version__}", f"Python {platform.python_version()} · {_shown_path(sys.executable)}")
    line("✓", f"SQLite {sqlite3.sqlite_version}")

    try:
        root = resolve_project(args.project)
    except ProjectError as exc:
        root = None
        problems += 1
        line("✗", "Project", str(exc))
    if root is not None:
        line("✓", "Project", _shown_path(root))
        path = board_path(root)
        linked = link_problem(root)
        if linked:  # before anything looks through it
            problems += 1
            line("✗", "Board", linked)
        elif not path.exists():
            line("–", "Board", f"none yet; the first handoff command or agent creates {_shown_path(path)}")
        else:
            private = ((path.parent, 0o700), (path, 0o600)) if sys.platform != "win32" else ()
            before = {item: item.stat().st_mode & 0o777 for item, _ in private}
            try:
                board = Board.open(root, create=False)  # also tightens loose permissions it owns
                counts = board.counts()
                kept = "forever" if board.keep_days is None else _plural(board.keep_days, "day")
                line("✓", "Board", f"{_shown_path(path)} · {_plural(sum(counts.values()), 'task')} · "
                                   f"done rule: {board.done_rule} · finished tasks kept {kept}")
                _say_expired(board)
                unreadable = board.unreadable()
                if unreadable:
                    line("⚠", f"{_plural(unreadable, 'task')} on the board can't be read",
                         "the file may be damaged or made by another tool; they're left out of every list")
            except BoardError as exc:
                problems += 1
                line("✗", "Board", str(exc))
            for item, want in private:
                mode = item.stat().st_mode & 0o777
                if mode & 0o077:
                    problems += 1
                    line("✗", f"{item.name} is readable by others", f"mode {mode:o}; run: chmod {want:o} {item}")
                elif before[item] & 0o077:
                    line("⚠", f"{item.name} was readable by others", f"mode {before[item]:o}; now {mode:o}")
        if gitignore_has_board(root) or (root / ".handoff" / ".gitignore").exists():
            line("✓", "The board stays out of git")
        else:
            line("⚠", "The board isn't in .gitignore", "add a .handoff/ line, or run any handoff command here")

    console.print()
    for host in hosts.HOSTS:
        label = hosts.HOST_LABELS[host]
        if not hosts.installed(host):
            line("–", label, "not installed")
            continue
        ok, detail = hosts.configured(host)
        if hosts.plugin_enabled(host) and hosts.in_config(host)[0]:
            remove = ("claude mcp remove --scope user handoff" if host == "claude-code"
                      else f"delete [mcp_servers.handoff] from {hosts.codex_config_path()}")
            line("⚠", f"{label} has Handoff twice", f"{detail}: that's two servers; keep the plugin, run: {remove}")
        elif ok:
            line("✓", f"{label} has Handoff", detail)
            if host == "claude-desktop" and root is not None and str(root) not in detail:
                line("⚠", "Claude Desktop points at another project", "run `handoff setup --write` here to switch")
        elif host == "grok" and detail.startswith(("not in ", "no ")) and hosts.in_config("claude-code")[0]:
            # Grok Build also loads the MCP servers in ~/.claude.json, behind its own of the same name (so only
            # when Grok has no handoff entry at all: one turned off or unreadable still has the name)
            line("⚠", "Grok Build uses Claude Code's Handoff", f"{detail}, so on the board it works as claude; "
                                                              "run: handoff setup --write")
        else:
            line("⚠", f"{label} doesn't have Handoff yet", f"{detail}; run: handoff setup --write")
        if host == "claude-code":
            hook = hosts.claude_hook_set()
            if hook is None:
                line("–", "Claude Code's session hook", "not set; `handoff setup --write` adds it")
            elif not hosts.runs_this_handoff(hook):
                line("⚠", "Claude Code's session hook runs another Handoff", "run: handoff setup --write")
            else:
                line("✓", "Claude Code's session hook", "it hears which tasks wait for it when a session starts")

    from handoff import ixel
    console.print()
    ixel_cmd = ixel.find_ixel()
    if ixel_cmd:
        line("✓", "Ixel", f"{_shown_path(ixel_cmd)}; reviews can ask the panel (handoff_review panel=true), and "
                          "dispatch can give answers, reviews and pictures to any model in it (handoff agents)")
    else:
        line("–", "Ixel", "not installed (optional: lets reviews ask a panel of other models, and dispatch give "
                          "work to models other than claude and codex)")
    if os.environ.get(ixel.DEPTH_VAR):
        line("⚠", "Running inside something Handoff started", f"{ixel.DEPTH_VAR} is set; no panels or workers "
                                                              "from here")

    from handoff.worker import AGENTS, Worker, WorkerUnavailable
    for agent in AGENTS:
        try:
            worker = Worker(None, Path("."), agent)  # type: ignore[arg-type]
            worker.check()
            if worker.fallback_reason:
                line("⚠", f"Worker ({agent})", f"ready, edit-only (no commands): {worker.fallback_reason}")
            else:
                line("✓", f"Worker ({agent})", f"ready, in its sandbox: handoff approve T-12 {agent}")
        except WorkerUnavailable as exc:
            line("–", f"Worker ({agent})", str(exc))
    console.print()
    return 1 if problems else 0


# ── mcp, version, help ───────────────────────────────────────────────────────

def cmd_mcp(argv: list[str]) -> int:
    """Run the MCP server on stdio. The host app starts this; stdout belongs to the protocol."""
    # A plain argparse parser: apps start this, and its errors stay as they've always been
    parser = argparse.ArgumentParser(prog="handoff mcp", description="Serve this project's board to an app over "
                                     "MCP (stdio). `handoff setup` writes the config.", formatter_class=_HelpFormatter,
                                     epilog="Examples:\n" + "\n".join(f"  {e}" for e in EXAMPLES["mcp"]))
    parser.add_argument("--project", metavar="PATH", help=PROJECT_HELP)
    parser.add_argument("--as", dest="me", required=True, metavar="NAME",
                        help="who this app is on the board: claude, codex, or another short name")
    args = parser.parse_args(argv)

    from handoff.board import HUMAN, BoardError, check_name
    try:
        me = check_name(args.me, "--as")
    except BoardError as exc:
        parser.error(str(exc))
    if me == HUMAN:
        parser.error("--as human is reserved for you, at the command line; give the app its own name "
                     "(claude, codex…)")

    from handoff.mcp_server import run_stdio
    run_stdio(me, args.project)
    return 0


def _aliases_line() -> str:
    names = [name for _, group in GROUPS for name in group]
    said = []
    for target in names:
        words = [alias for alias, to in ALIASES.items() if to == target]
        if words:
            said.append(f"{_or(words)} for {target}")
    return "Other names work too: " + "; ".join(said) + "."


def _command_help(word: str) -> int:
    name = ALIASES.get(word.lower(), word.lower())
    handler = HANDLERS.get(name)
    if handler is None:
        return _unknown_command(word)
    try:
        return handler(["--help"])
    except SystemExit as exc:  # argparse's --help exits 0 when it's printed
        return exc.code if isinstance(exc.code, int) else 0


def cmd_help(argv: list[str]) -> int:
    parser = _parser("help", "Show every command, or one command's options and examples.", project=False)
    parser.add_argument("command", nargs="?", help="a command, like show or approve")
    args = parser.parse_args(argv)
    if args.command:
        if args.command.lower() == "help":
            parser.print_help()
            return 0
        return _command_help(args.command)
    print_banner()
    usages = {usage.split()[1]: (usage, what) for usage, what in COMMANDS}
    for heading, names in GROUPS:
        console.print(f"  [{C['gold']}]{heading}[/]")
        for name in names:  # Text, not markup: "[--flags]" would be read as a Rich style tag
            usage, what = usages[name]
            console.print(Text("    " + usage, style=C["blue"]), soft_wrap=True)
            console.print(Text("        " + what, style=C["dim"]), soft_wrap=True)
        console.print()
    console.print(f"  [{C['gold']}]Examples[/]")
    for example in HELP_EXAMPLES:
        console.print(Text("    " + example), soft_wrap=True)
    console.print()
    for line in ("Task ids can be T-3, t3 or just 3, and handoff T-3 on its own shows that task.",
                 "handoff help COMMAND shows one command's options and examples.",
                 "Wherever you name someone, me means you: handoff assign T-3 me"):
        _hint(line)
    _prose(_aliases_line())
    _prose("Commands that use the board take --project PATH; by default it's the git repository you're in.")
    console.print()
    return 0


def cmd_update(argv: list[str]) -> int:
    from handoff import update

    parser = _parser("update", update.DESCRIPTION, project=False)

    def allowed(args) -> None:  # --check only looks
        if not args.check:
            _not_started_by_handoff()
    try:
        return update.main(argv, say=lambda line: console.print(f"  {escape(line)}", soft_wrap=True), parser=parser,
                           allowed=allowed)
    except update.UpdateError as exc:
        raise CommandError(str(exc)) from None


def cmd_version(argv: list[str]) -> int:
    _parser("version", "Show which version of Handoff this is.", project=False).parse_args(argv)
    console.print(f"  [{C['gold']}]Handoff[/] [{C['moon']}]v{__version__}[/]")
    return 0


HANDLERS = {
    "board": cmd_board,
    "show": cmd_show,
    "add": cmd_add,
    "assign": cmd_assign,
    "note": cmd_note,
    "done": cmd_done,
    "reopen": cmd_reopen,
    "block": cmd_block,
    "review": cmd_review,
    "status": cmd_status,
    "cancel": cmd_cancel,
    "delete": cmd_delete,
    "clean": cmd_clean,
    "done-rule": cmd_done_rule,
    "keep": cmd_keep,
    "approve": cmd_approve,
    "dispatch": cmd_dispatch,
    "run": cmd_run,
    "agents": cmd_agents,
    "worker": cmd_worker,
    "setup": cmd_setup,
    "doctor": cmd_doctor,
    "mcp": cmd_mcp,
    "update": cmd_update,
    "version": cmd_version,
    "help": cmd_help,
}


# What you do on the board as yourself. An agent with a shell runs as you, so one that Handoff started
# (HANDOFF_DEPTH is set) is refused these; the rest (show, board, agents, doctor…) only look.
# What only the person does. Anything Handoff started is refused these (setup and update decide on their
# parsed options: _not_started_by_handoff). A guard against an agent doing it by accident, not a wall: a
# shell can unset the variable, which is why SECURITY.md says to deny these in the agent's permissions too.
PERSON_WRITES = {"add", "assign", "note", "done", "reopen", "block", "review", "status", "cancel", "delete",
                 "clean", "done-rule", "keep", "approve"}


def _not_started_by_handoff() -> None:
    from handoff.ixel import DEPTH_VAR, depth
    if depth() > 0:
        raise CommandError(f"This was started by Handoff ({DEPTH_VAR} is set), so it can't change the board (or "
                           "Handoff's setup) as you. Only you can, from your own terminal.")


def _unknown_command(word: str, rest: list[str] = ()) -> int:
    close = difflib.get_close_matches(word.lower(), [*HANDLERS, *ALIASES], n=1, cutoff=0.6)
    hint = f" Did you mean: handoff {close[0]}" if close else ""
    err_console.print(f"  [{C['red']}]Unknown command: {escape(word)}.{escape(hint)}[/]", soft_wrap=True)
    words = " ".join([word, *rest]).strip()
    if not close and " " in words and not any(w.startswith("-") for w in rest):  # a request, typed as one
        err_console.print(f"  [{C['dim']}]To add it as a task:[/] handoff add {escape(_quoted(words))}",
                          soft_wrap=True)
    err_console.print(f"  [{C['dim']}]handoff help lists every command.[/]")
    return 2


def _quoted(text: str) -> str:
    """Text as one argument you can paste into your shell."""
    if not any(c in text for c in '"$`\\!'):
        return f'"{text}"'
    if sys.platform == "win32":
        import subprocess
        return subprocess.list2cmdline([text])
    import shlex
    return shlex.quote(text)


def _tolerate_unencodable_output() -> None:
    """A ✓ piped to a cp1252 file on Windows should become '?', not a crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    from handoff.board import BoardError, NotFound
    from handoff.hosts import SetupError
    from handoff.project import ProjectError

    global _opened
    _opened = None
    _tolerate_unencodable_output()
    args = sys.argv[1:] if argv is None else list(argv)
    first = args[0] if args else ""
    if first == "--project" or first.startswith("--project="):
        pair = args[:1 if "=" in first else 2]
        if len(args) > len(pair):  # `handoff --project PATH board` is `handoff board --project PATH`
            args = [args[len(pair)], *pair, *args[len(pair) + 1:]]
            first = args[0]
    if first == "api":  # the Ixel window's JSON door (api.py); not one for people, so not in help
        from handoff import api
        return api.main(args[1:])
    if first == "hook":  # what an app runs when a session starts (hook.py); `handoff setup` sets it up
        from handoff import hook
        return hook.main(args[1:])
    name = ""
    if not args or first == "--project" or first.startswith("--project="):
        handler, rest = cmd_home, args
    elif first.split("=", 1)[0] in ("--all", "--status", "--assignee", "--watch"):
        handler, rest = cmd_board, args  # `handoff --all` is `handoff board --all`
    else:
        name = FLAGS.get(first) or ALIASES.get(first.lower(), first.lower())
        handler, rest = HANDLERS.get(name), args[1:]
        if handler is None and _is_task_id(first):
            handler, rest = cmd_show, args  # `handoff T-3` is `handoff show T-3`
        if handler is None:
            return _unknown_command(first, args[1:])
    try:
        if name in PERSON_WRITES:
            _not_started_by_handoff()
        return handler(rest)
    except (BoardError, ProjectError, SetupError, CommandError) as exc:
        _say_problem(_for_you(str(exc)))
        if isinstance(exc, NotFound) and str(exc).startswith("There's no task") and _opened is not None:
            try:
                err_console.print(f"  [{C['dim']}]{escape(_which_tasks(_opened))}[/]", soft_wrap=True)
            except BoardError:
                pass
        return 1
    except (KeyboardInterrupt, EOFError):  # Ctrl+C, or no answer to a question
        if handler is cmd_mcp:
            raise
        err_console.print(f"\n  [{C['dim']}]Stopped.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
