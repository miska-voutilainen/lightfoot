import json
import time

from lightfoot.cli import main
from lightfoot.store import Launch, Prune, Rewrite, Store
from lightfoot.why import resolve_session, why


def _session(store, sid="abc123def4567890", mode="live", launch="l1"):
    now = time.time()
    store.put_launch(Launch(launch, "/work/project", mode, None, None, None))
    store.get_or_create_session(sid, launch, mode, now - 600)
    store.touch_session(sid, now, 12, "claude-opus-5-5")
    store.add_ledger(
        {
            "ts": now,
            "launch_id": launch,
            "session_id": sid,
            "mode": mode,
            "model": "claude-opus-5-5",
            "status": 200,
            "turn": 12,
            "input": 10,
            "cr": 150_000,
            "cw5": 0,
            "cw1h": 2_000,
            "output": 500,
            "saved_tokens": 50_000,
            "rules": json.dumps({"prune": 1, "spill": 2}),
        }
    )
    return sid


def test_why_shows_prune_summary_rewrites_and_signals(store):
    sid = _session(store)
    store.put_prune(Prune(sid, True, 40, "fp", "[lightfoot: summary]\n## Requests\n1. build it", "/h/x.md", 120_000))
    store.put_rewrites(
        [
            Rewrite(
                "t1", True, "h1", "head\n[lightfoot: output trimmed to 140 of 2000 lines]", "spill", None, sid, "/s"
            ),
            Rewrite("t2", True, "h2", '{"a":1}', "json", None, sid),
            Rewrite("t3", True, "h3", "[lightfoot: superseded read of a.py]", "stale_reads", 30, sid, "a.py"),
        ]
    )
    store.add_signal("t9", sid, "prune", "history_read", "/h/x.md")
    text = why(store)
    assert f"Session {sid}" in text and "Project: /work/project" in text
    assert "messages 2-40 replaced by a summary" in text and "Full text: /h/x.md" in text
    assert "| 1. build it" in text
    assert "[lightfoot: output trimmed to 140 of 2000 lines]" in text
    assert "JSON output without indentation (lossless)" in text
    assert "epoch at turn 30" in text
    assert "read a history file ×1" in text
    assert "prune ×1, spill ×2" in text


def test_why_marks_shadow_sessions(store):
    sid = _session(store, mode="shadow")
    assert "nothing was sent changed" in why(store, sid[:6])


def test_why_session_prefix_must_be_unambiguous(store):
    _session(store, "aaaa1111", launch="l1")
    _session(store, "aaaa2222", launch="l2")
    assert "Several sessions match" in why(store, "aaaa")
    assert why(store, "zzzz") == "No session matches 'zzzz'."
    assert resolve_session(store, "aaaa1")[0] == "aaaa1111"


def test_why_on_an_empty_database(store):
    assert why(store) == "No sessions recorded yet."


def test_why_command(capsys, lightfoot_home):
    store = Store()
    try:
        sid = _session(store)
    finally:
        store.close()
    assert main(["why", sid[:8]]) == 0
    assert f"Session {sid}" in capsys.readouterr().out
