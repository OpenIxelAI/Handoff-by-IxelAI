"""The guide (docs/guide.md) and SECURITY.md stay in step with the code."""
import asyncio
from pathlib import Path

import mcp

from handoff import cli
from handoff.board import DONE_RULES, STATUSES
from handoff.mcp_server import build_server

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "docs" / "guide.md").read_text(encoding="utf-8")  # the full reference; README.md is the short intro
SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")


def test_readme_lists_every_tool(tmp_path):
    async def go():
        async with mcp.Client(build_server("claude", tmp_path)) as client:
            return [t.name for t in (await client.list_tools()).tools]
    for name in asyncio.run(go()):
        assert f"`{name}`" in README, name


def test_readme_lists_every_command():
    for usage, _ in cli.COMMANDS:
        command = " ".join(usage.split()[:2])
        if command not in ("handoff mcp", "handoff version", "handoff help"):
            assert f"`{command}" in README, command


def test_readme_names_every_done_rule_and_status():
    for rule in DONE_RULES:
        assert rule in README, rule
    for status in STATUSES:
        assert status in README, status


def test_security_page_names_every_test_file():
    for name in sorted(p.name for p in (ROOT / "tests").glob("test_*.py")):
        assert f"`tests/{name}`" in SECURITY, name
