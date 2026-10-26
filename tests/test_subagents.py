"""`lightfoot estimate --subagents`: cost per subagent type from Claude Code's transcripts."""

import json

from lightfoot import subagents

MODEL = "claude-opus-5-5"


def write(path, usages):
    lines = []
    for i, u in enumerate(usages):
        lines.append(json.dumps({"type": "assistant", "message": {"id": f"m{i}", "model": MODEL, "usage": u}}))
    lines.append(lines[0])  # a response logged twice counts once
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cost_per_type_and_first_request_share(tmp_path):
    session = tmp_path / "proj" / "s1"
    (session / "subagents").mkdir(parents=True)
    write(
        tmp_path / "proj" / "s1.jsonl", [{"input_tokens": 10, "cache_read_input_tokens": 50_000, "output_tokens": 50}]
    )
    first = {
        "input_tokens": 3,
        "cache_creation_input_tokens": 20_000,
        "cache_read_input_tokens": 5_000,
        "output_tokens": 1,
    }
    later = {"input_tokens": 3, "cache_read_input_tokens": 25_000, "output_tokens": 400}
    write(session / "subagents" / "agent-a1.jsonl", [first, later])
    (session / "subagents" / "agent-a1.meta.json").write_text(json.dumps({"agentType": "code-reviewer"}))
    write(session / "subagents" / "agent-a2.jsonl", [first])  # no meta file
    types, main = subagents.measure(sorted(tmp_path.rglob("*.jsonl")))
    assert set(types) == {"code-reviewer", "unknown"} and main > 0
    reviewer = types["code-reviewer"]
    assert (reviewer.runs, reviewer.calls, reviewer.first_prompt, reviewer.first_read) == (1, 2, 25_003, 5_000)
    assert 0 < reviewer.first_cost < reviewer.cost
    out = subagents.render(types, main)
    assert out.startswith("Subagents: $") and "code-reviewer" in out and "unknown" in out
    assert subagents.render({}, 1.0) == "Subagents: none found in these transcripts."
