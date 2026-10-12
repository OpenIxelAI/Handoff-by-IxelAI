"""What an answer's run used is recorded with it (asks._ixel_usage): the fake `ixel` from test_dispatch.py reports
FAKE_IXEL_USAGE as its usage, and the board's run usage and the handback's words are checked here. Paths come from
the fixtures (pathlib under tmp_path), so nothing here depends on the operating system."""
import json

import pytest
from test_dispatch import fake_ixel, repo, run_cli  # noqa: F401

from handoff import asks
from handoff.board import Board

API = {"agent": "grok_build", "billing": "api", "model": "grok-4.7", "input_tokens": 3100, "output_tokens": 800,
       "cache_read_tokens": 0, "cache_write_tokens": 0, "estimated": False, "cost_usd": 0.0123}
PLAN = {"agent": "grok_build", "billing": "plan", "model": "grok-4.7", "input_tokens": 3100, "output_tokens": 800,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "estimated": True, "cost_usd": 0.5}


def last_handback(repo):  # noqa: F811
    return [e for e in Board.open(repo).events(1) if e.kind == "handoff"][-1].data


def run_answer(repo, run_cli, monkeypatch, record):  # noqa: F811
    """One answer through the fake Ixel, which reports `record` as its usage (None: it reports none).
    Returns what the handback says the run left, `left`."""
    if record is None:
        monkeypatch.delenv("FAKE_IXEL_USAGE", raising=False)
    else:
        monkeypatch.setenv("FAKE_IXEL_USAGE", json.dumps(record))
    assert run_cli("add", "List three databases for a small shop")[0] == 0
    code, _, err = run_cli("approve", "T-1", "gemini", "--kind", "answer", "--no-fallback", "--yes")
    assert code == 0, err
    code, _, err = run_cli("run", "T-1")
    assert code == 0, err
    return last_handback(repo)["left"]


def test_an_api_answer_records_its_usage_and_cost(repo, fake_ixel, run_cli, monkeypatch):  # noqa: F811
    left = run_answer(repo, run_cli, monkeypatch, API)
    expected = {"models": {"grok-4.7": {"in": 3100, "out": 800, "cache_read": 0, "cache_write": 0, "cost": 0.0123}}}
    assert Board.open(repo).run_usage() == {1: (0.0123, expected)}
    assert "grok-4.7: 3.1k in, 800 out" in left and "$0.0123" in left


def test_a_subscription_answer_costs_nothing(repo, fake_ixel, run_cli, monkeypatch):  # noqa: F811
    left = run_answer(repo, run_cli, monkeypatch, PLAN)
    cost, models = Board.open(repo).run_usage()[1]
    assert cost == 0.0 and models["models"]["grok-4.7"]["in"] == 3100
    assert "(estimated) · on your subscription" in left


def test_an_empty_model_is_keyed_by_the_agent(repo, fake_ixel, run_cli, monkeypatch):  # noqa: F811
    record = {"agent": "grok_build", "billing": "api", "model": "", "input_tokens": 100, "output_tokens": 20,
              "cost_usd": 0.01}
    left = run_answer(repo, run_cli, monkeypatch, record)
    cost, models = Board.open(repo).run_usage()[1]
    assert cost == 0.01 and list(models["models"]) == ["grok_build"]
    assert "grok_build: 100 in, 20 out · $0.0100" in left


def test_no_usage_records_nothing_and_says_so(repo, fake_ixel, run_cli, monkeypatch):  # noqa: F811
    left = run_answer(repo, run_cli, monkeypatch, None)
    assert Board.open(repo).run_usage() == {}
    assert "no usage reported" in left and "subscription" not in left


def test_a_subscription_with_no_numbers_says_so(repo, fake_ixel, run_cli, monkeypatch):  # noqa: F811
    left = run_answer(repo, run_cli, monkeypatch, {"agent": "grok_build", "billing": "plan"})
    assert Board.open(repo).run_usage() == {}
    assert "on your subscription, no usage reported" in left


@pytest.mark.parametrize("record, recorded, said", [
    ("lots", {}, "no usage reported"),
    ([1, 2], {}, "no usage reported"),
    # a name with a space and an escape, and an empty agent to fall back on: nothing is kept
    ({"model": "bad name\u001b[31m", "agent": "", "input_tokens": 5, "billing": "api", "cost_usd": 1}, {},
     "no usage reported"),
    # a negative count, a count that's a boolean, and a cost that's a string: nothing is kept
    ({"model": "x", "billing": "api", "input_tokens": -5, "output_tokens": True, "cost_usd": "9"}, {},
     "no usage reported"),
    # an unknown billing: the tokens are kept under the model, the cost isn't
    ({"model": "x", "billing": "free money", "input_tokens": 10, "cost_usd": 3.0},
     {1: (0.0, {"models": {"x": {"in": 10, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}}})},
     "cost not reported"),
])
def test_malformed_usage_is_left_out(repo, fake_ixel, run_cli, monkeypatch, record, recorded, said):  # noqa: F811
    left = run_answer(repo, run_cli, monkeypatch, record)
    assert Board.open(repo).run_usage() == recorded
    assert said in left and "\u001b" not in left and "free money" not in left and "bad name" not in left


def test_ixel_usage_checks_the_numbers_it_is_given():
    used, said = asks._ixel_usage({"usage": {"model": "x", "billing": "api", "input_tokens": 10 ** 13,
                                             "output_tokens": 5, "cost_usd": 0.5}})
    assert used["models"]["x"]["in"] == 0 and used["models"]["x"]["out"] == 5  # too many tokens: none counted
    assert said == "Used: x: 0 in, 5 out · $0.5000."
    for cost in (float("nan"), float("inf")):  # neither is a cost
        raw = {"model": "x", "billing": "api", "input_tokens": 100, "cost_usd": cost}
        used, said = asks._ixel_usage({"usage": raw})
        assert used["models"]["x"] == {"in": 100, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
        assert said == "Used: x: 100 in, 0 out · cost not reported."
    assert asks._ixel_usage({}) == (None, "Used: no usage reported.")
    assert asks._ixel_usage({"usage": {"billing": "plan"}}) == (
        None, "Used: on your subscription, no usage reported.")
