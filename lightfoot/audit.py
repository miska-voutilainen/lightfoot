"""`lightfoot audit`: what the fixed prefix (system prompt, tools, MCP servers) costs, per project.

The prefix is re-sent on every request and must never be rewritten: changing it would change
the agent's behaviour and break the cache. So this only reports, from what the proxy recorded
on each session's first request (hashes, token counts and a 60-character label per block,
never the full text):

- the prefix's size and cost: written once per session, then read on every call;
- MCP servers whose tools no session called, and what their definitions cost;
- parts that differ between sessions of one project, which stop new sessions from reading
  a cache another session wrote.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from . import pricing
from .store import Store


def _server(tool: str) -> str | None:
    """The MCP server of a tool named mcp__<server>__<tool>."""
    parts = tool.split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else None


def _prefix_cost(model: str | None, tokens: int, calls: int, long_ttl: bool) -> float:
    """Written once at the session's first call, read on every later one."""
    p, _ = pricing.lookup(model)
    w = p.write_long if long_ttl else p.write_short
    return p.input * tokens * (w + p.read * max(0, calls - 1)) / 1e6


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def audit(store: Store, days: float = 30) -> str:
    """The `lightfoot audit` report: each project's fixed prefix, its cost, unused MCP servers and parts that differ."""
    since = time.time() - days * 86400
    prefixes = store.query("SELECT * FROM prefixes WHERE ts >= ?", (since,))
    if not prefixes:
        return (
            f"No sessions recorded in the last {days:g} days. The audit reads what the proxy records on each "
            "session's first request, so run some sessions through lightfoot first."
        )
    cwd = {r["id"]: r["cwd"] for r in store.query("SELECT id, cwd FROM launches")}
    used: dict[str, set[str]] = defaultdict(set)
    for r in store.query("SELECT session_id, name FROM tools_used"):
        used[r["session_id"]].add(r["name"])
    calls: dict[str, dict] = defaultdict(lambda: {"n": 0, "long": False, "cost": 0.0})
    for r in store.query(
        "SELECT * FROM ledger WHERE ts >= ? AND status = 200 AND COALESCE(side, 0) = 0", (since - 86400,)
    ):
        c = calls[r["session_id"]]
        c["n"] += 1
        c["long"] = c["long"] or bool(r["cw1h"])
        c["cost"] += pricing.row_costs(r)[0]

    projects: dict[str, list] = defaultdict(list)
    for r in prefixes:
        projects[cwd.get(r["launch_id"]) or "(no project)"].append(r)

    out = [
        f"Fixed-prefix audit, last {days:g} days. Advisory only: lightfoot never changes the prefix.",
        "Costs at API prices: the prefix is written at each session's first call and read on every later one.",
    ]
    for project, rows in sorted(projects.items(), key=lambda kv: -len(kv[1])):
        out += ["", *_project(project, rows, used, calls)]
    return "\n".join(out)


def _project(project: str, rows: list, used: dict, calls: dict) -> list[str]:
    name = Path(project).name or project
    sizes, total, project_cost = [], 0.0, 0.0
    servers: dict[str, dict] = defaultdict(
        lambda: {"tools": set(), "tokens": [], "sessions": 0, "used": 0, "cost": 0.0}
    )
    block_variants: dict[int, set] = defaultdict(set)
    block_info: dict[int, tuple[int, str]] = {}
    tool_variants: dict[str, set] = defaultdict(set)
    orders = set()
    for r in rows:
        blocks, tools = json.loads(r["blocks"] or "[]"), json.loads(r["tools"] or "{}")
        sid = r["session_id"]
        c = calls.get(sid, {"n": 1, "long": False, "cost": 0.0})
        system = sum(b[1] for b in blocks)
        builtin = sum(v[1] for t, v in tools.items() if not _server(t))
        mcp = sum(v[1] for t, v in tools.items() if _server(t))
        sizes.append((system, builtin, mcp))
        total += _prefix_cost(r["model"], system + builtin + mcp, max(1, c["n"]), c["long"])
        project_cost += c["cost"]
        for i, (digest, tokens, label) in enumerate(blocks):
            block_variants[i].add(digest)
            block_info.setdefault(i, (tokens, label))
        for tool, (digest, _) in tools.items():
            tool_variants[tool].add(digest)
        orders.add(tuple(tools))
        per_server: dict[str, int] = defaultdict(int)
        for tool, (_, tokens) in tools.items():
            if _server(tool):
                per_server[_server(tool)] += tokens
                servers[_server(tool)]["tools"].add(tool)
        for server, tokens in per_server.items():
            s = servers[server]
            s["sessions"] += 1
            s["tokens"].append(tokens)
            s["used"] += any(_server(t) == server for t in used.get(sid, ()))
            s["cost"] += _prefix_cost(r["model"], tokens, max(1, c["n"]), c["long"])

    median = [statistics.median(x[i] for x in sizes) for i in range(3)]
    share = f", {100 * total / project_cost:.0f}% of its cost" if project_cost else ""
    lines = [
        f"{name}: {len(rows)} sessions, median prefix {_k(sum(median))} tokens "
        f"(system {_k(median[0])}, built-in tools {_k(median[1])}, MCP tools {_k(median[2])}); "
        f"≈ ${total:,.2f}{share}",
    ]
    if servers:
        lines.append(f"  {'MCP server':<24} {'Tools':>5} {'Tokens':>7} {'Sessions':>8} {'Used in':>7} {'Cost':>9}")
        for server, s in sorted(servers.items(), key=lambda kv: -kv[1]["cost"]):
            flag = "  unused: removing it would save about this" if s["used"] == 0 else ""
            lines.append(
                f"  {server[:24]:<24} {len(s['tools']):>5} {_k(statistics.median(s['tokens'])):>7} "
                f"{s['sessions']:>8} {s['used']:>7} ${s['cost']:>8,.2f}{flag}"
            )
    varying = [i for i, v in block_variants.items() if len(v) > 1]
    for i in varying:
        tokens, label = block_info[i]
        lines.append(
            f"  system block {i + 1} differs between sessions ({len(block_variants[i])} versions, {_k(tokens)} "
            f'tokens, starts "{label.strip()[:40]}"): sessions can\'t share the cache from there on. If it holds '
            "a date, a git status or another per-session value, moving that to the end keeps the rest shared"
        )
    changed = sorted(t for t, v in tool_variants.items() if len(v) > 1)
    if changed:
        lines.append(f"  tool definitions differ between sessions: {', '.join(changed[:8])}")
    if len(orders) > 1 and len({frozenset(o) for o in orders}) == 1:
        lines.append(f"  the same tools arrive in {len(orders)} different orders, so sessions can't share their cache")
    if not varying and not changed and len(orders) <= 1 and len(rows) > 1:
        lines.append("  the prefix is identical across sessions, so new sessions can read a cache another one wrote")
    return lines
