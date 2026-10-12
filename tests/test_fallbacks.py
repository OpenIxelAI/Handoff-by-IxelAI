"""Fallbacks: a model that hits its usage limit is benched and the run goes on with the next best one its approval
lists (rankings.py), and the handback says so, instead of stopping with nothing done.

Real git and a fake agent standing in for `claude -p --output-format json`, in Claude's edit-only mode.
"""
import json
import subprocess
from pathlib import Path

import pytest

from handoff import api, approvals, limits, rankings
from handoff.board import HUMAN, Board, BoardError
from handoff.worker import Worker

FABLE, OPUS, SONNET, HAIKU = "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
LIMIT = "You've hit your usage limit · resets 3pm"


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


def result(reply, model, cost=0.01, error=False):
    return json.dumps({"type": "result", "is_error": error, "result": reply, "total_cost_usd": cost,
                       "modelUsage": {model: {"inputTokens": 10, "outputTokens": 100, "cacheReadInputTokens": 0,
                                              "cacheCreationInputTokens": 0, "costUSD": cost}}})


class Agent:
    """Stands in for `claude -p`: a model in `out` says it's out of usage; the others do their work."""

    def __init__(self, replies, out=(), actions=None, error=LIMIT):
        self.replies, self.out, self.actions, self.error, self.calls = replies, set(out), actions or {}, error, []

    def __call__(self, argv, stdin, env, timeout, cwd):
        model = argv[argv.index("--model") + 1] if "--model" in argv else "default"
        self.calls.append({"argv": argv, "stdin": stdin, "model": model})
        if model in self.out:
            return subprocess.CompletedProcess(argv, 1, result(self.error, model, 0.0, error=True), "")
        if model in self.actions:
            self.actions[model](Path(cwd))
        return subprocess.CompletedProcess(argv, 0, self.replies[model], "")


def write_calc(folder):
    (folder / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def worker(board, repo, agent):
    return Worker(board, repo, "claude", runner=agent, command="claude", sandboxed=False)


def task(board):
    made, _ = board.create(HUMAN, "Add a calculator", "Write calc.py with add.", ["python -m pytest"])
    return made


# ── Usage-limit words ────────────────────────────────────────────────────────

@pytest.mark.parametrize("said", [LIMIT, "You've hit your Opus limit", "Your workspace is out of credits",
                                  "Error: insufficient_quota", "Weekly limit reached", "429 RESOURCE_EXHAUSTED"])
def test_out_of_usage(said):
    assert limits.out_of_usage(said)


@pytest.mark.parametrize("said", ["You've hit your rate limit, retry in 5s", "Prompt is too long for the context "
                                  "window", "Connection reset", "Not logged in", ""])
def test_not_out_of_usage(said):
    assert not limits.out_of_usage(said)


# ── Rankings ─────────────────────────────────────────────────────────────────

def test_the_published_rankings_file_matches_the_built_in_one():
    data = json.loads((Path(rankings.__file__).with_name("rankings.json")).read_text(encoding="utf-8"))
    assert data == {"updated": rankings.UPDATED, "ranks": rankings.RANKS}
    assert rankings.check(data) == rankings.RANKS


def test_every_ranking_entry_is_valid_and_named_once():
    for job, entries in rankings.RANKS.items():
        assert job in rankings.JOBS and entries
        assert all(rankings.check_entry(e) for e in entries) and len(set(entries)) == len(entries), job


def test_fallbacks_are_the_next_best_that_you_have():
    ranks = {"coding": ["claude:claude-sonnet-5-5", "claude:claude-opus-5-5", "codex:gpt-6-sol", "kimi:kimi-k3",
                        "claude:claude-haiku-5-5", "ollama:qwen3:4b-instruct"]}
    assert rankings.fallbacks("coding", "claude", "opus", {"claude", "codex", "ollama"}, ranks) == [
        "codex:gpt-6-sol", "claude:claude-haiku-5-5", "ollama:qwen3:4b-instruct"]
    # the best one falls back through everything below it, up to five, the last of them free
    assert rankings.fallbacks("coding", "claude", SONNET, None, ranks) == [
        "claude:claude-opus-5-5", "codex:gpt-6-sol", "kimi:kimi-k3", "claude:claude-haiku-5-5",
        "ollama:qwen3:4b-instruct"]
    many = {"coding": [f"codex:gpt-{n}" for n in range(9)] + ["opencode:opencode/big-pickle-free", "ollama:qwen3"]}
    assert rankings.fallbacks("coding", "codex", "gpt-0", None, many) == [
        "codex:gpt-1", "codex:gpt-2", "codex:gpt-3", "codex:gpt-4", "opencode:opencode/big-pickle-free"]
    # the last one has nobody below it
    assert rankings.fallbacks("coding", "ollama", "qwen3:4b-instruct", None, ranks) == []
    # a model the ranking doesn't know falls back to the free and local ones only
    assert rankings.fallbacks("coding", "claude", "claude-mystery-9", None, ranks) == ["ollama:qwen3:4b-instruct"]
    # no model: what's below the agent's best
    assert rankings.fallbacks("coding", "codex", None, {"claude", "ollama"}, ranks) == [
        "claude:claude-haiku-5-5", "ollama:qwen3:4b-instruct"]


def test_answer_fallbacks_name_agents_once():
    ranks = {"writing": ["claude:a", "codex:b", "claude:c", "gemini:d", "local:"]}
    assert rankings.fallbacks("writing", "claude", None, None, ranks, agents_only=True) == ["codex", "gemini",
                                                                                           "local"]


def test_your_own_rankings_file_wins_and_a_broken_one_is_ignored(tmp_path, monkeypatch):
    own = tmp_path / "rankings.json"
    monkeypatch.setattr(rankings, "own_path", lambda: own)
    own.write_text(json.dumps({"updated": "2027-01-01", "ranks": {"coding": ["codex:gpt-7"]}}), encoding="utf-8")
    ranks, updated = rankings.load()
    assert ranks["coding"] == ["codex:gpt-7"] and ranks["video"] == rankings.RANKS["video"]
    assert updated == "2027-01-01"
    own.write_text(json.dumps({"ranks": {"coding": ["-rf"]}}), encoding="utf-8")
    assert rankings.load() == (rankings.RANKS, rankings.UPDATED)


def test_an_update_that_isnt_a_rankings_file_changes_nothing(tmp_path, monkeypatch):
    own = tmp_path / "rankings.json"
    monkeypatch.setattr(rankings, "own_path", lambda: own)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"ranks": {"coding": ["$(rm -rf ~)"]}}), encoding="utf-8")
    with pytest.raises(rankings.RankingsError, match="nothing was changed"):
        rankings.update(bad.as_uri())
    assert not own.exists()
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"updated": "2026-11-01", "ranks": {"coding": ["claude:claude-opus-5-5"]}}),
                    encoding="utf-8")
    assert rankings.update(good.as_uri()) == "2026-11-01" and own.exists()


# ── Approvals ────────────────────────────────────────────────────────────────

def test_fallbacks_are_sealed(board):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], fallbacks=[f"claude:{SONNET}", "ollama:qwen3:4b-instruct"])
    [(_, approval)] = board.pending_runs("claude")
    key = approvals.load_key()
    assert approvals.is_sealed(key, board.path, t.id, approval.data, approval.at)
    # a board that adds a model (a dearer one, or another agent) breaks the seal
    for changed in ([f"claude:{FABLE}", "ollama:qwen3:4b-instruct"], [f"claude:{SONNET}"]):
        assert not approvals.is_sealed(key, board.path, t.id, {**approval.data, "fallbacks": changed}, approval.at)
    added = {**approval.data}
    added.pop("fallbacks")
    assert not approvals.is_sealed(key, board.path, t.id, added, approval.at)


@pytest.mark.parametrize("fallbacks", [[], ["a"] * 6, ["codex", "codex"], ["-rf"], ["codex:--yolo"], "codex", [1]])
def test_fallbacks_must_be_agents_and_models(board, fallbacks):
    t = task(board)
    with pytest.raises(BoardError, match="Fallbacks are"):
        board.approve(HUMAN, t.id, "claude", fallbacks=fallbacks)


def test_a_picture_has_no_fallbacks(board):
    t = task(board)
    with pytest.raises(BoardError, match="go on with another model"):
        board.approve(HUMAN, t.id, "grok", kind="image", fallbacks=["gpt"])


# ── A run that falls back ────────────────────────────────────────────────────

def test_a_model_out_of_usage_is_benched_and_the_next_one_finishes_the_run(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], fallbacks=[f"claude:{SONNET}", f"claude:{HAIKU}"])
    agent = Agent({SONNET: result("DONE: wrote calc.py", SONNET)}, out={OPUS}, actions={SONNET: write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok, ran.message
    assert [c["model"] for c in agent.calls] == [OPUS, SONNET]
    handback = board.events(t.id)[-1]
    assert f"{OPUS} hit its usage limit, so {SONNET} took over." in handback.data["left"]
    assert git(repo, "log", "-1", "--format=%B", "handoff/T-1").stdout.count(SONNET) >= 1
    assert board.get(t.id).status == "handed_off"
    assert ran.message == f"handed to human. {OPUS} hit its usage limit, so {SONNET} took over"


def test_each_model_out_of_usage_is_named(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], fallbacks=[f"claude:{SONNET}", f"claude:{HAIKU}"])
    agent = Agent({HAIKU: result("DONE: wrote calc.py", HAIKU)}, out={OPUS, SONNET}, actions={HAIKU: write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok and [c["model"] for c in agent.calls] == [OPUS, SONNET, HAIKU]
    assert f"{OPUS} and {SONNET} hit their usage limit, so {HAIKU} took over." in board.events(t.id)[-1].data["left"]


def test_when_every_model_is_out_of_usage_it_says_so(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], fallbacks=[f"claude:{SONNET}"])
    [ran] = worker(board, repo, Agent({}, out={OPUS, SONNET})).run_pending()
    assert not ran.ok and "hit their usage limit, and there was no other model to take over" in ran.message


def test_another_failure_doesnt_fall_back(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], fallbacks=[f"claude:{SONNET}"])
    agent = Agent({}, out={OPUS}, error="There's an issue with the selected model.")
    [ran] = worker(board, repo, agent).run_pending()
    assert not ran.ok and [c["model"] for c in agent.calls] == [OPUS]
    assert "There's an issue with the selected model." in ran.message


def test_a_run_with_no_fallbacks_stops_as_it_always_did(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS])
    agent = Agent({}, out={OPUS})
    [ran] = worker(board, repo, agent).run_pending()
    assert not ran.ok and [c["model"] for c in agent.calls] == [OPUS] and "usage limit" in ran.message


PLAN = {"steps": [{"model": OPUS, "title": "write calc.py", "instructions": "Write calc.py with add(a, b)."},
                  {"model": HAIKU, "title": "check it", "instructions": "Check calc.py."}]}


def test_a_chain_goes_on_when_a_step_model_is_out_of_usage(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, OPUS, HAIKU], fallbacks=[f"claude:{SONNET}"])
    plan = result(f"The plan.\n```json\n{json.dumps(PLAN)}\n```", FABLE, 0.5)
    agent = Agent({FABLE: plan, SONNET: result("DONE: wrote calc.py", SONNET),
                   HAIKU: result("DONE: checked it\nLEFT: nothing", HAIKU)}, out={OPUS}, actions={SONNET: write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok, ran.message
    assert [c["model"] for c in agent.calls] == [FABLE, OPUS, SONNET, HAIKU]
    handback = board.events(t.id)[-1]
    assert f"Step 1 of 2, by {SONNET}" in handback.data["done"]
    assert f"{OPUS} hit its usage limit, so {SONNET} took over." in handback.data["left"]
    log = git(repo, "log", "--format=%s|%b", "handoff/T-1", "-1").stdout
    assert SONNET in log and OPUS not in log


def test_a_benched_model_stays_benched_for_the_rest_of_the_chain(board, repo):
    t = task(board)
    plan = {"steps": [{"model": OPUS, "title": "one", "instructions": "Write calc.py."},
                      {"model": OPUS, "title": "two", "instructions": "Check it."}]}
    board.approve(HUMAN, t.id, "claude", models=[FABLE, OPUS], fallbacks=[f"claude:{SONNET}"])
    agent = Agent({FABLE: result(f"```json\n{json.dumps(plan)}\n```", FABLE),
                   SONNET: result("DONE: did it", SONNET)}, out={OPUS}, actions={SONNET: write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok, ran.message
    assert [c["model"] for c in agent.calls] == [FABLE, OPUS, SONNET, SONNET]


def test_a_planner_out_of_usage_hands_the_plan_to_the_next_planner(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, HAIKU], fallbacks=[f"claude:{OPUS}"])
    plan = {"steps": [{"model": HAIKU, "title": "write it", "instructions": "Write calc.py."}]}
    agent = Agent({OPUS: result(f"```json\n{json.dumps(plan)}\n```", OPUS),
                   HAIKU: result("DONE: wrote calc.py", HAIKU)}, out={FABLE}, actions={HAIKU: write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok, ran.message
    assert [c["model"] for c in agent.calls] == [FABLE, OPUS, HAIKU]
    assert board.events(t.id)[-1].data["done"].startswith(f"{OPUS} planned 1 step")


# ── Answers and reviews, and the app ─────────────────────────────────────────

def test_the_api_says_who_takes_over_and_the_rankings(board, repo, monkeypatch):
    from handoff import cli
    monkeypatch.setattr(cli, "suggested_fallbacks", lambda kind, agent, models, root: [f"claude:{SONNET}", "codex"])
    def call(op, **args):
        reply, _ = api.handle(json.dumps({"schema": 1, "op": op, "project": str(repo), "args": args}).encode())
        assert reply["ok"], reply
        return reply

    reply = call("fallbacks", agent="claude", models=[OPUS])
    assert reply["data"]["fallbacks"] == [f"claude:{SONNET}", "codex"]
    assert reply["data"]["words"] == f"{SONNET}, then codex"
    ranked = call("rankings")
    assert ranked["data"]["ranks"]["coding"][0] == rankings.RANKS["coding"][0]
    assert "video" in ranked["data"]["jobs"]


def test_an_agent_you_havent_enabled_doesnt_take_over(board, repo, monkeypatch):
    from handoff import adapters
    from handoff import worker as worker_module
    monkeypatch.setitem(worker_module.AGENTS, "kimi", adapters.presets()["kimi"])  # (as if kimi were installed)
    monkeypatch.setattr(adapters, "enabled", lambda name: False)
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], fallbacks=["kimi:kimi-k3"])
    w = worker(board, repo, Agent({}, out={OPUS}))
    monkeypatch.setattr(Worker, "check", lambda self: None)
    [ran] = w.run_pending()
    assert not ran.ok and "no other model to take over" in ran.message
    assert "kimi-k3 couldn't take over" in ran.message and "handoff agents enable kimi" in ran.message
