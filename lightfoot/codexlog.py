"""`lightfoot estimate --agent codex`: offline replay of OpenAI Codex CLI sessions.

Codex records each session in `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, one
`{timestamp, type, payload}` per line (format checked on a real rollout, Codex 2026-02):

| type | payload |
| --- | --- |
| `session_meta` | `id`, `cwd`, `cli_version`, `model_provider` |
| `turn_context` | `model` for the turns that follow |
| `response_item` | a Responses API input item: message, reasoning, function_call(_output), … |
| `compacted` | `replacement_history`: the items Codex continues with after compaction |
| `event_msg` / `token_count` | `info.last_token_usage` of the call that just finished; repeats don't grow the total |

Over HTTP Codex sends the full history each call, so a call's request is every item before
that response's own output items. Each request runs through the real planner with the
Responses adapter and the Codex profile: the output rules only, as live Codex support does.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import pricing
from .adapters import RESPONSES
from .adapters.responses import _is_model_side
from .config import DEFAULTS
from .estimate import _ts
from .profiles import CODEX
from .store import Store
from .transform import Context, plan_request


def default_root() -> Path:
    """Where Codex CLI keeps its rollouts, `~/.codex/sessions`."""
    return Path.home() / ".codex" / "sessions"


@dataclass
class CodexCall:
    """One Codex API call read back from a rollout: its model, input items and recorded token counts."""

    ts: float
    model: str
    items: list
    counts: dict


@dataclass
class CodexTotals:
    """What a Codex replay adds up to: recorded and estimated cost, saving and rule firings."""

    files: int = 0
    calls: int = 0
    baseline: float = 0.0
    estimate: float = 0.0
    saved_tokens: int = 0
    rules: dict = field(default_factory=dict)
    models: set = field(default_factory=set)


def calls_in(path: Path):
    """Every API call of one rollout, with the input items it was sent with."""
    items: list = []
    response_from = 0  # where the current response's own output items begin
    model, session = "", ""
    total = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind, payload = e.get("type"), e.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                session = str(payload.get("id") or "")
            elif kind == "turn_context":
                model = str(payload.get("model") or model)
            elif kind == "compacted" and isinstance(payload.get("replacement_history"), list):
                items = list(payload["replacement_history"])
                response_from = len(items)
            elif kind == "response_item":
                if _is_model_side(payload) and (not items or not _is_model_side(items[-1])):
                    response_from = len(items)
                items.append(payload)
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage, totals = info.get("last_token_usage"), info.get("total_token_usage") or {}
                grown = int(totals.get("total_tokens") or 0)
                if not isinstance(usage, dict) or grown <= total:
                    continue  # repeated event, not a new call
                total = grown
                cached = int(usage.get("cached_input_tokens") or 0)
                written = int(usage.get("cache_write_input_tokens") or 0)
                counts = {
                    "input": max(0, int(usage.get("input_tokens") or 0) - cached - written),
                    "cw5": written,
                    "cw1h": 0,
                    "cr": cached,
                    "output": int(usage.get("output_tokens") or 0),
                }
                request = items[:response_from] if response_from else list(items)
                yield CodexCall(_ts(e.get("timestamp")), model, request, {**counts, "session": session})


def _counterfactual(c: dict, replay_saved: int, ingest_saved: int) -> dict:
    """OpenAI caches automatically: replayed rewrites shrink cache reads, new ones uncached input."""
    out = dict(c)
    for key, amount in (("cr", replay_saved), ("input", ingest_saved)):
        for k in (key, "input", "cw5"):
            taken = min(out[k], max(0, amount))
            out[k] -= taken
            amount -= taken
    return out


def estimate(files: list[Path], config: dict | None = None) -> CodexTotals:
    """Replay Codex rollouts through the planner with the output rules, in a throwaway store."""
    cfg = copy.deepcopy(config or DEFAULTS)
    cfg["rewrite_budget_ms"] = 10_000  # offline: timing budgets are not the question here, as in estimate.py
    cfg["epochs"]["budget_ms"] = 10_000
    totals = CodexTotals(files=len(files))
    workdir = Path(tempfile.mkdtemp(prefix="lightfoot-codex-"))
    store = Store(workdir / "codex.db")
    try:
        for path in files:
            try:
                calls = list(calls_in(path))
            except OSError:
                continue
            for call in calls:
                body = {
                    "model": call.model,
                    "input": call.items,
                    "store": False,
                    "prompt_cache_key": call.counts["session"],
                }
                session = store.get_or_create_session(RESPONSES.session_key(body), None, "live", call.ts)
                ctx = Context(
                    cfg=cfg,
                    store=store,
                    mode="live",
                    session=session,
                    spill_dir=workdir / "spill",
                    graphify_report=None,
                    enforced=False,
                    started=time.perf_counter(),
                    now=call.ts,
                    adapter=RESPONSES,
                    profile=CODEX,
                )
                plan = plan_request(body, ctx)
                store.put_rewrites(plan.new_rewrites)
                ingest = sum(plan.new_saved.get(r.tool_use_id, 0) for r in plan.new_rewrites)
                base = {k: call.counts[k] for k in ("input", "cw5", "cw1h", "cr", "output")}
                cf = _counterfactual(base, plan.replay_saved, ingest) if plan.body_out is not None else base
                totals.calls += 1
                totals.models.add(call.model)
                totals.baseline += pricing.effective_cost(
                    call.model, *(base[k] for k in ("input", "cw5", "cw1h", "cr", "output"))
                )
                totals.estimate += pricing.effective_cost(
                    call.model, *(cf[k] for k in ("input", "cw5", "cw1h", "cr", "output"))
                )
                totals.saved_tokens += plan.saved_tokens
                for rule, n in plan.rules.items():
                    if any(r.rule == rule for r in plan.new_rewrites):
                        totals.rules[rule] = totals.rules.get(rule, 0) + n
    finally:
        store.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return totals


def render(t: CodexTotals) -> str:
    """The `lightfoot estimate --agent codex` report."""
    if not t.calls:
        return f"Codex: no API calls found in {t.files} rollout files."
    saving = 100 * (t.baseline - t.estimate) / t.baseline if t.baseline else 0.0
    unpriced = sorted(m for m in t.models if not pricing.priced(m))
    lines = [
        f"Codex CLI · {t.files} rollouts · {t.calls} API calls · models: {', '.join(sorted(t.models)) or '?'}",
        f"Recorded cost ${t.baseline:,.2f} · with lightfoot's output rules ${t.estimate:,.2f} · saving {saving:.1f}%",
        "Rule firings: " + (", ".join(f"{k} ×{v}" for k, v in sorted(t.rules.items())) or "none"),
        "Codex support runs the output rules only, until a live test shows that editing earlier output keeps "
        "OpenAI's reasoning items valid; pruning isn't estimated here.",
    ]
    if unpriced:
        lines.append(f"No price for {', '.join(unpriced)}: those calls count as $0 here.")
    return "\n".join(lines)
