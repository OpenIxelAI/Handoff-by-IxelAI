"""Auto crew from the command line and the window's JSON door: `handoff crew`, `handoff approve T-N --auto`, and the
`crew` op and approve's `auto`. No test calls a model or the network: the classifier answers nothing, and this
computer has only Claude here, so every crew is the same one."""
import json

import pytest
from rich.console import Console

from handoff import api, cli, crew_auto
from handoff.board import Board, BoardError

HAVE = {"claude"}
TITLE = "Fix the crash in the login endpoint"
BODY = "It fails on Safari."
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"


@pytest.fixture(autouse=True)
def _no_model_and_fixed_agents(monkeypatch):
    monkeypatch.setattr(crew_auto, "ask_model", lambda prompt: None)
    monkeypatch.setattr(crew_auto, "have_agents", lambda leave_out=(): set(HAVE))


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


def add_task(capsys):
    code, _, _ = run(capsys, "add", TITLE, "--to", "claude", "--body", BODY)
    assert code == 0


def approval_of(project):
    [(_, approval)] = Board.open(project).pending_runs("claude")
    return approval.data


# ── handoff crew ─────────────────────────────────────────────────────────────

def test_crew_previews_the_pick_for_words_and_approves_nothing(project, capsys):
    code, out, _ = run(capsys, "crew", TITLE)
    assert code == 0
    for line in ("Job:", "coding", "Size:", "light", "Decided by:", "keywords", SONNET, "medium effort", HAIKU,
                 "Nothing is approved or run"):
        assert line in out, line
    assert "low effort" not in out  # (Haiku takes no effort level in Claude Code)
    assert not (project / ".handoff").exists()  # looking never makes a board


def test_crew_names_a_task_and_approves_nothing(project, capsys):
    add_task(capsys)
    code, out, _ = run(capsys, "crew", "T-1")
    assert code == 0 and "T-1" in out and SONNET in out and "keywords" in out
    assert "handoff approve T-1 --auto" in out
    assert Board.open(project).pending_runs("claude") == []


def test_crew_for_words_never_names_a_task_to_approve(project, capsys):
    add_task(capsys)  # (T-1 is on the board, and is not what these words are)
    code, out, _ = run(capsys, "crew", "Fix a typo in the footer", "--effort-all", "max")
    assert code == 0 and SONNET in out
    assert "approve T-" not in out and "T-1" not in out and "--chain" not in out
    assert 'handoff add "Fix a typo in the footer"' in out
    assert "handoff approve <id> --auto --effort-all max" in out
    assert Board.open(project).pending_runs("claude") == []


def test_crew_for_words_with_a_quote_in_them_gives_a_placeholder_to_add(project, capsys):
    code, out, _ = run(capsys, "crew", 'Fix the "footer" typo')
    assert code == 0
    assert 'handoff add "WHAT YOU TYPED ABOVE"' in out and "approve T-" not in out
    assert "handoff approve <id> --auto" in out


def test_crew_for_a_wrong_project_is_an_error_not_no_usage_data(project, capsys, tmp_path):
    code, out, err = run(capsys, "crew", TITLE, "--project", str(tmp_path / "nowhere"))
    assert code == 1 and "no such folder" in err and "Job:" not in out


def test_crew_surfaces_a_board_it_cannot_read(project, capsys, monkeypatch):
    add_task(capsys)

    def unreadable(*args, **kwargs):
        raise BoardError("The board can't be read.")
    monkeypatch.setattr(Board, "open", staticmethod(unreadable))
    code, out, err = run(capsys, "crew", TITLE)
    assert code == 1 and "can't be read" in err and "Job:" not in out


def test_crew_opens_the_board_with_expiry_as_approve_does(project, capsys, monkeypatch):
    add_task(capsys)
    opened = []
    real = Board.open.__func__

    def recording(cls, root, **kwargs):
        opened.append(kwargs)
        return real(cls, root, **kwargs)
    monkeypatch.setattr(Board, "open", classmethod(recording))
    assert run(capsys, "crew", "T-1")[0] == 0
    assert opened and all(kwargs.get("expire", True) for kwargs in opened)  # (never expire=False)


def test_crew_says_a_seal_problem_cleanly_and_prints_no_command(project, capsys, monkeypatch):
    monkeypatch.setattr(crew_auto, "_effort_problem", lambda efforts: "a level it can't take")
    code, out, err = run(capsys, "crew", TITLE)
    assert code == 1 and "a level it can't take" in err
    assert "approve" not in out and "Traceback" not in err


def test_crew_for_a_picture_says_why_and_exits_1(project, capsys):
    code, _, err = run(capsys, "crew", "Draw a logo and an icon for the banner")
    assert code == 1 and "pictures" in err


def test_crew_effort_all_shows_one_level_for_every_model(project, capsys):
    code, out, _ = run(capsys, "crew", TITLE, "--effort-all", "max")
    assert code == 0 and "max effort" in out and "low effort" not in out and "--effort-all max" in out


# ── handoff approve T-N --auto ───────────────────────────────────────────────

def test_approve_auto_seals_the_picked_chain_and_efforts(project, capsys):
    add_task(capsys)
    code, out, _ = run(capsys, "approve", "T-1", "--auto", "--yes")
    assert code == 0 and "Auto crew picked these models" in out
    expected = crew_auto.auto_crew(TITLE, BODY, None, None)
    data = approval_of(project)
    assert data["models"] == [SONNET, HAIKU] == expected.chain
    assert data["efforts"] == {SONNET: "medium"} == expected.efforts  # (Haiku takes none)


def test_approve_auto_shows_the_crew_before_the_yes_and_seals_that_one_pick(project, capsys, monkeypatch):
    add_task(capsys)
    picks = []
    real = crew_auto.auto_crew

    def counting(*args, **kwargs):
        picks.append(args)
        return real(*args, **kwargs)
    monkeypatch.setattr(crew_auto, "auto_crew", counting)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    asked = []

    def say_yes(prompt):
        asked.append((prompt, capsys.readouterr().out))  # (everything printed up to the question)
        return "y"
    monkeypatch.setattr(cli.console, "input", say_yes)
    code, out, _ = run(capsys, "approve", "T-1", "--auto")
    assert code == 0
    [shown] = [text for prompt, text in asked if "Approve T-1 for the claude worker" in prompt]
    assert "Auto crew picked these models" in shown and SONNET in shown and HAIKU in shown
    assert len(picks) == 1  # (the one that was shown is the one that's sealed: no second pick after the yes)
    data = approval_of(project)
    assert data["models"] == [SONNET, HAIKU] and data["efforts"] == {SONNET: "medium"}


def test_approve_auto_says_a_seal_problem_cleanly_and_approves_nothing(project, capsys, monkeypatch):
    add_task(capsys)
    monkeypatch.setattr(crew_auto, "_effort_problem", lambda efforts: "a level it can't take")
    code, _, err = run(capsys, "approve", "T-1", "--auto", "--yes")
    assert code == 1 and "a level it can't take" in err and "Traceback" not in err
    assert Board.open(project).pending_runs("claude") == []


@pytest.mark.parametrize("level", ["max", "low"])
def test_approve_auto_effort_all_seals_one_level_for_every_model(project, capsys, level):
    add_task(capsys)
    assert run(capsys, "approve", "T-1", "--auto", "--effort-all", level, "--yes")[0] == 0
    data = approval_of(project)
    assert data["models"] == [SONNET, HAIKU]
    assert data["efforts"] == {SONNET: level}  # (the level for every model that takes one)


@pytest.mark.parametrize("extra", [["--model", OPUS], ["--chain", f"{OPUS},{HAIKU}"]])
def test_approve_auto_refuses_a_model_or_a_chain(project, capsys, extra):
    add_task(capsys)
    with pytest.raises(SystemExit) as refused:
        cli.main(["approve", "T-1", "--auto", *extra, "--yes"])
    assert refused.value.code == 2
    assert "can't go with --model or --chain" in capsys.readouterr().err
    assert Board.open(project).pending_runs("claude") == []


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_approve_auto_is_for_edits_only(project, capsys, kind):
    add_task(capsys)
    with pytest.raises(SystemExit) as refused:
        cli.main(["approve", "T-1", "--auto", "--kind", kind, "--yes"])
    assert refused.value.code == 2
    assert "picks the models for an edit" in capsys.readouterr().err
    assert Board.open(project).pending_runs("claude") == []


def test_effort_all_is_for_auto(project, capsys):
    add_task(capsys)
    with pytest.raises(SystemExit) as refused:
        cli.main(["approve", "T-1", "claude", "--effort-all", "max", "--yes"])
    assert refused.value.code == 2 and "--effort-all is for --auto" in capsys.readouterr().err


# ── The window's JSON door ───────────────────────────────────────────────────

@pytest.fixture
def api_project(tmp_path):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def call(op, project=None, **args):
    request = {"schema": 1, "op": op, "args": args}
    if project is not None:
        request["project"] = str(project)
    reply, code = api.handle(json.dumps(request).encode("utf-8"))
    assert reply["schema"] == 1 and reply["op"] == op
    assert (code == 0) == reply["ok"]
    return reply


def ok(op, project=None, **args):
    reply = call(op, project, **args)
    assert reply["ok"], reply
    return reply["data"]


def error(op, project=None, **args):
    reply = call(op, project, **args)
    assert not reply["ok"], reply
    return reply["error"]["code"], reply["error"]["message"]


def test_crew_is_a_read_op_and_auto_crew_a_feature():
    assert "crew" in api.READ_OPS
    assert "auto-crew" in api.FEATURES


def test_the_crew_op_returns_the_documented_keys(api_project):
    data = ok("crew", api_project, text=TITLE)
    assert {"job", "size", "how", "chain", "efforts", "roles", "crew_token"} <= set(data)
    assert data["job"] == "coding" and data["size"] == "light" and data["how"] == "keywords"
    assert data["chain"] == [SONNET, HAIKU] and data["efforts"] == {SONNET: "medium"}
    assert [role["role"] for role in data["roles"]] == ["planner", "builder"]
    assert [role["effort"] for role in data["roles"]] == ["medium", None]
    for role in data["roles"]:
        assert set(role) == {"role", "model", "effort", "why"}
    assert not (api_project / ".handoff").exists()  # reading never makes a board


def test_the_crew_op_takes_a_task_or_words_and_checks_its_args(api_project):
    ok("init", api_project)
    ok("add", api_project, title=TITLE, body=BODY)
    assert ok("crew", api_project, task="T-1")["chain"] == [SONNET, HAIKU]
    assert error("crew", api_project)[0] == "usage"
    assert error("crew", api_project, task="T-1", text=TITLE)[0] == "usage"
    assert error("crew", api_project, text=TITLE, effort_all=3)[0] == "usage"
    assert "An effort is one of" in error("crew", api_project, text=TITLE, effort_all="ultracode")[1]


def test_approve_with_auto_seals_the_crew_the_crew_op_showed(api_project):
    ok("init", api_project)
    ok("add", api_project, title=TITLE, body=BODY)
    shown = ok("crew", api_project, task="T-1", effort_all="max")
    assert shown["efforts"] == {SONNET: "max"} and len(shown["crew_token"]) == 32
    ok("approve", api_project, task="T-1", auto=True, effort_all="max", crew_token=shown["crew_token"])
    [(_, approval)] = Board.open(api_project).pending_runs("claude")
    assert approval.data["models"] == shown["chain"] == [SONNET, HAIKU]
    assert approval.data["efforts"] == shown["efforts"] == {SONNET: "max"}


def test_the_crew_token_is_the_same_for_the_same_crew_and_differs_for_another(api_project):
    first = ok("crew", api_project, text=TITLE)["crew_token"]
    assert ok("crew", api_project, text=TITLE)["crew_token"] == first
    assert ok("crew", api_project, text=TITLE, effort_all="max")["crew_token"] != first


def test_a_crew_token_follows_the_crew_and_not_how_it_was_decided():
    by_words = crew_auto.pick("coding", "light", HAVE, set(), how="keywords")
    by_model = crew_auto.pick("coding", "light", HAVE, set(), how="model (qwen3:4b-instruct)")
    assert by_words.token() == by_model.token()
    assert by_words.token() != crew_auto.pick("coding", "standard", HAVE, set()).token()


def test_approve_with_auto_needs_the_crew_token(api_project):
    ok("init", api_project)
    ok("add", api_project, title=TITLE, body=BODY)
    for token in ({}, {"crew_token": ""}, {"crew_token": "  "}):
        code, message = error("approve", api_project, task="T-1", auto=True, **token)
        assert code == "usage" and "crew_token" in message
    assert error("approve", api_project, task="T-1", auto=True, crew_token=7)[0] == "usage"
    assert error("approve", api_project, task="T-1", agent="claude", crew_token="abc")[0] == "usage"
    assert Board.open(api_project).pending_runs("claude") == []


def test_approve_with_auto_refuses_when_a_fresh_pick_is_not_the_crew_shown(api_project, monkeypatch):
    ok("init", api_project)
    ok("add", api_project, title=TITLE, body=BODY)
    shown = ok("crew", api_project, task="T-1")
    assert shown["chain"] == [SONNET, HAIKU]
    # (the classifier answers differently the second time: a fresh pick is another crew)
    monkeypatch.setattr(crew_auto, "ask_model", lambda prompt: ("test", '{"job": "coding", "size": "heavy"}'))
    code, message = error("approve", api_project, task="T-1", auto=True, crew_token=shown["crew_token"])
    assert code == "changed" and "Ask for the crew again" in message
    assert Board.open(api_project).pending_runs("claude") == []
    again = ok("crew", api_project, task="T-1")  # (shown again, it is accepted)
    assert again["chain"] != shown["chain"] and again["crew_token"] != shown["crew_token"]
    ok("approve", api_project, task="T-1", auto=True, crew_token=again["crew_token"])
    [(_, approval)] = Board.open(api_project).pending_runs("claude")
    assert approval.data["models"] == again["chain"]
    assert approval.data["efforts"] == again["efforts"] and again["efforts"]


def test_approve_with_auto_refuses_a_token_made_for_another_effort_all(api_project):
    ok("init", api_project)
    ok("add", api_project, title=TITLE, body=BODY)
    token = ok("crew", api_project, task="T-1", effort_all="max")["crew_token"]
    assert error("approve", api_project, task="T-1", auto=True, crew_token=token)[0] == "changed"
    assert error("approve", api_project, task="T-1", auto=True, effort_all="low", crew_token=token)[0] == "changed"
    assert Board.open(api_project).pending_runs("claude") == []


def test_effort_all_is_stripped_before_it_is_checked_and_used(api_project):
    assert api._effort_all({"effort_all": "high "}) == "high"
    assert api._effort_all({}) is None
    for bad in ("", "   ", 3):
        with pytest.raises(api.ApiError, match="args.effort_all must be"):
            api._effort_all({"effort_all": bad})
    ok("init", api_project)
    ok("add", api_project, title=TITLE, body=BODY)
    shown = ok("crew", api_project, task="T-1", effort_all=" high ")
    assert shown["efforts"] == {SONNET: "high"}
    ok("approve", api_project, task="T-1", auto=True, effort_all="high ", crew_token=shown["crew_token"])
    [(_, approval)] = Board.open(api_project).pending_runs("claude")
    assert approval.data["efforts"] == {SONNET: "high"}


def test_approve_with_auto_refuses_models_answers_and_bad_efforts(api_project):
    ok("init", api_project)
    ok("add", api_project, title=TITLE, body=BODY)
    assert "auto picks the models" in error("approve", api_project, task="T-1", auto=True, models=[OPUS])[1]
    assert "for an edit" in error("approve", api_project, task="T-1", auto=True, kind="answer")[1]
    assert "args.effort_all must be" in error("approve", api_project, task="T-1", auto=True, effort_all=3)[1]
    assert "auto: true" in error("approve", api_project, task="T-1", effort_all="max")[1]
    assert Board.open(api_project).pending_runs("claude") == []
