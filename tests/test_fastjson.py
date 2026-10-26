import json

from conftest import big_output, read_output, request, tool_result, tool_use, user_text

from lightfoot import fastjson


def test_fast_and_standard_paths_carry_identical_content():
    body = request(
        [
            user_text("héllo → ünïcode ✓ 😀"),
            tool_use("t1", "Read", {"file_path": "/a.py"}, thinking=True),
            tool_result("t1", read_output(50)),
            tool_use("t2", "Bash", {"command": "make"}),
            tool_result("t2", big_output(200)),
        ],
        temperature=0.25,
    )
    fast = fastjson.dumps(body)
    standard = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert json.loads(fast) == json.loads(standard) == body
    assert fastjson.loads(standard) == body


def test_same_input_gives_the_same_bytes():
    body = request([user_text("x"), tool_use("t1", "Bash", {"command": "ls"}), tool_result("t1", "a\nb")])
    assert fastjson.dumps(body) == fastjson.dumps(json.loads(fastjson.dumps(body)))


def test_edge_cases_fall_back_to_the_standard_library():
    lone = json.loads('{"text": "broken \\ud83d surrogate"}')
    out = fastjson.dumps(lone)
    assert json.loads(out) == lone
    assert fastjson.loads(b'{"text": "broken \\ud83d surrogate"}') == lone
    huge = {"n": 2**70}
    assert json.loads(fastjson.dumps(huge)) == huge
