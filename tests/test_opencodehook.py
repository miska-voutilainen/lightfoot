"""Hook mode for opencode: its plugin's tool.execute.after, condensed by the same rules as Claude Code's."""

import json
import shutil
import subprocess

import pytest
from conftest import big_output

from lightfoot import opencodehook
from lightfoot.store import Store


def _shell(tmp_path, output, tool="bash"):
    return {"tool": tool, "cwd": str(tmp_path), "session_id": "ses_1", "output": output}


def test_noisy_shell_output_is_condensed_and_recorded(tmp_path, lightfoot_home):
    payload = _shell(tmp_path, "\n".join(["same line repeated"] * 50) + "\nERROR: disk full")
    out = json.loads(opencodehook.run(json.dumps(payload)))
    assert "previous line repeated 49 more times" in out["output"] and "ERROR: disk full" in out["output"]
    store = Store()
    try:
        (row,) = store.query("SELECT * FROM hook_rewrites")
        assert row["rule"] == "noise" and row["tool"] == "bash" and row["session_id"] == "ses_1"
    finally:
        store.close()


def test_pretty_json_keeps_its_value(tmp_path, lightfoot_home):
    data = {"items": [{"id": i, "name": f"item {i}", "tags": ["a", "b"]} for i in range(40)]}
    out = json.loads(opencodehook.run(json.dumps(_shell(tmp_path, json.dumps(data, indent=2), tool="shell"))))
    assert json.loads(out["output"]) == data and len(out["output"]) < len(json.dumps(data, indent=2))


def test_large_output_is_spilled_into_the_project(tmp_path, lightfoot_home):
    out = json.loads(opencodehook.run(json.dumps(_shell(tmp_path, big_output()))))
    spill = list((tmp_path / ".lightfoot" / "spill").glob("*.log"))
    assert len(spill) == 1 and str(spill[0]) in out["output"]


@pytest.mark.parametrize(
    "payload",
    [
        {"tool": "read", "output": "\n".join(["same"] * 80)},  # not a shell tool
        {"tool": "bash", "output": "3 passed"},  # nothing to condense
        {"tool": "bash", "output": ""},
    ],
)
def test_other_tools_and_small_output_return_nothing(tmp_path, lightfoot_home, payload):
    assert opencodehook.run(json.dumps({**payload, "cwd": str(tmp_path), "session_id": "s"})) == ""


def test_bad_input_and_a_missing_cwd_never_raise(tmp_path, lightfoot_home):
    assert opencodehook.run("not json") == ""
    assert opencodehook.run(json.dumps(_shell(tmp_path / "missing", "\n".join(["same"] * 80)))) == ""


def test_hooks_can_be_disabled_per_project(tmp_path, lightfoot_home):
    (tmp_path / ".lightfoot").mkdir()
    (tmp_path / ".lightfoot" / "config.toml").write_text("[hooks]\nenabled = false\n", encoding="utf-8")
    assert opencodehook.run(json.dumps(_shell(tmp_path, "\n".join(["same"] * 80)))) == ""


def test_install_and_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "opencode" / "plugins" / "lightfoot-hook-mode.js"
    assert opencodehook.plugin_path() == path and not opencodehook.is_installed()
    opencodehook.install()
    opencodehook.install()  # replaces our own copy
    source = path.read_text(encoding="utf-8")
    assert opencodehook.MARK in source and "tool.execute.after" in source and '"opencode-tool-after"' in source
    assert opencodehook.is_installed()
    assert opencodehook.uninstall() is True and not path.exists()
    assert opencodehook.uninstall() is False


def test_install_never_overwrites_someone_elses_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = opencodehook.plugin_path()
    path.parent.mkdir(parents=True)
    path.write_text("export const Mine = async () => ({})\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="isn't Lightfoot's"):
        opencodehook.install()
    assert opencodehook.uninstall() is False and path.read_text(encoding="utf-8").startswith("export const Mine")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to parse the plugin")
def test_the_plugin_is_valid_javascript(tmp_path):
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(opencodehook.plugin_source(), encoding="utf-8")
    subprocess.run([shutil.which("node"), "--check", str(plugin)], check=True, capture_output=True)
