# Handoff

**Let your AI agents work as a team on the same project.**

Say "Codex, take the tests; Claude, do the API" in either app. The work, notes and reviews pass between them
through a shared task board, so you stop being the copy-paste courier. Or hand out a whole list at once, to any
model you use:

```bash
handoff dispatch "codex review my changes, grok make pictures of my app, claude finish the next step"
```

Handoff works in Claude Code, Codex, Claude Desktop, Gemini CLI, OpenCode and Grok Build. The board is a file
in your project, and Handoff never listens on the network.

**[ixelai.com/handoff](https://ixelai.com/handoff/)** · [Docs](https://ixelai.com/docs/handoff/) ·
[Privacy](https://ixelai.com/docs/privacy/)

## Install

Windows, in a normal PowerShell window (not "Run as administrator"):

```powershell
irm https://ixelai.com/handoff/install.ps1 | iex
```

macOS or Linux:

```bash
curl -fsSL https://ixelai.com/handoff/install.sh | sh
```

You need Python 3.10 or newer and Git. To get [Ixel MAT](https://ixelai.com/ixel-mat/) and the Ixel app too,
[install all of Ixel](https://ixelai.com/docs/install/#pick) instead. To update or uninstall, see the
[install guide](https://ixelai.com/docs/install/).

Then open a new terminal, go to your project's folder, connect your apps and restart them:

```bash
handoff setup --write
```

## Use it

- In any connected app, ask the agent to hand work over. In the other app, say **"check your handoffs"**.
- `handoff board` shows who's doing what. `handoff 3` shows task T-3, and `handoff done 3` closes it.
- `handoff dispatch "..."` splits one request across your agents and runs the parts side by side.
- `handoff approve 3 opencode` (or `claude`, `codex`, `ollama`, `kimi`…) lets one agent work on T-3 in its own
  branch. Every coding agent and local model you have can take tasks: `handoff agents` lists them.

Every command, tool and rule is in the [guide](docs/guide.md).

## Models, chains and the worker

Once you approve a task, the [worker](docs/guide.md#the-worker-preview) runs the agent on it in its own git
worktree and branch. It never pushes. You choose which models do the work:

```bash
handoff approve T-7 claude --model claude-haiku-5-5
handoff approve T-7 claude --chain claude-opus-5-5,claude-sonnet-5-5,ollama:qwen3:4b-instruct
```

- **A model, or a chain.** A chain lists models biggest first. The first plans up to 6 steps and the smaller
  ones do them, each step committed on its own on the task's branch. A step can go to another agent as
  `agent:model`, such as `ollama:qwen3:4b-instruct`. Work comes back up the chain: the biggest step model checks
  the smaller models' work and fixes it, and anything too big goes up to the planner.
  [Details](docs/guide.md#pick-the-model-or-a-chain-of-models).
- **Effort per model,** in each agent's own words: `claude-opus-5-5:high` in a chain, or `--effort` (claude takes
  low to max, codex minimal to ultra).
- **What it used.** A run reports the tokens each model used and an estimated cost at API prices.
  `handoff show T-7` and the handback show it. A local model reports its tokens at no cost.
- **Merge.** `handoff merge T-7` merges a finished task's branch into the branch you have out, as one merge
  commit. Nothing is pushed. The task is marked done and its worktree removed. It refuses, changing nothing,
  when the merge could touch files git doesn't track or the board.
- **Usage limits.** When a model hits its usage limit, the next best model takes over, and the handback says so:
  "claude-opus-5-5 hit its usage limit, so claude-sonnet-5-5 took over." Approve shows you who would take over
  first. `--no-fallback` turns it off. [Details](docs/guide.md#when-a-model-hits-its-usage-limit).
- **Rankings.** `handoff rankings` shows which models are best at what, in tiers. Top plans and reviews
  (claude-fable-5-1, claude-opus-5-5, gpt-6-astra). Middle builds and checks (claude-sonnet-5-5, gpt-6-sol).
  Small takes short, exact directions (claude-haiku-5-5, qwen3:4b-instruct).
  [Recommended setups](docs/guide.md#recommended-setups) has the rest.
- **Other agents.** OpenCode, Ollama, LM Studio, Hermes, Kimi, Cline and OpenClaw can take tasks too. Hermes,
  Kimi, Cline and OpenClaw run only once you enable them.
  [What each can reach and cost](docs/guide.md#every-other-agent-and-model-you-have).
- **The Board in the Ixel app.** The Board is also in the Ixel app (`ixel app`, installed with
  [all of Ixel](https://ixelai.com/docs/install/#pick)); `ixel gui` from [Ixel MAT](https://ixelai.com/ixel-mat/)
  shows the same pages in a browser. There you can approve runs with a model picker, see what each run used and
  cost, and merge with the Merge button.

**On macOS and Windows the worker is edit-only for now.** Claude Code, OpenCode and local models change files
but run no commands, tests or builds, and Codex needs the sandbox. The sandbox that runs your tests and builds
needs Linux with bubblewrap.

## Privacy

Handoff has no account, no telemetry and no network code of its own. The apps you connect read the board, and
an agent run sends its task to that agent's company. Some companies train on what you send:
[which ones, and how to keep your work out](https://ixelai.com/docs/privacy/#training).

## More

- [docs/guide.md](docs/guide.md): connecting each app, the plugin, dispatch, the worker with its models and
  chains, merging, every command and rule.
- [CONTRIBUTING.md](CONTRIBUTING.md): run it from source and run the tests.
- [SECURITY.md](SECURITY.md): what Handoff protects. Report a security problem privately to **openixel.ai@proton.me**.

## License

[MIT](LICENSE)
