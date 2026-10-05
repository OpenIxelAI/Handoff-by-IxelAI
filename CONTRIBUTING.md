# Working on Handoff

## Install from source

You need Python 3.10 or newer and Git. The installer makes a private environment in your home folder and adds
the `handoff` command. It never uses `sudo` or touches system Python.

macOS and Linux:

```bash
git clone https://github.com/OpenIxelAI/Handoff-by-IxelAI.git
cd Handoff-by-IxelAI
./install.sh
```

If Python is missing or too old, the installer names the command to get it. It uses
[uv](https://docs.astral.sh/uv/) when it's installed. Set `HANDOFF_PYTHON=/path/to/python` to choose the
interpreter: the install keeps your choice, and `handoff update` uses it again.

Windows, in a normal PowerShell window (not "Run as administrator"), one line at a time:

```powershell
cd ~
git clone https://github.com/OpenIxelAI/Handoff-by-IxelAI.git
cd Handoff-by-IxelAI
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

If Git or Python is missing: `winget install Git.Git` or `winget install Python.Python.3.13`, then open a new
window and start again. Where Smart App Control is on, the installer makes `handoff` a `handoff.cmd` that runs
the install's own signed python.exe, and `handoff setup --write` points your apps at it.

## Run the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

**On Windows**, run these in the Handoff folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python scripts\check_windows.py
```

It runs the tests, then the live tests with the Claude Code and Codex you have installed, against a fake model
on your machine: nothing is sent or billed, and your own settings aren't touched. Then it measures what Codex's
Windows sandbox lets commands do; before it tries Codex's "elevated" sandbox, which needs an administrator
prompt, it asks. It writes `windows-check-report.txt`; attach it to an issue.

`tests/test_docs.py` checks that [docs/guide.md](docs/guide.md) names every tool, command, status and done rule,
so update the guide with the code.
