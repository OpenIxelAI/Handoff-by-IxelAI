"""Runs through Ixel MAT: an answer, a review or pictures, by any model you set up in Ixel.

Edits are the worker's (worker.py): claude or codex in their own worktree. Everything else goes to
Ixel MAT, which holds your API keys and talks to the models:

- answer  `ixel ask --agent NAME --json -`: one model replies ("gemini, make me a list of projects").
          Files the task names are attached, read-only.
- review  the same, with your changes attached: uncommitted ones, else this branch since it left
          main, else your last commit (gitwork.review_target), and new files git doesn't track yet
          (`--new-files`: Ixel MAT reads them, and leaves out links, key files and the like).
- image   `ixel image --provider xai|openai --json`: pictures, saved in .handoff/outputs/T-N. Asked
          to base them on your work, Ixel's chat model reads the README (or the files named) first.

Like the panel (ixel.py), the task's text only ever goes on stdin; the command line is fixed apart
from names Handoff checked (an agent's board name, a provider, a branch, relative file paths), and
Ixel is started in the project folder. Ixel's output is other models' words: the board keeps it
sanitized and clipped, and the full answer goes to .handoff/outputs/T-N/answer.md.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from handoff import approvals, gitwork
from handoff.board import HUMAN, Board, BoardError, Event, Task, run_kind, safe_name
from handoff.fence import numbered
from handoff.ixel import child_env, find_ixel
from handoff.project import BOARD_DIR
from handoff.proc import Stopped, run_tree, stopping
from handoff.sanitize import clean_line, clean_text, find_secret

VIA_IXEL = ("answer", "review", "image")
OUTPUTS = Path(BOARD_DIR) / "outputs"
TIMEOUT_SEC = {"answer": 900.0, "review": 900.0, "image": 600.0}
LIST_TIMEOUT_SEC = 60.0
MAX_QUESTION_CHARS = 45_000
MAX_SECTION_BYTES = 6_000
MAX_FILES = 5
MAX_PICTURES = 4

# "grok, make pictures" means xAI's image model; "gpt" or "chatgpt", OpenAI's
IMAGE_NAMES = {"grok": "xai", "xai": "xai", "gpt": "openai", "chatgpt": "openai", "openai": "openai",
               "dalle": "openai"}
# "based on my work": the README is the work's own description
_ABOUT_THE_WORK = re.compile(r"\b(my|our|this|the) (work|project|app|game|site|website|code|repo|product|tool)\b"
                             r"|\bbased on\b|\bfrom my\b|\bof my\b", re.I)
_FILE_WORD = re.compile(r"(?<![\w/.-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,8})(?![\w/-])")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_NUMBER_WORDS = {"one": 1, "a": 1, "an": 1, "two": 2, "a couple": 2, "couple": 2, "three": 3, "four": 4,
                 "a few": 3, "few": 3, "some": 2}
_PICTURE = r"(?:photos?|pictures?|pics|images?|illustrations?|drawings?|paintings?|logos?|wallpapers?|icons?|" \
           r"thumbnails?|banners?|posters?|mockups?|renders?|artworks?|art)"

Runner = Callable[[list[str], str, dict, float, str], "subprocess.CompletedProcess[str]"]

# How a dispatched task's body starts: the whole request follows, for context, so the files it names
# belong to the other parts, not this one
DISPATCH_BODY = "'s part of a request the person split with `handoff dispatch`."


# What a run stopped with Ctrl+C records, and says when it never started
STOPPED = "Stopped by you (Ctrl+C) before it finished."
NOT_STARTED = "Stopped by you (Ctrl+C) before it started."


class RunProblem(Exception):
    """The run can't start; the message says why (nothing was sent to a model)."""


def _run(command: list[str], stdin: str, env: dict, timeout: float, cwd: str) -> "subprocess.CompletedProcess[str]":
    return run_tree(command, stdin, env, timeout, cwd=cwd)


@dataclass
class IxelRoster:
    agents: list[dict]       # `ixel ask --list`: name, label, model, ready, images
    providers: list[dict]    # `ixel image --list`: name, label, model, ready, why
    problem: str | None = None
    features: tuple[str, ...] = ()  # what this Ixel's `ask` can do: "new-files"


def _json_out(runner: Runner, command: list[str], root: Path, timeout: float) -> dict | None:
    try:
        proc = runner(command, "", child_env(), timeout, str(root))
        data = json.loads(proc.stdout)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None
    return data if isinstance(data, dict) else None


def ixel_roster(root: Path, runner: Runner = _run) -> IxelRoster:
    """Who Ixel can ask, and which picture services have a key. Nothing is sent to a model."""
    ixel = find_ixel()
    if ixel is None:
        return IxelRoster([], [], "Ixel MAT isn't installed (no `ixel` command), so only claude and codex can "
                                  "take tasks, as edits. Install Ixel MAT to give answers, reviews and pictures to "
                                  "any model.")
    listed = _json_out(runner, [ixel, "ask", "--list", "--json"], root, LIST_TIMEOUT_SEC)
    if listed is None or not isinstance(listed.get("agents"), list):
        return IxelRoster([], [], "This Ixel MAT can't take single tasks yet (`ixel ask` is missing). Update it "
                                  "with: ixel update")
    pictures = _json_out(runner, [ixel, "image", "--list", "--json"], root, LIST_TIMEOUT_SEC) or {}
    agents = [a for a in listed["agents"] if isinstance(a, dict) and isinstance(a.get("name"), str)]
    providers = [p for p in pictures.get("providers") or [] if isinstance(p, dict) and isinstance(p.get("name"), str)]
    features = tuple(f for f in listed.get("features") or [] if isinstance(f, str))
    return IxelRoster(agents, providers, features=features)


# ── What a run reads ─────────────────────────────────────────────────────────

def _inside(root: Path, name: str) -> str | None:
    """`name` as a path relative to the project, if it's a file there that's safe to name to Ixel."""
    name = name.strip().strip("`'\".,;:()").replace("\\", "/")
    if not name or not _SAFE_PATH.match(name) or ".." in name.split("/") or name.startswith(BOARD_DIR):
        return None
    try:
        path = (root / name).resolve()
        path.relative_to(root.resolve())
        return name if path.is_file() else None
    except (OSError, ValueError):  # outside the project, or a name too long for the system
        return None


def named_files(text: str, root: Path) -> list[str]:
    """Files in the project that the text names (README.md, docs/plan.md…), at most MAX_FILES."""
    found: list[str] = []
    for match in _FILE_WORD.finditer(text):
        name = _inside(root, match.group(1))
        if name and name not in found:
            found.append(name)
    return found[:MAX_FILES]


def readme(root: Path) -> str | None:
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        if _inside(root, name):
            return name
    return None


def picture_files(text: str, root: Path) -> list[str]:
    """What pictures are based on: the files named, else the README when it says "my work" and the like."""
    named = named_files(text, root)
    if named or not _ABOUT_THE_WORK.search(text):
        return named
    found = readme(root)
    if found is None:
        raise RunProblem("there's no README to base the pictures on. Name the files to use, like "
                         "“grok, make pictures from docs/intro.md”.")
    return [found]


def picture_count(text: str) -> int:
    """How many pictures the text asks for: a number before the word, else two for a plural, else one."""
    match = re.search(rf"\b(\d{{1,2}}|a couple|couple|a few|few|one|two|three|four|some|an?)\s+(?:\w+\s+)?"
                      rf"({_PICTURE})\b", text, re.I)
    if match:
        word = match.group(1).lower()
        count = int(word) if word.isdigit() else _NUMBER_WORDS.get(word, 1)
        return max(1, min(count, MAX_PICTURES))
    plural = re.search(rf"\b{_PICTURE}\b", text, re.I)
    return 2 if plural and plural.group(0).lower().endswith("s") else 1


def image_provider(agent: str, roster: IxelRoster) -> str | None:
    """The picture service an agent stands for: Grok is xAI, GPT is OpenAI."""
    for item in roster.agents:
        if safe_name(item.get("name")) == agent and item.get("images") in ("xai", "openai"):
            return item["images"]
    return IMAGE_NAMES.get(agent)


def picture_writer(agent: str, roster: IxelRoster) -> dict | None:
    """Who reads the files a picture is based on, and describes it: the agent named, when it's one of your
    chat models in Ixel MAT, else one at the picture service's company (Grok for xAI), else your first."""
    ready = [a for a in roster.agents if a.get("ready") and safe_name(a.get("name"))]
    provider = image_provider(agent, roster)
    for found in ([a for a in ready if safe_name(a.get("name")) == agent],
                  [a for a in ready if a.get("images") == provider], ready):
        if found:
            return found[0]
    return None


def writer_label(writer: dict) -> str:
    return clean_line(writer.get("label") or writer.get("name"))[:40] or str(writer.get("name"))


def unrunnable(agent: str, kind: str, roster: IxelRoster) -> str | None:
    """Why an answer, review or picture run by `agent` can't happen through this Ixel MAT, if it can't."""
    if roster.problem:
        return roster.problem
    if kind == "image":
        if image_provider(agent, roster) is None:
            return f"{agent} can't make pictures through Ixel MAT; xAI (grok) and OpenAI (gpt) can."
        return None
    names = [n for n in (safe_name(a.get("name")) for a in roster.agents) if n]
    if agent not in names:
        return (f"There's no model called {agent} in your Ixel MAT. You have: {', '.join(names) or 'none yet'}. "
                "Add one with: ixel setup")
    return None


def review_plan(root: Path, roster: IxelRoster | None = None) -> tuple[list[str], str]:
    """What a review reads, as `ixel ask` flags and in words: your uncommitted changes, else this branch since
    it left main, else your last commit, with new files git doesn't track yet when this Ixel MAT can read
    them (`--new-files`; it leaves out links, key files and the like). RunProblem if there's nothing."""
    try:
        new = bool(gitwork.untracked_files(root))  # first: if git refuses this folder, its reason is the problem
        target = gitwork.review_target(root)
    except gitwork.GitError as exc:
        raise RunProblem(str(exc)) from exc
    if new and roster is not None and "new-files" in roster.features:
        if target and target[0] == ["--diff"]:
            return ["--diff", "--new-files"], "your uncommitted changes and new files"
        if target and target[0][:1] == ["--base"] and target[0][1] != "HEAD~1":
            return [*target[0], "--new-files"], f"{target[1]}, and your new files"
        return ["--diff", "--new-files"], "your new files"
    if target is None:
        if new:
            raise RunProblem("the only changes are new files git doesn't track yet, and this Ixel MAT can't review "
                             "those. Update it with: ixel update (or `git add` them)")
        raise RunProblem("there's nothing to review: no changes since the last commit, no new files, no commits "
                         "on this branch beyond main, and no earlier commit.")
    if new:
        return target[0], f"{target[1]} (not your new files: this Ixel MAT can't read them; update it with: ixel update)"
    return target


def review_words(root: Path, roster: IxelRoster | None = None) -> str:
    """What a review would read now, in words (RunProblem if there's nothing to review)."""
    return review_plan(root, roster)[1]


# ── The run ──────────────────────────────────────────────────────────────────

def _task_text(task: Task) -> str:
    parts = [f"Task {task.ref}: {task.title}"]
    if task.body:
        parts += ["", task.body]
    if task.acceptance:
        parts += ["", "Checks it should meet:", numbered(task.acceptance)]
    return "\n".join(parts)


ANSWER_BRIEF = ("This is a task from the person's Handoff board, and it's yours to answer. Reply with the result "
                "itself (the list, the plan, the explanation): the person reads your reply on the board. You "
                "can't see or change their project beyond what's attached, so if you'd need something that isn't "
                "here, say what.")
REVIEW_BRIEF = ("This is a review from the person's Handoff board. The change is attached ({what}). Find bugs, "
                "security problems and anything that would break, and say what should change, most important "
                "first. If it looks right, say so.")
FILES_REVIEW_BRIEF = ("This is a review from the person's Handoff board. The files to review are attached ({what}). "
                      "Find bugs, security problems, mistakes and anything unclear, and say what should change, "
                      "most important first. If they look right, say so.")


def _clip(text: str) -> str:
    """Sanitized, secret-free, and at most MAX_SECTION_BYTES, so it fits one handoff."""
    text = clean_text(text)
    if find_secret(text):
        return "[withheld: it looked like it contained a secret]"
    data = text.encode("utf-8")
    return text if len(data) <= MAX_SECTION_BYTES else data[:MAX_SECTION_BYTES].decode("utf-8", "ignore") + " [… cut]"


def target_plan(target: dict, roster: IxelRoster | None) -> tuple[list[str], str]:
    """A review of two given commits (a pull request fetched into the project), as `ixel ask` flags and in
    words: what's committed on `head` since it left `base`. RunProblem if this Ixel MAT can't read that."""
    if roster is None or "head" not in roster.features:
        raise RunProblem("this Ixel MAT can't review a pull request it hasn't checked out. Update it with: "
                         "ixel update")
    return ["--base", target["base"], "--head", target["head"]], clean_line(target["label"])


def command_for(ixel: str, root: Path, task: Task, kind: str, agent: str, roster: IxelRoster | None,
                outputs: Path, target: dict | None = None) -> tuple[list[str], str, str]:
    """The ixel command line, what goes on stdin, and what the run reads (a sentence for the handback).
    `target`: a review's two commits, sealed in its approval (approvals.check_target)."""
    # The files the task names: in its title, or its body too unless the body is the whole request it was
    # split from (`handoff dispatch`), which names other agents' files
    own = task.title if DISPATCH_BODY in task.body.split("\n", 1)[0] else f"{task.title}\n{task.body}"
    if kind == "answer":
        files = named_files(own, root)
        stdin = f"{ANSWER_BRIEF}\n\n{_task_text(task)}"
        argv = [ixel, "ask", "--agent", agent, "--json"]
        for name in files:
            argv += ["-f", name]
        return argv + ["-"], stdin[:MAX_QUESTION_CHARS], (f"It read {', '.join(files)}." if files else "")
    if kind == "review":
        files = named_files(own, root) if target is None else []  # "review README.md": that file
        if target is not None:
            flags, what = target_plan(target, roster)
        elif files:
            flags, what = [arg for name in files for arg in ("-f", name)], ", ".join(files)
        else:
            flags, what = review_plan(root, roster)
        stdin = f"{(FILES_REVIEW_BRIEF if files else REVIEW_BRIEF).format(what=what)}\n\n{_task_text(task)}"
        return [ixel, "ask", "--agent", agent, "--json", *flags, "-"], stdin[:MAX_QUESTION_CHARS], f"It reviewed {what}."
    if kind == "image":
        provider = image_provider(agent, roster or IxelRoster([], []))
        if provider is None:
            raise RunProblem(f"{agent} can't make pictures through Ixel; xAI (grok) and OpenAI (gpt) can.")
        files = picture_files(task.title, root)
        argv = [ixel, "image", "--provider", provider, "--json", "-n", str(picture_count(task.title)),
                "--out", outputs.as_posix(), "--name", task.ref]
        for name in files:
            argv += ["-f", name]
        writer = picture_writer(agent, roster or IxelRoster([], [])) if files else None
        if writer:  # the one the plan named, whatever Ixel's own default would be
            argv += ["--writer", safe_name(writer.get("name")) or ""]
        return argv + ["-"], task.title, (f"Based on {', '.join(files)}." if files else "")
    raise RunProblem(f"Handoff doesn't know how to run a {kind} task through Ixel.")


def run(board: Board, root: Path, task: Task, approval: Event, runner: Runner = _run,
        roster: IxelRoster | None = None):
    """Run one approved answer, review or image task through Ixel and hand the result back."""
    from handoff.worker import RunResult  # the worker imports this module

    agent = safe_name(approval.data.get("worker")) or ""
    kind = run_kind(approval)
    return_to = safe_name(approval.data.get("return_to")) or HUMAN
    if stopping():  # Ctrl+C came before this one started: its approval stays
        return RunResult(task, False, f"{NOT_STARTED} It's still approved; run it with: handoff run {task.ref}")
    try:
        task, _ = board.start_run(agent, task.id, approval.id, {"kind": kind})
    except BoardError as exc:  # it changed since it was approved, or another run took it
        return RunResult(task, False, str(exc))

    def fail(reason: str) -> RunResult:
        try:
            return RunResult(board.fail_run(agent, task.id, return_to, reason), False, reason)
        except BoardError as exc:
            return RunResult(task, False, f"{reason} ({exc})")

    try:
        return _started(board, root, task, approval, runner, roster, agent, kind, return_to, fail)
    except Stopped:
        return fail(STOPPED)
    except BaseException as exc:  # Ctrl+C, or a crash: never leave it in progress with nobody running it
        fail(STOPPED if isinstance(exc, KeyboardInterrupt) else f"The run crashed: {type(exc).__name__}.")
        raise


def _started(board: Board, root: Path, task: Task, approval: Event, runner: Runner, roster: IxelRoster | None,
             agent: str, kind: str, return_to: str, fail: Callable[[str], object]):
    from handoff.worker import RunResult

    ixel = find_ixel()
    if ixel is None:
        return fail("Ixel MAT isn't installed (no `ixel` command), so there's no model to ask.")
    outputs = OUTPUTS / task.ref
    target = None
    if "target" in approval.data:  # (sealed with it: the board only hands back approvals that check out)
        target = approvals.check_target(approval.data["target"])
        if target is None:
            return fail("The commits this review was approved for aren't readable.")
    try:
        if kind in ("image", "review") and roster is None:
            roster = ixel_roster(root, runner)
        command, stdin, reads = command_for(ixel, root, task, kind, agent, roster, outputs, target)
        output_folder(root, outputs)
    except RunProblem as exc:
        return fail(str(exc))
    except OSError as exc:
        return fail(f"Couldn't make {outputs.as_posix()}: {exc}")
    try:
        proc = runner(command, stdin, child_env(), TIMEOUT_SEC[kind], str(root))
    except subprocess.TimeoutExpired:
        return fail(f"Ixel was stopped after {int(TIMEOUT_SEC[kind] // 60)} minutes.")
    except OSError as exc:
        return fail(f"Couldn't run ixel: {exc}")
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError):
        lines = (proc.stderr or "").strip().splitlines()
        why = clean_line(lines[-1])[:300] if lines else f"exit code {proc.returncode}"
        return fail(f"Ixel didn't return a result ({why}).")
    if not isinstance(data, dict):
        return fail("Ixel returned something Handoff doesn't understand.")
    if data.get("error") or proc.returncode != 0:
        return fail("Ixel: " + clean_line(data.get("error") or f"exit code {proc.returncode}")[:500])
    try:
        done, left, verify, files = (_pictures(data, root, outputs, reads) if kind == "image"
                                     else _answer(data, root, outputs, reads))
    except RunProblem as exc:
        return fail(str(exc))
    try:
        finished = board.finish_run(agent, task.id, return_to, done, left, verify, files)
    except BoardError as exc:  # (it can't stay in progress: nobody is running it now)
        return fail(f"The board didn't take the result: {exc} The result is in {outputs.as_posix()}.")
    return RunResult(finished, True, f"handed to {return_to}")


def output_folder(root: Path, outputs: Path) -> Path:
    """Make .handoff/outputs/T-N one folder at a time, refusing a link (or a junction) on the way: one
    already there, say from a cloned repository, would send Ixel's files outside the project."""
    here, expected = root, root.resolve()
    for part in outputs.parts:
        here, expected = here / part, expected / part
        if gitwork.is_link(here):
            raise RunProblem(f"{here.relative_to(root).as_posix()} is a link, so Handoff won't save anything "
                             "through it. Remove it and run again.")
        # before making anything: a link this Python can't see (a junction before 3.12) still shows here
        if os.path.normcase(here.resolve()) != os.path.normcase(expected):
            raise RunProblem(f"{here.relative_to(root).as_posix()} leads outside the project, so Handoff won't "
                             "save anything there.")
        here.mkdir(exist_ok=True)
    # Ixel never writes through a link (it makes each picture new), but a link planted in the task's
    # folder has no business there either
    try:
        planted = sorted(entry.name for entry in os.scandir(here) if gitwork.is_link(Path(entry.path)))
    except OSError:
        planted = []
    if planted:
        raise RunProblem(f"{outputs.as_posix()}/{clean_line(planted[0])[:120]} is a link, so Handoff won't run Ixel "
                         "there. Remove it and run again.")
    return here


def _replace(path: Path, text: str) -> None:
    """Write `path` as a new file moved into place, so a link already there is replaced, not followed."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _answer(data: dict, root: Path, outputs: Path, reads: str) -> tuple[str, str, str, list[str]]:
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise RunProblem("Ixel sent back an empty answer.")
    path = outputs / "answer.md"
    try:
        _replace(root / path, clean_text(answer) + "\n")
    except OSError as exc:
        raise RunProblem(f"Couldn't save the answer to {path.as_posix()}: {exc}") from exc
    who = clean_line(data.get("label") or data.get("agent") or "")[:80]
    model = clean_line(data.get("model") or "")[:80]
    done = _clip(answer)
    cut = done.endswith("[… cut]") or done.startswith("[withheld")
    # What Ixel left out of what it read (new files that hold keys, say), in its words
    notes = [_clip(clean_line(n)[:400]) for n in data.get("notes") or [] if isinstance(n, str) and n.strip()][:3]
    left = " ".join(n for n in (
        f"Answered by {who}{f' ({model})' if model else ''} through Ixel; nothing in the project changed." if who
        else "Answered through Ixel; nothing in the project changed.",
        reads, *notes,
        f"The full answer is in {path.as_posix()}." if cut else "") if n)
    return done, left, f"Read it here, or in {path.as_posix()}.", [path.as_posix()]


def _pictures(data: dict, root: Path, outputs: Path, reads: str) -> tuple[str, str, str, list[str]]:
    folder = (root / outputs).resolve()
    files = []
    for item in data.get("files") or []:
        try:
            path = Path(str(item))
            path = (path if path.is_absolute() else root / path).resolve()  # Ixel ran in the project folder
            name = path.relative_to(root.resolve()).as_posix()
            path.relative_to(folder)
        except (OSError, ValueError):
            raise RunProblem("Ixel saved the pictures somewhere other than the task's folder.") from None
        try:
            saved = path.is_file()
        except OSError:  # a name too long for the system, say
            saved = False
        if not saved:
            raise RunProblem(f"Ixel said it saved {clean_line(name)[:120]}, but there's no picture there.")
        files.append(name)
    if not files:
        raise RunProblem("Ixel sent back no pictures.")
    label = clean_line(data.get("label") or data.get("provider") or "")[:80]
    model = clean_line(data.get("model") or "")[:80]
    prompt = _clip(str(data.get("prompt") or ""))
    writer = data.get("writer") if isinstance(data.get("writer"), dict) else None
    lines = [f"Made {len(files)} picture{'s' if len(files) != 1 else ''} with {label}"
             f"{f' ({model})' if model else ''}:", *[f"- {f}" for f in files]]
    if prompt:
        described = f"{clean_line(writer.get('label'))[:80]} described it" if writer else "From the description"
        lines += ["", f"{described}: {prompt}"]
    return "\n".join(lines), reads, f"Open them in {outputs.as_posix()}.", files
