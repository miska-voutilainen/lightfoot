"""Request rewriting, protocol-neutral.

Only tool output content is ever changed. Every rewrite is stored under its tool call
id and replayed byte-identically on later requests (the frozen transcript), which keeps
both the prompt cache and reasoning signatures valid.

Two moments create rewrites:
  * ingest: tool outputs the model has not seen yet (rules json, json_trim, spill and noise), and
  * epoch: older outputs in one batch (rules images, stale_reads, searches, old_results), only
    when the cache economics say it pays, the provider is verified to tolerate history
    edits, and the reasoning-prefix check allows it.

The adapter (adapters/) knows the wire format; the profile (profiles.py) knows the
agent's tools. Everything here works on their neutral Index.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import guards, images, pricing, providers, prune
from .adapters import ANTHROPIC, Adapter, Index, Result, apply_changes
from .adapters.base import canonical
from .ingest import excluded, graph_note, ingest, json_line, spill_path, write_spill
from .profiles import CLAUDE_CODE, Profile
from .providers import Provider
from .store import Prune, Rewrite, Session, Store
from .text import (
    distinctive_identifiers,
    grep_line_path,
    norm_path,
    normalize_terminal,
    render_selection,
    same_file,
    select_lines,
    tokens,
)
from .validate import ValidationError, validate

EPOCH_RULES = {"images", "stale_reads", "searches", "old_results"}


@dataclass
class Context:
    """Per-request inputs to the planner: settings, the session's stored state and the launch's integrations."""

    cfg: dict
    store: Store
    mode: str  # live | shadow | freeze | passthrough
    session: Session | None
    spill_dir: Path
    graphify_report: str | None  # absolute path when Graphify was detected at launch
    enforced: bool
    started: float  # perf_counter() when the request arrived
    now: float = field(default_factory=time.time)
    adapter: Adapter = ANTHROPIC
    profile: Profile = CLAUDE_CODE
    consumer_auth: bool = False  # a Pro/Max login: lightfoot never sends its own requests with it
    provider: Provider = providers.ANTHROPIC


@dataclass
class Plan:
    """What the planner decided for one request, and everything needed to undo it.

    The server tries `body_out`, then `replay_body`, then the original. New rewrites and a
    new prune are stored before forwarding; `replaced_*` hold what they overwrote, so a 400
    can restore the previous state exactly.
    """

    session_id: str
    turns: int
    model: str | None
    body_out: dict | None = None  # full rewrite (stored + new); None if nothing changed
    replay_body: dict | None = None  # stored rewrites only, used as the first fallback
    replay_differs: bool = False
    replay_saved: int = 0
    new_rewrites: list[Rewrite] = field(default_factory=list)
    replaced_rewrites: list[Rewrite] = field(default_factory=list)
    rules: dict = field(default_factory=dict)
    saved_tokens: int = 0
    epoch: dict | None = None
    drop_block: bool = False
    signals: list[tuple[str, str, str, str]] = field(default_factory=list)  # (call id, rule, kind, path)
    new_saved: dict[str, int] = field(default_factory=dict)  # tokens saved per new rewrite, by tool call id
    new_prune: Prune | None = None  # created by this request (stored before forwarding)
    replaced_prune: Prune | None = None  # the record it replaced, for rollback
    prune_info: dict | None = None  # economics of a new prune
    compaction: dict | None = None  # a model summary to request in the background: prune, request
    compact_beta: bool = False  # the forwarded body carries a compaction block
    notes: list[str] = field(default_factory=list)
    tools_used: set[str] = field(default_factory=set)  # tool names called anywhere in the request
    guard_state: dict | None = None  # the session's quality-guard state, loaded for live requests
    guard_signals: list[tuple[str, str]] = field(default_factory=list)  # (signal kind, tool) for the guards
    guard_pre_rate: float | None = None  # reads and searches per turn before a new prune


def session_key(body: dict, adapter: Adapter = ANTHROPIC) -> str:
    """The session a request belongs to, as its adapter identifies it."""
    return adapter.session_key(body)


# --- epoch rules (3, 4, 5) ---------------------------------------------------------


def epoch_candidates(idx: Index, cfg: dict, ctx: Context) -> dict[int, tuple[str, str, str | None]]:
    """Map of result position -> (new_text, rule, path) for older outputs worth compacting."""
    rules, th = cfg["rules"], cfg["thresholds"]
    adapter, profile = ctx.adapter, ctx.profile
    last_eligible_turn = idx.turns - th["protected_turns"]
    results = idx.results

    def eligible(r: Result) -> bool:
        return (
            r.rewritable
            and not r.media  # results with images belong to the images rule only
            and r.turn <= last_eligible_turn
            and r.rule not in EPOCH_RULES
            and r.orig_text is not None
            and r.unit >= idx.pruned_before
        )

    out: dict[int, tuple[str, str, str | None]] = {}

    # Images: every image costs its tokens again on every turn, and screenshots are mostly
    # superseded by newer ones. All but the newest `images_keep` image results become a
    # text stub; each image is saved to a file Claude can read to see it again.
    if rules.get("images"):
        with_images = [k for k, r in enumerate(results) if r.media and r.rewritable]
        older = with_images[: max(0, len(with_images) - th["images_keep"])]
        for k in older:
            r = results[k]
            if r.rule is not None or r.turn > last_eligible_turn or r.unit < idx.pruned_before:
                continue
            if excluded(cfg, "images", r):
                continue
            stub = _image_stub(r, ctx)
            if stub is not None:
                out[k] = stub

    opened: list[tuple[int, str]] = []  # (unit, path) of every read or edit
    written: set[str] = set()
    for call in idx.calls.values():
        if call.kind not in ("read", "edit"):
            continue
        path = profile.path(call.name, call.inp)
        if path:
            opened.append((call.unit, path))
            if call.kind == "edit":
                written.add(norm_path(path))

    # Rule 3: a file read is superseded by a later read covering the same lines.
    if rules.get("stale_reads"):
        reads: dict[str, list[tuple[int, Result, tuple[int, int]]]] = defaultdict(list)
        for k, r in enumerate(results):
            path = profile.path(r.name, r.inp) if r.kind == "read" else None
            if path and r.shape and not r.is_error and r.orig_text:
                span = profile.read_span(r.orig_text)
                if span:
                    reads[norm_path(path)].append((k, r, span))
        for items in reads.values():
            for pos, (k, r, (a, b)) in enumerate(items):
                if not eligible(r) or tokens(r.orig_text) < th["min_read_tokens"]:
                    continue
                newer = [x for x in items[pos + 1 :] if x[2][0] <= a and x[2][1] >= b]
                if newer:
                    latest = newer[-1][1]
                    path = profile.path(r.name, r.inp)
                    out[k] = (
                        f"[lightfoot: superseded read of {path} (lines {a}-{b}). The file was read again "
                        f"at turn {latest.turn}; that newer copy is the current one.]",
                        "stale_reads",
                        norm_path(path),
                    )

        # Graph queries on the same node: the newest result stays, older ones become stubs.
        if ctx.graphify_report:
            last_by_key: dict[str, Result] = {}
            for r in results:
                if r.kind == "graph_query":
                    last_by_key[r.name + canonical(r.inp)] = r
            for k, r in enumerate(results):
                if k in out or r.kind != "graph_query" or not eligible(r):
                    continue
                latest = last_by_key[r.name + canonical(r.inp)]
                if latest is not r and tokens(r.orig_text) >= 50:
                    out[k] = (
                        f"[lightfoot: superseded graph query ({r.name.rsplit('__', 1)[-1]}). The same query "
                        f"ran again at turn {latest.turn}; that newer result is the current one.]",
                        "stale_reads",
                        None,
                    )

    # Rule 4: identical repeated searches, and old content searches whose hits were opened since.
    if rules.get("searches"):
        for k, r in enumerate(results):
            if k in out or r.kind != "search" or not eligible(r):
                continue
            key = canonical(r.inp)
            repeat = next(
                (
                    x
                    for x in results[k + 1 :]
                    if x.name == r.name and x.orig_hash == r.orig_hash and canonical(x.inp) == key
                ),
                None,
            )
            if repeat is not None and tokens(r.orig_text) >= 50:
                out[k] = (
                    f"[lightfoot: this {r.name} call was repeated with identical arguments and an identical "
                    f"result at turn {repeat.turn}; see that copy.]",
                    "searches",
                    None,
                )
                continue
            if profile.search_content_mode(r.name, r.inp) and idx.turns - r.turn >= th["search_age"]:
                counts: dict[str, int] = {}
                for line in r.orig_text.split("\n"):
                    hit = grep_line_path(line)
                    if hit:
                        counts[hit] = counts.get(hit, 0) + 1
                if not counts:
                    continue
                if not any(unit > r.unit and same_file(p, hit) for unit, p in opened for hit in counts):
                    continue
                listing = "\n".join(f"{p} ({n} match{'es' if n != 1 else ''})" for p, n in counts.items())
                note = "[lightfoot: older search results reduced to the matched paths; those files were opened since.]"
                if ctx.graphify_report:
                    note += graph_note(ctx)
                candidate = f"{note}\n{listing}"
                if tokens(candidate) <= 0.7 * tokens(r.orig_text) and tokens(r.orig_text) - tokens(candidate) >= 100:
                    out[k] = (candidate, "searches", None)

    # Rule 5: old, large outputs that nothing later refers to.
    if rules.get("old_results"):
        current = {r.path: r.current or "" for r in results}
        texts = [
            adapter.unit_text(ui, unit, current) if isinstance(unit, dict) else "" for ui, unit in enumerate(idx.units)
        ]
        offsets, total = [], 0
        for t in texts:
            offsets.append(total)
            total += len(t) + 1
        corpus = "\n".join(texts)
        for k, r in enumerate(results):
            if k in out or not eligible(r) or excluded(cfg, "old_results", r):
                continue
            if r.rule not in (None, "noise") or idx.turns - r.turn < th["old_result_age"]:
                continue
            text_now = r.current or ""
            if tokens(text_now) < th["old_result_tokens"]:
                continue
            path = profile.path(r.name, r.inp) if r.kind == "read" else None
            if path and norm_path(path) in written:
                continue  # never shrink a read of a file edited this session
            needles = [v for v in r.inp.values() if isinstance(v, str) and 6 <= len(v) <= 300]
            needles += distinctive_identifiers(r.orig_text)
            if not needles:
                continue  # nothing distinctive to check: keep it
            start = offsets[r.unit + 1] if r.unit + 1 < len(offsets) else len(corpus)
            if any(corpus.find(n, start) != -1 for n in needles):
                continue
            norm = normalize_terminal(r.orig_text)
            lines = norm.split("\n")
            if json_line(lines):
                continue  # trimming one line would keep only its start
            keep = select_lines(lines, th["spill_head"], th["spill_tail"], th["error_context"])
            target = spill_path(ctx.spill_dir, norm)
            candidate = (
                f"{render_selection(lines, keep)}\n[lightfoot: older output shortened to {len(keep)} of "
                f"{len(lines)} lines; nothing since refers to it. Full output: {target}]"
            )
            if tokens(candidate) <= 0.75 * tokens(text_now):
                try:
                    write_spill(target, norm)
                except OSError:
                    continue  # never shorten an output whose full text isn't safely on disk
                out[k] = (candidate, "old_results", norm_path(str(target)))
    return out


def _image_stub(r: Result, ctx: Context) -> tuple[str, str, str | None] | None:
    """(stub, "images", first saved path) for an old image result, or None if an image can't be saved."""
    folder = ctx.spill_dir.parent / "images"
    saved = []
    for block in r.media:
        path = images.save(block, folder)
        if path is None:
            return None  # never remove an image that isn't safely on disk
        saved.append((images.describe(block), norm_path(str(path))))
    listing = "; ".join(f"{label}: {path}" for label, path in saved)
    count = "an older image" if len(saved) == 1 else f"{len(saved)} older images"
    note = (
        f"[lightfoot: {count} removed from this result to save context; newer images came later. "
        f"Saved copy: {listing} (read the file to view it again).]"
    )
    stub = f"{r.orig_text}\n{note}" if r.orig_text else note
    return stub, "images", saved[0][1]


# --- planning ---------------------------------------------------------------------


def _over_budget(ctx: Context, budget_ms: float | None = None) -> bool:
    limit = ctx.cfg["rewrite_budget_ms"] if budget_ms is None else budget_ms
    return (time.perf_counter() - ctx.started) * 1000 > limit


def _build(body: dict, idx: Index, adapter: Adapter, pick) -> tuple[dict | None, dict]:
    """Body with the texts chosen by `pick(result)` applied; returns (body or None, changed map)."""
    changed: dict[tuple, tuple[Result, str, str]] = {}
    for r in idx.results:
        choice = pick(r)
        if choice is not None:
            changed[r.path] = (r, choice[0], choice[1])
    if not changed:
        return None, {}
    values = {path: adapter.content_value(r.container, r.shape, text) for path, (r, text, _) in changed.items()}
    return apply_changes(body, values), changed


def _protected_from(idx: Index, protected_turns: int) -> int:
    limit = idx.turns - protected_turns
    return next((ui for ui, t in enumerate(idx.unit_turn) if t > limit), len(idx.units))


def _meta(changed: dict, source: str | None = None) -> dict:
    return {path: (rule, source or r.source, r.unit, r.orig_text, text) for path, (r, text, rule) in changed.items()}


def plan_request(body: dict, ctx: Context) -> Plan:
    """Decide what to send for one request, in a fixed order.

    1. replay stored rewrites and the stored prune (frozen, byte-identical);
    2. ingest: rewrite tool output the model has not seen yet;
    3. a new prune or epoch, only when it pays for itself (`economics`);
    4. validate the result against the original.

    Never raises on bad input; problems become notes and the plan falls back to fewer changes.
    """
    cfg, adapter, profile = ctx.cfg, ctx.adapter, ctx.profile
    idx = adapter.index(body, profile)
    live = ctx.mode in ("live", "freeze")
    plan = Plan(session_id=adapter.session_key(body), turns=idx.turns, model=adapter.model(body))
    plan.tools_used = {c.name for c in idx.calls.values() if c.name}
    if ctx.mode == "passthrough":
        return plan
    if live and cfg.get("guards", {}).get("enabled", True):
        # Quality guards change only what happens next: stored rewrites and prunes still replay.
        plan.guard_state = guards.load(ctx.store, plan.session_id)
        cfg = ctx.cfg = guards.adjust(cfg, plan.guard_state)

    # 1. Frozen replay of stored rewrites.
    stored = ctx.store.get_rewrites([r.tid for r in idx.results if r.tid], live=live)
    for r in idx.results:
        rw = stored.get(r.tid)
        if rw is None or not r.rewritable:
            continue
        if rw.orig_hash != r.orig_hash:
            plan.notes.append(f"hash mismatch for {r.tid}; left unchanged")
            continue
        r.text, r.rule, r.note_path, r.source = rw.text, rw.rule, rw.path, "stored"

    # 1b. A stored old-turn prune is replayed whenever the same history is re-sent. Replay does
    # not depend on prune.enabled: once the model has seen the pruned form, it stays.
    stored_prune = pending_prune = None
    if adapter.supports_prune:
        record = ctx.store.get_prune(plan.session_id, live=live)
        if record is not None:
            if record.boundary >= len(idx.units) or prune.fingerprint(idx.units, record.boundary) != record.fingerprint:
                plan.notes.append("pruned history diverged (rewind or client compaction); prune not applied")
            elif record.pending and not (ctx.mode == "live" and prune.is_prompt(prune.newest(idx.units))):
                pending_prune = record  # a background summary waits for the next user prompt
            else:
                if record.pending:
                    ctx.store.activate_prune(plan.session_id, live)
                    record.pending = False
                stored_prune = record
                idx.pruned_before = record.boundary

    plan.replay_body, replay_changed = _build(
        body, idx, adapter, lambda r: (r.text, r.rule) if r.source == "stored" else None
    )
    plan.replay_saved = sum(max(0, r.orig_tokens - tokens(text)) for r, text, _ in replay_changed.values())

    new: dict[int, tuple[str, str, str | None]] = {}
    if ctx.mode != "freeze":
        # 2. Ingest: only tool outputs the model has not seen yet.
        for k, r in enumerate(idx.results):
            if r.unit < idx.ingest_from or r.source == "stored" or not r.rewritable or r.media:
                continue
            try:
                hit = ingest(r, cfg, ctx)
            except OSError as exc:
                plan.notes.append(f"spill write failed: {exc}")
                hit = None
            if hit:
                new[k] = hit
        if new and _over_budget(ctx):
            plan.notes.append("ingest over budget; dropped")
            new = {}

        # 3. Epoch.
        epoch_new = (
            _plan_epoch(body, idx, ctx, plan, new) if cfg["epochs"].get("enabled") and adapter.epochs_supported else {}
        )
        if epoch_new and _over_budget(ctx, cfg["epochs"].get("budget_ms", 250)):
            plan.notes.append("epoch over budget; dropped")
            epoch_new = {}
            plan.epoch = None
        new.update(epoch_new)

    for k, (text, rule, path) in new.items():
        r = idx.results[k]
        if r.source == "stored":
            plan.replaced_rewrites.append(Rewrite(r.tid, live, r.orig_hash, r.text, r.rule, None, None, r.note_path))
        plan.new_saved[r.tid] = max(0, r.current_tokens - tokens(text))
        r.text, r.rule, r.note_path, r.source = text, rule, path, "new"
        plan.new_rewrites.append(
            Rewrite(
                r.tid, live, r.orig_hash, text, rule, idx.turns if rule in EPOCH_RULES else None, plan.session_id, path
            )
        )
        plan.rules[rule] = plan.rules.get(rule, 0) + 1

    plan.body_out, changed = _build(
        body, idx, adapter, lambda r: (r.text, r.rule) if r.source in ("stored", "new") else None
    )
    plan.replay_differs = bool(plan.new_rewrites)
    plan.saved_tokens = sum(max(0, r.orig_tokens - tokens(text)) for r, text, _ in changed.values())

    if plan.body_out is not None:
        protected_from = _protected_from(idx, cfg["thresholds"]["protected_turns"])
        try:
            validate(body, plan.body_out, _meta(changed), protected_from, adapter)
        except ValidationError as exc:
            # A new rewrite broke an invariant: forward stored rewrites only.
            plan.notes.append(f"validation failed: {exc}")
            plan.new_rewrites, plan.replaced_rewrites, plan.rules, plan.epoch = [], [], {}, None
            plan.new_saved = {}
            plan.body_out = plan.replay_body
            plan.replay_differs = False
            plan.saved_tokens = plan.replay_saved
            if plan.replay_body is not None:
                validate(body, plan.replay_body, _meta(replay_changed, "stored"), len(idx.units), adapter)

    # 4. Old-turn prune: replay the stored one, or create a new one at a prompt boundary.
    new_prune = None
    if ctx.mode != "freeze" and adapter.supports_prune and cfg["prune"].get("enabled") and pending_prune is None:
        try:
            new_prune = _plan_prune(body, idx, ctx, plan, stored_prune)
        except OSError as exc:
            plan.notes.append(f"prune history write failed: {exc}")
        if new_prune is not None and _model_summary(body, ctx, plan, new_prune):
            # The model writes this summary in the background; until it is ready, nothing changes.
            plan.compaction = {"prune": new_prune}
            plan.notes.append("prune: model summary requested; it takes effect at the next prompt")
            new_prune, plan.prune_info = None, None
    if stored_prune is not None:
        base = plan.replay_body if plan.replay_body is not None else body
        plan.replay_body = prune.apply(base, stored_prune.boundary, stored_prune.summary, stored_prune.kind)
        prune.validate(base, plan.replay_body, stored_prune.boundary, stored_prune.summary, stored_prune.kind)
        plan.replay_saved += stored_prune.dropped_tokens
    final = new_prune or stored_prune
    if final is not None:
        base = plan.body_out if plan.body_out is not None else body
        try:
            pruned = prune.apply(base, final.boundary, final.summary, final.kind)
            prune.validate(base, pruned, final.boundary, final.summary, final.kind)
        except ValidationError as exc:
            if new_prune is None:
                raise
            plan.notes.append(f"prune validation failed: {exc}")
            new_prune, plan.new_prune, plan.prune_info = None, None, None
            plan.rules.pop(prune.RULE, None)
            final = stored_prune
            pruned = prune.apply(base, final.boundary, final.summary, final.kind) if final else base
        plan.body_out = pruned
        plan.saved_tokens += final.dropped_tokens if final else 0
    plan.compact_beta = final is not None and final.kind == "compaction"
    if plan.compaction is not None:
        # Summarize exactly what the model sees now (stored rewrites and any earlier prune applied).
        job = plan.compaction["prune"]
        seen = plan.body_out if plan.body_out is not None else body
        cut = job.boundary - (len(body["messages"]) - len(seen["messages"]))
        plan.compaction["request"] = prune.compaction_request(seen, cut)
    if new_prune is not None:
        plan.new_prune, plan.replaced_prune = new_prune, stored_prune
        plan.replay_differs = True
        plan.rules[prune.RULE] = 1

    # Reasoning blocks after a history edit (epoch or prune) need drop_block on enforcing accounts.
    if ctx.enforced and adapter.supports_drop_block and isinstance(body.get("thinking"), dict):
        epoch_edit = cfg["epochs"].get("enforced_accounts") == "drop_block" and any(
            rule in EPOCH_RULES and idx.reasoning_after(r.unit) for r, _, rule in changed.values()
        )
        prune_edit = (
            final is not None
            and final.kind != "compaction"  # the API keeps the kept turns' thinking valid
            and cfg["prune"].get("enforced_accounts") == "drop_block"
            and any(u >= final.boundary for u in idx.reasoning_units)
        )
        plan.drop_block = epoch_edit or prune_edit
    if plan.drop_block:
        plan.body_out = adapter.with_drop_block(plan.body_out) if plan.body_out is not None else None
        plan.replay_body = adapter.with_drop_block(plan.replay_body) if plan.replay_body is not None else None

    if plan.new_prune is not None:
        plan.guard_pre_rate = _retrieval_rate(idx, ctx, 0, plan.new_prune.boundary)
    if ctx.mode == "live":
        plan.signals = _signals(idx, ctx, plan)
        if plan.guard_state is not None and stored_prune is not None and plan.new_prune is None:
            _retrieval_signal(idx, ctx, plan, stored_prune.boundary)
    return plan


def _nothing_changed(idx: Index, after: int, before: int) -> bool:
    """True when every call between two units only read or searched. Re-running a command after
    an edit or another command is normal work (tests after a fix), not a sign its output was lost."""
    return all(c.kind in ("read", "search", "graph_query") for c in idx.calls.values() if after < c.unit < before)


RETRIEVAL_MIN_TURNS = 10  # turns after a prune before their retrieval rate is compared


def _retrieval_rate(idx: Index, ctx: Context, start: int, end: int) -> float:
    """Reads and searches per turn over units [start, end)."""
    calls = sum(1 for c in idx.calls.values() if start <= c.unit < end and c.kind in ("read", "search"))
    turns = idx.unit_turn[end - 1] - idx.unit_turn[start] + 1 if end > start else 0
    return calls / max(1, turns)


def _retrieval_signal(idx: Index, ctx: Context, plan: Plan, boundary: int) -> None:
    """Once per prune: reads and searches per turn since it more than doubled (the cost of compression
    moved into extra retrieval, as in arXiv 2608.16370)."""
    state = plan.guard_state
    pre = state.get("pre_rate")
    if pre is None or state.get("retrieval_for") == boundary or boundary >= len(idx.units):
        return
    if idx.turns - idx.unit_turn[boundary] < RETRIEVAL_MIN_TURNS:
        return
    post = _retrieval_rate(idx, ctx, boundary, len(idx.units))
    if post > 2 * pre + 0.5:
        state["retrieval_for"] = boundary
        plan.guard_signals.append(("retrieval", ""))
        plan.notes.append(f"guard: {post:.1f} reads and searches per turn since the prune, {pre:.1f} before it")


def _model_summary(body: dict, ctx: Context, plan: Plan, new_prune: Prune) -> bool:
    """True when this prune's summary should be written by the model (on-demand compaction).

    Opt-in (`prune.summarizer = "model"`), live mode only (shadow mode never makes the extra
    request), on models that support it, and not after a failed attempt in this session. Never
    on a consumer login: the compaction request is lightfoot's own, and subscription
    credentials are for Claude Code's requests only, so the extractive summary is used.
    """
    if ctx.cfg["prune"].get("summarizer") != "model" or ctx.mode != "live" or not ctx.provider.compaction:
        return False
    if ctx.consumer_auth:
        plan.notes.append("model summary skipped: subscription login; using the extractive summary")
        return False
    if not prune.supports_compaction(plan.model):
        return False
    if ctx.store.get_state(f"compaction_failed:{plan.session_id}"):
        return False
    msgs = body["messages"]
    # The first kept message must differ in role from the last summarized one, or the API merges them.
    return msgs[new_prune.boundary - 1].get("role") != msgs[new_prune.boundary].get("role")


def _safe_point(message) -> bool:
    """A user message holding only tool results: every call before it has its answer."""
    blocks = message.get("content") if isinstance(message, dict) and message.get("role") == "user" else None
    return (
        isinstance(blocks, list)
        and bool(blocks)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
    )


def _summary_holds_up(ctx: Context, stored: Prune | None) -> bool:
    """For `prune.early_trigger_tokens`: a model-written summary on an API key, or a session whose
    previous prune drew no pruning signal. In shadow mode the model saw everything, so only the
    first condition can apply there."""
    model_written = ctx.cfg["prune"].get("summarizer") == "model" and ctx.provider.compaction
    if model_written and not ctx.consumer_auth and ctx.mode == "live":
        return True
    if stored is None or ctx.mode not in ("live", "freeze"):
        return False
    state = guards.load(ctx.store, stored.session_id)
    return state["level"] == 0 and state["since_prune"] == 0


def _plan_prune(body: dict, idx: Index, ctx: Context, plan: Plan, stored: Prune | None) -> Prune | None:
    """A new prune when the conversation has outgrown the trigger and the user just sent a prompt."""
    pc, session, msgs = ctx.cfg["prune"], ctx.session, idx.units
    if session is None or not session.context_tokens or not session.floor_tokens:
        return None
    last = prune.newest(msgs)
    if not prune.is_prompt(last) and not (pc.get("mid_loop") and not ctx.enforced and _safe_point(last)):
        return None  # only when a new user prompt arrives, or with prune.mid_loop at a safe point
    dropped_before = stored.dropped_tokens if stored else 0
    # What the model last saw: pruned in live mode, the full history in shadow mode.
    seen_pruned = stored is not None and ctx.mode in ("live", "freeze")
    unpruned = max(0, session.context_tokens - session.floor_tokens) + (dropped_before if seen_pruned else 0)
    ttl = ctx.adapter.cache_ttl(body)
    cold = session.last_request is None or (ctx.now - session.last_request) > ttl
    # A cold cache makes the rewrite free, so the prune may come a little early rather than
    # waiting for a later, paid moment. It keeps the same keep_tokens either way.
    trigger = pc["trigger_tokens"]
    if cold and 0 < pc.get("cold_trigger_tokens", 0) < trigger:
        trigger = pc["cold_trigger_tokens"]
    if 0 < pc.get("early_trigger_tokens", 0) < trigger and _summary_holds_up(ctx, stored):
        trigger = pc["early_trigger_tokens"]
    if unpruned - dropped_before <= trigger:
        return None
    sizes = [prune.message_tokens(m) if isinstance(m, dict) else 0 for m in msgs]
    estimated = sum(sizes[1:])
    if estimated <= 0:
        return None
    scale = max(1.0, unpruned / estimated)  # hidden thinking tokens make the real size larger
    # After an idle gap the whole prompt is re-written anyway, so a deeper prune costs nothing
    # extra. It keeps less verbatim history, so it is opt-in (0 = use keep_tokens).
    keep = pc["keep_tokens"]
    if cold and 0 < pc.get("cold_keep_tokens", 0) < keep:
        keep = pc["cold_keep_tokens"]
    boundary = prune.choose_boundary(
        msgs, sizes, scale, keep, pc["min_kept_prompts"], after=stored.boundary if stored else 1
    )
    if boundary is None:
        return None
    history = ctx.spill_dir.parent / "history" / f"{plan.session_id}-{boundary}.md"
    summary = prune.build_summary(msgs, boundary, len(msgs), str(history), pc["max_summary_tokens"], ctx.profile)
    dropped = int(sum(sizes[1:boundary]) * scale) - tokens(summary)
    gain = dropped - dropped_before
    if gain < pc["min_drop_tokens"]:
        return None
    if ctx.enforced and pc.get("enforced_accounts") != "drop_block" and any(u >= boundary for u in idx.reasoning_units):
        plan.notes.append(
            "prune skipped: thinking blocks follow the boundary and the account enforces the prefix check"
        )
        return None

    kept = max(0, unpruned - dropped)
    info = {"cold": cold, "dropped": dropped, "gain": gain, "kept": kept, "boundary": boundary}
    if not cold:
        write = pricing.write_multiplier(plan.model, long_ttl=ttl >= 3600)
        read = pricing.read_multiplier(plan.model)
        n_star = (write * kept - read * (kept + gain)) / (read * gain)
        info["n_star"] = round(n_star, 1)
        if idx.turns < pc["safety_factor"] * n_star:
            plan.notes.append(f"prune deferred: break-even needs {n_star:.0f} more turns")
            return None
    prune.write_history(history, msgs, boundary, plan.session_id)
    plan.prune_info = info
    return Prune(
        plan.session_id,
        ctx.mode in ("live", "freeze"),
        boundary,
        prune.fingerprint(msgs, boundary),
        summary,
        norm_path(str(history)),
        dropped,
    )


def _signals(idx: Index, ctx: Context, plan: Plan) -> list[tuple[str, str, str, str]]:
    """Over-compression signals from the newest model turn: spill reads, quick re-reads, and
    re-runs of a command whose output was trimmed. Each also goes to `plan.guard_signals`."""
    found = []
    # command -> (tool, unit of its trimmed result)
    trimmed = {
        ctx.profile.command(r.name, r.inp): (r.name, r.unit)
        for r in idx.results
        if r.rule in guards.TRIM_RULES and r.unit < idx.model_from and r.kind == "shell"
    }
    for tid, call in idx.calls.items():
        if call.unit < idx.model_from or call.unit >= idx.ingest_from:
            continue
        if call.kind == "shell":
            command = ctx.profile.command(call.name, call.inp)
            if command in trimmed and _nothing_changed(idx, trimmed[command][1], call.unit):
                found.append((tid, "spill", "re_run", command[:200]))
                plan.guard_signals.append(("re_run", trimmed[command][0]))
            continue
        if call.kind != "read":
            continue
        path = ctx.profile.path(call.name, call.inp)
        if not path:
            continue
        if "/.lightfoot/history/" in norm_path(path):
            found.append((tid, prune.RULE, "history_read", path))
            plan.guard_signals.append(("history_read", ""))
            continue
        # Spill rewrites are stored under the spill file's path, stubbed reads under the file's own path.
        for rw in ctx.store.rewrites_by_path(norm_path(path), live=True):
            if rw.rule in ("spill", "old_results", "images"):
                found.append((tid, rw.rule, "spill_read", path))
                source = idx.calls.get(rw.tool_use_id)
                plan.guard_signals.append(("spill_read", source.name if source else ""))
                break
            if rw.rule == "stale_reads" and rw.epoch_turn is not None and plan.turns - rw.epoch_turn <= 2:
                found.append((tid, rw.rule, "re_read", path))
                plan.guard_signals.append(("re_read", ""))
                break
    return found


def _prune_near(ctx: Context) -> bool:
    """True when the conversation is within `epochs.prune_near` (a fraction) of the prune trigger."""
    pc, session = ctx.cfg["prune"], ctx.session
    if not (pc.get("enabled") and ctx.adapter.supports_prune) or session is None or not session.floor_tokens:
        return False
    above_floor = max(0, session.context_tokens - session.floor_tokens)
    return above_floor >= ctx.cfg["epochs"].get("prune_near", 0.8) * pc["trigger_tokens"]


def _plan_epoch(body: dict, idx: Index, ctx: Context, plan: Plan, ingest_new: dict) -> dict:
    """Choose history edits of old tool output (stale reads, repeated searches, old results).

    Evaluated on a cold cache, or every `warm_check_every` turns on a warm one, and skipped where
    the account enforces the thinking-prefix check and thinking follows the edit.
    """
    cfg, session, adapter = ctx.cfg, ctx.session, ctx.adapter
    ep = cfg["epochs"]
    if session is None or session.epochs_disabled:
        return {}
    if session.epochs and idx.turns - session.last_epoch_turn < ep["min_gap_turns"]:
        return {}
    ttl = adapter.cache_ttl(body)
    cold = session.last_request is None or (ctx.now - session.last_request) > ttl
    if not cold and idx.turns % max(1, int(ep.get("warm_check_every", 5))) != 0:
        return {}  # warm epochs are evaluated every Nth turn, not on every request
    if not cold and _prune_near(ctx):
        # A prune will soon replace the old turns anyway; a paid rewrite of them now is wasted.
        plan.notes.append("epoch skipped: a prune is near")
        return {}
    candidates = epoch_candidates(idx, cfg, ctx)
    if not candidates:
        return {}

    first_unit = min(idx.results[k].unit for k in candidates)
    if ctx.enforced and ep.get("enforced_accounts") != "drop_block" and idx.reasoning_after(first_unit):
        plan.notes.append("epoch skipped: thinking blocks follow the edit and the account enforces the prefix check")
        return {}

    # Text of every result as the model currently sees it. Image results that are still whole
    # are left out, so unit_tokens measures their blocks (images at their real cost).
    current = {r.path: r.current or "" for r in idx.results if not (r.media and r.text is None)}
    for k, (text, _, _) in ingest_new.items():
        current[idx.results[k].path] = text
    eligible_end = max(_protected_from(idx, cfg["thresholds"]["protected_turns"]), first_unit + 1)
    sizes = [
        adapter.unit_tokens(ui, unit, current) if isinstance(unit, dict) else 0 for ui, unit in enumerate(idx.units)
    ]
    saved = sum(max(0, idx.results[k].current_tokens - tokens(text)) for k, (text, _, _) in candidates.items())
    s_old = sum(sizes[first_unit:eligible_end])
    s_new = max(0, s_old - saved)
    tail = sum(sizes[eligible_end:])

    write = pricing.write_multiplier(plan.model, long_ttl=ttl >= 3600)
    read = pricing.read_multiplier(plan.model)
    info = {"cold": cold, "saved": saved, "s_old": s_old, "s_new": s_new, "tail": tail}

    if cold:
        if saved < ep["min_saving_tokens"]:
            return {}
    else:
        if saved < ep["min_saving_tokens_warm"] or s_old <= s_new:
            return {}
        n_star = (write * (s_new + tail) - read * (s_old + tail)) / (read * (s_old - s_new))
        expected_remaining = idx.turns  # sessions tend to run about as long again as they already have
        info["n_star"] = round(n_star, 2)
        if expected_remaining < ep["safety_factor"] * n_star:
            return {}
        info["predicted_write"] = s_new + tail
    plan.epoch = info
    return candidates
