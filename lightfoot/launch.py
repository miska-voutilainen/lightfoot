"""Proxy daemon management and the `lightfoot claude` wrapper."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__
from .config import ensure_global_config, lightfoot_home, load_config, private_dir
from .integrations import detect
from .store import Launch, Store

CONTROL_HEADER = {"x-lightfoot-control": "1"}


def base_url(port: int) -> str:
    """The proxy's base URL on this machine."""
    return f"http://127.0.0.1:{port}"


def _local_request(port: int, method: str, path: str, timeout: float, headers: dict | None = None):
    """(status, body) from the local proxy, or None if it can't be reached.

    The standard library keeps `lightfoot claude` from importing httpx (about 0.25 s) just to
    talk to localhost.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def health(port: int, timeout: float = 0.5) -> dict | None:
    """The running proxy's health response, or None when nothing answers on `port`."""
    result = _local_request(port, "GET", "/lightfoot/health", timeout)
    if result and result[0] == 200:
        try:
            return json.loads(result[1])
        except ValueError:
            return None
    return None


def running(port: int) -> dict | None:
    """Health info if the proxy is up. Without a pid file it can't be, so no connection is tried:
    on Windows a refused localhost connection takes about a second to fail."""
    if not (lightfoot_home() / "proxy.pid").exists():
        return None
    return health(port)


def start_daemon(port: int, wait: float = 10.0) -> dict:
    """Start the proxy in the background unless it is already running; returns its health info."""
    info = running(port)
    if info:
        return info
    home = lightfoot_home()
    private_dir(home)
    stderr_path = home / "proxy.stderr.log"
    try:  # keep one previous file instead of growing forever
        if stderr_path.stat().st_size > 5_000_000:
            stderr_path.replace(stderr_path.with_suffix(".log.1"))
    except OSError:
        pass
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(stderr_path, "ab") as stderr:  # the child keeps its own handle
        subprocess.Popen(
            [sys.executable, "-m", "lightfoot", "serve", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=stderr,
            stderr=stderr,
            close_fds=True,
            **kwargs,
        )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        info = health(port, timeout=0.2)
        if info:
            return info
        time.sleep(0.1)
    raise RuntimeError(f"the proxy did not start on port {port}; see {home / 'proxy.stderr.log'}")


def stop_daemon(port: int, wait: float = 10.0) -> bool:
    """Ask the proxy to shut down and wait for it; False when it wasn't running or didn't stop in time."""
    if not running(port):
        return False
    _local_request(port, "POST", "/lightfoot/shutdown", 2.0, CONTROL_HEADER)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not health(port, timeout=0.3):
            return True
        time.sleep(0.2)
    return False


def settings_base_url(cwd: str | None) -> str | None:
    """ANTHROPIC_BASE_URL from Claude Code's settings files, highest precedence first.

    Claude Code applies `env` from its settings over the process environment, so a gateway
    set there is where requests really go.
    """
    files = []
    if cwd:
        files += [Path(cwd) / ".claude" / "settings.local.json", Path(cwd) / ".claude" / "settings.json"]
    files.append(Path.home() / ".claude" / "settings.json")
    for path in files:
        try:
            env = json.loads(path.read_text(encoding="utf-8")).get("env") or {}
        except (OSError, ValueError, AttributeError):
            continue
        value = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_upstream(port: int, cfg: dict, cwd: str | None = None) -> str:
    """An ANTHROPIC_BASE_URL already set by the user (a gateway, say) stays the upstream.

    Claude Code's settings files take precedence over the shell environment, as in Claude Code.
    """
    ours = (base_url(port), f"http://localhost:{port}")
    for existing in (settings_base_url(cwd) or "", os.environ.get("ANTHROPIC_BASE_URL", "").strip()):
        if existing and not existing.startswith(ours):
            return existing.rstrip("/")
    return str(cfg["upstream"]).rstrip("/")


def resolve_openai_upstream(port: int, cfg: dict) -> str:
    """OPENAI_BASE_URL (minus its /v1) when the user already points at a gateway; else the config value."""
    existing = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    if existing and not existing.startswith((base_url(port), f"http://localhost:{port}")):
        return existing[:-3] if existing.endswith("/v1") else existing
    return str(cfg.get("openai_upstream", "https://api.openai.com")).rstrip("/")


def resolve_gemini_upstream(port: int, cfg: dict) -> str:
    """GOOGLE_GEMINI_BASE_URL when the user already points Gemini CLI at a gateway; else the config value."""
    existing = os.environ.get("GOOGLE_GEMINI_BASE_URL", "").strip().rstrip("/")
    if existing and not existing.startswith((base_url(port), f"http://localhost:{port}")):
        return existing
    return str(cfg.get("gemini_upstream", "https://generativelanguage.googleapis.com")).rstrip("/")


def register_launch(
    store: Store,
    cwd: str,
    mode: str,
    port: int,
    label: str | None = None,
    agent: str = "claude",
    experimental: bool = False,
    upstream: str | None = None,
) -> tuple[Launch, object]:
    """Store a new launch for `cwd`: its mode, detected integrations and the upstream its agent needs.

    Returns the launch and the detected integrations.
    """
    cfg = load_config(cwd)
    found = detect(cwd, cfg)
    launch = Launch(
        id=uuid.uuid4().hex[:12],
        cwd=str(Path(cwd).resolve()),
        mode=mode,
        upstream=upstream
        or (
            resolve_openai_upstream(port, cfg)
            if agent == "codex"
            else resolve_gemini_upstream(port, cfg)
            if agent == "gemini"
            else str(cfg["upstream"]).rstrip("/")
            if agent in ("opencode", "cline", "roo")
            else resolve_upstream(port, cfg, cwd)
        ),
        graphify_report=found.graphify_report,
        vault=found.vault,
        label=label,
        agent=agent,
        experimental=experimental,
    )
    store.put_launch(launch)
    return launch, found


def default_mode(store: Store, cfg: dict) -> str:
    """The mode for new launches: `lightfoot shadow on|off`, else the config; shadow unless it says live."""
    mode = store.get_state("mode_override") or str(cfg.get("mode", "shadow"))
    return mode if mode in ("live", "shadow") else "shadow"


def claude_env(port: int, launch: Launch) -> dict:
    """The environment for Claude Code: `ANTHROPIC_BASE_URL` pointing at this launch."""
    env = local_env()
    env["ANTHROPIC_BASE_URL"] = f"{base_url(port)}/l/{launch.id}"
    return env


def local_env() -> dict:
    """The current environment, with the proxy's loopback address exempt from any HTTP proxy."""
    env = os.environ.copy()
    no_proxy = [x for x in env.get("NO_PROXY", env.get("no_proxy", "")).split(",") if x.strip()]
    for host in ("127.0.0.1", "localhost"):
        if host not in no_proxy:
            no_proxy.append(host)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(no_proxy)
    return env


def find_claude() -> str:
    """Path to the `claude` executable, or an error naming what is missing."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("could not find the `claude` executable on PATH")
    return exe


WATCHDOG_INTERVAL = 5.0


def run_child(argv: list[str], env: dict, cwd: str | None = None, port: int | None = None) -> int:
    """Run a child process, letting it handle Ctrl+C itself; wait for it to exit.

    With `port`, also act as a watchdog: if the proxy dies while the agent runs, restart
    it. Launches and stored rewrites live in the database, so the restarted proxy picks
    up every running session where it left off.
    """
    process = subprocess.Popen(argv, env=env, cwd=cwd)
    while True:
        try:
            return process.wait(timeout=WATCHDOG_INTERVAL if port else None)
        except subprocess.TimeoutExpired:
            if not health(port, timeout=1.0):
                try:
                    start_daemon(port)
                    print("\nlightfoot: the proxy had stopped and was restarted", file=sys.stderr)
                except RuntimeError as exc:
                    print(f"\nlightfoot: the proxy stopped and could not be restarted: {exc}", file=sys.stderr)
                    port = None  # stop retrying; the agent's own retries will surface the error
        except KeyboardInterrupt:
            continue


def find_codex() -> str:
    """Path to the `codex` executable, or an error naming what is missing."""
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("could not find the `codex` executable on PATH")
    return exe


CODEX_PROVIDER = "lightfoot"


def codex_provider_args(port: int, launch: Launch) -> list[str]:
    """Codex `-c` overrides that route it through lightfoot.

    A custom provider, not `openai_base_url`: Codex's built-in openai provider talks
    Responses over WebSocket with incremental payloads, while a custom provider uses
    plain HTTP with the full history (codex-rs model-provider-info, supports_websockets).
    Values are TOML strings, so they parse the same on every OS.
    """
    settings = {
        "name": "lightfoot",
        "base_url": f"{base_url(port)}/l/{launch.id}/v1",
        "env_key": "OPENAI_API_KEY",
        "wire_api": "responses",
    }
    args = []
    for key, value in settings.items():
        args += ["-c", f"model_providers.{CODEX_PROVIDER}.{key}={json.dumps(value)}"]
    return [*args, "-c", f"model_provider={json.dumps(CODEX_PROVIDER)}"]


WELCOME_SHADOW = """\
lightfoot: first run. New sessions start in SHADOW mode: every rewrite is computed and
its saving logged, but your original requests are sent unchanged. After some normal
work, check `lightfoot stats`; when you are satisfied (ideally after `lightfoot eval`),
switch with `lightfoot shadow off`. `lightfoot doctor` checks the setup.
"""
WELCOME_LIVE = """\
lightfoot: first run, in LIVE mode (set in your config): tool output is compressed
before it is sent. `lightfoot stats` shows the savings, `lightfoot off` stops new
rewrites immediately, and `lightfoot doctor` checks the setup.
"""


def _prepare_launch(cwd: str, mode: str | None, agent: str, experimental: bool = False) -> tuple[Launch, object, int]:
    ensure_global_config()
    cfg = load_config(cwd)
    port = int(cfg["port"])
    info = start_daemon(port)
    if info.get("version") != __version__:
        print(
            f"lightfoot: the running proxy is version {info.get('version')}, this is {__version__}; "
            f"run `lightfoot restart` when no session is active",
            file=sys.stderr,
        )
    store = Store()
    try:
        launch_mode = mode or default_mode(store, cfg)
        if not store.get_state("welcomed"):
            print(WELCOME_LIVE if launch_mode == "live" else WELCOME_SHADOW, file=sys.stderr)
            store.set_state("welcomed", "1")
        launch, found = register_launch(store, cwd, launch_mode, port, agent=agent, experimental=experimental)
    finally:
        store.close()
    extras = [name for name, on in (("graphify", found.graphify_report), ("obsidian", found.vault)) if on]
    print(
        f"lightfoot: {launch.mode} mode{' (experimental)' if experimental else ''}, proxy on :{port}"
        + (f", integrations: {', '.join(extras)}" if extras else ""),
        file=sys.stderr,
    )
    return launch, found, port


def run_claude(args: list[str], mode: str | None = None) -> int:
    """`lightfoot claude`: start Claude Code through a new launch, passing every argument on.

    The base URL also goes in a temporary `--settings` file, which wins over `env` in the user's own
    settings files, and is deleted when Claude Code exits.
    """
    if os.environ.get("LIGHTFOOT_OFF") == "1":
        print("lightfoot: LIGHTFOOT_OFF=1, starting Claude Code without the proxy", file=sys.stderr)
        return run_child([find_claude(), *args], os.environ.copy())
    launch, _, port = _prepare_launch(os.getcwd(), mode, "claude")
    if "--settings" in args:
        print(
            "lightfoot: your --settings must not set ANTHROPIC_BASE_URL, or this session bypasses the proxy",
            file=sys.stderr,
        )
    settings = launch_settings_file(port, launch)
    try:
        return run_child([find_claude(), "--settings", str(settings), *args], claude_env(port, launch), port=port)
    finally:
        try:
            settings.unlink()
        except OSError:
            pass


def launch_settings_file(port: int, launch: Launch) -> Path:
    """A settings file for `claude --settings` that routes this run through its launch.

    Claude Code applies `env` from settings files over the process environment, so an
    ANTHROPIC_BASE_URL in ~/.claude/settings.json (a gateway, or `lightfoot integrate`)
    would otherwise send this run past its launch. `--settings` takes precedence over
    those files. A file rather than inline JSON keeps Windows .cmd shims from mangling quotes.
    """
    folder = private_dir(lightfoot_home() / "launches")
    path = folder / f"{launch.id}.json"
    env = {"ANTHROPIC_BASE_URL": f"{base_url(port)}/l/{launch.id}"}
    ttl = str(load_config(launch.cwd).get("cache", {}).get("ttl") or "")
    if ttl in ("5m", "1h"):
        env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = ttl  # cache.ttl; Claude Code ignores other values
    path.write_text(json.dumps({"env": env}), encoding="utf-8")
    return path


def find_opencode() -> str:
    """Path to the `opencode` executable, or an error naming what is missing."""
    exe = shutil.which("opencode")
    if not exe:
        raise RuntimeError("could not find the `opencode` executable on PATH")
    return exe


def opencode_config(port: int, launch: Launch, existing: str | None = None) -> str:
    """OPENCODE_CONFIG_CONTENT that points opencode's Anthropic provider at this launch.

    opencode merges this inline config over its config files, so the user's own
    opencode.json is never edited. Inline config the user already set is kept.
    """
    try:
        config = json.loads(existing) if existing else {}
    except ValueError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    provider = config.setdefault("provider", {}).setdefault("anthropic", {})
    provider.setdefault("options", {})["baseURL"] = f"{base_url(port)}/l/{launch.id}/v1"
    return json.dumps(config)


EXTENSION_SETUP = """In {title}: Settings > API Provider "Anthropic" > tick "Use custom base URL" and paste

    {url}

Your Anthropic API key stays in {title}; lightfoot forwards it unchanged.
Requests go through the proxy only while it runs: after a restart, run `lightfoot start`.
To stop using lightfoot, clear the base URL in {title}.
"""

# Agents that keep their provider settings themselves, so lightfoot routes them through a base
# URL the user pastes in: (name, URL suffix their Anthropic client needs before /messages).
# Cline's AI SDK client posts to <base>/messages; Roo Code's Anthropic SDK to <base>/v1/messages.
EXTENSIONS = {"cline": ("Cline", "/v1"), "roo": ("Roo Code", "")}


def setup_extension(agent: str, mode: str | None = None, experimental: bool = False) -> int:
    """Register a lasting launch for this project and print the base URL to paste into the agent.

    Cline and Roo Code (VS Code extensions) keep their provider settings themselves, so
    lightfoot can't start them with a base URL as it does other agents. Their support is
    experimental: shadow by default, live only with --live --experimental.
    """
    title, suffix = EXTENSIONS[agent]
    if mode == "live" and not experimental:
        raise RuntimeError(
            f"{title} support is experimental: use `lightfoot {agent} --live --experimental` to go live "
            "(without --live it runs in shadow mode, which never changes requests)"
        )
    launch, _, port = _prepare_launch(os.getcwd(), mode or "shadow", agent, experimental)
    print(EXTENSION_SETUP.format(title=title, url=f"{base_url(port)}/l/{launch.id}{suffix}"))
    return 0


def run_opencode(args: list[str], mode: str | None = None, experimental: bool = False) -> int:
    """opencode support is experimental: shadow by default, live only with --live --experimental."""
    opencode = find_opencode()
    if os.environ.get("LIGHTFOOT_OFF") == "1":
        print("lightfoot: LIGHTFOOT_OFF=1, starting opencode without the proxy", file=sys.stderr)
        return run_child([opencode, *args], os.environ.copy())
    if mode == "live" and not experimental:
        raise RuntimeError(
            "opencode support is experimental: use `lightfoot opencode --live --experimental` to go live "
            "(without --live it runs in shadow mode, which never changes requests)"
        )
    launch, _, port = _prepare_launch(os.getcwd(), mode or "shadow", "opencode", experimental)
    env = local_env()
    env["OPENCODE_CONFIG_CONTENT"] = opencode_config(port, launch, env.get("OPENCODE_CONFIG_CONTENT"))
    return run_child([opencode, *args], env, port=port)


def find_gemini() -> str:
    """Path to the `gemini` executable, or an error naming what is missing."""
    exe = shutil.which("gemini")
    if not exe:
        raise RuntimeError("could not find the `gemini` executable on PATH")
    return exe


# Gemini CLI auth types that send requests somewhere GOOGLE_GEMINI_BASE_URL doesn't apply:
# Google sign-in and compute credentials use Google's Code Assist service, Vertex AI its own URL.
GEMINI_UNROUTED_AUTH = {
    "oauth-personal": "Sign in with Google",
    "compute-default-credentials": "Cloud Shell or metadata-server credentials",
    "cloud-shell": "Cloud Shell credentials",
    "vertex-ai": "Vertex AI",
}


def gemini_auth_type(cwd: str) -> str | None:
    """Gemini CLI's saved auth choice (security.auth.selectedType), the project's settings first."""
    for path in (Path(cwd) / ".gemini" / "settings.json", Path.home() / ".gemini" / "settings.json"):
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
            value = settings["security"]["auth"]["selectedType"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(value, str) and value:
            return value
    return None


def run_gemini(args: list[str], mode: str | None = None, experimental: bool = False) -> int:
    """Gemini CLI support is experimental: shadow by default, live only with --live --experimental.

    Only API-key and gateway auth honour GOOGLE_GEMINI_BASE_URL; other auth types are
    reported, because their requests would never reach the proxy.
    """
    gemini = find_gemini()
    if os.environ.get("LIGHTFOOT_OFF") == "1":
        print("lightfoot: LIGHTFOOT_OFF=1, starting Gemini CLI without the proxy", file=sys.stderr)
        return run_child([gemini, *args], os.environ.copy())
    if mode == "live" and not experimental:
        raise RuntimeError(
            "Gemini CLI support is experimental: use `lightfoot gemini --live --experimental` to go live "
            "(without --live it runs in shadow mode, which never changes requests)"
        )
    auth = gemini_auth_type(os.getcwd())
    if auth in GEMINI_UNROUTED_AUTH:
        print(
            f"lightfoot: Gemini CLI is set to {GEMINI_UNROUTED_AUTH[auth]}, which doesn't use GOOGLE_GEMINI_BASE_URL, "
            'so this session bypasses lightfoot. Choose "Use Gemini API Key" in /auth to route it.',
            file=sys.stderr,
        )
    launch, _, port = _prepare_launch(os.getcwd(), mode or "shadow", "gemini", experimental)
    env = local_env()
    env["GOOGLE_GEMINI_BASE_URL"] = f"{base_url(port)}/l/{launch.id}"
    return run_child([gemini, *args], env, port=port)


def run_codex(args: list[str], mode: str | None = None, experimental: bool = False) -> int:
    """Codex CLI support is experimental: shadow by default, live only with --live --experimental."""
    codex = find_codex()
    if os.environ.get("LIGHTFOOT_OFF") == "1":
        print("lightfoot: LIGHTFOOT_OFF=1, starting Codex without the proxy", file=sys.stderr)
        return run_child([codex, *args], os.environ.copy())
    if mode == "live" and not experimental:
        raise RuntimeError(
            "Codex support is experimental: use `lightfoot codex --live --experimental` to go live "
            "(without --live it runs in shadow mode, which never changes requests)"
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "lightfoot codex needs OPENAI_API_KEY; Codex's ChatGPT sign-in cannot be routed through a custom provider"
        )
    launch, _, port = _prepare_launch(os.getcwd(), mode or "shadow", "codex", experimental)
    return run_child([codex, *codex_provider_args(port, launch), *args], local_env(), port=port)


def run_env(port: int, launches: dict[str, Launch]) -> dict:
    """The environment for `lightfoot run`: every known base-URL variable, one launch per protocol."""
    env = local_env()
    anthropic, openai, gemini = (f"{base_url(port)}/l/{launches[p].id}" for p in ("anthropic", "openai", "gemini"))
    env["ANTHROPIC_BASE_URL"] = env["ANTHROPIC_API_BASE"] = anthropic  # ANTHROPIC_API_BASE: LiteLLM (Aider)
    env["OPENAI_BASE_URL"] = env["OPENAI_API_BASE"] = f"{openai}/v1"  # OPENAI_API_BASE: older clients
    env["GOOGLE_GEMINI_BASE_URL"] = gemini
    return env


def run_any(command: list[str], mode: str | None = None, experimental: bool = False, capture: bool = False) -> int:
    """Run any tool that reads a base-URL variable through lightfoot.

    A launch has one upstream, so each protocol gets its own launch; each upstream keeps a
    gateway the user already set. The tool's profile comes from its user agent. Support is
    experimental: shadow by default, live only with --live --experimental.

    With `capture`, the proxy also writes every request, redacted, to ~/.lightfoot/captures/.
    """
    if not command:
        raise RuntimeError("usage: lightfoot run [--live --experimental] -- <command> [args…]")
    exe = shutil.which(command[0])
    if not exe:
        raise RuntimeError(f"could not find `{command[0]}` on PATH")
    if os.environ.get("LIGHTFOOT_OFF") == "1":
        print(f"lightfoot: LIGHTFOOT_OFF=1, starting {command[0]} without the proxy", file=sys.stderr)
        return run_child([exe, *command[1:]], os.environ.copy())
    if mode == "live" and not experimental:
        raise RuntimeError(
            "`lightfoot run` is experimental: use `lightfoot run --live --experimental -- …` to go live "
            "(without --live it runs in shadow mode, which never changes requests)"
        )
    cwd = os.getcwd()
    anthropic, _, port = _prepare_launch(cwd, mode or "shadow", "run", experimental)
    cfg = load_config(cwd)
    store = Store()
    try:
        launches = {"anthropic": anthropic}
        for protocol, upstream in (
            ("openai", resolve_openai_upstream(port, cfg)),
            ("gemini", resolve_gemini_upstream(port, cfg)),
        ):
            launches[protocol], _ = register_launch(
                store, cwd, anthropic.mode, port, agent="run", experimental=experimental, upstream=upstream
            )
        if capture:
            from .capture import capture_dir, state_key

            for launch in launches.values():
                store.set_state(state_key(launch.id), "1")
            folders = sorted({str(capture_dir(lt.id).parent) for lt in launches.values()})
            print(f"lightfoot: capturing redacted requests to {folders[0]}", file=sys.stderr)
    finally:
        store.close()
    code = run_child([exe, *command[1:]], run_env(port, launches), port=port)
    if capture:
        from .capture import capture_dir

        written = [str(capture_dir(lt.id)) for lt in launches.values() if capture_dir(lt.id).exists()]
        print("lightfoot: captured " + (", ".join(written) if written else "nothing"), file=sys.stderr)
    return code
