# User guide

Everything you need to run Lightfoot day to day. New here? Start with the [README](../README.md).

- [Install](#install)
- [Turn it on](#turn-it-on)
- [Commands](#commands)
- [Configuration](#configuration)
- [Supported agents](#supported-agents)
- [Pro and Max subscriptions](#pro-and-max-subscriptions)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)

---

## Install

The easiest way is to let your coding agent do it: paste the prompt from the [README](../README.md#install). To install it yourself you need Python 3.11+:

```sh
uv tool install lightfoot      # or: pipx install lightfoot, or: pip install lightfoot
lightfoot doctor               # checks the setup
```

### Optional extras

Each one is a single command to add or remove.

| Extra | For | Command | Adds |
| --- | --- | --- | --- |
| Extension | Gemini CLI | `lightfoot plugins install --agent gemini` | Teaches the model to read Lightfoot's notes and find the full text; `/lightfoot:stats` and `/lightfoot:why` |
| Plugin | Codex CLI | `lightfoot plugins install --agent codex` | The same, as skills (`lightfoot-stats`, `lightfoot-why`) |
| Plugin | Claude Code | `/plugin marketplace add miska-voutilainen/lightfoot`<br>`/plugin install lightfoot@lightfoot` | The same, as `/lightfoot:stats` and `/lightfoot:why` |
| Skills | opencode (and Gemini CLI, Codex CLI) | `lightfoot skills install` | The same, as skills in `~/.agents/skills/` |
| Hook mode | Claude Code | `lightfoot hook-mode install` | Condenses Bash, PowerShell and MCP output without the proxy |
| Hook mode | opencode | `lightfoot hook-mode install --agent opencode` | Condenses shell output without the proxy |
| Status line | Claude Code | `lightfoot status-line install` | The saving in Claude Code's status line, after your own |
| Every surface | Claude Code | `lightfoot integrate on` | Routes the VS Code extension, the desktop app and plain `claude` too |
| Obsidian handoff | Claude Code; others through `handoff_command` | `lightfoot config set integrations.vault_path "…"`<br>`lightfoot handoff install` | A handoff note in your vault when a session ends |

The Gemini CLI extension and the Codex plugin are installed by each agent's own installer, so they show in `gemini extensions list` and `codex plugin list`; Gemini CLI asks whether you trust the folder the extension comes from (Lightfoot's data folder). The skills go in `~/.agents/skills/`, which Gemini CLI, opencode and Codex CLI all read; use one or the other per agent. Hook mode isn't possible yet for Gemini CLI, Codex CLI or Cline: their hooks can't replace tool output cleanly, and a weekly check watches for that to change. Through the proxy, every agent gets the same rules either way.

`status-line`, `integrate`, `hook-mode` and `handoff` each back up `~/.claude/settings.json` before changing it; `hook-mode --agent opencode` writes one plugin file into opencode's plugins folder.

---

## Turn it on

Lightfoot starts in **shadow mode**: it computes every rewrite and logs what it would save, but sends your original requests.

```sh
lightfoot estimate      # what it would have saved on your past sessions (offline)
lightfoot stats         # what it projects for sessions since you installed it
lightfoot shadow off    # start saving (applies to new launches)
lightfoot off           # stop new rewrites immediately; `lightfoot on` resumes
```

**Resume sessions through Lightfoot too** (`lightfoot claude --resume`). Resuming without it re-sends history the model saw in condensed form, which costs one full cache rewrite.

---

## Commands

### Run an agent

| Command | What it does |
| --- | --- |
| `lightfoot claude [--live\|--shadow] …` | Start Claude Code through the proxy (starts the proxy if needed). Every other argument goes to `claude`. |
| `lightfoot codex`, `opencode`, `gemini`, `aider` | The same for other agents (experimental; see [Supported agents](#supported-agents)). |
| `lightfoot cline`, `lightfoot roo` | Print a base URL to paste into the extension's Anthropic provider. |
| `lightfoot run -- <command>` | Any tool that reads a base-URL variable. |
| `lightfoot capture -- <command>` | Run in shadow mode and save its requests, redacted (structure kept, text masked, no auth headers). |

### See what it does

| Command | What it does |
| --- | --- |
| `lightfoot estimate` | Offline estimate on your past sessions. `--pruning` adds the ceiling at other thresholds; `--cache` prices the 1-hour and 5-minute caches on your idle gaps and recommends a `cache.ttl`; `--subagents` costs each subagent type; `--agent codex`, `--agent gemini` and `--agent opencode` replay Codex CLI, Gemini CLI and opencode sessions. |
| `lightfoot stats [--days N] [--session ID]` | Cost, saving and rule health per session. |
| `lightfoot why [SESSION] [--full]` | What was condensed in a session, where the full text is, what it saved. |
| `lightfoot dashboard` | A local report page: one HTML file, nothing loaded from the network. |
| `lightfoot audit` | What your system prompt, tools and MCP servers cost per project, unused MCP servers, and parts that stop sessions sharing a cache. Advisory only. |
| `lightfoot eval run tasks.toml` | Paired eval on your own tasks, with and without Lightfoot ([example](../examples/tasks.example.toml)). `eval check` validates a task file for free. |
| `lightfoot report` | A numbers-only summary to paste into an issue. |
| `lightfoot events` | Recent warnings and notes. |

### Control it

| Command | What it does |
| --- | --- |
| `lightfoot shadow on\|off` | Measure only, or save. Applies to new launches. |
| `lightfoot off` / `on` | Kill switch, immediate. `off` keeps replaying stored rewrites so running sessions stay consistent; `off --hard` is raw passthrough. |
| `lightfoot start\|stop\|restart\|status` | Manage the background proxy. |
| `lightfoot doctor` | Check the setup, integrations, hooks and versions. |
| `lightfoot config [set KEY VALUE \| get KEY]` | Show or change `~/.lightfoot/config.toml`. |
| `lightfoot cleanup [--dry-run]` | Delete stored content of inactive sessions (also runs daily). |
| `lightfoot handoff install\|uninstall` | The Obsidian session-handoff hook. |

---

## Configuration

`lightfoot config` prints `~/.lightfoot/config.toml`, creating it with every setting commented.

```sh
lightfoot config set prune.trigger_tokens 300000      # condense later, keep more
lightfoot config set rules.noise false                # turn one rule off
lightfoot config set cache.ttl 1h                     # see `lightfoot estimate --cache`
```

| Section | Holds |
| --- | --- |
| top level | `mode`, `port`, upstream URLs, retention, `billing` |
| `[rules]` | each rule on or off |
| `[thresholds]`, `[prune]`, `[epochs]` | when and how much to condense |
| `[guards]` | when to condense less after the agent goes back for condensed content |
| `[exclude]` | tool kinds or names a rule never touches |
| `[integrations]` | Graphify, Obsidian, handoff |
| `[prices]` | price overrides and additions |

A project can override settings in `<project>/.lightfoot/config.toml`, **except** `port`, `upstream`, `openai_upstream`, `gemini_upstream`, `vault_path`, `handoff_folder` and `handoff_command`. A project file arrives with the repository, so it may not decide where your requests and credentials go, or what command runs.

---

## Supported agents

`lightfoot doctor` shows each agent's support level and its next step. Below **certified**, every agent starts in shadow mode; experimental ones go live only with `--live --experimental`.

| Agent | How to run it | Notes |
| --- | --- | --- |
| Claude Code | `lightfoot claude` | Everything: output rules, old-turn pruning, model-written summaries. |
| Claude Agent SDK apps | `lightfoot run -- <app>` | Anything that honours `ANTHROPIC_BASE_URL`. |
| OpenAI Codex CLI | `lightfoot codex` | Needs `OPENAI_API_KEY`; ChatGPT sign-in can't be routed. Output rules only. |
| opencode | `lightfoot opencode` | Base URL passed in `OPENCODE_CONFIG_CONTENT`; your config files are untouched. |
| Gemini CLI | `lightfoot gemini` | Needs Gemini API key auth: "Sign in with Google" and Vertex AI don't use a base URL. Output rules only. |
| Cline | `lightfoot cline` | Paste the printed URL into Cline's Anthropic provider ("Use custom base URL"). Cline's tool results are compact JSON, so mostly pruning applies. |
| Roo Code | `lightfoot roo` | As for Cline. The project was archived on 2026-05-15. |
| Aider | `lightfoot aider` | Measured only: Aider pastes files into messages instead of calling tools. |
| Other tools | `lightfoot run -- <command>` | Sets `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_BASE`, `OPENAI_BASE_URL`, `OPENAI_API_BASE` and `GOOGLE_GEMINI_BASE_URL` for one launch. |
| Cursor, Windsurf | - | Not possible: they send requests from their own servers. |

---

## Pro and Max subscriptions

This applies to Claude Code with a claude.ai login.

- **What you get.** The same rules. They save limit headroom rather than money; the status line shows the saving as a share of your 5-hour limit.
- **How it routes.** Anthropic documents [routing Claude Code through a gateway](https://code.claude.com/docs/en/llm-gateway) with `ANTHROPIC_BASE_URL`. Without a gateway credential your claude.ai login stays active, and Lightfoot forwards your headers unchanged.
- **What Lightfoot never does.** Send a request of its own with your login. Model-written summaries (`prune.summarizer = "model"`) need an API key and fall back to the extractive summary otherwise.
- **What isn't documented.** Whether a gateway may *rewrite* subscription requests, which live mode does. Shadow mode only measures; going live on a subscription is your call. Hook mode condenses tool output without a proxy.
- **When it can't route.** Managed settings with `allowedProviders = ["customEndpoint"]` and a fixed `ANTHROPIC_BASE_URL` make Claude Code refuse your own proxy.

---

## Troubleshooting

| Symptom | Try |
| --- | --- |
| Something looks wrong | `lightfoot off`, then `lightfoot events` and `lightfoot doctor`. [Open an issue](https://github.com/miska-voutilainen/lightfoot/issues) with the output of `lightfoot report`. |
| The proxy won't start | Read `~/.lightfoot/proxy.stderr.log`. If another program uses the port: `lightfoot config set port 8788`. |
| After upgrading | `lightfoot restart` when no session is active. |
| A session behaves oddly after a prune | `lightfoot why` shows what was condensed and where the full text is. |
| `doctor` lists tools "not in its profile" | The agent added or renamed tools since its profile was written, so their output is condensed as generic output. [Open an issue](https://github.com/miska-voutilainen/lightfoot/issues) with the names. |

---

## Uninstall

```sh
lightfoot integrate off            # each of these only if you turned it on
lightfoot hook-mode uninstall      # and: lightfoot hook-mode uninstall --agent opencode
lightfoot skills uninstall
lightfoot plugins uninstall --agent gemini     # or --agent codex
lightfoot status-line uninstall
lightfoot handoff uninstall
lightfoot stop
uv tool uninstall lightfoot        # or: pipx uninstall lightfoot
```

Then delete `~/.lightfoot/` and any `.lightfoot/` folders in your projects.
