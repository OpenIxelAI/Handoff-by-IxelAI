"""The board: every state transition (legal and illegal), authorization, limits and history, and deleting for
good: what's deleted, cleaned or past its keep leaves no trace in the board's files."""
import json
import os
import sqlite3
import sys

import pytest

from handoff import board as board_module
from handoff.board import HUMAN, Board, BoardError, Forbidden, NoBoard, NotFound, parse_task_id
from handoff.describe import describe


@pytest.fixture
def board(tmp_path):
    return Board.open(tmp_path)


def status_of(board, task_id):
    return board.get(task_id).status


def kinds(board, task_id):
    return [e.kind for e in board.events(task_id)]


def new(board, actor="claude", assignee="codex", **kwargs):
    task, _ = board.create(actor, kwargs.pop("title", "Write the tests"), assignee=assignee, **kwargs)
    return task


# ── Creating ─────────────────────────────────────────────────────────────────

def test_create_stores_everything_and_logs_it(board):
    task, overlaps = board.create("claude", "Write the tests", "Cover **every** route.\n\n- one\n- two",
                                  ["tests pass", "  ", "no network"], assignee="codex",
                                  paths=["tests/"], branch="feature/tests")
    assert task.ref == "T-1" and task.status == "open" and task.assignee == "codex"
    assert task.acceptance == ["tests pass", "no network"]
    assert task.branch == "feature/tests" and task.created_by == "claude"
    assert overlaps == []
    assert kinds(board, task.id) == ["created", "claim_paths"]
    assert [c.path_glob for c in board.active_claims()] == ["tests"]


def test_assigning_a_task_to_yourself_claims_it(board):
    task = new(board, "claude", "claude")
    assert task.status == "claimed"
    assert kinds(board, task.id) == ["created", "claimed"]


def test_the_person_assigning_to_themselves_leaves_it_open(board):
    assert new(board, HUMAN, HUMAN).status == "open"


def test_subtasks_need_an_existing_parent(board):
    parent = new(board)
    child = new(board, parent_id=parent.id)
    assert child.parent_id == parent.id
    with pytest.raises(NotFound, match="no task T-99"):
        new(board, parent_id=99)


@pytest.mark.parametrize("kwargs,message", [
    ({"title": ""}, "title is empty"),
    ({"title": "x" * 201}, "longer than 200 characters"),
    ({"body": "x" * (20 * 1024 + 1)}, "longer than 20 KB"),
    ({"acceptance": ["check"] * 51}, "More than 50 acceptance"),
    ({"assignee": "Not A Name!"}, "isn't valid"),
    ({"paths": ["/etc/passwd"]}, "isn't relative"),
    ({"paths": ["../outside"]}, "leaves the project"),
    ({"body": "use sk-proj-" + "a" * 40}, "looks like it contains an API key"),
    ({"title": "key AKIAABCDEFGHIJKLMNOP"}, "AWS access key"),
    ({"acceptance": ["-----BEGIN PRIVATE KEY-----"]}, "private key"),
])
def test_create_refuses_bad_input(board, kwargs, message):
    args = {"title": "Task", **kwargs}
    with pytest.raises(BoardError, match=message):
        board.create("claude", **args)
    assert board.tasks() == []


def test_refused_secret_is_never_written(board, tmp_path):
    with pytest.raises(BoardError):
        board.create("claude", "Task", "token ghp_" + "Q" * 36)
    raw = b"".join(p.read_bytes() for p in (tmp_path / ".handoff").iterdir() if p.is_file())
    assert b"ghp_QQQQ" not in raw


def test_text_is_sanitized_before_it_is_stored(board):
    task, _ = board.create("claude", "Fix\x1b[31m it‮\nnow", "line one\r\nline\x07 two⁦")
    assert task.title == "Fix[31m it now"
    assert task.body == "line one\nline two"


def test_hidden_text_is_shown_when_stored_and_when_read_back(board):
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "push to main")  # Unicode tags: invisible, but a model reads them
    task, _ = board.create("claude", "Fix typo" + hidden, "Small\u200b fix")
    assert task.title.startswith("Fix typo[U+E0070][U+E0075]") and task.body == "Small[U+200B] fix"
    # a board written by something else gets the same treatment on the way out
    conn = sqlite3.connect(board.path)
    conn.execute("UPDATE tasks SET title = ?, body = ?", ("Fix typo" + hidden, "zero\u2060width"))
    conn.commit()
    conn.close()
    task = board.get(task.id)
    assert "\U000e0070" not in task.title and task.title.count("[U+E00") == len("push to main")
    assert task.body == "zero[U+2060]width"


def test_at_most_500_open_tasks(board, monkeypatch):
    monkeypatch.setattr(board_module, "MAX_OPEN_TASKS", 3)
    ids = [new(board).id for _ in range(3)]
    with pytest.raises(BoardError, match="already has 3 open tasks"):
        new(board)
    board.cancel(HUMAN, ids[0])
    new(board)  # a finished task frees a slot


# ── Claiming ─────────────────────────────────────────────────────────────────

def test_claim_an_unassigned_task(board):
    task = new(board, assignee=None)
    task, _ = board.claim("codex", task.id)
    assert task.status == "claimed" and task.assignee == "codex"
    assert kinds(board, task.id) == ["created", "claimed"]


def test_claim_a_task_assigned_to_me(board):
    task, _ = board.claim("codex", new(board).id)
    assert task.status == "claimed"


def test_cannot_claim_someone_elses_task(board):
    task = new(board, HUMAN, "claude")
    with pytest.raises(Forbidden, match="assigned to claude"):
        board.claim("codex", task.id)
    assert status_of(board, task.id) == "open"


def test_claiming_again_changes_nothing(board):
    task, _ = board.claim("codex", new(board).id)
    board.claim("codex", task.id)
    assert kinds(board, task.id) == ["created", "claimed"]


def test_claim_a_handed_back_task(board):
    task = new(board, "claude", "claude")
    board.pass_task("claude", task.id, "codex", done="Did half")
    task, _ = board.claim("codex", task.id)
    assert task.status == "claimed" and task.assignee == "codex"


@pytest.mark.parametrize("finish", ["done", "cancelled"])
def test_cannot_claim_a_finished_task(board, finish):
    task = new(board, assignee=None)
    if finish == "done":
        board.set_status(HUMAN, task.id, "done")
    else:
        board.cancel(HUMAN, task.id)
    with pytest.raises(BoardError, match=f"is {finish}"):
        board.claim("codex", task.id)


def test_claiming_an_unassigned_blocked_task_takes_it_but_keeps_it_blocked(board):
    # The person blocked it with nobody on it: nobody but them could have set it open again, and an agent
    # claiming it heard "it's yours" while nothing changed
    task = new(board, assignee=None)
    board.set_status(HUMAN, task.id, "blocked", reason="Waiting for the API keys")
    task, _ = board.claim("codex", task.id)
    assert task.status == "blocked" and task.assignee == "codex"
    assert kinds(board, task.id) == ["created", "status", "claimed"]
    assert describe(board.events(task.id)[-1]) == "claimed it, still blocked"
    assert board.inbox("codex").mine == [task]
    assert board.set_status("codex", task.id, "open", reason="The keys arrived").status == "open"


def test_claiming_a_blocked_task_of_your_own_changes_nothing(board):
    task = new(board, "codex", "codex")
    board.set_status("codex", task.id, "blocked", reason="Waiting")
    board.claim("codex", task.id)
    assert kinds(board, task.id) == ["created", "claimed", "status"]


def test_cannot_claim_a_task_in_review(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Check the endpoint")
    with pytest.raises(BoardError, match="waiting for your review"):
        board.claim("codex", task.id)


def test_overlapping_path_claims_warn_but_succeed(board):
    api = new(board, "claude", "claude", paths=["src/api.py"])
    tests, found = board.create("claude", "Tests", assignee="codex", paths=["src", "tests/test_api.py"])
    assert [(o.path, o.other_task_id, o.other_path) for o in found] == [("src", api.id, "src/api.py")]
    assert "src overlaps src/api.py, claimed by T-1, assigned to claude" in found[0].describe()
    _, found = board.claim("codex", tests.id, ["src/api.py"])
    assert found[0].describe() == "src/api.py is also claimed by T-1, assigned to claude"
    event = board.events(tests.id)[-1]
    assert event.kind == "claim_paths" and event.data["overlaps"][0]["task_id"] == api.id


def test_both_tasks_see_an_overlap_whoever_claimed_first(board):
    api = new(board, "claude", "claude", paths=["src/api.py"])
    tests, _ = board.create("codex", "Tests", assignee="codex", paths=["src"])
    [first] = board.overlaps_for(api.id)
    assert first.describe() == "src/api.py overlaps src, claimed by T-2, assigned to codex"
    [second] = board.overlaps_for(tests.id)
    assert second.describe() == "src overlaps src/api.py, claimed by T-1, assigned to claude"
    board.set_status("codex", tests.id, "done", reason="Not needed")
    assert board.overlaps_for(api.id) == []


def test_finished_tasks_release_their_claims(board):
    api = new(board, "claude", "claude", paths=["src/api.py"])
    board.set_status("claude", api.id, "done", reason="Shipped")
    assert board.active_claims() == []
    assert kinds(board, api.id)[-2:] == ["release_paths", "status"]
    _, found = board.create("claude", "Next", paths=["src/api.py"])
    assert found == []


# ── Notes ────────────────────────────────────────────────────────────────────

def test_a_note_from_the_assignee_starts_the_work(board):
    task, _ = board.claim("codex", new(board).id)
    task = board.note("codex", task.id, "Started on the fixtures")
    assert task.status == "in_progress"
    assert board.events(task.id)[-1].text == "Started on the fixtures"
    from handoff.describe import describe
    assert describe(board.events(task.id)[-1]) == "added a note and set it claimed → in_progress"  # in the history
    board.note("codex", task.id, "More")
    assert describe(board.events(task.id)[-1]) == "added a note"


def test_only_the_assignee_can_add_notes(board):
    task = new(board)
    with pytest.raises(Forbidden, match="only its assignee can add notes"):
        board.note("claude", task.id, "I'll just do it")
    board.note(HUMAN, task.id, "Please use pytest")  # the person can always comment
    assert status_of(board, task.id) == "open"


def test_notes_on_a_blocked_task_keep_it_blocked(board):
    task = new(board, "codex", "codex")
    board.set_status("codex", task.id, "blocked", reason="Needs the endpoint")
    board.note("codex", task.id, "Still waiting")
    assert status_of(board, task.id) == "blocked"


def test_empty_and_oversized_notes_are_refused(board):
    task = new(board, "codex", "codex")
    with pytest.raises(BoardError, match="note is empty"):
        board.note("codex", task.id, " \x1b ")
    with pytest.raises(BoardError, match="20 KB"):
        board.note("codex", task.id, "é" * 11_000)  # 22 KB in UTF-8


def test_agents_cannot_note_finished_tasks(board):
    task = new(board, "codex", "codex")
    board.set_status("codex", task.id, "done", reason="Trivial")
    with pytest.raises(BoardError, match="is done"):
        board.note("codex", task.id, "one more thing")
    board.note(HUMAN, task.id, "for the record")


# ── Handing off ──────────────────────────────────────────────────────────────

def test_pass_reassigns_and_records_the_handoff(board):
    task, _ = board.claim("codex", new(board).id)
    task = board.pass_task("codex", task.id, "claude", done="Tests written in tests/test_api.py",
                           left="2 failing until the endpoint lands", verify="pytest tests/test_api.py",
                           files=["tests/test_api.py"], branch="feature/tests")
    assert task.status == "handed_off" and task.assignee == "claude" and task.branch == "feature/tests"
    event = board.events(task.id)[-1]
    assert event.kind == "handoff" and event.actor == "codex"
    assert event.data == {"to": "claude", "done": "Tests written in tests/test_api.py",
                          "left": "2 failing until the endpoint lands", "verify": "pytest tests/test_api.py",
                          "files": ["tests/test_api.py"], "branch": "feature/tests"}
    assert board.inbox("claude").mine == [task]


def test_pass_rules(board):
    task, _ = board.claim("codex", new(board).id)
    with pytest.raises(BoardError, match="to yourself"):
        board.pass_task("codex", task.id, "codex", done="x")
    with pytest.raises(BoardError, match="`done` is empty"):
        board.pass_task("codex", task.id, "claude", done="")
    with pytest.raises(Forbidden):
        board.pass_task("claude", task.id, "human", done="Taking it over")
    with pytest.raises(BoardError, match="In total|in total"):
        board.pass_task("codex", task.id, "claude", done="a" * 15_000, left="b" * 15_000)
    assert status_of(board, task.id) == "claimed"


def test_reviewer_cannot_pass_instead_of_reviewing(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Please check")
    with pytest.raises(BoardError, match="use handoff_review"):
        board.pass_task("codex", task.id, "human", done="Not mine")


# ── Reviews ──────────────────────────────────────────────────────────────────

def test_review_round_trip_and_done(board):
    task = new(board, "claude", "claude")
    task = board.request_review("claude", task.id, "codex", "Check the endpoint against the tests")
    assert task.status == "in_review" and task.assignee == "codex"
    assert board.inbox("codex").mine == [task]

    task = board.review("codex", task.id, "changes", "Missing the 404 case")
    assert task.status == "handed_off" and task.assignee == "claude"
    with pytest.raises(BoardError, match="hasn't had an approving review"):
        board.set_status("claude", task.id, "done")

    board.request_review("claude", task.id, "codex", "Added the 404 case")
    task = board.review("codex", task.id, "approve", "Looks right")
    assert task.status == "handed_off" and task.assignee == "claude"
    task = board.set_status("claude", task.id, "done")
    assert task.status == "done"
    assert kinds(board, task.id) == ["created", "claimed", "review_requested", "review", "review_requested",
                                     "review", "status"]


def test_review_rules(board):
    task = new(board, "claude", "claude")
    with pytest.raises(BoardError, match="isn't waiting for a review"):
        board.review("codex", task.id, "approve")
    with pytest.raises(BoardError, match="your own work"):
        board.request_review("claude", task.id, "claude", "Check me")
    with pytest.raises(BoardError, match="`what` is empty"):
        board.request_review("claude", task.id, "codex", "")
    board.request_review("claude", task.id, "codex", "Check it")
    with pytest.raises(BoardError, match="already in review"):
        board.request_review(HUMAN, task.id, "gemini", "Check it too")
    with pytest.raises(Forbidden, match="your own work"):
        board.review("claude", task.id, "approve")  # can't approve your own work either
    with pytest.raises(Forbidden, match="only its reviewer can review"):
        board.review("gemini", task.id, "approve")
    with pytest.raises(BoardError, match='"approve" or "changes"'):
        board.review("codex", task.id, "lgtm")
    with pytest.raises(BoardError, match="review.s text is empty"):
        board.review("codex", task.id, "changes", "")
    with pytest.raises(BoardError, match="waiting for review"):
        board.set_status("codex", task.id, "done", reason="fine")


def test_nobody_reviews_their_own_work_even_once_it_is_reassigned_to_them(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Check it")
    # the person can't hand the review to the work's own author...
    with pytest.raises(BoardError, match="review of claude's own work, so claude can't be its reviewer"):
        board.assign(HUMAN, task.id, "claude")
    assert board.get(task.id).assignee == "codex"
    # ...and if a board has it that way anyway (made by an older Handoff), the author still can't review it
    conn = sqlite3.connect(board.path)
    conn.execute("UPDATE tasks SET assignee = 'claude'")
    conn.commit()
    conn.close()
    for review in (board.review, board.check_review):
        with pytest.raises(Forbidden, match="your own work"):
            review("claude", task.id, "approve")
    assert board.get(task.id).status == "in_review"
    # another reviewer, or the person, still can
    assert board.assign(HUMAN, task.id, "gemini").assignee == "gemini"
    assert board.review(HUMAN, task.id, "approve", "Fine").assignee == "claude"


def test_a_refusal_never_suggests_handing_a_review_to_its_author(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Check it")
    for act in (lambda: board.note("claude", task.id, "one more thing"),
                lambda: board.set_status("claude", task.id, "done", reason="fine"),
                lambda: board.review("gemini", task.id, "approve")):
        with pytest.raises(Forbidden) as refused:
            act()
        assert "handoff assign" not in str(refused.value) and "in review with codex" in str(refused.value)


@pytest.mark.parametrize("word", ["me", "you", "myself", "Me"])
def test_the_persons_own_words_are_never_an_agents_name(board, word):
    # at the command line "me" is the person, so `handoff assign T-1 me` would never reach an agent called me
    with pytest.raises(BoardError, match="isn't a name an agent can have"):
        board_module.check_name(word)
    with pytest.raises(BoardError, match="isn't a name an agent can have"):
        new(board, word, "codex")
    with pytest.raises(BoardError, match="isn't a name an agent can have"):
        new(board, "claude", word)


def test_the_person_can_review_anything_in_review(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Check it")
    task = board.review(HUMAN, task.id, "approve", "I checked it myself")
    assert task.assignee == "claude"


def test_a_panel_verdict_is_kept_with_the_review(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Check it")
    board.review("codex", task.id, "approve", "ok", panel={"verdict": "Correct", "confidence": "high"})
    assert board.events(task.id)[-1].data["panel"] == {"verdict": "Correct", "confidence": "high"}


def checked_task_in_review(board):
    task, _ = board.create("claude", "Endpoint", acceptance=["GET works", "POST works", "404 on a missing id"],
                           assignee="claude")
    return board.request_review("claude", task.id, "codex", "Check it")


def test_a_review_marks_each_acceptance_check(board):
    task = checked_task_in_review(board)
    board.review("codex", task.id, "changes", "No 404 yet", checks=[
        {"check": 3, "result": "not_met", "note": "returns 500"}, {"check": 1, "result": "met"}])
    assert board.events(task.id)[-1].data["checks"] == [
        {"check": 1, "text": "GET works", "result": "met", "note": ""},
        {"check": 2, "text": "POST works", "result": "not_checked", "note": ""},  # left out: not checked
        {"check": 3, "text": "404 on a missing id", "result": "not_met", "note": "returns 500"}]


def test_marks_are_optional_and_only_stored_when_given(board):
    task = checked_task_in_review(board)
    board.review("codex", task.id, "approve", "ok")
    assert "checks" not in board.events(task.id)[-1].data


def test_an_approval_cant_leave_a_check_not_met(board):
    task = checked_task_in_review(board)
    for review in (board.review, board.check_review):
        with pytest.raises(BoardError, match="marked checks 1, 3 not met, so this can't be an approval"):
            review("codex", task.id, "approve", "ok", checks=[{"check": 1, "result": "not_met"},
                                                             {"check": 3, "result": "not_met"}])
    assert status_of(board, task.id) == "in_review"
    board.review("codex", task.id, "approve", "Couldn't run it here", checks=[
        {"check": 1, "result": "met"}, {"check": 3, "result": "not_checked", "note": "no database here"}])
    assert status_of(board, task.id) == "handed_off"


@pytest.mark.parametrize("marks,message", [
    ([{"check": 0, "result": "met"}], "isn't a check on T-1; its checks are numbered 1 to 3"),
    ([{"check": 4, "result": "met"}], "isn't a check on T-1"),
    ([{"check": "1", "result": "met"}], "isn't a check on T-1"),
    ([{"check": True, "result": "met"}], "isn't a check on T-1"),
    ([{"check": 1, "result": "met"}, {"check": 1, "result": "not_met"}], "Check 1 is marked twice"),
    ([{"check": 1, "result": "pass"}], "the result must be met, not_met, not_checked"),
    (["1=met"], "needs a number, a result"),
    ([{"check": 1, "result": "met", "note": "x" * 501}], "longer than 500 characters"),
    ([{"check": 1, "result": "met", "note": "token ghp_" + "a" * 36}], "Refused"),
])
def test_check_marks_are_checked(board, marks, message):
    task = checked_task_in_review(board)
    with pytest.raises(BoardError, match=message):
        board.review("codex", task.id, "changes", "see marks", checks=marks)
    assert status_of(board, task.id) == "in_review"


def test_a_task_without_checks_has_none_to_mark(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Check it")
    with pytest.raises(BoardError, match="T-1 has no acceptance checks to mark"):
        board.review("codex", task.id, "approve", checks=[{"check": 1, "result": "met"}])


# ── Status and the done rule ─────────────────────────────────────────────────

def test_blocked_needs_a_reason_and_can_wait_on_someone(board):
    task = new(board, "codex", "codex")
    with pytest.raises(BoardError, match="reason is empty"):
        board.set_status("codex", task.id, "blocked")
    task = board.set_status("codex", task.id, "blocked", reason="Needs the endpoint", waiting_on="claude")
    assert task.status == "blocked" and task.waiting_on == "claude"
    assert board.inbox("claude").blocked_on_me == [task]
    task = board.set_status("codex", task.id, "open", reason="Endpoint landed")
    assert task.status == "open" and task.waiting_on is None and task.assignee == "codex"
    assert board.inbox("claude").blocked_on_me == []


def test_status_values_are_limited(board):
    task = new(board, "codex", "codex")
    for bad in ("cancelled", "in_review", "handed_off", "wip"):
        with pytest.raises(BoardError, match="must be"):
            board.set_status("codex", task.id, bad)


def test_only_the_assignee_changes_status(board):
    task = new(board)
    with pytest.raises(Forbidden):
        board.set_status("claude", task.id, "done", reason="I say so")


def test_done_rule_review_or_reason(board):
    task = new(board, "codex", "codex")
    with pytest.raises(BoardError, match="give the reason"):
        board.set_status("codex", task.id, "done")
    assert board.set_status("codex", task.id, "done", reason="Docs typo, nothing to review").status == "done"


def test_done_rule_review_only(board):
    board.set_done_rule("review")
    task = new(board, "codex", "codex")
    with pytest.raises(BoardError, match="only after an approving review"):
        board.set_status("codex", task.id, "done", reason="Trust me")
    board.request_review("codex", task.id, "claude", "Check")
    board.review("claude", task.id, "approve", "ok")
    assert board.set_status("codex", task.id, "done").status == "done"


def test_done_rule_any(board):
    board.set_done_rule("any")
    assert board.set_status("codex", new(board, "codex", "codex").id, "done").status == "done"
    with pytest.raises(BoardError):
        board.set_done_rule("never")


def test_a_handoff_after_approval_needs_a_new_review(board):
    board.set_done_rule("review")
    task = new(board, "codex", "codex")
    board.request_review("codex", task.id, "claude", "Check")
    board.review("claude", task.id, "approve", "ok")
    board.pass_task("codex", task.id, "claude", done="Changed more after the review")
    with pytest.raises(BoardError, match="approving review"):
        board.set_status("claude", task.id, "done")


def test_the_person_is_not_bound_by_the_done_rule(board):
    board.set_done_rule("review")
    assert board.set_status(HUMAN, new(board).id, "done").status == "done"


@pytest.mark.parametrize("how", ["open", "blocked", "done", "cancel"])
def test_taking_a_task_out_of_review_hands_it_back_to_its_author(board, how):
    # Only the person can; before, the reviewer was left owning an open (or reopened) task
    task = new(board, "claude", "claude")
    board.claim("claude", task.id)
    board.request_review("claude", task.id, "codex", "Check the endpoint")
    assert board.get(task.id).assignee == "codex"
    if how == "cancel":
        task = board.cancel(HUMAN, task.id, "Not needed after all")
    else:
        task = board.set_status(HUMAN, task.id, how, reason="Changed my mind")
    assert task.status == ("cancelled" if how == "cancel" else how) and task.assignee == "claude"
    event = board.events(task.id)[-1]
    assert event.kind == "status" and event.data["assignee"] == "claude"
    assert describe(event).startswith(f"set it in_review → {task.status}, back with claude")
    if how in ("done", "cancel"):
        assert board.set_status(HUMAN, task.id, "open").assignee == "claude"


def test_an_agent_still_cannot_take_a_task_out_of_review(board):
    task = new(board, "claude", "claude")
    board.request_review("claude", task.id, "codex", "Check it")
    with pytest.raises(BoardError, match="waiting for review"):
        board.set_status("codex", task.id, "open")
    assert board.get(task.id).assignee == "codex"


def test_only_the_person_reopens_finished_tasks(board):
    task = new(board, "codex", "codex")
    board.set_status("codex", task.id, "done", reason="ok")
    with pytest.raises(BoardError, match="is done"):
        board.set_status("codex", task.id, "open")
    assert board.set_status(HUMAN, task.id, "open").status == "open"


def test_revision_changes_when_a_setting_changes(board):
    before = board.revision()
    board.set_keep_days(HUMAN, 30)
    after = board.revision()
    assert after != before
    board.set_done_rule("any")
    assert board.revision() not in (before, after)
    board.set_setting("gitignore_asked", "yes")
    assert len({before, after, board.revision()}) == 3


def test_overlaps_for_all_matches_overlaps_for(board):
    a = new(board, "codex", "codex", paths=["src"])
    b = new(board, "claude", "claude", paths=["src/api.py"])
    c = new(board, paths=["docs"])
    found = board.overlaps_for_all([a.id, b.id, c.id])
    assert found == {t.id: board.overlaps_for(t.id) for t in (a, b, c)}
    assert [o.other_task_id for o in found[a.id]] == [b.id] and found[c.id] == []
    assert board.overlaps_for_all([]) == {}


def test_opening_a_board_that_isnt_there_says_so(tmp_path):
    with pytest.raises(NoBoard):
        Board.open(tmp_path, create=False)
    with pytest.raises(NoBoard):
        Board.open_read_only(tmp_path)
    assert issubclass(NoBoard, NotFound)


# ── The person's controls ────────────────────────────────────────────────────

def test_assign(board):
    task, _ = board.claim("codex", new(board).id)
    board.note("codex", task.id, "working")
    with pytest.raises(Forbidden, match="Only the person"):
        board.assign("codex", task.id, "claude")
    task = board.assign(HUMAN, task.id, "claude")
    assert task.assignee == "claude" and task.status == "open"
    event = board.events(task.id)[-1]
    assert event.kind == "assigned" and event.data == {"from": "codex", "to": "claude"}
    assert board.assign(HUMAN, task.id, "claude") == task  # no change, no event
    assert board.assign(HUMAN, task.id, None).assignee is None


def test_cancel(board):
    task = new(board, paths=["docs"])
    with pytest.raises(Forbidden):
        board.cancel("claude", task.id)
    task = board.cancel(HUMAN, task.id, "Not needed")
    assert task.status == "cancelled" and board.active_claims() == []
    with pytest.raises(BoardError, match="is cancelled"):
        board.cancel(HUMAN, task.id)


def test_delete(board):
    parent = new(board)
    child = new(board, parent_id=parent.id)
    with pytest.raises(Forbidden):
        board.delete("claude", child.id)
    with pytest.raises(BoardError, match="has subtasks"):
        board.delete(HUMAN, parent.id)
    board.delete(HUMAN, child.id)
    board.delete(HUMAN, parent.id)
    assert board.tasks() == [] and board.events(parent.id) == []


def test_history_cannot_be_edited_or_deleted(board):
    task = new(board)
    conn = sqlite3.connect(board.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE events SET text = 'rewritten' WHERE task_id = ?", (task.id,))
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM events WHERE task_id = ?", (task.id,))
    finally:
        conn.close()


def test_unknown_tasks(board):
    for call in (lambda: board.get(7), lambda: board.claim("codex", 7), lambda: board.note("codex", 7, "x")):
        with pytest.raises(NotFound, match="no task T-7"):
            call()


# ── Reading ──────────────────────────────────────────────────────────────────

def test_inbox(board):
    mine = new(board, "claude", "codex", title="Mine")
    new(board, "claude", "claude", title="Claude's")
    free = new(board, "claude", None, title="Anyone")
    done = new(board, "claude", "codex", title="Finished")
    board.set_status(HUMAN, done.id, "done")
    inbox = board.inbox("codex")
    assert [t.id for t in inbox.mine] == [mine.id]
    assert [t.id for t in inbox.unassigned] == [free.id]


def test_tasks_filters_and_last_events(board):
    a = new(board, "claude", "codex")
    b = new(board, "claude", "claude")
    assert {t.id for t in board.tasks(statuses=["claimed"])} == {b.id}
    assert {t.id for t in board.tasks(assignee="codex")} == {a.id}
    assert board.tasks(statuses=[]) == []
    last = board.last_events([a.id, b.id])
    assert last[a.id].kind == "created" and last[b.id].kind == "claimed"
    assert board.counts() == {"open": 1, "claimed": 1}


@pytest.mark.parametrize("raw,expected", [("T-12", 12), ("t-3", 3), ("T12", 12), ("#4", 4), ("5", 5)])
def test_parse_task_id(raw, expected):
    assert parse_task_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "T-", "T-x", "12a", "T--1", "٣"])
def test_parse_task_id_refuses(raw):
    with pytest.raises(BoardError, match="look like T-12"):
        parse_task_id(raw)


# ── The file ─────────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_board_is_private(tmp_path):
    board = Board.open(tmp_path)
    assert os.stat(board.path.parent).st_mode & 0o777 == 0o700
    assert os.stat(board.path).st_mode & 0o777 == 0o600
    new(board)
    for extra in board.path.parent.glob("board.db-*"):  # SQLite's WAL files follow the board's mode
        assert os.stat(extra).st_mode & 0o077 == 0, extra


def test_board_keeps_itself_out_of_git(tmp_path):
    Board.open(tmp_path)
    assert (tmp_path / ".handoff" / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"


def test_board_uses_wal(board):
    conn = sqlite3.connect(board.path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_open_without_create(tmp_path):
    with pytest.raises(NotFound, match="no board"):
        Board.open(tmp_path, create=False)
    assert not (tmp_path / ".handoff").exists()


def test_a_newer_board_is_refused(tmp_path):
    board = Board.open(tmp_path)
    board.set_setting("schema_version", "99")
    with pytest.raises(BoardError, match="newer version of Handoff"):
        Board.open(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlinked_board_folder_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".handoff").symlink_to(elsewhere)
    with pytest.raises(BoardError, match="symbolic link"):
        Board.open(project)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("name", ["board.db", "board.db-wal", "board.db-shm", "board.db-journal"])
@pytest.mark.parametrize("dangling", [True, False])
def test_a_symlinked_board_file_is_refused(tmp_path, name, dangling):
    # a cloned repository's real .handoff folder, with the board (or SQLite's -wal, -shm or -journal file) leading
    # elsewhere
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "evil.db"
    if not dangling:
        Board.open(elsewhere)
        target = elsewhere / ".handoff" / "board.db"
    project = tmp_path / "project"
    (project / ".handoff").mkdir(parents=True)
    (project / ".handoff" / name).symlink_to(target)
    before = sorted(p.name for p in elsewhere.rglob("*"))
    for open_it in (lambda: Board.open(project), lambda: Board.open(project, create=False),
                    lambda: Board.open_read_only(project)):
        with pytest.raises(BoardError, match="symbolic link"):
            open_it()
    assert sorted(p.name for p in elsewhere.rglob("*")) == before


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_new_board_never_writes_through_a_planted_gitignore_link(tmp_path):
    outside = tmp_path / "autostart.desktop"
    project = tmp_path / "project"
    (project / ".handoff").mkdir(parents=True)
    (project / ".handoff" / ".gitignore").symlink_to(outside)
    Board.open(project)
    assert not outside.exists()


def test_read_only_board_never_changes_anything(tmp_path):
    board = Board.open(tmp_path)
    task, _ = board.create("claude", "Read me", assignee="claude")
    folder = tmp_path / ".handoff"
    os.chmod(folder / "board.db", 0o644)
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in folder.iterdir()}
    reader = Board.open_read_only(tmp_path)
    assert [t.ref for t in reader.inbox("claude").mine] == [task.ref]
    with pytest.raises(sqlite3.OperationalError):
        reader.set_setting("done_rule", "any")
    assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in folder.iterdir()} == before
    with pytest.raises(NotFound):
        Board.open_read_only(tmp_path / "nowhere")


def test_read_only_board_sees_changes_still_in_the_wal(tmp_path):
    board = Board.open(tmp_path)
    keep = sqlite3.connect(board.path)  # an open connection keeps the -wal file, as a running app's would
    try:
        keep.execute("SELECT COUNT(*) FROM tasks").fetchall()
        board.create("claude", "Fresh", assignee="claude")
        assert (tmp_path / ".handoff" / "board.db-wal").exists()
        assert [t.title for t in Board.open_read_only(tmp_path).inbox("claude").mine] == ["Fresh"]
    finally:
        keep.close()


def test_event_data_is_json(board):
    task = new(board)
    conn = sqlite3.connect(board.path)
    try:
        raw = conn.execute("SELECT data FROM events WHERE task_id = ?", (task.id,)).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(raw) == {"assignee": "codex", "parent_id": None}


def test_the_agent_that_built_it_cant_review_it_whoever_asks(board):
    # Claude plans it for Codex; Codex builds it and hands it back; Claude asks for a review
    task, _ = board.create("claude", "Tests for /api/orders", assignee="codex")
    board.claim("codex", task.id)
    board.pass_task("codex", task.id, to="claude", done="Tests written", verify="pytest", files=["tests"])
    with pytest.raises(BoardError, match="codex worked on T-1, so it can't review it"):
        board.request_review("claude", task.id, "codex", "Check the tests")
    board.request_review("claude", task.id, "gemini", "Check the tests")
    with pytest.raises(BoardError, match="review of codex's own work"):
        board.assign(HUMAN, task.id, "codex")
    for review in (board.review, board.check_review):
        with pytest.raises(Forbidden, match="your own work"):
            review("codex", task.id, "approve")
    assert board.review("gemini", task.id, "approve", "Covers GET and POST").assignee == "claude"


def test_the_agent_that_planned_a_task_can_review_it(board):
    # Claude plans it for Codex; Codex builds it and asks Claude, or the person asks Claude, to review it
    for asker in ("codex", HUMAN):
        task, _ = board.create("claude", "Tests for /api/orders", assignee="codex")
        board.claim("codex", task.id)
        assert board.request_review(asker, task.id, "claude", "Check the tests").assignee == "claude"
        assert board.review("claude", task.id, "approve", "Good").status != "in_review"


def _run(board, task, agent, kind, finish=True):
    board.approve(HUMAN, task.id, agent, return_to="claude", kind=kind)
    [(_, approval)] = board.pending_runs(agent)
    board.start_run(agent, task.id, approval.id,
                    {"branch": f"handoff/{task.ref}"} if kind == "edit" else {"kind": kind})
    if finish:
        board.finish_run(agent, task.id, "claude", done="Done")
    else:
        board.fail_run(agent, task.id, "claude", "It stopped")


def test_a_worker_run_that_only_reviewed_isnt_work_on_the_task(board):
    task, _ = board.create("claude", "The endpoint", assignee="claude")
    _run(board, task, "gemini", "review")
    board.claim("claude", task.id)
    assert board.request_review("claude", task.id, "gemini", "Check it").assignee == "gemini"


@pytest.mark.parametrize("finish", [True, False])
def test_an_agent_whose_worker_run_edited_the_task_cant_review_it(board, finish):
    task, _ = board.create(HUMAN, "The endpoint", assignee="codex")
    _run(board, task, "codex", "edit", finish=finish)  # a failed run's commits are still on its branch
    if not finish:
        board.assign(HUMAN, task.id, "claude")
    board.claim("claude", task.id)
    with pytest.raises(BoardError, match="codex worked on T-1"):
        board.request_review("claude", task.id, "codex", "Check it")


# ── Deleting for good ────────────────────────────────────────────────────────

CANARY = "handoff-canary-4b1d9e7a"  # text no board has by chance


def _in_files(board, text=CANARY):
    """Which of the board's files hold `text`, byte for byte, wherever SQLite put it."""
    wal = board.path.with_name(board.path.name + "-wal")
    return [path.name for path in (board.path, wal) if path.exists() and text.encode("utf-8") in path.read_bytes()]


@pytest.fixture(params=["as built here", "secure_delete off by default"])
def sqlite_build(request, monkeypatch):
    """SQLite as this Python has it, and as SQLite itself ships (deleted rows only marked free), which is what many
    builds keep. Handoff turns it on either way."""
    if request.param != "as built here":
        real = sqlite3.connect

        def connect(*args, **kwargs):
            conn = real(*args, **kwargs)
            conn.execute("PRAGMA secure_delete = OFF")
            return conn
        monkeypatch.setattr(sqlite3, "connect", connect)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def _outputs(root, ref, *names):
    """What a run through Ixel leaves in .handoff/outputs/T-N."""
    folder = root / ".handoff" / "outputs" / ref
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("a result", encoding="utf-8")
    return folder


def test_connections_overwrite_what_they_delete_and_write_no_temporary_files(tmp_path, sqlite_build):
    board = Board.open(tmp_path)
    for conn in (board._connect(), Board.open_read_only(tmp_path)._connect()):
        try:
            assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # in memory, never a temporary file
        finally:
            conn.close()
    conn = board._connect()
    try:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="sees the files SQLite opens through /proc (Linux)")
def test_a_delete_leaves_no_copy_in_a_temporary_file(tmp_path, monkeypatch):
    """Deleting a task, SQLite sets aside what it takes out, in case it has to undo it; past 64 KB, in a temporary
    file in the system's temp folder (removed at once, but the text stays on the disk), unless kept in memory."""
    (tmp_path / "temp").mkdir()
    monkeypatch.setenv("SQLITE_TMPDIR", str(tmp_path / "temp"))  # (so a failing run leaves nothing in /var/tmp)
    opened = set()

    def look():
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if "etilqs_" in target:  # what SQLite names its temporary files
                opened.add(target)
        return 0  # (go on)
    real = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_progress_handler(look, 50)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    (tmp_path / "project").mkdir()
    board = Board.open(tmp_path / "project")
    task = new(board, title=f"Fix {CANARY}")
    for n in range(10):
        board.note(HUMAN, task.id, f"{CANARY} {n} " + "z" * 15_000)
    assert board.delete(HUMAN, task.id) == []
    assert opened == set() and _in_files(board) == []


def test_a_deleted_task_leaves_no_trace_in_the_board_files(tmp_path, sqlite_build):
    board = Board.open(tmp_path)
    idle = sqlite3.connect(board.path)  # a running app's connection, which keeps the -wal file there
    try:
        idle.execute("SELECT COUNT(*) FROM tasks").fetchall()
        kept = new(board, title="Stays on the board")
        task, _ = board.create("claude", f"Fix {CANARY}", f"The body says {CANARY}", [f"Check {CANARY}"],
                               assignee="claude", paths=[f"src/{CANARY}.py"])
        board.note("claude", task.id, f"A note about {CANARY}")
        board.pass_task("claude", task.id, "codex", done=f"Did {CANARY}", files=[f"src/{CANARY}.py"])
        assert _in_files(board)  # (so the check below would see it)
        assert board.delete(HUMAN, task.id) == []
        assert (tmp_path / ".handoff" / "board.db-wal").exists()
        assert _in_files(board) == []
        assert board.get(kept.id).title == "Stays on the board" and board.events(task.id) == []
    finally:
        idle.close()


@pytest.mark.parametrize("written_to", ["board.db-wal", "board.db"])
def test_a_busy_board_is_cleared_the_next_time_it_opens(tmp_path, monkeypatch, sqlite_build, written_to):
    monkeypatch.setattr(board_module, "CLEAR_WAIT_MS", 50)
    board = Board.open(tmp_path)
    idle = sqlite3.connect(board.path)
    reader = sqlite3.connect(board.path, isolation_level=None)
    try:
        if written_to == "board.db-wal":
            idle.execute("SELECT COUNT(*) FROM tasks").fetchall()  # an app's connection keeps the -wal file there
        task = new(board, title=f"Fix {CANARY}")  # (else, the last connection to close writes it into board.db)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM tasks").fetchall()  # another program, in the middle of reading
        assert board.delete(HUMAN, task.id) == []  # the delete doesn't wait on it, or fail over it
        assert board.tasks() == []
        # The reader may still need the old copy, so it stays for now, wherever it was
        assert _in_files(board) == [written_to]
        reader.execute("ROLLBACK")
        Board.open(tmp_path)  # the next time anything opens the board for writing
        assert _in_files(board) == []
    finally:
        reader.close()
        idle.close()


def test_an_older_boards_deleted_text_is_cleared_once(tmp_path, monkeypatch, sqlite_build):
    board = Board.open(tmp_path)
    task = new(board, title=f"Fix {CANARY}")
    # What an older Handoff left behind: a task deleted with nothing overwritten, and no record of clearing it
    conn = sqlite3.connect(board.path, isolation_level=None)
    conn.execute("PRAGMA secure_delete = OFF")
    for table, column in (("claims", "task_id"), ("tasks", "id"), ("events", "task_id")):
        conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (task.id,))
    conn.execute("DELETE FROM meta WHERE key = 'deletions_cleared'")
    conn.close()  # (the last connection: the -wal file goes into board.db)
    assert _in_files(board) == ["board.db"]
    cleared = []
    clear = Board._clear_deleted
    monkeypatch.setattr(Board, "_clear_deleted", lambda self, **kwargs: cleared.append(1) or clear(self, **kwargs))
    Board.open(tmp_path)
    assert _in_files(board) == [] and cleared == [1]
    Board.open(tmp_path)
    assert cleared == [1]  # once


def test_a_new_board_has_nothing_to_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(Board, "_clear_deleted", lambda self, **kwargs: pytest.fail("cleared a new board"))
    board = Board.open(tmp_path)
    assert board.setting("deletions_cleared") == "0"
    Board.open(tmp_path)


def test_delete_takes_the_tasks_results_with_it(board, tmp_path):
    one, two, three = new(board), new(board), new(board)
    gone = _outputs(tmp_path, one.ref, "answer.md", "T-1-1.png", ".answer.md.x1.tmp")
    kept = _outputs(tmp_path, two.ref, "answer.md")
    assert board.delete(HUMAN, one.id) == []
    assert not gone.exists() and (kept / "answer.md").is_file()
    assert board.delete(HUMAN, three.id) == []  # (it had none)


def test_results_that_cant_be_removed_are_said(board, tmp_path, monkeypatch):
    task = new(board)
    _outputs(tmp_path, task.ref, "picture.png")
    monkeypatch.setattr(board_module.shutil, "rmtree", lambda *args, **kwargs: None)  # a picture open in a viewer
    assert board.delete(HUMAN, task.id) == [f"Some of .handoff/outputs/{task.ref} couldn't be removed; a file in it "
                                            "may be open in another program. Delete the folder yourself."]
    with pytest.raises(NotFound):
        board.get(task.id)  # the task itself is gone


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_delete_never_removes_anything_through_a_link(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep me", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    board = Board.open(project)
    one, two = new(board), new(board)
    outputs = project / ".handoff" / "outputs"
    outputs.mkdir()
    (outputs / one.ref).symlink_to(elsewhere, target_is_directory=True)  # as a cloned repository could ship it
    folder = _outputs(project, two.ref, "answer.md")
    (folder / "inside").symlink_to(elsewhere, target_is_directory=True)
    assert board.delete(HUMAN, one.id) == [".handoff/outputs/T-1 isn't a folder Handoff made (it's a link or a "
                                           "file), so it's still there."]
    assert (outputs / one.ref).is_symlink()
    assert board.delete(HUMAN, two.id) == [] and not folder.exists()
    assert (elsewhere / "precious.txt").read_text(encoding="utf-8") == "keep me"

    other = tmp_path / "other"
    other.mkdir()
    board = Board.open(other)
    (other / ".handoff" / "outputs").symlink_to(elsewhere, target_is_directory=True)
    (elsewhere / new(board).ref).mkdir()
    [said] = board.delete(HUMAN, 1)
    assert said.startswith(".handoff/outputs isn't a folder Handoff made") and (elsewhere / "T-1").is_dir()
    assert board.leftover_outputs() == []  # (nothing through that link either)

    (outputs / "T-9").symlink_to(elsewhere, target_is_directory=True)
    assert Board.open(project).leftover_outputs() == []


def test_results_whose_task_is_gone_are_found_and_cleaned(tmp_path):
    board = Board.open(tmp_path)
    assert board.leftover_outputs() == []  # (no outputs folder yet)
    kept = new(board)
    outputs = _outputs(tmp_path, kept.ref, "answer.md").parent
    _outputs(tmp_path, "T-7", "answer.md")  # what deleting T-7 with an older Handoff left
    _outputs(tmp_path, "T-12", "picture.png")
    for name in ("T-07", "T-\u0663", "T-0", "notes"):  # not names Handoff gives a task's folder
        (outputs / name).mkdir()
    (outputs / "T-9").write_text("a file, not a folder", encoding="utf-8")
    assert board.leftover_outputs() == ["T-7", "T-12"]
    with pytest.raises(Forbidden, match="Only the person can clean the board"):
        board.clean_outputs("claude", ["T-7"])
    # Only what the person was shown, and only if its task still isn't on the board
    assert board.clean_outputs(HUMAN, ["T-12", kept.ref, "T-07"]) == (["T-12"], [])
    assert board.leftover_outputs() == ["T-7"]
    assert (outputs / kept.ref / "answer.md").is_file() and (outputs / "T-07").is_dir()


def test_clean_removes_old_finished_tasks_with_everything_they_had(tmp_path, sqlite_build):
    clock = Clock("2026-01-01T00:00:00Z")
    board = Board.open(tmp_path, clock=clock)
    done, _ = board.create(HUMAN, f"Done long ago {CANARY}", f"The body says {CANARY}", paths=[f"docs/{CANARY}"])
    board.set_status(HUMAN, done.id, "done")
    cancelled = new(board)
    board.cancel(HUMAN, cancelled.id)
    active = new(board)
    parent = new(board)
    child = new(board, parent_id=parent.id)
    for task in (child, parent):
        board.set_status(HUMAN, task.id, "done")
    waits = new(board)
    open_child = new(board, parent_id=waits.id)
    board.set_status(HUMAN, waits.id, "done")
    results = _outputs(tmp_path, done.ref, "answer.md")
    clock.at = "2026-01-20T00:00:00Z"
    recent = new(board)
    board.set_status(HUMAN, recent.id, "done")
    clock.at = "2026-02-15T00:00:00Z"  # 45 days after the first ones, 26 after the recent one

    going, waiting = board.cleanable(30)
    assert [t.ref for t in going] == [done.ref, cancelled.ref, parent.ref, child.ref]
    assert [t.ref for t in waiting] == [waits.ref]  # until its open subtask can go too
    with pytest.raises(Forbidden, match="Only the person can clean the board"):
        board.clean("claude", 30)
    assert _in_files(board)  # (so the check below would see it)
    idle = sqlite3.connect(board.path)  # a running app's connection, so closing Handoff's doesn't tidy the files
    try:
        idle.execute("SELECT COUNT(*) FROM tasks").fetchall()
        removed, left = board.clean(HUMAN, 30)
        assert _in_files(board) == []
    finally:
        idle.close()
    assert [t.ref for t in removed] == [t.ref for t in going] and left == []
    assert sorted(t.id for t in board.tasks()) == sorted([active.id, waits.id, open_child.id, recent.id])
    assert board.events(done.id) == [] and not results.exists()
    conn = sqlite3.connect(board.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    finally:
        conn.close()
    assert [t.ref for t in board.clean(HUMAN, 0)[0]] == [recent.ref]  # 0: every finished task
    assert board.clean(HUMAN, 0) == ([], [])


def test_clean_removes_only_what_the_person_was_shown(tmp_path):
    clock = Clock("2026-01-01T00:00:00Z")
    board = Board.open(tmp_path, clock=clock)
    one, two, three = new(board), new(board), new(board)
    child = new(board, parent_id=three.id)
    for task in (one, two, three, child):
        board.set_status(HUMAN, task.id, "done")
    clock.at = "2026-03-01T00:00:00Z"
    board.set_status(HUMAN, two.id, "open")  # reopened after the person was shown it
    removed, _ = board.clean(HUMAN, 30, only=[one.id, two.id, three.id])  # (not the subtask, so not its task)
    assert [t.ref for t in removed] == [one.ref]


@pytest.mark.parametrize("days", [-1, board_module.MAX_KEEP_DAYS + 1, True, "30", None])
def test_clean_needs_a_number_of_days(board, days):
    with pytest.raises(BoardError, match="number of days"):
        board.clean(HUMAN, days)
    with pytest.raises(BoardError, match="number of days"):
        board.cleanable(days)


def test_keep_removes_finished_tasks_whenever_the_board_opens(tmp_path, sqlite_build):
    clock = Clock("2026-01-01T00:00:00Z")
    board = Board.open(tmp_path, clock=clock)
    old, active = new(board, title=f"Done {CANARY}", body=f"The body says {CANARY}"), new(board)
    board.set_status(HUMAN, old.id, "done")
    _outputs(tmp_path, old.ref, "answer.md")
    clock.at = "2026-03-01T00:00:00Z"
    assert board.keep_days is None  # by default, finished tasks stay
    assert Board.open(tmp_path, clock=clock).expired == []
    assert board.get(old.id).status == "done"
    with pytest.raises(Forbidden, match="Only the person can set how long finished tasks stay"):
        board.set_keep_days("claude", 1)
    board.set_keep_days(HUMAN, 30)
    assert board.keep_days == 30 and board.get(old.id)  # set, but nothing goes until the board opens
    # handoff keep opens it without removing anything, since it's about to change the setting
    assert Board.open(tmp_path, clock=clock, expire=False).expired == [] and board.get(old.id)
    assert _in_files(board)  # (so the check below would see it)
    idle = sqlite3.connect(board.path)  # a running app's connection, so closing Handoff's doesn't tidy the files
    try:
        idle.execute("SELECT COUNT(*) FROM tasks").fetchall()
        opened = Board.open(tmp_path, clock=clock)
        assert _in_files(board) == []  # removed on the way in, without waiting, and cleared all the same
    finally:
        idle.close()
    assert [t.ref for t in opened.expired] == [old.ref]
    with pytest.raises(NotFound):
        board.get(old.id)
    assert board.get(active.id) and not (tmp_path / ".handoff" / "outputs" / old.ref).exists()
    board.set_keep_days(HUMAN, None)
    assert board.keep_days is None and board.setting("keep_days") == "forever"
    for bad in (0, -3, board_module.MAX_KEEP_DAYS + 1, True, "30"):
        with pytest.raises(BoardError, match="number of days from 1"):
            board.set_keep_days(HUMAN, bad)


def test_the_default_keep_is_one_setting(tmp_path, monkeypatch, sqlite_build):
    monkeypatch.setattr(board_module, "DEFAULT_KEEP_DAYS", 30)  # what flipping the default does
    clock = Clock("2026-01-01T00:00:00Z")
    board = Board.open(tmp_path, clock=clock)
    old, kept = new(board, title=f"Done {CANARY}"), new(board)
    for task in (old, kept):
        board.set_status(HUMAN, task.id, "done")
    assert board.keep_days == 30
    clock.at = "2026-03-01T00:00:00Z"
    assert _in_files(board)
    idle = sqlite3.connect(board.path)
    try:
        idle.execute("SELECT COUNT(*) FROM tasks").fetchall()
        Board.open(tmp_path, clock=clock)
        assert board.tasks() == [] and _in_files(board) == []
    finally:
        idle.close()
    board.set_setting("keep_days", "soon")  # not a value Handoff writes: the default
    assert board.keep_days == 30
    board.set_keep_days(HUMAN, None)  # the person's own setting wins
    new(board)
    Board.open(tmp_path, clock=Clock("2030-01-01T00:00:00Z"))
    assert len(board.tasks()) == 1


def test_cleaning_on_open_never_stops_the_board_opening(tmp_path, monkeypatch):
    clock = Clock("2026-01-01T00:00:00Z")
    board = Board.open(tmp_path, clock=clock)
    board.set_status(HUMAN, new(board).id, "done")
    board.set_keep_days(HUMAN, 1)
    clock.at = "2026-03-01T00:00:00Z"

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(Board, "_remove_finished", broken)
    assert len(Board.open(tmp_path, clock=clock).tasks()) == 1
