"""Hook mode for opencode: the ingest rules in an opencode plugin, without the proxy.

opencode's `tool.execute.after` plugin hook may overwrite `output.output`, and opencode then
sends and stores the new text: every later request re-sends exactly those bytes, so the rewrite
is frozen by opencode itself. (Checked on opencode 1.19.4 against a local mock API: the
replaced output was what the model received and what the next request re-sent.)

`lightfoot hook-mode install --agent opencode` writes a small plugin into opencode's global
plugins folder. For shell tools only (`bash`, and `shell` in older versions) it runs
`lightfoot hook opencode-tool-after`, which applies the same rules and checks as Claude Code's
hook mode. Any problem, on either side, leaves opencode's original output in place.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .config import load_config
from .hookmode import _record, condense
from .text import tokens

MARK = "Lightfoot hook mode for opencode"
SHELL_TOOLS = ("bash", "shell")

PLUGIN = """// __MARK__: condenses shell output before opencode stores it.
// Written by `lightfoot hook-mode install --agent opencode`; remove it with
// `lightfoot hook-mode uninstall --agent opencode`.
import { spawnSync } from "node:child_process";

const PYTHON = __PYTHON__;
const SHELL_TOOLS = new Set(__TOOLS__);

export const LightfootHookMode = async ({ directory }) => ({
  "tool.execute.after": async (input, output) => {
    if (!SHELL_TOOLS.has(input.tool) || typeof output.output !== "string" || !output.output) return;
    try {
      const payload = JSON.stringify({
        tool: input.tool,
        session_id: input.sessionID,
        cwd: directory,
        output: output.output,
      });
      const run = spawnSync(PYTHON, ["-m", "lightfoot", "hook", "opencode-tool-after"], {
        input: payload,
        encoding: "utf8",
        timeout: 30000,
        windowsHide: true,
      });
      if (run.status !== 0 || !run.stdout) return;
      const reply = JSON.parse(run.stdout);
      if (typeof reply.output === "string") output.output = reply.output;
    } catch {
      // Any problem: opencode keeps the original output.
    }
  },
});
"""


def handle(payload: dict) -> tuple[str | None, list[tuple[str, str, int, int]]]:
    """(condensed output or None, [(tool, rule, tokens before, tokens after)]) for one shell call."""
    tool = str(payload.get("tool") or "")
    text = payload.get("output")
    cwd = payload.get("cwd")
    if tool not in SHELL_TOOLS or not isinstance(text, str) or not text:
        return None, []
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        return None, []
    cfg = load_config(cwd)
    if not cfg.get("hooks", {}).get("enabled", True):
        return None, []
    hit = condense(text, tool, "shell", cfg, Path(cwd) / ".lightfoot" / "spill")
    if hit is None:
        return None, []
    return hit[0], [(tool, hit[1], tokens(text), tokens(hit[0]))]


def run(stdin_text: str) -> str:
    """Entry point for `lightfoot hook opencode-tool-after`: the plugin's JSON in, `{"output": …}` or nothing out."""
    try:
        payload = json.loads(stdin_text)
        payload = payload if isinstance(payload, dict) else {}
        output, done = handle(payload)
    except Exception:  # the plugin must never break a tool call; opencode keeps the original output
        return ""
    if done:
        _record(payload, done)
    return json.dumps({"output": output}) if output is not None else ""


# --- install into opencode's global plugins folder -------------------------------------


def plugin_path() -> Path:
    """opencode's global plugins folder: `$XDG_CONFIG_HOME/opencode/plugins`, else under `~/.config`."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opencode" / "plugins" / "lightfoot-hook-mode.js"


def plugin_source() -> str:
    """The plugin, pointing at this Python so it finds this Lightfoot installation."""
    return (
        PLUGIN.replace("__MARK__", MARK)
        .replace("__PYTHON__", json.dumps(Path(sys.executable).as_posix()))
        .replace("__TOOLS__", json.dumps(list(SHELL_TOOLS)))
    )


def is_installed() -> bool:
    """Whether Lightfoot's plugin is in opencode's plugins folder."""
    path = plugin_path()
    try:
        return path.is_file() and MARK in path.read_text(encoding="utf-8")
    except OSError:
        return False


def install() -> str:
    """Write the plugin (replacing an older copy of ours). Returns its path."""
    path = plugin_path()
    if path.exists() and not is_installed():
        raise RuntimeError(f"{path} exists and isn't Lightfoot's; move it away first")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plugin_source(), encoding="utf-8")
    return str(path)


def uninstall() -> bool:
    """Remove the plugin. False when it wasn't installed."""
    if not is_installed():
        return False
    plugin_path().unlink()
    return True
