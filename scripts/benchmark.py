"""Measure lightfoot's own cost: idle CPU and memory, and per-request latency and CPU.

Runs the real daemon against a local mock API (no network, no API cost) with requests
shaped like long Claude Code sessions:

    python scripts/benchmark.py

Needs `psutil` (`pip install psutil`).
"""

from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from conftest import big_output, read_output, request, tool_result, tool_use, user_text  # noqa: E402

MOCK_API = textwrap.dedent("""
    import json, sys, uvicorn
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route
    USAGE = {"input_tokens": 3, "cache_creation_input_tokens": 500,
             "cache_read_input_tokens": 90000, "output_tokens": 50}
    START = {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": USAGE}}
    async def messages(request):
        await request.body()
        async def events():
            yield b"data: " + json.dumps(START).encode() + b"\\n\\n"
            for _ in range(20):
                yield b'data: {"type":"content_block_delta","delta":{"text":"word "}}\\n\\n'
            yield b'data: {"type":"message_delta","usage":{"output_tokens":50}}\\n\\n'
        return StreamingResponse(events(), media_type="text/event-stream")
    uvicorn.run(Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])]),
                host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
""")


def free_port() -> int:
    """A port on 127.0.0.1 that nothing listens on right now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def body(rounds: int, new_log: bool = False) -> bytes:
    """A Claude Code-like request with `rounds` file reads; with `new_log`, a large unseen test log at the end."""
    msgs = [user_text("task")]
    for i in range(rounds):
        msgs += [
            tool_use(f"t{i}", "Read", {"file_path": f"/r/f{i}.py"}, thinking=True),
            tool_result(f"t{i}", read_output(120)),
        ]
    msgs += [
        tool_use("z", "Bash", {"command": "pytest"}),
        tool_result("z", big_output(1500) if new_log else "3 passed"),
    ]
    return json.dumps(request(msgs, thinking={"type": "adaptive"})).encode()


def server_process(pid: int) -> psutil.Process:
    """The process that actually serves: on Windows a venv's python.exe is a launcher for a child."""
    proc = psutil.Process(pid)
    children = proc.children(recursive=True)  # a Windows venv python.exe is a launcher; the server is its child
    return children[-1] if children else proc


def cpu_seconds(proc: psutil.Process) -> float:
    """User plus system CPU time a process has used."""
    times = proc.cpu_times()
    return times.user + times.system


def main() -> None:
    """Start the mock API and a real daemon, then measure idle CPU, per-request cost and memory."""
    home = Path(tempfile.mkdtemp(prefix="lightfoot-bench-"))
    up_port, proxy_port = free_port(), free_port()
    (home / "config.toml").write_text(
        f'port = {proxy_port}\nmode = "live"\nupstream = "http://127.0.0.1:{up_port}"\n', encoding="utf-8"
    )
    env = {**os.environ, "LIGHTFOOT_HOME": str(home)}
    mock = home / "mock_api.py"
    mock.write_text(MOCK_API, encoding="utf-8")
    upstream = subprocess.Popen([sys.executable, str(mock), str(up_port)])
    daemon = subprocess.Popen([sys.executable, "-m", "lightfoot", "serve", "--port", str(proxy_port)], env=env)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{proxy_port}/lightfoot/health", timeout=0.3).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        os.environ["LIGHTFOOT_HOME"] = str(home)
        from lightfoot.store import Launch, Store

        store = Store()
        store.put_launch(Launch("bench", str(home / "proj"), "live", f"http://127.0.0.1:{up_port}", None, None))
        store.close()
        proc = server_process(daemon.pid)

        time.sleep(1)
        before = cpu_seconds(proc)
        time.sleep(20)
        idle = cpu_seconds(proc) - before
        print(f"idle:   {100 * idle / 20:.2f}% of one core, {proc.memory_info().rss / 1e6:.0f} MB memory")

        headers = {
            "x-api-key": "k",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "user-agent": "claude-cli/2.1.191 (external, cli)",
        }
        cases = [
            ("20 KB request", body(6)),
            ("1 MB request", body(240)),
            ("4 MB request, new 1,500-line log", body(900, True)),
        ]
        with httpx.Client(timeout=60, trust_env=False) as client:
            for label, raw in cases:
                results = {}
                for target in ("direct", "proxy"):
                    url = (
                        f"http://127.0.0.1:{up_port}/v1/messages"
                        if target == "direct"
                        else f"http://127.0.0.1:{proxy_port}/l/bench/v1/messages"
                    )
                    client.post(url, content=raw, headers=headers).read()
                    times, start = [], cpu_seconds(proc)
                    for _ in range(15):
                        t = time.perf_counter()
                        client.post(url, content=raw, headers=headers).read()
                        times.append((time.perf_counter() - t) * 1000)
                    results[target] = (statistics.median(times), (cpu_seconds(proc) - start) / 15 * 1000)
                added = results["proxy"][0] - results["direct"][0]
                print(f"{label:<34} added latency {added:5.1f} ms   proxy CPU {results['proxy'][1]:5.1f} ms/request")
        print(f"memory after load: {proc.memory_info().rss / 1e6:.0f} MB")
    finally:
        daemon.terminate()
        upstream.terminate()


if __name__ == "__main__":
    main()
