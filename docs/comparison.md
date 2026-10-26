# Comparison

How Lightfoot differs from other tools that cut a coding agent's token use, what each number means, and where Lightfoot doesn't help. Most of these tools target Claude Code, so that is what the comparison covers.

---

## The tools

Researched on 2026-10-25. "Own claim" means the project's published figure, not an independent measurement.

| Tool | Approach | What it changes | Published saving |
| --- | --- | --- | --- |
| [Tamp](https://github.com/sliday/tamp) | Proxy | Newest tool output; rewrites of stale turns are opt-in and break the prompt cache | 50% (own claim) |
| [Headroom](https://github.com/chopratejas/headroom) | Proxy, library or MCP server | Newest tool output; originals are fetched back through a retrieve tool | 60–95% on JSON (own claim) |
| [RTK](https://github.com/rtk-ai/rtk) | Bash `PreToolUse` hook | Bash command output only | 60–90% (own claim); 0% in an [independent test](https://computingforgeeks.com/reduce-claude-code-token-usage-tools/) |
| context-mode | MCP server and hooks | Moves large outputs to a searchable store | Not independently measured |
| token-optimizer | Plugin hooks | Re-reads sent as deltas, file skeletons; noncommercial license | 18%, measured |
| Claude Code `/compact` | Built in | The whole conversation, replaced by a model-written summary | Not applicable: manual |
| Lightfoot | Proxy, or a `PostToolUse` hook without the proxy | Old turns (pruning) and tool output; every rewrite validated and frozen | 26.7% estimated on 139 real sessions |

---

## What Lightfoot does differently

| Property | Lightfoot | Typical alternative |
| --- | --- | --- |
| Old turns | Pruned into an extractive summary at a new prompt, full text saved to a file | Untouched, so the conversation keeps growing |
| Prompt cache | Every rewrite is stored and replayed byte for byte, so the cache keeps hitting | Old content left alone, or rewritten each turn at the cost of a cache miss |
| When history is edited | Only when the edit pays for itself at the model's cache prices, or the cache is already cold | Every turn, or never |
| Thinking signatures | Learns whether the account enforces the prefix check and adapts | Not considered |
| Changing what the model writes | Never: no brevity rules, no model routing | Some tools add terse-output rules or route to cheaper models |
| Before turning it on | Shadow mode measures the saving and changes nothing | Live from the start |
| Removed content | Saved to a file named in the note; the agent reads it with its usual file tool | Dropped, moved to a separate store, or fetched back through an extra tool whose definition costs tokens every turn |

---

## What the numbers mean

Lightfoot's 26.7% is an offline replay (`lightfoot estimate`, run on 2026-10-25) of the maintainer's own 139 Claude Code sessions through the current rules, priced with each request's recorded usage. An earlier replay of a fixed 30-day set came out at 29%; the detailed measurements in [architecture.md](architecture.md) use that set. Neither is a benchmark:

- It's one person's sessions. Run `lightfoot estimate` to get the figure for yours; it's offline and costs nothing.
- It measures cost, not quality. The quality evidence so far is the summary miss rate (11.3% of later references to pruned content aren't in the summary, and the agent can still read the history file) and the paired eval you can run on your own tasks with `lightfoot eval`.
- A paired benchmark on pinned open-source tasks is ready ([bench/](../bench/README.md)), but hasn't been run yet: running it costs model usage.

Almost all of the saving comes from pruning old turns. Tool-output rules alone save little on Claude Code, because it already trims large outputs itself.

---

## Where Lightfoot doesn't help

| Situation | Why |
| --- | --- |
| Short sessions | Pruning starts at 150k tokens after an idle gap, 200k otherwise. Below that only the tool-output rules run |
| Amazon Bedrock or Google Vertex, or no local proxy allowed | Only hook mode works there: tool-output rules for Claude Code's Bash, PowerShell and MCP output or opencode's shell output, no pruning |
| Sessions you `/compact` often | `/compact` already replaces the old turns |
| Agents other than Claude Code | Codex CLI, opencode, Gemini CLI, Aider, Cline and Roo Code are experimental and shadow-only by default; Codex and Gemini get the tool-output rules only, without pruning |
| Gemini CLI with "Sign in with Google" | That route ignores the base URL, so no request reaches Lightfoot; only API-key auth works |

---

## Using it with other tools

Lightfoot changes only tool output and old turns, so it can run beside tools that work elsewhere. Two tools rewriting the same tool output may stack or conflict; measure with shadow mode and `lightfoot stats` before relying on both.
