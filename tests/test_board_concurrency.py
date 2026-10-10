"""Two processes writing to one board at once: no lost updates, no double claims."""
import subprocess
import sys
import textwrap
from collections import Counter

from handoff.board import Board

TASKS = 40
NOTES = 40

WORKER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from handoff.board import Board, BoardError

    root, me, go_file = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    board = Board.open(root)
    while not go_file.exists():  # start both writers together
        time.sleep(0.005)
    won = 0
    for task in board.tasks(statuses=["open"]):
        if not task.title.startswith("Race"):  # not the other writer's new tasks
            continue
        try:
            board.claim(me, task.id)
            won += 1
        except BoardError:
            pass
    shared = int(sys.argv[4])
    for i in range({notes}):
        board.note("human", shared, f"{{me}} note {{i}}")
        board.create(me, f"{{me}} task {{i}}")
    print(won)
""").format(notes=NOTES)


def test_two_writers_lose_nothing(tmp_path):
    board = Board.open(tmp_path)
    for i in range(TASKS):
        board.create("human", f"Race {i}")
    shared, _ = board.create("human", "Shared notes")
    board.cancel("human", shared.id)  # not claimable; only the person writes notes to it

    go = tmp_path / "go"
    workers = [subprocess.Popen([sys.executable, "-c", WORKER, str(tmp_path), name, str(go), str(shared.id)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
               for name in ("claude", "codex")]
    go.write_text("go", encoding="utf-8")
    results = [w.communicate(timeout=120) for w in workers]
    for worker, (_, err) in zip(workers, results, strict=True):
        assert worker.returncode == 0, err

    # every race task was claimed exactly once, and the wins add up
    wins = [int(out.strip()) for out, _ in results]
    assert sum(wins) == TASKS
    race = [t for t in board.tasks() if t.title.startswith("Race")]
    assert all(t.status == "claimed" and t.assignee in ("claude", "codex") for t in race)
    for task in race:
        assert Counter(e.kind for e in board.events(task.id))["claimed"] == 1

    # every note and every new task from both writers is there, with unique ids
    notes = [e.text for e in board.events(shared.id) if e.kind == "note"]
    assert sorted(notes) == sorted(f"{who} note {i}" for who in ("claude", "codex") for i in range(NOTES))
    created = [t for t in board.tasks() if " task " in t.title]
    assert len(created) == 2 * NOTES and len({t.id for t in created}) == 2 * NOTES
