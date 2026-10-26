# Contributing

Bug reports with `lightfoot report` output are as useful as code. This page covers the rules every change has to keep and how to propose one.

---

## Setup

See [docs/development.md](docs/development.md) for the environment, the test, lint and benchmark commands, the code layout, and releasing. [docs/architecture.md](docs/architecture.md) explains how the parts fit together.

---

## Rules every change keeps

Lightfoot rewrites live requests to a model, so these rules are what make it safe to run. The tests enforce them; a change that needs to break one needs a discussion first.

1. **Only tool output and validated prunes change.** The system prompt, tool definitions, user text, thinking and reasoning items, ids and message order are never edited. `validate.validate()` rejects any other difference. The one structural edit is an old-turn prune: it replaces `messages[1:boundary]` with a summary and keeps the first message and everything after the boundary unchanged, which `prune.validate()` checks.
2. **Rewrites are frozen.** A rewrite is stored (by tool call id, or per session for a prune) and replayed byte-identically on every later request. The same input always produces the same bytes.
3. **Unseen content is the only free edit.** Tool output after the model's last turn can be rewritten at once. Anything the model has already seen is a history edit.
4. **History edits are gated.** An epoch or prune runs only when the cache economics pay for it, when the adapter is verified safe for history edits (`epochs_supported`, `supports_prune`), and when the reasoning-prefix check allows it. Prunes happen only at a new user prompt.
5. **Failures drop new rewrites only.** Stored rewrites keep being replayed. The raw original goes out only as the last step of the retry ladder.

---

## Adding an agent

Most agents need only a profile. A new API format also needs an adapter.

- **Profile** (`lightfoot/profiles.py`): map the agent's tool names to the kinds `read`, `search`, `shell`, `edit`, `subagent`, `graph_query` and `other`. Take the names from the agent's source and note the commit you read in the docstring.
- **Adapter** (`lightfoot/adapters/`): an `Adapter` subclass that builds the neutral `Index` (units, tool calls, and tool outputs with the JSON path of their content), extracts and re-wraps output content, and returns a usage parser. Start with `epochs_supported = False`, `supports_prune = False` and `experimental = True`.
- **Tests:** build requests from the agent's real shapes; its own request snapshots or fixtures are ideal. Cover ingest, frozen replay across turns, untouched reasoning items and usage parsing. `tests/test_responses.py` is the Codex example.
- **Run it for real:** add the agent to `scripts/agent_check.py`, which starts it through Lightfoot against a local mock API at no cost. Passing that check is what moves an agent from `wired` to `0 · measure` in `lightfoot/support.py` and the README.
- **Leaving experimental:** an adapter is promoted to `certified` when a paired eval on that agent passes the ship gate in `lightfoot eval report`: a pass rate no more than 2 points below the baseline, at a lower median cost. Include the eval report in the pull request.

---

## Changing a rule or threshold

State the expected effect and back it with numbers: `lightfoot estimate` before and after, and if quality could be affected, an eval report. Tightening a rule because its over-compression signal rate is high (`lightfoot stats`) is always welcome.

---

## Pull requests

Keep them focused and include tests. Every public module, class and function needs a docstring (ruff checks it; tests are exempt). CI must pass on every platform: tests, `ruff check`, `ruff format --check` and the security scans. Add a line to `CHANGELOG.md` under "Unreleased".
