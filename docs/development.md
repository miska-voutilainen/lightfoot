# Development

How to set up, test, measure and release Lightfoot.

---

## Setup

```sh
git clone https://github.com/miska-voutilainen/lightfoot
cd lightfoot
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip install -e ".[dev]"
.venv/bin/pip install pre-commit && .venv/bin/pre-commit install
```

The install is editable: after a code change, `lightfoot restart` makes the background proxy pick it up. Set `LIGHTFOOT_HOME` to a scratch folder to keep test data out of your real `~/.lightfoot`, and give it a different `port` in its `config.toml`.

---

## Everyday commands

| Command | What it does |
| --- | --- |
| `pytest` | the test suite (about 10 s; no network, no API calls) |
| `pytest --cov=lightfoot` | with coverage; CI requires at least 80% |
| `ruff check lightfoot tests` / `ruff format lightfoot tests` | lint and format (pre-commit runs both) |
| `bandit -q -r lightfoot -ll` | static security scan, as in CI |
| `pip-audit --skip-editable` | known vulnerabilities in dependencies, as in CI |
| `python scripts/benchmark.py` | idle and per-request CPU, latency and memory of the real daemon (needs `psutil`) |
| `python scripts/drift_check.py` | run the installed Claude Code against a local mock API and compare its request shapes with `scripts/drift_baseline.json` (no API cost) |
| `python scripts/hook_watch.py` | check whether Gemini CLI's, Codex's or Cline's after-tool hook gained an output field (weekly in CI; reads GitHub, no model calls) |
| `python scripts/agent_check.py` | run the installed Codex CLI, opencode, Aider and Gemini CLI through Lightfoot against a local mock API, end to end (no API cost; weekly in CI) |
| `python scripts/docs_assets.py` | regenerate the diagrams and charts in `docs/assets/` |
| `lightfoot estimate` | replay your own past Claude Code sessions through the current rules, offline |

---

## Layout

```text
lightfoot/
  adapters/      wire formats -> neutral Index (anthropic.py, responses.py, chat.py, gemini.py, base.py)
  transform.py   the planner: replay, epochs, prune integration, signals
  ingest.py      the ingest rules (json, spill, noise), light enough for hook mode
  prune.py       old-turn pruning: boundaries, summary, history file, fingerprint
  validate.py    the structural validator every rewritten request passes
  server.py      HTTP proxy, fail-safe ladder, ledger recording
  store.py       SQLite schema and migrations
  profiles.py    agent tool names -> neutral kinds
  providers.py   provider profiles: cache model, history-edit safety, compaction, credentials, unit
  guards.py      quality guards: condense less when the agent goes back for condensed content
  support.py     support level per agent, shown by doctor and checked against the README
  text.py        pure text helpers (noise, error lines, selection, JSON minification)
  images.py      image token cost from the header; saving image blocks to files
  pricing.py     per-model prices and cache multipliers, loaded from data/models.toml
  data/          models.toml: prices, sources, fallbacks, compaction models, beta headers; skills/
  estimate.py    offline replay of past transcripts, summary coverage
  cachesim.py    `estimate --cache`: cache lifetimes and keep-alive on recorded gaps
  subagents.py   `estimate --subagents`: cost per subagent type
  codexlog.py    `estimate --agent codex`: replay of Codex CLI rollouts
  geminilog.py   `estimate --agent gemini`: replay of Gemini CLI sessions
  opencodelog.py `estimate --agent opencode`: replay of opencode's session database
  opencodehook.py hook mode for opencode: its plugin and the `hook opencode-tool-after` entry point
  agentskills.py `lightfoot skills`: Agent Skills for Gemini CLI, opencode and Codex CLI
  agentplugins.py `lightfoot plugins`: a Gemini CLI extension and a Codex plugin, built from those skills
  audit.py       `lightfoot audit`: the fixed prefix per project
  capture.py     `lightfoot capture`: redacted request recording
  idlehandoff.py Obsidian handoff for agents without a session-end hook
  launch.py      daemon management, the agent wrappers, --settings routing
  hookmode.py    the ingest rules as a PostToolUse hook, without the proxy
  integrate.py   route every Claude Code surface through the proxy
  statusline.py  the status line segment
  why.py         `lightfoot why`
  dashboard.py   `lightfoot dashboard`, one self-contained HTML page
  cli.py, reports.py, config.py, maintenance.py, handoff.py, evaluate.py, usage.py, fastjson.py
tests/           one file per area; conftest.py has request builders; test_properties.py uses Hypothesis
scripts/         benchmark.py, drift_check.py and its drift_baseline.json, agent_check.py, docs_assets.py, release_manifests.py
bench/           the public benchmark: tasks.toml, benchenv.py (a virtualenv per worktree), README.md
plugin/          the Claude Code plugin (skills for /lightfoot:stats and /lightfoot:why)
docs/            guide.md, architecture.md, comparison.md, development.md, agent-install.md
docs/assets/     SVG diagrams and charts, generated by scripts/docs_assets.py
```

---

## Testing

- Tests build requests with the helpers in `tests/conftest.py` (`request`, `user_text`, `tool_use`, `tool_result`, `big_output`, `read_output`).
- Server tests run the real Starlette app against a mock upstream through `httpx.ASGITransport`, so no ports or network are needed.
- `tests/test_cli.py::test_daemon_lifecycle` starts the real background daemon on a free port.
- Time budgets are lifted in the shared `cfg` fixture so results don't depend on machine load; `test_over_budget_drops_new_rewrites` tests the budget explicitly.
- `tests/test_properties.py` checks the safety promises (the validator, replay, JSON minification, trimming) on generated inputs with Hypothesis, which the `dev` extra installs.
- `tests/test_plugin.py` checks the plugin's manifest and skills; `claude plugin validate plugin` is the full check when Claude Code is installed.
- Coverage gaps worth closing next: `evaluate.run_one`, `launch.run_claude` and `run_codex`, and the handoff worker. They drive external programs (`claude`, `codex`, `git`) and need stand-in executables on PATH.

When you change rewriting, keep the invariants in [architecture.md](architecture.md#validation-and-fail-safe): only tool output or a validated prune changes, rewrites are deterministic and frozen, and problems drop only new rewrites. Add a test that would fail if your change broke one.

---

## Measuring a rule change

1. Run `lightfoot estimate` before and after. It replays your real transcripts through the planner with recorded usage.
2. For quality, run the paired eval on a task file (`lightfoot eval run tasks.toml`; see `examples/tasks.example.toml`, or the [benchmark](../bench/README.md)). Check a new task file first with `lightfoot eval check`, which spends nothing.
3. For CPU, run `python scripts/benchmark.py` before and after.

Include those numbers in the pull request.

---

## Agent drift

Lightfoot depends on the shape of Claude Code's requests, which can change in any release. `.github/workflows/drift.yml` runs `scripts/drift_check.py` every Monday with the newest Claude Code from npm. The script starts a mock Anthropic API on 127.0.0.1 that answers once with a Bash tool call and then with text, so Claude Code sends a request carrying a tool result. Each captured request is indexed, planned and validated by Lightfoot, and its top-level fields, content block types and beta headers are compared with the baseline.

A failure isn't necessarily a bug, because unknown content is never rewritten, but it's worth a look before users meet it. After reviewing, accept the new shapes with `python scripts/drift_check.py --update` and commit the baseline.

The other agents are checked in the same workflow, in a second job with the newest Codex CLI, opencode, Aider and Gemini CLI. `scripts/agent_check.py` starts Codex CLI, opencode and Aider the way a user would (`lightfoot codex`, `lightfoot opencode`, `lightfoot aider`), in shadow mode, with a throwaway `LIGHTFOOT_HOME` whose upstreams point at one mock API that speaks Anthropic Messages, OpenAI Responses and Chat Completions. An agent passes when its requests reached the mock API through the proxy, Lightfoot planned every one, the ledger recorded them without errors, and (for agents with a shell tool) a request carried the tool output. Each agent's request shapes (top-level fields, kinds of conversation units, tool names) are also compared with `scripts/agent_baseline.json`, and anything new fails the check, as for Claude Code. That is the evidence for support level `0 · measure`. After reviewing a change, accept it with `python scripts/agent_check.py --update`. Run it locally the same way; agents named on the command line must be installed, and `--bin-dir` points it at ones that aren't on PATH.

---

## Docs

The README is also the PyPI page, and many people read the docs in an editor preview, so they use only Markdown that renders everywhere:

- **No GitHub-only syntax:** no `> [!NOTE]` alerts and no Mermaid. Use a blockquote with a bold lead (`> **Starts in shadow mode.** …`) and an SVG from `docs/assets/`.
- **Diagrams are generated.** Change the drawing code in `scripts/docs_assets.py` and run it; don't edit the SVGs by hand. Each follows the reader's light or dark mode through `prefers-color-scheme`.
- **Numbers come with their source.** A measured figure names what was measured; an illustration says it is one.
- `tests/test_support.py` checks the README's agent table against `support.py`: change both together.

---

## Releasing

1. Update `__version__` in `lightfoot/__init__.py` and move the "Unreleased" section of `CHANGELOG.md` under the new version.
2. Make sure CI is green on `main`.
3. Tag and push: `git tag v1.0.0 && git push --tags`. `.github/workflows/release.yml` builds and publishes to PyPI through trusted publishing. Before the first release, configure the `lightfoot` project on PyPI to trust that workflow and its `pypi` environment.
4. The workflow's `manifests` job then builds the Homebrew formula and Scoop manifest from what PyPI serves (`scripts/release_manifests.py`) and attaches them to the run as the `manifests` artifact. Copy them into the tap and bucket repositories. winget isn't covered: it installs Windows installers, not Python packages.
