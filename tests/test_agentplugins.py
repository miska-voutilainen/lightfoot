"""`lightfoot plugins`: the Gemini CLI extension and the Codex plugin, built from the shipped skills."""

import json
import tomllib

import pytest

from lightfoot import __version__, agentplugins, agentskills


def test_the_gemini_extension(tmp_path):
    ext = agentplugins.build_gemini_extension(tmp_path / "lightfoot")
    manifest = json.loads((ext / "gemini-extension.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "lightfoot" and manifest["version"] == __version__
    stats = tomllib.loads((ext / "commands" / "lightfoot" / "stats.toml").read_text(encoding="utf-8"))
    why = tomllib.loads((ext / "commands" / "lightfoot" / "why.toml").read_text(encoding="utf-8"))
    assert "!{lightfoot stats}" in stats["prompt"] and stats["description"]
    assert "!{lightfoot why {{args}}}" in why["prompt"]  # Gemini CLI shell-escapes args inside !{...}
    skill = ext / "skills" / "lightfoot-condensed-context" / "SKILL.md"
    assert skill.read_bytes() == (agentskills.SOURCE / "lightfoot-condensed-context" / "SKILL.md").read_bytes()


def test_the_codex_marketplace(tmp_path):
    root = agentplugins.build_codex_marketplace(tmp_path)
    marketplace = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"] == [
        {"name": "lightfoot", "source": {"source": "local", "path": "./plugins/lightfoot"}}
    ]
    plugin = root / "plugins" / "lightfoot"
    manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__ and manifest["skills"] == "./skills"
    for name in agentskills.names():
        assert (plugin / "skills" / name / "SKILL.md").read_bytes() == (
            agentskills.SOURCE / name / "SKILL.md"
        ).read_bytes()


def test_install_and_uninstall_use_each_agents_own_commands(tmp_path, monkeypatch, lightfoot_home):
    calls = []
    monkeypatch.setattr(agentplugins, "_run", lambda argv, interactive=False: calls.append((argv[:4], interactive)))
    agentplugins.install("gemini")
    agentplugins.install("codex")
    assert calls[0] == (["gemini", "extensions", "install", str(agentplugins.gemini_extension_dir())], True)
    assert agentplugins.gemini_extension_dir().joinpath("gemini-extension.json").is_file()  # a fixed folder to trust
    assert [c[0] for c in calls[1:]] == [
        ["codex", "plugin", "marketplace", "add"],
        ["codex", "plugin", "add", "lightfoot@lightfoot"],
    ]
    assert agentplugins.codex_marketplace_dir().joinpath(".agents", "plugins", "marketplace.json").is_file()
    calls.clear()
    agentplugins.uninstall("gemini")
    agentplugins.uninstall("codex")
    assert [c[0] for c in calls] == [
        ["gemini", "extensions", "uninstall", "lightfoot"],
        ["codex", "plugin", "remove", "lightfoot@lightfoot"],
        ["codex", "plugin", "marketplace", "remove"],
    ]
    assert not agentplugins.codex_marketplace_dir().exists() and not agentplugins.gemini_extension_dir().exists()


def test_other_agents_are_pointed_elsewhere():
    with pytest.raises(RuntimeError, match="skills install"):
        agentplugins.install("opencode")
