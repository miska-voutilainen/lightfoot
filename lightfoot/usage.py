"""Extract token usage from responses, streamed (SSE) or not, per protocol.

Every parser reports the same counts: uncached input, short- and long-TTL cache
writes, cache reads, and output tokens.
"""

from __future__ import annotations

import json

MAX_JSON_BODY = 8 * 1024 * 1024


class _Parser:
    def __init__(self, content_type: str | None):
        self.sse = "text/event-stream" in (content_type or "")
        self._buf = b""
        self._body = bytearray()
        self.usage: dict = {}
        self.model: str | None = None

    def feed(self, chunk: bytes) -> None:
        try:
            if self.sse:
                # One split per chunk: splitting off one line at a time copied the rest each time.
                *lines, self._buf = (self._buf + chunk).split(b"\n")
                for line in lines:
                    self._line(line.rstrip(b"\r"))
            elif len(self._body) < MAX_JSON_BODY:
                self._body += chunk
        except Exception:  # noqa: S110 - parsing must never break the stream
            pass

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        # Most events are text deltas without usage: parse only those that can carry it, and
        # the first ones until the model is known.
        if payload[:1] == b"{" and (b"usage" in payload or self.model is None):
            self._event(json.loads(payload))

    def _event(self, event: dict) -> None:
        raise NotImplementedError

    def _final(self, data: dict) -> None:
        raise NotImplementedError

    def _merge(self, usage) -> None:
        if isinstance(usage, dict):
            for key, value in usage.items():
                if value is not None:
                    self.usage[key] = value

    def finish(self) -> None:
        try:
            if self.sse:
                if self._buf:
                    self._line(self._buf.rstrip(b"\r"))
                return
            data = json.loads(bytes(self._body))
            if isinstance(data, dict):
                self._final(data)
        except Exception:  # noqa: S110 - a usage-less response is still forwarded
            pass


class AnthropicUsage(_Parser):
    """Anthropic Messages: usage on `message_start` and `message_delta`, or in the body."""

    def _event(self, event):
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.model = message.get("model") or self.model
            self._merge(message.get("usage"))
        elif kind == "message_delta":
            self._merge(event.get("usage"))

    def _final(self, data):
        self.model = data.get("model") or self.model
        self._merge(data.get("usage"))

    def counts(self) -> dict:
        """Tokens by kind, with cache writes split into the 5-minute and 1-hour caches."""
        u = self.usage
        cache_creation = u.get("cache_creation")
        if isinstance(cache_creation, dict):
            cw5 = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
            cw1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
        else:
            cw5, cw1h = int(u.get("cache_creation_input_tokens") or 0), 0
        return {
            "input": int(u.get("input_tokens") or 0),
            "cw5": cw5,
            "cw1h": cw1h,
            "cr": int(u.get("cache_read_input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
        }


class GeminiUsage(_Parser):
    """Gemini API: every streamed chunk may carry usageMetadata; the last one is the total.

    promptTokenCount includes the implicitly cached part (cachedContentTokenCount), and
    output is billed as candidates plus thoughts. Implicit caching has no write charge, so
    no cache writes are reported. Without `alt=sse` a stream is one JSON array of chunks.
    """

    def _event(self, event):
        self.model = event.get("modelVersion") or self.model
        if isinstance(event.get("usageMetadata"), dict):
            self.usage = dict(event["usageMetadata"])

    def _final(self, data):
        self._event(data)

    def finish(self) -> None:
        """A non-streamed body may be one response or a list of chunks; read them all."""
        if self.sse:
            super().finish()
            return
        try:
            data = json.loads(bytes(self._body))
        except ValueError:
            return  # a usage-less response is still forwarded
        for chunk in data if isinstance(data, list) else [data]:
            if isinstance(chunk, dict):
                self._event(chunk)

    def counts(self) -> dict:
        """Tokens by kind. Gemini has no cache write charge; tool-use prompts count as input and thoughts as output."""
        u = self.usage
        prompt = int(u.get("promptTokenCount") or 0) + int(u.get("toolUsePromptTokenCount") or 0)
        cached = int(u.get("cachedContentTokenCount") or 0)
        return {
            "input": max(0, prompt - cached),
            "cw5": 0,
            "cw1h": 0,
            "cr": cached,
            "output": int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0),
        }


class ChatUsage(_Parser):
    """OpenAI Chat Completions and compatible providers.

    Streamed responses carry usage only in the last chunk, and only when the client asked
    for it (`stream_options.include_usage`); lightfoot doesn't add that, since it changes
    the stream. prompt_tokens includes cached tokens (`prompt_tokens_details.cached_tokens`,
    or DeepSeek's `prompt_cache_hit_tokens`).
    """

    def __init__(self, content_type: str | None, long_ttl: bool = False):
        super().__init__(content_type)
        self.long_ttl = long_ttl

    def _event(self, event):
        self.model = event.get("model") or self.model
        if isinstance(event.get("usage"), dict):
            self.usage = dict(event["usage"])

    def _final(self, data):
        self._event(data)

    def counts(self) -> dict:
        """Tokens by kind; cache writes, where a provider reports them, go to the lifetime the request asked for."""
        u = self.usage
        details = u.get("prompt_tokens_details") or {}
        total = int(u.get("prompt_tokens") or 0)
        cached = int(details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0)
        written = int(details.get("cache_write_tokens") or 0)
        return {
            "input": max(0, total - cached - written),
            "cw5": 0 if self.long_ttl else written,
            "cw1h": written if self.long_ttl else 0,
            "cr": cached,
            "output": int(u.get("completion_tokens") or 0),
        }


class ResponsesUsage(_Parser):
    """OpenAI Responses: usage arrives on response.completed (or .incomplete / .failed).

    input_tokens includes cached and cache-write tokens (input_tokens_details), so the
    uncached part is the remainder. Writes are short-TTL unless the request asked for
    extended retention.
    """

    def __init__(self, content_type: str | None, long_ttl: bool = False):
        super().__init__(content_type)
        self.long_ttl = long_ttl

    def _event(self, event):
        if event.get("type") in ("response.completed", "response.incomplete", "response.failed", "response.created"):
            response = event.get("response") or {}
            self.model = response.get("model") or self.model
            if response.get("usage"):
                self.usage = dict(response["usage"])

    def _final(self, data):
        self.model = data.get("model") or self.model
        if isinstance(data.get("usage"), dict):
            self.usage = dict(data["usage"])

    def counts(self) -> dict:
        """Tokens by kind; cache writes go to the lifetime `prompt_cache_retention` asked for."""
        u = self.usage
        details = u.get("input_tokens_details") or {}
        total = int(u.get("input_tokens") or 0)
        cached = int(details.get("cached_tokens") or 0)
        written = int(details.get("cache_write_tokens") or 0)
        return {
            "input": max(0, total - cached - written),
            "cw5": 0 if self.long_ttl else written,
            "cw1h": written if self.long_ttl else 0,
            "cr": cached,
            "output": int(u.get("output_tokens") or 0),
        }
