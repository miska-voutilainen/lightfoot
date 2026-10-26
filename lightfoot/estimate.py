"""Offline savings estimate from existing Claude Code transcripts (~/.claude/projects).

Every API call Claude Code made is in its transcript with the real usage (uncached
input, cache writes by TTL, cache reads, output). For each call this rebuilds the
request the model saw, runs it through the real planner (frozen replay, ingest rules,
epochs with the real timestamps), and re-prices the call with the rewritten prompt.

It is an estimate. Token counts of rewritten text are approximated (~4 chars/token),
output is assumed unchanged (no quality effect is modelled), the system prompt and
tools are not in transcripts (their cost is unchanged either way), and Claude Code's
own later behaviour is assumed identical.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import pricing, prune
from .adapters import ANTHROPIC
from .config import DEFAULTS
from .profiles import CLAUDE_CODE
from .store import Store
from .text import distinctive_identifiers
from .transform import Context, plan_request
from .validate import INGEST_RULES

# Output of one model-written summary, thinking included: an assumption, since transcripts can't
# show it. Measured on short Haiku 5.5 sessions: 650-900 tokens; long sessions write more.
MODEL_SUMMARY_OUTPUT_TOKENS = 4000


def default_root() -> Path:
    """Where Claude Code keeps its transcripts, `~/.claude/projects`."""
    return Path.home() / ".claude" / "projects"


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


@dataclass
class Call:
    """One API call read back from a transcript: its recorded usage and the messages it was sent."""

    msg_id: str
    model: str
    ts: float
    usage: dict
    messages: list  # the request's messages (ending with the user turn before this call)
    root: str = ""  # uuid of the conversation's first entry (one conversation per root)


def calls_in(path: Path):
    """Yield every API call in one transcript with the conversation it was sent with."""
    entries: dict[str, dict] = {}
    order: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("uuid"):
                entries[e["uuid"]] = e
                order.append(e)

    seen: set[str] = set()
    for e in order:
        message = e.get("message") if isinstance(e.get("message"), dict) else None
        if e.get("type") != "assistant" or not message or not message.get("id") or message["id"] in seen:
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict) or message.get("model") in (None, "<synthetic>"):
            continue
        seen.add(message["id"])
        chain = []
        node = entries.get(e.get("parentUuid"))
        guard = 0
        while node is not None and guard < 100_000:
            chain.append(node)
            node = entries.get(node.get("parentUuid"))
            guard += 1
        chain.reverse()
        root = chain[0].get("uuid", "") if chain else e.get("uuid", "")
        msgs: list[dict] = []
        last_assistant_id = None
        for c in chain:
            m = c.get("message") if isinstance(c.get("message"), dict) else None
            if c.get("type") not in ("user", "assistant") or not m:
                continue
            role = m.get("role") or c["type"]
            blocks = _blocks(m.get("content"))
            if not blocks:
                continue
            if role == "assistant":
                mid = m.get("id")
                if msgs and msgs[-1]["role"] == "assistant" and (mid == last_assistant_id or mid is None):
                    msgs[-1]["content"].extend(copy.deepcopy(blocks))
                else:
                    msgs.append({"role": "assistant", "content": copy.deepcopy(blocks)})
                last_assistant_id = mid
            else:
                if msgs and msgs[-1]["role"] == "user":
                    msgs[-1]["content"].extend(copy.deepcopy(blocks))
                else:
                    msgs.append({"role": "user", "content": copy.deepcopy(blocks)})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        if msgs and msgs[-1]["role"] == "user":
            yield Call(message["id"], message.get("model") or "", _ts(e.get("timestamp")), usage, msgs, root)


def _usage_counts(u: dict) -> dict:
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    cw1h = int((cc or {}).get("ephemeral_1h_input_tokens") or 0)
    cw5 = (
        int((cc or {}).get("ephemeral_5m_input_tokens") or 0) if cc else int(u.get("cache_creation_input_tokens") or 0)
    )
    return {
        "input": int(u.get("input_tokens") or 0),
        "cw5": cw5,
        "cw1h": cw1h,
        "cr": int(u.get("cache_read_input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
    }


def counterfactual(c: dict, plan, replay_saved: int, ingest_saved: int, floor: int = 0) -> dict:
    """Usage the same call would have had with the rewritten prompt."""
    out = dict(c)
    written = c["cw5"] + c["cw1h"]
    long_share = c["cw1h"] / written if written else 1.0
    if plan is not None and plan.new_prune is not None:
        # A new prune: the floor (system prompt, tools, first message) stays cached; the
        # rest of the smaller prompt is written to the cache once.
        prompt = c["input"] + written + c["cr"]
        new_prompt = max(c["input"], prompt - plan.saved_tokens)
        out["cr"] = min(c["cr"], floor)
        cw = max(0, new_prompt - c["input"] - out["cr"])
        out["cw1h"] = round(cw * long_share)
        out["cw5"] = cw - out["cw1h"]
        return out

    def take(key: str, amount: int) -> int:
        removed = min(out[key], max(0, amount))
        out[key] -= removed
        return amount - removed

    cw = written
    # Rewritten blocks the model saw before sit in the cached prefix: fewer cache reads.
    rest = take("cr", replay_saved)
    cw -= min(cw, rest)
    # Newly compressed tool output is part of what this call writes to the cache.
    cw = max(0, cw - ingest_saved)
    epoch = plan.epoch if plan and plan.new_rewrites else None
    if epoch:
        epoch_saved = max(0, plan.saved_tokens - replay_saved - ingest_saved)
        if epoch["cold"]:
            cw = max(0, cw - epoch_saved)
        else:
            # Warm epoch: everything after the first edit is re-written instead of read.
            moved = min(out["cr"], int(epoch["s_old"] + epoch["tail"]))
            out["cr"] -= moved
            cw += max(0, moved - epoch_saved)
    out["cw1h"] = round(cw * long_share)
    out["cw5"] = cw - out["cw1h"]
    return out


@dataclass
class Totals:
    """What one replay scenario adds up to: actual and estimated cost, rule firings, prunes and summary coverage."""

    calls: int = 0
    baseline: float = 0.0
    estimate: float = 0.0
    parts: dict = field(default_factory=lambda: defaultdict(float))
    rules: dict = field(default_factory=lambda: defaultdict(int))
    epochs: int = 0
    prunes: int = 0
    saved_tokens: int = 0
    skipped_notes: dict = field(default_factory=lambda: defaultdict(int))
    summary_refs: int = 0  # later references to content that a simulated prune removed
    summary_misses: int = 0  # ... that neither the summary nor the kept turns contained
    miss_classes: dict = field(default_factory=lambda: defaultdict(int))  # "path in tool results" -> misses
    model_summary_cost: float = 0.0  # what prune.summarizer = "model" would add: one compaction call per prune


_FILE_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _strings(value) -> list[str]:
    """String leaves of a tool input: its values, never its keys."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _plain(message: dict) -> str:
    """What a message says, as the model reads it: text, tool-input values and tool-result text.

    Not the JSON encoding of tool inputs, whose keys and escape sequences (`\\n`, doubled
    backslashes) would otherwise count as identifiers.
    """
    parts = []
    for b in _blocks(message.get("content")):
        kind = b.get("type")
        if kind == "text":
            parts.append(str(b.get("text", "")))
        elif kind == "tool_use":
            parts += _strings(b.get("input"))
        elif kind == "tool_result":
            parts.append(prune._result_text(b))
    return "\n".join(parts)


def _sources(messages: list) -> dict[str, str]:
    """Pruned text by where it came from, checked in this order: what the user wrote, what the
    assistant wrote, each tool-input field (`Write.content`), then tool results."""
    text: dict[str, list[str]] = {"user text": [], "assistant text": []}
    fields: dict[str, list[str]] = {}
    results: list[str] = []
    for m in messages:
        for b in _blocks(m.get("content")):
            kind = b.get("type")
            if kind == "text":
                text["user text" if m.get("role") == "user" else "assistant text"].append(str(b.get("text", "")))
            elif kind == "tool_use" and isinstance(b.get("input"), dict):
                for key, value in b["input"].items():
                    fields.setdefault(f"{b.get('name')}.{key}", []).extend(_strings(value))
            elif kind == "tool_result":
                results.append(prune._result_text(b))
    return {k: "\n".join(v) for k, v in {**text, **fields, "tool results": results}.items()}


class SummaryCoverage:
    """How often Claude later refers to pruned content that the summary did not keep.

    After a simulated prune, every later assistant turn is scanned for paths and compound
    identifiers (the same `distinctive_identifiers` the old_results rule uses). One that
    occurs in the pruned turns is a reference to pruned content; it is a miss when neither
    the summary nor the turns still visible to the model contain it. A miss is not a
    failure (Claude can read the history file or re-read the source), but a rising miss
    rate after a change to the summary means the summary got worse. Deterministic, offline.
    """

    def __init__(self) -> None:
        self.active: dict[str, dict] = {}  # session id -> state of its latest prune

    def start(self, sid: str, messages: list, boundary: int, summary: str) -> None:
        """Begin tracking a session after a simulated prune of `messages[1:boundary]`."""
        pruned = "\n".join(_plain(m) for m in messages[1:boundary])
        visible = [_plain(messages[0]), summary]
        visible += [_plain(m) for m in messages[boundary:]]
        self.active[sid] = {
            "pruned": pruned,
            "sources": _sources(messages[1:boundary]),
            "visible": visible,
            "upto": len(messages),
        }

    def check(self, sid: str, messages: list, totals: Totals) -> None:
        """Count references in the session's new turns to pruned content, and the ones the model could no longer see."""
        state = self.active.get(sid)
        if state is None or len(messages) <= state["upto"]:
            return
        pruned, visible = state["pruned"], state["visible"]
        for i in range(state["upto"], len(messages)):
            message = messages[i]
            text = _plain(message)
            if message.get("role") == "assistant":
                for needle in distinctive_identifiers(text):
                    if needle in pruned:
                        totals.summary_refs += 1
                        if not any(needle in part for part in visible):
                            totals.summary_misses += 1
                            kind = (
                                "path" if "/" in needle or "\\" in needle or _FILE_RE.search(needle) else "identifier"
                            )
                            source = next((k for k, v in state["sources"].items() if needle in v), "other")
                            totals.miss_classes[f"{kind} in {source}"] += 1
            visible.append(text)
        state["upto"] = len(messages)


def _price_parts(model: str, c: dict) -> dict:
    p, _ = pricing.lookup(model)
    prompt = c["input"] + c["cw5"] + c["cw1h"] + c["cr"]
    p_in, p_out = p.input, p.output
    if p.long_context_over and prompt > p.long_context_over:
        p_in, p_out = p.long_input or p_in, p.long_output or p_out
    return {
        "uncached input": p_in * c["input"] / 1e6,
        "cache writes": p_in * (p.write_short * c["cw5"] + p.write_long * c["cw1h"]) / 1e6,
        "cache reads": p_in * p.read * c["cr"] / 1e6,
        "output": p_out * c["output"] / 1e6,
    }


def estimate_file(
    path: Path,
    enforced: bool,
    workdir: Path,
    seen_ids: set,
    totals: Totals,
    per_session: dict,
    config: dict | None = None,
) -> None:
    """Replay one transcript through the planner, turn by turn, and price both versions with the
    recorded usage. The counterfactual keeps the real cache behaviour: a turn that rewrote
    history pays a cache write for everything after the edit.

    `config` is the user's configuration (defaults when None), so tuning a setting can be
    measured offline before it goes live.
    """
    cfg = copy.deepcopy(config if config is not None else DEFAULTS)
    cfg["rewrite_budget_ms"] = 10_000  # offline: timing budgets are not the question here
    cfg["epochs"]["budget_ms"] = 10_000
    store = Store(workdir / f"{path.stem}.db")
    sessions: dict[str, object] = {}
    seen_prompt: dict[str, int] = {}  # per session: the prompt size the previous call would have had
    coverage = SummaryCoverage()
    try:
        for call in calls_in(path):
            if call.msg_id in seen_ids:
                continue  # resumed sessions repeat earlier history in a new file
            seen_ids.add(call.msg_id)
            base = _usage_counts(call.usage)
            base_prompt = base["input"] + base["cw5"] + base["cw1h"] + base["cr"]
            ttl_1h = base["cw1h"] > 0 or base["cw5"] == 0
            system = [
                {
                    "type": "text",
                    "text": f"(system prompt of {path.stem})",
                    "cache_control": {"type": "ephemeral", **({"ttl": "1h"} if ttl_1h else {})},
                }
            ]
            body = {
                "model": call.model,
                "max_tokens": 32000,
                "system": system,
                "messages": call.messages,
                "thinking": {"type": "adaptive"},
            }
            sid = ANTHROPIC.session_key(body)
            session = sessions.get(sid)
            if session is None:
                session = store.get_or_create_session(sid, None, "live", call.ts)
                session.floor_tokens = base_prompt  # the first call: system prompt, tools, first message
                sessions[sid] = session
            # What the live proxy would know: the previous response's (rewritten) prompt size.
            session.context_tokens = seen_prompt.get(sid, 0)
            ctx = Context(
                cfg=cfg,
                store=store,
                mode="live",
                session=session,
                spill_dir=workdir / "spill",
                graphify_report=None,
                enforced=enforced,
                started=time.perf_counter(),
                now=call.ts,
                adapter=ANTHROPIC,
                profile=CLAUDE_CODE,
            )
            coverage.check(sid, call.messages, totals)
            plan = plan_request(body, ctx)
            store.put_rewrites(plan.new_rewrites)
            if plan.new_prune:
                store.put_prune(plan.new_prune)
                totals.prunes += 1
                if prune.supports_compaction(call.model):
                    # The compaction call reads the conversation from the cache and writes the summary.
                    totals.model_summary_cost += pricing.effective_cost(
                        call.model, 0, 0, 0, base_prompt, MODEL_SUMMARY_OUTPUT_TOKENS
                    )
                coverage.start(sid, call.messages, plan.new_prune.boundary, plan.new_prune.summary)
            ingest_saved = sum(
                plan.new_saved.get(r.tool_use_id, 0) for r in plan.new_rewrites if r.rule in INGEST_RULES
            )
            cf = (
                counterfactual(base, plan, plan.replay_saved, ingest_saved, session.floor_tokens)
                if plan.body_out is not None
                else base
            )
            seen_prompt[sid] = cf["input"] + cf["cw5"] + cf["cw1h"] + cf["cr"]
            if plan.epoch and plan.new_rewrites:
                session.epochs += 1
                session.last_epoch_turn = plan.turns
                totals.epochs += 1
            session.last_request = call.ts

            b_cost = pricing.effective_cost(
                call.model, base["input"], base["cw5"], base["cw1h"], base["cr"], base["output"]
            )
            e_cost = pricing.effective_cost(call.model, cf["input"], cf["cw5"], cf["cw1h"], cf["cr"], cf["output"])
            totals.calls += 1
            totals.baseline += b_cost
            totals.estimate += e_cost
            totals.saved_tokens += plan.saved_tokens
            for k, v in _price_parts(call.model, base).items():
                totals.parts[k] += v
            for rule, n in plan.rules.items():
                totals.rules[rule] += n
            for note in plan.notes:
                if note.startswith("epoch skipped"):
                    totals.skipped_notes["epochs skipped (thinking + enforced account)"] += 1
            s = per_session.setdefault(f"{path.parent.name}/{path.stem}", defaultdict(float))
            s["calls"] += 1
            s["baseline"] += b_cost
            s["estimate"] += e_cost
            s["last"] = max(s["last"], call.ts)
    finally:
        store.close()


def transcripts(root: Path | None = None, days: float = 30, project: str | None = None) -> list[Path]:
    """Transcript files changed in the last `days`, optionally only one project's."""
    root = Path(root) if root else default_root()
    files = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
    since = time.time() - days * 86400
    return [f for f in files if f.stat().st_mtime >= since and (not project or project.lower() in str(f).lower())]


def run(
    root: Path | None = None, days: float = 30, project: str | None = None, progress=None, config: dict | None = None
) -> dict:
    """Estimate every transcript twice: for accounts that allow history edits and for enforcing ones."""
    files = transcripts(root, days, project)
    results = {}
    for enforced in (False, True):
        totals, per_session, seen = Totals(), {}, set()
        workdir = Path(tempfile.mkdtemp(prefix="lightfoot-estimate-"))
        try:
            for i, f in enumerate(files, 1):
                if progress:
                    scenario = "enforced" if enforced else "allows edits"
                    progress(f"[{scenario}] {i}/{len(files)} {f.parent.name[:40]}/{f.name[:12]}")
                try:
                    estimate_file(f, enforced, workdir, seen, totals, per_session, config)
                except Exception as exc:  # one odd transcript must not stop the estimate
                    totals.skipped_notes[f"transcript skipped ({type(exc).__name__})"] += 1
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        results["enforced" if enforced else "allows_edits"] = (totals, per_session)
    results["files"] = len(files)
    return results


# --- old-turn pruning ceiling --------------------------------------------------------

SUMMARY_TOKENS = 4_000
SUMMARY_MODEL = "claude-haiku-5-5"


def _remove(c: dict, amount: int) -> dict:
    """Usage with `amount` fewer prompt tokens, taken from cache reads first, then writes."""
    out = dict(c)
    rest = max(0, amount)
    take = min(out["cr"], rest)
    out["cr"] -= take
    rest -= take
    written = out["cw5"] + out["cw1h"]
    if rest and written:
        share = out["cw1h"] / written
        written = max(0, written - rest)
        out["cw1h"] = round(written * share)
        out["cw5"] = written - out["cw1h"]
    return out


def prune_ceiling(
    root: Path | None = None,
    days: float = 30,
    project: str | None = None,
    caps: tuple[int, ...] = (100_000, 200_000, 300_000, 500_000),
    progress=None,
) -> dict:
    """What dropping old turns would save, if quality were unaffected.

    A conversation's first prompt (system prompt, tools, first message) is its fixed
    floor and is never dropped. Whenever the conversation above that floor passes `cap`,
    the oldest turns are dropped so it falls to cap/2 and a summary of SUMMARY_TOKENS
    replaces them. That call keeps the floor in the cache, re-writes the rest, and pays
    for a Haiku summary of what was dropped; every later call reads correspondingly
    less. Output is assumed unchanged.
    """
    root = Path(root) if root else default_root()
    files = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
    since = time.time() - days * 86400
    files = [f for f in files if f.stat().st_mtime >= since and (not project or project.lower() in str(f).lower())]

    conversations: dict[str, list] = defaultdict(list)
    seen: set[str] = set()
    for i, f in enumerate(files, 1):
        if progress:
            progress(f"[pruning] reading {i}/{len(files)} {f.parent.name[:40]}")
        try:
            for call in calls_in(f):
                if call.msg_id not in seen:
                    seen.add(call.msg_id)
                    conversations[f"{f}:{call.root}"].append((call.ts, call.model, _usage_counts(call.usage)))
        except Exception:  # noqa: S112 - one unreadable transcript must not stop the estimate
            continue

    baseline = 0.0
    for calls in conversations.values():
        for _, model, c in calls:
            baseline += pricing.effective_cost(model, c["input"], c["cw5"], c["cw1h"], c["cr"], c["output"])

    rows = {}
    for cap in caps:
        target = cap // 2 + SUMMARY_TOKENS
        total, prunes, affected = 0.0, 0, 0
        for calls in conversations.values():
            ordered = sorted(calls, key=lambda x: x[0])
            first = ordered[0][2]
            floor = first["input"] + first["cw5"] + first["cw1h"] + first["cr"]
            dropped, touched = 0, False
            for _, model, c in ordered:
                prompt = c["input"] + c["cw5"] + c["cw1h"] + c["cr"]
                if prompt - floor - dropped > cap:
                    removed_now = (prompt - floor - target) - dropped
                    dropped = prompt - floor - target
                    prunes += 1
                    touched = True
                    written = c["cw5"] + c["cw1h"]
                    long_share = c["cw1h"] / written if written else 1.0
                    rewrite = max(0, target - c["input"])
                    cf = {
                        "input": c["input"],
                        "cr": min(floor, c["cr"]),
                        "output": c["output"],
                        "cw1h": round(rewrite * long_share),
                    }
                    cf["cw5"] = rewrite - cf["cw1h"]
                    total += pricing.effective_cost(SUMMARY_MODEL, removed_now, 0, 0, 0, SUMMARY_TOKENS)
                else:
                    cf = _remove(c, dropped)
                total += pricing.effective_cost(model, cf["input"], cf["cw5"], cf["cw1h"], cf["cr"], cf["output"])
            affected += touched
        rows[cap] = {"cost": total, "prunes": prunes, "conversations": affected}
    return {
        "baseline": baseline,
        "rows": rows,
        "conversations": len(conversations),
        "calls": sum(len(v) for v in conversations.values()),
        "files": len(files),
    }


def render_pruning(result: dict) -> str:
    """The `estimate --pruning` table: cost and prunes at each trigger threshold."""
    base = result["baseline"]
    lines = [
        f"Old-turn pruning ceiling · {result['files']} transcripts · {result['conversations']} conversations · "
        f"{result['calls']} API calls · actual cost ${base:.2f} (API-equivalent)",
        "",
        f"{'Prune when conversation passes':<31} {'Estimated cost':>12} {'Saving':>8} {'Prunes':>7} "
        f"{'Conversations':>14}",
    ]
    for cap, r in result["rows"].items():
        saving = 100 * (base - r["cost"]) / base if base else 0.0
        lines.append(
            f"{format(cap, ','):>12} tokens above the floor ${r['cost']:>11.2f} {saving:>7.1f}% {r['prunes']:>7} "
            f"{r['conversations']:>14}"
        )
    lines += [
        "",
        "The floor is each conversation's first prompt (system prompt, tools, first message); it is never dropped.",
        "A ceiling, not a forecast: it assumes Claude works as well after old turns are dropped (no extra",
        "re-reads, same output). On Opus 5.5, dropping turns also invalidates later thinking blocks.",
    ]
    return "\n".join(lines)


def render(results: dict, top: int = 12) -> str:
    """The `lightfoot estimate` report.

    Where the cost went, both account scenarios, rule firings and the largest sessions.
    """
    a_tot, a_sessions = results["allows_edits"]
    e_tot, _ = results["enforced"]
    if not a_tot.calls:
        return "No Claude Code API calls found in the selected transcripts."

    def pct(t: Totals) -> float:
        return 100 * (t.baseline - t.estimate) / t.baseline if t.baseline else 0.0

    lines = [
        f"lightfoot estimate · {results['files']} transcripts · {a_tot.calls} API calls · "
        f"prices as of {pricing.PRICES_AS_OF} (API-equivalent USD)",
        "",
    ]
    lines.append("Where the actual cost went:")
    for k, v in sorted(a_tot.parts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k:<15} ${v:>9.2f}  {100 * v / a_tot.baseline:5.1f}%")
    lines.append(f"  {'total':<15} ${a_tot.baseline:>9.2f}")
    lines.append("")
    lines.append(f"{'Scenario':<44} {'Estimated cost':>15} {'Saving':>10}")
    lines.append(f"{'Account allows history edits':<44} ${a_tot.estimate:>14.2f} {pct(a_tot):>9.1f}%")
    lines.append(f"{'Account enforces the thinking-prefix check':<44} ${e_tot.estimate:>14.2f} {pct(e_tot):>9.1f}%")
    lines.append("")
    lines.append(
        "Rule firings (allows edits): "
        + (", ".join(f"{k} ×{v}" for k, v in sorted(a_tot.rules.items())) or "none")
        + f"; epochs ×{a_tot.epochs}, prunes ×{a_tot.prunes}"
    )
    lines.append(
        "Rule firings (enforced):     "
        + (", ".join(f"{k} ×{v}" for k, v in sorted(e_tot.rules.items())) or "none")
        + f"; epochs ×{e_tot.epochs}, prunes ×{e_tot.prunes}"
    )
    for k, v in {**a_tot.skipped_notes, **e_tot.skipped_notes}.items():
        lines.append(f"  {k}: {v}")
    if a_tot.summary_refs:
        rate = 100 * a_tot.summary_misses / a_tot.summary_refs
        lines.append(
            f"Summary coverage: {a_tot.summary_refs} later references to pruned content, "
            f"{a_tot.summary_misses} ({rate:.1f}%) in neither the summary nor the kept turns"
        )
        if a_tot.miss_classes:
            ranked = sorted(a_tot.miss_classes.items(), key=lambda kv: -kv[1])
            lines.append("  misses by kind and where they were: " + ", ".join(f"{k} {v}" for k, v in ranked))
    if a_tot.model_summary_cost:
        lines.append(
            f'Model-written summaries (prune.summarizer = "model") would add about ${a_tot.model_summary_cost:.2f}: '
            f"one compaction call per prune, reading the conversation from the cache and writing an assumed "
            f"{MODEL_SUMMARY_OUTPUT_TOKENS:,} tokens"
        )
    lines.append("")
    lines.append("Largest sessions (allows edits):")
    lines.append(f"  {'Session':<52} {'Calls':>6} {'Actual':>9} {'Estimate':>9} {'Saving':>7}")
    ranked = sorted(a_sessions.items(), key=lambda kv: -kv[1]["baseline"])[:top]
    for name, s in ranked:
        saving = 100 * (s["baseline"] - s["estimate"]) / s["baseline"] if s["baseline"] else 0
        lines.append(
            f"  {name[-52:]:<52} {int(s['calls']):>6} ${s['baseline']:>8.2f} ${s['estimate']:>8.2f} {saving:>6.1f}%"
        )
    lines.append("")
    lines.append("Estimate only: rewritten text is counted at ~4 characters per token, output is assumed unchanged,")
    lines.append("and Claude Code's own behaviour is assumed identical. Shadow mode measures the real thing.")
    return "\n".join(lines)
