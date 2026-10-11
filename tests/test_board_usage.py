"""The board list says what each task's worker runs used, and every run's on the board in all: read in one query,
and checked again when read, so a damaged or malformed usage row is skipped and a task with no runs shows none.

Real git, and runs recorded the way the worker records them (approve, start, finish with the run's usage).
"""
import subprocess

import pytest

from handoff import api, usage
from handoff.board import HUMAN, Board

FABLE, OPUS, HAIKU = "claude-fable-5-1", "claude-opus-5-5", "claude-haiku-5-5"

USE1 = {"models": {OPUS: {"in": 10, "out": 100, "cache_read": 1000, "cache_write": 0, "cost": 0.5}}}
USE2 = {"models": {OPUS: {"in": 20, "out": 50, "cache_read": 0, "cache_write": 200, "cost": 0.25},
                   HAIKU: {"in": 5, "out": 7, "cache_read": 0, "cache_write": 0, "cost": 0.001}}}
USE3 = {"models": {HAIKU: {"in": 3, "out": 4, "cache_read": 0, "cache_write": 0, "cost": 0.002}}}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("# Shop\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def board(repo):
    return Board.open(repo)


def task(board, title="Add a calculator"):
    made, _ = board.create(HUMAN, title, "Write calc.py with add and a test.", ["python -m pytest"])
    return made


def run(board, task_id, used, model=OPUS):
    """One worker run, recorded the way the worker records it: approved, started, finished with its usage."""
    board.approve(HUMAN, task_id, "claude", models=[model])
    [(_, approval)] = board.pending_runs("claude", [task_id])
    board.start_run("claude", task_id, approval.id, {"kind": "edit"})
    board.finish_run("claude", task_id, HUMAN, "DONE: wrote calc.py", extra={"usage": used})


def assert_same(got, want):
    """The same models, with the same tokens, and costs equal to within rounding."""
    assert set(got["models"]) == set(want["models"])
    for name, info in want["models"].items():
        found = got["models"][name]
        assert {k: v for k, v in found.items() if k != "cost"} == {k: v for k, v in info.items() if k != "cost"}
        assert found["cost"] == pytest.approx(info["cost"])


def listed(board, repo):
    return api.board_summary(board, repo, board.tasks())


def test_a_task_with_two_runs_sums_them_model_by_model(board, repo):
    t = task(board)
    run(board, t.id, USE1)
    run(board, t.id, USE2, model=HAIKU)

    total = {"models": {OPUS: {"in": 30, "out": 150, "cache_read": 1000, "cache_write": 200, "cost": 0.75},
                        HAIKU: {"in": 5, "out": 7, "cache_read": 0, "cache_write": 0, "cost": 0.001}}}
    assert_same(board.run_usage([t.id])[t.id][1], total)
    [summary] = listed(board, repo)["tasks"]
    assert_same(summary["usage"], total)
    assert summary["usage"]["models"][OPUS]["cost"] == pytest.approx(0.75)


def test_a_task_with_no_runs_has_no_usage(board, repo):
    t = task(board)
    assert board.run_usage([t.id]) == {}
    summary = api.board_summary(board, repo, [t])["tasks"][0]
    assert summary["usage"] is None and summary["cost"] == 0.0


def test_a_board_with_only_tasks_without_runs_has_no_usage(board, repo):
    task(board, "First")
    task(board, "Second")
    result = listed(board, repo)
    assert len(result["tasks"]) == 2 and result["usage"] is None and result["spent"] == 0.0
    assert all(summary["usage"] is None for summary in result["tasks"])


def test_the_board_total_is_the_sum_of_its_tasks(board, repo):
    a = task(board, "First")
    b = task(board, "Second")
    run(board, a.id, USE1)
    run(board, a.id, USE2)
    run(board, b.id, USE3, model=HAIKU)

    result = listed(board, repo)
    usages = {s["id"]: s["usage"] for s in result["tasks"] if s["usage"]}
    assert set(usages) == {a.id, b.id}
    assert_same(result["usage"], usage.merge(usages[a.id], usages[b.id]))
    assert_same(result["usage"], {"models": {
        OPUS: {"in": 30, "out": 150, "cache_read": 1000, "cache_write": 200, "cost": 0.75},
        HAIKU: {"in": 8, "out": 11, "cache_read": 0, "cache_write": 0, "cost": 0.003}}})
    assert usage.total_cost(result["usage"]) == pytest.approx(0.753) == result["spent"]


def test_a_malformed_usage_row_is_skipped_and_the_good_run_stays(board, repo):
    t = task(board)
    run(board, t.id, USE1)
    with board._write() as conn:
        for data in ("{oops", '{"usage": {"models": {"bad name!": {"in": 1}}}}', '{"usage": "lots"}'):
            conn.execute("INSERT INTO events (task_id, at, actor, kind, text, data) VALUES (?, ?, ?, ?, ?, ?)",
                         (t.id, "2026-10-11T12:00:00Z", "claude", "worker", "", data))

    assert_same(board.run_usage([t.id])[t.id][1], USE1)
    assert_same(listed(board, repo)["tasks"][0]["usage"], USE1)


def test_the_features_say_runs_report_usage(board):
    assert "run-usage" in api.op_hello({})["features"]
