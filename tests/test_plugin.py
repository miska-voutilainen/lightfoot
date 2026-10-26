"""The Claude Code plugin in plugin/ and the marketplace file that lists it."""

import json
import re
from pathlib import Path

from lightfoot import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_version_matches_the_package():
    manifest = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "lightfoot" and manifest["version"] == __version__


def test_marketplace_lists_the_plugin_folder():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    (entry,) = market["plugins"]
    assert entry["name"] == "lightfoot" and (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").exists()


def test_skills_name_only_existing_commands():
    for skill in (ROOT / "plugin" / "skills").glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        assert text.startswith("---\n") and "description:" in text.split("---")[1]
        for command in re.findall(r"!`lightfoot (\w[\w-]*)", text):
            assert command in {"stats", "why"}, f"{skill}: unknown command {command}"
