"""How far each agent's support is proven: one table, shown by `lightfoot doctor` and the README.

Levels, from the integration plan; each needs the evidence named, not just code:

| Level | lightfoot does | Evidence required |
| --- | --- | --- |
| wired | nothing proven | profile and adapter built from the agent's source; unit tests |
| 0 · measure | passthrough, ledger, estimate | the adapter parsed the real agent's traffic |
| 1 · output rules | ingest rules, frozen replay | shadow mode on real sessions showed the saving |
| 2 · history edits | epochs, pruning | a captured-traffic test kept the provider's reasoning state valid |
| 3 · model summaries | server-side compaction | a live probe kept the retained reasoning valid; API keys only |
| certified | live by default | a paired eval passing `lightfoot eval report`'s ship gate (see below) |

The ship gate: a pass rate at most 2 points below the baseline, at a lower median cost.

Below "certified", every agent starts in shadow mode. Experimental agents go live only with
`--live --experimental`; Claude Code with `lightfoot shadow off`.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("wired", "0 · measure", "1 · output rules", "2 · history edits", "3 · model summaries", "certified")


@dataclass(frozen=True)
class Support:
    """One agent's row: its protocol, support level, the evidence for it, and the next step."""

    agent: str
    protocol: str
    level: str
    evidence: str
    next_step: str


SUPPORT = (
    Support(
        "Claude Code",
        "Anthropic Messages",
        "3 · model summaries",
        "run end to end; pruning and the thinking check verified on the real API; compaction probed live",
        "the paired benchmark (bench/)",
    ),
    Support(
        "Apps on the Claude Agent SDK",
        "Anthropic Messages",
        "2 · history edits",
        "the same adapter as Claude Code",
        "run one SDK app end to end",
    ),
    Support(
        "Gemini CLI",
        "Gemini",
        "0 · measure",
        "Gemini CLI 0.65.0 run through lightfoot against a local mock API (scripts/agent_check.py)",
        "shadow sessions with a real key; a thought-signature test for history edits",
    ),
    Support(
        "opencode",
        "Anthropic Messages",
        "0 · measure",
        "opencode 1.19.4 run through lightfoot against a local mock API (scripts/agent_check.py)",
        "shadow sessions with a real key",
    ),
    Support("Cline", "Anthropic Messages", "wired", "profile from source at 40c5d99031fe", "run it end to end"),
    Support("Roo Code", "Anthropic Messages", "wired", "profile from source; project archived", "run it end to end"),
    Support(
        "OpenAI Codex CLI",
        "OpenAI Responses",
        "0 · measure",
        "Codex CLI 0.164.0 run through lightfoot against a local mock API (scripts/agent_check.py)",
        "shadow sessions with a real key; a reasoning-item test for history edits",
    ),
    Support(
        "Aider",
        "OpenAI Chat Completions",
        "0 · measure",
        "Aider 0.86.2 run through lightfoot against a local mock API (scripts/agent_check.py); no tool calls",
        "shadow sessions with a real key",
    ),
    Support("Other tools", "any of the above", "wired", "routing by base-URL variables", "per tool"),
)


def report() -> list[str]:
    """Lines for `lightfoot doctor`."""
    width = max(len(s.agent) for s in SUPPORT)
    lines = ["support levels (wired > 0 measure > 1 output rules > 2 history edits > 3 model summaries > certified):"]
    lines.extend(f"    {s.agent:<{width}}  {s.level:<19}  next: {s.next_step}" for s in SUPPORT)
    return lines
