"""The worker's OS sandbox: bubblewrap on Linux, sandbox-exec on macOS, around the agent.

The agent's own sandbox (Codex's `workspace-write`) blocks the network and
writes outside the worktree for the commands the agent runs, but not reads:
left to itself, a command can read ~/.ssh or any other file you can. So the
worker runs the agent inside an OS sandbox of its own, which shows it nothing
of yours.

On Linux, bubblewrap shows it a filesystem with nothing of yours in it:

- the system (/usr, /etc, /opt and the like), read-only;
- the agent's own install (and Node's), read-only;
- folders you share with `handoff worker --share` (tools installed in your
  home, say), read-only, with credential files in them shown empty;
- the task's worktree, read-write;
- a private home for the agent (its login, and nothing else), read-write;
- an empty /tmp and an empty home; every other home, /root, /mnt, /media,
  /var and the rest of the project are simply not there.

On macOS there is one filesystem and no way to mount over a part of it, so
sandbox-exec runs the agent under a profile that denies what bubblewrap
leaves out, and allows back what the agent needs:

- reads of /Users, /Volumes, your home, the project, /private/tmp,
  /private/var/folders and /private/var/root are denied, except the
  worktree, the private home and its temp folder, the agent's install, the
  folders you share and what the network needs. The rest of the system
  (/usr, /Applications, /Library, /opt) stays readable;
- credential folders in your home, and the files hidden in shared folders,
  are denied again after that, so an install or a share can't open them;
- writes are denied except in the worktree, the private home, its temp
  folder, /dev/null and the terminal;
- the agent can't look at or signal processes outside the sandbox, nor
  reach the security server that opens your keychain.

The private home can't be moved on macOS, so the agent finds it where it is
(`login_place`); its temp folder is beside it (`private_temp`).

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

from handoff.proc import Stopped, find_on_path, run_tree

SYSTEM_READ_ONLY = ("/usr", "/etc", "/opt", "/nix", "/snap", "/srv")
LIB_DIRS = ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")
HIDDEN = ("/tmp", "/home", "/root", "/mnt", "/media", "/run", "/var")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MAC_HIDDEN = ("/Users", "/Volumes", "/private/tmp", "/private/var/folders", "/private/var/root")
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


def check(needs: tuple[str, ...] = (), command: str | None = None) -> str:
    """The sandbox program (bwrap, or sandbox-exec on macOS), once it has shown it can start a sandbox here.
    `needs`: other programs the agent's own sandbox uses inside this one (Claude Code's: socat). Only its Linux
    sandbox needs them, so macOS ignores them. `command`: the agent's program, which on macOS must be a program
    of its own (agent_binary), since the profile gives that program, and only it, the network."""
    if sys.platform == "darwin":
        program = _check_mac()
        if command is not None:
            agent_binary(command)
        return program
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable("The worker's sandbox runs on Linux (bubblewrap) and macOS (sandbox-exec), "
                                 "not here yet.")
    bwrap = find_bwrap()
    if bwrap is None:
        raise SandboxUnavailable("The worker runs the agent inside bubblewrap, which isn't installed. Install it "
                                 f"({_install_hint('bubblewrap')}), then try again.")
    missing = [name for name in needs if find_on_path(name) is None]
    if missing:
        raise SandboxUnavailable(f"The agent's own sandbox needs {', '.join(missing)}, which isn't installed. "
                                 f"Install it ({_install_hint(missing[0])}), then try again.")
    probe = _base(bwrap) + ["--", "/usr/bin/env", "true"]
    try:  # (through run_tree, so Ctrl+C's stop_all stops it on whatever thread it runs)
        proc = run_tree(probe, "", dict(os.environ), 30.0)
    except Stopped as exc:
        raise SandboxUnavailable("The sandbox check was stopped before it finished.") from exc
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


def _check_mac() -> str:
    program = SANDBOX_EXEC
    if not os.access(program, os.X_OK):
        raise SandboxUnavailable(f"The worker runs the agent inside macOS's sandbox-exec, which isn't at {program} "
                                 "here.")
    probe = [program, "-p", "(version 1)(allow default)", "/usr/bin/true"]
    try:  # (through run_tree, so Ctrl+C's stop_all stops it on whatever thread it runs)
        proc = run_tree(probe, "", dict(os.environ), 30.0)
    except Stopped as exc:
        raise SandboxUnavailable("The sandbox check was stopped before it finished.") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxUnavailable(f"sandbox-exec didn't start: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise SandboxUnavailable("sandbox-exec couldn't start a sandbox here"
                                 + (f" ({detail[-1][:200]})" if detail else "") + ".")
    return program


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


def _is_mac(program: str) -> bool:
    return Path(program).name == "sandbox-exec"


def wrap(program: str, argv: list[str], *, worktree: Path, home: Path, private: dict[Path, Path],
         read_only: list[Path], env: dict[str, str], hidden: list[Path] = ()) -> list[str]:
    """`argv` run inside the sandbox, in the worktree. `private` maps host folders to where the agent sees them
    (on macOS, where they are: see login_place); `hidden` are folders and files inside the read-only ones that it
    sees empty, except what the network needs. `program` is what check() returned: bwrap or sandbox-exec."""
    if _is_mac(program):
        return _wrap_mac(program, argv, worktree=worktree, home=home, private=private, read_only=read_only,
                         env=env, hidden=hidden)
    out = _base(program)
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


def login_place(program: str, host: Path, inside: Path) -> Path:
    """Where the agent finds a private folder. Under bubblewrap that is `inside`, where it is mounted; sandbox-exec
    can't move a folder, so there it is the folder itself."""
    return _real(host) if _is_mac(program) else inside


def private_temp(host: Path) -> Path:
    """The run's temp folder on macOS, which has no empty /tmp to show. It sits beside the private home, not in
    it, because both agents' own sandboxes hide the private home from the commands they run."""
    return Path(f"{host}-tmp")


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(path))


def _sbpl_string(text: str) -> str:
    """`text` as a quoted string in a sandbox-exec profile."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _subpaths(paths: list[str | Path]) -> list[str]:
    return [f"(subpath {_sbpl_string(str(path))})" for path in paths]


def _above(paths: list[Path]) -> list[str]:
    """The folders above `paths` (never "/"), once each, as exact paths: a program walking down to one of them
    must see that the folders on the way are there, though not what is in them."""
    found = {str(parent) for path in paths for parent in path.parents if parent != Path(parent.anchor)}
    return [f"(literal {_sbpl_string(parent)})" for parent in sorted(found)]


def _rule(verb: str, operation: str, filters: list[str]) -> list[str]:
    """A rule of the profile, or none when there is nothing for it to match: a read rule with no filters would
    deny, or allow, every file."""
    return [f"({verb} {operation} {' '.join(filters)})"] if filters else []


def _project_of(worktree: Path) -> Path | None:
    """The project a task's worktree is in (project/.handoff/worktrees/T-1), or None if it isn't in one."""
    from handoff.gitwork import WORKTREES
    names = WORKTREES.parts
    if len(worktree.parts) > len(names) + 2 and worktree.parts[-len(names) - 1:-1] == names:
        return worktree.parents[len(names)]
    return None


# Programs that run whatever code they're given: a profile that gave one of them the network would give it to every
# command run with it too
INTERPRETERS = frozenset({"node", "nodejs", "bun", "deno", "python", "pypy", "ruby", "perl", "php", "lua", "java",
                          "osascript", "sh", "bash", "zsh", "dash", "ksh", "fish", "tcsh", "csh", "env"})
_MACHO = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf",
          b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}
# What a command could use to leave the sandbox through another program that runs outside it: Apple events
# (osascript telling Terminal to run something), Launch Services (open), the clipboard, and preferences (defaults
# write, which other programs then read)
MAC_SERVICES = ("com.apple.coreservices.appleevents", "com.apple.coreservices.launchservicesd",
                "com.apple.coreservices.quarantine-resolver", "com.apple.lsd.mapdb", "com.apple.lsd.modifydb",
                "com.apple.pasteboard.1", "com.apple.cfprefsd.agent", "com.apple.cfprefsd.daemon",
                "com.apple.SecurityServer", "com.apple.securityd.xpc", "com.apple.security.agent")


def agent_binary(command: str) -> Path:
    """The real path of the agent's own program, which macOS's profile names as the only one with the network, its
    private home and its login. Raises SandboxUnavailable for an interpreter (node, python, a shell…) or a script,
    since then every command run with the same program would get them too."""
    real = _real(command)
    name = real.name.lower()
    stem = name.rstrip("0123456789.")
    try:
        with open(real, "rb") as f:
            magic = f.read(4)
    except OSError as exc:
        raise SandboxUnavailable(f"Can't read the agent's program {real}: {exc}") from exc
    if stem in INTERPRETERS or name.startswith("python") or magic not in _MACHO:
        raise SandboxUnavailable(
            f"On macOS the sandbox gives the network to the agent's own program only, and {command} is "
            f"{'an interpreter' if magic in _MACHO else 'a script'} ({real}), which every command could use too. "
            "Install the agent as a program of its own (Claude Code's or Codex's native install) to run it in the "
            "sandbox.")
    return real


# Run outside the sandbox, before it: reads the API key file named in argv[1], hands it to the agent through a pipe
# (CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR) and starts the rest of argv. Claude Code reads the pipe when it starts, before
# it runs any command; after that the pipe is empty, and a pipe can't be read again from the start, so a command
# that inherits it gets nothing
KEY_PIPE = """
import os, sys
with open(sys.argv[1], "rb") as f:
    key = f.read()
read, write = os.pipe()
os.write(write, key)
os.close(write)
os.set_inheritable(read, True)
os.environ["CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"] = str(read)
os.execv(sys.argv[2], sys.argv[2:])
"""


def _key_file(argv: list[str]) -> Path | None:
    """The API key file Claude Code's settings would read with apiKeyHelper (`cat <file>`), if they do."""
    for i, word in enumerate(argv):
        if i and argv[i - 1] == "--settings":
            import json
            helper = json.loads(word).get("apiKeyHelper")
            if helper:
                words = shlex.split(helper)
                if len(words) == 2 and words[0] == "cat":
                    return Path(words[1])
    return None


def _agent_argv(argv: list[str]) -> list[str]:
    """`argv` with the agent's own sandbox switched off. macOS doesn't let a sandboxed program start a sandbox of its
    own (sandbox_apply fails, so every command would fail), so on macOS the one profile confines the agent and its
    commands both. Claude Code's --settings sandbox and Codex's permission profile are the ones the worker sets."""
    out: list[str] = []
    codex = False
    for word in argv:
        if out and out[-1] == "--settings":
            import json
            settings = json.loads(word)
            settings["sandbox"] = {"enabled": False}
            settings.pop("apiKeyHelper", None)  # a command it runs (cat) can't read the private home: see KEY_PIPE
            word = json.dumps(settings)
        elif out and out[-1] == "-c" and (word.startswith("permissions.handoff.")
                                          or word == 'default_permissions="handoff"'):
            out.pop()  # Codex's permission profile can only build on its own sandbox; there is none here
            codex = True
            continue
        out.append(word)
    if codex:
        at = out.index("exec") + 1 if "exec" in out else 1
        out[at:at] = ["--sandbox", "danger-full-access"]
    return out


def _wrap_mac(program: str, argv: list[str], *, worktree: Path, home: Path, private: dict[Path, Path],
              read_only: list[Path], env: dict[str, str], hidden: list[Path]) -> list[str]:
    """`argv` run by sandbox-exec under one profile for the agent and every command it runs (see the module
    docstring). Only the agent's own program (agent_binary) has the network and its private home; a command can
    neither reach the network nor start that program again. A later rule wins over an earlier one, so what is
    denied again comes after what is allowed. Sets HOME and TMPDIR in `env`, which the caller starts the agent with."""
    if not private:
        raise ValueError("The macOS sandbox needs a private folder for the agent to log in from.")
    agent = agent_binary(argv[0])
    tmp = private_temp(next(iter(private)))
    tmp.mkdir(mode=0o700, exist_ok=True)
    tmp = _real(tmp)
    hosts = [_real(host) for host in private]
    worktree, real_home = _real(worktree), _real(home)
    project = _project_of(worktree)
    reads = list(dict.fromkeys([worktree, tmp, *(_real(path) for path in read_only if os.path.exists(path))]))
    network = _extra_read_only(env)
    hidden_paths = [path for path in dict.fromkeys(_real(path) for path in hidden)
                    if not any(_inside(path, needed) for needed in network)]  # as bubblewrap's masks
    # A credential folder that holds the agent's install (~/.claude/local) stays as it is: the rest of the home
    # is denied already, so denying it too would only hide the install
    sensitive = [path for path in (_real(real_home / name) for name in SENSITIVE)
                 if not any(_inside(allowed, path) for allowed in reads)]
    me = f"(process-path {_sbpl_string(str(agent))})"
    lines = ["(version 1)", "(allow default)"]
    lines += _rule("deny", "file-read*", _subpaths([*MAC_HIDDEN, real_home, *([project] if project else [])]))
    lines += _rule("allow", "file-read*", _subpaths(reads))
    lines += [f"(allow file-read* (require-all {me} (require-any {' '.join(_subpaths(hosts))})))"]
    lines += _rule("allow", "file-read-metadata", _above([*reads, *hosts]))
    lines += _rule("deny", "file-read*", _subpaths([*hidden_paths, *sensitive]))
    # Seen to be there, but not read: as bubblewrap shows them empty. (An agent refuses a command naming a path it
    # can't even look up.)
    lines += _rule("allow", "file-read-metadata", _subpaths(hidden_paths))
    lines += _rule("allow", "file-read*", _subpaths(network))  # after the denies, as bubblewrap shows it after
    lines += _rule("allow", "file-read-metadata", _above(network))
    lines += ["(deny file-write*)",
              "(allow file-write* " + " ".join([*_subpaths([worktree, tmp]), '(literal "/dev/null")',
                                                '(regex #"^/dev/tty")', '(regex #"^/dev/fd/")']) + ")",
              f"(allow file-write* (require-all {me} (require-any {' '.join(_subpaths(hosts))})))",
              f"(deny file-write* {_subpaths([agent.parent])[0]})",  # its program can't be swapped
              f"(deny network* (require-not {me}))",
              # Started by sandbox-exec, or by itself; never by a command, which would get the network that way
              f"(deny process-exec (require-all (literal {_sbpl_string(str(agent))}) "
              f"(require-not (require-any (process-path {_sbpl_string(SANDBOX_EXEC)}) {me}))))",
              "(deny process-info* (target others))",
              "(deny signal)",
              "(allow signal (target same-sandbox))",
              f"(deny mach-lookup (require-all (require-not {me}) "
              f"(global-name {' '.join(_sbpl_string(name) for name in MAC_SERVICES)})))",
              '(deny mach-lookup (global-name "com.apple.SecurityServer"))']  # your keychain, the agent's too
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    # Claude Code keeps its own temp folder at /tmp/claude-<uid> unless told otherwise, and /private/tmp is hidden
    env["CLAUDE_CODE_TMPDIR"] = str(tmp)
    wrapped = [program, "-p", "\n".join(lines), *_agent_argv(argv)]
    key = _key_file(argv)
    if key is not None:
        return [sys.executable, "-I", "-c", KEY_PIPE, str(key), *wrapped]
    return wrapped
