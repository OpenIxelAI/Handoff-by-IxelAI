"""Events in words, for the MCP server and the CLI alike."""
from __future__ import annotations

from typing import Callable

from handoff.board import CHECK_RESULTS as _CHECK_RESULTS
from handoff.board import STATUSES as _STATUSES
from handoff.board import Event, safe_name
from handoff.sanitize import clean_line


def string_list(value: object) -> list[str]:
    """A stored list of strings, tolerating anything else a damaged board might hold."""
    return [clean_line(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def text_field(value: object) -> str:
    return value if isinstance(value, str) else ""


def handoff_text(data: dict) -> str:
    parts = [f"Done: {text_field(data.get('done'))}"]
    if text_field(data.get("left")):
        parts.append(f"Left: {data['left']}")
    if text_field(data.get("verify")):
        parts.append(f"Verify: {data['verify']}")
    if string_list(data.get("files")):
        parts.append("Files: " + ", ".join(string_list(data["files"])))
    if text_field(data.get("branch")):
        parts.append(f"Branch: {data['branch']}")
    return "\n".join(parts)


def check_marks(value: object) -> list[dict]:
    """A review's marks on the acceptance checks, keeping only well-formed ones (a board can be crafted)."""
    if not isinstance(value, list):
        return []
    return [{"check": m["check"], "result": m["result"], "text": clean_line(m.get("text")),
             "note": clean_line(m.get("note"))}
            for m in value if isinstance(m, dict) and isinstance(m.get("check"), int)
            and not isinstance(m.get("check"), bool) and m.get("result") in _CHECK_RESULTS]


def checks_text(value: object) -> str:
    """One line per acceptance check: its number, the reviewer's mark, the check, and their note."""
    lines = []
    for mark in check_marks(value):
        line = f"{mark['check']}. [{mark['result'].replace('_', ' ')}] {mark['text']}"
        lines.append(line + (f" — {mark['note']}" if mark["note"] else ""))
    return "\n".join(lines)


def checks_tally(value: object) -> str:
    """'3 of 4 checks met', from the marks alone. System words and numbers only."""
    marks = check_marks(value)
    if not marks:
        return ""
    met = sum(m["result"] == "met" for m in marks)
    return f"{met} of {len(marks)} check{'s' if len(marks) != 1 else ''} met"


def describe(event: Event, who: Callable[[str], str] | None = None) -> str:
    """A one-line, system-written summary of an event. Only names and fixed words: no free text.
    `who` says a name as the reader reads it (at the command line, `human` is "you")."""
    data = event.data

    def name(key: str, default: str = "nobody") -> str:
        found = safe_name(data.get(key)) or default
        return who(found) if who is not None else found

    if event.kind == "created":
        return "created it" + (f", assigned to {name('assignee')}" if data.get("assignee") else "")
    if event.kind == "claimed":
        return "claimed it"
    if event.kind == "note":
        moved = data.get("from") in _STATUSES and data.get("to") in _STATUSES
        return "added a note" + (f" and set it {data['from']} → {data['to']}" if moved else "")
    if event.kind == "handoff":
        return f"handed it to {name('to')}"
    if event.kind == "review_requested":
        return f"asked {name('reviewer')} for a review"
    if event.kind == "review":
        tally = checks_tally(data.get("checks"))
        return ("reviewed it: " + ("approved" if data.get("verdict") == "approve" else "changes requested")
                + (f" ({tally})" if tally else ""))
    if event.kind == "status":
        before, after = (data.get(k) if data.get(k) in _STATUSES else "?" for k in ("from", "to"))
        line = f"set it {before} → {after}"
        return line + (f", waiting on {name('waiting_on')}" if data.get("waiting_on") else "")
    if event.kind == "assigned":
        return f"reassigned it from {name('from')} to {name('to')}"
    if event.kind == "claim_paths":
        return "claimed paths"
    if event.kind == "release_paths":
        return "released its paths"
    if event.kind == "approved":
        kind = data.get("kind", "edit")
        what = {"answer": " to answer", "review": " to review your changes", "image": " to make pictures"}.get(
            kind, "" if kind == "edit" else " (a kind of run this version doesn't know)")
        target = data.get("target") if isinstance(data.get("target"), dict) else None
        if target and isinstance(target.get("label"), str) and kind in ("review", "edit"):
            what = f" to {'review' if kind == 'review' else 'work on'} {clean_line(target['label'])[:300]}"
        runs = (f"the {name('worker', 'unknown')} worker" if kind == "edit" else
                f"{name('worker', 'unknown')}, through Ixel MAT,")  # (answers and the like run no worker)
        return f"approved it for {runs}{what} (results go to {name('return_to', 'human')})"
    if event.kind == "worker":
        ixel = data.get("kind") in ("answer", "review", "image")
        return {"started": "started an Ixel MAT run" if ixel else "started a worker run",
                "finished": "finished the run", "failed": "stopped: the run failed",
                "revoked": "revoked the approval"}.get(data.get("state"), "did something to the run")
    return "did something this version of Handoff doesn't know"


def panel_text(panel: dict) -> str:
    if text_field(panel.get("error")):
        return f"The panel didn't reach a verdict: {panel['error']}"
    lines = [text_field(panel.get("verdict"))]
    meta = [f"{k} {clean_line(panel[k])}" for k in ("confidence", "moderator") if text_field(panel.get(k))]
    if meta:
        lines.append("(" + ", ".join(meta) + ")")
    lines += [f"Corrected: {item}" for item in string_list(panel.get("corrections"))]
    lines += [f"Still disputed: {item}" for item in string_list(panel.get("disagreements"))]
    return "\n".join(lines)
