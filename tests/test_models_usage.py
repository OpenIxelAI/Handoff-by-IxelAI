"""Models and usage: an edit approved for a model, or a chain of models (the biggest plans, the others do the
steps), and what each run used, as Claude Code reports it.

Real git and a fake agent standing in for `claude -p --output-format json`, in Claude's edit-only mode.
"""
import json
import subprocess
from pathlib import Path

import pytest

from handoff import api, approvals, usage
from handoff.board import HUMAN, Board, BoardError
from handoff.worker import PLANNER_TOOLS, Worker, parse_plan

FABLE, OPUS, HAIKU = "claude-fable-5-1", "claude-opus-5-5", "claude-haiku-5-5"


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


def result(reply, model, cost=0.01, out=100, error=False):
    """Claude Code's JSON result for one run on `model`."""
    return json.dumps({"type": "result", "is_error": error, "result": reply, "total_cost_usd": cost,
                       "modelUsage": {model: {"inputTokens": 10, "outputTokens": out, "cacheReadInputTokens": 1000,
                                              "cacheCreationInputTokens": 0, "costUSD": cost}}})


class Agent:
    """Stands in for `claude -p`: answers by the --model it's given, and does what that model is told to."""

    def __init__(self, replies, actions=None, codes=None):
        self.replies, self.actions, self.codes, self.calls = replies, actions or {}, codes or {}, []

    def __call__(self, argv, stdin, env, timeout, cwd):
        model = argv[argv.index("--model") + 1] if "--model" in argv else "default"
        self.calls.append({"argv": argv, "stdin": stdin, "model": model})
        if model in self.actions:
            self.actions[model](Path(cwd), stdin)
        reply = self.replies[model]
        return subprocess.CompletedProcess(argv, self.codes.get(model, 0), reply() if callable(reply) else reply, "")


def worker(board, repo, agent):
    return Worker(board, repo, "claude", runner=agent, command="claude", sandboxed=False)


def task(board, title="Add a calculator"):
    made, _ = board.create(HUMAN, title, "Write calc.py with add and a test.", ["python -m pytest"])
    return made


def tools(argv):
    return argv[argv.index("--tools") + 1].split(",")


# ── Approvals ────────────────────────────────────────────────────────────────

def test_an_edit_can_be_approved_for_a_model_and_the_model_is_sealed(board):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS])
    [(_, approval)] = board.pending_runs("claude")
    assert approval.data["models"] == [OPUS]
    # a board that swaps the model (to a bigger, dearer one) breaks the seal
    data = {**approval.data, "models": [FABLE]}
    assert not approvals.is_sealed(approvals.load_key(), board.path, t.id, data, approval.at)
    assert approvals.is_sealed(approvals.load_key(), board.path, t.id, approval.data, approval.at)


@pytest.mark.parametrize("models", [[], ["Opus 5"], [OPUS, OPUS], ["a"] * 5, "opus", [1]])
def test_models_must_be_model_names(board, models):
    t = task(board)
    with pytest.raises(BoardError, match="model names"):
        board.approve(HUMAN, t.id, "claude", models=models)


def test_only_an_edit_names_its_models(board):
    t = task(board)
    with pytest.raises(BoardError, match="Only a change"):
        board.approve(HUMAN, t.id, "gemini", kind="answer", models=[OPUS])


# ── One model ────────────────────────────────────────────────────────────────

def write_calc(folder, _stdin=""):
    (folder / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def test_a_run_on_a_model_says_what_it_used(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[HAIKU])
    agent = Agent({HAIKU: result("DONE: wrote calc.py\nLEFT: nothing\nVERIFY: pytest", HAIKU, 0.0042, 321)},
                  {HAIKU: write_calc})
    [ran] = worker(board, repo, agent).run_pending()

    assert ran.ok and agent.calls[0]["model"] == HAIKU
    assert ran.used == {"models": {HAIKU: {"in": 10, "out": 321, "cache_read": 1000, "cache_write": 0,
                                           "cost": 0.0042}}}
    finished = [e for e in board.events(t.id) if e.kind == "worker" and e.data["state"] == "finished"][0]
    assert finished.data["usage"] == ran.used
    handoff = board.events(t.id)[-1]
    assert handoff.data["done"] == "wrote calc.py"  # the reply, read out of the JSON
    assert f"{HAIKU}: 10 in, 321 out, 1.0k cached · $0.0042" in handoff.data["left"]
    detail = api.task_detail(board, repo, t.id)
    assert detail["task"]["cost"] == 0.0042 and HAIKU in detail["task"]["usage"]["models"]
    assert git(repo, "log", "-1", "--format=%B", "handoff/T-1").stdout.count(HAIKU) == 1


def test_a_run_with_no_model_uses_the_agents_own(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude")
    agent = Agent({"default": "DONE: did it"}, {"default": write_calc})  # (plain text: no usage)
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok and "--model" not in agent.calls[0]["argv"] and ran.used is None


def test_an_error_the_agent_reports_is_the_reason_and_its_usage_is_kept(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS])
    agent = Agent({OPUS: result("There's an issue with the selected model.", OPUS, 0.0, 0, error=True)},
                  codes={OPUS: 1})
    [ran] = worker(board, repo, agent).run_pending()
    assert not ran.ok and "There's an issue with the selected model." in ran.message
    assert board.get(t.id).status == "blocked"


# ── A chain ──────────────────────────────────────────────────────────────────

PLAN = {"steps": [{"model": OPUS, "title": "write calc.py", "instructions": "Write calc.py with add(a, b)."},
                  {"model": HAIKU, "title": "test it", "instructions": "Write test_calc.py for add."}]}


def plan_reply(plan=PLAN):
    return result(f"Here's the plan.\n```json\n{json.dumps(plan)}\n```", FABLE, 0.66, 5000)


def write_test(folder, _stdin=""):
    (folder / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                                         encoding="utf-8")


def test_a_chain_plans_with_the_biggest_and_trickles_the_steps_down(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, OPUS, HAIKU])
    agent = Agent({FABLE: plan_reply(),
                   OPUS: result("DONE: wrote calc.py\nVERIFY: python -c 'import calc'", OPUS, 0.12, 3000),
                   HAIKU: result("DONE: wrote test_calc.py\nLEFT: nothing\nVERIFY: python -m pytest", HAIKU, 0.003)},
                  {OPUS: write_calc, HAIKU: write_test})
    [ran] = worker(board, repo, agent).run_pending()

    assert ran.ok, ran.message
    assert [c["model"] for c in agent.calls] == [FABLE, OPUS, HAIKU]
    planner = agent.calls[0]
    assert tools(planner["argv"]) == list(PLANNER_TOOLS) and "--allowedTools" not in planner["argv"]
    assert "Don't change any files yourself" in planner["stdin"] and f"{OPUS}, {HAIKU}" in planner["stdin"]
    # each step model sees the whole plan, and is told which step is its own
    assert "Do only step 2" in agent.calls[2]["stdin"] and "Write calc.py with add(a, b)." in agent.calls[2]["stdin"]
    assert "Write" in tools(agent.calls[1]["argv"])
    # one commit per step, saying which model made it
    log = git(repo, "log", "--format=%s|%b", "handoff/T-1", "-2").stdout
    assert "T-1 step 2/2: test it" in log and HAIKU in log and "T-1 step 1/2: write calc.py" in log and OPUS in log
    assert set(git(repo, "show", "--name-only", "--format=", "handoff/T-1").stdout.split()) == {"test_calc.py"}
    # what each model used, and the total
    assert set(ran.used["models"]) == {FABLE, OPUS, HAIKU}
    assert usage.total_cost(ran.used) == pytest.approx(0.783)
    handoff = board.events(t.id)[-1]
    assert handoff.data["done"].startswith(f"{FABLE} planned 2 steps.")
    assert f"Step 2 of 2, by {HAIKU} (about $0.0030): test it" in handoff.data["done"]
    assert handoff.data["left"].startswith("nothing") and "Total: $0.7830" in handoff.data["left"]
    assert sorted(handoff.data["files"]) == ["calc.py", "test_calc.py"]


def test_a_plan_the_worker_cant_read_gives_the_whole_task_to_the_next_model(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, OPUS, HAIKU])
    agent = Agent({FABLE: result("I'd just write it.", FABLE), OPUS: result("DONE: all of it", OPUS)},
                  {OPUS: write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok and [c["model"] for c in agent.calls] == [FABLE, OPUS]
    assert "didn't write a plan the worker could read" in board.events(t.id)[-1].data["left"]


def test_a_failed_step_stops_the_chain_and_keeps_what_came_before(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, OPUS, HAIKU])
    agent = Agent({FABLE: plan_reply(), OPUS: result("DONE: wrote calc.py", OPUS),
                   HAIKU: result("Rate limited.", HAIKU, 0.0, 0, error=True)},
                  {OPUS: write_calc}, codes={HAIKU: 1})
    [ran] = worker(board, repo, agent).run_pending()
    assert not ran.ok and "Step 2 of 2" in ran.message and "Rate limited." in ran.message
    assert "committed on handoff/T-1" in ran.message
    assert git(repo, "show", "handoff/T-1:calc.py").stdout.startswith("def add")
    failed = [e for e in board.events(t.id) if e.kind == "worker" and e.data["state"] == "failed"][0]
    assert set(failed.data["usage"]["models"]) == {FABLE, OPUS, HAIKU}


def test_a_chain_runs_on_claude_only(board, repo):
    t = task(board)
    with pytest.raises(BoardError, match="chain of models runs on claude"):
        api.op_approve({"project": str(repo), "args": {"task": t.ref, "agent": "codex",
                                                         "models": [OPUS, HAIKU]}})


def test_parse_plan():
    text = f"plan:\n```json\n{json.dumps(PLAN)}\n```"
    steps = parse_plan(text, [OPUS, HAIKU])
    assert [(s.model, s.title) for s in steps] == [(OPUS, "write calc.py"), (HAIKU, "test it")]
    # a model that isn't one of the chain's goes to the biggest of them
    odd = {"steps": [{"model": "gpt-9", "title": "x", "instructions": "do x"}]}
    assert parse_plan(f"```json\n{json.dumps(odd)}\n```", [OPUS, HAIKU])[0].model == OPUS
    assert parse_plan("no plan here", [OPUS]) is None
    assert parse_plan('```json\n{"steps": []}\n```', [OPUS]) is None
    many = {"steps": [{"model": HAIKU, "title": str(n), "instructions": "x"} for n in range(20)]}
    assert len(parse_plan(f"```json\n{json.dumps(many)}\n```", [HAIKU])) == 6


# ── Usage ────────────────────────────────────────────────────────────────────

def test_usage_read_back_from_a_board_is_checked():
    assert usage.check({"models": {"bad name!": {"in": 1}}}) is None
    assert usage.check({"models": {OPUS: {"in": -5, "out": "lots", "cost": float("nan")}}}) == {
        "models": {OPUS: {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}}}
    assert usage.check("nope") is None


def test_parse_claude():
    reply, used, error = usage.parse_claude(result("hi", HAIKU, 0.5))
    assert reply == "hi" and error is None and used["models"][HAIKU]["cost"] == 0.5
    assert usage.parse_claude("plain text") == ("plain text", None, None)
    assert usage.parse_claude(result("bad model", HAIKU, error=True))[2] == "bad model"
