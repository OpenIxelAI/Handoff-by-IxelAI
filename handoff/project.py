"""Where a project's board lives: `<project root>/.handoff/board.db`.

The project root is the nearest folder above the working directory that has
a `.git`, or above the folder given with `--project` (that folder itself when
it isn't in a repository). A linked git worktree shares its main checkout's
board, so one repository has one board.
"""
from __future__ import annotations

from pathlib import Path

BOARD_DIR = ".handoff"
BOARD_FILE = "board.db"
GITIGNORE_LINE = ".handoff/"


class ProjectError(Exception):
    """No usable project; the message says what to do."""


def _main_checkout(git_file: Path) -> Path | None:
    """The main checkout of a linked worktree, read from its `.git` file (git isn't run)."""
    try:
        first = git_file.read_text(encoding="utf-8", errors="replace")[:4096].splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = Path(first[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = git_file.parent / gitdir
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None  # a submodule, not a worktree: it's a project of its own
    try:
        common_dir = (gitdir / common).resolve()
        return common_dir.parent if common_dir.name == ".git" and common_dir.is_dir() else None
    except OSError:
        return None


def find_project_root(start: Path) -> Path | None:
    try:
        current = start.resolve()
        for folder in (current, *current.parents):
            git = folder / ".git"
            if git.is_dir():
                if (git / "HEAD").is_file():  # a repository, not a stray empty folder (sandboxes leave those)
                    return folder
                continue
            if git.is_file():
                return _main_checkout(git) or folder
    except OSError:  # a path too long for the system, say
        return None
    return None


def resolve_project(project: str | Path | None = None, cwd: Path | None = None) -> Path:
    """The project root from `--project`, else from the working directory. Never a guess."""
    if project:
        path = Path(project).expanduser()
        try:
            found = path.is_dir()
        except OSError:  # a path too long for the system, say
            found = False
        if not found:
            raise ProjectError(f"--project {project}: no such folder.")
        # a subfolder or a linked worktree of a repository is that repository's board, as it is without --project
        return find_project_root(path) or path.resolve()
    here = cwd or Path.cwd()
    root = find_project_root(here)
    if root is None:
        raise ProjectError(f"No project found: {here} isn't inside a git repository. "
                           "Run Handoff from your project's folder, or pass --project PATH.")
    return root


def board_path(root: Path) -> Path:
    return root / BOARD_DIR / BOARD_FILE


def gitignore_has_board(root: Path) -> bool:
    """Does the project's .gitignore already keep `.handoff/` out of git?"""
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(line.strip() in (".handoff", ".handoff/", "/.handoff", "/.handoff/") for line in lines)


def add_board_to_gitignore(root: Path) -> None:
    """Append `.handoff/` to the project's .gitignore (only when the person said yes)."""
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        existing = ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{prefix}# Handoff's task board\n{GITIGNORE_LINE}\n")
