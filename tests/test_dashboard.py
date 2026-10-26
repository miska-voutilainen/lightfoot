import json
import re
from datetime import datetime, timedelta

from lightfoot import dashboard
from lightfoot.store import Launch


def _noon(days_ago: int) -> float:
    """Local noon `days_ago` calendar days back: a fixed 86,400 s step skips a day across a DST change."""
    day = datetime.now().date() - timedelta(days=days_ago)
    return datetime.combine(day, datetime.min.time()).replace(hour=12).timestamp()


def _row(store, launch, mode="live", days_ago=0, saved=40_000):
    store.add_ledger(
        {
            "ts": _noon(days_ago),
            "launch_id": launch,
            "session_id": f"s-{launch}",
            "mode": mode,
            "model": "claude-opus-5-5",
            "status": 200,
            "input": 10,
            "cw5": 0,
            "cw1h": 1_000,
            "cr": 100_000,
            "output": 300,
            "saved_tokens": saved,
            "rules": json.dumps({"prune": 1}),
        }
    )


def test_page_is_self_contained_and_escapes_project_paths(store):
    store.put_launch(Launch("l1", "C:/work/<script>alert(1)</script>", "live", None, None, None))
    for day in range(3):
        _row(store, "l1", days_ago=day)
    page = dashboard.render(dashboard.collect(store, 30))
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert not re.search(r"""(src|href)=["']https?:""", page)  # loads nothing from the network
    assert page.count('class="hit"') == 3  # one hover target per day
    assert "Projected" not in page


def test_shadow_only_data_is_labelled_as_a_projection(store):
    store.put_launch(Launch("l2", "/p", "shadow", None, None, None))
    _row(store, "l2", mode="shadow")
    data = dashboard.collect(store, 30)
    assert data["projected"] and "Projected saving" in dashboard.render(data)


def test_empty_period_and_rule_warnings(store):
    assert "No requests in this period." in dashboard.render(dashboard.collect(store, 30))
    store.put_launch(Launch("l3", "/p", "live", None, None, None))
    for _ in range(25):
        _row(store, "l3")
    for i in range(3):
        store.add_signal(f"t{i}", "s-l3", "prune", "history_read", "/h.md")
    assert "over 5%" in dashboard.render(dashboard.collect(store, 30))


def test_axis_ceiling_is_round():
    assert dashboard._nice_ceiling(6.8) == 8
    assert dashboard._nice_ceiling(0.31) == 0.4
    assert dashboard._nice_ceiling(0) == 1.0


def test_write_puts_the_page_in_lightfoot_home(store, lightfoot_home):
    path = dashboard.write(store, days=7)
    assert path == lightfoot_home / "dashboard.html" and path.read_text(encoding="utf-8").startswith("<!doctype html>")
