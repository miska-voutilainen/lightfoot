"""`lightfoot estimate --agent gemini`: Gemini CLI session files, replayed offline.

The records follow Gemini CLI's chatRecordingService.ts; the shapes match public session files.
"""

import copy
import json

from conftest import big_output

from lightfoot import geminilog
from lightfoot.config import DEFAULTS

TOKENS = {"input": 12000, "output": 40, "cached": 8000, "thoughts": 20, "tool": 0, "total": 12060}


def shell_call(cid, output, command="pytest -q"):
    response = {"functionResponse": {"id": cid, "name": "run_shell_command", "response": {"output": output}}}
    return {"id": cid, "name": "run_shell_command", "args": {"command": command}, "result": [response]}


def gemini(mid, text="", calls=(), tokens=TOKENS, model="gemini-2.5-pro"):
    return {"id": mid, "type": "gemini", "content": text, "toolCalls": list(calls), "tokens": tokens, "model": model}


def user(mid, text):
    return {"id": mid, "type": "user", "content": [{"text": text}]}


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


META = {"sessionId": "s-1", "projectHash": "abc", "startTime": "2026-10-26T10:00:00Z"}


def test_records_replay_like_gemini_cli(tmp_path):
    untokened = {**gemini("g1", calls=[shell_call("c1", "first")]), "tokens": None}
    records = [
        META,
        user("u1", "run the tests"),
        untokened,
        gemini("g1", calls=[shell_call("c1", "first")]),  # tokens arrive: same id, replaced in place
        {"$patch": {"id": "g1", "toolCalls": [{"id": "c1", "result": shell_call("c1", "patched")["result"]}]}},
        {"id": "i1", "type": "info", "content": "UI only"},
        user("u2", "try again"),
        gemini("g2", "done"),
        {"$rewindTo": "u2"},  # u2 and everything after it go
        {"$set": {"lastUpdated": "2026-10-26T10:05:00Z"}},
    ]
    session, messages = geminilog.load_messages(write_jsonl(tmp_path / "s.jsonl", records).read_text())
    assert session == "s-1"
    assert [m["id"] for m in messages] == ["u1", "g1", "i1"]
    assert messages[1]["tokens"] == TOKENS
    output = messages[1]["toolCalls"][0]["result"][0]["functionResponse"]["response"]["output"]
    assert output == "patched"


def test_remove_order_and_legacy_checkpoints(tmp_path):
    records = [META, user("u1", "a"), user("u2", "b"), user("u3", "c"), {"$patch": {"removeIds": ["u2"]}}]
    records.append({"$patch": {"orderIds": ["u1"]}})  # listed ids move to the end, in that order
    _, messages = geminilog.load_messages(write_jsonl(tmp_path / "a.jsonl", records).read_text())
    assert [m["id"] for m in messages] == ["u3", "u1"]
    records = [META, user("u1", "a"), {"$set": {"messages": [user("x1", "replaced")]}}]
    _, messages = geminilog.load_messages(write_jsonl(tmp_path / "b.jsonl", records).read_text())
    assert [m["id"] for m in messages] == ["x1"]  # a legacy full-history checkpoint replaces everything


def test_older_single_json_files(tmp_path):
    legacy = {**META, "messages": [user("u1", "edit test.go"), gemini("g1", "Done.")]}
    path = tmp_path / "session-2026-03-19T00-18-69583a22.json"
    path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")  # pretty-printed, as older versions wrote
    calls = list(geminilog.calls_in(path))
    assert len(calls) == 1 and calls[0].contents == [{"role": "user", "parts": [{"text": "edit test.go"}]}]


def test_each_call_is_sent_with_every_turn_before_it(tmp_path):
    records = [
        META,
        user("u1", "run the tests"),
        gemini("g1", calls=[shell_call("c1", "3 passed")]),
        gemini("g2", "ok"),
    ]
    calls = list(geminilog.calls_in(write_jsonl(tmp_path / "s.jsonl", records)))
    assert [len(c.contents) for c in calls] == [1, 3]  # user; user, model call, its function response
    model_turn, results = calls[1].contents[1], calls[1].contents[2]
    assert model_turn == {
        "role": "model",
        "parts": [{"functionCall": {"id": "c1", "name": "run_shell_command", "args": {"command": "pytest -q"}}}],
    }
    assert results["parts"][0]["functionResponse"]["id"] == "c1"
    # Tokens: the prompt includes cached tokens; thoughts are billed as output.
    assert calls[0].counts == {"input": 4000, "cw5": 0, "cw1h": 0, "cr": 8000, "output": 60, "session": "s-1"}


def test_malformed_recordings_are_skipped(tmp_path):
    bad = {"id": "c1", "name": "read_file", "args": "{'file_path': 'x'}", "result": "[{'functionResponse': {}}]"}
    records = [META, user("u1", "read it"), gemini("g1", calls=[bad]), gemini("g2", "ok"), "not an object"]
    path = write_jsonl(tmp_path / "s.jsonl", records[:-1])
    path.write_text(path.read_text() + "{broken json\n", encoding="utf-8")
    calls = list(geminilog.calls_in(path))
    assert [len(c.contents) for c in calls] == [1, 1]  # the bad call and result are left out, nothing crashes


def test_estimate_condenses_a_large_shell_output(tmp_path):
    big = big_output()
    records = [
        META,
        user("u1", "run the tests"),
        gemini("g1", calls=[shell_call("c1", big)]),
        gemini("g2", calls=[shell_call("c2", "1 passed", "pytest -q tests/test_mod.py")]),
        gemini("g3", "Fixed."),
    ]
    cfg = copy.deepcopy(DEFAULTS)
    cfg["rewrite_budget_ms"] = 0  # a slow machine: offline, the budget must not apply
    totals = geminilog.estimate([write_jsonl(tmp_path / "s.jsonl", records)], cfg)
    assert totals.calls == 3 and totals.models == {"gemini-2.5-pro"}
    assert totals.rules.get("spill") == 1
    assert 0 < totals.estimate < totals.baseline
    assert "saving" in geminilog.render(totals)


def test_session_files_are_found_in_chats_folders(tmp_path):
    chats = tmp_path / "proj" / "chats"
    (chats / "parent-1").mkdir(parents=True)
    for name in ("session-a.jsonl", "session-b.json", "parent-1/session-c.jsonl", "notes.json"):
        (chats / name).write_text("{}", encoding="utf-8")
    (tmp_path / "proj" / "logs.json").write_text("[]", encoding="utf-8")
    found = [p.relative_to(chats).as_posix() for p in geminilog.session_files(tmp_path)]
    assert found == ["parent-1/session-c.jsonl", "session-a.jsonl", "session-b.json"]
