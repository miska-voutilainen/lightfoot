"""Gemini API adapter (Gemini CLI), with request shapes taken from google-gemini/gemini-cli."""

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
from lightfoot.adapters import GEMINI, for_path
from lightfoot.launch import gemini_auth_type, resolve_gemini_upstream, run_gemini
from lightfoot.profiles import GEMINI_CLI, for_client
from lightfoot.server import Proxy, build_app, client_version, dumps
from lightfoot.store import Launch
from lightfoot.transform import plan_request
from lightfoot.usage import GeminiUsage

SIGNATURE = "skip_thought_signature_validator"  # what Gemini CLI 0.65.0 sent on its function calls


def user(text):
    return {"role": "user", "parts": [{"text": text}]}


def call(cid, command):
    return {
        "role": "model",
        "parts": [
            {"text": "Let me run it.", "thought": True, "thoughtSignature": SIGNATURE},
            {"functionCall": {"id": cid, "name": "run_shell_command", "args": {"command": command}}},
        ],
    }


def response(cid, output, name="run_shell_command"):
    return {"role": "user", "parts": [{"functionResponse": {"id": cid, "name": name, "response": {"output": output}}}]}


def shell_text(body, code=1):
    """run_shell_command output as Gemini CLI 0.65.0 sent it."""
    return f"<untrusted_context>\nOutput: {body}\nExit Code: {code}\nProcess Group PGID: 4242\n</untrusted_context>"


def gemini_request(contents):
    return {
        "contents": contents,
        "systemInstruction": {"role": "user", "parts": [{"text": "You are Gemini CLI."}]},
        "tools": [{"functionDeclarations": [{"name": "run_shell_command"}]}],
        "generationConfig": {"temperature": 1, "thinkingConfig": {"includeThoughts": True}},
    }


def base_contents():
    return [user("<session_context> cwd: /repo"), user("run the tests"), call("call_1", "pytest -q")]


def run(store, make_ctx, body, mode="live", **kw):
    sid = GEMINI.session_key(body)
    session = store.get_or_create_session(sid, None, "live", time.time())
    session.last_request = kw.pop("last_request", None)
    ctx = make_ctx(mode=mode, session=session, adapter=GEMINI, profile=GEMINI_CLI, **kw)
    plan = plan_request(body, ctx)
    store.put_rewrites(plan.new_rewrites)
    return plan


def test_routing():
    assert for_path("POST", "v1beta/models/gemini-2.5-pro:streamGenerateContent") is GEMINI
    assert for_path("POST", "v1beta/models/gemini-2.5-flash:generateContent") is GEMINI
    assert for_path("POST", "v1beta/models/gemini-2.5-pro:countTokens") is None
    assert for_path("GET", "v1beta/models/gemini-2.5-pro:generateContent") is None


def test_ingest_spills_new_output_and_keeps_model_turns(store, make_ctx):
    body = gemini_request([*base_contents(), response("call_1", shell_text(big_output()))])
    plan = run(store, make_ctx, body)
    assert plan.rules == {"spill": 1}
    new = plan.body_out["contents"][3]["parts"][0]["functionResponse"]
    assert set(new) == {"id", "name", "response"} and set(new["response"]) == {"output"}
    out = new["response"]["output"]
    assert out.startswith("<untrusted_context>\nOutput: ") and "</untrusted_context>" in out  # head and tail kept
    assert "FAILED tests/test_mod.py::test_case_1000" in out and "Exit Code: 1" in out
    assert plan.body_out["contents"][2] is body["contents"][2]  # thought signature untouched (same object)


def test_frozen_replay_across_turns(store, make_ctx):
    contents = [*base_contents(), response("call_1", shell_text(big_output()))]
    first = run(store, make_ctx, gemini_request(copy.deepcopy(contents)))
    contents += [call("call_2", "git status"), response("call_2", shell_text("clean", 0))]
    later = run(store, make_ctx, gemini_request(copy.deepcopy(contents)))
    assert later.new_rewrites == []
    assert dumps(later.body_out["contents"][3]) == dumps(first.body_out["contents"][3])


def test_only_unseen_outputs_are_ingested(store, make_ctx):
    contents = [
        *base_contents(),
        response("call_1", shell_text(big_output())),
        {"role": "model", "parts": [{"text": "Two tests fail."}]},
        user("thanks"),
    ]
    assert run(store, make_ctx, gemini_request(contents)).body_out is None


def test_errors_binary_and_unknown_shapes_are_never_touched(store, make_ctx):
    noisy = shell_text(big_output())
    shapes = [
        {"id": "call_1", "name": "run_shell_command", "response": {"error": noisy}},
        {"id": "call_1", "name": "run_shell_command", "response": {"output": noisy, "extra": 1}},
        {"id": "call_1", "name": "read_file", "response": {"output": noisy}, "parts": [{"inlineData": {}}]},
        {"name": "run_shell_command", "response": {"output": noisy}},  # no id: nothing to freeze it under
    ]
    for shape in shapes:
        contents = [*base_contents(), {"role": "user", "parts": [{"functionResponse": shape}]}]
        assert run(store, make_ctx, gemini_request(contents)).body_out is None, shape


def test_no_epochs_on_gemini(store, make_ctx, cfg):
    cfg["epochs"]["min_saving_tokens"] = 1
    contents = base_contents()[:-1]
    for i in range(12):
        contents += [call(f"c{i}", "grep -rn handler src"), response(f"c{i}", "src/a.py:1:handler\n" * 300)]
    plan = run(store, make_ctx, gemini_request(contents), config=cfg, last_request=time.time() - 7200)
    assert not any(r in plan.rules for r in ("stale_reads", "searches", "old_results"))
    assert plan.epoch is None


def test_profile_classification():
    assert GEMINI_CLI.kind("read_file", {"file_path": "/r/a.py"}) == "read"
    assert GEMINI_CLI.kind("replace", {}) == GEMINI_CLI.kind("write_file", {}) == "edit"
    assert GEMINI_CLI.kind("grep_search", {"pattern": "x"}) == "search"
    assert GEMINI_CLI.kind("run_shell_command", {"command": "ls"}) == "shell"
    assert GEMINI_CLI.kind("Read", {}) == "other"
    assert GEMINI_CLI.path("read_file", {"file_path": "/r/a.py"}) == "/r/a.py"
    assert GEMINI_CLI.path("run_shell_command", {"file_path": "/r"}) is None
    assert GEMINI_CLI.search_content_mode("grep_search", {}) is False
    assert for_client("gemini", "GeminiCLI/0.40.0/gemini-2.5-pro (win32; x64; terminal)") is GEMINI_CLI


def test_session_key_follows_the_first_user_turn():
    a = gemini_request(base_contents())
    b = gemini_request([*base_contents(), response("call_1", "x")])
    assert GEMINI.session_key(a) == GEMINI.session_key(b)
    other = gemini_request([user("<session_context> cwd: /other"), *base_contents()[1:]])
    assert GEMINI.session_key(a) != GEMINI.session_key(other)


USAGE = {
    "promptTokenCount": 50_000,
    "cachedContentTokenCount": 40_000,
    "candidatesTokenCount": 300,
    "thoughtsTokenCount": 600,
    "totalTokenCount": 50_900,
}


def test_usage_parser_counts():
    parser = GeminiUsage("text/event-stream")
    early = {"candidates": [], "usageMetadata": {"promptTokenCount": 50_000}, "modelVersion": "gemini-2.5-pro"}
    final = {"candidates": [], "usageMetadata": USAGE, "modelVersion": "gemini-2.5-pro"}
    parser.feed(b"data: " + json.dumps(early).encode() + b"\r\n\r\ndata: " + json.dumps(final).encode() + b"\r\n\r\n")
    parser.finish()
    assert parser.model == "gemini-2.5-pro"
    assert parser.counts() == {"input": 10_000, "cw5": 0, "cw1h": 0, "cr": 40_000, "output": 900}
    for body in (final, [early, final]):  # one response, or a stream without alt=sse
        plain = GeminiUsage("application/json")
        plain.feed(json.dumps(body).encode())
        plain.finish()
        assert plain.counts()["cr"] == 40_000 and plain.model == "gemini-2.5-pro"


def test_gemini_pricing():
    price, known = pricing.lookup("gemini-2.5-pro-001")
    assert known and price.read == 0.1 and price.write_short == price.write_long == 1.0
    assert pricing.lookup("gemini-2.5-flash-lite")[0].input == 0.10  # not gemini-2.5-flash
    assert pricing.lookup("gemini-3.1-pro-preview-customtools")[1]
    assert pricing.lookup("gemini-9-ultra") == (pricing.lookup("gemini-2.5-pro")[0], False)
    # above 200K prompt tokens the long-context rates apply
    assert abs(pricing.effective_cost("gemini-2.5-pro", 250_000, 0, 0, 0, 0) - 0.625) < 1e-9


def test_client_version():
    assert client_version("GeminiCLI/0.40.0/gemini-2.5-pro (win32; x64; terminal)") == "GeminiCLI/0.40.0"


# --- launch ----------------------------------------------------------------------


def test_upstream_keeps_a_users_gateway(monkeypatch):
    cfg = {"gemini_upstream": "https://generativelanguage.googleapis.com/"}
    monkeypatch.delenv("GOOGLE_GEMINI_BASE_URL", raising=False)
    assert resolve_gemini_upstream(8787, cfg) == "https://generativelanguage.googleapis.com"
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://gateway.example/gemini/")
    assert resolve_gemini_upstream(8787, cfg) == "https://gateway.example/gemini"
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "http://127.0.0.1:8787/l/old")
    assert resolve_gemini_upstream(8787, cfg) == "https://generativelanguage.googleapis.com"


def test_auth_type_is_read_from_project_then_user_settings(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "proj"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}})
    )
    assert gemini_auth_type(str(project)) == "oauth-personal"
    (project / ".gemini").mkdir(parents=True)
    (project / ".gemini" / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "gemini"}}}))
    assert gemini_auth_type(str(project)) == "gemini"
    (project / ".gemini" / "settings.json").write_text("{broken")
    assert gemini_auth_type(str(project)) == "oauth-personal"


def test_live_gemini_needs_the_experimental_flag(monkeypatch):
    monkeypatch.setattr("lightfoot.launch.find_gemini", lambda: "gemini")
    monkeypatch.delenv("LIGHTFOOT_OFF", raising=False)
    with pytest.raises(RuntimeError, match="experimental"):
        run_gemini([], mode="live", experimental=False)


def test_run_gemini_sets_the_base_url_and_warns_about_google_sign_in(tmp_path, monkeypatch, capsys):
    started = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lightfoot.launch.find_gemini", lambda: "gemini")
    monkeypatch.setattr("lightfoot.launch.gemini_auth_type", lambda cwd: "oauth-personal")
    monkeypatch.setattr(
        "lightfoot.launch._prepare_launch",
        lambda cwd, mode, agent, experimental: (Launch("g1", cwd, mode, None, None, None, agent=agent), None, 8787),
    )
    monkeypatch.setattr("lightfoot.launch.run_child", lambda cmd, env, port=None: started.update(cmd=cmd, env=env))
    run_gemini(["-p", "hi"])
    assert started["cmd"] == ["gemini", "-p", "hi"]
    assert started["env"]["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:8787/l/g1"
    assert "Sign in with Google" in capsys.readouterr().err


# --- server ----------------------------------------------------------------------


class GeminiUpstream:
    def __init__(self):
        self.seen = []

    async def generate(self, request: Request):
        self.seen.append((str(request.url), await request.body()))

        async def events():
            chunk = {
                "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "cachedContentTokenCount": 800, "candidatesTokenCount": 20},
                "modelVersion": "gemini-2.5-pro",
            }
            yield b"data: " + json.dumps(chunk).encode() + b"\r\n\r\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    def app(self):
        return Starlette(routes=[Route("/v1beta/models/{name}", self.generate, methods=["POST"])])


@pytest.fixture
def gemini_client(store, tmp_path):
    upstream = GeminiUpstream()
    proxy = Proxy(store, port=8787, transport=httpx.ASGITransport(app=upstream.app()))
    project = str(lift_budgets(tmp_path / "proj"))
    for lid, experimental in (("gem-live", False), ("gem-exp", True)):
        store.put_launch(
            Launch(
                lid,
                project,
                "live",
                "http://gemini",
                None,
                None,
                agent="gemini",
                experimental=experimental,
            )
        )
    with TestClient(build_app(proxy), base_url="http://127.0.0.1:8787") as client:
        yield client, upstream


HEADERS = {
    "x-goog-api-key": "test-key",
    "content-type": "application/json",
    "user-agent": "GeminiCLI/0.40.0/gemini-2.5-pro (win32; x64; terminal)",
}
PATH = "v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse"


def test_server_forces_shadow_without_experimental(gemini_client, store):
    client, upstream = gemini_client
    raw = json.dumps(gemini_request([*base_contents(), response("call_1", shell_text(big_output()))])).encode()
    r = client.post(f"/l/gem-live/{PATH}", content=raw, headers=HEADERS)
    assert r.status_code == 200
    assert upstream.seen[-1] == (f"http://gemini/{PATH}", raw)  # original bytes, query kept: shadow
    row = store.query("SELECT * FROM ledger ORDER BY id DESC LIMIT 1")[0]
    assert (row["mode"], row["agent"], row["model"]) == ("shadow", "gemini-cli", "gemini-2.5-pro")
    assert row["client_version"] == "GeminiCLI/0.40.0"
    assert (row["input"], row["cr"], row["output"]) == (200, 800, 20) and row["saved_tokens"] > 1000


def test_server_rewrites_with_experimental(gemini_client):
    client, upstream = gemini_client
    raw = json.dumps(gemini_request([*base_contents(), response("call_1", shell_text(big_output()))])).encode()
    client.post(f"/l/gem-exp/{PATH}", content=raw, headers=HEADERS)
    sent = json.loads(upstream.seen[-1][1])
    output = sent["contents"][3]["parts"][0]["functionResponse"]["response"]["output"]
    assert "[lightfoot: output trimmed" in output
    assert sent["contents"][2] == json.loads(raw)["contents"][2]
