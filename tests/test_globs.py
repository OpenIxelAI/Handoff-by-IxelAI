"""Path claims: normalizing globs and deciding when two claims overlap."""
import time

import pytest

from handoff.globs import PathError, normalize_path, overlaps


@pytest.mark.parametrize("raw,expected", [
    ("src/api.py", "src/api.py"),
    ("./src//api.py", "src/api.py"),
    ("src\\api.py", "src/api.py"),
    ("tests/", "tests"),
    (".", "**"),
    ("src/**/**/x.py", "src/**/x.py"),
])
def test_normalize(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "/etc/passwd", "~/.ssh", "C:\\Windows", "src/../../x", "a" * 300])
def test_normalize_refuses(raw):
    with pytest.raises(PathError):
        normalize_path(raw)


@pytest.mark.parametrize("a,b,expected", [
    ("src/api.py", "src/api.py", True),
    ("src", "src/api.py", True),                 # a folder covers what's inside it
    ("src/api.py", "src", True),
    ("src/api.py", "src/db.py", False),
    ("src/*.py", "src/api.py", True),
    ("src/*.py", "src/*.js", False),
    ("src/*.py", "src/a*", True),                # both match src/a.py
    ("src/?.py", "src/ab.py", False),
    ("**/test_*.py", "tests/unit/test_api.py", True),
    ("**/*.md", "src/api.py", False),
    ("docs/**", "docs", True),
    ("**", "anything/at/all", True),
    ("tests", "tests_old", False),
    ("Src/API.py", "src/api.py", True),          # case-insensitive filesystems: warn anyway
    ("v1.2", "v1.2/notes.txt", True),            # a folder with a dot in its name
    ("src/*", "src/db/models.py", True),
    ("src/*.py", "src/db/models.py", False),
])
def test_overlaps(a, b, expected):
    assert overlaps(a, b) is expected
    assert overlaps(b, a) is expected


def test_overlap_check_is_bounded_on_hostile_patterns():
    a = "/".join(["*a*b*c*d*"] * 25)
    b = "/".join(["**", "*" * 9] * 12)
    started = time.perf_counter()
    overlaps(normalize_path(a), normalize_path(b))
    assert time.perf_counter() - started < 2.0


def test_an_overlap_is_worked_out_once_whichever_way_round():
    from handoff.globs import _overlaps
    _overlaps.cache_clear()
    assert overlaps("src/*.py", "src/api.py") and overlaps("src/api.py", "src/*.py")
    assert (_overlaps.cache_info().misses, _overlaps.cache_info().hits) == (1, 1)
