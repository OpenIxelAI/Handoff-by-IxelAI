"""Connecting the apps: the MCP config for Claude Desktop, Claude Code, Codex, Gemini CLI, OpenCode and Grok Build.

Every snippet uses absolute paths to this install. `--write` merges into each
app's own config and keeps everything else in it: the result is parsed and
compared with the original before anything is saved, and saved atomically.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from handoff.proc import find_on_path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

SERVER_NAME = "handoff"
CODEX_TOOL_TIMEOUT = 960  # seconds: longer than a review with the Ixel panel may take (ixel.TIMEOUT_SEC)

JSON_TOOL_TIMEOUT_MS = CODEX_TOOL_TIMEOUT * 1000  # Gemini CLI's and OpenCode's (each tool call), in milliseconds

HOSTS = ("claude-code", "codex", "claude-desktop", "gemini", "opencode", "grok")
HOST_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "claude-desktop": "Claude Desktop",
               "gemini": "Gemini CLI", "opencode": "OpenCode", "grok": "Grok Build"}
# The name each app's agent goes by on the board (its `--as`)
AGENT_NAMES = {"claude-code": "claude", "claude-desktop": "claude", "codex": "codex", "gemini": "gemini",
               "opencode": "opencode", "grok": "grok"}


class SetupError(Exception):
    """A config Handoff won't touch; the message says what to do instead."""


@dataclass
class Result:
    host: str
    status: str   # "added", "updated", "unchanged", "skipped"; "removed", "not there"
    detail: str


# ── What to run ──────────────────────────────────────────────────────────────

def launch_command() -> tuple[str, list[str]]:
    """The absolute command that starts this install of Handoff, and any leading arguments."""
    exe = Path(sys.executable)
    if sys.platform == "win32" and sys.prefix != sys.base_prefix:
        # The environment's python.exe, not pip's handoff.exe: python.exe is signed (Smart App Control blocks
        # the unsigned handoff.exe) and an update never replaces it, so an app holding it open can't stop
        # `handoff update`. -I: nothing is imported from the project folder the app starts it in.
        return str(exe), ["-I", "-m", "handoff"]
    name = "handoff.exe" if sys.platform == "win32" else "handoff"
    # next to python in a virtualenv; in Scripts\ beside a plain Windows install
    for script in (exe.with_name(name), exe.parent / "Scripts" / name):
        if script.exists():
            return str(script), []
    return str(exe), ["-m", "handoff"]


def server_args(me: str, project: Path | None = None) -> tuple[str, list[str]]:
    command, prefix = launch_command()
    args = [*prefix, "mcp", "--as", me]
    if project is not None:
        args += ["--project", str(project)]
    return command, args


def _shell_word(word: str) -> str:
    if word and not any(c in word for c in ' \t"\'&|<>()^%$`;'):
        return word
    if sys.platform == "win32":
        return f'"{word}"'
    return "'" + word.replace("'", "'\\''") + "'"


# ── Where each app keeps its config ──────────────────────────────────────────

def home() -> Path:
    return Path.home()


# Claude Desktop from Anthropic's Windows installer runs as an app package. Windows gives it its own copy of
# %APPDATA% (in its package's folder under %LOCALAPPDATA%\Packages), and once the app has made its config file
# there, that's the one it reads: a config written to %APPDATA%\Claude is ignored.
CLAUDE_DESKTOP_PACKAGE = "Claude_pzs8sxrjxfjjc"


def _roaming() -> Path:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) if appdata else home() / "AppData" / "Roaming"


def desktop_package() -> Path:
    """Claude Desktop's app package folder on Windows. Found from APPDATA (the Local folder beside Roaming), not
    LOCALAPPDATA, so a home faked through APPDATA, as the tests do, fakes this too. Where an organization moves
    Roaming to a network share, it isn't found, and Handoff uses %APPDATA%\\Claude as it did before."""
    return _roaming().parent / "Local" / "Packages" / CLAUDE_DESKTOP_PACKAGE


def _plain_desktop_config() -> Path:
    return _roaming() / "Claude" / "claude_desktop_config.json"


def desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        packaged = desktop_package() / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
        plain = _plain_desktop_config()
        # Without its own copy, the packaged app reads %APPDATA%'s (the app from before packaging kept it there).
        # With neither file, a new one goes in the app's own folder, which it reads first.
        if packaged.is_file() or (not plain.is_file() and packaged.parent.is_dir()):
            return packaged
        return plain
    return home() / ".config" / "Claude" / "claude_desktop_config.json"


def codex_home() -> Path:
    custom = os.environ.get("CODEX_HOME")
    return Path(custom).expanduser() if custom else home() / ".codex"


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def gemini_config_path() -> Path:
    return home() / ".gemini" / "settings.json"


def opencode_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else home() / ".config") / "opencode"


def opencode_config_path() -> Path:
    """Its global config: opencode.json, or opencode.jsonc when that's the one there is."""
    folder = opencode_config_dir()
    plain, with_comments = folder / "opencode.json", folder / "opencode.jsonc"
    return with_comments if with_comments.exists() and not plain.exists() else plain


def grok_config_path() -> Path:
    return home() / ".grok" / "config.toml"


def claude_code_state_path() -> Path:
    return home() / ".claude.json"


def claude_code_settings_path() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(custom).expanduser() if custom else home() / ".claude") / "settings.json"


PLUGIN_NAME = "handoff"


def plugin_enabled(host: str) -> str | None:
    """The Handoff plugin's id (handoff@<marketplace>) if it's enabled in this app, for the user. Read only."""
    try:
        if host == "claude-code":
            data = json.loads(claude_code_settings_path().read_text(encoding="utf-8"))
            plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
            enabled = [k for k, v in (plugins or {}).items() if v is True]
        elif host == "codex":
            plugins = tomllib.loads(codex_config_path().read_text(encoding="utf-8")).get("plugins")
            enabled = [k for k, v in (plugins or {}).items() if isinstance(v, dict) and v.get("enabled", True)]
        else:
            return None
    except (OSError, ValueError, AttributeError):
        return None
    return next((k for k in sorted(enabled) if k.split("@")[0] == PLUGIN_NAME), None)


def installed(host: str) -> bool:
    if host == "claude-code":
        return find_on_path("claude") is not None
    if host == "codex":
        return codex_home().is_dir() or find_on_path("codex") is not None
    if host == "claude-desktop":
        return desktop_config_path().parent.is_dir() or (sys.platform == "win32" and desktop_package().is_dir())
    if host == "gemini":
        return find_on_path("gemini") is not None or gemini_config_path().parent.is_dir()
    if host == "opencode":
        return find_on_path("opencode") is not None or opencode_config_dir().is_dir()
    if host == "grok":
        return find_on_path("grok") is not None or grok_config_path().exists()
    raise ValueError(host)


# ── Snippets to copy ─────────────────────────────────────────────────────────

def desktop_entry(project: Path) -> dict:
    command, args = server_args("claude", project)
    return {"command": command, "args": args}


def claude_code_command() -> list[str]:
    command, args = server_args("claude")
    return ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", command, *args]


def _toml_string(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def codex_lines(timeout: int | float = CODEX_TOOL_TIMEOUT, me: str = "codex") -> list[str]:
    """[mcp_servers.handoff] for Codex, or Grok Build (the same form, with me="grok")."""
    command, args = server_args(me)
    return [f"command = {_toml_string(command)}",
            "args = [" + ", ".join(_toml_string(a) for a in args) + "]",
            f"tool_timeout_sec = {timeout}"]


def _longer(yours: object, ours: int | float) -> int | float:
    """Our timeout, or a longer one you already set."""
    longer = (isinstance(yours, (int, float)) and not isinstance(yours, bool) and math.isfinite(yours)
              and yours > ours)
    return yours if longer else ours


def _codex_timeout(config: dict) -> int | float:
    """The tool timeout to write: ours, or a longer one you already set."""
    servers = config.get("mcp_servers")
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    return _longer(entry.get("tool_timeout_sec") if isinstance(entry, dict) else None, CODEX_TOOL_TIMEOUT)


def _kept(current: dict | None, unless: tuple[str, ...]) -> dict:
    """What you set in our entry, less what would make the app connect somewhere else instead."""
    return {k: v for k, v in (current or {}).items() if k not in unless}


def gemini_entry(current: dict | None = None) -> dict:
    """Gemini CLI's "mcpServers" entry. Anything else you set in it (trust, env…) is kept."""
    command, args = server_args("gemini")
    return {**_kept(current, ("url", "httpUrl", "tcp", "headers", "oauth", "authProviderType", "targetAudience",
                              "targetServiceAccount")), "command": command, "args": args,
            "timeout": _longer((current or {}).get("timeout"), JSON_TOOL_TIMEOUT_MS)}


def opencode_entry(current: dict | None = None) -> dict:
    """OpenCode's "mcp" entry. Anything else you set in it (environment…) is kept. Its timeout covers each
    tool call, and a review with the Ixel panel takes minutes."""
    command, args = server_args("opencode")
    return {**_kept(current, ("url", "headers", "oauth", "disabled")), "type": "local", "command": [command, *args],
            "enabled": True, "timeout": _longer((current or {}).get("timeout"), JSON_TOOL_TIMEOUT_MS)}


def snippets(project: Path | None) -> str:
    """Copy-paste setup for each app, with this install's absolute paths."""
    code = " ".join(_shell_word(w) for w in claude_code_command())
    parts = [
        "Claude Code (it starts Handoff in the project you open, so one line covers every project):",
        f"  {code}",
        "",
        f"Codex: add to {codex_config_path()}",
        "  [mcp_servers.handoff]",
        *[f"  {line}" for line in codex_lines()],
        "",
    ]
    if project is None:
        parts += ["Claude Desktop: it has no project folder of its own, so its config names one. Run",
                  "  handoff setup   inside your project to get it.", ""]
    else:
        entry = json.dumps({"mcpServers": {SERVER_NAME: desktop_entry(project)}}, indent=2, ensure_ascii=False)
        parts += [f"Claude Desktop: Settings → Developer → Edit Config ({desktop_config_path()}),",
                  "  then add this server inside \"mcpServers\" (it points at this project; run setup again",
                  "  in another project to switch):",
                  *[f"  {line}" for line in entry.splitlines()], ""]
    for host, where, section in (("gemini", gemini_config_path(), "mcpServers"),
                                 ("opencode", opencode_config_path(), "mcp")):
        entry = json.dumps({section: {SERVER_NAME: _JSON_HOSTS[host][2]()}}, indent=2, ensure_ascii=False)
        parts += [f"{HOST_LABELS[host]}: add this server inside \"{section}\" in {where}",
                  "  (it starts Handoff in the folder you open it in):",
                  *[f"  {line}" for line in entry.splitlines()], ""]
    parts += [f"Grok Build: add to {grok_config_path()}",
              "  [mcp_servers.handoff]",
              *[f"  {line}" for line in codex_lines(me="grok")],
              ""]
    return "\n".join(parts)


# ── Writing: shared ──────────────────────────────────────────────────────────

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.handoff-tmp")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    try:
        tmp.unlink()  # left by a write that was cut short (or a link someone put there)
    except FileNotFoundError:
        pass
    # Made new, with the final file's permissions from the start: these configs can hold keys
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(tmp, flags, mode), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if sys.platform != "win32":
        os.chmod(tmp, mode)  # (the umask may have taken some away)
    os.replace(tmp, path)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise SetupError(f"{path} isn't UTF-8 text ({exc}); Handoff won't edit it.") from exc


# ── Claude Desktop (JSON) ────────────────────────────────────────────────────

def merge_desktop(text: str | None, entry: dict, path: Path) -> str:
    """The Desktop config with our server added or replaced, and nothing else changed."""
    if text is None or not text.strip():
        data: dict = {}
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SetupError(f"{path} isn't valid JSON ({exc.msg}, line {exc.lineno}). Fix it, or add the "
                             "server by hand from `handoff setup`.") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path} doesn't hold a JSON object; Handoff won't edit it.")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f'"mcpServers" in {path} isn\'t an object; Handoff won\'t edit it.')
    servers[SERVER_NAME] = entry
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_desktop(project: Path) -> Result:
    path = desktop_config_path()
    old = _read(path)
    new = merge_desktop(old, desktop_entry(project), path)
    if old is not None and old.strip() and json.loads(old) == json.loads(new):
        return Result("claude-desktop", "unchanged", f"already set up in {path}")
    _atomic_write(path, new)
    verb = "added" if old is None or SERVER_NAME not in (json.loads(old or "{}").get("mcpServers") or {}) \
        else "updated"
    return Result("claude-desktop", verb, f"{path} (project {project}); restart Claude Desktop")


# ── Codex (TOML) ─────────────────────────────────────────────────────────────

_OUR_KEYS = ("command", "args", "tool_timeout_sec")
# What would point Codex at another server instead, or keep ours turned off: setup takes them out of our entry
_DROPPED_KEYS = ("url", "bearer_token_env_var", "http_headers", "env_http_headers", "enabled")


def _header_name(line: str) -> str | None:
    """`[mcp_servers.handoff]` → 'mcp_servers.handoff' (spaces and simple quotes removed)."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    end = stripped.find("]")
    if end == -1:
        return None
    return stripped[1:end].replace(" ", "").replace('"', "").replace("'", "")


def _key_of(line: str) -> str | None:
    stripped = line.strip()
    eq = stripped.find("=")
    if eq <= 0:
        return None
    return stripped[:eq].strip().strip('"').strip("'")


def _bracket_depth(line: str) -> int:
    depth, quote = 0, ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            break
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth


def _strip_ours(data: dict) -> dict:
    """A copy of a parsed config without the keys Handoff manages."""
    copy = json.loads(json.dumps(data, default=str))
    server = copy.get("mcp_servers", {}).get(SERVER_NAME)
    if isinstance(server, dict):
        for key in _OUR_KEYS + _DROPPED_KEYS:
            server.pop(key, None)
        if not server:
            copy["mcp_servers"].pop(SERVER_NAME)
        if not copy["mcp_servers"]:
            copy.pop("mcp_servers")
    return copy


def merge_codex(text: str | None, lines: list[str], path: Path) -> str:
    """config.toml with [mcp_servers.handoff] added or updated, and nothing else changed."""
    old_text = text or ""
    try:
        old = tomllib.loads(old_text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{path} isn't valid TOML ({exc}). Fix it, or add the server by hand from "
                         "`handoff setup`.") from exc

    source = old_text.splitlines()
    out: list[str] = []
    found = False
    i = 0
    in_ours = False
    multiline = ""
    while i < len(source):
        line = source[i]
        if multiline:  # inside a multi-line string: copy it as is
            out.append(line)
            if line.count(multiline) % 2 == 1:
                multiline = ""
            i += 1
            continue
        header = _header_name(line)
        if header is not None:
            in_ours = header == f"mcp_servers.{SERVER_NAME}"
            out.append(line)
            if in_ours:
                found = True
                out.extend(lines)
            i += 1
            continue
        if in_ours and _key_of(line) in _OUR_KEYS + _DROPPED_KEYS:
            depth = _bracket_depth(line)  # skip our old value, even a multi-line array
            i += 1
            while depth > 0 and i < len(source):
                depth += _bracket_depth(source[i])
                i += 1
            continue
        for quote in ('"""', "'''"):
            if line.count(quote) % 2 == 1:
                multiline = quote
                break
        out.append(line)
        i += 1

    if not found:
        if out and out[-1].strip():
            out.append("")
        out += [f"[mcp_servers.{SERVER_NAME}]", *lines]
    new_text = "\n".join(out) + "\n"

    # Prove it: the new file parses, holds our server, and everything else is exactly as before.
    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"Couldn't safely edit {path} (its handoff entry is written in a form Handoff "
                         "doesn't edit). Add the server by hand from `handoff setup`.") from exc
    ours = tomllib.loads("\n".join(lines))
    server = new.get("mcp_servers", {}).get(SERVER_NAME, {})
    if any(server.get(k) != v for k, v in ours.items()) or _strip_ours(new) != _strip_ours(old):
        raise SetupError(f"Couldn't safely edit {path}. Add the server by hand from `handoff setup`.")
    return new_text


def write_codex() -> Result:
    path = codex_config_path()
    old = _read(path)
    try:
        timeout = _codex_timeout(tomllib.loads(old or ""))
    except tomllib.TOMLDecodeError:
        timeout = CODEX_TOOL_TIMEOUT  # (merge_codex says what's wrong with it)
    new = merge_codex(old, codex_lines(timeout), path)
    if old is not None and tomllib.loads(old) == tomllib.loads(new):
        return Result("codex", "unchanged", f"already set up in {path}")
    _atomic_write(path, new)
    had = old is not None and SERVER_NAME in tomllib.loads(old).get("mcp_servers", {})
    return Result("codex", "updated" if had else "added", f"{path}; restart Codex")


# ── Grok Build (TOML, in Codex's form) ───────────────────────────────────────

def write_grok() -> Result:
    path = grok_config_path()
    old = _read(path)
    try:
        timeout = _codex_timeout(tomllib.loads(old or ""))
    except tomllib.TOMLDecodeError:
        timeout = CODEX_TOOL_TIMEOUT  # (merge_codex says what's wrong with it)
    new = merge_codex(old, codex_lines(timeout, me="grok"), path)
    if old is not None and tomllib.loads(old) == tomllib.loads(new):
        return Result("grok", "unchanged", f"already set up in {path}")
    _atomic_write(path, new)
    had = old is not None and SERVER_NAME in tomllib.loads(old).get("mcp_servers", {})
    return Result("grok", "updated" if had else "added", f"{path}; restart Grok Build")


# ── Gemini CLI and OpenCode (JSON) ───────────────────────────────────────────

def _without_comments(text: str, commas: bool = True) -> str:
    """JSON with comments (JSONC) as plain JSON: // and /* */ comments outside strings taken out, and, with
    `commas`, a comma just before a closing bracket (OpenCode allows those; Gemini CLI doesn't). For reading
    only."""
    out: list[str] = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        elif ch == "," and commas and _closes_next(text, i + 1):
            pass
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _closes_next(text: str, i: int) -> bool:
    """Is the next thing after `i`, past spaces and comments, a closing bracket?"""
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            return text[i] in "}]"
    return False


def _read_json(text: str, path: Path, host: str = "opencode") -> object:
    """A settings file's contents as its app reads it, for reading (doctor, --remove): Claude Desktop takes
    plain JSON, Gemini CLI comments too, and OpenCode trailing commas as well. Raises SetupError if it can't."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if host not in _JSON_HOSTS:
            raise SetupError(f"{path} isn't valid JSON ({exc.msg}, line {exc.lineno}).") from exc
    try:
        return json.loads(_without_comments(text, commas=host == "opencode"))
    except json.JSONDecodeError as exc:
        raise SetupError(f"{path} isn't valid JSON ({exc.msg}, line {exc.lineno}).") from exc


def _json_config(text: str | None, path: Path) -> dict:
    """The config to edit: plain JSON only, since writing it back would lose your comments."""
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        try:
            json.loads(_without_comments(text))
        except json.JSONDecodeError:
            raise SetupError(f"{path} isn't valid JSON ({exc.msg}, line {exc.lineno}). Fix it, or add the "
                             "server by hand: `handoff setup` shows the entry.") from exc
        raise SetupError(f"{path} has comments (or trailing commas) in it, and Handoff doesn't edit those, since "
                         "saving it would lose them. Add the server by hand: `handoff setup` shows the "
                         "entry.") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path} doesn't hold a JSON object; Handoff won't edit it.")
    return data


def merge_json_host(host: str, text: str | None, path: Path) -> str:
    """Gemini CLI's or OpenCode's config with our server added or updated, and nothing else changed."""
    _, section, make_entry = _JSON_HOSTS[host]
    data = _json_config(text, path)
    servers = data.setdefault(section, {})
    if not isinstance(servers, dict):
        raise SetupError(f'"{section}" in {path} isn\'t an object; Handoff won\'t edit it.')
    current = servers.get(SERVER_NAME)
    servers[SERVER_NAME] = make_entry(current if isinstance(current, dict) else None)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_json_host(host: str) -> Result:
    path = _JSON_HOSTS[host][0]()
    section = _JSON_HOSTS[host][1]
    old = _read(path)
    new = merge_json_host(host, old, path)
    before = _json_config(old, path)
    if old is not None and old.strip() and before == json.loads(new):
        return Result(host, "unchanged", f"already set up in {path}")
    _atomic_write(path, new)
    had = isinstance(before.get(section), dict) and SERVER_NAME in before[section]
    return Result(host, "updated" if had else "added", f"{path}; restart {HOST_LABELS[host]}")


# Where each keeps its config, the object its servers go in, and our entry
_JSON_HOSTS = {"gemini": (gemini_config_path, "mcpServers", gemini_entry),
               "opencode": (opencode_config_path, "mcp", opencode_entry)}


# ── Claude Code (its own CLI) ────────────────────────────────────────────────

def claude_code_configured() -> dict | None:
    """Our user-scope entry in Claude Code's state file, if any (read only)."""
    try:
        data = json.loads(claude_code_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    server = (data.get("mcpServers") or {}).get(SERVER_NAME) if isinstance(data, dict) else None
    return server if isinstance(server, dict) else None


def write_claude_code() -> Result:
    claude = find_on_path("claude")
    if claude is None:
        raise SetupError("The `claude` command isn't on PATH, so Handoff can't add itself to Claude Code. "
                         "Install Claude Code, or run the `claude mcp add` line from `handoff setup` later.")
    command, args = server_args("claude")
    current = claude_code_configured()
    if current and current.get("command") == command and current.get("args") == args:
        return Result("claude-code", "unchanged", "already set up, user scope")
    # `add` refuses to overwrite, so remove any old entry first (user scope only)
    subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=60)
    proc = subprocess.run([claude, *claude_code_command()[1:]], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=60)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise SetupError("`claude mcp add` failed" + (f": {detail[-1]}" if detail else "") + ".")
    return Result("claude-code", "updated" if current else "added", "user scope; restart Claude Code")


# ── Claude Code's session hook (its settings.json) ──────────────────────────
#
# When a session starts, Claude Code runs `handoff hook session-start --as claude`, which tells it which
# tasks wait for it, by ref only (hook.py). Only Handoff's own entry in "hooks" → "SessionStart" changes;
# a copy of the file as it was is kept next to it.

HOOK_EVENT = "SessionStart"
HOOK_TIMEOUT = 15  # seconds
# handoff hook session-start, however this install starts it (handoff, handoff.exe, python -I -m handoff), quoted or not
_HOOK_MARK = re.compile(r"""(?:-m handoff|(?:^|[\s/\\'"])handoff(?:\.exe|\.cmd)?['"]?)\s+hook\s+session-start""")
# Characters Git Bash and PowerShell (Claude Code's two shells for hooks on Windows) both read as part of one
# plain word; any other character needs quoting, and no quoting reads the same way in both
_PLAIN = set("_-./:")


def hook_handler(me: str = "claude") -> dict:
    """The hook as Claude Code runs it. Elsewhere it's one line for sh. On Windows the line runs in Git Bash, or
    in PowerShell without it: a path both read the same way unquoted is written as one line, which every
    Claude Code version runs; a path that needs quoting (a space in the user name) is given as the program and
    its arguments, with no shell at all, which Claude Code runs from version 2.1.139 (May 2026)."""
    command, prefix = launch_command()
    words = [*prefix, "hook", "session-start", "--as", me]
    if sys.platform != "win32":
        return {"type": "command", "command": " ".join(_shell_word(w) for w in (command, *words)),
                "timeout": HOOK_TIMEOUT}
    forward = command.replace("\\", "/")
    if all(c.isalnum() or c in _PLAIN for c in forward):
        return {"type": "command", "command": " ".join([forward, *words]), "timeout": HOOK_TIMEOUT}
    return {"type": "command", "command": command, "args": words, "timeout": HOOK_TIMEOUT}


def _is_ours(hook: object) -> bool:
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return False
    args = hook.get("args")
    words = [hook["command"], *(args if isinstance(args, list) and all(isinstance(a, str) for a in args) else [])]
    return _HOOK_MARK.search(" ".join(words)) is not None


def runs_this_handoff(hook: dict | None) -> bool:
    """Does this hook (from claude_hook_set) start this install of Handoff the way setup writes it?"""
    want = hook_handler()
    return hook is not None and (hook.get("command"), hook.get("args")) == (want["command"], want.get("args"))


def _without_ours(groups: list) -> list:
    kept = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(map(_is_ours, group["hooks"])):
            rest = [h for h in group["hooks"] if not _is_ours(h)]
            if rest:
                kept.append({**group, "hooks": rest})
        else:
            kept.append(group)
    return kept


def _settings(text: str | None, path: Path) -> dict:
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{path} isn't valid JSON ({exc.msg}, line {exc.lineno}). Fix it, or add the hook by "
                         "hand from `handoff setup`.") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path} doesn't hold a JSON object; Handoff won't edit it.")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get(HOOK_EVENT, []), list):
        raise SetupError(f'"hooks" in {path} isn\'t in a form Handoff knows; it won\'t edit it. Add the hook by hand '
                         "from `handoff setup`.")
    return data


def hook_entry(me: str = "claude") -> dict:
    # no matcher: every way a session starts (new, resumed, cleared, compacted, forked)
    return {"hooks": [hook_handler(me)]}


def merge_claude_hook(text: str | None, path: Path, add: bool = True) -> str:
    """settings.json with Handoff's session hook added (or replaced, or with add=False removed), and nothing
    else changed: checked by comparing everything but our entry before and after."""
    data = _settings(text, path)
    new = json.loads(json.dumps(data))
    hooks = new.setdefault("hooks", {})
    groups = _without_ours(hooks.get(HOOK_EVENT, []))
    if add:
        groups.append(hook_entry())
    if groups:
        hooks[HOOK_EVENT] = groups
    else:
        hooks.pop(HOOK_EVENT, None)
        if not hooks:
            new.pop("hooks")

    def stripped(d: dict) -> dict:
        d = json.loads(json.dumps(d))
        h = d.get("hooks", {})
        if isinstance(h, dict):
            rest = _without_ours(h.get(HOOK_EVENT, []))
            if rest:
                h[HOOK_EVENT] = rest
            else:
                h.pop(HOOK_EVENT, None)
            if not h:
                d.pop("hooks", None)
        return d

    if stripped(new) != stripped(data):  # pragma: no cover - a guard, not a path
        raise SetupError(f"Handoff's edit to {path} would have changed more than its own hook; nothing was saved.")
    return json.dumps(new, indent=2, ensure_ascii=False) + "\n"


def claude_hook_set() -> dict | None:
    """Handoff's session hook in settings.json, if it's there (read only)."""
    try:
        data = json.loads(claude_code_settings_path().read_text(encoding="utf-8"))
        groups = data["hooks"][HOOK_EVENT]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for group in groups if isinstance(groups, list) else []:
        for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
            if _is_ours(hook):
                return hook
    return None


def write_claude_hook(add: bool = True) -> Result:
    """Add (or with add=False remove) the session hook in Claude Code's settings.json. The plugin doesn't bring
    one: its line would be a bare `handoff`, which Git Bash doesn't find where Smart App Control made it
    handoff.cmd, and Claude Code would run a plugin's copy as well as this one."""
    path = claude_code_settings_path()
    old = _read(path)
    current = claude_hook_set()
    if not add and current is None:
        return Result("claude-hook", "unchanged", "not set")
    if add and runs_this_handoff(current):
        return Result("claude-hook", "unchanged", f"in {path}")
    new = merge_claude_hook(old, path, add=add)
    if old is not None:
        _atomic_write(path.with_name(path.name + ".handoff-bak"), old)
    _atomic_write(path, new)
    if not add:
        return Result("claude-hook", "removed", f"from {path}")
    backup = f"; a copy of the old file is {path.name}.handoff-bak" if old is not None else ""  # (none to copy)
    return Result("claude-hook", "updated" if current else "added", f"in {path}{backup}")


def write(host: str, project: Path | None) -> Result:
    if host == "claude-desktop":
        if project is None:
            return Result(host, "skipped", "needs a project: run `handoff setup --write` inside one")
        return write_desktop(project)
    if host == "codex":
        return write_codex()
    if host == "claude-code":
        return write_claude_code()
    if host == "grok":
        return write_grok()
    if host in _JSON_HOSTS:
        return write_json_host(host)
    raise ValueError(host)


# ── Taking it out again (`handoff setup --remove`) ──────────────────────────
#
# Everything setup added, and only that: Handoff's server in each app's config, and the session hook. Each
# says what it removed, or that there was nothing to remove, so running it twice is fine.

def _without_handoff(data: dict) -> dict:
    copy = json.loads(json.dumps(data, default=str))
    servers = copy.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(SERVER_NAME, None)
        if not servers:
            copy.pop("mcp_servers")
    return copy


def unmerge_codex(text: str, path: Path) -> str:
    """config.toml without [mcp_servers.handoff] (and its own sub-tables), and nothing else changed."""
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{path} isn't valid TOML ({exc}). Remove the [mcp_servers.{SERVER_NAME}] table by "
                         "hand.") from exc
    ours = f"mcp_servers.{SERVER_NAME}"
    out: list[str] = []
    skipping, multiline = False, ""
    for line in text.splitlines():
        if multiline:  # inside a multi-line string: it goes with its table
            if not skipping:
                out.append(line)
            if line.count(multiline) % 2 == 1:
                multiline = ""
            continue
        header = _header_name(line)
        if header is not None:
            was = skipping
            skipping = header == ours or header.startswith(ours + ".")
            if skipping:
                while out and not out[-1].strip():  # the blank line that set our table apart
                    out.pop()
                continue
            if was and out:
                out.append("")
        if skipping:
            for quote in ('"""', "'''"):
                if line.count(quote) % 2 == 1:
                    multiline = quote
                    break
            continue
        for quote in ('"""', "'''"):
            if line.count(quote) % 2 == 1:
                multiline = quote
                break
        out.append(line)
    new_text = "\n".join(out).strip("\n") + "\n" if any(line.strip() for line in out) else ""
    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        new = None
    if new is None or new != _without_handoff(old):
        raise SetupError(f"Couldn't safely edit {path} (its handoff entry is written in a form Handoff doesn't "
                         f"edit). Remove the [mcp_servers.{SERVER_NAME}] entry by hand.")
    return new_text


def remove_codex(host: str = "codex") -> Result:
    """Codex's [mcp_servers.handoff] (or, with host="grok", Grok Build's)."""
    path = codex_config_path() if host == "codex" else grok_config_path()
    old = _read(path)
    try:
        there = old is not None and SERVER_NAME in (tomllib.loads(old).get("mcp_servers") or {})
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{path} isn't valid TOML ({exc}); Handoff won't edit it.") from exc
    if not there:
        return Result(host, "not there", f"nothing in {path}")
    _atomic_write(path, unmerge_codex(old, path))
    return Result(host, "removed", f"from {path}; restart {HOST_LABELS[host]}")


def remove_json_host(host: str) -> Result:
    path_of, section, _ = _JSON_HOSTS[host]
    path = path_of()
    old = _read(path)
    found = _read_json(old, path, host) if old and old.strip() else {}
    servers = found.get(section) if isinstance(found, dict) else None
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return Result(host, "not there", f"nothing in {path}")
    try:
        data = _json_config(old, path)
    except SetupError as exc:
        raise SetupError(f"{exc} To remove it, delete \"{SERVER_NAME}\" under \"{section}\" in it by hand."
                         .replace("Add the server by hand: `handoff setup` shows the entry. ", "")) from exc
    servers = data[section]
    del servers[SERVER_NAME]
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return Result(host, "removed", f"from {path}; restart {HOST_LABELS[host]}")


def _remove_from_desktop_config(path: Path) -> bool:
    old = _read(path)
    try:
        data = json.loads(old) if old and old.strip() else {}
    except json.JSONDecodeError as exc:
        raise SetupError(f"{path} isn't valid JSON ({exc.msg}, line {exc.lineno}); Handoff won't edit it.") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return False
    del servers[SERVER_NAME]
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


def remove_desktop() -> Result:
    path = desktop_config_path()
    paths = [path]
    if sys.platform == "win32" and path != _plain_desktop_config():
        # One an earlier Handoff wrote to %APPDATA%: the app ignores it while it has its own, not once that's gone
        paths.append(_plain_desktop_config())
    removed = [p for p in paths if _remove_from_desktop_config(p)]
    if not removed:
        return Result("claude-desktop", "not there", f"nothing in {path}")
    return Result("claude-desktop", "removed", f"from {' and '.join(map(str, removed))}; restart Claude Desktop")


def remove_claude_code() -> Result:
    if claude_code_configured() is None:
        return Result("claude-code", "not there", f"nothing in {claude_code_state_path()}")
    claude = find_on_path("claude")
    if claude is not None:  # its own command, as setup used to add it
        proc = subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=60)
        if proc.returncode != 0 and claude_code_configured() is not None:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise SetupError("`claude mcp remove` failed" + (f": {detail[-1]}" if detail else "") + ".")
        return Result("claude-code", "removed", "user scope; restart Claude Code")
    # Claude Code itself is gone: take the entry out of the file it left
    path = claude_code_state_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["mcpServers"][SERVER_NAME]
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return Result("claude-code", "removed", f"from {path}")


def remove(host: str) -> Result:
    if host == "claude-desktop":
        return remove_desktop()
    if host == "codex":
        return remove_codex()
    if host == "claude-code":
        return remove_claude_code()
    if host == "grok":
        return remove_codex("grok")
    if host in _JSON_HOSTS:
        return remove_json_host(host)
    raise ValueError(host)


# ── Doctor: what's configured ────────────────────────────────────────────────

def configured(host: str) -> tuple[bool, str]:
    """Is Handoff in this app, through its plugin or its config, and what does it point at? Read only."""
    plugin = plugin_enabled(host)
    ok, detail = in_config(host)
    if plugin and ok:
        return True, f"through the plugin ({plugin}) and also its config ({detail})"
    if plugin:
        return True, f"through the plugin ({plugin})"
    return ok, detail


def _entry_in(data: object, section: str) -> object:
    servers = data.get(section) if isinstance(data, dict) else None
    return servers.get(SERVER_NAME) if isinstance(servers, dict) else None


def _described(entry: object, path: Path | str) -> tuple[bool, str]:
    """Our entry, as the command it runs; or why it isn't one Handoff can read."""
    if not isinstance(entry, dict):
        return False, f"not in {path}"
    if entry.get("enabled") is False:
        return False, f"turned off in {path}"
    command, args = entry.get("command"), entry.get("args", [])
    words = command if isinstance(command, list) else [command, *(args if isinstance(args, list) else [None])]
    if not words or not all(isinstance(w, str) for w in words):
        return False, f"its handoff entry in {path} isn't one Handoff can read"
    return True, " ".join(words)


def in_config(host: str) -> tuple[bool, str]:
    """Is Handoff in this app's own MCP config (not its plugins), and what does it point at? Read only."""
    if host == "claude-code":
        entry = claude_code_configured()
        return _described(entry, "~/.claude.json") if entry else (False, "not in ~/.claude.json (user scope)")
    if host in ("codex", "grok", "claude-desktop") or host in _JSON_HOSTS:
        if host in ("codex", "grok"):
            path, section = (codex_config_path() if host == "codex" else grok_config_path()), "mcp_servers"
        elif host == "claude-desktop":
            path, section = desktop_config_path(), "mcpServers"
        else:
            path, section = _JSON_HOSTS[host][0](), _JSON_HOSTS[host][1]
        try:
            text = path.read_text(encoding="utf-8")
            data = tomllib.loads(text) if path.suffix == ".toml" else _read_json(text, path, host)
        except FileNotFoundError:
            return False, f"no {path}"
        except (OSError, ValueError, SetupError) as exc:
            return False, f"can't read {path}: {exc}"
        return _described(_entry_in(data, section), path)
    raise ValueError(host)
