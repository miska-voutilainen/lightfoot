"""`lightfoot doctor`: tool names an agent's profile doesn't know."""

import time

from lightfoot.reports import unknown_tools


def session(store, sid, profile, names, ts=None):
    store.add_ledger({"ts": ts or time.time(), "session_id": sid, "mode": "shadow", "status": 200, "agent": profile})
    store.add_tools_used(sid, names)


def test_unknown_tools_lists_only_names_the_profile_lacks(store):
    session(store, "s1", "claude-code", ["Bash", "Read", "NewTool", "mcp__github__search"])
    session(store, "s2", "opencode", ["read", "websearch"])
    session(store, "s3", "generic", ["anything"])  # no profile to update
    assert unknown_tools(store) == {"claude-code": ["NewTool"], "opencode": ["websearch"]}


def test_unknown_tools_ignores_old_sessions(store):
    session(store, "old", "claude-code", ["NewTool"], ts=time.time() - 40 * 86400)
    assert unknown_tools(store, days=30) == {}
