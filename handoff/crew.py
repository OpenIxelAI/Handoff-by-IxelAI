"""`handoff dispatch`: one request, split across your agents, run side by side.

    handoff dispatch "codex review my changes, grok make pictures of my app, claude finish the next step,
                      and gemini make me a list of projects to check out"

Each part that starts with an agent's name becomes a task for that agent, with a kind of run:

- edit    claude or codex change files, each in its own worktree and branch (the worker, worker.py)
- review  a model reads your changes and says what's wrong (Ixel MAT)
- image   pictures from xAI (grok) or OpenAI (gpt) (Ixel MAT)
- answer  everything else: a model replies (Ixel MAT)

The split is done by plain rules, not a model, so it's predictable: you see every part, who gets
it and what kind of run it is before anything is added, and nothing runs until you say yes. Then
each task is approved for exactly that run (sealed, approvals.py), and they all run at once.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from handoff import asks
from handoff.board import HUMAN, MAX_TITLE_CHARS, Board, BoardError, Event, Task, check_name, run_kind
from handoff.sanitize import clean_line, clean_text

MAX_PARTS = 12
MAX_REQUEST_CHARS = 4_000

# Names people use for agents that aren't set up here yet, so "deepseek, do X" is a part with a
# problem ("not set up") rather than words glued onto the part before it
KNOWN_NAMES = ("claude", "codex", "gemini", "grok", "opencode", "gpt", "chatgpt", "openai", "xai", "deepseek",
               "llama", "mistral", "qwen", "kimi", "copilot", "cursor", "perplexity", "openclaw", "hermes", "ollama")
LEAD_WORDS = frozenset({"handoff", "all", "everyone", "everybody", "team", "guys", "folks", "hey", "ok", "okay", "so",
                        "please", "pls", "agents", "y'all", "yall"})
_JOINERS = frozenset({"and", "then", "also", "plus", "&", "while", "meanwhile"})
_FILLER = re.compile(
    r"^(?:[\s,:;.\-–—]+|(?:or|and) (?:whoever|anyone|anybody|someone|somebody)(?: else)?\b"
    r"|(?:hey|ok|okay|please|pls)\b|(?:i|we) (?:need|want|would like|'d like)(?: for)?(?: you)? to\b"
    r"|(?:can|could|would|will) you(?: please)?\b|you(?: should| can| could)?\b|to\b)+", re.I)

# A picture word that's part of something in code: "an image upload endpoint", "a logo component", "an
# image sitemap", "a docker image"
_CODE_NOUNS = (r"components?|uploads?|uploaders?|endpoints?|sitemaps?|tags?|files?|builds?|paths?|urls?|fields?|"
               r"types?|formats?|handlers?|loaders?|processing|processors?|resizer|resizing|compression|cache|"
               r"caching|gallery|galleries|picker|carousel|slider|viewer|editor|library|service|apis?|routes?|"
               r"storage|buckets?|cdn|tests?|elements?|class|classes|props?|modules?|widgets?|registry|pipeline|"
               r"layers?|sizes?|width|height|alt|src|crop\w*|optimi[sz]\w*")
_NOT_CODE_AFTER = rf"(?!\s+(?:{_CODE_NOUNS})\b)"
_CODE_BEFORE = r"docker|container|disk|vm|iso|boot|os|system|base|ami|vagrant|qemu|kernel"
# A picture to make: a make word, then a few words (not "the", "my"…: those are things that exist), then a
# picture word ("make 2 pictures of my app", "draw a logo", "create an image of"). "fix the image upload"
# and "build the docker image" aren't, and neither are the code ones above.
_MAKE_PICTURE = re.compile(r"\b(?:make|create|generate|draw|design|paint|illustrate|sketch|produce)\s+"
                           r"(?:(?!(?:the|my|our|this|that|these|those|its|their|your|"
                           rf"{_CODE_BEFORE})\b)[\w'-]+\s+){{0,3}}?{asks._PICTURE}\b{_NOT_CODE_AFTER}", re.I)
# Words that mean a picture on their own, when nothing in the part asks for an edit or a review
_PICTURE_WORD = re.compile(r"\b(?:photos?|pictures?|pics|illustrations?|drawings?|paintings?|wallpapers?|artworks?|"
                           rf"logos?|posters?|mockups?|draw|paint|illustrate|sketch)\b{_NOT_CODE_AFTER}", re.I)
_EDIT = re.compile(r"\b(finish|fix|implement|build|add|change|update|refactor|rename|remove|delete|migrate|"
                   r"continue|write (?:the |some |a |an )?(?:code|tests?|functions?|docs?|script)|next step|"
                   r"clean up|set up|wire up|make the change|make it work|code (?:it|this|that)|"
                   # making something in code: "create an image upload endpoint", "make a logo component"
                   # (but "make a list of the files that…" is an answer about it)
                   r"(?:make|create|generate|write) (?!(?:me |us )?(?:a |an |the )?(?:list|summary|table|"
                   r"overview|report|rundown|count|outline|breakdown|description)s?\s+(?:of|on|about)\b)"
                   r"(?:[\w'-]+ ){0,3}?(?:" + _CODE_NOUNS +
                   r"|docker ?files?|docker images?|containers?|functions?|scripts?|pages?|migrations?|"
                   r"hooks?|schemas?|models?|views?|controllers?))\b", re.I)
# With a review word, only these make it an edit ("review and fix it", not "review the update")
_STRONG_EDIT = re.compile(r"\b(fix|implement|finish|refactor|rename|migrate|write (?:the |some |a |an )?code|"
                          r"next step|make the change)\b", re.I)
_REVIEW = re.compile(r"\b(review|code review|look over|go over|audit|critique|double[- ]check|check (?:my|the|our|"
                     r"this) (?:work|changes?|code|diff|pr|branch))\b", re.I)

KIND_WORDS = {"edit": "changes files", "review": "reviews", "image": "makes pictures", "answer": "answers"}


@dataclass
class Member:
    name: str                                       # its name on the board
    label: str
    kinds: dict[str, str] = field(default_factory=dict)  # kind -> "" (ready) or why it can't
    notes: dict[str, str] = field(default_factory=dict)  # kind -> what to know (ran edit-only…)


@dataclass
class Roster:
    members: dict[str, Member]
    ixel: asks.IxelRoster
    problems: list[str] = field(default_factory=list)

    def keys(self) -> dict[str, str]:
        """What people call each member (its name, its label, the label's first word) -> its name."""
        found: dict[str, str] = {}
        for member in self.members.values():
            for word in _called(member):
                if word and len(word) >= 2:
                    found.setdefault(word, member.name)
        return found

    def ready_for(self, word: str, kind: str) -> Member | None:
        """The first member people may mean by `word` that can do `kind` here: for "claude review…", Claude
        Code in Ixel MAT, when the claude that edits can't review."""
        return next((m for m in self.members.values() if word in _called(m) and m.kinds.get(kind) == ""), None)


def _called(member: Member) -> list[str]:
    return [member.name, _key(member.label), *(_key(w) for w in member.label.split()[:1])]


def _key(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def board_name(name: object) -> str | None:
    try:
        return check_name(str(name).lower(), "name")
    except BoardError:
        return None


def build_roster(board: Board | None, root: Path, runner: asks.Runner = asks._run, check_edits: bool = True) -> Roster:
    """Who can take which kind of run, here, now. Checks the edit agents and asks Ixel who it knows;
    nothing is sent to a model, and nothing reads the board (a plan needs none)."""
    from handoff.worker import AGENTS, Worker, WorkerUnavailable

    members: dict[str, Member] = {}
    for name in AGENTS:
        member = members.setdefault(name, Member(name, name.capitalize()))
        if not check_edits:
            member.kinds["edit"] = ""
            continue
        try:
            worker = Worker(board, root, name)  # type: ignore[arg-type]  (only checked, never run)
            worker.check()
            member.kinds["edit"] = ""
            if worker.fallback_reason:
                member.notes["edit"] = f"edits without running commands here ({worker.fallback_reason})"
        except WorkerUnavailable as exc:
            member.kinds["edit"] = str(exc)

    ixel = asks.ixel_roster(root, runner)
    providers = {p["name"]: p for p in ixel.providers}
    for item in ixel.agents:
        name = board_name(item.get("name"))
        if name is None:
            continue
        label = clean_line(item.get("label") or name)[:40] or name
        member = members.setdefault(name, Member(name, label))
        if member.label == name.capitalize():
            member.label = label
        why = "" if item.get("ready") else "it has no API key yet (ixel setup adds one)"
        member.kinds["answer"] = member.kinds["review"] = why
    for name, member in members.items():
        provider = asks.image_provider(name, ixel)
        if provider is None:
            continue
        found = providers.get(provider)
        if found is None:
            member.kinds["image"] = "this Ixel can't make pictures yet (ixel update)" if ixel.agents else (
                ixel.problem or "Ixel MAT isn't set up")
        else:
            member.kinds["image"] = "" if found.get("ready") else clean_line(found.get("why") or "no key")
    # a picture service with a key but no chat model of the same company: "grok" still means it
    for provider, alias in (("xai", "grok"), ("openai", "gpt")):
        found = providers.get(provider)
        if found and found.get("ready") and not any(asks.image_provider(n, ixel) == provider for n in members):
            members[alias] = Member(alias, clean_line(found.get("label") or alias), {"image": ""})
    problems = [ixel.problem] if ixel.problem else []
    return Roster(members, ixel, problems)


# ── Splitting a request ──────────────────────────────────────────────────────

@dataclass
class Part:
    word: str          # the name as written
    text: str          # what was asked of it


def split(request: str, names: set[str]) -> tuple[str, list[Part]]:
    """The context before the first name, and one part per name that starts a sentence or a clause
    ("…, codex review", "… and gemini make…"). Names only count there, so "compare it with codex" stays
    inside its part."""
    request = clean_text(request)
    words = list(re.finditer(r"[A-Za-z0-9][\w'-]*|&", request))
    starts: list[tuple[int, int, str]] = []  # (where the part starts, where its text starts, name)
    leading = True
    for i, match in enumerate(words):
        word = match.group(0).lower()
        key = _key(word)
        before = request[words[i - 1].end():match.start()] if i else request[:match.start()]
        boundary = (i == 0 or leading or bool(re.search(r"[,;:.!?\n]", before))
                    or words[i - 1].group(0).lower() in _JOINERS)
        if key in names and boundary:
            start = words[i - 1].start() if i and words[i - 1].group(0).lower() in _JOINERS else match.start()
            starts.append((start, match.end(), key))
            leading = False
            continue
        if leading and word.lstrip("/") not in LEAD_WORDS:
            leading = False
    if not starts:
        return request.strip(), []
    context = request[:starts[0][0]].strip(" \t\n,;:.-")
    if all(w.lower().lstrip("/") in LEAD_WORDS for w in re.findall(r"[/\w'-]+", context)):
        context = ""
    parts = []
    for n, (_, text_start, key) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(request)
        text = _FILLER.sub("", request[text_start:end]).strip(" \t\n,;:.-–—")
        parts.append(Part(key, text))
    return context, parts[:MAX_PARTS]


def guess_kind(text: str, member: Member | None) -> str:
    can_edit = member is not None and "edit" in member.kinds
    review = _REVIEW.search(text)
    editing = (_STRONG_EDIT if review else _EDIT).search(text)
    if _MAKE_PICTURE.search(text) or not (review or editing) and _PICTURE_WORD.search(text):
        return "image"
    if can_edit and editing:
        return "edit"
    return "review" if review else "answer"


# ── The plan ─────────────────────────────────────────────────────────────────

@dataclass
class Step:
    agent: str           # the board name ("" if the name can't be one)
    label: str
    kind: str
    text: str            # what was asked
    problem: str = ""    # why it can't run ("" = it can)
    note: str = ""       # what to know before saying yes
    detail: str = ""     # what it reads

    @property
    def ok(self) -> bool:
        return not self.problem

    def title(self) -> str:
        text = self.text[:1].upper() + self.text[1:]
        if self.kind == "review" and len(self.text.split()) <= 3 and self.detail:
            text = f"Review {self.detail}"
        # cut as the board will store it, and not after a joiner, which then has nothing to join and shows
        return clean_line(text)[:MAX_TITLE_CHARS].rstrip(" \u200c\u200d")


def plan(request: str, roster: Roster, root: Path, return_to: str = HUMAN) -> tuple[str, list[Step]]:
    """The context and one step per part of the request (results go to `return_to`). Nothing is added to
    the board."""
    request = request.strip()[:MAX_REQUEST_CHARS]
    keys = roster.keys()
    context, parts = split(request, set(keys) | set(KNOWN_NAMES))
    steps = []
    for part in parts:
        name = keys.get(part.word)
        member = roster.members.get(name) if name else None
        kind = guess_kind(part.text, member)
        if member is not None and member.kinds.get(kind) != "":
            other = roster.ready_for(part.word, kind)
            if other is not None:
                name, member = other.name, other
        step = Step(agent=name or board_name(part.word) or "", label=member.label if member else part.word,
                    kind=kind, text=part.text)
        if not part.text:
            step.problem = f"nothing was asked of {part.word}."
        elif step.agent == return_to:
            step.problem = (f"its results would go back to {step.label}, which runs it. Send them to someone "
                            "else with --return-to (the default is you).")
        elif member is None:
            step.problem = (f"{part.word} isn't set up here. " + (roster.ixel.problem or
                            "Add it to Ixel MAT with: ixel setup"))
        elif kind not in member.kinds:
            step.problem = {
                "image": f"{member.label} can't make pictures through Ixel; xAI (grok) and OpenAI (gpt) can.",
                "edit": f"{member.label} can't change files through Handoff; claude and codex can.",
            }.get(kind) or (roster.ixel.problem or f"{member.label} isn't set up in Ixel MAT (ixel setup).")
        elif kind == "edit" and member.kinds["edit"] and member.kinds.get("answer") == "":
            # it can't change files on this computer, but it can say what to change
            step.kind = "answer"
            step.note = (f"{member.label} can't change files on this computer, so it answers with what to change "
                         f"({member.kinds['edit'].split('. ')[0].rstrip('.')})")
        else:
            step.problem = member.kinds[kind]
            step.note = member.notes.get(kind, "")
        kind = step.kind
        if not step.problem and kind == "answer" and _EDIT.search(part.text) and "edit" not in member.kinds:
            step.note = f"{member.label} can't change files, so it answers with what to change"
        if not step.problem:
            try:
                if kind == "review":  # the files it names, else your changes
                    files = asks.named_files(part.text, root)
                    step.detail = ", ".join(files) if files else asks.review_words(root, roster.ixel)
                elif kind == "image":
                    files = asks.picture_files(part.text, root)
                    count = asks.picture_count(part.text)
                    step.detail = f"{count} picture{'s' if count != 1 else ''}"
                    if files:  # another model may read them: say which, since they go to it
                        writer = asks.picture_writer(step.agent, roster.ixel)
                        if writer is None:
                            raise asks.RunProblem("A picture based on your files needs a chat model to read them "
                                                  "first. Add one with: ixel setup")
                        step.detail += f", based on {', '.join(files)}, which {asks.writer_label(writer)} reads"
                elif kind == "answer":
                    files = asks.named_files(part.text, root)
                    step.detail = f"reads {', '.join(files)}" if files else ""
            except asks.RunProblem as exc:
                step.problem = str(exc)
        steps.append(step)
    return context, steps


def task_body(request: str, step: Step) -> str:
    lines = [f"{step.label}{asks.DISPATCH_BODY} The whole request, for context (the other parts went to other "
             "agents):", "", request.strip()[:MAX_REQUEST_CHARS]]
    if step.kind == "edit":
        lines += ["", f"Do only this part: {step.text}"]
    return "\n".join(lines)


def add_and_approve(board: Board, request: str, steps: list[Step],
                    return_to: str = HUMAN) -> list[tuple[Task, Event]]:
    """Add a task per runnable step, assigned to its agent, and approve each for its one run. All or none:
    if one can't be added or approved, none of them stay (a worker would run one that did)."""
    added, made = [], []
    try:
        for step in steps:
            if not step.ok:
                continue
            task, _ = board.create(HUMAN, step.title(), task_body(request, step), assignee=step.agent)
            made.append(task.id)
            board.approve(HUMAN, task.id, step.agent, return_to, step.kind)
            approved = board.pending_runs(step.agent, [task.id])
            if approved:
                added.append(approved[0])
    except BaseException:  # Ctrl+C too
        for task_id in reversed(made):
            try:
                board.delete(HUMAN, task_id)
            except Exception:  # (the board went away, say): the error that stopped it says more
                pass
        raise
    return added


# ── Running side by side ─────────────────────────────────────────────────────

def run_one(board: Board, root: Path, task: Task, approval: Event, runner: asks.Runner = asks._run,
            roster: asks.IxelRoster | None = None, timeout_sec: float | None = None):
    """One approved run, whatever its kind."""
    from handoff.worker import DEFAULT_TIMEOUT_MIN, RunResult, Worker, WorkerUnavailable
    if run_kind(approval) == "edit":
        agent = task.assignee or ""
        try:
            worker = Worker(board, root, agent, timeout_sec=timeout_sec or DEFAULT_TIMEOUT_MIN * 60)
            worker.check()
        except WorkerUnavailable as exc:  # nothing started: the approval stays for when it can run
            return RunResult(task, False, f"{exc} It's still approved; run it later with: handoff run {task.ref}")
        return worker.run(task, approval)
    if run_kind(approval) not in asks.VIA_IXEL:
        return RunResult(task, False, "This approval is for a kind of run this version of Handoff doesn't know. "
                                      "Update Handoff (handoff update).")
    return asks.run(board, root, task, approval, runner, roster)


def run_all(board: Board, root: Path, pairs: list[tuple[Task, Event]], runner: asks.Runner = asks._run,
            roster: asks.IxelRoster | None = None, on_start: Callable[[Task, str], None] | None = None,
            on_done: Callable[[object], None] | None = None) -> list:
    """Run every approved task at once, each on its own thread. Ctrl+C stops them all, and each records that."""
    from handoff import proc
    from handoff.worker import RunResult

    proc.allow_runs()  # (an earlier set of runs may have been stopped)
    results: list = [None] * len(pairs)
    finished: list[threading.Event] = []
    lock = threading.Lock()

    def go(n: int, task: Task, approval: Event) -> None:
        try:
            try:
                result = run_one(board, root, task, approval, runner, roster)
            except Exception as exc:  # noqa: BLE001 — one run's crash shouldn't take the others down
                result = RunResult(task, False, f"The run crashed: {type(exc).__name__}: {exc}")
            results[n] = result
            if on_done:
                with lock:
                    on_done(result)
        finally:
            finished[n].set()

    try:
        for n, (task, approval) in enumerate(pairs):
            if on_start:
                on_start(task, run_kind(approval))
            finished.append(threading.Event())
            threading.Thread(target=go, args=(n, task, approval), daemon=True).start()
        for done in finished:
            while not done.wait(0.2):
                pass
    except KeyboardInterrupt:
        proc.stop_all()  # each run sees its program stop, or not start, and records that on the board
        # Wait on events, not Thread.join: a join cut short by Ctrl+C can wrongly take its thread as finished
        deadline = time.monotonic() + 30
        for done in finished:
            while not done.is_set() and time.monotonic() < deadline:
                try:
                    done.wait(max(0.0, min(0.2, deadline - time.monotonic())))
                except KeyboardInterrupt:  # a second Ctrl+C: the records are a moment away
                    pass
        raise
    return results
