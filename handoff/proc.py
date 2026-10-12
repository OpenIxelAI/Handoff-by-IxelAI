"""Running other programs: the Ixel panel, the worker's agent, and what `handoff update` runs.

Agent-written text only ever goes on stdin, never on the command line. A
timeout stops the whole process tree, not just the launcher: its process group
everywhere, and on Linux also what left the group (with setsid, say) but still
carries the run's mark.
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

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
# What to tell about each program run_tree starts on this thread (children_noted)
_noted = threading.local()


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


@contextmanager
def children_noted(note: Callable[[int], None]) -> Iterator[None]:
    """While this is open, `note` gets the process id of each program run_tree starts on this thread: a board
    run keeps them, so a run whose worker was killed outright isn't taken for stopped while what it started goes
    on (on Linux the sandbox stops with its worker; elsewhere it may not)."""
    before = getattr(_noted, "note", None)
    _noted.note = note
    try:
        yield
    finally:
        _noted.note = before


def taskkill() -> str:
    """Windows' taskkill, from where Windows keeps it: by a bare name, Windows would look in the current folder
    first, and that is often a project you cloned."""
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")


# In the environment of everything one run starts: a process that leaves the run's process group keeps it
RUN_VAR = "HANDOFF_RUN"


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


def kill_tree(proc: subprocess.Popen, mark: str | None = None) -> None:
    """Stop a program and everything it started (on Windows, a `.cmd` launcher wraps the real process).
    `mark`: the run's mark, to find on Linux what started its own session to get away."""
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


def pid_alive(pid: object) -> bool | None:
    """Is there a process with this id? True: there is. False: it's gone. None: this can't tell (not a process
    id, which a board someone else wrote may hold, or the system won't say)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 1 <= pid <= 2**31 - 1:
        return None  # (0 and -1 would mean whole groups of processes to os.kill)
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)  # signal 0 sends nothing; it only checks that the process is there
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # it's there, and not yours
        except OSError:
            return None
        return True
    # Windows: os.kill would terminate the process, so ask for its exit code, which is STILL_ACTIVE while it runs
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p  # a HANDLE is pointer-sized
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process has this id
            return False
        return True if error == 5 else None  # ERROR_ACCESS_DENIED: it's there, and not yours
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def group_alive(pid: object) -> bool | None:
    """Is the program run_tree started as `pid`, or anything still in its process group, there? As pid_alive."""
    if sys.platform == "win32" or pid_alive(pid) is None:
        return pid_alive(pid)
    try:
        os.killpg(pid, 0)  # run_tree starts each program in a session, and so a process group, of its own
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


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
            if (note := getattr(_noted, "note", None)) is not None:
                note(proc.pid)
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
