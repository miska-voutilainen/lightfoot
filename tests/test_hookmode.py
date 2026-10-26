import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest
from conftest import big_output

from lightfoot import hookmode
from lightfoot.store import Store


def _bash(tmp_path, stdout, **extra):
    response = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False}
    response.update(extra)
    return {"tool_name": "Bash", "cwd": str(tmp_path), "session_id": "cc-1", "tool_response": response}


def test_noisy_bash_output_is_condensed_and_other_fields_kept(tmp_path, lightfoot_home):
    payload = _bash(tmp_path, "\n".join(["same line repeated"] * 50) + "\nERROR: disk full", stderr="warn")
    out = json.loads(hookmode.run(json.dumps(payload)))
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert "previous line repeated 49 more times" in updated["stdout"] and "ERROR: disk full" in updated["stdout"]
    assert updated["stderr"] == "warn" and updated["noOutputExpected"] is False
    store = Store()
    try:
        (row,) = store.query("SELECT * FROM hook_rewrites")
        assert row["rule"] == "noise" and row["orig_tokens"] > row["new_tokens"] and row["session_id"] == "cc-1"
    finally:
        store.close()


def test_savings_are_recorded_in_an_existing_or_older_database(tmp_path, lightfoot_home):
    noisy = "\n".join(["same line repeated"] * 50)
    Store().close()  # an existing database: the plain sqlite3 insert
    hookmode.run(json.dumps(_bash(tmp_path, noisy)))
    conn = sqlite3.connect(lightfoot_home / "lightfoot.db")
    conn.execute("DROP TABLE hook_rewrites")  # as written by a lightfoot before hook mode
    conn.commit()
    conn.close()
    hookmode.run(json.dumps(_bash(tmp_path, noisy)))
    hookmode.run(json.dumps(_bash(tmp_path, noisy)))
    store = Store()
    try:
        assert len(store.query("SELECT * FROM hook_rewrites")) == 2
    finally:
        store.close()


def test_a_locked_database_skips_the_note_without_waiting(tmp_path, lightfoot_home):
    Store().close()
    lock = sqlite3.connect(lightfoot_home / "lightfoot.db")
    lock.execute("BEGIN EXCLUSIVE")
    try:
        start = time.perf_counter()
        out = hookmode.run(json.dumps(_bash(tmp_path, "\n".join(["same line repeated"] * 50))))
        assert "updatedToolOutput" in out  # the rewrite still happens
        assert time.perf_counter() - start < 5  # the 2 s busy timeout, not Store's 10 s
    finally:
        lock.rollback()
        lock.close()


def test_large_output_is_spilled_into_the_project(tmp_path, lightfoot_home):
    out = json.loads(hookmode.run(json.dumps(_bash(tmp_path, big_output()))))
    stdout = out["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
    assert "AssertionError: expected 3, got 4" in stdout and "Full output:" in stdout
    assert len(list((tmp_path / ".lightfoot" / "spill").glob("*.log"))) == 1


def test_pretty_json_from_an_mcp_tool_is_minified(tmp_path, lightfoot_home):
    pretty = json.dumps({"items": [{"id": i, "name": f"n{i}"} for i in range(40)]}, indent=2)
    payload = {"tool_name": "mcp__api__list", "cwd": str(tmp_path), "tool_response": [{"type": "text", "text": pretty}]}
    updated = json.loads(hookmode.run(json.dumps(payload)))["hookSpecificOutput"]["updatedToolOutput"]
    assert json.loads(updated[0]["text"]) == json.loads(pretty) and "\n" not in updated[0]["text"]


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_response": {"type": "text", "file": {"content": "x"}}},  # not handled
        {"tool_name": "Bash", "tool_response": {"stdout": "", "isImage": False}},  # nothing to do
        {"tool_name": "Bash", "tool_response": {"stdout": "img", "isImage": True}},
        {"tool_name": "Bash", "tool_response": {"stdout": "x\n" * 50, "interrupted": True}},
        {"tool_name": "mcp__x__y", "tool_response": [{"type": "image", "data": "..."}]},
    ],
)
def test_unhandled_or_unchanged_output_returns_nothing(tmp_path, lightfoot_home, payload):
    assert hookmode.run(json.dumps({**payload, "cwd": str(tmp_path)})) == ""


def test_bad_input_and_a_missing_cwd_never_raise(tmp_path, lightfoot_home):
    assert hookmode.run("not json") == ""
    assert hookmode.run(json.dumps(_bash(tmp_path / "missing", "x\n" * 80))) == ""


def test_hooks_can_be_disabled_per_project(tmp_path, lightfoot_home):
    (tmp_path / ".lightfoot").mkdir()
    (tmp_path / ".lightfoot" / "config.toml").write_text("[hooks]\nenabled = false\n", encoding="utf-8")
    assert hookmode.run(json.dumps(_bash(tmp_path, "\n".join(["same"] * 80)))) == ""


def test_condensing_loads_neither_the_planner_nor_the_adapters(tmp_path):
    # The hook runs once per tool call, so its imports are its cost: keep them light.
    payload = _bash(tmp_path, "\n".join(["same line repeated"] * 50))
    code = (
        "import json, sys\n"
        "from lightfoot import hookmode\n"
        f"out, done = hookmode.handle(json.loads({json.dumps(json.dumps(payload))}))\n"
        "assert done, 'nothing was condensed'\n"
        "heavy = {'transform', 'adapters', 'store', 'prune', 'pricing', 'validate'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[:2][-1] in heavy and m.startswith('lightfoot.')))\n"
    )
    env = {**os.environ, "LIGHTFOOT_HOME": str(tmp_path / "home")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == "[]"


def test_install_and_uninstall_keep_other_hooks(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(hookmode, "settings_path", lambda: path)
    mine = {"matcher": "Write", "hooks": [{"type": "command", "command": "fmt"}]}
    path.write_text(json.dumps({"hooks": {"PostToolUse": [mine]}}), encoding="utf-8")
    hookmode.install()
    hookmode.install()  # idempotent
    entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    assert entries[0] == mine and len(entries) == 2 and entries[1]["matcher"] == hookmode.MATCHER
    assert hookmode.is_installed()
    assert hookmode.uninstall() is True
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["PostToolUse"] == [mine]
    assert hookmode.uninstall() is False


def test_compact_json_from_an_mcp_tool_has_its_long_strings_trimmed(tmp_path, lightfoot_home):
    log = "\n".join(f"step {i} done" for i in range(2000)) + "\nERROR: step 2000 failed"
    compact = json.dumps({"job": 7, "log": log}, separators=(",", ":"))
    payload = {"tool_name": "mcp__ci__logs", "cwd": str(tmp_path), "session_id": "cc-1", "tool_response": compact}
    out = json.loads(hookmode.run(json.dumps(payload)))["hookSpecificOutput"]["updatedToolOutput"]
    value = json.loads(out)
    assert value["job"] == 7 and "ERROR: step 2000 failed" in value["log"] and len(out) < len(compact) / 4
