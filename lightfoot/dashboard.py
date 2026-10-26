"""`lightfoot dashboard`: a local report page, written as one self-contained HTML file.

No server and no external resources: the page is generated from the ledger, saved under
~/.lightfoot/, and opened in the browser. It loads no scripts or fonts from anywhere, so
nothing about your sessions leaves the machine. Project paths are escaped.

Chart colors are the two first slots of a validated categorical palette (light and dark
steps checked for colour-vision deficiency and contrast against their surfaces).
"""

from __future__ import annotations

import html
import json
import math
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__, pricing
from .config import lightfoot_home, private_dir
from .reports import SIGNAL_LIMIT
from .store import Store

RULES = ("json", "json_trim", "noise", "spill", "images", "stale_reads", "searches", "old_results", "prune")
CHART_W, CHART_H, PAD_L, PAD_B, PAD_T = 760, 240, 56, 28, 12


def _money(x: float, subscription: bool) -> str:
    return f"{x:,.2f}" if subscription else f"${x:,.2f}"


def collect(store: Store, days: float) -> dict:
    """Everything the page shows, computed from the ledger, sessions, launches and signals."""
    since = time.time() - days * 86400
    rows = store.query("SELECT * FROM ledger WHERE ts >= ? AND status = 200 ORDER BY ts", (since,))
    cwd = {r["id"]: r["cwd"] for r in store.query("SELECT id, cwd FROM launches")}
    live_rows = [r for r in rows if r["mode"] in ("live", "freeze")]
    shown = live_rows or rows  # with no live traffic yet, show the shadow projection
    per_day: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    per_project: dict[str, dict] = defaultdict(lambda: {"sessions": set(), "cost": 0.0, "saved": 0.0})
    for r in shown:
        cost, saved = pricing.row_costs(r)
        day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
        per_day[day][0] += cost
        per_day[day][1] += saved
        project = per_project[cwd.get(r["launch_id"]) or "(no project: IDE or plain claude)"]
        project["sessions"].add(r["session_id"])
        project["cost"] += cost
        project["saved"] += saved
    firings: dict[str, dict[str, int]] = {"live": defaultdict(int), "shadow": defaultdict(int)}
    for r in rows:
        try:
            for rule, n in json.loads(r["rules"] or "{}").items():
                firings["shadow" if r["mode"] == "shadow" else "live"][rule] += n
        except ValueError:
            continue
    signals = {
        s["rule"]: s["n"]
        for s in store.query("SELECT rule, COUNT(*) AS n FROM signals WHERE ts >= ? GROUP BY rule", (since,))
    }
    cost = sum(c for c, _ in per_day.values())
    saved = sum(s for _, s in per_day.values())
    return {
        "days": days,
        "projected": not live_rows and bool(rows),
        "requests": len(shown),
        "sessions": len({r["session_id"] for r in shown}),
        "cost": cost,
        "saved": saved,
        "share": 100 * saved / (saved + cost) if saved + cost else 0.0,
        "per_day": sorted(per_day.items()),
        "per_project": sorted(per_project.items(), key=lambda kv: -kv[1]["saved"]),
        "firings": firings,
        "signals": signals,
    }


def _nice_ceiling(value: float) -> float:
    """The smallest of 1, 2, 4, 6 or 8 times a power of ten that is >= value, so quarter ticks stay round."""
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    return next(step * power for step in (1, 2, 4, 6, 8, 10) if step * power >= value)


def _chart(per_day: list, subscription: bool, projected: bool) -> str:
    """Stacked daily bars: paid (bottom) plus saved (top) is what the day would have cost."""
    if not per_day:
        return '<p class="empty">No requests in this period.</p>'
    top = _nice_ceiling(max(c + s for _, (c, s) in per_day))
    plot_w, plot_h = CHART_W - PAD_L - 8, CHART_H - PAD_B - PAD_T
    slot = plot_w / len(per_day)
    bar = max(4.0, min(28.0, slot * 0.6))
    parts = []
    for i in range(5):  # recessive grid with value labels
        value = top * i / 4
        y = PAD_T + plot_h - plot_h * i / 4
        parts.append(f'<line class="grid" x1="{PAD_L}" x2="{CHART_W - 8}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="tick" x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{_money(value, subscription)}</text>'
        )
    label_every = max(1, round(len(per_day) / 8))
    saved_name = "Projected saving" if projected else "Saved"
    for i, (day, (cost, saved)) in enumerate(per_day):
        x = PAD_L + slot * i + (slot - bar) / 2
        h_cost = plot_h * cost / top
        h_saved = plot_h * saved / top
        base = PAD_T + plot_h
        tip = html.escape(
            f"{day} · paid {_money(cost, subscription)} · {saved_name.lower()} {_money(saved, subscription)}"
        )
        # The paid segment sits on the baseline; the saved segment is stacked above it, 2px apart,
        # and only the topmost data end is rounded.
        if h_cost > 0:
            rounded = h_saved < 2
            parts.append(_rect(x, base - h_cost, bar, h_cost, "paid", rounded, tip))
        if h_saved >= 2:
            parts.append(_rect(x, base - h_cost - h_saved, bar, max(0.0, h_saved - 2), "saved", True, tip))
        # An invisible, wider column gives each day a hover target bigger than its bars.
        parts.append(
            f'<rect class="hit" x="{PAD_L + slot * i:.1f}" y="{PAD_T}" width="{slot:.1f}" height="{plot_h}" '
            f'data-tip="{tip}"/>'
        )
        if i % label_every == 0:
            parts.append(
                f'<text class="tick" x="{x + bar / 2:.1f}" y="{CHART_H - 8}" text-anchor="middle">{day[5:]}</text>'
            )
    body = "".join(parts)
    return (
        f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" aria-label="Daily cost: paid and {saved_name.lower()}">'
        f"{body}</svg>"
    )


def _rect(x: float, y: float, w: float, h: float, cls: str, rounded: bool, tip: str) -> str:
    if not rounded or h < 4:
        return f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" data-tip="{tip}"/>'
    r = min(4.0, w / 2, h)
    path = (
        f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z"
    )
    return f'<path class="{cls}" d="{path}" data-tip="{tip}"/>'


def render(data: dict, subscription: bool = False) -> str:
    """The dashboard as one self-contained HTML page, from the data `collect` returns."""
    money = lambda x: _money(x, subscription)  # noqa: E731
    saved_label = "Projected saving" if data["projected"] else "Saved"
    tiles = [
        (saved_label, money(data["saved"])),
        ("Saving", f"{data['share']:.1f}%"),
        ("Paid", money(data["cost"])),
        ("Requests", f"{data['requests']:,}"),
        ("Sessions", f"{data['sessions']:,}"),
    ]
    tile_html = "".join(f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in tiles)
    day_rows = "".join(f"<tr><td>{d}</td><td>{money(c)}</td><td>{money(s)}</td></tr>" for d, (c, s) in data["per_day"])
    project_rows = "".join(
        f"<tr><td class='path'>{html.escape(name)}</td><td>{len(p['sessions'])}</td><td>{money(p['cost'])}</td>"
        f"<td>{money(p['saved'])}</td>"
        f"<td>{100 * p['saved'] / (p['saved'] + p['cost']) if p['saved'] + p['cost'] else 0:.1f}%</td></tr>"
        for name, p in data["per_project"]
    )
    rule_rows = []
    for rule in RULES:
        live, shadow = data["firings"]["live"][rule], data["firings"]["shadow"][rule]
        sig = data["signals"].get(rule, 0)
        rate = sig / live if live else 0.0
        flag = "⚠ over 5%: tighten or disable" if live >= 20 and rate > SIGNAL_LIMIT else ""
        rule_rows.append(
            f"<tr><td>{rule}</td><td>{live:,}</td><td>{shadow:,}</td><td>{sig:,}</td>"
            f"<td>{100 * rate:.1f}%</td><td>{flag}</td></tr>"
        )
    unit = "API-equivalent usage" if subscription else "USD at API rates"
    note = (
        "No live sessions yet: the numbers are what lightfoot projects in shadow mode, where nothing is changed."
        if data["projected"]
        else "Live sessions only. Saved is estimated from the tokens not sent, at each request's own input rate."
    )
    return TEMPLATE.format(
        version=__version__,
        days=f"{data['days']:g}",
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        unit=unit,
        prices=pricing.PRICES_AS_OF,
        note=note,
        tiles=tile_html,
        chart=_chart(data["per_day"], subscription, data["projected"]),
        saved_label=saved_label,
        day_rows=day_rows,
        project_rows=project_rows or "<tr><td colspan='5'>No requests in this period.</td></tr>",
        rule_rows="".join(rule_rows),
    )


def write(store: Store, days: float = 30, subscription: bool = False) -> Path:
    """Write the dashboard to `~/.lightfoot/dashboard.html` and return its path."""
    path = private_dir(lightfoot_home()) / "dashboard.html"
    path.write_text(render(collect(store, days), subscription), encoding="utf-8")
    return path


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>lightfoot report</title>
<style>
:root {{
  color-scheme: light;
  --surface: #fcfcfb; --card: #f4f3f0; --line: #e3e1dc;
  --text: #0b0b0b; --text-2: #52514e; --muted: #77756f;
  --paid: #2a78d6; --saved: #eb6834;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --surface: #1a1a19; --card: #232321; --line: #34332f;
    --text: #f0efec; --text-2: #c3c2b7; --muted: #9a998f;
    --paid: #3987e5; --saved: #d95926;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface: #1a1a19; --card: #232321; --line: #34332f;
  --text: #f0efec; --text-2: #c3c2b7; --muted: #9a998f;
  --paid: #3987e5; --saved: #d95926;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--surface); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 32px 0 8px; }}
.meta, .note {{ color: var(--text-2); margin: 0; }}
.note {{ font-size: 13px; margin-top: 8px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-top: 20px; }}
.tile {{ background: var(--card); border-radius: 8px; padding: 12px; }}
.tile .k {{ color: var(--text-2); font-size: 13px; }}
.tile .v {{ font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.legend {{ display: flex; gap: 16px; color: var(--text-2); font-size: 13px; margin: 8px 0; }}
.legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 6px; vertical-align: -1px; }}
.chart {{ position: relative; }}
svg {{ width: 100%; height: auto; display: block; }}
.grid {{ stroke: var(--line); stroke-width: 1; }}
.tick {{ fill: var(--muted); font-size: 11px; }}
.paid {{ fill: var(--paid); }}
.saved {{ fill: var(--saved); }}
.hit {{ fill: transparent; }}
.hit:hover {{ fill: var(--text); fill-opacity: .05; }}
#tip {{ position: absolute; pointer-events: none; background: var(--text); color: var(--surface);
  font-size: 12px; padding: 4px 8px; border-radius: 4px; white-space: nowrap; display: none; }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }}
th {{ color: var(--text-2); font-weight: 500; }}
td.path {{ word-break: break-all; }}
details {{ margin-top: 8px; color: var(--text-2); }}
.empty {{ color: var(--text-2); }}
</style>
</head>
<body>
<main>
<h1>lightfoot report</h1>
<p class="meta">Last {days} days · lightfoot {version} · generated {generated} · {unit}, prices as of {prices}</p>
<div class="tiles">{tiles}</div>
<p class="note">{note}</p>

<h2>Cost per day</h2>
<div class="legend"><span><i style="background:var(--paid)"></i>Paid</span>
<span><i style="background:var(--saved)"></i>{saved_label}</span></div>
<div class="chart">{chart}<div id="tip"></div></div>
<details><summary>Table view</summary><div class="table-wrap"><table>
<tr><th>Day</th><th>Paid</th><th>{saved_label}</th></tr>{day_rows}</table></div></details>

<h2>Projects</h2>
<div class="table-wrap"><table>
<tr><th>Project</th><th>Sessions</th><th>Paid</th><th>{saved_label}</th><th>Saving</th></tr>{project_rows}
</table></div>

<h2>Rule health</h2>
<p class="note">Signals count the times Claude went back for condensed content (read a saved file, or re-read a
file soon after its read was stubbed). A rule over 5% is cutting too much.</p>
<div class="table-wrap"><table>
<tr><th>Rule</th><th>Live</th><th>Shadow</th><th>Signals</th><th>Rate</th><th></th></tr>{rule_rows}
</table></div>
</main>
<script>
(function () {{
  var tip = document.getElementById("tip"), chart = document.querySelector(".chart");
  if (!tip || !chart) return;
  chart.addEventListener("mousemove", function (e) {{
    var t = e.target.getAttribute && e.target.getAttribute("data-tip");
    if (!t) {{ tip.style.display = "none"; return; }}
    var box = chart.getBoundingClientRect();
    tip.textContent = t;
    tip.style.display = "block";
    var x = Math.min(e.clientX - box.left + 12, box.width - tip.offsetWidth - 4);
    tip.style.left = Math.max(4, x) + "px";
    tip.style.top = (e.clientY - box.top - 32) + "px";
  }});
  chart.addEventListener("mouseleave", function () {{ tip.style.display = "none"; }});
}})();
</script>
</body>
</html>
"""
