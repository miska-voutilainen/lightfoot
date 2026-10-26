"""scripts/release_manifests.py: the Homebrew formula and Scoop manifest, with PyPI faked."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_manifests.py"
spec = importlib.util.spec_from_file_location("release_manifests", SCRIPT)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)


def pypi(name, version, *, sdist=True):
    files = [{"packagetype": "bdist_wheel", "url": f"https://files/{name}-{version}.whl", "digests": {"sha256": "w"}}]
    if sdist:
        files.append(
            {
                "packagetype": "sdist",
                "url": f"https://files/{name}-{version}.tar.gz",
                "digests": {"sha256": f"h-{name}"},
            }
        )
    return {"urls": files}


def test_sdist_picks_the_source_distribution():
    got = rm.sdist("httpx", "0.28.1", fetch=lambda url: pypi("httpx", "0.28.1"))
    assert got == {
        "name": "httpx",
        "version": "0.28.1",
        "url": "https://files/httpx-0.28.1.tar.gz",
        "sha256": "h-httpx",
    }
    with pytest.raises(RuntimeError, match="no source distribution"):
        rm.sdist("x", "1", fetch=lambda url: pypi("x", "1", sdist=False))


def test_sdist_retries_while_pypi_lags(monkeypatch):
    monkeypatch.setattr(rm.time, "sleep", lambda s: None)
    calls = []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("404")
        return pypi("lightfoot", "0.1.0")

    assert rm.sdist("lightfoot", "0.1.0", fetch=flaky, attempts=5)["sha256"] == "h-lightfoot"
    assert calls == ["https://pypi.org/pypi/lightfoot/0.1.0/json"] * 3
    with pytest.raises(OSError):
        rm.sdist("lightfoot", "0.1.0", fetch=lambda url: (_ for _ in ()).throw(OSError("down")), attempts=2)


def test_formula_lists_every_resource_and_needs_rust_only_for_orjson():
    package = {"name": "lightfoot", "version": "0.1.0", "url": "https://files/lightfoot-0.1.0.tar.gz", "sha256": "abc"}
    plain = rm.formula(package, [{"name": "httpx", "url": "https://files/httpx.tar.gz", "sha256": "h1"}])
    assert 'resource "httpx" do' in plain and 'sha256 "h1"' in plain and 'sha256 "abc"' in plain
    assert "rust" not in plain and "virtualenv_install_with_resources" in plain
    rusty = rm.formula(package, [{"name": "orjson", "url": "https://files/orjson.tar.gz", "sha256": "h2"}])
    assert 'depends_on "rust" => :build' in rusty


def test_scoop_manifest_is_valid_json_pointing_at_the_sdist():
    package = {"name": "lightfoot", "version": "0.1.0", "url": "https://files/lightfoot-0.1.0.tar.gz", "sha256": "abc"}
    manifest = json.loads(rm.scoop(package))
    assert manifest["version"] == "0.1.0" and manifest["hash"] == "abc"
    assert manifest["url"] == "https://files/lightfoot-0.1.0.tar.gz#/lightfoot-0.1.0.tar.gz"
    assert "lightfoot-0.1.0.tar.gz" in manifest["installer"]["script"][1]


def test_main_writes_both_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rm, "resolve", lambda version: [("anyio", "4.9.0"), ("orjson", "3.10.0")])
    monkeypatch.setattr(
        rm,
        "sdist",
        lambda name, version, attempts=1: {
            "name": name,
            "version": version,
            "url": f"https://files/{name}.tar.gz",
            "sha256": f"h-{name}",
        },
    )
    assert rm.main(["0.1.0", "--out", str(tmp_path)]) == 0
    formula = (tmp_path / "lightfoot.rb").read_text(encoding="utf-8")
    assert 'resource "anyio"' in formula and 'resource "orjson"' in formula
    assert json.loads((tmp_path / "lightfoot.json").read_text(encoding="utf-8"))["hash"] == "h-lightfoot"
    assert "2 resources" in capsys.readouterr().out
