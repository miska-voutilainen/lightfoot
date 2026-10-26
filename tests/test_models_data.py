"""lightfoot/data/models.toml: model prices and provider facts, kept as data."""

import pytest

from lightfoot import pricing, prune, server


def test_every_source_says_where_and_when():
    for name, source in pricing.DATA["sources"].items():
        assert source.get("page") and source.get("checked"), name
    assert set(pricing.DATA["prices"]) <= set(pricing.DATA["sources"])  # every price section has a source


def test_every_fallback_is_a_priced_model():
    for family, model in pricing.DATA["fallbacks"].items():
        assert model in pricing._TABLE, family


def test_the_code_reads_its_facts_from_the_file():
    anthropic = pricing.DATA["anthropic"]
    assert anthropic["compact_beta"] == prune.COMPACT_BETA
    assert anthropic["binding_beta"] == server.BINDING_BETA
    assert tuple(anthropic["compaction_models"]) == prune.COMPACTION_MODELS
    assert pricing.DATA["prices_as_of"] == pricing.PRICES_AS_OF


def test_list_prices_become_multipliers():
    sol = pricing._TABLE["gpt-6-sol"]  # input 2.00, cached 0.20
    assert (sol.read, sol.write_short, sol.write_long, sol.long_context_over) == (0.1, 1.25, 2.5, 272_000)
    assert pricing._TABLE["gpt-5"].write_short == 1.0  # no cache-write price listed: billed as input
    assert pricing._TABLE["gemini-2.5-pro"].write_short == 1.0  # Gemini bills no cache writes
    assert pricing._TABLE["claude-opus-5-5"].read == 0.05


def test_xai_and_deepseek_prices():
    # docs.x.ai/docs/models and api-docs.deepseek.com pricing, read 2026-10-26.
    grok = pricing._TABLE["grok-4.7"]  # 2.00 in, 0.50 cached, 6.00 out; 4.00 / 12.00 from 200K
    assert (grok.read, grok.write_short, grok.long_context_over, grok.long_input) == (0.25, 1.0, 200_000, 4.0)
    assert pricing.effective_cost("grok-4.7", 1_000_000, 0, 0, 0, 0) == 4.0  # past 200K, the whole request
    flash = pricing._TABLE["deepseek-flash"]  # off-peak: 0.15 miss, 0.003 hit, 0.60 out
    assert (flash.input, flash.output, flash.read, flash.write_short) == (0.15, 0.60, 0.02, 1.0)
    assert pricing.lookup("deepseek-v4-flash")[0] == flash  # the retired name bills at the Flash price
    assert not pricing.priced("grok-9") and not pricing.priced("deepseek-chat")  # unknown: tokens, not a guess


def test_a_malformed_entry_names_itself():
    with pytest.raises(ValueError, match=r"\[prices.openai\] 'gpt-x'"):
        pricing._table({"openai": {"gpt-x": {"input": 1.0, "output": 2.0}}})  # no cached price
    with pytest.raises(ValueError, match=r"\[prices.anthropic\] 'claude-x'"):
        pricing._table({"anthropic": {"claude-x": {"input": 1.0, "output": 2.0, "reed": 0.1}}})
    with pytest.raises(ValueError, match="unknown provider"):
        pricing._table({"acme": {"m": {"input": 1.0, "output": 2.0}}})


def test_data_age():
    checked = pricing.data_checked()
    assert checked >= pricing.DATA["prices_as_of"]
    assert pricing.data_age_days(checked) == 0
    assert pricing.data_age_days("2099-01-01") > pricing.STALE_AFTER_DAYS
