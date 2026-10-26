"""Configuration: one commented TOML template is the source of truth for defaults.

Load order: built-in defaults < ~/.lightfoot/config.toml < <project>/.lightfoot/config.toml

A project file comes with the repository, so it may not choose where requests (and the
credentials they carry) are sent, nor a command to run: those settings (GLOBAL_ONLY) are
read from the user's own file only.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

TEMPLATE = """\
# lightfoot configuration. Delete a line to fall back to its default.

# Mode for NEW sessions: "shadow" computes rewrites but forwards the original
# request (zero risk, logs projected savings); "live" forwards the rewrite.
# `lightfoot shadow on|off` overrides this without editing the file.
mode = "shadow"

# Where requests go. These, vault_path, handoff_folder and handoff_command are read from this
# file only: a project's .lightfoot/config.toml can't change them.
port = 8787
upstream = "https://api.anthropic.com"
openai_upstream = "https://api.openai.com"
gemini_upstream = "https://generativelanguage.googleapis.com"

# Stored rewrites, spill files and events of sessions idle this long are deleted
# (automatically once a day, or with `lightfoot cleanup`). The cost ledger holds no
# conversation content and is kept for ledger_retention_days.
retention_days = 30
ledger_retention_days = 365

# "api" reports dollars; "subscription" labels the same numbers as
# API-equivalent usage, which is what Max plan limits track.
billing = "api"

# New rewrites that take longer than this are dropped for the request
# (stored rewrites are still replayed, so the transcript stays frozen).
rewrite_budget_ms = 50

[rules]
json = true           # pretty-printed JSON output -> the same JSON without indentation (ingest, lossless)
json_trim = true      # long strings inside one line of JSON (Cline, MCP) -> head/tail/errors + spill file (ingest)
noise = true          # 1: strip ANSI codes, progress bars, repeated lines (ingest)
spill = true          # 2: large output -> head/tail/errors + spill file (ingest)
stale_reads = true    # 3: superseded file reads and graph queries (epoch)
searches = true       # 4: superseded / repeated searches (epoch)
old_results = true    # 5: old, unreferenced large results (epoch)
images = true         # older images in tool results -> a stub + saved image file (epoch)

[thresholds]
spill_tokens = 4000
spill_head = 60
spill_tail = 80
error_context = 2
search_age = 8
old_result_age = 10
old_result_tokens = 2000
protected_turns = 3
min_read_tokens = 200
json_min_saving_tokens = 50     # minify JSON only when it saves at least this much
json_trim_tokens = 1000         # json_trim: a string value larger than this is trimmed like spill
images_keep = 3                 # the newest results with images always stay whole

[epochs]
enabled = true
# Epochs are rare and mostly run after an idle gap, so they get their own budget.
budget_ms = 250
warm_check_every = 5            # with a warm cache, evaluate an epoch every Nth turn
min_saving_tokens = 2000        # cold cache: the rewrite is free, smaller gate
min_saving_tokens_warm = 10000  # warm cache: also must pass the break-even test
safety_factor = 2.0             # compact only if expected turns > factor * N*
min_gap_turns = 5
prune_near = 0.8               # no paid epoch once the conversation is this close to the prune trigger
# Accounts that enforce the thinking-prefix check reject edits that sit before
# a thinking block. "skip" leaves such regions alone; "drop_block" edits them
# and tells the API to drop the invalidated thinking blocks (gate it on eval).
enforced_accounts = "skip"
account_enforced = "auto"       # auto (learned from the API) | yes | no

[prune]
# Old-turn pruning: when the conversation (beyond the system prompt and tools) passes
# trigger_tokens, the oldest turns are replaced by a summary, keeping at least
# keep_tokens of recent conversation. It only happens when you send a new prompt
# (never mid-task), always keeps your first message and your last min_kept_prompts
# prompts with their work, and saves the full pruned text to .lightfoot/history/.
enabled = true
trigger_tokens = 200000
# With a cold cache (after an idle gap) the rewrite is free, so a prune may start from this
# lower size instead of waiting for a paid moment later; keep_tokens still applies.
# Measured on 30 days of real sessions: 28.5% -> 29.2% saving, summary coverage unchanged.
# 0 = always use trigger_tokens.
cold_trigger_tokens = 150000
# Prune earlier (at early_trigger_tokens, 0 = off) once the summary has held up: when the
# session's previous prune drew no signal that Claude needed pruned content (see [guards]),
# or when the model writes the summary on an API key. Opt-in until the paired benchmark
# shows quality holds; `lightfoot estimate` prices it.
early_trigger_tokens = 0
# Also prune inside a long tool loop, not only when a new prompt arrives: when the request
# ends with completed tool results (no call left open). The cut is still at an older prompt.
# Never on accounts that enforce the thinking check. Opt-in: the model sees the summary mid-task.
mid_loop = false
keep_tokens = 100000
# After an idle gap longer than the cache lifetime the prompt is re-written anyway, so a
# prune then can keep less at no extra cost. Keeps less verbatim history: opt-in, 0 = off.
cold_keep_tokens = 0
min_kept_prompts = 3
min_drop_tokens = 20000
max_summary_tokens = 8000
# Who writes the summary. "extractive": lightfoot, from the conversation itself (free, no
# delay). "model": the model, through the API's on-demand compaction; one extra request per
# prune, sent in the background and billed like any request, and the kept turns keep their
# thinking on accounts that enforce the thinking check. Live mode only; falls back to
# "extractive" on any failure, and on a Pro/Max login, which lightfoot never uses for a
# request of its own. Opt-in.
summarizer = "extractive"
safety_factor = 2.0
# Accounts that enforce the thinking-prefix check: "drop_block" prunes and lets the
# API drop the retained turns' earlier reasoning traces (as Claude Code's own /compact
# does); "skip" never prunes on such accounts.
enforced_accounts = "drop_block"

[cache]
# Cache lifetime for `lightfoot claude`'s main conversation: "" leaves Claude Code's own
# choice (one hour on a Pro/Max plan, five minutes with an API key), "1h" or "5m" sets
# CLAUDE_CODE_PROMPT_CACHE_TTL (Claude Code 2.1.242+). `lightfoot estimate --cache` shows
# which is cheaper on your own idle gaps.
ttl = ""

[guards]
# Live sessions only. When Claude keeps reading history files, re-reading stubbed files or
# doing more reads per turn after a prune, the next prune keeps twice as much (after
# keep_more signals since the latest prune), and if that isn't enough, pruning and epochs
# stop for the session. Reading a saved full output, or re-running a trimmed command with
# nothing but reads in between, spill_off times stops trimming that tool. Nothing already
# sent is undone. Details: `lightfoot why`.
enabled = true
keep_more = 2
spill_off = 2
quiet_turns = 20

[hooks]
# Hook mode (`lightfoot hook-mode install`): the json, spill and noise rules applied to
# Bash, PowerShell and MCP output by a Claude Code hook, without the proxy. Set to false to
# pause it for every project, or in one project's .lightfoot/config.toml.
enabled = true

[integrations]
graphify = "auto"               # auto | on | off
graphify_report = "graphify-out/GRAPH_REPORT.md"
obsidian = "auto"               # auto | on | off
vault_path = ""                 # absolute path of your Obsidian vault
handoff_folder = "Claude Handoffs"
handoff_model = "haiku"
handoff_min_turns = 5
# Agents without a session-end hook (everything but Claude Code): the proxy writes the
# note once a session has been idle this long, by running handoff_command with the
# prompt on stdin, e.g. "claude -p --model haiku". Empty: off. lightfoot holds no key for it.
idle_handoff_minutes = 30
handoff_command = ""

[exclude]
# Tool kinds (read, search, shell, edit, subagent, graph_query, other) or exact tool
# names whose results a rule never rewrites. File reads are never trimmed mid-file,
# and subagent reports are conclusions, not logs.
json = ["read", "edit", "subagent"]
noise = ["read", "edit"]
spill = ["read", "subagent"]
old_results = ["subagent"]

# Model price overrides or additions (USD per million tokens), for example:
# [prices."my-model"]
# input = 3.0
# output = 15.0
# read = 0.1           # cache read multiplier
# write_short = 1.25   # cache write multiplier, short TTL
# write_long = 2.0     # cache write multiplier, long TTL
"""

DEFAULTS: dict = tomllib.loads(TEMPLATE)

# Settings a project's .lightfoot/config.toml may not override: a cloned repository could
# otherwise send your API key to its own server, or run a command of its choosing.
GLOBAL_ONLY = {
    ("port",),
    ("upstream",),
    ("openai_upstream",),
    ("gemini_upstream",),
    ("integrations", "vault_path"),
    ("integrations", "handoff_folder"),
    ("integrations", "handoff_command"),
}


class ConfigError(Exception):
    """A config file that can't be read, or a setting that doesn't exist or has the wrong type."""

    pass


def private_dir(path: Path) -> Path:
    """Create a folder readable only by the user (on POSIX; Windows profiles are private already)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def lightfoot_home() -> Path:
    """Lightfoot's data folder: `LIGHTFOOT_HOME`, else `~/.lightfoot`."""
    return Path(os.environ.get("LIGHTFOOT_HOME") or (Path.home() / ".lightfoot"))


def global_config_path() -> Path:
    """The user's own config file, `~/.lightfoot/config.toml`."""
    return lightfoot_home() / "config.toml"


def project_config_path(project_dir: str | os.PathLike) -> Path:
    """A project's config file, `<project>/.lightfoot/config.toml`."""
    return Path(project_dir) / ".lightfoot" / "config.toml"


def _merge(base: dict, over: dict) -> dict:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _copy(value):
    """Deep copy of parsed TOML (dicts, lists and scalars), several times faster than copy.deepcopy."""
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _without_global_only(data: dict) -> dict:
    out = _copy(data)
    for path in GLOBAL_ONLY:
        node = out
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return out


_file_cache: dict[str, tuple[tuple, dict]] = {}
_merged_cache: dict[tuple, dict] = {}


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _read_toml(path: Path) -> dict:
    stamp = _stamp(path)
    if stamp is None:
        return {}
    cached = _file_cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        # utf-8-sig: Windows tools (PowerShell 5, older Notepad) often save with a BOM.
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    _file_cache[str(path)] = (stamp, data)
    return data


def load_config(project_dir: str | os.PathLike | None = None) -> dict:
    """The merged settings, as a fresh copy the caller may change.

    The proxy loads them for every request, so the merge is cached until either file changes.
    """
    global_path = global_config_path()
    project_path = project_config_path(project_dir) if project_dir else None
    key = (str(global_path), _stamp(global_path), str(project_path), project_path and _stamp(project_path))
    merged = _merged_cache.get(key)
    if merged is None:
        merged = _merge(_copy(DEFAULTS), _copy(_read_toml(global_path)))
        if project_path is not None:
            _merge(merged, _without_global_only(_read_toml(project_path)))
        if len(_merged_cache) > 256:
            _merged_cache.clear()
        _merged_cache[key] = merged
    return _copy(merged)


def _toml_value(raw: str, default):
    """Convert a command-line string to the TOML literal for a setting of `default`'s type."""
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low not in ("true", "false", "yes", "no", "on", "off", "1", "0"):
            raise ConfigError(f"expected true or false, got {raw!r}")
        return "true" if low in ("true", "yes", "on", "1") else "false"
    if isinstance(default, int):
        try:
            return str(int(raw.replace("_", "").replace(",", "")))
        except ValueError:
            raise ConfigError(f"expected a whole number, got {raw!r}") from None
    if isinstance(default, float):
        try:
            return repr(float(raw))
        except ValueError:
            raise ConfigError(f"expected a number, got {raw!r}") from None
    if isinstance(default, list):
        items = [x.strip() for x in raw.split(",") if x.strip()]
        return "[" + ", ".join(json.dumps(x) for x in items) + "]"
    return json.dumps(raw)  # a JSON string literal is a valid TOML basic string


def set_value(dotted_key: str, raw: str, path: Path | None = None) -> Path:
    """Set one known setting (e.g. integrations.vault_path) in the config file, keeping its comments."""
    section, _, key = dotted_key.rpartition(".")
    defaults = DEFAULTS.get(section, {}) if section else DEFAULTS
    if not isinstance(defaults, dict) or key not in defaults or isinstance(defaults[key], dict):
        raise ConfigError(f"unknown setting {dotted_key!r}")
    literal = _toml_value(raw, defaults[key])
    path = path or ensure_global_config()
    if not path.exists():
        private_dir(path.parent)
        path.write_text(TEMPLATE, encoding="utf-8")
    original = path.read_text(encoding="utf-8-sig")
    lines = original.splitlines()
    header = f"[{section}]" if section else None
    start = 0 if header is None else next((i + 1 for i, line in enumerate(lines) if line.strip() == header), None)
    assignment = re.compile(rf"^\s*{re.escape(key)}\s*=")
    if start is None:
        lines += ["", header, f"{key} = {literal}"]
    else:
        end = next((i for i in range(start, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        found = next((i for i in range(start, end) if assignment.match(lines[i])), None)
        if found is None:
            lines.insert(start, f"{key} = {literal}")
        else:
            comment = re.search(r"\s+#[^\"']*$", lines[found])
            lines[found] = f"{key} = {literal}" + (comment.group(0) if comment else "")
    updated = "\n".join(lines) + "\n"
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write an invalid config: {exc}") from exc
    path.write_text(updated, encoding="utf-8")
    _file_cache.pop(str(path), None)
    _merged_cache.clear()
    return path


def get_value(dotted_key: str, project_dir=None):
    """One setting by dotted key (`prune.trigger_tokens`), with project overrides applied."""
    cfg = load_config(project_dir)
    node = cfg
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"unknown setting {dotted_key!r}")
        node = node[part]
    return node


def ensure_global_config() -> Path:
    """The user's config file, created from the commented template on first use."""
    path = global_config_path()
    if not path.exists():
        private_dir(path.parent)
        path.write_text(TEMPLATE, encoding="utf-8")
    return path
