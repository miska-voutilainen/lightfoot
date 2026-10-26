"""OpenAI Chat Completions adapter: Aider, gateways, local servers and compatible providers."""

import copy
import json
import time

import httpx
from conftest import big_output, lift_budgets
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from lightfoot.adapters import CHAT, for_path
from lightfoot.profiles import GENERIC, for_client
from lightfoot.server import Proxy, build_app, dumps
from lightfoot.store import Launch
from lightfoot.transform import plan_request
from lightfoot.usage import ChatUsage


def call(cid, command):
    return {"id": cid, "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}


def chat_request(messages, **extra):
    return {"model": "deepseek-v4", "messages": messages, "stream": True, **extra}


def base(output=None, reasoning=False):
    assistant = {"role": "assistant", "content": None, "tool_calls": [call("c1", "pytest -q")]}
    if reasoning:
        assistant["reasoning_content"] = "Let me run the tests."
    messages = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "run the tests"},
        assistant,
    ]
    if output is not None:
        messages.append({"role": "tool", "tool_call_id": "c1", "content": output})
    return messages


def run(store, make_ctx, body, mode="live"):
    sid = CHAT.session_key(body)
    session = store.get_or_create_session(sid, None, "live", time.time())
    plan = plan_request(body, make_ctx(mode=mode, session=session, adapter=CHAT, profile=GENERIC))
    store.put_rewrites(plan.new_rewrites)
    return plan


def test_routing_and_profile():
    assert for_path("POST", "v1/chat/completions") is CHAT
    assert for_path("POST", "api/v1/chat/completions/") is CHAT
    assert for_path("GET", "v1/chat/completions") is None
    assert for_client("chat", "OpenAI/Python 1.99") is GENERIC


def test_ingest_spills_new_tool_output_and_keeps_reasoning(store, make_ctx):
    body = chat_request(base(big_output(), reasoning=True))
    plan = run(store, make_ctx, body)
    assert plan.rules == {"spill": 1}
    new = plan.body_out["messages"][3]["content"]
    assert "[lightfoot: output trimmed" in new and "FAILED tests/test_mod.py::test_case_1000" in new
    assert plan.body_out["messages"][2] is body["messages"][2]  # the reasoning message is untouched


def test_list_content_frozen_replay_and_no_history_edits(store, make_ctx, cfg):
    messages = base([{"type": "text", "text": big_output()}])
    first = run(store, make_ctx, chat_request(copy.deepcopy(messages)))
    assert first.body_out["messages"][3]["content"][0]["type"] == "text"
    messages += [
        {"role": "assistant", "content": "One test fails.", "tool_calls": [call("c2", "git status")]},
        {"role": "tool", "tool_call_id": "c2", "content": "clean"},
    ]
    later = run(store, make_ctx, chat_request(copy.deepcopy(messages)))
    assert later.new_rewrites == [] and later.epoch is None
    assert dumps(later.body_out["messages"][3]) == dumps(first.body_out["messages"][3])


def test_seen_output_stays_and_odd_parts_are_left_alone(store, make_ctx):
    seen = [*base(big_output()), {"role": "assistant", "content": "done"}, {"role": "user", "content": "thanks"}]
    assert run(store, make_ctx, chat_request(seen)).body_out is None
    image = base([{"type": "image_url", "image_url": {"url": "data:..."}}])
    assert run(store, make_ctx, chat_request(image)).body_out is None


def test_session_key():
    a = CHAT.session_key(chat_request(base()))
    assert a == CHAT.session_key(chat_request(base("later output")))
    assert a != CHAT.session_key(chat_request([{"role": "user", "content": "other"}]))
    assert CHAT.session_key(chat_request(base(), prompt_cache_key="k")) != a


def test_streamed_usage_survives_any_chunking():
    from lightfoot.usage import AnthropicUsage

    stream = (
        b'event: message_start\r\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5",'
        b'"usage":{"input_tokens":5,"cache_read_input_tokens":900}}}\r\n\r\n'
        + b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n' * 50
        + b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":77}}\n\n'
    )
    for size in (1, 7, 64, len(stream)):
        parser = AnthropicUsage("text/event-stream")
        for start in range(0, len(stream), size):
            parser.feed(stream[start : start + size])
        parser.finish()
        assert parser.model == "claude-opus-5-5"
        assert parser.counts() == {"input": 5, "cw5": 0, "cw1h": 0, "cr": 900, "output": 77}


def test_usage_parser_counts():
    stream = ChatUsage("text/event-stream")
    chunk = {"model": "gpt-6-sol", "choices": [{"delta": {"content": "hi"}}]}
    final = {
        "model": "gpt-6-sol",
        "choices": [],
        "usage": {
            "prompt_tokens": 50_000,
            "completion_tokens": 900,
            "prompt_tokens_details": {"cached_tokens": 40_000},
        },
    }
    for event in (chunk, final):
        stream.feed(b"data: " + json.dumps(event).encode() + b"\n\n")
    stream.feed(b"data: [DONE]\n\n")
    stream.finish()
    assert stream.model == "gpt-6-sol"
    assert stream.counts() == {"input": 10_000, "cw5": 0, "cw1h": 0, "cr": 40_000, "output": 900}
    deepseek = ChatUsage("application/json")
    usage = {"prompt_tokens": 3000, "prompt_cache_hit_tokens": 2000, "prompt_cache_miss_tokens": 1000}
    deepseek.feed(json.dumps({"model": "deepseek-v4", "usage": {**usage, "completion_tokens": 5}}).encode())
    deepseek.finish()
    assert deepseek.counts()["cr"] == 2000 and deepseek.counts()["input"] == 1000


def test_server_measures_in_shadow_without_experimental(store, tmp_path):
    seen = []

    async def completions(request):
        seen.append(await request.body())
        usage = {"prompt_tokens": 9000, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 8000}}
        return JSONResponse({"model": "gpt-6-sol", "choices": [], "usage": usage})

    upstream = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
    project = lift_budgets(tmp_path / "proj")  # a slow CI runner must not push ingest over its time budget
    store.put_launch(Launch("run1", str(project), "live", "http://up", None, None, agent="run"))
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream))
    raw = json.dumps(chat_request(base(big_output()))).encode()
    with TestClient(build_app(proxy), base_url="http://127.0.0.1:8787") as client:
        r = client.post("/l/run1/v1/chat/completions", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 200 and seen == [raw]  # experimental: measured, never changed
    row = store.query("SELECT * FROM ledger")[0]
    assert (row["mode"], row["cr"], row["input"], row["model"]) == ("shadow", 8000, 1000, "gpt-6-sol")
    assert row["saved_tokens"] > 1000
