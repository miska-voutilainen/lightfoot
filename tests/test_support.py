"""Support levels: one table, in agreement with the README."""

from pathlib import Path

from lightfoot.support import LEVELS, SUPPORT, report

README = Path(__file__).resolve().parent.parent / "README.md"


def test_every_level_is_known_and_the_readme_agrees():
    rows = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) == 3 and cells[1] in LEVELS:
            rows[cells[0]] = cells[1]
    assert rows == {s.agent: s.level for s in SUPPORT}
    assert all(s.level != "certified" for s in SUPPORT)  # only a passing paired eval may change this


def test_doctor_lines():
    lines = report()
    assert lines[0].startswith("support levels") and len(lines) == len(SUPPORT) + 1
    assert any("Claude Code" in x and "3 · model summaries" in x for x in lines)
