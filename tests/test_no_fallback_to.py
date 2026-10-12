"""`handoff approve T-N AGENT --no-fallback-to codex[,agent...]` (and the API's args.no_fallback_to, on approve
and on fallbacks) leaves the agents you name out of the fallbacks sealed in an approval: for an edit, a chain, an
answer and a review alike. The others still take over; an agent Handoff has never heard of is refused before
anything is approved.

The rankings are made up here (codex's models are test-a, test-b and test-x, so no price rule drops them), every
edit agent looks installed, and the fake `ixel` from test_dispatch.py has claude, codex, gemini and grok ready.
"""
import json

import pytest
from test_dispatch import fake_ixel, repo, run_cli  # noqa: F401

from handoff import adapters, api, cli, rankings
from handoff.board import HUMAN, Board

CODING = ["claude:claude-opus-5-5", "codex:test-a", "claude:claude-sonnet-5-5", "codex:test-b",
          "claude:claude-haiku-5-5"]
ASKING = ["gemini:test-g", "claude:test-c", "codex:test-x", "grok:test-k"]
RANKS = {"coding": CODING, "planning": CODING, "writing": ASKING, "review": ASKING}


@pytest.fixture(autouse=True)
def ranked(_only_built_in_agents, monkeypatch):
    monkeypatch.setattr(rankings, "load", lambda: (RANKS, "test"))
    monkeypatch.setattr(adapters, "find_on_path", lambda name: name)  # (every agent looks installed)
    monkeypatch.setattr(adapters, "enabled", lambda name, config=None: True)
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)  # (the API won't approve from inside a Handoff run)


def sealed(root, task_id=1) -> dict:
    """What the task's last approval sealed."""
    return [e for e in Board.open(root).events(task_id) if e.kind == "approved"][-1].data


def agents_of(entries) -> list[str]:
    """["codex:test-a", "claude"] -> ["codex", "claude"]: the agent each entry belongs to."""
    return [rankings.split(entry)[0] for entry in entries]


# ── The command ──────────────────────────────────────────────────────────────

def test_an_edit_leaves_the_agent_out_of_its_fallbacks(repo, run_cli):  # noqa: F811
    assert run_cli("add", "Fix the footer in README.md")[0] == 0
    # (without the flag, codex's models take over too: the flag made the difference)
    assert "codex:test-a" in cli.suggested_fallbacks("edit", "claude", ["claude-opus-5-5"], repo)
    code, out, err = run_cli("approve", "T-1", "claude", "--model", "claude-opus-5-5", "--no-fallback-to", "codex",
                             "--yes")
    assert code == 0, err
    fallbacks = sealed(repo)["fallbacks"]
    assert fallbacks and set(agents_of(fallbacks)) == {"claude"}
    assert "goes on with: claude-sonnet-5-5, then claude-haiku-5-5" in out
    assert "test-a" not in out and "test-b" not in out
    [(task, event)] = Board.open(repo).pending_runs()  # sealed: it checks out, so the worker will run it
    assert task.ref == "T-1" and event.data["fallbacks"] == fallbacks


def test_a_chain_leaves_the_agent_out_of_its_fallbacks(repo, run_cli):  # noqa: F811
    assert run_cli("add", "Fix the footer in README.md")[0] == 0
    chain = ["claude-opus-5-5", "claude-haiku-5-5"]
    assert "codex:test-a" in cli.suggested_fallbacks("edit", "claude", chain, repo)
    code, out, err = run_cli("approve", "T-1", "claude", "--chain", ",".join(chain), "--no-fallback-to", "codex",
                             "--yes")
    assert code == 0, err
    approval = sealed(repo)
    assert approval["models"] == chain
    assert approval["fallbacks"] and set(agents_of(approval["fallbacks"])) == {"claude"}
    assert "test-a" not in out and "test-b" not in out


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_an_answer_or_a_review_leaves_the_agent_out(repo, fake_ixel, run_cli, kind):  # noqa: F811
    assert run_cli("add", "Review the documentation in README.md")[0] == 0
    # (without the flag, codex takes over after claude)
    assert cli.suggested_fallbacks(kind, "gemini", None, repo) == ["claude", "codex", "grok"]
    code, out, err = run_cli("approve", "T-1", "gemini", "--kind", kind, "--no-fallback-to", "codex", "--yes")
    assert code == 0, err
    assert sealed(repo)["fallbacks"] == ["claude", "grok"]
    assert "claude, then grok take over" in out
    [(_, event)] = Board.open(repo).pending_runs()
    assert event.data["fallbacks"] == ["claude", "grok"]


def test_a_comma_list_leaves_out_each_agent_in_it(repo, fake_ixel, run_cli):  # noqa: F811
    assert run_cli("add", "Review the documentation in README.md")[0] == 0
    code, _, err = run_cli("approve", "T-1", "gemini", "--kind", "answer", "--no-fallback-to", "codex,grok", "--yes")
    assert code == 0, err
    assert sealed(repo)["fallbacks"] == ["claude"]
    # names are read as they're said: in any case, with spaces round them, and once however often they're given
    assert run_cli("add", "Review the notes in README.md")[0] == 0
    code, _, err = run_cli("approve", "T-2", "gemini", "--kind", "review", "--no-fallback-to", "Codex, grok ,codex",
                           "--yes")
    assert code == 0, err
    assert sealed(repo, 2)["fallbacks"] == ["claude"]


def test_leaving_out_every_agent_seals_no_fallbacks_and_says_so(repo, run_cli):  # noqa: F811
    assert run_cli("add", "Fix the footer in README.md")[0] == 0
    code, out, err = run_cli("approve", "T-1", "claude", "--no-fallback-to", "codex,claude", "--yes")
    assert code == 0, err
    assert "fallbacks" not in sealed(repo)
    assert "no other model you have can take over" in out
    assert len(Board.open(repo).pending_runs()) == 1


@pytest.mark.parametrize(("value", "said"), [
    ("codx", "codx"),
    ("codex,codx", "codx"),
    (",", "takes the agents to leave out"),
])
def test_an_agent_handoff_does_not_know_is_refused(repo, run_cli, capsys, value, said):  # noqa: F811
    assert run_cli("add", "Fix the footer in README.md")[0] == 0
    with pytest.raises(SystemExit) as exited:
        run_cli("approve", "T-1", "claude", "--no-fallback-to", value, "--yes")
    assert exited.value.code == 2
    assert said in capsys.readouterr().err
    assert Board.open(repo).pending_runs() == []
    assert [e for e in Board.open(repo).events(1) if e.kind == "approved"] == []


def test_no_fallback_and_no_fallback_to_are_not_used_together(repo, run_cli, capsys):  # noqa: F811
    assert run_cli("add", "Fix the footer in README.md")[0] == 0
    with pytest.raises(SystemExit) as exited:
        run_cli("approve", "T-1", "claude", "--no-fallback", "--no-fallback-to", "codex", "--yes")
    assert exited.value.code == 2
    assert "not both" in capsys.readouterr().err
    assert Board.open(repo).pending_runs() == []
    assert [e for e in Board.open(repo).events(1) if e.kind == "approved"] == []


def test_a_picture_has_no_fallbacks_to_leave_agents_out_of(repo, run_cli, capsys):  # noqa: F811
    assert run_cli("add", "Draw a logo for the shop")[0] == 0
    with pytest.raises(SystemExit) as exited:
        run_cli("approve", "T-1", "grok", "--kind", "image", "--no-fallback-to", "codex", "--yes")
    assert exited.value.code == 2
    assert "pictures don't fall back" in capsys.readouterr().err
    assert Board.open(repo).pending_runs() == []


# ── The API ──────────────────────────────────────────────────────────────────

def call(root, op, **args):
    raw = json.dumps({"schema": 1, "op": op, "project": str(root), "args": args}).encode("utf-8")
    reply, code = api.handle(raw)
    assert (code == 0) == reply["ok"]
    return reply


def ok(root, op, **args):
    reply = call(root, op, **args)
    assert reply["ok"], reply
    return reply["data"]


def refused(root, op, **args):
    reply = call(root, op, **args)
    assert not reply["ok"], reply
    return reply["error"]["code"], reply["error"]["message"]


def test_the_fallbacks_op_leaves_the_agent_out_of_an_edit(repo):  # noqa: F811
    everyone = ok(repo, "fallbacks", agent="claude", models=["claude-opus-5-5"])
    assert "codex:test-a" in everyone["fallbacks"]
    data = ok(repo, "fallbacks", agent="claude", models=["claude-opus-5-5"], no_fallback_to=["codex"])
    assert data["fallbacks"] and set(agents_of(data["fallbacks"])) == {"claude"}
    assert "codex" not in data["words"]


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_the_fallbacks_op_leaves_the_agent_out_of_an_answer_or_a_review(repo, fake_ixel, kind):  # noqa: F811
    assert ok(repo, "fallbacks", agent="gemini", kind=kind)["fallbacks"] == ["claude", "codex", "grok"]
    data = ok(repo, "fallbacks", agent="gemini", kind=kind, no_fallback_to=["codex"])
    assert data["fallbacks"] == ["claude", "grok"]
    assert "codex" not in data["words"]


def test_the_fallbacks_op_has_none_for_a_picture_either(repo):  # noqa: F811
    assert ok(repo, "fallbacks", agent="grok", kind="image", no_fallback_to=["codex"])["fallbacks"] == []


@pytest.mark.parametrize(("kind", "agent", "models", "expected"), [
    pytest.param("edit", "claude", ["claude-opus-5-5"], {"claude"}, id="edit"),
    pytest.param("edit", "claude", ["claude-opus-5-5", "claude-haiku-5-5"], {"claude"}, id="chain"),
    pytest.param("answer", "gemini", None, {"claude", "grok"}, id="answer"),
    pytest.param("review", "gemini", None, {"claude", "grok"}, id="review"),
])
def test_the_api_seals_fallbacks_without_the_agent(repo, fake_ixel, kind, agent, models, expected):  # noqa: F811
    task = Board.open(repo).create(HUMAN, "Review the documentation", "Check README.md")[0]
    args = {"task": task.ref, "agent": agent, "kind": kind, "no_fallback_to": ["codex"]}
    if models:
        args["models"] = models
    ok(repo, "approve", **args)
    approval = sealed(repo, task.id)
    assert approval["kind"] == kind
    assert approval["fallbacks"] and set(agents_of(approval["fallbacks"])) == expected
    if models:
        assert approval["models"] == models
    [(_, event)] = Board.open(repo).pending_runs()  # sealed: it checks out, so the worker will run it
    assert event.data["fallbacks"] == approval["fallbacks"]


def test_the_api_drops_the_agent_from_the_fallbacks_it_is_given(repo):  # noqa: F811
    board = Board.open(repo)
    given = ["codex:test-a", "claude:claude-sonnet-5-5", "codex:test-b"]
    task = board.create(HUMAN, "Fix the footer", "Edit README.md")[0]
    ok(repo, "approve", task=task.ref, agent="claude", models=["claude-opus-5-5"], fallbacks=given,
       no_fallback_to=["codex"])
    assert sealed(repo, task.id)["fallbacks"] == ["claude:claude-sonnet-5-5"]
    # an answer's are the agents alone
    task = board.create(HUMAN, "Review the documentation", "Check README.md")[0]
    ok(repo, "approve", task=task.ref, agent="gemini", kind="answer", fallbacks=["codex", "grok"],
       no_fallback_to=["codex"])
    assert sealed(repo, task.id)["fallbacks"] == ["grok"]
    # nothing is left of the list: none are sealed, and none are suggested in their place
    task = board.create(HUMAN, "Fix the header", "Edit README.md")[0]
    ok(repo, "approve", task=task.ref, agent="claude", fallbacks=["codex:test-a"], no_fallback_to=["codex"])
    assert "fallbacks" not in sealed(repo, task.id)
    # no names to leave out: the list is sealed as it came
    task = board.create(HUMAN, "Fix the sidebar", "Edit README.md")[0]
    ok(repo, "approve", task=task.ref, agent="claude", fallbacks=given, no_fallback_to=None)
    assert sealed(repo, task.id)["fallbacks"] == given


def test_the_api_refuses_an_agent_handoff_does_not_know(repo):  # noqa: F811
    task = Board.open(repo).create(HUMAN, "Fix the footer", "Edit README.md")[0]
    for op, args in (("approve", {"task": task.ref, "agent": "claude"}), ("fallbacks", {"agent": "claude"})):
        code, message = refused(repo, op, **args, no_fallback_to=["codex", "codx"])
        assert code == "usage" and "codx" in message
    assert Board.open(repo).pending_runs() == []
    assert [e for e in Board.open(repo).events(task.id) if e.kind == "approved"] == []


@pytest.mark.parametrize("value", ["codex", [], ["codex", 3], {"codex": True}, 3])
def test_the_api_refuses_no_fallback_to_that_is_not_a_list_of_names(repo, value):  # noqa: F811
    task = Board.open(repo).create(HUMAN, "Fix the footer", "Edit README.md")[0]
    for op, args in (("approve", {"task": task.ref, "agent": "claude"}), ("fallbacks", {"agent": "claude"})):
        code, message = refused(repo, op, **args, no_fallback_to=value)
        assert code == "usage" and "no_fallback_to" in message
    assert Board.open(repo).pending_runs() == []


def test_the_api_refuses_no_fallback_to_for_a_picture(repo):  # noqa: F811
    task = Board.open(repo).create(HUMAN, "Draw a logo", "For the shop")[0]
    code, message = refused(repo, "approve", task=task.ref, agent="grok", kind="image", no_fallback_to=["codex"])
    assert code == "usage" and "pictures" in message
    assert Board.open(repo).pending_runs() == []
