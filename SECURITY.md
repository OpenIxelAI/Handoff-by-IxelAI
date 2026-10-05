# Security

Handoff lets AI agents in different apps pass work to each other through a task board on your computer.
Every agent reads what the others wrote, so the board is a channel from one agent to another. This page
covers what Handoff protects, from whom, and how each protection is checked.

## Reporting a problem

Please report vulnerabilities privately, not in a public issue: by email to openixel.ai@proton.me, or
through GitHub's **Report a vulnerability** button (Security tab) on this repository once it's public.

## What Handoff protects

| Asset | Where it lives |
|---|---|
| Each agent's judgment | What an agent reads from the board must not take it over |
| The board and its history | `<project>/.handoff/board.db` (SQLite), and what runs leave in `.handoff/outputs` |
| Your secrets | Nowhere on the board: text that looks like a key is refused |
| Your files and your machine | Handoff edits no project files itself. It runs an agent only for a task you approve: an edit is confined to a worktree (and a sandbox, where one can run) |

## Who we assume might be hostile

1. **Anything an agent writes**, and anything that reaches an agent: a task body can carry text from a web
   page, an issue or a diff, written to hijack the next agent ("ignore your instructions and…").
2. **An agent acting outside its role**: taking over another agent's task, approving its own work, or
   rewriting history.
3. **The project folder**, for example a cloned repository that ships its own `.handoff` folder.
4. **Other programs on your network.**

We don't try to protect against someone who already controls your user account. An agent that can run
commands as you is close to that: see [Agents with a shell](#agents-with-a-shell).

## Protections

### No network, no commands

- `handoff mcp` talks only over stdio to the app that started it. Handoff never opens a listening socket
  and makes no network connections itself. The programs it starts for you (below) do: the agents and
  Ixel MAT reach their models, and `handoff update` reaches GitHub.
- Handoff stores and serves text. It edits no project files and calls no model itself. The programs it
  starts, each by its full path (below), are:
  - an agent, or Ixel MAT, for a task you approved: the worker, `handoff run` and `handoff dispatch`,
    which you start yourself;
  - `ixel review`, when you or a reviewer asks for the Ixel panel;
  - `claude --help`, `codex exec --help` and a bubblewrap test run, to see which agents can run here
    (`handoff doctor`, `handoff agents`, `handoff approve`, a dispatch plan, and before an edit run);
  - `ixel ask --list` and `ixel image --list`, to see which models Ixel MAT has (no model is asked);
  - `claude mcp add` and `claude mcp remove`, for `handoff setup --write` and `--remove`;
  - git and bash (PowerShell on Windows), for `handoff update`; and git, for the worker's worktrees, and
    to see which worker branches are left after `handoff delete`, `handoff clean` or a delete in the Ixel
    window (`git for-each-ref`, which only reads).
- The files it writes are the `.handoff` folder, and your `.gitignore` if you say yes. In your user
  folder: the key that seals approvals and the list of approvals already used (`approval.key` and
  `approvals-used`, below). With `handoff setup --write` (or `--remove`): each app's MCP config, and
  Claude Code's `settings.json` for the session hook, keeping the old one as `settings.json.handoff-bak`.
- Handoff has no config of its own, so nothing in a project folder can make Handoff itself run anything.
  Its one hook is the Claude Code session hook (below), which only reads. Git is different: when the
  worker makes a worktree and commits, git still follows the repository's own config, as it always does.
  The worker turns off what would run a program (hooks, the filesystem monitor, signing and external diff
  programs), but leaves filters alone, since Git LFS needs them: a repository you don't trust can still
  name a filter program in its config, which runs when the worktree is checked out or its files are
  staged.
- `handoff api` is how the Ixel window shows a board and acts on it for you. It reads one JSON request on
  stdin and writes one JSON reply, its command line is always just `api`, and it acts as you with the same
  rules as the CLI: the board still refuses what it refuses, and an edit run is still claude or codex only.
  Reading never makes a board. Approval seals never leave it. Its one way to start anything is `run.start`,
  which starts `handoff run` for a task you already approved, as you would at a prompt.
- `handoff update`, which only you run, pulls the checkout Handoff was installed from with git and runs its
  installer. It starts git and bash from PATH, and Windows PowerShell from where Windows keeps it, by their
  full paths: never from the folder you run it in, which Windows would otherwise look in first.
- Every other program Handoff starts (claude, codex, ixel, git, bubblewrap) is found the same way, on PATH
  only, so a `claude.cmd` or `git.exe` planted in a cloned project is never what runs.
  `tests/test_code_hygiene.py` fails if `shutil.which`, which looks in the current folder on Windows, comes
  back.
- The worker's git runs with hooks pointed at an empty folder it makes outside the project, so a repository
  can't ship hooks that run when a worktree is made or committed to.
- On Windows the apps start Handoff through the install's own python.exe with `-I`, so nothing is imported
  from the project folder they start it in.

### Everything on the board is untrusted

- Every response that quotes board text opens with a frame telling the agent it was written by other
  agents or the user, to evaluate and not to obey. Each quote is fenced with a random marker made for that
  response (`<HANDOFF-3f9a1c0e2b7d body>` … `</HANDOFF-3f9a1c0e2b7d>`). The quoted text can't guess it,
  and any copy of it in the text is removed.
- Control characters, terminal escape sequences and bidi overrides are stripped when text is stored, and
  again when it's shown to an agent or printed in your terminal. The CLI never reads board text as Rich
  markup. Characters you can't see but a model reads (Unicode tags, zero-width characters, variation
  selectors) are kept but shown, as `[U+200B]`, so you and the agents see the same text. Two are left as
  they are where writing needs them: one style selector after a visible character (⚠️), and a zero-width
  joiner or non-joiner between two non-ASCII characters (👩‍💻, Persian, Indic scripts).
- A board Handoff didn't write (a repository can ship a crafted `.handoff/board.db`) is checked field by
  field as it's read. Names must be valid names or they show as `unknown`, text is sanitized again, and
  data of the wrong shape is ignored. A task it can't read is reported as damaged, not shown.
- The sanitizer, the secret check and the path matcher scan agent text in linear time: character
  tables, `str.find` loops and a bounded glob matcher, with no backtracking regular expressions. (Ixel
  once saw 41-second stalls from one.) Dispatch also uses a few simple regular expressions, on your
  request (up to 4,000 characters) and a task's title (up to 200), to split it and find the files and
  number of pictures it names; a test times the kind guesser on hostile input.
- Sizes are capped: titles at 200 characters, bodies and notes at 20 KB, 500 open tasks.

### Server-side rules

- Identity is the `--as` name in each app's MCP config. `human` is reserved for your own CLI, and
  `handoff mcp --as human` is refused.
- The board enforces who may do what, and every change is one SQLite transaction that checks the task's
  current state. Only the assignee can note, hand off, ask for a review or change status. Only the named
  reviewer can review, and nobody can review their own work (an agent that claimed the task, handed it over
  or ran it through the worker can't be its reviewer, whoever asks; one that only reviewed it can). A review that marks an acceptance check not met
  can't be an approval. By default `done` needs an approving review or a stated reason. Only you can
  reassign, cancel, delete, clean or reopen.
- Every change is recorded in `events`. SQLite triggers refuse edits to the history, and refuse deletes
  while the task exists. Agents have no delete tool at all.

### Agents with a shell

The rules above bind the MCP tools, which is how an app's agent works on the board. An agent that can also
run commands as you (Claude Code's Bash tool, Codex's shell) can run the `handoff` CLI, and at the CLI it
is you: it could approve a task, run it, or delete one. Handoff can't tell it apart from you at a terminal.

- Anything Handoff started (`HANDOFF_DEPTH` is set) is refused the CLI's writes (add, assign, note, status,
  review, approve, delete, clean, keep and the like, plus `setup` when it would change an app's config or
  hook, and `update` other than `--check`), `handoff api`'s writes, and `handoff run`, `dispatch` and
  `worker`. It can still look. This is a guard against an agent doing it by accident, not a wall: a shell
  can unset the variable (`env -u HANDOFF_DEPTH`, or `HANDOFF_DEPTH=0`), and Handoff can't stop that.
- So for every agent with a shell, deny those commands in its app's permission settings too. In Claude
  Code, add `Bash(handoff approve:*)`, `Bash(handoff delete:*)`, `Bash(handoff clean:*)`,
  `Bash(handoff keep:*)` and `Bash(handoff run:*)` to `permissions.deny` in `~/.claude/settings.json`.
  Where an app can't deny single commands, have it ask you before it runs any.

### No secrets on the board

Text that looks like a common API key or token is refused with a clear error, and nothing is written.
That covers keys that start `sk-` (OpenAI, Anthropic), `sk_live_` and `rk_live_` (Stripe), `xai-`, `gsk_`
(Groq), `hf_` (Hugging Face) and `npm_`; GitHub and GitLab tokens; Slack tokens and webhook URLs; Google
API keys and OAuth client secrets; AWS access keys, and an AWS secret key next to one of its usual names
(`aws_secret_access_key`, or `"SecretAccessKey"` as the AWS CLI prints it); JSON Web Tokens; and private
key blocks (PEM and PuTTY). The error never repeats the secret. Other secrets, such
as passwords, aren't detected, so never write them on the board.

### The board file

- On macOS and Linux, `.handoff` is created with mode 0700 and `board.db` with 0600. Handoff tightens
  them again when it opens a board it owns, and `handoff doctor` reports loose ones.
- `.handoff` contains a `.gitignore` that ignores everything in it, so the board is never committed by
  accident.
- A `.handoff` that is a symbolic link is refused, so a cloned repository can't point the board somewhere
  else.

### Deleting, and how long finished tasks stay

- **Finished tasks stay until you remove them.** That's the default. `handoff delete T-N` removes one task.
  `handoff clean` removes every finished task (done or cancelled) whose last change was more than 30 days
  ago (`--older-than DAYS` for another age), after listing them and asking. `handoff keep DAYS` has the
  board do that by itself: each time Handoff opens it for writing (any `handoff` command, a request from
  the Ixel window, an app's agent first using the board), finished tasks older than DAYS are removed.
  `handoff keep forever` turns that off, and `handoff doctor` shows the setting. A task goes only with all
  of its subtasks, so one with a subtask that's still open, or newer, waits for it.
- **Changing the setting never removes anything by the old one.** `handoff keep` on its own only shows it.
  `handoff keep DAYS` lists the finished tasks the new setting removes now and asks first (`--yes` to
  skip the question), and `handoff keep forever` removes nothing. When a command at the terminal opens the
  board and the setting removes tasks, it says which.
- **Removing a task takes everything the board has of it**: its title, text and checks, every note,
  handoff and review, its history, its path claims, and its folder in `.handoff/outputs` (answers, reviews
  and pictures from runs through Ixel). Only you can remove tasks: at the command line, or one task at a
  time in the Ixel window. Agents have no tool for it.
- **Deleted text is overwritten, not just marked free.** Every connection Handoff writes with turns on
  SQLite's `secure_delete`, so a deleted row is overwritten with zeros. After a delete or a clean, Handoff
  copies `board.db-wal` (SQLite's log, which keeps earlier copies of changed pages) into the board and
  empties it, and rebuilds `board.db` from what's still on it (`VACUUM`). What SQLite sets aside while it
  works stays in memory (`temp_store`): the rows a delete takes out, which it keeps in case it has to undo
  the delete, and VACUUM's copy of the board. So none of it is written to a temporary file in the
  system's temp folder. The tests check, byte by byte, that a deleted task's text is in neither file.
- **A busy board is finished later.** If another program is reading the board at that moment, the task is
  still deleted, but the old copies can stay in `board.db` and `board.db-wal` until the next time Handoff
  opens the board for writing, which finishes the job. A board from an older Handoff, whose deletes left
  text behind, is rebuilt the same way once, the first time this version opens it for writing (or the
  next time, if it's busy then).
- **Results an older Handoff left:** before this version, deleting a task (at the command line or in the
  Ixel window) left its folder in `.handoff/outputs`. A run that finishes after its task was deleted can
  leave one too. `handoff clean` lists every folder there whose task is no longer on the board and
  removes them with the rest, after asking. `handoff delete` says when there are any. Nothing removes
  them without asking.
- **What stays:** an edit run's worktree (`.handoff/worktrees/T-N`) and branch (`handoff/T-N`) hold the
  agent's work, so Handoff leaves them, and their commit messages carry the task's title. `handoff delete`
  and `handoff clean` say which are left, with the git commands that remove them, and so does Handoff's
  reply to a delete from the Ixel window.
- **What Handoff can't reach:** copies outside its files. That's backups (Time Machine, File History), a
  synced folder holding the project, the history of the app whose agent read the task, and the disk
  itself, which can keep old blocks until it reuses them (SSDs and copy-on-write file systems such as
  APFS in particular). Files in `.handoff/outputs` are deleted the ordinary way, not overwritten.

### Connecting apps

- `handoff setup --write` changes only Handoff's own entry. For Codex, only `command`, `args` and
  `tool_timeout_sec` under `[mcp_servers.handoff]` change (a longer timeout you set is kept), and Grok
  Build's `~/.grok/config.toml` is edited the same way. For Gemini CLI and OpenCode, only the `handoff`
  entry in their JSON settings changes, keeping anything else you set in it (except a `url` that would point the app somewhere else); a file
  with comments or trailing commas is never rewritten. The new file is parsed and compared with the original before it's saved, and it's written atomically with the file's permissions kept. A config it
  can't edit safely (invalid, or written in a form it doesn't recognize) is left alone, and you get the
  lines to add by hand.
- Claude Code is configured through its own `claude mcp add` command, with fixed arguments.
- The session hook `handoff setup --write` adds to Claude Code's `settings.json` runs `handoff hook
  session-start --as claude` (this install's absolute path) when a session starts. It only reads the board,
  never makes one, and tells the session task refs and counts only: no title, note or other text anyone
  wrote, so a task (or a board in a cloned repository) can't put instructions into a session. It says nothing
  inside an agent Handoff started, and it exits 0 quietly whatever goes wrong. Only Handoff's own entry
  under `hooks` → `SessionStart` changes; everything else is compared before and after, the old file is kept
  as `settings.json.handoff-bak`, and `--remove-hook` takes it out. On Windows the path goes unquoted only
  when Git Bash and PowerShell both read it as one word; otherwise the hook names the program and its
  arguments separately, so no shell reads them at all.
- The plugin (`plugins/handoff`) starts the same server with the same fixed arguments (`handoff mcp --as
  claude` or `--as codex`), one manifest per app, so neither app can join the board under the other's
  name. It adds a skill (instructions only, no code) and nothing else: no hooks, no commands. It gives no
  way to approve the worker, which stays at your terminal.

### The optional Ixel review

- It runs only when you or a reviewer asks for it, and only if `ixel` is on your PATH.
- The command line is fixed (`ixel review --json -`). The review text goes on stdin, so nothing an agent
  wrote can become a flag.
- The panel's answer is other models' words: it's sanitized, clipped, checked for secrets and stored as
  untrusted text like everything else.
- **Loop guard:** everything Handoff starts gets `HANDOFF_DEPTH` one higher. A Handoff running with it set,
  or inside an Ixel panel (`IXEL_PANEL_DEPTH`), won't start a panel.

### Dispatch: answers, reviews and pictures through Ixel

`handoff dispatch "codex review my changes, gemini make me a list…"` splits a request into tasks, one per
agent. Edits go to the worker, with all of its protections. Answers, reviews and pictures go to Ixel MAT
(`ixel ask`, `ixel image`), which holds your API keys; the model gets text and attachments, and no file,
shell or network access of its own.

- **Nothing runs until you say yes.** The split is done by fixed rules, not a model, and you see every part,
  who gets it, what kind of run it is and which files it reads before anything is added.
- **Each approval is for one kind of run.** The seal (see the worker's approvals) covers the kind too, so
  an approval to answer can't be edited into an approval to change files. Only claude and codex can be
  approved to edit.
- **The command line is fixed** apart from names Handoff checked: the agent's board name, a provider, a
  branch made only of safe characters, and files named in the task that exist inside the project, with
  safe characters and no `..`. The task's text goes on stdin. Ixel starts in the project folder.
- **What a review reads** is decided by Handoff: the files the task's title names inside the project
  (checked like any file a run reads), else your uncommitted changes, else the branch since it left
  main, else your last commit. Ixel refuses to send code that looks like it holds a key.
- **A picture based on your files** has them read first by a chat model, which writes the description.
  The plan names that model, and the run asks Ixel MAT for that one.
- **Results stay next to the board.** The full answer goes to `.handoff/outputs/T-N/answer.md`, pictures to
  `.handoff/outputs/T-N/`. A result that names pictures anywhere else is refused. The board keeps the answer
  sanitized, clipped and checked for secrets, like everything else.
- **Loop guard:** neither command runs inside something Handoff started (`HANDOFF_DEPTH`). Ctrl+C stops
  every run it started, and each one records that it was stopped.

## How it's checked

| Test | What it proves |
|---|---|
| `tests/test_board.py` | Every state transition, legal and illegal; authorization; limits; secrets refused and never written; history can't be edited; file permissions; symlinked folder refused; a deleted, cleaned or expired task's text is in neither `board.db` nor `board.db-wal`, byte for byte, with or without SQLite overwriting by default, on a busy board once it's free, and on a board an older Handoff left; a delete writes no temporary file (on Linux, where the test can see one); its results go too, never through a link, and results whose task is gone are found |
| `tests/test_board_concurrency.py` | Two processes racing on one board: every task claimed exactly once, no note or task lost |
| `tests/test_sanitize.py`, `tests/test_globs.py` | Control codes and bidi stripped; secret detection; linear time on hostile input |
| `tests/test_crafted_board.py` | A board crafted to inject text through names, event kinds, paths, review marks and malformed data reaches neither an agent nor the terminal unfiltered |
| `tests/test_mcp_server.py` | The frame and fence, including a quote that tries to close the fence and inject instructions; identity rules through the real tools; two real stdio servers sharing one board |
| `tests/test_plugin.py` | The plugin's manifests start the server as the right agent, with no shared MCP config; the real Claude Code and Codex install it from a local marketplace (this checkout, not GitHub) and create a task through it, on the board of the project they were opened in |
| `tests/test_setup.py` | Config merges keep everything else, are idempotent, and refuse what they can't edit safely |
| `tests/test_hook.py` | The session hook: refs and counts only, never a title or note; silent with no board, nothing waiting, inside an agent Handoff started, or a broken board; `settings.json` keeps everything else, gets the hook once, and gets it back out; a Windows path that needs quotes skips the shell; the real Claude Code runs it, in both forms, and the model hears the refs but no task text |
| `tests/test_api.py` | The window's JSON door: reading never makes a board; the board's rules still hold (an edit run is claude or codex only, secrets refused); approval seals never leave it; bad requests get an error, never a crash; stdout is one ASCII line whatever the code page; text comes back exactly as written; a delete takes the task's results too, and says what's left |
| `tests/test_ixel.py` | Agent text reaches Ixel only on stdin; the loop guard; hostile panel output is sanitized and secrets withheld |
| `tests/test_worker_claude_live.py` | **Claude's attack test**, with the real Claude Code CLI inside real bubblewrap (below) |
| `tests/test_worker_codex_live.py` | **Codex's attack test**, with the real Codex CLI inside real bubblewrap (below) |
| `tests/test_worker_live.py` | **The attack test for Claude's edit-only mode**, where the sandbox can't run (below) |
| `tests/test_sandbox.py` | The sandbox's command line (what's hidden, what's shown, in what order); which folders can be shared and what's hidden inside them; Claude's sandbox settings; each agent's private login and when a refreshed one is copied back; which environment variables count as credentials |
| `tests/test_worker.py` | Approvals (the person only; one run; lapse when the task moves; sealed to this computer and board, so a cloned or edited board carries none); the worktree, branch and commit; a rewritten `.git` file and the repository's own hooks can't make the worker's git run anything; links out of the worktree are refused; the agent's environment and command line; deleting or cleaning a task leaves its worktree and branch, and says so |
| `tests/test_dispatch.py` | How a request splits and which kind of run each part gets; nothing is added before a yes; the runs really happen at once; the kind is sealed, so a run can't be turned into an edit; only claude and codex edit; the files a run reads stay inside the project; pictures saved anywhere else are refused; one failing part blocks only its own task; the loop guard |
| `tests/test_update.py` | Update never overwrites local changes, and runs the installer again until the latest is installed; git, bash and PowerShell never come from the folder it's run in |
| `tests/test_code_hygiene.py` | Every text file is opened with an explicit encoding; the installers stay ASCII for Windows PowerShell 5.1; no program is started by a bare name, and taskkill comes from System32 |
| `tests/test_cli.py`, `tests/test_cli_commands.py` | Every command and its messages: what's refused and what to type instead; nothing an agent wrote is read as markup; an agent Handoff started can't act as you; `clean` and `keep` list what goes and ask first, and changing `keep` never removes anything by the old setting |
| `tests/test_project.py` | Which folder is the project, for a subfolder, a linked worktree, a submodule or an empty `.git`; paths too long for the system |
| `tests/test_docs.py` | The guide names every tool, command, status and done rule, and this page names every test file |

## The worker

`handoff worker` runs an agent that edits files and runs commands, with nobody watching.
Before each agent shipped it got a security review, and an attack test that runs the real CLI.

### What we measured

We pointed each CLI at a fake model API that answers with attacks, from inside a git worktree:

| | Reads `~/.ssh` | Writes outside the worktree | `git push` | Network | Can run tests |
|---|---|---|---|---|---|
| `codex exec --sandbox workspace-write` | yes | yes (all of `/tmp` is writable) | yes | blocked | yes |
| `claude -p` with Bash allowed | yes | yes | yes | yes | yes |
| `claude -p --restricted`, file tools only | blocked | blocked | blocked | blocked | no |
| `codex exec` (workspace-write) inside Handoff's bubblewrap sandbox | blocked | blocked | blocked | blocked | yes |
| `claude -p --restricted` with Bash and its own sandbox, inside Handoff's bubblewrap sandbox | blocked | blocked | blocked | blocked | yes |

No CLI's own sandbox, left to its defaults, confines what commands can *read*. So both agents run inside
an OS sandbox that Handoff controls (bubblewrap, on Linux; macOS is next), with their own sandbox inside
it. Where Handoff's sandbox can't run, Claude runs edit-only, with no way to run anything, and Codex
doesn't run. Findings from the reviews:

- **Commands can read every process's environment.** Codex passes `OPENAI_API_KEY` to the commands it
  runs, and inside the sandbox `/proc/*/environ` shows every process's variables. So a sandboxed agent
  gets no environment variable whose name says it holds a credential; an API key it needs goes into its
  private login file instead.
- **Codex's home holds more than its login.** `~/.codex` also has the transcripts of your other sessions
  and your config (which can hold API keys and start programs). So each run gets a private home with a
  copy of `auth.json` only, which Codex's commands can't read. The same goes for Claude's `~/.claude`.
- **Claude's sandbox passes `ANTHROPIC_API_KEY` to its commands too.** So Claude gets its key from a file
  in its private home, which it reads itself (the `apiKeyHelper` setting) and its commands can't.
- **Claude's sandbox can't start inside Handoff's as it is.** Handoff's sandbox protects parts of its
  `/proc`, and the kernel then refuses the fresh `/proc` Claude's sandbox mounts for its commands. So
  Claude's sandbox runs in its nested mode (`enableWeakerNestedSandbox`), where commands see the `/proc` of
  Handoff's sandbox. That only holds the agent's own processes, and the attack test tries to read their
  environment, open files, memory and root folder (which leads to the private home): every read is
  refused. The Codex attack test now tries the root-folder route too, and it's refused there as well.
- **Claude Code's own checks on commands aren't a boundary.** In headless mode it refuses some commands
  it can't be sure about (one using `$VAR`, one reading `/proc/*/environ`) but runs the same thing
  written differently. So the worker allows every command (`--allowedTools Bash`) and relies on the
  sandbox, which every command runs in; the attack test writes its probes the way an attacker would.
- **Inside Handoff's sandbox, Claude's commands have no network at all.** Its sandbox sends their traffic
  through its own proxy, which doesn't connect from inside Handoff's sandbox; and the settings allow no
  host anyway (`allowedDomains: []`, `strictAllowlist`).

### Protections

- **Your approval, per task, per run.** Only you can approve (`handoff approve T-7 claude`), from
  the CLI. An approval covers one run, and only while the task is as you approved it: a handoff, review,
  reassignment or status change first makes it lapse. The agent gets the task and its history as they were
  at approval; notes added later aren't sent. Two workers can't start the same approval. `handoff approve`
  prints the task's text and history, which other agents may have written, and asks before approving.
  What it seals is what it showed you: if an agent adds a note or changes the task while you read it, the
  approval is refused and you're shown the task again. (`handoff api` offers the same check to the Ixel
  window: its `task` reply has a `content` hash, and an approve that passes it back as `shown` is refused
  if the task changed.) When you don't name the agent, it offers the task's assignee only at a terminal,
  where that question is asked; with `--yes` or without a terminal, you have to name it.
- **Approvals made here, by you.** An approval is a row in the board, and the board lives in the project,
  so a cloned repo could ship one (with its `.gitignore` taken out). Each approval therefore carries an
  HMAC-SHA256 seal over the board's own path, the task, the worker, the kind of run, who gets it back, the
  time, a random nonce, and a hash of what the agent will be given (the task's title, body and checks, and
  its history up to the approval). It's made with a 32-byte key in your user folder
  (`handoff/approvals.py`; created 0600 on the first approval). The worker runs only approvals whose seal
  checks out, so one from another computer, or copied to another board or task, is ignored and listed when
  the worker starts, and a task whose text or history was changed in the board file after you approved it
  doesn't run. When a run starts, its approval's nonce goes on a list next to the key (`approvals-used`),
  so an approval runs once, even if a copy of it is written back into the board. The key doesn't stop a
  program that already runs as you and can read it; such a program could run anything anyway.
- **Its own worktree and branch.** `.handoff/worktrees/T-7` on `handoff/T-7`, made from your current
  commit. The worker commits the result there and never pushes, fetches or touches your checkout.
- **Handoff's sandbox.** The worker runs the agent inside bubblewrap. It sees the system (`/usr`, `/etc`,
  `/opt`) read-only, its own install read-only, the worktree, and a private home; your home folder, other
  homes, `/root`, `/tmp`, `/run`, `/var`, `/mnt`, `/media` and the rest of the project are empty or absent.
- **Claude: commands, in two sandboxes.** Claude Code runs as `claude -p --restricted --tools
  Read,Edit,Write,Glob,Grep,Bash --allowedTools Bash --permission-mode acceptEdits --strict-mcp-config`,
  with settings that turn its own sandbox on for every command (`failIfUnavailable`, no
  `allowUnsandboxedCommands`), allow no network, hide its private home from commands, and turn hooks off.
  `--restricted` ignores your Claude settings and the repository's (which could allow anything, switch the
  sandbox off or run commands), keeps the file tools inside the worktree, and leaves out web and MCP
  servers. Claude's own sandbox needs `socat`.
- **Claude edit-only, where the sandbox can't run** (macOS and Windows for now, or Linux without
  bubblewrap or `socat`): the same, but with only Read, Edit, Write, Glob and Grep, so no shell at all, and
  the handback says it ran that way. `--restricted` carries most of this: without it, permissive settings
  in the repository or your home folder let the agent read `~/.ssh` (the attack test proves it).
- **Codex: commands, in two sandboxes.** Inside Handoff's sandbox, Codex's own `workspace-write` sandbox
  (as a permission profile) keeps its commands' writes in the worktree and takes their network away, and
  marks the private home unreadable to them. Codex runs with `--ignore-user-config` (none of your MCP
  servers, notify commands or profiles), `--ephemeral`, and web search, image viewing and sub-agents off.
- **The CLIs are checked.** The worker checks `claude --help` and `codex exec --help` for every flag it
  relies on and refuses to run a version without them, and refuses to run Codex where bubblewrap can't
  start. On Ubuntu 24.04 and later that means adding an AppArmor profile that lets bubblewrap make user
  namespaces ([the guide](docs/guide.md#the-worker-preview) has it), the kind Ubuntu itself ships for
  Flatpak and Chrome. Know what it gives up: Ubuntu's restriction keeps unprivileged programs away from the kernel's user-namespace code, and with
  the profile any program can reach that code by starting itself through bubblewrap. Turning the
  restriction off (`kernel.apparmor_restrict_unprivileged_userns=0`) gives up the same and a little more.
  The live-test job, which runs only when started by hand, applies the profile on a stock Ubuntu runner
  before the attack test.
- **The agent's login: a private copy** (in the sandbox; where Claude runs edit-only, it uses your normal
  login, as `claude` does when you run it). Each run gets a fresh home holding only a copy of the agent's
  login: Codex's `auth.json` (or one made from `OPENAI_API_KEY`); Claude's `.credentials.json` (or one
  made from `CLAUDE_CODE_OAUTH_TOKEN`), or a file with `ANTHROPIC_API_KEY`. If the agent refreshes its
  login during the run, the new tokens are copied back to yours only if it is still what it was before the
  run and the new file is a valid login (for Codex, for the same account; Claude's file doesn't say which
  account it's for, but only Claude itself can write it, since the private home is hidden from its
  commands). A token made from a key or `CLAUDE_CODE_OAUTH_TOKEN` is never copied back. On Linux, Claude
  Code keeps its login in that file; a refresh against the real service is the one part the attack tests
  can't exercise.
- **Folders you share, read-only.** Tools installed in your home folder aren't in the sandbox unless you
  start the worker with `--share FOLDER`, which only you can do. Handoff refuses to share your whole home
  folder, the project itself (the agent has its worktree), and folders that hold credentials (`~/.ssh`,
  `~/.aws`, `~/.config`, the agents' own homes and the like). Inside a folder you share, credential
  folders and files that often hold a token (`credentials.toml`, `.npmrc`, `*.pem` and so on) are shown
  empty, and so is what a link in it points at when either looks like one and the target is in a folder
  you share or the project. A link can't lead anywhere else of yours (the rest of your home isn't in the
  sandbox); what it leads to in the system, such as the CA bundle, stays as it is, and so does a
  certificate file your network settings name (`SSL_CERT_FILE` and the like). Everything else in a shared
  folder is readable by the agent's commands, so share tool folders, not work.
- **Nothing secret is committed.** Before committing, the worker scans every added or changed file for
  keys, tokens and private keys (the same check the board uses). If it finds one, it commits nothing and
  blocks the task, naming the file. The one exception is a JSON Web Token on its own: test fixtures often
  hold a sample one (jwt.io's, say), and they expire, so the worker commits it and its handback names the
  file for you to check before you merge. On the board, a JSON Web Token is still refused.
- **Git that can't be redirected.** The worker runs git with `--git-dir` taken from the main repository's
  own list of worktrees, never from the worktree's `.git` file, and with hooks and `core.fsmonitor` off.
  Commit messages go in on stdin.
- **No links out.** A worktree with a symbolic link (or Windows junction) pointing outside it is refused
  before the agent starts.
- **No secrets in its environment.** The agent gets only what it needs to run, sign in and reach its API
  through a proxy (`PATH`, `HOME`, locale, temp folders, proxy and certificate settings, and its own
  `ANTHROPIC_*`/`CLAUDE_CODE_*` or `OPENAI_*`/`CODEX_*` variables). Tokens for other services, such as
  `GITHUB_TOKEN`, cloud keys and other providers' API keys, stay out, and a sandboxed agent gets no
  credential variables at all (see above). Handoff has no secrets of its own.
- **Text in, text out.** The prompt goes in on stdin, framed and fenced like every MCP response. The
  command line is fixed. The agent's reply is sanitized and checked for secrets before it goes on the board.
- **Loop guard.** Everything the worker starts gets `HANDOFF_DEPTH` one higher, and a worker won't start
  with it set.
- **Timeouts** (30 minutes by default) stop the agent and what it started. In the sandbox, that's everything
  in it. Elsewhere it's the agent's process group, and on Linux also anything that left the group (with
  `setsid`, say) but still has the run's `HANDOFF_RUN` variable. A run that fails or times out
  keeps what it changed, committed on its branch, and blocks the task with the reason.

### The attack tests

`tests/test_worker_claude_live.py` runs the real worker, with the real `claude` inside real bubblewrap,
against a fake model API. The fake model runs commands that read `~/.ssh`, Claude's login, the main
checkout and the board; dump `env`; look into every other process through `/proc` (environment, open
files, memory, and the root folder that leads to the private home), written as code Claude's own command
checks can't read; write outside the worktree and into the main checkout; `git push`; reach the network,
including with a request to run outside the sandbox; and plant settings in Claude's home. The repository
and your Claude settings are hostile (hooks, MCP servers, "allow everything", the sandbox switched off).
It runs once with `ANTHROPIC_API_KEY` and once with a `claude setup-token` token, and with a shared tool
folder: the tool in it must run, and the token next to it must not reach the model. It fails if any secret
reaches the model, anything outside the worktree changes, Claude falls back to edit-only, or the one
legitimate command's output isn't committed on the task's branch. A job you can run by hand runs it
against the latest Claude Code. We checked it: leaving credential variables in the environment fails it (the API key reaches
the model), so do letting commands read the private home, switching Claude's sandbox off (the login
leaks), and allowing commands outside the sandbox (one reaches the network). Running Claude without
Handoff's sandbox fails it too, but only because the private login is then missing, so for Claude
this test doesn't separate what Handoff's sandbox stops on its own; the Codex test shows that, and it's
the same sandbox.

`tests/test_worker_live.py` runs Claude's edit-only mode, with the real `claude`, against a fake model API. The
fake model tries to run a shell command and `git push`, read `~/.ssh`, the board file and the main checkout,
search the home folder, write and edit files outside the worktree, rewrite the worktree's `.git` file and
Claude settings, and fetch a URL. The repository ships hostile Claude settings (hooks, "allow everything",
`additionalDirectories: ["/"]`), an MCP server and a hostile `CLAUDE.md`. Your Claude settings are
permissive too, and `GITHUB_TOKEN` is set. The test fails if any of it works, if any tool beyond the five
file tools is offered, or if the legitimate write doesn't land, committed, on the task's branch. The same
hand-started job installs the latest Claude Code, so running it after a release shows whether that
release weakened `--restricted`.
We checked the test itself: dropping `--restricted`, or adding Bash, makes it fail.

`tests/test_worker_codex_live.py` does the same for Codex, with the real `codex` inside real bubblewrap.
The fake model runs commands that read `~/.ssh`, Codex's login (through `$CODEX_HOME`, the real path and
other processes' `/proc/*/root`), the main checkout and the board; dump `env` and every
`/proc/*/environ`; write outside the worktree and into the main checkout; `git push`; reach the network;
and add an MCP server to Codex's config. It runs once with a signed-in login and once with only
`OPENAI_API_KEY` set, and shares a tool folder the same way. It fails if any secret reaches the
model or anything outside the worktree changes, and it checks that one legitimate command ran and its
output was committed on the task's branch. The same hand-started job runs it against the latest Codex. We checked this test
too: running Codex without bubblewrap fails it (`~/.ssh` reaches the model), so does leaving credential
variables in the environment (the API key does), and so does letting commands read the private home (the
login does).
