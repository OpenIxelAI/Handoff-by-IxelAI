"""The person's commands: board, show, add, assign, note, done, reopen, block, review, status, cancel, delete,
done-rule, approve, doctor; and what makes them easy: task ids on their own, aliases, help, plain-words errors."""
import os
import sqlite3
import sys
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


def test_add_board_show(project, capsys):
    code, out, _ = run(capsys, "add", "Write tests for /api/items", "--to", "codex", "--accept", "pytest passes",
                       "--path", "tests/", "--body", "Cover GET and POST.")
    assert code == 0 and "Added T-1 for codex." in out and "Tell codex to check its handoffs." in out
    code, out, _ = run(capsys, "add", "Build /api/items", "--to", "claude", "--path", "tests/test_api.py")
    assert "tests/test_api.py overlaps tests, claimed by T-1, assigned to codex" in out
    run(capsys, "add", "Subtask", "--parent", "T-1")

    code, out, _ = run(capsys, "board")
    assert code == 0 and "3 active · 3 open" in out and cli._shown_path(project) in out
    for line in ("T-1", "Write tests for /api/items", "codex", "you created it, assigned to codex"):
        assert line in out, line

    code, out, _ = run(capsys, "show", "T-1")
    assert code == 0
    for line in ("T-1  open  assigned to codex", "Write tests for /api/items", "subtasks T-3", "claims tests",
                 "Cover GET and POST.", "☐ 1. pytest passes", "you created it, assigned to codex"):
        assert line in out, line


def test_body_from_stdin(project, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("From a pipe\nline two\n"))
    run(capsys, "add", "Piped", "--body", "-")
    assert Board.open(project).get(1).body == "From a pipe\nline two"


def test_the_persons_controls(project, capsys):
    run(capsys, "add", "Task", "--to", "codex")
    assert run(capsys, "assign", "T-1", "claude")[1].splitlines() == ["  ✓ T-1 is assigned to claude (open).",
                                                                     "  Tell claude to check its handoffs."]
    assert "Noted on T-1" in run(capsys, "note", "1", "Please add docs")[1]
    assert "T-1 is blocked" in run(capsys, "status", "T-1", "blocked", "--reason", "Waiting", "--waiting-on",
                                    "codex")[1]
    assert "T-1 is open" in run(capsys, "status", "T-1", "open")[1]
    assert "T-1 is cancelled" in run(capsys, "cancel", "T-1", "--reason", "Not needed")[1]
    assert "T-1 is open" in run(capsys, "status", "T-1", "open")[1]  # the person can reopen
    assert "T-1 is done" in run(capsys, "status", "T-1", "done")[1]  # and close without a review

    code, out, _ = run(capsys, "board")
    assert "Nothing is active right now" in out
    code, out, _ = run(capsys, "board", "--all")
    assert "T-1" in out and "done" in out
    code, out, _ = run(capsys, "board", "--status", "cancelled")
    assert "No tasks match." in out


def test_review_from_the_command_line(project, capsys):
    board = Board.open(project)
    task, _ = board.create("claude", "Endpoint", assignee="claude")
    board.request_review("claude", task.id, "human", "Please look at src/api.py")
    code, out, _ = run(capsys, "review", "T-1", "changes", "Handle 404")
    assert code == 0 and "Sent back T-1; it's with claude again." in out
    assert board.get(1).status == "handed_off"


def test_review_marks_checks_from_the_command_line(project, capsys):
    board = Board.open(project)
    task, _ = board.create("claude", "Endpoint", acceptance=["GET works", "404 on a missing id"], assignee="claude")
    board.request_review("claude", task.id, "human", "Please look at src/api.py")
    code, _, err = run(capsys, "review", "T-1", "changes", "Handle 404", "--check", "two=met")
    assert code == 1 and "mark a check by its number" in err
    code, _, err = run(capsys, "review", "T-1", "approve", "--check", "2=not-met")
    assert code == 1 and "can't be an approval" in err
    code, out, _ = run(capsys, "review", "T-1", "changes", "Handle 404", "--check", "1=met",
                       "--check", "2=not_met:returns 500: not 404")
    assert code == 0 and "Sent back T-1" in out
    out = run(capsys, "show", "T-1")[1]
    assert "☐ 1. GET works" in out and "you reviewed it: changes requested (1 of 2 checks met)" in out
    assert "1. [met] GET works" in out and "2. [not met] 404 on a missing id — returns 500: not 404" in out


def test_delete_needs_confirmation(project, capsys, monkeypatch):
    run(capsys, "add", "Task")
    code, _, err = run(capsys, "delete", "T-1")
    assert code == 1 and "pass --yes" in err
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": "no")
    assert run(capsys, "delete", "T-1")[0] == 1
    monkeypatch.setattr(cli.console, "input", lambda prompt="": "t-1")
    code, out, _ = run(capsys, "delete", "T-1")
    assert code == 0 and "Deleted T-1" in out and Board.open(project).tasks() == []


def test_done_rule(project, capsys):
    assert "(review_or_reason)" in run(capsys, "done-rule")[1]
    assert "only after an approving review" in run(capsys, "done-rule", "review")[1]
    assert Board.open(project).done_rule == "review"
    with pytest.raises(SystemExit) as exited:  # argparse rejects it before the board is touched
        cli.main(["done-rule", "sometimes"])
    assert exited.value.code == 2


def test_errors_are_plain_and_exit_1(project, capsys, tmp_path, monkeypatch):
    code, _, err = run(capsys, "show", "T-9")
    assert code == 1 and "This project has no board yet, so there's no task T-9." in err
    run(capsys, "add", "One")
    code, _, err = run(capsys, "show", "T-9")
    assert code == 1 and "There's no task T-9 on this board." in err
    code, _, err = run(capsys, "show", "nine")
    assert code == 1 and "look like T-12" in err
    code, _, err = run(capsys, "add", "leak", "--body", "ghp_" + "A" * 36)
    assert code == 1 and "GitHub token" in err
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, _, err = run(capsys, "board")
    assert code == 1 and "isn't inside a git repository" in err
    assert run(capsys, "board", "--project", str(project))[0] == 0


def test_show_never_prints_control_codes(project, capsys):
    board = Board.open(project)
    board.create("claude", "Task", "body", ["check"], assignee="claude")
    board.note("claude", 1, "note")
    conn = sqlite3.connect(board.path)
    conn.execute("UPDATE tasks SET title = ?, body = ?", ("T\x1b]0;pwned\x07itle", "B\x1b[2Jody‮"))
    conn.execute("DROP TRIGGER events_append_only_update")  # simulate a board edited by something else
    conn.execute("UPDATE events SET text = ? WHERE kind = 'note'", ("N\x1b[31mote",))
    conn.commit()
    conn.close()
    for args in (("show", "T-1"), ("board",)):
        out = run(capsys, *args)[1]
        assert "\x1b" not in out and "\x07" not in out and "‮" not in out


def test_gitignore_isn_t_asked_while_the_board_keeps_itself_out(project, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": pytest.fail(f"asked: {prompt}"))
    run(capsys, "board")
    assert (project / ".handoff" / ".gitignore").exists() and not (project / ".gitignore").exists()


@pytest.fixture
def no_inner_gitignore(project):
    """A .handoff whose own .gitignore was changed, so it no longer keeps the board out of git."""
    (project / ".handoff").mkdir()
    (project / ".handoff" / ".gitignore").write_text("# changed\n", encoding="utf-8")


def test_gitignore_is_asked_once(project, no_inner_gitignore, capsys, monkeypatch):
    answers = iter(["", "should not be asked"])
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": next(answers))
    run(capsys, "board")
    assert (project / ".gitignore").read_text(encoding="utf-8").endswith(".handoff/\n")
    run(capsys, "board")
    assert next(answers) == "should not be asked"


def test_gitignore_no_is_remembered(project, no_inner_gitignore, capsys, monkeypatch):
    asked = []
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": asked.append(prompt) or "n")
    run(capsys, "board")
    run(capsys, "board")
    assert len(asked) == 1 and not (project / ".gitignore").exists()


def test_doctor(project, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    run(capsys, "add", "Task")
    code, out, _ = run(capsys, "doctor")
    assert code == 0, out
    assert "Project" in out and "1 task ·" in out and "done rule: review_or_reason" in out
    assert "The board stays out of git" in out
    if sys.platform != "win32":
        os.chmod(project / ".handoff" / "board.db", 0o644)
        code, out, _ = run(capsys, "doctor")
        assert code == 0 and "board.db was readable by others  mode 644; now 600" in out


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_doctor_reports_a_linked_board(project, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (project / ".handoff").mkdir()
    (project / ".handoff" / "board.db").symlink_to(tmp_path / "evil.db")
    code, out, _ = run(capsys, "doctor")
    assert code == 1 and "board.db is a symbolic link" in out
    code, _, err = run(capsys, "add", "Task")
    assert code == 1 and "symbolic link" in err and not (tmp_path / "evil.db").exists()


def test_doctor_says_when_grok_build_would_work_as_claude(project, capsys, monkeypatch, tmp_path):
    """Grok Build also loads the MCP servers in ~/.claude.json: without a Handoff of its own it uses Claude's."""
    import json

    from handoff import hosts
    monkeypatch.setattr(cli, "console", Console(width=300, highlight=False))
    monkeypatch.setattr(hosts, "home", lambda: tmp_path)
    monkeypatch.setattr(hosts, "installed", lambda host: host in ("claude-code", "grok"))
    _, out, _ = run(capsys, "doctor")
    assert "Grok Build doesn't have Handoff yet" in out and "works as claude" not in out
    command, args = hosts.server_args("claude")
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"handoff": {"command": command, "args": args}}}),
                                           encoding="utf-8")
    _, out, _ = run(capsys, "doctor")
    assert "Grok Build uses Claude Code's Handoff" in out and "on the board it works as claude" in out
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "config.toml").write_text('[mcp_servers.handoff]\ncommand = "handoff"\nenabled = false\n',
                                                     encoding="utf-8")
    _, out, _ = run(capsys, "doctor")  # Grok's own entry, turned off, still has the name
    assert "Grok Build doesn't have Handoff yet" in out and "works as claude" not in out
    hosts.write("grok", None)
    _, out, _ = run(capsys, "doctor")
    assert "Grok Build has Handoff" in out and "works as claude" not in out


def test_doctor_outside_a_project(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "console", Console(width=200, highlight=False))
    code, out, _ = run(capsys, "doctor")
    assert code == 1 and "isn't inside a git repository" in out


# ── Friendlier commands ──────────────────────────────────────────────────────

def usage_error(capsys, *args) -> str:
    """Run a command that should stop with a usage error: exit 2, plain words on stderr, no argparse usage."""
    with pytest.raises(SystemExit) as exited:
        cli.main(list(args))
    _, err = capsys.readouterr()
    assert exited.value.code == 2, err
    assert "usage:" not in err and "error:" not in err, err
    return err


def answering(monkeypatch, project, *replies):
    """Make it a terminal where the person types `replies`, one per question; returns the questions asked."""
    (project / ".gitignore").write_text(".handoff/\n", encoding="utf-8")  # so that question isn't asked
    asked, replies = [], iter(replies)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": asked.append(str(prompt)) or next(replies))
    return asked


@pytest.mark.parametrize("typed", ["T-1", "t1", "#1", "1"])
def test_a_task_id_on_its_own_shows_the_task(project, capsys, typed):
    run(capsys, "add", "Fix the login page")
    code, out, _ = run(capsys, typed)
    assert code == 0 and "T-1  open  assigned to nobody" in out and "Fix the login page" in out


def test_a_task_id_on_its_own_takes_the_project_flag(project, capsys, tmp_path, monkeypatch):
    run(capsys, "add", "Fix the login page")
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "T-1", "--project", str(project))
    assert code == 0 and "Fix the login page" in out


def test_handoff_on_its_own_shows_the_board_it_has(project, capsys):
    run(capsys, "add", "Fix the login page", "--to", "codex")
    code, out, _ = run(capsys)
    assert code == 0 and "Handoff board" in out and "Fix the login page" in out
    assert "Every command: handoff help · One task: handoff T-N" in out


def test_handoff_on_its_own_never_makes_a_board_or_asks(project, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": pytest.fail(f"asked: {prompt}"))
    code, out, _ = run(capsys)
    assert code == 0 and "handoff help lists every command." in out and "handoff board starts one" in out
    for usage, _ in cli.START:
        assert usage in out
    assert not (project / ".handoff").exists() and not (project / ".gitignore").exists()
    monkeypatch.chdir(tmp_path)  # not a project at all
    code, out, _ = run(capsys)
    assert code == 0 and "Run Handoff in your project's folder" in out


def test_board_flags_on_their_own_show_the_board(project, capsys):
    run(capsys, "add", "Task")
    run(capsys, "done", "T-1")
    code, out, _ = run(capsys, "--all")
    assert code == 0 and "T-1" in out and "done" in out


def test_done_reopen_and_block(project, capsys):
    run(capsys, "add", "Fix the login page", "--to", "codex")
    assert run(capsys, "done", "T-1")[1].strip() == "✓ T-1 is done."
    assert "T-1 is already done." in run(capsys, "done", "1")[1]
    assert "T-1 is open again, assigned to codex." in run(capsys, "reopen", "T-1")[1]
    assert "T-1 is already open." in run(capsys, "reopen", "T-1")[1]
    code, out, _ = run(capsys, "block", "T-1", "Waiting", "for", "the", "keys", "--waiting-on", "me")
    assert code == 0 and "T-1 is blocked, waiting on you." in out and "handoff reopen T-1" in out
    board = Board.open(project)
    task = board.get(1)
    assert task.status == "blocked" and task.waiting_on == "human"
    assert board.events(1)[-1].text == "Waiting for the keys"
    assert "T-1 is open again" in run(capsys, "reopen", "T-1")[1]
    assert run(capsys, "block", "T-1", "--reason", "Needs a decision")[0] == 0
    assert board.events(1)[-1].text == "Needs a decision"
    run(capsys, "reopen", "T-1")
    assert run(capsys, "done", "T-1", "--reason", "Fixed upstream")[0] == 0
    assert board.events(1)[-1].text == "Fixed upstream"
    run(capsys, "reopen", "T-1")
    run(capsys, "cancel", "T-1")
    assert "T-1 is open again" in run(capsys, "reopen", "T-1")[1]


def test_reopen_and_block_say_plainly_what_they_cant_do(project, capsys):
    board = Board.open(project)
    board.create("claude", "Endpoint", assignee="claude")
    board.note("claude", 1, "Started")
    code, _, err = run(capsys, "reopen", "T-1")
    assert code == 1 and "T-1 is in_progress, not done, cancelled or blocked" in err
    assert "handoff status T-1 open" in err
    board.request_review("claude", 1, "human", "Look at it")
    code, _, err = run(capsys, "reopen", "T-1")
    assert code == 1 and "waiting for a review" in err and "handoff review T-1" in err
    err = usage_error(capsys, "block", "T-1")
    assert 'Say why T-1 is blocked, like: handoff block T-1 "Waiting for the API keys"' in err
    assert "More: handoff help block" in err


def test_the_board_says_when_only_finished_tasks_are_hidden(project, capsys):
    run(capsys, "add", "Task")
    run(capsys, "done", "T-1")
    assert "Nothing is active right now. To see finished tasks too: handoff board --all" in run(capsys, "board")[1]
    assert "No tasks match." in run(capsys, "board", "--assignee", "codex")[1]


def test_board_messages_are_said_to_you(project, capsys):
    run(capsys, "add", "Task")
    run(capsys, "done", "T-1")
    code, _, err = run(capsys, "cancel", "T-1")
    assert code == 1 and "T-1 is done, so you can't cancel it. You can reopen it with: handoff reopen T-1" in err
    assert "The person" not in err and "handoff status T-1 open" not in err


@pytest.mark.parametrize("alias,command", [(a, c) for a, c in cli.ALIASES.items()])
def test_aliases_run_their_command(alias, command, monkeypatch):
    called = []
    monkeypatch.setitem(cli.HANDLERS, command, lambda argv: called.append(argv) or 0)
    assert cli.main([alias, "T-1"]) == 0 and called == [["T-1"]]


def test_aliases_work_end_to_end(project, capsys, monkeypatch):
    assert "Added T-1" in run(capsys, "new", "Fix", "the", "login", "page")[1]
    assert "Fix the login page" in run(capsys, "ls")[1]
    assert "Fix the login page" in run(capsys, "view", "1")[1]
    assert "Noted on T-1" in run(capsys, "comment", "T-1", "Looked", "at", "it")[1]
    assert "T-1 is done" in run(capsys, "close", "T-1")[1]
    assert "Deleted T-1" in run(capsys, "rm", "T-1", "--yes")[1]


@pytest.mark.parametrize("word", ["me", "you", "myself", "Me"])
def test_me_means_you(project, capsys, word):
    run(capsys, "add", "Task", "--to", word)
    board = Board.open(project)
    assert board.get(1).assignee == "human"
    run(capsys, "assign", "T-1", "codex")
    code, out, _ = run(capsys, "assign", "T-1", word)
    assert code == 0 and "T-1 is assigned to you (open)." in out
    code, out, _ = run(capsys, "board", "--assignee", word)
    assert "T-1" in out
    run(capsys, "status", "T-1", "blocked", "--reason", "Waiting", "--waiting-on", word)
    assert board.get(1).waiting_on == "human"


def test_approve_return_to_me(project, capsys):
    run(capsys, "add", "Task")
    assert run(capsys, "approve", "T-1", "claude", "--return-to", "me", "--yes")[0] == 0
    approval = Board.open(project).events(1)[-1]
    assert approval.kind == "approved" and approval.data["return_to"] == "human"


def test_an_unknown_command_suggests_the_closest(capsys):
    assert cli.main(["boards"]) == 2
    err = capsys.readouterr().err
    assert "Unknown command: boards. Did you mean: handoff board" in err and "handoff help" in err
    code, _, err = run(capsys, "fix", "the", "login", "page")  # typed without quotes
    assert code == 2 and 'To add it as a task: handoff add "fix the login page"' in err
    code, _, err = run(capsys, 'tell codex "hi" now')
    quoted = "\"tell codex \\\"hi\\\" now\"" if sys.platform == "win32" else "'tell codex \"hi\" now'"
    assert f"To add it as a task: handoff add {quoted}" in err
    assert cli.main(["unblok"]) == 2
    assert "Did you mean: handoff unblock" in capsys.readouterr().err
    assert cli.main(["xyzzy"]) == 2
    assert "Did you mean" not in capsys.readouterr().err


def test_help_groups_every_command_once():
    grouped = [name for _, names in cli.GROUPS for name in names]
    assert sorted(grouped) == sorted(usage.split()[1] for usage, _ in cli.COMMANDS)
    assert sorted(grouped) == sorted(cli.HANDLERS)


def test_help_shows_groups_examples_and_the_footer(capsys):
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    for heading, _ in cli.GROUPS:
        assert heading in out
    for example in cli.HELP_EXAMPLES:
        assert example in out
    for words in ("Task ids can be T-3, t3 or just 3", "handoff help COMMAND", "--project PATH", "unblock for reopen",
                  "me means you"):
        assert words in out


@pytest.mark.parametrize("command", sorted(cli.HANDLERS))
def test_every_command_has_help_with_examples(command, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.HANDLERS[command](["--help"])
    assert exited.value.code == 0
    text = capsys.readouterr().out
    assert f"usage: handoff {command}" in text
    examples = text.split("Examples:\n", 1)[1].strip().splitlines()
    assert 1 <= len(examples) <= 3 and all(line.strip().startswith("handoff ") for line in examples)
    # handoff help COMMAND says the same
    assert cli.main(["help", command]) == 0
    assert capsys.readouterr().out == text


def test_help_for_an_alias_and_for_nothing_known(capsys):
    assert cli.main(["help", "ls"]) == 0
    assert "usage: handoff board" in capsys.readouterr().out
    assert cli.main(["help", "boards"]) == 2
    assert "Did you mean: handoff board" in capsys.readouterr().err


def test_show_without_a_task_lists_some(project, capsys):
    err = usage_error(capsys, "show")
    assert "Say which task, like: handoff show T-3" in err and "More: handoff help show" in err
    assert "On this board" not in err and not (project / ".handoff").exists()  # and no board was made
    for title in ("Fix the login page", "Write tests", "Old"):
        run(capsys, "add", title)
    run(capsys, "done", "T-3")
    err = usage_error(capsys, "show")
    assert "T-1  Fix the login page" in err and "T-2  Write tests" in err and "Old" not in err


@pytest.mark.parametrize("args,said", [
    (["status", "T-1", "xyz"], "'xyz' isn't a status you can set; use open, blocked or done."),
    (["review", "T-1", "maybe"], "'maybe' isn't a verdict; use approve or changes."),
    (["approve", "T-1", "claude", "--kind", "poem"], "'poem' isn't a kind of run; use edit, answer, review or image."),
    (["done-rule", "sometimes"], "'sometimes' isn't a done rule; use review_or_reason, review or any."),
    (["show", "T-1", "--foo"], "handoff show doesn't take --foo."),
    (["board", "--wach"], "handoff board doesn't take --wach. Did you mean --watch?"),
    (["add", "Title", "--assignee", "codex"], "handoff add doesn't take --assignee. Did you mean --to?"),
    (["assign", "T-7"], "Say who gets it: handoff assign T-7 codex, or nobody"),
    (["note", "T-1"], 'Say what to note, like: handoff note T-1 "The bug only happens on Safari"'),
    (["status", "T-1"], "Say the new status: handoff status T-1 open, blocked or done"),
    (["worker"], "Say which agent runs the tasks, like: handoff worker --agent claude"),
    (["worker", "--agent", "claude", "--poll", "soon"], "--poll needs a number, not 'soon'."),
    (["add", "Title", "--to"], "--to needs a value after it."),
    (["show", "T-1", "T-2"], "handoff show didn't expect 'T-2'."),
])
def test_usage_errors_in_plain_words(project, capsys, args, said):
    err = usage_error(capsys, *args)
    assert said in err and f"More: handoff help {args[0]}" in err


def test_a_wrong_choice_shows_a_right_one(project, capsys):
    err = usage_error(capsys, "review", "T-4", "maybe")
    assert "For example: handoff review T-4 approve" in err


def test_add_asks_at_a_terminal_and_explains_elsewhere(project, capsys, monkeypatch):
    err = usage_error(capsys, "add")
    assert 'Say what needs doing, like: handoff add "Fix the login page" --to codex' in err
    assert not (project / ".handoff").exists()
    asked = answering(monkeypatch, project, "Fix the login page", "me")
    code, out, _ = run(capsys, "add")
    assert code == 0 and "Added T-1 for you." in out
    assert asked == ["  What needs doing? ", "  Who should do it? (claude, codex… or Enter for anyone) "]
    answering(monkeypatch, project, "Tidy the CSS", "")
    assert "Added T-2 (anyone can claim it)." in run(capsys, "add")[1]
    answering(monkeypatch, project, "")
    assert run(capsys, "add")[0] == 1
    assert [t.title for t in Board.open(project).tasks()] == ["Tidy the CSS", "Fix the login page"]


def test_titles_and_notes_need_no_quotes(project, capsys):
    run(capsys, "add", "Fix", "the", "login", "page", "--to", "codex")
    run(capsys, "note", "T-1", "Only", "on", "Safari")
    board = Board.open(project)
    assert board.get(1).title == "Fix the login page" and board.events(1)[-1].text == "Only on Safari"


def in_review_for_you(project, checks=("GET works", "404 on a missing id")):
    board = Board.open(project)
    task, _ = board.create("claude", "Add /api/orders", acceptance=list(checks), assignee="claude")
    board.pass_task("claude", task.id, "codex", "Built the endpoint", "Docs", "pytest tests/test_orders.py")
    board.claim("codex", task.id)
    board.request_review("codex", task.id, "human", "Please check the 404 case")
    return board


def test_review_without_a_verdict_walks_you_through_it(project, capsys, monkeypatch):
    board = in_review_for_you(project)
    err = usage_error(capsys, "review", "T-1")
    assert "handoff review T-1 approve" in err and 'handoff review T-1 changes "what to change"' in err
    asked = answering(monkeypatch, project, "a", "y", "", "Looks right")
    code, out, _ = run(capsys, "review", "T-1")
    assert code == 0, out
    for shown in ("Add /api/orders", "Done: Built the endpoint", "Verify: pytest tests/test_orders.py",
                  "Please check the 404 case", "1. GET works", "2. 404 on a missing id"):
        assert shown in out, shown
    assert asked[0] == "  Approve it, or send it back with changes? [a/c] "
    assert "Approved T-1; it's with codex again." in out
    review = board.events(1)[-1]
    assert review.data["verdict"] == "approve" and review.text == "Looks right"
    assert [m["result"] for m in review.data["checks"]] == ["met", "not_checked"]


def test_review_at_a_terminal_sends_back_a_check_that_isnt_met(project, capsys, monkeypatch):
    board = in_review_for_you(project)
    answering(monkeypatch, project, "a", "y", "n", "", "Handle the 404")
    code, out, _ = run(capsys, "review", "T-1")
    assert code == 0 and "Sent back T-1" in out
    review = board.events(1)[-1]
    assert review.data["verdict"] == "changes" and review.text == "Handle the 404"
    assert [m["result"] for m in review.data["checks"]] == ["met", "not_met"]


def test_review_at_a_terminal_records_nothing_without_an_answer(project, capsys, monkeypatch):
    board = in_review_for_you(project, checks=())
    answering(monkeypatch, project, "")
    assert run(capsys, "review", "T-1")[0] == 1
    answering(monkeypatch, project, "c", "")  # changes, but not what to change
    assert run(capsys, "review", "T-1")[0] == 1
    assert board.get(1).status == "in_review"
    answering(monkeypatch, project, "c", "Handle the 404")
    assert "Sent back T-1" in run(capsys, "review", "T-1")[1]


def test_review_of_a_task_that_isnt_in_review(project, capsys):
    run(capsys, "add", "Task")
    code, _, err = run(capsys, "review", "T-1")
    assert code == 1 and "T-1 isn't waiting for a review (it's open)." in err


def test_approve_takes_the_agent_after_the_task(project, capsys):
    run(capsys, "add", "Task")
    run(capsys, "add", "Another")
    code, out, _ = run(capsys, "approve", "T-1", "claude", "--yes")
    assert code == 0 and "Approved T-1 for the claude worker." in out
    assert run(capsys, "approve", "T-2", "--worker", "codex", "--yes")[0] == 0  # the old way still works
    board = Board.open(project)
    assert {t.assignee for t, _ in board.pending_runs()} == {"claude", "codex"}
    err = usage_error(capsys, "approve", "T-1", "claude", "--worker", "codex")
    assert "Name the agent once" in err


def test_approve_without_an_agent(project, capsys, monkeypatch):
    run(capsys, "add", "Nobody has it")
    err = usage_error(capsys, "approve", "T-1")
    assert "handoff approve T-1 claude (or codex)" in err and "handoff agents" in err
    run(capsys, "add", "Claude has it", "--to", "claude")
    err = usage_error(capsys, "approve", "T-2")  # not at a terminal: name it
    assert "handoff approve T-2 claude" in err and "when it can ask you first" in err
    asked = answering(monkeypatch, project, "y")
    err = usage_error(capsys, "approve", "T-2", "--yes")  # --yes skips the question, so it can't pick for you
    assert asked == []
    code, out, _ = run(capsys, "approve", "T-2")
    assert code == 0 and "The claude worker (T-2's assignee) gets T-2" in out
    assert asked == ["  Approve T-2 for the claude worker? [Y/n] "]
    assert [t.ref for t, _ in Board.open(project).pending_runs("claude")] == ["T-2"]


def test_approve_a_task_waiting_for_your_review(project, capsys):
    in_review_for_you(project)
    code, _, err = run(capsys, "approve", "T-1")
    assert code == 1 and "T-1 is waiting for your review." in err
    assert "handoff review T-1 approve" in err and 'handoff review T-1 changes "what to change"' in err


def test_approve_a_finished_task_says_so_first(project, capsys):
    run(capsys, "add", "Task")
    run(capsys, "done", "T-1")
    code, out, err = run(capsys, "approve", "T-1", "claude")
    assert code == 1 and "T-1 is done, so there's nothing to run" in err and "handoff reopen T-1" in err
    assert "gets T-1" not in out


def test_an_unknown_task_says_which_there_are(project, capsys):
    code, _, err = run(capsys, "done", "T-3")
    assert code == 1 and "There's no task T-3 on this board." in err
    assert 'The board is empty; add one with: handoff add "title"' in err
    for title in ("One", "Two", "Three"):
        run(capsys, "add", title)
    assert "This board has T-1 to T-3; handoff board --all lists them." in run(capsys, "T-9")[2]
    run(capsys, "delete", "T-2", "--yes")
    assert "This board has T-1 and T-3" in run(capsys, "done", "T-9")[2]
    run(capsys, "add", "Four")
    assert "This board has 3 tasks, from T-1 to T-4" in run(capsys, "T-9")[2]
    run(capsys, "delete", "T-3", "--yes")
    run(capsys, "delete", "T-4", "--yes")
    assert "This board has only T-1" in run(capsys, "T-9")[2]


def test_the_person_is_you_in_what_the_cli_prints(project, capsys):
    board = in_review_for_you(project)
    out = run(capsys, "show", "T-1")[1]
    assert "assigned to you" in out and "codex asked you for a review" in out and "human" not in out
    code, out, _ = run(capsys, "review", "T-1", "approve")
    assert "it's with codex again" in out
    board.pass_task("codex", 1, "human", "All done")
    out = run(capsys, "board")[1]
    assert "codex handed it to you" in out and "human" not in out
    run(capsys, "add", "Mine", "--to", "me")
    out = run(capsys, "board")[1]
    assert " you " in out and "human" not in out


def start_a_run(b):
    b.create("human", "T", assignee="codex")
    task, approval = b.approve("human", 1, "codex"), b.pending_runs("codex")[0][1]
    b.start_run("codex", task.id, approval.id, {})


@pytest.mark.parametrize("setup,next_lines", [
    (lambda b: b.create("human", "Open"),
     ["Next, to give it to an agent: handoff assign T-1 codex", "Or let one run it now: handoff approve T-1 claude"]),
    (lambda b: b.create("human", "Open", assignee="codex"),
     ['Next: codex picks it up in its app when you say "check your handoffs".',
      "Or run it now: handoff approve T-1 codex"]),
    (lambda b: b.create("human", "Open", assignee="gemini"),
     ["Or run it now: handoff approve T-1 gemini --kind answer"]),
    (lambda b: b.create("human", "Mine", assignee="human"),
     ["Next: it's with you. When it's finished: handoff done T-1", "To hand it on: handoff assign T-1 codex"]),
    (lambda b: (b.create("human", "T"), b.set_status("human", 1, "done")),
     ["Next, to work on it again: handoff reopen T-1"]),
    (lambda b: (b.create("human", "T"), b.set_status("human", 1, "blocked", "Why")),
     ["Next, when it can go on: handoff reopen T-1"]),
    (lambda b: (b.create("human", "T"), b.cancel("human", 1)), ["Next, to work on it again: handoff reopen T-1"]),
    (lambda b: (b.create("human", "T"), b.approve("human", 1, "claude")),
     ["Next: it's approved for claude. Run it now: handoff run T-1"]),
    (lambda b: (b.create("human", "T", assignee="claude"), b.claim("claude", 1)),
     ["Next: claude is working on it; it shows here when claude hands it over."]),
    # approving it again while the worker runs would throw its result away, so show doesn't suggest it
    (start_a_run, ["Next: the codex worker is running it now; the result shows here when it finishes."]),
])
def test_show_says_what_you_can_do_next(project, capsys, setup, next_lines):
    setup(Board.open(project))
    out = run(capsys, "show", "T-1")[1]
    for line in next_lines:
        assert f"  {line}\n" in out
    nexts = out[out.index("  Next"):].strip().splitlines()
    assert not any(line.rstrip().endswith((",", ";")) for line in nexts), nexts  # each command copies whole
    if setup is start_a_run:
        assert "handoff approve" not in out


def test_show_next_for_a_review(project, capsys):
    in_review_for_you(project)
    out = run(capsys, "T-1")[1]
    assert "  Next, to accept the work: handoff review T-1 approve\n" in out
    assert '  Or send it back: handoff review T-1 changes "what to change"\n' in out


def test_paths_show_your_home_as_a_tilde(project, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(project.parent))
    monkeypatch.setenv("USERPROFILE", str(project.parent))
    shown = cli._shown_path(project)
    assert shown == str(Path("~", "project"))
    assert f"  {shown}\n" in run(capsys, "board")[1]


def test_piped_output_keeps_paths_and_help_whole(tmp_path):
    import subprocess
    deep = tmp_path / "a-long-folder-name-for-a-shop-project" / "with-another-long-folder-name-inside" / "shop"
    (deep / ".git").mkdir(parents=True)
    (deep / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "80", "HOME": str(tmp_path / "home"),
           "USERPROFILE": str(tmp_path / "home")}
    env.pop("HANDOFF_DEPTH", None)

    def handoff(*args):
        return subprocess.run([sys.executable, "-m", "handoff", *args], cwd=deep, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=60).stdout
    run_out = handoff("add", "Fix the login page and the footer while you're there", "--to", "codex")
    assert "Added T-1" in run_out
    board = handoff("board")
    assert str(deep.resolve()) in board and "you created it, assigned to codex" in board
    assert "Commands that use the board take --project PATH; by default it's the git" in handoff("help")
    assert "You can always close a task yourself: handoff done T-12" in handoff("done-rule")
    assert "1 task ·" in handoff("doctor")


def test_long_text_in_the_cli_has_no_long_lines_or_dashes():
    # The help reads well at 80 columns; new prose doesn't use em-dashes
    for usage, what in cli.COMMANDS:
        assert len("        " + what) <= 88, what
    for usage, what in cli.START:
        assert len("        " + what) <= 80 and "—" not in what
    assert "—" not in cli._aliases_line()


def test_piped_show_keeps_long_words_whole(project, capsys, monkeypatch):
    monkeypatch.setattr(cli, "console", Console(width=40, highlight=False))  # narrow, and not a terminal
    url = "https://example.com/a/very/long/url/that/goes/on/and/on/index.html"
    run(capsys, "add", "Fix the crash in src/components/checkout/PaymentForm.tsx", "--body", f"See {url} for more",
        "--accept", "A long acceptance check that goes past forty columns")
    run(capsys, "note", "T-1", "A long note that goes past forty columns for sure")
    out = run(capsys, "show", "T-1")[1]
    assert "  Fix the crash in src/components/checkout/PaymentForm.tsx\n" in out
    assert f"  See {url} for more\n" in out
    assert "☐ 1. A long acceptance check that goes past forty columns\n" in out
    assert "      A long note that goes past forty columns for sure\n" in out
    out = run(capsys, "approve", "T-1", "claude", "--yes")[1]
    assert f"      See {url} for more\n" in out


def test_errors_for_a_program_end_with_the_reason(project, capsys):
    # The Ixel app shows the last line of what Handoff says on stderr
    err = usage_error(capsys, "dispatch", "--json", "--plan", "--budget", "5", "codex fix the footer")
    assert err.strip().splitlines()[-1].strip() == "handoff dispatch doesn't take --budget."
    err = usage_error(capsys, "run", "--json", "--bogus")
    assert err.strip().splitlines() == ["handoff run doesn't take --bogus."]
    assert "More: handoff help run" in usage_error(capsys, "run", "--bogus")


def test_a_title_or_note_after_two_dashes(project, capsys):
    code, out, _ = run(capsys, "add", "--to", "codex", "--", "-weird", "title")
    assert code == 0 and Board.open(project).get(1).title == "-weird title"
    assert run(capsys, "note", "T-1", "--", "-x", "marks", "the", "spot")[0] == 0
    assert Board.open(project).events(1)[-1].text == "-x marks the spot"


def test_the_project_flag_can_come_first(project, capsys, tmp_path, monkeypatch):
    run(capsys, "add", "Fix the login page")
    monkeypatch.chdir(tmp_path)
    for args in (["--project", str(project), "board"], [f"--project={project}", "board"],
                 ["--project", str(project), "T-1"], ["--project", str(project), "--all"], ["--project", str(project)]):
        code, out, _ = run(capsys, *args)
        assert code == 0 and "Fix the login page" in out, args
    fresh = tmp_path / "fresh"
    (fresh / ".git").mkdir(parents=True)
    (fresh / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    code, out, _ = run(capsys, "--project", str(fresh))
    assert code == 0 and "This project has no board yet; handoff board --project" in out and str(fresh) in out
    assert not (fresh / ".handoff").exists()


def test_assign_to_anyone_leaves_it_unassigned(project, capsys):
    run(capsys, "add", "Task", "--to", "codex")
    code, out, _ = run(capsys, "assign", "T-1", "anyone")
    assert code == 0 and "T-1 is assigned to nobody (open)." in out
    assert Board.open(project).get(1).assignee is None


def test_looking_at_a_task_never_makes_a_board(project, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt="": pytest.fail(f"asked: {prompt}"))
    for args in (["3"], ["show", "T-3"]):
        code, _, err = run(capsys, *args)
        assert code == 1 and "This project has no board yet, so there's no task T-3." in err
        assert 'Add one with: handoff add "title"' in err
    assert not (project / ".handoff").exists()


def test_a_home_folder_at_the_root_isnt_shown_as_a_tilde(project, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(project.anchor)))
    assert cli._shown_path(project) == str(project)


def test_cli_and_board_agree_on_the_persons_words():
    from handoff.board import PERSON_WORDS
    assert cli.YOU == PERSON_WORDS


def test_an_app_can_t_join_as_me(project, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["mcp", "--as", "me"])
    assert stopped.value.code == 2 and "isn't a name an agent can have" in capsys.readouterr().err


def test_name_problems_are_said_to_you(project, capsys):
    run(capsys, "add", "Task")
    code, _, err = run(capsys, "approve", "T-1", "claude", "--return-to", "claude", "--yes")
    assert code == 1 and "The results need to go to someone other than claude; the default is you." in err
    code, _, err = run(capsys, "assign", "T-1", "bad!")
    assert code == 1 and "such as claude, codex or me." in err
    code, _, err = run(capsys, "approve", "T-1", "codex", "--return-to", "Bad!", "--yes")
    assert code == 1 and "--return-to 'Bad!' isn't valid" in err
    code, _, err = run(capsys, "approve", "T-1", "Bad!", "--kind", "answer", "--yes")
    assert code == 1 and "The agent 'Bad!' isn't valid: use a short lowercase name such as claude or codex." in err


def test_an_agent_an_app_named_you_is_told_apart(project, capsys):
    # Handoff no longer lets an agent be called you or me, but a board from an older one can have them
    board = Board.open(project)
    board.create("claude", "Made by an agent called you", assignee="codex")
    conn = sqlite3.connect(board.path)
    conn.execute("DROP TRIGGER events_append_only_update")
    conn.execute("UPDATE tasks SET created_by = 'you', assignee = 'me'")
    conn.execute("""UPDATE events SET actor = 'you', data = '{"assignee": "me", "parent_id": null}'""")
    conn.commit()
    conn.close()
    out = run(capsys, "board")[1]
    assert "you (an agent) created it, assigned to me (an agent)" in out
    out = run(capsys, "show", "T-1")[1]
    assert "assigned to me (an agent)" in out and "created by you (an agent)" in out


def test_an_agent_handoff_started_can_t_act_as_you(project, capsys, monkeypatch):
    run(capsys, "add", "Task")
    from handoff import hosts
    fake = project.parent / "fake-home"  # were the refusal ever to break, setup would write here, not your apps'
    monkeypatch.setattr(hosts, "home", lambda: fake)
    monkeypatch.setenv("APPDATA", str(fake / "AppData" / "Roaming"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HANDOFF_DEPTH", "1")  # a shell inside something Handoff started runs as you
    for args in (("delete", "T-1", "--yes"), ("note", "T-1", "posing as the person"), ("done", "T-1"),
                 ("approve", "T-1", "codex", "--yes"), ("add", "Another"), ("setup", "--write"), ("update",),
                 # decided on the options as parsed: argparse takes --wri for --write
                 ("setup", "--wri"), ("setup", "--hook"), ("setup", "--remove-hook"), ("setup", "--remove"),
                 ("setup", "--remove-h"), ("update", "--force")):
        code, _, err = run(capsys, *args)
        assert code == 1 and "can't change the board (or Handoff's setup) as you" in err, args
    code, out, _ = run(capsys, "show", "T-1")  # looking is fine
    assert code == 0 and "Task" in out and "posing" not in out
    from handoff import update
    monkeypatch.setattr(update, "INSTALL_INFO", project / "no-install.json")  # (so it looks no further)
    for args in (("update", "--check"), ("update", "--ch")):  # only looks
        err = run(capsys, *args)[2]
        assert "as you" not in err and "Run the installer from a fresh clone once" in err, args
    monkeypatch.delenv("HANDOFF_DEPTH")
    assert run(capsys, "delete", "T-1", "--yes")[0] == 0  # you, at your own terminal


def test_a_missing_reason_or_review_text_says_what_to_type(project, capsys):
    run(capsys, "add", "Task")
    for args, said in ((["status", "T-1", "blocked"], 'Say why it\'s blocked: handoff status T-1 blocked --reason "'),
                       (["review", "T-1", "changes"], 'Say what to change: handoff review T-1 changes "what to change"')):
        with pytest.raises(SystemExit) as stopped:
            cli.main(args)
        assert stopped.value.code == 2 and said in capsys.readouterr().err


def test_watch_prints_the_board_when_output_is_not_a_terminal(project, capsys, monkeypatch):
    run(capsys, "add", "Watched task")
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 1:
            cli.main(["add", "Another task"])  # the board changes: a new copy
        elif len(naps) == 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", nap)
    code, out, _ = run(capsys, "board", "--watch")
    assert code == 0 and out.count("Watched task") == 2 and out.count("Another task") == 1

