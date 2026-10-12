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
from typing import Callable, Mapping

from handoff import approvals, gitwork, limits, sandbox, usage
from handoff.board import HUMAN, Board, BoardError, Event, Task, run_efforts, run_kind, run_models, safe_name
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
# How hard each agent's model thinks, in the agent's own words, least first: Claude Code's --effort (as its --help
# lists them) and Codex's model_reasoning_effort (the newest GPT models go up to max and ultra; older ones take
# fewer, and the Ixel app's Board offers only those a model takes)
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

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
              "worktree of their project). Don't change any files yourself. Read only what you need to split the "
              "task well, such as the files it names: the models doing the steps read the files themselves, so "
              "don't read the whole project, and name files in the steps rather than copying them. Then split the "
              "task into 1 to {max_steps} steps, in order, that together finish it.\n"
              "\n"
              "Give each step to one of these models, biggest first: {tiers}. Give a step to the smallest model "
              "that will do it well: the bigger ones for design, tricky logic and anything that needs judgment, "
              "the smaller ones for clear, mechanical work such as tests, renames, docs and small edits. Fewer "
              "steps are better: don't split what one model can do in one go. Write each step's instructions so "
              "its model can do it without asking: which files, what to change, and how to check it. Those "
              "models can {powers_short}.{local}\n"
              "\n"
              "End your reply with the plan as JSON in a ```json block, in exactly this shape:\n"
              '{{"steps": [{{"model": "one of the models above", "title": "a few words", '
              '"instructions": "what to do"}}]}}')

# A model on this computer (or another small open one) takes directions well, but isn't reliable at design or at
# writing much code: what the planner is told about the ones in its chain
LOCAL_BRIEF = (" {names} {verb} small, on this computer: give {them} only short, exact directions for work you're "
               "sure {they} can do, such as one change in one named file, spelled out (the function, the lines, "
               "what they become). Nothing that needs judgment or a lot of new code.")

# After the steps, the biggest of the step models checks what the smaller ones did, and fixes what's wrong; what's
# too big for it goes up to the planner
CHECK_BRIEF = ("\n\n## Check the work\n\n"
               "{planner} planned this task as {count} steps, and smaller models did {which}. Their work is in "
               "this folder now; what they changed is below. Check it against the task and the plan: read the "
               "files it touches, and fix any mistake you find (a bug, a missed part of the task, a broken "
               "test or file), in place. Don't redo work that's right, and don't add what the task doesn't ask "
               "for.\n\n"
               "Write DONE, LEFT and VERIFY about your check (DONE: \"nothing to fix\" when it was all right). "
               "Then, on its own line, write ESCALATE: no, or, if something is wrong that's too big or too hard "
               "for you to fix well (a wrong design, a large part missing), ESCALATE: and what it is, in a "
               "sentence.")

FIX_BRIEF = ("\n\n## Fix what the check found\n\n"
             "You planned this task; the models doing its steps and the one checking them have finished, and the "
             "check found a problem too big for it: {problem}\n"
             "What the chain changed is below. Fix that problem in place (you can change files now), then write "
             "DONE, LEFT and VERIFY about your fix.")

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
    effort_args: tuple[str, ...] = ()    # how it's told how hard to think, with {effort} for the level
    effort_levels: tuple[str, ...] = ()  # the levels it takes, least first (none: it can't be told)
    # The agents Handoff doesn't ship a sandbox for (adapters.py): OpenCode, Hermes Agent, Kimi Code, Cline,
    # OpenClaw, a model on your own computer, and any agent you add yourself
    label: str = ""                      # its name for people ("" : the name, capitalized)
    kind: str = "cli"                    # cli: a program; local: a model server, driven by Handoff (local_model.py)
    output: str = "text"                 # how its reply and usage are read: text, claude, opencode, hermes, local
    access: str = "sandbox"              # what it can reach (ACCESS): sandbox, edit-only, files or full
    needs_ok: bool = False               # it runs only once you've said so (handoff agents enable NAME)
    env: tuple[tuple[str, str], ...] = ()  # set for the run, with {worktree} for its folder
    url: str = ""                        # a local model server's OpenAI-style API, like http://127.0.0.1:11434/v1
    key_env: str = ""                    # the variable holding that server's key, if it wants one
    default_model: str = ""              # the model it runs when the approval names none ("" : its own choice)
    models_args: tuple[str, ...] = ()    # `<command> <models_args>` lists the models it can run, one per line
    custom: bool = False                 # you added it yourself (agents.toml), rather than Handoff knowing it
    newer: "AgentSpec | None" = None     # the same agent's newer command line, run where its --help has those flags


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
    model_flag="--model", reports_usage=True, effort_args=("--effort", "{effort}"), effort_levels=CLAUDE_EFFORTS,
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


def _and(items: list[str]) -> str:
    """["a", "b", "c"] → "a, b and c"."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _on(agent: str, model: str | None, effort: str | None) -> str:
    """What ran a run, as its commit says: "claude, claude-opus-5-5, high effort"."""
    return ", ".join([agent, *([model] if model else []), *([f"{effort} effort"] if effort else [])])


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
                target: dict | None = None, efforts: dict[str, str] | None = None) -> str:
    """What a chain's planner (models[0]) is given: how to plan for the others, then the task."""
    powers = "run commands and tests here" if spec.sandboxed else "only read and edit files (no commands or tests)"
    efforts = efforts or {}
    tiers = ", ".join(f"{m} ({efforts[m]} effort)" if m in efforts else m for m in models[1:])
    small = [m for m in models[1:] if is_small(m)]
    local = LOCAL_BRIEF.format(names=_and(small), verb="is" if len(small) == 1 else "are",
                               them="it" if len(small) == 1 else "them",
                               they="it" if len(small) == 1 else "they") if small else ""
    brief = PLAN_BRIEF.format(planner=models[0], ref=task.ref, max_steps=MAX_STEPS, tiers=tiers,
                              powers_short=powers, local=local)
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


def step_agent(model: str, agent: str) -> tuple[str, str]:
    """Which agent runs a chain's model, and the model as that agent takes it: "ollama:qwen3:4b-instruct" runs on
    ollama, "opencode:opencode/step-5-preview-free" on opencode; a plain name (or one whose part before the colon
    isn't another agent, like Ollama's own qwen3:4b) on the chain's agent."""
    head, colon, rest = model.partition(":")
    if colon and rest and head != agent and head in AGENTS:
        return head, rest
    return agent, model


def is_small(model: str) -> bool:
    """A model on this computer, or a free one: given short, exact directions only (LOCAL_BRIEF)."""
    head, _, rest = model.partition(":")
    spec = AGENTS.get(head)
    return bool(spec and spec.kind == "local" and rest) or model.endswith("-free")


def _diff_text(root: Path, worktree: gitwork.Worktree, limit: int = 60_000) -> str:
    """What the chain changed since it started, as a diff, cut to `limit` bytes (fenced by the caller)."""
    try:
        text = gitwork.diff_since(root, worktree)
    except (gitwork.GitError, AttributeError):
        return ""
    data = text.encode("utf-8", "replace")
    return text if len(data) <= limit else data[:limit].decode("utf-8", "ignore") + "\n[… cut]"


_ESCALATE = re.compile(r"^\s*ESCALATE:\s*(.+?)\s*$", re.M | re.I)


def escalation(reply: str) -> str | None:
    """The problem a check sent up to the planner, or None ("ESCALATE: no", or no such line)."""
    found = _ESCALATE.findall(reply or "")
    if not found:
        return None
    said = clean_line(found[-1])[:500].strip().rstrip(".")
    return None if said.lower() in ("no", "none", "nothing", "n/a", "no.") else said


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
            if find_secret(title):  # (it goes in commit messages and on the board)
                title = "a step (its title was withheld: it looked like it contained a secret)"
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
    model_flag="--model", reports_usage=True, effort_args=("--effort", "{effort}"), effort_levels=CLAUDE_EFFORTS,
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
    effort_args=("-c", 'model_reasoning_effort="{effort}"'), effort_levels=CODEX_EFFORTS,
)

BUILT_IN = {"claude": CLAUDE, "codex": CODEX}
# Every agent the worker can run here: claude and codex, then the others Handoff finds, and yours (adapters.py).
# load_agents() reads them again (agents.toml may have changed); AGENTS stays the same dict
AGENTS: dict[str, AgentSpec] = dict(BUILT_IN)
AGENT_PROBLEMS: list[str] = []  # what's wrong in agents.toml, if anything
CHAIN_AGENTS = ("claude",)  # the agents a chain of models can run on (its planner needs read-only tools)


def load_agents() -> dict[str, AgentSpec]:
    from handoff import adapters
    found, problems = adapters.build()
    AGENTS.clear()
    AGENTS.update(BUILT_IN)
    AGENTS.update({name: spec for name, spec in found.items() if name not in BUILT_IN})
    AGENT_PROBLEMS[:] = problems
    return AGENTS


def names(items, word: str = "and") -> str:
    """["claude", "codex", "opencode"] -> "claude, codex and opencode"."""
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} {word} {items[-1]}"


def label(spec: AgentSpec) -> str:
    return spec.label or spec.name.capitalize()


def access(spec: AgentSpec, fallback: bool = False) -> str:
    """What the agent can reach when it runs here, for people: "sandbox", "edit-only", "files" or "full"."""
    if spec.access == "sandbox" and (fallback or not spec.sandboxed):
        return "edit-only"
    return spec.access


load_agents()


def effort_problem(agent: str, efforts: Mapping[str, str] | None) -> str | None:
    """Why the agent's worker can't be told these efforts (None: it can, or there are none)."""
    for model, level in (efforts or {}).items():
        who = step_agent(model, agent)[0] if model else agent
        spec = AGENTS.get(who)
        levels = spec.effort_levels if spec else ()
        if level not in levels:
            if not levels:
                return f"The {who} worker can't be told how hard to think."
            return f"{who} takes effort {', '.join(levels[:-1])} or {levels[-1]}, not {level}."
    return None


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
            raise WorkerUnavailable(f"The worker changes files with {names(AGENTS, 'or')}. For answers, reviews and "
                                    f"pictures by {agent} (through Ixel MAT), approve with --kind and use: handoff run")
        self.spec = AGENTS[agent]
        if not sandboxed:
            if self.spec.fallback is None:
                raise WorkerUnavailable(f"The {agent} worker only runs inside its sandbox.")
            self.spec = self.spec.fallback
        self.fallback_reason: str | None = None  # why it's running edit-only, when its sandbox can't run here
        self.command = command or (find_on_path(self.spec.command) if self.spec.command else None)
        self.bwrap: str | None = None
        self.help = ""  # what `<command> --help` said, once check() has run
        self.no_effort = False  # its version can't be told an effort (OpenCode 2): runs think at its default
        self.models: list[str] = []  # a local model server's models, once check() has run
        # Tasks that couldn't start (task id: its approval, and the login problem then): run_pending passes
        # them by until the approval or the login changes, so a worker doesn't retry one every few seconds
        self._waiting: dict[int, tuple[int, str | None]] = {}
        self._helpers: dict[str, "Worker"] = {}  # the workers of other agents a run's fallbacks name

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
        if self.spec.kind == "local":
            self.check_login()
            self._check_server()
            return
        if not self.command:
            raise WorkerUnavailable(f"The `{self.spec.command}` command isn't on PATH, so the {self.spec.name} "
                                    "worker can't run.")
        try:  # (through run_tree, so Ctrl+C's stop_all stops it on whatever thread it runs)
            proc = run_tree([self.command, *self.spec.help_args], "", agent_env(self.spec), 60.0)
        except Stopped as exc:
            raise WorkerUnavailable("The check was stopped before it finished.") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkerUnavailable(f"Couldn't run `{self.spec.command} --help`: {exc}") from exc
        self.help = proc.stdout + proc.stderr
        from handoff import adapters
        if adapters.prompt_on_command_line(self.spec) and adapters.windows_shim(self.command):
            raise WorkerUnavailable(f"{self.command} is a .cmd or .bat file, which runs through cmd.exe, and "
                                    f"{self.spec.name} takes the task on its command line, where cmd.exe would read "
                                    "it. Handoff won't run it that way on Windows.")
        missing = [flag for flag in self.spec.required_flags if flag not in self.help]
        newer = self.spec.newer
        if missing and newer and newer.help_args == self.spec.help_args \
                and all(flag in self.help for flag in newer.required_flags):
            self.no_effort = bool(self.spec.effort_levels) and not newer.effort_levels
            self.spec, missing = newer, []
        if missing:
            raise WorkerUnavailable(f"This version of {self.spec.command} doesn't have {', '.join(missing)}, which "
                                    "the worker needs to keep the agent in its worktree. Update it and try again.")
        self.check_login()

    def _enabled(self) -> bool:
        from handoff import adapters
        return adapters.enabled(self.spec.name)

    def _server_key(self) -> str | None:
        return os.environ.get(self.spec.key_env) if self.spec.key_env else None

    def _check_server(self) -> None:
        """A local model: its server answers, and has a model to run."""
        from handoff import local_model
        try:
            models = local_model.list_models(self.spec.url, self._server_key())
        except local_model.ServerError as exc:
            start = {"ollama": " Start it (open Ollama, or run: ollama serve).",
                     "lmstudio": " Start its server (in LM Studio, or run: lms server start)."}.get(self.spec.name, "")
            raise WorkerUnavailable(f"{label(self.spec)} isn't answering at {self.spec.url}: {exc}.{start}") from None
        if not models:
            pull = " Get one with: ollama pull qwen3:4b-instruct" if self.spec.name == "ollama" else ""
            raise WorkerUnavailable(f"{label(self.spec)} at {self.spec.url} has no models yet.{pull}")
        self.models = models

    def list_models(self) -> list[str]:
        """The models this agent can run, as it lists them ([] where it can't say)."""
        from handoff import local_model
        if self.spec.kind == "local":
            try:
                return local_model.list_models(self.spec.url, self._server_key())
            except local_model.ServerError:
                return []
        if not self.spec.models_args or not self.command:
            return []
        if self.spec.newer and not self.help:
            try:
                self.check()  # (which command line this version takes: OpenCode 2 lists them its own way)
            except WorkerUnavailable:
                pass
        try:
            proc = run_tree([self.command, *self.spec.models_args], "", self._env(), 30.0)
        except (Stopped, OSError, subprocess.SubprocessError):
            return []
        names = [line.strip() for line in (proc.stdout or "").splitlines()]
        return [n for n in names if n and approvals.check_models([n])][:300] if proc.returncode == 0 else []

    def _env(self, worktree: Path | None = None) -> dict[str, str]:
        env = agent_env(self.spec)
        for name, value in self.spec.env:
            env[name] = value.replace("{worktree}", str(worktree) if worktree else "")
        return env

    def check_login(self) -> None:
        """A sandboxed agent gets a copy of its login: say so now if there's none, before anything starts."""
        problem = self._login_problem()
        if problem:
            raise WorkerUnavailable(problem)

    def _login_problem(self) -> str | None:
        if self.spec.needs_ok and not self._enabled():  # (as a login is: its approval waits until you say so)
            from handoff import adapters
            return (f"{label(self.spec)} {adapters.ACCESS[self.spec.access]}, so it runs only once you've said so: "
                    f"handoff agents enable {self.spec.name} (or switch it on in the Ixel app's Board).")
        if self.spec.sandboxed and self.spec.login is not None:
            return self.spec.login.missing(agent_env(self.spec))
        return None

    def argv(self, values: dict[str, str] | None = None, model: str | None = None, plan: bool = False,
             effort: str | None = None) -> list[str]:
        """The agent's command line, with its login's values in place of the placeholders (in one pass). `model`:
        the model to run on (the agent's default if None). `plan`: a chain's planner, with read-only tools.
        `effort`: how hard the model thinks, one of the agent's effort_levels (its default if None)."""
        if self.spec.kind == "local":
            raise WorkerUnavailable(f"{self.spec.name} is a model server, not a program to run.")
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
        if effort:
            at = len(args) - 1 if args and args[-1] == "-" else len(args)
            args[at:at] = self._effort_args(effort)
        return [command, *args]

    def _effort(self, effort: str | None) -> str | None:
        """The effort a run is told: none where this version of the agent can't be told one (its default), so an
        effort approved for the agent's older command line doesn't stop the run."""
        return None if self.no_effort else effort

    def _effort_args(self, effort: str) -> list[str]:
        levels, name = self.spec.effort_levels, self.spec.name
        if effort not in levels:
            takes = f"takes {', '.join(levels[:-1])} or {levels[-1]}" if levels else "can't be told one"
            raise WorkerUnavailable(f"The {name} worker can't think at {effort} effort: it {takes}.")
        flag = self.spec.effort_args[0]
        if flag.startswith("--") and self.help and flag not in self.help:
            raise WorkerUnavailable(f"This version of {self.spec.command} has no {flag}. Update it, or approve the "
                                    "task again without an effort.")
        return [a.replace("{effort}", effort) for a in self.spec.effort_args]

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
                timeout: float | None = None,
                effort: str | None = None) -> tuple["subprocess.CompletedProcess[str]", str | None]:
        """Run the agent, in the sandbox with a private login if it has one. Returns its result and a login note."""
        if self.spec.sandboxed and self.bwrap is None:
            self.check()  # may fall back to edit-only
        env = agent_env(self.spec)
        timeout = self.timeout if timeout is None else timeout
        model = model or self.spec.default_model or None
        if self.spec.kind == "local":
            return self._run_local(worktree, prompt, model, timeout, effort), None
        if not self.spec.sandboxed:
            return self._run_unsandboxed(worktree, prompt, model, plan, timeout, effort), None
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
            argv = sandbox.wrap(self.bwrap, self.argv(login.values(inside), model, plan, effort),
                                worktree=worktree.path, home=home, private={login.home: inside},
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

    def _run_local(self, worktree: gitwork.Worktree, prompt: str, model: str | None, timeout: float,
                   effort: str | None) -> "subprocess.CompletedProcess[str]":
        from handoff import local_model
        if effort and effort not in self.spec.effort_levels:
            self._effort_args(effort)  # (raises, saying which levels it takes)
        if model is None:
            models = getattr(self, "models", None) or local_model.list_models(self.spec.url, self._server_key())
            if not models:
                raise WorkerUnavailable(f"{label(self.spec)} has no models to run.")
            model = models[0]
        elif not approvals.check_models([model]):
            raise WorkerUnavailable(f"The {self.spec.name} worker can't be told to use {model}.")
        return local_model.run(self.spec.url, model, prompt, str(worktree.path), timeout, effort or "none",
                               self._server_key())

    def _run_unsandboxed(self, worktree: gitwork.Worktree, prompt: str, model: str | None, plan: bool,
                         timeout: float, effort: str | None) -> "subprocess.CompletedProcess[str]":
        """An agent with no sandbox of Handoff's: Claude edit-only, or one of adapters.py's. The task goes in on
        stdin, or, for an agent that only takes it on its command line, as the value it's given there (the brief
        starts it, so it never reads as a flag), or in a file it reads."""
        env = self._env(worktree.path)
        argv = self.argv(model=model, plan=plan, effort=effort)
        stdin = prompt
        scratch = Path(tempfile.mkdtemp(prefix="handoff-run-"))  # 0700
        try:
            usage_file = scratch / "usage.json"
            for n, arg in enumerate(argv):
                if arg == "{prompt}":
                    argv[n], stdin = prompt, ""
                elif arg == "{prompt_file}":
                    _write_private(scratch / "task.md", prompt.encode("utf-8"))
                    argv[n], stdin = str(scratch / "task.md"), ""
                elif arg == "{usage_file}":
                    argv[n] = str(usage_file)
            proc = self.runner(argv, stdin, env, timeout, str(worktree.path))
            if self.spec.output == "hermes":
                try:
                    proc.usage_report = usage_file.read_text(encoding="utf-8")[:100_000]  # type: ignore[attr-defined]
                except OSError:
                    pass
            return proc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

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
            except BoardError:  # (it can't stay in progress: say it failed in words the board takes)
                try:
                    failed = self.board.fail_run(agent, task.id, return_to, _clip(reason), used)
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

        from handoff.board import run_fallbacks
        models, efforts = run_models(approval), run_efforts(approval)
        if len(models) > 1:
            return self._chain(task, history, branch, return_to, target, worktree, models, efforts, fail,
                               run_fallbacks(approval))
        model = models[0] if models else None
        effort = self._effort(efforts.get(model or ""))
        prompt = build_prompt(self.spec, task, history, branch, target)
        deadline = time.monotonic() + self.timeout
        swaps: list[str] = []
        try:
            reply, used, problem, (agent, model, effort), login_note = self._launch_or_fallback(
                worktree, prompt, model, effort, run_fallbacks(approval), set(), swaps,
                lambda: max(deadline - time.monotonic(), 1.0))
        except subprocess.TimeoutExpired:
            reply, used, problem, login_note = "", None, f"{agent} was stopped after {duration(self.timeout)}.", None
        if swaps:
            login_note = "\n\n".join(n for n in (" ".join(swaps), login_note) if n)

        try:  # keep whatever it changed, even from a run that failed, unless it holds a secret
            commit, tokens = self._commit(worktree, f"{task.ref}: {task.title}\n\nMade by the Handoff worker "
                                          f"({_on(agent, model, effort)}) from the task as the person approved "
                                          "it.\n")
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
                            used, fail, by=agent, took_over=[s for s in swaps if "took over" in s])

    def _chain(self, task: Task, history: list[Event], branch: str, return_to: str, target: dict | None,
               worktree: gitwork.Worktree, models: list[str], efforts: dict[str, str],
               fail: Callable[..., RunResult], fallbacks: list[str] | None = None) -> RunResult:
        """A chain of models, biggest first: models[0] reads the task and plans the steps, giving each to one of the
        others; then each step runs in turn, in the same worktree, and is committed on its own, so the branch shows
        which model made what. `efforts`: how hard each model thinks. The whole chain gets the run's time limit.
        If a step fails, the chain stops there: the steps before it stay committed, and the reason says what they
        did. `fallbacks` take a model's place when it's out of usage (the planner's only on an agent that can
        plan), and that model is benched for the rest of the chain."""
        agent, planner = self.spec.name, models[0]
        fallbacks = fallbacks or []
        if not self.spec.reports_usage:
            return fail(f"A chain of models runs on {' or '.join(CHAIN_AGENTS)}, not {agent}.")
        deadline = time.monotonic() + self.timeout
        notes: list[str] = []
        spent: list[dict | None] = []
        done_parts: list[str] = []
        commit: str | None = None
        tokens: list[str] = []

        def left_time() -> float:
            return max(deadline - time.monotonic(), 1.0)

        benched: set[tuple[str, str]] = set()  # out of usage: not tried again in this chain
        ran: dict[str, tuple[str, str, str | None]] = {}  # each model of the chain → who last ran in its place
        took: list[str] = []  # who took over from whom, for the run's summary

        def launch(prompt: str, model: str, plan: bool = False) -> tuple[str, str | None]:
            """Run one model (or, when it's out of usage, the next of the run's fallbacks); its reply, and what
            went wrong (None if nothing did)."""
            swaps: list[str] = []
            on, name = step_agent(model, agent)
            level = efforts.get(model)
            try:
                reply, used, problem, who, note = self._launch_or_fallback(
                    worktree, prompt, name, self._effort(level) if on == agent else level, fallbacks, benched, swaps,
                    left_time, plan=plan, agent=on)
            except subprocess.TimeoutExpired:
                return "", f"{model} was stopped: the chain ran past its {duration(self.timeout)}."
            ran[model] = who
            notes.extend(n for n in (" ".join(swaps), note) if n)
            took.extend(s for s in swaps if "took over" in s)
            spent.append(used)
            return reply, problem

        def stop(reason: str) -> RunResult:
            """The chain stops here: why, and what the steps before it did and where their work is."""
            if done_parts:
                kept = (f"What they changed is committed on {branch}, up to {commit[:12]}." if commit else
                        "They changed no files.")
                reason += "\n\n" + _clip(f"The steps before it finished. {kept}\n\n" + "\n\n".join(done_parts))
            token = _token_note(sorted(set(tokens))) if commit else None
            return fail(reason + (f"\n\n{token}" if token else ""), usage.merge(*spent), branch=branch,
                        commit=commit)

        reply, problem = launch(plan_prompt(self.spec, task, history, models, target, efforts), planner, plan=True)
        if problem:
            return stop(f"{planner} couldn't plan it: {problem}")
        steps = parse_plan(reply, models[1:])
        if steps is None:
            steps = [Step(models[1], "the whole task", "Do the whole task.")]
            notes.append(f"{planner} didn't write a plan the worker could read, so {models[1]} did the whole task.")
        verify_parts, left = [], ""
        mid = models[1]  # the biggest of the step models: it takes back a smaller one's step that failed, and checks
        lower: list[int] = []  # the steps a smaller model did, for the check
        for number, step in enumerate(steps, 1):
            before = len(spent)
            reply, problem = launch(step_prompt(self._spec_of(step.model), task, history, branch, target, planner,
                                                steps, number), step.model)
            if problem and step.model != mid and "was stopped" not in problem:
                # passed back up: the bigger model does the step the smaller one couldn't
                notes.append(f"{step.model} couldn't do step {number} ({_clip(problem)[:300]}), so {mid} did it.")
                step = Step(mid, step.title, step.instructions)
                steps[number - 1] = step
                reply, problem = launch(step_prompt(self._spec_of(mid), task, history, branch, target, planner,
                                                    steps, number), mid)
            if step.model != mid:
                lower.append(number)
            cost = usage.total_cost(usage.merge(*spent[before:]))
            by_agent, by_model, effort = ran.get(step.model, (agent, step.model, efforts.get(step.model)))
            try:
                made, found = self._commit(worktree, f"{task.ref} step {number}/{len(steps)}: {step.title}\n\nMade by "
                                           f"the Handoff worker ({_on(by_agent, by_model, effort)}), planned by "
                                           f"{ran.get(planner, (agent, planner, None))[1]}, from the task as the "
                                           "person approved it.\n")
            except _Withheld as exc:
                return stop(f"Step {number} of {len(steps)} ({step.model}: {step.title}): "
                            f"{problem + ' ' if problem else ''}{exc}")
            except gitwork.GitError as exc:
                return stop(f"Committing step {number} of {len(steps)} ({step.model}: {step.title}) failed: {exc}")
            if problem:
                kept = f" What it changed is committed on {branch}." if made else ""
                commit, tokens = made or commit, tokens + found
                return stop(f"Step {number} of {len(steps)} ({step.model}: {step.title}) failed: {problem}{kept}")
            commit, tokens = made or commit, tokens + found
            step_done, step_left, step_verify = parse_reply(reply)
            shown_model = by_model if by_agent == agent else f"{by_agent} ({by_model})" if by_model else by_agent
            by = f"{shown_model}, {effort} effort" if effort else shown_model
            done_parts.append(f"Step {number} of {len(steps)}, by {by} (about ${cost:.4f}): {step.title}"
                              f"{'' if made else ' (changed no files)'}\n{step_done}")
            if step_verify and step_verify not in verify_parts:
                verify_parts.append(step_verify)
            left = step_left  # (the last step knows what's still left)
        # Back up the chain: the biggest step model checks what the smaller ones did and fixes it; what's too big
        # for it goes to the planner
        if lower:
            which = f"step{'s' if len(lower) > 1 else ''} {_and([str(n) for n in lower])}"
            plan_text = Reply()
            for n, st in enumerate(steps, 1):
                plan_text.quote(f"step {n} of {len(steps)}, for {st.model}: {st.title}", st.instructions)
            changes = Reply()
            changes.quote("what the chain changed (a diff)", _diff_text(self.root, worktree) or "(nothing)")
            before = len(spent)
            reply, problem = launch(build_prompt(self._spec_of(mid), task, history, branch, target)
                                    + CHECK_BRIEF.format(planner=planner, count=len(steps), which=which) + "\n\n"
                                    + plan_text.render() + "\n\n" + changes.render(), mid)
            made, problem2 = self._chain_commit(worktree, f"{task.ref} check: {mid} checked {which}", tokens)
            if problem2:
                return stop(f"The check by {mid}: {problem2}")
            commit = made or commit
            if problem or problem2:
                notes.append(f"{mid} couldn't check the smaller models' work: {_clip(problem or problem2)[:300]}")
            else:
                cost = usage.total_cost(usage.merge(*spent[before:]))
                check_done = parse_reply(reply)[0]
                done_parts.append(f"Check, by {mid} (about ${cost:.4f}): {which}"
                                  f"{'' if made else ' (changed no files)'}\n{check_done}")
                big = escalation(reply)
                if big:
                    changes = Reply()
                    changes.quote("what the chain changed (a diff)", _diff_text(self.root, worktree) or "(nothing)")
                    before = len(spent)
                    reply, problem = launch(build_prompt(self.spec, task, history, branch, target)
                                            + FIX_BRIEF.format(problem=big) + "\n\n" + changes.render(), planner)
                    made, problem2 = self._chain_commit(worktree, f"{task.ref} fix: {planner} fixed what the check "
                                                        "sent up", tokens)
                    if problem2:
                        return stop(f"The fix by {planner}: {problem2}")
                    commit = made or commit
                    cost = usage.total_cost(usage.merge(*spent[before:]))
                    if problem or problem2:
                        notes.append(f"The check sent up a problem ({big}), and {planner} couldn't fix it: "
                                     f"{_clip(problem or problem2)[:300]}")
                    else:
                        done_parts.append(f"Fix, by {planner} (about ${cost:.4f}): {big}\n{parse_reply(reply)[0]}")
        try:
            files = gitwork.changed_files(self.root, worktree)
        except gitwork.GitError as exc:
            return stop(f"Reading what the chain changed failed: {exc}")
        planned_by = ran.get(planner, (agent, planner, None))[1] or planner
        done = _clip(f"{planned_by} planned {len(steps)} step{'s' if len(steps) > 1 else ''}.\n\n"
                     + "\n\n".join(done_parts))
        if commit is None:
            done = "No files were changed.\n\n" + done
        token = _token_note(sorted(set(tokens))) if commit else None
        verify = _clip("\n\n".join(verify_parts))
        return self._finish(task, return_to, branch, worktree, commit, files, done, left, verify, token,
                            "\n\n".join(notes) or None, usage.merge(*spent), fail, took_over=took)

    def _spec_of(self, model: str) -> AgentSpec:
        """The agent spec a chain's model runs on (its brief says what that agent can do)."""
        on = step_agent(model, self.spec.name)[0]
        return self.spec if on == self.spec.name else AGENTS[on]

    def _chain_commit(self, worktree: gitwork.Worktree, title: str, tokens: list[str]) -> tuple[str | None, str | None]:
        """Commit a check's or a fix's changes: the commit (None if nothing changed) and what went wrong."""
        try:
            made, found = self._commit(worktree, f"{title}\n\nMade by the Handoff worker, from the task as the "
                                       "person approved it.\n")
        except (_Withheld, gitwork.GitError) as exc:
            return None, str(exc)
        tokens.extend(found)
        return made, None

    def _launch_or_fallback(self, worktree: gitwork.Worktree, prompt: str, model: str | None, effort: str | None,
                            fallbacks: list[str], benched: set[tuple[str, str]], swaps: list[str],
                            time_left: Callable[[], float], plan: bool = False, agent: str | None = None,
                            ) -> tuple[str, dict | None, str | None, tuple[str, str, str | None], str | None]:
        """Run `model` on this agent; when it's out of usage (limits.py), bench it and run the next of the run's
        `fallbacks` ("agent:model", sealed in its approval) that isn't benched, and so on: a model that hits its
        plan's limit doesn't stop the work. Returns the reply, the usage of every try, what went wrong (None if
        nothing did), who ran it (agent, model, effort) and a login note; `swaps` gets a sentence for each
        model that was out of usage. Any other failure of the run's own model stops there: it needs fixing, and
        the next model working would hide it (a fallback that fails is passed by, and said). TimeoutExpired is
        raised as it comes."""
        from handoff.rankings import split
        tries = [(agent or self.spec.name, model or "", effort)] + [(*split(entry), None) for entry in fallbacks]
        spent: list[dict | None] = []
        out: list[str] = []  # the models found out of usage, in words
        first = True
        for agent, name, level in tries:
            if (agent, name) in benched or (plan and agent not in CHAIN_AGENTS):
                continue
            label = name or agent
            try:
                worker = self if agent == self.spec.name else self._helper(agent)
                proc, note = worker._launch(worktree, prompt, name or None, plan=plan, timeout=time_left(),
                                            effort=worker._effort(level))
            except (OSError, WorkerUnavailable) as exc:
                if first:
                    return "", usage.merge(*spent), f"Couldn't start {agent}{f' on {name}' if name else ''}: {exc}", \
                        (agent, name, level), None
                swaps.append(f"{label} couldn't take over ({clean_line(str(exc))[:200]}).")
                continue
            first = False
            reply, used, error = worker._read(proc)
            spent.append(used)
            if proc.returncode == 0 and not error:
                if out:
                    swaps.append(f"{_and(out)} hit {'its' if len(out) == 1 else 'their'} usage limit, so {label} "
                                 "took over.")
                return reply, usage.merge(*spent), None, (agent, name, level), note
            said = error or proc.stderr or proc.stdout or ""
            if fallbacks and limits.out_of_usage(said[-2000:]):
                benched.add((agent, name))
                out.append(label)
                continue
            if out and (agent, name) != tries[0][:2]:  # a fallback that failed another way: the next one may not
                swaps.append(f"{label} couldn't take over ({clean_line(self._exit_problem(proc, error))[:200]}).")
                continue
            return reply, usage.merge(*spent), self._exit_problem(proc, error), (agent, name, level), note
        why = " ".join(swaps[-5:])  # (who couldn't take over, and why)
        if out:
            return "", usage.merge(*spent), (f"{_and(out)} hit {'its' if len(out) == 1 else 'their'} usage limit, and "
                                             f"there was no other model to take over. {why}").strip(), tries[0], None
        return "", usage.merge(*spent), f"No model could run it: every one is out of usage. {why}".strip(), \
            tries[0], None

    def _helper(self, agent: str) -> "Worker":
        """Another agent's worker, for a fallback the approval names (checked once per run)."""
        if agent not in self._helpers:
            helper = Worker(self.board, self.root, agent, self.runner, self.timeout, share=self.share)
            helper.check()
            helper.check_login()  # (one you haven't enabled doesn't take over either)
            self._helpers[agent] = helper
        return self._helpers[agent]

    def _read(self, proc: "subprocess.CompletedProcess[str]") -> tuple[str, dict | None, str | None]:
        """The agent's reply, what it used and what went wrong, from its output."""
        if self.spec.reports_usage:
            return usage.parse_claude(proc.stdout or "")
        if self.spec.output == "opencode":
            return usage.parse_opencode(proc.stdout or "", self._model_named(proc))
        if self.spec.output == "hermes":
            return usage.parse_hermes(proc.stdout or "", getattr(proc, "usage_report", None))
        if self.spec.output == "local":
            return usage.parse_local(proc.stdout or "")
        return proc.stdout or "", None, None

    def _model_named(self, proc: "subprocess.CompletedProcess[str]") -> str:
        """The model on the agent's command line (what usage is recorded under), or the agent's name."""
        args = list(proc.args) if isinstance(proc.args, (list, tuple)) else []
        flag = self.spec.model_flag
        if flag and flag in args and args.index(flag) + 1 < len(args):
            return str(args[args.index(flag) + 1])
        return self.spec.name

    def _commit(self, worktree: gitwork.Worktree, message: str) -> tuple[str | None, list[str]]:
        """Commit what the agent changed: the new commit (None if nothing changed) and the files holding only a JSON
        Web Token (committed, and said: it's often a sample). _Withheld if anything looks like a secret."""
        staged = gitwork.stage_all(self.root, worktree)
        tokens: list[str] = []
        found = gitwork.secrets_in(self.root, worktree, staged, tokens)
        if found:
            gitwork.unstage_all(self.root, worktree)
            where = ", ".join(f"{path[:200]} ({what})" for path, what in found[:5])
            raise _Withheld(f"The changes include what looks like a secret: {where}. Nothing was committed; the "
                            f"files are in {worktree.path.relative_to(self.root).as_posix()}.")
        return gitwork.commit_staged(self.root, worktree, message), tokens

    def _finish(self, task: Task, return_to: str, branch: str, worktree: gitwork.Worktree, commit: str | None,
                files: list[str], done: str, left: str, verify: str, token: str | None, login_note: str | None,
                used: dict | None, fail: Callable[..., RunResult], by: str | None = None,
                took_over: list[str] | None = None) -> RunResult:
        """`by`: the agent that did the work, when a fallback of another agent took over (its note is the one that
        holds); `took_over`: what the run's summary says about models that were out of usage."""
        agent = self.spec.name
        doer = self._helpers[by] if by and by != agent and by in self._helpers else self
        fallback = (f"Its sandbox can't run here, so {agent} ran edit-only; `handoff doctor` says what it needs."
                    if self.fallback_reason and doer is self else None)
        shared = (f"You shared these folders with it, read-only: {', '.join(_home_relative(p) for p in self.shared)}."
                  if self.shared and self.spec.sandboxed else None)
        spent = ("Used: " + "; ".join(usage.lines(used)) + " (estimated at API prices)") if used else None
        notes = "\n\n".join(n for n in (token, doer.spec.note, shared, fallback, login_note, spent) if n)
        left = f"{left}\n\n{notes}" if left else notes
        try:
            finished = self.board.finish_run(agent, task.id, return_to, done, left, verify, files, branch,
                                             extra={"base": worktree.base, "commit": commit, "usage": used})
        except BoardError as exc:  # (it can't stay in progress: nobody is running it now)
            return fail(f"The board didn't take the result: {exc} The work is on {branch}.", used, branch=branch,
                        commit=commit)
        said = ". ".join([f"handed to {return_to}", *(t.rstrip(".") for t in took_over or [])]) + \
            (f". {token}" if token else "")
        return RunResult(finished, True, said, branch, commit, used=used)

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

