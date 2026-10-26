"""Retention: delete stored conversation content of inactive sessions.

What is deleted after `retention_days` of inactivity: stored rewrites (they contain
tool output), spill files, saved images, events, and session rows. The cost ledger holds only token
counts and is kept for `ledger_retention_days`.

A rewrite is kept while any session that used it is still active, and a spill file is
kept while any remaining rewrite points at it, so a running conversation never loses
what it replays.
"""

from __future__ import annotations

import time
from pathlib import Path

from .config import lightfoot_home
from .store import Store
from .text import norm_path


def spill_dirs(store: Store) -> list[Path]:
    """Folders holding spill files (*.log) and pruned-history files (*.md)."""
    dirs = {lightfoot_home() / "spill", lightfoot_home() / "history"}
    for r in store.query("SELECT DISTINCT cwd FROM launches WHERE cwd IS NOT NULL"):
        dirs.add(Path(r["cwd"]) / ".lightfoot" / "spill")
        dirs.add(Path(r["cwd"]) / ".lightfoot" / "history")
    return sorted(d for d in dirs if d.is_dir())


def image_dirs(store: Store) -> list[Path]:
    """Folders holding images saved by the images rule and by pruned-history files."""
    dirs = {lightfoot_home() / "images"}
    for r in store.query("SELECT DISTINCT cwd FROM launches WHERE cwd IS NOT NULL"):
        dirs.add(Path(r["cwd"]) / ".lightfoot" / "images")
    return sorted(d for d in dirs if d.is_dir())


def cleanup(
    store: Store, cfg: dict, days: float | None = None, dry_run: bool = False, now: float | None = None
) -> dict:
    """Delete stored content of sessions idle longer than `retention_days`.

    Never removes anything a session that is still active replays, since dropping a frozen
    rewrite would change history the model has already seen.
    """
    now = now or time.time()
    days = float(days if days is not None else cfg.get("retention_days", 30))
    cutoff = now - days * 86400
    ledger_cutoff = now - float(cfg.get("ledger_retention_days", 365)) * 86400

    active = {
        r["id"] for r in store.query("SELECT id FROM sessions WHERE COALESCE(last_request, first_seen) >= ?", (cutoff,))
    }
    old_rewrites = store.query("SELECT tool_use_id, live, session_id FROM rewrites WHERE created < ?", (cutoff,))
    doomed = [(r["tool_use_id"], r["live"]) for r in old_rewrites if r["session_id"] not in active]

    doomed_set = set(doomed)
    kept_paths = {
        r["path"]
        for r in store.query("SELECT tool_use_id, live, path FROM rewrites WHERE path IS NOT NULL")
        if (r["tool_use_id"], r["live"]) not in doomed_set
    }
    stale_prunes = [
        (r["session_id"], r["live"], r["history_path"])
        for r in store.query("SELECT * FROM prunes")
        if r["session_id"] not in active
    ]
    stale_prune_keys = {(s, live) for s, live, _ in stale_prunes}
    kept_paths |= {
        r["history_path"]
        for r in store.query("SELECT * FROM prunes WHERE history_path IS NOT NULL")
        if (r["session_id"], r["live"]) not in stale_prune_keys
    }
    spill = []
    kept_histories = []  # real paths: kept_paths are lower-cased, which a case-sensitive file system can't open
    for directory in spill_dirs(store):
        for f in [*directory.glob("*.log"), *directory.glob("*.md")]:
            try:
                if norm_path(str(f)) in kept_paths:
                    if f.suffix == ".md":
                        kept_histories.append(f)
                elif f.stat().st_mtime < cutoff:
                    spill.append(f)
            except OSError:
                continue
    # Saved images: one image stub or history file can name several, so a file is kept while
    # any kept stub or kept history file still mentions it.
    kept_text = [
        r["text"] or ""
        for r in store.query("SELECT tool_use_id, live, text FROM rewrites WHERE rule = 'images'")
        if (r["tool_use_id"], r["live"]) not in doomed_set
    ]
    for history in kept_histories:
        try:
            kept_text.append(history.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    mentioned = "\n".join(kept_text)
    for directory in image_dirs(store):
        for f in directory.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff and f.name not in mentioned:
                    spill.append(f)
            except OSError:
                continue

    counts = {
        "rewrites": len(doomed),
        "prunes": len(stale_prunes),
        "spill_files": len(spill),
        "spill_bytes": sum(f.stat().st_size for f in spill if f.exists()),
        "sessions": store.query(
            "SELECT COUNT(*) AS n FROM sessions WHERE COALESCE(last_request, first_seen) < ?", (cutoff,)
        )[0]["n"],
        "events": store.query("SELECT COUNT(*) AS n FROM events WHERE ts < ?", (cutoff,))[0]["n"],
        "ledger_rows": store.query("SELECT COUNT(*) AS n FROM ledger WHERE ts < ?", (ledger_cutoff,))[0]["n"],
    }
    if dry_run:
        return counts

    store.delete_rewrites(doomed)
    for session_id, live, _ in stale_prunes:
        store.execute("DELETE FROM prunes WHERE session_id=? AND live=?", (session_id, live))
    for f in spill:
        try:
            f.unlink()
        except OSError:
            counts["spill_files"] -= 1
    store.execute("DELETE FROM sessions WHERE COALESCE(last_request, first_seen) < ?", (cutoff,))
    store.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    store.execute("DELETE FROM signals WHERE ts < ?", (ledger_cutoff,))
    store.execute("DELETE FROM ledger WHERE ts < ?", (ledger_cutoff,))
    store.execute("DELETE FROM limits WHERE ts < ?", (ledger_cutoff,))
    store.execute("DELETE FROM prefixes WHERE ts < ?", (ledger_cutoff,))
    store.execute("DELETE FROM tools_used WHERE session_id NOT IN (SELECT session_id FROM prefixes)")
    store.execute(
        "DELETE FROM launches WHERE created < ? AND id NOT IN (SELECT DISTINCT launch_id FROM ledger "
        "WHERE launch_id IS NOT NULL)",
        (cutoff,),
    )
    store.execute("DELETE FROM state WHERE key LIKE 'capture:%' AND substr(key, 9) NOT IN (SELECT id FROM launches)")
    store.set_state("last_prune", str(now))
    return counts
