"""Run real agents through lightfoot against a local mock model API, end to end.

Each agent is started the way a user starts it (`lightfoot codex`, `lightfoot opencode`,
`lightfoot aider`), in shadow mode, with a throwaway LIGHTFOOT_HOME whose upstreams point at
a mock API on 127.0.0.1. No network, no API cost: every key is a dummy. The mock API speaks
Anthropic Messages, OpenAI Responses and Chat Completions; for agents with a shell tool it
answers the first request with one shell call that prints pretty JSON, and the next with
plain text, so the agent sends a request carrying a tool output for lightfoot to plan.

    python scripts/agent_check.py                    # every agent found on PATH
    python scripts/agent_check.py codex aider        # just these; each must be installed
    python scripts/agent_check.py --bin-dir DIR ...  # look for the agents in DIR first
    python scripts/agent_check.py --update           # accept the current request shapes

An agent passes when its requests reached the mock API through the proxy, lightfoot's ledger
recorded them with status 200, no error event was logged, (with a shell tool) a request
carried the tool output, and its request shapes (top-level fields, conversation unit kinds,
tool names) hold nothing new against scripts/agent_baseline.json. This is the evidence for
support level "0 · measure". New shapes are not necessarily bugs, but each is worth a look.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = "Run `cat data.json` in the shell, then say done."
SHELL_TOOLS = ("shell", "exec_command", "shell_command", "bash", "Bash", "run_shell_command")
DATA = json.dumps({"items": [{"id": i, "name": f"item {i}", "tags": ["a", "b"]} for i in range(40)]}, indent=2)

BASELINE = Path(__file__).with_name("agent_baseline.json")

captured: list[dict] = []
observed: dict[str, dict] = {}  # agent -> request shapes, filled by check()


def _has_tool_output(body: dict) -> bool:
    for m in body.get("messages") or []:
        if m.get("role") == "tool":
            return True
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return True
    if any(isinstance(i, dict) and i.get("type") == "function_call_output" for i in body.get("input") or []):
        return True
    return any(
        isinstance(p, dict) and "functionResponse" in p
        for c in body.get("contents") or []
        if isinstance(c, dict)
        for p in c.get("parts") or []
    )


def _tool_names(body: dict) -> list[str]:
    names = []
    for t in body.get("tools") or []:
        declarations = t.get("functionDeclarations") or t.get("function_declarations")
        if isinstance(declarations, list):  # Gemini groups its functions in one tool
            names += [str(d.get("name") or "") for d in declarations if isinstance(d, dict)]
        else:
            names.append(t.get("name") or (t.get("function") or {}).get("name") or "")
    return names


def _shell_call(body: dict) -> tuple[str, dict] | None:
    """The shell tool and its arguments to call first, or None to answer with text."""
    if _has_tool_output(body):
        return None
    for name in SHELL_TOOLS:
        if name in _tool_names(body):
            if name == "exec_command":
                return name, {"cmd": "cat data.json"}
            if name in ("shell", "shell_command"):
                return name, {"command": ["cat", "data.json"] if name == "shell" else "cat data.json"}
            return name, {"command": "cat data.json", "description": "Print data.json"}
    return None


def _sse(handler: BaseHTTPRequestHandler, events, named: bool = True, done: bool = False) -> None:
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream")
    handler.end_headers()
    for event in events:
        head = f"event: {event['type']}\n" if named else ""
        handler.wfile.write(f"{head}data: {json.dumps(event)}\n\n".encode())
    if done:
        handler.wfile.write(b"data: [DONE]\n\n")


def _json(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    data = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _anthropic(handler, body):
    call = _shell_call(body)
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}
    if call:
        content, stop = [{"type": "tool_use", "id": "toolu_fake1", "name": call[0], "input": call[1]}], "tool_use"
    else:
        content, stop = [{"type": "text", "text": "done"}], "end_turn"
    message = {"id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model", "fake")}
    if not body.get("stream"):
        return _json(handler, {**message, "content": content, "stop_reason": stop, "usage": usage})

    def events():
        yield {"type": "message_start", "message": {**message, "content": [], "usage": usage}}
        for i, block in enumerate(content):
            if block["type"] == "text":
                yield {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                yield {"type": "content_block_start", "index": i, "content_block": {**block, "input": {}}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            yield {"type": "content_block_delta", "index": i, "delta": delta}
            yield {"type": "content_block_stop", "index": i}
        yield {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}}
        yield {"type": "message_stop"}

    _sse(handler, events())


def _responses(handler, body):
    call = _shell_call(body)
    if call:
        item = {
            "type": "function_call",
            "id": "fc_fake1",
            "call_id": "call_fake1",
            "name": call[0],
            "arguments": json.dumps(call[1]),
            "status": "completed",
        }
    else:
        text = {"type": "output_text", "text": "done", "annotations": []}
        item = {"type": "message", "id": "msg_fake", "role": "assistant", "status": "completed", "content": [text]}
    usage = {
        "input_tokens": 10,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 5,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 15,
    }
    response = {"id": "resp_fake", "object": "response", "model": body.get("model", "fake"), "output": []}
    done = {**response, "status": "completed", "output": [item], "usage": usage}
    if not body.get("stream"):
        return _json(handler, done)
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": done},
    ]
    _sse(handler, ({**e, "sequence_number": n} for n, e in enumerate(events)))


def _chat(handler, body):
    call = _shell_call(body)
    model = body.get("model", "fake")
    if call:
        function = {"name": call[0], "arguments": json.dumps(call[1])}
        delta = {
            "role": "assistant",
            "tool_calls": [{"index": 0, "id": "call_fake1", "type": "function", "function": function}],
        }
        finish = "tool_calls"
    else:
        delta, finish = {"role": "assistant", "content": "done"}, "stop"
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    chunk = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    if not body.get("stream"):
        message = {k: v for k, v in delta.items() if k != "tool_calls"} | (
            {"tool_calls": [{k: v for k, v in delta["tool_calls"][0].items() if k != "index"}]} if call else {}
        )
        choice = {"index": 0, "message": message, "finish_reason": finish}
        return _json(handler, {**chunk, "object": "chat.completion", "choices": [choice], "usage": usage})
    events = [
        {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {**chunk, "choices": [], "usage": usage},
    ]
    _sse(handler, events, named=False, done=True)


def _gemini(handler, body):
    model = handler.path.split("?")[0].rsplit("/models/", 1)[-1].split(":")[0]
    call = _shell_call(body)
    if call:
        parts = [{"functionCall": {"name": call[0], "args": call[1]}}]
    elif (body.get("generationConfig") or {}).get("responseMimeType") == "application/json":
        # Gemini CLI's own checks (who speaks next, loop detection) ask for JSON.
        parts = [{"text": json.dumps({"reasoning": "done", "next_speaker": "user"})}]
    else:
        parts = [{"text": "done"}]
    usage = {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
    reply = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": usage,
        "modelVersion": model,
    }
    if handler.path.split("?")[0].endswith(":streamGenerateContent"):
        return _sse(handler, [reply], named=False)
    _json(handler, reply)


class MockAPI(BaseHTTPRequestHandler):
    """One mock model API for every protocol, chosen by path; it records each request it answers."""

    def log_message(self, *args):
        """Silent, to keep the check's output readable."""

    def do_GET(self):
        """Model listings come back empty; agents only need the call to succeed."""
        _json(self, {"object": "list", "data": []} if self.path.split("?")[0].endswith("/models") else {}, 200)

    def do_POST(self):
        """Answer a model request in its protocol's format, or 404 for an unknown path."""
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith(":countTokens"):
            return _json(self, {"totalTokens": 10})
        for suffix, protocol, answer in (
            ("/messages", "anthropic", _anthropic),
            ("/responses", "responses", _responses),
            ("/chat/completions", "chat", _chat),
            (":streamGenerateContent", "gemini", _gemini),
            (":generateContent", "gemini", _gemini),
        ):
            if path.endswith(suffix):
                captured.append({"protocol": protocol, "body": body, "agent": self.headers.get("user-agent", "")})
                return answer(self, body)
        _json(self, {"error": {"message": f"mock API: no route for {path}"}}, 404)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _agents(home: Path) -> dict:
    """argv after `lightfoot`, extra environment and expected protocol, per agent."""
    return {
        "codex": {
            "exe": "codex",
            "argv": [
                *("codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"),
                *("-m", "gpt-5", PROMPT),
            ],
            "env": {"OPENAI_API_KEY": "agent-check-dummy", "CODEX_HOME": str(home / "codex")},
            "protocol": "responses",
            "shell": True,
        },
        "opencode": {
            "exe": "opencode",
            "argv": ["opencode", "run", "--model", "anthropic/claude-sonnet-4-5", PROMPT],
            "env": {
                "ANTHROPIC_API_KEY": "agent-check-dummy",
                "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"bash": "allow"}, "autoupdate": False}),
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "XDG_CONFIG_HOME": str(home / "xdg-config"),
                "XDG_DATA_HOME": str(home / "xdg-data"),
                "XDG_CACHE_HOME": str(home / "xdg-cache"),
                "XDG_STATE_HOME": str(home / "xdg-state"),
            },
            "protocol": "anthropic",
            "shell": True,
        },
        "aider": {
            "exe": "aider",
            "argv": [
                *("aider", "--model", "openai/gpt-4o", "--message", "Say done.", "--yes-always", "--no-git"),
                *("--no-check-update", "--analytics-disable", "--no-show-model-warnings", "--no-pretty", "--no-stream"),
                *("--map-tokens", "0", "data.json"),
            ],
            "env": {"OPENAI_API_KEY": "agent-check-dummy", "AIDER_ANALYTICS": "false"},
            "protocol": "chat",
            "shell": False,  # Aider pastes files into messages instead of calling tools
        },
        "gemini": {
            "exe": "gemini",
            "argv": ["gemini", "-p", PROMPT, "--yolo", "-m", "gemini-2.5-pro"],
            # Its own home folder: API-key auth, no usage statistics, no user settings or skills.
            "env": {
                "GEMINI_API_KEY": "agent-check-dummy",
                "GEMINI_CLI_TRUST_WORKSPACE": "true",  # headless runs need a trusted folder
                "HOME": str(home / "gemini-home"),
                "USERPROFILE": str(home / "gemini-home"),
            },
            "files": {
                "gemini-home/.gemini/settings.json": json.dumps(
                    {
                        "security": {"auth": {"selectedType": "gemini-api-key"}},
                        "privacy": {"usageStatisticsEnabled": False},
                    }
                )
            },
            "protocol": "gemini",
            "shell": True,
        },
    }


def _ledger(db: Path) -> tuple[list[dict], list[dict]]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        ledger = [dict(r) for r in con.execute("SELECT * FROM ledger ORDER BY id")]
        events = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id")]
    finally:
        con.close()
    return ledger, events


def check(name: str, spec: dict, exe: str, mock: str, base: Path) -> list[str]:
    """Run one agent through a fresh lightfoot; the problems found, empty when it passed."""
    home = base / name
    work = home / "work"
    work.mkdir(parents=True)
    for relative, content in spec.get("files", {}).items():  # agent settings, under the check's own folder
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (work / "data.json").write_text(DATA + "\n", encoding="utf-8")
    port = _free_port()
    lf_home = home / "lightfoot"
    lf_home.mkdir()
    upstreams = "".join(f'{key} = "{mock}"\n' for key in ("upstream", "openai_upstream", "gemini_upstream"))
    (lf_home / "config.toml").write_text(f'mode = "shadow"\nport = {port}\n{upstreams}', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OPENAI_", "LIGHTFOOT_", "GEMINI_"))}
    env.update(spec["env"])
    env["LIGHTFOOT_HOME"] = str(lf_home)
    env["PWD"] = str(work)  # opencode takes its working directory from PWD, not the process cwd
    env["PATH"] = str(Path(exe).parent) + os.pathsep + env.get("PATH", "")
    lightfoot = [sys.executable, "-m", "lightfoot"]
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    before = len(captured)
    problems = []
    try:
        run = subprocess.run(
            [*lightfoot, *spec["argv"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=work,
            timeout=300,
            stdin=subprocess.DEVNULL,
        )
        if run.returncode != 0:
            problems.append(f"exited {run.returncode}: {(run.stderr or run.stdout).strip()[-800:]}")
    except subprocess.TimeoutExpired:
        problems.append("timed out after 300 s")
    finally:
        subprocess.run([*lightfoot, "stop"], capture_output=True, env=env, timeout=60)

    mine = [c for c in captured[before:] if c["protocol"] == spec["protocol"]]
    db = next(lf_home.glob("*.db"), None)
    ledger, events = _ledger(db) if db else ([], [])
    print(f"  requests at the mock API: {len(mine)}; ledger rows: {len(ledger)}")
    if mine:
        print(f"  user agent: {mine[0]['agent'][:100]}")
    if not mine:
        problems.append(f"no {spec['protocol']} request reached the mock API")
    if not ledger:
        problems.append("lightfoot's ledger recorded nothing")
    bad = sorted({r["status"] for r in ledger} - {200})
    if bad:
        problems.append(f"ledger statuses other than 200: {bad}")
    # A row's turn is set only when lightfoot indexed and planned the request.
    unplanned = sum(1 for r in ledger if r["turn"] is None)
    if unplanned:
        problems.append(f"{unplanned} of {len(ledger)} requests were passed through without a plan")
    if spec["shell"] and not any(_has_tool_output(c["body"]) for c in mine):
        problems.append("no request carried a tool output; the mock API's shell call was not run")
    problems.extend(
        f"{e['level']} event {e['kind']}: {str(e['detail'])[:300]}"
        for e in events
        if e["level"] in ("error", "warn", "warning")
    )
    agents = sorted({str(r.get("agent")) for r in ledger})
    rules = sorted({rule for r in ledger for rule in json.loads(r.get("rules") or "{}")})
    saved = sum(r.get("saved_tokens") or 0 for r in ledger)
    print(f"  profile: {', '.join(agents)}; turns planned: {[r['turn'] for r in ledger]}")
    print(f"  shadow rules that fired: {', '.join(rules) or 'none'}; tokens it would save: {saved}")
    if not problems:  # a failed run's shapes would make a wrong baseline
        observed[name] = shapes(mine, spec["protocol"])
    return problems


def shapes(requests: list[dict], protocol: str) -> dict:
    """What Lightfoot depends on in an agent's requests, as sorted lists.

    Top-level fields, the kinds of conversation units (role and content type), and tool names.
    A new entry means the agent changed something its adapter or profile should be checked against.
    """
    top, units, tools = set(), set(), set()
    for r in requests:
        body = r["body"]
        top.update(body)
        for t in body.get("tools") or []:
            if "functionDeclarations" in t or "function_declarations" in t:
                continue  # Gemini's functions are named below
            name = t.get("name") or (t.get("function") or {}).get("name")
            tools.add(name or f"type:{t.get('type')}")  # built-in tools such as web_search have no name
        if protocol == "gemini":
            tools.update(n for n in _tool_names(body) if n)
            for content in body.get("contents") or []:
                for part in content.get("parts") or [] if isinstance(content, dict) else []:
                    units.update(f"{content.get('role')}:{key}" for key in part if isinstance(part, dict))
            continue
        for unit in body.get("input" if protocol == "responses" else "messages") or []:
            if not isinstance(unit, dict):
                continue
            if protocol == "responses":
                kind = unit.get("type") or "message"
                units.add(f"message:{unit.get('role')}" if kind == "message" else kind)
                continue
            content = unit.get("content")
            if isinstance(content, list):
                units.update(f"{unit.get('role')}:{b.get('type')}" for b in content if isinstance(b, dict))
            elif content is not None:
                units.add(f"{unit.get('role')}:str")
            if unit.get("tool_calls"):
                units.add(f"{unit.get('role')}:tool_calls")
    return {"top_level": sorted(top), "units": sorted(units), "tools": sorted(tools)}


def compare(name: str, current: dict, baseline: dict) -> bool:
    """Print what changed against the baseline; True when something is new (gone entries only inform)."""
    known = baseline.get(name)
    if known is None:
        print(f"  NEW     no baseline for {name} yet")
        return True
    new = False
    for key, values in current.items():
        for v in sorted(set(values) - set(known.get(key, []))):
            print(f"  NEW     {key}: {v}")
            new = True
        for v in sorted(set(known.get(key, [])) - set(values)):
            print(f"  gone    {key}: {v}")
    return new


def main() -> int:
    """Check every requested agent that is installed; the exit code is 1 if any failed."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("agents", nargs="*", help="codex, opencode, aider, gemini (default: every one found)")
    parser.add_argument("--bin-dir", action="append", default=[], help="a directory to look for agents in first")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory for inspection")
    parser.add_argument("--update", action="store_true", help="accept the agents' current request shapes")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    mock = f"http://127.0.0.1:{server.server_address[1]}"
    base = Path(tempfile.mkdtemp(prefix="lightfoot-agents-"))
    search = os.pathsep.join([*args.bin_dir, os.environ.get("PATH", "")])
    specs = _agents(base)
    names = args.agents or list(specs)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    failed = []
    try:
        for name in names:
            spec = specs.get(name)
            if not spec:
                print(f"{name}: unknown agent (known: {', '.join(specs)})")
                failed.append(name)
                continue
            exe = shutil.which(spec["exe"], path=search)
            if not exe:
                if args.agents:  # named on the command line, so a missing one is a failure (CI)
                    print(f"{name}: not found")
                    failed.append(name)
                else:
                    print(f"{name}: not found, skipped")
                continue
            version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
            print(f"{name} {version.stdout.strip().splitlines()[-1] if version.stdout.strip() else '?'}")
            problems = check(name, spec, exe, mock, base)
            for p in problems:
                print(f"  PROBLEM {p}")
            drifted = name in observed and not args.update and compare(name, observed[name], baseline)
            if drifted:
                problems.append("request shapes changed; review the lines above, then accept them with --update")
            print(f"  {'FAIL' if problems else 'ok'}")
            if problems:
                failed.append(name)
    finally:
        server.shutdown()
        if args.keep:
            print(f"kept {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)
    if args.update and observed:
        BASELINE.write_text(json.dumps({**baseline, **observed}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written to {BASELINE} for {', '.join(sorted(observed))}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
