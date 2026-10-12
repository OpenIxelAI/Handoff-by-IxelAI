"""
The worker's other agents: every coding agent and model you have, not only Claude Code and Codex.

Handoff knows these, and offers each one once it finds it here:

- OpenCode (`opencode`), edit-only: its permissions are set for the run so it has only its file tools (read,
  edit, write, search), confined to the worktree, with no shell, web or subagents, whatever your own or the
  project's OpenCode config says. Its free models (opencode/…-free) cost nothing.
- Grok Build (`grok`), edit-only: only its file tools (read, edit, write, list, search), with no MCP servers, web
  search or subagents, inside its own OS sandbox (`--sandbox strict`: it writes only the worktree, /tmp and
  ~/.grok; checked on macOS with the real grok: writes to ~/.grok, ~/.grok/plugins and ~/.grok/hooks are refused).
  It uses your SuperGrok login, never a key, and runs once you've said so. It loads your own Grok
  plugins, hooks and MCP servers whatever its switches say, so before every run the worker reads `grok inspect
  --json` in the worktree (with the run's --cwd) and refuses the run, changing nothing, when any of them would
  run, or when what it says can't be read.
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
from handoff.sanitize import clean_line

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
GROK_EFFORTS = ("low", "medium", "high", "xhigh")  # --reasoning-effort

# Grok Build's file tools, and all it's left with (--tools). search_tool and use_tool are taken away too
# (--disallowed-tools): left on, they still reach your MCP servers, Handoff's own included
GROK_TOOLS = "read_file,search_replace,write,list_dir,grep"
GROK_DISALLOWED = "search_tool,use_tool"

# Grok Build's settings for a run, as environment variables. "0" turns off its memory, web fetch, subagents,
# telemetry and feedback, and the summaries, recaps, titles and suggestions it makes with extra model calls;
# GROK_DISABLE_AUTOUPDATER is the other way round ("1" stops its auto-updater)
_GROK_SWITCHES = ("GROK_MEMORY", "GROK_SUBAGENTS", "GROK_WEB_FETCH", "GROK_TELEMETRY_ENABLED",
                  "GROK_TELEMETRY_MIXPANEL_ENABLED", "GROK_TELEMETRY_TRACE_UPLOAD", "GROK_FEEDBACK_ENABLED",
                  "GROK_EXTERNAL_OTEL", "GROK_TURN_SUMMARY", "GROK_SESSION_RECAP", "GROK_TITLE_REFRESH",
                  "GROK_PROMPT_SUGGESTIONS")
GROK_ENV = (
    *((name, "0") for name in _GROK_SWITCHES),
    ("GROK_DISABLE_AUTOUPDATER", "1"),
    # Grok also loads Claude Code's and Cursor's hooks, MCP servers, rules, skills and agents: none of them here
    *((f"GROK_{app}_{what}_ENABLED", "0") for app in ("CLAUDE", "CURSOR")
      for what in ("HOOKS", "MCPS", "RULES", "SKILLS", "AGENTS")),
)

# Those switches don't cover Grok's own plugins and hooks (from ~/.grok, or the project): a plugin can run a hook
# that writes into the folder it works in, and --tools doesn't stop hooks. `grok inspect --json` only reads config
# and lists what it would load, so the worker runs it before every run (worker.py _check_inspect) and refuses the
# run when a plugin with hooks or a hook would run (inspect_report); MCP servers are named in a note instead.
GROK_INSPECT = ("inspect", "--json")
INSPECT_TIMEOUT = 60.0

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
        # --always-approve is what lets its edits happen with nobody there to approve them, and it's safe only
        # together with the three flags that confine it: --tools (its file tools and nothing else),
        # --disallowed-tools (without it search_tool and use_tool still reach your MCP servers, Handoff's own
        # included) and --sandbox strict (its OS sandbox: it reads only the working directory and system paths,
        # and is documented to write only there, /tmp and ~/.grok). Never run it with --always-approve and without
        # them. It's signed in with your SuperGrok plan, found through HOME: env_prefixes is empty, so none of your
        # GROK_* or XAI_* variables pass (an xAI API key, GROK_CONFIG and GROK_CONFIG_PATH among them).
        # The confinement is not complete, and this can't close it: the run uses your real HOME, so ~/.grok (its
        # login, settings, sessions, skills) and /tmp stay inside what the sandbox lets it write. A private HOME or
        # GROK_CONFIG would hide the SuperGrok login, and Handoff has no private-login copy for Grok as it has for
        # Claude and Codex (worker.py, the Login classes), since where Grok keeps its login isn't known here. What
        # was checked by hand (SECURITY.md) is that plugins/ and hooks/ under ~/.grok were refused on macOS; the
        # rest of ~/.grok was not, and neither was Linux or Windows. The note below says so to the person.
        # No models_args: how `grok models` prints its list isn't known, so it isn't parsed.
        # inspect_args: its own plugins, hooks and MCP servers are checked before every run (GROK_INSPECT).
        "grok": AgentSpec(
            name="grok", label="Grok Build", command="grok",
            args=("--output-format", "json", "--tools", GROK_TOOLS, "--disallowed-tools", GROK_DISALLOWED,
                  "--disable-web-search", "--no-subagents", "--sandbox", "strict", "--always-approve",
                  "--cwd", "{worktree}", "--prompt-file", "{prompt_file}"),
            help_args=("--help",),
            required_flags=("--output-format", "--tools", "--disallowed-tools", "--sandbox", "--always-approve",
                            "--disable-web-search", "--no-subagents", "--cwd", "--prompt-file"),
            env_prefixes=(), env=GROK_ENV, powers=_EDIT_POWERS,
            note="The worker ran grok edit-only: its file tools, inside its OS sandbox (--sandbox strict), which "
                 "reads the worktree and system paths but may still write to /tmp and your ~/.grok, so look there "
                 "if it matters. No commands or tests were run.",
            model_flag="-m", effort_args=("--reasoning-effort", "{effort}"), effort_levels=GROK_EFFORTS,
            output="grok", access="edit-only", needs_ok=True, inspect_args=GROK_INSPECT),
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


# ── What Grok Build would load (`grok inspect --json`) ───────────────────────

_INSPECT_MAX_CHARS = 1_000_000
_INSPECT_SHOWN = 8
_INSPECT_MCP_KEYS = ("mcpServers", "mcp_servers")  # the MCP servers' list, under either name
# What a plugin may name, in any case and with its hyphens, underscores and spaces left out, for hooks or MCP
# servers: hooks, hook, mcpServers, mcp_servers, mcp-servers, mcp
_HOOK_NAMES = ("hooks", "hook")
_MCP_NAMES = ("mcpservers", "mcpserver", "mcp")


def _spelled(name: object, names: tuple[str, ...]) -> bool:
    """Whether a name (a key, or an item of "provides") is one of `names`, however it's spelled."""
    return isinstance(name, str) and re.sub(r"[-_\s]", "", name.lower()) in names


def _inspect_entries(value: object, what: str) -> list[tuple[str, object]]:
    """The items of one list in inspect's JSON, as (name, item). ValueError if it isn't a list."""
    if not isinstance(value, list):
        raise ValueError(f"no {what} list" if value is None else f"its {what} aren't a list")
    entries = []
    for n, item in enumerate(value, 1):
        name = next((item[k] for k in ("name", "id") if isinstance(item, dict) and isinstance(item.get(k), str)),
                    item if isinstance(item, str) else "")
        entries.append((clean_line(name)[:60] or f"#{n}", item))
    return entries


def _inspect_brings(plugin: dict, name: str) -> tuple[list[str], list[str]]:
    """What an enabled plugin brings that would run in a run, as (hooks, MCP servers), each as it spells them.
    Counted: a key of the plugin itself (hooks, mcpServers, mcp ...) with something in it, and what its "provides"
    names, as an object's key with something in it, or as a list's (or a lone string's) item. ValueError for a
    "provides" of another shape, or no "provides" and none of those keys: it can't be told what the plugin brings."""
    own = [key for key in plugin if _spelled(key, _HOOK_NAMES + _MCP_NAMES)]
    hooks = [key for key in own if _spelled(key, _HOOK_NAMES) and plugin[key]]
    mcp = [key for key in own if _spelled(key, _MCP_NAMES) and plugin[key]]
    provides = plugin.get("provides")
    if isinstance(provides, dict):  # {"hooks": 2, "mcpServers": [], "skills": 1}: a name whose value isn't empty
        hooks += [key for key, item in provides.items() if _spelled(key, _HOOK_NAMES) and item]
        mcp += [key for key, item in provides.items() if _spelled(key, _MCP_NAMES) and item]
    elif isinstance(provides, str):
        hooks += [provides] if _spelled(provides, _HOOK_NAMES) else []
        mcp += [provides] if _spelled(provides, _MCP_NAMES) else []
    elif isinstance(provides, list) and all(isinstance(item, str) for item in provides):
        hooks += [item for item in provides if _spelled(item, _HOOK_NAMES)]
        mcp += [item for item in provides if _spelled(item, _MCP_NAMES)]
    elif provides is not None or not own:
        raise ValueError(f'the "provides" of plugin "{name}" is neither a list nor an object')
    return (list(dict.fromkeys(clean_line(item)[:40] for item in hooks)),
            list(dict.fromkeys(clean_line(item)[:40] for item in mcp)))


def _hook_label(name: str, hook: dict) -> str:
    """How a hook is shown: its own name or id if it has one, else what it runs (its event and target, which
    the real hooks have, since they carry no name). Falls back to `name` when there's nothing else."""
    if any(isinstance(hook.get(key), str) for key in ("name", "id")):
        return name
    parts = [hook[key] for key in ("event", "target") if isinstance(hook.get(key), str) and hook[key]]
    return clean_line(" ".join(parts))[:100] or name


def inspect_findings(text: str) -> list[str]:
    """What `grok inspect --json` lists that would stop a Board run, in words ([] when none would): an enabled
    plugin that provides hooks, and a hook that counts as enabled (its compatibilityStatus is "enabled", or there
    is none, which counts as enabled; only a status that is other text is left out). MCP servers are not in it
    (inspect_report names them). See inspect_report for how the text is read."""
    return inspect_report(text)[0]


def inspect_report(text: str) -> tuple[list[str], list[str]]:
    """Read `grok inspect --json`: (refusals, MCP servers). The refusals are what would run and stop a Board run
    (an enabled plugin that provides hooks, and a hook that counts as enabled, as inspect_findings says). The MCP
    servers are their names: the top-level ones, then each enabled plugin that provides MCP servers but no hooks.
    Those don't stop a run (the run's --tools and --disallowed-tools keep their tools from the model), so they're
    named in a note. Fails closed: the text isn't trusted, so anything that isn't the JSON object with all of
    `plugins`, `hooks` and `mcpServers` (or `mcp_servers`) as lists of the shape this knows, or that carries an
    error, is a ValueError that says why, never a pass: a list that's missing is one it didn't say, not an empty one."""
    if len(text) > _INSPECT_MAX_CHARS:
        raise ValueError("its answer is too long")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("it didn't print JSON") from None
    if not isinstance(data, dict):
        raise ValueError("it didn't print a JSON object")
    for key in ("error", "errors"):
        if data.get(key):
            raise ValueError(f"it reported an error: {clean_line(str(data[key]))[:100]}")
    mcp_keys = [key for key in _INSPECT_MCP_KEYS if key in data]
    for key in ("plugins", "hooks"):
        if key not in data:
            raise ValueError(f"no {key} list")
    if not mcp_keys:
        raise ValueError("no mcpServers list")
    refusals: list[str] = []
    plugin_mcp: list[str] = []  # the enabled plugins that provide MCP servers but no hooks, after the top-level ones
    for name, plugin in _inspect_entries(data["plugins"], "plugins"):
        if not isinstance(plugin, dict) or not isinstance(plugin.get("enabled"), bool):
            raise ValueError(f'plugin "{name}" has no true or false "enabled"')
        hooks, servers = _inspect_brings(plugin, name) if plugin["enabled"] else ([], [])
        if hooks:
            refusals.append(f'plugin "{name}" (enabled, provides {" and ".join(hooks)})')
        elif servers:
            plugin_mcp.append(f"{name} (plugin)")
    for name, hook in _inspect_entries(data["hooks"], "hooks"):
        if not isinstance(hook, dict):
            raise ValueError(f'hook "{name}" is not an object')
        status = hook.get("compatibilityStatus", "enabled")  # no status at all: it runs, so it counts as enabled
        if not isinstance(status, str):
            raise ValueError(f'hook "{name}" has a "compatibilityStatus" that is not text')
        if status.strip().lower() == "enabled":
            refusals.append(f'hook "{_hook_label(name, hook)}" (enabled)')
    servers = [name for key in mcp_keys for name, _ in _inspect_entries(data[key], "MCP servers")]
    return refusals, list(dict.fromkeys(servers)) + plugin_mcp


def inspect_note(spec, names: list[str]) -> str:
    """The handback's note on the MCP servers a run loaded: named, and kept from the model by --disallowed-tools."""
    if not names:
        return ""
    more = len(names) - _INSPECT_SHOWN
    listed = ", ".join(names[:_INSPECT_SHOWN]) + (f", and {more} more" if more > 0 else "")
    return (f"{spec.label or spec.name} loaded your MCP servers {listed}; their tools were kept from the model with "
            "--disallowed-tools.")


def _inspect_command(spec) -> str:
    return f"`{spec.command} {' '.join(spec.inspect_args)}`"


def inspect_refusal(spec, findings: list[str]) -> str:
    """Why a run was refused: what would run, and how to turn it off for Board runs."""
    from handoff import hosts
    more = len(findings) - _INSPECT_SHOWN
    listed = "; ".join(findings[:_INSPECT_SHOWN]) + (f"; and {more} more" if more > 0 else "")
    plugins: list[str] = []
    for finding in findings:
        match = re.match(r'plugin "(.*)" \(enabled', finding)
        if match and match.group(1) not in plugins:
            plugins.append(match.group(1))
    commands = "; ".join(f"`{spec.command} plugin disable {name}`" for name in plugins[:_INSPECT_SHOWN])
    here = f"Here: {commands}. " if commands else ""
    return (f"{_inspect_command(spec)} says {spec.label or spec.name} would load: {listed}. Handoff's switches don't "
            "turn these off, so the run was refused before it started, and nothing was changed. To run it on the "
            f"Board, turn them off: `{spec.command} plugin disable NAME` for a plugin (a hook from a plugin goes "
            "with it), or remove the hook where it's set: your Grok config "
            f"({hosts.grok_config_path()}) or the project's own Grok config, whichever lists it when you run that "
            f"command in the project. {here}Hooks run code, so they are refused; the run's --tools and "
            "--disallowed-tools keep MCP tools away, so MCP servers alone don't stop a run.")


def inspect_unreadable(spec, why: str) -> str:
    """Why a run was refused when inspect couldn't be run or read."""
    return (f"{_inspect_command(spec)} {why}, so Handoff can't tell which of your Grok plugins, hooks or MCP "
            "servers would run, and the run was refused before it started. Nothing was changed. Run that command "
            "yourself in the project to see what it says.")


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
