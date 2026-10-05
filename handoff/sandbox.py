"""The worker's OS sandbox on Linux: bubblewrap around the agent.

The agent's own sandbox (Codex's `workspace-write`) blocks the network and
writes outside the worktree for the commands the agent runs, but not reads:
left to itself, a command can read ~/.ssh or any other file you can. So the
worker runs the agent inside bubblewrap, which shows it a filesystem with
nothing of yours in it:

- the system (/usr, /etc, /opt and the like), read-only;
- the agent's own install (and Node's), read-only;
- folders you share with `handoff worker --share` (tools installed in your
  home, say), read-only, with credential files in them shown empty;
- the task's worktree, read-write;
- a private home for the agent (its login, and nothing else), read-write;
- an empty /tmp and an empty home; every other home, /root, /mnt, /media,
  /var and the rest of the project are simply not there.

The network stays shared, because the agent must reach its model API; the
agent's own sandbox takes the network away from the commands it runs.
Nothing here is needed on the command line of the agent itself: the paths
are Handoff's own, and the agent's words still go in on stdin.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

from handoff.proc import find_on_path

SYSTEM_READ_ONLY = ("/usr", "/etc", "/opt", "/nix", "/snap", "/srv")
LIB_DIRS = ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")
HIDDEN = ("/tmp", "/home", "/root", "/mnt", "/media", "/run", "/var")
CERT_ENV = ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
APPARMOR_USERNS = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
APPARMOR_FILE = "/etc/apparmor.d/bwrap"


# Folders and files under the home folder that hold credentials: never shared into the sandbox, and hidden
# (shown empty) inside a folder that is
SENSITIVE = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config", ".local/share/keyrings",
             ".password-store", ".pki", ".mozilla", ".codex", ".claude", ".claude.json", ".netrc",
             ".git-credentials", ".npmrc", ".pypirc", "Library/Keychains")
# Files that often hold a token, hidden (shown empty) wherever they are in a shared folder
CREDENTIAL_NAMES = {"credentials", "credentials.toml", "credentials.json", ".credentials.json", ".netrc",
                    ".git-credentials", ".npmrc", ".pypirc", ".yarnrc.yml", "auth.json", "hosts.yml",
                    "settings.xml", "gradle.properties", ".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")


class SandboxUnavailable(Exception):
    """The OS sandbox can't run here; the message says what to install or change."""


class ShareRefused(Exception):
    """A folder the person asked to share can't be shared; the message says why."""


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def shared_folders(paths: list[str | Path], *, project: Path, home: Path | None = None
                   ) -> tuple[list[Path], list[Path]]:
    """Folders the person shares with the agent (read-only), checked, and what to hide inside them.

    Returns (folders, hidden). A folder is refused if it is, or holds, your whole home folder or the
    project (the worktree is shared already), or if it is, or is inside, a folder that holds
    credentials. Credential folders inside a shared folder, and files that often hold a token, are
    hidden: the agent sees them empty. So is what a link in a shared folder points at when the link, or
    what it points at, looks like a credential, and it is in a shared folder or the project (where the
    worktree is). Anything else a link can point at isn't in the sandbox (the rest of your home, /root,
    /tmp), or is the system's, which stays as it is (a cacert.pem link to the CA bundle).
    """
    home = Path(os.path.realpath(home or Path.home()))
    project = Path(os.path.realpath(project))
    sensitive = [Path(os.path.realpath(home / name)) for name in SENSITIVE]
    folders: list[Path] = []
    hidden: list[Path] = []
    for raw in paths:
        shown = str(raw)
        path = Path(os.path.realpath(Path(raw).expanduser()))
        if not os.path.isdir(path):  # never raises (Path.is_dir does, inside a folder you can't open)
            raise ShareRefused(f"Can't share {shown}: it isn't a folder you can open.")
        if _inside(home, path):
            raise ShareRefused(f"Can't share {shown}: it holds your whole home folder. Share the folders your "
                               "tools are in instead, such as ~/.cargo or ~/.nvm.")
        if _inside(project, path) or _inside(path, project):
            raise ShareRefused(f"Can't share {shown}: the project stays hidden from the agent, which works in its "
                               "own worktree.")
        for folder in sensitive:
            if _inside(path, folder):
                raise ShareRefused(f"Can't share {shown}: {folder} holds credentials.")
        folders.append(path)
    for path in folders:
        for folder in sensitive:
            if path in folder.parents and os.path.lexists(folder):
                hidden.append(folder)
        hidden += _credential_files(path, sensitive, visible=[*folders, project])
    return folders, sorted(set(hidden))


def _credential_name(name: str) -> bool:
    return name in CREDENTIAL_NAMES or name.endswith(CREDENTIAL_SUFFIXES)


def _is_link(path: Path) -> bool:
    """Path.is_symlink as it was up to Python 3.13: one that can't be checked raises (from 3.14, it's False)."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False  # gone since its folder was listed


def _credential_files(folder: Path, sensitive: list[Path], visible: list[Path]) -> list[Path]:
    found = []
    def scan_failed(error: OSError) -> None:
        raise ShareRefused(f"Can't share {folder}: couldn't check it for credential files ({error}).") from error

    for dirpath, dirs, files in os.walk(folder, onerror=scan_failed):
        here = Path(dirpath)
        try:  # a folder you can list but not open (r--): nothing in it can be checked
            links = [here / name for name in (*dirs, *files) if _is_link(here / name)]
            dirs[:] = [d for d in dirs if not _is_link(here / d)]
            found += [here / name for name in files if _credential_name(name) and not _is_link(here / name)]
        except OSError as error:
            scan_failed(error)
        # A link can't be hidden itself (a mount follows it), so hide what it points at: the agent reads
        # the same file either way, but only in a shared folder or the project (see shared_folders).
        # Links to a folder aren't walked; one that goes into a credential folder hides that folder.
        for link in links:
            target = Path(os.path.realpath(link))
            if not any(_inside(target, place) for place in visible):
                continue  # not in the sandbox, or the system's
            # os.path.exists never raises: a target you can't read is one the agent, which runs as you, can't
            # read either (Path.exists raises PermissionError there, up to Python 3.13)
            if not os.path.exists(target):
                continue  # nothing to read
            if (_credential_name(link.name) or _credential_name(target.name)
                    or any(_inside(target, s) for s in sensitive)):
                found.append(target)
    return found


def find_bwrap() -> str | None:
    return find_on_path("bwrap")


def _base(bwrap: str) -> list[str]:
    argv = [bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup-try"]
    for path in SYSTEM_READ_ONLY:
        if Path(path).is_dir():
            argv += ["--ro-bind", path, path]
    for path in LIB_DIRS:
        p = Path(path)
        if p.is_symlink():  # merged /usr: /bin -> usr/bin
            argv += ["--symlink", os.readlink(p), path]
        elif p.is_dir():
            argv += ["--ro-bind", path, path]
    argv += ["--proc", "/proc", "--dev", "/dev"]
    for path in HIDDEN:
        argv += ["--tmpfs", path]
    return argv


def _install_hint(package: str) -> str:
    return (f"Debian/Ubuntu: sudo apt install {package}; Fedora: sudo dnf install {package}; "
            f"Arch: sudo pacman -S {package}")


def check(needs: tuple[str, ...] = ()) -> str:
    """The bwrap command, once it has shown it can start a sandbox here. `needs`: other programs the agent's
    own sandbox uses inside this one (Claude Code's: socat)."""
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable("The worker's sandbox is Linux-only for now (it uses bubblewrap); macOS is next.")
    bwrap = find_bwrap()
    if bwrap is None:
        raise SandboxUnavailable("The worker runs the agent inside bubblewrap, which isn't installed. Install it "
                                 f"({_install_hint('bubblewrap')}), then try again.")
    missing = [name for name in needs if find_on_path(name) is None]
    if missing:
        raise SandboxUnavailable(f"The agent's own sandbox needs {', '.join(missing)}, which isn't installed. "
                                 f"Install it ({_install_hint(missing[0])}), then try again.")
    probe = _base(bwrap) + ["--", "/usr/bin/env", "true"]
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxUnavailable(f"bubblewrap didn't start: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        problem = "bubblewrap couldn't start a sandbox here" + (f" ({detail[-1][:200]})" if detail else "") + "."
        if _apparmor_restricts_userns():
            raise SandboxUnavailable(
                f"{problem} AppArmor here (Ubuntu 24.04 and later) stops programs from making user namespaces "
                f"unless a profile allows it, and nothing ships one for bubblewrap. To add it, run: "
                f"{apparmor_fix(bwrap)}")
        raise SandboxUnavailable(f"{problem} It needs unprivileged user namespaces: check that they're on "
                                 "(`sysctl user.max_user_namespaces` above 0) and that you aren't inside a container "
                                 "that forbids them.")
    return bwrap


def _apparmor_restricts_userns() -> bool:
    try:
        return APPARMOR_USERNS.read_text(encoding="ascii").strip() == "1"
    except OSError:
        return False


def apparmor_profile(bwrap: str) -> str:
    """The AppArmor profile that lets bubblewrap make user namespaces, in the form Ubuntu ships for other
    programs that need them (Flatpak, Chrome); on one line, which AppArmor reads the same."""
    return f"abi <abi/4.0>, profile bwrap {os.path.realpath(bwrap)} flags=(unconfined) {{ userns, }}"


def apparmor_fix(bwrap: str) -> str:
    """One command, to paste, that installs and loads `apparmor_profile`."""
    return (f"echo {shlex.quote(apparmor_profile(bwrap))} | sudo tee {APPARMOR_FILE} "
            f"&& sudo apparmor_parser -r {APPARMOR_FILE}")


def _extra_read_only(env: dict[str, str]) -> list[Path]:
    """Files the network needs that live outside the system folders: DNS config, proxy CA bundles."""
    paths: list[Path] = []
    resolv = Path("/etc/resolv.conf")
    if resolv.is_symlink():  # Ubuntu: -> /run/systemd/resolve/stub-resolv.conf
        target = Path(os.path.realpath(resolv))
        if target.exists():
            paths.append(target.parent)
    for name in CERT_ENV:
        value = env.get(name)
        if value and os.path.exists(value):  # never raises: one you can't read, the agent can't either
            paths.append(Path(os.path.realpath(value)))
    return paths


def install_dirs(command: str) -> list[Path]:
    """What running `command` needs to see: its package folder, and Node's install if it's a Node script."""
    real = Path(os.path.realpath(command))
    modules = [p for p in real.parents if p.name == "node_modules"]
    if not modules:
        return [real.parent]
    dirs = [modules[-1]]
    node = find_on_path("node")
    if node:
        dirs.append(Path(os.path.realpath(node)).parent.parent)  # the prefix: bin/node, lib/node_modules…
    return dirs


def _covered(path: Path) -> bool:
    return any(path == Path(root) or Path(root) in path.parents for root in SYSTEM_READ_ONLY + LIB_DIRS)


def wrap(bwrap: str, argv: list[str], *, worktree: Path, home: Path, private: dict[Path, Path],
         read_only: list[Path], env: dict[str, str], hidden: list[Path] = ()) -> list[str]:
    """`argv` run inside the sandbox, in the worktree. `private` maps host folders to where the agent sees them;
    `hidden` are folders and files inside the read-only ones that it sees empty, except what the network needs."""
    out = _base(bwrap)
    seen: set[str] = set()

    def show(paths: list[Path]) -> None:
        for path in paths:
            key = str(path)
            if key not in seen and not _covered(path) and path.exists():
                seen.add(key)
                out.extend(["--ro-bind", key, key])

    network = _extra_read_only(env)
    show(read_only)
    for path in hidden:
        if any(_inside(path, needed) for needed in network):
            continue  # a CA bundle in a shared folder, say: hiding it would break the agent's HTTPS
        out += ["--tmpfs", str(path)] if path.is_dir() and not path.is_symlink() else \
               ["--ro-bind", "/dev/null", str(path)]
    show(network)  # after the masks, so a hidden folder never covers what the network needs in it
    for host, inside in private.items():
        out += ["--bind", str(host), str(inside)]
    out += ["--bind", str(worktree), str(worktree), "--chdir", str(worktree),
            "--setenv", "HOME", str(home), "--setenv", "TMPDIR", "/tmp", "--", *argv]
    return out
