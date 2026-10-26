"""Property tests for the safety core: the validator, replay, JSON minification and trimming.

Each property is a promise lightfoot makes about every request, so it is checked on many
generated inputs rather than a few hand-written ones.
"""

import copy
import json
import time

import pytest
from conftest import request, tool_result, tool_use, user_text
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lightfoot.adapters import ANTHROPIC
from lightfoot.config import DEFAULTS
from lightfoot.server import dumps
from lightfoot.store import Store
from lightfoot.text import error_line_indices, minify_json, render_selection, select_lines
from lightfoot.transform import Context, plan_request, session_key
from lightfoot.validate import ValidationError, validate

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=8), children, max_size=5),
    max_leaves=30,
)


@SETTINGS
@given(json_values, st.sampled_from([1, 2, 4, "\t"]))
def test_minified_json_always_parses_to_the_same_value(value, indent):
    pretty = json.dumps(value, indent=indent, ensure_ascii=False)
    out = minify_json(pretty)
    if out is not None:
        assert json.loads(out) == json.loads(pretty) and len(out) < len(pretty)


log_lines = st.lists(
    st.one_of(
        st.text(alphabet="abcdefghij .:/0123456789", max_size=40),
        st.sampled_from(["ERROR: disk full", "Traceback (most recent call last):", "FAILED test_x - assert 1 == 2"]),
    ),
    min_size=1,
    max_size=400,
)


@SETTINGS
@given(log_lines, st.integers(0, 30), st.integers(0, 30))
def test_trimming_never_drops_an_error_line(lines, head, tail):
    kept = render_selection(lines, select_lines(lines, head, tail, 1))
    for i in error_line_indices(lines):
        assert lines[i] in kept.split("\n")


def _conversation(outputs):
    msgs = [user_text("start")]
    for i, (name, text) in enumerate(outputs):
        inp = {"file_path": f"/r/f{i % 3}.py"} if name == "Read" else {"command": f"cmd {i}"}
        msgs += [tool_use(f"t{i}", name, inp), tool_result(f"t{i}", text)]
    return msgs


outputs = st.lists(
    st.tuples(
        st.sampled_from(["Bash", "Read", "Grep", "mcp__api__get"]),
        st.one_of(
            st.text(max_size=200).filter(str.strip),
            log_lines.map("\n".join),
            st.builds(lambda v: json.dumps(v, indent=2), json_values),
        ),
    ),
    min_size=1,
    max_size=8,
)


def _plan(store, tmp_path, body):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["rewrite_budget_ms"] = 10_000
    cfg["epochs"]["budget_ms"] = 10_000
    cfg["thresholds"]["spill_tokens"] = 200  # small, so trimming happens on generated outputs
    session = store.get_or_create_session(session_key(body), None, "live", time.time())
    ctx = Context(cfg, store, "live", session, tmp_path / ".lightfoot" / "spill", None, False, time.perf_counter())
    plan = plan_request(body, ctx)
    store.put_rewrites(plan.new_rewrites)
    return plan


@SETTINGS
@given(outputs)
def test_rewrites_touch_only_tool_output_and_replay_byte_identically(tmp_path, items):
    store = Store(tmp_path / f"p{time.time_ns()}.db")
    try:
        body = request(_conversation(items))
        original = copy.deepcopy(body)
        first = _plan(store, tmp_path, copy.deepcopy(body))
        assert body == original  # planning never mutates the request it was given
        if first.body_out is None:
            return
        # Everything except tool_result content is exactly as the client sent it.
        for m_orig, m_out in zip(original["messages"], first.body_out["messages"], strict=True):
            assert m_orig["role"] == m_out["role"]
            for b_orig, b_out in zip(m_orig["content"], m_out["content"], strict=True):
                if b_orig.get("type") == "tool_result":
                    assert {k: v for k, v in b_orig.items() if k != "content"} == {
                        k: v for k, v in b_out.items() if k != "content"
                    }
                else:
                    assert b_orig == b_out
        assert {k: v for k, v in original.items() if k != "messages"} == {
            k: v for k, v in first.body_out.items() if k != "messages"
        }
        second = _plan(store, tmp_path, copy.deepcopy(body))
        assert second.new_rewrites == [] and dumps(second.body_out) == dumps(first.body_out)
    finally:
        store.close()


def _leaf_paths(value, path=()):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaf_paths(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _leaf_paths(v, (*path, i))
    else:
        yield path


@SETTINGS
@given(outputs, st.data())
def test_validator_rejects_any_change_outside_the_declared_paths(items, data):
    body = request(_conversation(items))
    declared = ("messages", 2, "content", 0, "content")
    leaves = [p for p in _leaf_paths(body) if p[: len(declared)] != declared]
    target = data.draw(st.sampled_from(leaves))
    out = copy.deepcopy(body)
    node = out
    for key in target[:-1]:
        node = node[key]
    old = node[target[-1]]
    node[target[-1]] = (old + "x") if isinstance(old, str) else ("changed" if old is not True else False)
    with pytest.raises(ValidationError):
        validate(body, out, {}, len(body["messages"]), ANTHROPIC)
