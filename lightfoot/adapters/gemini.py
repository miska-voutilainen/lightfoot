"""Gemini API (POST …/models/<model>:generateContent and :streamGenerateContent): Gemini CLI.

Shapes researched from google-gemini/gemini-cli at commit cd1b45c2db0a (2026-10-23),
packages/core/src/utils/generateContentResponseUtilities.ts:
  {"role": "user", "parts": [{"text"}]}
  {"role": "model", "parts": [{"text", "thought"?, "thoughtSignature"?},
                              {"functionCall": {"id", "name", "args"}, "thoughtSignature"?}]}
  {"role": "user", "parts": [{"functionResponse": {"id", "name", "response": {"output": str}}}]}
Errors are sent as `response: {"error": …}`, and binary output as extra `parts` or sibling
parts; only a response that is exactly `{"output": <text>}` is rewritable.

Checked end to end with Gemini CLI 0.65.0 against a local mock API on 2026-10-25: shell
output arrives wrapped in <untrusted_context>, and function calls carry the synthetic
thoughtSignature "skip_thought_signature_validator".

The model is named in the URL, not the body, so it comes from the response's
`modelVersion`. Whether editing an earlier function response invalidates a later thought
signature is not verified, so this adapter never runs epochs or prunes (ingest rules only).
Model turns, and so every thought signature, are never touched.
"""

from __future__ import annotations

import hashlib
import json

from ..text import tokens
from ..usage import GeminiUsage
from .base import Adapter, Index, Result, ToolCall, canonical, encode

RESULT_KEYS = {"id", "name", "response"}


def _parts(content) -> list:
    parts = content.get("parts") if isinstance(content, dict) else None
    return parts if isinstance(parts, list) else []


def _is_model(content) -> bool:
    return isinstance(content, dict) and content.get("role") == "model"


def _text(parts: list) -> str:
    return "\n".join(str(p["text"]) for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))


class GeminiAdapter(Adapter):
    """Gemini generateContent (Gemini CLI): ingest rules only, experimental."""

    name = "gemini"
    units_key = "contents"
    epochs_supported = False
    experimental = True

    def index(self, body, profile):
        """Index a generateContent request: functionResponse parts are the rewritable content,
        model turns with thoughts or thought signatures mark the reasoning units.
        """
        contents = body["contents"]
        calls: dict[str, ToolCall] = {}
        pending = []
        unit_turn: list[int] = []
        reasoning: set[int] = set()
        turn, previous_model = 0, True
        for i, content in enumerate(contents):
            model = _is_model(content)
            if not model and previous_model:
                turn += 1
            previous_model = model
            unit_turn.append(turn)
            for j, part in enumerate(_parts(content)):
                if not isinstance(part, dict):
                    continue
                if model and (part.get("thought") or part.get("thoughtSignature")):
                    reasoning.add(i)
                call = part.get("functionCall")
                if model and isinstance(call, dict) and isinstance(call.get("id"), str):
                    name = str(call.get("name") or "")
                    inp = call.get("args") if isinstance(call.get("args"), dict) else {}
                    calls[call["id"]] = ToolCall(name, inp, profile.kind(name, inp), i)
                response = part.get("functionResponse")
                if not model and isinstance(response, dict):
                    pending.append((i, j, response, turn))

        results = []
        for i, j, response, t in pending:
            tid = response.get("id") if isinstance(response.get("id"), str) else ""
            call = calls.get(tid)
            shape, text = self.extract(response)
            inner = response.get("response")
            results.append(
                Result(
                    unit=i,
                    path=("contents", i, "parts", j, "functionResponse", "response"),
                    tid=tid,
                    container=response,
                    name=call.name if call else str(response.get("name") or ""),
                    inp=call.inp if call else {},
                    kind=call.kind if call else "other",
                    turn=t,
                    shape=shape,
                    orig_text=text,
                    is_error=isinstance(inner, dict) and "error" in inner,
                )
            )

        # The model has seen everything up to its last turn; what follows is new.
        last_model = max((i for i, c in enumerate(contents) if _is_model(c)), default=-1)
        ingest_from = last_model + 1
        model_from = last_model
        while model_from > 0 and _is_model(contents[model_from - 1]):
            model_from -= 1
        if last_model < 0:
            model_from = len(contents)
        return Index(contents, results, calls, unit_turn, turn, ingest_from, model_from, reasoning)

    def extract(self, container):
        """The text of a functionResponse whose `response` is exactly `{"output": text}`.

        Anything else is left alone.
        """
        if set(container) - RESULT_KEYS:
            return None, None  # nested binary parts or unknown fields: never touched
        response = container.get("response")
        if not isinstance(response, dict) or set(response) != {"output"}:
            return None, None
        output = response["output"]
        return ("output", output) if isinstance(output, str) and output else (None, None)

    def content_value(self, container, shape, text):
        """The rewritten `response`: `{"output": text}`."""
        return {"output": text}

    def content_text(self, value):
        """The `output` text of a `{"output": text}` response."""
        if isinstance(value, dict) and set(value) == {"output"} and isinstance(value["output"], str):
            return value["output"]
        return None

    def _result_text(self, ui: int, j: int, part: dict, current: dict) -> str:
        path = ("contents", ui, "parts", j, "functionResponse", "response")
        if path in current:
            return current[path] or ""
        return self.extract(part["functionResponse"])[1] or canonical(part["functionResponse"].get("response"))

    def unit_text(self, ui, unit, current):
        """A turn's text (not thoughts), call arguments and responses, with rewritten responses taken from `current`."""
        out = []
        for j, part in enumerate(_parts(unit)):
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str) and not part.get("thought"):
                out.append(part["text"])
            elif isinstance(part.get("functionCall"), dict):
                out.append(json.dumps(part["functionCall"].get("args") or {}))
            elif isinstance(part.get("functionResponse"), dict):
                out.append(self._result_text(ui, j, part, current))
        return "\n".join(out)

    def unit_tokens(self, ui, unit, current):
        """Tokens of one turn, with rewritten responses taken from `current`."""
        total = 0
        for j, part in enumerate(_parts(unit)):
            path = ("contents", ui, "parts", j, "functionResponse", "response")
            total += tokens(current[path] or "") if path in current else tokens(canonical(part))
        return total

    def session_key(self, body):
        """No session id is sent, so the system prompt and the first user turn identify the conversation."""
        system = body.get("systemInstruction") or body.get("system_instruction")
        first = next((c for c in body.get("contents") or [] if isinstance(c, dict) and c.get("role") == "user"), {})
        seed = _text(_parts(system)) + "\x00" + _text(_parts(first))
        return hashlib.sha256(encode("gemini:" + seed)).hexdigest()[:16]

    def model(self, body):
        """Always None: Gemini names the model in the URL, so the usage parser reads it from the response."""
        return None  # in the URL; the usage parser reads it from the response

    def usage_parser(self, content_type, request_body):
        """Usage from `usageMetadata`, with the model from `modelVersion`."""
        return GeminiUsage(content_type)

    def prefix(self, body):
        """The system instruction's texts and each function declaration by name."""
        system = body.get("systemInstruction") or body.get("system_instruction") or {}
        parts = system.get("parts") if isinstance(system, dict) else None
        texts = [str(p.get("text", "")) for p in parts or [] if isinstance(p, dict)]
        tools = [
            (str(d.get("name", "")), d)
            for t in body.get("tools") or []
            if isinstance(t, dict)
            for d in t.get("functionDeclarations") or t.get("function_declarations") or []
            if isinstance(d, dict)
        ]
        return texts, tools
