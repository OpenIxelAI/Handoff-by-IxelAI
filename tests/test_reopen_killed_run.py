"""`reopen` takes back a task that a killed worker run left in progress, keeps its worktree, and still refuses a
run that's alive, a hand claim, and a run whose process can't be told."""
import os
import subprocess
import sys
from pathlib import Path

from test_cli_commands import project, run  # noqa: F401

from handoff.board import Board


def dead_pid():
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        pass  # the with block waits for it to exit
    return child.pid


def start_a_run_with_pid(root: Path, pid):
    board = Board.open(root)
    board.create("human", "Endpoint", assignee="codex")
    board.approve("human", 1, "codex")
    approval = board.pending_runs("codex")[0][1]
    board.start_run("codex", 1, approval.id, {"branch": "handoff/T-1", "pid": pid})


def test_a_killed_run_reopens_and_keeps_its_worktree(project, capsys):  # noqa: F811
    worktree = project / ".handoff" / "worktrees" / "T-1"
    worktree.mkdir(parents=True)
    (worktree / "work.txt").write_text("half done\n", encoding="utf-8")
    start_a_run_with_pid(project, dead_pid())

    code, out, _ = run(capsys, "reopen", "T-1")
    assert code == 0
    for line in ("T-1 is open again, assigned to codex", ".handoff/worktrees/T-1", "handoff/T-1", "kept"):
        assert line in out, line

    board = Board.open(project)
    task = board.get(1)
    assert task.status == "open" and task.assignee == "codex"
    assert board.events(1)[-1].kind == "status"
    assert worktree.is_dir()
    assert (worktree / "work.txt").read_text(encoding="utf-8") == "half done\n"


def test_a_live_run_is_refused(project, capsys):  # noqa: F811
    start_a_run_with_pid(project, os.getpid())

    code, _, err = run(capsys, "reopen", "T-1")
    assert code == 1 and "being run by the codex worker now" in err
    assert Board.open(project).get(1).status == "in_progress"


def test_a_hand_claim_is_refused(project, capsys):  # noqa: F811
    board = Board.open(project)
    board.create("claude", "Endpoint", assignee="claude")
    board.note("claude", 1, "Started")

    code, _, err = run(capsys, "reopen", "T-1")
    assert code == 1 and "T-1 is in_progress, not done, cancelled or blocked" in err
    assert "handoff status T-1 open" in err
    assert Board.open(project).get(1).status == "in_progress"


def test_a_run_with_no_recorded_process_is_refused(project, capsys):  # noqa: F811
    start_a_run_with_pid(project, None)

    code, _, err = run(capsys, "reopen", "T-1")
    assert code == 1 and "T-1 is in_progress, not done, cancelled or blocked" in err
    assert Board.open(project).get(1).status == "in_progress"
