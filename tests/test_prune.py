"""Old-turn pruning: quality guards, frozen replay, and the economics gate."""

import copy
import json
import time

import httpx
from conftest import lift_budgets, request, tool_result, tool_use, user_text

from lightfoot import prune
from lightfoot.estimate import counterfactual
from lightfoot.server import Proxy, build_app, dumps
from lightfoot.store import Launch
from lightfoot.transform import Plan, plan_request, session_key

BLOB = "compiled module output line with some detail\n" * 180  # ~2k tokens per tool result


def long_conversation(rounds=120, prompt_every=10, thinking=False, end_with_prompt=True):
    msgs = [user_text("Build the importer described in docs/spec.md")]
    for i in range(rounds):
        if i and i % prompt_every == 0:
            msgs.append(user_text(f"Request {i // prompt_every}: now handle case {i} carefully"))
            msgs.append(
                {
                    "role": "assistant",
                    "content": ([{"type": "thinking", "thinking": "", "signature": f"sig{i}"}] if thinking else [])
                    + [
                        {"type": "text", "text": f"Working on case {i}."},
                        {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"make case{i}"}},
                    ],
                }
            )
        else:
            msgs.append(
                tool_use(
                    f"t{i}",
                    "Read" if i % 3 == 0 else "Bash",
                    {"file_path": f"/repo/src/mod_{i}.py"} if i % 3 == 0 else {"command": f"make step{i}"},
                    thinking=thinking,
                )
            )
        msgs.append(tool_result(f"t{i}", BLOB + f"step {i} done"))
    if end_with_prompt:
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "Case finished; all tests pass."}]})
        msgs.append(user_text("Final request: write the README section"))
    return msgs


def session_for(store, body, context=260_000, floor=10_000, last_request=None, mode="live"):
    s = store.get_or_create_session(session_key(body), None, mode, time.time())
    s.context_tokens, s.floor_tokens, s.last_request = context, floor, last_request
    return s


def plan_for(store, make_ctx, body, mode="live", **kw):
    session = kw.pop("session", None) or session_for(
        store,
        body,
        mode=mode if mode != "freeze" else "live",
        **{k: kw.pop(k) for k in list(kw) if k in ("context", "floor", "last_request")},
    )
    ctx = make_ctx(mode=mode, session=session, **kw)
    plan = plan_request(body, ctx)
    store.put_rewrites(plan.new_rewrites)
    if plan.new_prune:
        store.put_prune(plan.new_prune)
    return plan, ctx


def test_prune_keeps_first_message_recent_prompts_and_everything_after(store, make_ctx, tmp_path):
    body = request(long_conversation())
    plan, _ctx = plan_for(store, make_ctx, body)
    p = plan.new_prune
    assert p is not None and plan.rules.get("prune") == 1
    out = plan.body_out["messages"]
    orig = body["messages"]
    assert out[0] is orig[0]  # the first message is untouched
    assert out[1]["role"] == "assistant" and out[1]["content"][0]["text"] == p.summary
    assert prune.is_prompt(orig[p.boundary])  # cut only at a user prompt
    kept_prompts = [m for m in orig[p.boundary :] if prune.is_prompt(m)]
    assert len(kept_prompts) >= 3
    # Kept turns keep their structure (their old tool results may also be compacted by other rules).
    shape = lambda m: (m["role"], [(b.get("type"), b.get("id") or b.get("tool_use_id")) for b in m["content"]])
    assert [shape(m) for m in out[2:]] == [shape(m) for m in orig[p.boundary :]]
    assert out[-1] is orig[-1]  # the new prompt is exactly as sent
    assert len(out) == len(orig) - p.boundary + 2
    # Quality guards in the summary and the history file.
    assert "Request 1: now handle case 10 carefully" in p.summary  # user words verbatim
    assert "/repo/src/mod_3.py" in p.summary  # files read are listed
    history = tmp_path / "proj" / ".lightfoot" / "history" / f"{plan.session_id}-{p.boundary}.md"
    assert history.exists() and "step 1 done" in history.read_text(encoding="utf-8")
    assert str(history) in p.summary
    assert plan.saved_tokens >= p.dropped_tokens > 100_000


def test_never_prunes_mid_task(store, make_ctx):
    body = request(long_conversation(end_with_prompt=False))  # ends with a tool result: Claude is mid-task
    plan, _ = plan_for(store, make_ctx, body)
    assert plan.new_prune is None


def test_a_trailing_system_message_does_not_stop_a_prune(store, make_ctx):
    # Claude Code 2.1.310+ ends requests with a system note, so the prompt is not the last message.
    msgs = long_conversation()
    msgs.append(
        {"role": "system", "content": [{"type": "text", "text": "<total_tokens>9000 tokens left</total_tokens>"}]}
    )
    body = request(msgs)
    plan, _ = plan_for(store, make_ctx, body)
    assert plan.new_prune is not None
    assert plan.body_out["messages"][-1] is body["messages"][-1]


def test_no_prune_mid_task_behind_a_system_message(store, make_ctx):
    msgs = long_conversation(end_with_prompt=False)
    msgs.append(
        {"role": "system", "content": [{"type": "text", "text": "<total_tokens>9000 tokens left</total_tokens>"}]}
    )
    plan, _ = plan_for(store, make_ctx, request(msgs))
    assert plan.new_prune is None


def test_the_summary_keeps_system_messages_it_prunes(store, make_ctx):
    # Claude Code 2.1.310+ sends its environment block as a system message after the first one.
    env = {"role": "system", "content": [{"type": "text", "text": "# Environment\nPrimary working directory: /repo"}]}
    note = {"role": "system", "content": [{"type": "text", "text": "<total_tokens>9000 tokens left</total_tokens>"}]}
    msgs = long_conversation()
    msgs[1:1] = [env]
    for i in (30, 60):
        msgs.insert(i, note)
    plan, _ = plan_for(store, make_ctx, request(msgs))
    summary = plan.new_prune.summary
    assert "Primary working directory: /repo" in summary
    assert summary.count("<total_tokens>9000 tokens left</total_tokens>") == 1  # repeats listed once


def test_newest_skips_system_messages():
    msgs = [user_text("a"), {"role": "assistant", "content": []}, {"role": "system", "content": "note"}]
    assert prune.newest(msgs) is msgs[1]
    assert prune.newest([{"role": "system", "content": "only"}]) is None


def test_no_prune_below_trigger(store, make_ctx):
    body = request(long_conversation())
    plan, _ = plan_for(store, make_ctx, body, context=150_000)
    assert plan.new_prune is None


def test_prune_replays_byte_identically(store, make_ctx):
    msgs = long_conversation()
    first, _ = plan_for(store, make_ctx, request(copy.deepcopy(msgs)))
    msgs += [tool_use("n1", "Bash", {"command": "ls"}), tool_result("n1", "README.md")]
    body2 = request(copy.deepcopy(msgs))
    session = session_for(store, body2, context=130_000)  # the model now sees the pruned prompt
    later, _ = plan_for(store, make_ctx, body2, session=session)
    assert later.new_prune is None
    assert dumps(later.body_out["messages"][1]) == dumps(first.body_out["messages"][1])
    assert later.body_out["messages"][0] == first.body_out["messages"][0]
    assert later.replay_saved >= first.new_prune.dropped_tokens


def test_diverged_history_is_not_pruned(store, make_ctx):
    msgs = long_conversation()
    first, _ = plan_for(store, make_ctx, request(copy.deepcopy(msgs)))
    # The user rewound and edited an earlier prompt inside the pruned span.
    edited = next(i for i in range(1, first.new_prune.boundary) if prune.is_prompt(msgs[i]))
    msgs[edited]["content"][0]["text"] = "a different request after a rewind"
    body = request(msgs)
    plan, _ = plan_for(store, make_ctx, body, session=session_for(store, body, context=50_000))
    assert any("diverged" in n for n in plan.notes)
    assert plan.body_out is None or len(plan.body_out["messages"]) == len(msgs)


def test_enforced_account_uses_drop_block(store, make_ctx):
    body = request(long_conversation(thinking=True), thinking={"type": "adaptive"})
    plan, _ = plan_for(store, make_ctx, body, enforced=True)
    assert plan.new_prune is not None and plan.drop_block
    assert plan.body_out["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}


def test_enforced_account_skip_policy(store, make_ctx, cfg):
    cfg["prune"]["enforced_accounts"] = "skip"
    body = request(long_conversation(thinking=True), thinking={"type": "adaptive"})
    plan, _ = plan_for(store, make_ctx, body, enforced=True, config=cfg)
    assert plan.new_prune is None and any("prune skipped" in n for n in plan.notes)


def test_warm_cache_waits_for_break_even(store, make_ctx, cfg):
    cfg["prune"]["safety_factor"] = 10.0  # break-even here is about 11 turns; 31 turns so far is not 10x that
    body = request(long_conversation(rounds=24, prompt_every=4))
    plan, _ = plan_for(store, make_ctx, body, context=260_000, last_request=time.time() - 30, config=cfg)
    assert plan.new_prune is None and any("prune deferred" in n for n in plan.notes)
    cold, _ = plan_for(
        store, make_ctx, copy.deepcopy(body), context=260_000, last_request=time.time() - 7200, config=cfg
    )
    assert cold.new_prune is not None and cold.prune_info["cold"]  # an expired cache makes it free


def test_shadow_prunes_are_separate(store, make_ctx):
    body = request(long_conversation())
    shadow, _ = plan_for(store, make_ctx, copy.deepcopy(body), mode="shadow")
    assert shadow.new_prune is not None and shadow.new_prune.live is False
    assert store.get_prune(shadow.session_id, live=True) is None


def test_disabled_prune_still_replays_a_stored_one(store, make_ctx, cfg):
    msgs = long_conversation()
    plan_for(store, make_ctx, request(copy.deepcopy(msgs)))
    cfg["prune"]["enabled"] = False
    body = request(msgs)
    plan, _ = plan_for(store, make_ctx, body, config=cfg, session=session_for(store, body, context=130_000))
    assert plan.body_out is not None and len(plan.body_out["messages"]) < len(msgs)


def test_summary_respects_budget_and_keeps_requests():
    msgs = [user_text("start")]
    for i in range(400):
        msgs.append(user_text(f"request number {i} " + "detail " * 200))
        msgs.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "reply " * 400},
                    {"type": "tool_use", "id": f"x{i}", "name": "Bash", "input": {"command": "echo " + "y" * 300}},
                ],
            }
        )
        msgs.append(tool_result(f"x{i}", "ok"))
    text = prune.build_summary(msgs, len(msgs) - 1, len(msgs), "/h.md", max_tokens=8000)
    assert len(text) // 4 <= 8000 * 1.15
    assert "request number 0" in text and "request number 399" in text


def test_choose_boundary_keeps_minimum_prompts():
    msgs = long_conversation()
    sizes = [prune.message_tokens(m) for m in msgs]
    b = prune.choose_boundary(msgs, sizes, 1.0, keep_tokens=1, min_kept_prompts=3)
    assert len([m for m in msgs[b:] if prune.is_prompt(m)]) == 3


def test_counterfactual_for_a_new_prune_keeps_the_floor_cached():
    plan = Plan(session_id="s", turns=1, model="claude-opus-5-5")
    plan.saved_tokens = 150_000
    plan.new_prune = object()
    usage = {"input": 2, "cw5": 0, "cw1h": 3_000, "cr": 257_000, "output": 500}
    cf = counterfactual(usage, plan, 0, 0, floor=20_000)
    assert cf["cr"] == 20_000
    assert cf["cw1h"] == 260_002 - 150_000 - 2 - 20_000


def test_server_rolls_back_a_rejected_prune(store, tmp_path):
    from starlette.testclient import TestClient
    from test_server import HEADERS, Upstream

    upstream = Upstream()
    upstream.reject_rewritten = True
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream.app()))
    store.put_launch(Launch("lp", str(lift_budgets(tmp_path / "proj")), "live", "http://upstream", None, None))
    body = request(long_conversation())
    session_for(store, body)
    raw = json.dumps(body).encode()
    with TestClient(build_app(proxy), base_url="http://127.0.0.1:8787") as client:
        r = client.post("/l/lp/v1/messages", content=raw, headers=HEADERS)
    assert r.status_code == 200
    assert b"[lightfoot:" in upstream.seen[0][1]  # the pruned request was tried first …
    assert upstream.seen[-1][1] == raw  # … rejected, and the original went out
    assert store.get_prune(session_key(body), live=True) is None  # so the model never saw the prune
    row = store.query("SELECT attempt FROM ledger ORDER BY id DESC LIMIT 1")[0]
    assert row["attempt"] == "original"


def test_cold_keep_tokens_prunes_deeper_only_on_a_cold_cache(store, make_ctx, cfg):
    cfg["prune"]["cold_keep_tokens"] = 40_000
    body = request(long_conversation())
    warm, _ = plan_for(store, make_ctx, copy.deepcopy(body), last_request=time.time() - 30, config=cfg)
    store.execute("DELETE FROM prunes")
    cold, _ = plan_for(store, make_ctx, copy.deepcopy(body), last_request=time.time() - 7200, config=cfg)
    assert warm.new_prune is not None and cold.new_prune is not None
    assert cold.new_prune.boundary > warm.new_prune.boundary  # less history kept after an idle gap


def test_cold_keep_tokens_is_off_by_default(store, make_ctx):
    body = request(long_conversation())
    warm, _ = plan_for(store, make_ctx, copy.deepcopy(body), last_request=time.time() - 30)
    store.execute("DELETE FROM prunes")
    cold, _ = plan_for(store, make_ctx, copy.deepcopy(body), last_request=time.time() - 7200)
    assert cold.new_prune.boundary == warm.new_prune.boundary


def test_summary_coverage_counts_references_the_summary_lost():
    from lightfoot.estimate import SummaryCoverage, Totals

    msgs = [
        user_text("start the importer"),
        tool_use("a", "Read", {"file_path": "/repo/src/importer/parse_rows.py"}),
        tool_result("a", "def handle_special_case(row):\n    return normalize_header_cells(row)"),
        user_text("next request"),
        {"role": "assistant", "content": [{"type": "text", "text": "Kept turn mentions normalize_header_cells."}]},
    ]
    coverage, totals = SummaryCoverage(), Totals()
    coverage.start("s", msgs, 3, "Files read: /repo/src/importer/parse_rows.py")
    later = [
        *msgs,
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Edit /repo/src/importer/parse_rows.py: handle_special_case and normalize_header_cells",
                }
            ],
        },
    ]
    coverage.check("s", later, totals)
    # parse_rows.py is in the summary, normalize_header_cells in a kept turn, handle_special_case nowhere.
    assert totals.summary_misses == 1
    assert totals.summary_refs >= 3
    coverage.check("s", later, totals)  # the same messages are not counted twice
    assert totals.summary_misses == 1


def test_cold_trigger_prunes_earlier_only_on_a_cold_cache(store, make_ctx, cfg):
    cfg["prune"]["cold_trigger_tokens"] = 150_000
    body = request(long_conversation())
    warm, _ = plan_for(store, make_ctx, copy.deepcopy(body), context=190_000, last_request=time.time() - 30, config=cfg)
    assert warm.new_prune is None  # 180k above the floor: under the normal 200k trigger
    cold, _ = plan_for(
        store, make_ctx, copy.deepcopy(body), context=190_000, last_request=time.time() - 7200, config=cfg
    )
    assert cold.new_prune is not None and cold.prune_info["cold"]


def test_no_paid_epoch_when_a_prune_is_near(store, make_ctx):
    from conftest import read_output

    msgs = [user_text("refactor")]
    for i in range(13):  # the same file read again and again; 15 turns, so a warm epoch is evaluated
        msgs += [tool_use(f"r{i}", "Read", {"file_path": "/repo/a.py"}), tool_result(f"r{i}", read_output(400))]
    msgs.append(user_text("continue"))
    body = request(msgs)
    session = session_for(store, body, context=180_000, floor=10_000, last_request=time.time() - 30)
    plan, _ = plan_for(store, make_ctx, body, session=session)
    assert not plan.epoch and "epoch skipped: a prune is near" in plan.notes


def test_summary_keeps_defined_names_and_command_paths():
    code = "import os\n\nclass RowParser:\n    pass\n\nasync def parse_rows(x):\n    return x\n\nMAX_ROWS = 10\n"
    msgs = [
        user_text("start"),
        tool_use("w", "Write", {"file_path": "/repo/src/rows.py", "content": code}),
        tool_result("w", "File created"),
        tool_use("b", "Bash", {"command": "python -m pytest tests/test_rows.py && cat docs/spec.md"}),
        tool_result("b", "1 passed"),
        user_text("next"),
    ]
    text = prune.build_summary(msgs, len(msgs) - 1, len(msgs), "/h.md", max_tokens=8000)
    assert "Names your edits defined: /repo/src/rows.py: RowParser, parse_rows, MAX_ROWS" in text
    assert "Paths used in commands: tests/test_rows.py, docs/spec.md" in text


def test_summary_coverage_reads_plain_text_not_json():
    from lightfoot.estimate import SummaryCoverage, Totals

    msgs = [
        user_text("start"),
        tool_use("a", "Bash", {"command": "ls", "run_in_background": True}),
        tool_result("a", "ok"),
        user_text("next request"),
    ]
    coverage, totals = SummaryCoverage(), Totals()
    coverage.start("s", msgs, 3, "")
    later = [*msgs, tool_use("b", "Bash", {"command": "ls", "run_in_background": True})]
    coverage.check("s", later, totals)
    assert totals.summary_refs == 0  # a tool-input key is not a reference to pruned content


def test_mid_loop_prunes_only_when_enabled_at_a_safe_point_and_never_on_enforcing_accounts(store, make_ctx, cfg):
    from lightfoot.transform import _safe_point

    body = request(long_conversation(end_with_prompt=False))  # ends with a tool result, mid-task
    assert _safe_point(body["messages"][-1]) and not _safe_point(user_text("hi"))
    off, _ = plan_for(store, make_ctx, copy.deepcopy(body))
    assert off.new_prune is None  # by default only a new prompt may trigger a prune
    cfg["prune"]["mid_loop"] = True
    enforced, _ = plan_for(store, make_ctx, copy.deepcopy(body), config=cfg, enforced=True)
    assert enforced.new_prune is None
    on, _ = plan_for(store, make_ctx, copy.deepcopy(body), config=cfg)
    assert on.new_prune is not None and prune.is_prompt(body["messages"][on.new_prune.boundary])
