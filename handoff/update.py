"""`handoff update`: get the latest Handoff into the checkout it was installed from, and reinstall it.

install.sh and install.ps1 record where they installed from in install.json, next to the virtual
environment, and, as their last step, the commit they installed. Update pulls that checkout (fast-forward
only, and never over local changes) and runs its installer again when what it has isn't what's installed,
which keeps your boards and settings: they live in each project and in each app.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from handoff import proc

INSTALL_INFO = Path(sys.prefix).parent / "install.json"
Say = Callable[[str], None]


class UpdateError(Exception):
    """Why this copy can't be updated; the message says what to do instead."""


def install_info(path: Path | None = None) -> dict | None:
    try:
        info = json.loads((path or INSTALL_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("source"), str) else None


def _installed_commit(info: dict) -> str:
    """The commit the installer recorded as its last step; "" if it recorded none (it was from before they did,
    or couldn't tell)."""
    commit = info.get("commit")
    return commit if isinstance(commit, str) else ""


def _program(name: str) -> str:
    """A program's full path from PATH, never the current folder: handoff update is usually run from inside a
    project, and Windows would look there first."""
    found = proc.find_on_path(name)
    if found is None:
        raise UpdateError(f"Can't update: {name} isn't installed (or isn't on your PATH).")
    return found


def _powershell() -> str:
    """Windows PowerShell, from where Windows keeps it (or PATH)."""
    builtin = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
               / "powershell.exe")
    if builtin.is_file():
        return str(builtin)
    found = proc.find_on_path("powershell")
    if found is None:
        raise UpdateError(f"Can't update: Windows PowerShell isn't installed (it isn't at {builtin}, or on your "
                          "PATH).")
    return found


def _git(source: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([_program("git"), "-C", source, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _checkout(info: dict | None) -> str:
    """The checkout this copy was installed from, ready to pull; UpdateError otherwise."""
    if info is None:
        raise UpdateError("This copy of Handoff wasn't installed with install.sh or install.ps1 (or was installed "
                          "before they recorded where from). Run the installer from a fresh clone "
                          "once; after that, handoff update works. For a checkout installed with pip install -e: "
                          "git pull.")
    source = info["source"]
    if not (Path(source) / ".git").exists():
        raise UpdateError(f"Can't update: {source} isn't a git checkout any more. Run the installer from a fresh "
                          "clone instead.")
    if _git(source, "rev-parse", "--abbrev-ref", "@{upstream}").returncode != 0:
        raise UpdateError(f"Can't update: {source} isn't following a branch on GitHub. To follow the main "
                          f'version: git -C "{source}" switch main, then handoff update.')
    return source


def behind(source: str) -> int | None:
    """New commits on GitHub that the checkout doesn't have yet; None if GitHub can't be reached."""
    if _git(source, "fetch", "--quiet", timeout=120).returncode != 0:
        return None
    counted = _git(source, "rev-list", "--count", "HEAD..@{upstream}", timeout=30)
    return int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else None


def update(check_only: bool = False, force: bool = False, say: Say = print, info: dict | None = None,
           reinstall: Callable[[dict, Say], int] | None = None) -> int:
    info = info if info is not None else install_info()
    source = _checkout(info)
    if check_only:
        waiting = behind(source)
        if waiting is None:
            say("Couldn't reach GitHub to check. Try again later.")
            return 1
        if waiting:
            say(f"{waiting} new change{'s' if waiting != 1 else ''} available. Run: handoff update")
        elif _installed_commit(info) not in ("", _git(source, "rev-parse", "HEAD").stdout.strip()):
            say("An update is downloaded but not installed yet. Run: handoff update")  # say, its reinstall failed
        else:
            say("Handoff is up to date.")
        return 0

    dirty = _git(source, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0 or dirty.stdout.strip():
        # (not "commit them": a commit of your own there stops the next pull, which only fast-forwards)
        raise UpdateError(f"Can't update: {source} has local changes. Put them aside with: git -C \"{source}\" "
                          "stash, then run handoff update again.")
    say(f"Getting the latest Handoff into {source} …")
    before = _git(source, "rev-parse", "HEAD").stdout.strip()
    # Not captured: a private repository may ask you to sign in
    pulled = subprocess.run([_program("git"), "-C", source, "pull", "--ff-only"], timeout=600)
    if pulled.returncode != 0:
        raise UpdateError("git pull didn't complete, so nothing was changed.")
    # Compare with what's installed, so a reinstall that failed after an earlier pull runs again; without a
    # recorded commit, with what was here before the pull
    installed = _installed_commit(info) or before
    if installed and _git(source, "rev-parse", "HEAD").stdout.strip() == installed and not force:
        say("Handoff is already up to date.")
        return 0
    return (reinstall or _reinstall)(info, say)


def _reinstall(info: dict, say: Say) -> int:
    env = {**os.environ, "HANDOFF_SKIP_PATH_UPDATE": "1"}
    for key, name in (("install_root", "HANDOFF_INSTALL_ROOT"), ("bin_dir", "HANDOFF_BIN_DIR")):
        if info.get(key):
            env[name] = info[key]
    # The Python you chose with HANDOFF_PYTHON when you installed, unless you choose another one now
    chosen = info.get("python")
    if isinstance(chosen, str) and chosen and not env.get("HANDOFF_PYTHON"):
        if os.path.isfile(chosen):
            env["HANDOFF_PYTHON"] = chosen
        else:
            say(f"The Python you installed Handoff with ({chosen}) isn't there any more, so the installer picks "
                "one. To choose, set HANDOFF_PYTHON and run handoff update --force.")
    source = Path(info["source"])
    if proc.WINDOWS:
        # The installer renames a running handoff.exe out of the way and keeps the environment an app may
        # be using, so it can run from here
        command = [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(source / "install.ps1")]
    else:
        command = [_program("bash"), str(source / "install.sh")]
    result = subprocess.run(command, env=env)
    if result.returncode == 0:
        say("Updated. Restart Claude Desktop, Claude Code and Codex to use the new version.")
    elif proc.WINDOWS:
        # --force, which always runs it: a plain update runs it only when the checkout isn't at the commit
        # installed, so not after a --force of the latest, or for an install that didn't record its commit
        say("The update didn't finish. If it says a file is in use, close the apps using Handoff and run "
            "handoff update --force.")
    return result.returncode


DESCRIPTION = "Get the latest Handoff into the folder it was installed from, then reinstall it."


def main(argv: list[str], say: Say = print, parser: argparse.ArgumentParser | None = None,
         allowed: Callable[[argparse.Namespace], None] | None = None) -> int:
    """`handoff update`. The command line passes its own parser, for its help and its plain-words errors, and
    `allowed`, which raises if what was asked (as parsed) can't be done from here."""
    parser = parser or argparse.ArgumentParser(prog="handoff update", description=DESCRIPTION)
    parser.add_argument("--check", action="store_true", help="only say whether an update is available")
    parser.add_argument("--force", action="store_true", help="reinstall even if the latest is installed already")
    args = parser.parse_args(argv)
    if allowed is not None:
        allowed(args)
    try:
        return update(args.check, args.force, say)
    except FileNotFoundError:
        raise UpdateError("Can't update: git isn't installed (or isn't on your PATH).") from None
    except subprocess.TimeoutExpired:
        raise UpdateError("Can't update: git took too long. Check your connection and try again.") from None
