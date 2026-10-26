"""Detection of the optional Graphify and Obsidian integrations.

Detection runs once per `lightfoot claude` launch and is stored with the launch, so it is
fixed for the life of every session in that launch. An absent tool means the
touchpoints that depend on it are skipped; nothing is substituted for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Integrations:
    """Graphify and Obsidian as detected for one project, with the reason for each result."""

    graphify_report: str | None  # absolute path to GRAPH_REPORT.md when present
    vault: str | None  # absolute path to the Obsidian vault when present
    graphify_reason: str
    obsidian_reason: str


def _setting(value) -> str:
    value = str(value).strip().lower()
    return value if value in ("auto", "on", "off") else "auto"


def detect(cwd: str | Path, cfg: dict) -> Integrations:
    """Find the project's Graphify report and the user's Obsidian vault, unless the config turns them off."""
    settings = cfg["integrations"]

    graphify_report, graphify_reason = None, "off in config"
    if _setting(settings.get("graphify")) != "off":
        report = (Path(cwd) / settings.get("graphify_report", "graphify-out/GRAPH_REPORT.md")).resolve()
        if report.is_file():
            graphify_report, graphify_reason = str(report), f"found {report}"
        else:
            graphify_reason = f"no graph yet ({report} does not exist)"

    vault, obsidian_reason = None, "off in config"
    if _setting(settings.get("obsidian")) != "off":
        configured = str(settings.get("vault_path") or "").strip()
        if not configured:
            obsidian_reason = "vault_path is not set"
        else:
            path = Path(configured).expanduser().resolve()
            if (path / ".obsidian").is_dir():
                vault, obsidian_reason = str(path), f"vault at {path}"
            elif _setting(settings.get("obsidian")) == "on" and path.is_dir():
                vault, obsidian_reason = str(path), f"forced on: {path} (no .obsidian folder)"
            else:
                obsidian_reason = f"{path} is not an Obsidian vault (no .obsidian folder)"
    return Integrations(graphify_report, vault, graphify_reason, obsidian_reason)
