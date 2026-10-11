"""Merging a finished task's branch into the project: the person only, from the window (op merge) or the command line.

These use a real temporary git repository; a run is recorded on the board the way the worker records one.
"""
import json
import subprocess

import pytest
from rich.console import Console

from handoff import api, cli, gitwork
from handoff.board import HUMAN, Board, Forbidden
from handoff.describe import describe

IDENTITY = ("-c", "user.name=Ada", "-c", "user.email=ada@example.com")


def git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          check=check)


def commit(folder, message):
    git(folder, "add", "-A")
    git(folder, *IDENTITY, "commit", "-q", "-m", message)


def head(root):
    return git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture(autouse=True)
def _as_the_person(monkeypatch):
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)  # (these may run under a Handoff worker)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("# Shop\n", encoding="utf-8")
    commit(root, "init")
    return root


@pytest.fixture
def board(repo):
    return Board.open(repo)


@pytest.fixture
def current(repo):
    return git(repo, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()


def finished_task(board, repo, title="Add a hello script", path="hello.txt", text="hi\n"):
    """A task the claude worker ran: its branch handoff/T-N, with one commit, in .handoff/worktrees/T-N, and the
    run recorded on the board with that commit. Returns the task and the commit."""
    task, _ = board.create(HUMAN, title, "Write it.")
    board.approve(HUMAN, task.id, "claude")
    [(_, approval)] = board.pending_runs("claude", [task.id])
    branch = f"handoff/{task.ref}"
    folder = repo / ".handoff" / "worktrees" / task.ref
    base = head(repo)
    git(repo, "worktree", "add", "-b", branch, str(folder))
    (folder / path).parent.mkdir(parents=True, exist_ok=True)
    (folder / path).write_text(text, encoding="utf-8")
    commit(folder, title)
    tip = head(folder)
    board.start_run("claude", task.id, approval.id, {"branch": branch})
    board.finish_run("claude", task.id, HUMAN, "did it", branch=branch, extra={"base": base, "commit": tip})
    return board.get(task.id), tip


def call(op, project=None, **args):
    request = {"schema": 1, "op": op, "args": args}
    if project is not None:
        request["project"] = str(project)
    reply, code = api.handle(json.dumps(request).encode("utf-8"))
    assert reply["schema"] == 1 and reply["op"] == op
    assert (code == 0) == reply["ok"]
    return reply


def ok(op, project=None, **args):
    reply = call(op, project, **args)
    assert reply["ok"], reply
    return reply["data"]


def error(op, project=None, **args):
    reply = call(op, project, **args)
    assert not reply["ok"], reply
    return reply["error"]["code"], reply["error"]["message"]


def merge_actions(repo, ref):
    return [a for a in ok("task", repo, task=ref)["actions"] if a["op"] == "merge"]


# ── Merging ──────────────────────────────────────────────────────────────────

def test_a_clean_merge_through_the_api(board, repo, current):
    task, tip = finished_task(board, repo)
    assert task.status == "handed_off" and task.branch == "handoff/T-1"
    data = ok("merge", repo, task="T-1")
    short = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    assert data["merged"] == {"branch": "handoff/T-1", "into": current, "sha": short}
    assert data["left"] == [] and data["task"]["status"] == "done"
    # one merge commit, of the branch, with the task in its subject
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "Merge T-1: Add a hello script"
    assert git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()[2] == tip
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "hi\n"
    assert board.get(1).status == "done"
    [merged] = [e for e in board.events(1) if e.kind == "merged"]
    assert merged.actor == HUMAN
    assert describe(merged) == f"merged handoff/T-1 into {current} at {short}"
    assert merged.data["commit"] == tip and merged.data["merge"] == head(repo)
    # the worktree is gone; the branch stays
    assert not (repo / ".handoff" / "worktrees" / "T-1").exists()
    assert git(repo, "rev-parse", "--verify", "refs/heads/handoff/T-1", check=False).returncode == 0


def test_a_branch_merged_once_is_already_merged(board, repo):
    finished_task(board, repo)
    ok("merge", repo, task="T-1")
    before = head(repo)
    code, message = error("merge", repo, task="T-1")
    assert code == "invalid" and "already merged" in message
    assert head(repo) == before


def test_a_branch_merged_by_hand_is_already_merged(board, repo):
    finished_task(board, repo)
    git(repo, *IDENTITY, "merge", "-q", "--no-ff", "-m", "by hand", "handoff/T-1")
    before = head(repo)
    code, message = error("merge", repo, task="T-1")
    assert code == "invalid" and "already merged" in message
    assert head(repo) == before and board.get(1).status == "handed_off"


def test_a_checkout_with_uncommitted_changes_is_refused(board, repo):
    finished_task(board, repo)
    before = head(repo)
    (repo / "README.md").write_text("# Shop, half edited\n", encoding="utf-8")
    code, message = error("merge", repo, task="T-1")
    assert code == "invalid" and "uncommitted" in message
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Shop, half edited\n"
    assert head(repo) == before and not (repo / "hello.txt").exists()
    assert board.get(1).status == "handed_off"


def test_a_detached_head_is_refused(board, repo):
    finished_task(board, repo)
    git(repo, "checkout", "-q", "--detach")
    before = head(repo)
    code, message = error("merge", repo, task="T-1")
    assert code == "invalid" and "detached" in message
    assert head(repo) == before and not (repo / "hello.txt").exists()
    assert board.get(1).status == "handed_off"


def test_a_conflict_is_aborted_and_reported_with_nothing_changed(board, repo):
    finished_task(board, repo, path="README.md", text="# Shop, by the agent\n")
    (repo / "README.md").write_text("# Shop, by you\n", encoding="utf-8")
    commit(repo, "my own change")
    before = head(repo)
    code, message = error("merge", repo, task="T-1")
    assert code == "invalid" and "README.md" in message and "nothing was changed" in message
    assert git(repo, "status", "--porcelain").stdout.strip() == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert head(repo) == before
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Shop, by you\n"
    assert board.get(1).status == "handed_off"
    assert not [e for e in board.events(1) if e.kind == "merged"]


def test_a_branch_that_changed_since_the_run_is_refused(board, repo):
    finished_task(board, repo)
    folder = repo / ".handoff" / "worktrees" / "T-1"
    (folder / "extra.txt").write_text("not what the run did\n", encoding="utf-8")
    commit(folder, "added later")
    before = head(repo)
    code, message = error("merge", repo, task="T-1")
    assert code == "changed" and "changed since the worker's run" in message
    assert head(repo) == before and not (repo / "hello.txt").exists()
    assert board.get(1).status == "handed_off"


def test_a_task_with_no_run_has_nothing_to_merge(board, repo):
    board.create(HUMAN, "Not run yet")
    code, message = error("merge", repo, task="T-1")
    assert code == "invalid" and "no branch" in message


# ── The person only ──────────────────────────────────────────────────────────

def test_agents_can_t_merge(board, repo, monkeypatch):
    task, tip = finished_task(board, repo)
    with pytest.raises(Forbidden, match="Only the person"):
        board.record_merge("claude", task.id, "main", tip, tip[:7], tip)
    assert board.get(task.id).status == "handed_off"
    assert "merge" in api.WRITE_OPS and "run-merge" in ok("hello")["features"]
    before = head(repo)
    monkeypatch.setenv("HANDOFF_DEPTH", "1")
    code, message = error("merge", repo, task="T-1")
    assert code == "forbidden" and "can't change the board as the person" in message
    monkeypatch.delenv("HANDOFF_DEPTH")
    assert head(repo) == before and board.get(task.id).status == "handed_off"


# ── The window's button ──────────────────────────────────────────────────────

def test_the_merge_action_is_offered_until_it_s_merged(board, repo, current):
    finished_task(board, repo)
    actions = ok("task", repo, task="T-1")["actions"]
    assert actions[0] == {"op": "merge", "label": f"Merge into {current}", "primary": False, "confirm": True}
    ok("merge", repo, task="T-1")
    assert merge_actions(repo, "T-1") == []
    board.create(HUMAN, "A fresh one")
    assert merge_actions(repo, "T-2") == []


# ── The command line ─────────────────────────────────────────────────────────

def test_handoff_merge_from_the_command_line(board, repo, current, monkeypatch, capsys):
    monkeypatch.setattr(cli, "console", Console(width=300, highlight=False))
    monkeypatch.setattr(cli, "err_console", Console(stderr=True, width=300, highlight=False))
    finished_task(board, repo)
    assert cli.main(["merge", "T-1", "--yes", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    short = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    assert f"Merged handoff/T-1 into {current} at {short}. T-1 is done." in out
    assert board.get(1).status == "done" and (repo / "hello.txt").exists()
    assert not (repo / ".handoff" / "worktrees" / "T-1").exists()
    assert cli.main(["merge", "T-1", "--yes", "--project", str(repo)]) == 1
    captured = capsys.readouterr()
    assert "already merged" in captured.out + captured.err
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "Merge T-1: Add a hello script"


# ── What a task's branch may not do to your checkout (found by an adversarial review) ──

def test_an_ignored_file_of_yours_is_never_overwritten(board, repo):
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    commit(repo, "ignore .env")
    (repo / ".env").write_text("MINE=1\n", encoding="utf-8")
    # The agent empties .gitignore in its worktree and commits its own .env
    task, _ = board.create(HUMAN, "Settings", "x")
    board.approve(HUMAN, task.id, "claude")
    [(_, approval)] = board.pending_runs("claude", [task.id])
    folder = repo / ".handoff" / "worktrees" / task.ref
    git(repo, "worktree", "add", "-b", f"handoff/{task.ref}", str(folder))
    (folder / ".gitignore").write_text("", encoding="utf-8")
    (folder / ".env").write_text("AGENT=1\n", encoding="utf-8")
    commit(folder, "settings")
    tip = head(folder)
    board.start_run("claude", task.id, approval.id, {"branch": f"handoff/{task.ref}"})
    board.finish_run("claude", task.id, HUMAN, "did it", branch=f"handoff/{task.ref}", extra={"commit": tip})
    before = head(repo)
    code, message = error("merge", repo, task=task.ref)
    assert code == "invalid" and ".env" in message and "doesn't track" in message
    assert (repo / ".env").read_text(encoding="utf-8") == "MINE=1\n" and head(repo) == before


def test_a_branch_that_touches_the_board_folder_is_refused(board, repo):
    task, tip = finished_task(board, repo, title="Sneaky", path=".handoff/board.db", text="not a board\n")
    before = head(repo)
    code, message = error("merge", repo, task=task.ref)
    assert code == "invalid" and "board folder" in message
    assert head(repo) == before and Board.open(repo).get(task.id).title == "Sneaky"


def test_a_branch_that_changes_what_git_runs_is_refused(board, repo):
    task, _ = finished_task(board, repo, title="LFS", path=".lfsconfig", text="[lfs]\n\turl = http://127.0.0.1:9/x\n")
    before = head(repo)
    code, message = error("merge", repo, task=task.ref)
    assert code == "invalid" and ".lfsconfig" in message and head(repo) == before


def test_a_merge_of_yours_in_progress_is_left_alone(board, repo, current):
    task, _ = finished_task(board, repo)
    # The person is in the middle of their own conflicted merge
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "README.md").write_text("# Side\n", encoding="utf-8")
    commit(repo, "side")
    git(repo, "checkout", "-q", current)
    (repo / "README.md").write_text("# Main\n", encoding="utf-8")
    commit(repo, "main")
    git(repo, *IDENTITY, "merge", "side", check=False)
    assert git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
    code, message = error("merge", repo, task=task.ref)
    assert code == "invalid" and "middle of a merge" in message
    assert git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0  # still theirs


def test_no_merge_while_a_run_is_under_way(board, repo):
    task, _ = finished_task(board, repo)
    board.set_status(HUMAN, task.id, "open")
    board.approve(HUMAN, task.id, "claude")
    code, message = error("merge", repo, task=task.ref)
    assert code == "invalid" and ("approved for another run" in message or "handed back" in message)
    assert merge_actions(repo, task.ref) == []
    [(_, approval)] = board.pending_runs("claude", [task.id])
    board.start_run("claude", task.id, approval.id, {"branch": task.branch})
    code, message = error("merge", repo, task=task.ref)
    assert code == "invalid"
    assert (repo / ".handoff" / "worktrees" / task.ref).is_dir()  # the run's worktree is still there


def test_a_worktree_with_uncommitted_work_is_kept(board, repo):
    task, _ = finished_task(board, repo)
    folder = repo / ".handoff" / "worktrees" / task.ref
    (folder / "draft.txt").write_text("not committed\n", encoding="utf-8")
    data = ok("merge", repo, task=task.ref)
    assert folder.is_dir() and (folder / "draft.txt").exists()
    assert data["left"] and "uncommitted" in data["left"][0]


def test_the_merge_button_goes_once_the_branch_is_merged_by_hand(board, repo):
    task, tip = finished_task(board, repo)
    assert [a["label"] for a in merge_actions(repo, task.ref)]
    git(repo, *IDENTITY, "merge", "-q", "--no-ff", "--no-edit", task.branch)
    assert merge_actions(repo, task.ref) == []


def test_the_worker_never_commits_a_board_folder(repo):
    folder = repo / ".handoff" / "worktrees" / "T-9"
    git(repo, "worktree", "add", "-q", "-b", "handoff/T-9", str(folder))
    worktree = gitwork.Worktree(folder, "handoff/T-9", gitwork.common_dir(repo) / "worktrees" / "T-9", head(repo))
    (folder / ".handoff").mkdir()
    (folder / ".handoff" / "board.db").write_text("forged\n", encoding="utf-8")
    (folder / ".HANDOFF2").write_text("fine\n", encoding="utf-8")
    (folder / "app.txt").write_text("work\n", encoding="utf-8")
    staged = gitwork.stage_all(repo, worktree)
    assert "app.txt" in staged and not any(name.casefold().startswith(".handoff/") for name in staged)
