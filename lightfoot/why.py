"""`lightfoot why`: what lightfoot changed in one session, and what each change saved.

Everything here is read from the local database; nothing is recomputed. The session is
picked by id prefix (as `lightfoot stats` shows it), or defaults to the most recent one.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

from .reports import _k, _money, row_costs
from .store import Store

_NOTE_RE = re.compile(r"\[lightfoot: [^\]]*\]")

# What a rule did, for rewrites whose text carries no note of its own.
RULE_LABELS = {
    "json": "JSON output without indentation (lossless)",
    "json_trim": "long text values inside one line of JSON trimmed to head, tail and error lines",
    "noise": "terminal noise removed (ANSI codes, progress bars, repeated lines)",
}


def _when(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def _describe(rule: str, text: str) -> str:
    """One line for a stored rewrite: lightfoot's own note in it, or what the rule does."""
    notes = _NOTE_RE.findall(text or "")
    if notes:
        note = notes[-1]
        return note if len(note) <= 220 else note[:217] + "…]"
    return RULE_LABELS.get(rule, rule)


def resolve_session(store: Store, prefix: str | None) -> tuple[str | None, list[str]]:
    """(session id, other matches). The newest session when `prefix` is empty."""
    if prefix:
        rows = store.query(
            "SELECT id FROM sessions WHERE id LIKE ? ORDER BY COALESCE(last_request, first_seen) DESC", (prefix + "%",)
        )
    else:
        rows = store.query("SELECT id FROM sessions ORDER BY COALESCE(last_request, first_seen) DESC LIMIT 1")
    ids = [r["id"] for r in rows]
    return (ids[0] if ids else None), ids[1:]


def why(store: Store, session: str | None = None, full: bool = False, limit: int = 20, subscription=False) -> str:
    """The `lightfoot why` report: every rewrite and prune in a session, what it saved, and where the full text is."""
    sid, others = resolve_session(store, session)
    if sid is None:
        return f"No session matches {session!r}." if session else "No sessions recorded yet."
    if session and others:
        return "Several sessions match; use a longer prefix:\n" + "\n".join(f"  {s}" for s in [sid, *others])

    srow = store.query("SELECT * FROM sessions WHERE id = ?", (sid,))[0]
    launch = store.query("SELECT cwd FROM launches WHERE id = ?", (srow["launch_id"],)) if srow["launch_id"] else []
    rows = store.query("SELECT * FROM ledger WHERE session_id = ? AND status = 200 ORDER BY ts", (sid,))
    out = [
        f"Session {sid} · mode {srow['mode']} · {len(rows)} requests · "
        f"{_when(srow['first_seen'])} to {_when(srow['last_request'])}",
    ]
    if launch and launch[0]["cwd"]:
        out.append(f"Project: {launch[0]['cwd']}")
    if srow["mode"] == "shadow":
        out.append("Shadow mode: everything below was computed and measured, but nothing was sent changed.")

    cost = saved = 0.0
    saved_tokens = prompt_tokens = 0
    firings: dict[str, int] = defaultdict(int)
    for r in rows:
        c, s = row_costs(r)
        cost, saved = cost + c, saved + s
        saved_tokens += r["saved_tokens"] or 0
        prompt_tokens += (r["input"] or 0) + (r["cw5"] or 0) + (r["cw1h"] or 0) + (r["cr"] or 0)
        try:
            for rule, n in json.loads(r["rules"] or "{}").items():
                firings[rule] += n
        except ValueError:
            continue
    if rows:
        share = 100 * saved_tokens / max(1, saved_tokens + prompt_tokens)
        out.append(
            f"Cost {_money(cost, subscription)} · saved ≈ {_money(saved, subscription)} · "
            f"{_k(saved_tokens)} prompt tokens not sent ({share:.1f}% of what would have been)"
        )
    if firings:
        out.append("Rules fired: " + ", ".join(f"{rule} ×{n}" for rule, n in sorted(firings.items())))

    prunes = store.query("SELECT * FROM prunes WHERE session_id = ? ORDER BY live DESC", (sid,))
    for p in prunes:
        out.append("")
        kind = "Old-turn prune" if p["live"] else "Old-turn prune (shadow)"
        out.append(
            f"{kind}, {_when(p['created'])}: messages 2-{p['boundary']} replaced by a summary, "
            f"about {_k(p['dropped_tokens'])} tokens fewer on every later turn."
        )
        if p["history_path"]:
            out.append(f"  Full text: {p['history_path']}")
        lines = p["summary"].splitlines()
        shown = lines if full else lines[:12]
        out.extend("  | " + line for line in shown)
        if len(shown) < len(lines):
            out.append(f"  | … {len(lines) - len(shown)} more summary lines (--full)")

    rewrites = store.query(
        "SELECT rule, text, path, epoch_turn, created, live FROM rewrites WHERE session_id = ? ORDER BY created",
        (sid,),
    )
    if rewrites:
        out.append("")
        out.append(f"Tool output rewrites ({len(rewrites)}):")
        shown = rewrites if full else rewrites[-limit:]
        if len(shown) < len(rewrites):
            out.append(f"  … {len(rewrites) - len(shown)} earlier ones not shown (--full)")
        for r in shown:
            moment = f"epoch at turn {r['epoch_turn']}" if r["epoch_turn"] is not None else "new output"
            out.append(f"  {r['rule']:<12} {moment:<20} {_describe(r['rule'], r['text'])}")

    signals = store.query("SELECT kind, COUNT(*) AS n FROM signals WHERE session_id = ? GROUP BY kind", (sid,))
    if signals:
        out.append("")
        labels = {
            "history_read": "read a history file",
            "spill_read": "read a saved full output",
            "re_read": "re-read a file soon after its read was stubbed",
            "re_run": "re-ran a command whose output was trimmed",
        }
        out.append(
            "Claude went back for condensed content: "
            + ", ".join(f"{labels.get(s['kind'], s['kind'])} ×{s['n']}" for s in signals)
        )
    steps = store.query("SELECT ts, detail FROM events WHERE kind = 'guard' AND session_id = ? ORDER BY ts", (sid,))
    if steps:
        out.append("")
        out.append("Quality guards (condense less after Claude went back for condensed content):")
        out += [f"  {_when(e['ts'])}  {e['detail']}" for e in steps]
    if not rows and not prunes and not rewrites:
        out.append("Nothing changed in this session.")
    return "\n".join(out)
