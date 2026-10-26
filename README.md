<h1 align="center">Lightfoot V1</h1>

<p align="center">
  <a href="https://pypi.org/project/lightfoot/"><img src="https://img.shields.io/pypi/v/lightfoot.svg" alt="PyPI"/></a>
  <a href="https://github.com/miska-voutilainen/lightfoot/actions/workflows/ci.yml"><img src="https://github.com/miska-voutilainen/lightfoot/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
</p>

<p align="center">
  <b>Long coding-agent sessions, cheaper. Without making them worse.</b>
</p>

Every turn of an agent session re-sends the whole conversation, and on long sessions most of the bill is the model re-reading it from the cache. Lightfoot is a local proxy between your agent and the model API that keeps that conversation lean. Start your agent through it (`lightfoot claude`, `lightfoot codex`, `lightfoot run -- <command>`) and nothing else changes.

- **Never mid-task.** Old turns are condensed only at your next prompt, past 200k tokens. Work in progress is never cut into.
- **Nothing is lost.** Everything condensed is saved under `.lightfoot/`, and the summary says where to find it.
- **Free to run.** No model calls. The summary is built from the conversation itself, and nothing leaves your machine except the request your agent was already sending.

## Install

Paste this into your coding agent. It asks before installing anything optional, and shows each command before running it ([the instructions it follows](docs/agent-install.md)):

```text
Install Lightfoot for me: run `curl -fsSL https://raw.githubusercontent.com/miska-voutilainen/lightfoot/main/docs/agent-install.md`, then follow those instructions step by step.
```

**Or install it yourself.** Needs Python 3.11+ and uv (or pipx):

```sh
uv tool install lightfoot      # or: pipx install lightfoot
lightfoot estimate             # what it would have saved on your past sessions (offline, free)
lightfoot claude               # start your agent through Lightfoot; every argument passes through
```

It starts in **shadow mode**: it measures what it would save and changes nothing. When `lightfoot stats` looks good, `lightfoot shadow off` turns the savings on. `lightfoot off` stops them immediately.

---

## See it in action

`lightfoot estimate` replays your existing session transcripts offline and prices them with and without Lightfoot. A real run on 139 sessions (8,646 API calls, API prices as of 2026-10-25):

| Where the cost went | Cost | Share |
| --- | ---: | ---: |
| Cache reads | $560.08 | 58.1% |
| Cache writes | $248.76 | 25.8% |
| Output | $155.75 | 16.1% |
| Uncached input | $0.14 | 0.0% |
| Total | $964.72 | 100% |

58% of the money went to re-reading the conversation from the cache. That is the part Lightfoot shrinks: with it, the same sessions would have cost **$706.88**, a saving of **26.7%**. The longest sessions saved 35 to 46%; short sessions that never pass 200k tokens are left mostly alone and save close to nothing.

It is an estimate. Shadow mode measures the real thing on your own work, and `lightfoot eval` runs a paired comparison.

---

## What it does

Your agent sends its request to Lightfoot on your machine. Lightfoot condenses it, validates the result against the original, and forwards it to the model API. The response comes back unchanged.

| Rule | What it does |
| --- | --- |
| **Old-turn pruning** | Past 200k tokens, at your next prompt, the oldest turns become one summary. Your requests stay verbatim. |
| **Noise stripping** | ANSI codes, terminal redraws, progress bars and repeated lines go. |
| **Large-output trimming** | Output over ~4k tokens keeps its head, tail and every error line. The rest is saved to a file. |
| **JSON minification** | Pretty-printed JSON loses only its whitespace, checked by re-parsing. |
| **Stale reads and searches** | Superseded file reads and repeated searches become one-line stubs. |
| **Old images** | All but the 3 newest screenshots become a stub. Each image is saved to a file. |

Each rewrite happens once and is then replayed byte for byte on every later turn, so the prompt cache stays warm. Almost all of the saving comes from pruning old turns.

---

## What stays intact

- **Your words.** Your first message, your last 3 prompts with all their work, and at least the latest 100k tokens are kept as they were.
- **Fail-safe.** If a rewrite fails validation for any reason, your original request goes out instead.
- **Measured.** `lightfoot stats` counts how often the agent goes back for condensed content, and flags rules that cut too much.

---

## Supported agents

| Agent | Level | Status |
| --- | --- | --- |
| Claude Code | 3 · model summaries | Supported |
| Apps on the Claude Agent SDK | 2 · history edits | Supported |
| OpenAI Codex CLI | 0 · measure | Experimental |
| opencode | 0 · measure | Experimental |
| Gemini CLI | 0 · measure | Experimental, API-key auth only |
| Cline | wired | Experimental |
| Roo Code | wired | Experimental (project archived) |
| Aider | 0 · measure | Measured only |
| Other tools | wired | Experimental, via `lightfoot run -- <command>` |
| Cursor, Windsurf | - | Not possible: requests leave from their servers |

The level says how much is proven for each agent, from least to most: `wired` (built from its source, unit-tested) > `0 · measure` (its real traffic parsed) > `1 · output rules` > `2 · history edits` > `3 · model summaries` > `certified` (a passing paired eval). Experimental agents stay in shadow mode unless started with `--live --experimental`. [Details per agent](docs/guide.md#supported-agents).

---

## Common commands

```sh
lightfoot claude                           # start an agent through Lightfoot (also: codex, opencode, gemini, aider)
lightfoot run -- <command>                 # any other tool that reads a base-URL variable
lightfoot estimate                         # what it would have saved on your past sessions
lightfoot stats                            # cost, savings and rule health per session
lightfoot why                              # what was condensed in a session, and where the full text is
lightfoot dashboard                        # a local report page
lightfoot shadow off                       # start saving (shadow on goes back to measuring)
lightfoot off                              # kill switch, immediate (lightfoot on resumes)
lightfoot doctor                           # check the setup
lightfoot plugins install --agent gemini   # Lightfoot's extension for Gemini CLI (or --agent codex)
lightfoot skills install                   # the same guidance as skills, e.g. for opencode
```

Resume sessions through Lightfoot too (`lightfoot claude --resume`); resuming without it re-sends the history uncondensed and rewrites the cache once. Hook mode (no proxy, for Claude Code and opencode), the Claude Code plugin and status line, every setting, and troubleshooting: **[the user guide](docs/guide.md)**.

---

## Privacy

- **Local only.** Listens on 127.0.0.1. Requests with other Host headers, and requests from browsers, are refused.
- **Your credentials** are forwarded unchanged and never written anywhere. With a subscription login, Lightfoot sends no requests of its own.
- **Project files can't take over.** A repository's `.lightfoot/config.toml` can't redirect your requests or run commands.
- **Retention.** Stored content is deleted 30 days after a session's last use. The cost ledger holds only token counts.
- **No telemetry**, no usage tracking, no analytics.

It uses about 50 MB of memory and almost no CPU when idle; a turn of a long session costs it a few tens of milliseconds. Works with API keys and with Pro and Max subscriptions ([what that means](docs/guide.md#pro-and-max-subscriptions)).

---

## Works well with Graphify and Obsidian

Both are optional, detected when present, and never installed unless you ask.

- **[Graphify](https://github.com/Graphify-Labs/graphify)** cuts the tokens spent *finding* code. Lightfoot points trimmed searches at `graphify-out/GRAPH_REPORT.md` and drops repeated graph queries.
- **[Obsidian](https://obsidian.md)** cuts the tokens spent *re-establishing* context. Lightfoot writes a handoff note (what's done, decisions, open threads) into your vault when a session ends.

---

## Learn more

- [User guide](docs/guide.md): every command and setting, agents, subscriptions, troubleshooting, uninstall
- [Architecture](docs/architecture.md): exactly what Lightfoot does to a request, and why
- [Comparison](docs/comparison.md): how it differs from other token-saving tools
- [Development](docs/development.md): set up, test, measure, release
- [Security](SECURITY.md): guarantees, and how to report a problem

---

## Contributing

Contributions are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the setup and the git workflow.

---

## License

[MIT](LICENSE). Lightfoot is an independent project, not affiliated with or endorsed by Anthropic, OpenAI, Google, Graphify or Obsidian.
