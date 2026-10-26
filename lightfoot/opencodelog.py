"""`lightfoot estimate --agent opencode`: offline replay of opencode sessions.

opencode 1.19 keeps its sessions in a SQLite database, `opencode.db`, in its data folder
(`$XDG_DATA_HOME/opencode`, by default `~/.local/share/opencode` on every OS). The format
was read from a real opencode 1.19.4 database and opencode's source (`session/usage.ts`):

| Table | Holds |
| --- | --- |
| `message` | one row per message; `data` is JSON: `role`, and for the assistant `modelID`, `providerID` |
| `part` | the parts of a message, in id order; `data` is JSON with a `type` |

| Part type | Content |
| --- | --- |
| `text` | `text` |
| `tool` | `tool` (its name), `callID`, `state`: `status`, `input`, `output` or `error` |
| `step-start`, `step-finish` | one model call; `step-finish` carries that call's `tokens` |
| `reasoning`, `file`, `patch`, … | not replayed |

`tokens` are `input` (uncached), `output`, `reasoning` (billed as output) and `cache.read` /
`cache.write`. Each `step-finish` is one API call, sent with every turn before its step.

The database also holds credentials (`account`, `credential`); this reader opens it read-only
and queries only `message` and `part`. Requests are rebuilt as Anthropic Messages, the format
Lightfoot's opencode support speaks, and run through the real planner with the opencode profile:
the output rules only, as for the other agents' estimates.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import pricing
from .adapters import ANTHROPIC
from .config import DEFAULTS
from .estimate import counterfactual
from .profiles import OPENCODE
from .store import Store
from .transform import Context, plan_request


def default_db() -> Path:
    """opencode's database: `$XDG_DATA_HOME/opencode/opencode.db`, else under `~/.local/share`."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "opencode" / "opencode.db"


@dataclass
class OpencodeCall:
    """One opencode API call read back from its database: model, request messages and token counts."""

    ts: float
    model: str
    session: str
    messages: list
    counts: dict


@dataclass
class OpencodeTotals:
    """What an opencode replay adds up to: recorded and estimated cost, saving and rule firings."""

    sessions: int = 0
    calls: int = 0
    baseline: float = 0.0
    estimate: float = 0.0
    saved_tokens: int = 0
    rules: dict = field(default_factory=dict)
    models: set = field(default_factory=set)


def _rows(db: Path, since: float) -> dict[str, list[tuple[dict, list[dict]]]]:
    """Messages with their parts, per session, oldest first, for sessions active since `since` (seconds)."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        sessions: dict[str, list] = {}
        parts: dict[str, list[dict]] = {}
        for message_id, data in con.execute("SELECT message_id, data FROM part ORDER BY message_id, id"):
            try:
                parts.setdefault(message_id, []).append(json.loads(data))
            except ValueError:
                continue
        query = "SELECT id, session_id, time_created, data FROM message ORDER BY session_id, time_created, id"
        for message_id, session_id, created, data in con.execute(query):
            try:
                info = json.loads(data)
            except ValueError:
                continue
            if isinstance(info, dict):
                info["_ts"] = (created or 0) / 1000
                sessions.setdefault(session_id, []).append((info, parts.get(message_id, [])))
    finally:
        con.close()
    return {s: m for s, m in sessions.items() if m and max(i["_ts"] for i, _ in m) >= since}


def _tool_turns(tools: list[dict]) -> tuple[list[dict], list[dict]]:
    """The tool_use blocks of one step and the tool_result blocks answering them."""
    uses, results = [], []
    for p in tools:
        state = p.get("state") if isinstance(p.get("state"), dict) else {}
        call_id = p.get("callID")
        if not isinstance(call_id, str) or state.get("status") not in ("completed", "error"):
            continue
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        uses.append({"type": "tool_use", "id": call_id, "name": str(p.get("tool") or ""), "input": inp})
        error = state.get("status") == "error"
        output = state.get("error") if error else state.get("output")
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": str(output or ""),
                **({"is_error": True} if error else {}),
            }
        )
    return uses, results


def calls_in(session: str, messages: list[tuple[dict, list[dict]]]):
    """Every API call of one session, with the messages it was sent with."""
    history: list = []
    for info, parts in messages:
        if info.get("role") == "user":
            text = [{"type": "text", "text": p["text"]} for p in parts if p.get("type") == "text" and p.get("text")]
            if text:
                history.append({"role": "user", "content": text})
            continue
        if info.get("role") != "assistant":
            continue
        model = str(info.get("modelID") or "")
        texts, tools = [], []

        def close_step(texts=texts, tools=tools):
            uses, results = _tool_turns(tools)
            blocks = [{"type": "text", "text": t} for t in texts] + uses
            if blocks:
                history.append({"role": "assistant", "content": blocks})
            if results:
                history.append({"role": "user", "content": results})
            texts.clear()
            tools.clear()

        for p in parts:
            kind = p.get("type")
            if kind == "step-start":
                close_step()
            elif kind == "text" and p.get("text"):
                texts.append(str(p["text"]))
            elif kind == "tool":
                tools.append(p)
            elif kind == "step-finish" and isinstance(p.get("tokens"), dict):
                t = p["tokens"]
                cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
                counts = {
                    "input": int(t.get("input") or 0),
                    "cw5": int(cache.get("write") or 0),
                    "cw1h": 0,
                    "cr": int(cache.get("read") or 0),
                    "output": int(t.get("output") or 0) + int(t.get("reasoning") or 0),
                }
                yield OpencodeCall(info["_ts"], model, session, list(history), counts)
                close_step()
        close_step()


def estimate(db: Path, since: float = 0.0, config: dict | None = None) -> OpencodeTotals:
    """Replay opencode sessions through the planner with the output rules, in a throwaway store."""
    cfg = copy.deepcopy(config or DEFAULTS)
    cfg["prune"]["enabled"] = False  # the other agents' estimates measure the output rules only
    cfg["epochs"]["enabled"] = False
    cfg["rewrite_budget_ms"] = 10_000  # offline: timing budgets are not the question here, as in estimate.py
    cfg["epochs"]["budget_ms"] = 10_000
    totals = OpencodeTotals()
    if not db.is_file():
        return totals
    workdir = Path(tempfile.mkdtemp(prefix="lightfoot-opencode-"))
    store = Store(workdir / "opencode.db")
    try:
        sessions = _rows(db, since)
        totals.sessions = len(sessions)
        for session_id, messages in sessions.items():
            for call in calls_in(session_id, messages):
                if not call.messages:
                    continue
                body = {"model": call.model, "system": f"session {session_id}", "messages": call.messages}
                session = store.get_or_create_session(ANTHROPIC.session_key(body), None, "live", call.ts)
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
                    adapter=ANTHROPIC,
                    profile=OPENCODE,
                )
                plan = plan_request(body, ctx)
                store.put_rewrites(plan.new_rewrites)
                ingest = sum(plan.new_saved.get(r.tool_use_id, 0) for r in plan.new_rewrites)
                base = call.counts
                cf = counterfactual(base, plan, plan.replay_saved, ingest) if plan.body_out is not None else base
                totals.calls += 1
                totals.models.add(call.model)
                keys = ("input", "cw5", "cw1h", "cr", "output")
                totals.baseline += pricing.effective_cost(call.model, *(base[k] for k in keys))
                totals.estimate += pricing.effective_cost(call.model, *(cf[k] for k in keys))
                totals.saved_tokens += plan.saved_tokens
                for rule, n in plan.rules.items():
                    if any(r.rule == rule for r in plan.new_rewrites):
                        totals.rules[rule] = totals.rules.get(rule, 0) + n
    finally:
        store.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return totals


def render(t: OpencodeTotals) -> str:
    """The `lightfoot estimate --agent opencode` report."""
    if not t.calls:
        return f"opencode: no API calls found in {t.sessions} sessions."
    saving = 100 * (t.baseline - t.estimate) / t.baseline if t.baseline else 0.0
    unpriced = sorted(m for m in t.models if not pricing.priced(m))
    lines = [
        f"opencode · {t.sessions} sessions · {t.calls} API calls · models: {', '.join(sorted(t.models)) or '?'}",
        f"Recorded cost ${t.baseline:,.2f} · with Lightfoot's output rules ${t.estimate:,.2f} · saving {saving:.1f}%",
        "Rule firings: " + (", ".join(f"{k} ×{v}" for k, v in sorted(t.rules.items())) or "none"),
        "This counts the output rules only; old-turn pruning isn't estimated for opencode yet.",
    ]
    if unpriced:
        lines.append(f"No price for {', '.join(unpriced)}: those calls count as $0 here.")
    return "\n".join(lines)
