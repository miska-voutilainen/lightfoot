import json
import re

import pytest

from lightfoot import integrate, launch
from lightfoot.store import Launch


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake user home, so ~/.claude/settings.json is a temporary file."""
    monkeypatch.setattr(launch.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(integrate, "settings_path", lambda: tmp_path / ".claude" / "settings.json")
    monkeypatch.setattr(integrate, "start_daemon", lambda port: {"pid": 1})
    return tmp_path


def _settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_on_sets_the_base_url_and_check_hooks_and_off_removes_them(home):
    (home / ".claude").mkdir()
    keep = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "mine"}]}]}, "model": "opus"}
    (home / ".claude" / "settings.json").write_text(json.dumps(keep), encoding="utf-8")
    integrate.on(8787)
    data = _settings(home)
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787" and integrate.is_on(8787)
    for event in integrate.EVENTS:
        assert integrate.MARK in data["hooks"][event][0]["hooks"][0]["command"]
    assert data["hooks"]["SessionStart"][1]["hooks"][0]["command"] == "mine"
    integrate.on(8787)  # idempotent: no duplicate hooks
    assert len(_settings(home)["hooks"]["SessionStart"]) == 2
    assert integrate.off(8787) is True
    assert _settings(home) == keep
    assert integrate.off(8787) is False


def test_on_refuses_to_replace_a_gateway(home):
    (home / ".claude").mkdir()
    gateway = {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.com"}}
    (home / ".claude" / "settings.json").write_text(json.dumps(gateway), encoding="utf-8")
    with pytest.raises(integrate.IntegrateError, match=re.escape("gateway.example.com")):
        integrate.on(8787)
    assert _settings(home) == gateway  # untouched


def test_check_is_silent_when_the_proxy_runs_and_warns_when_it_cannot_start(home, monkeypatch):
    monkeypatch.setattr(integrate, "running", lambda port: {"pid": 1})
    assert integrate.check(8787) == ""
    monkeypatch.setattr(integrate, "running", lambda port: None)
    assert integrate.check(8787) == ""  # started (start_daemon is faked to succeed)

    def broken(port):
        raise RuntimeError("port in use")

    monkeypatch.setattr(integrate, "start_daemon", broken)
    warning = json.loads(integrate.check(8787))
    assert "could not be started (port in use)" in warning["systemMessage"]


def test_a_gateway_in_claude_settings_becomes_the_upstream(home, tmp_path, monkeypatch, cfg):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://user-gw.example"}}), encoding="utf-8"
    )
    assert launch.resolve_upstream(8787, cfg, str(project)) == "https://user-gw.example"
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://local-gw.example/"}}), encoding="utf-8"
    )
    assert launch.resolve_upstream(8787, cfg, str(project)) == "https://local-gw.example"
    # lightfoot's own address (from `integrate on`) is never taken as an upstream.
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}}), encoding="utf-8"
    )
    (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    assert launch.resolve_upstream(8787, cfg, str(project)) == cfg["upstream"]


def test_launch_settings_file_routes_the_run_through_its_launch(lightfoot_home):
    path = launch.launch_settings_file(8787, Launch("abc123", "/p", "live", None, None, None))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787/l/abc123"}}
