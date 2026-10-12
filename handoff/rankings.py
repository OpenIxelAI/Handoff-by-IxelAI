"""
Which models are best at what, from public benchmarks: what the worker hands a run to when a model is out of usage.

A model that hits its plan's limit (limits.py) is benched for the rest of the run, and the run goes on with the
next model its approval lists, saying so in the handback ("claude-opus-5-5 hit its usage limit, so
claude-fable-5-1 took over"), instead of stopping with nothing done. That list, the run's fallbacks, is worked out
from RANKS when the person approves the run, shown to them, and sealed into the approval with the models
(approvals.py): a board can't add a model to it.

The fallbacks are the models ranked below the one the run names, for its kind of work, that this computer has an
agent for: the next best, then the one after, ending with the free and local ones. RANKS is a ranking per job,
best first, as "agent:model" (the agent that runs it, then the model's name as that agent takes it). It's kept
current: `handoff rankings update` fetches the newest from Handoff's own repository (a public file: nothing
about you is sent), and a newer Handoff carries it too. A file of your own, rankings.json next to agents.toml,
replaces it ({"updated": "…", "ranks": {"coding": ["claude:claude-opus-5-5", …], …}}).
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

UPDATED = "2026-10-12"
SOURCES = (
    ("BenchLM coding ranking (SWE-bench Verified, LiveCodeBench), Oct 10 2026", "https://benchlm.ai/coding"),
    ("Arena (LMArena) text and webdev leaderboards", "https://arena.ai/leaderboard"),
    ("Artificial Analysis text-to-image arena", "https://artificialanalysis.ai/image/leaderboard/text-to-image"),
    ("Artificial Analysis text-to-video arena", "https://artificialanalysis.ai/video/leaderboard/text-to-video"),
    ("Handoff's own runs: Fable plans best, Haiku does small steps well (2026-10-11)", ""),
)

JOBS = {
    "coding": "changing code (an edit, or a chain's steps)",
    "planning": "planning a chain's steps",
    "review": "reviewing changes",
    "writing": "answering questions and writing",
    "images": "making pictures",
    "video": "making and editing video",
}

# Best first. An edit's fallbacks come from "coding" (a chain's planner from "planning"); an answer's from
# "writing" and a review's from "review" (those run through Ixel MAT, which picks the agent's model itself, so
# only the agent counts there). "images" and "video" say who's best, for choosing; a run doesn't fall back there.
RANKS: dict[str, list[str]] = {
    "coding": [
        "claude:claude-sonnet-5-5", "claude:claude-opus-5-5", "claude:claude-fable-5-1", "codex:gpt-6-astra",
        "codex:gpt-6.1-sol", "codex:gpt-6-sol", "codex:gpt-5.5", "kimi:kimi-k3", "cline:claude-sonnet-5-5",
        "opencode:opencode/step-5-preview-free", "claude:claude-haiku-5-5", "codex:gpt-6-luna",
        "ollama:qwen3:4b-instruct", "lmstudio:qwen3-4b-instruct-2507",
    ],
    "planning": [
        "claude:claude-fable-5-1", "claude:claude-opus-5-5", "codex:gpt-6-astra", "claude:claude-sonnet-5-5",
        "codex:gpt-6.1-sol", "claude:claude-haiku-5-5",
    ],
    "review": [
        "claude:claude-opus-5-5", "codex:gpt-6-astra", "claude:claude-fable-5-1", "gemini:gemini-3.1-pro-preview",
        "grok:grok-4.7", "claude:claude-sonnet-5-5", "antigravity:gemini-3.1-pro-preview", "local:",
    ],
    "writing": [
        "claude:claude-opus-5-5", "claude:claude-fable-5-1", "codex:gpt-6-astra", "gemini:gemini-3.1-pro-preview",
        "grok:grok-4.7", "claude:claude-sonnet-5-5", "antigravity:gemini-3.1-pro-preview", "local:",
    ],
    "images": [
        "codex:gpt-image-2.5", "gemini:nano-banana-2.1", "grok:grok-imagine-image-2.0", "flux:flux-3",
    ],
    "video": [
        "wan:wan-3.0", "seedance:seedance-2.5", "minimax:minimax-h3", "flux:flux-3", "gemini:gemini-omni-flash-1.1",
    ],
}

# The short names an agent takes for a model, as the ranking names it
ALIASES = {"claude": {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                      "haiku": "claude-haiku-5-5"}}
# The kind of run → the job its fallbacks come from
KIND_JOB = {"edit": "coding", "answer": "writing", "review": "review"}
MAX_FALLBACKS = 5
LATEST = "https://raw.githubusercontent.com/OpenIxelAI/Handoff-by-IxelAI/main/handoff/rankings.json"

_AGENT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,79}$")


class RankingsError(Exception):
    pass


def split(entry: str) -> tuple[str, str]:
    """"agent:model" → (agent, model); the model may hold colons of its own (ollama:qwen3:4b)."""
    agent, _, model = entry.partition(":")
    return agent, model


def check_entry(entry: object) -> str | None:
    """An "agent:model" (or a bare "agent") if it's a valid one."""
    if not isinstance(entry, str):
        return None
    agent, _, model = entry.partition(":")
    if not _AGENT.fullmatch(agent) or (model and not _MODEL.fullmatch(model)):
        return None
    return entry


def check(data: object) -> dict[str, list[str]] | None:
    """A rankings file's "ranks", if every job is a list of valid entries."""
    if not isinstance(data, dict) or not isinstance(data.get("ranks"), dict):
        return None
    ranks = {}
    for job, entries in data["ranks"].items():
        if job not in JOBS or not isinstance(entries, list) or len(entries) > 100:
            return None
        if any(check_entry(e) is None for e in entries):
            return None
        ranks[job] = list(entries)
    return ranks


def own_path() -> Path:
    from handoff import adapters
    return adapters.config_path().with_name("rankings.json")


def load() -> tuple[dict[str, list[str]], str]:
    """The rankings in use, and how current they are: your rankings.json if there's a valid one, else Handoff's."""
    path = own_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RANKS, UPDATED
    ranks = check(data)
    if ranks is None:
        return RANKS, UPDATED
    updated = data.get("updated") if isinstance(data.get("updated"), str) else "unknown"
    return {**RANKS, **ranks}, updated[:20]


def update(url: str = LATEST, timeout: float = 20.0) -> str:
    """Fetch the newest rankings and keep them as your rankings.json. Returns their date."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (a fixed https URL)
            data = json.loads(response.read(1_000_000).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise RankingsError(f"Couldn't fetch the rankings: {exc}") from exc
    ranks = check(data)
    if ranks is None:
        raise RankingsError("The rankings fetched aren't in the form Handoff reads; nothing was changed.")
    updated = data.get("updated") if isinstance(data.get("updated"), str) else "unknown"
    path = own_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated": updated, "ranks": ranks}, indent=1) + "\n", encoding="utf-8")
    return updated[:20]


def canonical(agent: str, model: str) -> str:
    return ALIASES.get(agent, {}).get(model.lower(), model)


def fallbacks(job: str, agent: str, model: str | None, have: set[str] | None = None,
              ranks: dict[str, list[str]] | None = None, agents_only: bool = False) -> list[str]:
    """The run's fallbacks: the entries ranked below agent:model for this job that an agent in `have` runs
    (every agent if None), best first, up to MAX_FALLBACKS. A model the ranking doesn't name falls back to the
    free and local ones only; no model (the agent's own default) to what's below the agent's first entry.
    `agents_only`: each agent once, without its model (a run through Ixel MAT, which picks the model)."""
    ranked = (ranks or load()[0]).get(job, [])
    entries = [split(e) for e in ranked]
    wanted = canonical(agent, model) if model else None
    names = [(a, canonical(a, m)) for a, m in entries]
    if wanted is None:
        start = next((i + 1 for i, (a, _) in enumerate(names) if a == agent), None)
    else:
        start = next((i + 1 for i, entry in enumerate(names) if entry == (agent, wanted)), None)
    if start is None:  # (not ranked: only what's free)
        below = [(a, m) for a, m in names if a in FREE_AGENTS or m.endswith("-free")]
    else:
        below = names[start:]
    found: list[str] = []
    for a, m in below:
        if have is not None and a not in have:
            continue
        if (a, m) == (agent, wanted) or (agents_only and a == agent):
            continue
        entry = a if agents_only or not m else f"{a}:{m}"
        if entry not in found:
            found.append(entry)
        if len(found) >= MAX_FALLBACKS:
            break
    return found


FREE_AGENTS = ("ollama", "lmstudio", "local")


def shown(entries: list[str]) -> str:
    """The fallbacks in words: "claude-fable-5-1, then codex (gpt-6-astra), then ollama (qwen3:4b-instruct)"."""
    words = []
    for entry in entries:
        agent, model = split(entry)
        words.append(f"{agent} ({model})" if model and not model.startswith(agent) else (model or agent))
    return ", then ".join(words)
