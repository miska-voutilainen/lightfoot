"""`lightfoot integrate`: route every Claude Code session through the proxy, IDE and desktop app included.

`lightfoot claude` is a wrapper, so sessions started any other way (the VS Code extension,
the desktop app, plain `claude`) bypass the proxy. `integrate on` sets ANTHROPIC_BASE_URL
in ~/.claude/settings.json, which every Claude Code surface reads.

A dead proxy would then fail every request, so `integrate on` also adds a SessionStart and a
UserPromptSubmit hook that run `lightfoot integrate check`: it starts the proxy if it isn't
running, prints nothing when all is well (hook output on these events reaches Claude's
context), and otherwise shows a warning without blocking the prompt.

Sessions routed this way use the global settings and ~/.lightfoot/ for saved files;
`lightfoot claude` still gives each project its own launch, through `--settings`, which
takes precedence over the user settings file.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from .config import load_config
from .launch import base_url, running, start_daemon

MARK = "lightfoot integrate check"
EVENTS = ("SessionStart", "UserPromptSubmit")


class IntegrateError(Exception):
    """Routing every surface would override a base URL the user already set."""

    pass


def settings_path() -> Path:
    """Claude Code's user settings file."""
    return Path.home() / ".claude" / "settings.json"


def check_command() -> str:
    """The command the session hooks run: `lightfoot integrate check` through this Python."""
    return f'"{Path(sys.executable).as_posix()}" -m {MARK}'


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, settings: dict) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.lightfoot-backup-{int(time.time())}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _ours(url: str | None, port: int) -> bool:
    return bool(url) and str(url).rstrip("/") in (base_url(port), f"http://localhost:{port}")


def _without_mark(entries: list) -> list:
    return [
        e
        for e in entries
        if not any(MARK in str(h.get("command", "")) for h in e.get("hooks", []) if isinstance(h, dict))
    ]


def is_on(port: int | None = None) -> bool:
    """Whether `~/.claude/settings.json` points Claude Code at this proxy."""
    port = port or int(load_config()["port"])
    try:
        env = _load(settings_path()).get("env") or {}
    except (OSError, ValueError):
        return False
    return _ours(env.get("ANTHROPIC_BASE_URL"), port)


def on(port: int) -> str:
    """Point Claude Code's settings at the proxy and add the hooks that start it when needed. Returns the settings path.

    Refuses when the settings already name another base URL, which would otherwise be lost.
    """
    path = settings_path()
    settings = _load(path)
    env = settings.setdefault("env", {})
    current = env.get("ANTHROPIC_BASE_URL")
    if current and not _ours(current, port):
        raise IntegrateError(
            f"~/.claude/settings.json already sends Claude Code to {current}. To keep using it behind "
            f"lightfoot, run `lightfoot config set upstream {current}`, remove ANTHROPIC_BASE_URL from that "
            "file, then run `lightfoot integrate on` again."
        )
    start_daemon(port)  # fail here, before any session depends on it
    env["ANTHROPIC_BASE_URL"] = base_url(port)
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        entries = _without_mark(hooks.get(event) or [])
        entries.insert(0, {"hooks": [{"type": "command", "command": check_command(), "timeout": 15}]})
        hooks[event] = entries
    _write(path, settings)
    return str(path)


def off(port: int) -> bool:
    """Remove the setting and the hooks. False when integrate wasn't on."""
    path = settings_path()
    settings = _load(path)
    env = settings.get("env") or {}
    hooks = settings.get("hooks") or {}
    had_hooks = any(len(_without_mark(hooks.get(e) or [])) != len(hooks.get(e) or []) for e in EVENTS)
    if not _ours(env.get("ANTHROPIC_BASE_URL"), port) and not had_hooks:
        return False
    if _ours(env.get("ANTHROPIC_BASE_URL"), port):
        env.pop("ANTHROPIC_BASE_URL")
    if not env:
        settings.pop("env", None)
    for event in EVENTS:
        kept = _without_mark(hooks.get(event) or [])
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    _write(path, settings)
    return True


def check(port: int) -> str:
    """Hook output: "" when the proxy runs (or was just started), else a JSON warning for the user."""
    if running(port):
        return ""
    try:
        start_daemon(port)
        return ""
    except RuntimeError as exc:
        return json.dumps(
            {
                "systemMessage": f"lightfoot: the proxy is not running and could not be started ({exc}). "
                "Requests from this session will fail; run `lightfoot integrate off` and restart Claude Code."
            }
        )
