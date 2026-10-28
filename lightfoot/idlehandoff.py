"""Obsidian handoff for agents without a session-end hook: written once a session goes idle.

Claude Code has a SessionEnd hook (`handoff.py`). Other agents don't, so the proxy keeps a
reference to each such session's latest request body, in memory only, and when the
session has been idle for `idle_handoff_minutes` it condenses that body into a log and
starts the same background worker. The worker runs the user's own `handoff_command`
(for example `claude -p --model haiku`): lightfoot never makes the summary request
itself and never holds a credential for it. Without a vault or a command, nothing runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from .config import lightfoot_home, private_dir
from .handoff import GUARD_ENV, MAX_LOG_CHARS
from .profiles import PROFILES

MAX_PENDING = 20  # bodies held at once; the oldest is dropped first


@dataclass
class Pending:
    """The latest request of a session waiting for its idle handoff, with what is needed to summarize it."""

    body: dict
    adapter: str
    profile: str
    cwd: str
    last: float
    minutes: float


def enabled(cfg: dict) -> float:
    """Idle minutes before a handoff, or 0 when the idle handoff is off."""
    settings = cfg.get("integrations") or {}
    if not str(settings.get("handoff_command") or "").strip():
        return 0.0
    try:
        return max(0.0, float(settings.get("idle_handoff_minutes", 30)))
    except (TypeError, ValueError):
        return 0.0


class IdleHandoffs:
    """Sessions waiting for their idle handoff, newest request last."""

    def __init__(self):
        self._pending: dict[str, Pending] = {}

    def note(self, session_id: str, body: dict, adapter: str, profile: str, cwd: str, minutes: float, now: float):
        """Remember a session's latest request, keeping at most `MAX_PENDING` sessions (the oldest go first)."""
        self._pending.pop(session_id, None)
        self._pending[session_id] = Pending(body, adapter, profile, cwd, now, minutes)
        while len(self._pending) > MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))

    def due(self, now: float) -> list[tuple[str, Pending]]:
        """Sessions idle long enough, removed from the waiting list: each gets one note per idle gap."""
        ready = [(sid, p) for sid, p in self._pending.items() if now - p.last >= p.minutes * 60]
        for sid, _ in ready:
            del self._pending[sid]
        return ready


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(c.get("text", ""))
            for c in content
            if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def _calls(body: dict, adapter: str):
    """(speaker, text), ("tool", (name, input)) and ("turn", None) items of a request, in order.

    "turn" marks the start of each model response, for each wire format.
    """
    if adapter == "anthropic":
        for m in body.get("messages") or []:
            if not isinstance(m, dict):
                continue
            speaker = "USER" if m.get("role") == "user" else "AGENT"
            if speaker == "AGENT":
                yield "turn", None
            blocks = m.get("content")
            if isinstance(blocks, str):
                yield speaker, blocks
                continue
            for b in blocks if isinstance(blocks, list) else []:
                if isinstance(b, dict) and b.get("type") == "text":
                    yield speaker, str(b.get("text", ""))
                elif isinstance(b, dict) and b.get("type") == "tool_use":
                    yield "tool", (str(b.get("name", "")), b.get("input") if isinstance(b.get("input"), dict) else {})
    elif adapter == "responses":
        in_response = False  # one response's items (reasoning, message, calls) arrive together
        for item in body.get("input") or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type", "message")
            from_model = kind in ("reasoning", "function_call") or (
                kind == "message" and item.get("role") == "assistant"
            )
            if from_model and not in_response:
                yield "turn", None
            in_response = from_model
            if kind == "message" and item.get("role") in ("user", "assistant"):
                yield ("USER" if item["role"] == "user" else "AGENT"), _text(item.get("content"))
            elif item.get("type") == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except ValueError:
                    args = {}
                yield "tool", (str(item.get("name", "")), args if isinstance(args, dict) else {})
    elif adapter == "gemini":
        for c in body.get("contents") or []:
            if not isinstance(c, dict):
                continue
            speaker = "AGENT" if c.get("role") == "model" else "USER"
            if speaker == "AGENT":
                yield "turn", None
            for part in c.get("parts") or []:
                if not isinstance(part, dict) or part.get("thought"):
                    continue
                if isinstance(part.get("text"), str):
                    yield speaker, part["text"]
                elif isinstance(part.get("functionCall"), dict):
                    call = part["functionCall"]
                    yield (
                        "tool",
                        (str(call.get("name", "")), call.get("args") if isinstance(call.get("args"), dict) else {}),
                    )


def condense_request(body: dict, adapter: str, profile_name: str) -> tuple[str, int, list[str]]:
    """(condensed log, agent turns, files edited) from one request's full history, like handoff.condense."""
    profile = PROFILES.get(profile_name) or PROFILES["generic"]
    lines: list[str] = []
    touched: list[str] = []
    turns = 0
    for speaker, value in _calls(body, adapter):
        if speaker == "turn":
            turns += 1
        elif speaker == "tool":
            name, inp = value
            kind = profile.kind(name, inp)
            path = profile.path(name, inp)
            target = path or (profile.command(name, inp) if kind == "shell" else "") or profile.search_text(name, inp)
            lines.append(f"TOOL {name}: {str(target)[:200]}")
            if kind == "edit" and path and path not in touched:
                touched.append(path)
        elif value.strip():
            lines.append(f"{speaker}: {value[: 2000 if speaker == 'USER' else 1200]}")
    text = "\n".join(lines)
    if len(text) > MAX_LOG_CHARS:
        head = MAX_LOG_CHARS // 5
        text = text[:head] + "\n[… middle of the session omitted …]\n" + text[-(MAX_LOG_CHARS - head) :]
    return text, turns, touched


def start(session_id: str, pending: Pending, min_turns: int) -> bool:
    """Condense the session and start the detached handoff worker. False when the session is too short."""
    log_text, turns, touched = condense_request(pending.body, pending.adapter, pending.profile)
    if turns < min_turns:
        return False
    folder = private_dir(lightfoot_home() / "handoff")
    log = folder / f"{session_id[:32]}-{int(time.time())}.json"
    log.write_text(json.dumps({"log": log_text, "touched": touched}), encoding="utf-8")
    kwargs: dict = {}
    if os.name == "nt":
        # A hidden console, not none: a venv's python.exe is a launcher, and a console program started
        # without one gets a new, visible console window. CREATE_NO_WINDOW keeps it hidden for both.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = os.environ.copy()
    env[GUARD_ENV] = "1"
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lightfoot",
            "handoff",
            "worker",
            "--log",
            str(log),
            "--cwd",
            pending.cwd,
            "--session",
            session_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=env,
        **kwargs,
    )
    return True
