"""The `lightfoot` command line, run in-process against a throwaway data folder."""

import json
import socket
import time

import pytest

from lightfoot import cli, launch
from lightfoot.store import Store


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def ledger(lightfoot_home):
    store = Store()
    now = time.time()
    for i in range(30):
        store.add_ledger(
            {
                "ts": now - 60 * i,
                "session_id": "sess-a" if i < 20 else "sess-b",
                "mode": "live" if i < 20 else "shadow",
                "model": "claude-opus-5-5",
                "status": 200,
                "turn": i,
                "input": 5,
                "cw5": 0,
                "cw1h": 2000,
                "cr": 150_000,
                "output": 300,
                "saved_tokens": 40_000,
                "rules": {"prune": 1} if i == 5 else {"noise": 1},
                "agent": "claude-code",
                "client_version": "claude-cli/2.1.191",
            }
        )
    store.get_or_create_session("sess-a", None, "live", now)
    store.set_context("sess-a", 180_000)
    store.close()


def test_version_and_help(capsys):
    assert run(capsys, "--version")[1].startswith("lightfoot ")
    code, out, _ = run(capsys, "--help")
    assert code == 0 and "lightfoot estimate" in out and "lightfoot cleanup" in out


def test_config_show_set_get(capsys, lightfoot_home):
    code, out, _ = run(capsys, "config")
    assert code == 0 and "[prune]" in out
    assert run(capsys, "config", "set", "prune.keep_tokens", "120000")[0] == 0
    assert run(capsys, "config", "get", "prune.keep_tokens")[1].strip() == "120000"
    code, _, err = run(capsys, "config", "set", "prune.keep_tokens", "many")
    assert code == 1 and "config error" in err
    assert str(lightfoot_home) in run(capsys, "config", "--path")[1]


def test_switches(capsys):
    for argv, expected in ((("shadow", "off"), "live"), (("shadow", "on"), "shadow")):
        assert run(capsys, *argv)[0] == 0
        store = Store()
        assert store.get_state("mode_override") == expected
        store.close()
    for argv, value in ((("off",), "off"), (("off", "--hard"), "hard"), (("on",), "on")):
        run(capsys, *argv)
        store = Store()
        assert store.get_state("switch") == value
        store.close()


def test_stats_report_events_cleanup(capsys, ledger):
    code, out, _ = run(capsys, "stats")
    assert code == 0 and "sess-a" in out and "Live sessions: saved" in out and "prune" in out
    code, out, _ = run(capsys, "stats", "--session", "sess-b")
    assert "Session sess-b" in out
    code, out, err = run(capsys, "report")
    assert code == 0 and "claude-code/live" in out
    assert "counts and percentages only" in err
    assert run(capsys, "events")[1].strip() == "no events"
    code, out, _ = run(capsys, "cleanup", "--dry-run")
    assert code == 0 and out.startswith("would delete")


def test_stats_with_no_data(capsys):
    assert "No completed requests" in run(capsys, "stats")[1]


def test_doctor_without_a_running_proxy(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "doctor")
    assert code == 0 and "proxy not running" in out and "old-turn pruning: on" in out


def test_estimate_on_a_transcript(capsys, tmp_path):
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 400,
        "cache_read_input_tokens": 30_000,
        "output_tokens": 50,
        "cache_creation": {"ephemeral_1h_input_tokens": 400, "ephemeral_5m_input_tokens": 0},
    }
    rows, parent = [], None
    for i in range(6):
        rows.append(
            {
                "type": "user",
                "uuid": f"u{i}",
                "parentUuid": parent,
                "timestamp": "2026-10-20T10:00:00Z",
                "message": {"role": "user", "content": f"step {i}"},
            }
        )
        rows.append(
            {
                "type": "assistant",
                "uuid": f"a{i}",
                "parentUuid": f"u{i}",
                "timestamp": "2026-10-20T10:00:05Z",
                "message": {
                    "id": f"m{i}",
                    "role": "assistant",
                    "model": "claude-opus-5-5",
                    "usage": usage,
                    "content": [{"type": "text", "text": f"done {i}"}],
                },
            }
        )
        parent = f"a{i}"
    folder = tmp_path / "projects" / "demo"
    folder.mkdir(parents=True)
    (folder / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    code, out, _ = run(capsys, "estimate", "--path", str(tmp_path / "projects"), "--days", "36500", "--pruning")
    assert code == 0 and "6 API calls" in out and "Old-turn pruning ceiling" in out


def test_handoff_status(capsys, monkeypatch, tmp_path):
    from lightfoot import handoff

    monkeypatch.setattr(handoff, "settings_path", lambda: tmp_path / "settings.json")
    assert "installed" in run(capsys, "handoff", "status")[1]


def test_eval_errors_are_reported(capsys, tmp_path):
    bad = tmp_path / "tasks.toml"
    bad.write_text('[[task]]\nid = "x"\nrepo = "."\nprompt = "p"\ntest = "t"\n', encoding="utf-8")
    code, _, err = run(capsys, "eval", "run", str(bad))
    assert code == 1 and "permission_mode" in err
    assert "No eval runs yet" in run(capsys, "eval", "report")[1]


def test_codex_requires_an_api_key(capsys, monkeypatch):
    monkeypatch.setattr(launch, "find_codex", lambda: "codex")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code, _, err = run(capsys, "codex")
    assert code == 1 and "OPENAI_API_KEY" in err


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_daemon_lifecycle(lightfoot_home):
    """The real background proxy: start, answer health checks, stop."""
    port = _free_port()
    info = launch.start_daemon(port, wait=20)
    try:
        assert info["ok"] and launch.health(port)["pid"] == info["pid"]
        assert launch.start_daemon(port)["pid"] == info["pid"]  # already running: reused
    finally:
        assert launch.stop_daemon(port)
    assert launch.health(port) is None


def test_a_second_daemon_on_the_same_port_leaves_the_pid_file_alone(lightfoot_home):
    """Two launches starting the proxy at once: the loser fails to bind and must not remove the winner's pid file."""
    import subprocess
    import sys

    port = _free_port()
    info = launch.start_daemon(port, wait=20)
    try:
        loser = subprocess.run(
            [sys.executable, "-m", "lightfoot", "serve", "--port", str(port)], capture_output=True, timeout=60
        )
        assert loser.returncode != 0
        assert (lightfoot_home / "proxy.pid").read_text(encoding="utf-8") == str(info["pid"])
        assert launch.running(port)["pid"] == info["pid"]
    finally:
        assert launch.stop_daemon(port)
    # The proxy stops answering before its process exits and removes the file.
    deadline = time.monotonic() + 10
    while (lightfoot_home / "proxy.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not (lightfoot_home / "proxy.pid").exists()
