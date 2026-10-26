"""Check the newest Claude Code's request shapes against what lightfoot was built for.

Runs the real `claude -p` against a local mock Anthropic API (no network, no API cost: a
dummy key and a base URL on 127.0.0.1). The mock API answers the first request with one
Bash tool call and the next with plain text, so Claude Code sends a request that carries a
tool result. Every captured request is then indexed, planned and validated by lightfoot,
and its shape (top-level fields, content block types, beta headers) is compared with
scripts/drift_baseline.json:

    python scripts/drift_check.py            # compare; exit 1 on anything new
    python scripts/drift_check.py --update   # accept the current shapes as the new baseline

New shapes are not necessarily bugs (unknown content is never rewritten), but each one is
worth a look before users meet it.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lightfoot.adapters import ANTHROPIC  # noqa: E402
from lightfoot.config import DEFAULTS  # noqa: E402
from lightfoot.profiles import CLAUDE_CODE  # noqa: E402
from lightfoot.store import Store  # noqa: E402
from lightfoot.transform import Context, plan_request  # noqa: E402

BASELINE = Path(__file__).with_name("drift_baseline.json")
USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}

captured: list[dict] = []


def _events(content: list, stop_reason: str):
    """Server-sent events for one streamed message with the given content blocks."""
    yield {
        "type": "message_start",
        "message": {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "usage": USAGE,
        },
    }
    for i, block in enumerate(content):
        if block["type"] == "text":
            yield {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}
            yield {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}}
        else:
            start = {**block, "input": {}}
            yield {"type": "content_block_start", "index": i, "content_block": start}
            yield {
                "type": "content_block_delta",
                "index": i,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
            }
        yield {"type": "content_block_stop", "index": i}
    yield {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}}
    yield {"type": "message_stop"}


class MockAnthropic(BaseHTTPRequestHandler):
    """A mock Anthropic Messages API: one Bash tool call first, then plain text."""

    def log_message(self, *args):
        """Silent, to keep the check's output readable."""

    def do_POST(self):
        """Record the request and its beta headers, then answer it, streamed or not."""
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if not self.path.split("?")[0].endswith("/v1/messages"):
            self.send_response(404)
            self.end_headers()
            return
        betas = sorted({b.strip() for b in (self.headers.get("anthropic-beta") or "").split(",") if b.strip()})
        captured.append({"body": body, "betas": betas})
        msgs = body.get("messages") or []
        has_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for m in msgs
            if isinstance(m.get("content"), list)
            for b in m["content"]
        )
        wants_tool = any(t.get("name") == "Bash" for t in body.get("tools") or []) and not has_result
        if wants_tool:
            content = [{"type": "tool_use", "id": "toolu_fake1", "name": "Bash", "input": {"command": "echo drift"}}]
            stop = "tool_use"
        else:
            content, stop = [{"type": "text", "text": "done"}], "end_turn"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for event in _events(content, stop):
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        else:
            payload = {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": content,
                "stop_reason": stop,
                "usage": USAGE,
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def shapes(requests: list[dict]) -> dict:
    """What lightfoot cares about in the captured requests, as sorted lists."""
    top, blocks, results, betas, managed = set(), set(), set(), set(), set()
    for r in requests:
        body = r["body"]
        top.update(body)
        betas.update(r["betas"])
        if "context_management" in body:
            # lightfoot's server-side context editing has to merge with whatever Claude Code sends here.
            managed.add(json.dumps(body["context_management"], sort_keys=True))
        for m in body.get("messages") or []:
            for b in m.get("content") if isinstance(m.get("content"), list) else []:
                blocks.add(f"{m.get('role')}:{b.get('type')}")
                if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                    results.update(str(c.get("type")) for c in b["content"] if isinstance(c, dict))
    return {
        "top_level": sorted(top),
        "blocks": sorted(blocks),
        "tool_result_content": sorted(results),
        "betas": sorted(betas),
        "context_management": sorted(managed),
    }


def check_with_lightfoot(requests: list[dict], workdir: Path) -> list[str]:
    """Index, plan and validate every captured request; any exception is a problem."""
    problems = []
    cfg = copy.deepcopy(DEFAULTS)
    cfg["rewrite_budget_ms"] = 10_000
    store = Store(workdir / "drift.db")
    try:
        for i, r in enumerate(requests):
            body = r["body"]
            if not ANTHROPIC.accepts(body):
                continue
            try:
                ANTHROPIC.index(body, CLAUDE_CODE)
                session = store.get_or_create_session(ANTHROPIC.session_key(body), None, "live", time.time())
                ctx = Context(cfg, store, "live", session, workdir / "spill", None, False, time.perf_counter())
                plan_request(copy.deepcopy(body), ctx)
            except Exception as exc:  # report every failure, then keep checking the rest
                problems.append(f"request {i}: {type(exc).__name__}: {exc}")
    finally:
        store.close()
    return problems


def main() -> int:
    """Run Claude Code against the mock API and compare its request shapes with the baseline."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="write the current shapes as the baseline")
    parser.add_argument("--claude", default=shutil.which("claude"), help="path to the claude executable")
    args = parser.parse_args()
    if not args.claude:
        print("claude not found on PATH (npm install -g @anthropic-ai/claude-code)")
        return 2

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    workdir = Path(tempfile.mkdtemp(prefix="lightfoot-drift-"))
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
        # A bearer token needs no interactive approval, unlike ANTHROPIC_API_KEY in a fresh config.
        "ANTHROPIC_AUTH_TOKEN": "drift-check-dummy",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CONFIG_DIR": str(workdir / "claude-config"),  # no user settings, hooks or MCP servers
    }
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        version = subprocess.run([args.claude, "--version"], capture_output=True, text=True, env=env, timeout=60)
        print(f"Claude Code {version.stdout.strip()}")
        run = subprocess.run(
            [args.claude, "-p", "Run `echo drift` with Bash, then say done.", "--allowedTools", "Bash(echo:*)"],
            capture_output=True,
            text=True,
            env=env,
            cwd=workdir,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
        if run.returncode != 0:
            print(f"claude exited {run.returncode}: {run.stderr.strip()[-800:]}")
        problems = check_with_lightfoot(captured, workdir)
        current = shapes(captured)
    finally:
        server.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"captured {len(captured)} requests")
    if not any("tool_result" in b for b in current["blocks"]):
        problems.append("no request carried a tool result; the mock API's tool call was not run")
    for p in problems:
        print(f"PROBLEM {p}")
    if args.update:
        BASELINE.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {BASELINE}")
        return 1 if problems else 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    new = {k: sorted(set(v) - set(baseline.get(k, []))) for k, v in current.items()}
    gone = {k: sorted(set(baseline.get(k, [])) - set(v)) for k, v in current.items()}
    for k, values in new.items():
        for v in values:
            print(f"NEW     {k}: {v}")
    for k, values in gone.items():
        for v in values:
            print(f"gone    {k}: {v}")
    changed = any(new.values()) or problems
    print("drift: none" if not changed else "drift: review the lines above")
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
