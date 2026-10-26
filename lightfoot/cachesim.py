"""Cache-lifetime simulation for `lightfoot estimate --cache`: what each cache policy would
have cost on your own gap pattern.

Policies, each replayed over every recorded conversation:

| Policy | Writes priced at | Idle gaps |
| --- | --- | --- |
| 1h | the 1-hour rate (2x input) | a gap over an hour rebuilds the prompt |
| 5m | the 5-minute rate (1.25x) | a gap over 5 minutes rebuilds it |
| 5m + keep-alive | 1.25x | a ping (a cache read of the prompt) every 4.5 minutes keeps it warm, up to a limit |
| 1h + keep-alive | 2x | a ping every 55 minutes keeps it warm, up to a limit |

Each call's cacheable prompt (cache writes plus reads, as recorded) is re-split: when the
cache is warm under the policy, the previous call's prompt is read and the rest written;
when cold, all of it is written. The first call of a conversation keeps its recorded split.
Keep-alive pings would be lightfoot's own requests, so they are only for API-key users.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import pricing
from .estimate import _price_parts, _ts, _usage_counts

KEEPALIVE_LIMIT_S = 3 * 3600  # an abandoned session would otherwise ping forever
POLICIES = {
    "1h": (3600, False),
    "5m": (300, False),
    "5m + keep-alive": (300, True),
    "1h + keep-alive": (3600, True),
}
PING_MARGIN_S = {300: 30, 3600: 300}  # ping this long before the cache would expire


@dataclass
class Point:
    """One recorded API call: when it happened, its model and its token counts."""

    ts: float
    model: str
    counts: dict


@dataclass
class PolicyResult:
    """What one cache policy would have cost: in total, in keep-alive pings and in cache rebuilds."""

    cost: float = 0.0
    ping_cost: float = 0.0
    pings: int = 0
    rebuilds: int = 0
    rebuild_cost: float = 0.0


@dataclass
class Simulation:
    """The recorded cost, the gaps between calls, and each policy's result."""

    recorded: float = 0.0
    calls: int = 0
    conversations: int = 0
    gaps: dict = field(default_factory=lambda: defaultdict(int))  # bucket -> count
    policies: dict = field(default_factory=dict)  # name -> PolicyResult


def read_conversations(files: list[Path]) -> list[list[Point]]:
    """Every conversation's API calls (deduplicated by message id), oldest first."""
    groups: dict[tuple, list[Point]] = defaultdict(list)
    for path in files:
        seen: set[str] = set()
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    message = e.get("message") if isinstance(e, dict) else None
                    if e.get("type") != "assistant" or not isinstance(message, dict):
                        continue
                    mid, usage = message.get("id"), message.get("usage")
                    if (
                        not mid
                        or mid in seen
                        or not isinstance(usage, dict)
                        or message.get("model") in (None, "<synthetic>")
                    ):
                        continue
                    seen.add(mid)
                    key = (str(path), e.get("sessionId"), bool(e.get("isSidechain")))
                    groups[key].append(Point(_ts(e.get("timestamp")), message["model"], _usage_counts(usage)))
        except OSError:
            continue
    return [sorted(points, key=lambda p: p.ts) for points in groups.values() if points]


def _cost(model: str, inp: int, write: int, read: int, output: int, long_ttl: bool) -> float:
    p, _ = pricing.lookup(model)
    w = p.write_long if long_ttl else p.write_short
    return (p.input * (inp + w * write + p.read * read) + p.output * output) / 1e6


def _pings(gap: float, ttl: int, limit: float) -> tuple[int, bool]:
    """(pings sent during the gap, whether the cache is still warm when the next call arrives)."""
    interval = ttl - PING_MARGIN_S[ttl]
    n = 0
    while (n + 1) * interval < min(gap, limit):
        n += 1
    return n, gap <= n * interval + ttl


def simulate(conversations: list[list[Point]], limit: float = KEEPALIVE_LIMIT_S) -> Simulation:
    """Re-price every conversation under each cache policy, using the real gaps between its calls.

    Keep-alive pings stop after `limit` seconds of silence, since an abandoned session would ping forever.
    """
    sim = Simulation(conversations=len(conversations))
    sim.policies = {name: PolicyResult() for name in POLICIES}
    for points in conversations:
        previous: Point | None = None
        for point in points:
            c = point.counts
            sim.calls += 1
            sim.recorded += sum(_price_parts(point.model, c).values())
            cacheable = c["cw5"] + c["cw1h"] + c["cr"]
            gap = point.ts - previous.ts if previous else 0.0
            if previous:
                sim.gaps["under 5 min" if gap <= 300 else "5–60 min" if gap <= 3600 else "over 60 min"] += 1
            for name, (ttl, keepalive) in POLICIES.items():
                result = sim.policies[name]
                long_ttl = ttl == 3600
                if previous is None:
                    read, write = c["cr"], c["cw5"] + c["cw1h"]
                else:
                    pings, warm = _pings(gap, ttl, limit) if keepalive else (0, gap <= ttl)
                    prefix = sum(previous.counts[k] for k in ("input", "cw5", "cw1h", "cr"))
                    if pings:
                        ping = pings * _cost(point.model, 0, 0, prefix, 0, long_ttl)
                        result.pings += pings
                        result.ping_cost += ping
                        result.cost += ping
                    read = min(prefix, cacheable) if warm else 0
                    write = cacheable - read
                    if not warm:
                        result.rebuilds += 1
                        warm_read = min(prefix, cacheable)
                        # what the rebuild cost over reading the same prefix
                        result.rebuild_cost += _cost(point.model, 0, warm_read, 0, 0, long_ttl) - _cost(
                            point.model, 0, 0, warm_read, 0, long_ttl
                        )
                result.cost += _cost(point.model, c["input"], write, read, c["output"], long_ttl)
            previous = point
    return sim


def render(sim: Simulation, limit: float = KEEPALIVE_LIMIT_S) -> str:
    """The `lightfoot estimate --cache` report, with a recommended `cache.ttl`."""
    if not sim.calls:
        return "Cache lifetime: no API calls found."
    base = sim.policies["1h"].cost
    lines = [
        f"Cache lifetime: {sim.calls} calls in {sim.conversations} conversations, "
        f"recorded cost ${sim.recorded:,.2f} at API prices",
        "Gaps between calls: " + ", ".join(f"{k} {v}" for k, v in sim.gaps.items()),
        "",
        f"{'Policy':<17} {'Cost':>10} {'vs 1h':>8} {'Rebuilds':>9} {'Rebuild cost':>13} {'Pings':>7} {'Ping cost':>10}",
    ]
    for name, r in sim.policies.items():
        delta = 100 * (r.cost - base) / base if base else 0.0
        lines.append(
            f"{name:<17} ${r.cost:>9,.2f} {delta:>+7.1f}% {r.rebuilds:>9} ${r.rebuild_cost:>12,.2f} "
            f"{r.pings:>7} ${r.ping_cost:>9,.2f}"
        )
    best = min(sim.policies.items(), key=lambda kv: kv[1].cost)[0]
    five, hour = sim.policies["5m"].cost, sim.policies["1h"].cost
    choice = "1h" if hour < five else "5m"
    lines += [
        "",
        f"Cheapest on this history: {best}. Of the plain lifetimes, {choice} is cheaper by "
        f"{100 * abs(five - hour) / max(five, hour):.1f}%. Pro and Max get the 1-hour cache already; with an API key "
        f"Claude Code uses 5 minutes unless told otherwise: `lightfoot config set cache.ttl {choice}` sets it for "
        "`lightfoot claude`.",
        f"lightfoot doesn't send keep-alive pings (simulated here stopping after {limit / 3600:g} h idle): they would "
        "be its own requests, and on top of the right lifetime they are worth little.",
    ]
    return "\n".join(lines)
