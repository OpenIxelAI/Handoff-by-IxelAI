"""Make text from agents and people safe to store and to show.

Everything here runs in linear time on hostile input: character tables and
`str.find` loops, no regular expressions that could backtrack.
"""
from __future__ import annotations

import string

# C0 controls (keeping tab and newline), DEL, C1 controls, and the invisible
# direction marks and overrides that make text display differently from what
# it is. Removing ESC also defuses every terminal escape sequence.
_REMOVE = [c for c in range(0x20) if c not in (0x09, 0x0A)] + list(range(0x7F, 0xA0))
_REMOVE += [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]

# Characters you can't see, which can still carry text a model reads: Unicode tag characters (they can
# spell out a whole hidden sentence after "Fix typo"), zero-width and other format characters, fillers
# that draw as blank space, variation selectors (runs of them can encode bytes), and line separators
# that don't look like line breaks. They're kept but shown, as [U+200B], to the person and the agents
# alike, so nobody reads something the other can't see.
_HIDDEN = [0x00AD, 0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180E, 0x200B, 0x200C, 0x200D, 0x2028, 0x2029,
           *range(0x2060, 0x2065), *range(0x206A, 0x2070), 0x3164, *range(0xFE00, 0xFE10), 0xFEFF, 0xFFA0,
           *range(0xFFF9, 0xFFFC), *range(0x1BCA0, 0x1BCA4), *range(0x1D173, 0x1D17B), *range(0xE0000, 0xE0080),
           *range(0xE0100, 0xE01F0)]
_SHOWN = {c: f"[U+{c:04X}]" for c in _HIDDEN}
_CLEAN_TABLE = {**dict.fromkeys(_REMOVE), **_SHOWN}
_LINE_TABLE = {**_CLEAN_TABLE, 0x09: " ", 0x0A: " "}
# Two of them are kept as they are where text needs them, decided on the characters as written (so a typed
# "[U+FE0F]" stays those eight characters):
# - one text-or-emoji style selector right after a character you can see ("⚠️", "1️⃣") is how emoji are
#   written, and it can hide nothing; a second one, or one on its own, is shown like the rest;
# - a zero-width joiner or non-joiner between two non-ASCII characters is part of emoji (👩‍💻) and of
#   Persian and Indic writing; next to ASCII, or in a run, it's shown.
_STYLES = "\ufe0e\ufe0f"
_JOINERS = "\u200c\u200d"


def _seen(char: str) -> bool:
    """A character that is itself seen: not a space, and not one that's removed or shown as [U+...]."""
    return char not in " \t\n" and ord(char) not in _CLEAN_TABLE


def _clean(text: str, table: dict) -> str:
    places = []
    for char in _STYLES + _JOINERS:
        pos = text.find(char)
        while pos != -1:
            places.append(pos)
            pos = text.find(char, pos + 1)
    if not places:
        return text.translate(table)
    places.sort()
    out, start, kept = [], 0, set()
    for pos in places:
        char, before = text[pos], text[pos - 1] if pos else ""
        after = text[pos + 1] if pos + 1 < len(text) else ""
        if char in _STYLES:
            keep = bool(before) and _seen(before)
        else:  # (after an emoji's own style selector too: ❤️‍🔥)
            keep = (bool(before) and ord(before) > 0x7F and (_seen(before) or pos - 1 in kept)
                    and bool(after) and ord(after) > 0x7F and _seen(after))
        if keep:
            kept.add(pos)
        out += [text[start:pos].translate(table), char if keep else table[ord(char)]]
        start = pos + 1
    out.append(text[start:].translate(table))
    return "".join(out)


def clean_text(text: object) -> str:
    """Strip control characters and bidi overrides, and show invisible characters; keep tabs and newlines."""
    if text is None:
        return ""
    cleaned = str(text).removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")  # a file's BOM hides nothing
    return _clean(cleaned, _CLEAN_TABLE).strip()


def clean_line(text: object) -> str:
    """Like clean_text, on one line: for titles, names and paths."""
    if text is None:
        return ""
    cleaned = str(text).removeprefix("\ufeff").replace("\r\n", " ").replace("\r", " ")
    return _clean(cleaned, _LINE_TABLE).strip()


# ── Secrets ───────────────────────────────────────────────────────────────────

_ALNUM = frozenset(string.ascii_letters + string.digits)
_TOKEN = frozenset(string.ascii_letters + string.digits + "_-")
_ALNUM_US = frozenset(string.ascii_letters + string.digits + "_")
_UPPER_DIGIT = frozenset(string.ascii_uppercase + string.digits)
_BASE64 = frozenset(string.ascii_letters + string.digits + "+/")
_URL_PATH = frozenset(string.ascii_letters + string.digits + "/")

# (prefix, characters the key is made of, minimum length after the prefix, what it is)
_KEY_PATTERNS = [
    ("sk-", _TOKEN, 20, "an API key (sk-…)"),
    ("sk_live_", _ALNUM, 20, "a Stripe secret key (sk_live_…)"),
    ("rk_live_", _ALNUM, 20, "a Stripe secret key (rk_live_…)"),
    ("xai-", _ALNUM, 32, "an xAI API key (xai-…)"),
    ("gsk_", _ALNUM, 32, "a Groq API key (gsk_…)"),
    ("hf_", _ALNUM, 30, "a Hugging Face token (hf_…)"),
    ("npm_", _ALNUM, 36, "an npm token (npm_…)"),
    ("ghp_", _ALNUM_US, 20, "a GitHub token (ghp_…)"),
    ("gho_", _ALNUM_US, 20, "a GitHub token (gho_…)"),
    ("ghu_", _ALNUM_US, 20, "a GitHub token (ghu_…)"),
    ("ghs_", _ALNUM_US, 20, "a GitHub token (ghs_…)"),
    ("ghr_", _ALNUM_US, 20, "a GitHub token (ghr_…)"),
    ("github_pat_", _ALNUM_US, 20, "a GitHub token (github_pat_…)"),
    ("glpat-", _TOKEN, 20, "a GitLab token (glpat-…)"),
    ("xoxb-", _TOKEN, 10, "a Slack token (xoxb-…)"),
    ("xoxp-", _TOKEN, 10, "a Slack token (xoxp-…)"),
    ("xoxa-", _TOKEN, 10, "a Slack token (xoxa-…)"),
    ("xoxr-", _TOKEN, 10, "a Slack token (xoxr-…)"),
    ("xoxs-", _TOKEN, 10, "a Slack token (xoxs-…)"),
    ("xapp-", _TOKEN, 20, "a Slack token (xapp-…)"),
    ("AIza", _TOKEN, 35, "a Google API key (AIza…)"),
    ("GOCSPX-", _TOKEN, 20, "a Google OAuth client secret (GOCSPX-…)"),
    ("AKIA", _UPPER_DIGIT, 16, "an AWS access key (AKIA…)"),
    ("ASIA", _UPPER_DIGIT, 16, "an AWS access key (ASIA…)"),
]
_PEM_START = "-----BEGIN "
_PUTTY = "PuTTY-User-Key-File-"
_SLACK_HOOK = "hooks.slack.com/services/"
_JWT = "eyJ"  # base64 of '{"', how a JSON Web Token's header and payload start
_JWT_PART, _JWT_SIGNATURE = 10, 16  # shortest header or payload, and signature, that count
# An AWS secret access key is 40 base64 characters with nothing to mark it, except the name it goes by
# (the last two: the AWS CLI's and STS's JSON, "SecretAccessKey": "...", and the JavaScript SDK's config)
_AWS_SECRET_NAMES = ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "SecretAccessKey", "secretAccessKey")
_AWS_SECRET_CHARS = 40


def _run(text: str, start: int, allowed: frozenset[str], limit: int) -> int:
    """How many characters from `start` are in `allowed`, counting up to `limit`."""
    end = min(len(text), start + limit)
    i = start
    while i < end and text[i] in allowed:
        i += 1
    return i - start


def _starts_word(text: str, pos: int) -> bool:
    # a key starts a word: "task-…" or "risk-…" aren't "sk-…"
    return pos == 0 or text[pos - 1] not in _TOKEN


def _jwt_at(text: str, pos: int) -> tuple[bool, int]:
    """Is there a JSON Web Token (eyJ….eyJ….signature) at `pos`? Also where its header ends, so the
    search goes on from there: each character is looked at a few times at most, whatever the text."""
    header = _run(text, pos, _TOKEN, len(text))
    end = pos + header
    if header < _JWT_PART or not text.startswith("." + _JWT, end):
        return False, end
    payload = _run(text, end + 1, _TOKEN, len(text))
    tail = end + 1 + payload
    if payload < _JWT_PART or not text.startswith(".", tail):
        return False, end
    return _run(text, tail + 1, _TOKEN, _JWT_SIGNATURE) >= _JWT_SIGNATURE, end


def _aws_secret_after(text: str, pos: int) -> bool:
    """`aws_secret_access_key = <40 characters>` (an assignment, a YAML or JSON key, quoted or not)."""
    i, end = pos, min(len(text), pos + 12)
    seen_sign = False
    while i < end and text[i] in " \t\"':=":
        seen_sign = seen_sign or text[i] in ":="
        i += 1
    return seen_sign and _run(text, i, _BASE64, _AWS_SECRET_CHARS + 1) == _AWS_SECRET_CHARS


JWT = "a JSON Web Token (eyJ…)"


def find_secret(text: str, tokens: bool = True) -> str | None:
    """Describe the first obvious secret in `text`, or None. Never returns the secret itself. With
    `tokens=False`, a JSON Web Token doesn't count (find_token): a file can hold a sample one."""
    for prefix, allowed, minimum, what in _KEY_PATTERNS:
        pos = text.find(prefix)
        while pos != -1:
            if _starts_word(text, pos) and _run(text, pos + len(prefix), allowed, minimum) >= minimum:
                return what
            pos = text.find(prefix, pos + 1)
    pos = text.find(_PEM_START)
    while pos != -1:
        header = text[pos + len(_PEM_START):pos + len(_PEM_START) + 60]
        end = header.find("-----")
        if end != -1 and "PRIVATE KEY" in header[:end]:
            return "a private key"
        pos = text.find(_PEM_START, pos + 1)
    pos = text.find(_PUTTY)
    while pos != -1:
        after = pos + len(_PUTTY)
        digits = _run(text, after, frozenset(string.digits), 3)
        if digits and text.startswith(":", after + digits):
            return "a private key (PuTTY)"
        pos = text.find(_PUTTY, pos + 1)
    pos = text.find(_SLACK_HOOK)
    while pos != -1:
        if _run(text, pos + len(_SLACK_HOOK), _URL_PATH, 30) >= 30:
            return "a Slack webhook URL"
        pos = text.find(_SLACK_HOOK, pos + 1)
    for name in _AWS_SECRET_NAMES:
        pos = text.find(name)
        while pos != -1:
            if _aws_secret_after(text, pos + len(name)):
                return "an AWS secret access key"
            pos = text.find(name, pos + 1)
    return find_token(text) if tokens else None


def find_token(text: str) -> str | None:
    """JWT if `text` holds a JSON Web Token (eyJ….eyJ….signature), else None."""
    pos = text.find(_JWT)
    while pos != -1:
        found, end = (_jwt_at(text, pos) if _starts_word(text, pos) else (False, pos + 1))
        if found:
            return JWT
        pos = text.find(_JWT, max(end, pos + 1))
    return None
