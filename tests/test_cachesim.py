"""`lightfoot estimate --cache`: cache lifetimes and keep-alive replayed over recorded gaps."""

import json

import pytest

from lightfoot import cachesim, pricing

MODEL = "claude-opus-5-5"


def point(ts, cw=0, cr=0, inp=10, out=100):
    return cachesim.Point(ts, MODEL, {"input": inp, "cw5": 0, "cw1h": cw, "cr": cr, "output": out})


def test_pings_and_warmth():
    assert cachesim._pings(200, 300, 10_800) == (0, True)
    assert cachesim._pings(400, 300, 10_800) == (1, True)  # one ping at 270 s keeps it warm until 570 s
    assert cachesim._pings(20_000, 300, 10_800)[1] is False  # the limit stops the pings
    assert cachesim._pings(3000, 3600, 10_800) == (0, True)


def test_a_long_gap_rebuilds_without_keep_alive_and_pings_with_it():
    conv = [point(0, cw=100_000), point(60, cw=1_000, cr=100_000), point(60 + 2 * 3600, cw=101_100)]
    sim = cachesim.simulate([conv])
    p, _ = pricing.lookup(MODEL)
    one_hour, kept = sim.policies["1h"], sim.policies["1h + keep-alive"]
    assert one_hour.rebuilds == 1 and kept.rebuilds == 0 and kept.pings == 2
    rebuilt = p.input * (p.write_long - p.read) * 101_010 / 1e6  # the second call's prompt, written not read
    assert one_hour.rebuild_cost == pytest.approx(rebuilt)
    assert kept.ping_cost == pytest.approx(2 * p.input * p.read * 101_010 / 1e6)
    assert kept.cost == pytest.approx(one_hour.cost - rebuilt + kept.ping_cost)
    assert sim.policies["5m"].rebuilds == 1 and sim.gaps == {"under 5 min": 1, "over 60 min": 1}


def test_reads_transcripts_and_renders(tmp_path):
    def entry(mid, ts, usage, session="s1"):
        msg = {"id": mid, "model": MODEL, "usage": usage}
        return json.dumps({"type": "assistant", "sessionId": session, "timestamp": ts, "message": msg})

    usage = {"input_tokens": 5, "cache_creation_input_tokens": 1000, "output_tokens": 9}
    lines = [
        entry("m1", "2026-10-25T10:00:00Z", usage),
        entry("m1", "2026-10-25T10:00:00Z", usage),  # the same response logged twice
        entry("m2", "2026-10-25T12:00:00Z", {**usage, "cache_read_input_tokens": 1000}),
        entry("m3", "2026-10-25T12:00:00Z", usage, session="s2"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    convs = cachesim.read_conversations([path])
    assert sorted(len(c) for c in convs) == [1, 2]
    out = cachesim.render(cachesim.simulate(convs))
    assert "3 calls in 2 conversations" in out and "Cheapest on this history:" in out
    assert cachesim.render(cachesim.simulate([])) == "Cache lifetime: no API calls found."
