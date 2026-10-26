"""Provider profiles: what each model provider means for the planner and the reports.

The adapter knows a wire format; the provider behind it decides how the cache bills, what a
history edit may break, whether a server-side compaction exists, which credentials lightfoot
may use for a request of its own, and which unit a saving is best reported in. Chat
Completions is spoken by many providers, so for it the provider comes from the upstream host.

Facts are from each provider's own documentation, with the date checked; anything not
confirmed there is marked so rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Provider:
    """What one model provider allows: its cache model, whether history edits are verified, and its credentials."""

    name: str
    cache: str  # "explicit" (markers, a write premium) | "automatic" (prefix cache, no write premium) | "unknown"
    history_edits: str  # what an edit of earlier output can break, and whether lightfoot verified it is safe
    compaction: bool  # a server-side compaction API lightfoot can use for model-written summaries
    own_requests: str  # credentials lightfoot may send a request of its own with
    unit: str  # how a saving is best reported: "dollars", "limit" (subscriptions) or "tokens"
    source: str
    checked: str


ANTHROPIC = Provider(
    "Anthropic",
    "explicit",
    "verified: thinking signatures bind the prefix; lightfoot learns whether the account enforces it",
    True,
    "API keys and gateway tokens; never a Pro or Max login",
    "dollars",
    "platform.claude.com prompt caching and compaction; code.claude.com prompt caching",
    "2026-10-25",
)
OPENAI = Provider(
    "OpenAI",
    "automatic",
    "unverified: encrypted reasoning items may depend on earlier output; ingest rules only",
    False,
    "none",
    "dollars",
    "developers.openai.com prompt caching (writes billed from gpt-5.6)",
    "2026-10-25",
)
GEMINI = Provider(
    "Google Gemini",
    "automatic",
    "unverified: thought signatures may depend on earlier output; ingest rules only",
    False,
    "none",
    "dollars",
    "ai.google.dev pricing and implicit caching",
    "2026-10-22",
)
XAI = Provider(
    "xAI",
    "automatic",
    "unverified; ingest rules only",
    False,
    "none",
    "dollars",  # prices in data/models.toml, from docs.x.ai/docs/models
    "docs.x.ai prompt caching, usage and pricing (updated 2026-05-10); models page",
    "2026-10-26",
)
DEEPSEEK = Provider(
    "DeepSeek",
    "automatic",
    "unverified; reasoning_content is never touched; ingest rules only",
    False,
    "none",
    "dollars",  # off-peak prices in data/models.toml; peak hours bill double
    "api-docs.deepseek.com context caching (prefix from token 0; prompt_cache_hit_tokens); models & pricing",
    "2026-10-26",
)
OPENROUTER = Provider(
    "OpenRouter",
    "unknown",
    "depends on the routed model; ingest rules only",
    False,
    "none",
    "tokens",
    "routes to other providers; caching follows the model",
    "2026-10-25",
)
LOCAL = Provider(
    "Local server",
    "unknown",
    "nothing is billed; ingest rules only",
    False,
    "none",
    "tokens",  # the saving is context room, not money
    "Ollama, LM Studio and similar serve Chat Completions on localhost",
    "2026-10-25",
)
UNKNOWN = Provider(
    "Unknown OpenAI-compatible",
    "unknown",
    "unknown; ingest rules only",
    False,
    "none",
    "tokens",
    "-",
    "2026-10-25",
)

PROVIDERS = (ANTHROPIC, OPENAI, GEMINI, XAI, DEEPSEEK, OPENROUTER, LOCAL, UNKNOWN)
_HOSTS = {
    "api.anthropic.com": ANTHROPIC,
    "api.openai.com": OPENAI,
    "generativelanguage.googleapis.com": GEMINI,
    "api.x.ai": XAI,
    "api.deepseek.com": DEEPSEEK,
    "openrouter.ai": OPENROUTER,
}


def for_request(adapter: str, upstream: str | None) -> Provider:
    """The provider behind a request: by wire format, and for Chat Completions by upstream host."""
    if adapter == "anthropic":
        return ANTHROPIC  # a gateway in front still bills and caches as Anthropic
    if adapter == "gemini":
        return GEMINI
    host = (urlparse(upstream or "").hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return LOCAL
    for known, provider in _HOSTS.items():
        if host == known or host.endswith("." + known):
            return provider
    return OPENAI if adapter == "responses" else UNKNOWN
