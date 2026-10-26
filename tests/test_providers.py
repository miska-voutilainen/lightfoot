"""Provider profiles, and no borrowed prices for models outside the known families."""

import time

from lightfoot import pricing, providers
from lightfoot.reports import stats
from lightfoot.store import Launch


def test_provider_by_wire_format_and_host():
    assert providers.for_request("anthropic", "https://gateway.example") is providers.ANTHROPIC
    assert providers.for_request("gemini", None) is providers.GEMINI
    assert providers.for_request("responses", "https://api.openai.com") is providers.OPENAI
    assert providers.for_request("chat", "https://api.deepseek.com") is providers.DEEPSEEK
    assert providers.for_request("chat", "https://api.x.ai/v1") is providers.XAI
    assert providers.for_request("chat", "https://openrouter.ai/api") is providers.OPENROUTER
    assert providers.for_request("chat", "http://127.0.0.1:11434") is providers.LOCAL
    assert providers.for_request("chat", "https://llm.corp.example") is providers.UNKNOWN
    assert [p.name for p in providers.PROVIDERS if p.compaction] == ["Anthropic"]


def test_unknown_families_get_no_borrowed_price():
    assert pricing.lookup("deepseek-v4") == (pricing.UNPRICED, False) and not pricing.priced("grok-5")
    assert pricing.priced("claude-opus-9") and pricing.priced("us.anthropic.claude-sonnet-5-5-v1:0")
    assert pricing.priced("gpt-7-nova") and pricing.priced(None)  # family defaults, as before


def test_stats_reports_unpriced_savings_in_tokens(store):
    store.put_launch(Launch("l1", "/p", "live", None, None, None))
    store.add_ledger(
        {
            "ts": time.time(),
            "launch_id": "l1",
            "session_id": "s1",
            "mode": "live",
            "model": "deepseek-v4",
            "status": 200,
            "input": 1000,
            "cw5": 0,
            "cw1h": 0,
            "cr": 50_000,
            "output": 10,
            "saved_tokens": 12_000,
            "agent": "generic",
        }
    )
    out = stats(store)
    row = next(line for line in out.splitlines() if line.startswith("s1"))
    assert "—" in row and "12k tok" in row
    assert "No price for deepseek-v4: their savings are shown in tokens" in out
