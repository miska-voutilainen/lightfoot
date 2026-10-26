"""Hook mode: the ingest rules as a Claude Code PostToolUse hook, without the proxy.

A PostToolUse hook may return `updatedToolOutput`, and Claude Code then sends that
instead of the tool's real output. It also stores the replacement in its transcript, so
`--resume` re-sends exactly the same bytes and the prompt cache keeps working: the
rewrite is frozen by Claude Code itself. (Verified against Claude Code 2.1.191 with a local
mock API: scripts/drift_check.py has the harness.)

That makes the ingest rules (json, json_trim, spill, noise) usable where a proxy isn't: Amazon
Bedrock, Google Vertex, company policies against local proxies. History edits (epochs,
old-turn pruning) still need the proxy, because a hook only sees new output.

Only Bash, PowerShell and MCP output are handled: that is where terminal noise and large
logs come from, and Claude Code's own Read, Grep and Edit outputs are already compact.
Any problem returns nothing, and Claude Code then uses the original output.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import lightfoot_home, load_config
from .text import error_line_indices, normalize_terminal, tokens

MARK = "lightfoot hook post-tool-use"
MATCHER = "Bash|PowerShell|mcp__.*"
SHELL_TOOLS = {"Bash": "shell", "PowerShell": "shell"}


class _Ctx:
    """The two fields of transform.Context that the ingest rules read."""

    def __init__(self, spill_dir: Path, graphify_report: str | None = None):
        self.spill_dir = spill_dir
        self.graphify_report = graphify_report


class _Output:
    """The three fields of adapters.base.Result that the ingest rules read."""

    def __init__(self, text: str, name: str, kind: str):
        self.orig_text = text
        self.name = name
        self.kind = kind


def _keeps_errors(before: str, after: str) -> bool:
    """The validator's promise, checked here too: every error line of the original survives."""
    kept = {line.strip() for line in after.split("\n")}
    lines = normalize_terminal(before).split("\n")
    return all(not lines[i].strip() or lines[i].strip() in kept for i in error_line_indices(lines))


def condense(text: str, tool: str, kind: str, cfg: dict, spill_dir: Path) -> tuple[str, str] | None:
    """(new text, rule) for one output, or None to leave it as it is."""
    from .ingest import ingest, json_trim_ok  # deferred: only needed when there is something to condense

    hit = ingest(_Output(text, tool, kind), cfg, _Ctx(spill_dir))
    if hit is None:
        return None
    new_text, rule, _ = hit
    if rule == "json_trim":
        return (new_text, rule) if json_trim_ok(text, new_text) else None
    if rule != "json" and not _keeps_errors(text, new_text):
        return None
    if rule == "json":
        try:
            if json.loads(new_text) != json.loads(text):
                return None
        except ValueError:
            return None
    return new_text, rule


def handle(payload: dict) -> tuple[dict | None, list[tuple[str, str, int, int]]]:
    """(hook output or None, [(tool, rule, tokens before, tokens after)]) for one PostToolUse call."""
    tool = str(payload.get("tool_name") or "")
    response = payload.get("tool_response")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        return None, []
    cfg = load_config(cwd)
    if not cfg.get("hooks", {}).get("enabled", True):
        return None, []
    spill_dir = Path(cwd) / ".lightfoot" / "spill"
    done: list[tuple[str, str, int, int]] = []

    if tool in SHELL_TOOLS and isinstance(response, dict):
        stdout = response.get("stdout")
        if response.get("isImage") or response.get("interrupted") or not isinstance(stdout, str) or not stdout:
            return None, []
        hit = condense(stdout, tool, SHELL_TOOLS[tool], cfg, spill_dir)
        if hit is None:
            return None, []
        updated = dict(response)  # every field kept; only stdout changes, stderr is never touched
        updated["stdout"] = hit[0]
        done.append((tool, hit[1], tokens(stdout), tokens(hit[0])))
        return _output(updated), done

    if tool.startswith("mcp__"):
        if isinstance(response, str) and response:
            hit = condense(response, tool, "other", cfg, spill_dir)
            if hit:
                done.append((tool, hit[1], tokens(response), tokens(hit[0])))
                return _output(hit[0]), done
        if isinstance(response, list) and len(response) == 1 and isinstance(response[0], dict):
            block = response[0]
            if block.get("type") == "text" and isinstance(block.get("text"), str) and set(block) <= {"type", "text"}:
                hit = condense(block["text"], tool, "other", cfg, spill_dir)
                if hit:
                    done.append((tool, hit[1], tokens(block["text"]), tokens(hit[0])))
                    return _output([{"type": "text", "text": hit[0]}]), done
    return None, []


def _output(value) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": value}}


def run(stdin_text: str) -> str:
    """Entry point for `lightfoot hook post-tool-use`: hook JSON in, hook JSON (or nothing) out."""
    try:
        payload = json.loads(stdin_text)
        output, done = handle(payload if isinstance(payload, dict) else {})
    except Exception:  # a hook must never break the tool call; Claude Code keeps the original output
        return ""
    if done:
        _record(payload, done)
    return json.dumps(output) if output else ""


RECORD_SQL = (
    "INSERT INTO hook_rewrites(ts, session_id, cwd, tool, rule, orig_tokens, new_tokens) VALUES(?, ?, ?, ?, ?, ?, ?)"
)


def _record(payload: dict, done: list) -> None:
    """Note what was condensed, so `lightfoot stats` can count hook-mode savings.

    A plain sqlite3 insert, because importing the store costs more than the hook's own work.
    Only a database without the table yet goes through Store, which creates the schema.
    """
    import sqlite3

    now, session, cwd = time.time(), str(payload.get("session_id") or ""), str(payload.get("cwd"))
    rows = [(now, session, cwd, tool, rule, before, after) for tool, rule, before, after in done]
    try:
        db = lightfoot_home() / "lightfoot.db"
        if db.exists():
            conn = sqlite3.connect(str(db), timeout=2)
            try:
                with conn:
                    conn.executemany(RECORD_SQL, rows)
                return
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    return  # busy or locked: skip this note rather than delay the tool call
                # an older schema without hook_rewrites: Store below migrates it
            finally:
                conn.close()
        from .store import Store

        store = Store()
        try:
            for row in rows:
                store.execute(RECORD_SQL, row)
        finally:
            store.close()
    except Exception:  # noqa: S110 - bookkeeping only; the rewrite itself already happened
        pass


# --- install into ~/.claude/settings.json ----------------------------------------------


def settings_path() -> Path:
    """Claude Code's user settings file, where the PostToolUse hook goes."""
    return Path.home() / ".claude" / "settings.json"


def hook_command() -> str:
    """The hook's command line: this Python running the hook module."""
    return f'"{Path(sys.executable).as_posix()}" -m {MARK}'


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, settings: dict) -> None:
    import shutil  # deferred: the hook itself runs on every tool call and never needs it

    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.lightfoot-backup-{int(time.time())}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _ours(entry: dict) -> bool:
    return any(MARK in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def is_installed() -> bool:
    """Whether the PostToolUse hook is in Claude Code's settings."""
    try:
        entries = (_load(settings_path()).get("hooks") or {}).get("PostToolUse") or []
    except (OSError, ValueError):
        return False
    return any(_ours(e) for e in entries)


def install() -> str:
    """Add the PostToolUse hook for Bash, PowerShell and MCP tools. Returns the settings path."""
    path = settings_path()
    settings = _load(path)
    hooks = settings.setdefault("hooks", {})
    entries = [e for e in hooks.get("PostToolUse") or [] if not _ours(e)]
    entries.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]})
    hooks["PostToolUse"] = entries
    _write(path, settings)
    return str(path)


def uninstall() -> bool:
    """Remove the hook. False when it wasn't installed."""
    path = settings_path()
    settings = _load(path)
    hooks = settings.get("hooks") or {}
    entries = hooks.get("PostToolUse") or []
    kept = [e for e in entries if not _ours(e)]
    if len(kept) == len(entries):
        return False
    if kept:
        hooks["PostToolUse"] = kept
    else:
        hooks.pop("PostToolUse", None)
    if not hooks:
        settings.pop("hooks", None)
    _write(path, settings)
    return True
