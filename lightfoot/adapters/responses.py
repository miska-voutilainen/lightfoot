"""OpenAI Responses API (POST /v1/responses): OpenAI Codex CLI over HTTP.

Item shapes researched from openai/codex (codex-rs/protocol/src/models.rs, 2026-10-25):
  {"type": "function_call", "name", "arguments": "<json>", "call_id"}
  {"type": "function_call_output", "call_id", "output": str | [{"type": "input_text", "text"}]}
  {"type": "custom_tool_call", "name", "input": str, "call_id"}         (apply_patch)
  {"type": "custom_tool_call_output", "call_id", "output": str | [...]}
  {"type": "reasoning", "summary", "encrypted_content"}                  (never touched)
  {"type": "message", "role": "developer" | "user" | "assistant", "content": [...]}

Over HTTP Codex sends the full history with store=false, so frozen replay works as it
does for Anthropic. Whether editing an earlier output invalidates a later reasoning
item is not yet verified, so this adapter never runs epochs (ingest rules only).
"""

from __future__ import annotations

import hashlib
import json

from ..text import tokens
from ..usage import ResponsesUsage
from .base import Adapter, Index, Result, ToolCall, canonical, encode

OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}
CALL_TYPES = {"function_call", "custom_tool_call"}
MODEL_SIDE = CALL_TYPES | {"reasoning"}
LONG_RETENTION = {"12h", "24h", "extended"}


def _item_type(item) -> str:
    if not isinstance(item, dict):
        return ""
    return item.get("type") or ("message" if "role" in item else "")


def _message_text(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(c.get("text", "")) for c in content or [] if isinstance(c, dict) and "text" in c)


def _is_model_side(item) -> bool:
    kind = _item_type(item)
    return kind in MODEL_SIDE or (kind == "message" and item.get("role") == "assistant")


class ResponsesAdapter(Adapter):
    """OpenAI Responses (Codex CLI): ingest rules only, experimental. Reasoning items are never touched."""

    name = "responses"
    units_key = "input"
    epochs_supported = False
    experimental = True

    def index(self, body, profile):
        """Index an OpenAI Responses request: function_call_output and custom_tool_call_output items
        are the rewritable content, reasoning items mark the reasoning units.
        """
        items = body["input"]
        calls: dict[str, ToolCall] = {}
        pending = []
        unit_turn: list[int] = []
        reasoning: set[int] = set()
        turn, previous_model_side = 0, True
        for i, item in enumerate(items):
            kind = _item_type(item)
            model_side = _is_model_side(item)
            if not model_side and previous_model_side:
                turn += 1
            previous_model_side = model_side
            unit_turn.append(turn)
            if kind == "reasoning":
                reasoning.add(i)
            elif kind in CALL_TYPES and isinstance(item.get("call_id"), str):
                name = str(item.get("name") or "")
                if kind == "function_call":
                    try:
                        inp = json.loads(item.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        inp = {}
                    inp = inp if isinstance(inp, dict) else {}
                else:
                    inp = {"input": item.get("input")} if isinstance(item.get("input"), str) else {}
                calls[item["call_id"]] = ToolCall(name, inp, profile.kind(name, inp), i)
            elif kind in OUTPUT_TYPES:
                pending.append((i, item, turn))

        results = []
        for i, item, t in pending:
            tid = item.get("call_id") if isinstance(item.get("call_id"), str) else ""
            call = calls.get(tid)
            shape, text = self.extract(item)
            results.append(
                Result(
                    unit=i,
                    path=("input", i, "output"),
                    tid=tid,
                    container=item,
                    name=call.name if call else "",
                    inp=call.inp if call else {},
                    kind=call.kind if call else "other",
                    turn=t,
                    shape=shape,
                    orig_text=text,
                    is_error=False,
                )
            )

        # The model has seen everything up to its last item; what follows is new.
        last_model = max((i for i, item in enumerate(items) if _is_model_side(item)), default=-1)
        ingest_from = last_model + 1
        model_from = last_model
        while model_from > 0 and _is_model_side(items[model_from - 1]):
            model_from -= 1
        if last_model < 0:
            model_from = len(items)
        return Index(items, results, calls, unit_turn, turn, ingest_from, model_from, reasoning)

    def extract(self, container):
        """Shape and text of a tool output item: a string, or a list of `input_text` parts."""
        output = container.get("output")
        if isinstance(output, str):
            return ("str", output) if output else (None, None)
        if isinstance(output, list) and output:
            texts = []
            for part in output:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "input_text"
                    or not isinstance(part.get("text"), str)
                ):
                    return None, None
                if set(part) - {"type", "text"}:
                    return None, None
                texts.append(part["text"])
            return "list", "\n".join(texts)
        return None, None

    def content_value(self, container, shape, text):
        """A string for a "str" result; otherwise one `input_text` part."""
        return text if shape == "str" else [{"type": "input_text", "text": text}]

    def content_text(self, value):
        """Text of a string or of a list of `input_text` parts."""
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(p, dict) and p.get("type") == "input_text" for p in value):
            return "\n".join(str(p.get("text", "")) for p in value)
        return None

    def unit_text(self, ui, unit, current):
        """An item's text: message text, call arguments or input, or tool output (rewritten from `current`)."""
        kind = _item_type(unit)
        if kind == "message":
            return _message_text(unit)
        if kind == "function_call":
            return str(unit.get("arguments") or "")
        if kind == "custom_tool_call":
            return str(unit.get("input") or "")
        if kind in OUTPUT_TYPES:
            path = ("input", ui, "output")
            return current.get(path) if path in current else (self.extract(unit)[1] or "")
        return ""

    def unit_tokens(self, ui, unit, current):
        """Tokens of one input item, with a rewritten output taken from `current`."""
        if _item_type(unit) in OUTPUT_TYPES and ("input", ui, "output") in current:
            return tokens(current[("input", ui, "output")] or "")
        return tokens(canonical(unit))

    def session_key(self, body):
        """`prompt_cache_key` when the client sends one; else the instructions and the first messages."""
        key = body.get("prompt_cache_key")
        if isinstance(key, str) and key:
            return hashlib.sha256(encode("pck:" + key)).hexdigest()[:16]
        items = body.get("input") or []
        first = [
            _message_text(i)
            for i in items
            if _item_type(i) == "message" and i.get("role") in ("developer", "system", "user")
        ][:3]
        seed = str(body.get("instructions") or "") + "\x00" + "\x00".join(first)
        return hashlib.sha256(encode(seed)).hexdigest()[:16]

    def usage_parser(self, content_type, request_body):
        """Usage from `response.completed` or the body; `prompt_cache_retention` decides the cache lifetime."""
        retention = (request_body or {}).get("prompt_cache_retention")
        return ResponsesUsage(content_type, long_ttl=str(retention or "").lower() in LONG_RETENTION)

    def prefix(self, body):
        """The instructions, developer messages, and each tool definition by name."""
        texts = [body["instructions"]] if isinstance(body.get("instructions"), str) else []
        texts += [
            _message_text(i)
            for i in body.get("input") or []
            if _item_type(i) == "message" and i.get("role") == "developer"
        ]
        tools = [(str(t.get("name") or t.get("type") or ""), t) for t in body.get("tools") or [] if isinstance(t, dict)]
        return texts, tools
