"""OpenAI Responses adapter (Codex CLI), with request shapes taken from openai/codex."""

import copy
import json
import time

import httpx
import pytest
from conftest import big_output, lift_budgets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from lightfoot import pricing
from lightfoot.adapters import RESPONSES, for_path
from lightfoot.profiles import CODEX
from lightfoot.server import Proxy, build_app, client_version, dumps
from lightfoot.store import Launch
from lightfoot.transform import plan_request
from lightfoot.usage import ResponsesUsage


def dev(text):
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def user(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def reasoning(n):
    return {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"gAAAA{n}" * 20}


def call(cid, cmd):
    return {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": cmd}), "call_id": cid}


def output(cid, text):
    return {"type": "function_call_output", "call_id": cid, "output": text}


def exec_text(body):
    return f"Exit code: 1\nWall time: 3.2 seconds\nOutput:\n{body}"


def codex_request(items, **extra):
    body = {
        "model": "gpt-6-sol",
        "input": items,
        "tools": [{"type": "function", "name": "exec_command"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": "medium"},
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "thread-123",
    }
    body.update(extra)
    return body


def base_items():
    return [
        dev("<MODEL_INSTRUCTIONS>"),
        dev("<PERMISSIONS_INSTRUCTIONS>"),
        user("<ENVIRONMENT_CONTEXT>"),
        user("run the tests"),
        reasoning(1),
        call("call_1", "pytest -q"),
    ]


def run(store, make_ctx, body, mode="live", **kw):
    sid = RESPONSES.session_key(body)
    session = store.get_or_create_session(sid, None, "live", time.time())
    session.last_request = kw.pop("last_request", None)
    ctx = make_ctx(mode=mode, session=session, adapter=RESPONSES, profile=CODEX, **kw)
    plan = plan_request(body, ctx)
    store.put_rewrites(plan.new_rewrites)
    return plan


def test_routing():
    assert for_path("POST", "v1/responses") is RESPONSES
    assert for_path("POST", "v1/responses/compact") is None
    assert for_path("GET", "v1/responses") is None


def test_ingest_spills_new_output_and_keeps_reasoning(store, make_ctx):
    body = codex_request([*base_items(), output("call_1", exec_text(big_output()))])
    plan = run(store, make_ctx, body)
    assert plan.rules == {"spill": 1}
    new = plan.body_out["input"][6]["output"]
    assert new.startswith("Exit code: 1\nWall time: 3.2 seconds\nOutput:")
    assert "FAILED tests/test_mod.py::test_case_1000" in new and "[lightfoot: output trimmed" in new
    assert plan.body_out["input"][4] is body["input"][4]  # reasoning item untouched (same object)
    assert plan.body_out["include"] == ["reasoning.encrypted_content"]


def test_frozen_replay_across_turns(store, make_ctx):
    items = [*base_items(), output("call_1", exec_text(big_output()))]
    first = run(store, make_ctx, codex_request(copy.deepcopy(items)))
    items += [reasoning(2), call("call_2", "git status"), output("call_2", exec_text("clean"))]
    later = run(store, make_ctx, codex_request(copy.deepcopy(items)))
    assert later.new_rewrites == []
    assert later.body_out["input"][6]["output"] == first.body_out["input"][6]["output"]
    assert dumps(later.body_out["input"][6]) == dumps(first.body_out["input"][6])


def test_only_unseen_outputs_are_ingested(store, make_ctx):
    # The model already answered after this output: it saw it raw, so it stays raw.
    items = [
        *base_items(),
        output("call_1", exec_text(big_output())),
        reasoning(2),
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]},
        user("thanks"),
    ]
    assert run(store, make_ctx, codex_request(items)).body_out is None


def test_list_output_and_images(store, make_ctx):
    listed = [
        *base_items(),
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": exec_text(big_output())}],
        },
    ]
    plan = run(store, make_ctx, codex_request(listed))
    assert plan.body_out["input"][6]["output"][0]["type"] == "input_text"
    image = [
        *base_items(),
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
        },
    ]
    assert run(store, make_ctx, codex_request(image)).body_out is None


def test_file_reads_via_cat_are_not_spilled(store, make_ctx):
    items = [
        *base_items()[:-1],
        call("call_1", "cat src/big_module.py"),
        output("call_1", exec_text("\n".join(f"line {i} of code" for i in range(3000)))),
    ]
    assert CODEX.kind("exec_command", {"cmd": "cat src/big_module.py"}) == "read"
    plan = run(store, make_ctx, codex_request(items))
    assert "spill" not in plan.rules


def test_no_epochs_on_responses(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 1
    items = base_items()[:-1]
    for i in range(12):
        items += [
            reasoning(i),
            call(f"c{i}", "rg -n handler src"),
            output(f"c{i}", exec_text("src/a.py:1:handler\n" * 300)),
        ]
    plan = run(store, make_ctx, codex_request(items), config=cfg, last_request=time.time() - 7200)
    assert not any(r in plan.rules for r in ("stale_reads", "searches", "old_results"))
    assert plan.epoch is None


def test_profile_classification():
    assert CODEX.kind("exec_command", {"cmd": "sed -n '1,200p' src/app.py"}) == "read"
    assert CODEX.path("exec_command", {"cmd": "sed -n '1,200p' src/app.py"}) == "src/app.py"
    assert CODEX.kind("exec_command", {"cmd": "rg -n foo src"}) == "search"
    assert CODEX.search_content_mode("exec_command", {"cmd": "rg -n foo src"})
    assert not CODEX.search_content_mode("exec_command", {"cmd": "rg -l foo"})
    assert CODEX.kind("exec_command", {"cmd": "cat a.py | grep x"}) == "shell"
    assert CODEX.kind("exec_command", {"cmd": "pytest -q"}) == "shell"
    assert CODEX.kind("exec_command", {"command": ["bash", "-lc", "cat README.md"]}) == "read"
    assert CODEX.kind("apply_patch", {"input": "*** Begin Patch"}) == "edit"
    # Codex 0.164.0's multi_agent_v1 namespace, from its own tool definitions.
    for name in ("spawn_agent", "send_input", "wait_agent", "resume_agent", "close_agent"):
        assert CODEX.kind(name, {}) == "subagent", name


def test_session_key_uses_prompt_cache_key():
    a = codex_request(base_items())
    b = codex_request([*base_items(), output("call_1", "x")])
    assert RESPONSES.session_key(a) == RESPONSES.session_key(b)
    assert RESPONSES.session_key(a) != RESPONSES.session_key(codex_request(base_items(), prompt_cache_key="other"))


def test_usage_parser_counts():
    parser = ResponsesUsage("text/event-stream")
    usage = {
        "input_tokens": 50_000,
        "input_tokens_details": {"cached_tokens": 40_000, "cache_write_tokens": 6_000},
        "output_tokens": 900,
        "output_tokens_details": {"reasoning_tokens": 600},
        "total_tokens": 50_900,
    }
    event = {"type": "response.completed", "response": {"model": "gpt-6-sol", "usage": usage}}
    parser.feed(b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n")
    parser.finish()
    assert parser.model == "gpt-6-sol"
    assert parser.counts() == {"input": 4_000, "cw5": 6_000, "cw1h": 0, "cr": 40_000, "output": 900}
    long = ResponsesUsage("application/json", long_ttl=True)
    long.feed(json.dumps({"model": "gpt-6-sol", "usage": usage}).encode())
    long.finish()
    assert long.counts()["cw1h"] == 6_000 and long.counts()["cw5"] == 0


def test_openai_pricing():
    price, known = pricing.lookup("gpt-6.1-sol")
    assert known and price.read == 0.05 and price.write_long == 2.5
    assert not pricing.lookup("gpt-5.7-sol")[1]  # a different model, not gpt-5
    assert pricing.lookup("gpt-5-2025-08-07")[1]  # a gpt-5 snapshot
    assert pricing.lookup("claude-opus-4-1-20250805")[0].input == 15.0
    assert pricing.lookup("claude-sonnet-4-5-20250929")[0].input == 3.0
    # gpt-6-sol under 272K input: $2 input, 0.1x reads, 1.25x 30-minute writes, $10 output
    cost = pricing.effective_cost("gpt-6-sol", 50_000, 50_000, 0, 50_000, 100_000)
    assert abs(cost - (2 * (50_000 * (1 + 1.25 + 0.1)) + 10 * 100_000) / 1e6) < 1e-9
    # above 272K input tokens the long-context rates apply
    assert abs(pricing.effective_cost("gpt-6-sol", 300_000, 0, 0, 0, 0) - 1.2) < 1e-9
    assert pricing.lookup("gpt-5.5")[0].write_short == 1.0


def test_price_overrides():
    try:
        assert not pricing.priced("my-model-v2")  # looked up (and memoized) before the override exists
        assert pricing.configure({"my-model": {"input": 3, "output": 9}}) == []
        price, known = pricing.lookup("my-model-v2")
        assert known and price.input == 3.0 and price.output == 9.0
        assert pricing.configure({"bad": {"input": "x"}})
    finally:
        pricing.configure(None)


def test_client_version():
    assert client_version("claude-cli/2.1.191 (external, cli)") == "claude-cli/2.1.191"
    assert client_version("codex_cli_rs/0.80.0 (Windows 10.0; x86_64) vscode/1.2") == "codex_cli_rs/0.80.0"
    assert client_version("Mozilla") is None


# --- server ----------------------------------------------------------------------


class OpenAIUpstream:
    def __init__(self):
        self.seen = []

    async def responses(self, request: Request):
        self.seen.append((str(request.url), await request.body()))

        async def events():
            done = {
                "type": "response.completed",
                "response": {
                    "model": "gpt-6-sol",
                    "usage": {
                        "input_tokens": 1000,
                        "input_tokens_details": {"cached_tokens": 800},
                        "output_tokens": 20,
                    },
                },
            }
            yield b"event: response.completed\ndata: " + json.dumps(done).encode() + b"\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    def app(self):
        return Starlette(routes=[Route("/v1/responses", self.responses, methods=["POST"])])


@pytest.fixture
def codex_client(store, tmp_path):
    upstream = OpenAIUpstream()
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream.app()))
    project = str(lift_budgets(tmp_path / "proj"))
    store.put_launch(
        Launch("codex-live", project, "live", "http://openai", None, None, agent="codex", experimental=False)
    )
    store.put_launch(
        Launch("codex-exp", project, "live", "http://openai", None, None, agent="codex", experimental=True)
    )
    with TestClient(build_app(proxy), base_url="http://127.0.0.1:8787") as client:
        yield client, upstream


HEADERS = {
    "authorization": "Bearer sk-test",
    "content-type": "application/json",
    "user-agent": "codex_cli_rs/0.80.0 (Windows 10.0; x86_64)",
}


def test_server_forces_shadow_without_experimental(codex_client, store):
    client, upstream = codex_client
    raw = json.dumps(codex_request([*base_items(), output("call_1", exec_text(big_output()))])).encode()
    r = client.post("/l/codex-live/v1/responses", content=raw, headers=HEADERS)
    assert r.status_code == 200
    assert upstream.seen[-1] == ("http://openai/v1/responses", raw)  # original bytes: shadow
    row = store.query("SELECT * FROM ledger ORDER BY id DESC LIMIT 1")[0]
    assert (row["mode"], row["agent"], row["client_version"]) == ("shadow", "codex", "codex_cli_rs/0.80.0")
    assert (row["input"], row["cr"], row["output"]) == (200, 800, 20) and row["saved_tokens"] > 1000


def test_server_rewrites_with_experimental(codex_client, store):
    client, upstream = codex_client
    raw = json.dumps(codex_request([*base_items(), output("call_1", exec_text(big_output()))])).encode()
    client.post("/l/codex-exp/v1/responses", content=raw, headers=HEADERS)
    sent = json.loads(upstream.seen[-1][1])
    assert "[lightfoot: output trimmed" in sent["input"][6]["output"]
    assert sent["input"][4] == json.loads(raw)["input"][4]


def test_codex_provider_args():
    from lightfoot.launch import codex_provider_args

    args = codex_provider_args(8787, Launch("abc", None, "shadow", None, None, None))
    pairs = dict(a.split("=", 1) for a in args if a != "-c")
    assert pairs["model_providers.lightfoot.base_url"] == '"http://127.0.0.1:8787/l/abc/v1"'
    assert pairs["model_providers.lightfoot.wire_api"] == '"responses"'
    assert pairs["model_provider"] == '"lightfoot"'
    assert args.count("-c") == 5
