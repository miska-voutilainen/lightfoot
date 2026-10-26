"""Per-model prices (USD per million tokens) and cache multipliers.

The prices, their sources and the fallback models live in `lightfoot/data/models.toml`, so a
new model or a price change is a data edit. This module turns each provider's list prices into
`Price` rows and looks models up.

Ledger columns cw5 / cw1h hold short- and long-TTL cache writes for every provider.
Override or add models in config:

    [prices."my-model"]
    input = 3.0
    output = 15.0
    read = 0.1
    write_short = 1.25
    write_long = 2.0
"""

from __future__ import annotations

import re
import tomllib
from collections import namedtuple
from pathlib import Path

DATA_FILE = Path(__file__).with_name("data") / "models.toml"
DATA: dict = tomllib.loads(DATA_FILE.read_text(encoding="utf-8"))
PRICES_AS_OF: str = DATA["prices_as_of"]

# A namedtuple rather than a dataclass: the status line imports this module on every refresh,
# and dataclasses (with inspect) is its largest import.
Price = namedtuple(
    "Price",
    [
        "input",
        "output",
        "read",  # cache read multiplier on the input price
        "write_short",  # cache write multiplier, short TTL
        "write_long",  # cache write multiplier, long TTL
        "long_context_over",
        "long_input",
        "long_output",
    ],
    defaults=(0.1, 1.25, 2.0, None, None, None),
)


def _openai(inp, out, cached, write=True, long=None):
    """OpenAI row from list prices; `long` = (input, output) above 272K input tokens."""
    return Price(
        inp,
        out,
        read=round(cached / inp, 4),
        write_short=1.25 if write else 1.0,
        write_long=2.5 if write else 1.0,
        long_context_over=272_000 if long else None,
        long_input=long[0] if long else None,
        long_output=long[1] if long else None,
    )


def _gemini(inp, out, long=None):
    """Gemini row: no cache-write charge, reads at 0.1x; `long` = (input, output) above 200K tokens."""
    return Price(
        inp,
        out,
        read=0.1,
        write_short=1.0,
        write_long=1.0,
        long_context_over=200_000 if long else None,
        long_input=long[0] if long else None,
        long_output=long[1] if long else None,
    )


def _automatic(inp, out, cached, long=None, long_over=200_000):
    """Row for a provider with automatic caching and no write charge (xAI, DeepSeek)."""
    return Price(
        inp,
        out,
        read=round(cached / inp, 4),
        write_short=1.0,
        write_long=1.0,
        long_context_over=long_over if long else None,
        long_input=long[0] if long else None,
        long_output=long[1] if long else None,
    )


def _anthropic(row: dict) -> Price:
    """Anthropic row: the listed prices and any multipliers that differ from the defaults."""
    return Price(**row)


def _row(provider: str, row: dict) -> Price:
    """One model's Price from its provider's list prices in the data file."""
    long = tuple(row["long"]) if "long" in row else None
    if provider == "anthropic":
        return _anthropic(row)
    if provider == "openai":
        return _openai(row["input"], row["output"], row["cached"], row.get("cache_writes", True), long)
    if provider == "gemini":
        return _gemini(row["input"], row["output"], long)
    if provider in ("xai", "deepseek"):
        return _automatic(row["input"], row["output"], row["cached"], long, row.get("long_context_over", 200_000))
    raise ValueError(f"unknown provider section [prices.{provider}]")


def _table(prices: dict) -> dict[str, Price]:
    """Every model in the data file's [prices] sections, as a Price.

    A malformed entry fails loudly with its name: wrong prices must never be used silently.
    """
    table: dict[str, Price] = {}
    for provider, rows in prices.items():
        for model, row in rows.items():
            try:
                table[model] = _row(provider, row)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{DATA_FILE.name}: [prices.{provider}] {model!r}: {exc!r}") from None
    return table


_TABLE: dict[str, Price] = _table(DATA["prices"])

# Unknown Claude, OpenAI and Gemini models are priced like their family's default model and
# flagged in stats. Models from any other family (DeepSeek, Grok, local models, …) get no
# price rather than a borrowed one: their savings are reported in tokens until [prices] has them.
UNPRICED = Price(0.0, 0.0, read=0.1, write_short=1.0, write_long=1.0)
FALLBACK_MODEL: str = DATA["fallbacks"]["anthropic"]
OPENAI_FALLBACK_MODEL: str = DATA["fallbacks"]["openai"]
GEMINI_FALLBACK_MODEL: str = DATA["fallbacks"]["gemini"]

_overrides: dict[str, Price] = {}
_TABLE_IDS = sorted(_TABLE, key=len, reverse=True)  # longest first, so gpt-5.4-mini wins over gpt-5.4
_looked_up: dict[str | None, tuple[Price, bool]] = {}  # reports price every ledger row: memoized per model


STALE_AFTER_DAYS = 60


def data_checked() -> str:
    """The newest date anything in the data file was checked against a provider's page (YYYY-MM-DD)."""
    dates = [DATA["prices_as_of"], DATA["anthropic"]["compaction_models_checked"]]
    dates += [source["checked"] for source in DATA["sources"].values()]
    return max(dates)


def data_age_days(today: str | None = None) -> int:
    """Days since `data_checked()`; past STALE_AFTER_DAYS, newer models and prices are likely missing."""
    from datetime import date

    return ((date.fromisoformat(today) if today else date.today()) - date.fromisoformat(data_checked())).days


def configure(prices: dict | None) -> list[str]:
    """Install `[prices]` overrides from config; returns error strings for invalid entries."""
    _overrides.clear()
    _looked_up.clear()
    errors = []
    for model, spec in (prices or {}).items():
        if not isinstance(spec, dict):
            errors.append(f"[prices.{model!r}] must be a table")
            continue
        base, _ = _lookup_table(model)
        try:
            fields = {
                k: float(v) for k, v in spec.items() if k in ("input", "output", "read", "write_short", "write_long")
            }
        except (TypeError, ValueError):
            errors.append(f"[prices.{model!r}] has a non-numeric value")
            continue
        _overrides[model.lower()] = base._replace(long_context_over=None, long_input=None, long_output=None, **fields)
    return errors


def _normalize(model: str | None) -> str:
    return (model or "").lower().split("[", 1)[0].strip()


# A table id matches itself or itself plus a snapshot suffix (claude-opus-4-1-20250805,
# gpt-5-2025-08-07, gemini-2.5-pro-001, …), never a longer different model (gpt-5.7-sol is
# not gpt-5).
_SNAPSHOT_SUFFIX = re.compile(r"^(?:[-@](?:\d{8}|\d{4}-\d{2}-\d{2}|\d{3}|latest|preview)\b.*|@.*)?$")


def _matches(name: str, model_id: str) -> bool:
    return name.startswith(model_id) and bool(_SNAPSHOT_SUFFIX.match(name[len(model_id) :]))


def _lookup_table(model: str | None) -> tuple[Price, bool]:
    name = _normalize(model)
    for model_id in _TABLE_IDS:
        if _matches(name, model_id):
            return _TABLE[model_id], True
    if name.startswith("gemini-"):
        fallback = GEMINI_FALLBACK_MODEL
    elif name.startswith(("gpt-", "o1", "o3", "o4", "codex")):
        fallback = OPENAI_FALLBACK_MODEL
    elif not name or "claude" in name:  # also Bedrock and Vertex ids (us.anthropic.claude-…)
        fallback = FALLBACK_MODEL
    else:
        return UNPRICED, False
    return _TABLE[fallback], False


def lookup(model: str | None) -> tuple[Price, bool]:
    """(price, known) for a model id such as 'claude-opus-5-5', 'claude-opus-5-5[1m]' or 'gpt-6-sol'."""
    found = _looked_up.get(model)
    if found is None:
        name = _normalize(model)
        prefix = next((p for p in sorted(_overrides, key=len, reverse=True) if name.startswith(p)), None)
        found = (_overrides[prefix], True) if prefix is not None else _lookup_table(model)
        if len(_looked_up) > 1000:
            _looked_up.clear()
        _looked_up[model] = found
    return found


def priced(model: str | None) -> bool:
    """False for a model with no price of its own or of its family: report its savings in tokens."""
    return lookup(model)[0] is not UNPRICED


def effective_cost(
    model: str | None,
    input_tokens: int,
    cache_write_short: int,
    cache_write_long: int,
    cache_read: int,
    output_tokens: int,
) -> float:
    """Dollars for one call's tokens at the model's price, long-context rates included past their threshold."""
    price, _ = lookup(model)
    p_in, p_out = price.input, price.output
    prompt = input_tokens + cache_write_short + cache_write_long + cache_read
    if price.long_context_over and prompt > price.long_context_over:
        p_in, p_out = price.long_input or p_in, price.long_output or p_out
    weighted = (
        input_tokens
        + price.write_short * cache_write_short
        + price.write_long * cache_write_long
        + price.read * cache_read
    )
    return (p_in * weighted + p_out * output_tokens) / 1_000_000


def input_cost(
    model: str | None, input_tokens: int, cache_write_short: int, cache_write_long: int, cache_read: int
) -> float:
    """Dollars for one call's input side only: uncached input, cache writes and cache reads."""
    return effective_cost(model, input_tokens, cache_write_short, cache_write_long, cache_read, 0)


def read_multiplier(model: str | None) -> float:
    """What a cache read costs relative to uncached input for this model."""
    return lookup(model)[0].read


def write_multiplier(model: str | None, long_ttl: bool) -> float:
    """What a cache write costs relative to uncached input, for the short or the long cache lifetime."""
    price = lookup(model)[0]
    return price.write_long if long_ttl else price.write_short


def row_costs(row) -> tuple[float, float]:
    """(effective cost, estimated cost avoided) for one ledger row.

    The avoided cost prices the saved tokens at the row's own average input rate, so a
    request served mostly from the cache counts its saving at cache-read prices.
    """
    model = row["model"]
    cost = effective_cost(model, row["input"], row["cw5"], row["cw1h"], row["cr"], row["output"])
    prompt = row["input"] + row["cw5"] + row["cw1h"] + row["cr"]
    saved = 0.0
    if row["saved_tokens"] and prompt:
        rate = input_cost(model, row["input"], row["cw5"], row["cw1h"], row["cr"]) / prompt
        saved = row["saved_tokens"] * rate
    return cost, saved
