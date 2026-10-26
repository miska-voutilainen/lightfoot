# Installing Lightfoot: instructions for AI coding agents

You are installing **Lightfoot** for the user: a local proxy that makes long coding-agent sessions cheaper. Follow these steps in order. Most steps are the same for every agent; the ones that apply only to Claude Code say so.

---

## Rules

1. **Install only what the user chooses.** Lightfoot is the only required part. Graphify and Obsidian are optional; never install them unless the user picked them in step 1.
2. **Say what each command does before you run it**, in one short sentence. Never run a command that installs software or changes settings without the user seeing it.
3. **Do not edit** CLAUDE.md, AGENTS.md, shell profiles, PATH or other settings files unless a step below says so, and then only after the user agrees.
4. **If a step fails, stop.** Show the error, explain it in plain words, and suggest the fix. Do not try alternative tools or packages that are not listed here.
5. Never ask for API keys, passwords or tokens. Lightfoot doesn't need any.

---

## Step 1: Ask about the optional companions

Before installing anything, ask the user one question, using your question tool if you have one (for example AskUserQuestion), otherwise in plain text. Make it clear that both companions are optional and can be added later:

> Lightfoot will be installed. We also recommend two optional companions that work alongside it. Would you like either?
>
> - **Lightfoot only** — just the proxy.
> - **+ Graphify** — builds a knowledge graph of your codebase, so your agent spends fewer tokens searching for code. Free Python package.
> - **+ Obsidian** — a free notes app. Lightfoot writes a short handoff note into an Obsidian vault at the end of each session, so you can start fresh sessions without the old context. Desktop app.
> - **+ Both** — Graphify and Obsidian.

Remember the answer and do only the matching steps below.

---

## Step 2: Check the prerequisites

- Python 3.11 or newer: run `python --version` (Windows: `py --version`; macOS and Linux: `python3 --version`). If it's missing or older, stop and tell the user to install Python 3.11+ from [python.org/downloads](https://www.python.org/downloads/).
- An installer for command-line tools. Check for `uv --version`, then `pipx --version`, and use the first one found. If neither is installed, ask the user whether to install pipx (`python -m pip install --user pipx`, then `python -m pipx ensurepath`) or to use plain pip instead.

---

## Step 3: Install Lightfoot

Run the first command that matches what you found in step 2:

```sh
uv tool install lightfoot
pipx install lightfoot
python -m pip install --user lightfoot
```

If the installer reports that the package `lightfoot` doesn't exist (it may not be published to PyPI yet), install it from GitHub instead, with the same tool:

```sh
uv tool install git+https://github.com/miska-voutilainen/lightfoot
pipx install git+https://github.com/miska-voutilainen/lightfoot
python -m pip install --user git+https://github.com/miska-voutilainen/lightfoot
```

Check it with `lightfoot --version`. If the command isn't found, the tool folder isn't on PATH yet. Run `uv tool update-shell` or `pipx ensurepath` (whichever installer you used), tell the user that new terminals will find `lightfoot`, and use the full path printed by the installer for the rest of this session.

---

## Step 4: Check the setup

Run `lightfoot doctor` and briefly report anything marked `XX`.

---

## Step 5: Graphify (only if the user chose it)

```sh
uv tool install graphifyy      # or: pipx install graphifyy   (the package name has a double y)
graphify install               # detects the user's agent and registers the /graphify skill
```

Tell the user:

- To build a project's graph, run `/graphify .` in their agent inside that project. Lightfoot detects the graph (`graphify-out/GRAPH_REPORT.md`) automatically the next time it starts an agent there.
- Once a graph exists, adding the line `See graphify-out/GRAPH_REPORT.md for the project map.` to the project's instruction file (CLAUDE.md for Claude Code, AGENTS.md for most other agents) keeps the map in the agent's cached context. Offer to add it, and do so only if they say yes and the graph already exists.

---

## Step 6: Obsidian (only if the user chose it)

**6a. The app.** Ask whether Obsidian is already installed. If it isn't, install it with the system's package manager, telling the user that this installs a desktop application:

| System | Command |
| --- | --- |
| Windows | `winget install --id Obsidian.Obsidian --exact` |
| macOS (Homebrew) | `brew install --cask obsidian` |
| Linux (Flatpak) | `flatpak install flathub md.obsidian.Obsidian` |

If none of these package managers is available, give the user the download page, [obsidian.md/download](https://obsidian.md/download), and continue; the app is only needed for reading the notes, not for Lightfoot to write them.

**6b. The vault.** Ask whether they have an Obsidian vault they want the notes in, and get its absolute path. If they don't have one, offer to create one at `~/Documents/Obsidian/Claude`: create that folder and an empty `.obsidian` folder inside it. Obsidian can then open it as a vault, and Lightfoot recognises it.

**6c. Connect it to Lightfoot** (use the absolute path; keep the quotes):

```sh
lightfoot config set integrations.vault_path "ABSOLUTE/PATH/TO/VAULT"
```

**6d. When the note is written.** This depends on the agent:

- **Claude Code:** run `lightfoot handoff install`. Tell the user that it adds a SessionEnd hook to `~/.claude/settings.json` and saves a backup of that file first. At the end of each session of 5 or more turns, it writes a handoff note into `<vault>/Claude Handoffs/`, using a short Haiku request through their normal Claude Code login. Remove it at any time with `lightfoot handoff uninstall`.
- **Any other agent:** there is no session-end hook, so the proxy writes the note after a session has been idle for 30 minutes. Lightfoot never sends that summary request itself, so it needs a command of the user's own that reads a prompt on stdin and prints the note, set with `lightfoot config set integrations.handoff_command "…"`. Explain this and ask whether they want to set one now; skip it if they don't.

Run `lightfoot doctor` again; it should show the vault as detected.

---

## Step 7: Offer the extras

**On Gemini CLI or Codex CLI:** offer `lightfoot plugins install --agent gemini` (or `--agent codex`). It installs Lightfoot as a native extension or plugin with the agent's own installer: guidance that teaches the model to read Lightfoot's notes and find the full text they point to, plus stats and why (on Gemini CLI as `/lightfoot:stats` and `/lightfoot:why`). Gemini CLI asks the user whether to trust the folder; that answer is theirs, so let them give it. Remove it with `lightfoot plugins uninstall --agent …`.

**On opencode:** offer `lightfoot skills install`, which adds the same three as skills in `~/.agents/skills/`. Remove them with `lightfoot skills uninstall`. On opencode, also offer `lightfoot hook-mode install --agent opencode`, which condenses shell output even when opencode isn't started through Lightfoot.

**On Claude Code:** ask the user whether they want either of these. Both are optional, and both can be added later:

| Option | What it does | How to add it |
| --- | --- | --- |
| Status line | Shows the saving for the current session in Claude Code's status line. An existing status line keeps working; the segment is added to its end | `lightfoot status-line install`; remove with `lightfoot status-line uninstall` |
| Claude Code plugin | Adds `/lightfoot:stats` and `/lightfoot:why`, and a skill that tells Claude how to use condensed context | In Claude Code, the user types `/plugin marketplace add miska-voutilainen/lightfoot`, then `/plugin install lightfoot@lightfoot` |

Tell the user that `status-line install` changes `~/.claude/settings.json` and saves a backup of it first. The plugin commands are slash commands the user types in Claude Code; don't run them yourself.

---

## Step 8: Finish

Tell the user, briefly:

- **Start the agent through Lightfoot from now on.** Give them the command for their agent; every argument passes through unchanged:

  | Agent | Command |
  | --- | --- |
  | Claude Code | `lightfoot claude` |
  | Codex CLI, opencode, Gemini CLI, Aider | `lightfoot codex`, `lightfoot opencode`, `lightfoot gemini`, `lightfoot aider` |
  | Cline, Roo Code | `lightfoot cline` or `lightfoot roo` prints a base URL to paste into the extension's Anthropic provider |
  | Anything else | `lightfoot run -- <command>` |

  This current session isn't running through Lightfoot; they need to exit and start a new one that way. Support for agents other than Claude Code is experimental.
- **It starts in shadow mode:** it measures what it would save and changes nothing. After some normal use, `lightfoot stats` shows the numbers, and `lightfoot shadow off` turns the savings on.
- **`lightfoot estimate` shows what it would have saved on their past sessions** (Claude Code sessions; `lightfoot estimate --agent codex` for Codex CLI). It runs offline with no API calls; offer to run it now.
- `lightfoot off` stops all rewriting immediately, and `lightfoot doctor` checks the setup at any time.
