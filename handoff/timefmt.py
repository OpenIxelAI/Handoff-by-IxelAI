"""Board timestamps (UTC, ISO 8601) as people read them."""
from __future__ import annotations

from datetime import datetime, timezone


def parse(at: str) -> datetime | None:
    try:
        return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def ago(at: str, now: datetime | None = None) -> str:
    """'just now', '5m ago', '3h ago', '2d ago'."""
    then = parse(at)
    if then is None:
        return "?"
    seconds = int(((now or datetime.now(timezone.utc)) - then).total_seconds())
    if seconds < 60:
        return "just now"
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def stamp(at: str) -> str:
    """'2026-09-25 10:04 UTC'."""
    then = parse(at)
    return then.strftime("%Y-%m-%d %H:%M UTC") if then else at
