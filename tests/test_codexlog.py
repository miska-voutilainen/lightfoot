"""`lightfoot estimate --agent codex`: Codex rollouts, in the format checked on a real one."""

import copy
import json

from conftest import big_output

from lightfoot import codexlog
from lightfoot.config import DEFAULTS


def line(kind, payload, ts="2026-10-25T10:00:00Z"):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def tokens(total, inp, cached, out):
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "total_tokens": total}
    return line("event_msg", {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}})


def rollout(tmp_path):
    user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run the tests"}]}
    call = {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["pytest"]}), "call_id": "c1"}
    lines = [
        line("session_meta", {"id": "sess-1", "cwd": "/repo", "cli_version": "0.99.0"}),
        line("turn_context", {"model": "gpt-6-sol", "cwd": "/repo"}),
        line("event_msg", {"type": "token_count", "info": None}),
        line("response_item", user),
        line("response_item", {"type": "reasoning", "encrypted_content": "e1"}),
        line("response_item", call),
        tokens(1100, 1000, 0, 100),
        line("response_item", {"type": "function_call_output", "call_id": "c1", "output": big_output()}),
        tokens(1100, 1000, 0, 100),  # a repeated event: no new call
        line(
            "response_item",
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "1 failed"}]},
        ),
        tokens(60_000, 58_000, 1_000, 900),
    ]
    path = tmp_path / "rollout-2026-10-25T10-00-00-sess-1.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_calls_are_rebuilt_from_the_rollout(tmp_path):
    calls = list(codexlog.calls_in(rollout(tmp_path)))
    assert len(calls) == 2
    assert [i["type"] for i in calls[0].items] == ["message"]  # the response's own items are not in its request
    assert [i["type"] for i in calls[1].items] == ["message", "reasoning", "function_call", "function_call_output"]
    assert calls[1].counts == {"input": 57_000, "cw5": 0, "cw1h": 0, "cr": 1_000, "output": 900, "session": "sess-1"}
    assert calls[1].model == "gpt-6-sol"


def test_estimate_condenses_new_tool_output(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["rewrite_budget_ms"] = 0  # a slow machine: offline, the budget must not apply
    totals = codexlog.estimate([rollout(tmp_path)], cfg)
    assert totals.calls == 2 and totals.rules.get("spill") == 1
    assert 0 < totals.estimate < totals.baseline
    out = codexlog.render(totals)
    assert "2 API calls" in out and "saving" in out
    assert codexlog.render(codexlog.CodexTotals(files=3)) == "Codex: no API calls found in 3 rollout files."


def test_codex_compaction_replaces_the_history(tmp_path):
    path = rollout(tmp_path)
    summary = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary"}]}
    with path.open("a", encoding="utf-8") as f:
        f.write(line("compacted", {"message": "", "replacement_history": [summary]}) + "\n")
        f.write(line("response_item", {"type": "message", "role": "assistant", "content": []}) + "\n")
        f.write(tokens(70_000, 5_000, 0, 50) + "\n")
    last = list(codexlog.calls_in(path))[-1]
    assert last.items == [summary]
