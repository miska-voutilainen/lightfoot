"""`lightfoot estimate --agent gemini`: offline replay of Gemini CLI sessions.

Gemini CLI records each session under `~/.gemini/tmp/<project>/chats/` (subagents in a
subfolder named after their parent). The format was read from Gemini CLI's own
`chatRecordingService.ts` and `chatRecordingTypes.ts` (main, 2026-10-26) and checked
against public session files:

| File | Content |
| --- | --- |
| `session-*.jsonl` (current) | one record per line, replayed in order (below) |
| `session-*.json` (older) | one JSON object: `sessionId`, `projectHash`, `messages` |

| Record | Effect |
| --- | --- |
| `{sessionId, projectHash, …}` | session metadata (may carry `messages`) |
| a message (`id`, `type`, `content`, …) | added, or replaces the message with that id in place |
| `{"$patch": …}` | updates content or tool results by id; `removeIds`, `orderIds` |
| `{"$rewindTo": id}` | drops that message and everything after it |
| `{"$set": …}` | metadata; a `messages` list replaces the whole history (older versions) |

Message types are `user`, `gemini`, and `info`, `error` and `warning` (shown in the UI,
never sent to the model). A `gemini` message holds the model's text and its `toolCalls`,
each with `result`: the `functionResponse` parts sent back. `tokens` are that response's
usage: `input` is the whole prompt (cached tokens included), `tool` the tool-use prompt.

Each `gemini` message with tokens is one API call, sent with every turn before it. The
requests run through the real planner with the Gemini adapter and profile: the output
rules only, as live Gemini support does. Gemini bills no cache writes.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import pricing
from .adapters import GEMINI
from .codexlog import _counterfactual
from .config import DEFAULTS
from .estimate import _ts
from .profiles import GEMINI_CLI
from .store import Store
from .transform import Context, plan_request

UI_ONLY = {"info", "error", "warning"}  # message types Gemini CLI never sends to the model


def default_root() -> Path:
    """Where Gemini CLI keeps its sessions, `~/.gemini/tmp`."""
    return Path.home() / ".gemini" / "tmp"


def session_files(root: Path) -> list[Path]:
    """Every session file under `root` (a file is returned as is)."""
    if root.is_file():
        return [root]
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("session-*.json*") if p.suffix in (".json", ".jsonl") and "chats" in p.parts)


@dataclass
class GeminiCall:
    """One Gemini API call read back from a session: its model, request contents and token counts."""

    ts: float
    model: str
    contents: list
    counts: dict


@dataclass
class GeminiTotals:
    """What a Gemini replay adds up to: recorded and estimated cost, saving and rule firings."""

    files: int = 0
    calls: int = 0
    baseline: float = 0.0
    estimate: float = 0.0
    saved_tokens: int = 0
    rules: dict = field(default_factory=dict)
    models: set = field(default_factory=set)


def _records(text: str) -> list:
    """The file's records: the single object of an older `.json` file, else one per JSONL line.

    Like Gemini CLI's loader, a line that doesn't parse is skipped rather than failing the file.
    """
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    records = []
    for line in text.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _patch(message: dict, patch: dict) -> None:
    """Apply one `$patch` update to a message: new content, or new results for its tool calls."""
    if "content" in patch and patch["content"] is not None:
        message["content"] = patch["content"]
    calls = {tc.get("id"): tc for tc in message.get("toolCalls") or [] if isinstance(tc, dict)}
    for tc_patch in patch.get("toolCalls") or []:
        if isinstance(tc_patch, dict) and tc_patch.get("id") in calls and "result" in tc_patch:
            calls[tc_patch["id"]]["result"] = tc_patch["result"]


def load_messages(text: str) -> tuple[str, list[dict]]:
    """(session id, messages) after replaying every record the way Gemini CLI's loader does."""
    session, messages = "", {}
    for record in _records(text):
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("$rewindTo"), str):
            ids = list(messages)
            cut = ids.index(record["$rewindTo"]) if record["$rewindTo"] in ids else 0
            for message_id in ids[cut:]:
                del messages[message_id]
        elif isinstance(record.get("$patch"), dict):
            patch = record["$patch"]
            for update in [patch, *(patch.get("updates") or [])]:
                if isinstance(update, dict) and update.get("id") in messages:
                    _patch(messages[update["id"]], update)
            for message_id in patch.get("removeIds") or []:
                messages.pop(message_id, None)
            if isinstance(patch.get("orderIds"), list):
                ordered = [i for i in patch["orderIds"] if i in messages]
                rest = [i for i in messages if i not in set(ordered)]
                messages = {i: messages[i] for i in rest + ordered}
        elif isinstance(record.get("id"), str):
            messages[record["id"]] = dict(record)
        else:
            update = record.get("$set") if isinstance(record.get("$set"), dict) else record
            session = str(update.get("sessionId") or session)
            if isinstance(update.get("messages"), list):
                if "$set" in record:
                    messages = {}  # a legacy full-history checkpoint
                for message in update["messages"]:
                    if isinstance(message, dict) and isinstance(message.get("id"), str):
                        messages[message["id"]] = dict(message)
    return session, list(messages.values())


def _parts(content) -> list:
    """Gemini API parts for a recorded `content` (a string, a part, or a list of either)."""
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        return [p for c in content for p in _parts(c)]
    return []


def calls_in(path: Path):
    """Every API call of one session, with the contents it was sent with."""
    session, messages = load_messages(path.read_text(encoding="utf-8", errors="replace"))
    contents: list = []
    for message in messages:
        kind = message.get("type")
        if kind == "user":
            parts = _parts(message.get("content"))
            if parts:
                contents.append({"role": "user", "parts": parts})
        elif kind == "gemini":
            tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else None
            if tokens:
                prompt = int(tokens.get("input") or 0) + int(tokens.get("tool") or 0)
                cached = int(tokens.get("cached") or 0)
                counts = {
                    "input": max(0, prompt - cached),
                    "cw5": 0,
                    "cw1h": 0,
                    "cr": cached,
                    "output": int(tokens.get("output") or 0) + int(tokens.get("thoughts") or 0),
                    "session": session,
                }
                model = str(message.get("model") or "")
                yield GeminiCall(_ts(message.get("timestamp")), model, list(contents), counts)
            calls = [tc for tc in message.get("toolCalls") or [] if isinstance(tc, dict)]
            model_parts = _parts(message.get("content"))
            model_parts += [
                {
                    "functionCall": {
                        **({"id": tc["id"]} if isinstance(tc.get("id"), str) else {}),
                        "name": tc.get("name"),
                        "args": tc["args"],
                    }
                }
                for tc in calls
                if isinstance(tc.get("args"), dict)  # malformed recordings carry args as a string
            ]
            if model_parts:
                contents.append({"role": "model", "parts": model_parts})
            results = [p for tc in calls for p in _parts(tc.get("result")) if "functionResponse" in p]
            if results:
                contents.append({"role": "user", "parts": results})
        # info, error and warning messages are UI only


def estimate(files: list[Path], config: dict | None = None) -> GeminiTotals:
    """Replay Gemini CLI sessions through the planner with the output rules, in a throwaway store."""
    cfg = copy.deepcopy(config or DEFAULTS)
    cfg["rewrite_budget_ms"] = 10_000  # offline: timing budgets are not the question here, as in estimate.py
    cfg["epochs"]["budget_ms"] = 10_000
    totals = GeminiTotals(files=len(files))
    workdir = Path(tempfile.mkdtemp(prefix="lightfoot-gemini-"))
    store = Store(workdir / "gemini.db")
    try:
        for path in files:
            try:
                calls = list(calls_in(path))
            except OSError:
                continue
            for call in calls:
                # The session id stands in for the system prompt, which sessions don't record, so two
                # sessions that start with the same words stay apart.
                body = {
                    "contents": call.contents,
                    "systemInstruction": {"parts": [{"text": f"session {call.counts['session'] or path}"}]},
                }
                session = store.get_or_create_session(GEMINI.session_key(body), None, "live", call.ts)
                ctx = Context(
                    cfg=cfg,
                    store=store,
                    mode="live",
                    session=session,
                    spill_dir=workdir / "spill",
                    graphify_report=None,
                    enforced=False,
                    started=time.perf_counter(),
                    now=call.ts,
                    adapter=GEMINI,
                    profile=GEMINI_CLI,
                )
                plan = plan_request(body, ctx)
                store.put_rewrites(plan.new_rewrites)
                ingest = sum(plan.new_saved.get(r.tool_use_id, 0) for r in plan.new_rewrites)
                base = {k: call.counts[k] for k in ("input", "cw5", "cw1h", "cr", "output")}
                cf = _counterfactual(base, plan.replay_saved, ingest) if plan.body_out is not None else base
                totals.calls += 1
                totals.models.add(call.model)
                totals.baseline += pricing.effective_cost(
                    call.model, *(base[k] for k in ("input", "cw5", "cw1h", "cr", "output"))
                )
                totals.estimate += pricing.effective_cost(
                    call.model, *(cf[k] for k in ("input", "cw5", "cw1h", "cr", "output"))
                )
                totals.saved_tokens += plan.saved_tokens
                for rule, n in plan.rules.items():
                    if any(r.rule == rule for r in plan.new_rewrites):
                        totals.rules[rule] = totals.rules.get(rule, 0) + n
    finally:
        store.close()
        shutil.rmtree(workdir, ignore_errors=True)
    return totals


def render(t: GeminiTotals) -> str:
    """The `lightfoot estimate --agent gemini` report."""
    if not t.calls:
        return f"Gemini CLI: no API calls found in {t.files} session files."
    saving = 100 * (t.baseline - t.estimate) / t.baseline if t.baseline else 0.0
    unpriced = sorted(m for m in t.models if not pricing.priced(m))
    lines = [
        f"Gemini CLI · {t.files} sessions · {t.calls} API calls · models: {', '.join(sorted(t.models)) or '?'}",
        f"Recorded cost ${t.baseline:,.2f} · with Lightfoot's output rules ${t.estimate:,.2f} · saving {saving:.1f}%",
        "Rule firings: " + (", ".join(f"{k} ×{v}" for k, v in sorted(t.rules.items())) or "none"),
        "Gemini support runs the output rules only, until a live test shows that editing earlier output keeps "
        "Gemini's thought signatures valid; pruning isn't estimated here.",
    ]
    if unpriced:
        lines.append(f"No price for {', '.join(unpriced)}: those calls count as $0 here.")
    return "\n".join(lines)
