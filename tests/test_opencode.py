import json

import pytest

from lightfoot import prune
from lightfoot.launch import opencode_config, run_opencode
from lightfoot.profiles import CLAUDE_CODE, OPENCODE, for_client
from lightfoot.store import Launch


def test_opencode_tools_map_to_neutral_kinds():
    assert OPENCODE.kind("read", {"path": "/r/a.py"}) == "read"
    assert OPENCODE.kind("patch", {}) == OPENCODE.kind("write", {}) == "edit"
    assert OPENCODE.kind("grep", {"pattern": "x"}) == "search"
    assert OPENCODE.kind("shell", {"command": "ls"}) == "shell"
    assert OPENCODE.kind("Read", {}) == "other"  # Claude Code names mean nothing here
    assert OPENCODE.path("read", {"path": "/r/a.py"}) == "/r/a.py"
    assert OPENCODE.path("shell", {"path": "/r"}) is None
    assert OPENCODE.read_span("1: import os\n2: x = 1\n40: end") == (1, 40)
    assert OPENCODE.search_content_mode("grep", {}) is False


def test_opencode_1_19_tool_names():
    # opencode 1.19.4's own tool definitions, captured by scripts/agent_check.py.
    assert OPENCODE.kind("bash", {"command": "ls"}) == "shell"
    assert OPENCODE.kind("task", {"prompt": "x", "subagent_type": "general"}) == "subagent"
    assert OPENCODE.path("read", {"filePath": "/r/a.py", "offset": 1}) == "/r/a.py"
    assert OPENCODE.path("edit", {"filePath": "/r/a.py", "oldString": "a", "newString": "b"}) == "/r/a.py"
    # Its read output, captured from opencode 1.19.4 reading a 324-line file.
    read = "\n".join(
        [
            "<path>/r/data.json</path>",
            "<type>file</type>",
            "<content>",
            "1: {",
            '2:   "items": [',
            "324: }",
            "",
            "(End of file - total 324 lines)",
            "</content>",
        ]
    )
    assert OPENCODE.read_span(read) == (1, 324)


def test_the_launch_agent_picks_the_profile():
    assert for_client("anthropic", "ai-sdk/anthropic", "opencode") is OPENCODE
    assert for_client("anthropic", "claude-cli/2.1.191 (external, cli)", "claude") is CLAUDE_CODE


def test_opencode_config_keeps_the_users_inline_config():
    launch = Launch("abc123", "/p", "shadow", None, None, None)
    mine = json.dumps({"theme": "dark", "provider": {"anthropic": {"options": {"timeout": 5}}}})
    out = json.loads(opencode_config(8787, launch, mine))
    assert out["theme"] == "dark" and out["provider"]["anthropic"]["options"]["timeout"] == 5
    assert out["provider"]["anthropic"]["options"]["baseURL"] == "http://127.0.0.1:8787/l/abc123/v1"
    assert json.loads(opencode_config(8787, launch, "not json"))["provider"]["anthropic"]["options"]["baseURL"]


def test_live_opencode_needs_the_experimental_flag(monkeypatch):
    monkeypatch.setattr("lightfoot.launch.find_opencode", lambda: "opencode")
    monkeypatch.delenv("LIGHTFOOT_OFF", raising=False)
    with pytest.raises(RuntimeError, match="experimental"):
        run_opencode([], mode="live", experimental=False)


def test_prune_summary_understands_opencode_tools():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "start"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "a", "name": "read", "input": {"path": "/r/src/app.py"}},
                {"type": "tool_use", "id": "b", "name": "edit", "input": {"path": "/r/src/app.py"}},
                {"type": "tool_use", "id": "c", "name": "shell", "input": {"command": "pytest -q"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "1: x"},
                {"type": "tool_result", "tool_use_id": "b", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "c", "content": "1 failed", "is_error": True},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
    ]
    summary = prune.build_summary(msgs, 3, len(msgs), None, 8000, OPENCODE)
    assert "Edited or written: /r/src/app.py" in summary and "Read: /r/src/app.py" in summary
    assert "`pytest -q` (failed)" in summary
