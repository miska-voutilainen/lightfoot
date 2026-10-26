"""Model-written prune summaries through on-demand compaction (prune.summarizer = "model")."""

import copy
import json
import time

import httpx
import pytest
from conftest import lift_budgets, request, user_text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from test_prune import long_conversation, plan_for
from test_server import HEADERS

from lightfoot import prune
from lightfoot.server import COMPACT_BETA, Proxy, build_app, consumer_auth
from lightfoot.store import Launch, Prune
from lightfoot.transform import session_key

LONG_SESSION = {"input_tokens": 1000, "cache_read_input_tokens": 259_000, "cache_creation_input_tokens": 0}
SIGNED = {"type": "compaction", "content": "Summary: the importer is built; cases 0-99 pass.", "signature": "EuYB"}


def conversation(extra_prompts=0):
    """long_conversation with a closing assistant reply before every prompt, as Claude Code sends it."""
    out = []
    for m in long_conversation():
        if prune.is_prompt(m) and out and out[-1]["role"] == "user":
            out.append({"role": "assistant", "content": [{"type": "text", "text": "Done with that step."}]})
        out.append(m)
    for i in range(extra_prompts):
        out += [{"role": "assistant", "content": [{"type": "text", "text": f"Reply {i}."}]}, user_text(f"Next {i}")]
    return out


@pytest.fixture
def model_cfg(cfg):
    cfg["prune"]["summarizer"] = "model"
    cfg["epochs"]["enabled"] = False  # keep old tool output as sent, so messages compare directly
    return cfg


def test_live_prune_requests_a_model_summary_instead(store, make_ctx, model_cfg):
    body = request(conversation())
    body["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
    body["stream"] = True
    plan, _ = plan_for(store, make_ctx, body, config=model_cfg)
    assert plan.new_prune is None and (plan.body_out is None or plan.body_out["messages"] == body["messages"])
    job = plan.compaction
    cut = job["prune"].boundary
    req = job["request"]
    assert req["compaction"] == {"type": "summarize"} and len(req["messages"]) == cut
    assert "context_management" not in req and "stream" not in req and req["max_tokens"] <= 32000
    assert req["system"] == body["system"] and req["tools"] == body["tools"] and req["model"] == body["model"]
    assert "cache_control" in json.dumps(req["messages"][-1])  # reads the cached prefix
    assert "cache_control" not in json.dumps(body["messages"][cut - 1])  # the client's body is untouched


@pytest.mark.parametrize("why", ["shadow", "unsupported model", "failed before", "extractive", "subscription"])
def test_otherwise_the_extractive_summary_is_used(store, make_ctx, model_cfg, why):
    body = request(conversation())
    mode = "shadow" if why == "shadow" else "live"
    if why == "unsupported model":
        body["model"] = "claude-haiku-4-5"
    if why == "failed before":
        store.set_state(f"compaction_failed:{session_key(body)}", "refusal")
    if why == "extractive":
        model_cfg["prune"]["summarizer"] = "extractive"
    extra = {"consumer_auth": True} if why == "subscription" else {}
    plan, _ = plan_for(store, make_ctx, body, mode=mode, config=model_cfg, **extra)
    assert plan.compaction is None and plan.new_prune is not None and plan.new_prune.kind == "extractive"


def stored_compaction(store, body, boundary, pending=True):
    p = Prune(
        session_key(body),
        True,
        boundary,
        prune.fingerprint(body["messages"], boundary),
        json.dumps(SIGNED),
        None,
        90_000,
        kind="compaction",
        pending=pending,
    )
    store.put_prune(p)
    return p


def test_a_pending_summary_waits_for_the_next_prompt(store, make_ctx, model_cfg):
    msgs = conversation()
    first = request(copy.deepcopy(msgs))
    cut = plan_for(store, make_ctx, first, config=model_cfg)[0].compaction["prune"].boundary
    stored_compaction(store, first, cut)

    # Mid-task (the newest message is a tool result): nothing changes yet.
    mid = request(
        [
            *copy.deepcopy(msgs[:-1]),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t119", "content": "ok"}]},
        ]
    )
    plan, _ = plan_for(store, make_ctx, mid, config=model_cfg)
    assert plan.body_out is None or plan.body_out["messages"][0] == mid["messages"][0]
    assert plan.compaction is None  # and no second summary is requested meanwhile
    assert store.get_prune(session_key(first), live=True).pending

    # At the next prompt the block replaces messages[:cut]; everything after stays the same.
    nxt = request(
        [
            *copy.deepcopy(msgs),
            {"role": "assistant", "content": [{"type": "text", "text": "Written."}]},
            user_text("Thanks"),
        ]
    )
    plan, _ = plan_for(store, make_ctx, nxt, config=model_cfg)
    out = plan.body_out["messages"]
    assert out[0] == {"role": "assistant", "content": [SIGNED]} and out[1:] == nxt["messages"][cut:]
    assert plan.compact_beta and not plan.drop_block
    assert not store.get_prune(session_key(first), live=True).pending
    # From then on it is replayed on every request, mid-task included.
    later = request(
        [*copy.deepcopy(nxt["messages"]), {"role": "assistant", "content": [{"type": "text", "text": "x"}]}]
    )
    plan, _ = plan_for(store, make_ctx, later, config=model_cfg)
    assert plan.body_out["messages"][0]["content"] == [SIGNED]


def test_enforced_accounts_keep_thinking_without_drop_block(store, make_ctx, model_cfg):
    msgs = conversation()
    body = request(copy.deepcopy(msgs), thinking={"type": "adaptive"})
    cut = plan_for(store, make_ctx, body, config=model_cfg)[0].compaction["prune"].boundary
    stored_compaction(store, body, cut, pending=False)
    plan, _ = plan_for(store, make_ctx, body, config=model_cfg, enforced=True)
    assert plan.compact_beta and not plan.drop_block and "block_binding" not in json.dumps(plan.body_out["thinking"])


def test_validation_rejects_a_bad_compaction_shape():
    from lightfoot.validate import ValidationError

    body = request(conversation())
    cut = next(i for i in range(2, len(body["messages"])) if prune.is_prompt(body["messages"][i]))
    good = prune.apply(body, cut, json.dumps(SIGNED), "compaction")
    prune.validate(body, good, cut, json.dumps(SIGNED), "compaction")
    unsigned = prune.apply(body, cut, json.dumps({**SIGNED, "signature": ""}), "compaction")
    with pytest.raises(ValidationError):
        prune.validate(body, unsigned, cut, "", "compaction")


# --- server ----------------------------------------------------------------------------


class CompactingUpstream:
    """Fake API: compaction requests get a signed block (or a refusal); other requests stream a reply."""

    def __init__(self, refuse=False):
        self.seen: list[tuple[dict, dict]] = []
        self.refuse = refuse

    async def messages(self, request: Request):
        body = json.loads(await request.body())
        self.seen.append((body, dict(request.headers)))
        if "compaction" in body:
            usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "iterations": [
                    {
                        "type": "compaction",
                        "input_tokens": 3000,
                        "cache_read_input_tokens": 150_000,
                        "output_tokens": 900,
                    }
                ],
            }
            if self.refuse:
                return JSONResponse(
                    {"model": "claude-opus-5-5", "content": [], "stop_reason": "refusal", "usage": usage}
                )
            return JSONResponse(
                {"model": "claude-opus-5-5", "content": [SIGNED], "stop_reason": "compaction", "usage": usage}
            )

        async def events():
            start = {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": LONG_SESSION}}
            yield b"event: message_start\ndata: " + json.dumps(start).encode() + b"\n\n"
            yield b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    def app(self):
        return Starlette(routes=[Route("/v1/messages", self.messages, methods=["POST"])])


def run_server(store, tmp_path, upstream, bodies, headers=HEADERS):
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream.app()))
    project = lift_budgets(tmp_path / "proj", epochs="enabled = false\n")
    with (project / ".lightfoot" / "config.toml").open("a", encoding="utf-8") as f:
        f.write('\n[prune]\nsummarizer = "model"\n')
    store.put_launch(Launch("lc", str(project), "live", "http://upstream", None, None))
    with TestClient(build_app(proxy), base_url="http://127.0.0.1:8787") as client:
        for body in bodies:
            raw = json.dumps(body).encode()
            assert client.post("/l/lc/v1/messages", content=raw, headers=headers).status_code == 200
            for _ in range(100):  # let the background summary finish before the next turn
                if not proxy._compacting:
                    break
                time.sleep(0.02)


def session_for_server(store, body):
    s = store.get_or_create_session(session_key(body), "lc", "live", time.time())
    store.execute(
        "UPDATE sessions SET context_tokens=?, floor_tokens=? WHERE id=?", (260_000, 10_000, session_key(body))
    )
    return s


def test_server_requests_the_summary_and_swaps_it_in_at_the_next_prompt(store, tmp_path):
    upstream = CompactingUpstream()
    msgs = conversation()
    first = request(copy.deepcopy(msgs))
    session_for_server(store, first)
    nxt = request(
        [
            *copy.deepcopy(msgs),
            {"role": "assistant", "content": [{"type": "text", "text": "Written."}]},
            user_text("Thanks"),
        ]
    )
    run_server(store, tmp_path, upstream, [first, nxt])

    sent = [b for b, _ in upstream.seen]
    assert sent[0]["messages"] == first["messages"]  # the first request went out unchanged
    comp = next(b for b in sent if "compaction" in b)
    assert "context_management" not in comp
    # The fake keeps reporting a 260k context, so a further summary may be requested; the turn
    # itself is the last request without the compaction parameter.
    final, headers = [(b, h) for b, h in upstream.seen if "compaction" not in b][-1]
    assert final["messages"][0]["content"] == [SIGNED]
    assert COMPACT_BETA in headers["anthropic-beta"]
    row = store.query("SELECT * FROM ledger WHERE attempt = 'compaction'")[0]
    assert (row["cr"], row["output"], row["mode"]) == (150_000, 900, "live")


def test_server_falls_back_to_the_extractive_summary_after_a_refusal(store, tmp_path):
    upstream = CompactingUpstream(refuse=True)
    msgs = conversation()
    first = request(copy.deepcopy(msgs))
    session_for_server(store, first)
    nxt = request(
        [
            *copy.deepcopy(msgs),
            {"role": "assistant", "content": [{"type": "text", "text": "Written."}]},
            user_text("Thanks"),
        ]
    )
    run_server(store, tmp_path, upstream, [first, nxt])

    assert store.get_state(f"compaction_failed:{session_key(first)}") == "refusal"
    final, headers = upstream.seen[-1]
    assert "[lightfoot:" in json.dumps(final["messages"][1])  # the extractive summary
    assert COMPACT_BETA not in headers.get("anthropic-beta", "")


def test_consumer_logins_are_recognised():
    assert consumer_auth([("anthropic-beta", "claude-code-20250219,oauth-2025-04-20")])
    assert consumer_auth([("Authorization", "Bearer sk-ant-oat01-abc")])
    assert not consumer_auth([("x-api-key", "sk-ant-api03-abc"), ("anthropic-beta", "claude-code-20250219")])
    assert not consumer_auth([("authorization", "Bearer gateway-token")])


def test_server_never_sends_its_own_request_on_a_subscription_login(store, tmp_path):
    upstream = CompactingUpstream()
    first = request(conversation())
    session_for_server(store, first)
    oauth = {k: v for k, v in HEADERS.items() if k != "x-api-key"}
    oauth.update(
        {"authorization": "Bearer sk-ant-oat01-test", "anthropic-beta": "claude-code-20250219,oauth-2025-04-20"}
    )
    run_server(store, tmp_path, upstream, [first], headers=oauth)
    assert upstream.seen and not any("compaction" in b for b, _ in upstream.seen)
    prune_row = store.query("SELECT kind FROM prunes")
    assert [r["kind"] for r in prune_row] == ["extractive"]
