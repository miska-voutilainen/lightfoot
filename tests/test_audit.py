"""`lightfoot audit`: the fixed prefix recorded by the proxy, priced per project."""

import json

from conftest import request, tool_result, tool_use, user_text
from test_server import HEADERS, proxy_client, upstream  # noqa: F401 - fixtures

from lightfoot.audit import audit

TOOLS = [
    {"name": "Bash", "description": "Run a command " * 50, "input_schema": {"type": "object"}},
    {"name": "mcp__github__search", "description": "Search GitHub " * 80, "input_schema": {"type": "object"}},
    {"name": "mcp__browser__open", "description": "Open a page " * 120, "input_schema": {"type": "object"}},
]


def body(date, messages):
    system = [{"type": "text", "text": "You are Claude Code. " * 40}, {"type": "text", "text": f"Today is {date}."}]
    return request(messages, system=system, tools=TOOLS)


def test_audit_prices_the_prefix_and_flags_unused_servers_and_variants(proxy_client, upstream, store):  # noqa: F811
    client, _ = proxy_client
    first = [user_text("fix it"), tool_use("t1", "mcp__github__search", {"q": "bug"}), tool_result("t1", "found")]
    for date, messages in (("2026-10-24", first), ("2026-10-25", [user_text("hello")])):
        raw = json.dumps(body(date, messages)).encode()
        assert client.post("/l/launch-shadow/v1/messages", content=raw, headers=HEADERS).status_code == 200
    out = audit(store)
    assert "proj: 2 sessions, median prefix" in out
    github = next(line for line in out.splitlines() if "github" in line)
    browser = next(line for line in out.splitlines() if "browser" in line)
    assert "unused" not in github and "unused: removing it would save about this" in browser
    assert "system block 2 differs between sessions (2 versions" in out and 'starts "Today is 2026-10-2' in out
    rows = store.query("SELECT name FROM tools_used")
    assert [r["name"] for r in rows] == ["mcp__github__search"]
    assert "No sessions recorded" in audit(store, days=0)
