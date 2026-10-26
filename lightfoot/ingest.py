"""The ingest rules (json, json_trim, spill, noise): rewrites of tool output the model has not seen yet.

Kept apart from transform.py, and free of the adapters, the store and the planner, so hook
mode (hookmode.py), which runs once per tool call, imports only what these rules need.
`ingest` reads three attributes of its result (orig_text, kind, name) and two of its
context (spill_dir, graphify_report).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import private_dir
from .text import (
    denoise_lines,
    error_line_indices,
    minify_json,
    norm_path,
    normalize_terminal,
    render_selection,
    select_lines,
    tokens,
)

TYPE_CHECKING = False  # typing.TYPE_CHECKING without importing typing, which hook mode never needs
if TYPE_CHECKING:
    from .adapters.base import Result

# --- spill files ----------------------------------------------------------------------


def spill_path(spill_dir: Path, text: str) -> Path:
    """Where the full text of a trimmed output is saved, named by its hash so the same output is saved once."""
    import hashlib  # deferred: hook mode needs it only when an output is spilled

    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return spill_dir / f"{digest[:16]}.log"


def write_spill(path: Path, text: str) -> None:
    """Save a spill file atomically, in a private folder that git ignores."""
    if path.exists():
        return
    if path.parent.parent.name == ".lightfoot":
        private_dir(path.parent.parent)
    private_dir(path.parent)
    data_dir = path.parent.parent
    if data_dir.name == ".lightfoot":
        ignore = data_dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    tmp = path.with_name(f"{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(text, encoding="utf-8", errors="replace")
    tmp.replace(path)


def excluded(cfg: dict, rule: str, r: Result) -> bool:
    """Whether the config's `[exclude]` lists keep `rule` away from this result, by tool kind or name."""
    listed = cfg["exclude"].get(rule) or []
    return r.kind in listed or r.name in listed


def graph_note(ctx) -> str:
    """The note added to trimmed searches when the project has a Graphify report."""
    return f"\n[lightfoot: for codebase orientation, see the Graphify report at {ctx.graphify_report}.]"


def json_line(lines: list[str]) -> bool:
    """True for output that is one JSON document on a single line (Cline's tool results, compact MCP output).

    Line-based trimming would keep only the start of that one line and lose its end, where a
    command's summary usually is, so such output is never spilled; json_trim condenses the
    strings inside it instead.
    """
    if len(lines) != 1 or lines[0].lstrip()[:1] not in ("{", "["):
        return False
    try:
        json.loads(lines[0])
    except ValueError:
        return False
    return True


# --- json_trim: long strings inside one line of JSON -------------------------------

# The encodings tried, in order, to reproduce a JSON line byte for byte: JavaScript's
# JSON.stringify (Cline, most MCP servers), then Python's json.dumps defaults.
_JSON_FORMATS = (
    {"separators": (",", ":"), "ensure_ascii": False},
    {"separators": (",", ":")},
    {"ensure_ascii": False},
    {},
)


def _json_format(value, text: str) -> dict | None:
    """The json.dumps options that encode `value` exactly as `text`, or None if none does."""
    for options in _JSON_FORMATS:
        if json.dumps(value, **options) == text:
            return options
    return None


def _trim_string(s: str, th: dict, spill_dir: Path, saved: list) -> str:
    """One string value condensed as `spill` and `noise` condense raw output; `s` itself if neither helps."""
    if "\n" not in s:
        return s
    norm = normalize_terminal(s)
    lines = norm.split("\n")
    if tokens(norm) > th["json_trim_tokens"]:
        keep = select_lines(lines, th["spill_head"], th["spill_tail"], th["error_context"])
        target = spill_path(spill_dir, norm)
        candidate = (
            f"{render_selection(lines, keep)}\n[lightfoot: this value trimmed to {len(keep)} of {len(lines)} lines; "
            f"the head, the tail and every error line are kept. Full text: {target} (read it with offsets).]"
        )
        if tokens(candidate) <= 0.75 * tokens(s):
            write_spill(target, norm)
            saved.append(norm_path(str(target)))
            return candidate
    denoised = "\n".join(denoise_lines(lines))
    return denoised if denoised.strip() and len(denoised) < len(s) else s


def trim_json_strings(text: str, th: dict, spill_dir: Path) -> tuple[str, str | None] | None:
    """(new text, first spill file) for one line of JSON whose long string values can be condensed.

    Only string values change, and the rest is re-encoded byte for byte: output whose
    encoding can't be reproduced exactly is left alone.
    """
    body = text.strip()
    try:
        value = json.loads(body)
    except ValueError:
        return None
    options = _json_format(value, body)
    if options is None:
        return None
    saved: list[str] = []

    def walk(v):
        if isinstance(v, str):
            return _trim_string(v, th, spill_dir, saved)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    new = json.dumps(walk(value), **options)
    if len(new) >= len(body):
        return None
    lead = text[: len(text) - len(text.lstrip())]
    return f"{lead}{new}{text[len(lead) + len(body) :]}", (saved[0] if saved else None)


def json_trim_ok(before: str, after: str) -> bool:
    """The json_trim promise: the same JSON except for shorter strings that keep every error line."""
    try:
        return _strings_condensed(json.loads(before), json.loads(after))
    except ValueError:
        return False


def _strings_condensed(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        if a == b:
            return True
        if len(b) >= len(a):
            return False
        kept = {line.strip() for line in b.split("\n")}
        lines = normalize_terminal(a).split("\n")
        return all(not lines[i].strip() or lines[i].strip() in kept for i in error_line_indices(lines))
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_strings_condensed(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        return list(a) == list(b) and all(_strings_condensed(a[k], b[k]) for k in a)
    return a == b


# --- ingest rules (json, json_trim, noise, spill) ------------------------------------


def ingest(result: Result, cfg: dict, ctx) -> tuple[str, str, str | None] | None:
    """(new_text, rule, path) for a fresh tool output, or None to leave it as is."""
    rules, th = cfg["rules"], cfg["thresholds"]
    text = result.orig_text or ""

    # Pretty-printed JSON (MCP servers, API responses) loses only its indentation. When even the
    # minified form is too large, the line-based spill below is more useful, so it runs instead.
    if rules.get("json") and not excluded(cfg, "json", result):
        minified = minify_json(text)
        if (
            minified is not None
            and tokens(minified) <= th["spill_tokens"]
            and tokens(text) - tokens(minified) >= th["json_min_saving_tokens"]
        ):
            return minified, "json", None

    norm = normalize_terminal(text)
    lines = norm.split("\n")

    # One line of JSON (Cline's tool results, compact MCP output): condense the long strings
    # inside it, since trimming the line itself would keep only its start.
    if json_line(lines):
        if rules.get("json_trim") and not excluded(cfg, "json_trim", result):
            hit = trim_json_strings(text, th, ctx.spill_dir)
            if hit is not None:
                return hit[0], "json_trim", hit[1]
        return None

    if rules.get("spill") and not excluded(cfg, "spill", result) and tokens(norm) > th["spill_tokens"]:
        keep = select_lines(lines, th["spill_head"], th["spill_tail"], th["error_context"])
        trimmed = render_selection(lines, keep)
        target = spill_path(ctx.spill_dir, norm)
        note = (
            f"[lightfoot: output trimmed to {len(keep)} of {len(lines)} lines; the head, the tail and every "
            f"error line are kept. Full output: {target} (read it with offsets for omitted lines).]"
        )
        if ctx.graphify_report and result.kind == "search":
            note += graph_note(ctx)
        candidate = f"{trimmed}\n{note}"
        if tokens(candidate) <= 0.75 * tokens(text):
            write_spill(target, norm)
            return candidate, "spill", norm_path(str(target))

    if rules.get("noise") and not excluded(cfg, "noise", result):
        candidate = "\n".join(denoise_lines(lines))
        if candidate != text and candidate.strip() and len(candidate) < len(text):
            return candidate, "noise", None
    return None
