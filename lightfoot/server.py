"""The proxy server.

Routes:
  /l/<launch_id>/<path>   requests from a `lightfoot claude|codex` launch (project, integrations, mode known)
  /<path>                 anything else (treated as a launch without a project)
  /lightfoot/health, /lightfoot/shutdown

POST .../v1/messages (Anthropic), .../responses and .../chat/completions (OpenAI) and Gemini's
...:generateContent are rewritten by their adapters. Every other request, and every response, is
streamed through unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import logging.handlers
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, fastjson, guards, pricing, providers, prune
from .adapters import Adapter, for_path
from .config import DEFAULTS, ConfigError, lightfoot_home, load_config, private_dir
from .idlehandoff import IdleHandoffs
from .idlehandoff import enabled as idle_enabled
from .idlehandoff import start as idle_start
from .profiles import for_client
from .store import Launch, Prune, Store
from .text import tokens
from .transform import Context, Plan, plan_request

log = logging.getLogger("lightfoot")
# Agents whose profiles are researched but not yet measured in an eval: shadow unless opted in.
EXPERIMENTAL_PROFILES = {"opencode", "cline", "roo-code"}

HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "accept-encoding",
}
RESPONSE_DROP = {"content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailers", "upgrade"}
BINDING_BETA: str = pricing.DATA["anthropic"]["binding_beta"]
COMPACT_BETA = prune.COMPACT_BETA
BINDING_ERROR_RE = re.compile(r"prefix[_ -]?mismatch|block_binding|thinking-binding|prefix[_ -]?binding", re.I)
CONTROL_HEADER = "x-lightfoot-control"
PRUNE_INTERVAL = 24 * 3600
_CLIENT_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+/[0-9][A-Za-z0-9_.\-+]*)")
dumps = fastjson.dumps


def api_error(status: int, message: str) -> JSONResponse:
    """An error in the Anthropic API's JSON shape, so agents show the message instead of failing to parse it."""
    return JSONResponse(
        {"type": "error", "error": {"type": "api_error", "message": f"lightfoot: {message}"}}, status_code=status
    )


MAX_BODY_BYTES = 100_000_000  # the API itself accepts at most 32 MB


async def read_body(request: Request) -> bytes | None:
    """The request body, or None if it exceeds MAX_BODY_BYTES (checked while streaming)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def consumer_auth(headers) -> bool:
    """True for a Claude Pro/Max login (OAuth), as opposed to an API key or a gateway token.

    Claude Code marks OAuth requests with the `oauth-…` beta; its access tokens start with
    `sk-ant-oat`.
    """
    beta = ",".join(v for k, v in headers if k.lower() == "anthropic-beta")
    if any(b.strip().startswith("oauth-") for b in beta.split(",")):
        return True
    auth = next((v for k, v in headers if k.lower() == "authorization"), "")
    return auth.removeprefix("Bearer ").strip().startswith("sk-ant-oat")


def client_version(user_agent: str | None) -> str | None:
    """'claude-cli/2.1.191 (external, cli)' -> 'claude-cli/2.1.191'."""
    match = _CLIENT_RE.match(user_agent or "")
    return match.group(1) if match else None


@dataclass
class Prepared:
    """A request ready to forward: the attempts to try in order (name, body, is_rewrite) and the plan behind them."""

    session_id: str | None
    mode: str
    plan: Plan | None
    attempts: list[tuple[str, bytes, bool]]
    notes: list[str]
    adapter: Adapter | None = None
    agent: str | None = None
    request_body: dict | None = None
    side: bool = False  # no tools: a title, a quick check or another helper request, not the conversation


class Proxy:
    """The HTTP proxy: routes by launch, rewrites model requests, streams everything else through unchanged."""

    def __init__(self, store: Store, port: int = 8787, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self.port = port
        self._transport = transport
        self.client: httpx.AsyncClient | None = None
        self._launches: dict[str, Launch | None] = {}
        self._capturing: dict[str, bool] = {}  # launches started with `lightfoot capture`
        self.idle = IdleHandoffs()
        self._prefixed: dict[str, set[str]] = {}  # session -> tools already recorded as used (for the audit)
        self._idle_task: asyncio.Task | None = None
        self._switch: tuple[float, str] = (0.0, "on")
        self._prune_task: asyncio.Task | None = None
        self._compacting: set[str] = set()  # sessions with a model summary in flight
        self._tasks: set[asyncio.Task] = set()
        self.server = None  # uvicorn.Server, set by serve()

    async def startup(self, prune: bool = False) -> None:
        """Open the upstream HTTP client; with `prune`, also start the daily cleanup and the idle-handoff loops."""
        self.client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(connect=30.0, read=900.0, write=120.0, pool=60.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
            follow_redirects=False,
        )
        if prune:
            self._prune_task = asyncio.create_task(self._prune_loop())
            self._idle_task = asyncio.create_task(self._idle_loop())

    async def shutdown(self) -> None:
        """Stop the background loops and close the upstream client."""
        for task in (self._prune_task, self._idle_task):
            if task:
                task.cancel()
        if self.client:
            await self.client.aclose()

    async def _prune_loop(self) -> None:
        from .maintenance import cleanup

        while True:
            try:
                last = float(self.store.get_state("last_prune", "0") or 0)
                if time.time() - last >= PRUNE_INTERVAL:
                    counts = await asyncio.to_thread(cleanup, self.store, self.config(None))
                    log.info("pruned %s", counts)
            except Exception:
                log.exception("prune failed")
            await asyncio.sleep(3600)

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(self.start_idle_handoffs, time.time())
            except Exception:
                log.exception("idle handoff failed")

    def start_idle_handoffs(self, now: float) -> int:
        """Start the handoff worker for every session idle long enough; returns how many started."""
        started = 0
        for sid, pending in self.idle.due(now):
            try:
                min_turns = int(load_config(pending.cwd)["integrations"].get("handoff_min_turns", 5))
            except (ConfigError, TypeError, ValueError):
                min_turns = 5
            if idle_start(sid, pending, min_turns):
                started += 1
                self.store.event("info", "idle_handoff", "handoff worker started", sid)
        return started

    def update_guards(self, sid: str, plan: Plan, cfg: dict) -> None:
        """Apply this request's signals to the session's quality guards; each step becomes an event."""
        state = plan.guard_state
        before = json.dumps(state, sort_keys=True)
        limits = {**DEFAULTS["guards"], **cfg.get("guards", {})}
        steps = guards.update(state, plan.guard_signals, plan.turns, plan.new_prune is not None, limits)
        if plan.new_prune is not None:
            state["pre_rate"], state["retrieval_for"] = plan.guard_pre_rate, None
        if json.dumps(state, sort_keys=True) != before:
            guards.save(self.store, sid, state)
        for step in steps:
            self.store.event("info", "guard", step, sid)

    def record_prefix(self, sid: str, launch: Launch | None, body: dict, adapter: Adapter, plan: Plan) -> None:
        """For `lightfoot audit`: the session's fixed prefix once, and each tool the first time it is called."""
        try:
            known = self._prefixed.get(sid)
            if known is None:
                if len(self._prefixed) > 2000:
                    self._prefixed.clear()
                known = self._prefixed[sid] = set()
                texts, tools = adapter.prefix(body)
                blocks = [[_digest(t), tokens(t), t[:60]] for t in texts]
                defs = {}
                for name, definition in tools:
                    text = dumps(definition).decode()
                    defs[name] = [_digest(text), tokens(text)]
                self.store.put_prefix(sid, launch.id if launch else None, plan.model, blocks, defs)
            new = sorted(plan.tools_used - known)
            if new:
                self.store.add_tools_used(sid, new)
                known.update(new)
        except Exception as exc:  # measurement only: never affects the request
            self.store.event("warn", "audit", repr(exc), sid)

    def note_idle(self, prepared: Prepared, launch: Launch | None) -> None:
        """Remember this session's latest history for an idle handoff, if it can get one.

        Claude Code has its own SessionEnd hook, so only other agents are tracked.
        """
        if launch is None or not launch.vault or launch.agent in ("claude", "handoff"):
            return
        if prepared.request_body is None or not prepared.session_id or prepared.adapter is None:
            return
        minutes = idle_enabled(self.config(launch))
        if minutes:
            self.idle.note(
                prepared.session_id,
                prepared.request_body,
                prepared.adapter.name,
                prepared.agent or "generic",
                launch.cwd or "",
                minutes,
                time.time(),
            )

    # --- state ---------------------------------------------------------------
    def switch(self) -> str:
        """Global kill switch: on | off (freeze: replay stored rewrites, make no new ones) | hard (raw passthrough)."""
        checked, value = self._switch
        if time.monotonic() - checked > 1.0:
            value = self.store.get_state("switch", "on") or "on"
            self._switch = (time.monotonic(), value)
        return value

    def launch(self, launch_id: str | None) -> Launch | None:
        """The launch named in the request path, cached once found."""
        if not launch_id:
            return None
        launch = self._launches.get(launch_id)
        if launch is None:
            # Only known launches are cached: the id comes from the URL, so unknown ones are unbounded.
            launch = self.store.get_launch(launch_id)
            if launch is not None:
                self._launches[launch_id] = launch
        return launch

    def capturing(self, launch_id: str | None) -> bool:
        """Set before the launch's agent starts, so it is read once per launch."""
        if not launch_id:
            return False
        if launch_id not in self._capturing:
            from .capture import state_key

            self._capturing[launch_id] = self.store.get_state(state_key(launch_id)) == "1"
        return self._capturing[launch_id]

    def config(self, launch: Launch | None) -> dict:
        """The config for a launch's project; on a config error, the defaults (and a warning event)."""
        try:
            return load_config(launch.cwd if launch else None)
        except ConfigError as exc:
            log.warning("config error, using defaults: %s", exc)
            self.store.event("warn", "config", str(exc))
            return copy.deepcopy(DEFAULTS)

    def enforced(self, cfg: dict) -> bool:
        """Whether the account enforces the thinking-prefix check: set in the config, or learned from a 400."""
        setting = str(cfg["epochs"].get("account_enforced", "auto")).lower()
        if setting in ("yes", "true", "1"):
            return True
        if setting in ("no", "false", "0"):
            return False
        return self.store.get_state("account_enforced") == "1"

    # --- request preparation (runs in a worker thread) ----------------------------
    def prepare(
        self,
        raw: bytes,
        launch: Launch | None,
        started: float,
        adapter: Adapter,
        user_agent: str | None,
        consumer: bool = False,
        cfg: dict | None = None,
    ) -> Prepared:
        """Plan one model request and return the attempts to send.

        Runs in a worker thread, so JSON parsing and planning never block the event loop. Any
        exception here falls back to passing the original request through.
        """
        profile = for_client(adapter.name, user_agent, launch.agent if launch else None)
        passthrough = [("original", raw, False)]
        try:
            body = fastjson.loads(raw)
        except Exception:
            return Prepared(None, "passthrough", None, passthrough, ["body is not JSON"], adapter, profile.name)
        if not adapter.accepts(body):
            return Prepared(
                None, "passthrough", None, passthrough, [f"no {adapter.units_key} array"], adapter, profile.name
            )

        sid = adapter.session_key(body)
        side = not body.get("tools")
        cfg = cfg if cfg is not None else self.config(launch)
        switch = self.switch()
        now = time.time()
        model = adapter.model(body)
        if switch == "hard" or (launch and launch.mode == "passthrough"):
            self.store.get_or_create_session(sid, launch.id if launch else None, "passthrough", now)
            self.store.touch_session(sid, now, 0, model)
            return Prepared(sid, "passthrough", None, passthrough, [], adapter, profile.name, body, side)

        default_mode = (
            launch.mode
            if launch and launch.mode in ("live", "shadow")
            else (self.store.get_state("mode_override") or cfg.get("mode", "shadow"))
        )
        session = self.store.get_or_create_session(sid, launch.id if launch else None, default_mode, now)
        mode = session.mode
        experimental = adapter.experimental or profile.name in EXPERIMENTAL_PROFILES
        if mode == "live" and experimental and not (launch and launch.experimental):
            mode = "shadow"  # experimental support goes live only on an explicit --live --experimental launch
        if switch == "off":
            mode = "freeze" if mode == "live" else "passthrough"
        if mode not in ("live", "shadow", "freeze"):
            self.store.touch_session(sid, now, session.turns, model)
            return Prepared(sid, "passthrough", None, passthrough, [], adapter, profile.name, body, side)

        spill_dir = Path(launch.cwd) / ".lightfoot" / "spill" if launch and launch.cwd else lightfoot_home() / "spill"
        ctx = Context(
            cfg=cfg,
            store=self.store,
            mode=mode,
            session=session,
            spill_dir=spill_dir,
            graphify_report=launch.graphify_report if launch else None,
            enforced=self.enforced(cfg),
            started=started,
            now=now,
            adapter=adapter,
            profile=profile,
            consumer_auth=consumer,
            provider=providers.for_request(adapter.name, self.upstream(launch, adapter, cfg)),
        )
        plan = plan_request(body, ctx)
        self.store.touch_session(sid, now, plan.turns, plan.model)
        if not side:
            self.record_prefix(sid, launch, body, adapter, plan)

        live_traffic = mode in ("live", "freeze")
        if plan.new_rewrites or plan.new_prune:
            try:
                self.store.put_rewrites(plan.new_rewrites)
                if plan.new_prune:
                    self.store.put_prune(plan.new_prune)
            except Exception as exc:  # cannot freeze it, so do not send it
                plan.notes.append(f"could not store rewrites: {exc}")
                try:
                    self.store.restore_rewrites(plan.new_rewrites, plan.replaced_rewrites)
                    if plan.new_prune:
                        self.store.restore_prune(plan.new_prune, plan.replaced_prune)
                except Exception:  # noqa: S110 - best effort; nothing new is sent either way
                    pass
                plan.new_rewrites, plan.replaced_rewrites, plan.epoch = [], [], None
                plan.new_prune, plan.prune_info = None, None
                plan.body_out, plan.replay_differs = plan.replay_body, False
                plan.saved_tokens = plan.replay_saved
        if plan.epoch and plan.new_rewrites:
            self.store.record_epoch(sid, plan.turns, int(plan.epoch.get("predicted_write", 0)) if live_traffic else 0)
        for tid, rule, kind, path in plan.signals:
            self.store.add_signal(tid, sid, rule, kind, path)
        if plan.guard_state is not None and live_traffic:
            self.update_guards(sid, plan, cfg)

        attempts: list[tuple[str, bytes, bool]] = []
        if live_traffic and plan.body_out is not None:
            attempts.append(("rewrite", dumps(plan.body_out), plan.drop_block))
            if plan.replay_differs and plan.replay_body is not None:
                attempts.append(("replay", dumps(plan.replay_body), plan.drop_block))
        attempts.append(("original", raw, False))
        return Prepared(sid, mode, plan, attempts, plan.notes, adapter, profile.name, body, side)

    # --- HTTP --------------------------------------------------------------------
    def host_allowed(self, request: Request) -> bool:
        """Whether the Host header is this proxy on 127.0.0.1 or localhost. Anything else may be DNS rebinding."""
        host = (request.headers.get("host") or "").lower()
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}", "127.0.0.1", "localhost"}

    async def health(self, request: Request) -> Response:
        """`GET /lightfoot/health`: the version and pid, for `lightfoot status` and the launchers."""
        if not self.host_allowed(request):  # a rebound DNS name would otherwise read the pid and version
            return api_error(403, "unexpected Host header")
        return JSONResponse({"ok": True, "version": __version__, "pid": os.getpid()})

    async def shutdown_route(self, request: Request) -> Response:
        """`POST /lightfoot/shutdown`, which needs the control header a browser can't send cross-site."""
        if request.headers.get(CONTROL_HEADER) != "1" or not self.host_allowed(request):
            return api_error(403, "forbidden")
        if self.server is not None:
            self.server.should_exit = True
        return JSONResponse({"ok": True})

    def upstream(self, launch: Launch | None, adapter: Adapter | None, cfg: dict) -> str:
        """Where a request goes: the launch's upstream, else the configured one for its protocol."""
        if launch and launch.upstream:
            return launch.upstream
        if adapter is not None and adapter.name in ("responses", "chat"):
            return cfg.get("openai_upstream", "https://api.openai.com")
        if adapter is not None and adapter.name == "gemini":
            return cfg.get("gemini_upstream", "https://generativelanguage.googleapis.com")
        return cfg["upstream"]

    async def handle(self, request: Request) -> Response:
        """Every proxied request: refuse foreign hosts and web pages, rewrite model requests, stream everything else."""
        if not self.host_allowed(request):
            return api_error(403, "unexpected Host header")
        # Browsers attach Origin to cross-site requests; coding agents (Node, Rust, Python clients)
        # never do. Refusing it keeps any web page from using the proxy.
        if request.headers.get("origin") is not None:
            return api_error(403, "requests from web pages are not accepted")
        launch_id = request.path_params.get("launch")
        path = request.path_params.get("path", "")
        launch = self.launch(launch_id)
        adapter = for_path(request.method, path)
        cfg = self.config(launch)
        url = self.upstream(launch, adapter, cfg).rstrip("/") + "/" + path.lstrip("/")
        if request.url.query:
            url += "?" + request.url.query
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_HEADERS]
        headers.append(("accept-encoding", "identity"))
        raw = await read_body(request)
        if raw is None:
            return api_error(413, f"request body larger than {MAX_BODY_BYTES // 1_000_000} MB")
        if launch is not None and self.capturing(launch_id):
            from .capture import save

            try:
                await asyncio.to_thread(save, launch_id, request.method, path, headers, raw)
            except Exception as exc:  # a capture must never affect the request
                self.store.event("warn", "capture", repr(exc))

        if adapter is None:
            return await self.forward(
                request.method, url, headers, [("original", raw, False)], None, launch_id, None, None
            )

        started = time.perf_counter()
        user_agent = request.headers.get("user-agent")
        consumer = consumer_auth(headers)
        try:
            prepared = await asyncio.to_thread(self.prepare, raw, launch, started, adapter, user_agent, consumer, cfg)
        except Exception as exc:  # fail-safe: the original request goes out unchanged
            log.exception("prepare failed")
            self.store.event("error", "prepare", repr(exc))
            prepared = Prepared(None, "passthrough", None, [("original", raw, False)], [repr(exc)], adapter)
        for note in prepared.notes:
            self.store.event("info", "note", note, prepared.session_id)
        self.note_idle(prepared, launch)
        plan = prepared.plan
        if (
            plan is not None
            and plan.compaction
            and prepared.session_id not in self._compacting
            and not consumer  # never lightfoot's own request on a subscription login
        ):
            self._compacting.add(prepared.session_id)
            task = asyncio.create_task(self.compact(prepared, url, headers, launch_id, client_version(user_agent)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return await self.forward(
            request.method, url, headers, prepared.attempts, prepared, launch_id, started, client_version(user_agent)
        )

    async def forward(
        self,
        method: str,
        url: str,
        headers: list,
        attempts: list,
        prepared: Prepared | None,
        launch_id: str | None,
        started: float | None,
        client: str | None,
    ) -> Response:
        """Send the attempts in order until one is accepted, then stream the response back.

        Only a 400 on a rewritten attempt moves to the next one; any other status, or a 400 on
        the original, goes straight to the client exactly as the API sent it.
        """
        if self.client is None:
            return api_error(503, "proxy is starting up")
        response: httpx.Response | None = None
        used = attempts[-1][0]
        failed_with_stored = False
        compacted = bool(prepared and prepared.plan and prepared.plan.compact_beta)
        for i, (name, content, drop_block) in enumerate(attempts):
            hdrs = _with_beta(list(headers), BINDING_BETA) if drop_block else headers
            if compacted and name != "original":
                hdrs = _with_beta(list(hdrs), COMPACT_BETA)
            try:
                request = self.client.build_request(method, url, headers=hdrs, content=content)
                response = await self.client.send(request, stream=True)
            except httpx.HTTPError as exc:
                return api_error(502, f"upstream unreachable: {exc!r}")
            if response.status_code == 400 and i < len(attempts) - 1:
                detail = (await response.aread()).decode("utf-8", "replace")
                await response.aclose()
                await asyncio.to_thread(self.on_rejected, name, detail, prepared)
                carried_stored = bool(prepared and prepared.plan and prepared.plan.replay_body is not None)
                failed_with_stored = failed_with_stored or carried_stored
                continue
            used = name
            break
        if response is None:  # attempts is never empty, so this cannot happen
            return api_error(500, "no request was sent")
        if (
            prepared
            and used == "original"
            and failed_with_stored
            and prepared.session_id
            and response.status_code < 400
        ):
            # The stored rewrites themselves are rejected: stop rewriting this conversation.
            self.store.set_session_mode(prepared.session_id, "passthrough")
            self.store.event(
                "warn",
                "session_passthrough",
                "stored rewrites rejected by the API; session switched to passthrough",
                prepared.session_id,
            )

        parser = None
        if prepared and prepared.adapter is not None:
            parser = prepared.adapter.usage_parser(response.headers.get("content-type"), prepared.request_body)
        out_headers = {k: v for k, v in response.headers.items() if k.lower() not in RESPONSE_DROP}
        status = response.status_code

        async def stream():
            try:
                async for chunk in response.aiter_raw():
                    if parser:
                        parser.feed(chunk)
                    yield chunk
            finally:
                await response.aclose()
                if parser and prepared:
                    parser.finish()
                    try:
                        self.record(prepared, parser, status, used, launch_id, started, client)
                    except Exception:
                        log.exception("ledger write failed")

        return StreamingResponse(stream(), status_code=status, headers=out_headers)

    async def compact(self, prepared: Prepared, url: str, headers: list, launch_id: str | None, client: str | None):
        """Ask the API for a model-written summary of the oldest turns (prune.summarizer = "model").

        Runs beside the user's request, with that request's own auth headers (held in memory
        only). A signed block is stored as a pending prune, which takes effect at the next user
        prompt. Any failure marks the session, and its next prune uses the extractive summary.
        """
        sid, job = prepared.session_id, prepared.plan.compaction
        new: Prune = job["prune"]
        started = time.perf_counter()
        status, data = 0, {}
        try:
            hdrs = [(k, v) for k, v in _with_beta(list(headers), COMPACT_BETA) if k.lower() != "accept"]
            response = await self.client.post(url, headers=hdrs, content=dumps(job["request"]))
            status = response.status_code
            data = response.json() if "json" in response.headers.get("content-type", "") else {}
        except (httpx.HTTPError, ValueError) as exc:
            data = {"error": repr(exc)}
        finally:
            self._compacting.discard(sid)
        try:
            await asyncio.to_thread(self._finish_compaction, prepared, new, status, data, launch_id, started, client)
        except Exception:
            log.exception("compaction bookkeeping failed")

    def _finish_compaction(self, prepared, new, status, data, launch_id, started, client) -> None:
        sid = prepared.session_id
        usage = data.get("usage") if isinstance(data, dict) else None
        steps = [it for it in (usage or {}).get("iterations") or [] if it.get("type") == "compaction"]
        if steps:
            writes = [it.get("cache_creation") or {} for it in steps]
            self.store.add_ledger(
                {
                    "ts": time.time(),
                    "launch_id": launch_id,
                    "session_id": sid,
                    "mode": prepared.mode,
                    "model": data.get("model") or prepared.plan.model,
                    "status": status,
                    "turn": prepared.plan.turns,
                    "input": sum(int(it.get("input_tokens") or 0) for it in steps),
                    "cw5": sum(int(w.get("ephemeral_5m_input_tokens") or 0) for w in writes),
                    "cw1h": sum(int(w.get("ephemeral_1h_input_tokens") or 0) for w in writes),
                    "cr": sum(int(it.get("cache_read_input_tokens") or 0) for it in steps),
                    "output": sum(int(it.get("output_tokens") or 0) for it in steps),
                    "saved_tokens": 0,
                    "rules": {},
                    "epoch": 0,
                    "attempt": "compaction",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "agent": prepared.agent,
                    "client_version": client,
                }
            )
        block = prune.compaction_block(data) if status == 200 else None
        if block is None:
            reason = (data.get("stop_reason") if status == 200 else None) or f"status {status}: {str(data)[:300]}"
            self.store.set_state(f"compaction_failed:{sid}", str(reason)[:500])
            note = f"model summary not used ({reason}); the extractive summary is used instead"
            self.store.event("warn", "compaction_failed", note, sid)
            return
        msgs = prepared.request_body["messages"]
        dropped = new.dropped_tokens + tokens(new.summary) + prune.message_tokens(msgs[0]) - tokens(block["content"])
        stored = Prune(
            sid,
            new.live,
            new.boundary,
            new.fingerprint,
            json.dumps(block, separators=(",", ":"), ensure_ascii=False),
            new.history_path,
            max(0, dropped),
            kind="compaction",
            pending=True,
        )
        self.store.put_prune(stored)
        note = f"model summary of {new.boundary} messages ready for the next prompt"
        self.store.event("info", "compaction_ready", note, sid)

    def on_rejected(self, name: str, detail: str, prepared: Prepared | None) -> None:
        """Undo what a rejected attempt stored, and learn from the error.

        New rewrites and a new prune were stored before sending, so a 400 restores the previous
        records. A prefix-binding error means this account enforces the thinking check.
        """
        plan = prepared.plan if prepared else None
        sid = prepared.session_id if prepared else None
        self.store.event("warn", f"rejected_{name}", detail[:2000], sid)
        if plan is None:
            return
        if name == "rewrite" and plan.new_rewrites:
            self.store.restore_rewrites(plan.new_rewrites, plan.replaced_rewrites)
        if name == "rewrite" and plan.new_prune:
            self.store.restore_prune(plan.new_prune, plan.replaced_prune)
        if prepared.adapter is not None and prepared.adapter.supports_drop_block and BINDING_ERROR_RE.search(detail):
            self.store.set_state("account_enforced", "1")
            self.store.event(
                "warn",
                "account_enforced",
                "the API enforces the thinking-prefix check for this account; epochs now avoid "
                "regions followed by thinking blocks",
                sid,
            )

    def record(
        self,
        prepared: Prepared,
        parser,
        status: int,
        used: str,
        launch_id: str | None,
        started: float | None,
        client: str | None,
    ) -> None:
        """Write the ledger row for a finished request: tokens by kind, cost, saving and the attempt used."""
        plan = prepared.plan
        counts = parser.counts()
        # Shadow rows carry projected savings; live rows what the forwarded request actually saved.
        applied = plan is not None and (prepared.mode == "shadow" or used == "rewrite")
        if applied:
            saved = plan.saved_tokens
        elif plan is not None and used == "replay":
            saved = plan.replay_saved
        else:
            saved = 0
        self.store.add_ledger(
            {
                "ts": time.time(),
                "launch_id": launch_id,
                "session_id": prepared.session_id,
                "mode": prepared.mode,
                "model": parser.model or (plan.model if plan else None),
                "status": status,
                "turn": plan.turns if plan else None,
                **counts,
                "saved_tokens": saved,
                "rules": plan.rules if applied else {},
                "epoch": int(bool(applied and plan.epoch)),
                "attempt": used,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1) if started else None,
                "agent": prepared.agent,
                "client_version": client,
                "side": int(prepared.side),
            }
        )
        if prepared.session_id and status == 200:
            context = counts["input"] + counts["cw5"] + counts["cw1h"] + counts["cr"]
            self.store.set_context(prepared.session_id, context)
            if plan and plan.epoch and used == "rewrite" and plan.epoch.get("predicted_write"):
                written = counts["cw5"] + counts["cw1h"]
                if written > 1.5 * plan.epoch["predicted_write"] + 5000:
                    self.store.disable_epochs(prepared.session_id)
                    self.store.event(
                        "warn",
                        "epochs_disabled",
                        f"cache write {written} exceeded prediction {plan.epoch['predicted_write']}",
                        prepared.session_id,
                    )
                else:
                    self.store.clear_pending_write(prepared.session_id)


def _with_beta(headers: list, beta: str) -> list:
    out, found = [], False
    for k, v in headers:
        if k.lower() == "anthropic-beta":
            values = [x.strip() for x in v.split(",") if x.strip()]
            if beta not in values:
                values.append(beta)
            out.append((k, ",".join(values)))
            found = True
        else:
            out.append((k, v))
    if not found:
        out.append(("anthropic-beta", beta))
    return out


def build_app(proxy: Proxy, prune: bool = False) -> Starlette:
    """The Starlette app: the health and shutdown routes, then every path under `/l/<launch>/` and `/`."""
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await proxy.startup(prune=prune)
        try:
            yield
        finally:
            await proxy.shutdown()

    return Starlette(
        routes=[
            Route("/lightfoot/health", proxy.health, methods=["GET"]),
            Route("/lightfoot/shutdown", proxy.shutdown_route, methods=["POST"]),
            Route("/l/{launch}/{path:path}", proxy.handle, methods=methods),
            Route("/{path:path}", proxy.handle, methods=methods),
        ],
        lifespan=lifespan,
    )


def serve(port: int | None = None) -> None:
    """Run the proxy in the foreground (the daemon's entry point), logging to `~/.lightfoot/proxy.log`."""
    import uvicorn

    home = lightfoot_home()
    private_dir(home)
    handler = logging.handlers.RotatingFileHandler(
        home / "proxy.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per upstream request is noise
    cfg = load_config()
    for error in pricing.configure(cfg.get("prices")):
        log.warning("config: %s", error)
    port = port or int(cfg["port"])
    store = Store()
    proxy = Proxy(store, port=port)
    app = build_app(proxy, prune=True)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False, timeout_keep_alive=75
    )
    pid_file, pid = home / "proxy.pid", str(os.getpid())

    class Server(uvicorn.Server):
        async def startup(self, sockets=None) -> None:
            await super().startup(sockets)
            # Only once the port is bound: a second daemon started at the same moment fails to
            # bind and exits, and must not leave the running one without its pid file.
            if self.started:
                pid_file.write_text(pid, encoding="utf-8")
                log.info("lightfoot %s listening on 127.0.0.1:%s", __version__, port)

    server = Server(config)
    proxy.server = server
    try:
        server.run()
    finally:
        try:
            if pid_file.read_text(encoding="utf-8").strip() == pid:
                pid_file.unlink()
        except OSError:
            pass
