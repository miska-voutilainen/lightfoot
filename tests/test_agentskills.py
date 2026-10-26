"""`lightfoot skills`: the Agent Skills that Gemini CLI, opencode and Codex CLI load from ~/.agents/skills."""

import re
from pathlib import Path

import pytest

from lightfoot import agentskills

PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "skills"


def frontmatter(text: str) -> tuple[dict, str]:
    """(header fields, body) of a SKILL.md; enough YAML for these files."""
    head, body = text.split("\n---\n", 1)
    fields = {}
    for line in head.removeprefix("---\n").splitlines():
        if ": " in line and not line.startswith(" "):
            key, value = line.split(": ", 1)
            fields[key] = value.strip("'").replace("''", "'")
    return fields, body.lstrip("\n")


def test_every_skill_meets_the_agent_skills_rules():
    assert agentskills.names() == ["lightfoot-condensed-context", "lightfoot-stats", "lightfoot-why"]
    for name in agentskills.names():
        fields, body = frontmatter((agentskills.SOURCE / name / "SKILL.md").read_text(encoding="utf-8"))
        assert fields["name"] == name  # opencode: the name matches its folder
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
        assert 1 <= len(fields["description"]) <= 1024
        assert body.strip()


def test_the_guidance_matches_the_claude_code_plugin():
    _, ours = frontmatter((agentskills.SOURCE / "lightfoot-condensed-context" / "SKILL.md").read_text(encoding="utf-8"))
    _, plugin = frontmatter((PLUGIN / "condensed-context" / "SKILL.md").read_text(encoding="utf-8"))
    assert ours == plugin


def test_install_and_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr(agentskills, "skills_root", lambda: tmp_path)
    (tmp_path / "someone-elses").mkdir()
    assert agentskills.installed() == []
    assert agentskills.install() == agentskills.names()
    agentskills.install()  # replaces our own copies
    assert agentskills.installed() == agentskills.names()
    assert agentskills.uninstall() == agentskills.names()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["someone-elses"]
    assert agentskills.uninstall() == []


def test_install_never_overwrites_someone_elses_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(agentskills, "skills_root", lambda: tmp_path)
    theirs = tmp_path / "lightfoot-stats" / "SKILL.md"
    theirs.parent.mkdir()
    theirs.write_text("---\nname: lightfoot-stats\ndescription: mine\n---\n\nmine\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="lightfoot-stats"):
        agentskills.install()
    assert not (tmp_path / "lightfoot-why").exists()  # nothing written
    assert agentskills.uninstall() == [] and theirs.read_text(encoding="utf-8").endswith("mine\n")
