"""The worker's git worktrees: one per task, on its own branch, never pushed.

This is the one place Handoff runs git, on the person's own repository. Git
always gets its directories spelled out (`--git-dir` from the main repository's
own list of worktrees, and `--work-tree`), so nothing the agent writes in the
worktree, such as a rewritten `.git` file, can point git somewhere else. Hooks
are switched off, commit messages go in on stdin, and nothing is fetched or
pushed.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from handoff.board import Changed, task_of_name
from handoff.proc import find_on_path
from handoff.sanitize import clean_line, find_secret, find_token

WORKTREES = Path(".handoff") / "worktrees"
_no_hooks: Path | None = None
# Dispatched runs work side by side, each in its own worktree; making a worktree and committing change
# the repository's shared records, so they take turns
_REPO_LOCK = threading.RLock()


class GitError(Exception):
    """Git couldn't do what the worker needs; the message says what to do."""


class MergeConflict(GitError):
    """A merge stopped on conflicts; it was aborted, so nothing changed. `files`: the conflicting ones."""

    def __init__(self, message: str, files: list[str]):
        super().__init__(message)
        self.files = files


class AlreadyMerged(GitError):
    """The branch has nothing the current branch lacks."""


@dataclass
class Worktree:
    path: Path
    branch: str
    git_dir: Path  # this worktree's own directory inside the main repository's .git
    base: str      # the commit the branch started from


def no_hooks() -> Path:
    """An empty folder for core.hooksPath, made by this process outside the project: one inside it (it was
    .handoff/no-hooks) could be committed to the repository with hooks in it."""
    global _no_hooks
    if _no_hooks is None or not _no_hooks.is_dir():
        _no_hooks = Path(tempfile.mkdtemp(prefix="handoff-no-hooks-"))
        atexit.register(shutil.rmtree, _no_hooks, True)
    return _no_hooks


def _git(root: Path, args: list[str], *, cwd: Path | None = None, worktree: Worktree | None = None,
         stdin: str | None = None, check: bool = True, errors: str = "replace") -> "subprocess.CompletedProcess[str]":
    hooks = no_hooks()
    # From PATH only: Windows would run a git.exe in the project folder first
    git = find_on_path("git")
    if git is None:
        raise GitError("git isn't installed (no `git` command on PATH); the worker needs it.")
    argv = [git]
    if worktree is not None:
        argv += [f"--git-dir={worktree.git_dir}", f"--work-tree={worktree.path}"]
    # The repository's config can name programs for git to run. These never run for Handoff's own git calls:
    # hooks, an fsmonitor, a signing program (commit.gpgSign with gpg.program) and, below, an external diff.
    # Filters, such as Git LFS's, still run when files are staged, as they would for you.
    argv += ["-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false", *args]
    # (no replace refs: a commit approved by its id is that commit, whatever refs/replace says it is)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    try:
        proc = subprocess.run(argv, cwd=cwd or (worktree.path if worktree else root), input=stdin,
                              capture_output=True, text=True, encoding="utf-8", errors=errors, env=env,
                              timeout=120)
    except FileNotFoundError as exc:
        raise GitError("git isn't installed (no `git` command on PATH); the worker needs it.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} took too long.") from exc
    if check and proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise GitError(f"git {args[0]} failed" + (f": {clean_line(lines[-1])[:300]}" if lines else "."))
    return proc


def common_dir(root: Path) -> Path:
    out = _git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
    return Path(out)


def head_commit(root: Path) -> str:
    proc = _git(root, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], check=False)
    if proc.returncode != 0:
        raise GitError("The project has no commits yet. The worker starts from your latest commit, so commit "
                       "something first.")
    return proc.stdout.strip()


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _registered_git_dir(root: Path, path: Path) -> Path | None:
    """The main repository's record of the worktree at `path` (read from .git/worktrees, never from the worktree)."""
    worktrees = common_dir(root) / "worktrees"
    if not worktrees.is_dir():
        return None
    for entry in worktrees.iterdir():
        try:
            recorded = Path((entry / "gitdir").read_text(encoding="utf-8").strip())
        except OSError:
            continue
        if _same(recorded, path / ".git"):
            return entry
    return None


def _branch_exists(root: Path, branch: str) -> bool:
    return _git(root, ["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], check=False).returncode == 0


def task_branches(root: Path) -> set[str]:
    """The tasks (T-N) that have a worker branch, handoff/T-N, in this repository. Only looks; none if git can't
    read it."""
    prefix = "refs/heads/handoff/"
    out = _git(root, ["for-each-ref", "--format=%(refname)", prefix], check=False).stdout
    return {line[len(prefix):] for line in out.splitlines() if line.startswith(prefix)}


def work_left(root: Path, gone: Callable[[str], bool]) -> list[tuple[str, bool, bool]]:
    """What the worker made for tasks that are gone (`gone` is given each T-N), oldest task first: the task's T-N,
    whether its worktree (.handoff/worktrees/T-N) is still there, and whether its branch (handoff/T-N) is.
    Removing a task leaves both, since they hold an agent's work. Only looks."""
    try:
        folders = {p.name for p in (root / WORKTREES).iterdir() if p.is_dir()}
    except OSError:
        folders = set()
    try:
        branches = task_branches(root)
    except GitError:
        branches = set()
    found = {}
    for ref in folders | branches:
        task_id = task_of_name(ref)
        if task_id is not None and gone(ref):
            found[task_id] = (ref, ref in folders, ref in branches)
    return [found[task_id] for task_id in sorted(found)]


def ensure_worktree(root: Path, task_id: int, base: str | None = None, start: str | None = None) -> Worktree:
    """The task's worktree at .handoff/worktrees/T-N on branch handoff/T-N, made the first time from HEAD,
    or from `start` (a full commit id, such as a pull request's head fetched into the project)."""
    with _REPO_LOCK:
        return _ensure_worktree(root, task_id, base, start)


def _ensure_worktree(root: Path, task_id: int, base: str | None, start: str | None = None) -> Worktree:
    path = root / WORKTREES / f"T-{task_id}"
    branch = f"handoff/T-{task_id}"
    if start:
        try:
            _git(root, ["cat-file", "-e", f"{start}^{{commit}}"])
        except GitError:
            raise GitError(f"The commit it was approved to start from ({start[:12]}) isn't in this repository "
                           "any more. Fetch it again, then approve the task again.") from None
        # A branch left by an earlier run (the board was made again, so its ids started over) must already hold
        # that commit, or the agent would work on something else while told it starts from there
        if _branch_exists(root, branch) and _git(root, ["merge-base", "--is-ancestor", start, f"refs/heads/{branch}"],
                                                 check=False).returncode != 0:
            raise GitError(f"{branch} is left from an earlier run and doesn't start from the commit this was approved "
                           f"for ({start[:12]}). Move it out of the way (git worktree remove {WORKTREES}/T-{task_id}, "
                           f"then git branch -m {branch} {branch}-old) and run again.")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if _branch_exists(root, branch):  # an earlier run's branch, its worktree since removed
            _git(root, ["worktree", "add", str(path), branch])
            base = base or start or _git(root, ["merge-base", "HEAD", branch]).stdout.strip()
        elif start:
            base = start
            _git(root, ["worktree", "add", "-b", branch, str(path), base])
        else:
            base = head_commit(root)
            _git(root, ["worktree", "add", "-b", branch, str(path), base])
    git_dir = _registered_git_dir(root, path)
    if git_dir is None:
        raise GitError(f"{path} exists but isn't a worktree of this repository. Move it away and run again.")
    worktree = Worktree(path, branch, git_dir, base or start or "")
    if not worktree.base:
        worktree.base = _git(root, ["merge-base", "HEAD", branch]).stdout.strip()
    return worktree


_MOUNT_POINT = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT: a junction


def _is_junction(path: Path) -> bool:
    check = getattr(path, "is_junction", None)  # Python 3.12+
    if check is not None:
        return bool(check())
    try:  # older Pythons: only Windows has a reparse tag to look at
        return getattr(os.lstat(path), "st_reparse_tag", 0) == _MOUNT_POINT
    except OSError:
        return False


def is_link(path: Path) -> bool:
    """A symbolic link, or a Windows junction."""
    return path.is_symlink() or _is_junction(path)


def links_leaving(path: Path) -> list[str]:
    """Symbolic links (and Windows junctions) in the worktree that point outside it."""
    real = path.resolve()
    found: list[str] = []
    for folder, dirs, files in os.walk(path):
        for name in dirs + files:
            item = Path(folder) / name
            if name == ".git" and Path(folder) == path:
                continue
            if item.is_symlink() or _is_junction(item):
                target = item.resolve()
                if target != real and real not in target.parents:
                    found.append(str(item.relative_to(path)))
    return sorted(found)


MAX_SCAN_BYTES = 2 * 1024 * 1024
# Almost every signature find_secret looks for needs fewer than 80 characters (including the private-key
# header); a JSON Web Token is longer, but rarely more than a few KB. Keep enough context to find one split
# across reads, along with the character before it for token-boundary checks.
SCAN_OVERLAP_BYTES = 8 * 1024


def stage_all(root: Path, worktree: Worktree) -> list[str]:
    """Stage everything the agent changed. Returns the added, modified or retyped paths (a link that's now a file,
    say), each naming its file even where the name isn't UTF-8."""
    _git(root, ["add", "-A"], worktree=worktree)
    out = _git(root, ["diff", "--no-ext-diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z"],
               worktree=worktree, errors="surrogateescape").stdout
    return [name for name in out.split("\0") if name]


def _shown(name: str) -> str:
    """A path as people see it (a name that isn't UTF-8 shows � where it isn't)."""
    return clean_line(name.encode("utf-8", "surrogateescape").decode("utf-8", "replace"))


def unstage_all(root: Path, worktree: Worktree) -> None:
    _git(root, ["reset", "-q"], worktree=worktree)


# A file the worktree's last commit had is compared with that version, line by line, when it's at most this big;
# a bigger one is checked whole
MAX_COMPARE_BYTES = 8 * MAX_SCAN_BYTES


def _known_lines(root: Path, worktree: Worktree, name: str) -> set[str] | None:
    """The lines a file had at the worktree's last commit, or None: it's new, or too big to compare."""
    spec = f"HEAD:{name}"
    size = _git(root, ["cat-file", "-s", spec], worktree=worktree, check=False).stdout.strip()
    if not size.isdigit() or int(size) > MAX_COMPARE_BYTES:
        return None
    blob = _git(root, ["cat-file", "blob", spec], worktree=worktree, check=False)
    return set(blob.stdout.splitlines()) if blob.returncode == 0 else None


def _chunks(file) -> Iterator[bytes]:
    while chunk := file.read(MAX_SCAN_BYTES):
        yield chunk


# A private key is found by its header line, which an old key in the file can share with a new one: a key's lines,
# from its header to its end, are checked together whenever any of them is new
_KEY_ENDS = ("-----END", "Private-MAC:")
MAX_KEY_BLOCK_BYTES = 1024 * 1024


def _is_key_header(text: str) -> bool:
    return ("PRIVATE KEY" in text or "PuTTY-User-Key-File-" in text) and find_secret(text, tokens=False) is not None


def _added(file, known: set[str]) -> Iterator[bytes]:
    """The lines of a file that its last commit didn't have anywhere, in chunks of about MAX_SCAN_BYTES. Lines
    are compared as text, as git gives the old ones; a line too long to read at once is always checked, whole, and
    so is a private key with any line that's new."""
    piece, block = bytearray(), bytearray()
    long = block_new = False
    while line := file.readline(MAX_SCAN_BYTES):
        whole = line.endswith(b"\n") or len(line) < MAX_SCAN_BYTES  # (else the rest of it comes next)
        text = line.decode("utf-8", "replace")
        new = long or not whole or any(part not in known for part in text.splitlines())
        long = not whole
        if not block and _is_key_header(text):
            block, block_new = bytearray(line), new
        elif block:
            block += line
            block_new = block_new or new
        elif new:
            piece += line
        if block and (any(end in text for end in _KEY_ENDS) or len(block) >= MAX_KEY_BLOCK_BYTES):
            if block_new:
                piece += block
            block, block_new = bytearray(), False
        if len(piece) >= MAX_SCAN_BYTES:
            yield bytes(piece)
            piece.clear()
    if block_new:
        piece += block
    if piece:
        yield bytes(piece)


def secrets_in(root: Path, worktree: Worktree, paths: list[str],
               tokens: list[str] | None = None) -> list[tuple[str, str]]:
    """Staged files that look like they add a secret: (path, what it looks like). A file the worktree's last
    commit already had is checked only for the lines it adds, so a secret that was already committed there (such
    as a test's fake key) doesn't hold up the changes around it; a new file is checked whole, and a link by what
    it points to (git commits that). Given a `tokens` list, a file whose only one is a JSON Web Token goes on it
    instead (such as a test's sample token)."""
    found = []
    for name in paths:
        path = worktree.path / name
        has_token = False
        try:
            if path.is_symlink():
                what = find_secret(os.readlink(path), tokens=tokens is None)
                if what:
                    found.append((_shown(name), what))
                elif tokens is not None and find_token(os.readlink(path)):
                    tokens.append(_shown(name))
                continue
            if not path.is_file():
                continue
            known = _known_lines(root, worktree, name)
            with path.open("rb") as file:
                overlap = b""
                for chunk in (_chunks(file) if known is None else _added(file, known)):
                    combined = overlap + chunk
                    text = combined.decode("utf-8", "replace")
                    what = find_secret(text, tokens=tokens is None)
                    if what:
                        found.append((_shown(name), what))
                        break
                    has_token = has_token or (tokens is not None and find_token(text) is not None)
                    overlap = combined[-SCAN_OVERLAP_BYTES:]
                else:
                    if has_token:
                        tokens.append(_shown(name))
        except OSError:
            continue
    return found


def commit_staged(root: Path, worktree: Worktree, message: str) -> str | None:
    """Commit what's staged on the worktree's branch. Returns the new commit, or None if nothing changed."""
    with _REPO_LOCK:
        return _commit_staged(root, worktree, message)


def _commit_staged(root: Path, worktree: Worktree, message: str) -> str | None:
    if _git(root, ["diff", "--no-ext-diff", "--cached", "--quiet"], worktree=worktree, check=False).returncode == 0:
        return None
    named = _git(root, ["config", "user.email"], check=False).stdout.strip()
    identity = [] if named else ["-c", "user.name=Handoff worker", "-c", "user.email=handoff@localhost"]
    _git(root, [*identity, "commit", "--no-verify", "-q", "-F", "-"], worktree=worktree, stdin=message)
    return _git(root, ["rev-parse", "HEAD"], worktree=worktree).stdout.strip()


def commit_all(root: Path, worktree: Worktree, message: str) -> str | None:
    """Commit everything in the worktree on its branch. Returns the new commit, or None if nothing changed."""
    stage_all(root, worktree)
    return commit_staged(root, worktree, message)


def changed_files(root: Path, worktree: Worktree) -> list[str]:
    out = _git(root, ["diff", "--no-ext-diff", "--name-only", "-z", worktree.base, "HEAD"], worktree=worktree).stdout
    return [clean_line(name) for name in out.split("\0") if name]


_SAFE_REF = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")


def _default_branch(root: Path) -> str | None:
    """The branch work is measured against: origin's default, else a local main or master."""
    remote = _git(root, ["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], check=False).stdout.strip()
    candidates = [remote] if remote else []
    candidates += ["main", "master"]
    for name in candidates:
        if name and _git(root, ["rev-parse", "--verify", "-q", f"{name}^{{commit}}"], check=False).returncode == 0:
            return name
    return None


def current_branch(root: Path) -> str | None:
    """The branch the project's own checkout has out; None when HEAD is detached. Only looks."""
    proc = _git(root, ["symbolic-ref", "-q", "--short", "HEAD"], cwd=root, check=False)
    if proc.returncode != 0:
        return None
    return clean_line(proc.stdout)[:200] or None


def merge_branch(root: Path, branch: str, message: str, expected_tip: str) -> tuple[str, str, str]:
    """Merge `branch` (a task's, such as handoff/T-3) into the branch the project's own checkout has out, as one
    merge commit (--no-ff), on the person's say-so. Returns that branch's name, the merge commit and its short id.
    Refuses, changing nothing, when the checkout isn't on a branch of its own or has uncommitted changes to
    tracked files, when `branch` is gone, isn't at `expected_tip` (the commit the worker's run recorded: Changed)
    or has nothing to add (AlreadyMerged). A conflict is aborted (MergeConflict). Never fetches or pushes."""
    if not branch or not set(branch) <= _SAFE_REF or branch.startswith("-") or ".." in branch:
        raise GitError(f"{clean_line(branch)[:80]!r} isn't a branch name Handoff merges.")
    with _REPO_LOCK:
        return _merge_branch(root, branch, message, expected_tip)


def _merge_branch(root: Path, branch: str, message: str, expected_tip: str) -> tuple[str, str, str]:
    ref = f"refs/heads/{branch}"
    if not _branch_exists(root, branch):
        raise GitError(f"{branch} isn't in this repository any more, so there's nothing to merge.")
    proc = _git(root, ["symbolic-ref", "-q", "--short", "HEAD"], cwd=root, check=False)
    current = proc.stdout.strip()
    if proc.returncode != 0 or not current:
        raise GitError("Your checkout isn't on a branch (HEAD is detached). Check out the branch to merge into "
                       "first, such as: git checkout main")
    shown = clean_line(current)[:200]
    if current.startswith("handoff/"):
        raise GitError(f"Your checkout has {shown} out, a task's own branch. Check out your project's branch "
                       "first, such as: git checkout main")
    if _git(root, ["status", "--porcelain", "--untracked-files=no"], cwd=root).stdout.strip():
        raise GitError(f"{shown} has uncommitted changes to tracked files. Commit or stash them first, then "
                       "merge.")
    tip = _git(root, ["rev-parse", ref], cwd=root).stdout.strip()
    if tip != expected_tip:
        raise Changed(f"{branch} changed since the worker's run: its tip ({tip[:12]}) isn't the commit the run "
                      f"recorded ({clean_line(expected_tip)[:12]}). Look at it before merging.")
    ahead = _git(root, ["rev-list", "--count", f"HEAD..{ref}"], cwd=root).stdout.strip()
    if ahead == "0":
        raise AlreadyMerged(f"{branch} is already merged into {shown}: it has nothing {shown} lacks.")
    named = _git(root, ["config", "user.email"], cwd=root, check=False).stdout.strip()
    identity = [] if named else ["-c", "user.name=Handoff worker", "-c", "user.email=handoff@localhost"]
    merged = _git(root, [*identity, "merge", "--no-ff", "--no-edit", "--no-gpg-sign", "-m", message, ref],
                  cwd=root, check=False)
    if merged.returncode != 0:
        out = _git(root, ["diff", "--name-only", "--diff-filter=U", "-z"], cwd=root, check=False,
                   errors="surrogateescape").stdout
        files = sorted(_shown(name) for name in out.split("\0") if name)
        _git(root, ["merge", "--abort"], cwd=root, check=False)
        if files:
            raise MergeConflict(f"Merging {branch} into {shown} conflicts in: {', '.join(files[:50])}. The merge "
                                "was aborted, so nothing changed.", files)
        lines = (merged.stderr or merged.stdout).strip().splitlines()
        said = f": {clean_line(lines[-1])[:300]}" if lines else ""
        raise GitError(f"git merge of {branch} into {shown} failed{said}. Nothing was merged.")
    sha = _git(root, ["rev-parse", "HEAD"], cwd=root).stdout.strip()
    short = _git(root, ["rev-parse", "--short", "HEAD"], cwd=root).stdout.strip()
    return shown, sha, short


def remove_worktree(root: Path, task_id: int) -> str | None:
    """Remove the task's worktree, .handoff/worktrees/T-N, keeping its branch. A note in words if it couldn't
    be, else None."""
    path = root / WORKTREES / f"T-{task_id}"
    where = (WORKTREES / f"T-{task_id}").as_posix()
    with _REPO_LOCK:
        if is_link(path):
            return f"{where} is a link, not the worker's worktree, so Handoff left it."
        if not path.exists():
            return None
        _git(root, ["worktree", "remove", "--force", str(path)], cwd=root, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            _git(root, ["worktree", "prune"], cwd=root, check=False)
    if path.exists():
        return f"Couldn't remove {where}; remove it with: git worktree remove --force {where}"
    return None


def untracked_files(root: Path) -> list[str]:
    """New files git doesn't track yet (ignored ones left out), relative to `root`. A diff doesn't have them."""
    out = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout
    return [name for name in out.split("\0") if name]


def review_target(root: Path) -> tuple[list[str], str] | None:
    """What a review reads, as `ixel ask` flags and in words: your uncommitted changes, else this branch since
    it left the default branch, else your last commit. None when there's nothing to review. New files git
    doesn't track yet aren't in any of these; asks.review_plan asks Ixel MAT for them too."""
    if _git(root, ["status", "--porcelain", "--untracked-files=no"], check=False).stdout.strip():
        return ["--diff"], "your uncommitted changes"
    branch = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.strip()
    default = _default_branch(root)
    if default and set(default) <= _SAFE_REF and branch not in (default, default.split("/", 1)[-1]):
        ahead = _git(root, ["rev-list", "--count", f"{default}..HEAD"], check=False).stdout.strip()
        if ahead.isdigit() and int(ahead) > 0:
            return ["--base", default], f"this branch ({clean_line(branch)}) since it left {default}"
    if _git(root, ["rev-parse", "--verify", "-q", "HEAD~1^{commit}"], check=False).returncode == 0:
        return ["--base", "HEAD~1"], "your last commit"
    return None
