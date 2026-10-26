"""A lightfoot segment for Claude Code's status line.

`lightfoot status-line install` points Claude Code's `statusLine` setting at
`lightfoot status-line`. If you already had a status line, its command is remembered and
still runs: its output is printed first and the lightfoot segment is added to its last line,
so nothing you had disappears. `lightfoot status-line uninstall` puts your command back.

The segment covers this Claude Code run: `lightfoot claude` gives each run its own base
URL (`/l/<launch>`), which the status line inherits in ANTHROPIC_BASE_URL. It reads the
database read-only and imports nothing heavy, because Claude Code re-runs it often.

For Pro and Max subscribers Claude Code passes the 5-hour and 7-day limit use
(`rate_limits`). It is recorded per launch, only when it changes, so the segment and
`lightfoot stats` can show the saving as a share of the limit.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import pricing
from .config import lightfoot_home, load_config, private_dir

LAUNCH_RE = re.compile(r"/l/([A-Za-z0-9_-]+)(?:/|$)")
MARK = "lightfoot status-line"
WRAPPED_TIMEOUT_S = 2.0
LIMIT_WINDOWS = ("five_hour", "seven_day")
NUDGE_SHARE = 10  # % projected saving before the shadow segment suggests going live
WINDOW_LIMIT_SQL = (
    "SELECT ts, used_pct FROM limits WHERE launch_id = ? AND window = ? AND resets_at = "
    "(SELECT resets_at FROM limits WHERE launch_id = ? AND window = ? ORDER BY ts DESC LIMIT 1) ORDER BY ts"
)


def settings_path() -> Path:
    """Claude Code's user settings file, where the status line command goes."""
    return Path.home() / ".claude" / "settings.json"


def state_path() -> Path:
    """Where the status line command lightfoot wraps is remembered."""
    return lightfoot_home() / "statusline.json"


def our_command() -> str:
    """The status line command: this Python running the status line module."""
    return f'"{Path(sys.executable).as_posix()}" -m {MARK}'


def read_only_uri(db: Path) -> str:
    """SQLite URI that opens `db` read-only. Built by hand: Path.as_uri() imports urllib.request
    on Windows, about 35 ms on every status line refresh."""
    path = db.resolve().as_posix()
    for char, escaped in (("%", "%25"), ("?", "%3F"), ("#", "%23"), (" ", "%20")):
        path = path.replace(char, escaped)
    return f"file://{'' if path.startswith('/') else '/'}{path}?mode=ro"


def launch_id(env: dict | None = None) -> str | None:
    """The launch this Claude Code run belongs to, read from the `/l/<launch>` path in `ANTHROPIC_BASE_URL`."""
    match = LAUNCH_RE.search((env if env is not None else os.environ).get("ANTHROPIC_BASE_URL", ""))
    return match.group(1) if match else None


def record_limits(launch: str | None, data: object, db: Path | None = None, now: float | None = None) -> None:
    """Record this launch's subscription limit use when it changed since the last run.

    A plain sqlite3 write, and only on a change: Claude Code re-runs the status line often.
    """
    if not launch or not isinstance(data, dict) or not isinstance(data.get("rate_limits"), dict):
        return
    values = []
    for window in LIMIT_WINDOWS:
        v = data["rate_limits"].get(window)
        if isinstance(v, dict) and isinstance(v.get("used_percentage"), (int, float)):
            values.append((window, float(v["used_percentage"]), int(v.get("resets_at") or 0)))
    db = db or lightfoot_home() / "lightfoot.db"
    if not values or not db.exists():
        return
    try:
        conn = sqlite3.connect(str(db), timeout=0.5)
    except sqlite3.Error:
        return
    try:
        for window, pct, resets in values:
            last = conn.execute(
                "SELECT used_pct, resets_at FROM limits WHERE launch_id = ? AND window = ? ORDER BY ts DESC LIMIT 1",
                (launch, window),
            ).fetchone()
            if last != (pct, resets):
                conn.execute(
                    "INSERT INTO limits(ts, launch_id, window, used_pct, resets_at) VALUES (?, ?, ?, ?, ?)",
                    (now or time.time(), launch, window, pct, resets),
                )
        conn.commit()
    except sqlite3.Error:  # an older schema without the table, or a busy database: skip this run
        pass
    finally:
        conn.close()


def limit_use(points: list) -> tuple[float, float] | None:
    """(first ts, percentage points used) over one limit window's (ts, used_pct) records, oldest first."""
    if len(points) < 2:
        return None
    return float(points[0][0]), max(0.0, float(points[-1][1]) - float(points[0][1]))


def limit_saved(used: float, cost: float, saved: float) -> float:
    """Percentage points of a limit the saving is worth, assuming limit use follows API-priced cost.

    Without lightfoot the same work would have cost cost + saved, so it would have used
    used × (cost + saved) / cost of the limit.
    """
    return used * saved / cost if cost > 0 else 0.0


def segment(launch: str | None, db: Path | None = None, subscription: bool = False) -> str:
    """The lightfoot text for one launch, or "" when this Claude Code run isn't using lightfoot."""
    if not launch:
        return ""
    db = db or lightfoot_home() / "lightfoot.db"
    if not db.exists():
        return "lightfoot ready"
    try:
        conn = sqlite3.connect(read_only_uri(db), uri=True, timeout=0.5)
    except sqlite3.Error:
        return ""
    try:
        conn.row_factory = sqlite3.Row
        switch = conn.execute("SELECT value FROM state WHERE key = 'switch'").fetchone()
        rows = conn.execute(
            "SELECT ts, model, input, cw5, cw1h, cr, output, saved_tokens, mode FROM ledger "
            "WHERE launch_id = ? AND status = 200",
            (launch,),
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return ""
    try:
        window = limit_use(conn.execute(WINDOW_LIMIT_SQL, (launch, "five_hour", launch, "five_hour")).fetchall())
    except sqlite3.Error:  # an older schema without the limits table
        window = None
    conn.close()
    subscription = subscription or window is not None  # only subscribers get rate_limits
    if switch and switch["value"] in ("off", "hard"):
        return "lightfoot off"
    if not rows:
        return "lightfoot ready"
    cost = saved = w_cost = w_saved = 0.0
    shadow = True
    since = window[0] if window else None
    for r in rows:
        c, s = pricing.row_costs(r)
        cost, saved = cost + c, saved + s
        if since is not None and r["ts"] >= since:
            w_cost, w_saved = w_cost + c, w_saved + s
        shadow = shadow and r["mode"] == "shadow"
    share = 100 * saved / (saved + cost) if saved + cost > 0 else 0.0
    limit = ""
    if window and window[1] > 0:
        points = limit_saved(window[1], w_cost, w_saved)
        if points >= 0.05:
            limit = f" · ≈{points:.1f}% of 5h limit"
    if shadow:
        # Once the saving is clear, name the command that makes it real (it applies to new launches).
        nudge = " · `lightfoot shadow off` to save" if share >= NUDGE_SHARE else ""
        return f"lightfoot shadow · would save {share:.0f}%" + limit + nudge
    if saved <= 0:
        return "lightfoot · nothing to condense yet"
    # Subscription limits track usage, so the percentage is the meaningful number there.
    if subscription:
        return f"lightfoot −{share:.0f}% usage" + (f"{limit} saved" if limit else "")
    return f"lightfoot −{share:.0f}% · ${saved:,.2f} saved"


def _wrapped() -> str | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    command = data.get("command") if isinstance(data, dict) else None
    return command if isinstance(command, str) and command.strip() else None


def render(stdin_text: str) -> str:
    """Full status line output: the wrapped command's lines, with the lightfoot segment added."""
    cfg = load_config()
    pricing.configure(cfg.get("prices"))
    launch = launch_id()
    try:
        record_limits(launch, json.loads(stdin_text))
    except ValueError:
        pass
    ours = segment(launch, subscription=cfg.get("billing") == "subscription")
    previous = ""
    command = _wrapped()
    if command:
        import shutil  # deferred, like subprocess: most status lines wrap no command
        import subprocess

        # Claude Code runs status line commands with bash (Git Bash on Windows), so `~` and
        # bash syntax in the user's command must keep working; cmd.exe is the last resort.
        bash = shutil.which("bash")
        argv = [bash, "-c", command] if bash else command
        try:
            # The user's own status line command, exactly as they configured it in Claude Code.
            done = subprocess.run(  # nosec B602
                argv,
                shell=bash is None,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=WRAPPED_TIMEOUT_S,
                encoding="utf-8",
                errors="replace",
            )
            previous = done.stdout.rstrip("\n")
        except (OSError, subprocess.SubprocessError):
            previous = ""
    if not ours:
        return previous
    if not previous:
        return ours
    lines = previous.split("\n")
    lines[-1] = f"{lines[-1]}  {ours}"
    return "\n".join(lines)


def _load_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_settings(path: Path, settings: dict) -> None:
    import shutil

    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.lightfoot-backup-{int(time.time())}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def is_installed() -> bool:
    """Whether Claude Code's status line runs Lightfoot's segment."""
    try:
        line = _load_settings(settings_path()).get("statusLine") or {}
    except (OSError, ValueError):
        return False
    return isinstance(line, dict) and MARK in str(line.get("command", ""))


def install() -> str:
    """Point Claude Code's status line at lightfoot, keeping any existing command running inside it."""
    path = settings_path()
    settings = _load_settings(path)
    current = settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else {}
    command = str(current.get("command") or "")
    if MARK not in command:
        private_dir(lightfoot_home())
        state_path().write_text(json.dumps({"command": command, "original": current}, indent=2), encoding="utf-8")
    line = dict(current)
    line.update({"type": "command", "command": our_command()})
    settings["statusLine"] = line
    _write_settings(path, settings)
    return str(path)


def uninstall() -> bool:
    """Restore the status line as it was before `install`. False if lightfoot wasn't installed."""
    if not is_installed():
        return False
    path = settings_path()
    settings = _load_settings(path)
    try:
        original = json.loads(state_path().read_text(encoding="utf-8")).get("original")
    except (OSError, ValueError):
        original = None
    if isinstance(original, dict) and original.get("command"):
        settings["statusLine"] = original
    else:
        settings.pop("statusLine", None)
    _write_settings(path, settings)
    try:
        state_path().unlink()
    except OSError:
        pass
    return True


def main(stdin_bytes: bytes) -> bytes:
    """Entry point for `lightfoot status-line`: Claude Code's JSON in, UTF-8 text out."""
    return render(stdin_bytes.decode("utf-8", errors="replace")).encode("utf-8")
