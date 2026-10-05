"""Path claims: normalize the globs agents claim, and tell when two can overlap.

A folder claim covers everything under it, so `src` overlaps `src/api.py`.
A claim whose last part has an extension (`api.py`, `*.md`) is taken to be
a file. Wildcards: `*` and `?` within one path segment, `**` across
segments. Everything is decided by walking a product of the two patterns,
which takes at most len(a) × len(b) steps: no regular expressions.
"""
from __future__ import annotations

from handoff.sanitize import clean_line

MAX_PATH_CHARS = 256


class PathError(ValueError):
    pass


def normalize_path(raw: str) -> str:
    """A claim as a clean relative path: forward slashes, no `.` or `..`, no leading `./`."""
    text = clean_line(raw)
    if not text:
        raise PathError("a path is empty")
    if len(text) > MAX_PATH_CHARS:
        raise PathError(f"a path is longer than {MAX_PATH_CHARS} characters")
    path = text.replace("\\", "/")
    if path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
        raise PathError(f"{text!r} isn't relative to the project root")
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PathError(f"{text!r} leaves the project ('..' isn't allowed)")
        if segment == "**" and parts and parts[-1] == "**":
            continue
        parts.append(segment)
    return "/".join(parts) if parts else "**"


def _segments_intersect(p: str, q: str) -> bool:
    """Could one path segment match both patterns? (`*` any run, `?` any one character)"""
    n, m = len(p), len(q)
    seen: set[tuple[int, int]] = set()
    stack = [(0, 0)]
    while stack:
        i, j = stack.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == n and j == m:
            return True
        if i < n and p[i] == "*":
            stack.append((i + 1, j))
        if j < m and q[j] == "*":
            stack.append((i, j + 1))
        if i < n and j < m:
            # both sides take the same next character
            a, b = p[i], q[j]
            if a in "*?" or b in "*?" or a == b:
                stack.append((i if a == "*" else i + 1, j if b == "*" else j + 1))
    return False


def _pattern(path: str) -> list[str]:
    segments = path.casefold().split("/")
    last = segments[-1]
    if last != "**" and "." not in last[1:]:
        segments.append("**")  # a folder: the claim covers what's inside it
    return segments


def overlaps(a: str, b: str) -> bool:
    """Can some path be covered by both claims? Case-insensitive, to err on the side of warning."""
    left, right = _pattern(a), _pattern(b)
    if not any(c in a + b for c in "*?"):
        # two plain paths overlap when one is inside the other (a folder named v1.2 too)
        plain_a, plain_b = a.casefold().split("/"), b.casefold().split("/")
        k = min(len(plain_a), len(plain_b))
        return plain_a[:k] == plain_b[:k]
    n, m = len(left), len(right)
    same: dict[tuple[int, int], bool] = {}
    seen: set[tuple[int, int]] = set()
    stack = [(0, 0)]
    while stack:
        i, j = stack.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == n and j == m:
            return True
        a_any = i < n and left[i] == "**"
        b_any = j < m and right[j] == "**"
        if a_any:
            stack.append((i + 1, j))
            if j < m and not b_any:
                stack.append((i, j + 1))  # `**` takes one more segment
        if b_any:
            stack.append((i, j + 1))
            if i < n and not a_any:
                stack.append((i + 1, j))
        if i < n and j < m and not a_any and not b_any:
            if (i, j) not in same:
                same[(i, j)] = _segments_intersect(left[i], right[j])
            if same[(i, j)]:
                stack.append((i + 1, j + 1))
    return False
