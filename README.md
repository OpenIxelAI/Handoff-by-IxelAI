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

Every command, tool and rule is in the [guide](docs/guide.md).

## Privacy

Handoff has no account, no telemetry and no network code of its own. The apps you connect read the board, and
an agent run sends its task to that agent's company. Some companies train on what you send:
[which ones, and how to keep your work out](https://ixelai.com/docs/privacy/#training).

## More

- [docs/guide.md](docs/guide.md): connecting each app, the plugin, dispatch, the worker, every command and rule.
- [CONTRIBUTING.md](CONTRIBUTING.md): run it from source and run the tests.
- [SECURITY.md](SECURITY.md): what Handoff protects. Report a security problem privately to **openixel.ai@proton.me**.

## License

[MIT](LICENSE)
