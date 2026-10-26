"""OpenAI Chat Completions (POST …/chat/completions): Aider, gateways, local servers and most
OpenAI-compatible providers.

Message shapes, from the OpenAI API reference (2026-10-25):
  {"role": "system" | "developer" | "user", "content": str | [{"type": "text", "text"}, …]}
  {"role": "assistant", "content": str | None, "tool_calls": [{"id", "type": "function",
   "function": {"name", "arguments": "<json>"}}]}
  {"role": "tool", "tool_call_id", "content": str | [{"type": "text", "text"}]}

Providers add reasoning to assistant messages under their own keys (`reasoning_content`,
`reasoning`, `reasoning_details`, `thinking_blocks`); those messages count as reasoning
units and are never touched. Only `tool` message content is rewritable. Whether editing
an earlier tool message breaks any provider's reasoning state is not verified, so this
adapter runs the ingest rules only, like Responses and Gemini.
"""

from __future__ import annotations

import hashlib
import json

from ..text import tokens
from ..usage import ChatUsage
from .base import Adapter, Index, Result, ToolCall, canonical, encode

REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_details", "thinking_blocks")
LONG_RETENTION = {"12h", "24h", "extended"}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and "text" in c)
    return ""


def _is_model_side(message) -> bool:
    return isinstance(message, dict) and message.get("role") == "assistant"


class ChatAdapter(Adapter):
    """OpenAI Chat Completions (Aider, gateways, local servers): ingest rules only, experimental."""

    name = "chat"
    units_key = "messages"
    epochs_supported = False
    experimental = True

    def index(self, body, profile):
        """Index a Chat Completions request: `tool` messages are the rewritable content, and a turn starts
        at each user message that follows the model's side of the conversation.
        """
        messages = body["messages"]
        calls: dict[str, ToolCall] = {}
        pending = []
        unit_turn: list[int] = []
        reasoning: set[int] = set()
        turn, previous_model_side = 0, True
        for i, m in enumerate(messages):
            model_side = _is_model_side(m)
            if not model_side and previous_model_side:
                turn += 1
            previous_model_side = model_side
            unit_turn.append(turn)
            if not isinstance(m, dict):
                continue
            if model_side:
                if any(m.get(k) for k in REASONING_KEYS):
                    reasoning.add(i)
                for call in m.get("tool_calls") or []:
                    if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = str(fn.get("name") or "")
                    try:
                        inp = json.loads(fn.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        inp = {}
                    inp = inp if isinstance(inp, dict) else {}
                    calls[call["id"]] = ToolCall(name, inp, profile.kind(name, inp), i)
            elif m.get("role") == "tool":
                pending.append((i, m, turn))

        results = []
        for i, m, t in pending:
            tid = m.get("tool_call_id") if isinstance(m.get("tool_call_id"), str) else ""
            call = calls.get(tid)
            shape, text = self.extract(m)
            results.append(
                Result(
                    unit=i,
                    path=("messages", i, "content"),
                    tid=tid,
                    container=m,
                    name=call.name if call else "",
                    inp=call.inp if call else {},
                    kind=call.kind if call else "other",
                    turn=t,
                    shape=shape,
                    orig_text=text,
                    is_error=False,
                )
            )

        last_model = max((i for i, m in enumerate(messages) if _is_model_side(m)), default=-1)
        model_from = last_model if last_model >= 0 else len(messages)
        return Index(messages, results, calls, unit_turn, turn, last_model + 1, model_from, reasoning)

    def extract(self, container):
        """Shape and text of a `tool` message: a string, or a list of plain text parts."""
        content = container.get("content")
        if isinstance(content, str):
            return ("str", content) if content else (None, None)
        if isinstance(content, list) and content:
            texts = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                    return None, None
                if set(part) - {"type", "text"}:
                    return None, None
                texts.append(part["text"])
            return "list", "\n".join(texts)
        return None, None

    def content_value(self, container, shape, text):
        """A string for a "str" result; otherwise one text part."""
        return text if shape == "str" else [{"type": "text", "text": text}]

    def content_text(self, value):
        """Text of a string or of a list of text parts."""
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in value):
            return "\n".join(str(p.get("text", "")) for p in value)
        return None

    def unit_text(self, ui, unit, current):
        """A message's text and its tool-call arguments, with a rewritten `tool` message taken from `current`."""
        if not isinstance(unit, dict):
            return ""
        path = ("messages", ui, "content")
        text = current.get(path) if path in current else _text(unit.get("content"))
        args = [
            str((c.get("function") or {}).get("arguments") or "")
            for c in unit.get("tool_calls") or []
            if isinstance(c, dict)
        ]
        return "\n".join([text or "", *args]).strip()

    def unit_tokens(self, ui, unit, current):
        """Tokens of one message, with a rewritten `tool` message taken from `current`."""
        path = ("messages", ui, "content")
        if isinstance(unit, dict) and unit.get("role") == "tool" and path in current:
            return tokens(current[path] or "")
        return tokens(canonical(unit))

    def session_key(self, body):
        """`prompt_cache_key` when the client sends one; else the system prompt and the first user messages."""
        key = body.get("prompt_cache_key")
        if isinstance(key, str) and key:
            return hashlib.sha256(encode("pck:" + key)).hexdigest()[:16]
        messages = body.get("messages") or []
        system = [
            _text(m.get("content"))
            for m in messages
            if isinstance(m, dict) and m.get("role") in ("system", "developer")
        ]
        first = [_text(m.get("content")) for m in messages if isinstance(m, dict) and m.get("role") == "user"][:3]
        seed = "\x00".join(system[:1] + first)
        return hashlib.sha256(encode("chat:" + seed)).hexdigest()[:16]

    def usage_parser(self, content_type, request_body):
        """Usage from the final chunk (present only with `stream_options.include_usage`) or the body."""
        retention = (request_body or {}).get("prompt_cache_retention")
        return ChatUsage(content_type, long_ttl=str(retention or "").lower() in LONG_RETENTION)

    def prefix(self, body):
        """System and developer messages, and each tool definition by name."""
        texts = [
            _text(m.get("content"))
            for m in body.get("messages") or []
            if isinstance(m, dict) and m.get("role") in ("system", "developer")
        ]
        tools = [
            (str((t.get("function") or {}).get("name") or t.get("type") or ""), t)
            for t in body.get("tools") or []
            if isinstance(t, dict)
        ]
        return texts, tools
