import json
import os
import sqlite3
import time

from lightfoot.maintenance import cleanup
from lightfoot.reports import report
from lightfoot.store import SCHEMA_VERSION, Launch, Rewrite, Store
from lightfoot.text import norm_path


def test_migrates_a_v1_database(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE launches (id TEXT PRIMARY KEY, cwd TEXT, mode TEXT, upstream TEXT, graphify_report TEXT,
                               vault TEXT, label TEXT, created REAL);
        CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, launch_id TEXT, session_id TEXT,
                             mode TEXT, model TEXT, status INTEGER, turn INTEGER, input INTEGER DEFAULT 0,
                             cw5 INTEGER DEFAULT 0, cw1h INTEGER DEFAULT 0, cr INTEGER DEFAULT 0,
                             output INTEGER DEFAULT 0, orig_tokens INTEGER DEFAULT 0, saved_tokens INTEGER DEFAULT 0,
                             rules TEXT, epoch INTEGER DEFAULT 0, attempt TEXT, latency_ms REAL);
        INSERT INTO launches VALUES ('old', '/x', 'shadow', NULL, NULL, NULL, NULL, 1.0);
    """)
    db.commit()
    db.close()
    store = Store(path)
    try:
        assert store.get_state("schema_version") == str(SCHEMA_VERSION)
        assert "floor_tokens" in {r["name"] for r in store.query("PRAGMA table_info(sessions)")}
        launch = store.get_launch("old")
        assert launch.agent == "claude" and launch.experimental is False
        store.add_ledger({"ts": 1.0, "agent": "codex", "client_version": "codex_cli_rs/1"})
    finally:
        store.close()
    Store(path).close()  # re-opening is a no-op


def _setup(store, tmp_path, now):
    spill_dir = tmp_path / "proj" / ".lightfoot" / "spill"
    spill_dir.mkdir(parents=True)
    store.put_launch(Launch("l1", str(tmp_path / "proj"), "live", None, None, None))
    old, active = now - 40 * 86400, now - 86400
    store.get_or_create_session("stale", "l1", "live", old)
    store._exec("UPDATE sessions SET last_request=? WHERE id='stale'", (old,))
    store.get_or_create_session("active", "l1", "live", old)
    store._exec("UPDATE sessions SET last_request=? WHERE id='active'", (active,))
    files = {}
    for name in ("stale", "active", "orphan"):
        f = spill_dir / f"{name}.log"
        f.write_text("x" * 100, encoding="utf-8")
        os.utime(f, (old, old))
        files[name] = f
    store.put_rewrites(
        [
            Rewrite("t-stale", True, "h", "x", "spill", None, "stale", norm_path(str(files["stale"]))),
            Rewrite("t-active", True, "h", "x", "spill", None, "active", norm_path(str(files["active"]))),
        ]
    )
    store._exec("UPDATE rewrites SET created=?", (old,))
    store.event("info", "note", "old event")
    store._exec("UPDATE events SET ts=?", (old,))
    return files


def test_prune_keeps_what_active_sessions_replay(store, tmp_path, cfg):
    now = time.time()
    files = _setup(store, tmp_path, now)
    dry = cleanup(store, cfg, dry_run=True, now=now)
    assert dry["rewrites"] == 1 and dry["spill_files"] == 2 and files["stale"].exists()
    counts = cleanup(store, cfg, now=now)
    assert counts["rewrites"] == 1 and counts["sessions"] == 1 and counts["events"] == 1
    remaining = {r["tool_use_id"] for r in store.query("SELECT tool_use_id FROM rewrites")}
    assert remaining == {"t-active"}
    assert files["active"].exists() and not files["stale"].exists() and not files["orphan"].exists()
    assert store.get_state("last_prune")


def test_prune_records_and_history_follow_session_activity(store, tmp_path, cfg):
    from lightfoot.store import Prune

    now = time.time()
    history = tmp_path / "proj" / ".lightfoot" / "history"
    history.mkdir(parents=True)
    store.put_launch(Launch("l1", str(tmp_path / "proj"), "live", None, None, None))
    old = now - 40 * 86400
    for sid, last in (("stale", old), ("active", now - 3600)):
        store.get_or_create_session(sid, "l1", "live", old)
        store.execute("UPDATE sessions SET last_request=? WHERE id=?", (last, sid))
        f = history / f"{sid}-10.md"
        f.write_text("history", encoding="utf-8")
        os.utime(f, (old, old))
        store.put_prune(Prune(sid, True, 10, "fp", "summary", norm_path(str(f)), 1000))
    counts = cleanup(store, cfg, now=now)
    assert counts["prunes"] == 1
    assert store.get_prune("active", True) is not None and store.get_prune("stale", True) is None
    assert (history / "active-10.md").exists() and not (history / "stale-10.md").exists()


def test_estimate_reads_claude_code_transcripts(tmp_path):
    from lightfoot import estimate

    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 9000,
        "output_tokens": 40,
        "cache_creation": {"ephemeral_1h_input_tokens": 500, "ephemeral_5m_input_tokens": 0},
    }
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "timestamp": "2026-10-20T10:00:00Z",
            "message": {"role": "user", "content": "fix the bug"},
        },
        {"type": "attachment", "uuid": "a1", "parentUuid": "u1", "attachment": {"type": "environment"}},
        # one assistant message split over two entries (thinking, then the tool call)
        {
            "type": "assistant",
            "uuid": "m1a",
            "parentUuid": "a1",
            "timestamp": "2026-10-20T10:00:05Z",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "thinking", "thinking": "", "signature": "s"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "m1b",
            "parentUuid": "m1a",
            "timestamp": "2026-10-20T10:00:06Z",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": "m1b",
            "timestamp": "2026-10-20T10:00:09Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 failed"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "m2",
            "parentUuid": "u2",
            "timestamp": "2026-10-20T10:00:15Z",
            "message": {
                "id": "msg_2",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "text", "text": "fixed"}],
            },
        },
    ]
    path = tmp_path / "proj" / "s.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    calls = list(estimate.calls_in(path))
    assert [c.msg_id for c in calls] == ["msg_1", "msg_2"]
    second = calls[1].messages
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert [b["type"] for b in second[1]["content"]] == ["thinking", "tool_use"]  # split entries merged
    # A resumed session repeats history in a new file: each call is counted once.
    (tmp_path / "proj" / "s2.jsonl").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    result = estimate.run(tmp_path, days=36500)
    assert result["allows_edits"][0].calls == 2


def test_report_has_no_paths_or_content(store, tmp_path, cfg):
    store.add_ledger(
        {
            "ts": time.time(),
            "session_id": "s1",
            "mode": "live",
            "model": "claude-opus-5-5",
            "status": 200,
            "input": 10,
            "cw5": 0,
            "cw1h": 0,
            "cr": 1000,
            "output": 5,
            "saved_tokens": 400,
            "rules": {"spill": 1},
            "agent": "claude-code",
            "client_version": "claude-cli/2.1.191",
        }
    )
    store.event("info", "note", f"spill write failed: [Errno 13] {tmp_path / 'secret' / 'path.log'}")
    store.event("info", "note", "hash mismatch for toolu_ABC; left unchanged")
    text = report(store)
    assert "claude-cli/2.1.191" in text and "claude-code/live" in text
    assert "secret" not in text and "toolu_ABC" not in text and str(tmp_path) not in text
    assert "spill write failed ×1" in text and "hash mismatch ×1" in text
