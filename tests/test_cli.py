"""The `handoff` command itself: dispatch, version, help."""
import os
import subprocess
import sys

from handoff import __version__, cli


def run(*args, **kwargs):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **kwargs.pop("env", {})}
    return subprocess.run([sys.executable, "-m", "handoff", *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=60, **kwargs)


def test_version():
    proc = run("version")
    assert proc.returncode == 0, proc.stderr
    assert f"v{__version__}" in proc.stdout


def test_version_flags_and_help(tmp_path):
    assert run("--version").returncode == 0
    for flag in ("--help", "-h", "help"):
        proc = run(flag)
        assert proc.returncode == 0 and "handoff version" in proc.stdout
    proc = run(cwd=tmp_path)  # on its own, outside a project: how to start
    assert proc.returncode == 0 and "handoff help lists every command" in proc.stdout


def test_unknown_command_says_so():
    proc = run("frobnicate")
    assert proc.returncode == 2
    assert "Unknown command: frobnicate" in proc.stderr


def test_main_returns_exit_codes_in_process(capsys):
    assert cli.main(["version"]) == 0
    assert cli.main(["nope"]) == 2
