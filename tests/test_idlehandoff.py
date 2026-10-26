"""The idle handoff for agents without a session-end hook: condensing, tracking, the proxy and the worker."""

import json
import sys

from lightfoot import handoff, idlehandoff
from lightfoot.server import Prepared, Proxy
from lightfoot.store import Launch

ANTHROPIC = {
    "messages": [
        {"role": "user", "content": "Fix the parser"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "edit", "input": {"path": "/p/a.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
    ]
}
RESPONSES = {
    "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Run the tests"}]},
        {"type": "reasoning", "encrypted_content": "e"},
        {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": '{"command": ["pytest", "-q"]}'},
        {"type": "function_call_output", "call_id": "c1", "output": "1 failed"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "One test fails."}]},
    ]
}
GEMINI = {
    "contents": [
        {"role": "user", "parts": [{"text": "List files"}]},
        {"role": "model", "parts": [{"text": "hmm", "thought": True}, {"functionCall": {"name": "ls", "args": {}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "ls", "response": {"output": "a.py"}}}]},
        {"role": "model", "parts": [{"text": "There is a.py."}]},
    ]
}


def test_condense_reads_every_wire_format():
    text, turns, touched = idlehandoff.condense_request(ANTHROPIC, "anthropic", "opencode")
    assert text.splitlines() == ["USER: Fix the parser", "TOOL edit: /p/a.py", "AGENT: Done."]
    assert (turns, touched) == (2, ["/p/a.py"])
    text, turns, _ = idlehandoff.condense_request(RESPONSES, "responses", "codex")
    assert text.splitlines()[0] == "USER: Run the tests" and "TOOL shell:" in text and "AGENT: One test fails." in text
    assert turns == 2  # the reasoning and call are one response; the answer is the next
    text, turns, _ = idlehandoff.condense_request(GEMINI, "gemini", "gemini-cli")
    assert "hmm" not in text and "AGENT: There is a.py." in text and turns == 2


def test_tracker_waits_for_the_gap_and_caps_what_it_holds():
    idle = idlehandoff.IdleHandoffs()
    idle.note("s1", {}, "anthropic", "generic", "/p", 30, now=1000)
    assert idle.due(1000 + 29 * 60) == []
    idle.note("s1", {}, "anthropic", "generic", "/p", 30, now=1000 + 29 * 60)  # a new request resets the clock
    assert idle.due(1000 + 31 * 60) == []
    assert [sid for sid, _ in idle.due(1000 + 60 * 60)] == ["s1"]
    assert idle.due(1000 + 90 * 60) == []  # one note per idle gap
    for i in range(idlehandoff.MAX_PENDING + 5):
        idle.note(f"s{i}", {}, "anthropic", "generic", "/p", 30, now=i)
    assert len(idle._pending) == idlehandoff.MAX_PENDING and "s0" not in idle._pending


def test_enabled_needs_a_command():
    assert idlehandoff.enabled({"integrations": {"idle_handoff_minutes": 30}}) == 0
    assert idlehandoff.enabled({"integrations": {"idle_handoff_minutes": 10, "handoff_command": "x"}}) == 10


def test_proxy_tracks_other_agents_with_a_vault_only(store, tmp_path, monkeypatch, lightfoot_home):
    project = tmp_path / "proj"
    (project / ".lightfoot").mkdir(parents=True)
    lightfoot_home.mkdir(parents=True, exist_ok=True)
    (lightfoot_home / "config.toml").write_text('[integrations]\nhandoff_command = "summarize"\n', encoding="utf-8")
    (project / ".lightfoot" / "config.toml").write_text("[integrations]\nhandoff_min_turns = 2\n", encoding="utf-8")
    proxy = Proxy(store)
    from lightfoot.adapters import for_path

    adapter = for_path("POST", "v1/messages")
    prepared = Prepared("sid1", "shadow", None, [], [], adapter, "opencode", ANTHROPIC)
    for agent, vault in (("claude", "/v"), ("opencode", None)):
        proxy.note_idle(prepared, Launch("l", str(project), "shadow", None, None, vault, agent=agent))
    assert proxy.idle._pending == {}
    proxy.note_idle(prepared, Launch("l", str(project), "shadow", None, None, "/v", agent="opencode"))
    started = []
    monkeypatch.setattr("lightfoot.server.idle_start", lambda sid, p, n: started.append((sid, n)) or True)
    assert proxy.start_idle_handoffs(0) == 0
    assert proxy.start_idle_handoffs(1e12) == 1 and started == [("sid1", 2)]


def test_worker_runs_the_handoff_command_and_writes_the_note(tmp_path, monkeypatch, lightfoot_home):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    project = tmp_path / "proj"
    (project / ".lightfoot").mkdir(parents=True)
    script = tmp_path / "summ.py"
    script.write_text("import sys; text = sys.stdin.read(); print('## Summary', 'USER: hi' in text)")
    command = f'"{sys.executable}" "{script}"'
    lightfoot_home.mkdir(parents=True, exist_ok=True)
    (lightfoot_home / "config.toml").write_text(
        f"[integrations]\nvault_path = {json.dumps(str(vault))}\nhandoff_command = {json.dumps(command)}\n",
        encoding="utf-8",
    )
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"log": "USER: hi", "touched": ["/p/a.py"]}), encoding="utf-8")
    assert handoff.run_worker(None, str(project), "sid1", log=str(log)) == 0
    assert not log.exists()
    (note,) = (vault / "Claude Handoffs").glob("*.md")
    body = note.read_text(encoding="utf-8")
    assert "## Summary True" in body and "`/p/a.py`" in body


def test_a_project_config_cannot_set_the_handoff_command(tmp_path, monkeypatch, lightfoot_home):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    lightfoot_home.mkdir(parents=True, exist_ok=True)
    (lightfoot_home / "config.toml").write_text(
        f"[integrations]\nvault_path = {json.dumps(str(vault))}\n", encoding="utf-8"
    )
    project = tmp_path / "cloned-repo"
    (project / ".lightfoot").mkdir(parents=True)
    marker = tmp_path / "ran"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w')\""
    (project / ".lightfoot" / "config.toml").write_text(
        f"[integrations]\nhandoff_command = {json.dumps(command)}\n", encoding="utf-8"
    )
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"log": "USER: hi", "touched": []}), encoding="utf-8")
    assert handoff.run_worker(None, str(project), "sid1", log=str(log)) == 0
    assert not marker.exists()
