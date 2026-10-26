import os
import subprocess
import sys

import pytest

from lightfoot import launch


def test_watchdog_restarts_a_dead_proxy(monkeypatch):
    calls = {"health": 0, "start": 0}

    def fake_health(port, timeout=0.5):
        calls["health"] += 1
        return None  # the proxy is down

    def fake_start(port, wait=10.0):
        calls["start"] += 1
        return {"version": "x"}

    monkeypatch.setattr(launch, "WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(launch, "health", fake_health)
    monkeypatch.setattr(launch, "start_daemon", fake_start)
    code = launch.run_child([sys.executable, "-c", "import time; time.sleep(0.4)"], dict(os.environ), port=8787)
    assert code == 0
    assert calls["start"] >= 1


def test_without_port_there_is_no_watchdog(monkeypatch):
    monkeypatch.setattr(launch, "health", lambda *a, **k: (_ for _ in ()).throw(AssertionError("checked")))
    assert launch.run_child([sys.executable, "-c", "pass"], dict(os.environ)) == 0


def test_child_exit_code_is_returned():
    assert launch.run_child([sys.executable, "-c", "raise SystemExit(3)"], dict(os.environ)) == 3


def test_openai_upstream_strips_v1(monkeypatch, cfg):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/openai/v1/")
    assert launch.resolve_openai_upstream(8787, cfg) == "https://gateway.example/openai"
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8787/l/abc/v1")  # ours: ignored
    assert launch.resolve_openai_upstream(8787, cfg) == "https://api.openai.com"
    monkeypatch.delenv("OPENAI_BASE_URL")
    assert launch.resolve_openai_upstream(8787, cfg) == "https://api.openai.com"


def test_codex_live_requires_experimental(monkeypatch):
    monkeypatch.setattr(launch, "find_codex", lambda: "codex")
    try:
        launch.run_codex([], mode="live", experimental=False)
    except RuntimeError as exc:
        assert "--live --experimental" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")


def test_cli_version_and_help():
    out = subprocess.run([sys.executable, "-m", "lightfoot", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("lightfoot ")
    out = subprocess.run([sys.executable, "-m", "lightfoot", "--help"], capture_output=True, text=True)
    assert "lightfoot codex" in out.stdout and "lightfoot cleanup" in out.stdout


def test_run_routes_every_protocol_through_its_own_launch(tmp_path, monkeypatch, capsys):
    from lightfoot import __version__
    from lightfoot.store import Store

    started = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")  # the user's gateway stays the upstream
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(launch, "start_daemon", lambda port: {"version": __version__})
    monkeypatch.setattr(launch, "run_child", lambda cmd, env, port=None: started.update(cmd=cmd, env=env) or 0)
    assert launch.run_any(["aider", "--yes"]) == 0
    env = started["env"]
    assert started["cmd"] == ["/bin/aider", "--yes"]
    assert env["OPENAI_BASE_URL"] == env["OPENAI_API_BASE"] and env["OPENAI_BASE_URL"].endswith("/v1")
    ids = {
        name: env[var].split("/l/")[1].split("/")[0]
        for name, var in (
            ("anthropic", "ANTHROPIC_BASE_URL"),
            ("openai", "OPENAI_BASE_URL"),
            ("gemini", "GOOGLE_GEMINI_BASE_URL"),
        )
    }
    assert len(set(ids.values())) == 3
    store = Store()
    try:
        launches = {name: store.get_launch(i) for name, i in ids.items()}
    finally:
        store.close()
    assert {lt.mode for lt in launches.values()} == {"shadow"} and {lt.agent for lt in launches.values()} == {"run"}
    assert launches["openai"].upstream == "https://gateway.example"
    assert launches["gemini"].upstream == "https://generativelanguage.googleapis.com"


def test_run_needs_a_command_and_experimental_for_live(monkeypatch):
    with pytest.raises(RuntimeError, match="usage"):
        launch.run_any([])
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="on PATH"):
        launch.run_any(["nothing"])
    monkeypatch.setattr(launch.shutil, "which", lambda name: name)
    with pytest.raises(RuntimeError, match="--live --experimental"):
        launch.run_any(["aider"], mode="live")


def test_aider_is_run_with_leading_flags_only(monkeypatch):
    from lightfoot import cli

    got = {}
    monkeypatch.setattr(
        launch,
        "run_any",
        lambda cmd, mode, experimental=False, capture=False: (
            got.update(cmd=cmd, mode=mode, experimental=experimental) or 0
        ),
    )
    assert cli.main(["aider", "--live", "--experimental", "--model", "x", "--live"]) == 0
    assert got == {"cmd": ["aider", "--model", "x", "--live"], "mode": "live", "experimental": True}


def test_cache_ttl_reaches_claude_code_through_the_launch_settings(tmp_path):
    import json

    from lightfoot.config import set_value
    from lightfoot.store import Launch

    lt = Launch("ttl1", str(tmp_path), "shadow", None, None, None)
    env = json.loads(launch.launch_settings_file(8787, lt).read_text(encoding="utf-8"))["env"]
    assert "CLAUDE_CODE_PROMPT_CACHE_TTL" not in env  # Claude Code's own choice by default
    set_value("cache.ttl", "1h")
    env = json.loads(launch.launch_settings_file(8787, lt).read_text(encoding="utf-8"))["env"]
    assert env == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787/l/ttl1", "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}


def test_doctor_suggests_the_cache_check_on_the_five_minute_cache(tmp_path):
    import time

    from lightfoot.reports import doctor
    from lightfoot.store import Store

    store = Store()
    try:
        row = {"ts": time.time(), "status": 200, "cw5": 50_000, "cw1h": 0, "agent": "claude-code", "model": "m"}
        store.add_ledger(row)
    finally:
        store.close()
    assert "`lightfoot estimate --cache`" in doctor(str(tmp_path))
