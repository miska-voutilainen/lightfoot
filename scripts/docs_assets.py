"""Generate the diagrams and charts in docs/assets/, in one plain style that follows light and dark mode.

    python scripts/docs_assets.py

Edit the drawing code below and re-run it rather than editing the SVG files. The style is a
technical drawing: no background, thin outlines, square corners, one accent colour for what
Lightfoot changes, and no sentences inside the picture (the text around it explains).
context-growth.svg is an illustration, labelled as one.
"""

import random
import sys
from html import escape
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace"

# GitHub's own text, border and link colours, so the drawings sit on the page in either mode.
STYLE = f"""
<style>
  text {{ font-family: {SANS}; fill: #1f2328; }}
  .mono {{ font-family: {MONO}; }}
  .muted {{ fill: #59636e; }}
  .accent {{ fill: #0969da; }}
  .box {{ fill: none; stroke: #59636e; }}
  .box-accent {{ fill: none; stroke: #0969da; }}
  .frame {{ fill: none; stroke: #8c959f; stroke-dasharray: 4 3; }}
  .line {{ stroke: #59636e; fill: none; }}
  .line-accent {{ stroke: #0969da; fill: none; }}
  .faint {{ stroke: #d0d7de; fill: none; }}
  .head {{ fill: #59636e; }}
  .head-accent {{ fill: #0969da; }}
  @media (prefers-color-scheme: dark) {{
    text {{ fill: #e6edf3; }}
    .muted {{ fill: #9198a1; }}
    .accent {{ fill: #4493f8; }}
    .box {{ stroke: #9198a1; }}
    .box-accent {{ stroke: #4493f8; }}
    .frame {{ stroke: #656c76; }}
    .line {{ stroke: #9198a1; }}
    .line-accent {{ stroke: #4493f8; }}
    .faint {{ stroke: #3d444d; }}
    .head {{ fill: #9198a1; }}
    .head-accent {{ fill: #4493f8; }}
  }}
</style>
"""
# Arrowheads: neutral and accent (url(#a), url(#aa)).
_MARKER = (
    '<marker id="{}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
    'orient="auto-start-reverse"><path class="{}" d="M0,0 L10,5 L0,10 z"/></marker>'
)
STYLE += "<defs>" + "".join(_MARKER.format(i, c) for i, c in (("a", "head"), ("aa", "head-accent"))) + "</defs>"


def svg(name: str, w: int, h: int, title: str, body: list[str]) -> None:
    """Write `docs/assets/<name>.svg` with the shared style; `title` is what screen readers announce."""
    doc = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        f'aria-labelledby="title"><title id="title">{escape(title)}</title>{STYLE}' + "".join(body) + "</svg>\n"
    )
    (OUT / f"{name}.svg").write_text(doc, encoding="utf-8")


def t(x, y, s, size=13, cls="", anchor="start", weight=400):
    """A text element at (x, y); `cls` picks a style class from STYLE."""
    c = f' class="{cls}"' if cls else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}"{c}>{escape(s)}</text>'
    )


def box(x, y, w, h, cls="box"):
    """A plain rectangle with square corners."""
    width = 1.5 if cls == "box-accent" else 1
    return f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="2" stroke-width="{width}"/>'


def arrow(points, cls="line", head="a", dashed=False, width=1.25):
    """A line through `points` ending in an arrowhead (`head` names the marker: a or aa)."""
    d = "M" + " L".join(f"{x},{y}" for x, y in points)
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    return f'<path class="{cls}" d="{d}" stroke-width="{width}"{dash} marker-end="url(#{head})"/>'


def labelled(x, y, w, h, title, sub="", cls="box", mono_sub=False):
    """A box with a centred title and an optional second line."""
    tcls = "accent" if cls == "box-accent" else ""
    out = [box(x, y, w, h, cls)]
    if sub:
        out += [
            t(x + w / 2, y + h / 2 - 3, title, 13, tcls, "middle", 600),
            t(x + w / 2, y + h / 2 + 14, sub, 11.5, "mono muted" if mono_sub else "muted", "middle"),
        ]
    else:
        out.append(t(x + w / 2, y + h / 2 + 4.5, title, 13, tcls, "middle", 600))
    return out


# --- how it works -------------------------------------------------------------------
h = [
    *labelled(10, 50, 130, 56, "your agent"),
    box(230, 30, 300, 96, "box-accent"),
    t(380, 62, "Lightfoot", 14, "accent", "middle", 600),
    t(380, 80, "on your machine", 11.5, "muted", "middle"),
    t(380, 106, "condense · validate · freeze", 12, "", "middle"),
    *labelled(620, 50, 130, 56, "model API"),
    arrow([(140, 68), (228, 68)], "line-accent", "aa"),
    t(184, 61, "request", 11.5, "muted", "middle"),
    arrow([(530, 68), (618, 68)], "line-accent", "aa"),
    t(574, 61, "condensed", 11.5, "accent", "middle"),
    arrow([(618, 92), (532, 92)]),
    t(574, 108, "response", 11.5, "muted", "middle"),
    arrow([(228, 92), (142, 92)]),
    t(184, 108, "unchanged", 11.5, "muted", "middle"),
    arrow([(320, 126), (320, 158)], dashed=True),
    arrow([(440, 126), (440, 158)], dashed=True),
    *labelled(230, 160, 140, 44, "ledger", "tokens, cost"),
    *labelled(390, 160, 140, 44, ".lightfoot/", "full text kept"),
]
svg("how-it-works", 760, 214, "How Lightfoot works: a local proxy between your agent and the model API", h)

# --- context growth (illustration) ---------------------------------------------------
X0, X1, Y0, Y1 = 56, 640, 24, 214
TURNS, YMAX = 300, 450_000


def px(turn):
    """The chart's x position of a turn number."""
    return X0 + (X1 - X0) * turn / TURNS


def py(tok):
    """The chart's y position of a token count."""
    return Y1 - (Y1 - Y0) * tok / YMAX


rng = random.Random(7)  # noqa: S311 - a fixed seed: the illustration must not change between runs
without, with_ = [], []
a = c = 22_000
for turn in range(TURNS + 1):
    step = rng.uniform(900, 1900)
    a += step
    c += step
    if turn % 12 == 0 and c > 200_000 and turn < TURNS - 20:
        c = 108_000  # prune at a prompt: keep ≥100k + the summary
    without.append((px(turn), py(a)))
    with_.append((px(turn), py(c)))
g = [f'<line class="faint" x1="{X0}" x2="{X1}" y1="{Y1}" y2="{Y1}"/>']
for tok in (0, 100_000, 200_000, 300_000, 400_000):
    y = py(tok)
    g.append(t(X0 - 8, y + 4, f"{tok // 1000}k", 11, "muted", "end"))
    if tok:
        g.append(f'<line class="faint" x1="{X0}" x2="{X1}" y1="{y:.1f}" y2="{y:.1f}" stroke-dasharray="2 4"/>')
g += [t(px(turn), Y1 + 17, str(turn), 11, "muted", "middle") for turn in (0, 100, 200, 300)]
g += [
    f'<path class="line" stroke-width="1.5" d="M{" L".join(f"{x:.1f},{y:.1f}" for x, y in without)}"/>',
    f'<path class="line-accent" stroke-width="1.75" d="M{" L".join(f"{x:.1f},{y:.1f}" for x, y in with_)}"/>',
    t(X1 + 8, without[-1][1] + 4, "without Lightfoot", 12, "muted"),
    t(X1 + 8, with_[-1][1] + 4, "with Lightfoot", 12, "accent"),
    t(X0, 14, "tokens re-sent per turn", 11, "muted"),
    t(X1, 14, "illustration", 11, "muted", "end"),
    t(px(150), Y1 + 34, "turns", 11, "muted", "middle"),
]
svg(
    "context-growth",
    780,
    256,
    "Illustration: the context Lightfoot re-sends stays bounded while the full one keeps growing",
    g,
)

# --- architecture: pipeline ------------------------------------------------------------
p = [
    *labelled(10, 66, 100, 48, "your agent"),
    box(140, 24, 560, 142, "frame"),
    t(152, 42, "Lightfoot daemon, 127.0.0.1:8787", 11.5, "muted"),
]
stages = (
    ("adapter", "adapters/", "parse into a neutral view"),
    ("planner", "transform.py", "replay stored, plan new"),
    ("validator", "validate.py", "nothing else changed"),
    ("fail-safe", "server.py", "fall back on a 400"),
)
for i, (name, module, what) in enumerate(stages):
    bx = 156 + i * 136
    p += [*labelled(bx, 66, 116, 48, name, module, mono_sub=True), t(bx + 58, 136, what, 11, "muted", "middle")]
    if i:
        p.append(arrow([(bx - 20, 90), (bx - 2, 90)], "line-accent", "aa"))
p += [
    arrow([(110, 90), (154, 90)], "line-accent", "aa"),
    arrow([(680, 90), (728, 90)], "line-accent", "aa"),
    *labelled(730, 66, 100, 48, "model API"),
    arrow([(670, 114), (670, 196)], dashed=True),  # right of the fail-safe caption
    *labelled(612, 198, 116, 44, "ledger", "store.py", mono_sub=True),
]
svg("pipeline", 840, 252, "The request pipeline: adapter, planner, validator, fail-safe, ledger", p)

# --- architecture: prune before / after ----------------------------------------------------
r = [t(10, 20, "before", 12, "muted"), t(500, 20, "after, at a new user prompt", 12, "muted")]


def strip(x, y, w, h, label, note, cls="box", code=True):
    """One run of messages: its index range (or a name) on the left, what it holds on the right."""
    tcls = ("mono " if code else "") + ("accent" if cls == "box-accent" else "")
    return [
        box(x, y, w, h, cls),
        t(x + 12, y + 22, label, 12.5, tcls.strip()),
        t(x + w - 12, y + 22, note, 11.5, "muted", "end"),
    ]


r += [
    *strip(10, 30, 360, 34, "messages[0]", "first message"),
    *strip(10, 70, 360, 110, "messages[1:boundary]", "the oldest turns"),
    *strip(10, 186, 360, 56, "messages[boundary:]", "at least 100k tokens, 3 prompts"),
    arrow([(386, 125), (484, 125)], "line-accent", "aa"),
    *strip(500, 30, 360, 34, "messages[0]", "unchanged"),
    *strip(500, 70, 360, 34, "one assistant turn", "the summary", "box-accent", code=False),
    *strip(500, 110, 360, 56, "messages[boundary:]", "unchanged"),
    arrow([(370, 160), (436, 160), (436, 212), (498, 212)], dashed=True),
    *strip(500, 196, 360, 34, ".lightfoot/history/", "full text"),
]
svg(
    "prune",
    870,
    252,
    "Old-turn pruning: messages[1:boundary] become one summary turn; the full text goes to a history file",
    r,
)

# --- architecture: fail-safe ladder --------------------------------------------------------
f = []
steps = (
    ("1. rewrite", "stored + new rewrites", "box-accent"),
    ("2. stored rewrites only", "new ones rolled back", "box"),
    ("3. the original request", "session goes passthrough", "box"),
)
for i, (title, sub, cls) in enumerate(steps):
    bx = 10 + i * 256
    f += [
        *labelled(bx, 20, 212, 52, title, sub, cls),
        arrow([(bx + 106, 72), (bx + 106, 112)], "line-accent" if i == 0 else "line", "aa" if i == 0 else "a"),
        t(bx + 114, 96, "accepted", 11, "muted"),
    ]
    if i < 2:
        f += [arrow([(bx + 212, 46), (bx + 254, 46)]), t(bx + 233, 39, "400", 11, "muted", "middle")]
f += [box(10, 114, 724, 30), t(372, 134, "the response streams back to the agent, unchanged", 12.5, "", "middle")]
svg("failsafe", 744, 154, "The fail-safe ladder: rewrite, then stored rewrites only, then the original", f)

# --- architecture: quality guards ---------------------------------------------------------
q = []
states = ("normal", "watch", "keep more", "stop")
effects = ("", "", "next prune keeps twice the tokens", "no new prunes or epochs")
for i, (name, effect) in enumerate(zip(states, effects, strict=True)):
    bx = 10 + i * 215
    q += labelled(bx, 40, 120, 40, name)
    if effect:
        q.append(t(bx + 60, 98, effect, 11, "muted", "middle"))
    if i < 3:
        q.append(arrow([(bx + 120, 60), (bx + 213, 60)]))
labels = (("pruning", "signal"), ("keep_more", "signals"), ("as many", "again"))
for i, (l1, l2) in enumerate(labels):
    mx = 10 + i * 215 + 167
    q += [t(mx, 39, l1, 11, "muted", "middle"), t(mx, 51, l2, 11, "muted", "middle")]
q += [
    arrow([(285, 80), (285, 124), (70, 124), (70, 82)], dashed=True),
    t(178, 140, "quiet_turns without a signal", 11, "muted", "middle"),
]
svg("guards", 790, 150, "Quality guards per session: normal, watch, keep more, stop", q)


# --- sequence diagrams ----------------------------------------------------------------
def sequence(name, title, lanes, messages, width=760):
    """A sequence diagram: one lane per party, then `messages` from top to bottom.

    Each message is (kind, from lane, to lane, label): kind "solid", "dash" or "accent" draws an
    arrow in that style, and "note" writes the label beside the first lane.
    """
    gap = (width - 160) / (len(lanes) - 1)
    xs = [80 + i * gap for i in range(len(lanes))]
    height = 70 + 34 * len(messages)
    out = []
    for x, label in zip(xs, lanes, strict=True):
        cls = "box-accent" if label.startswith("Lightfoot") else "box"
        out += [
            *labelled(x - 70, 10, 140, 30, label, cls=cls),
            f'<line class="faint" x1="{x}" x2="{x}" y1="40" y2="{height - 8}"/>',
        ]
    y = 70
    for kind, a, b, label in messages:
        if kind == "note":
            out.append(t(xs[a] + 10, y + 4, label, 11.5, "muted"))
        else:
            x1, x2 = xs[a], xs[b]
            cls, head = ("line-accent", "aa") if kind == "accent" else ("line", "a")
            off = 2 if x2 > x1 else -2
            out.append(arrow([(x1 + off, y), (x2 - off * 2, y)], cls, head, dashed=kind == "dash"))
            out.append(t((x1 + x2) / 2, y - 6, label, 11.5, "accent" if kind == "accent" else "", "middle"))
        y += 34
    svg(name, width, height, title, out)


sequence(
    "model-summary",
    "Model-written summaries: requested in the background, swapped in at the next prompt",
    ["your agent", "Lightfoot", "Anthropic API"],
    [
        ("solid", 0, 1, "request at prompt N (a prune is due)"),
        ("solid", 1, 2, "the request, unchanged"),
        ("dash", 1, 2, "in the background: summarize messages[:boundary]"),
        ("dash", 2, 1, "a signed compaction block"),
        ("note", 1, 1, "stored as a pending prune"),
        ("solid", 0, 1, "request at prompt N+1"),
        ("accent", 1, 2, "block + messages[boundary:], beta header added"),
    ],
)
sequence(
    "hook-mode",
    "Hook mode: Claude Code's PostToolUse hook condenses output before it is stored",
    ["Bash or MCP tool", "Claude Code", "Lightfoot hook", "model API"],
    [
        ("solid", 0, 1, "tool output"),
        ("solid", 1, 2, "PostToolUse JSON on stdin"),
        ("accent", 2, 1, "updatedToolOutput (or nothing)"),
        ("note", 1, 1, "stored in the transcript"),
        ("accent", 1, 3, "every later request carries the condensed output"),
    ],
)
print("wrote", sorted(p.name for p in OUT.glob("*.svg")))
