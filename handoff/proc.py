"""Running other programs: the Ixel panel, the worker's agent, and what `handoff update` runs.

Agent-written text only ever goes on stdin, never on the command line. A
timeout stops the whole process tree, not just the launcher: its process group
everywhere, and on Linux and macOS also what left the group (with setsid, say)
but still carries the run's mark.
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import threading

WINDOWS = os.name == "nt"
# What CreateProcess can start (anything else in PATHEXT, like .py or .js, it can't)
LAUNCHABLE = (".exe", ".com", ".bat", ".cmd")


def find_on_path(name: str) -> str | None:
    """A program's full path, searching only PATH. (On Windows, shutil.which and CreateProcess both look in
    the current folder first, and that is often a project you cloned: a git.exe there must not be what runs.)"""
    exts = [""]
    if WINDOWS:
        # PATHEXT's order, but only what CreateProcess can start: a git.py earlier on PATH mustn't shadow git.exe
        pathext = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        exts = [e for e in pathext if e in LAUNCHABLE] or list(LAUNCHABLE)
        if os.path.splitext(name)[1].lower() in LAUNCHABLE:
            exts = [""]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or not os.path.isabs(folder):  # "", "." and the like mean the current folder
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and (WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


# Programs run_tree has running now, with each one's mark, so Ctrl+C can stop the ones dispatch started side by side
_running: dict[subprocess.Popen, str] = {}
_running_lock = threading.Lock()
# Set by stop_all: from then on nothing new starts, and what was stopped says so
_stopping = threading.Event()


class Stopped(Exception):
    """You stopped the runs (Ctrl+C): this program was stopped, or never started."""


def stopping() -> bool:
    return _stopping.is_set()


def allow_runs() -> None:
    """Let programs start again, for a new set of runs after an earlier one was stopped."""
    _stopping.clear()


def stop_all() -> int:
    """Stop everything run_tree is running (and what each one started), and anything it's about to start.
    Returns how many were running."""
    _stopping.set()
    with _running_lock:
        procs = list(_running.items())
    for proc, mark in procs:
        kill_tree(proc, mark)
    return len(procs)


def taskkill() -> str:
    """Windows' taskkill, from where Windows keeps it: by a bare name, Windows would look in the current folder
    first, and that is often a project you cloned."""
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")


# In the environment of everything one run starts: a process that leaves the run's process group keeps it
RUN_VAR = "HANDOFF_RUN"
# macOS: where ps lives, and sysctl's numbers for a process's arguments and environment (kern.procargs2)
PS = "/bin/ps"
CTL_KERN = 1
KERN_PROCARGS2 = 49


def _marked(mark: str) -> list[int]:
    """Linux: your processes that carry this run's mark. (Other people's environments aren't readable.)"""
    want = f"{RUN_VAR}={mark}".encode()
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return []
    found = []
    for name in names:
        try:
            with open(f"/proc/{name}/environ", "rb") as f:
                if want in f.read().split(b"\0"):
                    found.append(int(name))
        except (OSError, ValueError):
            continue  # gone already, or not yours
    return found


def _parse_procargs2(data: bytes) -> tuple[bytes, list[bytes], list[bytes]]:
    """macOS: what sysctl KERN_PROCARGS2 gives for a process, as (program path, arguments, environment). A buffer
    that is cut short gives back only what was complete, and nothing here raises. Apple's own strings after the
    environment are not part of it."""
    if len(data) < 4:
        return b"", [], []
    argc = int.from_bytes(data[:4], sys.byteorder, signed=True)
    end = data.find(b"\0", 4)
    if end < 0:
        return b"", [], []
    path = data[4:end]
    if argc < 0:
        return path, [], []
    pos = end
    while pos < len(data) and data[pos] == 0:  # the padding after the path
        pos += 1
    argv: list[bytes] = []
    while len(argv) < argc:  # counted, because an argument can be empty
        end = data.find(b"\0", pos)
        if end < 0:
            return path, argv, []
        argv.append(data[pos:end])
        pos = end + 1
    env: list[bytes] = []
    while pos < len(data):
        end = data.find(b"\0", pos)
        if end <= pos:  # the empty string that ends the environment, or a last one with no closing NUL
            break
        env.append(data[pos:end])
        pos = end + 1
    return path, argv, env


def _procargs2(pid: int) -> bytes | None:
    """macOS: a process's raw KERN_PROCARGS2 buffer, or None when macOS won't give it (another user's process, a
    hardened one, or one that is gone)."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        libc.sysctl.restype = ctypes.c_int
        try:
            size = os.sysconf("SC_ARG_MAX")
        except (OSError, ValueError):
            size = 0
        size = size if size > 0 else 1 << 20
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        buf = ctypes.create_string_buffer(size)
        length = ctypes.c_size_t(size)
        if libc.sysctl(mib, 3, buf, ctypes.byref(length), None, 0) != 0:
            return None
        return ctypes.string_at(buf, length.value)
    except (OSError, AttributeError, TypeError):
        return None


def _ps_table(text: str) -> list[tuple[int, int, int]]:
    """The output of `ps -A -o pid=,ppid=,uid=` as (pid, parent pid, uid) rows. A line that isn't three whole
    numbers is skipped."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return rows


def _processes() -> list[tuple[int, int, int]]:
    """macOS: every process as (pid, parent pid, uid), from ps. Empty when ps can't be run."""
    try:
        done = subprocess.run([PS, "-A", "-o", "pid=,ppid=,uid="], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return _ps_table(done.stdout) if done.returncode == 0 else []


def _descendants(table: list[tuple[int, int, int]], root: int) -> set[int]:
    """Every pid whose chain of parents reaches `root`, not counting `root` itself."""
    children: dict[int, list[int]] = {}
    for pid, ppid, _ in table:
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    todo = [root]
    while todo:
        for pid in children.get(todo.pop(), []):
            if pid != root and pid not in found:  # (so a loop of parents can't go on for ever)
                found.add(pid)
                todo.append(pid)
    return found


def _marked_darwin(mark: str, root: int) -> list[int]:
    """macOS (no /proc): your processes that carry this run's mark, read with sysctl. One whose environment macOS
    won't show counts when it was started under `root`. Never this process or its parent."""
    want = f"{RUN_VAR}={mark}".encode()
    table = _processes()
    tree = _descendants(table, root) if root > 1 else set()
    me = os.getuid()
    skip = {os.getpid(), os.getppid()}
    found = []
    for pid, _, uid in table:
        if uid != me or pid in skip or pid <= 1:
            continue
        data = _procargs2(pid)
        if data is None:
            if pid in tree:
                found.append(pid)
        elif want in _parse_procargs2(data)[2]:
            found.append(pid)
    return found


def kill_tree(proc: subprocess.Popen, mark: str | None = None) -> None:
    """Stop a program and everything it started (on Windows, a `.cmd` launcher wraps the real process).
    `mark`: the run's mark, to find on Linux and macOS what started its own session to get away."""
    early = []
    if mark and sys.platform == "darwin":
        # Who is under a program can only be read while it lives (after, launchd has them), and a program that's
        # already been reaped has no pid of its own to look under
        early = _marked_darwin(mark, proc.pid if proc.returncode is None else 0)
    try:
        if sys.platform == "win32":
            subprocess.run([taskkill(), "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()
    if mark and sys.platform.startswith("linux"):
        for _ in range(5):  # again, for any that started another just before it was stopped
            left = [pid for pid in _marked(mark) if pid != os.getpid()]
            if not left:
                break
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    elif mark and sys.platform == "darwin":
        for pid in early:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for _ in range(5):  # again, for any that started another just before it was stopped
            left = _marked_darwin(mark, proc.pid if proc.returncode is None else 0)
            if not left:
                break
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def run_tree(command: list[str], stdin: str, env: dict, timeout: float,
             cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    """Run a program with text on stdin. On timeout, stop its whole process tree and raise TimeoutExpired.
    Once stop_all has run, raise Stopped instead of starting, or when it's the reason the program ended."""
    if _stopping.is_set():
        raise Stopped("it was stopped before it started")
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
             else {"start_new_session": True})
    mark = secrets.token_hex(8)
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", env={**env, RUN_VAR: mark}, cwd=cwd,
                          **group) as proc:
        with _running_lock:
            _running[proc] = mark
        try:
            if _stopping.is_set():  # stop_all ran while this was starting, after it looked at what was running
                kill_tree(proc, mark)
            out, err = proc.communicate(stdin, timeout=timeout)
        except BaseException:  # a timeout, or Ctrl+C: its own process group never sees the terminal's
            kill_tree(proc, mark)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            with _running_lock:
                _running.pop(proc, None)
    if proc.returncode != 0 and _stopping.is_set():  # (one that finished just in time keeps its result)
        raise Stopped("it was stopped while it was running")
    return subprocess.CompletedProcess(command, proc.returncode, out, err)
