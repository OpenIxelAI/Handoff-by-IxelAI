"""An answer or a review can carry one effort level: `handoff approve T-N AGENT --kind answer|review --effort LEVEL`
(and the API) seals it in the approval and passes it to `ixel ask` when Ixel MAT can take it. An image can't, and
neither can --model or --chain: Ixel MAT picks the model.

The fake `ixel` from test_dispatch.py stands in for Ixel MAT; it doesn't advertise "effort" unless a test says so.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from handoff import api, asks
from handoff.board import HUMAN, Board, BoardError
from test_dispatch import fake_ixel, repo, run_cli  # noqa: F401


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_approve_an_answer_or_a_review_with_an_effort(repo, fake_ixel, run_cli, kind):
    assert run_cli("add", "Review the documentation in README.md")[0] == 0
    code, out, err = run_cli("approve", "T-1", "gemini", "--kind", kind, "--effort", "high", "--no-fallback", "--yes")
    assert code == 0, err
    assert "at high effort" in out
    approval = Board.open(repo).events(1)[-1]
    assert approval.data["kind"] == kind
    assert approval.data["efforts"] == {"": "high"}
    assert "models" not in approval.data
    [(task, event)] = Board.open(repo).pending_runs()  # sealed: it checks out, so the worker will run it
    assert task.ref == "T-1" and event.data["efforts"] == {"": "high"}


def test_answers_and_reviews_still_refuse_a_model_and_a_chain_and_pictures_an_effort(repo, fake_ixel, run_cli,
                                                                                    capsys):
    assert run_cli("add", "Review the documentation in README.md")[0] == 0
    refused = (
        (["gemini", "--kind", "answer", "--model", "gemini-3"], "--model and --chain"),
        (["gemini", "--kind", "review", "--chain", "a,b"], "--model and --chain"),
        (["gemini", "--kind", "answer", "--model", "x", "--effort", "high"], "--model and --chain"),
        (["grok", "--kind", "image", "--effort", "high"], "--effort"),
    )
    for args, said in refused:
        with pytest.raises(SystemExit) as exited:
            run_cli("approve", "T-1", *args, "--yes")
        assert exited.value.code == 2 and said in capsys.readouterr().err
    assert Board.open(repo).pending_runs() == []


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_the_api_approves_an_answer_or_a_review_with_an_effort(repo, kind):
    task = Board.open(repo).create(HUMAN, "Review the documentation", "Check README.md")[0]
    api.op_approve({"project": str(repo), "args": {"task": task.ref, "agent": "gemini", "kind": kind,
                                                   "efforts": {"": "high"}}})
    board = Board.open(repo)
    assert board.events(task.id)[-1].data["efforts"] == {"": "high"}
    [(_, event)] = board.pending_runs()
    assert event.data["efforts"] == {"": "high"}


def test_the_api_refuses_a_model_or_an_effort_that_does_not_fit_the_run(repo):
    task = Board.open(repo).create(HUMAN, "Review the documentation", "Check README.md")[0]
    refused = (
        {"agent": "gemini", "kind": "answer", "models": ["gemini-3"]},
        {"agent": "gemini", "kind": "answer", "efforts": {"gemini-3": "high"}},
        {"agent": "grok", "kind": "image", "efforts": {"": "high"}},
    )
    for args in refused:
        with pytest.raises(BoardError):
            api.op_approve({"project": str(repo), "args": {"task": task.ref, **args}})
    assert Board.open(repo).pending_runs() == []


def roster(*features):
    return asks.IxelRoster(agents=[{"name": n, "label": n, "ready": True} for n in ("gemini", "codex")],
                           providers=[], features=features)


def effort_of(argv):
    return argv[argv.index("--effort") + 1] if "--effort" in argv else None


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_command_for_adds_the_effort_only_when_ixel_can_take_it(repo, kind):
    task = Board.open(repo).create(HUMAN, "Review the documentation", "Check README.md")[0]

    def argv_for(agent, ixel_roster, effort):
        argv, _, _ = asks.command_for("ixel", repo, task, kind, agent, ixel_roster, Path("out"), effort=effort)
        return argv

    # one agent, and Ixel has "effort": the level goes to that agent
    argv = argv_for("gemini", roster("effort"), "high")
    assert effort_of(argv) == "high" and argv[-1] == "-"
    # fallbacks (several agents) and no "effort-first": Ixel refuses --effort with several agents, so it's left out
    assert effort_of(argv_for("gemini,codex", roster("effort"), "high")) is None
    # fallbacks, and Ixel applies the level to the first agent only
    assert effort_of(argv_for("gemini,codex", roster("effort", "effort-first"), "high")) == "high"
    # Ixel without "effort", or no roster at all: the run goes on at the agent's own setting
    assert effort_of(argv_for("gemini", roster(), "high")) is None
    assert effort_of(argv_for("gemini", None, "high")) is None
    # no effort on the run: nothing to pass
    assert effort_of(argv_for("gemini", roster("effort"), None)) is None


def test_run_passes_the_effort_to_ixel_when_it_can_take_it(repo, fake_ixel, run_cli, monkeypatch):
    original = asks.roster_from_lists

    def with_effort(*args):  # an Ixel MAT that advertises "effort", as the one that takes it does
        listed = original(*args)
        return replace(listed, features=(*listed.features, "effort"))

    monkeypatch.setattr(asks, "roster_from_lists", with_effort)
    assert run_cli("add", "List three databases for a small shop")[0] == 0
    code, _, err = run_cli("approve", "T-1", "gemini", "--kind", "answer", "--effort", "high", "--no-fallback", "--yes")
    assert code == 0, err
    code, _, err = run_cli("run", "T-1")
    assert code == 0, err
    [call] = fake_ixel("ask")
    assert call["argv"][call["argv"].index("--effort") + 1] == "high"


def test_run_leaves_the_effort_out_when_ixel_cannot_take_it(repo, fake_ixel, run_cli):
    assert run_cli("add", "List three databases for a small shop")[0] == 0
    code, _, err = run_cli("approve", "T-1", "gemini", "--kind", "answer", "--effort", "high", "--no-fallback", "--yes")
    assert code == 0, err
    code, _, err = run_cli("run", "T-1")
    assert code == 0, err
    [call] = fake_ixel("ask")
    assert "--effort" not in call["argv"]
