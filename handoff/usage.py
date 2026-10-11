"""
What a worker run used: tokens and cost per model, as the agent reported them.

Claude Code says it when run with `--output-format json`: its result has `modelUsage`, one entry per model
that answered, with tokens and an estimated cost in US dollars (at API prices, so on a subscription it's what
the run would have cost, not what you paid). That's also the proof of which model did the work.

A board can come from somewhere else, so usage read back from it is checked again (`check`) before it's shown.
"""
from __future__ import annotations

import json
import math
import re

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:@/\-]{0,79}$")
_FIELDS = (("inputTokens", "in"), ("outputTokens", "out"), ("cacheReadInputTokens", "cache_read"),
           ("cacheCreationInputTokens", "cache_write"))
MAX_MODELS = 8
MAX_TOKENS = 10 ** 12
MAX_COST = 10 ** 6


def parse_claude(stdout: str) -> tuple[str, dict | None, str | None]:
    """Claude Code's `--output-format json` result: its reply, its usage (or None) and its error (or None).
    Output that isn't that (plain text) comes back as the reply, with no usage."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout, None, None
    if not isinstance(data, dict) or data.get("type") != "result":
        return stdout, None, None
    reply = data.get("result") if isinstance(data.get("result"), str) else ""
    models = {}
    for name, info in (data.get("modelUsage") or {}).items() if isinstance(data.get("modelUsage"), dict) else ():
        if isinstance(info, dict):
            models[name] = {short: info.get(long) for long, short in _FIELDS} | {"cost": info.get("costUSD")}
    usage = check({"models": models}) if models else None
    error = (reply or "the agent reported an error") if data.get("is_error") else None
    return reply, usage, error


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKENS:
        return None
    return value


def _cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int) and not 0 <= value <= MAX_COST:  # (one too big for a float raises in float())
        return None
    value = float(value)
    return round(value, 6) if math.isfinite(value) and 0 <= value <= MAX_COST else None


def check(usage: object) -> dict | None:
    """Usage as the board keeps it, {"models": {name: {"in", "out", "cache_read", "cache_write", "cost"}}},
    with anything malformed left out. None if nothing's left."""
    if not isinstance(usage, dict) or not isinstance(usage.get("models"), dict):
        return None
    models = {}
    for name, info in list(usage["models"].items())[:MAX_MODELS]:
        if not (isinstance(name, str) and _MODEL.fullmatch(name) and isinstance(info, dict)):
            continue
        entry = {short: _count(info.get(short)) or 0 for _, short in _FIELDS}
        entry["cost"] = _cost(info.get("cost")) or 0.0
        models[name] = entry
    return {"models": models} if models else None


def merge(*runs: dict | None) -> dict | None:
    """Several runs' usage added up, model by model (a chain's steps)."""
    total: dict[str, dict] = {}
    for run in runs:
        for name, info in ((check(run) or {}).get("models") or {}).items():
            entry = total.setdefault(name, {short: 0 for _, short in _FIELDS} | {"cost": 0.0})
            for _, short in _FIELDS:
                entry[short] = min(entry[short] + info[short], MAX_TOKENS)
            entry["cost"] = round(min(entry["cost"] + info["cost"], MAX_COST), 6)
    return check({"models": total})


def tokens(n: int) -> str:
    """12345 -> "12.3k"."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def lines(usage: object) -> list[str]:
    """One line per model and a total, for people: "claude-opus-5-5: 12.3k in, 2.1k out, 40.0k cached · $0.31"."""
    usage = check(usage)
    if not usage:
        return []
    out, cost = [], 0.0
    for name, info in usage["models"].items():
        cached = info["cache_read"] + info["cache_write"]
        out.append(f"{name}: {tokens(info['in'])} in, {tokens(info['out'])} out"
                   f"{f', {tokens(cached)} cached' if cached else ''} · ${info['cost']:.4f}")
        cost += info["cost"]
    if len(out) > 1:
        out.append(f"Total: ${cost:.4f}")
    return out


def total_cost(usage: object) -> float:
    usage = check(usage)
    return round(sum(i["cost"] for i in usage["models"].values()), 6) if usage else 0.0
