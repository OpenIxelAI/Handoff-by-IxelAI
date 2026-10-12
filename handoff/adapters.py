"""
The worker's other agents: every coding agent and model you have, not only Claude Code and Codex.

Handoff knows these, and offers each one once it finds it here:

- OpenCode (`opencode`), edit-only: its permissions are set for the run so it has only its file tools (read,
  edit, write, search), confined to the worktree, with no shell, web or subagents, whatever your own or the
  project's OpenCode config says. Its free models (opencode/…-free) cost nothing.
- Hermes Agent (`hermes`), file tools only: no terminal, browser, web or code execution. Its file tools can
  read files outside the worktree, so it runs once you've said so.
- Kimi Code (`kimi`), Cline (`cline`) and OpenClaw (`openclaw`): they run with their own permissions, which
  let them run commands as you, with the network. Each runs only once you've said so.
- A model on your own computer: Ollama, LM Studio, or any server with an OpenAI-style API. Handoff drives it
  itself (local_model.py), with four tools that only read and write files in the worktree; it runs no
  commands, and it costs nothing.

You say so, or add your own agents and model servers, in agents.toml in Handoff's folder (next to the approval
key: ~/.config/handoff on Linux, ~/Library/Application Support/Handoff on macOS, %APPDATA%\\Handoff on
Windows), or with `handoff agents enable NAME`. That file is yours: a project can't add an agent or say one
may run. For example:

    [agents.kimi]
    enabled = true                       # it runs with its own permissions: commands, as you

    [agents.ollama]
    model = "qwen3:4b-instruct"          # what it runs when an approval names no model

    [agents.studio]                      # a model server of your own
    url = "http://192.168.1.20:1234/v1"
    model = "qwen3-coder-30b"
    key_env = "STUDIO_KEY"               # only if the server wants a key

    [agents.myagent]                     # any command-line agent
    command = "myagent"
    args = ["run", "--quiet"]            # the task goes in on stdin, or where "{prompt}" is
    model_args = ["--model", "{model}"]
    effort_args = ["--effort", "{effort}"]
    efforts = ["low", "high"]
    env = ["MYAGENT_"]                   # what of your environment it gets: names, or prefixes ending in _
    enabled = true

What each of them did is still the worker's: its own git worktree and branch, the task as you approved it, no
secrets of other services in its environment, nothing committed that looks like a secret, and never a push.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from handoff import approvals
from handoff.proc import find_on_path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_NAME = "agents.toml"

# What an agent can reach, in words for people (`handoff agents`, the Board)
ACCESS = {
    "sandbox": "runs in Handoff's sandbox: the worktree only, commands with no network",
    "edit-only": "edits files in the worktree only: no commands, no web",
    "files": "uses file tools only, with no commands, but they can read files outside the worktree",
    "full": "runs with its own permissions: it can run commands as you, with the network",
}

# OpenCode's permissions for a run: its file tools in the worktree, nothing else. Set through
# OPENCODE_CONFIG_CONTENT, which wins over your config and the project's (and the project's isn't read at all)
OPENCODE_CONFIG = json.dumps({
    "share": "disabled", "autoupdate": False,
    "permission": {"edit": "allow", "bash": "deny", "webfetch": "deny", "websearch": "deny", "task": "deny",
                   "skill": "deny", "external_directory": "deny", "doom_loop": "deny"},
}, separators=(",", ":"))

# OpenCode 2 drops --pure and --variant, and its free models (OpenCode Zen) refuse a run whose tools were taken away
# ("OpenCode's free tier can only be used from within OpenCode"). So there every other tool is left on "ask": a
# non-interactive run can't ask, so OpenCode rejects each such call itself. Its tools have new names there: shell,
# subagent, and execute: a JavaScript runtime whose HTTP requests "ask" doesn't stop, so it's taken away (which the
# free models allow).
# --standalone gives the run a private server, which reads these settings, rather than your background one.
OPENCODE2_CONFIG = json.dumps({
    "share": "disabled", "autoupdate": False,
    "permission": {"edit": "allow", "bash": "ask", "shell": "ask", "execute": "deny", "webfetch": "ask",
                   "websearch": "ask", "task": "ask", "subagent": "ask", "skill": "ask", "external_directory": "ask",
                   "doom_loop": "ask"},
}, separators=(",", ":"))

# Their levels, in their own words, least first
OPENCODE_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")  # --variant: what the model offers
CLINE_EFFORTS = ("none", "low", "medium", "high", "xhigh")
OPENCLAW_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
LOCAL_EFFORTS = ("none", "low", "medium", "high")  # reasoning_effort; none turns a thinking model's thinking off

OLLAMA_URL = "http://127.0.0.1:11434/v1"
LMSTUDIO_URL = "http://127.0.0.1:1234/v1"

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RESERVED = ("claude", "codex", "human", "me", "nobody", "everyone")


class ConfigError(Exception):
    """agents.toml can't be used; the message says where."""


def config_path() -> Path:
    """agents.toml, next to the approval key (approvals.key_path), outside every project."""
    return approvals.key_path().with_name(CONFIG_NAME)


def presets() -> dict:
    """The agents Handoff knows, as AgentSpecs (they're found on PATH, or by their model server, when checked)."""
    from handoff.worker import AgentSpec
    note_free = "The worker ran {name} with only its file tools: no commands or tests were run."
    return {
        "opencode": AgentSpec(
            name="opencode", label="OpenCode", command="opencode",
            args=("run", "--format", "json", "--pure"),
            help_args=("run", "--help"), required_flags=("--format", "--pure", "--model"),
            env_prefixes=("OPENCODE_",), powers=_EDIT_POWERS,
            note=note_free.format(name="opencode"), model_flag="--model",
            effort_args=("--variant", "{effort}"), effort_levels=OPENCODE_EFFORTS,
            output="opencode", access="edit-only",
            env=(("OPENCODE_CONFIG_CONTENT", OPENCODE_CONFIG), ("OPENCODE_DISABLE_PROJECT_CONFIG", "1"),
                 ("OPENCODE_DISABLE_AUTOUPDATE", "1"), ("OPENCODE_DISABLE_SHARE", "1")),
            models_args=("models",), newer=AgentSpec(
                name="opencode", label="OpenCode", command="opencode",
                args=("run", "--standalone", "--format", "json"),  # (no --auto: what isn't allowed is rejected)
                help_args=("run", "--help"), required_flags=("--standalone", "--format", "--model"),
                env_prefixes=("OPENCODE_",), powers=_EDIT_POWERS,
                note=note_free.format(name="opencode"), model_flag="--model",
                output="opencode", access="edit-only",
                env=(("OPENCODE_CONFIG_CONTENT", OPENCODE2_CONFIG), ("OPENCODE_DISABLE_PROJECT_CONFIG", "1"),
                     ("OPENCODE_DISABLE_AUTOUPDATE", "1"), ("OPENCODE_DISABLE_SHARE", "1")),
                models_args=("models", "--standalone"))),
        "hermes": AgentSpec(
            name="hermes", label="Hermes Agent", command="hermes",
            # -z: one shot, only the reply on stdout; -t file: its file tools and nothing else; --ignore-rules: no
            # AGENTS.md, memory or skills injected from anywhere
            args=("-z", "{prompt}", "--usage-file", "{usage_file}", "-t", "file", "--ignore-rules"),
            help_args=("--help",), required_flags=("--oneshot", "--usage-file", "--toolsets", "--ignore-rules"),
            env_prefixes=("HERMES_",), powers=_EDIT_POWERS,
            note=note_free.format(name="hermes"), model_flag="-m",
            output="hermes", access="files", needs_ok=True),
        "kimi": AgentSpec(
            name="kimi", label="Kimi Code", command="kimi",
            args=("--prompt", "{prompt}", "--output-format", "text"),
            help_args=("--help",), required_flags=("--prompt", "--output-format", "--model"),
            env_prefixes=("KIMI_",), powers=_FULL_POWERS,
            note="The worker ran kimi with its own permissions.", model_flag="--model",
            access="full", needs_ok=True),
        "cline": AgentSpec(
            name="cline", label="Cline", command="cline",
            args=("--auto-approve", "true", "{prompt}"),
            help_args=("--help",), required_flags=("--auto-approve", "--thinking", "--model"),
            env_prefixes=("CLINE_",), powers=_FULL_POWERS,
            note="The worker ran cline with its own permissions.", model_flag="--model",
            effort_args=("--thinking", "{effort}"), effort_levels=CLINE_EFFORTS,
            access="full", needs_ok=True),
        "openclaw": AgentSpec(
            name="openclaw", label="OpenClaw", command="openclaw",
            # --local: its own agent here, not through its gateway, so nothing goes to a chat channel
            args=("agent", "--local", "--message-file", "{prompt_file}"),
            help_args=("agent", "--help"), required_flags=("--local", "--message-file", "--model"),
            env_prefixes=("OPENCLAW_",), powers=_FULL_POWERS,
            note="The worker ran openclaw with its own permissions.", model_flag="--model",
            effort_args=("--thinking", "{effort}"), effort_levels=OPENCLAW_EFFORTS,
            access="full", needs_ok=True, env=(("OPENCLAW_WORKSPACE_DIR", "{worktree}"),)),
        "ollama": _local("ollama", "Ollama", OLLAMA_URL),
        "lmstudio": _local("lmstudio", "LM Studio", LMSTUDIO_URL),
    }


_EDIT_POWERS = ("You can read and edit files in this folder with your file tools. You can't run commands, tests or "
                "builds, and you can't use the web, so don't try.")
_FULL_POWERS = ("You can read and edit files in this folder and run commands here, such as tests and builds. Work "
                "only in this folder, and don't push, publish or send anything anywhere.")
_LOCAL_POWERS = ("You have four tools: list_files, read_file, write_file and replace_in_file, for files in this "
                 "folder only. You can't run commands, tests or builds. Read what you need, make the change with "
                 "the tools, then reply.")

# A local server is "found" when its program is on PATH (it may just not be running yet)
_LOCAL_COMMANDS = {"ollama": "ollama", "lmstudio": "lms"}


def _local(name: str, label: str, url: str, model: str = "", key_env: str = "", custom: bool = False):
    from handoff.worker import AgentSpec
    return AgentSpec(
        name=name, label=label, command=_LOCAL_COMMANDS.get(name, ""), args=(), help_args=(), required_flags=(),
        env_prefixes=(), powers=_LOCAL_POWERS,
        note=f"The worker ran {name} (a model on your computer) with tools that only read and write files in the "
             "worktree: no commands or tests were run.",
        model_flag="model", effort_args=("{effort}",), effort_levels=LOCAL_EFFORTS,
        kind="local", output="local", access="edit-only", url=url, key_env=key_env, default_model=model,
        custom=custom)


def load_config(path: Path | None = None) -> dict:
    """agents.toml's [agents] table ({} if there's no file). ConfigError if it can't be read."""
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path} can't be read: {exc}") from exc
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        raise ConfigError(f"{path}: [agents] must be a table of agents, like [agents.kimi].")
    return agents


def _strings(value: object, what: str, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and "\0" not in v for v in value):
        raise ConfigError(f"agents.{name}.{what} must be a list of strings.")
    return tuple(value)


def _custom(name: str, entry: dict, base=None):
    """An agent from agents.toml: one Handoff knows (base), changed, or a new one."""
    from dataclasses import replace

    from handoff.worker import AgentSpec
    model = entry.get("model", "")
    if not isinstance(model, str) or (model and not approvals.check_models([model])):
        raise ConfigError(f"agents.{name}.model isn't a model name.")
    if "url" in entry or (base is not None and base.kind == "local"):
        url = entry.get("url", base.url if base else "")
        key_env = entry.get("key_env", "")
        if not isinstance(url, str) or not re.match(r"^https?://[^\s/]+(/\S*)?$", url):
            raise ConfigError(f"agents.{name}.url must be an http:// or https:// address, like {OLLAMA_URL}")
        if key_env and not (isinstance(key_env, str) and _ENV_NAME.match(key_env)):
            raise ConfigError(f"agents.{name}.key_env must be the name of an environment variable.")
        label = entry.get("label") or (base.label if base else name)
        return _local(name, str(label)[:40], url.rstrip("/"), model or (base.default_model if base else ""),
                      key_env, custom=base is None)
    if base is not None:
        return replace(base, default_model=model or base.default_model)
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ConfigError(f"agents.{name}.command must say which program runs it, like command = \"{name}\".")
    args = _strings(entry.get("args"), "args", name)
    model_args = _strings(entry.get("model_args"), "model_args", name)
    effort_args = _strings(entry.get("effort_args"), "effort_args", name)
    efforts = _strings(entry.get("efforts"), "efforts", name)
    if any(not re.match(r"^[a-z]{1,16}$", e) for e in efforts):
        raise ConfigError(f"agents.{name}.efforts must be lowercase words, like [\"low\", \"high\"].")
    if efforts and not any("{effort}" in a for a in effort_args):
        raise ConfigError(f"agents.{name}.effort_args must say where the level goes, like "
                          "[\"--effort\", \"{effort}\"].")
    if model_args and (len(model_args) != 2 or model_args[1] != "{model}"):
        raise ConfigError(f"agents.{name}.model_args must be a flag and \"{{model}}\", like "
                          "[\"--model\", \"{model}\"].")
    env = _strings(entry.get("env"), "env", name)
    if any(not _ENV_NAME.match(e) for e in env):
        raise ConfigError(f"agents.{name}.env must be names of environment variables, or prefixes ending in _.")
    return AgentSpec(
        name=name, label=str(entry.get("label") or name)[:40], command=command.strip(), args=args,
        help_args=(), required_flags=(), env_prefixes=env, powers=_FULL_POWERS,
        note=f"The worker ran {name} (an agent you added) with its own permissions.",
        model_flag=model_args[0] if model_args else "", effort_args=effort_args, effort_levels=efforts,
        access="full", needs_ok=True, default_model=model, custom=True)


def build(config: dict | None = None, on_path=None) -> tuple[dict, list[str]]:
    """The agents beyond claude and codex, by name, and what's wrong in agents.toml (shown, never fatal): the
    ones Handoff knows that are on PATH (a model server: whose program is, or that you named), and yours."""
    problems: list[str] = []
    on_path = on_path or find_on_path
    try:
        config = load_config() if config is None else config
    except ConfigError as exc:
        config, problems = {}, [str(exc)]
    known = presets()
    found = {}
    for name, spec in known.items():
        if name in config or (spec.command and on_path(spec.command)):
            found[name] = spec
    for name, entry in config.items():
        if not isinstance(entry, dict):
            problems.append(f"agents.{name} must be a table, like [agents.{name}].")
            continue
        if not _NAME.match(str(name)) or name in RESERVED:
            problems.append(f"agents.{name}: an agent's name is lowercase letters, digits, - and _ (and not "
                            f"{', '.join(RESERVED)}).")
            continue
        try:
            found[name] = _custom(name, entry, known.get(name))
        except ConfigError as exc:
            found.pop(name, None)
            problems.append(str(exc))
    return found, problems


def enabled(name: str, config: dict | None = None) -> bool:
    """Whether you've said this agent may run (enabled = true in agents.toml)."""
    try:
        config = load_config() if config is None else config
    except ConfigError:
        return False
    entry = config.get(name)
    return isinstance(entry, dict) and entry.get("enabled") is True


def set_enabled(name: str, on: bool, path: Path | None = None) -> Path:
    """Say an agent may run, or no longer may: `enabled` in its [agents.NAME] table, adding the table if it isn't
    there. The rest of the file stays as you wrote it."""
    if not _NAME.match(name) or name in RESERVED:
        raise ConfigError(f"{name} isn't an agent name Handoff can enable.")
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    load_config(path) if text else None  # (refuse to edit a file that doesn't parse)
    value = "true" if on else "false"
    lines = text.splitlines()
    header = re.compile(rf"^\s*\[\s*agents\s*\.\s*\"?{re.escape(name)}\"?\s*\]\s*(#.*)?$")
    start = next((n for n, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        if not on:
            return path
        lines += ([""] if lines and lines[-1].strip() else []) + [f"[agents.{name}]", f"enabled = {value}"]
    else:
        end = next((n for n in range(start + 1, len(lines)) if lines[n].lstrip().startswith("[")), len(lines))
        at = next((n for n in range(start + 1, end) if re.match(r"^\s*enabled\s*=", lines[n])), None)
        if at is None:
            lines.insert(start + 1, f"enabled = {value}")
        else:
            lines[at] = f"enabled = {value}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{CONFIG_NAME}.handoff-tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    load_config(path)
    return path


def prompt_on_command_line(spec) -> bool:
    return any("{prompt}" in a for a in spec.args)


def windows_shim(command: str) -> bool:
    """A .cmd or .bat (npm's shims on Windows) runs through cmd.exe, which reads %, ^ and & in its arguments:
    text an agent wrote must never be one of them there."""
    return sys.platform == "win32" and os.path.splitext(command)[1].lower() in (".cmd", ".bat")
