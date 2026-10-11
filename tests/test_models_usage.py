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
from handoff.describe import describe
from handoff.worker import PLANNER_TOOLS, Worker, WorkerUnavailable, parse_plan

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
        effort = argv[argv.index("--effort") + 1] if "--effort" in argv else None
        self.calls.append({"argv": argv, "stdin": stdin, "model": model, "effort": effort})
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


def test_a_chain_that_stops_on_a_secret_says_what_the_steps_before_it_did(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, OPUS, HAIKU])

    def leak(folder, _stdin=""):
        (folder / "config.py").write_text("KEY = 'sk-proj-" + "a" * 40 + "'\n", encoding="utf-8")
    agent = Agent({FABLE: plan_reply(), OPUS: result("DONE: wrote calc.py", OPUS), HAIKU: result("DONE: tests", HAIKU)},
                  {OPUS: write_calc, HAIKU: leak})
    [ran] = worker(board, repo, agent).run_pending()
    assert not ran.ok and "Step 2 of 2 (claude-haiku-5-5: test it)" in ran.message
    assert "looks like a secret: config.py" in ran.message
    assert "The steps before it finished. What they changed is committed on handoff/T-1, up to" in ran.message
    assert f"Step 1 of 2, by {OPUS} (about $0.0100): write calc.py\nwrote calc.py" in ran.message
    # and that's what the board keeps, for handoff show and the Board
    reason = [e for e in board.events(t.id) if e.kind == "status"][-1].text
    assert f"Step 1 of 2, by {OPUS}" in reason and git(repo, "show", "handoff/T-1:calc.py").stdout


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


# ── Effort ───────────────────────────────────────────────────────────────────

def test_each_models_effort_is_sealed(board):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, HAIKU], efforts={FABLE: "max", HAIKU: "low"})
    [(_, approval)] = board.pending_runs("claude")
    assert approval.data["efforts"] == {FABLE: "max", HAIKU: "low"}
    key = approvals.load_key()
    assert approvals.is_sealed(key, board.path, t.id, approval.data, approval.at)
    # a board can't turn a model up, or drop the levels the person picked
    for efforts in ({FABLE: "max", HAIKU: "max"}, {FABLE: "max"}):
        assert not approvals.is_sealed(key, board.path, t.id, {**approval.data, "efforts": efforts}, approval.at)
    dropped = {k: v for k, v in approval.data.items() if k != "efforts"}
    assert not approvals.is_sealed(key, board.path, t.id, dropped, approval.at)
    assert "as a chain (claude-fable-5-1 (max effort) plans; claude-haiku-5-5 (low effort) do the steps)" in \
        describe(approval)


@pytest.mark.parametrize("models,efforts", [([OPUS], {HAIKU: "high"}), (None, {OPUS: "high"}), ([OPUS], {"": "high"}),
                                            ([OPUS], {OPUS: "very high"}), ([OPUS], {}), ([OPUS], {OPUS: 3}),
                                            ([OPUS], "high")])
def test_each_effort_is_one_word_for_one_of_the_runs_models(board, models, efforts):
    t = task(board)
    with pytest.raises(BoardError, match="Each effort is one word"):
        board.approve(HUMAN, t.id, "claude", models=models, efforts=efforts)


def test_a_run_on_a_model_thinks_at_its_effort(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], efforts={OPUS: "xhigh"})
    agent = Agent({OPUS: result("DONE: wrote calc.py", OPUS)}, {OPUS: write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok and agent.calls[0]["effort"] == "xhigh"
    assert f"(claude, {OPUS}, xhigh effort)" in git(repo, "log", "-1", "--format=%B", "handoff/T-1").stdout


def test_the_agents_own_model_can_be_given_an_effort(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", efforts={"": "low"})
    agent = Agent({"default": "DONE: did it"}, {"default": write_calc})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok and "--model" not in agent.calls[0]["argv"] and agent.calls[0]["effort"] == "low"
    approval = [e for e in board.events(t.id) if e.kind == "approved"][0]
    assert describe(approval).startswith("approved it for the claude worker at low effort")


def test_each_model_in_a_chain_thinks_at_its_own_effort(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[FABLE, OPUS, HAIKU], efforts={FABLE: "max", OPUS: "high"})
    agent = Agent({FABLE: plan_reply(), OPUS: result("DONE: wrote calc.py", OPUS), HAIKU: result("DONE: tests", HAIKU)},
                  {OPUS: write_calc, HAIKU: write_test})
    [ran] = worker(board, repo, agent).run_pending()
    assert ran.ok, ran.message
    assert [(c["model"], c["effort"]) for c in agent.calls] == [(FABLE, "max"), (OPUS, "high"), (HAIKU, None)]
    planner = agent.calls[0]["stdin"]
    assert f"{OPUS} (high effort), {HAIKU}" in planner  # it knows how hard each one thinks
    assert "Read only what you need" in planner and "don't read the whole project" in planner
    assert f"Step 1 of 2, by {OPUS}, high effort (about $0.0100)" in board.events(t.id)[-1].data["done"]
    assert f"(claude, {OPUS}, high effort)" in git(repo, "log", "-1", "--format=%B", "handoff/T-1~1").stdout


def test_a_level_the_agent_doesnt_take_never_reaches_it(board, repo):
    t = task(board)
    board.approve(HUMAN, t.id, "claude", models=[OPUS], efforts={OPUS: "ultracode"})  # (one word, so a board takes it)
    agent = Agent({OPUS: result("DONE", OPUS)})
    [ran] = worker(board, repo, agent).run_pending()
    assert not ran.ok and agent.calls == []
    assert "can't think at ultracode effort: it takes low, medium, high, xhigh or max" in ran.message
    with pytest.raises(BoardError, match="claude takes effort low, medium, high, xhigh or max, not minimal"):
        api.op_approve({"project": str(repo), "args": {"task": t.ref, "agent": "claude", "models": [OPUS],
                                                         "efforts": {OPUS: "minimal"}}})


def test_each_agent_is_told_its_effort_its_own_way(board, repo):
    claude = worker(board, repo, Agent({}))
    assert claude.argv(model=OPUS, effort="max")[-4:] == ["--model", OPUS, "--effort", "max"]
    claude.help = "Usage: claude [options]\n  --model <model>\n"  # a Claude Code from before --effort
    with pytest.raises(WorkerUnavailable, match="has no --effort. Update it"):
        claude.argv(model=OPUS, effort="max")
    assert "--effort" not in claude.argv(model=OPUS)
    codex = Worker(board, repo, "codex", runner=Agent({}), command="codex")
    assert codex.argv(model="gpt-5.5", effort="xhigh")[-5:] == ["--model", "gpt-5.5", "-c",
                                                                  'model_reasoning_effort="xhigh"', "-"]
    with pytest.raises(WorkerUnavailable, match="codex worker can't think at max effort: it takes minimal"):
        codex.argv(effort="max")


def test_the_api_takes_efforts_and_says_what_each_worker_takes(board, repo):
    t = task(board)
    api.op_approve({"project": str(repo), "args": {"task": t.ref, "agent": "codex", "models": ["gpt-5.5"],
                                                     "efforts": {"gpt-5.5": "xhigh"}}})
    [(_, approval)] = board.pending_runs("codex")
    assert approval.data["efforts"] == {"gpt-5.5": "xhigh"}
    workers = api.task_detail(board, repo, t.id)["task"]["workers"]
    assert workers["claude"] == {"efforts": ["low", "medium", "high", "xhigh", "max"], "chain": True}
    assert workers["codex"] == {"efforts": ["minimal", "low", "medium", "high", "xhigh"], "chain": False}
    assert "run-effort" in api.op_hello({})["features"]
    with pytest.raises(api.ApiError, match="args.efforts must be an object"):
        api.op_approve({"project": str(repo), "args": {"task": t.ref, "agent": "claude", "efforts": "high"}})


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
