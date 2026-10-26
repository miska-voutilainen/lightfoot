"""Protocol-neutral view of a request, shared by every adapter.

An adapter turns a provider's request body into an `Index`: the conversation's units
(Anthropic messages, OpenAI input items), the tool calls, and every tool output with
the JSON path of its content. The core engine only ever replaces content at those
paths, so one generic `apply_changes` and one generic validator serve all protocols.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..images import image_tokens
from ..text import tokens

Path = tuple  # JSON path into the request body, e.g. ("messages", 4, "content", 0, "content")


def encode(text: str) -> bytes:
    """UTF-8 bytes of `text`, keeping lone surrogates that some clients send."""
    return text.encode("utf-8", "surrogatepass")


def canonical(value) -> str:
    """Deterministic JSON for hashing and comparison: sorted keys, ASCII only, no spaces."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def content_hash(shape: str, is_error: bool, text: str) -> str:
    """Hash of what a tool output says, ignoring cache_control placement (markers move between turns)."""
    return hashlib.sha256(f"{shape}|{int(is_error)}|".encode() + encode(text)).hexdigest()


@dataclass
class ToolCall:
    """A tool call the model made, by id; results look up their call to learn name, input and kind."""

    name: str
    inp: dict
    kind: str
    unit: int


@dataclass
class Result:
    """One tool output in the request: the only kind of content a rule may rewrite.

    `orig_text` is what the client sent; `text` is set when a stored or new rewrite applies.
    Rewrites are keyed by `tid` and checked against `orig_hash`, so a replay never lands on
    output that has changed since it was frozen.
    """

    unit: int
    path: Path  # where the content lives
    tid: str  # stable id used for frozen replay (tool_use_id / call_id)
    container: dict  # the dict whose content field is replaced
    name: str
    inp: dict
    kind: str  # read | search | shell | edit | subagent | graph_query | other
    turn: int
    shape: str | None  # "str" | "list" | None (not rewritable)
    orig_text: str | None
    is_error: bool
    text: str | None = None  # current text when a rewrite applies
    rule: str | None = None
    note_path: str | None = None
    source: str | None = None  # "stored" | "new"
    media: list = field(default_factory=list)  # inline image blocks (shape "image" only)
    _hash: str | None = field(default=None, repr=False)

    @property
    def orig_hash(self) -> str | None:
        """Content hash, computed only when needed (replay checks, new rewrites, repeat detection)."""
        if self._hash is None and self.shape is not None:
            text = self.orig_text or ""
            if self.media:
                # Screenshots often carry no text at all, so the image data must be part of the hash.
                text += "\x00" + "".join(hashlib.sha256(encode(b["source"]["data"])).hexdigest() for b in self.media)
            self._hash = content_hash(self.shape, self.is_error, text)
        return self._hash

    @property
    def current(self) -> str | None:
        """The text this result will carry: the rewrite if one applies, else the original."""
        return self.text if self.text is not None else self.orig_text

    @property
    def orig_tokens(self) -> int:
        """Tokens of the original content, images included at their real cost."""
        return tokens(self.orig_text or "") + sum(image_tokens(b) for b in self.media)

    @property
    def current_tokens(self) -> int:
        """Tokens of `current`, images included at their real cost while unrewritten."""
        return tokens(self.text) if self.text is not None else self.orig_tokens

    @property
    def rewritable(self) -> bool:
        """Whether any rule may touch this result: a known shape, not an error, and a stable id."""
        return self.shape is not None and not self.is_error and bool(self.tid)


@dataclass
class Index:
    """Everything the planner needs to know about a request, independent of its wire format.

    `ingest_from` splits history (seen by the model, so editing it is a history edit) from
    new content (free to rewrite). `reasoning_units` lets the planner tell whether a history
    edit would land before a thinking block, whose signature binds everything before it.
    """

    units: list
    results: list[Result]
    calls: dict[str, ToolCall]
    unit_turn: list[int]
    turns: int
    ingest_from: int  # first unit the model has not seen yet
    model_from: int  # first unit of the newest model turn (for signals)
    reasoning_units: set[int] = field(default_factory=set)
    pruned_before: int = 0  # units before this are replaced by a stored prune summary

    def reasoning_after(self, unit: int) -> bool:
        """Whether a thinking or reasoning unit comes after `unit`, so editing `unit` would break its signature."""
        return any(u > unit for u in self.reasoning_units)


def apply_changes(body: dict, changes: dict[Path, object]) -> dict:
    """Copy of `body` with each path replaced; everything not on a changed path is shared."""
    out = dict(body)
    copied: dict[tuple, object] = {(): out}
    for path, value in changes.items():
        node = out
        for depth in range(len(path) - 1):
            prefix = path[: depth + 1]
            if prefix not in copied:
                child = node[path[depth]]
                child = list(child) if isinstance(child, list) else dict(child)
                node[path[depth]] = child
                copied[prefix] = child
            node = copied[prefix]
        node[path[-1]] = value
    return out


def count_cache_markers(value) -> int:
    """How many `cache_control` markers `value` holds, at any depth.

    The validator requires the same count after a rewrite.
    """
    if isinstance(value, dict):
        return ("cache_control" in value) + sum(
            count_cache_markers(v) for k, v in value.items() if k != "cache_control"
        )
    if isinstance(value, list):
        return sum(count_cache_markers(v) for v in value)
    return 0


class Adapter:
    """Interface every protocol adapter implements."""

    name = "base"
    units_key = ""
    epochs_supported = False  # history edits verified safe for this provider
    supports_prune = False  # old-turn pruning (a history edit, so it needs the same verification)
    supports_drop_block = False
    experimental = False

    def accepts(self, body) -> bool:
        """Whether this adapter can index `body`: a dict whose `units_key` holds a list."""
        return isinstance(body, dict) and isinstance(body.get(self.units_key), list)

    def index(self, body: dict, profile) -> Index:
        """Build the protocol-neutral `Index` of a request, using `profile` to give each tool call its kind."""
        raise NotImplementedError

    def extract(self, container: dict) -> tuple[str | None, str | None]:
        """Shape and plain text of a tool output's content, or (None, None) when it can't be rewritten."""
        raise NotImplementedError

    def content_value(self, container: dict, shape: str, text: str):
        """The value to store in `container`'s content field for rewritten `text`, in the original's `shape`."""
        raise NotImplementedError

    def content_text(self, value) -> str | None:
        """Plain text of a stored content value, or None when it isn't plain text."""
        raise NotImplementedError

    def unit_text(self, ui: int, unit: dict, current: dict[Path, str]) -> str:
        """Plain text of unit `ui` for "referenced since" checks; `current` maps result path -> text."""
        raise NotImplementedError

    def unit_tokens(self, ui: int, unit: dict, current: dict[Path, str]) -> int:
        """Tokens of unit `ui`, counting rewritten results from `current` (result path -> text)."""
        raise NotImplementedError

    def session_key(self, body: dict) -> str:
        """A stable id for the conversation `body` belongs to, the same on every one of its turns."""
        raise NotImplementedError

    def cache_ttl(self, body: dict) -> int:
        """Seconds the provider keeps this request's prompt cache; 300 unless the protocol says otherwise."""
        return 300

    def model(self, body: dict) -> str | None:
        """The model named in the request body, if any."""
        return body.get("model") if isinstance(body.get("model"), str) else None

    def with_drop_block(self, body: dict) -> dict:
        """`body` with the provider told to drop thinking that no longer matches its prefix.

        A no-op for providers without that option.
        """
        return body

    def usage_parser(self, content_type: str | None, request_body: dict | None):
        """A parser that reads token usage from this protocol's response, streamed or not."""
        raise NotImplementedError

    def prefix(self, body: dict) -> tuple[list[str], list[tuple[str, object]]]:
        """The fixed prefix every request of a session repeats: system texts, and (name, definition) per tool."""
        return [], []
