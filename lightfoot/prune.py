"""Old-turn pruning: replace the oldest turns of a long conversation with a summary.

This is where most of the cost of a long Claude Code session is: every turn re-reads
the whole conversation from the cache. Pruning shrinks that, so it is built around
quality guards rather than savings:

  * It only happens when the user sends a new prompt, never while Claude is mid-task
    in a tool loop, so no in-progress work state is ever cut.
  * The first message, the last `min_kept_prompts` prompts with all their work, and at
    least `keep_tokens` of recent conversation are always kept as they are.
  * The summary is extractive and deterministic (no model call, no cost, no delay): the
    user's requests verbatim, Claude's closing replies, files read and edited, commands,
    errors, subagent reports and the latest todo list.
  * The full text of the pruned turns, with every tool result, is written to
    .lightfoot/history/, and the summary names the file so Claude can read any detail.
  * A prune is frozen like every rewrite: later requests replay the same summary over
    the same span (confirmed by a fingerprint), so the cache stays warm.

With `[prune] summarizer = "model"` (opt-in, live mode only), the summary is written by
the model instead, through the API's on-demand compaction (beta compact-2026-09-04): one
extra request summarizes messages[:boundary] and returns a signed `compaction` block that
replaces them. Thinking in the kept turns stays valid, so enforcing accounts need no
drop_block. The request runs in the background and the block takes effect at the next
user prompt; any failure falls back to the extractive summary.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import images, pricing
from .adapters.base import count_cache_markers
from .config import private_dir
from .profiles import CLAUDE_CODE, Profile
from .text import tokens

RULE = "prune"
COMPACT_BETA: str = pricing.DATA["anthropic"]["compact_beta"]
# Models that support on-demand compaction, from lightfoot/data/models.toml. Matched by prefix,
# so dated and suffixed ids count too.
COMPACTION_MODELS: tuple[str, ...] = tuple(pricing.DATA["anthropic"]["compaction_models"])
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# Names a piece of code defines, in the common languages: functions, classes, types, constants.
_DEFINES_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:pub(?:\([\w:]+\))?[ \t]+)?(?:async[ \t]+)?"
    r"(?:def|class|function|func|fn|struct|enum|trait|interface|type|const|let|var|impl)[ \t]+"
    r"(?:\([^)]*\)[ \t]*)?([A-Za-z_]\w*)"
    r"|^([A-Z][A-Z0-9_]{3,})[ \t]*[:=]",
    re.M,
)
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\w.~-]*[\\/][\w.\\/~-]+")
TODO_TOOLS = {"TodoWrite", "todowrite"}  # Claude Code, opencode


# --- structure ------------------------------------------------------------------


def _blocks(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _prompt_text(message: dict) -> str:
    """The user's own words in a message: text blocks without system reminders."""
    parts = []
    for b in _blocks(message):
        if b.get("type") == "text":
            text = _REMINDER_RE.sub("", str(b.get("text", ""))).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def newest(msgs: list) -> dict | None:
    """The newest message that isn't a system message, or None.

    Claude Code 2.1.310 and later put system messages inside the conversation, and often end a
    request with one (a token-budget note after the tool results). What the model is answering
    is the message before them.
    """
    for message in reversed(msgs):
        if isinstance(message, dict) and message.get("role") != "system":
            return message
    return None


def is_prompt(message) -> bool:
    """A user turn that starts new work: user text, and no tool results (so no tool loop is open)."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    blocks = _blocks(message)
    if any(b.get("type") == "tool_result" for b in blocks):
        return False
    return bool(_prompt_text(message))


def fingerprint(msgs: list, upto: int) -> str:
    """Cheap, stable identity of msgs[:upto]: roles, block types, ids and text shape."""
    h = hashlib.sha256()
    for m in msgs[:upto]:
        h.update(str(m.get("role")).encode())
        for b in _blocks(m):
            kind = b.get("type", "")
            h.update(kind.encode())
            if kind == "text":
                t = str(b.get("text", ""))
                h.update(f"{len(t)}|{t[:96]}|{t[-96:]}".encode("utf-8", "surrogatepass"))
            elif kind == "tool_use":
                h.update(str(b.get("id")).encode())
            elif kind == "tool_result":
                h.update(f"{b.get('tool_use_id')}|{b.get('is_error')}".encode())
            elif kind in ("thinking", "redacted_thinking"):
                h.update(str(b.get("signature") or b.get("data") or "")[:64].encode())
        h.update(b"\x00")
    return h.hexdigest()[:32]


def message_tokens(message: dict) -> int:
    """Tokens of one message: text, tool inputs and tool results."""
    total = 0
    for b in _blocks(message):
        kind = b.get("type")
        if kind == "text":
            total += tokens(str(b.get("text", "")))
        elif kind == "tool_use":
            total += tokens(json.dumps(b.get("input"), ensure_ascii=False))
        elif kind == "tool_result":
            content = b.get("content")
            if isinstance(content, str):
                total += tokens(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        total += images.image_tokens(c) if c.get("type") == "image" else tokens(str(c.get("text", "")))
        elif kind == "thinking":
            total += tokens(str(b.get("thinking", "")))
        elif kind == "image":
            total += images.image_tokens(b)
    return total


def choose_boundary(
    msgs: list, sizes: list[int], scale: float, keep_tokens: int, min_kept_prompts: int, after: int = 1
) -> int | None:
    """Index B of the prompt where the kept conversation starts (msgs[0] and msgs[B:] are kept).

    The earliest prompt boundary whose tail fits in keep_tokens, but never one that keeps
    fewer than min_kept_prompts prompts. None if no boundary leaves anything to prune.
    """
    prompts = [i for i, m in enumerate(msgs) if i > max(1, after) and is_prompt(m)]
    candidates = prompts[: max(0, len(prompts) - min_kept_prompts + 1)]
    if not candidates:
        return None
    suffix = [0] * (len(msgs) + 1)
    for i in range(len(msgs) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + sizes[i]
    for b in candidates:
        if suffix[b] * scale <= keep_tokens:
            return b
    return candidates[-1]


# --- summary ---------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return (
        text
        if len(text) <= limit
        else text[:limit].rstrip() + f" … [{len(text) - limit} more characters in the history file]"
    )


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _input_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _input_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _input_strings(v)]
    return []


def _collect(msgs: list, start: int, end: int, profile: Profile = CLAUDE_CODE) -> dict:
    """Gather the facts the summary keeps from the pruned turns: requests, closing replies,
    files, commands, searches, errors, subagent reports and the latest todo list.

    Tools are recognised by the agent's profile (read, edit, shell, search, subagent), so
    the same summary works for every agent lightfoot supports.
    """
    calls: dict[str, tuple[str, dict]] = {}
    out = {
        "prompts": [],
        "replies": [],
        "edited": {},
        "read": {},
        "commands": [],
        "searches": [],
        "subagents": [],
        "errors": [],
        "todo": None,
        "other_tools": {},
        "defined": {},  # edited path -> names its new code defines
        "command_paths": {},  # file paths used in shell commands, in first-use order
        "system": [],  # distinct system message texts, newest last
    }
    last_reply = None
    for i in range(start, end):
        m = msgs[i]
        if m.get("role") == "assistant":
            for b in _blocks(m):
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    last_reply = str(b["text"])
                elif b.get("type") == "tool_use":
                    name, inp = str(b.get("name", "")), b.get("input") if isinstance(b.get("input"), dict) else {}
                    calls[str(b.get("id"))] = (name, inp)
                    kind, path = profile.kind(name, inp), profile.path(name, inp)
                    if kind == "edit" and path:
                        out["edited"][path] = out["edited"].get(path, 0) + 1
                        names = out["defined"].setdefault(path, [])
                        for code in _input_strings(inp):
                            for match in _DEFINES_RE.finditer(code):
                                defined = match.group(1) or match.group(2)
                                if defined not in names:
                                    names.append(defined)
                    elif kind == "read" and path:
                        out["read"][path] = out["read"].get(path, 0) + 1
                    elif kind == "shell":
                        command = profile.command(name, inp)
                        out["commands"].append([command, None])
                        for token in _PATH_RE.findall(command):
                            token = token.strip(".")
                            if len(token) >= 4 and "//" not in token:
                                out["command_paths"].setdefault(token, None)
                    elif kind == "search":
                        out["searches"].append(profile.search_text(name, inp))
                    elif name in TODO_TOOLS and isinstance(inp.get("todos"), list):
                        out["todo"] = inp["todos"]
                    elif kind != "subagent":
                        out["other_tools"][name] = out["other_tools"].get(name, 0) + 1
        elif m.get("role") == "system":
            # Claude Code 2.1.310+ puts its environment block and notes here; repeats keep their newest place.
            text = "\n".join(str(b.get("text", "")) for b in _blocks(m) if b.get("type") == "text").strip()
            if text:
                if text in out["system"]:
                    out["system"].remove(text)
                out["system"].append(text)
        elif m.get("role") == "user":
            if is_prompt(m):
                if last_reply is not None:  # request 0 is the (kept) first message
                    out["replies"].append((len(out["prompts"]), last_reply))
                last_reply = None
                out["prompts"].append(_prompt_text(m))
            for b in _blocks(m):
                if b.get("type") != "tool_result":
                    continue
                name, inp = calls.get(str(b.get("tool_use_id")), ("", {}))
                text = _result_text(b)
                kind = profile.kind(name, inp) if name else "other"
                if kind == "subagent":
                    out["subagents"].append(
                        (str(inp.get("description") or inp.get("subagent_type") or "subagent"), text)
                    )
                if kind == "shell" and out["commands"] and out["commands"][-1][0] == profile.command(name, inp):
                    out["commands"][-1][1] = "error" if b.get("is_error") else "ok"
                if b.get("is_error"):
                    first = next((ln for ln in text.splitlines() if ln.strip()), "")
                    out["errors"].append((name or "tool", first))
    if last_reply is not None:
        out["replies"].append((len(out["prompts"]), last_reply))
    return out


def build_summary(
    msgs: list,
    end: int,
    total_messages: int,
    history_path: str | None,
    max_tokens: int,
    profile: Profile = CLAUDE_CODE,
) -> str:
    """Deterministic summary of msgs[1:end] within roughly max_tokens.

    Renders with generous limits, then lowers them one section at a time, least important
    first, until it fits. The user's own requests are shortened last.
    """
    data = _collect(msgs, 1, end, profile)
    where = (
        f" The full text of those turns, including every tool result, is in {history_path} - read it if you need"
        " an exact detail."
        if history_path
        else ""
    )
    header = (
        f"[lightfoot: to keep this long session fast, the earlier part of the conversation (messages 2-{end} of "
        f"{total_messages}) is condensed below. The first message and everything after this summary are unchanged."
        f"{where} Re-read a file before editing it if you have not read it since this point.]"
    )

    def render(limits: dict) -> str:
        parts = [header]
        if data["system"] and limits["system_notes"]:
            parts.append("## System messages in this part (verbatim, newest last)")
            parts += [f"- {_clip(t, limits['system_note'])}" for t in data["system"][-limits["system_notes"] :]]
        prompts = data["prompts"]
        if prompts:
            parts.append("## The user's requests in this part (verbatim, oldest first)")
            # The first and the most recent requests keep the most room; older middle ones are
            # shortened first, and only if that is not enough are the oldest of them counted
            # instead of listed (they are all in the history file).
            recent = set(range(max(1, len(prompts) - limits["recent_prompts"]), len(prompts))) | {0}
            middle = [i for i in range(len(prompts)) if i not in recent]
            hidden = set(middle[: max(0, len(middle) - limits["middle_prompts"])])
            for i, p in enumerate(prompts):
                if i in hidden:
                    if i == min(hidden):
                        parts.append(f"(requests {i + 1}-{max(hidden) + 1} are in the history file)")
                    continue
                parts.append(f"{i + 1}. {_clip(p, limits['prompt'] if i in recent else limits['prompt_old'])}")
        if data["replies"]:
            parts.append("## Your closing replies to those requests")
            parts += [
                f"- ({'the first message' if n == 0 else f'request {n}'}) {_clip(r, limits['reply'])}"
                for n, r in data["replies"][-limits["replies"] :]
            ]
        if data["todo"]:
            parts.append("## Your todo list at that point")
            parts += [
                f"- [{t.get('status', '?')}] {_clip(str(t.get('content', '')), 300)}"
                for t in data["todo"]
                if isinstance(t, dict)
            ]
        if data["edited"] or data["read"]:
            parts.append("## Files")
            if data["edited"]:
                parts.append(
                    "Edited or written: "
                    + ", ".join(
                        f"{p}" + (f" ({n}x)" if n > 1 else "")
                        for p, n in list(data["edited"].items())[: limits["files"]]
                    )
                )
            if data["read"]:
                parts.append("Read: " + ", ".join(list(data["read"])[: limits["files"]]))
            defined = [(p, n) for p, n in data["defined"].items() if n][: limits["files"]]
            if defined and limits["defined"]:
                parts.append(
                    "Names your edits defined: "
                    + "; ".join(f"{p}: {', '.join(n[: limits['defined']])}" for p, n in defined)
                )
        if data["command_paths"] and limits["command_paths"]:
            listed = set(data["edited"]) | set(data["read"])
            paths = [p for p in data["command_paths"] if p not in listed]
            if paths:
                parts.append("Paths used in commands: " + ", ".join(paths[-limits["command_paths"] :]))
        if data["commands"]:
            parts.append("## Commands run")
            shown = data["commands"][-limits["commands"] :]
            if len(data["commands"]) > len(shown):
                parts.append(f"({len(data['commands']) - len(shown)} earlier commands are in the history file)")
            parts += [
                f"- `{_clip(c.replace(chr(10), ' '), 200)}`" + (" (failed)" if s == "error" else "") for c, s in shown
            ]
        if data["searches"]:
            parts.append("## Searches: " + ", ".join(_clip(s, 80) for s in data["searches"][-limits["commands"] :]))
        if data["subagents"]:
            parts.append("## Subagent reports")
            parts += [f"- {d}: {_clip(t, limits['subagent'])}" for d, t in data["subagents"][-limits["subagents"] :]]
        if data["errors"]:
            parts.append("## Errors seen")
            parts += [f"- {n}: {_clip(e, 240)}" for n, e in data["errors"][-limits["errors"] :]]
        if data["other_tools"]:
            parts.append("## Other tools used: " + ", ".join(f"{k} ({v}x)" for k, v in data["other_tools"].items()))
        return "\n".join(parts)

    limits = {
        "prompt": 4000,
        "prompt_old": 1500,
        "recent_prompts": 12,
        "middle_prompts": 10_000,
        "reply": 2500,
        "replies": 40,
        "files": 120,
        "commands": 60,
        "subagent": 1500,
        "subagents": 12,
        "errors": 25,
        "defined": 25,
        "command_paths": 80,
        "system_notes": 6,
        "system_note": 2000,
    }
    floors = {
        "prompt": 400,
        "prompt_old": 120,
        "recent_prompts": 3,
        "middle_prompts": 0,
        "reply": 200,
        "replies": 4,
        "files": 20,
        "commands": 8,
        "subagent": 200,
        "subagents": 3,
        "errors": 5,
        "defined": 0,
        "command_paths": 0,
        "system_notes": 1,
        "system_note": 300,
    }
    text = render(limits)
    # Shrink the least important parts first; the user's own words last.
    order = (
        "subagent",
        "commands",
        "errors",
        "system_notes",
        "system_note",
        "command_paths",  # compact, so kept longer than the verbose command list (measured)
        "defined",
        "files",
        "reply",
        "replies",
        "subagents",
        "prompt_old",
        "middle_prompts",
        "recent_prompts",
        "prompt",
    )
    for key in order:
        if tokens(text) <= max_tokens:
            break
        if key == "middle_prompts":
            limits[key] = min(limits[key], len(data["prompts"]))
        while tokens(text) > max_tokens and limits[key] > floors[key]:
            limits[key] = max(floors[key], limits[key] // 2)
            text = render(limits)
    return text


# --- history file and application ------------------------------------------------


def write_history(path: Path, msgs: list, end: int, session_id: str) -> None:
    """Full text of msgs[1:end] as Markdown, so nothing pruned is lost."""
    if path.exists():
        return
    if path.parent.parent.name == ".lightfoot":
        private_dir(path.parent.parent)
    private_dir(path.parent)
    data_dir = path.parent.parent
    if data_dir.name == ".lightfoot" and not (data_dir / ".gitignore").exists():
        (data_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    lines = [
        f"# Condensed history of session {session_id}",
        "",
        f"Messages 2-{end}, removed from the live context by lightfoot. Written {time.strftime('%Y-%m-%d %H:%M')}.",
        "",
    ]
    for i in range(1, end):
        m = msgs[i]
        lines.append(f"## Message {i + 1} ({m.get('role')})")
        for b in _blocks(m):
            kind = b.get("type")
            if kind == "text":
                lines.append(str(b.get("text", "")))
            elif kind == "tool_use":
                lines.append(f"**Tool call `{b.get('name')}`** (id {b.get('id')})")
                lines.append("```json\n" + json.dumps(b.get("input"), indent=2, ensure_ascii=False) + "\n```")
            elif kind == "tool_result":
                label = "error result" if b.get("is_error") else "result"
                lines.append(f"**Tool {label}** (for {b.get('tool_use_id')})")
                lines.append("```\n" + _result_text(b) + "\n```")
                content = b.get("content")
                inner = content if isinstance(content, list) else []
                lines.extend(
                    _image_line(c, path.parent.parent / "images")
                    for c in inner
                    if isinstance(c, dict) and c.get("type") == "image"
                )
            elif kind == "image":
                lines.append(_image_line(b, path.parent.parent / "images"))
        lines.append("")
    tmp = path.with_name(f"{path.name}.{time.time_ns()}.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8", errors="replace")
    tmp.replace(path)


def _image_line(block: dict, folder: Path) -> str:
    """History-file line for an image: where it was saved, or that it couldn't be."""
    try:
        saved = images.save(block, folder)
    except OSError:
        saved = None
    if saved is None:
        return "*(image: not saved; it was not inline image data)*"
    return f"*(image, {images.describe(block)}: saved to {saved})*"


def summary_message(summary: str) -> dict:
    """The assistant turn that replaces the pruned messages."""
    return {"role": "assistant", "content": [{"type": "text", "text": summary}]}


def compaction_message(block_json: str) -> dict:
    """The assistant turn holding the API's signed compaction block, from its stored JSON."""
    return {"role": "assistant", "content": [json.loads(block_json)]}


def apply(body: dict, boundary: int, summary: str, kind: str = "extractive") -> dict:
    """Copy of body with messages[1:boundary] replaced by one summary turn.

    A compaction prune replaces messages[:boundary], the first message included: the API
    requires the block to come first.
    """
    msgs = body["messages"]
    out = dict(body)
    if kind == "compaction":
        out["messages"] = [compaction_message(summary), *msgs[boundary:]]
    else:
        out["messages"] = [msgs[0], summary_message(summary), *msgs[boundary:]]
    return out


def validate(base: dict, out: dict, boundary: int, summary: str, kind: str = "extractive") -> None:
    """The pruned body keeps base exactly except for the condensed span."""
    from .validate import ValidationError

    bm, om = base["messages"], out["messages"]
    if set(base) != set(out) or any(out[k] is not base[k] and out[k] != base[k] for k in base if k != "messages"):
        raise ValidationError("prune changed a field other than messages")
    if not (2 <= boundary < len(bm)) or not is_prompt(bm[boundary]):
        raise ValidationError("prune boundary is not a user prompt")
    if kind == "compaction":
        content = om[0].get("content") if om else None
        block = content[0] if isinstance(content, list) and content else {}
        if len(om) != len(bm) - boundary + 1 or block.get("type") != "compaction" or not block.get("signature"):
            raise ValidationError("compaction prune does not start with one signed compaction block")
        if bm[boundary - 1].get("role") == bm[boundary].get("role"):
            raise ValidationError("compaction cut would merge the first kept message into the summary")
        if any(a is not b for a, b in zip(om[1:], bm[boundary:], strict=True)):
            raise ValidationError("prune changed a kept message")
        return
    if len(om) != len(bm) - boundary + 2 or om[0] is not bm[0]:
        raise ValidationError("prune did not keep the first message")
    if om[1] != summary_message(summary) or not summary.strip():
        raise ValidationError("prune summary turn is malformed")
    if any(a is not b for a, b in zip(om[2:], bm[boundary:], strict=True)):
        raise ValidationError("prune changed a kept message")


# --- model-written summary (on-demand compaction) ---------------------------------------

# Parameters the API rejects on a compaction request, or that do nothing there.
_NOT_FOR_COMPACTION = ("context_management", "stop_sequences", "stream")


def supports_compaction(model: str | None) -> bool:
    """Whether the model supports on-demand compaction (model-written summaries)."""
    name = (model or "").lower().split("[", 1)[0]
    return name.startswith(COMPACTION_MODELS)


def compaction_request(body: dict, boundary: int) -> dict:
    """The request that asks the API to summarize messages[:boundary] of `body`.

    The same model, system prompt, tools and thinking settings as the conversation, so the
    kept turns' thinking stays valid. A cache marker on the last summarized message lets the
    request read the conversation's cached prefix instead of paying for it in full.
    """
    out = {k: v for k, v in body.items() if k not in _NOT_FOR_COMPACTION}
    choice = out.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in ("any", "tool"):
        out.pop("tool_choice")
    config = out.get("output_config")
    if isinstance(config, dict) and "format" in config:
        out["output_config"] = {k: v for k, v in config.items() if k != "format"}
    thinking = out.get("thinking")
    if isinstance(thinking, dict) and "block_binding" in thinking:
        out["thinking"] = {k: v for k, v in thinking.items() if k != "block_binding"}
    msgs = json.loads(json.dumps(body["messages"][:boundary]))
    if count_cache_markers(body.get("system")) + count_cache_markers(body.get("tools")) + count_cache_markers(msgs) < 4:
        _mark_last(msgs[-1], _ttl(body))
    out["messages"] = msgs
    out["max_tokens"] = min(int(body.get("max_tokens") or 16000), 32000)
    out["compaction"] = {"type": "summarize"}
    return out


def _mark_last(message: dict, ttl: str) -> None:
    if isinstance(message.get("content"), str):
        message["content"] = [{"type": "text", "text": message["content"]}]
    blocks = [b for b in message.get("content") or [] if isinstance(b, dict)]
    if blocks and "cache_control" not in blocks[-1] and blocks[-1].get("type") not in ("thinking", "redacted_thinking"):
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": ttl}


def _ttl(body: dict) -> str:
    """The conversation's own cache TTL, so the marker reads the entries its requests wrote."""
    marks = json.dumps([body.get("system"), body.get("tools"), body.get("messages", [])[-2:]])
    return "1h" if '"ttl": "1h"' in marks else "5m"


def compaction_block(response: dict) -> dict | None:
    """The signed block of a successful compaction response, or None (refusal, cut off, no summary)."""
    if not isinstance(response, dict) or response.get("stop_reason") != "compaction":
        return None
    blocks = [b for b in response.get("content") or [] if isinstance(b, dict) and b.get("type") == "compaction"]
    if len(blocks) != 1 or not blocks[0].get("signature") or not str(blocks[0].get("content") or "").strip():
        return None
    return blocks[0]
