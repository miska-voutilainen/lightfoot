import json

import httpx
import pytest
from conftest import big_output, lift_budgets, request, tool_result, tool_use, user_text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from lightfoot.server import BINDING_BETA, Proxy, build_app
from lightfoot.store import Launch

USAGE = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 3000,
    "cache_read_input_tokens": 40000,
    "cache_creation": {"ephemeral_5m_input_tokens": 3000, "ephemeral_1h_input_tokens": 0},
    "output_tokens": 1,
}


class Upstream:
    """Fake Anthropic API that records what it receives."""

    def __init__(self):
        self.seen: list[tuple[str, bytes, dict]] = []
        self.reject_rewritten = False
        self.reject_all = False

    async def messages(self, request: Request):
        body = await request.body()
        self.seen.append(
            (request.url.path + ("?" + request.url.query if request.url.query else ""), body, dict(request.headers))
        )
        if self.reject_all or (self.reject_rewritten and b"[lightfoot:" in body):
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking block prefix_mismatch: conversation prefix was edited",
                    },
                },
                status_code=400,
            )

        async def events():
            yield (
                b"event: message_start\ndata: "
                + json.dumps(
                    {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": USAGE}}
                ).encode()
                + b"\n\n"
            )
            yield b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
            yield b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":57}}\n\n'
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    async def other(self, request: Request):
        self.seen.append((request.url.path, await request.body(), dict(request.headers)))
        return JSONResponse({"data": [{"id": "claude-opus-5-5"}]})

    def app(self):
        return Starlette(
            routes=[
                Route("/v1/messages", self.messages, methods=["POST"]),
                Route("/v1/{rest:path}", self.other, methods=["GET", "POST"]),
            ]
        )


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def proxy_client(store, upstream, tmp_path):
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream.app()))
    project = lift_budgets(tmp_path / "proj")
    for mode in ("live", "shadow", "passthrough"):
        store.put_launch(Launch(f"launch-{mode}", str(project), mode, "http://upstream", None, None))
    with TestClient(build_app(proxy), base_url="http://127.0.0.1:8787") as client:
        yield client, proxy


def conversation():
    return request(
        [user_text("run tests"), tool_use("t1", "Bash", {"command": "pytest"}), tool_result("t1", big_output())]
    )


HEADERS = {
    "x-api-key": "sk-test",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219",
    "content-type": "application/json",
}


def test_non_message_routes_pass_through(proxy_client, upstream):
    client, _ = proxy_client
    r = client.get("/l/launch-live/v1/models", headers=HEADERS)
    assert r.status_code == 200 and r.json()["data"][0]["id"] == "claude-opus-5-5"
    path, _, headers = upstream.seen[-1]
    assert path == "/v1/models"
    assert headers["x-api-key"] == "sk-test"
    assert headers["host"] == "upstream"


def test_live_rewrite_streams_back_and_records_usage(proxy_client, upstream, store):
    client, _ = proxy_client
    raw = json.dumps(conversation()).encode()
    r = client.post("/l/launch-live/v1/messages?beta=true", content=raw, headers=HEADERS)
    assert r.status_code == 200
    assert b'"text_delta","text":"hi"' in r.content
    path, sent, headers = upstream.seen[-1]
    assert path == "/v1/messages?beta=true"
    assert len(sent) < len(raw) / 3 and b"[lightfoot: output trimmed" in sent
    assert headers["accept-encoding"] == "identity"
    row = store.query("SELECT * FROM ledger ORDER BY id DESC LIMIT 1")[0]
    assert (row["status"], row["mode"], row["attempt"]) == (200, "live", "rewrite")
    assert (row["input"], row["cw5"], row["cr"], row["output"]) == (12, 3000, 40000, 57)
    assert row["saved_tokens"] > 1000 and json.loads(row["rules"]) == {"spill": 1}


def test_shadow_forwards_original_bytes(proxy_client, upstream, store):
    client, _ = proxy_client
    raw = json.dumps(conversation()).encode()
    client.post("/l/launch-shadow/v1/messages", content=raw, headers=HEADERS)
    assert upstream.seen[-1][1] == raw
    row = store.query("SELECT * FROM ledger ORDER BY id DESC LIMIT 1")[0]
    assert row["mode"] == "shadow" and row["saved_tokens"] > 1000


def test_passthrough_launch_and_hard_switch(proxy_client, upstream, store):
    client, proxy = proxy_client
    raw = json.dumps(conversation()).encode()
    client.post("/l/launch-passthrough/v1/messages", content=raw, headers=HEADERS)
    assert upstream.seen[-1][1] == raw
    store.set_state("switch", "hard")
    proxy._switch = (0.0, "on")
    client.post("/l/launch-live/v1/messages", content=raw, headers=HEADERS)
    assert upstream.seen[-1][1] == raw


def test_rejected_rewrite_falls_back_rolls_back_and_learns(proxy_client, upstream, store):
    client, _ = proxy_client
    upstream.reject_rewritten = True
    raw = json.dumps(conversation()).encode()
    r = client.post("/l/launch-live/v1/messages", content=raw, headers=HEADERS)
    assert r.status_code == 200
    assert upstream.seen[-2][1] != raw and upstream.seen[-1][1] == raw  # rewrite, then the original
    assert store.query("SELECT COUNT(*) AS n FROM rewrites")[0]["n"] == 0  # rolled back: model saw it raw
    assert store.get_state("account_enforced") == "1"
    row = store.query("SELECT * FROM ledger ORDER BY id DESC LIMIT 1")[0]
    assert row["attempt"] == "original" and row["saved_tokens"] == 0


def test_error_on_original_is_returned_unchanged(proxy_client, upstream, store):
    client, _ = proxy_client
    upstream.reject_all = True
    body = conversation()
    r = client.post("/l/launch-live/v1/messages", content=json.dumps(body).encode(), headers=HEADERS)
    assert r.status_code == 400 and "prefix_mismatch" in r.text
    # An error unrelated to the rewrites does not switch the session to passthrough.
    assert store.query("SELECT mode FROM sessions")[0]["mode"] == "live"


def test_rejected_stored_rewrites_switch_session_to_passthrough(proxy_client, upstream, store):
    client, _ = proxy_client
    body = conversation()
    client.post("/l/launch-live/v1/messages", content=json.dumps(body).encode(), headers=HEADERS)
    body["messages"] += [tool_use("t2", "Bash", {"command": "ls"}), tool_result("t2", "ok")]
    upstream.reject_rewritten = True
    r = client.post("/l/launch-live/v1/messages", content=json.dumps(body).encode(), headers=HEADERS)
    assert r.status_code == 200
    assert store.query("SELECT mode FROM sessions")[0]["mode"] == "passthrough"


def test_drop_block_adds_beta_header(proxy_client, upstream, store, tmp_path):
    client, _proxy = proxy_client
    store.set_state("account_enforced", "1")
    lift_budgets(tmp_path / "proj", epochs='enforced_accounts = "drop_block"\nmin_saving_tokens = 500\n')
    from test_transform import stale_read_conversation

    body = request(stale_read_conversation(thinking=True), thinking={"type": "adaptive"})
    client.post("/l/launch-live/v1/messages", content=json.dumps(body).encode(), headers=HEADERS)
    _, sent, headers = upstream.seen[-1]
    assert BINDING_BETA in headers["anthropic-beta"] and "claude-code-20250219" in headers["anthropic-beta"]
    assert json.loads(sent)["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}


def test_foreign_host_is_refused(store, upstream):
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream.app()))
    with TestClient(build_app(proxy), base_url="http://evil.example") as client:
        assert client.post("/v1/messages", content=b"{}").status_code == 403
        assert client.post("/lightfoot/shutdown", headers={"x-lightfoot-control": "1"}).status_code == 403
        assert client.get("/lightfoot/health").status_code == 403  # no pid or version for a rebound name
    assert upstream.seen == []


def test_unknown_launch_ids_are_not_cached(proxy_client, upstream):
    client, proxy = proxy_client
    for i in range(50):
        client.get(f"/l/no-such-launch-{i}/v1/models", headers=HEADERS)
    assert not any(k.startswith("no-such-launch") for k in proxy._launches)


def test_requests_from_web_pages_are_refused(proxy_client, upstream):
    client, _ = proxy_client
    r = client.post("/l/launch-live/v1/messages", content=b"{}", headers={**HEADERS, "origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.get("/l/launch-live/v1/models", headers={"origin": "null"})
    assert r.status_code == 403
    assert upstream.seen == []


def test_oversized_bodies_are_rejected(proxy_client, upstream, monkeypatch):
    import lightfoot.server as server_module

    monkeypatch.setattr(server_module, "MAX_BODY_BYTES", 1000)
    client, _ = proxy_client
    r = client.post("/l/launch-live/v1/messages", content=b"x" * 5000, headers=HEADERS)
    assert r.status_code == 413
    assert upstream.seen == []


def test_invalid_json_is_forwarded(proxy_client, upstream):
    client, _ = proxy_client
    client.post("/l/launch-live/v1/messages", content=b"not json", headers=HEADERS)
    assert upstream.seen[-1][1] == b"not json"


def test_signals_spill_read(proxy_client, upstream, store):
    client, _ = proxy_client
    body = conversation()
    client.post("/l/launch-live/v1/messages", content=json.dumps(body).encode(), headers=HEADERS)
    spill_note = json.loads(upstream.seen[-1][1])["messages"][2]["content"][0]["content"]
    spill_path = spill_note.split("Full output: ")[1].split(" (read it")[0]
    body["messages"] += [tool_use("t2", "Read", {"file_path": spill_path}), tool_result("t2", "     1→x")]
    client.post("/l/launch-live/v1/messages", content=json.dumps(body).encode(), headers=HEADERS)
    sig = store.query("SELECT * FROM signals")
    assert len(sig) == 1 and sig[0]["kind"] == "spill_read" and sig[0]["rule"] == "spill"


def test_side_requests_are_tagged_and_summarized_in_stats(proxy_client, upstream, store):
    from lightfoot.reports import stats

    client, _ = proxy_client
    client.post("/l/launch-shadow/v1/messages", content=json.dumps(conversation()).encode(), headers=HEADERS)
    title = request([user_text("Write a 5-word title for: run tests")])
    del title["tools"]
    client.post("/l/launch-shadow/v1/messages", content=json.dumps(title).encode(), headers=HEADERS)
    rows = store.query("SELECT side FROM ledger ORDER BY id")
    assert [r["side"] for r in rows] == [0, 1]
    out = stats(store)
    assert "1 sessions · 2 requests" in out
    assert "Side requests (no tools: titles, quick checks): 1 of 2 requests" in out
