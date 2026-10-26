# Changelog

Notable changes to Lightfoot. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - Unreleased

First public release.

### Added since the first draft

- Rules: lossless JSON minification (`json`) and old-image stubs with saved image files (`images`).
- Pruning: an earlier, free prune after an idle gap (`prune.cold_trigger_tokens`, 150k), opt-in `prune.cold_keep_tokens`, and no paid epoch when a prune is near (`epochs.prune_near`).
- `lightfoot why`, `lightfoot dashboard`, `lightfoot status-line`, `lightfoot integrate`, `lightfoot hook-mode`, and experimental `lightfoot opencode`.
- Experimental Gemini CLI support: `lightfoot gemini`, a Gemini API adapter (ingest rules only, shadow unless opted in), Gemini prices, and a warning when Gemini CLI's auth can't be routed.
- Experimental Roo Code support (`lightfoot roo`), for installs that still run it after the project's shutdown.
- `json_trim`: long strings inside one line of JSON (Cline's tool results, compact MCP output) are trimmed like `spill`; everything else is re-encoded byte for byte.
- Model-written prune summaries through the API's on-demand compaction (`prune.summarizer = "model"`, opt-in, live mode only): requested in the background, swapped in at the next prompt, and the kept turns keep their thinking on enforcing accounts. `lightfoot estimate` prices them.
- `lightfoot eval check`, `repo_url` and `setup` in task files, and a public benchmark in `bench/`.
- Experimental Cline support: a Cline profile (its SDK and older tool names) and `lightfoot cline`, which prints a base URL for Cline's Anthropic provider. The prune summary now reads commands and searches through the profile.
- A Claude Code plugin (`plugin/`) with a marketplace file.
- Experimental OpenAI Chat Completions adapter (gateways, local servers, compatible providers; ingest rules only) and `lightfoot aider`.
- Experimental `lightfoot run -- <command>`: routes any tool that reads `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_BASE`, `OPENAI_BASE_URL`, `OPENAI_API_BASE` or `GOOGLE_GEMINI_BASE_URL`, one launch per protocol.
- `lightfoot capture -- <command>`: records an agent's requests, redacted, for confirming or extending its profile.
- Obsidian handoff for agents without a session-end hook: the proxy writes the note after `idle_handoff_minutes` of idleness, using the user's own `handoff_command`.
- `lightfoot estimate --cache`: what the 1-hour and 5-minute caches, with and without keep-alive pings, would have cost on your own idle gaps.
- `lightfoot estimate --subagents`: cost per subagent type from Claude Code's transcripts, and how much each run spends writing its own prefix.
- After a few shadow sessions, `lightfoot doctor` and the status line show the measured saving and the command to go live.
- Support levels per agent (wired, measure, output rules, history edits, model summaries, certified) in `lightfoot doctor` and the README, from one table (`support.py`).
- Model prices, their sources and dates, fallback models, the models that support model-written summaries and the beta header names live in one data file, `lightfoot/data/models.toml`, so a model launch is a data edit. `lightfoot doctor` warns when that data is more than 60 days old.
- Hook mode for opencode: `lightfoot hook-mode install --agent opencode` adds a plugin that condenses shell output with the same rules and checks as Claude Code's hook mode, without the proxy. Checked on opencode 1.19.4: the condensed output is what the model receives and what later requests re-send.
- `lightfoot plugins install --agent gemini|codex`: Lightfoot as a native Gemini CLI extension (`/lightfoot:stats`, `/lightfoot:why`, the condensed-context guidance) or Codex plugin (the three skills), installed with each agent's own installer. Both installed and run for real: Gemini CLI's `/lightfoot:stats` put real stats in front of the model, and Codex loaded all three skills.
- `lightfoot skills install`: the condensed-context guidance and `lightfoot-stats` and `lightfoot-why` as Agent Skills in `~/.agents/skills/`, for Gemini CLI, opencode and Codex CLI.
- `scripts/hook_watch.py`, weekly in CI: flags when Gemini CLI's, Codex's or Cline's after-tool hook gains an output field, which could make hook mode possible for it.
- `lightfoot doctor` shows hook mode and the skills, and accepts the Graphify note in AGENTS.md or GEMINI.md as well as CLAUDE.md.
- `lightfoot estimate --agent opencode`: replays opencode sessions from its SQLite database (opened read-only; only the message and part tables are read) through the output rules, priced from each step's recorded tokens.
- `lightfoot estimate --agent gemini`: replays Gemini CLI sessions (`~/.gemini/tmp/*/chats/`, current JSONL and older JSON files) through the output rules, priced from their recorded tokens.
- Prices for xAI (Grok 4.3 to 4.7, Grok 4.20, grok-build-0.1) and DeepSeek (Flash and V4 Pro, at off-peak rates) from their own pricing pages, so their savings are shown in dollars. Unlisted models of these families stay in tokens.
- `lightfoot doctor` lists tool names an agent's profile doesn't know, from the last 30 days of sessions, so an agent update that renames tools shows up.
- `scripts/agent_check.py` also compares each agent's request shapes with `scripts/agent_baseline.json`, so a new field, unit kind or tool name fails the weekly check.
- `scripts/agent_check.py` covers Gemini CLI too, with a mock of the Gemini API; Gemini CLI 0.65.0 passes, and the check runs weekly in CI.
- `scripts/agent_check.py`: runs Codex CLI, opencode and Aider through Lightfoot against a local mock API, end to end. All three now reach support level `0 · measure`.
- `lightfoot audit`: the fixed prefix (system prompt, tools, MCP servers) per project, its cost, unused MCP servers and the parts that differ between sessions.
- The extractive prune summary also lists the names each edit defined and the file paths every command used: misses 11.7% -> 11.2% at the same saving.
- Quality guards: when Claude keeps going back for condensed content in a live session, the next prune keeps more, then pruning stops; a tool whose trimmed output was needed is no longer trimmed. New signals: re-runs of trimmed commands and a jump in reads per turn after a prune.
- `cache.ttl` ("1h" or "5m"): `lightfoot claude` sets Claude Code's prompt-cache lifetime; API-key sessions default to 5 minutes, which `estimate --cache` and `doctor` flag when the hour would be cheaper.
- Opt-in `prune.early_trigger_tokens`: later prunes come earlier in sessions where the previous prune drew no signal (about +1 point at 100–120k offline, with lower summary coverage).
- Provider profiles (`providers.py`): cache model, history-edit safety, compaction support, credential policy and reporting unit per provider, chosen by wire format and upstream host; xAI, DeepSeek, OpenRouter and local servers recognised.
- `lightfoot estimate --agent codex`: offline replay of Codex CLI rollouts through the Responses adapter.
- Opt-in `prune.mid_loop`: prune inside long tool loops at a safe point (+0.3 points offline, lower coverage; never on enforcing accounts).
- Side requests (no tools: titles, quick checks) are tagged in the ledger; `lightfoot stats` reports their share on one line.
- Subscription limits: the status line records Pro and Max limit use per launch, and the segment and `lightfoot stats` show the saving in limit points.
- The release workflow builds the Homebrew formula and Scoop manifest from PyPI after publishing.
- `lightfoot estimate` uses your own settings and reports summary coverage; `eval report` adds retrieval calls and paired 95% intervals.
- `scripts/drift_check.py` and a weekly CI job that checks the newest Claude Code against a local mock API; property tests with Hypothesis.

### Security

- A project's `.lightfoot/config.toml` could set `upstream` (and the OpenAI and Gemini upstreams), so running Lightfoot inside a cloned repository could send your API key or login token to a server of the repository's choosing. It could also set `handoff_command`, which the idle handoff runs with a shell. These settings, `port`, `vault_path` and `handoff_folder` are now read from `~/.lightfoot/config.toml` only.
- `/lightfoot/health` now refuses foreign `Host` headers like every other route, so a rebound DNS name can't read the proxy's pid and version.

### Fixed

- `lightfoot estimate --agent codex|gemini|opencode` applied the live proxy's per-request time budget, so on a slow machine it skipped rewrites and underestimated the saving. Like the Claude Code estimate, they now replay without it.
- A prune dropped the system messages in the turns it replaced, which on Claude Code 2.1.310 includes the environment block (working directory, platform, git status). The summary now keeps them verbatim, each text once, newest last.
- Codex CLI 0.164.0's subagent tools `wait_agent` and `resume_agent` (in its `multi_agent_v1` group) were unknown to its profile; they now count as subagent calls.
- opencode 1.19.4 names its shell tool `bash`, its subagent tool `task`, and passes file paths as `filePath`. Its profile knew only the older names, so shell output was treated as generic output and reads and edits had no path (no stale-read stubs, no files in the prune summary). Both naming schemes are now recognised.
- Claude Code 2.1.310 puts system messages inside the conversation and often ends a request with one (a token-budget note). Lightfoot took that to mean no new tool output and no new prompt, so on that version the tool-output rules never ran and old turns were never pruned. The newest message is now the newest one that isn't a system message.
- A second proxy started at the same moment (two `lightfoot claude` launches at once) failed to bind, then deleted the running proxy's pid file, after which `lightfoot status` and `stop` reported it as not running. The pid file is now written only after the port is bound, and removed only by its owner.
- Cleanup read kept history files through their lower-cased path, which fails on case-sensitive file systems, and could then delete images those files still named.
- `lightfoot capture` numbered files by counting the folder on every request; requests saved at the same time could overwrite each other. Gemini `functionResponse.response` values are now masked like other tool output.
- Unknown launch ids in request paths were cached without bound.
- Faster: the status line no longer imports `urllib.request`, `dataclasses`, `shutil` or `subprocess` (about 140 ms -> 80 ms per refresh on Windows); model prices are looked up once per model instead of re-sorting the price table for every ledger row (`stats`, `dashboard`, `why`, the status line); streamed usage parsing skips the text deltas (about 8x less CPU per response); settings are cached between requests; and hook mode skips `argparse`, `typing`, `copy` and `hashlib` unless it spills.
- Models outside the Claude, OpenAI and Gemini families were priced as Claude Opus. They now have no price until `[prices]` adds one, and `lightfoot stats` shows their savings in tokens.
- Summary coverage counted tool-input keys and JSON escape sequences as references to pruned content, inflating the miss rate (13.6% instead of 11.7%). It now compares plain text and breaks misses down by kind and source.
- Model-written summaries were requested with a Pro or Max login too. Lightfoot now never sends a request of its own with a subscription credential and uses the extractive summary there.
- Image blocks were measured by their base64 length (about 40x too large) in epoch economics.
- Pruned images were dropped from history files; they are now saved next to them.
- An `ANTHROPIC_BASE_URL` in Claude Code's settings files silently bypassed the proxy; `lightfoot claude` now routes with `--settings`, and such a gateway becomes the upstream.
- Output that is one line of JSON was trimmed to its first 1,000 characters by `spill` and `old_results`, losing its end; it is now left whole.
- The `old_results` rule named a full-output file that was never written. The file is now saved first, and the output is left whole if saving fails.
- Hook mode no longer loads the planner, the adapters or the store: about 65–75 ms per tool call instead of 90–100 ms on Windows.

### Proxy

- Local proxy for Claude Code and Claude Agent SDK apps (Anthropic Messages API). Every rewrite is frozen and replayed byte-identically, checked by a structural validator, and backed by a retry ladder that falls back to the original request.
- Shadow mode by default: savings are measured and nothing is changed until `lightfoot shadow off`.
- Kill switch (`lightfoot off`) that takes effect immediately and keeps running sessions consistent.

### Savings

- Old-turn pruning: when a conversation passes 200k tokens and a new prompt arrives, the oldest turns are replaced by a summary built from the conversation itself. The full text is kept in `.lightfoot/history/`. Estimated saving on real sessions: 28%.
- Tool-output rules: noise stripping, trimming of large outputs (keeping every error line), and de-duplication of stale file reads and repeated searches.
- Break-even gating for history edits, using each model's real cache prices.
- Thinking-aware history edits: whether the account enforces the thinking-prefix check is learned automatically. Prunes use `drop_block` on enforcing accounts; other history edits are skipped there.

### Measuring

- `lightfoot estimate`: offline replay of past Claude Code sessions through the real rules, with their recorded usage.
- `lightfoot stats`: cost, savings, rule firings and over-compression signals per session.
- `lightfoot eval`: paired evaluation on your own tasks, with and without Lightfoot.
- `lightfoot report`: a numbers-only summary for issues.
- Per-model pricing for Anthropic and OpenAI, with `[prices]` overrides.

### Integrations

- Graphify: trimmed search results point to the graph report; repeated graph queries are de-duplicated.
- Obsidian: a SessionEnd hook writes a handoff note into the vault.
- Agent-driven installation through `docs/agent-install.md`, which asks before installing either companion.
- OpenAI Codex CLI support (experimental): `lightfoot codex`, shadow-only unless opted in.

### Operations

- `lightfoot doctor`, `lightfoot config set|get`, `lightfoot cleanup`, and daily automatic cleanup of inactive sessions' data.
- Hardening: browser requests are refused, request bodies are capped at 100 MB, and data folders are private to the user.
- Low overhead: orjson for request bodies, lazy hashing, standard-library health checks and rotating logs.
