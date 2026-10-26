"""`lightfoot capture`: record an agent's requests, redacted, to confirm or extend its profile.

A capture keeps what a profile is built from (the JSON structure, block types, roles, tool
and model names, header names, the client's user agent and betas) and masks everything
else: in every other string, letters become `x` and digits `0`, so the shape of tool
output (`src/xxx.xx:00:xxxx`, `<xxxx_xxxxxxx xxxx="…">`) survives but its content doesn't.
Strings holding JSON (OpenAI's function `arguments`) are redacted inside, so their keys stay.
Auth headers are never written.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .config import lightfoot_home, private_dir

# Keys whose string values are structure, not content: kept as they are.
KEEP_VALUES = {"type", "role", "name", "model", "status", "object", "stop_reason", "finish_reason", "wire_api"}
# Tool-call arguments and tool results: everything inside is content, whatever its key
# (Gemini's functionResponse.response can hold a "name" that is not a tool's).
TOOL_INPUTS = {"input", "args", "arguments", "response"}
# Headers whose values describe the client, not the user.
KEEP_HEADERS = {"user-agent", "anthropic-version", "anthropic-beta", "openai-beta", "content-type", "x-stainless-lang"}
DROP_HEADERS = {"authorization", "x-api-key", "x-goog-api-key", "api-key", "cookie", "proxy-authorization"}
_LETTER = re.compile(r"[^\W\d_]")
_DIGIT = re.compile(r"\d")


def state_key(launch_id: str) -> str:
    """The `state` key that turns capture on for a launch."""
    return f"capture:{launch_id}"


def capture_dir(launch_id: str) -> Path:
    """Where a launch's captured requests are written."""
    return lightfoot_home() / "captures" / launch_id


def mask(text: str) -> str:
    """A string with every letter replaced by `x` and every digit by `0`; JSON inside keeps its keys."""
    stripped = text.strip()
    if stripped[:1] in ("{", "[") and stripped[-1:] in ("}", "]"):
        try:  # JSON inside a string is content (tool output, arguments): keys stay, every value is masked
            return json.dumps(redact(json.loads(stripped), content=True), ensure_ascii=False)
        except ValueError:
            pass
    return _DIGIT.sub("0", _LETTER.sub("x", text))


def redact(value: object, key: str | None = None, content: bool = False) -> object:
    """`value` with its content masked; `content` is set inside tool-call arguments."""
    content = content or key in TOOL_INPUTS
    if isinstance(value, dict):
        return {k: redact(v, k, content) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key, content) for v in value]
    if isinstance(value, str):
        if key in KEEP_VALUES and not content and len(value) <= 200:
            return value
        if key == "data" and len(value) > 200:  # base64 images and files
            return f"<{len(value)} characters>"
        return mask(value)
    return value


def redact_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    """Request headers safe to save: auth and cookies dropped, unknown values masked."""
    out = {}
    for name, value in headers:
        lower = name.lower()
        if lower in DROP_HEADERS:
            continue
        out[lower] = value if lower in KEEP_HEADERS else "…"
    return out


_next_seq: dict[str, int] = {}
_seq_lock = threading.Lock()  # requests are saved from worker threads, several at once


def _claim(folder: Path) -> int:
    """The next sequence number in `folder`, counted once per folder rather than on every request."""
    with _seq_lock:
        seq = _next_seq.get(str(folder)) or len(list(folder.glob("*.json"))) + 1
        _next_seq[str(folder)] = seq + 1
        return seq


def save(launch_id: str, method: str, path: str, headers: list[tuple[str, str]], raw: bytes) -> Path:
    """Write one redacted request as <seq>.json in the launch's capture folder."""
    folder = private_dir(capture_dir(launch_id))
    try:
        body: object = redact(json.loads(raw)) if raw else None
    except ValueError:
        body = f"<{len(raw)} bytes, not JSON>"
    record = {"ts": round(time.time(), 3), "method": method, "path": path, "headers": redact_headers(headers)}
    text = json.dumps({**record, "body": body}, indent=1, ensure_ascii=False)
    while True:
        out = folder / f"{_claim(folder):05d}.json"
        try:
            with open(out, "x", encoding="utf-8") as f:  # never overwrite an earlier capture
                f.write(text)
            return out
        except FileExistsError:
            continue
