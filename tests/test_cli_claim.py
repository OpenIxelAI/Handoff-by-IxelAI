"""handoff claim: the person takes a task, and the paths they'll change, from the command line."""
from pathlib import Path

import pytest
from rich.console import Console

from handoff import cli
from handoff.board import Board


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "console", Console(width=200, highlight=False))
    monkeypatch.setattr(cli, "err_console", Console(width=200, highlight=False, stderr=True))
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    return root


def run(capsys, *args):
    code = cli.main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_claim_with_no_paths(project, capsys):
    run(capsys, "add", "Task")
    code, out, _ = run(capsys, "claim", "T-1")
    assert code == 0 and "T-1 is yours (claimed)." in out
    task = Board.open(project).get(1)
    assert task.assignee == "human" and task.status == "claimed"
    assert not any(event.kind == "claim_paths" for event in Board.open(project).events(1))


def test_claim_with_paths(project, capsys):
    run(capsys, "add", "Task")
    code, out, _ = run(capsys, "claim", "1", str(Path("src", "api.py")), "docs")
    assert code == 0 and "T-1 is yours" in out
    events = [event for event in Board.open(project).events(1) if event.kind == "claim_paths"]
    assert len(events) == 1
    assert events[0].data["paths"] == [Path("src", "api.py").as_posix(), "docs"]
    assert "overlaps" not in out


def test_claim_reports_an_overlap(project, capsys):
    run(capsys, "add", "First", "--to", "codex", "--path", "src")
    run(capsys, "add", "Second")
    code, out, _ = run(capsys, "claim", "T-2", str(Path("src", "api.py")))
    assert code == 0 and "T-2 is yours" in out
    assert "Another active task claims overlapping paths" in out
    assert f"{Path('src', 'api.py').as_posix()} overlaps src, claimed by T-1, assigned to codex" in out


def test_claim_an_unknown_task_fails_cleanly(project, capsys):
    run(capsys, "add", "Task")
    code, out, err = run(capsys, "claim", "T-99")
    assert code == 1
    assert "There's no task T-99 on this board." in err
    assert "Traceback" not in err
    assert "is yours" not in out
    assert Board.open(project).get(1).assignee is None
