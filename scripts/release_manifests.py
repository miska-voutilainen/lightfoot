"""Build the Homebrew formula and Scoop manifest for a published lightfoot release.

Run after the version is on PyPI (the release workflow does this on Linux, so the resolved
dependencies are the macOS and Linux ones Homebrew needs):

    python scripts/release_manifests.py 1.0.0 --out dist/manifests

Everything comes from PyPI itself: pip resolves the full dependency set, and each
package's source distribution and SHA-256 come from PyPI's JSON API. Nothing is guessed.

winget is not covered: it installs Windows installers (exe, msi or a portable zip), and
lightfoot is a Python package. Scoop installs it into its own virtualenv instead.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PYPI = "https://pypi.org/pypi"
HOMEPAGE = "https://github.com/miska-voutilainen/lightfoot"
DESCRIPTION = "Local, cache-aware token-saving proxy for coding agents"


def fetch_json(url: str) -> dict:
    """GET a JSON document from PyPI."""
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed https PyPI URLs only
        return json.load(response)


def sdist(name: str, version: str, fetch=fetch_json, attempts: int = 1) -> dict:
    """{"name", "version", "url", "sha256"} of a release's source distribution on PyPI."""
    for attempt in range(attempts):
        try:
            data = fetch(f"{PYPI}/{name}/{version}/json")
            for f in data.get("urls", []):
                if f.get("packagetype") == "sdist":
                    return {"name": name, "version": version, "url": f["url"], "sha256": f["digests"]["sha256"]}
            raise RuntimeError(f"{name} {version} has no source distribution on PyPI")
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(20)  # PyPI's JSON API can lag a fresh upload by a minute or two
    raise RuntimeError(f"{name} {version} not found on PyPI")


def resolve(version: str) -> list[tuple[str, str]]:
    """(name, version) of every package `pip install lightfoot==version` would install, lightfoot excluded."""
    report = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--quiet",
            "--ignore-installed",
            "--report",
            "-",
            f"lightfoot=={version}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    installs = json.loads(report.stdout)["install"]
    pins = {(i["metadata"]["name"].lower(), i["metadata"]["version"]) for i in installs}
    return sorted(p for p in pins if p[0] != "lightfoot")


def formula(package: dict, resources: list[dict]) -> str:
    """A Homebrew formula that installs lightfoot and its dependencies into a virtualenv."""
    blocks = "\n".join(
        f'  resource "{r["name"]}" do\n    url "{r["url"]}"\n    sha256 "{r["sha256"]}"\n  end\n' for r in resources
    )
    rust = (
        '  depends_on "rust" => :build # orjson builds from source\n'
        if any(r["name"] == "orjson" for r in resources)
        else ""
    )
    return f'''class Lightfoot < Formula
  include Language::Python::Virtualenv

  desc "{DESCRIPTION}"
  homepage "{HOMEPAGE}"
  url "{package["url"]}"
  sha256 "{package["sha256"]}"
  license "MIT"

{rust}  depends_on "python@3.13"

{blocks}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/lightfoot --version")
  end
end
'''


def scoop(package: dict) -> str:
    """A Scoop manifest: the verified source distribution, installed into its own virtualenv."""
    archive = package["url"].rsplit("/", 1)[-1]
    manifest = {
        "version": package["version"],
        "description": DESCRIPTION,
        "homepage": HOMEPAGE,
        "license": "MIT",
        "depends": "python",
        "url": f"{package['url']}#/{archive}",
        "hash": package["sha256"],
        "installer": {
            "script": [
                '& python -m venv "$dir\\venv"',
                f'& "$dir\\venv\\Scripts\\python.exe" -m pip install --quiet "$dir\\{archive}"',
            ]
        },
        "bin": "venv\\Scripts\\lightfoot.exe",
        "checkver": {"url": f"{PYPI}/lightfoot/json", "jsonpath": "$.info.version"},
        "autoupdate": {"url": "https://pypi.org/packages/source/l/lightfoot/lightfoot-$version.tar.gz"},
    }
    return json.dumps(manifest, indent=4) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the Homebrew formula and the Scoop manifest for `version`, built from what PyPI serves."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version")
    parser.add_argument("--out", default="dist/manifests")
    args = parser.parse_args(argv)
    package = sdist("lightfoot", args.version, attempts=10)
    resources = [sdist(name, version) for name, version in resolve(args.version)]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lightfoot.rb").write_text(formula(package, resources), encoding="utf-8")
    (out / "lightfoot.json").write_text(scoop(package), encoding="utf-8")
    print(f"wrote {out / 'lightfoot.rb'} ({len(resources)} resources) and {out / 'lightfoot.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
