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

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,79}$")
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


def parse_opencode(stdout: str, model: str) -> tuple[str, dict | None, str | None]:
    """`opencode run --format json`: one JSON event a line. The reply is the text of its last message; usage adds
    up its steps' tokens and cost (OpenCode's own estimate; nothing for its free models)."""
    texts: dict[str, list[str]] = {}
    order: list[str] = []
    totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
    steps, error = 0, None
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            message = str(part.get("messageID") or "")
            if message not in texts:
                texts[message] = []
                order.append(message)
            texts[message].append(part["text"])
        elif event.get("type") == "step_finish":
            steps += 1
            counts = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = counts.get("cache") if isinstance(counts.get("cache"), dict) else {}
            for ours, value in (("in", counts.get("input")), ("out", (_count(counts.get("output")) or 0)
                                                               + (_count(counts.get("reasoning")) or 0)),
                                ("cache_read", cache.get("read")), ("cache_write", cache.get("write"))):
                totals[ours] += _count(value) or 0
            totals["cost"] += _cost(part.get("cost")) or 0.0
        elif event.get("type") == "error":
            found = event.get("error") if isinstance(event.get("error"), dict) else {}
            data = found.get("data") if isinstance(found.get("data"), dict) else {}
            error = str(data.get("message") or found.get("name") or "OpenCode reported an error")[:500]
    reply = "".join(texts[order[-1]]) if order else ""
    used = check({"models": {model: totals}}) if steps else None
    return reply, used, error


def parse_hermes(stdout: str, report: str | None) -> tuple[str, dict | None, str | None]:
    """Hermes Agent's one-shot reply (stdout) and its --usage-file report."""
    try:
        data = json.loads(report) if report else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return stdout or "", None, None
    model = data.get("model") if isinstance(data.get("model"), str) else "hermes"
    used = check({"models": {model: {"in": data.get("input_tokens"), "out": data.get("output_tokens"),
                                     "cache_read": data.get("cache_read_tokens"),
                                     "cache_write": data.get("cache_write_tokens"),
                                     "cost": data.get("estimated_cost_usd")}}})
    error = "Hermes Agent reported that the run failed." if data.get("failed") is True else None
    return stdout or "", used, error


def parse_local(stdout: str) -> tuple[str, dict | None, str | None]:
    """A local model's run (local_model.run): its reply, its tokens (it costs nothing) and its error."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout, None, None
    if not isinstance(data, dict):
        return stdout, None, None
    counts = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), str) else ""
    used = check({"models": {model: {"in": counts.get("in"), "out": counts.get("out"), "cost": 0}}}) \
        if model else None
    reply = data.get("reply") if isinstance(data.get("reply"), str) else ""
    error = data.get("error") if isinstance(data.get("error"), str) else None
    return reply, used, error


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
