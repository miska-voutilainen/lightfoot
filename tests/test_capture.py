"""`lightfoot capture`: redaction, and the proxy writing captures without touching requests."""

import json

from test_server import HEADERS, conversation, proxy_client, upstream  # noqa: F401 - fixtures

from lightfoot import capture


def test_concurrent_saves_never_overwrite_each_other(lightfoot_home):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        paths = list(pool.map(lambda i: capture.save("L", "POST", "v1/messages", [], b'{"n": %d}' % i), range(40)))
    assert len(set(paths)) == 40
    assert len(list(capture.capture_dir("L").glob("*.json"))) == 40


def test_gemini_function_responses_are_content():
    part = {"functionResponse": {"name": "read_file", "response": {"name": "Ana Smith", "output": "secret"}}}
    out = capture.redact(part)
    assert out["functionResponse"]["name"] == "read_file"
    assert out["functionResponse"]["response"] == {"name": "xxx xxxxx", "output": "xxxxxx"}


def test_redaction_keeps_structure_and_masks_content():
    body = {
        "model": "claude-opus-5-5",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Fix /home/ana/app.py line 42"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {"name": "secret.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "content": '{"name": "Ana Smith", "ok": true}'}]},
        ],
        "tools": [{"name": "Read", "description": "Reads a file"}],
        "input": [{"type": "function_call", "name": "shell", "arguments": '{"command": ["cat", "notes.md"]}'}],
        "image": {"data": "A" * 500},
    }
    out = capture.redact(body)
    assert out["model"] == "claude-opus-5-5" and out["tools"][0]["name"] == "Read"
    assert out["messages"][0]["content"][0] == {"type": "text", "text": "xxx /xxxx/xxx/xxx.xx xxxx 00"}
    assert out["messages"][1]["content"][0]["input"] == {"name": "xxxxxx.xxx"}  # inside tool input: masked
    assert json.loads(out["messages"][2]["content"][0]["content"]) == {"name": "xxx xxxxx", "ok": True}
    assert json.loads(out["input"][0]["arguments"]) == {"command": ["xxx", "xxxxx.xx"]}
    assert out["tools"][0]["description"] == "xxxxx x xxxx" and out["image"]["data"] == "<500 characters>"


def test_headers_drop_auth_and_hide_unknown_values():
    headers = [("X-Api-Key", "sk"), ("authorization", "Bearer t"), ("user-agent", "claude-cli/2.1"), ("x-custom", "v")]
    assert capture.redact_headers(headers) == {"user-agent": "claude-cli/2.1", "x-custom": "…"}


def test_proxy_writes_redacted_captures_and_forwards_unchanged(proxy_client, upstream, store):  # noqa: F811
    client, _ = proxy_client
    raw = json.dumps(conversation()).encode()
    client.post("/l/launch-shadow/v1/messages", content=raw, headers=HEADERS)
    assert not capture.capture_dir("launch-shadow").exists()  # not capturing
    store.set_state(capture.state_key("launch-shadow"), "1")
    client.get("/l/launch-shadow/v1/models", headers=HEADERS)  # a new launch id is read fresh; this one was cached
    assert not capture.capture_dir("launch-shadow").exists()
    _, proxy = proxy_client
    proxy._capturing.clear()
    client.post("/l/launch-shadow/v1/messages", content=raw, headers=HEADERS)
    client.get("/l/launch-shadow/v1/models", headers=HEADERS)
    assert upstream.seen[-2][1] == raw  # the request itself is untouched
    files = sorted(capture.capture_dir("launch-shadow").glob("*.json"))
    assert [f.name for f in files] == ["00001.json", "00002.json"]
    first = json.loads(files[0].read_text(encoding="utf-8"))
    assert first["path"] == "v1/messages" and "x-api-key" not in first["headers"]
    assert first["headers"]["anthropic-beta"] == "claude-code-20250219"
    assert first["body"]["messages"][1]["content"][0]["name"] == "Bash"
    assert "pytest" not in files[0].read_text(encoding="utf-8")
