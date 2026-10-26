"""`lightfoot skills install`: Lightfoot's skills for every agent that reads the Agent Skills folder.

Gemini CLI, opencode and Codex CLI all load user skills from `~/.agents/skills/<name>/SKILL.md`
(each run against a mock API with the skills installed, 2026-10-26, and listed all three).
One install there gives each of them:

| Skill | What it does |
| --- | --- |
| `lightfoot-condensed-context` | How to read Lightfoot's `[lightfoot: …]` notes and find the full text they point to |
| `lightfoot-stats` | Runs `lightfoot stats` when the user asks what Lightfoot saved |
| `lightfoot-why` | Runs `lightfoot why` when the user asks what was condensed |

Claude Code gets the same through its plugin (`plugin/`). The skills ship in
`lightfoot/data/skills/`; each carries `metadata.source: lightfoot`, so install and uninstall
never touch another skill of the same name.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SOURCE = Path(__file__).with_name("data") / "skills"
MARK = "source: lightfoot"


def skills_root() -> Path:
    """The Agent Skills folder that Gemini CLI, opencode and Codex CLI read: `~/.agents/skills`."""
    return Path.home() / ".agents" / "skills"


def names() -> list[str]:
    """The skills Lightfoot ships."""
    return sorted(p.parent.name for p in SOURCE.glob("*/SKILL.md"))


def _ours(folder: Path) -> bool:
    skill = folder / "SKILL.md"
    try:
        return skill.is_file() and MARK in skill.read_text(encoding="utf-8")
    except OSError:
        return False


def installed() -> list[str]:
    """Lightfoot's skills present in the Agent Skills folder."""
    return [n for n in names() if _ours(skills_root() / n)]


def install() -> list[str]:
    """Copy every skill into the Agent Skills folder, replacing older copies of ours. Returns their names.

    Refuses, before writing anything, when a folder of the same name holds someone else's skill.
    """
    root = skills_root()
    taken = [n for n in names() if (root / n).exists() and not _ours(root / n)]
    if taken:
        raise RuntimeError(f"{root} already has skills named {', '.join(taken)} that aren't Lightfoot's")
    for name in names():
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name / "SKILL.md", target / "SKILL.md")
    return names()


def uninstall() -> list[str]:
    """Remove Lightfoot's skills (and only ours). Returns the names removed."""
    removed = []
    for name in installed():
        folder = skills_root() / name
        (folder / "SKILL.md").unlink()
        if not any(folder.iterdir()):
            folder.rmdir()
        removed.append(name)
    return removed
