"""Obsidian session handoff, written by a Claude Code SessionEnd hook.

SessionEnd (not Stop, which fires after every response) runs `lightfoot handoff hook`, which
returns immediately and starts a background worker. The worker condenses the
transcript, asks Haiku (through `claude -p`, so it uses your normal login) for a
structured note, and writes it into the vault. Without a detected vault it does nothing.

For agents without a session-end hook, the proxy starts the same worker with a condensed
log once a session goes idle (`idlehandoff.py`), and the worker runs `handoff_command`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import lightfoot_home, load_config, private_dir
from .integrations import detect

GUARD_ENV = "LIGHTFOOT_HANDOFF_WORKER"
HOOK_MARK = "lightfoot handoff hook"
MAX_LOG_CHARS = 80_000

PROMPT = """You are writing a handoff note so that a fresh Claude Code session can continue this work \
without the old context. Using the session log below, write Markdown with exactly these sections:

## Summary
Two to four sentences: the goal and where it stands.

## Done
What was completed, with file and function names.

## Decisions
Each decision with its reason.

## Open threads
What is unfinished or broken, and the next concrete steps.

Be specific: name files, functions, commands and error messages. No preamble, no closing remarks. \
Do not use any tools.

<session-log>
{log}
</session-log>
"""


def settings_path() -> Path:
    """Claude Code's user settings file, where the SessionEnd hook goes."""
    return Path.home() / ".claude" / "settings.json"


def hook_command() -> str:
    """The hook's command line: this Python running the handoff module."""
    return f'"{Path(sys.executable).as_posix()}" -m {HOOK_MARK}'


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _commands(entry: dict) -> list[str]:
    return [h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)]


def hook_status() -> dict:
    """Whether the hook is installed, and whether it runs after a Graphify hook (it should run first)."""
    try:
        settings = _load_settings(settings_path())
    except (OSError, ValueError):
        return {"installed": False, "after_graphify": False}
    entries = (settings.get("hooks") or {}).get("SessionEnd") or []
    ours = [i for i, e in enumerate(entries) if any(HOOK_MARK in c for c in _commands(e))]
    graphify = [i for i, e in enumerate(entries) if any("graphify" in c.lower() for c in _commands(e))]
    return {"installed": bool(ours), "after_graphify": bool(ours and graphify and min(ours) > min(graphify))}


def install() -> str:
    """Add the SessionEnd hook first in the chain, after backing up the settings file. Returns its path."""
    path = settings_path()
    settings = _load_settings(path)
    if path.exists():
        backup = path.with_name(f"settings.json.lightfoot-backup-{int(time.time())}")
        shutil.copy2(path, backup)
    hooks = settings.setdefault("hooks", {})
    entries = [e for e in hooks.get("SessionEnd") or [] if not any(HOOK_MARK in c for c in _commands(e))]
    # First in the chain, so anything that refreshes the vault afterwards sees the new note.
    entries.insert(0, {"hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]})
    hooks["SessionEnd"] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return str(path)


def uninstall() -> bool:
    """Remove the hook, after backing up the settings file. False when it wasn't installed."""
    path = settings_path()
    settings = _load_settings(path)
    hooks = settings.get("hooks") or {}
    entries = hooks.get("SessionEnd") or []
    kept = [e for e in entries if not any(HOOK_MARK in c for c in _commands(e))]
    if len(kept) == len(entries):
        return False
    shutil.copy2(path, path.with_name(f"settings.json.lightfoot-backup-{int(time.time())}"))
    if kept:
        hooks["SessionEnd"] = kept
    else:
        hooks.pop("SessionEnd", None)
    if not hooks:
        settings.pop("hooks", None)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def _log(message: str) -> None:
    try:
        private_dir(lightfoot_home())
        with open(lightfoot_home() / "handoff.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def run_hook(stdin_text: str) -> int:
    """Entry point of the SessionEnd hook. Never fails the session: always exits 0."""
    if os.environ.get(GUARD_ENV):
        return 0
    try:
        data = json.loads(stdin_text or "{}")
        cwd = data.get("cwd") or os.getcwd()
        transcript = data.get("transcript_path")
        if not transcript or not Path(transcript).is_file():
            return 0
        if not detect(cwd, load_config(cwd)).vault:
            return 0
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
                "--transcript",
                transcript,
                "--cwd",
                cwd,
                "--session",
                str(data.get("session_id") or ""),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=env,
            **kwargs,
        )
    except Exception as exc:
        _log(f"hook error: {exc!r}")
    return 0


def condense(transcript: Path) -> tuple[str, int, list[str]]:
    """(condensed log, assistant turns, files touched) from a Claude Code transcript (JSONL)."""
    lines: list[str] = []
    turns: set[str] = set()
    touched: list[str] = []
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta"):
                continue
            kind = entry.get("type")
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            content = message.get("content")
            if kind == "user":
                if isinstance(content, str):
                    lines.append(f"USER: {content[:2000]}")
                elif isinstance(content, list):
                    lines.extend(
                        f"USER: {str(block.get('text', ''))[:2000]}"
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
            elif kind == "assistant":
                turns.add(str(message.get("id") or entry.get("uuid")))
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        lines.append(f"CLAUDE: {str(block.get('text', ''))[:1200]}")
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        target = (
                            inp.get("file_path")
                            or inp.get("notebook_path")
                            or inp.get("path")
                            or inp.get("pattern")
                            or inp.get("command")
                            or inp.get("description")
                            or ""
                        )
                        lines.append(f"TOOL {name}: {str(target)[:200]}")
                        path = inp.get("file_path") or inp.get("notebook_path")
                        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and path and path not in touched:
                            touched.append(path)
    text = "\n".join(lines)
    if len(text) > MAX_LOG_CHARS:
        head = MAX_LOG_CHARS // 5
        text = text[:head] + "\n[… middle of the session omitted …]\n" + text[-(MAX_LOG_CHARS - head) :]
    return text, len(turns), touched


def run_worker(transcript: str | None, cwd: str, session_id: str, log: str | None = None) -> int:
    """Write one session's handoff note: from a Claude Code transcript, or from an idle agent's request log.

    Returns 0 when nothing needed doing; a short session or a missing vault is not an error.
    """
    try:
        cfg = load_config(cwd)
        vault = detect(cwd, cfg).vault
        if not vault:
            return 0
        settings = cfg["integrations"]
        if log:
            return _summarize_log(Path(log), Path(vault), settings, cwd, session_id)
        log_text, turns, touched = condense(Path(transcript))
        if turns < int(settings.get("handoff_min_turns", 5)):
            _log(f"skipped {session_id}: {turns} turns")
            return 0
        claude = shutil.which("claude")
        if not claude:
            _log("claude not found on PATH")
            return 1
        env = os.environ.copy()
        env[GUARD_ENV] = "1"
        env.update(_proxy_env(cwd))
        result = subprocess.run(
            [
                claude,
                "-p",
                "--model",
                str(settings.get("handoff_model", "haiku")),
                "--no-session-persistence",
                "--tools",
                "",
                "--strict-mcp-config",
                "--output-format",
                "text",
            ],
            input=PROMPT.format(log=log_text),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=env,
            timeout=300,
        )
        summary = (result.stdout or "").strip()
        if result.returncode != 0 or not summary:
            _log(f"claude -p failed for {session_id}: rc={result.returncode} {result.stderr[-500:]!r}")
            return 1
        write_note(Path(vault), settings.get("handoff_folder", "Claude Handoffs"), cwd, session_id, summary, touched)
        return 0
    except Exception as exc:
        _log(f"worker error: {exc!r}")
        return 1


def _summarize_log(log: Path, vault: Path, settings: dict, cwd: str, session_id: str) -> int:
    """An idle handoff: the proxy's condensed log, summarized by the user's own handoff_command."""
    try:
        data = json.loads(log.read_text(encoding="utf-8"))
    finally:
        try:
            log.unlink()
        except OSError:
            pass
    command = str(settings.get("handoff_command") or "").strip()
    if not command:
        return 0
    env = os.environ.copy()
    env[GUARD_ENV] = "1"
    # The user's own summarizer command, exactly as configured; it reads the prompt on stdin.
    result = subprocess.run(  # noqa: S602  # nosec B602
        command,
        shell=True,
        input=PROMPT.replace("Claude Code session", "session").format(log=data.get("log", "")),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
        timeout=300,
    )
    summary = (result.stdout or "").strip()
    if result.returncode != 0 or not summary:
        _log(f"handoff_command failed for {session_id}: rc={result.returncode} {result.stderr[-500:]!r}")
        return 1
    write_note(
        vault, settings.get("handoff_folder", "Claude Handoffs"), cwd, session_id, summary, data.get("touched", [])
    )
    return 0


def _proxy_env(cwd: str) -> dict:
    """Route the summary call through the proxy (passthrough) so its cost lands in the ledger."""
    try:
        from .launch import base_url, resolve_upstream, running
        from .store import Launch, Store

        cfg = load_config(cwd)
        port = int(cfg["port"])
        if not running(port):
            return {}
        store = Store()
        try:
            # The hook inherits the session's environment; if that points at one of our
            # launches, reuse its upstream so a custom gateway keeps being honoured.
            inherited = os.environ.get("ANTHROPIC_BASE_URL", "")
            marker = f"{base_url(port)}/l/"
            parent = store.get_launch(inherited[len(marker) :].strip("/")) if inherited.startswith(marker) else None
            upstream = parent.upstream if parent else resolve_upstream(port, cfg)
            launch = Launch(
                uuid.uuid4().hex[:12], str(Path(cwd).resolve()), "passthrough", upstream, None, None, "handoff"
            )
            store.put_launch(launch)
        finally:
            store.close()
        return {"ANTHROPIC_BASE_URL": f"{base_url(port)}/l/{launch.id}"}
    except Exception as exc:
        _log(f"could not route the summary through the proxy: {exc!r}")
        return {}


def write_note(vault: Path, folder: str, cwd: str, session_id: str, summary: str, touched: list[str]) -> Path:
    """Write the note into `<vault>/<folder>/`, numbered when one of the same name exists. Returns its path."""
    now = datetime.now()
    project = Path(cwd).name or "project"
    target_dir = vault / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{now:%Y-%m-%d %H%M} {project} handoff.md"
    files = "\n".join(f"- `{p}`" for p in touched) or "- (none)"
    note = (
        f"---\ntype: handoff\nproject: {project}\ncwd: {json.dumps(cwd)}\nsession: {session_id}\n"
        f"date: {now.isoformat(timespec='minutes')}\n---\n\n# {project} handoff, {now:%Y-%m-%d %H:%M}\n\n"
        f"{summary}\n\n## Files touched\n{files}\n"
    )
    path = target_dir / name
    counter = 2
    while path.exists():
        path = target_dir / f"{now:%Y-%m-%d %H%M} {project} handoff {counter}.md"
        counter += 1
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(note, encoding="utf-8")
    tmp.replace(path)
    _log(f"wrote {path}")
    return path
