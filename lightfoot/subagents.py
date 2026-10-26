"""`lightfoot estimate --subagents`: what each kind of Claude Code subagent costs.

Claude Code keeps each subagent's transcript in `<session>/subagents/agent-<id>.jsonl`, next to
an `agent-<id>.meta.json` naming its `agentType`. Per type this reports runs, calls and cost at
API prices, and how much of that went into the subagent's first request: a fresh subagent
writes its own system prompt and tools to the cache, unless a sibling already did.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .estimate import _price_parts, _usage_counts


@dataclass
class TypeCost:
    """What one subagent type cost: runs, calls, and how much went into each run's first request."""

    runs: int = 0
    calls: int = 0
    cost: float = 0.0
    first_cost: float = 0.0  # the first request of each run
    first_prompt: int = 0  # tokens in those first prompts
    first_read: int = 0  # of which read from a cache another request wrote


def _calls(path: Path) -> list[tuple[str, dict]]:
    """(model, usage counts) of each API call in a transcript, oldest first, deduplicated."""
    out, seen = [], set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            message = e.get("message") if isinstance(e, dict) else None
            if not isinstance(message, dict) or e.get("type") != "assistant":
                continue
            mid, usage = message.get("id"), message.get("usage")
            if not mid or mid in seen or not isinstance(usage, dict) or message.get("model") in (None, "<synthetic>"):
                continue
            seen.add(mid)
            out.append((message["model"], _usage_counts(usage)))
    return out


def measure(files: list[Path]) -> tuple[dict[str, TypeCost], float]:
    """Cost per subagent type, and the cost of everything else (main conversations)."""
    types: dict[str, TypeCost] = defaultdict(TypeCost)
    main = 0.0
    for path in files:
        try:
            calls = _calls(path)
        except OSError:
            continue
        costs = [sum(_price_parts(model, c).values()) for model, c in calls]
        if path.parent.name != "subagents":
            main += sum(costs)
            continue
        try:
            meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            kind = str(meta.get("agentType") or "unknown")
        except (OSError, ValueError, AttributeError):
            kind = "unknown"
        t = types[kind]
        t.runs += 1
        t.calls += len(calls)
        t.cost += sum(costs)
        if calls:
            c = calls[0][1]
            t.first_cost += costs[0]
            t.first_prompt += c["input"] + c["cw5"] + c["cw1h"] + c["cr"]
            t.first_read += c["cr"]
    return dict(types), main


def render(types: dict[str, TypeCost], main: float) -> str:
    """The `lightfoot estimate --subagents` report, against the main sessions' spend `main`."""
    total = sum(t.cost for t in types.values())
    if not types:
        return "Subagents: none found in these transcripts."
    lines = [
        f"Subagents: ${total:,.2f} at API prices, {100 * total / max(1e-9, total + main):.1f}% of all spend",
        "",
        f"{'Type':<22} {'Runs':>5} {'Calls':>6} {'Cost':>9} {'Per run':>8} {'1st request':>12} {'1st read':>9}",
    ]
    for kind, t in sorted(types.items(), key=lambda kv: kv[1].cost, reverse=True):
        lines.append(
            f"{kind[:22]:<22} {t.runs:>5} {t.calls:>6} ${t.cost:>8,.2f} ${t.cost / t.runs:>7,.2f} "
            f"{100 * t.first_cost / max(1e-9, t.cost):>11.0f}% {100 * t.first_read / max(1, t.first_prompt):>8.0f}%"
        )
    lines += [
        "",
        "1st request: share of the type's cost spent on each run's first request, which writes its prompt to the "
        "cache. 1st read: how much of that first prompt was already cached by an earlier request.",
    ]
    return "\n".join(lines)
