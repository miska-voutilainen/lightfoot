"""`lightfoot estimate --agent opencode`: opencode's SQLite sessions, replayed offline.

The tables and JSON shapes match a real opencode 1.19.4 database.
"""

import copy
import json
import sqlite3
import time

from conftest import big_output

from lightfoot import opencodelog
from lightfoot.config import DEFAULTS

TOKENS = {"total": 0, "input": 300, "output": 40, "reasoning": 10, "cache": {"write": 2000, "read": 9000}}


def make_db(path, messages, credential="stand-in credential"):
    """messages: (id, session, role, [parts]); parts are part data dicts."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE message (id text PRIMARY KEY, session_id text, time_created integer, time_updated integer, data text)"
    )
    con.execute(
        "CREATE TABLE part (id text PRIMARY KEY, message_id text, session_id text, time_created integer, time_updated integer, data text)"
    )
    con.execute("CREATE TABLE credential (id text, value text)")  # never read
    con.execute("INSERT INTO credential VALUES ('c', ?)", (credential,))
    now = int(time.time() * 1000)
    for n, (mid, session, role, parts) in enumerate(messages):
        info = {
            "role": role,
            **({"modelID": "claude-sonnet-4-5", "providerID": "anthropic"} if role == "assistant" else {}),
        }
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, session, now + n, now + n, json.dumps(info)))
        for k, part in enumerate(parts):
            con.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (f"{mid}_p{k:02}", mid, session, now, now, json.dumps(part)),
            )
    con.commit()
    con.close()
    return path


def tool(call_id, output, command="pytest -q", status="completed"):
    state = {"status": status, "input": {"command": command}}
    state["error" if status == "error" else "output"] = output
    return {"type": "tool", "tool": "bash", "callID": call_id, "state": state}


def step(*parts, tokens=TOKENS):
    return [{"type": "step-start"}, *parts, {"type": "step-finish", "reason": "tool-calls", "tokens": tokens}]


def session_rows(db):
    return list(opencodelog._rows(db, 0).items())


def test_each_step_is_one_call_sent_with_every_turn_before_it(tmp_path):
    db = make_db(
        tmp_path / "opencode.db",
        [
            ("m1", "s1", "user", [{"type": "text", "text": "run the tests"}]),
            (
                "m2",
                "s1",
                "assistant",
                [
                    *step(tool("t1", "1 failed")),
                    *step({"type": "text", "text": "Fixing."}, tool("t2", "boom", status="error")),
                    *step({"type": "text", "text": "Done."}),
                ],
            ),
        ],
    )
    [(session, messages)] = session_rows(db)
    calls = list(opencodelog.calls_in(session, messages))
    assert [len(c.messages) for c in calls] == [1, 3, 5]
    shapes = [(m["role"], [b["type"] for b in m["content"]]) for m in calls[2].messages]
    assert shapes == [
        ("user", ["text"]),
        ("assistant", ["tool_use"]),
        ("user", ["tool_result"]),
        ("assistant", ["text", "tool_use"]),
        ("user", ["tool_result"]),
    ]
    assert calls[2].messages[4]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t2",
        "content": "boom",
        "is_error": True,
    }
    # input is uncached; reasoning bills as output; cache writes go to the 5-minute cache.
    assert calls[0].counts == {"input": 300, "cw5": 2000, "cw1h": 0, "cr": 9000, "output": 50}
    assert calls[0].model == "claude-sonnet-4-5"


def test_estimate_condenses_a_large_shell_output(tmp_path):
    db = make_db(
        tmp_path / "opencode.db",
        [
            ("m1", "s1", "user", [{"type": "text", "text": "run the tests"}]),
            (
                "m2",
                "s1",
                "assistant",
                [
                    *step(tool("t1", big_output())),
                    *step(tool("t2", "1 passed")),
                    *step({"type": "text", "text": "Fixed."}),
                ],
            ),
        ],
    )
    cfg = copy.deepcopy(DEFAULTS)
    cfg["rewrite_budget_ms"] = 0  # a slow machine: offline, the budget must not apply
    totals = opencodelog.estimate(db, config=cfg)
    assert (totals.sessions, totals.calls) == (1, 3)
    assert totals.rules.get("spill") == 1
    assert 0 < totals.estimate < totals.baseline
    assert "saving" in opencodelog.render(totals)


def test_unfinished_tools_and_other_parts_are_left_out(tmp_path):
    running = tool("t1", "", status="running")
    db = make_db(
        tmp_path / "opencode.db",
        [
            ("m1", "s1", "user", [{"type": "text", "text": "go"}, {"type": "file", "url": "data:..."}]),
            (
                "m2",
                "s1",
                "assistant",
                [*step({"type": "reasoning", "text": "hmm"}, running), *step({"type": "text", "text": "ok"})],
            ),
        ],
    )
    [(session, messages)] = session_rows(db)
    calls = list(opencodelog.calls_in(session, messages))
    assert [len(c.messages) for c in calls] == [1, 1]  # the running tool and the reasoning aren't replayed


def test_old_sessions_and_a_missing_database(tmp_path):
    db = make_db(tmp_path / "opencode.db", [("m1", "s1", "user", [{"type": "text", "text": "go"}])])
    assert opencodelog._rows(db, since=time.time() + 3600) == {}
    assert opencodelog.estimate(tmp_path / "missing.db").calls == 0


def test_the_database_is_opened_read_only(tmp_path):
    db = make_db(tmp_path / "opencode.db", [("m1", "s1", "user", [{"type": "text", "text": "go"}])])
    before = db.read_bytes()
    opencodelog.estimate(db)
    assert db.read_bytes() == before


def test_default_db_follows_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert opencodelog.default_db() == tmp_path / "opencode" / "opencode.db"
