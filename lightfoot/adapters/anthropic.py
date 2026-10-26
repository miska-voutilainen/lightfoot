"""Anthropic Messages API (POST /v1/messages): Claude Code and Claude Agent SDK apps."""

from __future__ import annotations

import hashlib

from ..images import image_tokens, is_base64_image
from ..text import tokens
from ..usage import AnthropicUsage
from .base import Adapter, Index, Result, ToolCall, canonical, encode

THINKING_TYPES = {"thinking", "redacted_thinking"}


def plain_text(content) -> str:
    """The text of a string or a list of content blocks; non-text blocks are skipped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


class AnthropicAdapter(Adapter):
    """Anthropic Messages: the only adapter verified for history edits (epochs, prunes, drop_block)."""

    name = "anthropic"
    units_key = "messages"
    epochs_supported = True
    supports_prune = True
    supports_drop_block = True

    def index(self, body, profile):
        """Index an Anthropic Messages request: tool_result blocks are the rewritable content,
        thinking and redacted_thinking blocks mark the reasoning units.
        """
        msgs = body["messages"]
        calls: dict[str, ToolCall] = {}
        pending = []
        unit_turn: list[int] = []
        reasoning: set[int] = set()
        turn = 0
        for mi, message in enumerate(msgs):
            role = message.get("role") if isinstance(message, dict) else None
            if role == "user":
                turn += 1
            unit_turn.append(turn)
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if role == "assistant" and kind in THINKING_TYPES:
                    reasoning.add(mi)
                if kind == "tool_use" and role == "assistant" and isinstance(block.get("id"), str):
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    name = str(block.get("name") or "")
                    calls[block["id"]] = ToolCall(name, inp, profile.kind(name, inp), mi)
                elif kind == "tool_result" and role == "user":
                    pending.append((mi, bi, block, turn))

        results = []
        for mi, bi, block, t in pending:
            tid = block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else ""
            call = calls.get(tid)
            shape, text = self.extract(block)
            is_error = bool(block.get("is_error"))
            media = [c for c in block["content"] if is_base64_image(c)] if shape == "image" else []
            results.append(
                Result(
                    unit=mi,
                    path=("messages", mi, "content", bi, "content"),
                    tid=tid,
                    container=block,
                    name=call.name if call else "",
                    inp=call.inp if call else {},
                    kind=call.kind if call else "other",
                    turn=t,
                    shape=shape,
                    orig_text=text,
                    is_error=is_error,
                    media=media,
                )
            )

        # Trailing system messages (Claude Code 2.1.310+) don't end the user's turn: skip them.
        last = len(msgs) - 1
        while last >= 0 and isinstance(msgs[last], dict) and msgs[last].get("role") == "system":
            last -= 1
        ingest_from = (
            last if last >= 0 and isinstance(msgs[last], dict) and msgs[last].get("role") == "user" else len(msgs)
        )
        model_from = ingest_from - 1
        while model_from >= 0 and isinstance(msgs[model_from], dict) and msgs[model_from].get("role") == "system":
            model_from -= 1
        if model_from < 0:
            model_from = len(msgs)
        return Index(msgs, results, calls, unit_turn, turn, ingest_from, model_from, reasoning)

    def extract(self, container):
        """Shape and text of a tool_result's content, or (None, None) when it can't be rewritten.

        "str" and "list" are plain text. "image" is text blocks plus at least one inline image;
        only the images rule rewrites it. Documents, URL images, extra block fields or more than
        one cache marker make the content non-rewritable.
        """
        content = container.get("content")
        if isinstance(content, str):
            return ("str", content) if content else (None, None)
        if isinstance(content, list) and content:
            texts, markers, has_image = [], 0, False
            for inner in content:
                if is_base64_image(inner) and not set(inner) - {"type", "source", "cache_control"}:
                    has_image = True
                elif (
                    isinstance(inner, dict)
                    and inner.get("type") == "text"
                    and isinstance(inner.get("text"), str)
                    and not set(inner) - {"type", "text", "cache_control"}
                ):
                    texts.append(inner["text"])
                else:
                    return None, None
                markers += "cache_control" in inner
            if markers > 1:
                return None, None
            return ("image" if has_image else "list"), "\n".join(texts)
        return None, None

    def content_value(self, container, shape, text):
        """A string for a "str" result; otherwise one text block that keeps the client's cache marker."""
        if shape == "str":
            return text
        inner = {"type": "text", "text": text}
        for old in container.get("content") or []:
            if isinstance(old, dict) and "cache_control" in old:
                inner["cache_control"] = old["cache_control"]  # markers follow the client's placement
        return [inner]

    def content_text(self, value):
        """Text of a string or of a list of text blocks."""
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in value):
            return "\n".join(str(b.get("text", "")) for b in value)
        return None

    def block_tokens(self, block: dict) -> int:
        """Tokens of one content block, with images at their real cost rather than their base64 length."""
        kind = block.get("type")
        if kind == "text":
            return tokens(str(block.get("text", "")))
        if kind in THINKING_TYPES:
            return tokens(str(block.get("thinking", "")))
        if kind == "image":
            return image_tokens(block)
        if kind == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                return sum(self.block_tokens(c) for c in content if isinstance(c, dict))
            return tokens(content) if isinstance(content, str) else 0
        return tokens(canonical(block))

    def unit_text(self, ui, unit, current):
        """Text, tool inputs and tool results of one message, with rewritten results taken from `current`."""
        content = unit.get("content")
        if isinstance(content, str):
            return content
        parts = []
        for bi, block in enumerate(content or []):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                parts.append(canonical(block.get("input")))
            elif kind == "tool_result":
                path = ("messages", ui, "content", bi, "content")
                parts.append(current.get(path, plain_text(block.get("content"))) or "")
        return "\n".join(parts)

    def unit_tokens(self, ui, unit, current):
        """Tokens of one message, with rewritten results taken from `current`."""
        content = unit.get("content")
        if isinstance(content, str):
            return tokens(content)
        total = 0
        for bi, block in enumerate(content or []):
            if not isinstance(block, dict):
                continue
            path = ("messages", ui, "content", bi, "content")
            if path in current:
                total += tokens(current[path] or "")
            else:
                total += self.block_tokens(block)
        return total

    def session_key(self, body):
        """System prompt + first user message: stable for a conversation, distinct per subagent."""
        system = body.get("system")
        system_text = system if isinstance(system, str) else plain_text(system)
        first = next((m for m in body.get("messages", []) if isinstance(m, dict) and m.get("role") == "user"), None)
        first_text = plain_text(first.get("content")) if first else ""
        return hashlib.sha256(encode(system_text + "\x00" + first_text)).hexdigest()[:16]

    def cache_ttl(self, body):
        """300 for the default cache, 3600 if the client marks blocks with a 1-hour TTL."""

        def scan(blocks) -> bool:
            if isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict):
                        cc = b.get("cache_control")
                        if isinstance(cc, dict) and cc.get("ttl") == "1h":
                            return True
                        if scan(b.get("content")):
                            return True
            return False

        top = body.get("cache_control")
        if isinstance(top, dict) and top.get("ttl") == "1h":
            return 3600
        candidates = [body.get("system"), body.get("tools")]
        candidates += [m.get("content") for m in body.get("messages", [])[-3:] if isinstance(m, dict)]
        return 3600 if any(scan(c) for c in candidates) else 300

    def with_drop_block(self, body):
        """Set `thinking.block_binding.prefix_mismatch_behavior` to `drop_block`.

        The client's other thinking settings are kept.
        """
        thinking = dict(body["thinking"])
        binding = dict(thinking.get("block_binding") or {})
        binding["prefix_mismatch_behavior"] = "drop_block"
        thinking["block_binding"] = binding
        out = dict(body)
        out["thinking"] = thinking
        return out

    def usage_parser(self, content_type, request_body):
        """Usage from `message_start` and `message_delta`, or from a non-streamed body."""
        return AnthropicUsage(content_type)

    def prefix(self, body):
        """The system prompt's texts and each tool definition by name."""
        system = body.get("system")
        if isinstance(system, str):
            texts = [system]
        else:
            texts = [str(b.get("text", "")) for b in system or [] if isinstance(b, dict)]
        tools = [(str(t.get("name", "")), t) for t in body.get("tools") or [] if isinstance(t, dict)]
        return texts, tools
