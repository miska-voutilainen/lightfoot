"""JSON for request bodies: orjson when available (a default dependency on CPython), the standard library otherwise.

Parsing and re-serializing every request is most of lightfoot's per-turn CPU on long
sessions, and orjson is several times faster. Mixing the two is safe for the prompt
cache: the API caches parsed content, not JSON formatting.
"""

from __future__ import annotations

import json

try:
    import orjson
except ImportError:  # pragma: no cover - depends on the install
    orjson = None


def loads(data: bytes):
    """Parse JSON with orjson, falling back to the standard library for what orjson refuses."""
    if orjson is not None:
        try:
            return orjson.loads(data)
        except orjson.JSONDecodeError:
            pass  # e.g. lone surrogates, which only the standard library accepts
    return json.loads(data)


def dumps(obj) -> bytes:
    """Compact JSON bytes with orjson, falling back to the standard library for what orjson refuses."""
    if orjson is not None:
        try:
            return orjson.dumps(obj)
        except (TypeError, orjson.JSONEncodeError):
            pass  # integers beyond 64 bits, lone surrogates
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates: escaped output
        return json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")
