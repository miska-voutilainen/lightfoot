"""`lightfoot` command line."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from . import __version__

USAGE = """\
lightfoot claude [--live|--shadow] [claude args…]    run Claude Code through the proxy
lightfoot codex [--live --experimental] [args…]      run OpenAI Codex CLI through it (experimental)
lightfoot opencode [--live --experimental] [args…]   run opencode through it (experimental)
lightfoot gemini [--live --experimental] [args…]     run Gemini CLI through it (experimental, API-key auth)
lightfoot cline [--live --experimental]              print a base URL that routes Cline through it (experimental)
lightfoot roo [--live --experimental]                the same for Roo Code (discontinued; experimental)
lightfoot run [--live --experimental] -- CMD [args…] run any tool that reads a base-URL variable (experimental)
lightfoot aider [--live --experimental] [args…]      run Aider through it: `run -- aider` (experimental)
lightfoot capture -- CMD [args…]                     run it in shadow mode and save its requests, redacted
lightfoot estimate [--days N] [--project TEXT]       offline savings estimate from your past sessions
lightfoot estimate --cache                           what each cache lifetime and keep-alive would have cost
lightfoot estimate --subagents                       what each kind of subagent costs
lightfoot estimate --agent codex [--path P]          replay OpenAI Codex CLI sessions (~/.codex/sessions)
lightfoot estimate --agent gemini [--path P]         replay Gemini CLI sessions (~/.gemini/tmp)
lightfoot estimate --agent opencode [--path DB]      replay opencode sessions (~/.local/share/opencode/opencode.db)
lightfoot stats [--days N] [--session ID]            cost and savings per session, rule health
lightfoot audit [--days N]                           what the system prompt, tools and MCP servers cost per project
lightfoot why [SESSION] [--full]                     what was changed in a session, and what it saved
lightfoot dashboard [--days N]                       local report page: savings over time, projects, rules
lightfoot shadow on|off                              measure only, or save (applies to new launches)
lightfoot off [--hard] | lightfoot on                kill switch, takes effect immediately
lightfoot doctor                                     check setup, integrations and hooks
lightfoot config [--path] | set KEY VALUE | get KEY  show or change ~/.lightfoot/config.toml
lightfoot report [--days N]                          shareable summary (numbers only) for issues
lightfoot events [-n N]                              recent proxy warnings and notes
lightfoot cleanup [--days N] [--dry-run]             delete stored content of inactive sessions
lightfoot start | stop | restart | status            manage the background proxy
lightfoot integrate on|off                           route IDE and desktop sessions through it too
lightfoot hook-mode install|uninstall [--agent A]    condense tool output with a hook, no proxy (claude, opencode)
lightfoot skills install|uninstall                   Lightfoot's skills for Gemini CLI, opencode and Codex CLI
lightfoot plugins install|uninstall --agent A        a native Gemini CLI extension or Codex plugin
lightfoot status-line install|uninstall              savings in the Claude Code status line
lightfoot handoff install|uninstall                  Obsidian SessionEnd hook
lightfoot eval run TASKS.toml [--runs N]             paired eval: with and without lightfoot
lightfoot eval report [RUN]                          eval results and the ship gate
lightfoot eval check TASKS.toml                      validate a task file: setup works, tests fail before (free)
"""


def _store():
    from .store import Store

    return Store()


def _config():
    from . import pricing
    from .config import load_config

    cfg = load_config()
    pricing.configure(cfg.get("prices"))
    return cfg


def _port() -> int:
    return int(_config()["port"])


def _launch_flags(argv: list[str], allowed: tuple[str, ...]) -> tuple[str | None, set[str], list[str]]:
    """Leading lightfoot flags (before the agent's own arguments)."""
    mode, flags = None, set()
    while argv and argv[0] in allowed:
        flag = argv.pop(0)
        if flag in ("--live", "--shadow"):
            mode = flag[2:]
        else:
            flags.add(flag)
    return mode, flags, argv


def cmd_claude(argv: list[str]) -> int:
    """`lightfoot claude [--live|--shadow] …`: Claude Code through the proxy."""
    from .launch import run_claude

    mode, _, rest = _launch_flags(argv, ("--live", "--shadow"))
    try:
        return run_claude(rest, mode)
    except RuntimeError as exc:
        print(f"lightfoot: {exc}", file=sys.stderr)
        return 1


def cmd_codex(argv: list[str]) -> int:
    """`lightfoot codex [--live --experimental] …`: Codex CLI through the proxy."""
    from .launch import run_codex

    mode, flags, rest = _launch_flags(argv, ("--live", "--shadow", "--experimental"))
    try:
        return run_codex(rest, mode, experimental="--experimental" in flags)
    except RuntimeError as exc:
        print(f"lightfoot: {exc}", file=sys.stderr)
        return 1


def cmd_gemini(argv: list[str]) -> int:
    """`lightfoot gemini [--live --experimental] …`: Gemini CLI through the proxy."""
    from .launch import run_gemini

    mode, flags, rest = _launch_flags(argv, ("--live", "--shadow", "--experimental"))
    try:
        return run_gemini(rest, mode, experimental="--experimental" in flags)
    except RuntimeError as exc:
        print(f"lightfoot: {exc}", file=sys.stderr)
        return 1


def cmd_extension(agent: str, argv: list[str]) -> int:
    """`lightfoot cline` and `lightfoot roo`: register a lasting launch and print its base URL."""
    from .launch import setup_extension

    mode, flags, rest = _launch_flags(argv, ("--live", "--shadow", "--experimental"))
    if rest:
        print(f"lightfoot: unexpected arguments: {' '.join(rest)}", file=sys.stderr)
        return 2
    try:
        return setup_extension(agent, mode, experimental="--experimental" in flags)
    except RuntimeError as exc:
        print(f"lightfoot: {exc}", file=sys.stderr)
        return 1


def cmd_opencode(argv: list[str]) -> int:
    """`lightfoot opencode [--live --experimental] …`: opencode through the proxy."""
    from .launch import run_opencode

    mode, flags, rest = _launch_flags(argv, ("--live", "--shadow", "--experimental"))
    try:
        return run_opencode(rest, mode, experimental="--experimental" in flags)
    except RuntimeError as exc:
        print(f"lightfoot: {exc}", file=sys.stderr)
        return 1


def cmd_run(argv: list[str], capture: bool = False) -> int:
    """`lightfoot run -- <command>`, and with `capture` `lightfoot capture -- <command>` (shadow only)."""
    from .launch import run_any

    allowed = ("--shadow",) if capture else ("--live", "--shadow", "--experimental")
    mode, flags, rest = _launch_flags(argv, allowed)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    try:
        return run_any(rest, mode, experimental="--experimental" in flags, capture=capture)
    except RuntimeError as exc:
        print(f"lightfoot: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """The `lightfoot` command: its exit code. Output never fails on a console that can't print every character."""
    from .config import ConfigError

    for stream in (sys.stdout, sys.stderr):
        try:  # a redirected Windows console may use a code page without "…" or "≈"
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        return _main(argv)
    except ConfigError as exc:
        print(f"lightfoot: config error: {exc}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print(f"lightfoot {__version__}")
        return 0
    if argv[0] == "claude":
        return cmd_claude(argv[1:])
    if argv[0] == "codex":
        return cmd_codex(argv[1:])
    if argv[0] == "opencode":
        return cmd_opencode(argv[1:])
    if argv[0] == "gemini":
        return cmd_gemini(argv[1:])
    if argv[0] in ("cline", "roo"):
        return cmd_extension(argv[0], argv[1:])
    if argv[0] == "run":
        return cmd_run(argv[1:])
    if argv[0] == "aider":
        rest = argv[1:]
        flags = []
        while rest and rest[0] in ("--live", "--shadow", "--experimental"):  # leading lightfoot flags
            flags.append(rest.pop(0))
        return cmd_run([*flags, "--", "aider", *rest])
    if argv[0] == "capture":
        return cmd_run(argv[1:], capture=True)
    if argv[:2] == ["hook", "post-tool-use"]:
        # Runs on every Bash, PowerShell and MCP tool call: skip argparse and the heavy modules.
        from .hookmode import run as post_tool_use

        out = post_tool_use(sys.stdin.buffer.read().decode("utf-8-sig", errors="replace"))  # tolerate a BOM
        if out:
            sys.stdout.buffer.write(out.encode("utf-8"))
        return 0
    if argv[:2] == ["hook", "opencode-tool-after"]:
        # Runs on every opencode shell call, from its plugin: skip argparse and the heavy modules.
        from .opencodehook import run as opencode_tool_after

        out = opencode_tool_after(sys.stdin.buffer.read().decode("utf-8-sig", errors="replace"))
        if out:
            sys.stdout.buffer.write(out.encode("utf-8"))
        return 0
    if argv[0] == "status-line" and len(argv) == 1:
        # Claude Code re-runs this often: skip argparse and the heavy modules.
        from .statusline import main as status_line

        sys.stdout.buffer.write(status_line(sys.stdin.buffer.read()))
        sys.stdout.flush()
        return 0

    import argparse  # after the hook and status-line fast paths, which never need it

    parser = argparse.ArgumentParser(prog="lightfoot", usage=USAGE)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int)
    p = sub.add_parser("report")
    p.add_argument("--days", type=float, default=30)
    p = sub.add_parser("estimate")
    p.add_argument("--path", help="a transcript file or folder (default: ~/.claude/projects)")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--project", help="only transcripts whose path contains this text")
    p.add_argument("--pruning", action="store_true", help="also estimate the ceiling of old-turn pruning")
    p.add_argument("--cache", action="store_true", help="only simulate cache lifetimes and keep-alive on your history")
    p.add_argument("--subagents", action="store_true", help="only report what each subagent type costs")
    p.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini", "opencode"],
        default="claude",
        help="whose session logs to replay",
    )
    p = sub.add_parser("cleanup")
    p.add_argument("--days", type=float)
    p.add_argument("--dry-run", action="store_true")
    for name in ("start", "stop", "restart", "status", "doctor", "on"):
        sub.add_parser(name)
    p = sub.add_parser("off")
    p.add_argument("--hard", action="store_true", help="raw passthrough instead of freezing stored rewrites")
    p = sub.add_parser("shadow")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("stats")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--session")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("integrate")
    p.add_argument("action", choices=["on", "off", "status", "check"])
    p = sub.add_parser("skills")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p = sub.add_parser("plugins")
    p.add_argument("action", choices=["install", "uninstall"])
    p.add_argument("--agent", choices=["gemini", "codex"], required=True, help="whose plugin system to use")
    p = sub.add_parser("hook-mode")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--agent", choices=["claude", "opencode"], default="claude", help="whose hook system to use")
    p = sub.add_parser("status-line")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p = sub.add_parser("dashboard")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--no-open", action="store_true", help="write the page without opening a browser")
    p = sub.add_parser("audit")
    p.add_argument("--days", type=float, default=30)
    p = sub.add_parser("why")
    p.add_argument("session", nargs="?", help="session id or prefix (default: the most recent)")
    p.add_argument("--full", action="store_true", help="the whole summary and every rewrite")
    p = sub.add_parser("events")
    p.add_argument("-n", type=int, default=30)
    p = sub.add_parser("config")
    p.add_argument("--path", action="store_true")
    p.add_argument("action", nargs="?", choices=["set", "get"])
    p.add_argument("key", nargs="?", help="e.g. integrations.vault_path")
    p.add_argument("value", nargs="?")
    p = sub.add_parser("handoff")
    p.add_argument("action", choices=["install", "uninstall", "hook", "worker", "status"])
    p.add_argument("--transcript")
    p.add_argument("--log", help="a condensed log from the proxy's idle handoff")
    p.add_argument("--cwd")
    p.add_argument("--session", default="")
    p = sub.add_parser("eval")
    p.add_argument("action", choices=["run", "report", "check"])
    p.add_argument("target", nargs="?")
    p.add_argument("--runs", type=int)
    p.add_argument("--arms", help="comma-separated: baseline,proxy")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .server import serve

        serve(args.port)
        return 0

    if args.command in ("start", "restart", "stop", "status"):
        from .launch import running, start_daemon, stop_daemon

        port = _port()
        if args.command in ("stop", "restart"):
            stopped = stop_daemon(port)
            print("proxy stopped" if stopped else "proxy was not running")
        if args.command in ("start", "restart"):
            info = start_daemon(port)
            print(f"proxy running on 127.0.0.1:{port} (pid {info.get('pid')}, version {info.get('version')})")
        if args.command == "status":
            info = running(port)
            print(
                f"proxy running on 127.0.0.1:{port} (pid {info.get('pid')}, version {info.get('version')})"
                if info
                else "proxy not running"
            )
            return 0 if info else 1
        return 0

    if args.command == "doctor":
        from .reports import doctor

        print(doctor(os.getcwd()))
        return 0

    if args.command == "report":
        from .reports import report

        _config()
        store = _store()
        try:
            print(report(store, days=args.days))
        finally:
            store.close()
        print(
            "\nThis summary contains counts and percentages only (no prompts, paths or code); "
            "paste it into an issue as is.",
            file=sys.stderr,
        )
        return 0

    if args.command == "estimate":
        from . import estimate

        cfg = _config()
        if args.agent == "codex":
            from . import codexlog

            root = Path(args.path) if args.path else codexlog.default_root()
            since = time.time() - args.days * 86400
            files = [root] if root.is_file() else sorted(root.rglob("rollout-*.jsonl")) if root.exists() else []
            files = [f for f in files if f.stat().st_mtime >= since or root.is_file()]
            print(codexlog.render(codexlog.estimate(files, cfg)))
            return 0
        if args.agent == "gemini":
            from . import geminilog

            root = Path(args.path) if args.path else geminilog.default_root()
            since = time.time() - args.days * 86400
            files = [f for f in geminilog.session_files(root) if root.is_file() or f.stat().st_mtime >= since]
            print(geminilog.render(geminilog.estimate(files, cfg)))
            return 0
        if args.agent == "opencode":
            from . import opencodelog

            db = Path(args.path) if args.path else opencodelog.default_db()
            print(opencodelog.render(opencodelog.estimate(db, time.time() - args.days * 86400, cfg)))
            return 0
        if args.subagents:
            from . import subagents

            print(subagents.render(*subagents.measure(estimate.transcripts(args.path, args.days, args.project))))
            return 0
        if args.cache:
            from . import cachesim

            files = estimate.transcripts(args.path, args.days, args.project)
            print(cachesim.render(cachesim.simulate(cachesim.read_conversations(files))))
            return 0
        print("Replaying your Claude Code transcripts offline (no API calls)…", file=sys.stderr)
        results = estimate.run(
            args.path,
            days=args.days,
            project=args.project,
            config=cfg,  # your settings, so a change can be measured before it goes live
            progress=lambda msg: print(f"\r{msg[:100]:<100}", end="", file=sys.stderr),
        )
        print("\r" + " " * 100 + "\r", end="", file=sys.stderr)
        print(estimate.render(results))
        if args.pruning:
            ceiling = estimate.prune_ceiling(
                args.path,
                days=args.days,
                project=args.project,
                progress=lambda msg: print(f"\r{msg[:100]:<100}", end="", file=sys.stderr),
            )
            print("\r" + " " * 100 + "\r", end="", file=sys.stderr)
            print()
            print(estimate.render_pruning(ceiling))
        return 0

    if args.command == "audit":
        from .audit import audit

        _config()
        store = _store()
        try:
            print(audit(store, args.days))
        finally:
            store.close()
        return 0

    if args.command == "integrate":
        from . import integrate

        port = _port()
        if args.action == "check":  # runs as a Claude Code hook: print nothing unless something is wrong
            out = integrate.check(port)
            if out:
                print(out)
            return 0
        if args.action == "on":
            try:
                path = integrate.on(port)
            except integrate.IntegrateError as exc:
                print(f"lightfoot: {exc}", file=sys.stderr)
                return 1
            print(f"Every Claude Code session now goes through lightfoot, IDE and desktop app included ({path}).")
            print("Restart open Claude Code windows to pick it up. `lightfoot integrate off` undoes it.")
        elif args.action == "off":
            done = integrate.off(port)
            print("integration removed; restart open Claude Code windows" if done else "integration was not on")
        else:
            print("on" if integrate.is_on(port) else "off")
        return 0

    if args.command == "plugins":
        from . import agentplugins

        try:
            done = agentplugins.install(args.agent) if args.action == "install" else agentplugins.uninstall(args.agent)
        except RuntimeError as exc:
            print(f"lightfoot: {exc}", file=sys.stderr)
            return 1
        print(f"{done}." if args.action == "uninstall" else f"installed: {done}.")
        return 0

    if args.command == "skills":
        from . import agentskills

        if args.action == "install":
            try:
                done = agentskills.install()
            except RuntimeError as exc:
                print(f"lightfoot: {exc}", file=sys.stderr)
                return 1
            print(f"skills installed in {agentskills.skills_root()}: {', '.join(done)}.")
            print("Gemini CLI, opencode and Codex CLI load them from there; Claude Code uses the plugin.")
        elif args.action == "uninstall":
            removed = agentskills.uninstall()
            print(f"removed: {', '.join(removed)}" if removed else "no Lightfoot skills were installed")
        else:
            have = agentskills.installed()
            print(f"installed: {', '.join(have)}" if have else "not installed")
        return 0

    if args.command == "hook-mode":
        if args.agent == "opencode":
            from . import opencodehook as hooks

            done = "Shell output is now condensed in every opencode session, proxy or not."
        else:
            from . import hookmode as hooks

            done = "Bash, PowerShell and MCP output is now condensed in every Claude Code session, proxy or not."
        if args.action == "install":
            try:
                print(f"hook mode installed ({hooks.install()}).")
            except RuntimeError as exc:
                print(f"lightfoot: {exc}", file=sys.stderr)
                return 1
            print(done)
        elif args.action == "uninstall":
            print("hook mode removed" if hooks.uninstall() else "hook mode was not installed")
        else:
            print("installed" if hooks.is_installed() else "not installed")
        return 0

    if args.command == "status-line":
        from . import statusline

        if args.action == "install":
            print(f"lightfoot segment added to the Claude Code status line ({statusline.install()}).")
            print("It appears in Claude Code sessions started with `lightfoot claude`.")
        elif args.action == "uninstall":
            done = statusline.uninstall()
            print("status line restored" if done else "the lightfoot status line was not installed")
        else:
            print("installed" if statusline.is_installed() else "not installed")
        return 0

    if args.command == "dashboard":
        import webbrowser

        from .dashboard import write

        cfg = _config()
        store = _store()
        try:
            path = write(store, days=args.days, subscription=cfg.get("billing") == "subscription")
        finally:
            store.close()
        print(f"report written to {path}")
        if not args.no_open:
            webbrowser.open(path.as_uri())
        return 0

    if args.command == "why":
        from .why import why

        cfg = _config()
        store = _store()
        try:
            print(why(store, args.session, full=args.full, subscription=cfg.get("billing") == "subscription"))
        finally:
            store.close()
        return 0

    if args.command == "cleanup":
        from .maintenance import cleanup

        store = _store()
        try:
            counts = cleanup(store, _config(), days=args.days, dry_run=args.dry_run)
        finally:
            store.close()
        verb = "would delete" if args.dry_run else "deleted"
        print(
            f"{verb}: {counts['rewrites']} stored rewrites, {counts['prunes']} prune records, "
            f"{counts['spill_files']} spill/history files "
            f"({counts['spill_bytes'] / 1e6:.1f} MB), {counts['sessions']} session rows, {counts['events']} events, "
            f"{counts['ledger_rows']} ledger rows past ledger retention"
        )
        return 0

    if args.command in ("on", "off"):
        store = _store()
        value = "on" if args.command == "on" else ("hard" if args.hard else "off")
        store.set_state("switch", value)
        store.close()
        print(
            {
                "on": "lightfoot is on: new rewrites resume.",
                "off": "lightfoot is off: no new rewrites; stored ones are still replayed so running sessions stay "
                "consistent (`lightfoot off --hard` for raw passthrough).",
                "hard": "lightfoot is in raw passthrough. Running sessions that already had rewrites will see one full "
                "cache rewrite, and on accounts that enforce the thinking-prefix check they may fail; "
                "start new sessions.",
            }[value]
        )
        return 0

    if args.command == "shadow":
        store = _store()
        store.set_state("mode_override", "shadow" if args.state == "on" else "live")
        store.close()
        print(
            f"New `lightfoot claude` launches will run in {'shadow' if args.state == 'on' else 'live'} mode. "
            f"Sessions already running keep their mode."
        )
        return 0

    if args.command == "stats":
        from .reports import stats

        _config()  # installs [prices] overrides
        store = _store()
        try:
            print(stats(store, days=args.days, session=args.session, limit=args.limit))
        finally:
            store.close()
        return 0

    if args.command == "events":
        from datetime import datetime

        store = _store()
        try:
            rows = store.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.n,))
        finally:
            store.close()
        for r in reversed(rows):
            when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
            print(f"{when} {r['level']:<5} {r['kind']:<20} {(r['session_id'] or '')[:10]:<10} {r['detail'][:300]}")
        if not rows:
            print("no events")
        return 0

    if args.command == "config":
        from .config import ensure_global_config, get_value, global_config_path, set_value

        if args.action == "set":
            if not args.key or args.value is None:
                print("usage: lightfoot config set KEY VALUE", file=sys.stderr)
                return 2
            print(f"{args.key} set in {set_value(args.key, args.value)}")
            return 0
        if args.action == "get":
            if not args.key:
                print("usage: lightfoot config get KEY", file=sys.stderr)
                return 2
            print(get_value(args.key, os.getcwd()))
            return 0
        path = ensure_global_config()
        if args.path:
            print(global_config_path())
        else:
            print(f"# {path}\n")
            print(path.read_text(encoding="utf-8"))
        return 0

    if args.command == "handoff":
        from . import handoff

        if args.action == "install":
            print(
                f"SessionEnd handoff hook installed in {handoff.install()} (a backup was saved next to it). "
                f"It only writes notes when an Obsidian vault is configured and detected."
            )
        elif args.action == "uninstall":
            print("hook removed" if handoff.uninstall() else "hook was not installed")
        elif args.action == "status":
            print(handoff.hook_status())
        elif args.action == "hook":
            return handoff.run_hook(sys.stdin.read())
        elif args.action == "worker":
            return handoff.run_worker(args.transcript, args.cwd or os.getcwd(), args.session, log=args.log)
        return 0

    if args.command == "eval":
        from . import evaluate

        try:
            if args.action == "run":
                if not args.target:
                    print("usage: lightfoot eval run TASKS.toml", file=sys.stderr)
                    return 2
                arms = [a.strip() for a in args.arms.split(",")] if args.arms else None
                path = evaluate.run(args.target, runs=args.runs, arms=arms)
                print(f"\nresults: {path}\n")
                print(evaluate.report(str(path)))
            elif args.action == "check":
                if not args.target:
                    print("usage: lightfoot eval check TASKS.toml", file=sys.stderr)
                    return 2
                result = evaluate.check(args.target)
                print(result)
                return 1 if any(line.startswith("XX") for line in result.splitlines()) else 0
            else:
                print(evaluate.report(args.target))
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"lightfoot eval: {exc}", file=sys.stderr)
            return 1
        return 0
    return 2
