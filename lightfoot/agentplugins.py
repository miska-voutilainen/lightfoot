"""`lightfoot plugins install --agent gemini|codex`: Lightfoot as a native Gemini CLI extension or Codex plugin.

Each agent installs it with its own installer, so it shows in `gemini extensions list` or
`codex plugin list` and is removed the agent's way too. Both are built from the skills that ship
in `lightfoot/data/skills/`, so the guidance is the same everywhere:

| Agent | Package and installer | Gives |
| --- | --- | --- |
| Gemini CLI | an extension, `gemini extensions install` | `/lightfoot:stats`, `/lightfoot:why`, condensed context |
| Codex CLI | a plugin in a local marketplace, `codex plugin add` | the three skills |

Formats checked against Gemini CLI 0.65.0's extension reference and Codex 0.164.0's plugin
loader and tests (2026-10-26), and each installed for real. Claude Code has its own plugin in
`plugin/`; opencode reads the skills from `lightfoot skills install`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import __version__
from .agentskills import SOURCE as SKILLS
from .agentskills import names as skill_names
from .config import lightfoot_home

NAME = "lightfoot"
DESCRIPTION = "Companion for the Lightfoot proxy: how to read condensed context, and Lightfoot's stats and why."

GEMINI_COMMANDS = {
    "stats": (
        "Show Lightfoot's cost and savings for recent sessions.",
        "Lightfoot statistics for the last 7 days:\n\n!{lightfoot stats}\n\n"
        "Present these numbers to the user briefly: the total saving, the sessions that saved the most, and any "
        "rule marked with a warning. Don't add numbers that aren't in the output.",
    ),
    "why": (
        "Show what Lightfoot changed in a session (the most recent one by default) and what each change saved.",
        "!{lightfoot why {{args}}}\n\n"
        "Explain this to the user in plain words: what was condensed, roughly how much it saved, and where the full "
        "text of anything condensed is kept. Don't add numbers that aren't in the output.",
    ),
}


def _toml_string(text: str) -> str:
    """A TOML multi-line basic string."""
    return '"""\n' + text.replace("\\", "\\\\").replace('"""', '\\"""') + '\n"""'


def _copy_skills(dest: Path, only: list[str] | None = None) -> None:
    for name in only or skill_names():
        (dest / name).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILLS / name / "SKILL.md", dest / name / "SKILL.md")


def build_gemini_extension(dest: Path) -> Path:
    """Write the Gemini CLI extension into `dest` and return it.

    The stats and why skills become `/lightfoot:stats` and `/lightfoot:why` commands, which run
    the command themselves (`!{…}`), as the Claude Code plugin does.
    """
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {"name": NAME, "version": __version__, "description": DESCRIPTION}
    (dest / "gemini-extension.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for command, (description, prompt) in GEMINI_COMMANDS.items():
        path = dest / "commands" / NAME / f"{command}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"description = {json.dumps(description)}\nprompt = {_toml_string(prompt)}\n", encoding="utf-8")
    _copy_skills(dest / "skills", only=["lightfoot-condensed-context"])
    return dest


def build_codex_marketplace(dest: Path) -> Path:
    """Write a local Codex marketplace holding the Lightfoot plugin into `dest` and return it."""
    plugin = dest / "plugins" / NAME
    (plugin / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {"name": NAME, "version": __version__, "description": DESCRIPTION, "skills": "./skills"}
    (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _copy_skills(plugin / "skills")
    marketplace = {
        "name": NAME,
        "plugins": [{"name": NAME, "source": {"source": "local", "path": f"./plugins/{NAME}"}}],
    }
    (dest / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)
    (dest / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(marketplace, indent=2) + "\n", encoding="utf-8"
    )
    return dest


def gemini_extension_dir() -> Path:
    """Where the Gemini CLI extension is built before Gemini CLI copies it: a fixed folder, since you trust it."""
    return lightfoot_home() / "plugins" / "gemini-extension" / NAME


def codex_marketplace_dir() -> Path:
    """Where the Codex marketplace lives: Codex keeps a local marketplace's path, so it must stay."""
    return lightfoot_home() / "plugins" / "codex-marketplace"


def _run(argv: list[str], interactive: bool = False) -> None:
    """Run an agent's own command; `interactive` leaves the terminal to it, for questions only the user answers."""
    exe = shutil.which(argv[0])
    if not exe:
        raise RuntimeError(f"could not find `{argv[0]}` on PATH")
    if interactive:
        if subprocess.run([exe, *argv[1:]]).returncode != 0:
            raise RuntimeError(f"`{' '.join(argv)}` did not finish")
        return
    done = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()[-600:]
        raise RuntimeError(f"`{' '.join(argv)}` failed: {detail}")


def install(agent: str) -> str:
    """Install Lightfoot's extension or plugin with the agent's own installer. Returns what to call it by."""
    if agent == "gemini":
        folder = gemini_extension_dir()
        if folder.exists():
            shutil.rmtree(folder)
        # Gemini CLI asks whether you trust the folder an extension comes from: that answer is yours,
        # so its installer gets the terminal.
        _run(["gemini", "extensions", "install", str(build_gemini_extension(folder)), "--consent"], interactive=True)
        return f"Gemini CLI extension `{NAME}` (/lightfoot:stats, /lightfoot:why)"
    if agent == "codex":
        folder = codex_marketplace_dir()
        if folder.exists():
            shutil.rmtree(folder)
        build_codex_marketplace(folder)
        _run(["codex", "plugin", "marketplace", "add", str(folder)])
        _run(["codex", "plugin", "add", f"{NAME}@{NAME}"])
        return f"Codex plugin `{NAME}@{NAME}`"
    raise RuntimeError(f"no plugin for {agent}; use `lightfoot skills install` (opencode) or the Claude Code plugin")


def uninstall(agent: str) -> str:
    """Remove the extension or plugin with the agent's own uninstaller."""
    if agent == "gemini":
        _run(["gemini", "extensions", "uninstall", NAME])
        shutil.rmtree(gemini_extension_dir().parent, ignore_errors=True)
        return f"Gemini CLI extension `{NAME}` removed"
    if agent == "codex":
        _run(["codex", "plugin", "remove", f"{NAME}@{NAME}"])
        _run(["codex", "plugin", "marketplace", "remove", NAME])
        shutil.rmtree(codex_marketplace_dir(), ignore_errors=True)
        return f"Codex plugin `{NAME}@{NAME}` removed"
    raise RuntimeError(f"no plugin for {agent}")
