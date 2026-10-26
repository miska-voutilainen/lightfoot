"""`lightfoot stats`, `lightfoot doctor` and `lightfoot report`."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__, pricing
from .config import ConfigError, global_config_path, lightfoot_home, load_config
from .integrations import detect
from .launch import GEMINI_UNROUTED_AUTH, gemini_auth_type, running
from .store import Store

FRESH_SESSION_TOKENS = 100_000
SIGNAL_LIMIT = 0.05

# Newest client versions whose request shapes were tested. Newer ones usually work
# (rewrites fail safe), but `doctor` points them out.
TESTED_CLIENTS = {"claude-cli": "2.1.191"}


def _version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", version))


def newest_clients(store: Store, since: float = 0) -> dict[str, str]:
    """Newest version seen per client name, e.g. {'claude-cli': '2.1.200'}."""
    newest: dict[str, str] = {}
    for r in store.query(
        "SELECT DISTINCT client_version FROM ledger WHERE client_version IS NOT NULL AND ts >= ?", (since,)
    ):
        name, _, version = r["client_version"].partition("/")
        if name not in newest or _version_key(version) > _version_key(newest[name]):
            newest[name] = version
    return newest


def _k(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return f"{n:.0f}"


def _money(x: float, subscription: bool) -> str:
    return f"{x:,.2f}" if subscription else f"${x:,.2f}"


row_costs = pricing.row_costs  # kept here for existing importers


def stats(store: Store, days: float = 7, session: str | None = None, limit: int = 20) -> str:
    """The `lightfoot stats` report: cost, saving and rule health per session.

    Covers the last `days`, or one session when `session` (an id prefix) is given.
    """
    cfg = load_config()
    subscription = cfg.get("billing") == "subscription"
    since = time.time() - days * 86400
    where, params = "ts >= ?", [since]
    if session:
        where, params = "session_id LIKE ?", [session + "%"]
    # `where` is a fixed clause; every value is a bound parameter.
    rows = store.query(f"SELECT * FROM ledger WHERE {where} AND status = 200 ORDER BY ts", params)  # noqa: S608  # nosec B608
    sessions = {r["id"]: r for r in store.query("SELECT * FROM sessions")}
    vaults = {r["id"]: r["vault"] for r in store.query("SELECT id, vault FROM launches")}

    out: list[str] = []
    unit = "API-equivalent usage (USD at API rates)" if subscription else "USD at API rates"
    switch = store.get_state("switch", "on")
    out.append(
        f"lightfoot {__version__} · new launches: {store.get_state('mode_override') or cfg.get('mode')} · "
        f"switch: {switch} · costs in {unit} (prices as of {pricing.PRICES_AS_OF})"
    )
    if not rows:
        out.append(
            f"No completed requests in the last {days:g} days." if not session else "No requests for that session."
        )
        return "\n".join(out)

    per: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    unknown_models: set[str] = set()
    for r in rows:
        sid = r["session_id"] or "-"
        cost, saved = row_costs(r)
        if not pricing.lookup(r["model"])[1]:
            unknown_models.add(str(r["model"]))
        s = per[sid]
        s["requests"] += 1
        s["cost"] += cost
        s["saved"] += saved
        s["saved_tokens"] += r["saved_tokens"] or 0
        s["unpriced"] = s["unpriced"] or not pricing.priced(r["model"])
        s["first"] = s["first"] or r["ts"]
        s["last"] = r["ts"]
        s["mode"] = r["mode"]
        s["agent"] = r["agent"] or "claude-code"
        s["model"] = r["model"]
        s["launch"] = r["launch_id"]
        s["turns"] = max(s["turns"], r["turn"] or 0)
        s["epochs"] += r["epoch"] or 0
        s["side"] += r["side"] or 0

    side_sessions = {sid for sid, s in per.items() if s["side"] == s["requests"]}
    side_requests = sum(int(s["side"]) for s in per.values())
    side_cost = sum(row_costs(r)[0] for r in rows if r["side"])
    total_cost = sum(s["cost"] for s in per.values())
    live_saved = sum(s["saved"] for s in per.values() if s["mode"] in ("live", "freeze"))
    shadow_saved = sum(s["saved"] for s in per.values() if s["mode"] == "shadow")
    shadow_cost = sum(s["cost"] for s in per.values() if s["mode"] == "shadow")
    out.append(
        f"{'Last ' + format(days, 'g') + ' days' if not session else 'Session ' + session} · "
        f"{len(per) - len(side_sessions)} sessions · {len(rows)} requests · cost {_money(total_cost, subscription)}"
    )
    if live_saved:
        live_cost = sum(s["cost"] for s in per.values() if s["mode"] in ("live", "freeze"))
        out.append(
            f"Live sessions: saved ≈ {_money(live_saved, subscription)} "
            f"({100 * live_saved / max(1e-9, live_saved + live_cost):.1f}% of what they would have cost)"
        )
    if shadow_saved or shadow_cost:
        out.append(
            f"Shadow sessions: projected saving ≈ {_money(shadow_saved, subscription)} "
            f"({100 * shadow_saved / max(1e-9, shadow_saved + shadow_cost):.1f}%) — nothing was changed"
        )
    out.append("")
    header = (
        f"{'Session':<10} {'Last request':<12} {'Model':<18} {'Req':>4} {'Turns':>5} {'Context':>7} "
        f"{'Cost':>9} {'Saved':>9} {'Mode':<11} Flags"
    )
    out.append(header)
    out.append("-" * len(header))
    now = time.time()
    # Sessions of only side requests (titles, quick checks) are summarized below the table instead.
    listed = {sid: s for sid, s in per.items() if sid not in side_sessions}
    ordered = sorted(listed.items(), key=lambda kv: kv[1]["last"], reverse=True)[:limit]
    for sid, s in ordered:
        srow = sessions.get(sid)
        context = srow["context_tokens"] if srow else 0
        flags = []
        if srow and context > FRESH_SESSION_TOKENS and now - (srow["last_request"] or 0) > 300:
            note = "start fresh"
            if vaults.get(s["launch"]):
                note += " (handoff goes to vault)"
            flags.append(note)
        if srow and srow["epochs_disabled"]:
            flags.append("epochs off")
        if s["epochs"]:
            flags.append(f"{int(s['epochs'])} epoch{'s' if s['epochs'] > 1 else ''}")
        last = datetime.fromtimestamp(s["last"]).strftime("%m-%d %H:%M")
        model = str(s["model"] or "?")[:18]
        mode = s["mode"] if s["agent"] == "claude-code" else f"{s['mode']}·{s['agent']}"
        if s["unpriced"]:  # no price for its model: the saving is context room, in tokens
            cost_cell, saved_cell = "—", f"{_k(s['saved_tokens'])} tok"
        else:
            cost_cell, saved_cell = _money(s["cost"], subscription), _money(s["saved"], subscription)
        out.append(
            f"{sid[:10]:<10} {last:<12} {model:<18} {int(s['requests']):>4} {int(s['turns']):>5} "
            f"{_k(context):>7} {cost_cell:>9} {saved_cell:>9} "
            f"{mode:<11} {', '.join(flags)}"
        )
    if len(listed) > limit:
        out.append(f"… {len(listed) - limit} older sessions not shown (--limit)")
    if side_requests:
        out.append(
            f"Side requests (no tools: titles, quick checks): {side_requests} of {len(rows)} requests, "
            f"{_money(side_cost, subscription)} · {100 * side_cost / max(1e-9, total_cost):.1f}% of cost; "
            "passed through like any request"
        )

    out.extend(limit_report(store, rows))
    out.extend(guard_report(store, set(per)))

    hooks = store.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(orig_tokens - new_tokens), 0) AS saved FROM hook_rewrites WHERE ts >= ?",
        (since,),
    )[0]
    if hooks["n"]:
        out.append(
            f"Hook mode: {hooks['n']} tool outputs condensed, {_k(hooks['saved'])} tokens smaller "
            "(each is re-sent on every later turn)"
        )
    out.append("")
    out.extend(rule_report(store, where, params))
    if unknown_models:
        out.append("")
        borrowed = sorted(m for m in unknown_models if pricing.priced(m))
        unpriced = sorted(m for m in unknown_models if not pricing.priced(m))
        if borrowed:
            out.append(f"Priced like their family's default model (unknown ids): {', '.join(borrowed)}")
        if unpriced:
            out.append(
                f"No price for {', '.join(unpriced)}: their savings are shown in tokens and left out of the totals; "
                f'add [prices."{unpriced[0]}"] to your config to price them'
            )
    warnings = store.query(
        "SELECT kind, COUNT(*) AS n FROM events WHERE level IN ('warn','error') AND ts >= ? "
        "GROUP BY kind ORDER BY n DESC",
        (since,),
    )
    if warnings:
        out.append("")
        out.append("Warnings: " + ", ".join(f"{w['kind']} ×{w['n']}" for w in warnings) + "  (lightfoot events)")
    return "\n".join(out)


def guard_report(store: Store, sessions: set[str]) -> list[str]:
    """How many of these sessions the quality guards acted on (`lightfoot why` has the steps)."""
    from .guards import LEVELS

    levels: dict[str, int] = defaultdict(int)
    tools = 0
    for r in store.query("SELECT key, value FROM state WHERE key LIKE 'guard:%'"):
        if r["key"][6:] not in sessions:
            continue
        try:
            state = json.loads(r["value"])
        except ValueError:
            continue
        if state.get("level"):
            levels[LEVELS[min(int(state["level"]), 3)]] += 1
        tools += len(state.get("off") or [])
    if not levels and not tools:
        return []
    parts = [f"{n} at {name}" for name, n in levels.items()]
    if tools:
        parts.append(f"{tools} tools no longer trimmed")
    return ["Quality guards: " + ", ".join(parts) + " (see `lightfoot why`)"]


def limit_report(store: Store, rows: list) -> list[str]:
    """Subscription limit use per launch (from the status line), and the share the saving is worth."""
    from .statusline import LIMIT_WINDOWS, WINDOW_LIMIT_SQL, limit_saved, limit_use

    by_launch: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["launch_id"]:
            by_launch[r["launch_id"]].append(r)
    lines = []
    for launch, launch_rows in by_launch.items():
        for window in LIMIT_WINDOWS:
            use = limit_use(store.query(WINDOW_LIMIT_SQL, (launch, window, launch, window)))
            if not use or use[1] <= 0:
                continue
            cost = saved = 0.0
            for r in launch_rows:
                if r["ts"] >= use[0]:
                    c, s = row_costs(r)
                    cost, saved = cost + c, saved + s
            label = "5-hour" if window == "five_hour" else "7-day"
            mode = "would save" if all(r["mode"] == "shadow" for r in launch_rows) else "saved"
            lines.append(
                f"  launch {launch[:10]}: {label} limit +{use[1]:.1f} points, "
                f"{mode} ≈ {limit_saved(use[1], cost, saved):.1f} points"
            )
    if not lines:
        return []
    return [
        "",
        "Subscription limits (latest window per launch; assumes limit use follows API-priced cost, "
        "and counts other sessions running at the same time):",
        *lines,
    ]


def rule_report(store: Store, where: str, params: list) -> list[str]:
    """Rule firings (live and shadow) and over-compression signals for the ledger rows `where` selects."""
    firings: dict[str, dict[str, int]] = {"live": defaultdict(int), "shadow": defaultdict(int)}
    # `where` is a fixed clause; every value is a bound parameter.
    for r in store.query(f"SELECT mode, rules FROM ledger WHERE {where} AND rules IS NOT NULL", params):  # noqa: S608  # nosec B608
        bucket = "shadow" if r["mode"] == "shadow" else "live"
        try:
            for rule, n in json.loads(r["rules"] or "{}").items():
                firings[bucket][rule] += n
        except ValueError:
            continue
    signals: dict[str, int] = defaultdict(int)
    # The signals table has the same ts and session_id columns, so the ledger filter applies as is.
    # `where` is a fixed clause; every value is a bound parameter.
    for r in store.query(f"SELECT rule, COUNT(*) AS n FROM signals WHERE {where} GROUP BY rule", params):  # noqa: S608  # nosec B608
        signals[r["rule"]] += r["n"]
    if not any(firings["live"].values()) and not any(firings["shadow"].values()):
        return ["Rules: none fired yet."]
    lines = [f"{'Rule':<12} {'Live':>6} {'Signals':>8} {'Rate':>6} {'Shadow':>7}"]
    for rule in ("json", "json_trim", "noise", "spill", "images", "stale_reads", "searches", "old_results", "prune"):
        live, shadow, sig = firings["live"][rule], firings["shadow"][rule], signals[rule]
        rate = sig / live if live else 0.0
        warn = "  ⚠ over 5%: tighten or disable this rule" if live >= 20 and rate > SIGNAL_LIMIT else ""
        lines.append(f"{rule:<12} {live:>6} {sig:>8} {100 * rate:>5.1f}% {shadow:>7}{warn}")
    return lines


NUDGE_SESSIONS = 3  # shadow sessions measured before suggesting going live


def shadow_readiness(store: Store, days: float = 30) -> str | None:
    """The measured shadow saving and the command to go live, once there is enough to go on."""
    rows = store.query(
        "SELECT * FROM ledger WHERE ts >= ? AND status = 200 AND mode = 'shadow' AND COALESCE(side, 0) = 0",
        (time.time() - days * 86400,),
    )
    sessions = {r["session_id"] for r in rows}
    if len(sessions) < NUDGE_SESSIONS:
        return None
    cost = saved = 0.0
    for r in rows:
        c, sv = row_costs(r)
        cost, saved = cost + c, saved + sv
    share = 100 * saved / max(1e-9, cost + saved)
    return (
        f"shadow mode measured {len(sessions)} sessions in {days:g} days: they would have cost {share:.0f}% less "
        f"(≈ ${saved:,.2f} at API prices). To save for real: `lightfoot shadow off` "
        "(`lightfoot eval` checks quality on your own tasks first)"
    )


def unknown_tools(store: Store, days: float = 30) -> dict[str, list[str]]:
    """Tool names each agent profile doesn't know, from sessions in the last `days`.

    An agent update that renames or adds a tool makes its calls fall back to kind `other`, so the
    rules quietly condense less. MCP tools and the generic profile are expected to be unknown.
    """
    from .profiles import PROFILES

    rows = store.query(
        "SELECT DISTINCT l.agent AS profile, t.name AS name FROM tools_used t "
        "JOIN ledger l ON l.session_id = t.session_id WHERE l.ts >= ? AND l.agent IS NOT NULL",
        (time.time() - days * 86400,),
    )
    found: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        profile = PROFILES.get(r["profile"])
        name = r["name"] or ""
        if profile is None or profile.name == "generic" or name.startswith("mcp__"):
            continue
        if profile.kind(name, {}) == "other":
            found[profile.name].add(name)
    return {k: sorted(v) for k, v in sorted(found.items())}


def doctor(cwd: str) -> str:
    """The `lightfoot doctor` report: Python, config, proxy, routing, integrations, hooks and support levels."""
    out: list[str] = []

    def line(ok: bool | None, text: str) -> None:
        mark = {True: "ok ", False: "XX ", None: "-- "}[ok]
        out.append(f"{mark} {text}")

    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]} ({sys.executable})")
    try:
        cfg = load_config(cwd)
        line(
            True,
            f"config: {global_config_path()}"
            + ("" if global_config_path().exists() else " (defaults; not created yet)"),
        )
    except ConfigError as exc:
        line(False, f"config error: {exc}")
        return "\n".join(out)
    port = int(cfg["port"])
    info = running(port)
    if info:
        same = info.get("version") == __version__
        line(
            same,
            f"proxy running on 127.0.0.1:{port} (pid {info.get('pid')}, version {info.get('version')})"
            + ("" if same else f" — this install is {__version__}; run `lightfoot restart`"),
        )
    else:
        line(None, "proxy not running (starts automatically with `lightfoot claude`)")

    store = Store()
    try:
        mode = store.get_state("mode_override") or cfg.get("mode")
        line(
            True,
            f"mode for new launches: {mode}"
            + (" (set by `lightfoot shadow`)" if store.get_state("mode_override") else ""),
        )
        cache = store.query(
            "SELECT SUM(cw5) AS short, SUM(cw1h) AS long FROM ledger WHERE ts >= ? AND agent = 'claude-code'",
            (time.time() - 7 * 86400,),
        )[0]
        if (cache["short"] or 0) > 4 * (cache["long"] or 0) and not cfg.get("cache", {}).get("ttl"):
            line(
                None,
                "your Claude Code sessions write the 5-minute cache: `lightfoot estimate --cache` shows whether "
                "the 1-hour one (`lightfoot config set cache.ttl 1h`) is cheaper for you",
            )
        if mode == "shadow":
            ready = shadow_readiness(store)
            if ready:
                line(None, ready)
        switch = store.get_state("switch", "on")
        line(
            switch == "on",
            f"switch: {switch}"
            + {"on": "", "off": " (frozen: replaying stored rewrites only)", "hard": " (raw passthrough)"}.get(
                switch, ""
            ),
        )
        enforced = store.get_state("account_enforced")
        setting = cfg["epochs"].get("account_enforced")
        line(
            None,
            f"thinking-prefix enforcement: {'detected' if enforced == '1' else 'not seen'} "
            f"(setting: {setting}; enforced accounts: {cfg['epochs'].get('enforced_accounts')})",
        )
        for name, version in newest_clients(store).items():
            tested = TESTED_CLIENTS.get(name)
            if tested and _version_key(version) > _version_key(tested):
                line(
                    None,
                    f"{name} {version} is newer than the last tested version ({tested}); rewrites fail safe, "
                    f"but watch `lightfoot events` and report problems",
                )
            elif tested:
                line(True, f"{name} {version} (tested up to {tested})")
        pc = cfg["prune"]
        line(
            None,
            "old-turn pruning: "
            + (
                f"on (at {int(pc['trigger_tokens']):,} conversation tokens, keeping "
                f"{int(pc['keep_tokens']):,}; enforced accounts: {pc['enforced_accounts']})"
                if pc.get("enabled")
                else "off"
            ),
        )
        last_prune = float(store.get_state("last_prune", "0") or 0)
        line(
            None,
            f"retention: {cfg.get('retention_days')} days; last prune "
            + (datetime.fromtimestamp(last_prune).strftime("%Y-%m-%d %H:%M") if last_prune else "never"),
        )
        for profile, names in unknown_tools(store).items():
            shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
            line(
                None,
                f"{profile}: {len(names)} tool{'s' if len(names) != 1 else ''} not in its profile ({shown}); "
                "their output is condensed as generic output. The profile may need updating (lightfoot/profiles.py)",
            )
    finally:
        store.close()
    if shutil.which("codex"):
        has_key = bool(os.environ.get("OPENAI_API_KEY"))
        line(
            has_key or None,
            "Codex CLI found (experimental support: `lightfoot codex`)"
            + ("" if has_key else "; OPENAI_API_KEY is not set, which `lightfoot codex` needs"),
        )
    if shutil.which("gemini"):
        auth = gemini_auth_type(cwd)
        line(
            None if auth in GEMINI_UNROUTED_AUTH else True,
            "Gemini CLI found (experimental support: `lightfoot gemini`)"
            + (
                f"; it is set to {GEMINI_UNROUTED_AUTH[auth]}, which lightfoot can't route (use a Gemini API key)"
                if auth in GEMINI_UNROUTED_AUTH
                else ""
            ),
        )
    from .support import report as support_report

    levels = support_report()
    line(None, levels[0])
    out.extend(levels[1:])
    for error in pricing.configure(cfg.get("prices")):
        line(False, f"config: {error}")
    age = pricing.data_age_days()
    line(
        age <= pricing.STALE_AFTER_DAYS,
        f"model and price data checked {pricing.data_checked()}"
        + (
            ""
            if age <= pricing.STALE_AFTER_DAYS
            else f", {age} days ago: newer models and prices may be missing (lightfoot/data/models.toml; "
            "your own [prices] config wins over it)"
        ),
    )

    found = detect(cwd, cfg)
    line(bool(found.graphify_report) or None, f"Graphify: {found.graphify_reason}")
    if found.graphify_report:
        # Each agent's project instruction file: whichever one names the report keeps it in the cached prefix.
        files = [f for f in ("AGENTS.md", "CLAUDE.md", "GEMINI.md") if (Path(cwd) / f).is_file()]
        naming = [f for f in files if "GRAPH_REPORT" in (Path(cwd) / f).read_text(encoding="utf-8", errors="replace")]
        line(
            bool(naming) or None,
            f"{', '.join(naming)} references GRAPH_REPORT.md (keeps it in the cached prefix)"
            if naming
            else "no AGENTS.md, CLAUDE.md or GEMINI.md references GRAPH_REPORT.md; "
            "add it to your agent's instruction file to keep it in the cached prefix",
        )
        hook = Path(cwd) / ".git" / "hooks" / "post-commit"
        if hook.is_file() and "graphify" in hook.read_text(encoding="utf-8", errors="replace"):
            line(True, "Graphify refreshes from a git post-commit hook (independent of the handoff hook)")
    line(bool(found.vault) or None, f"Obsidian: {found.obsidian_reason}")

    from .handoff import hook_status

    status = hook_status()
    if found.vault:
        line(
            status["installed"],
            "SessionEnd handoff hook installed"
            if status["installed"]
            else "SessionEnd handoff hook not installed (run `lightfoot handoff install`)",
        )
        if status["installed"] and status["after_graphify"]:
            line(
                False,
                "the handoff hook runs after a graphify SessionEnd hook; "
                "run `lightfoot handoff install` again to reorder",
            )
        if "graphify-out" in Path(found.vault).as_posix():
            line(
                None,
                "the vault is inside graphify-out; handoffs go to their own folder, which Graphify does not delete",
            )
    elif status["installed"]:
        line(None, "handoff hook installed, but no vault is configured, so it does nothing")

    from . import agentskills, hookmode, opencodehook

    for agent, hooks in (("Claude Code", hookmode), ("opencode", opencodehook)):
        if hooks.is_installed():
            line(True, f"hook mode installed for {agent} (tool output condensed without the proxy)")
    skills = agentskills.installed()
    line(
        bool(skills) or None,
        f"skills for Gemini CLI, opencode and Codex CLI: {', '.join(skills)}"
        if skills
        else "skills for Gemini CLI, opencode and Codex CLI not installed (`lightfoot skills install`)",
    )

    spill = Path(cwd) / ".lightfoot" / "spill"
    if spill.is_dir():
        files = list(spill.glob("*.log"))
        size = sum(f.stat().st_size for f in files)
        ignored = (Path(cwd) / ".lightfoot" / ".gitignore").is_file()
        line(
            ignored,
            f"spill: {len(files)} files, {size / 1e6:.1f} MB in {spill}"
            + ("" if ignored else " — .lightfoot/.gitignore missing"),
        )
    line(None, f"data: {lightfoot_home()}")
    if os.environ.get("ANTHROPIC_BASE_URL"):
        line(
            None,
            f"ANTHROPIC_BASE_URL is set in this shell: {os.environ['ANTHROPIC_BASE_URL']} "
            f"(lightfoot uses it as the upstream)",
        )
    return "\n".join(out)


def report(store: Store, days: float = 30) -> str:
    """A summary safe to paste into a public issue: numbers only, no prompts, paths or code."""
    since = time.time() - days * 86400
    rows = store.query("SELECT * FROM ledger WHERE ts >= ? AND status = 200", (since,))
    lines = [
        "```",
        f"lightfoot {__version__} · {platform.system()} {platform.release()} · "
        f"Python {platform.python_version()} · last {days:g} days",
    ]
    clients = newest_clients(store, since)
    lines.append("clients: " + (", ".join(f"{k}/{v}" for k, v in sorted(clients.items())) or "none"))
    by_agent: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    sessions_by: dict[str, set] = defaultdict(set)
    for r in rows:
        agent = r["agent"] or "claude-code"
        mode = "shadow" if r["mode"] == "shadow" else ("live" if r["mode"] in ("live", "freeze") else "passthrough")
        key = f"{agent}/{mode}"
        cost, saved = row_costs(r)
        by_agent[key]["requests"] += 1
        by_agent[key]["cost"] += cost
        by_agent[key]["saved"] += saved
        by_agent[key]["epochs"] += r["epoch"] or 0
        sessions_by[key].add(r["session_id"])
    for key, b in sorted(by_agent.items()):
        pct = 100 * b["saved"] / max(1e-9, b["saved"] + b["cost"])
        lines.append(
            f"{key}: {len(sessions_by[key])} sessions, {int(b['requests'])} requests, "
            f"saving {pct:.1f}%, {int(b['epochs'])} epochs"
        )
    lines.append("")
    lines.extend(rule_report(store, "ts >= ?", [since]))
    events = store.query(
        "SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? AND level IN ('warn','error') "
        "GROUP BY kind ORDER BY n DESC",
        (since,),
    )
    notes = store.query("SELECT detail FROM events WHERE ts >= ? AND kind = 'note'", (since,))
    note_kinds: dict[str, int] = defaultdict(int)
    for n in notes:
        note_kinds[re.sub(r" for \S+.*| \(.*|:.*", "", n["detail"])] += 1
    lines.append("")
    lines.append("warnings: " + (", ".join(f"{e['kind']} ×{e['n']}" for e in events) or "none"))
    lines.append("notes: " + (", ".join(f"{k} ×{v}" for k, v in sorted(note_kinds.items())) or "none"))
    lines.append("enforcement detected: " + ("yes" if store.get_state("account_enforced") == "1" else "no"))
    lines.append("```")
    return "\n".join(lines)
