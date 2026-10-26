"""Watch whether other agents' after-tool hooks can replace tool output yet (hook mode).

Hook mode condenses tool output without the proxy, so it needs a hook that replaces the output
the model sees, and an agent that stores the replacement. On 2026-10-26 only Claude Code and
opencode had one. The others fell short:

| Agent | Its after-tool hook | Why not hook mode |
| --- | --- | --- |
| Gemini CLI | `AfterTool`: `decision: "deny"` with a `reason` | the result becomes "Tool result blocked: …", an error |
| Codex CLI | `PostToolUse`: `updatedMCPToolOutput` | MCP tools only, not shell output |
| Cline | `PostToolUse`: `cancel`, `contextModification` | observes only |

This script reads each agent's current hook output definition from GitHub (no model calls) and
compares its fields with scripts/hook_watch_baseline.json:

    python scripts/hook_watch.py            # compare; exit 1 when a field is new
    python scripts/hook_watch.py --update   # accept the current fields as the new baseline

A new field is the signal to check whether hook mode is now possible for that agent, by the
same test opencode passed: the replaced output is what the model receives and what the next
request re-sends.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

BASELINE = Path(__file__).with_name("hook_watch_baseline.json")


def _section(text: str, start: str, end: str) -> str:
    """The text from the line containing `start` up to the next line starting with `end`."""
    begin = text.index(start)
    stop = text.find("\n" + end, begin + len(start))
    return text[begin : stop if stop != -1 else len(text)]


def _gemini(text: str) -> set[str]:
    """AfterTool's output fields, from the hooks reference."""
    section = _section(text, "### `AfterTool`", "##")
    outputs = section.split("Relevant Output Fields", 1)[1]
    return set(re.findall(r"^\s*- `([A-Za-z_.]+)`", outputs, re.M))


def _codex(text: str) -> set[str]:
    """PostToolUse's output fields (snake_case, as serde maps them), with renames as written."""
    fields = set()
    for struct in ("PostToolUseCommandOutputWire", "PostToolUseHookSpecificOutputWire"):
        body = _section(text, f"struct {struct} {{", "}")
        fields |= set(re.findall(r'rename = "(\w+)"', body))
        fields |= {f"{struct}.{f}" for f in re.findall(r"pub (\w+):", body)}
    return fields


def _cline(text: str) -> set[str]:
    """The hook output fields, from the hooks README."""
    section = _section(text, "### Output", "###")
    return set(re.findall(r'"(\w+)":', section))


SOURCES = {
    "gemini-cli": ("google-gemini/gemini-cli", "main", "docs/hooks/reference.md", _gemini),
    "codex": ("openai/codex", "main", "codex-rs/hooks/src/schema.rs", _codex),
    "cline": ("cline/cline", "main", ".clinerules/hooks/README.md", _cline),
}


def fetch(repo: str, branch: str, path: str) -> str:
    """A file from GitHub's raw content host."""
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def main() -> int:
    """Compare every agent's hook output fields with the baseline; exit 1 on anything new or unreadable."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="write the current fields as the baseline")
    args = parser.parse_args()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    current, failed = {}, False
    for agent, (repo, branch, path, extract) in SOURCES.items():
        try:
            current[agent] = sorted(extract(fetch(repo, branch, path)))
        except Exception as exc:  # a moved file or a changed layout needs a look too
            print(f"{agent}: could not read {repo}/{path}: {exc}")
            failed = True
            continue
        new = sorted(set(current[agent]) - set(baseline.get(agent, [])))
        gone = sorted(set(baseline.get(agent, [])) - set(current[agent]))
        for field in new:
            print(f"{agent}: NEW  {field}")
        for field in gone:
            print(f"{agent}: gone {field}")
        if new:
            failed = True
        elif not gone:
            print(f"{agent}: unchanged")
    if args.update:
        BASELINE.write_text(json.dumps({**baseline, **current}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written to {BASELINE}")
        return 0
    if failed:
        print("a hook changed: check whether it can now replace tool output (see this script's docstring)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
