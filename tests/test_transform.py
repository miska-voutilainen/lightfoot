import copy
import json
import time
from pathlib import Path

import pytest
from conftest import big_output, read_output, request, tool_result, tool_use, user_text

from lightfoot.adapters import ANTHROPIC
from lightfoot.server import dumps
from lightfoot.transform import plan_request, session_key
from lightfoot.validate import ValidationError, validate


def session(store, body, mode="live", last_request=None, turns=0):
    sid = session_key(body)
    s = store.get_or_create_session(sid, None, mode, time.time())
    s.last_request = last_request
    return s


def run(store, make_ctx, body, mode="live", **kw):
    s = kw.pop("sess", None) or session(
        store, body, mode if mode in ("live", "shadow") else "live", kw.pop("last_request", None)
    )
    ctx = make_ctx(mode=mode, session=s, **kw)
    plan = plan_request(body, ctx)
    store.put_rewrites(plan.new_rewrites)
    return plan


def result_content(body, mi):
    return body["messages"][mi]["content"][0]["content"]


# --- ingest -----------------------------------------------------------------------


def test_large_output_is_spilled_with_errors_kept(store, make_ctx, tmp_path):
    output = big_output()
    body = request(
        [user_text("run the tests"), tool_use("t1", "Bash", {"command": "pytest"}), tool_result("t1", output)]
    )
    plan = run(store, make_ctx, body)
    assert plan.rules == {"spill": 1}
    new = result_content(plan.body_out, 2)
    assert "FAILED tests/test_mod.py::test_case_1000 - AssertionError: expected 3, got 4" in new
    assert "\x1b[" not in new
    assert len(new) < len(output) / 4
    spill = list((tmp_path / "proj" / ".lightfoot" / "spill").glob("*.log"))
    assert len(spill) == 1 and str(spill[0]) in new
    assert spill[0].read_text(encoding="utf-8").count("\n") == 1999  # full output, ANSI stripped
    assert (tmp_path / "proj" / ".lightfoot" / ".gitignore").read_text() == "*\n"


def test_same_request_twice_gives_identical_bytes(store, make_ctx):
    body = request([user_text("go"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", big_output())])
    first = run(store, make_ctx, copy.deepcopy(body))
    second = run(store, make_ctx, copy.deepcopy(body))
    assert second.new_rewrites == []  # replayed, not recomputed
    assert dumps(first.body_out) == dumps(second.body_out)


def test_rewrite_is_frozen_on_later_turns(store, make_ctx):
    msgs = [user_text("go"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", big_output())]
    first = run(store, make_ctx, request(copy.deepcopy(msgs)))
    msgs += [tool_use("t2", "Bash", {"command": "ls"}), tool_result("t2", "a\nb")]
    later = run(store, make_ctx, request(copy.deepcopy(msgs)))
    assert result_content(later.body_out, 2) == result_content(first.body_out, 2)
    assert later.new_rewrites == []


SYSTEM_NOTE = {
    "role": "system",
    "content": [{"type": "text", "text": "<total_tokens>14999985 tokens left</total_tokens>"}],
}


def test_a_trailing_system_message_does_not_hide_new_output(store, make_ctx):
    # Claude Code 2.1.310+ ends requests with a system note after the tool results.
    body = request(
        [
            user_text("run the tests"),
            {"role": "system", "content": [{"type": "text", "text": "# Environment"}]},
            tool_use("t1", "Bash", {"command": "pytest"}),
            tool_result("t1", big_output()),
            SYSTEM_NOTE,
        ]
    )
    plan = run(store, make_ctx, body)
    assert plan.rules == {"spill": 1}
    assert plan.body_out["messages"][4] is body["messages"][4]  # the system note itself is untouched


def test_only_the_newest_message_is_ingested(store, make_ctx):
    # A resumed session: old results the model already saw raw must stay raw.
    msgs = [
        user_text("go"),
        tool_use("t1", "Bash", {"command": "make"}),
        tool_result("t1", big_output()),
        tool_use("t2", "Bash", {"command": "ls"}),
        tool_result("t2", "small"),
    ]
    plan = run(store, make_ctx, request(msgs))
    assert plan.body_out is None and plan.new_rewrites == []


def test_reads_errors_and_images_are_never_ingested(store, make_ctx):
    image = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]
    for name, content, is_error in [
        ("Read", read_output(3000), False),
        ("Bash", big_output(), True),
        ("Bash", image, False),
    ]:
        body = request(
            [
                user_text("x"),
                tool_use("t-" + name + str(is_error), name, {"file_path": "/a.py"}),
                tool_result("t-" + name + str(is_error), content, is_error=is_error),
            ]
        )
        assert run(store, make_ctx, body).body_out is None


def test_noise_rule_on_small_output(store, make_ctx):
    out = "\x1b[1mBuilding\x1b[0m\n" + "warning: unused import\n" * 10 + "done"
    body = request([user_text("x"), tool_use("t1", "Bash", {"command": "cargo build"}), tool_result("t1", out)])
    plan = run(store, make_ctx, body)
    assert plan.rules == {"noise": 1}
    assert result_content(plan.body_out, 2) == (
        "Building\nwarning: unused import\n[lightfoot: previous line repeated 9 more times]\ndone"
    )


def test_cache_marker_is_carried_and_hash_ignores_it(store, make_ctx):
    inner = [{"type": "text", "text": big_output(), "cache_control": {"type": "ephemeral"}}]
    msgs = [user_text("x"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", inner)]
    plan = run(store, make_ctx, request(copy.deepcopy(msgs)))
    new = result_content(plan.body_out, 2)
    assert len(new) == 1 and new[0]["cache_control"] == {"type": "ephemeral"}
    # Next turn: Claude Code moved the marker to the newest message. Replay must follow it.
    del msgs[2]["content"][0]["content"][0]["cache_control"]
    msgs += [
        tool_use("t2", "Bash", {"command": "ls"}),
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "ok", "cache_control": {"type": "ephemeral"}}
            ],
        },
    ]
    later = run(store, make_ctx, request(msgs))
    replayed = result_content(later.body_out, 2)
    assert "cache_control" not in replayed[0] and replayed[0]["text"] == new[0]["text"]


def test_changed_content_is_not_replayed(store, make_ctx):
    msgs = [user_text("x"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", big_output())]
    run(store, make_ctx, request(copy.deepcopy(msgs)))
    msgs[2]["content"][0]["content"] = "[Old tool result content cleared]"
    msgs += [tool_use("t2", "Bash", {"command": "ls"}), tool_result("t2", "ok")]
    plan = run(store, make_ctx, request(msgs))
    assert plan.body_out is None
    assert any("hash mismatch" in n for n in plan.notes)


def test_shadow_and_live_rewrites_are_separate(store, make_ctx):
    body = request([user_text("x"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", big_output())])
    shadow = run(store, make_ctx, copy.deepcopy(body), mode="shadow")
    assert shadow.body_out is not None and all(not r.live for r in shadow.new_rewrites)
    live = run(store, make_ctx, copy.deepcopy(body), mode="live")
    assert live.new_rewrites and all(r.live for r in live.new_rewrites)


def test_freeze_replays_but_creates_nothing(store, make_ctx):
    msgs = [user_text("x"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", big_output())]
    run(store, make_ctx, request(copy.deepcopy(msgs)))
    msgs += [tool_use("t2", "Bash", {"command": "make again"}), tool_result("t2", big_output())]
    plan = run(store, make_ctx, request(msgs), mode="freeze")
    assert plan.new_rewrites == []
    assert result_content(plan.body_out, 2) != msgs[2]["content"][0]["content"]
    assert result_content(plan.body_out, 4) == msgs[4]["content"][0]["content"]


def test_over_budget_drops_new_rewrites(store, make_ctx, cfg):
    cfg["rewrite_budget_ms"] = -1
    body = request([user_text("x"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", big_output())])
    plan = run(store, make_ctx, body, config=cfg)
    assert plan.body_out is None and plan.new_rewrites == []


# --- epochs -------------------------------------------------------------------------


def stale_read_conversation(thinking=False, filler_rounds=4):
    msgs = [user_text("refactor config.py")]
    msgs += [
        tool_use("r1", "Read", {"file_path": "/repo/config.py"}, thinking=thinking),
        tool_result("r1", read_output(400)),
    ]
    msgs += [
        tool_use("r2", "Read", {"file_path": "/repo/config.py"}, thinking=thinking),
        tool_result("r2", read_output(400)),
    ]
    for i in range(filler_rounds):
        msgs += [tool_use(f"f{i}", "Bash", {"command": f"echo {i}"}, thinking=thinking), tool_result(f"f{i}", f"{i}")]
    return msgs


def test_cold_epoch_stubs_superseded_read(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 500
    body = request(stale_read_conversation())
    plan = run(store, make_ctx, body, config=cfg, last_request=time.time() - 3600)
    assert plan.rules == {"stale_reads": 1}
    assert plan.epoch and plan.epoch["cold"]
    stub = result_content(plan.body_out, 2)
    assert stub.startswith("[lightfoot: superseded read of /repo/config.py (lines 1-400)")
    assert result_content(plan.body_out, 4) == body["messages"][4]["content"][0]["content"]  # newest stays whole


def test_epoch_never_touches_protected_turns(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 500
    body = request(stale_read_conversation(filler_rounds=1))  # the first read is within the last 3 turns
    plan = run(store, make_ctx, body, config=cfg, last_request=time.time() - 3600)
    assert plan.body_out is None


def test_warm_epoch_needs_break_even(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens_warm"] = 500
    body = request(stale_read_conversation())
    plan = run(store, make_ctx, body, config=cfg, last_request=time.time() - 10)
    # Warm cache, short session: the rewrite would not pay for itself.
    assert plan.body_out is None
    assert plan.epoch is None


def test_enforced_account_skips_epochs_before_thinking(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 500
    body = request(stale_read_conversation(thinking=True), thinking={"type": "adaptive"})
    plan = run(store, make_ctx, body, config=cfg, enforced=True, last_request=time.time() - 3600)
    assert plan.body_out is None
    assert any("thinking blocks follow" in n for n in plan.notes)


def test_enforced_account_with_drop_block(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 500
    cfg["epochs"]["enforced_accounts"] = "drop_block"
    body = request(stale_read_conversation(thinking=True), thinking={"type": "adaptive"})
    plan = run(store, make_ctx, body, config=cfg, enforced=True, last_request=time.time() - 3600)
    assert plan.drop_block
    assert plan.body_out["thinking"] == {
        "type": "adaptive",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert body["thinking"] == {"type": "adaptive"}  # the original is untouched


def test_unenforced_account_compacts_despite_thinking(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 500
    body = request(stale_read_conversation(thinking=True), thinking={"type": "adaptive"})
    plan = run(store, make_ctx, body, config=cfg, enforced=False, last_request=time.time() - 3600)
    assert plan.rules == {"stale_reads": 1} and not plan.drop_block


def test_identical_search_repeat(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 100
    hits = "\n".join(f"src/module_{i}.py" for i in range(200))
    msgs = [
        user_text("find it"),
        tool_use("g1", "Glob", {"pattern": "**/*.py"}),
        tool_result("g1", hits),
        tool_use("g2", "Glob", {"pattern": "**/*.py"}),
        tool_result("g2", hits),
    ]
    for i in range(4):
        msgs += [tool_use(f"f{i}", "Bash", {"command": f"echo {i}"}), tool_result(f"f{i}", str(i))]
    plan = run(store, make_ctx, request(msgs), config=cfg, last_request=time.time() - 3600)
    assert plan.rules == {"searches": 1}
    assert "repeated with identical arguments" in result_content(plan.body_out, 2)


def test_old_grep_reduced_to_paths_after_hits_opened(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 100
    grep = "\n".join(f"src/app/handlers.py:{i}:    result = handle_request(request_{i})" for i in range(1, 150))
    msgs = [
        user_text("where is handle_request"),
        tool_use("g1", "Grep", {"pattern": "handle_request", "output_mode": "content", "-n": True}),
        tool_result("g1", grep),
        tool_use("r1", "Read", {"file_path": "/repo/src/app/handlers.py"}),
        tool_result("r1", read_output(20)),
    ]
    for i in range(9):
        msgs += [tool_use(f"f{i}", "Bash", {"command": f"echo {i}"}), tool_result(f"f{i}", str(i))]
    plan = run(store, make_ctx, request(msgs), config=cfg, last_request=time.time() - 3600)
    assert plan.rules.get("searches") == 1
    assert result_content(plan.body_out, 2).endswith("src/app/handlers.py (149 matches)")


def test_old_unreferenced_result_is_shrunk_but_referenced_kept(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 100
    log_lines = "\n".join(f"worker_pool_manager started job_{i} ok" for i in range(800))
    msgs = [
        user_text("check the logs"),
        tool_use("b1", "Bash", {"command": "cat service_runtime.log"}),
        tool_result("b1", log_lines),
    ]
    for i in range(11):
        msgs += [tool_use(f"f{i}", "Bash", {"command": f"echo {i}"}), tool_result(f"f{i}", str(i))]
    plan = run(store, make_ctx, request(copy.deepcopy(msgs)), config=cfg, last_request=time.time() - 3600)
    assert plan.rules == {"old_results": 1}
    shortened = result_content(plan.body_out, 2)
    assert "nothing since refers to it" in shortened
    full = Path(shortened.split("Full output: ")[1].rstrip("]"))
    assert full.read_text(encoding="utf-8") == log_lines  # the note names a file that really holds it

    # Same history, but a later message mentions an identifier from the output: keep it.
    store.restore_rewrites(plan.new_rewrites, [])
    msgs2 = copy.deepcopy(msgs)
    msgs2[-2]["content"].insert(0, {"type": "text", "text": "the worker_pool_manager looks fine"})
    plan2 = run(store, make_ctx, request(msgs2), config=cfg, last_request=time.time() - 3600)
    assert "old_results" not in plan2.rules


def test_epoch_gap_between_epochs(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 500
    body = request(stale_read_conversation())
    s = session(store, body, last_request=time.time() - 3600)
    s.epochs, s.last_epoch_turn = 1, 6
    plan = run(store, make_ctx, body, config=cfg, sess=s)
    assert plan.body_out is None


# --- validator ------------------------------------------------------------------------

PATH = ("messages", 2, "content", 0, "content")
ORIG_TEXT = "Error: boom\nmore\nmore\nmore"


def _valid_pair():
    orig = request([user_text("x"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", ORIG_TEXT)])
    out = copy.deepcopy(orig)
    out["messages"][2]["content"][0]["content"] = "Error: boom\nmore"
    meta = {PATH: ("noise", "new", 2, ORIG_TEXT, "Error: boom\nmore")}
    return orig, out, meta


def test_validator_accepts_a_good_rewrite():
    orig, out, meta = _valid_pair()
    validate(orig, out, meta, 99, ANTHROPIC)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o["messages"][0]["content"][0].update(text="changed user text"),
        lambda o: o["system"].append({"type": "text", "text": "extra"}),
        lambda o: o["messages"][2]["content"][0].update(tool_use_id="other"),
        lambda o: o["messages"][2]["content"][0].update(cache_control={"type": "ephemeral"}),
        lambda o: o["messages"][1]["content"][0]["input"].update(command="rm -rf /"),
        lambda o: o.update(model="claude-other"),
        lambda o: o["messages"].pop(),
    ],
)
def test_validator_rejects(mutate):
    orig, out, meta = _valid_pair()
    mutate(out)
    with pytest.raises(ValidationError):
        validate(orig, out, meta, 99, ANTHROPIC)


def test_validator_rejects_dropped_error_line():
    orig, out, meta = _valid_pair()
    out["messages"][2]["content"][0]["content"] = "more"
    meta[PATH] = ("noise", "new", 2, ORIG_TEXT, "more")
    with pytest.raises(ValidationError):
        validate(orig, out, meta, 99, ANTHROPIC)


def test_validator_rejects_epoch_rule_in_protected_turns():
    orig, out, meta = _valid_pair()
    meta[PATH] = ("old_results", "new", 2, ORIG_TEXT, "Error: boom\nmore")
    with pytest.raises(ValidationError):
        validate(orig, out, meta, 0, ANTHROPIC)


def test_validator_rejects_rewritten_error_result():
    orig, out, meta = _valid_pair()
    orig["messages"][2]["content"][0]["is_error"] = True
    out["messages"][2]["content"][0]["is_error"] = True
    with pytest.raises(ValidationError):
        validate(orig, out, meta, 99, ANTHROPIC)


def test_apply_changes_shares_untouched_parts():
    from lightfoot.adapters import apply_changes

    body = request([user_text("x"), tool_use("t1", "Bash", {"command": "make"}), tool_result("t1", "abc")])
    out = apply_changes(body, {PATH: "a"})
    assert out["messages"][2]["content"][0]["content"] == "a"
    assert body["messages"][2]["content"][0]["content"] == "abc"  # original untouched
    assert out["messages"][0] is body["messages"][0] and out["system"] is body["system"]


def test_performance_on_a_large_session(store, make_ctx, cfg):
    # Measures and prints the planning time; the real time budgets would make the outcome depend on
    # machine load (a slow run correctly drops the rewrite), so they are lifted here.
    cfg["rewrite_budget_ms"] = 10_000
    cfg["epochs"]["budget_ms"] = 10_000
    msgs = [user_text("big session")]
    for i in range(150):
        msgs += [
            tool_use(f"r{i}", "Read", {"file_path": f"/repo/file_{i % 40}.py"}, thinking=True),
            tool_result(f"r{i}", read_output(300)),
        ]
    msgs += [tool_use("last", "Bash", {"command": "pytest"}), tool_result("last", big_output(3000))]
    body = request(msgs, thinking={"type": "adaptive"})
    raw = json.dumps(body).encode()
    assert len(raw) > 2_000_000
    started = time.perf_counter()
    json.loads(raw)
    plan = run(store, make_ctx, body, last_request=time.time() - 3600, config=cfg)
    elapsed = (time.perf_counter() - started) * 1000
    assert plan.rules.get("spill") == 1
    print(f"plan on {len(raw) / 1e6:.1f} MB request: {elapsed:.0f} ms, rules {plan.rules}")


# --- json rule ---------------------------------------------------------------------


def _pretty_payload(n=60):
    return json.dumps({"results": [{"id": i, "name": f"item {i}", "tags": ["a", "b"]} for i in range(n)]}, indent=2)


def test_pretty_json_from_an_mcp_tool_is_minified_losslessly(store, make_ctx):
    pretty = _pretty_payload()
    body = request([user_text("list"), tool_use("t1", "mcp__api__list", {}), tool_result("t1", pretty)])
    plan = run(store, make_ctx, body)
    assert plan.rules == {"json": 1}
    new = result_content(plan.body_out, 2)
    assert "\n" not in new and json.loads(new) == json.loads(pretty)


def test_json_rule_skips_file_reads_and_errors(store, make_ctx):
    pretty = _pretty_payload()
    body = request(
        [
            user_text("go"),
            tool_use("t1", "mcp__api__list", {}),
            tool_result("t1", pretty, is_error=True),
            tool_use("t2", "Read", {"file_path": "/r/data.json"}),
            tool_result("t2", pretty),
        ]
    )
    plan = run(store, make_ctx, body)
    assert "json" not in plan.rules


def test_json_too_large_even_minified_falls_through_to_spill(store, make_ctx, cfg):
    cfg["thresholds"]["spill_tokens"] = 500
    pretty = _pretty_payload(400)
    body = request([user_text("dump"), tool_use("t1", "Bash", {"command": "curl api"}), tool_result("t1", pretty)])
    plan = run(store, make_ctx, body, config=cfg)
    assert plan.rules == {"spill": 1}


def test_validator_rejects_a_json_rewrite_that_changes_a_value():
    pretty = '{\n  "a": 1,\n  "b": [1, 2]\n}'
    body = request([user_text("x"), tool_use("t1", "mcp__api__get", {}), tool_result("t1", pretty)])
    out = copy.deepcopy(body)
    out["messages"][2]["content"][0]["content"] = '{"a":2,"b":[1,2]}'
    path = ("messages", 2, "content", 0, "content")
    with pytest.raises(ValidationError, match="json minification changed a value"):
        validate(body, out, {path: ("json", "new", 2, pretty, '{"a":2,"b":[1,2]}')}, 3, ANTHROPIC)
