"""Protocol adapters, chosen by request path."""

from __future__ import annotations

import re

from .anthropic import AnthropicAdapter
from .base import Adapter, Index, Result, ToolCall, apply_changes, count_cache_markers
from .chat import ChatAdapter
from .gemini import GeminiAdapter
from .responses import ResponsesAdapter

ANTHROPIC = AnthropicAdapter()
RESPONSES = ResponsesAdapter()
GEMINI = GeminiAdapter()
CHAT = ChatAdapter()

GEMINI_PATH_RE = re.compile(r"(?:^|/)models/[^/]+:(?:stream)?generateContent$", re.I)


def for_path(method: str, path: str) -> Adapter | None:
    """The adapter that rewrites this request, or None for pure passthrough."""
    if method != "POST":
        return None
    path = path.rstrip("/")
    if path.endswith("v1/messages"):
        return ANTHROPIC
    if path.endswith("/responses") or path == "responses":
        return RESPONSES
    if GEMINI_PATH_RE.search(path):
        return GEMINI
    if path.endswith("chat/completions"):
        return CHAT
    return None


__all__ = [
    "ANTHROPIC",
    "CHAT",
    "GEMINI",
    "RESPONSES",
    "Adapter",
    "Index",
    "Result",
    "ToolCall",
    "apply_changes",
    "count_cache_markers",
    "for_path",
]
