"""Merging a task whose branch was made by hand with git (no worker run): refused, with how to merge it by hand."""
import json
import subprocess

import pytest
from rich.console import Console

from handoff import api, cli
from handoff.board import HUMAN, Board

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


def call(op, project=None, **args):
    request = {"schema": 1, "op": op, "args": args}
    if project is not None:
        request["project"] = str(project)
    reply, code = api.handle(json.dumps(request).encode("utf-8"))
    assert reply["schema"] == 1 and reply["op"] == op
    assert (code == 0) == reply["ok"]
    return reply


def error(op, project=None, **args):
    reply = call(op, project, **args)
    assert not reply["ok"], reply
    return reply["error"]["code"], reply["error"]["message"]


def run_cli(repo, monkeypatch, capsys):
    monkeypatch.setattr(cli, "console", Console(width=300, highlight=False))
    monkeypatch.setattr(cli, "err_console", Console(stderr=True, width=300, highlight=False))
    code = cli.main(["merge", "T-1", "--yes", "--project", str(repo)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


# ── Hand-made branches ───────────────────────────────────────────────────────

def test_a_hand_made_branch_is_refused_with_how_to_merge_it(board, repo, current, monkeypatch, capsys):
    board.create(HUMAN, "Made by hand", "Write it.")  # T-1, with no worker run
    git(repo, "checkout", "-q", "-b", "handoff/T-1")
    (repo / "hand.txt").write_text("by hand\n", encoding="utf-8")
    commit(repo, "by hand")
    git(repo, "checkout", "-q", current)
    before_head = head(repo)
    before_tip = git(repo, "rev-parse", "handoff/T-1").stdout.strip()
    before_status = board.get(1).status
    before_porcelain = git(repo, "status", "--porcelain").stdout

    code, message = error("merge", repo, task="T-1")
    assert code == "invalid"
    assert "handoff/T-1" in message and "made by hand rather than by a worker run" in message
    assert "Handoff won't merge it" in message and "run: git merge handoff/T-1" in message
    assert "nothing to merge" not in message

    code, output = run_cli(repo, monkeypatch, capsys)
    assert code == 1
    assert "made by hand rather than by a worker run" in output and "git merge handoff/T-1" in output

    assert head(repo) == before_head
    assert git(repo, "rev-parse", "handoff/T-1").stdout.strip() == before_tip
    assert board.get(1).status == before_status
    assert git(repo, "status", "--porcelain").stdout == before_porcelain
    assert not (repo / "hand.txt").exists()
    assert not [e for e in board.events(1) if e.kind == "merged"]


# ── No branch at all ─────────────────────────────────────────────────────────

def test_a_task_with_no_branch_at_all_keeps_the_old_wording(board, repo, monkeypatch, capsys):
    board.create(HUMAN, "Not run yet", "Write it.")  # T-1, and no branch
    before = head(repo)

    code, message = error("merge", repo, task="T-1")
    assert code == "invalid"
    assert "T-1 has no branch: nothing to merge." in message and "made by hand" not in message

    code, output = run_cli(repo, monkeypatch, capsys)
    assert code == 1
    assert "T-1 has no branch, so there's nothing to merge." in output and "made by hand" not in output

    assert head(repo) == before
