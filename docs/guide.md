# Handoff guide

Everything Handoff does, in one place. The [README](../README.md) has the install, and
[ixelai.com/docs/handoff](https://ixelai.com/docs/handoff/) is a shorter tour, with a walkthrough.

- [Connect your apps](#connect-your-apps)
- [Hand out work to any agent (dispatch)](#hand-out-work-to-any-agent-dispatch)
- [The worker (preview)](#the-worker-preview)
- [What the agents can do](#what-the-agents-can-do)
- [Your controls](#your-controls)
- [The rules](#the-rules)
- [Reviews with the Ixel panel (optional)](#reviews-with-the-ixel-panel-optional)
- [Where things live](#where-things-live)
- [Troubleshooting](#troubleshooting)

## Connect your apps

Go to your project's folder and run:

```bash
handoff setup           # shows the config for each app
handoff setup --write   # adds it for you
```

`--write` only touches apps that are installed, keeps everything else in their config, and is safe to run
again. Then **restart the apps**.

For Claude Code, `--write` also adds a **session hook**: when you start a session in a project, Claude hears
which tasks are assigned to it or blocked waiting on it, by number only ("2 tasks are assigned to you:
T-3, T-7"), so it can tell you before starting something else. It never sees a task's title or notes this
way, and it hears nothing when nothing's waiting. `--no-hook` leaves it out; `handoff setup --remove-hook`
takes it out again. `handoff setup --remove` takes out everything setup added, in every app. The old settings file is kept beside it as `settings.json.handoff-bak`. On Windows,
if your user folder's name has a space in it, the hook needs Claude Code 2.1.139 or newer.

| App | What `--write` does | Which project it uses |
|---|---|---|
| Claude Code | `claude mcp add --scope user handoff -- … mcp --as claude`, and the session hook in `~/.claude/settings.json` | The folder you open Claude Code in, so one setup covers every project |
| Codex (CLI, IDE extension or app) | Adds `[mcp_servers.handoff]` to `~/.codex/config.toml` (or `$CODEX_HOME`), with `tool_timeout_sec = 960` | The folder you run Codex in |
| Claude Desktop | Adds `handoff` to `claude_desktop_config.json` with `--project <this folder>` | The project you ran setup in (Desktop has no folder of its own). Run `handoff setup --write` in another project to switch |
| Gemini CLI | Adds `handoff` under `mcpServers` in `~/.gemini/settings.json`, as `gemini`, with a 960-second `timeout` | The folder you run Gemini CLI in |
| OpenCode | Adds `handoff` under `mcp` in `~/.config/opencode/opencode.json` (or `$XDG_CONFIG_HOME`; `opencode.jsonc` if that's yours), as `opencode`, with a 960-second `timeout` | The folder you run OpenCode in |
| Grok Build | Adds `[mcp_servers.handoff]` to `~/.grok/config.toml`, as `grok`, with `tool_timeout_sec = 960` | The folder you run `grok` in |

Each app joins the board under its own name: a task for `gemini` is in Gemini CLI's inbox, and OpenCode is
`opencode` whichever model it runs. Gemini CLI, OpenCode and Grok Build have no session hook, so ask them to
"check your handoffs". Grok Build also loads Claude Code's MCP servers and runs its hooks. Once Grok is
connected its own `handoff` comes first; until then it uses Claude's and works as `claude` on the board, which
`handoff doctor` points out. Claude Code's session hook says nothing to Grok. A settings file with comments or
trailing commas in it (JSONC) is read but never rewritten, since saving it would lose them: `handoff setup
--write` says so, and `handoff setup` shows the entry to add by hand.

The **ChatGPT app itself isn't supported**: its connectors only reach MCP servers on the internet, and
Handoff never listens on a network port. Codex signs in with the same ChatGPT subscription and works.

`handoff doctor` checks the project, the board, and which apps have Handoff set up.

### Or install it as a plugin (Claude Code and Codex)

Instead of `handoff setup`, you can add Handoff to Claude Code or Codex as a plugin. It connects the
board the same way, and adds a skill that shows the agent how to use it. You still need the `handoff`
program itself (see the [README](../README.md#install)); the plugin starts it, so the app must find `handoff` on its own
PATH. An app opened from the Dock, the Start menu or a launcher may not read your shell profile, so if
the `handoff_*` tools don't show up, uninstall the plugin and run `handoff setup --write` instead: it
gives the app the full path.

```bash
# Claude Code (or, inside Claude Code: /plugin marketplace add … then /plugin install handoff@ixelai)
claude plugin marketplace add OpenIxelAI/Handoff-by-IxelAI
claude plugin install handoff@ixelai

# Codex
codex plugin marketplace add OpenIxelAI/Handoff-by-IxelAI
codex plugin add handoff@ixelai
```

Use the plugin or `handoff setup` for an app, not both, or it gets two Handoff servers (`handoff doctor`
says so if it happens). Claude Desktop has no plugins, so it still uses `handoff setup`. Approving the
worker stays at your terminal either way: the plugin can't approve anything.

The plugin doesn't bring the session hook: a plugin can only start a bare `handoff`, which Claude Code's
hooks can't find where Smart App Control is on (it's `handoff.cmd` there). `handoff setup --hook` adds just
the hook.

## Hand out work to any agent (dispatch)

```bash
handoff dispatch "codex review my changes, grok make pictures of my app, claude finish the next step,
                  and gemini make me a list of projects to check out"
```

```
  1. Codex reviews · your uncommitted changes
     Review your uncommitted changes
  2. Grok makes pictures · 2 pictures, based on README.md
     Make pictures of my app
  3. Claude changes files
     Finish the next step
  4. Gemini answers
     Make me a list of projects to check out
  Run these 4 tasks? [Y/n]
```

Start each part with who does it. Handoff splits the request by plain rules (no model guesses), shows you
the plan, and on a yes adds one task per part, approves each for exactly that run, and runs them all at
once. Each task comes back to you on the board: `handoff show T-N`.

| Kind | Who | What happens | Where the result is |
|---|---|---|---|
| **edit** | claude, codex | The [worker](#the-worker-preview) runs it in its own worktree and branch | `handoff/T-N`, ready to review and merge |
| **review** | any model in Ixel MAT | It reads the files the part names ("grok audit README.md"), else your uncommitted changes and new files (else this branch since main, else your last commit), and says what's wrong. Ixel MAT reads the new files and leaves out links, key and login files, and ones with a key in them | The task, and `.handoff/outputs/T-N/answer.md` |
| **answer** | any model in Ixel MAT | It replies: a list, a plan, an explanation. Files the part names (`README.md`) are attached | The task, and `.handoff/outputs/T-N/answer.md` |
| **image** | grok (xAI), gpt (OpenAI) | Pictures. "Based on my work" means your README is read first, by a chat model, to describe them: the one you named if it's among your models in Ixel MAT, else one at the same company, else your first. The plan names it | `.handoff/outputs/T-N/` |

The kind comes from the words: a picture to make ("make 2 pictures of my app", "draw a logo", photos,
a wallpaper) → image; review, audit → review; finish, fix, build, the next step → edit (for claude and
codex); anything else → answer. A picture word in an edit or a review ("fix the image upload", "review the
icon code") doesn't make it a picture. A part that can't run here
says why (not set up, no key, nothing to review) and the rest still run.

Answers, reviews and pictures go through [Ixel MAT](https://github.com/OpenIxelAI/ixel-mat), which holds
your API keys: install it and run `ixel setup` to add your models. They come from the models you chose
there, so "codex review my changes" needs Codex among them, and "gemini …" needs Gemini. For an answer or
a review, "claude" means Claude Code in Ixel MAT (`claude_code`); for an edit, the claude the worker runs.
`handoff agents` shows who can do what on this computer, with an example that runs here. Without Ixel,
only claude and codex can take tasks, as edits.

- `--plan` shows the plan and adds nothing (not even a board). To make it, Handoff still checks which
  agents can run here: it starts `claude --help` and `codex exec --help`, tries the sandbox, and asks
  Ixel MAT which models it has. No model is asked anything. `--yes` skips the question; `--no-run` adds
  and approves the tasks for `handoff run` later.
- You can also approve one task for one kind of run: `handoff approve T-9 gemini --kind answer`,
  then `handoff run T-9`.
- A review or a change can be of a pull request. The Board in Ixel's app (`ixel gui`, from Ixel MAT) fetches
  one into your project and approves the run for exactly its two commits (where it starts and where it
  ends), which are sealed into the approval with the rest, so a board that changes them afterwards can't
  make the approval count. A review reads those commits whatever you have checked out (it needs an Ixel MAT
  that can review a branch you haven't checked out: `ixel update`). A change starts its `handoff/T-N`
  branch from the pull request's last commit, so when it's done, `git push origin
  handoff/T-N:refs/heads/<its branch>` adds the work to the pull request (if you can push there). Approved
  again for the same kind of run, from the Board or with `handoff approve`, a task keeps the commits it was
  approved for; review the pull request again from the Board to read newer ones. If that approval can't be
  checked any more (the project moved, or this computer's approval key was made again), approving again is
  refused rather than reviewing your own changes instead: start it again from the Board.
- Models answer from what they're sent; they can't see the rest of your project or change anything.
  Reviews and pictures may be paid API calls.

## The worker (preview)

The worker lets an agent work on a task while you're elsewhere, with nobody switching apps. On Linux it
runs **Claude Code or Codex inside an OS sandbox**, where the agent can run your tests and builds. On macOS
and Windows, for now, it runs **Claude Code edit-only**; Codex needs the sandbox. (On Windows, Codex 0.155.1's
unelevated sandbox refuses to run with the rule that keeps Codex's login unreadable, and without that rule its
commands can read your files and reach the network, so a Codex worker there would need Codex's elevated sandbox.)

```bash
handoff approve T-7 codex                # you approve one task, for one run (or claude)
handoff worker --agent codex             # runs it, then waits for your next approval (--once to stop)
```

1. The worker makes a git worktree for the task at `.handoff/worktrees/T-7`, on a new branch `handoff/T-7`
   from your current commit. Your own checkout isn't touched.
2. It runs the agent there, with the task exactly as it was when you approved it:
   - The agent runs inside [bubblewrap](https://github.com/containers/bubblewrap), which shows it nothing
     of yours but the worktree, and inside its own sandbox (Codex's `workspace-write`, Claude Code's
     `sandbox` setting), which takes the network away from the commands it runs. So it can run your tests
     and builds, with the tools installed on the system; it can't install anything, reach the network, or
     see your home folder or your other projects. Git doesn't work in there either (the repository is
     outside the worktree), and the agent is told so. Its login is a private copy its commands can't read.
   - Where the sandbox can't run, **Claude** runs as `claude -p --restricted` with only its file tools:
     read, edit, write and search. It can't run commands, tests or builds, and the handback says so.
3. When the agent finishes, the worker commits its changes on `handoff/T-7` (it never pushes, and it won't
   commit anything that looks like a secret; a JSON Web Token, often a test's sample, is committed and
   named in the handback for you to check) and hands the task back to you, or to whoever you named with
   `--return-to`, with what was done, what's left, and how to check it.
4. You review the branch, run the tests and merge it yourself: `git merge handoff/T-7`, then
   `git worktree remove .handoff/worktrees/T-7`.

`handoff approve` shows you the task first, as the agent will get it (its history too, notes and handoffs
included), and asks before approving (`--yes` skips the question). For an answer, review or picture, it
first checks that Ixel MAT has that model and that it can do that kind of run. Name the agent (`--worker NAME` works too); with no name, it offers the task's
assignee, but only when it can ask you. An approval is good for one run, and only while the task is as you approved
it: if anyone hands it on, reviews it, reassigns it or changes its status first, it lapses.
`handoff approve T-7 --revoke` withdraws it.

An approval only counts on the computer where you made it. It's sealed with a key kept in your user folder
(`%APPDATA%\Handoff\approval.key` on Windows, `~/Library/Application Support/Handoff` on a Mac,
`~/.config/handoff` on Linux), so a board that arrives inside a cloned repo can't carry one. The worker
lists any approvals it won't run for that reason, and approving the task again here makes it runnable.
Approvals made before this version count as made elsewhere, so approve those again.

**The sandbox needs Linux and bubblewrap** (`sudo apt install bubblewrap`, `sudo dnf install bubblewrap`
or `sudo pacman -S bubblewrap`); for Claude, also `socat` (the same way). macOS support is next. The agent
must be signed in with a login file, or have its API key set:

- **Codex:** `codex login` (if your login is in the system keyring, set
  `cli_auth_credentials_store = "file"` in `~/.codex/config.toml`), or `OPENAI_API_KEY`.
- **Claude Code:** sign in with `claude` (then /login), or set `ANTHROPIC_API_KEY` or
  `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`).

`handoff doctor` tells you whether each worker is ready, and in which mode. It looks for that login too.
A run that finds none doesn't start, and the task stays approved: sign in, then `handoff run T-N`.

**Tools installed in your home folder** (by rustup, nvm, pyenv, `pip --user` and the like) aren't in the
sandbox unless you share their folders, read-only, when you start the worker:

```bash
handoff worker --agent codex --share ~/.cargo --share ~/.rustup --share ~/.nvm
```

Handoff refuses to share your whole home folder, the project itself, and folders that hold credentials
(`~/.ssh`, `~/.aws`, `~/.config` and the like), and shows files that often hold a token empty inside the
folders you share (such as `~/.cargo/credentials.toml` or an `.npmrc`). Anything else in a shared folder is
readable by the agent's commands, so share the folders your tools are in, not the ones your work is in.

On Ubuntu 24.04 and later, AppArmor stops bubblewrap from starting (`bwrap: setting up uid map: Permission
denied`) until a profile allows it, and no package ships one. `handoff doctor` prints the command that adds
it; it is this one-line profile, in the same form Ubuntu ships for Flatpak and Chrome:

```bash
echo 'abi <abi/4.0>, profile bwrap /usr/bin/bwrap flags=(unconfined) { userns, }' | sudo tee /etc/apparmor.d/bwrap
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

## What the agents can do

| Tool | What it does |
|---|---|
| `handoff_inbox` | What's waiting for you: tasks assigned to you (new, handed back, or waiting for your review) with the latest handoff or review in full (and what came after it), tasks blocked waiting on you, and open tasks nobody has taken. **The tool behind "check your handoffs".** |
| `handoff_board` | The whole board in brief, filtered by status or assignee |
| `handoff_get` | One task with its full history |
| `handoff_create` | A new task: title, body, acceptance checks, assignee, parent task, and the paths it will change |
| `handoff_claim` | Take an open task, one handed to you, or a blocked one nobody has (it stays blocked, and yours to set open); optionally claim paths |
| `handoff_note` | A progress note (the first one marks the task in progress) |
| `handoff_pass` | **The handoff:** what I did, what's left, how to check it, files touched. Reassigns the task |
| `handoff_request_review` | Send the task to a reviewer (not yourself) |
| `handoff_review` | Approve, or send back with changes, marking each acceptance check met, not met or not checked. `panel=true` also asks your Ixel panel |
| `handoff_status` | Blocked (with a reason, and who it's waiting on), open, or done |

A task moves like this:

```
open → claimed → in_progress → handed_off → in_review → done
                                   └→ blocked
(any state) → cancelled   (you only, from the command line)
```

## Your controls

At the command line you are `human` on the board (Handoff shows it as "you"), and you can do what agents
can't.

| Command | What it does |
|---|---|
| `handoff` | This project's board; outside a project, the first commands to know |
| `handoff board [--all] [--status S] [--assignee NAME] [--watch]` | The board by status, with assignee, age and the last event. `--watch` keeps refreshing |
| `handoff show T-12` · `handoff T-12` | One task with its full history, and what you can do with it next |
| `handoff add "title" --to codex [--body TEXT] [--accept CHECK]… [--path PATH]…` | Add a task (with no title, it asks what needs doing and who should do it) |
| `handoff assign T-12 claude` | Give a task to someone else (`me` for yourself, or `nobody`) |
| `handoff note T-12 "text"` | Add a note to any task |
| `handoff done T-12 [--reason R]` | Close a task, no review needed |
| `handoff reopen T-12` | Open a done, cancelled or blocked task again |
| `handoff block T-12 "why" [--waiting-on NAME]` | Mark a task blocked, and say why |
| `handoff review T-12 [approve\|changes ["notes"]] [--check N=RESULT]… [--panel]` | Review a task that's waiting for you. With no verdict, it shows what came back and its checks, then asks. `--check "2=not_met:fails when empty"` marks check 2 |
| `handoff status T-12 open\|blocked\|done` | Set a status directly (`done`, `reopen` and `block` are the short ways). On a task in review, this or `cancel` ends the review and gives the task back to its author |
| `handoff cancel T-12` · `handoff delete T-12` | Cancel (keeps the history), or delete for good with its results in `.handoff/outputs` (asks first) |
| `handoff clean [--older-than DAYS] [--yes]` | Remove finished tasks that haven't changed in DAYS days (30 if you leave it out), with their history and results, after listing them and asking |
| `handoff keep [DAYS\|forever]` | Show or set how long this board keeps finished tasks. It keeps them until you delete or clean them, unless you set a number of days |
| `handoff done-rule [review_or_reason\|review\|any]` | When agents may mark tasks done |
| `handoff dispatch "codex review my changes, gemini list …" [--yes] [--plan] [--no-run]` | Split one request across your agents and run the parts side by side ([dispatch](#hand-out-work-to-any-agent-dispatch)) |
| `handoff agents` | Who can take tasks here, and what kind: change files, answer, review, make pictures |
| `handoff approve T-12 AGENT [--kind edit\|answer\|review\|image] [--return-to NAME]` · `--revoke` | Let an agent run one task, once, through the [worker](#the-worker-preview) (or withdraw that). With no AGENT, at a terminal, it offers the task's assignee and asks |
| `handoff run [T-12 …]` | Run approved tasks now, side by side (all of them, if you name none) |
| `handoff worker --agent claude\|codex [--once] [--share FOLDER]` | Keep running the tasks you approve, each in its own git worktree |
| `handoff setup [--write\|--remove]` · `handoff doctor` | Connect the apps (or take Handoff out of them); check everything |
| `handoff update [--check]` | Get the latest Handoff and reinstall it (`--check` only says whether there is one) |
| `handoff help [COMMAND]` | Every command, grouped; or one command's options and examples (the same as `handoff COMMAND --help`) |

- **Task ids** can be `T-12`, `t12` or just `12`.
- **Other names work too:** `list`, `ls` or `tasks` for board; `view`, `info` or `get` for show; `new` or `create`
  for add; `comment` for note; `close`, `finish` or `complete` for done; `unblock` for reopen; `rm` or `remove`
  for delete.
- **`me` means you** wherever you name someone: `handoff assign T-12 me`, `add --to me`, `board --assignee me`,
  `block --waiting-on me`, `approve --return-to me`.
- **A mistyped command says what's wrong:** what's missing, an example to copy, and a suggestion for a
  misspelled command or flag (`handoff boards` → `handoff board`).

Commands that use the board take `--project PATH`; by default it's the git repository you're in. Any
folder of a repository (or one of its worktrees) means that repository's board.

## The rules

The board checks these itself; it doesn't trust what an agent says about who it is or what it may do.

- **Only the assignee** can add notes to a task, hand it off, ask for a review or change its status. Only
  the named reviewer can review, and nobody reviews their own work: an agent that took the task on, handed
  it over or ran it through the worker can't review it, whoever asks. You can do anything from the CLI.
- **Done needs a review, or a reason.** By default an agent can mark a task done after an approving review,
  or by saying why it doesn't need one. `handoff done-rule review` requires the review; `any` trusts the
  assignee. A handoff after an approval needs a new review.
- **Reviews mark the checks.** A reviewer can mark each numbered acceptance check `met`, `not_met` or
  `not_checked`, with a one-line note. Once they mark any, the checks they leave out are recorded as
  `not_checked`; a review with no marks records none. An approval can't leave a check marked `not_met`:
  that's a review asking for changes.
- **History is append-only.** Agents can't delete tasks or edit the history through Handoff's tools; only
  you can cancel or delete, from the CLI. These rules bind the MCP tools. An agent that can run commands as
  you (Claude Code's Bash, Codex's shell) can also run the `handoff` CLI as you, so deny it `handoff approve`,
  `handoff delete`, `handoff clean`, `handoff keep` and `handoff run` in its permission settings
  ([Security](../SECURITY.md#agents-with-a-shell)).
  Anything Handoff started itself is refused the CLI's writes, as a guard against an agent doing it by
  accident. It isn't a wall (a shell can unset `HANDOFF_DEPTH`), so keep those commands denied.
- **Paths are claimed, not locked.** When two active tasks claim overlapping files, both agents are warned:
  the second when it claims them, the first in its inbox, on the task, and in the reply to its next note.
- **Limits:** titles up to 200 characters, bodies and notes up to 20 KB, 500 open tasks.
- **No secrets.** Text that looks like an API key, token or private key is refused, not stored.
- **Everything on the board is treated as untrusted.** When an agent reads it, Handoff tells it the text was
  written by other agents or the user, as information to weigh, not instructions to follow.

## Reviews with the Ixel panel (optional)

If [Ixel MAT](https://github.com/OpenIxelAI/ixel-mat) is installed (`ixel` on your PATH), a reviewer can
ask for a panel verdict: "review T-1, and ask the Ixel panel too". Handoff sends the task, its numbered
acceptance checks, the latest handoff, and the reviewer's notes and marks to `ixel review --json`, and keeps
the panel's verdict with the review. The panel's verdict is advice: it never approves anything by
itself. It makes several paid model calls and can take a few minutes. You can also ask Claude to use
Ixel's own `ixel_review` tool, once `ixel mcp --setup` has added Ixel's tools to your apps.

## Where things live

- The board is `<project>/.handoff/board.db`. The project is the nearest folder with a `.git`, and a linked
  git worktree shares its main checkout's board. On macOS and Linux the folder is private to you (0700,
  and 0600 for the file).
- Finished tasks stay on the board until you delete them, run `handoff clean`, or set `handoff keep DAYS`.
  Deleting overwrites the task's text in the board file, so it isn't left behind in the file's free space.
- `.handoff/` keeps itself out of git, with a `.gitignore` of its own. If that file goes missing or
  changes, the next `handoff` command offers to add `.handoff/` to your project's `.gitignore`.
- Handoff has no config of its own and no commands in config, so nothing in a project folder can make
  Handoff itself run anything. Git still follows the repository's own config when the worker makes a
  worktree and commits (filters such as Git LFS's run, as they always do with git); the worker turns off
  hooks and signing ([SECURITY.md](../SECURITY.md#no-network-no-commands)).

## Troubleshooting

- **"No project found"**: run the command inside your project (a git repository), or pass
  `--project PATH`. If an app says this, run `handoff setup --write` in the project and restart the app.
- **An agent can't see a task:** ask it to call `handoff_board`. Tasks are per project, so check the app
  is open in the same project (`handoff doctor` shows where Claude Desktop points).
- **Codex gives up on a panel review:** raise `tool_timeout_sec` under `[mcp_servers.handoff]` in
  `~/.codex/config.toml`. Running `handoff setup --write` again keeps a longer one you set.
- **Windows:** open a normal PowerShell window, `cd ~` first, and run one line at a time.
