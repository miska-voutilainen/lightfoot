import json

import pytest

from lightfoot import pricing
from lightfoot.text import (
    denoise_lines,
    distinctive_identifiers,
    grep_line_path,
    is_error_line,
    line_number_range,
    minify_json,
    normalize_terminal,
    render_selection,
    same_file,
    select_lines,
)


def test_normalize_strips_ansi_and_resolves_carriage_returns():
    raw = "\x1b[31mred\x1b[0m\nprogress 10%\rprogress 50%\rprogress 100%\nwindows\r\nend"
    assert normalize_terminal(raw) == "red\nprogress 100%\nwindows\nend"


def test_denoise_collapses_repeats_and_progress_runs():
    lines = (
        ["start"]
        + ["same line"] * 5
        + [f"[{'=' * i}{' ' * (20 - i)}] {i * 5}%" for i in range(1, 6)]
        + ["", "", "", "end"]
    )
    out = denoise_lines(lines)
    assert out[0] == "start"
    assert out[1] == "same line"
    assert out[2] == "[lightfoot: previous line repeated 4 more times]"
    assert sum(1 for line in out if "%" in line) == 1  # only the last progress line survives
    assert out[-1] == "end"
    assert out.count("") == 2


def test_progress_removal_never_drops_error_lines():
    lines = ["[==========          ] 50% error: disk full", "[====================] 100%"]
    assert denoise_lines(lines)[0].endswith("error: disk full")


def test_selection_keeps_head_tail_and_errors_with_numbered_gaps():
    lines = [f"line {i}" for i in range(1, 201)]
    lines[99] = "Traceback (most recent call last):"
    keep = select_lines(lines, head=10, tail=10, context=2)
    text = render_selection(lines, keep)
    assert "line 1\n" in text and "line 200" in text
    assert "Traceback (most recent call last):" in text
    assert "… [lightfoot: lines 11-97 omitted] …" in text
    assert "… [lightfoot: lines 103-190 omitted] …" in text
    assert "line 98" in text and "line 102" in text


def test_error_regex():
    assert is_error_line("E   assert 3 == 4")
    assert is_error_line("TypeError: x is not a function")
    assert is_error_line("java.lang.NullPointerException")
    assert is_error_line("npm ERR! code ELIFECYCLE")
    assert is_error_line("FAILED tests/a.py::t")
    assert not is_error_line("PASSED tests/a.py::t in 0.01s")


def test_line_number_range_both_formats():
    assert line_number_range("     1→a\n     2→b\n    30→c") == (1, 30)
    assert line_number_range("    10\ta\n    11\tb") == (10, 11)
    assert line_number_range("no numbers here") is None


def test_grep_paths():
    assert grep_line_path("src/app/main.py:12:    foo()") == "src/app/main.py"
    assert grep_line_path(r"C:\repo\src\main.py:12:    foo()") == r"C:\repo\src\main.py"
    assert grep_line_path("src/app/main.py:    foo()") == "src/app/main.py"
    assert grep_line_path("12:    foo()") is None
    assert grep_line_path("--") is None


def test_same_file_matches_relative_and_absolute():
    assert same_file(r"C:\repo\src\Main.py", "src/main.py")
    assert not same_file("/repo/src/main.py", "src/other.py")


def test_distinctive_identifiers():
    ids = distinctive_identifiers("calls parse_config_file in src/lean/config.py and UserAccountService")
    assert "parse_config_file" in ids
    assert "src/lean/config.py" in ids
    assert "UserAccountService" in ids


def test_pricing():
    assert pricing.lookup("claude-opus-5-5")[0].read == 0.05
    assert pricing.lookup("claude-opus-5-5[1m]")[1]
    assert pricing.lookup("claude-opus-5")[0].input == 5.0
    assert pricing.lookup("claude-fable-5-1")[0].read == 0.025
    assert not pricing.lookup("some-new-model")[1]
    # 1M input at $4, 1M 5m-writes at 1.25x, 1M reads at 0.05x, 1M output at $20
    cost = pricing.effective_cost("claude-opus-5-5", 1_000_000, 1_000_000, 0, 1_000_000, 1_000_000)
    assert abs(cost - (4 * (1 + 1.25 + 0.05) + 20)) < 1e-9
    haiku_long = pricing.effective_cost("claude-haiku-5-5", 200_000, 0, 0, 0, 0)
    assert abs(haiku_long - 0.1) < 1e-9  # $0.50/MTok beyond 100k prompt tokens


# --- minify_json -------------------------------------------------------------------


def test_minify_json_removes_only_whitespace_between_tokens():
    pretty = json.dumps({"name": "a  b", "items": [1, 2.50, {"x": "line\nbreak"}], "e": "\\"}, indent=2)
    out = minify_json(pretty)
    assert out == r'{"name":"a  b","items":[1,2.5,{"x":"line\nbreak"}],"e":"\\"}'
    assert json.loads(out) == json.loads(pretty)


def test_minify_json_keeps_number_spelling_key_order_and_duplicates():
    pretty = '{\n  "b": 1.000,\n  "a": 1e5,\n  "a": -0.0\n}'
    assert minify_json(pretty) == '{"b":1.000,"a":1e5,"a":-0.0}'


def test_minify_json_keeps_escaped_quotes_and_spaces_inside_strings():
    pretty = '[\n  "say \\"hi  there\\"",\n  "tab\\t  end"\n]'
    assert minify_json(pretty) == r'["say \"hi  there\"","tab\t  end"]'


@pytest.mark.parametrize(
    "text",
    [
        "plain text output",
        '{"already":"minified"}',
        "{not json at all}",
        '"just a string"',
        "123",
        "",
        '{"a": 1}\nextra trailing text',
    ],
)
def test_minify_json_returns_none_when_nothing_to_gain_or_not_json(text):
    assert minify_json(text) is None


def test_one_line_json_is_never_spilled(tmp_path):
    from lightfoot.adapters.base import Result
    from lightfoot.config import DEFAULTS
    from lightfoot.ingest import ingest, json_line

    class Ctx:
        spill_dir = tmp_path / "spill"
        graphify_report = None

    rows = [{"id": i, "log": "\n".join(f"line {j} of job {i}" for j in range(50))} for i in range(200)]
    compact = json.dumps(rows, separators=(",", ":"))
    assert json_line([compact]) and not json_line(["not json"]) and not json_line(["{", "}"])
    result = Result(0, (), "t1", {}, "mcp__x__y", {}, "other", 0, "str", compact, False)
    assert ingest(result, DEFAULTS, Ctx()) is None  # keeping only the start of the one line would lose its end
    pretty = json.dumps(rows, indent=2)
    result.orig_text = pretty
    assert ingest(result, DEFAULTS, Ctx())[1] in ("json", "spill")  # multi-line JSON is still handled
