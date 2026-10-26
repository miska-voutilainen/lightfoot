import json
import time

import pytest

from lightfoot import statusline
from lightfoot.store import Launch, Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "claude" / "settings.json"
    monkeypatch.setattr(statusline, "settings_path", lambda: path)
    return path


def _ledger(store, launch, mode="live", saved=50_000):
    store.put_launch(Launch(launch, "/p", mode, None, None, None))
    store.add_ledger(
        {
            "ts": time.time(),
            "launch_id": launch,
            "session_id": "s",
            "mode": mode,
            "model": "claude-opus-5-5",
            "status": 200,
            "input": 10,
            "cw5": 0,
            "cw1h": 1_000,
            "cr": 100_000,
            "output": 300,
            "saved_tokens": saved,
        }
    )


def test_reads_a_database_whose_path_needs_escaping(tmp_path):
    folder = tmp_path / "My Data #1 100%"
    store = Store(folder / "lightfoot.db")
    try:
        _ledger(store, "abc")
    finally:
        store.close()
    assert statusline.segment("abc", db=folder / "lightfoot.db").startswith("lightfoot −")


def test_launch_id_comes_from_the_base_url():
    assert statusline.launch_id({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787/l/ab12_cd"}) == "ab12_cd"
    assert statusline.launch_id({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None
    assert statusline.launch_id({}) is None


def test_segment_for_live_shadow_off_and_unknown_runs(lightfoot_home):
    store = Store()
    try:
        _ledger(store, "live1")
        _ledger(store, "shad1", mode="shadow")
        db = lightfoot_home / "lightfoot.db"
        live = statusline.segment("live1", db)
        assert live.startswith("lightfoot −") and "saved" in live
        assert statusline.segment("live1", db, subscription=True).endswith("% usage")
        assert statusline.segment("shad1", db).startswith("lightfoot shadow · would save ")
        assert statusline.segment("nobody", db) == "lightfoot ready"
        assert statusline.segment(None, db) == ""
        store.set_state("switch", "off")
        assert statusline.segment("live1", db) == "lightfoot off"
    finally:
        store.close()


def test_install_keeps_and_wraps_an_existing_status_line(settings, lightfoot_home, monkeypatch):
    settings.parent.mkdir(parents=True)
    original = {"type": "command", "command": "echo mine", "padding": 1}
    settings.write_text(json.dumps({"statusLine": original, "model": "opus"}), encoding="utf-8")
    statusline.install()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert statusline.MARK in data["statusLine"]["command"] and data["statusLine"]["padding"] == 1
    assert data["model"] == "opus" and statusline.is_installed()
    statusline.install()  # a second install must not wrap lightfoot inside itself
    assert json.loads(statusline.state_path().read_text(encoding="utf-8"))["command"] == "echo mine"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787/l/nobody")
    out = statusline.render("{}")
    assert out == "mine  lightfoot ready"

    assert statusline.uninstall() is True
    assert json.loads(settings.read_text(encoding="utf-8"))["statusLine"] == original
    assert statusline.uninstall() is False


def test_install_without_a_previous_status_line(settings, lightfoot_home, monkeypatch):
    statusline.install()
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert statusline.render("{}") == ""  # not a lightfoot run, and nothing else to show
    statusline.uninstall()
    assert "statusLine" not in json.loads(settings.read_text(encoding="utf-8"))


def test_main_writes_utf8(lightfoot_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787/l/nobody")
    assert statusline.main(b"{}") == b"lightfoot ready"


def _limits(five, resets=2_000_000_000):
    return {"rate_limits": {"five_hour": {"used_percentage": five, "resets_at": resets}}}


def test_limits_are_recorded_only_on_change_and_shown_as_a_share_of_the_limit(lightfoot_home):
    from lightfoot.reports import limit_report

    store = Store()
    try:
        db = lightfoot_home / "lightfoot.db"
        now = time.time() - 100  # before the ledger row
        statusline.record_limits("live1", _limits(10.0), db, now=now)
        statusline.record_limits("live1", _limits(10.0), db, now=now + 1)  # unchanged: not written again
        statusline.record_limits("live1", {"rate_limits": "bogus"}, db)
        statusline.record_limits(None, _limits(50.0), db)
        assert len(store.query("SELECT * FROM limits")) == 1
        assert statusline.segment("live1", db) == "lightfoot ready"
        _ledger(store, "live1")
        assert "limit" not in statusline.segment("live1", db)  # one point: no use measured yet
        statusline.record_limits("live1", _limits(14.0), db, now=now + 2)
        live = statusline.segment("live1", db)  # rate limits mean a subscription, without billing set
        assert live.startswith("lightfoot −") and live.endswith("% of 5h limit saved") and "$" not in live
        points = float(live.split("≈")[1].split("%")[0])
        cost, saved = statusline.pricing.row_costs(store.query("SELECT * FROM ledger")[0])
        assert points == pytest.approx(4.0 * saved / cost, abs=0.05)
        rows = store.query("SELECT * FROM ledger")
        report = "\n".join(limit_report(store, rows))
        assert "5-hour limit +4.0 points, saved ≈" in report
        statusline.record_limits("live1", _limits(1.0, resets=2_000_018_000), db, now=now + 3)  # a new window
        assert "limit" not in statusline.segment("live1", db)
    finally:
        store.close()


def test_render_records_limits_from_claude_codes_input(lightfoot_home, monkeypatch):
    Store().close()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787/l/run1")
    statusline.render(json.dumps(_limits(7.5)))
    statusline.render("not json")
    store = Store()
    try:
        assert [tuple(r) for r in store.query("SELECT launch_id, window, used_pct FROM limits")] == [
            ("run1", "five_hour", 7.5)
        ]
    finally:
        store.close()


def test_shadow_nudges_once_the_saving_is_clear(lightfoot_home):
    from lightfoot.reports import shadow_readiness

    store = Store()
    try:
        db = lightfoot_home / "lightfoot.db"
        _ledger(store, "big", mode="shadow")
        _ledger(store, "small", mode="shadow", saved=100)
        assert statusline.segment("big", db).endswith("· `lightfoot shadow off` to save")
        assert "shadow off" not in statusline.segment("small", db)
        store.execute("UPDATE ledger SET session_id = launch_id")
        assert shadow_readiness(store) is None  # two sessions are not enough to go on
        _ledger(store, "third", mode="shadow")
        store.execute("UPDATE ledger SET session_id = launch_id")
        ready = shadow_readiness(store)
        assert ready.startswith("shadow mode measured 3 sessions in 30 days") and "`lightfoot shadow off`" in ready
    finally:
        store.close()
