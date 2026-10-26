"""Quality guards: when Claude keeps going back for condensed content, condense less.

Signals (recorded by the planner, live sessions only, since in shadow mode the model sees
everything):

| Signal | Means | Acts on |
| --- | --- | --- |
| `history_read` | a history file was read: a pruned detail was needed | pruning |
| `re_read` | a file was re-read soon after its old read was stubbed | pruning |
| `retrieval` | after a prune, reads and searches per turn more than doubled | pruning |
| `spill_read` | a saved full output was read | trimming, for that tool |
| `re_run` | a command whose output was trimmed was run again | trimming, for that tool |

Pruning escalates per session: normal → watch (one signal) → keep more (`keep_more` signals
since the latest prune: the next prune keeps twice the tokens) → stop (as many again after a
keep-more prune: no new prunes or epochs). Watch returns to normal after `quiet_turns` turns
without a signal. Trimming stops for one tool after `spill_off` signals from its output.

A guard never un-prunes: sending the original turns back would be another history edit (a
full cache rewrite, and invalid thinking on current models). It only changes what comes next.
"""

from __future__ import annotations

import copy
import json

LEVELS = ("normal", "watch", "keep more", "stop")
PRUNE_KINDS = {"history_read", "re_read", "retrieval"}
SPILL_KINDS = {"spill_read", "re_run"}
TRIM_RULES = ("spill", "json_trim", "old_results")


def key(session_id: str) -> str:
    """The `state` key holding a session's guard state."""
    return f"guard:{session_id}"


def new_state() -> dict:
    """A session's guard state before any signal: level 0, nothing switched off."""
    return {"level": 0, "since_prune": 0, "pruned_at": 0, "last_signal": 0, "spill": {}, "off": [], "pre_rate": None}


def load(store, session_id: str) -> dict:
    """A session's guard state, or a fresh one when none is stored or it can't be read."""
    try:
        state = json.loads(store.get_state(key(session_id)) or "null")
    except ValueError:
        state = None
    return {**new_state(), **state} if isinstance(state, dict) else new_state()


def adjust(cfg: dict, state: dict) -> dict:
    """The settings for this request: `cfg` itself when nothing applies, else a changed copy."""
    if state["level"] < 2 and not state["off"]:
        return cfg
    out = copy.deepcopy(cfg)
    if state["level"] >= 2:
        out["prune"]["keep_tokens"] = 2 * int(out["prune"]["keep_tokens"])
        if out["prune"].get("cold_keep_tokens"):
            out["prune"]["cold_keep_tokens"] = 2 * int(out["prune"]["cold_keep_tokens"])
    if state["level"] >= 3:
        out["prune"]["enabled"] = False
        out["epochs"]["enabled"] = False
    for rule in TRIM_RULES:
        out.setdefault("exclude", {})[rule] = [*out.get("exclude", {}).get(rule, []), *state["off"]]
    return out


def update(state: dict, signals: list[tuple[str, str]], turn: int, pruned: bool, limits: dict) -> list[str]:
    """Apply one request's signals (kind, tool) and prune to `state`; returns the steps taken, for events."""
    steps = []
    if pruned:
        state["pruned_at"] = state["level"]
        state["since_prune"] = 0
    for kind, tool in signals:
        state["last_signal"] = turn
        if kind in PRUNE_KINDS:
            state["since_prune"] += 1
            level = state["level"]
            if level == 0:
                level = 1
            if state["since_prune"] >= limits["keep_more"]:
                level = 3 if state["pruned_at"] >= 2 else max(level, 2)
            if level > state["level"]:
                state["level"] = level
                steps.append(f"{LEVELS[level]} (after {kind} at turn {turn})")
        elif kind in SPILL_KINDS and tool:
            state["spill"][tool] = state["spill"].get(tool, 0) + 1
            if state["spill"][tool] >= limits["spill_off"] and tool not in state["off"]:
                state["off"].append(tool)
                steps.append(f"stopped trimming {tool} output (after {kind} at turn {turn})")
    if state["level"] == 1 and not signals and turn - state["last_signal"] >= limits["quiet_turns"]:
        state["level"] = 0
        steps.append(f"normal (quiet since turn {state['last_signal']})")
    return steps


def save(store, session_id: str, state: dict) -> None:
    """Store a session's guard state."""
    store.set_state(key(session_id), json.dumps(state, sort_keys=True))
