"""Stray-process cleanup on macOS, where there's no /proc: the KERN_PROCARGS2 parser, the walk down the parent
pids, which processes count as the run's, and (on a Mac) the real thing.

Everything but the last test runs on every OS, on made-up processes.
"""
import os
import secrets
import signal
import subprocess
import sys

import pytest

from handoff import proc

FAR = 7_100_000  # pids no machine here has
MARK = "a1b2c3d4e5f60718"
MARKED = f"{proc.RUN_VAR}={MARK}".encode()


def kern_procargs2(argv, env, path=b"/bin/sleep"):
    """What sysctl KERN_PROCARGS2 gives. Apple's own strings at the end carry the mark too: they must never be
    read as the environment."""
    return (len(argv).to_bytes(4, sys.byteorder) + path + b"\0\0\0"
            + b"".join(a + b"\0" for a in argv) + b"".join(e + b"\0" for e in env)
            + b"\0" + b"executable_path=" + path + b"\0" + MARKED + b"\0")


def test_procargs2_gives_the_path_arguments_and_environment_but_not_apples_strings():
    data = ((3).to_bytes(4, sys.byteorder) + b"/bin/sleep\0" + b"\0\0\0"
            + b"sleep\0" + b"\0" + b"HANDOFF_RUN=abc\0"
            + b"PATH=/bin\0HANDOFF_RUN=abcd\0" + b"\0"
            + b"executable_path=/bin/sleep\0HANDOFF_RUN=abc\0")
    assert proc._parse_procargs2(data) == (b"/bin/sleep", [b"sleep", b"", b"HANDOFF_RUN=abc"],
                                           [b"PATH=/bin", b"HANDOFF_RUN=abcd"])


def test_procargs2_that_is_cut_short_gives_only_what_was_complete():
    head = (3).to_bytes(4, sys.byteorder) + b"/bin/sleep\0\0\0\0"
    assert proc._parse_procargs2(b"") == (b"", [], [])
    assert proc._parse_procargs2(b"\x01\x00") == (b"", [], [])
    assert proc._parse_procargs2((1).to_bytes(4, sys.byteorder) + b"/bin/sl") == (b"", [], [])
    assert proc._parse_procargs2((-1).to_bytes(4, sys.byteorder, signed=True) + b"/bin/sleep\0") == (
        b"/bin/sleep", [], [])
    assert proc._parse_procargs2(head + b"sleep\0\0HANDOFF_") == (b"/bin/sleep", [b"sleep", b""], [])
    assert proc._parse_procargs2(head + b"sleep\0") == (b"/bin/sleep", [b"sleep"], [])
    last_env_unfinished = (1).to_bytes(4, sys.byteorder) + b"/bin/sleep\0\0\0\0sleep\0PATH=/bin\0HANDOFF_RUN=abc"
    assert proc._parse_procargs2(last_env_unfinished) == (b"/bin/sleep", [b"sleep"], [b"PATH=/bin"])


def test_the_parent_pid_walk_finds_everything_started_under_the_root():
    root, a, b, c, d, e, x, y = (FAR + n for n in (0, 1, 2, 3, 20, 21, 30, 31))
    text = f"""\
  PID  PPID   UID
{a:>8} {root:>8}   501
{b:>8} {a:>8}   501

{c:>8} {root:>8}   501
{e:>8} {d:>8}   501
{x:>8} {y:>8}   501
{y:>8} {x:>8}   501
{x:>8} {root:>8}   501 extra
 not a process line
{d:>8} 1 0
"""
    table = proc._ps_table(text)
    assert table == [(a, root, 501), (b, a, 501), (c, root, 501), (e, d, 501), (x, y, 501), (y, x, 501), (d, 1, 0)]
    assert proc._descendants(table, root) == {a, b, c}
    assert proc._ps_table("") == []


def test_the_parent_pid_walk_ends_when_the_root_is_its_own_descendant():
    root, a, b = FAR, FAR + 1, FAR + 2
    table = [(root, b, 501), (a, root, 501), (b, a, 501)]
    assert proc._descendants(table, root) == {a, b}


def fake_macos(monkeypatch):
    """Pretend processes, seen through proc's ps and sysctl. Returns their pids by name."""
    me = 501
    names = ("root", "child", "deep", "away", "hidden", "plain", "in_argv", "longer", "other_read", "other_refused")
    pids = dict(zip(names, range(FAR, FAR + len(names))))
    ghost = FAR + 99
    rows = [(1, 0, 0),
            (pids["root"], 1, me),  # the run's launcher
            (pids["child"], pids["root"], me),  # macOS won't show its environment
            (pids["deep"], pids["child"], me),  # nor this one's
            (pids["away"], 1, me),  # left the tree, but carries the mark
            (pids["hidden"], 1, me),  # not under the root, and macOS won't show its environment
            (pids["plain"], pids["root"], me),  # under the root, but no mark
            (pids["in_argv"], 1, me),
            (pids["longer"], 1, me),
            (pids["other_read"], pids["root"], 0),
            (pids["other_refused"], pids["root"], 0),
            (os.getpid(), ghost, me),
            (os.getppid(), ghost, me)]
    marked = kern_procargs2([b"sleep", b"60"], [b"PATH=/bin", MARKED])
    buffers = {
        pids["root"]: marked,
        pids["away"]: marked,
        pids["plain"]: kern_procargs2([b"sleep"], [b"PATH=/bin"]),
        pids["in_argv"]: kern_procargs2([b"sleep", MARKED], [b"PATH=/bin"]),
        pids["longer"]: kern_procargs2([b"sleep"], [MARKED + b"x", b"OLD_" + MARKED]),
        pids["other_read"]: marked,
        os.getpid(): marked,
        os.getppid(): marked,
    }
    text = "\n".join(f"{pid} {ppid} {uid}" for pid, ppid, uid in rows)
    monkeypatch.setattr(proc, "_processes", lambda: proc._ps_table(text))
    monkeypatch.setattr(proc, "_procargs2", buffers.get)
    monkeypatch.setattr(proc.os, "getuid", lambda: me, raising=False)
    return pids


def test_a_process_counts_by_its_environment_or_when_that_is_hidden_by_its_parents(monkeypatch):
    pids = fake_macos(monkeypatch)
    found = proc._marked_darwin(MARK, pids["root"])
    assert set(found) == {pids[n] for n in ("root", "child", "deep", "away")}
    assert os.getpid() not in found and os.getppid() not in found


def test_without_a_root_nothing_counts_by_its_parents(monkeypatch):
    pids = fake_macos(monkeypatch)
    for root in (0, 1):  # (a program that was reaped has none; everything on the machine is under 1)
        assert set(proc._marked_darwin(MARK, root)) == {pids["root"], pids["away"]}


@pytest.mark.skipif(sys.platform != "darwin", reason="reads other processes' environments with sysctl, on macOS")
def test_kill_tree_stops_a_process_that_left_the_session_but_carries_the_mark():
    mark = secrets.token_hex(8)
    env = {**os.environ, proc.RUN_VAR: mark}
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    sleeper = subprocess.Popen(["/bin/sleep", "60"], env=env, start_new_session=True, **quiet)
    root = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env,
                            start_new_session=True, **quiet)
    try:
        assert sleeper.pid in proc._descendants(proc._processes(), os.getpid())
        assert sleeper.pid in proc._marked_darwin(mark, root.pid)
        proc.kill_tree(root, mark)
        assert sleeper.wait(timeout=5) == -signal.SIGKILL
        assert root.wait(timeout=5) is not None
    finally:
        for p in (sleeper, root):
            if p.poll() is None:
                p.kill()
                p.wait()
