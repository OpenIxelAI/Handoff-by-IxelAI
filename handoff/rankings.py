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
from typing import Callable

UPDATED = "2026-10-12"
SOURCES = (
    ("BenchLM coding ranking (SWE-bench Verified, LiveCodeBench), Oct 10 2026", "https://benchlm.ai/coding"),
    ("BenchLM SWE-bench Verified, open models included, Oct 10 2026",
     "https://benchlm.ai/benchmarks/swe-bench-verified"),
    ("Morph: best open-source LLMs (Terminal-Bench 2.1, GPQA, HLE), Oct 3 2026",
     "https://www.morphllm.com/best-open-source-llm"),
    ("Morph: best Ollama models, Sep 29 2026", "https://www.morphllm.com/best-ollama-models"),
    ("Vellum open LLM leaderboard, Jul 24 2026", "https://www.vellum.ai/open-llm-leaderboard"),
    ("Arena (LMArena) text and webdev leaderboards", "https://arena.ai/leaderboard"),
    ("Artificial Analysis text-to-image arena", "https://artificialanalysis.ai/image/leaderboard/text-to-image"),
    ("Artificial Analysis text-to-video arena", "https://artificialanalysis.ai/video/leaderboard/text-to-video"),
    ("Handoff's own runs: Fable plans best, Haiku and qwen3:4b-instruct do small steps well (2026-10-11)", ""),
    ("Model prices as reported in posts (not vendor pages), Oct 12 2026", ""),
)
# Open models' scores are mostly their makers' own, gathered by the sites above: their order is a best guess

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
# Open models you run yourself are here by their Ollama names (ollama:qwen3.6:27b); through LM Studio or another
# server they're the same models.
RANKS: dict[str, list[str]] = {
    "coding": [
        "claude:claude-sonnet-5-5", "claude:claude-opus-5-5", "claude:claude-fable-5-1", "codex:gpt-6-astra",
        "codex:gpt-6.1-sol", "grok:grok-4.7", "codex:gpt-6-sol", "codex:gpt-5.5", "kimi:kimi-k3",
        "cline:claude-sonnet-5-5", "ollama:qwen3.6:27b", "ollama:qwen3.6:35b", "opencode:opencode/step-5-preview-free",
        "claude:claude-haiku-5-5", "ollama:gpt-oss:120b", "ollama:qwen3-coder:30b", "codex:gpt-6-luna",
        "ollama:gpt-oss:20b", "ollama:granite4.2:8b", "ollama:qwen3.5:9b", "ollama:qwen2.5-coder:14b",
        "ollama:qwen3:4b-instruct", "lmstudio:qwen3-4b-instruct-2507",
    ],
    "planning": [
        "claude:claude-fable-5-1", "claude:claude-opus-5-5", "codex:gpt-6-astra", "kimi:kimi-k3",
        "claude:claude-sonnet-5-5", "codex:gpt-6.1-sol", "ollama:gpt-oss:120b", "ollama:qwen3.6:27b",
        "claude:claude-haiku-5-5", "ollama:deepseek-r1:14b",
    ],
    "review": [
        "claude:claude-opus-5-5", "codex:gpt-6-astra", "claude:claude-fable-5-1", "gemini:gemini-3.1-pro-preview",
        "kimi:kimi-k3", "grok:grok-4.7", "claude:claude-sonnet-5-5", "antigravity:gemini-3.1-pro-preview",
        "ollama:gpt-oss:120b", "ollama:qwen3.6:27b", "local:",
    ],
    "writing": [
        "claude:claude-opus-5-5", "claude:claude-fable-5-1", "codex:gpt-6-astra", "gemini:gemini-3.1-pro-preview",
        "kimi:kimi-k3", "grok:grok-4.7", "claude:claude-sonnet-5-5", "antigravity:gemini-3.1-pro-preview",
        "ollama:qwen3.6:27b", "ollama:gemma4:12b", "local:",
    ],
    "images": [
        "codex:gpt-image-2.5", "gemini:nano-banana-2.1", "grok:grok-imagine-image-2.0", "flux:flux-3",
    ],
    "video": [
        "wan:wan-3.0", "seedance:seedance-2.5", "minimax:minimax-h3", "flux:flux-3", "gemini:gemini-omni-flash-1.1",
    ],
}

# Where each model fits in a chain: "top" plans and reviews (and fixes what a check sends up), "mid" builds and
# checks the smaller models' work, "low" takes short, exact directions. Open models are listed with what it takes
# to run them.
TIERS: dict[str, list[dict[str, str]]] = {
    "top": [
        {"model": "claude:claude-fable-5-1", "note": "the best planner; the dearest, so for hard tasks"},
        {"model": "claude:claude-opus-5-5", "note": "plans and reviews well at about 40% of Fable's price"},
        {"model": "codex:gpt-6-astra", "note": "on a ChatGPT plan"},
        {"model": "antigravity:gemini-3.1-pro-preview",
         "note": "Google's; for answers and reviews through Ixel MAT, not an edit worker here"},
        {"model": "kimi:kimi-k3", "note": "open (about 2.8T, 104B active): the best open planner; Kimi's own "
                                          "service, or a very large GPU server"},
        {"model": "DeepSeek V4 Pro / V4.1 Flash", "note": "open (1.6T and 552B): on a hosted open-model server"},
        {"model": "GLM-5.3", "note": "open (753B): on a hosted open-model server"},
        {"model": "MiniMax M3", "note": "open (428B, 23B active): on a hosted open-model server"},
    ],
    "mid": [
        {"model": "claude:claude-sonnet-5-5", "note": "the best builder for its price"},
        {"model": "grok:grok-4.7", "note": "on SuperGrok through grok, edit-only: strong and cheap"},
        {"model": "codex:gpt-6-sol", "note": "on a ChatGPT plan"},
        {"model": "codex:gpt-6.1-sol", "note": "on a ChatGPT plan"},
        {"model": "ollama:qwen3.6:27b", "note": "open, 27B: needs a 32 GB Mac or a 24 GB GPU"},
        {"model": "ollama:qwen3.6:35b", "note": "open, 35B (3B active): fast for its size; 32 GB or more"},
        {"model": "ollama:qwen3-coder:30b", "note": "open, 30B (3.3B active): 32 GB or more"},
        {"model": "ollama:gpt-oss:120b", "note": "open, 117B (5.1B active): 80 GB or more"},
    ],
    "low": [
        {"model": "claude:claude-haiku-5-5", "note": "small steps, very cheap"},
        {"model": "codex:gpt-6-luna", "note": "on a ChatGPT plan; very cheap"},
        {"model": "opencode:opencode/step-5-preview-free", "note": "free through OpenCode"},
        {"model": "ollama:qwen3.5:9b", "note": "open, 9B: runs on a 16 GB Mac"},
        {"model": "ollama:qwen2.5-coder:14b", "note": "open, 14B: the most a 16 GB Mac runs well"},
        {"model": "ollama:granite4.2:8b", "note": "open, 8B: runs on a 16 GB Mac"},
        {"model": "ollama:qwen3:4b-instruct", "note": "open, 4B: fast on any Mac; one exact change at a time"},
    ],
}
TIER_WORDS = {"top": "Top: plans, reviews, and fixes what a check sends up",
              "mid": "Middle: builds, and checks what the smaller models did",
              "low": "Small: short, exact directions only"}
HARDWARE = ("A 16 GB Mac runs open models up to about 14B (9 to 10 GB at Q4) at 12 to 22 tokens a second: they're for "
            "the small tier. 27B to 35B models need 32 GB or more; the top open models need a hosted open-model "
            "server or a very large GPU machine.")

# The short names an agent takes for a model, as the ranking names it
ALIASES = {"claude": {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                      "haiku": "claude-haiku-5-5"}}
# What a model costs per token at API prices, relative (Haiku 5.5's input, $0.10 a million, is 1; measured from
# Claude Code's own cost reports, 2026-10-11). A model is only ever replaced by one that costs the same or less, or
# by a free or local one: a usage limit never moves a run onto a dearer model. A model with no price here (another
# company's, on its own plan) isn't held back by it.
# The new ones are in the same unit: each model's input price per million tokens in USD (Haiku's $0.10 is 1), as
# reported on 2026-10-12 in posts, not on vendor pages. Grok 4.7: $2 in, $6 out, cached $0.50 (cached unsure).
# Gemini 3.1 Pro: $2 in, $12 out (inferred; exact API ID not confirmed). GPT-6 Astra: $10 in, $50 out, cached about
# $1 (unsure). GPT-6.1 Sol: $2 in, $10 out, cached $0.10. GPT-6 Luna: $0.10 in, $0.50 out, cached $0.01 (unsure;
# ID not confirmed). The Claude values come from Claude Code's cost reports, so a tie or a near tie is not exact.
# COST is a price and TIERS is a role: a model can be top-tier and cheap (Gemini 3.1 Pro, $2, like Grok 4.7 and
# Sol), so a price never says which tier a model is in; "the same or less" above means price only. GPT-6 Sol has no
# price reported yet, so it isn't held back by one (and crew_auto treats it as on a plan). Add its price here
# when there is one, as the same unit; don't guess it.
COST = {"claude-fable-5-1": 95, "claude-opus-5-5": 38, "claude-sonnet-5-5": 19, "claude-haiku-5-5": 1,
        "gpt-6-astra": 100, "grok-4.7": 20, "gemini-3.1-pro-preview": 20, "gpt-6.1-sol": 20, "gpt-6-luna": 1}
# The model an agent runs when the approval names none
DEFAULT_MODEL = {"claude": "claude-opus-5-5"}
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
              ranks: dict[str, list[str]] | None = None, agents_only: bool = False,
              usable: Callable[[str, str], bool] | None = None) -> list[str]:
    """The run's fallbacks: the entries ranked below agent:model for this job that an agent in `have` runs
    (every agent if None), best first, up to MAX_FALLBACKS, the last of them free when one is ranked. A model
    the ranking doesn't name falls back to the free and local ones only; no model, to what's below the agent's
    default (DEFAULT_MODEL), or its first entry. Never to a model that costs more (COST).
    `agents_only`: each agent once, without its model (a run through Ixel MAT, which picks the model)."""
    ranked = (ranks or load()[0]).get(job, [])
    entries = [split(e) for e in ranked]
    wanted = canonical(agent, model) if model else DEFAULT_MODEL.get(agent)
    names = [(a, canonical(a, m)) for a, m in entries]
    start = next((i + 1 for i, entry in enumerate(names) if entry == (agent, wanted)), None) if wanted else None
    if start is None and not model:  # (its own model, unranked: what's below the agent's best)
        start = next((i + 1 for i, (a, _) in enumerate(names) if a == agent), None)
    if start is None:  # (not ranked: only what's free)
        below = [(a, m) for a, m in names if a in FREE_AGENTS or m.endswith("-free")]
    else:  # what's ranked below it, and anything ranked above it that costs no more (a better, cheaper model)
        cheaper = COST.get(wanted or "")
        below = [(a, m) for i, (a, m) in enumerate(names)
                 if i >= start or (cheaper is not None and COST.get(m, float("inf")) <= cheaper)]
    found: list[str] = []
    for a, m in below:
        if have is not None and a not in have:
            continue
        if usable is not None and not usable(a, m):  # (a model your server doesn't have, say)
            continue
        if (a, m) == (agent, wanted) or (agents_only and a == agent):
            continue
        if not _free(f"{a}:{m}") and COST.get(m, 0) > COST.get(wanted or "", float("inf")):
            continue  # (never a dearer model)
        entry = a if agents_only or not m else f"{a}:{m}"
        if entry not in found:
            found.append(entry)
    # The last place goes to the best free or local one, when the dearer ones would fill the list: a run that
    # outlasts every plan still finishes, and at no cost
    free = next((e for e in found if _free(e)), None)
    if len(found) > MAX_FALLBACKS and free and free not in found[:MAX_FALLBACKS]:
        return found[:MAX_FALLBACKS - 1] + [free]
    return found[:MAX_FALLBACKS]


def _free(entry: str) -> bool:
    agent, model = split(entry)
    return agent in FREE_AGENTS or model.endswith("-free")


FREE_AGENTS = ("ollama", "lmstudio", "local")


def shown(entries: list[str]) -> str:
    """The fallbacks in words: "claude-fable-5-1, then codex (gpt-6-astra), then ollama (qwen3:4b-instruct)"."""
    words = []
    for entry in entries:
        agent, model = split(entry)
        words.append(f"{agent} ({model})" if model and not model.startswith(agent) else (model or agent))
    return ", then ".join(words)
