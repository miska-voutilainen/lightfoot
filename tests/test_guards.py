"""Quality guards: condense less when Claude keeps going back for condensed content."""

import copy
import time

from conftest import big_output, request, tool_result, tool_use, user_text

from lightfoot import guards
from lightfoot.config import DEFAULTS
from lightfoot.transform import plan_request, session_key

LIMITS = DEFAULTS["guards"]


def test_pruning_escalates_watch_keep_more_stop_and_watch_calms_down():
    state = guards.new_state()
    assert guards.update(state, [("history_read", "")], 10, False, LIMITS) == ["watch (after history_read at turn 10)"]
    assert guards.update(state, [], 25, False, LIMITS) == []
    assert guards.update(state, [], 30, False, LIMITS) == ["normal (quiet since turn 10)"]
    guards.update(state, [("history_read", ""), ("re_read", "")], 40, False, LIMITS)
    assert state["level"] == 2  # keep more
    guards.update(state, [], 50, True, LIMITS)  # the next prune keeps more
    guards.update(state, [("retrieval", "")], 60, False, LIMITS)
    assert state["level"] == 2
    assert guards.update(state, [("history_read", "")], 61, False, LIMITS) == ["stop (after history_read at turn 61)"]


def test_trimming_stops_per_tool():
    state = guards.new_state()
    guards.update(state, [("spill_read", "Bash")], 5, False, LIMITS)
    assert state["off"] == []
    steps = guards.update(state, [("re_run", "Bash"), ("spill_read", "mcp__db__query")], 6, False, LIMITS)
    assert steps == ["stopped trimming Bash output (after re_run at turn 6)"] and state["level"] == 0


def test_adjust():
    cfg = copy.deepcopy(DEFAULTS)
    state = guards.new_state()
    assert guards.adjust(cfg, state) is cfg
    state.update(level=2, off=["Bash"])
    two = guards.adjust(cfg, state)
    assert two["prune"]["keep_tokens"] == 2 * cfg["prune"]["keep_tokens"] and two["prune"]["enabled"]
    assert "Bash" in two["exclude"]["spill"] and "Bash" not in cfg["exclude"]["spill"]
    state["level"] = 3
    three = guards.adjust(cfg, state)
    assert not three["prune"]["enabled"] and not three["epochs"]["enabled"]


def plan(store, make_ctx, body):
    session = store.get_or_create_session(session_key(body), None, "live", time.time())
    return plan_request(body, make_ctx(mode="live", session=session))


def conversation(between=()):
    return [
        user_text("run the tests"),
        tool_use("t1", "Bash", {"command": "pytest -q"}),
        tool_result("t1", big_output()),
        *between,
        tool_use("t2", "Bash", {"command": "pytest -q"}),
        tool_result("t2", "1 failed"),
    ]


def test_re_run_counts_only_when_nothing_could_have_changed_the_output(store, make_ctx):
    first = plan(store, make_ctx, request(conversation()[:3]))
    store.put_rewrites(first.new_rewrites)
    assert first.rules == {"spill": 1}
    again = plan(store, make_ctx, request(conversation()))
    assert again.guard_signals == [("re_run", "Bash")]
    assert [s[2] for s in again.signals] == ["re_run"]
    edit = [tool_use("e", "Edit", {"file_path": "/p/a.py"}), tool_result("e", "ok")]
    after_edit = plan(store, make_ctx, request(conversation(edit)))
    assert after_edit.guard_signals == []  # re-running tests after a fix is normal work


def test_a_tool_whose_trims_were_needed_is_left_whole(store, make_ctx):
    body = request(conversation()[:3])
    state = guards.new_state()
    state["off"] = ["Bash"]
    guards.save(store, session_key(body), state)
    rules = plan(store, make_ctx, body).rules
    assert "spill" not in rules and rules.get("noise") == 1  # not trimmed; ANSI codes still stripped


def test_server_records_guard_steps(store, make_ctx):
    from types import SimpleNamespace

    from lightfoot.server import Proxy

    proxy = Proxy(store)
    state = guards.new_state()
    fake = SimpleNamespace(
        guard_state=state, guard_signals=[("history_read", "")], turns=12, new_prune=None, guard_pre_rate=None
    )
    proxy.update_guards("sid", fake, DEFAULTS)
    assert guards.load(store, "sid")["level"] == 1
    events = store.query("SELECT detail FROM events WHERE kind = 'guard'")
    assert [e["detail"] for e in events] == ["watch (after history_read at turn 12)"]


def test_stats_counts_guarded_sessions(store):
    from lightfoot.reports import guard_report

    for sid, level, off in (("a", 2, []), ("b", 0, ["Bash"]), ("c", 3, []), ("old", 3, [])):
        state = guards.new_state()
        state.update(level=level, off=off)
        guards.save(store, sid, state)
    assert guard_report(store, {"a", "b", "c"}) == [
        "Quality guards: 1 at keep more, 1 at stop, 1 tools no longer trimmed (see `lightfoot why`)"
    ]
    assert guard_report(store, {"nothing"}) == []


def test_early_trigger_needs_a_summary_that_held_up(store, make_ctx, cfg):
    from lightfoot.store import Prune
    from lightfoot.transform import _summary_holds_up

    stored = Prune("sid", True, 5, "fp", "summary", None, 1000)
    live = make_ctx(mode="live")
    assert not _summary_holds_up(live, None)  # the first prune always waits for the normal trigger
    assert _summary_holds_up(live, stored)  # a quiet session
    state = guards.new_state()
    state["since_prune"] = 1
    guards.save(store, "sid", state)
    assert not _summary_holds_up(live, stored)  # Claude went back for pruned content
    assert not _summary_holds_up(make_ctx(mode="shadow"), stored)
    cfg["prune"]["summarizer"] = "model"
    assert _summary_holds_up(make_ctx(mode="live", config=cfg), None)
    assert not _summary_holds_up(make_ctx(mode="live", config=cfg, consumer_auth=True), None)
