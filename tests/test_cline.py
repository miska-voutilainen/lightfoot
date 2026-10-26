"""Cline and Roo Code support: tool names, the route `lightfoot cline|roo` registers, and JSON tool results."""

import json
import time
from pathlib import Path

import httpx
import pytest
from conftest import lift_budgets, request, tool_result, tool_use, user_text
from starlette.testclient import TestClient
from test_server import Upstream

from lightfoot import prune
from lightfoot.adapters import ANTHROPIC
from lightfoot.launch import register_launch, setup_extension
from lightfoot.profiles import CLAUDE_CODE, CLINE, ROO_CODE, for_client
from lightfoot.server import Proxy, build_app
from lightfoot.store import Launch
from lightfoot.transform import plan_request, session_key


def cline_output(text, query="/repo/src/app.py", success=True):
    """A Cline SDK tool result as it reaches the API: the tool's list, serialized as compact JSON."""
    return json.dumps([{"query": query, "result": text, "success": success}], separators=(",", ":"))


def test_both_tool_sets_map_to_neutral_kinds():
    assert CLINE.kind("read_files", {"files": [{"path": "/r/a.py"}]}) == "read"
    assert CLINE.kind("run_commands", {"commands": ["ls"]}) == "shell"
    assert CLINE.kind("search_codebase", {"queries": ["handler"]}) == "search"
    assert CLINE.kind("editor", {"path": "/r/a.py"}) == CLINE.kind("apply_patch", {}) == "edit"
    assert CLINE.kind("read_file", {"path": "/r/a.py"}) == "read"
    assert CLINE.kind("execute_command", {"command": "ls"}) == "shell"
    assert CLINE.kind("replace_in_file", {}) == CLINE.kind("write_to_file", {}) == "edit"
    assert CLINE.kind("Read", {}) == "other"  # Claude Code names mean nothing here


def test_paths_commands_and_searches():
    assert CLINE.path("read_files", {"files": [{"path": "/r/a.py", "start_line": 1}]}) == "/r/a.py"
    assert CLINE.path("read_files", {"files": ["/r/a.py"]}) == "/r/a.py"
    assert CLINE.path("read_files", {"files": [{"path": "/r/a.py"}, {"path": "/r/b.py"}]}) is None
    assert CLINE.path("editor", {"path": "/r/a.py"}) == "/r/a.py"
    assert CLINE.path("run_commands", {"path": "/r"}) is None
    assert CLINE.command("run_commands", {"commands": ["pytest -q", {"command": "git status"}]}) == (
        "pytest -q && git status"
    )
    assert CLINE.command("execute_command", {"command": "ls"}) == "ls"
    assert CLINE.search_text("search_codebase", {"queries": ["handler", "router"]}) == "handler, router"
    assert CLINE.search_text("search_files", {"regex": "TODO", "path": "/r"}) == "TODO"
    assert CLINE.search_content_mode("search_codebase", {}) is False


def test_the_launch_agent_picks_the_profile():
    assert for_client("anthropic", "ai-sdk/anthropic/3.0.0", "cline") is CLINE
    assert for_client("anthropic", "ai-sdk/anthropic/3.0.0", None) is CLAUDE_CODE


def test_prune_summary_understands_cline_tools():
    msgs = [
        user_text("start"),
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "a", "name": "read_files", "input": {"files": [{"path": "/r/app.py"}]}},
                {"type": "tool_use", "id": "b", "name": "editor", "input": {"path": "/r/app.py", "new_text": "x"}},
                {"type": "tool_use", "id": "c", "name": "run_commands", "input": {"commands": ["pytest -q"]}},
                {"type": "tool_use", "id": "d", "name": "search_codebase", "input": {"queries": ["handler"]}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": k, "content": cline_output("ok")} for k in ("a", "b", "c", "d")
            ],
        },
        user_text("next"),
    ]
    summary = prune.build_summary(msgs, 3, len(msgs), None, 8000, CLINE)
    assert "Read: /r/app.py" in summary and "Edited or written: /r/app.py" in summary
    assert "pytest -q" in summary and "handler" in summary


def plan_cline(store, make_ctx, output, cfg=None):
    msgs = [
        user_text("run the tests"),
        tool_use("c1", "run_commands", {"commands": ["pytest"]}),
        tool_result("c1", output),
    ]
    body = request(msgs)
    session = store.get_or_create_session(session_key(body), None, "live", time.time())
    extra = {"config": cfg} if cfg else {}
    return plan_request(body, make_ctx(session=session, adapter=ANTHROPIC, profile=CLINE, **extra))


def test_long_strings_inside_compact_json_are_trimmed(store, make_ctx, tmp_path):
    lines = [f"\x1b[32mPASSED\x1b[0m tests/test_mod.py::test_case_{i}" for i in range(3000)]
    lines[1500] = "FAILED tests/test_mod.py::test_case_1500 - AssertionError: expected 3, got 4"
    lines.append("1 failed, 2999 passed in 12.3s")
    original = cline_output("\n".join(lines), query="pytest")
    plan = plan_cline(store, make_ctx, original)
    assert plan.rules == {"json_trim": 1}
    sent = plan.body_out["messages"][2]["content"][0]["content"]
    value = json.loads(sent)  # still valid JSON, in Cline's own compact encoding
    assert sent == json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    assert [set(v) for v in value] == [{"query", "result", "success"}] and value[0]["query"] == "pytest"
    result = value[0]["result"]
    assert "FAILED tests/test_mod.py::test_case_1500" in result  # every error line kept
    assert "1 failed, 2999 passed in 12.3s" in result  # and the tail, which a line cut would have lost
    assert "\x1b[" not in result and "[lightfoot: this value trimmed" in result
    full = result.split("Full text: ")[1].split(" (read it")[0]
    assert "test_case_2999" in Path(full).read_text(encoding="utf-8")
    assert len(sent) < len(original) / 5


def test_short_or_unreproducible_json_is_left_alone(store, make_ctx):
    assert plan_cline(store, make_ctx, cline_output("ok\nfine")).body_out is None  # nothing to gain
    spaced = '[{"query": "pytest" , "result": ' + json.dumps("\n".join(["same line"] * 3000)) + "}]"
    assert plan_cline(store, make_ctx, spaced).body_out is None  # its encoding can't be reproduced


def test_json_trim_can_be_turned_off(store, make_ctx, cfg):
    cfg["rules"]["json_trim"] = False
    noisy = cline_output("\n".join(f"line {i} of a long log" for i in range(3000)))
    assert plan_cline(store, make_ctx, noisy, cfg).body_out is None


def test_json_trim_promise():
    from lightfoot.ingest import json_trim_ok

    before = json.dumps([{"query": "q", "result": "ok\nERROR: disk full\n" + "x\n" * 50, "success": True}])
    good = json.dumps([{"query": "q", "result": "ok\nERROR: disk full\n[lightfoot: …]", "success": True}])
    assert json_trim_ok(before, good)
    assert not json_trim_ok(before, good.replace("true", "false"))  # a non-string value changed
    assert not json_trim_ok(before, good.replace("ERROR: disk full", "disk"))  # an error line dropped
    assert not json_trim_ok(before, json.dumps([{"result": "ok", "query": "q", "success": True}]))  # key order
    assert not json_trim_ok(before, "not json")


# --- route -------------------------------------------------------------------------


def test_live_cline_needs_the_experimental_flag():
    with pytest.raises(RuntimeError, match="experimental"):
        setup_extension("cline", mode="live", experimental=False)


def test_setup_prints_the_launch_base_url(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def prepare(cwd, mode, agent, experimental):
        seen.update(mode=mode, agent=agent, experimental=experimental)
        return Launch("cl1", cwd, mode, None, None, None, agent=agent), None, 8787

    monkeypatch.setattr("lightfoot.launch._prepare_launch", prepare)
    assert setup_extension("cline") == 0
    assert seen == {"mode": "shadow", "agent": "cline", "experimental": False}
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8787/l/cl1/v1" in out and "Use custom base URL" in out


def test_cline_ignores_a_gateway_set_for_claude_code(store, tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gw.example"}}))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    cline, _ = register_launch(store, str(project), "shadow", 8787, agent="cline")
    claude, _ = register_launch(store, str(project), "shadow", 8787, agent="claude")
    assert cline.upstream == "https://api.anthropic.com" and claude.upstream == "https://gw.example"


def test_server_keeps_cline_in_shadow_without_experimental(store, tmp_path):
    upstream = Upstream()
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream.app()))
    project = str(lift_budgets(tmp_path / "proj"))
    store.put_launch(Launch("cl-live", project, "live", "http://upstream", None, None, agent="cline"))
    body = request(
        [
            user_text("run tests"),
            tool_use("t1", "run_commands", {"commands": ["pytest"]}),
            tool_result("t1", "\n".join(["same line repeated"] * 200)),  # a plain-text result (older tools)
        ]
    )
    raw = json.dumps(body).encode()
    headers = {"x-api-key": "sk-test", "content-type": "application/json", "user-agent": "ai-sdk/anthropic/3.0.0"}
    with TestClient(build_app(proxy), base_url="http://127.0.0.1:8787") as client:
        assert client.post("/l/cl-live/v1/messages", content=raw, headers=headers).status_code == 200
    assert upstream.seen[-1][1] == raw  # forwarded unchanged: shadow
    row = store.query("SELECT mode, agent, saved_tokens FROM ledger ORDER BY id DESC LIMIT 1")[0]
    assert (row["mode"], row["agent"]) == ("shadow", "cline") and row["saved_tokens"] > 0


# --- Roo Code ----------------------------------------------------------------------


def test_roo_tools_paths_and_read_spans():
    assert ROO_CODE.kind("read_file", {"path": "src/a.py"}) == "read"
    assert ROO_CODE.kind("apply_diff", {}) == ROO_CODE.kind("search_replace", {}) == "edit"
    assert ROO_CODE.kind("codebase_search", {"query": "auth"}) == "search"
    assert ROO_CODE.kind("execute_command", {"command": "ls"}) == "shell"
    assert ROO_CODE.kind("read_files", {}) == "other"  # Cline's SDK names are not Roo's
    assert ROO_CODE.path("read_file", {"path": "src/a.py"}) == "src/a.py"
    assert ROO_CODE.path("read_file", {"files": [{"path": "src/b.py"}], "_legacyFormat": True}) == "src/b.py"
    assert ROO_CODE.read_span("File: src/a.py\n 12 | import os\n 13 | x = 1\n140 | end") == (12, 140)
    assert ROO_CODE.search_text("codebase_search", {"query": "auth flow"}) == "auth flow"
    assert for_client("anthropic", "Anthropic/JS 0.60.0", "roo") is ROO_CODE


def test_roo_setup_prints_a_base_url_without_v1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "lightfoot.launch._prepare_launch",
        lambda cwd, mode, agent, experimental: (Launch("r1", cwd, mode, None, None, None, agent=agent), None, 8787),
    )
    assert setup_extension("roo") == 0
    out = capsys.readouterr().out
    assert "In Roo Code:" in out and "http://127.0.0.1:8787/l/r1\n" in out  # the Anthropic SDK adds /v1/messages
    with pytest.raises(RuntimeError, match="Roo Code support is experimental"):
        setup_extension("roo", mode="live")
