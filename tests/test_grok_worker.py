"""Grok Build as a Board edit worker (adapters.py): it runs only once you enable it, with its file tools in the
worktree under its own sandbox, and reads its reply and usage from its JSON. Before every run the worker reads
`grok inspect --json` (in the worktree, with the run's environment) and refuses a run, changing nothing, when
one of your own Grok plugins that provides hooks, or a hook, would run. MCP servers don't stop a run: the handback
names them in a note.

A fake `grok` stands in for the real CLI (no test calls it), as in tests/test_adapters.py: it answers --help, and
`inspect --json` with canned output, noting where and with what environment it was asked.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_adapters import REPLY, FakeAgent, approve, board, config, git, repo, script_program, write_hello  # noqa: F401

from handoff import adapters, gitwork, hosts, usage, worker
from handoff.worker import AGENTS, Worker, WorkerUnavailable

GROK_HELP = ("Usage: grok [options]\n  -m, --model MODEL\n  --reasoning-effort LEVEL\n  --output-format FORMAT\n"
             "  --tools LIST\n  --disallowed-tools LIST\n  --sandbox MODE\n  --always-approve\n"
             "  --disable-web-search\n  --no-subagents\n  --cwd DIR\n  --prompt-file PATH\n")
GROK_RESULT = {
    "text": REPLY, "stopReason": "end_turn", "sessionId": "s", "requestId": "r",
    "usage": {"input_tokens": 1200, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
              "output_tokens": 300, "reasoning_tokens": 0, "total_tokens": 1600},
    "num_turns": 2, "total_cost_usd": 0.01,
    "modelUsage": {"grok-4.7-build": {"inputTokens": 1200, "outputTokens": 300, "cacheReadInputTokens": 100,
                                      "cacheCreationInputTokens": 0, "modelCalls": 2, "costUSD": 0.01}},
}
SECRETS = ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "GROK_CONFIG", "GROK_CONFIG_PATH", "GITHUB_TOKEN")
CLEAN = {"plugins": [], "hooks": [], "mcpServers": []}
# What `grok inspect --json` may list without a run being refused: a plugin that's off, one that's on but brings
# only skills, a hook that's off
ALLOWED = {
    "plugins": [{"name": "cache-writer", "enabled": False, "provides": {"hooks": 2, "mcpServers": 1}},
                {"name": "skills-only", "enabled": True, "provides": {"skills": 3, "hooks": 0, "mcpServers": []}}],
    "hooks": [{"name": "old-hook", "event": "PostToolUse", "compatibilityStatus": "disabled"}],
    "mcpServers": [],
}
# What the run still goes ahead with: MCP servers, which the run's --tools and --disallowed-tools keep from the model,
# and which the handback names in its note
WITH_MCP = {
    "plugins": [{"name": "fs-tools", "enabled": True, "provides": ["skills", "mcpServers"]}],
    "hooks": [],
    "mcpServers": [{"name": "filesystem", "command": "npx"}],
}


def grok_result(**changes):
    return json.dumps({**GROK_RESULT, **changes})


def fake_grok(tmp_path, inspect=CLEAN, code=0, help_text=GROK_HELP, sleep=0):
    """A `grok` on disk. Its --help prints `help_text`. `inspect --json` (with or without a --cwd before it) notes
    (in inspect.log) where it ran, what environment and what arguments it was given, prints `inspect` (a dict, as
    JSON, or text as it is) and exits with `code`."""
    text = inspect if isinstance(inspect, str) else json.dumps(inspect)
    (tmp_path / "bin").mkdir(exist_ok=True)
    lines = [
        "import json, os, sys, time",
        "if sys.argv[-2:] == ['inspect', '--json']:",
        f"    with open({str(tmp_path / 'inspect.log')!r}, 'a', encoding='utf-8') as seen:",
        "        names = ('HOME', 'USERPROFILE', 'GROK_MEMORY', 'XAI_API_KEY')",
        "        seen.write(json.dumps([os.getcwd(), {n: os.environ.get(n) for n in names}, sys.argv[1:]]) + '\\n')",
        f"    time.sleep({sleep})",
        f"    sys.stdout.write({text!r})",
        f"    sys.stderr.write({'boom: no config' if code else ''!r})",
        f"    sys.exit({code})",
        f"print({help_text!r})",
    ]
    return script_program(tmp_path / "bin", "grok", "\n".join(lines) + "\n")


def inspected(tmp_path):
    """Each `grok inspect --json` the fake answered: [its working folder, the environment it saw, its arguments]."""
    log = tmp_path / "inspect.log"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def grok_worker(tmp_path, board, repo, agent, help_text=GROK_HELP, **fake):  # noqa: F811
    """`fake`: how the fake grok answers `inspect --json` (fake_grok's inspect, code and sleep)."""
    worker.load_agents()
    AGENTS["grok"] = adapters.presets()["grok"]
    return Worker(board, repo, "grok", runner=agent, command=fake_grok(tmp_path, help_text=help_text, **fake))


def enable_grok(config):  # noqa: F811
    config("")
    adapters.set_enabled("grok", True)


def test_grok_waits_until_you_enable_it_then_runs(tmp_path, board, repo, config):  # noqa: F811
    config("")
    agent = FakeAgent(write_hello, stdout=grok_result())
    task = approve(board, "grok")
    grok = grok_worker(tmp_path, board, repo, agent)
    with pytest.raises(WorkerUnavailable, match="handoff agents enable grok"):
        grok.check_login()
    [waiting] = grok.run_pending()
    assert not waiting.ok and "handoff agents enable grok" in waiting.waiting and agent.calls == []
    assert board.pending_runs("grok"), "the approval is kept for when you enable it"

    adapters.set_enabled("grok", True)
    grok.check_login()
    [result] = grok.run_pending()
    assert result.ok, result.message
    assert board.get(task.id).status == "handed_off"


def test_the_command_line_confines_grok_and_the_task_goes_in_a_file(tmp_path, board, repo, config):  # noqa: F811
    enable_grok(config)
    seen = {}

    def act(folder, argv):
        seen["task"] = Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8")
        write_hello(folder)
    agent = FakeAgent(act, stdout=grok_result())
    task = approve(board, "grok", models=["grok-4.7"], efforts={"grok-4.7": "high"})
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert result.ok, result.message
    [call] = agent.calls
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "read_file,search_replace,write,list_dir,grep"
    assert argv[argv.index("--disallowed-tools") + 1] == "search_tool,use_tool"
    assert argv[argv.index("--sandbox") + 1] == "strict"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert {"--disable-web-search", "--no-subagents", "--always-approve"} <= set(argv)
    assert argv[argv.index("--cwd") + 1] == call["cwd"] and Path(call["cwd"]).name == task.ref
    assert argv[argv.index("-m") + 1] == "grok-4.7" and argv[argv.index("--reasoning-effort") + 1] == "high"
    assert "Add a hello script" in seen["task"] and "Add a hello script" not in " ".join(argv)
    assert call["stdin"] == ""
    assert board.get(task.id).status == "handed_off"


def test_always_approve_comes_only_with_the_confinement():
    spec = adapters.presets()["grok"]
    args = list(spec.args)
    if "--always-approve" in args:
        assert "--tools" in args
        assert args[args.index("--disallowed-tools") + 1] == "search_tool,use_tool"
        assert args[args.index("--sandbox") + 1] == "strict"
    for flag in ("--tools", "--disallowed-tools", "--sandbox", "--always-approve"):
        assert flag in spec.required_flags
    assert spec.env_prefixes == () and spec.needs_ok


def test_grok_gets_no_xai_key_and_its_switches_are_off(tmp_path, board, repo, config, monkeypatch):  # noqa: F811
    for name in SECRETS:
        monkeypatch.setenv(name, "fake-for-the-test")
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=grok_result())
    approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert result.ok, result.message
    env = agent.calls[0]["env"]
    assert not any(name in env for name in SECRETS)
    assert env["GROK_MEMORY"] == "0" and env["GROK_TELEMETRY_ENABLED"] == "0"
    assert env["GROK_DISABLE_AUTOUPDATER"] == "1" and env["GROK_CLAUDE_HOOKS_ENABLED"] == "0"
    assert env["GROK_CURSOR_MCPS_ENABLED"] == "0"


def test_grok_usage_is_read_from_its_model_usage(tmp_path, board, repo, config):  # noqa: F811
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=grok_result())
    approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert result.ok, result.message
    assert result.used["models"]["grok-4.7-build"] == {"in": 1200, "out": 300, "cache_read": 100,
                                                       "cache_write": 0, "cost": 0.01}


def test_parse_grok_reads_the_reply_usage_and_error():
    reply, used, error = usage.parse_grok(grok_result())
    assert reply == REPLY and error is None
    assert used["models"]["grok-4.7-build"]["in"] == 1200 and used["models"]["grok-4.7-build"]["cost"] == 0.01


@pytest.mark.parametrize("stdout, said", [
    ("plain answer", 'It printed: "plain answer"'),   # not JSON: a crash, or output cut off
    ("", "It printed nothing"),
    ('{"text": "half an answ', "wasn't its JSON result"),
    ("Traceback\n  boom\n" + "x" * 500, "wasn't its JSON result"),
    ("[]", "wasn't a JSON object"),
    ('"end_turn"', "wasn't a JSON object"),
    ("null", "wasn't a JSON object"),
    ("[" * 50_000, "wasn't its JSON result"),         # too deep to read
])
def test_parse_grok_output_that_isnt_a_json_result_object_is_an_error(stdout, said):
    reply, used, error = usage.parse_grok(stdout)
    assert used is None and error and said in error
    assert len(error) < 400, "a short snippet, not the whole output"


def test_parse_grok_snippet_of_a_crash_is_its_end_without_line_breaks():
    _, _, error = usage.parse_grok("starting\n" + "y" * 300 + "\npanic: out of memory\n")
    assert "panic: out of memory" in error and "starting" not in error and "\n" not in error


@pytest.mark.parametrize("change", [
    {"stopReason": None}, {"stopReason": ""}, {"stopReason": "  "}, {"stopReason": 7}, {"stopReason": True},
    {"stopReason": ["end_turn"]}, {"stopReason": {"end_turn": 1}},
])
def test_parse_grok_a_stop_reason_that_isnt_a_non_empty_string_is_an_error(change):
    reply, used, error = usage.parse_grok(grok_result(**change))
    assert error and "no stopReason" in error
    assert reply == REPLY and used is not None, "what it did say is kept, for the board to show"


def test_parse_grok_a_result_with_no_stop_reason_is_an_error_whatever_else_it_has():
    data = {key: value for key, value in GROK_RESULT.items() if key != "stopReason"}
    assert "no stopReason" in usage.parse_grok(json.dumps(data))[2]
    assert "no stopReason" in usage.parse_grok(json.dumps({"text": REPLY}))[2]  # a cut-off payload with text
    assert "no stopReason" in usage.parse_grok("{}")[2]


@pytest.mark.parametrize("reason", ["max_tokens", "refusal", "END_TURN", "end_turn ", "error"])
def test_parse_grok_only_exactly_end_turn_is_a_finished_run(reason):
    _, _, error = usage.parse_grok(grok_result(stopReason=reason))
    assert error and "before it finished" in error and "stopReason" in error


def test_parse_grok_cancelled_keeps_its_own_words():
    _, _, error = usage.parse_grok(grok_result(stopReason="cancelled"))
    assert error == "Grok stopped the run because a tool call was refused (stopReason cancelled)."


def test_a_cancelled_grok_run_fails_and_says_a_tool_call_was_refused(tmp_path, board, repo, config):  # noqa: F811
    enable_grok(config)
    agent = FakeAgent(stdout=grok_result(stopReason="cancelled"))
    task = approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert not result.ok and "a tool call was refused" in result.message
    assert board.get(task.id).status != "in_progress"


@pytest.mark.parametrize("stdout, said", [
    ("Segmentation fault", "wasn't its JSON result"),
    ('{"text": "I added the file", "modelUsage": {}}', "no stopReason"),
    (grok_result(stopReason="max_tokens"), "before it finished"),
])
def test_a_grok_run_that_didnt_finish_fails_not_succeeds(tmp_path, board, repo, config, stdout, said):  # noqa: F811
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=stdout)
    task = approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert not result.ok and said in result.message
    assert board.get(task.id).status != "handed_off"
    assert board.get(task.id).status != "in_progress"


def test_what_grok_said_on_stderr_goes_with_the_error_so_a_usage_limit_is_still_seen(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    from handoff import limits
    enable_grok(config)
    grok = grok_worker(tmp_path, board, repo, FakeAgent())
    proc = subprocess.CompletedProcess(["grok"], 1, "", "Error:\n  You've hit your usage limit. Try again later.\n")
    reply, used, error = grok._read(proc)
    assert "wasn't its JSON result" in error and "usage limit" in error and "\n" not in error
    assert limits.out_of_usage(error)
    ok = subprocess.CompletedProcess(["grok"], 0, grok_result(), "some warning")
    assert grok._read(ok)[2] is None, "a finished run's stderr is not an error"


@pytest.mark.parametrize("flag", ["--sandbox", "--disallowed-tools"])
def test_an_older_grok_without_the_flags_that_confine_it_cant_run(tmp_path, board, repo, flag):  # noqa: F811
    older = GROK_HELP.replace(flag, "")
    with pytest.raises(WorkerUnavailable, match=flag):
        grok_worker(tmp_path, board, repo, FakeAgent(), help_text=older).check()


# ── Your own Grok plugins, hooks and MCP servers (`grok inspect --json`) ─────

def run_refused(tmp_path, board, repo, config, **fake):  # noqa: F811
    """Approve a grok run against this `grok inspect` answer: it's refused, grok's edit run never starts and the
    worktree is as it was made. Returns the reason the task was given."""
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=grok_result())
    task = approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent, **fake).run_pending()
    assert not result.ok and agent.calls == [], "the run never started"
    assert len(inspected(tmp_path)) == 1
    assert board.get(task.id).status == "blocked"
    folder = repo / gitwork.WORKTREES / task.ref
    assert folder.is_dir() and not (folder / "src").exists()
    assert git(folder, "status", "--porcelain").stdout == ""
    assert git(folder, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "HEAD").stdout
    assert result.commit is None and "othing was changed" in result.message
    return result.message


def test_only_grok_is_checked_with_inspect_before_its_runs():
    specs = adapters.presets()
    assert specs["grok"].inspect_args == ("inspect", "--json")
    assert [name for name, spec in specs.items() if spec.inspect_args] == ["grok"]
    assert not worker.CLAUDE_EDIT_ONLY.inspect_args


def test_a_clean_inspect_lets_the_run_go_ahead_in_the_worktree_with_its_environment(  # noqa: F811
        tmp_path, board, repo, config, monkeypatch):  # noqa: F811
    monkeypatch.setenv("XAI_API_KEY", "fake-for-the-test")
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=grok_result())
    task = approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert result.ok, result.message
    assert len(agent.calls) == 1
    [[cwd, env, args]] = inspected(tmp_path)
    assert Path(cwd).resolve() == (repo / gitwork.WORKTREES / task.ref).resolve()
    assert env["HOME"] == os.environ.get("HOME") and env["USERPROFILE"] == os.environ.get("USERPROFILE")
    assert env["GROK_MEMORY"] == "0" and env["XAI_API_KEY"] is None
    run = agent.calls[0]["argv"]
    assert args == ["--cwd", run[run.index("--cwd") + 1], "inspect", "--json"], "the run's own --cwd"
    assert Path(args[1]).resolve() == (repo / gitwork.WORKTREES / task.ref).resolve()


def test_the_task_file_is_in_the_worktree_where_groks_sandbox_reads_and_is_gone_after(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    enable_grok(config)
    seen = {}

    def act(folder, argv):
        path = Path(argv[argv.index("--prompt-file") + 1])
        seen.update(path=path, text=path.read_text(encoding="utf-8"), folder=folder)
        write_hello(folder)
    agent = FakeAgent(act, stdout=grok_result())
    approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert result.ok, result.message
    assert "Add a hello script" in seen["text"]
    assert seen["folder"].resolve() in seen["path"].resolve().parents, "inside the worktree, not a temp folder"
    assert not seen["path"].exists() and not (seen["folder"] / ".handoff").exists()
    committed = git(repo, "ls-tree", "-r", "--name-only", result.commit).stdout.split()
    assert "src/hello.py" in committed and not any(".handoff" in name for name in committed)


def test_plugins_and_hooks_that_are_off_or_bring_nothing_that_runs_dont_stop_a_run(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=grok_result())
    task = approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent, inspect=ALLOWED).run_pending()
    assert result.ok, result.message
    assert len(agent.calls) == 1 and board.get(task.id).status == "handed_off"


def test_an_enabled_plugin_with_hooks_refuses_the_run_and_the_message_says_how_to_turn_it_off(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    inspect = {"plugins": [{"name": "cache-writer", "enabled": True, "provides": {"hooks": 1, "skills": 2}},
                           {"name": "off", "enabled": False, "provides": {"hooks": 1}}],
               "hooks": [], "mcpServers": []}
    message = run_refused(tmp_path, board, repo, config, inspect=inspect)
    assert 'plugin "cache-writer" (enabled, provides hooks)' in message and '"off"' not in message
    assert "`grok plugin disable NAME`" in message
    assert "the run's --tools and --disallowed-tools keep MCP tools away" in message
    assert "remove the MCP server" not in message


def run_noted(tmp_path, board, repo, config, **fake):  # noqa: F811
    """Approve a grok run against this `grok inspect` answer, which lets it go ahead: it finishes, grok's edit run
    starts once, and the task is handed off. Returns the handback's left text, where the note on MCP servers is."""
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=grok_result())
    task = approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent, **fake).run_pending()
    assert result.ok, result.message
    assert len(agent.calls) == 1 and board.get(task.id).status == "handed_off"
    return board.events(task.id)[-1].data["left"]


def test_a_plugin_that_provides_mcp_servers_runs_and_the_note_names_it_with_the_top_level_ones(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    left = run_noted(tmp_path, board, repo, config, inspect=WITH_MCP)
    assert ("Grok Build loaded your MCP servers filesystem, fs-tools (plugin); their tools were kept from the model "
            "with --disallowed-tools.") in left


def test_top_level_mcp_servers_dont_stop_a_run_and_the_note_names_them(tmp_path, board, repo, config):  # noqa: F811
    inspect = {"plugins": [], "hooks": [], "mcp_servers": [{"name": "filesystem"}, {"id": "opentakeoff"}]}
    left = run_noted(tmp_path, board, repo, config, inspect=inspect)
    assert "Grok Build loaded your MCP servers filesystem, opentakeoff; " in left
    assert "their tools were kept from the model with --disallowed-tools." in left


def test_a_plugin_that_names_its_hooks_on_itself_refuses_the_run(tmp_path, board, repo, config):  # noqa: F811
    inspect = {"plugins": [{"name": "own", "enabled": True, "hooks": [{"event": "PostToolUse"}]},
                           {"name": "spelled", "enabled": True, "provides": {"mcp-servers": ["fs"]}}],
               "hooks": [], "mcpServers": []}
    message = run_refused(tmp_path, board, repo, config, inspect=inspect)
    assert 'plugin "own" (enabled, provides hooks)' in message
    assert "spelled" not in message


def test_a_plugin_with_hooks_and_mcp_servers_is_refused_naming_only_the_hooks(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    inspect = {"plugins": [{"name": "both", "enabled": True, "provides": {"hooks": 1, "mcpServers": 2}}],
               "hooks": [], "mcpServers": []}
    message = run_refused(tmp_path, board, repo, config, inspect=inspect)
    assert 'plugin "both" (enabled, provides hooks)' in message and "mcpServers" not in message


def test_inspect_that_lists_only_some_of_its_lists_refuses_the_run(tmp_path, board, repo, config):  # noqa: F811
    message = run_refused(tmp_path, board, repo, config, inspect={"plugins": []})
    assert "printed something Handoff can't read (no hooks list)" in message


def test_a_hook_whose_compatibility_status_is_enabled_refuses_the_run(tmp_path, board, repo, config):  # noqa: F811
    inspect = {"plugins": [], "hooks": [{"name": "format-on-save", "compatibilityStatus": "enabled"},
                                        {"name": "quiet", "compatibilityStatus": "disabled"}], "mcpServers": []}
    message = run_refused(tmp_path, board, repo, config, inspect=inspect)
    assert 'hook "format-on-save" (enabled)' in message and "quiet" not in message


# The shapes `grok inspect --json` really prints on a Mac with the impeccable plugin: the plugin's own hook has no
# compatibilityStatus, and neither does it have a name or id
REAL_HOOKS = [{"event": "session_start", "hookType": "command", "target": "/home/u/.grok/hooks/start.sh",
               "source": {"type": "user", "path": "/home/u/.grok/settings.json"}, "matcher": None,
               "vendor": "claude", "compatibilityStatus": "enabled"},
              {"event": "(plugin)", "hookType": "file", "target": "/home/u/.grok/plugins/impeccable/hooks.json",
               "source": {"type": "user", "path": "/home/u/.grok/plugins/impeccable"}, "matcher": None}]
REAL_PLUGINS = [{"name": "impeccable", "scope": "user", "path": "/home/u/.grok/plugins/impeccable", "enabled": True,
                 "provides": {"skills": 1, "agents": 1, "hooks": True, "mcpServers": 0}}]


def test_a_real_grok_setup_with_a_plugin_and_hooks_without_status_refuses_naming_the_plugin(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    inspect = {"plugins": REAL_PLUGINS, "hooks": REAL_HOOKS, "mcpServers": []}
    message = run_refused(tmp_path, board, repo, config, inspect=inspect)
    assert "can't read" not in message, "it was refused as what it found, not as unreadable"
    assert 'plugin "impeccable" (enabled, provides hooks)' in message
    assert 'hook "session_start /home/u/.grok/hooks/start.sh" (enabled)' in message
    assert "/home/u/.grok/plugins/impeccable/hooks.json" in message
    assert "`grok plugin disable impeccable`" in message


def test_inspect_findings_counts_a_hook_without_status_as_enabled():
    hooks = REAL_HOOKS + [{"event": "stop", "hookType": "command", "target": "/home/u/.grok/hooks/stop.sh",
                           "compatibilityStatus": "disabled"}]
    found = adapters.inspect_findings(json.dumps({"plugins": REAL_PLUGINS, "hooks": hooks, "mcpServers": []}))
    assert len(found) == 3
    assert found == ['plugin "impeccable" (enabled, provides hooks)',
                     'hook "session_start /home/u/.grok/hooks/start.sh" (enabled)',
                     'hook "(plugin) /home/u/.grok/plugins/impeccable/hooks.json" (enabled)']


def test_a_hook_refusal_says_where_the_hook_is_set_and_doesnt_mention_mcp_servers(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    inspect = {"plugins": [], "hooks": [{"name": "format-on-save", "compatibilityStatus": "enabled"}],
               "mcpServers": [{"name": "filesystem", "command": "npx"}]}
    message = run_refused(tmp_path, board, repo, config, inspect=inspect)
    assert 'hook "format-on-save" (enabled)' in message and "`grok inspect --json`" in message
    assert str(hosts.grok_config_path()) in message, "the path this machine's code resolves, not a POSIX-only one"
    assert "the project's own Grok config" in message, "it can come from the project too"
    assert "remove the MCP server" not in message and "filesystem" not in message


def test_the_refusal_names_what_it_found_without_control_characters_and_not_too_many(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    hooks = [{"name": f"hook-{n}", "compatibilityStatus": "enabled"} for n in range(10)]
    hooks[0]["name"] = "evil\x1b[31m\x07name"
    message = run_refused(tmp_path, board, repo, config, inspect={**CLEAN, "hooks": hooks})
    assert "\x1b" not in message and "\x07" not in message
    assert 'hook "hook-7" (enabled)' in message and "hook-8" not in message and "and 2 more" in message


def test_the_note_names_mcp_servers_without_control_characters_and_not_too_many(  # noqa: F811
        tmp_path, board, repo, config):  # noqa: F811
    servers = [{"name": f"server-{n}"} for n in range(10)]
    servers[0]["name"] = "evil\x1b[31m\x07name"
    left = run_noted(tmp_path, board, repo, config, inspect={**CLEAN, "mcpServers": servers})
    assert "\x1b" not in left and "\x07" not in left
    assert "server-7" in left and "server-8" not in left and "and 2 more" in left


def test_inspect_that_exits_with_an_error_refuses_the_run(tmp_path, board, repo, config):  # noqa: F811
    message = run_refused(tmp_path, board, repo, config, inspect=CLEAN, code=3)
    assert "exit code 3" in message and "boom: no config" in message
    assert "can't tell which of your Grok plugins, hooks or MCP servers would run" in message


def test_inspect_that_doesnt_answer_in_time_refuses_the_run(tmp_path, board, repo, config, monkeypatch):  # noqa: F811
    monkeypatch.setattr(adapters, "INSPECT_TIMEOUT", 1.0)
    message = run_refused(tmp_path, board, repo, config, inspect=CLEAN, sleep=30)
    assert "didn't answer within 1 second" in message


@pytest.mark.parametrize("answer", [
    "Grok Build 1.2.3\nplugins: none\n",   # not JSON
    "",
    "[]",                                  # not an object
    '{"unrelated": true}',                 # an object, but not inspect's
    '{"plugins": []}',                     # the other lists weren't said, which isn't saying they're empty
    '{"plugins": [], "hooks": []}',        # no MCP servers list
    '{"plugins": "none", "hooks": [], "mcpServers": []}',
    '{"plugins": [], "hooks": [], "mcpServers": [], "error": "could not read config"}',  # a 0 exit, with an error
    '{"plugins": [{"name": "p", "provides": {"hooks": 1}}], "hooks": [], "mcpServers": []}',  # no "enabled"
    '{"plugins": [{"name": "p", "enabled": true}], "hooks": [], "mcpServers": []}',  # no "provides"
    '{"plugins": [], "hooks": ["h"], "mcpServers": []}',  # a hook that isn't an object
    "[" * 50_000,                          # too deep to read
])
def test_an_inspect_answer_that_cant_be_read_refuses_the_run(tmp_path, board, repo, config, answer):  # noqa: F811
    message = run_refused(tmp_path, board, repo, config, inspect=answer)
    assert "printed something Handoff can't read" in message


def plugin(**fields):
    return {**CLEAN, "plugins": [{"name": "a", "enabled": True, **fields}]}


@pytest.mark.parametrize("answer, found", [
    (CLEAN, []),
    ({"plugins": [], "hooks": [], "mcp_servers": []}, []),   # the other spelling of the MCP list
    (ALLOWED, []),
    (plugin(provides=[]), []),
    (plugin(provides={"mcp": 0, "hooks": [], "mcpServers": None}), []),
    (plugin(provides=["skills", "commands"]), []),
    (plugin(hooks=[], mcpServers={}), []),                    # its own keys, with nothing in them
    ({**plugin(provides={"hooks": 1, "mcp": True}), "plugins": [
        {"name": "off", "enabled": False, "provides": {"hooks": 1, "mcp": True}, "hooks": [1], "mcp": True}]}, []),
    (plugin(provides=["Hooks"]), ['plugin "a" (enabled, provides Hooks)']),
    (plugin(provides="hooks"), ['plugin "a" (enabled, provides hooks)']),
    (plugin(provides=["hook"]), ['plugin "a" (enabled, provides hook)']),
    (plugin(provides={"mcp": True}), []),                     # MCP servers are a note, not a refusal
    (plugin(provides={"mcp-servers": ["fs"]}), []),
    (plugin(provides={"MCP_Servers": 1}), []),
    (plugin(provides={"mcpServers": 1, "hooks": 2}), ['plugin "a" (enabled, provides hooks)']),
    (plugin(hooks=[{"event": "PostToolUse"}]), ['plugin "a" (enabled, provides hooks)']),
    (plugin(hook={"event": "x"}), ['plugin "a" (enabled, provides hook)']),
    (plugin(mcpServers={"fs": {}}), []),
    (plugin(mcp_servers=["fs"]), []),
    (plugin(mcp=True), []),
    (plugin(hooks=[1], provides=["hooks"]), ['plugin "a" (enabled, provides hooks)']),
    ({**CLEAN, "hooks": [{"name": "h", "compatibilityStatus": " Enabled "}]}, ['hook "h" (enabled)']),
    ({**CLEAN, "hooks": [{"name": "h"}]}, ['hook "h" (enabled)']),  # no compatibilityStatus: it runs
    ({**CLEAN, "hooks": [{"name": "h", "compatibilityStatus": "disabled"}]}, []),
    ({**CLEAN, "mcpServers": ["one", "two"]}, []),
    ({**CLEAN, "mcpServers": [], "mcp_servers": [{"id": "x"}]}, []),
    ({"plugins": [], "hooks": [], "mcp_servers": [{"id": "x"}]}, []),
])
def test_inspect_findings_counts_what_would_run_whatever_the_spelling(answer, found):
    assert adapters.inspect_findings(json.dumps(answer)) == found


@pytest.mark.parametrize("answer, refusals, names", [
    ({"plugins": [], "hooks": [], "mcpServers": [{"name": "fs", "command": "npx"}, {"id": "gh"}, "plain"]},
     [], ["fs", "gh", "plain"]),
    ({"plugins": [], "hooks": [], "mcp_servers": [{"name": "fs"}]}, [], ["fs"]),
    # the same name under both spellings is listed once
    ({"plugins": [], "hooks": [], "mcpServers": ["fs"], "mcp_servers": [{"name": "fs"}, {"name": "gh"}]},
     [], ["fs", "gh"]),
    (plugin(mcp_servers=["fs"]), [], ["a (plugin)"]),
    ({**CLEAN, "plugins": [{"name": "off", "enabled": False, "provides": {"mcpServers": 1}}]}, [], []),
    ({"plugins": [{"name": "fs-tools", "enabled": True, "provides": ["skills", "mcpServers"]}], "hooks": [],
      "mcpServers": [{"name": "filesystem"}]}, [], ["filesystem", "fs-tools (plugin)"]),
    ({"plugins": [{"name": "both", "enabled": True, "provides": {"hooks": 1, "mcpServers": 2}}], "hooks": [],
      "mcpServers": [{"name": "fs"}]}, ['plugin "both" (enabled, provides hooks)'], ["fs"]),
])
def test_inspect_report_names_mcp_servers_whatever_the_spelling(answer, refusals, names):
    assert adapters.inspect_report(json.dumps(answer)) == (refusals, names)


@pytest.mark.parametrize("answer", [
    {"plugins": []},                                    # hooks and MCP servers weren't listed
    {"hooks": []},
    {"mcpServers": []},
    {"plugins": [], "hooks": []},
    {"plugins": [], "mcpServers": []},
    {"hooks": [], "mcpServers": []},
    {"plugins": None, "hooks": None, "mcpServers": None},
    {**CLEAN, "plugins": None},
    {**CLEAN, "hooks": None},
    {**CLEAN, "mcpServers": None},
    {**CLEAN, "mcp_servers": None},
    {**CLEAN, "plugins": {}},                           # not lists
    {**CLEAN, "hooks": {"h": {"compatibilityStatus": "enabled"}}},
    {**CLEAN, "mcpServers": {"fs": {"command": "npx"}}},
    {**CLEAN, "plugins": "none"},
    {**CLEAN, "hooks": 3},
    {**CLEAN, "mcpServers": "fs"},
    {"plugins": [], "hooks": [], "mcp_servers": "fs"},
    {**CLEAN, "error": "could not read the config"},    # an in-band error, with empty lists
    {**CLEAN, "errors": [{"message": "bad hook"}]},
    {**CLEAN, "plugins": [{"name": "a", "enabled": "yes", "provides": []}]},
    {**CLEAN, "plugins": [{"name": "a", "enabled": True}]},                       # nothing about what it brings
    {**CLEAN, "plugins": [{"name": "a", "enabled": True, "provides": 3}]},
    {**CLEAN, "plugins": [{"name": "a", "enabled": True, "provides": [{"kind": "hooks"}]}]},
    {**CLEAN, "plugins": ["a"]},
    {**CLEAN, "hooks": [{"name": "h", "compatibilityStatus": True}]},
    {**CLEAN, "hooks": ["h"]},
    {"version": "1"},
    {},
])
def test_inspect_findings_refuses_what_it_doesnt_know_the_shape_of(answer):
    with pytest.raises(ValueError):
        adapters.inspect_findings(json.dumps(answer))
    with pytest.raises(ValueError):
        adapters.inspect_report(json.dumps(answer))


def test_inspect_findings_says_which_list_it_didnt_find():
    for answer, what in (({"plugins": []}, "no hooks list"), ({"hooks": []}, "no plugins list"),
                         ({"plugins": [], "hooks": []}, "no mcpServers list"),
                         ({**CLEAN, "hooks": {}}, "its hooks aren't a list"),
                         ({**CLEAN, "error": "boom"}, "it reported an error: boom")):
        with pytest.raises(ValueError, match=what):
            adapters.inspect_findings(json.dumps(answer))


def test_an_empty_or_false_error_field_is_no_error():
    for empty in (None, "", [], {}, False):
        assert adapters.inspect_findings(json.dumps({**CLEAN, "error": empty})) == []


def test_the_note_doesnt_say_grok_is_confined_to_the_worktree_for_writes():
    # The run uses the real HOME (the SuperGrok login), so ~/.grok and /tmp are within what its sandbox may write
    note = adapters.presets()["grok"].note
    assert "/tmp" in note and "~/.grok" in note and "may still write" in note
    assert "No commands or tests were run" in note


def test_grok_runs_with_the_real_home_and_none_of_your_grok_config_variables(  # noqa: F811
        tmp_path, board, repo, config, monkeypatch):  # noqa: F811
    # What the note above owns up to: nothing swaps HOME or GROK_CONFIG, because that would hide the login
    monkeypatch.setenv("GROK_CONFIG", str(tmp_path / "mine.toml"))
    monkeypatch.setenv("GROK_CONFIG_PATH", str(tmp_path / "mine.toml"))
    enable_grok(config)
    agent = FakeAgent(write_hello, stdout=grok_result())
    approve(board, "grok")
    [result] = grok_worker(tmp_path, board, repo, agent).run_pending()
    assert result.ok, result.message
    [[_cwd, env, _args]] = inspected(tmp_path)
    assert env["HOME"] == os.environ.get("HOME")
    env_seen = agent.calls[0].get("env") or {}
    assert "GROK_CONFIG" not in env_seen and "GROK_CONFIG_PATH" not in env_seen
