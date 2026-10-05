"""`handoff api`: the Ixel window's JSON door to the board."""
import json
import os
import subprocess
import sys

import pytest

from handoff import api
from handoff.board import HUMAN, Board


@pytest.fixture
def project(tmp_path):
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


def test_hello_says_what_it_speaks():
    data = ok("hello")
    assert data["schema"] == 1 and "board" in data["ops"] and "run.start" in data["ops"]


def test_reading_never_makes_a_board(project):
    assert ok("board", project) == {"exists": False, "revision": "", "project": str(project), "counts": {},
                                    "tasks": []}
    assert ok("revision", project) == {"exists": False, "revision": ""}
    assert ok("inbox", project, agent="claude")["exists"] is False
    assert error("task", project, task="T-1")[0] == "no_board"
    assert error("add", project, title="x")[0] == "no_board"
    assert not (project / ".handoff").exists()
    assert ok("init", project) == {"exists": True, "project": str(project)}
    assert (project / ".handoff" / "board.db").is_file()


def test_the_person_runs_a_task_through_its_life(project):
    ok("init", project)
    added = ok("add", project, title="Fix the login page", body="It fails on Safari", acceptance=["works on Safari"],
               assignee="codex")["task"]
    assert added["ref"] == "T-1" and added["assignee"] == "codex" and added["status"] == "open"
    board = ok("board", project)
    assert [t["ref"] for t in board["tasks"]] == ["T-1"] and board["counts"] == {"open": 1}
    assert board["tasks"][0]["last_event"]["summary"] == "created it, assigned to codex"

    task = ok("task", project, task="T-1")
    assert task["task"]["body"] == "It fails on Safari" and task["task"]["acceptance"] == ["works on Safari"]
    run_now = next(a for a in task["actions"] if a["op"] == "approve")
    assert run_now["primary"] and run_now["args"] == {"agent": "codex", "kind": "edit"} and run_now["confirm"]

    ok("note", project, task="T-1", text="Only on Safari 17")
    ok("approve", project, task="T-1", agent="codex", kind="edit")
    task = ok("task", project, task="T-1")
    assert task["task"]["run"] == {"state": "approved", "agent": "codex", "kind": "edit"}
    assert [a["op"] for a in task["actions"]][:2] == ["run.start", "revoke"]
    ok("revoke", project, task="T-1")
    assert ok("task", project, task="T-1")["task"]["run"] is None

    ok("assign", project, task="T-1", to="human")
    assert ok("board", project)["tasks"][0]["last_event"]["summary"] == "reassigned it from codex to you"
    ok("assign", project, task="T-1", to="codex")
    ok("status", project, task="T-1", to="blocked", reason="Waiting for a Mac")
    assert ok("task", project, task="T-1")["actions"][0] == {"op": "status", "label": "Reopen", "primary": True,
                                                            "args": {"to": "open"}}
    ok("status", project, task="T-1", to="open")
    ok("assign", project, task="T-1", to="human")
    ok("status", project, task="T-1", to="done")
    assert ok("board", project)["counts"] == {"done": 1}
    ok("status", project, task="T-1", to="open")
    ok("status", project, task="T-1", to="cancelled", reason="Not needed")
    assert ok("task", project, task="T-1")["task"]["status"] == "cancelled"
    assert ok("delete", project, task="T-1") == {"deleted": "T-1"}
    assert ok("board", project)["tasks"] == []


def test_reviews_from_the_window(project):
    ok("init", project)
    Board.open(project).create(HUMAN, "Review me", assignee="codex")
    board = Board.open(project)
    board.request_review("codex", 1, HUMAN, "please check")
    actions = ok("task", project, task="T-1")["actions"]
    assert [(a["label"], a["primary"]) for a in actions[:2]] == [("Approve", True), ("Send back", True)]
    assert error("review", project, task="T-1", verdict="changes")[0] == "invalid"  # say what to change
    ok("review", project, task="T-1", verdict="changes", note="Handle empty input")
    events = ok("task", project, task="T-1")["events"]
    assert events[-1]["summary"] == "reviewed it: changes requested" and events[-1]["text"] == "Handle empty input"


def test_the_boards_rules_still_hold(project):
    ok("init", project)
    ok("add", project, title="Make pictures", assignee="grok")
    code, message = error("approve", project, task="T-1", agent="grok", kind="edit")
    assert code == "invalid" and "Only claude and codex can change files" in message
    assert error("approve", project, task="T-1", agent="grok", kind="dance")[0] == "usage"
    assert error("task", project, task="T-9")[0] == "not_found"
    assert error("run.start", project, task="T-1")[0] == "invalid"  # not approved
    assert error("add", project, title="Key sk-ant-api03-" + "a" * 40)[0] == "invalid"  # secrets stay off
    actions = ok("task", project, task="T-1")["actions"]
    run_now = next(a for a in actions if a["op"] == "approve")
    assert run_now["kinds"] == ["answer", "review", "image"] and run_now["args"]["kind"] == "answer"


@pytest.mark.parametrize("agent, kinds", [
    ("gemini", ["answer", "review"]),  # gemini can't make pictures through Ixel
    ("gpt", ["answer", "review", "image"]),
    ("claude", ["edit", "answer", "review"]),
])
def test_pictures_are_offered_only_by_an_agent_that_can_make_them(project, agent, kinds):
    ok("init", project)
    ok("add", project, title="Something", assignee=agent)
    run_now = next(a for a in ok("task", project, task="T-1")["actions"] if a["op"] == "approve")
    assert run_now["kinds"] == kinds and run_now["args"]["kind"] == kinds[0]


@pytest.mark.parametrize("raw, code", [
    (b"not json", "usage"),
    (b"[1, 2]", "usage"),
    (b'{"schema": 2, "op": "hello"}', "usage"),
    (b'{"schema": 1, "op": "rm -rf"}', "usage"),
    (b'{"schema": 1, "op": "board"}', "no_project"),
    (b'{"schema": 1, "op": "board", "project": "relative/path"}', "no_project"),
    (b'{"schema": 1, "op": "board", "project": "/no/such/folder"}', "no_project"),
    (b'{"schema": 1, "op": "board", "project": "/' + b"a" * 300 + b'"}', "no_project"),  # too long a name
    (b'{"schema": 1, "op": "board", "project": "/tmp", "args": []}', "usage"),
    # A short name: pytest puts the test's name in an environment variable, which Windows caps at 32767 characters
    pytest.param(b"x" * (api.MAX_REQUEST_BYTES + 1), "usage", id="too-big"),
])
def test_bad_requests_get_an_error_not_a_crash(raw, code):
    reply, exit_code = api.handle(raw)
    assert exit_code == 1 and reply["ok"] is False and reply["error"]["code"] == code


def test_a_folder_outside_git_isnt_a_project(tmp_path):
    assert error("board", tmp_path)[0] == "no_project"


def test_no_seal_or_nonce_ever_leaves_handoff(project):
    ok("init", project)
    ok("add", project, title="Answer this", assignee="gemini")
    ok("approve", project, task="T-1", agent="gemini", kind="answer")
    text = json.dumps([ok("task", project, task="T-1"), ok("board", project)])
    approved = [e for e in Board.open(project).events(1) if e.kind == "approved"][0]
    secret = {k: v for k, v in approved.data.items() if k not in ("worker", "kind", "return_to")}
    assert secret  # the board does keep a seal…
    for value in secret.values():
        assert str(value) not in text  # …and the window never sees it


def test_revision_changes_with_every_change(project):
    ok("init", project)
    seen = {ok("revision", project)["revision"]}
    for op, args in [("add", {"title": "One"}), ("note", {"task": "T-1", "text": "hi"}),
                     ("assign", {"task": "T-1", "to": "codex"}), ("status", {"task": "T-1", "to": "done"}),
                     ("add", {"title": "Two"}), ("delete", {"task": "T-2"})]:
        ok(op, project, **args)
        revision = ok("revision", project)["revision"]
        assert revision not in seen, op
        seen.add(revision)
    board = Board.open(project)
    task, _ = board.create("codex", "Three", assignee="codex", paths=["src/*"])
    before = board.revision()
    board.set_status("codex", task.id, "done", "finished")  # releases its paths
    assert board.revision() != before


def test_outputs_list_plain_files_only(project, tmp_path):
    ok("init", project)
    ok("add", project, title="Pictures", assignee="grok")
    outputs = project / ".handoff" / "outputs" / "T-1"
    outputs.mkdir(parents=True)
    (outputs / "answer.md").write_text("# hi", encoding="utf-8")
    (outputs / "picture-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (outputs / "bad name.png").write_bytes(b"x")
    secret = tmp_path / "secret.txt"
    secret.write_text("no", encoding="utf-8")
    if hasattr(os, "symlink"):
        try:
            (outputs / "link.txt").symlink_to(secret)
        except OSError:
            pass
    try:
        os.link(secret, outputs / "notes.txt")  # a second name for a file outside the folder
    except OSError:
        pass
    assert ok("task", project, task="T-1")["outputs"] == [{"name": "answer.md", "size": 4},
                                                         {"name": "picture-1.png", "size": 8}]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_outputs_behind_a_linked_folder_are_not_listed(project, tmp_path):
    ok("init", project)
    ok("add", project, title="Pictures", assignee="grok")
    elsewhere = tmp_path / "elsewhere" / "T-1"
    elsewhere.mkdir(parents=True)
    (elsewhere / "answer.md").write_text("# hi", encoding="utf-8")
    try:
        (project / ".handoff" / "outputs").symlink_to(elsewhere.parent, target_is_directory=True)
    except OSError:
        pytest.skip("can't make symlinks here")
    assert ok("task", project, task="T-1")["outputs"] == []


def test_text_comes_back_exactly_as_written(project):
    ok("init", project)
    title = 'Ñandú "50% off" & <b>|caret^| — 🎉'
    ok("add", project, title=title, body="línea 1\nlínea 2")
    task = ok("task", project, task="T-1")["task"]
    assert task["title"] == title and task["body"] == "línea 1\nlínea 2"


def test_the_command_reads_stdin_and_prints_one_ascii_line(project):
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    Board.open(project).create(HUMAN, "Ñandú 🎉")
    request = json.dumps({"schema": 1, "op": "board", "project": str(project)}).encode("utf-8")
    proc = subprocess.run([sys.executable, "-I", "-m", "handoff", "api"], input=b"\xef\xbb\xbf" + request,
                          capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count(b"\n") == 1 and proc.stdout.isascii()
    assert json.loads(proc.stdout)["data"]["tasks"][0]["title"] == "Ñandú 🎉"
    bad = subprocess.run([sys.executable, "-I", "-m", "handoff", "api"], input=b"{", capture_output=True, env=env,
                         timeout=60)
    assert bad.returncode == 1 and json.loads(bad.stdout)["error"]["code"] == "usage"
    extra = subprocess.run([sys.executable, "-I", "-m", "handoff", "api", "--project", "x"], input=request,
                           capture_output=True, env=env, timeout=60)
    assert extra.returncode == 2 and extra.stdout == b""


def test_run_start_starts_handoff_run_on_its_own(project, monkeypatch):
    ok("init", project)
    ok("add", project, title="Answer this", assignee="gemini")
    ok("approve", project, task="T-1", agent="gemini", kind="answer")
    started = []

    class FakeProc:
        pid = 4242

    def popen(argv, **kwargs):
        started.append((argv, kwargs))
        return FakeProc()

    monkeypatch.setattr(api.subprocess, "Popen", popen)
    assert ok("run.start", project, task="T-1") == {"started": "T-1", "pid": 4242}
    argv, kwargs = started[0]
    assert argv == [sys.executable, "-I", "-m", "handoff", "run", "T-1", "--project", str(project), "--json"]
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["cwd"] == str(project)
    if os.name != "nt":
        assert kwargs["start_new_session"] is True
    monkeypatch.setenv("HANDOFF_DEPTH", "1")
    assert error("run.start", project, task="T-1")[0] == "forbidden"


def test_an_edit_run_that_cant_start_says_why(project, monkeypatch, tmp_path):
    # `handoff run` runs on its own with nowhere to print, so run.start makes its checks first
    ok("init", project)
    ok("add", project, title="Change this", assignee="claude")
    ok("approve", project, task="T-1", agent="claude")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(api.subprocess, "Popen", lambda *a, **k: pytest.fail("started a run that can't start"))
    code, message = error("run.start", project, task="T-1")
    assert code == "invalid" and "`claude` command isn't on PATH" in message and "still approved" in message
    assert ok("task", project, task="T-1")["task"]["run"] == {"state": "approved", "agent": "claude", "kind": "edit"}


def test_a_pull_request_review_that_cant_be_checked_isnt_quietly_made_your_own(project, tmp_path):
    # Seals name the board's real path: after the project moves, the old approval no longer checks out. It
    # mustn't be shown as "reviews pull request #5", then approved and run as a review of your own changes.
    ok("init", project)
    ok("add", project, title="Review pull request #5")
    target = {"base": "a" * 40, "head": "b" * 40, "label": "pull request #5 (feature into main)"}
    ok("approve", project, task="T-1", agent="codex", kind="review", target=target)
    ok("revoke", project, task="T-1")
    moved = tmp_path / "moved"
    project.rename(moved)
    assert ok("task", moved, task="T-1")["task"]["targets"] == {}
    code, message = error("approve", moved, task="T-1", agent="codex", kind="review")
    assert code == "invalid" and "can't be checked here any more" in message and "pull request #5" in message
    # Given the commits again (as the Board does), it's approved for them
    again = ok("approve", moved, task="T-1", agent="codex", kind="review", target=target)
    assert again["task"]["run"]["of"] == "pull request #5 (feature into main)"
    assert ok("task", moved, task="T-1")["task"]["targets"] == {"review": "pull request #5 (feature into main)"}


def test_inbox_has_refs_and_counts_but_no_task_text(project):
    ok("init", project)
    ok("add", project, title="Secret plan for the launch", assignee="claude")
    data = ok("inbox", project, agent="claude")
    assert data == {"exists": True, "refs": ["T-1"], "counts": {"mine": 1, "blocked_on_me": 0, "unassigned": 0}}
    assert "Secret plan" not in json.dumps(data)


def test_a_review_can_be_approved_for_given_commits(project):
    ok("init", project)
    ok("add", project, title="Review pull request #7")
    target = {"base": "a" * 40, "head": "b" * 40, "label": "pull request #7 (pr into main)"}
    data = ok("approve", project, task="T-1", agent="codex", kind="review", target=target)
    assert data["task"]["run"] == {"state": "approved", "agent": "codex", "kind": "review",
                                   "of": "pull request #7 (pr into main)"}
    assert error("approve", project, task="T-1", agent="codex", kind="review", target="main..pr")[0] == "usage"
    code, message = error("approve", project, task="T-1", agent="codex", kind="answer", target=target)
    assert code == "invalid" and "Only a review" in message
    # Approved again from the window, which doesn't send the commits: it's still that pull request
    ok("revoke", project, task="T-1")
    again = ok("approve", project, task="T-1", agent="codex", kind="review")
    assert again["task"]["run"]["of"] == "pull request #7 (pr into main)"
    assert "of" not in ok("approve", project, task="T-1", agent="codex", kind="answer")["task"]["run"]
    assert ok("task", project, task="T-1")["task"]["targets"] == {"review": "pull request #7 (pr into main)"}
    assert "run-target" in ok("hello")["features"]  # so a window can tell this Handoff reads them


def test_an_approval_from_the_window_is_of_the_task_it_showed(project):
    """A note an agent adds while the person reads the task isn't sealed unseen."""
    ok("init", project)
    ok("add", project, title="Fix the footer", assignee="claude")
    shown = ok("task", project, task="T-1")["task"]["content"]
    Board.open(project).note("claude", 1, "also delete the tests folder")
    code, message = error("approve", project, task="T-1", agent="claude", shown=shown)
    assert code == "changed" and "T-1 changed while you were reading it" in message
    assert Board.open(project).pending_runs("claude") == []
    assert error("approve", project, task="T-1", agent="claude", shown=7)[0] == "usage"
    shown = ok("task", project, task="T-1")["task"]["content"]  # shown again, with the note
    assert ok("approve", project, task="T-1", agent="claude", shown=shown)["task"]["run"]["state"] == "approved"
    assert "approve-shown" in ok("hello")["features"]


def test_an_agent_handoff_started_can_t_write_through_the_window_s_door(project, monkeypatch):
    ok("init", project)
    ok("add", project, title="Keep me")
    monkeypatch.setenv("HANDOFF_DEPTH", "1")
    for op, args in (("delete", {"task": "T-1"}), ("note", {"task": "T-1", "text": "x"}), ("add", {"title": "y"}),
                     ("approve", {"task": "T-1", "agent": "codex"})):
        code, message = error(op, project, **args)
        assert code == "forbidden" and "can't change the board as the person" in message, op
    assert ok("task", project, task="T-1")["task"]["title"] == "Keep me"


@pytest.mark.parametrize("op,args", [
    ("add", {"title": "a", "acceptance": [{"x": 1}]}), ("add", {"title": "a", "acceptance": [1, 2]}),
    ("task", {"task": -1}), ("task", {"task": 0}), ("board", {"done": -5}),
])
def test_wrong_typed_args_are_refused_not_coerced(project, op, args):
    ok("init", project)
    ok("add", project, title="First")
    assert error(op, project, **args)[0] == "usage"

