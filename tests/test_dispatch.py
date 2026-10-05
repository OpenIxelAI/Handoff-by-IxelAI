"""`handoff dispatch`, `handoff run` and `handoff agents`: one request split across agents, run side by side.

A fake `ixel` stands in for Ixel MAT (ixel ask, ixel image) and a fake `claude` for the edit worker,
so this runs anywhere; the edit worker itself is covered in test_worker.py.
"""
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from rich.console import Console

from handoff import asks, cli, crew, sandbox
from handoff.board import HUMAN, Board, BoardError, run_kind
from test_worker import _fake_claude, _no_sandbox, git

REQUEST = ("/handoff all codex i need you to review, grok generate photos based on my work, claude finish the "
           "next step and gemini or whoever make me a list of projects to check out")
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")
AGENTS = [
    {"name": "claude", "label": "Claude", "model": "model-c", "ready": True, "images": None},
    {"name": "codex", "label": "Codex", "model": "", "ready": True, "images": None},
    {"name": "gemini", "label": "Gemini", "model": "model-g", "ready": True, "images": None},
    {"name": "grok", "label": "Grok", "model": "model-x", "ready": True, "images": "xai"},
    {"name": "gpt", "label": "GPT", "model": "model-o", "ready": False, "images": "openai"},
]
PROVIDERS = [
    {"name": "xai", "label": "xAI (Grok)", "model": "grok-imagine-image-2.0", "ready": True},
    {"name": "openai", "label": "OpenAI", "model": "gpt-image-1", "ready": False, "why": "no OPENAI_API_KEY"},
]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# Shop\nA corner shop's till, in the browser.\n", encoding="utf-8")
    (root / ".gitignore").write_text(".handoff/\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def fake_ixel(tmp_path, monkeypatch):
    """A stand-in `ixel` on PATH: lists AGENTS and PROVIDERS, answers `ask`, saves pictures for `image`, and
    logs every call. FAKE_IXEL_ASK=error makes `ask` fail the way Ixel does; FAKE_IXEL_UNSAVED=1 makes `image`
    report pictures it never wrote; FAKE_IXEL_OLD=1 is an Ixel MAT whose `ask` can't read new files."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "ixel.jsonl"
    script = bin_dir / "fake_ixel.py"
    script.write_text(textwrap.dedent(f'''
        import json, os, sys
        args = sys.argv[1:]
        stdin = sys.stdin.read()
        with open({str(log)!r}, "a", encoding="utf-8") as f:
            f.write(json.dumps({{"argv": args, "stdin": stdin, "cwd": os.getcwd(),
                                 "depth": os.environ.get("HANDOFF_DEPTH")}}) + "\\n")
        def opt(name, default=None):
            return args[args.index(name) + 1] if name in args else default
        if args[:2] == ["ask", "--list"]:
            listed = {{"agents": {AGENTS!r}}}
            if not os.environ.get("FAKE_IXEL_OLD"):
                listed["features"] = ["new-files", "head"]
            print(json.dumps(listed))
        elif args[:2] == ["image", "--list"]:
            print(json.dumps({{"providers": {PROVIDERS!r}}}))
        elif args[0] == "ask":
            if os.environ.get("FAKE_IXEL_ASK") == "error":
                print(json.dumps({{"error": "Couldn't connect to Gemini: 401 Unauthorized"}}))
                sys.exit(1)
            agent = opt("--agent")
            answer = os.environ.get("FAKE_IXEL_ANSWER") or f"{{agent}} says: 1. Textual 2. htmx 3. Litestream"
            result = {{"agent": agent, "label": agent.capitalize(), "model": "m-" + agent, "answer": answer, "ms": 5}}
            if "--new-files" in args:
                result["notes"] = ["New files left out: auth.json (its name says it holds keys or logins)."]
            print(json.dumps(result))
        elif args[0] == "image":
            out = os.environ.get("FAKE_IXEL_OUT") or opt("--out")
            os.makedirs(out, exist_ok=True)
            files = []
            for n in range(int(opt("-n", "1"))):
                path = os.path.abspath(os.path.join(out, f"{{opt('--name')}}-{{n + 1}}.png"))
                if not os.environ.get("FAKE_IXEL_UNSAVED"):  # says it saved them, but didn't
                    with open(path, "wb") as f:
                        f.write(bytes.fromhex({PNG.hex()!r}))
                files.append(path)
            result = {{"provider": opt("--provider"), "label": "xAI (Grok)", "model": "grok-imagine-image-2.0",
                       "prompt": "A cosy corner shop at dusk, warm light.", "files": files}}
            if "-f" in args:
                result["writer"] = {{"name": "grok", "label": "Grok"}}
            print(json.dumps(result))
    '''), encoding="utf-8")
    if sys.platform == "win32":
        (bin_dir / "ixel.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        wrapper = bin_dir / "ixel"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    _fake_claude(bin_dir, tmp_path / "claude.log")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)
    monkeypatch.delenv("IXEL_PANEL_DEPTH", raising=False)
    monkeypatch.setattr(sandbox, "check", _no_sandbox)  # claude runs edit-only and codex can't edit, anywhere

    def calls(kind=None):
        found = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        return [c for c in found if kind is None or (c["argv"][0] == kind and "--list" not in c["argv"])]
    return calls


@pytest.fixture
def run_cli(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "console", Console(width=200, highlight=False))
    monkeypatch.setattr(cli, "_interactive", lambda: False)

    def run(*args):
        code = cli.main(list(args))
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


def by_agent(board):
    """Each task by the agent it was added for (once handed back, its assignee is you)."""
    return {board.events(t.id)[0].data["assignee"]: t for t in board.tasks(statuses=None)}


# ── Splitting ────────────────────────────────────────────────────────────────

NAMES = {"claude", "codex", "gemini", "grok", "gpt"}


def test_split_the_owners_request():
    context, parts = crew.split(REQUEST, NAMES)
    assert context == ""
    assert [(p.word, p.text) for p in parts] == [
        ("codex", "review"), ("grok", "generate photos based on my work"), ("claude", "finish the next step"),
        ("gemini", "make me a list of projects to check out")]


@pytest.mark.parametrize("request_text,context,parts", [
    ("Before the launch: codex, review my changes; gemini summarize README.md",
     "Before the launch", [("codex", "review my changes"), ("gemini", "summarize README.md")]),
    ("claude fix the login bug then codex review it", "", [("claude", "fix the login bug"), ("codex", "review it")]),
    ("Gemini: can you please list five ideas.\nGrok, draw a logo", "",
     [("gemini", "list five ideas"), ("grok", "draw a logo")]),
    # a name inside a sentence stays part of it
    ("claude compare your plan with codex's notes", "", [("claude", "compare your plan with codex's notes")]),
    ("compare claude and codex", "compare claude", [("codex", "")]),
    ("write me a poem", "write me a poem", []),
])
def test_split(request_text, context, parts):
    found_context, found = crew.split(request_text, NAMES)
    assert found_context == context and [(p.word, p.text) for p in found] == parts


@pytest.mark.parametrize("text,can_edit,kind", [
    ("review", True, "review"),
    ("review my changes", True, "review"),
    ("review the update", True, "review"),
    ("review and fix what's wrong", True, "edit"),
    ("finish the next step", True, "edit"),
    ("fix the login bug", False, "answer"),
    ("generate photos based on my work", False, "image"),
    ("draw a logo for the site", True, "image"),
    ("make me a list of projects to check out", True, "answer"),
    # a picture word in an everyday edit or review isn't a picture to make
    ("fix the image upload bug", True, "edit"),
    ("fix the broken icons in the header", True, "edit"),
    ("update the banner text", True, "edit"),
    ("build the docker image", True, "edit"),
    ("add a thumbnail cache", True, "edit"),
    ("fix the render loop", True, "edit"),
    ("review the image resizing code", False, "review"),
    ("fix the logo alignment", False, "answer"),
    # a picture word that's part of something in code (a paid picture run with --yes, otherwise)
    ("create a docker image for the api", True, "edit"),
    ("make a logo component in React", True, "edit"),
    ("create an image upload endpoint", True, "edit"),
    ("generate an image sitemap", True, "edit"),
    # a list about the code is an answer, not a change to it
    ("make a list of files that import board.py", True, "answer"),
    ("make me a summary of the config files", True, "answer"),
    ("write a report on the api routes", True, "answer"),
    # but a list or table that's part of the code is still made in code
    ("create a table component", True, "edit"),
    ("make a report page", True, "edit"),
    ("generate a summary endpoint", True, "edit"),
    ("write a description field", True, "edit"),
    ("create an overview page", True, "edit"),
    ("create a docker image for the api", False, "answer"),
    ("make a logo component in React", False, "answer"),
    ("generate an image sitemap", False, "answer"),
    ("make a picture file picker", False, "answer"),
    # what a picture request sounds like
    ("make 2 pictures of my app", True, "image"),
    ("create an image of a corner shop", True, "image"),
    ("make me a wallpaper", False, "image"),
    ("generate three icons for the toolbar", False, "image"),
    ("photos of a beach", False, "image"),
    ("a logo", False, "image"),
    ("draw a cat", False, "image"),
    ("make a logo for my React app", False, "image"),
    ("create an image of the api's architecture", True, "image"),
    ("design a poster for the launch", True, "image"),
])
def test_guess_kind(text, can_edit, kind):
    member = crew.Member("x", "X", {"edit": ""} if can_edit else {"answer": ""})
    assert crew.guess_kind(text, member) == kind


@pytest.mark.parametrize("hostile", ["make " * 800, "make " + "a" * 4000, "make a b c " * 360,
                                     "draw " + "x- " * 1300, "make " + "the " * 990 + "image"],
                         ids=["makes", "one-long-word", "short-words", "dashes", "determiners"])
def test_guessing_the_kind_stays_quick_on_any_request(hostile):
    import time
    started = time.perf_counter()
    for _ in range(10):
        crew.guess_kind(hostile[:crew.MAX_REQUEST_CHARS], None)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("text,count", [
    ("generate photos based on my work", 2), ("make 3 pictures", 3), ("draw a logo", 1), ("a poster", 1),
    ("make 40 wallpapers", 4), ("a couple of icons", 2), ("an image of the shop", 1),
])
def test_picture_count(text, count):
    assert asks.picture_count(text) == count


def test_the_plan_says_which_model_reads_the_files_a_picture_is_based_on(repo):
    # grok has no chat key here, so another model reads the README: the plan says so, and the run uses it
    ixel = asks.IxelRoster(agents=[{"name": "grok", "label": "Grok", "ready": False, "images": "xai"},
                                   {"name": "gemini", "label": "Gemini", "ready": True, "images": None}],
                           providers=[{"name": "xai", "label": "xAI (Grok)", "ready": True}])
    roster = crew.Roster({"grok": crew.Member("grok", "Grok", {"image": ""})}, ixel)
    _, [step] = crew.plan("grok make 2 pictures of my app", roster, repo)
    assert step.detail == "2 pictures, based on README.md, which Gemini reads"
    task = Board.open(repo).create(HUMAN, "Make 2 pictures of my app")[0]
    argv, _, _ = asks.command_for("ixel", repo, task, "image", "grok", ixel, Path("out"))
    assert argv[argv.index("--writer") + 1] == "gemini"
    nobody = asks.IxelRoster(agents=[], providers=ixel.providers)
    roster = crew.Roster({"grok": crew.Member("grok", "Grok", {"image": ""})}, nobody)
    _, [step] = crew.plan("grok make 2 pictures of my app", roster, repo)
    assert "needs a chat model to read them first" in step.problem
    _, [step] = crew.plan("grok make 2 pictures of a cat", roster, repo)  # nothing to read: no reader needed
    assert step.ok and step.detail == "2 pictures"


def test_claude_reviews_through_claude_code_in_ixel(repo):
    """"claude" is the claude that edits, and Claude Code in Ixel MAT (claude_code) answers and reviews."""
    ixel = asks.IxelRoster(agents=[{"name": "claude_code", "label": "Claude Code", "ready": True}], providers=[])
    members = {"claude": crew.Member("claude", "Claude", {"edit": ""}),
               "claude_code": crew.Member("claude_code", "Claude Code", {"answer": "", "review": ""})}
    roster = crew.Roster(members, ixel)
    (repo / "README.md").write_text("# Shop, changed\n", encoding="utf-8")
    _, steps = crew.plan("claude review my changes, claude explain README.md, claude fix the footer", roster, repo)
    assert [(s.agent, s.kind, s.ok) for s in steps] == [("claude_code", "review", True),
                                                       ("claude_code", "answer", True), ("claude", "edit", True)]
    assert steps[0].label == "Claude Code"


def test_a_review_of_a_named_file_reads_that_file(repo, fake_ixel, run_cli):
    # nothing has changed since the last commit: the review is of README.md itself
    code, out, _ = run_cli("dispatch", "--plan", "--json", "grok audit README.md")
    [step] = json.loads(out)["steps"]
    assert (step["kind"], step["detail"], step["problem"]) == ("review", "README.md", "")
    code, out, _ = run_cli("dispatch", "--yes", "grok audit README.md")
    assert code == 0, out
    [ask] = fake_ixel("ask")
    assert ask["argv"] == ["ask", "--agent", "grok", "--json", "-f", "README.md", "-"]
    assert "The files to review are attached (README.md)" in ask["stdin"]


def test_files_a_run_reads(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "intro.md").write_text("hi", encoding="utf-8")
    assert asks.named_files("summarize README.md and docs/intro.md, not missing.md or ../etc/passwd", repo) == [
        "README.md", "docs/intro.md"]
    assert asks.picture_files("pictures based on my work", repo) == ["README.md"]
    assert asks.picture_files("pictures from docs/intro.md", repo) == ["docs/intro.md"]
    assert asks.picture_files("a picture of a cat", repo) == []
    (repo / "README.md").unlink()
    with pytest.raises(asks.RunProblem, match="no README"):
        asks.picture_files("make pictures of my app", repo)
    assert asks.named_files(f"read {'a' * 300}.md and docs/intro.md", repo) == ["docs/intro.md"]  # too long a name


def test_a_file_name_too_long_for_the_system_is_just_not_a_file(repo, fake_ixel, run_cli):
    code, out, err = run_cli("dispatch", "--plan", f"gemini summarize {'a' * 300}.md")
    assert code == 0 and "Traceback" not in out + err
    outputs = Path(".handoff") / "outputs" / "T-1"
    (repo / outputs).mkdir(parents=True)
    with pytest.raises(asks.RunProblem, match="there's no picture there"):
        asks._pictures({"files": [str(outputs / ("a" * 300 + ".png"))]}, repo, outputs, "")


# ── The plan ─────────────────────────────────────────────────────────────────

def test_agents_says_who_can_do_what(repo, fake_ixel, run_cli):
    code, out, _ = run_cli("agents", "--json")
    assert code == 0
    agents = {a["name"]: a for a in json.loads(out)["agents"]}
    assert agents["claude"]["kinds"] == {"edit": "", "answer": "", "review": ""}
    assert "edits without running commands here" in agents["claude"]["notes"]["edit"]
    assert agents["codex"]["kinds"]["edit"] and agents["codex"]["kinds"]["review"] == ""  # no sandbox: no edits
    assert agents["grok"]["kinds"] == {"answer": "", "review": "", "image": ""}
    assert agents["gpt"]["kinds"]["image"] == "no OPENAI_API_KEY" and "no API key" in agents["gpt"]["kinds"]["answer"]
    code, out, _ = run_cli("agents")
    assert code == 0 and "grok  answers, reviews, makes pictures" in out  # no "grok Grok"
    assert 'handoff dispatch "claude review my changes, codex make me a list of …"' in out
    assert not (repo / ".handoff").exists()  # looking makes no board


def test_the_plan_shows_every_part_and_adds_nothing(repo, fake_ixel, run_cli):
    (repo / "README.md").write_text("# Shop\nNow with receipts.\n", encoding="utf-8")  # something to review
    code, out, _ = run_cli("dispatch", "--plan", "--json", REQUEST + ", and deepseek write a poem")
    assert code == 0
    steps = json.loads(out)["steps"]
    assert [(s["agent"], s["kind"], s["detail"]) for s in steps] == [
        ("codex", "review", "your uncommitted changes"),
        ("grok", "image", "2 pictures, based on README.md, which Grok reads"),
        ("claude", "edit", ""),
        ("gemini", "answer", ""),
        ("deepseek", "answer", "")]
    assert [bool(s["problem"]) for s in steps] == [False, False, False, False, True]
    assert "deepseek isn't set up here" in steps[4]["problem"]
    assert steps[0]["title"] == "Review your uncommitted changes"
    assert not (repo / ".handoff" / "board.db").exists() or Board.open(repo).tasks() == []
    assert fake_ixel("ask") == [] and fake_ixel("image") == []


def test_dispatch_needs_a_yes(repo, fake_ixel, run_cli):
    code, _, err = run_cli("dispatch", "gemini make me a list of projects")
    assert code == 1 and "Add --yes" in err
    assert Board.open(repo).tasks() == []


def test_dispatch_explains_a_request_with_no_names(repo, fake_ixel, run_cli):
    code, _, err = run_cli("dispatch", "--yes", "make me a list of projects")
    assert code == 1 and "Start each part with who does it" in err


# ── Running ──────────────────────────────────────────────────────────────────

def test_dispatch_runs_every_part_side_by_side(repo, fake_ixel, run_cli):
    (repo / "README.md").write_text("# Shop\nA corner shop's till, in the browser. Now with receipts.\n",
                                    encoding="utf-8")
    code, out, err = run_cli("dispatch", "--yes", REQUEST)
    assert code == 0, out + err
    board = Board.open(repo)
    tasks = by_agent(board)
    assert sorted(tasks) == ["claude", "codex", "gemini", "grok"]
    assert all(t.status == "handed_off" and t.assignee == HUMAN for t in tasks.values())

    # codex reviewed the uncommitted change, answer-only, through Ixel
    [review] = [c for c in fake_ixel("ask") if c["argv"][2] == "codex"]
    assert review["argv"] == ["ask", "--agent", "codex", "--json", "--diff", "-"]
    assert "Find bugs, security problems" in review["stdin"] and "your uncommitted changes" in review["stdin"]
    assert review["depth"] == "1" and Path(review["cwd"]).resolve() == repo.resolve()

    # grok made two pictures from the README, saved with the task
    [image] = fake_ixel("image")
    ref = tasks["grok"].ref
    assert image["argv"] == ["image", "--provider", "xai", "--json", "-n", "2", "--out", f".handoff/outputs/{ref}",
                             "--name", ref, "-f", "README.md", "--writer", "grok", "-"]
    assert image["stdin"] == "Generate photos based on my work"
    pictures = sorted((repo / ".handoff" / "outputs" / ref).iterdir())
    assert [p.name for p in pictures] == [f"{ref}-1.png", f"{ref}-2.png"]
    handoff = [e for e in board.events(tasks["grok"].id) if e.kind == "handoff"][-1].data
    assert handoff["files"] == [f".handoff/outputs/{ref}/{ref}-1.png", f".handoff/outputs/{ref}/{ref}-2.png"]
    assert "Grok described it: A cosy corner shop" in handoff["done"]

    # gemini answered; the full answer is kept next to the board
    gemini = tasks["gemini"]
    assert (repo / ".handoff" / "outputs" / gemini.ref / "answer.md").read_text(encoding="utf-8").startswith(
        "gemini says: 1. Textual")
    last = [e for e in board.events(gemini.id) if e.kind == "handoff"][-1]
    assert last.data["done"].startswith("gemini says:") and "nothing in the project changed" in last.data["left"]

    # claude changed files on its own branch
    claude = tasks["claude"]
    assert git(repo, "show", f"handoff/{claude.ref}:from_claude.txt").stdout == "hello\n"
    assert f"{claude.ref} claude: handed back to you; the work is on handoff/{claude.ref}" in out
    # every approval was for one kind of run, and each was used once
    assert {name: run_kind([e for e in board.events(t.id) if e.kind == "approved"][-1])
            for name, t in tasks.items()} == {"codex": "review", "grok": "image", "claude": "edit", "gemini": "answer"}
    assert board.pending_runs() == []


def test_runs_really_happen_at_the_same_time(repo):
    board = Board.open(repo)
    pairs = []
    for name in ("gemini", "grok", "gpt"):
        task, _ = board.create(HUMAN, f"Ideas from {name}", assignee=name)
        board.approve(HUMAN, task.id, name, kind="answer")
        pairs += board.pending_runs(name)
    together = threading.Barrier(3, timeout=10)  # breaks unless all three runs are going at once

    def runner(argv, stdin, env, timeout, cwd):
        together.wait()
        return subprocess.CompletedProcess(argv, 0, json.dumps({"agent": argv[3], "answer": "ok"}), "")

    original = asks.find_ixel
    asks.find_ixel = lambda: "ixel"
    try:
        results = crew.run_all(board, repo, pairs, runner=runner)
    finally:
        asks.find_ixel = original
    assert [r.ok for r in results] == [True, True, True], [r.message for r in results]


def test_ctrl_c_stops_every_run_and_each_one_says_so(repo, monkeypatch):
    # Real programs, stopped the way Ctrl+C stops them; a few rounds, since what went wrong was a race
    import _thread
    import time

    from handoff import proc
    monkeypatch.setattr(asks, "find_ixel", lambda: "ixel")
    board = Board.open(repo)
    slow = [sys.executable, "-c", "import time; time.sleep(60)"]

    def runner(argv, stdin, env, timeout, cwd):
        if "late" in argv:  # reaches its program only after Ctrl+C
            while not proc.stopping():
                time.sleep(0.01)
        return proc.run_tree(slow, stdin, env, timeout, cwd)

    def press_ctrl_c(running):
        deadline = time.monotonic() + 20
        while len(proc._running) < running and time.monotonic() < deadline:
            time.sleep(0.01)
        _thread.interrupt_main()

    try:
        for _ in range(3):
            pairs = []
            for name in ("gemini", "grok", "gpt", "mistral", "late"):
                task, _ = board.create(HUMAN, f"Ideas from {name}", assignee=name)
                board.approve(HUMAN, task.id, name, kind="answer")
                pairs += board.pending_runs(name)
            threading.Thread(target=press_ctrl_c, args=(4,), daemon=True).start()
            started = time.monotonic()
            with pytest.raises(KeyboardInterrupt):
                crew.run_all(board, repo, pairs, runner=runner)
            assert time.monotonic() - started < 15
            for task, _ in pairs:
                assert board.get(task.id).status == "blocked", task.ref  # none left in progress
                assert board.events(task.id)[-1].text == asks.STOPPED, task.ref
            assert not proc._running
    finally:
        proc.allow_runs()


def test_a_run_ctrl_c_got_to_first_keeps_its_approval(repo, monkeypatch):
    from handoff import proc
    monkeypatch.setattr(asks, "find_ixel", lambda: "ixel")
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Ideas", assignee="gemini")
    board.approve(HUMAN, task.id, "gemini", kind="answer")
    [(task, approval)] = board.pending_runs("gemini")
    proc.stop_all()
    try:
        result = asks.run(board, repo, task, approval, runner=lambda *a: pytest.fail("it started"))
    finally:
        proc.allow_runs()
    assert not result.ok and result.message.startswith(asks.NOT_STARTED) and "handoff run T-1" in result.message
    assert board.get(1).status == "open" and board.pending_runs("gemini")


def test_an_answer_the_board_refuses_still_hands_the_task_back(repo, monkeypatch):
    monkeypatch.setattr(asks, "find_ixel", lambda: "ixel")
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Ideas", assignee="gemini")
    board.approve(HUMAN, task.id, "gemini", kind="answer")
    [(task, approval)] = board.pending_runs("gemini")
    label = "sk-proj-" + "a" * 40  # a model's name that looks like a key: the board won't store it

    def runner(argv, stdin, env, timeout, cwd):
        return subprocess.CompletedProcess(argv, 0, json.dumps({"agent": "gemini", "label": label,
                                                                "answer": "ok"}), "")
    result = asks.run(board, repo, task, approval, runner=runner)
    assert not result.ok and board.get(1).status == "blocked"  # not in progress forever
    reason = board.events(1)[-1].text
    assert "didn't take the result" in reason and ".handoff/outputs/T-1" in reason and "sk-proj" not in reason


@pytest.mark.parametrize("problem", [KeyboardInterrupt, RuntimeError])
def test_an_ixel_run_stopped_or_crashed_is_never_left_in_progress(repo, monkeypatch, problem):
    monkeypatch.setattr(asks, "find_ixel", lambda: "ixel")
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Ideas", assignee="gemini")
    board.approve(HUMAN, task.id, "gemini", kind="answer")
    [(task, approval)] = board.pending_runs("gemini")

    def runner(argv, stdin, env, timeout, cwd):
        raise problem()
    with pytest.raises(problem):
        asks.run(board, repo, task, approval, runner=runner)
    assert board.get(1).status == "blocked"
    assert board.events(1)[-1].text == (asks.STOPPED if problem is KeyboardInterrupt
                                        else "The run crashed: RuntimeError.")


def test_a_failing_part_blocks_only_its_task(repo, fake_ixel, run_cli, monkeypatch):
    monkeypatch.setenv("FAKE_IXEL_ASK", "error")
    code, out, _ = run_cli("dispatch", "--yes", "gemini make me a list of projects, grok draw a logo")
    assert code == 1
    tasks = by_agent(Board.open(repo))
    assert tasks["gemini"].status == "blocked" and tasks["grok"].status == "handed_off"
    assert "Ixel: Couldn't connect to Gemini: 401 Unauthorized" in out


def test_a_long_answer_is_cut_on_the_board_and_kept_whole(repo, fake_ixel, run_cli, monkeypatch):
    monkeypatch.setenv("FAKE_IXEL_ANSWER", "idea " * 3000)
    code, _, _ = run_cli("dispatch", "--yes", "gemini list ideas")
    assert code == 0
    board = Board.open(repo)
    [task] = board.tasks(statuses=None)
    done = [e for e in board.events(task.id) if e.kind == "handoff"][-1].data
    assert done["done"].endswith("[… cut]") and "The full answer is in .handoff/outputs/T-1/answer.md" in done["left"]
    assert len((repo / ".handoff/outputs/T-1/answer.md").read_text(encoding="utf-8")) > 14_000


def test_pictures_saved_elsewhere_are_refused(repo, fake_ixel, run_cli, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_IXEL_OUT", str(tmp_path / "elsewhere"))
    code, out, _ = run_cli("dispatch", "--yes", "grok draw a logo")
    assert code == 1 and "somewhere other than the task's folder" in out


def test_pictures_that_were_never_saved_are_refused(repo, fake_ixel, run_cli, monkeypatch):
    monkeypatch.setenv("FAKE_IXEL_UNSAVED", "1")
    code, out, _ = run_cli("dispatch", "--yes", "grok draw a logo")
    assert code == 1 and "Ixel said it saved .handoff/outputs/T-1/T-1-1.png, but there's no picture there" in out
    [task] = Board.open(repo).tasks(statuses=None)
    assert task.status == "blocked"


def _link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:  # Windows without Developer Mode
        pytest.skip("can't make symbolic links here")


@pytest.mark.parametrize("per_task", [False, True])
def test_a_linked_output_folder_is_refused(repo, fake_ixel, run_cli, tmp_path, per_task):
    """A link planted in .handoff (a cloned repository can carry one) can't send Ixel's files outside."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    Board.open(repo)
    if per_task:
        (repo / ".handoff" / "outputs").mkdir()
        for ref in ("T-1", "T-2"):
            _link(repo / ".handoff" / "outputs" / ref, elsewhere)
    else:
        _link(repo / ".handoff" / "outputs", elsewhere)
    for ref, request in (("T-1", "gemini list ideas"), ("T-2", "grok draw a logo")):
        code, out, _ = run_cli("dispatch", "--yes", request)
        linked = f".handoff/outputs/{ref}" if per_task else ".handoff/outputs"
        assert code == 1 and f"{linked} is a link, so Handoff won't save anything through it" in out
    assert list(elsewhere.iterdir()) == [] and fake_ixel("ask") == fake_ixel("image") == []


def test_a_link_this_python_can_t_see_is_still_refused_before_anything_is_made(repo, fake_ixel, run_cli,
                                                                               tmp_path, monkeypatch):
    """A junction looks like a plain folder to Python before 3.12: where it leads gives it away."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    Board.open(repo)
    _link(repo / ".handoff" / "outputs", elsewhere)
    monkeypatch.setattr(asks.gitwork, "is_link", lambda path: False)
    code, out, _ = run_cli("dispatch", "--yes", "gemini list ideas")
    assert code == 1 and ".handoff/outputs leads outside the project, so Handoff won't save anything there" in out
    assert list(elsewhere.iterdir()) == [] and fake_ixel("ask") == []


@pytest.mark.parametrize("name,request_text", [("T-1-1.png", "grok draw a logo"), ("answer.md", "gemini list ideas")])
def test_a_link_planted_in_the_task_s_folder_is_refused(repo, fake_ixel, run_cli, tmp_path, name, request_text):
    """Ixel makes each picture new and Handoff writes answer.md through a fresh file, so neither would follow
    the link; a link has no business there anyway, so nothing runs."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"mine")
    Board.open(repo)
    (repo / ".handoff" / "outputs" / "T-1").mkdir(parents=True)
    _link(repo / ".handoff" / "outputs" / "T-1" / name, victim)
    code, out, _ = run_cli("dispatch", "--yes", request_text)
    assert code == 1 and f".handoff/outputs/T-1/{name} is a link, so Handoff won't run Ixel there" in out
    assert victim.read_bytes() == b"mine" and fake_ixel("image") == fake_ixel("ask") == []


def test_an_answer_is_written_through_a_new_file(repo, tmp_path):
    """Belt and braces: a link that appears at answer.md after the check is replaced, not followed."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    folder = repo / "out"
    folder.mkdir()
    _link(folder / "answer.md", victim)
    asks._replace(folder / "answer.md", "the answer\n")
    assert not (folder / "answer.md").is_symlink() and (folder / "answer.md").read_text(encoding="utf-8") == "the answer\n"
    assert victim.read_text(encoding="utf-8") == "keep me\n" and [p.name for p in folder.iterdir()] == ["answer.md"]


# ── Reviews of new files ─────────────────────────────────────────────────────

def _review_call(fake_ixel, run_cli):
    code, out, _ = run_cli("dispatch", "--yes", "codex review my changes")
    assert code == 0, out
    [review] = fake_ixel("ask")
    return review


def test_a_review_of_only_new_files_reads_them(repo, fake_ixel, run_cli):
    """New files git doesn't track yet are the work: not "nothing to review", and not the last commit."""
    (repo / "till.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    code, out, _ = run_cli("dispatch", "--plan", "--json", "codex review my changes")
    [step] = json.loads(out)["steps"]
    assert (step["kind"], step["problem"], step["detail"]) == ("review", "", "your new files")
    review = _review_call(fake_ixel, run_cli)
    # Ixel MAT reads them, with its own checks (links, key files, filters, its size limit)
    assert review["argv"] == ["ask", "--agent", "codex", "--json", "--diff", "--new-files", "-"]
    assert "The change is attached (your new files)" in review["stdin"]
    [task] = Board.open(repo).tasks(statuses=None)
    handback = [e for e in Board.open(repo).events(task.id) if e.kind == "handoff"][-1].data
    assert "It reviewed your new files. New files left out: auth.json (its name says" in handback["left"]


def test_a_review_of_changes_includes_new_files(repo, fake_ixel, run_cli):
    (repo / "README.md").write_text("# Shop\nNow with receipts.\n", encoding="utf-8")
    (repo / "till.py").write_text("x = 1\n", encoding="utf-8")
    review = _review_call(fake_ixel, run_cli)
    assert review["argv"] == ["ask", "--agent", "codex", "--json", "--diff", "--new-files", "-"]
    assert "your uncommitted changes and new files" in review["stdin"]


def test_a_stray_new_file_doesn_t_replace_the_branch_review(repo, fake_ixel, run_cli):
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "pay.py").write_text("def pay(x):\n    return x * 2\n", encoding="utf-8")
    git(repo, "add", "pay.py")
    git(repo, "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-q", "-m", "pay")
    (repo / "notes.txt").write_text("todo\n", encoding="utf-8")
    review = _review_call(fake_ixel, run_cli)
    assert review["argv"] == ["ask", "--agent", "codex", "--json", "--base", "main", "--new-files", "-"]
    assert "this branch (feature) since it left main, and your new files" in review["stdin"]


def test_an_older_ixel_reviews_without_new_files_and_says_so(repo, fake_ixel, run_cli, monkeypatch):
    monkeypatch.setenv("FAKE_IXEL_OLD", "1")  # its `ask --list` has no features
    (repo / "till.py").write_text("x = 1\n", encoding="utf-8")
    code, out, _ = run_cli("dispatch", "--plan", "--json", "codex review my changes")
    [step] = json.loads(out)["steps"]
    assert "the only changes are new files git doesn't track yet" in step["problem"] and "ixel update" in step["problem"]
    (repo / "README.md").write_text("# Shop\nNow with receipts.\n", encoding="utf-8")
    review = _review_call(fake_ixel, run_cli)
    assert review["argv"] == ["ask", "--agent", "codex", "--json", "--diff", "-"]
    assert "your uncommitted changes (not your new files: this Ixel MAT can't read them" in review["stdin"]


def test_a_review_says_why_git_refused(tmp_path):
    with pytest.raises(asks.RunProblem, match="git ls-files failed.*not a git repository"):
        asks.review_plan(tmp_path)


def test_approve_a_kind_and_run_it(repo, fake_ixel, run_cli):
    assert run_cli("add", "List three databases for a small shop")[0] == 0
    code, _, err = run_cli("approve", "T-1", "--worker", "gemini")
    assert code == 1 and "Only claude and codex can change files" in err and "--kind answer" in err
    code, out, _ = run_cli("approve", "T-1", "--worker", "gemini", "--kind", "answer")
    assert code == 0 and "Approved T-1 for gemini to answer" in out and "handoff run T-1" in out
    code, out, _ = run_cli("run", "T-1")
    assert code == 0 and "T-1 gemini: handed back to you" in out
    [call] = fake_ixel("ask")
    assert "List three databases for a small shop" in call["stdin"] and "it's yours to answer" in call["stdin"]
    code, _, err = run_cli("run", "T-1")
    assert code == 1 and "T-1 isn't approved to run" in err
    assert "Approve it first, like: handoff approve T-1 gemini --kind answer" in err  # as it ran, not "claude"
    code, out, _ = run_cli("run")
    assert code == 0 and "Nothing is approved to run" in out
    shown = run_cli("show", "T-1")[1]
    assert "approved it for gemini, through Ixel MAT, to answer" in shown and "started an Ixel MAT run" in shown
    assert "worker" not in shown  # an answer runs no worker


def test_approving_a_run_ixel_can_t_do_is_refused_first(repo, fake_ixel, run_cli, monkeypatch):
    run_cli("add", "Draw a logo")
    code, _, err = run_cli("approve", "T-1", "claude", "--kind", "image", "--yes")
    assert code == 1 and "claude can't make pictures through Ixel MAT; xAI (grok) and OpenAI (gpt) can." in err
    code, _, err = run_cli("approve", "T-1", "mistral", "--kind", "answer", "--yes")
    assert code == 1 and "There's no model called mistral in your Ixel MAT. You have: claude, codex, gemini" in err
    monkeypatch.setattr(asks, "find_ixel", lambda: None)
    code, _, err = run_cli("approve", "T-1", "gemini", "--kind", "review", "--yes")
    assert code == 1 and "Ixel MAT isn't installed" in err
    task = Board.open(repo).get(1)
    assert task.status == "open" and Board.open(repo).pending_runs() == []  # nothing was approved
    assert fake_ixel("ask") == fake_ixel("image") == []  # and no model was asked


def test_the_edit_worker_hands_other_kinds_to_ixel(repo, fake_ixel, run_cli):
    run_cli("add", "Explain the till")
    run_cli("approve", "T-1", "--worker", "claude", "--kind", "answer")
    code, out, _ = run_cli("worker", "--agent", "claude", "--once")
    assert code == 0 and "T-1 handed back to you" in out and "the work is on" not in out
    assert "▶ T-1 claude answers, through Ixel MAT…" in out and "worktrees" not in out
    assert [c["argv"][:3] for c in fake_ixel("ask")] == [["ask", "--agent", "claude"]]
    assert not (repo / ".handoff" / "worktrees" / "T-1").exists()  # an answer never gets a worktree


def test_the_kind_is_sealed(repo):
    """An approval to answer can't be turned into an approval to edit."""
    import sqlite3
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Explain it", assignee="claude")
    board.approve(HUMAN, task.id, "claude", kind="answer")
    approval = board.events(task.id)[-1]
    assert approval.data["kind"] == "answer" and [t.id for t, _ in board.pending_runs("claude")] == [task.id]
    conn = sqlite3.connect(board.path)
    with conn:
        conn.execute("INSERT INTO events (task_id, at, actor, kind, text, data) VALUES (?, ?, 'human', 'approved', "
                     "'', ?)", (task.id, approval.at, json.dumps({**approval.data, "kind": "edit"})))
    conn.close()
    assert board.pending_runs("claude") == []
    with pytest.raises(BoardError, match="A run is one of"):
        board.approve(HUMAN, task.id, "claude", kind="deploy")


# ── Reviews of a pull request ─────────────────────────────────────────────────

def _pull_request(repo):
    """A branch fetched but not checked out, as Ixel fetches a pull request: its commits, and you on main."""
    git(repo, "switch", "-q", "-c", "pr")
    (repo / "pay.py").write_text("def pay(x):\n    return x * 2\n", encoding="utf-8")
    git(repo, "add", "pay.py")
    git(repo, "-c", "user.name=Ada", "-c", "user.email=ada@example.com", "commit", "-q", "-m", "pay")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "switch", "-q", "main")
    (repo / "README.md").write_text("# Shop\nMy own change, not the pull request's.\n", encoding="utf-8")
    return {"base": git(repo, "rev-parse", "main").stdout.strip(), "head": head,
            "label": "pull request #7 (pr into main)"}


def test_a_pull_request_is_reviewed_at_the_commits_approved(repo, fake_ixel, run_cli):
    target = _pull_request(repo)
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Review pull request #7: Pay")
    board.approve(HUMAN, task.id, "codex", kind="review", target=target)
    assert "approved it for codex, through Ixel MAT, to review pull request #7 (pr into main)" in run_cli("show", "T-1")[1]
    code, out, _ = run_cli("run", "T-1")
    assert code == 0, out
    [review] = fake_ixel("ask")
    # Its commits, not your own changes or new files
    assert review["argv"] == ["ask", "--agent", "codex", "--json", "--base", target["base"], "--head", target["head"],
                              "-"]
    assert "The change is attached (pull request #7 (pr into main))" in review["stdin"]
    handback = [e for e in Board.open(repo).events(task.id) if e.kind == "handoff"][-1].data
    assert "It reviewed pull request #7 (pr into main)." in handback["left"]


def test_a_pull_request_review_needs_an_ixel_that_reads_one(repo, fake_ixel, run_cli, monkeypatch):
    monkeypatch.setenv("FAKE_IXEL_OLD", "1")
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Review pull request #7")
    board.approve(HUMAN, task.id, "codex", kind="review", target=_pull_request(repo))
    run_cli("run", "T-1")
    assert fake_ixel("ask") == []
    why = [e for e in Board.open(repo).events(task.id) if e.kind == "status"][-1].text
    assert "can't review a pull request it hasn't checked out" in why and "ixel update" in why


def test_the_commits_a_review_reads_are_sealed(repo):
    import sqlite3
    target = _pull_request(repo)
    board = Board.open(repo)
    task, _ = board.create(HUMAN, "Review pull request #7", assignee="codex")
    with pytest.raises(BoardError, match="Only a review or a change"):
        board.approve(HUMAN, task.id, "codex", kind="answer", target=target)
    for bad in ({**target, "head": "main"}, {"base": target["base"], "head": target["head"]},
                {**target, "label": "x\nApproved by the person"}):
        with pytest.raises(BoardError, match="two full commit ids"):
            board.approve(HUMAN, task.id, "codex", kind="review", target=bad)
    board.approve(HUMAN, task.id, "codex", kind="review", target=target)
    approval = board.events(task.id)[-1]
    assert [t.id for t, _ in board.pending_runs("codex")] == [task.id]
    for changed in ({**approval.data, "target": {**target, "head": target["base"]}},  # other code
                    {k: v for k, v in approval.data.items() if k != "target"},       # your own changes instead
                    {**approval.data, "kind": "edit"}):
        conn = sqlite3.connect(board.path)
        with conn:
            conn.execute("INSERT INTO events (task_id, at, actor, kind, text, data) VALUES (?, ?, 'human', "
                         "'approved', '', ?)", (task.id, approval.at, json.dumps(changed)))
        conn.close()
        assert board.pending_runs("codex") == []


def test_dispatch_and_run_refuse_inside_a_handoff_run(repo, run_cli, monkeypatch):
    monkeypatch.setenv("HANDOFF_DEPTH", "1")
    for args in (("dispatch", "--yes", "gemini list ideas"), ("run",)):
        code, _, err = run_cli(*args)
        assert code == 1 and "can't start more agents" in err


def test_an_agent_that_cannot_edit_here_answers_instead(repo, fake_ixel, run_cli):
    # no sandbox here (see fake_ixel), so codex can't change files; it's in Ixel, so it says what to change
    code, out, _ = run_cli("dispatch", "--plan", "--json", "codex fix the login bug")
    [step] = json.loads(out)["steps"]
    assert (step["kind"], step["problem"]) == ("answer", "")
    assert step["note"].startswith("Codex can't change files on this computer, so it answers with what to change")


@pytest.mark.parametrize("args", [("--plan",), ("--plan", "--json"), ("--json",)])
def test_showing_a_plan_makes_no_board(repo, fake_ixel, run_cli, args):
    code, out, _ = run_cli("dispatch", *args, "claude fix the typo, gemini list ideas")
    assert code == 0, out
    assert not (repo / ".handoff").exists() and fake_ixel("ask") == []  # no model was asked either


def test_a_return_to_that_can_t_be_leaves_no_task(repo, fake_ixel, run_cli):
    for args in (("--plan",), ("--yes",)):
        code, _, err = run_cli("dispatch", *args, "--return-to", "Bad Name!", "gemini list ideas")
        assert code == 1 and "--return-to 'Bad Name!' isn't valid" in err
    code, _, err = run_cli("dispatch", "--yes", "--no-run", "--return-to", "gemini", "gemini list ideas")
    assert code == 1 and "None of these can run here" in err
    assert not (repo / ".handoff" / "board.db").exists() or Board.open(repo).tasks(statuses=None) == []


def test_a_part_whose_results_would_go_back_to_its_own_agent_is_flagged_before_anything_is_added(
        repo, fake_ixel, run_cli):
    code, out, _ = run_cli("dispatch", "--plan", "--json", "--return-to", "codex",
                           "claude fix the footer, codex review my changes")
    steps = json.loads(out)["steps"]
    assert steps[1]["agent"] == "codex" and "its results would go back to Codex, which runs it" in steps[1]["problem"]
    code, out, err = run_cli("dispatch", "--yes", "--no-run", "--return-to", "grok",
                             "gemini list ideas, grok list more ideas")
    assert code == 0, err
    [task] = Board.open(repo).tasks(statuses=None)  # the other part, added and approved; nothing half done
    assert task.assignee == "gemini" and "Won't run:" in out


def test_a_long_part_s_title_fits_however_it_is_cut(repo):
    board = Board.open(repo)
    for text in ("x" * 198 + "\U0001f469\u200d\U0001f4bb and more", "y" * 199 + "\u200b and more"):
        title = crew.Step("claude", "Claude", "edit", text).title()
        assert len(title) <= 200 and board.create(HUMAN, title)[0].title == title


@pytest.mark.parametrize("fails", ["create", "approve"])
def test_a_dispatch_that_fails_partway_leaves_no_task_behind(repo, fails, monkeypatch):
    """Not just the step that failed: an approved task left behind would be run by a worker."""
    board = Board.open(repo)
    steps = [crew.Step("claude", "Claude", "edit", "fix the footer"),
             crew.Step("gemini", "Gemini", "answer", "list ideas")]
    real = getattr(board, fails)
    calls = []

    def second_one_fails(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(*args, **kwargs)
    monkeypatch.setattr(board, fails, second_one_fails)
    with pytest.raises(OSError):
        crew.add_and_approve(board, "claude fix the footer, gemini list ideas", steps)
    assert board.tasks(statuses=None) == []


def test_a_dispatched_answer_reads_only_its_own_files(repo, fake_ixel, run_cli):
    (repo / "NOTES.md").write_text("notes", encoding="utf-8")
    code, out, _ = run_cli("dispatch", "--yes", "grok draw a logo from NOTES.md, gemini summarize README.md")
    assert code == 0, out
    [ask] = fake_ixel("ask")
    assert ask["argv"] == ["ask", "--agent", "gemini", "--json", "-f", "README.md", "-"]


def test_without_ixel_only_edits_are_offered(repo, run_cli, monkeypatch, tmp_path):
    bin_dir = tmp_path / "only-claude"
    bin_dir.mkdir()
    _fake_claude(bin_dir, tmp_path / "claude.log")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(asks, "find_ixel", lambda: None)
    monkeypatch.setattr(sandbox, "check", _no_sandbox)
    monkeypatch.delenv("HANDOFF_DEPTH", raising=False)
    code, out, _ = run_cli("dispatch", "--plan", "--json", "claude fix the typo, gemini list ideas")
    plan = json.loads(out)
    assert [(s["agent"], s["kind"], bool(s["problem"])) for s in plan["steps"]] == [
        ("claude", "edit", False), ("gemini", "answer", True)]
    assert "Ixel MAT isn't installed" in plan["steps"][1]["problem"] and plan["problems"]
    code, out, _ = run_cli("agents")  # its example is one that runs here
    assert code == 0 and 'handoff dispatch "claude fix the footer"' in out


def test_a_review_reads_the_file_its_task_names_in_its_body(repo):
    ixel = asks.IxelRoster(agents=[{"name": "codex", "label": "Codex", "ready": True}], providers=[])
    board = Board.open(repo)
    task = board.create(HUMAN, "Review the documentation", "Check README.md")[0]
    argv, _, what = asks.command_for("ixel", repo, task, "review", "codex", ixel, Path("out"))
    assert argv[argv.index("-f") + 1] == "README.md" and what == "It reviewed README.md."
    split = board.create(HUMAN, "Review the documentation", f"codex{asks.DISPATCH_BODY}\n\nAlso fix README.md")[0]
    with pytest.raises(asks.RunProblem, match="nothing to review"):  # the whole request named it, for another agent,
        asks.command_for("ixel", repo, split, "review", "codex", ixel, Path("out"))  # so it reviews your changes
