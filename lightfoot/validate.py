"""Invariant checks on every rewritten request, for any protocol. Violations raise ValidationError.

The structural check is protocol-neutral: the rewritten body must equal the original
everywhere except at the declared content paths (and, for drop_block, the thinking
parameter's block_binding). That covers system prompt, tools, user text, thinking or
reasoning items, ids, ordering and tool-call pairing in one walk.
"""

from __future__ import annotations

import json

from .adapters.base import count_cache_markers
from .images import is_base64_image
from .ingest import json_trim_ok
from .text import error_line_indices, normalize_terminal

# Rules that only rewrite output the model has not seen yet (the newest turn).
INGEST_RULES = {"json", "json_trim", "noise", "spill"}
ERROR_PRESERVING_RULES = {"noise", "spill", "old_results"}


class ValidationError(Exception):
    """A rewritten request differs from its original somewhere a rewrite may not change."""

    pass


def _walk(a, b, path: tuple, skip: set, prefixes: set) -> None:
    """Compare original and rewritten bodies node by node, skipping the declared change paths.

    Raises on the first difference anywhere else, so a bug in a rule can never alter a
    part of the request it was not allowed to touch.
    """
    if a is b or path in skip:
        return
    if path not in prefixes:
        if a != b:
            raise ValidationError(f"unexpected change at {path or 'top level'}")
        return
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            raise ValidationError(f"keys changed at {path or 'top level'}")
        for key in a:
            _walk(a[key], b[key], (*path, key), skip, prefixes)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            raise ValidationError(f"length changed at {path}")
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _walk(x, y, (*path, i), skip, prefixes)
    else:
        raise ValidationError(f"type changed at {path}")


def _same_json(before: str, after: str) -> bool:
    """True when both texts parse to the same JSON value (the json rule may only drop whitespace)."""
    try:
        return json.loads(before) == json.loads(after)
    except ValueError:
        return False


def _get(body, path):
    node = body
    for key in path:
        node = node[key]
    return node


def validate(
    orig: dict, out: dict, changed: dict, protected_from: int, adapter, allow_thinking_binding: bool = False
) -> None:
    """`changed` maps content path -> (rule, source, unit index, original text, new text)."""
    skip = set(changed)
    if allow_thinking_binding:
        skip.add(("thinking",))
        a, b = dict(orig.get("thinking") or {}), dict(out.get("thinking") or {})
        a.pop("block_binding", None)
        b.pop("block_binding", None)
        if a != b:
            raise ValidationError("thinking parameter changed beyond block_binding")
    prefixes = {path[:i] for path in skip for i in range(len(path))}
    _walk(orig, out, (), skip, prefixes)

    for path, (rule, source, unit, orig_text, new_text) in changed.items():
        try:
            before, after = _get(orig, path), _get(out, path)
            container = _get(orig, path[:-1])
        except (KeyError, IndexError, TypeError):
            raise ValidationError(f"{path}: content path missing") from None
        if isinstance(container, dict) and container.get("is_error"):
            raise ValidationError(f"{path}: error results are never rewritten")
        text = adapter.content_text(after)
        if not text or not text.strip() or text != new_text:
            raise ValidationError(f"{path}: invalid replacement content")
        if rule == "images":
            # The stub replaces image blocks with text, so it is longer than the original text alone;
            # what must hold is that there were images, and that every original text block survives.
            if not isinstance(before, list) or not any(is_base64_image(b) for b in before):
                raise ValidationError(f"{path}: images rule on content without images")
            for b in before:
                if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")) not in text:
                    raise ValidationError(f"{path}: images rule dropped a text block")
        elif orig_text is None or len(text) > len(orig_text):
            raise ValidationError(f"{path}: replacement is not smaller")
        if count_cache_markers(before) != count_cache_markers(after):
            raise ValidationError(f"{path}: cache_control markers changed")
        if source == "new":
            if unit >= protected_from and rule not in INGEST_RULES:
                raise ValidationError(f"{path}: epoch rule {rule} inside the protected turns")
            if rule == "json" and not _same_json(orig_text, text):
                raise ValidationError(f"{path}: json minification changed a value")
            if rule == "json_trim" and not json_trim_ok(orig_text, text):
                raise ValidationError(f"{path}: json_trim changed more than long strings, or dropped an error line")
            if rule in ERROR_PRESERVING_RULES:
                kept = {line.strip() for line in text.split("\n")}
                lines = normalize_terminal(orig_text).split("\n")
                for i in error_line_indices(lines):
                    if lines[i].strip() and lines[i].strip() not in kept:
                        raise ValidationError(f"{path}: error line dropped by {rule}")
