"""
`handoff worker --agent claude|codex`: run an agent headlessly on tasks the person approved.

Security model (plan §8.8; tests/test_worker_live.py and tests/test_worker_codex_live.py
attack it with the real CLIs)
- Per-task approval: only the person can approve a task (`handoff approve T-7
  --worker claude`), for one run, and only while the task is as they approved
  it. The agent gets the task as it was at approval; nothing added later is sent.
- Its own git worktree and branch: .handoff/worktrees/T-7 on handoff/T-7, made
  from HEAD. The worker commits the result there and never pushes, and it
  won't commit anything that looks like a secret.
- Both agents run commands inside two sandboxes. Bubblewrap (sandbox.py) shows
  the agent nothing of yours but the worktree; inside it, the agent's own
  sandbox (Codex's `workspace-write`, Claude Code's `sandbox` setting) takes
  the network away from its commands and keeps their writes in the worktree.
  Its login is a private copy its commands can't read.
- Claude Code also runs in `--restricted` mode: no web, no MCP servers, no
  hooks, no user or repository settings, file tools confined to the worktree.
  Where the sandbox can't run, Claude falls back to edit-only (Read, Edit,
  Write, Glob and Grep, no shell) and the handback says so; Codex refuses.
- A worktree with links pointing outside it is refused before the agent starts.
- No secrets in its environment: only what the agent needs to run and sign in.
- Loop guard: everything the worker starts gets HANDOFF_DEPTH one higher, and a
  worker won't start with it set.
- Agent-written text goes in on stdin; the command line is fixed.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from handoff import approvals, gitwork, sandbox, usage
from handoff.board import HUMAN, Board, BoardError, Event, Task, run_kind, run_models, safe_name
from handoff.describe import describe
from handoff.fence import Reply, numbered, quote_event
from handoff.ixel import DEPTH_VAR, depth
from handoff.proc import Stopped, find_on_path, run_tree, stopping
from handoff.sanitize import clean_line, clean_text, find_secret
from handoff.timefmt import stamp

DEFAULT_TIMEOUT_MIN = 30

MAX_SECTION_BYTES = 6_000  # three of these, plus a note, stay under the board's 20 KB

CLAUDE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
CLAUDE_SANDBOX_TOOLS = CLAUDE_TOOLS + ("Bash",)
PLANNER_TOOLS = ("Read", "Glob", "Grep")  # a chain's planner reads; the models it gives the steps to change files
MAX_STEPS = 6

WORKER_RULES = ("You're running headlessly for Handoff's worker, with nobody watching. You can read and edit "
                "files in the current folder only, and you can't run commands or use the network.")
SANDBOX_RULES = ("You're running headlessly for Handoff's worker, with nobody watching. You can read and edit "
                 "files in the current folder and run commands here; they have no network, and nothing outside "
                 "this folder is visible.")
SANDBOX_POWERS = ("You can read and edit files in this folder and run commands here, such as tests and builds. There's "
                  "no network, and nothing outside this folder is visible, so tools or packages that aren't already "
                  "installed on the system can't be added. Git commands don't work here either: the repository "
                  "this folder belongs to is outside it.")

BRIEF = ("You're {agent}, working on task {ref} through Handoff's worker. The person approved this task for "
         "you to do on your own.\n"
         "\n"
         "You're in a git worktree of their project (the current folder), on the branch {branch}. {powers} "
         "Make the changes the task asks for, carefully, then stop. The worker commits what you changed on "
         "{branch}; it never pushes.\n"
         "\n"
         "When you're done, end your reply with these three sections:\n"
         "DONE: what you changed, file by file\n"
         "LEFT: what's still to do, including anything you couldn't do or check here (or \"nothing\")\n"
         "VERIFY: how the person should check it, such as the test commands to run")

PLAN_BRIEF = ("You're {planner}, planning task {ref} for Handoff's worker. The person approved this task to be done "
              "as a chain: you plan, and other models do the steps, one after another, in this folder (a git "
              "worktree of their project). Don't change any files yourself. Read what you need, then split the "
              "task into 1 to {max_steps} steps, in order, that together finish it.\n"
              "\n"
              "Give each step to one of these models, biggest first: {tiers}. Give a step to the smallest model "
              "that will do it well: the bigger ones for design, tricky logic and anything that needs judgment, "
              "the smaller ones for clear, mechanical work such as tests, renames, docs and small edits. Fewer "
              "steps are better: don't split what one model can do in one go. Write each step's instructions so "
              "its model can do it without asking: which files, what to change, and how to check it. Those "
              "models can {powers_short}.\n"
              "\n"
              "End your reply with the plan as JSON in a ```json block, in exactly this shape:\n"
              '{{"steps": [{{"model": "one of the models above", "title": "a few words", '
              '"instructions": "what to do"}}]}}')

STEP_BRIEF = ("\n\n## Your step\n\n"
              "{planner} planned this task as {count} steps, each done by a model in this folder in turn. Do only "
              "step {number}; the steps before it are already done here, and the ones after go to other models. "
              "Write DONE, LEFT and VERIFY about your step.")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    command: str
    args: tuple[str, ...]            # placeholders (AGENT_HOME, SETTINGS) are filled in by its login
    help_args: tuple[str, ...]       # `<command> <help_args>` must list every required flag
    required_flags: tuple[str, ...]
    env_prefixes: tuple[str, ...]    # environment the agent needs to run and sign in
    powers: str                      # what the brief tells the agent it can do
    note: str                        # what the handback tells the person about how it ran
    sandboxed: bool = False          # runs inside the OS sandbox, with a private login
    login: type | None = None        # the private login class, for a sandboxed agent
    needs: tuple[str, ...] = ()      # other programs its own sandbox needs inside ours
    fallback: "AgentSpec | None" = None  # what runs where the sandbox can't; None: refuse
    model_flag: str = ""             # how it's told which model to use ("" : it can't be)
    reports_usage: bool = False      # its output is Claude Code's JSON result, with what it used


_CLAUDE_FLAGS = ("--restricted", "--tools", "--strict-mcp-config", "--permission-mode", "--no-session-persistence",
                 "--settings")

CLAUDE_EDIT_ONLY = AgentSpec(
    name="claude", command="claude",
    args=("-p", "--restricted", "--tools", ",".join(CLAUDE_TOOLS), "--permission-mode", "acceptEdits",
          "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
          "--settings", '{"disableAllHooks": true}', "--append-system-prompt", WORKER_RULES),
    help_args=("--help",),
    required_flags=_CLAUDE_FLAGS,
    env_prefixes=("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_DIR"),
    powers=("You can read and edit files in this folder and nowhere else. You can't run commands, tests or "
            "builds, and you can't use the network, so don't try."),
    note="The worker ran claude edit-only: no commands or tests were run.",
    model_flag="--model", reports_usage=True,
)

# What any agent may see of the environment: enough to run, find its login, and reach its API
# through a proxy. Everything else (tokens for other services, cloud keys…) stays out.
_BASE_ENV = {name.upper() for name in (
    "PATH", "HOME", "USER", "USERNAME", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE")}
_CODEX_KEYS = ("CODEX_API_KEY", "OPENAI_API_KEY")
_CLAUDE_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
# In the sandbox every process can read every other's environment (`env`, /proc/*/environ), so a
# sandboxed agent gets no variable whose name says it holds a credential. An API key it needs goes
# into its private login file instead, which its commands can't read.
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE", "SESSION")


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(word in upper for word in _SECRET_WORDS)

Runner = Callable[[list[str], str, dict, float, str], "subprocess.CompletedProcess[str]"]


class WorkerUnavailable(Exception):
    """The worker can't run here; the message says why and what to do."""


@dataclass
class RunResult:
    task: Task
    ok: bool
    message: str
    branch: str | None = None
    commit: str | None = None
    waiting: str = ""  # why it didn't start (a missing login, say); its approval is kept for later
    used: dict | None = None  # what it used, model by model (usage.check), where the agent said


class _Withheld(Exception):
    """The changes look like they hold a secret, so nothing was committed; the message says where."""


def _token_note(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    return (f"Check {', '.join(tokens[:5])}{' …' if len(tokens) > 5 else ''} before you merge: "
            f"{'it looks like it holds' if len(tokens) == 1 else 'they look like they hold'} a JSON Web Token "
            "(eyJ…). That's often a sample in a test, but make sure it isn't a real one.")


def agent_env(spec: AgentSpec, source: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (os.environ if source is None else source).items()
           if k.upper() in _BASE_ENV or k.upper().startswith("LC_") or k.startswith(spec.env_prefixes)}
    env[DEPTH_VAR] = str(depth() + 1)
    return env


def _run(command: list[str], stdin: str, env: dict, timeout: float, cwd: str) -> "subprocess.CompletedProcess[str]":
    return run_tree(command, stdin, env, timeout, cwd=cwd)


def build_prompt(spec: AgentSpec, task: Task, history: list[Event], branch: str, target: dict | None = None) -> str:
    """The brief, then the task and its history as they were at approval, framed and fenced as untrusted.
    `target`: the commits it starts from (a pull request), as the approval sealed them."""
    brief = BRIEF.format(agent=spec.name, ref=task.ref, branch=branch, powers=spec.powers)
    return brief + "\n\n" + task_text(task, history, target)


def plan_prompt(spec: AgentSpec, task: Task, history: list[Event], models: list[str],
                target: dict | None = None) -> str:
    """What a chain's planner (models[0]) is given: how to plan for the others, then the task."""
    powers = "run commands and tests here" if spec.sandboxed else "only read and edit files (no commands or tests)"
    brief = PLAN_BRIEF.format(planner=models[0], ref=task.ref, max_steps=MAX_STEPS, tiers=", ".join(models[1:]),
                              powers_short=powers)
    return brief + "\n\n" + task_text(task, history, target)


def step_prompt(spec: AgentSpec, task: Task, history: list[Event], branch: str, target: dict | None,
                planner: str, steps: list["Step"], number: int) -> str:
    """What the model for step `number` (from 1) is given: the brief and the task, the whole plan, and its step.
    The plan is a model's words, so it's fenced like the task."""
    reply = Reply()
    for n, step in enumerate(steps, 1):
        reply.quote(f"step {n} of {len(steps)}, for {step.model}: {step.title}", step.instructions)
    brief = STEP_BRIEF.format(planner=planner, count=len(steps), number=number)
    return build_prompt(spec, task, history, branch, target) + brief + "\n\n" + reply.render()


def task_text(task: Task, history: list[Event], target: dict | None = None) -> str:
    """The task and its history as they were at approval, fenced as untrusted."""
    reply = Reply()
    reply.add(f"# {task.ref}", "")
    if target:
        reply.add(f"Your branch starts from commit {target['head']}, the last commit of what the person approved "
                  "this for you to work on:", "")
        reply.quote("what it's for", target["label"])
    reply.quote("title", task.title)
    if task.body:
        reply.quote("body", task.body)
    if task.acceptance:
        reply.quote("acceptance checks", numbered(task.acceptance))
    if history:
        reply.add("", "## History", "")
        for n, event in enumerate(history, 1):
            reply.add(f"{n}. {stamp(event.at)} · {event.actor} {describe(event)}")
            quote_event(reply, event)
    return reply.render()


@dataclass(frozen=True)
class Step:
    model: str
    title: str
    instructions: str


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_plan(text: str, models: list[str]) -> list[Step] | None:
    """The steps a planner wrote (the last JSON block in its reply), each for one of `models`. None if there's no
    plan it could use. A step for a model that isn't one of them goes to the first (the biggest)."""
    for raw in reversed(_JSON_BLOCK.findall(text or "")):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        found = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(found, list) or not found:
            continue
        steps = []
        for item in found[:MAX_STEPS]:
            if not isinstance(item, dict) or not isinstance(item.get("instructions"), str):
                continue
            instructions = clean_text(item["instructions"]).strip()
            if not instructions:
                continue
            model = item.get("model") if item.get("model") in models else models[0]
            title = clean_line(item.get("title") if isinstance(item.get("title"), str) else "")[:200] or "a step"
            steps.append(Step(model, title, instructions[:MAX_SECTION_BYTES]))
        if steps:
            return steps
    return None


def _clip(text: str) -> str:
    """Sanitized, secret-free, and at most MAX_SECTION_BYTES, so the three sections fit one handoff."""
    text = clean_text(text)
    if find_secret(text):
        return "[withheld: it looked like it contained a secret]"
    data = text.encode("utf-8")
    return text if len(data) <= MAX_SECTION_BYTES else data[:MAX_SECTION_BYTES].decode("utf-8", "ignore") + " [… cut]"


def _after_header(line: str) -> str:
    """What follows "DONE:" on its line, as the agent wrote it (CODEX_WAS_HERE.txt keeps its underscores),
    without the "**" that closes a bold header."""
    rest = line[line.index(":") + 1:]
    marks = len(rest) - len(rest.lstrip("*_"))
    return rest[marks:] if marks and (marks == len(rest) or rest[marks].isspace()) else rest


def parse_reply(text: str) -> tuple[str, str, str]:
    """DONE / LEFT / VERIFY from the agent's last message; all of it as DONE if it didn't use them."""
    sections: dict[str, list[str]] = {"DONE": [], "LEFT": [], "VERIFY": []}
    current = None
    for line in clean_text(text).splitlines():
        stripped = line.strip().lstrip("#- ")
        head = stripped.replace("*", "").replace("_", "")  # "**DONE:**" is DONE: too
        key = next((k for k in sections if head[:len(k) + 1].upper() == k + ":"), None)
        if key:
            current = key
            line = _after_header(stripped)
        if current:
            sections[current].append(line)
    done, left, verify = ("\n".join(sections[k]).strip() for k in ("DONE", "LEFT", "VERIFY"))
    if not done:
        done = clean_text(text) or "(the agent didn't say what it did)"
    return _clip(done), _clip(left), _clip(verify)


def _base_hint(events: list[Event]) -> str | None:
    """The branch's starting commit from an earlier run, if it looks like one (it goes to git)."""
    for event in reversed(events):
        base = event.data.get("base") if event.kind == "worker" else None
        if isinstance(base, str) and len(base) in (40, 64) and all(c in "0123456789abcdef" for c in base):
            return base
    return None


# ── The agents' private logins ───────────────────────────────────────────────

def codex_home() -> Path:
    custom = os.environ.get("CODEX_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".codex"


def claude_home() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(custom).expanduser() if custom else Path.home() / ".claude"


def _write_private(path: Path, content: bytes) -> None:
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(content)


class PrivateLogin:
    """A private home for one run holding only a copy of the agent's login.

    The agent's real home also holds your other sessions' transcripts and your
    config, and the agent sees none of it. If the agent refreshes its login
    during the run, the new tokens go back to your real login file, but only
    when it is still what it was before the run and the account is the same.
    """
    folder = ""          # where the agent looks for its home inside the sandbox, under HOME
    home_var = ""        # the variable that points the agent at it
    login_file = ""      # the login file, in the real home and the private one
    changed_note = ""    # for the person, when a refreshed login isn't copied back

    def __init__(self, env: dict[str, str]):
        self.real = self.real_home() / self.login_file
        self.original: bytes | None = None
        self.env = env
        self.home = Path(tempfile.mkdtemp(prefix=f"handoff-{self.folder.strip('.')}-"))  # 0700

    @staticmethod
    def real_home() -> Path:
        raise NotImplementedError

    @staticmethod
    def identity(data: object) -> tuple | None:
        """Which account a login belongs to, or None if it isn't a login Handoff understands."""
        raise NotImplementedError

    def prepare(self) -> None:
        raise NotImplementedError

    @classmethod
    def missing(cls, env: dict[str, str]) -> str | None:
        """Why there's no login to pass to the agent, if there isn't one. Only looks: nothing is copied."""
        raise NotImplementedError

    def values(self, inside: Path) -> dict[str, str]:
        """What fills the placeholders in the agent's arguments."""
        return {"AGENT_HOME": json.dumps(str(inside))}  # a quoted, escaped TOML (and JSON) string

    def _copy_real(self) -> bool:
        """Copy the real login file into the private home. False if there isn't one."""
        try:
            self.original = self.real.read_bytes()
        except FileNotFoundError:
            return False
        _write_private(self.home / self.login_file, self.original)
        return True

    def finish(self) -> str | None:
        """Copy a refreshed login back. Returns a note for the person if it didn't."""
        copy = self.home / self.login_file
        if self.original is None or not copy.exists():
            return None
        new = copy.read_bytes()
        if new == self.original:
            return None
        try:
            before, after = self.identity(json.loads(self.original)), self.identity(json.loads(new))
            unchanged = self.real.read_bytes() == self.original  # you didn't sign in again meanwhile
        except (OSError, ValueError):
            before, after, unchanged = None, None, False
        if before is None or after != before or not unchanged:
            return self.changed_note
        tmp = self.real.with_name(f".{self.login_file.lstrip('.')}.handoff-tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(new)
        os.replace(tmp, self.real)
        return None

    def cleanup(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)


class CodexLogin(PrivateLogin):
    """Codex's login (auth.json), or a login file made from OPENAI_API_KEY."""
    folder, home_var, login_file = ".codex", "CODEX_HOME", "auth.json"
    changed_note = ("Codex's login changed during the run in a way Handoff didn't copy back to your own; if Codex "
                    "asks you to sign in again, run `codex login`.")

    @staticmethod
    def real_home() -> Path:
        return codex_home()

    @staticmethod
    def identity(data: object) -> tuple | None:
        if not isinstance(data, dict):
            return None
        tokens = data.get("tokens")
        account = tokens.get("account_id") if isinstance(tokens, dict) else None
        key = data.get("OPENAI_API_KEY")
        return (account, key) if (account or key) else None

    @classmethod
    def missing(cls, env: dict[str, str]) -> str | None:
        real = cls.real_home() / cls.login_file
        if any(env.get(k) for k in _CODEX_KEYS) or real.is_file():
            return None
        return (f"Codex isn't signed in with a login Handoff can pass to it: there's no {real}. Run "
                "`codex login`; if your login is kept in the system keyring, set cli_auth_credentials_store = "
                '"file" in ~/.codex/config.toml first.')

    def prepare(self) -> None:
        if self._copy_real():
            return
        key = next((self.env[k] for k in _CODEX_KEYS if self.env.get(k)), None)
        if key is None:
            raise WorkerUnavailable(self.missing(self.env) or "Codex isn't signed in.")
        _write_private(self.home / self.login_file, json.dumps({"OPENAI_API_KEY": key}).encode("utf-8"))


CLAUDE_KEY_FILE = ".handoff-api-key"


class ClaudeLogin(PrivateLogin):
    """Claude Code's login: your signed-in login (.credentials.json), a `claude setup-token` token, or an API key.

    The login goes in the private home in Claude Code's own format; an API key
    goes in a file there that Claude reads with its `apiKeyHelper` setting. Its
    commands can't read the private home (the sandbox settings deny it), and
    no credential stays in its environment.
    """
    folder, home_var, login_file = ".claude", "CLAUDE_CONFIG_DIR", ".credentials.json"
    changed_note = ("Claude Code's login changed during the run in a way Handoff didn't copy back to your own; if "
                    "Claude asks you to sign in again, run `claude` and /login.")

    def __init__(self, env: dict[str, str]):
        super().__init__(env)
        self.api_key = False

    @staticmethod
    def real_home() -> Path:
        return claude_home()

    @staticmethod
    def identity(data: object) -> tuple | None:
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        return ("claude.ai",) if isinstance(oauth, dict) and oauth.get("refreshToken") else None

    def prepare(self) -> None:
        # the order Claude Code itself uses: a key or token in the environment wins over the saved login
        key = next((self.env[k] for k in _CLAUDE_KEYS if self.env.get(k)), None)
        token = self.env.get("CLAUDE_CODE_OAUTH_TOKEN")
        if key:
            _write_private(self.home / CLAUDE_KEY_FILE, key.encode("utf-8"))
            self.api_key = True
        elif token:  # a long-lived `claude setup-token` token, in the form of a login that never refreshes
            login = {"claudeAiOauth": {"accessToken": token, "refreshToken": None, "expiresAt": None,
                                       "scopes": ["user:inference"]}}
            _write_private(self.home / self.login_file, json.dumps(login).encode("utf-8"))
        elif not self._copy_real():
            raise WorkerUnavailable(self.missing(self.env) or "Claude Code isn't signed in.")

    @classmethod
    def missing(cls, env: dict[str, str]) -> str | None:
        real = cls.real_home() / cls.login_file
        if any(env.get(k) for k in (*_CLAUDE_KEYS, "CLAUDE_CODE_OAUTH_TOKEN")) or real.is_file():
            return None
        return (f"Claude Code isn't signed in with a login Handoff can pass to it: there's no {real}. Sign in "
                "with `claude` (then /login), or set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.")

    def values(self, inside: Path) -> dict[str, str]:
        return {**super().values(inside), "SETTINGS": claude_sandbox_settings(inside, self.api_key)}


def claude_sandbox_settings(inside: Path, api_key: bool = False) -> str:
    """Claude Code's settings for a run inside Handoff's sandbox (passed with --settings, which --restricted keeps)."""
    settings: dict = {
        "disableAllHooks": True,
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,          # never run a command without it
            "allowUnsandboxedCommands": False,  # and no way for the agent to ask for one
            "autoAllowBashIfSandboxed": True,
            # Handoff's sandbox protects parts of its /proc, so Claude's can't mount a fresh one inside it;
            # its commands see Handoff's instead, and can't look into the processes there (the attack test
            # reads their environment, open files, memory and root folder, and is refused)
            "enableWeakerNestedSandbox": True,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "filesystem": {"denyRead": [str(inside)]},
        },
    }
    if api_key:
        settings["apiKeyHelper"] = f"cat {shlex.quote(str(inside / CLAUDE_KEY_FILE))}"
    return json.dumps(settings)


CLAUDE = AgentSpec(
    name="claude", command="claude",
    # --allowedTools Bash: every command runs, and only runs, inside the sandbox, rather than some being
    # refused by Claude Code's own guesses about what a command might do
    args=("-p", "--restricted", "--tools", ",".join(CLAUDE_SANDBOX_TOOLS), "--allowedTools", "Bash",
          "--permission-mode", "acceptEdits", "--strict-mcp-config", "--no-session-persistence",
          "--output-format", "json", "--settings", "SETTINGS", "--append-system-prompt", SANDBOX_RULES),
    help_args=("--help",),
    required_flags=_CLAUDE_FLAGS + ("--allowedTools",),
    env_prefixes=CLAUDE_EDIT_ONLY.env_prefixes,
    powers=SANDBOX_POWERS,
    note=("The worker ran claude in a sandbox: it could run commands in the worktree, with no network and nothing "
          "else of yours visible. Dependencies installed only in your home or your own checkout weren't there."),
    sandboxed=True, login=ClaudeLogin, needs=("socat",), fallback=CLAUDE_EDIT_ONLY,
    model_flag="--model", reports_usage=True,
)

CODEX = AgentSpec(
    name="codex", command="codex",
    # --ignore-user-config: none of your MCP servers, notify hooks or profiles. The permission
    # profile is Codex's workspace-write sandbox (commands write only here, no network) with the
    # private home, where its login is, unreadable to the commands it runs.
    args=("exec", "--skip-git-repo-check", "--ephemeral", "--color", "never", "--ignore-user-config",
          "--ignore-rules", "--disable", "view_image", "--disable", "multi_agent", "--disable", "goals",
          "-c", 'web_search="disabled"', "-c", 'cli_auth_credentials_store="file"',
          "-c", 'default_permissions="handoff"', "-c", 'permissions.handoff.extends=":workspace"',
          "-c", 'permissions.handoff.filesystem={AGENT_HOME="none"}', "-"),
    help_args=("exec", "--help"),
    required_flags=("--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--disable"),
    env_prefixes=("OPENAI_", "CODEX_"),
    powers=SANDBOX_POWERS,
    note=("The worker ran codex in a sandbox: it could run commands in the worktree, with no network and nothing "
          "else of yours visible. Dependencies installed only in your home or your own checkout weren't there."),
    sandboxed=True, login=CodexLogin, model_flag="--model",
)

AGENTS = {"claude": CLAUDE, "codex": CODEX}
CHAIN_AGENTS = ("claude",)  # the agents a chain of models can run on (its planner needs read-only tools)


def _read_only(args: list[str]) -> list[str]:
    """Claude Code's arguments with only the planner's tools: it reads, and runs nothing."""
    out, skip = [], False
    for n, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--allowedTools":
            skip = True
            continue
        out.append(",".join(PLANNER_TOOLS) if n and args[n - 1] == "--tools" else arg)
    return out


def _home_relative(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path(os.path.realpath(Path.home()))).as_posix()
    except ValueError:
        return str(path)


def duration(seconds: float) -> str:
    """A run's time limit as people read it: "30 minutes", "1.5 minutes", "3 seconds"."""
    if seconds < 60:
        n, unit = round(seconds, 1), "second"
    else:
        n, unit = round(seconds / 60, 1), "minute"
    return f"{n:g} {unit}{'' if n == 1 else 's'}"


# ── The worker ───────────────────────────────────────────────────────────────

class Worker:
    def __init__(self, board: Board, root: Path, agent: str, runner: Runner = _run,
                 timeout_sec: float = DEFAULT_TIMEOUT_MIN * 60, command: str | None = None, sandboxed: bool = True,
                 share: list[str] | tuple[str, ...] = ()):
        """`sandboxed=False` runs an agent that has an edit-only mode (Claude) in that mode, sandbox or not.
        `share`: folders the person shows the agent read-only in the sandbox (sandbox.shared_folders)."""
        self.board, self.root, self.runner, self.timeout = board, root, runner, timeout_sec
        self.share = list(share)
        self.shared: list[Path] = []   # checked, once check() has run
        self.hidden: list[Path] = []   # credential folders and files inside them, shown empty
        if agent not in AGENTS:
            raise WorkerUnavailable(f"The worker changes files with {' or '.join(AGENTS)}. For answers, reviews and "
                                    f"pictures by {agent} (through Ixel MAT), approve with --kind and use: handoff run")
        self.spec = AGENTS[agent]
        if not sandboxed:
            if self.spec.fallback is None:
                raise WorkerUnavailable(f"The {agent} worker only runs inside its sandbox.")
            self.spec = self.spec.fallback
        self.fallback_reason: str | None = None  # why it's running edit-only, when its sandbox can't run here
        self.command = command or find_on_path(self.spec.command)
        self.bwrap: str | None = None
        # Tasks that couldn't start (task id: its approval, and the login problem then): run_pending passes
        # them by until the approval or the login changes, so a worker doesn't retry one every few seconds
        self._waiting: dict[int, tuple[int, str | None]] = {}

    def check(self) -> None:
        """Refuse to start where the worker's protections can't hold, or fall back to a mode where they do."""
        if depth() > 0:
            raise WorkerUnavailable(f"This was started by Handoff ({DEPTH_VAR} is set), and a worker can't start "
                                    "another worker.")
        if self.spec.sandboxed:
            try:
                self.bwrap = sandbox.check(self.spec.needs)
            except sandbox.SandboxUnavailable as exc:
                if self.spec.fallback is None:
                    raise WorkerUnavailable(str(exc)) from exc
                self.spec, self.fallback_reason = self.spec.fallback, str(exc)
        if self.share:
            try:
                self.shared, self.hidden = sandbox.shared_folders(self.share, project=self.root)
            except sandbox.ShareRefused as exc:
                raise WorkerUnavailable(str(exc)) from exc
        if not self.command:
            raise WorkerUnavailable(f"The `{self.spec.command}` command isn't on PATH, so the {self.spec.name} "
                                    "worker can't run.")
        try:  # (through run_tree, so Ctrl+C's stop_all stops it on whatever thread it runs)
            proc = run_tree([self.command, *self.spec.help_args], "", agent_env(self.spec), 60.0)
        except Stopped as exc:
            raise WorkerUnavailable("The check was stopped before it finished.") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkerUnavailable(f"Couldn't run `{self.spec.command} --help`: {exc}") from exc
        missing = [flag for flag in self.spec.required_flags if flag not in proc.stdout + proc.stderr]
        if missing:
            raise WorkerUnavailable(f"This version of {self.spec.command} doesn't have {', '.join(missing)}, which "
                                    "the worker needs to keep the agent in its worktree. Update it and try again.")
        self.check_login()

    def check_login(self) -> None:
        """A sandboxed agent gets a copy of its login: say so now if there's none, before anything starts."""
        problem = self._login_problem()
        if problem:
            raise WorkerUnavailable(problem)

    def _login_problem(self) -> str | None:
        if self.spec.sandboxed and self.spec.login is not None:
            return self.spec.login.missing(agent_env(self.spec))
        return None

    def argv(self, values: dict[str, str] | None = None, model: str | None = None, plan: bool = False) -> list[str]:
        """The agent's command line, with its login's values in place of the placeholders (in one pass). `model`:
        the model to run on (the agent's default if None). `plan`: a chain's planner, with read-only tools."""
        command = os.path.realpath(self.command) if self.spec.sandboxed else str(self.command)
        values = values or {}
        pattern = re.compile("|".join(re.escape(k) for k in values)) if values else None
        args = [pattern.sub(lambda m: values[m.group()], a) if pattern else a for a in self.spec.args]
        if plan:
            args = _read_only(args)
        if model:
            if not self.spec.model_flag or not approvals.check_models([model]):
                raise WorkerUnavailable(f"The {self.spec.name} worker can't be told to use {model}.")
            at = len(args) - 1 if args and args[-1] == "-" else len(args)  # (codex reads the prompt from "-")
            args[at:at] = [self.spec.model_flag, model]
        return [command, *args]

    def run_pending(self, on_start: Callable[[Task, str], None] | None = None,
                    on_done: Callable[[RunResult], None] | None = None) -> list[RunResult]:
        """Every approved run for this agent, one at a time: edits here, answers and reviews through Ixel.
        `on_start` gets each task and its kind of run as it starts; `on_done` each result as it comes."""
        from handoff import asks
        results = []
        login = self._login_problem()
        for task, approval in self.board.pending_runs(self.spec.name):
            if self._waiting.get(task.id) == (approval.id, login):
                continue  # (it said why once; it starts when you sign in, or approve it again)
            kind = run_kind(approval)
            if on_start:
                on_start(task, kind)
            if kind == "edit":
                result = self.run(task, approval)
                if result.waiting:
                    self._waiting[task.id] = (approval.id, login)
            elif kind in asks.VIA_IXEL:
                result = asks.run(self.board, self.root, task, approval)
            else:
                result = RunResult(task, False, "This approval is for a kind of run this version of Handoff "
                                                "doesn't know. Update Handoff (handoff update).")
            results.append(result)
            if on_done:
                on_done(result)
        return results

    def _launch(self, worktree: gitwork.Worktree, prompt: str, model: str | None = None, plan: bool = False,
                timeout: float | None = None) -> tuple["subprocess.CompletedProcess[str]", str | None]:
        """Run the agent, in the sandbox with a private login if it has one. Returns its result and a login note."""
        if self.spec.sandboxed and self.bwrap is None:
            self.check()  # may fall back to edit-only
        env = agent_env(self.spec)
        timeout = self.timeout if timeout is None else timeout
        if not self.spec.sandboxed:
            return self.runner(self.argv(model=model, plan=plan), prompt, env, timeout, str(worktree.path)), None
        assert self.spec.login is not None and self.bwrap is not None
        home = Path.home()
        login = self.spec.login(env)
        inside = home / login.folder
        note = None
        try:
            login.prepare()
            env = {k: v for k, v in env.items() if not _looks_secret(k)}
            env[login.home_var] = str(inside)
            env["HOME"] = str(home)
            argv = sandbox.wrap(self.bwrap, self.argv(login.values(inside), model, plan), worktree=worktree.path,
                                home=home, private={login.home: inside},
                                read_only=[*sandbox.install_dirs(self.command), *self.shared],
                                env=env, hidden=self.hidden)
            proc = self.runner(argv, prompt, env, timeout, str(worktree.path))
        finally:  # a login refreshed mid-run goes back even if the run failed or timed out
            try:
                note = login.finish()
            except OSError as exc:
                note = f"{self.spec.name} refreshed its login during the run, but copying it back failed ({exc})."
            login.cleanup()
        return proc, note

    def run(self, task: Task, approval: Event) -> RunResult:
        from handoff.asks import NOT_STARTED
        agent = self.spec.name
        return_to = safe_name(approval.data.get("return_to")) or HUMAN
        branch = f"handoff/{task.ref}"
        if stopping():  # Ctrl+C came before this one started: its approval stays
            return RunResult(task, False, f"{NOT_STARTED} It's still approved; run it with: handoff run {task.ref}")
        try:  # (before the approval is used: once you sign in, say, it runs as approved)
            if self.spec.sandboxed and self.bwrap is None:
                self.check()  # may fall back to edit-only
            else:
                self.check_login()
        except WorkerUnavailable as exc:
            return RunResult(task, False, f"{exc} It's still approved; run it later with: handoff run {task.ref}",
                             waiting=str(exc))
        try:
            task, history = self.board.start_run(agent, task.id, approval.id, {"branch": branch})
        except BoardError as exc:  # another worker took it, or it changed since approval
            return RunResult(task, False, str(exc))

        def fail(reason: str, used: dict | None = None, **extra) -> RunResult:
            try:
                failed = self.board.fail_run(agent, task.id, return_to, reason, used)
            except BoardError as exc:
                return RunResult(task, False, f"{reason} ({exc})", used=used, **extra)
            return RunResult(failed, False, reason, used=used, **extra)

        def stopped() -> str:
            folder = self.root / gitwork.WORKTREES / task.ref
            kept = (f" Anything it changed is in {folder.relative_to(self.root).as_posix()}, not committed."
                    if folder.is_dir() else "")
            return f"Stopped by you (Ctrl+C) while {agent} was running.{kept}"

        try:
            return self._started(task, approval, history, branch, return_to, fail)
        except Stopped:
            return fail(stopped(), branch=branch)
        except BaseException as exc:  # Ctrl+C, or a crash: never leave it in progress with nobody running it
            fail(stopped() if isinstance(exc, KeyboardInterrupt) else f"The run crashed: {type(exc).__name__}.",
                 branch=branch)
            raise

    def _started(self, task: Task, approval: Event, history: list[Event], branch: str, return_to: str,
                 fail: Callable[..., RunResult]) -> RunResult:
        agent = self.spec.name
        target = None
        if "target" in approval.data:  # a pull request's commits, sealed in the approval: start from its head
            target = approvals.check_target(approval.data["target"])
            if target is None:
                return fail("The commits this was approved to start from aren't readable.")
        try:
            worktree = gitwork.ensure_worktree(self.root, task.id, _base_hint(self.board.events(task.id)),
                                               target["head"] if target else None)
            leaving = gitwork.links_leaving(worktree.path)
        except gitwork.GitError as exc:
            return fail(str(exc))
        if leaving:
            shown = ", ".join(leaving[:5]) + (" …" if len(leaving) > 5 else "")
            return fail(f"The worktree has links pointing outside it ({shown}), so Handoff won't run an agent "
                        f"there. Remove them from {branch}, then approve the task again.", branch=branch)

        models = run_models(approval)
        if len(models) > 1:
            return self._chain(task, history, branch, return_to, target, worktree, models, fail)
        model = models[0] if models else None
        prompt = build_prompt(self.spec, task, history, branch, target)
        login_note, used, reply = None, None, ""
        try:
            proc, login_note = self._launch(worktree, prompt, model)
            reply, used, error = self._read(proc)
            problem = None if proc.returncode == 0 and not error else self._exit_problem(proc, error)
        except subprocess.TimeoutExpired:
            proc, problem = None, f"{agent} was stopped after {duration(self.timeout)}."
        except (OSError, WorkerUnavailable) as exc:
            proc, problem = None, f"Couldn't start {agent}: {exc}"

        try:  # keep whatever it changed, even from a run that failed, unless it holds a secret
            commit, tokens = self._commit(worktree, f"{task.ref}: {task.title}\n\nMade by the Handoff worker "
                                          f"({agent}{f', {model}' if model else ''}) from the task as the person "
                                          "approved it.\n")
            files = gitwork.changed_files(self.root, worktree)
        except _Withheld as exc:
            return fail(f"{problem + ' ' if problem else ''}{exc}", used, branch=branch)
        except gitwork.GitError as exc:
            return fail(f"{problem + ' ' if problem else ''}Committing the result failed: {exc}", used, branch=branch)
        token = _token_note(tokens) if commit else None
        if problem:
            kept = f" What it changed is committed on {branch}." if commit else ""
            return fail(problem + kept + (f" {token}" if token else ""), used, branch=branch, commit=commit)

        done, left, verify = parse_reply(reply)
        if commit is None:
            done = "No files were changed.\n\n" + done
        return self._finish(task, return_to, branch, worktree, commit, files, done, left, verify, token, login_note,
                            used, fail)

    def _chain(self, task: Task, history: list[Event], branch: str, return_to: str, target: dict | None,
               worktree: gitwork.Worktree, models: list[str], fail: Callable[..., RunResult]) -> RunResult:
        """A chain of models, biggest first: models[0] reads the task and plans the steps, giving each to one of the
        others; then each step runs in turn, in the same worktree, and is committed on its own, so the branch shows
        which model made what. The whole chain gets the run's time limit."""
        agent, planner = self.spec.name, models[0]
        if not self.spec.reports_usage:
            return fail(f"A chain of models runs on {' or '.join(CHAIN_AGENTS)}, not {agent}.")
        deadline = time.monotonic() + self.timeout
        notes: list[str] = []
        spent: list[dict | None] = []

        def left_time() -> float:
            return max(deadline - time.monotonic(), 1.0)

        def launch(prompt: str, model: str, plan: bool = False) -> tuple[str, str | None]:
            """Run one model; its reply, and what went wrong (None if nothing did)."""
            try:
                proc, note = self._launch(worktree, prompt, model, plan=plan, timeout=left_time())
            except subprocess.TimeoutExpired:
                return "", f"{model} was stopped: the chain ran past its {duration(self.timeout)}."
            except (OSError, WorkerUnavailable) as exc:
                return "", f"Couldn't start {agent} on {model}: {exc}"
            if note:
                notes.append(note)
            reply, used, error = self._read(proc)
            spent.append(used)
            return reply, (None if proc.returncode == 0 and not error else self._exit_problem(proc, error))

        reply, problem = launch(plan_prompt(self.spec, task, history, models, target), planner, plan=True)
        if problem:
            return fail(f"{planner} couldn't plan it: {problem}", usage.merge(*spent), branch=branch)
        steps = parse_plan(reply, models[1:])
        if steps is None:
            steps = [Step(models[1], "the whole task", "Do the whole task.")]
            notes.append(f"{planner} didn't write a plan the worker could read, so {models[1]} did the whole task.")
        done_parts, verify_parts, left, tokens, commit = [], [], "", [], None
        for number, step in enumerate(steps, 1):
            before = len(spent)
            reply, problem = launch(step_prompt(self.spec, task, history, branch, target, planner, steps, number),
                                    step.model)
            cost = usage.total_cost(usage.merge(*spent[before:]))
            try:
                made, found = self._commit(worktree, f"{task.ref} step {number}/{len(steps)}: {step.title}\n\nMade by "
                                           f"the Handoff worker ({agent}, {step.model}), planned by {planner}, "
                                           "from the task as the person approved it.\n")
            except _Withheld as exc:
                return fail(f"{problem + ' ' if problem else ''}{exc}", usage.merge(*spent), branch=branch,
                            commit=commit)
            except gitwork.GitError as exc:
                return fail(f"Committing step {number} failed: {exc}", usage.merge(*spent), branch=branch,
                            commit=commit)
            commit, tokens = made or commit, tokens + found
            if problem:
                kept = f" What the steps changed is committed on {branch}." if commit else ""
                return fail(f"Step {number} of {len(steps)} ({step.model}: {step.title}) failed: {problem}{kept}",
                            usage.merge(*spent), branch=branch, commit=commit)
            step_done, step_left, step_verify = parse_reply(reply)
            done_parts.append(f"Step {number} of {len(steps)}, by {step.model} (about ${cost:.4f}): {step.title}"
                              f"{'' if made else ' (changed no files)'}\n{step_done}")
            if step_verify and step_verify not in verify_parts:
                verify_parts.append(step_verify)
            left = step_left  # (the last step knows what's still left)
        try:
            files = gitwork.changed_files(self.root, worktree)
        except gitwork.GitError as exc:
            return fail(f"Reading what the chain changed failed: {exc}", usage.merge(*spent), branch=branch)
        done = _clip(f"{planner} planned {len(steps)} step{'s' if len(steps) > 1 else ''}.\n\n"
                     + "\n\n".join(done_parts))
        if commit is None:
            done = "No files were changed.\n\n" + done
        token = _token_note(sorted(set(tokens))) if commit else None
        verify = _clip("\n\n".join(verify_parts))
        return self._finish(task, return_to, branch, worktree, commit, files, done, left, verify, token,
                            "\n\n".join(notes) or None, usage.merge(*spent), fail)

    def _read(self, proc: "subprocess.CompletedProcess[str]") -> tuple[str, dict | None, str | None]:
        """The agent's reply, what it used and what went wrong, from its output."""
        if not self.spec.reports_usage:
            return proc.stdout or "", None, None
        return usage.parse_claude(proc.stdout or "")

    def _commit(self, worktree: gitwork.Worktree, message: str) -> tuple[str | None, list[str]]:
        """Commit what the agent changed: the new commit (None if nothing changed) and the files holding only a JSON
        Web Token (committed, and said: it's often a sample). _Withheld if anything looks like a secret."""
        staged = gitwork.stage_all(self.root, worktree)
        tokens: list[str] = []
        found = gitwork.secrets_in(worktree, staged, tokens)
        if found:
            gitwork.unstage_all(self.root, worktree)
            where = ", ".join(f"{path} ({what})" for path, what in found[:5])
            raise _Withheld(f"The changes include what looks like a secret: {where}. Nothing was committed; the "
                            f"files are in {worktree.path.relative_to(self.root).as_posix()}.")
        return gitwork.commit_staged(self.root, worktree, message), tokens

    def _finish(self, task: Task, return_to: str, branch: str, worktree: gitwork.Worktree, commit: str | None,
                files: list[str], done: str, left: str, verify: str, token: str | None, login_note: str | None,
                used: dict | None, fail: Callable[..., RunResult]) -> RunResult:
        agent = self.spec.name
        fallback = (f"Its sandbox can't run here, so {agent} ran edit-only; `handoff doctor` says what it needs."
                    if self.fallback_reason else None)
        shared = (f"You shared these folders with it, read-only: {', '.join(_home_relative(p) for p in self.shared)}."
                  if self.shared and self.spec.sandboxed else None)
        spent = ("Used: " + "; ".join(usage.lines(used)) + " (estimated at API prices)") if used else None
        notes = "\n\n".join(n for n in (token, self.spec.note, shared, fallback, login_note, spent) if n)
        left = f"{left}\n\n{notes}" if left else notes
        try:
            finished = self.board.finish_run(agent, task.id, return_to, done, left, verify, files, branch,
                                             extra={"base": worktree.base, "commit": commit, "usage": used})
        except BoardError as exc:  # (it can't stay in progress: nobody is running it now)
            return fail(f"The board didn't take the result: {exc} The work is on {branch}.", used, branch=branch,
                        commit=commit)
        return RunResult(finished, True, f"handed to {return_to}" + (f". {token}" if token else ""), branch, commit,
                         used=used)

    @staticmethod
    def _exit_problem(proc: "subprocess.CompletedProcess[str]", error: str | None = None) -> str:
        """Why the agent stopped: the error it reported (Claude Code's JSON result), else its output's last line."""
        lines = (error or proc.stderr or proc.stdout or "").strip().splitlines()
        detail = clean_line(lines[-1])[:300] if lines else ""
        if find_secret(detail):  # the board would refuse it, leaving the task stuck in progress
            detail = "[withheld: it looked like it contained a secret]"
        if detail and detail[-1] not in ".!?":  # (more may follow it)
            detail += "."
        return f"The agent stopped with exit code {proc.returncode}" + (f": {detail}" if detail else ".")

