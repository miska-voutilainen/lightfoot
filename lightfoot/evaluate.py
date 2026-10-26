"""Paired eval: the same tasks with and without the proxy, scored by each task's own test.

Each run gets a fresh git worktree at the task's commit and its own launch id, so its
requests are isolated in the ledger. The baseline arm goes through the proxy in
passthrough mode (nothing rewritten, usage still recorded) so both arms are measured
the same way.

Pass rate alone can hide what compression costs: an agent that lost context may still
pass, after many more reads and searches to get it back. So each Claude Code run also
counts its tool calls from its transcript, split into retrieval (reads, searches) and
execution (everything else), and the report compares both arms on them.
"""

from __future__ import annotations

import json
import random
import shutil
import statistics
import subprocess
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from . import pricing
from .config import ensure_global_config, lightfoot_home, load_config, private_dir
from .handoff import GUARD_ENV
from .launch import (
    claude_env,
    codex_provider_args,
    find_claude,
    find_codex,
    launch_settings_file,
    local_env,
    register_launch,
    start_daemon,
)
from .pricing import row_costs
from .profiles import CLAUDE_CODE
from .store import Store

ARMS = {"baseline": "passthrough", "proxy": "live"}
RETRIEVAL_KINDS = {"read", "search", "graph_query"}
BOOTSTRAP_SAMPLES = 2000


def eval_dir() -> Path:
    """Where eval results are written, `~/.lightfoot/eval/`."""
    path = lightfoot_home() / "eval"
    private_dir(path)
    return path


AGENTS = ("claude", "codex")


def agent_of(spec: dict, task: dict) -> str:
    """The agent a task runs with: the task's own, else the file's, else Claude Code."""
    return str(task.get("agent") or spec.get("agent") or "claude")


def load_tasks(path: Path) -> dict:
    """The task file, with `{task_dir}` in setup and test commands replaced by the file's folder."""
    spec = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    tasks = spec.get("task") or []
    if not tasks:
        raise ValueError("the task file has no [[task]] entries")
    task_dir = str(path.resolve().parent)
    for t in tasks:
        missing = [k for k in ("id", "prompt", "test") if not t.get(k)]
        if not (t.get("repo") or t.get("repo_url")):
            missing.append("repo or repo_url")
        if missing:
            raise ValueError(f"task {t.get('id', '?')}: missing {', '.join(missing)}")
        if t.get("repo_url") and not t.get("commit"):
            raise ValueError(f"task {t['id']}: a repo_url task needs a pinned commit")
        for key in ("setup", "test", "prompt"):
            if isinstance(t.get(key), str):
                t[key] = t[key].replace("{task_dir}", task_dir)
        agent = agent_of(spec, t)
        if agent not in AGENTS:
            raise ValueError(f"task {t['id']}: unknown agent {agent!r}; use one of {', '.join(AGENTS)}")
        if agent == "claude" and not spec.get("permission_mode"):
            raise ValueError(
                "set permission_mode in the task file (headless runs cannot answer permission prompts; "
                'for example "acceptEdits", or "bypassPermissions" if you accept the risk in a worktree)'
            )
        if agent == "codex" and not (t.get("codex_args") or spec.get("codex_args")):
            raise ValueError(
                "set codex_args for Codex tasks (headless runs cannot answer approvals; for example "
                '["--full-auto"], which allows edits inside the throwaway worktree)'
            )
    return spec


def find_transcript(session_id: str, root: Path | None = None) -> Path | None:
    """The Claude Code transcript of a session, wherever its project folder is."""
    if not session_id:
        return None
    root = root or Path.home() / ".claude" / "projects"
    return next(iter(root.glob(f"*/{session_id}.jsonl")), None)


def tool_calls(transcript: Path) -> dict:
    """Tool calls in a Claude Code transcript: retrieval, execution, and reads of lightfoot's files."""
    counts = {"retrieval_calls": 0, "execution_calls": 0, "condensed_reads": 0}
    seen: set[str] = set()
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("id") in seen:
                    continue
                seen.add(block.get("id"))
                name = str(block.get("name") or "")
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                kind = CLAUDE_CODE.kind(name, inp)
                counts["retrieval_calls" if kind in RETRIEVAL_KINDS else "execution_calls"] += 1
                path = CLAUDE_CODE.path(name, inp) or ""
                if kind == "read" and "/.lightfoot/" in path.replace("\\", "/"):
                    counts["condensed_reads"] += 1
    return counts


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def repo_path(task: dict) -> str:
    """The local repository for a task: its `repo`, or a clone of `repo_url` kept in the eval folder."""
    if task.get("repo"):
        return str(Path(task["repo"]).resolve())
    url, commit = task["repo_url"], task["commit"]
    name = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    folder = private_dir(eval_dir() / "repos") / name
    if not (folder / ".git").exists():
        cloned = subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", url, str(folder)], capture_output=True, text=True
        )
        if cloned.returncode != 0:
            raise RuntimeError(f"git clone {url} failed: {cloned.stderr.strip()}")
    if _git(str(folder), "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        fetched = _git(str(folder), "fetch", "--quiet", "origin", commit)
        if fetched.returncode != 0:
            raise RuntimeError(f"commit {commit} not found in {url}: {fetched.stderr.strip()}")
    return str(folder)


def _shell(command: str, cwd: Path, minutes: float) -> tuple[bool, str]:
    """(succeeded, output tail) for a setup or test command the user wrote in their own task file."""
    try:
        proc = subprocess.run(  # noqa: S602  # nosec B602
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=minutes * 60
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]


def _worktree(task: dict) -> tuple[str, Path, str | None]:
    """(repo, fresh worktree at the task's commit, error)."""
    repo = repo_path(task)
    worktree = Path(tempfile.mkdtemp(prefix=f"lightfoot-eval-{task['id']}-"))
    added = _git(repo, "worktree", "add", "--detach", str(worktree), task.get("commit") or "HEAD")
    if added.returncode != 0:
        shutil.rmtree(worktree, ignore_errors=True)
        return repo, worktree, f"git worktree add failed: {added.stderr.strip()}"
    return repo, worktree, None


def _remove_worktree(repo: str, worktree: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(worktree))
    shutil.rmtree(worktree, ignore_errors=True)


def check(task_file: str) -> str:
    """Validate a task file without running any agent: each task's setup must succeed and its
    test must fail at the pinned commit, or a pass would prove nothing. No API calls."""
    spec = load_tasks(Path(task_file))
    lines = []
    for task in spec["task"]:
        try:
            repo, worktree, error = _worktree(task)
        except RuntimeError as exc:
            lines.append(f"XX {task['id']}: {exc}")
            continue
        try:
            if error:
                lines.append(f"XX {task['id']}: {error}")
                continue
            if task.get("setup"):
                ok, tail = _shell(task["setup"], worktree, float(task.get("setup_timeout_minutes", 15)))
                if not ok:
                    lines.append(f"XX {task['id']}: setup failed\n{tail}")
                    continue
            ok, tail = _shell(task["test"], worktree, float(task.get("test_timeout_minutes", 10)))
            if ok:
                lines.append(f"XX {task['id']}: the test already passes before any change; the task proves nothing")
            else:
                last = [line for line in tail.strip().splitlines() if line.strip()][-1:] or [""]
                lines.append(f"ok {task['id']}: setup works and the test fails as it should ({last[0][:120]})")
        finally:
            _remove_worktree(repo, worktree)
    return "\n".join(lines)


def run_one(store: Store, spec: dict, task: dict, arm: str, run_index: int, run_id: str, port: int) -> dict:
    """Run one task once in one arm: a fresh git worktree, its setup, the agent headless, then the task's test."""
    record = {"run_id": run_id, "task": task["id"], "arm": arm, "run": run_index, "ts": time.time()}
    try:
        repo, worktree, error = _worktree(task)
    except RuntimeError as exc:
        record.update(error=str(exc), passed=False)
        return record
    if error:
        record.update(error=error, passed=False)
        return record
    settings_file = None
    try:
        if task.get("setup"):
            ok, tail = _shell(task["setup"], worktree, float(task.get("setup_timeout_minutes", 15)))
            if not ok:
                record.update(error="setup failed", setup_tail=tail, passed=False)
                return record
        agent = agent_of(spec, task)
        record["agent"] = agent
        label = f"eval:{run_id}:{task['id']}:{arm}:{run_index}"
        # Codex: both arms use the same custom provider; only the proxy arm rewrites (live, experimental).
        launch, _ = register_launch(
            store, str(worktree), ARMS[arm], port, label=label, agent=agent, experimental=agent == "codex"
        )
        model = task.get("model") or spec.get("model")
        if agent == "codex":
            env = local_env()
            cmd = (
                [find_codex(), *codex_provider_args(port, launch)]
                + (["-m", model] if model else [])
                + ["exec", *(task.get("codex_args") or spec.get("codex_args")), task["prompt"]]
            )
            stdin = None
        else:
            env = claude_env(port, launch)
            # --settings: a gateway in the user's Claude Code settings must not route the run past its launch.
            settings_file = launch_settings_file(port, launch)
            cmd = [find_claude(), "--settings", str(settings_file), "-p", "--output-format", "json"]
            cmd += ["--permission-mode", spec["permission_mode"]]
            cmd += ["--model", model] if model else []
            stdin = task["prompt"]
        env[GUARD_ENV] = "1"  # no handoff notes for eval sessions
        timeout = float(task.get("timeout_minutes", spec.get("timeout_minutes", 30))) * 60
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=stdin,
                cwd=worktree,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                stdin=subprocess.DEVNULL if stdin is None else None,
            )
            output = proc.stdout
            record["agent_rc"] = proc.returncode
        except subprocess.TimeoutExpired:
            output = ""
            record["agent_rc"] = "timeout"
        record["seconds"] = round(time.time() - started, 1)
        result = {}
        if agent == "claude":
            try:
                result = json.loads(output) if output.strip() else {}
            except ValueError:
                result = {}
        record["claude_cost_usd"] = result.get("total_cost_usd")
        transcript = find_transcript(str(result.get("session_id") or "")) if agent == "claude" else None
        if transcript is not None:
            record.update(tool_calls(transcript))
        record["passed"], record["test_tail"] = _shell(
            task["test"], worktree, float(task.get("test_timeout_minutes", 10))
        )
        rows = store.query("SELECT * FROM ledger WHERE launch_id=? AND status=200", (launch.id,))
        record["requests"] = len(rows)
        record["turns"] = result.get("num_turns") or len(rows)  # model requests when the agent doesn't report turns
        record["cost"] = round(sum(row_costs(r)[0] for r in rows), 6)
        record["output_tokens"] = sum(r["output"] for r in rows)
        record["saved_tokens"] = sum(r["saved_tokens"] for r in rows)
    finally:
        _remove_worktree(repo, worktree)
        if settings_file is not None:
            settings_file.unlink(missing_ok=True)
    return record


def run(task_file: str, runs: int | None = None, arms: list[str] | None = None) -> Path:
    """Run the paired eval: every task, `runs` times per arm, interleaved. Returns the results file."""
    spec = load_tasks(Path(task_file))
    arms = arms or list(ARMS)
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm(s): {', '.join(unknown)}; use {', '.join(ARMS)}")
    runs = runs or int(spec.get("runs", 3))
    ensure_global_config()
    cfg = load_config()
    pricing.configure(cfg.get("prices"))
    port = int(cfg["port"])
    start_daemon(port)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out_path = eval_dir() / f"{run_id}.jsonl"
    store = Store()
    try:
        total = runs * len(spec["task"]) * len(arms)
        done = 0
        # Interleave arms so slow drift (load, model updates) affects both equally.
        for i in range(runs):
            for task in spec["task"]:
                for arm in arms:
                    done += 1
                    print(f"[{done}/{total}] {task['id']} · {arm} · run {i + 1}", flush=True)
                    record = run_one(store, spec, task, arm, i, run_id, port)
                    with open(out_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record) + "\n")
                    status = "pass" if record.get("passed") else "FAIL"
                    print(
                        f"        {status} · cost {record.get('cost', 0):.4f} · turns {record.get('turns')}"
                        + (f" · {record['error']}" if record.get("error") else ""),
                        flush=True,
                    )
    finally:
        store.close()
    return out_path


def paired_ci(pairs: list[tuple[float, float]], stat, seed: int = 0) -> tuple[float, float, float]:
    """(estimate, low, high): `stat` over paired runs, with a 95% bootstrap interval.

    Pairs are (baseline, proxy) values of the same task and run index, resampled together,
    so task difficulty cancels out. A fixed seed keeps a report reproducible.
    """
    point = stat(pairs)
    # Resampling for statistics, seeded so a report is reproducible; nothing secret depends on it.
    rng = random.Random(seed)  # noqa: S311  # nosec B311
    samples = sorted(stat([rng.choice(pairs) for _ in pairs]) for _ in range(BOOTSTRAP_SAMPLES))
    return point, samples[int(0.025 * len(samples))], samples[int(0.975 * len(samples)) - 1]


def _relative_change(pairs: list[tuple[float, float]]) -> float:
    base = sum(b for b, _ in pairs)
    return 100 * (sum(p for _, p in pairs) - base) / base if base else float("nan")


def _point_change(pairs: list[tuple[float, float]]) -> float:
    return 100 * sum(p - b for b, p in pairs) / len(pairs)


def report(path: str | None = None) -> str:
    """The report for a results file (by path or run id), or for the latest run."""
    if path is None:
        files = sorted(eval_dir().glob("*.jsonl"))
        if not files:
            return "No eval runs yet (lightfoot eval run tasks.toml)."
        file = files[-1]
    else:
        file = Path(path)
        if not file.exists():
            file = eval_dir() / f"{path}.jsonl"
    records = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_arm: dict[str, list[dict]] = {}
    for r in records:
        by_arm.setdefault(r["arm"], []).append(r)

    def med(values):
        values = [v for v in values if isinstance(v, (int, float))]
        return statistics.median(values) if values else float("nan")

    lines = [
        f"Eval {file.stem} · {len(records)} runs",
        "",
        f"{'Arm':<10} {'Runs':>5} {'Pass rate':>10} {'Median cost':>12} {'Median turns':>13} {'Median output':>14} "
        f"{'Retrieval':>10} {'Execution':>10}",
    ]
    summary = {}
    for arm, rs in by_arm.items():
        rate = 100 * sum(1 for r in rs if r.get("passed")) / len(rs)
        summary[arm] = (rate, med([r.get("cost") for r in rs]))
        lines.append(
            f"{arm:<10} {len(rs):>5} {rate:>9.1f}% {med([r.get('cost') for r in rs]):>12.4f} "
            f"{med([r.get('turns') for r in rs]):>13} {med([r.get('output_tokens') for r in rs]):>14} "
            f"{med([r.get('retrieval_calls') for r in rs]):>10} {med([r.get('execution_calls') for r in rs]):>10}"
        )
    if "baseline" in summary and "proxy" in summary:
        (b_rate, b_cost), (p_rate, p_cost) = summary["baseline"], summary["proxy"]
        saving = 100 * (b_cost - p_cost) / b_cost if b_cost else float("nan")
        quality = p_rate >= b_rate - 2.0
        cheaper = p_cost < b_cost
        lines += [
            "",
            f"Median cost change: {-saving:+.1f}% (target: -20% or better)",
            f"Pass rate change: {p_rate - b_rate:+.1f} points (gate: no worse than -2.0)",
            f"Ship gate: {'PASS' if quality and cheaper else 'FAIL'}"
            + ("" if saving >= 20 or not (quality and cheaper) else " (cheaper, but short of the 20% goal)"),
        ]
        lines += _paired_lines(by_arm["baseline"], by_arm["proxy"])
    lines += ["", f"{'Task':<24} " + " ".join(f"{a:>18}" for a in by_arm)]
    for task in sorted({r["task"] for r in records}):
        cells = []
        for arm in by_arm:
            rs = [r for r in by_arm[arm] if r["task"] == task]
            passed = sum(1 for r in rs if r.get("passed"))
            cells.append(f"{passed}/{len(rs)} {med([r.get('cost') for r in rs]):>9.4f}")
        lines.append(f"{task[:24]:<24} " + " ".join(f"{c:>18}" for c in cells))
    return "\n".join(lines)


def _paired_lines(baseline: list[dict], proxy: list[dict]) -> list[str]:
    """Paired changes with 95% intervals, matching runs by task and run index."""
    base = {(r["task"], r.get("run")): r for r in baseline if r.get("run") is not None}
    matched = [(base[(r["task"], r.get("run"))], r) for r in proxy if (r["task"], r.get("run")) in base]
    if len(matched) < 2:
        return []

    def pairs(key):
        out = [(b.get(key), p.get(key)) for b, p in matched]
        return [(float(b), float(p)) for b, p in out if isinstance(b, (int, float)) and isinstance(p, (int, float))]

    lines = ["", f"Paired over {len(matched)} runs (95% bootstrap intervals):"]
    rows = (
        ("Cost", pairs("cost"), _relative_change, "%"),
        ("Retrieval calls", pairs("retrieval_calls"), _relative_change, "%"),
        ("Turns", pairs("turns"), _relative_change, "%"),
        (
            "Pass rate",
            [(float(bool(b.get("passed"))), float(bool(p.get("passed")))) for b, p in matched],
            _point_change,
            " points",
        ),
    )
    for label, values, stat, unit in rows:
        if len(values) < 2:
            continue
        point, low, high = paired_ci(values, stat)
        lines.append(f"  {label:<16} {point:+7.1f}{unit}  [{low:+.1f}, {high:+.1f}]")
    retrieval = pairs("retrieval_calls")
    if len(retrieval) >= 2:
        point, _, _ = paired_ci(retrieval, _relative_change)
        lines.append(
            "  Retrieval within +10%: "
            + ("yes" if point <= 10 else "NO, the proxy arm went back for context more often")
        )
    condensed = sum(int(p.get("condensed_reads") or 0) for _, p in matched)
    if condensed:
        lines.append(f"  Reads of lightfoot's saved files (proxy arm): {condensed}")
    return lines
