import json

from lightfoot import evaluate, handoff
from lightfoot.integrations import detect


def test_condense_transcript(tmp_path):
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Fix the login bug"}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Looking at auth.py"},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/repo/auth.py", "old_string": "a", "new_string": "b"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"id": "side", "content": [{"type": "text", "text": "subagent"}]},
        },
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}],
            },
        },
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in lines) + "\nnot json\n", encoding="utf-8")
    text, turns, touched = handoff.condense(path)
    assert turns == 2
    assert touched == ["/repo/auth.py"]
    assert "USER: Fix the login bug" in text and "TOOL Bash: pytest -q" in text and "subagent" not in text


def test_write_note(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    path = handoff.write_note(vault, "Claude Handoffs", str(tmp_path / "myproj"), "abc", "## Summary\nDone.", ["/a.py"])
    text = path.read_text(encoding="utf-8")
    assert path.parent == vault / "Claude Handoffs"
    assert text.startswith("---\ntype: handoff\nproject: myproj")
    assert "## Files touched\n- `/a.py`" in text
    second = handoff.write_note(vault, "Claude Handoffs", str(tmp_path / "myproj"), "abc", "x", [])
    assert second != path


def test_install_puts_hook_first_and_uninstall_restores(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "graphify vault refresh"}]}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(handoff, "settings_path", lambda: settings)
    handoff.install()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    commands = [e["hooks"][0]["command"] for e in data["hooks"]["SessionEnd"]]
    assert handoff.HOOK_MARK in commands[0] and commands[1] == "graphify vault refresh"
    assert handoff.hook_status() == {"installed": True, "after_graphify": False}
    handoff.install()  # idempotent
    assert len(json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]) == 2
    assert handoff.uninstall()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionEnd"] == [{"hooks": [{"type": "command", "command": "graphify vault refresh"}]}]
    assert list(tmp_path.glob("settings.json.lightfoot-backup-*"))


def test_hook_is_silent_without_vault_and_under_guard(tmp_path, monkeypatch, cfg):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    payload = json.dumps({"cwd": str(tmp_path), "transcript_path": str(transcript), "session_id": "s"})
    assert handoff.run_hook(payload) == 0
    monkeypatch.setenv(handoff.GUARD_ENV, "1")
    assert handoff.run_hook(payload) == 0


def test_detection(tmp_path, cfg):
    assert detect(tmp_path, cfg).graphify_report is None
    report = tmp_path / "graphify-out" / "GRAPH_REPORT.md"
    report.parent.mkdir()
    report.write_text("# report", encoding="utf-8")
    assert detect(tmp_path, cfg).graphify_report == str(report.resolve())
    cfg["integrations"]["graphify"] = "off"
    assert detect(tmp_path, cfg).graphify_report is None
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg["integrations"]["vault_path"] = str(vault)
    assert detect(tmp_path, cfg).vault is None  # no .obsidian folder
    (vault / ".obsidian").mkdir()
    assert detect(tmp_path, cfg).vault == str(vault.resolve())


def test_eval_report_gate(tmp_path):
    path = tmp_path / "run.jsonl"
    records = []
    for i in range(10):
        records.append(
            {"task": f"t{i % 5}", "arm": "baseline", "passed": True, "cost": 1.0, "turns": 10, "output_tokens": 500}
        )
        records.append(
            {"task": f"t{i % 5}", "arm": "proxy", "passed": True, "cost": 0.7, "turns": 10, "output_tokens": 500}
        )
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    text = evaluate.report(str(path))
    assert "Median cost change: -30.0%" in text and "Ship gate: PASS" in text
    records[-1]["passed"] = False
    records[-3]["passed"] = False
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    assert "Ship gate: FAIL" in evaluate.report(str(path))


def test_tool_calls_split_retrieval_and_execution(tmp_path):
    def assistant(*blocks):
        return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}

    def use(tid, name, inp):
        return {"type": "tool_use", "id": tid, "name": name, "input": inp}

    lines = [
        assistant(use("a", "Read", {"file_path": "/r/x.py"}), use("b", "Grep", {"pattern": "x"})),
        assistant(use("c", "Bash", {"command": "pytest"})),
        assistant(use("d", "Read", {"file_path": "/r/.lightfoot/history/s-9.md"})),
        assistant(use("a", "Read", {"file_path": "/r/x.py"})),  # same id again: a split entry, counted once
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ]
    transcript = tmp_path / "proj" / "sess-1.jsonl"
    transcript.parent.mkdir()
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    assert evaluate.find_transcript("sess-1", tmp_path) == transcript
    assert evaluate.find_transcript("missing", tmp_path) is None
    assert evaluate.tool_calls(transcript) == {"retrieval_calls": 3, "execution_calls": 1, "condensed_reads": 1}


def test_paired_intervals_are_reproducible_and_contain_the_estimate():
    pairs = [(1.0, 0.7), (2.0, 1.5), (1.5, 1.2), (3.0, 2.0), (1.0, 0.9)]
    first = evaluate.paired_ci(pairs, evaluate._relative_change)
    assert first == evaluate.paired_ci(pairs, evaluate._relative_change)
    point, low, high = first
    assert low <= point <= high < 0


def test_eval_report_shows_paired_changes_and_retrieval(tmp_path):
    path = tmp_path / "run.jsonl"
    records = []
    for i in range(6):
        common = {"task": f"t{i % 3}", "run": i // 3, "passed": True, "turns": 10, "output_tokens": 500}
        records.append({**common, "arm": "baseline", "cost": 1.0, "retrieval_calls": 10, "execution_calls": 5})
        records.append({**common, "arm": "proxy", "cost": 0.7, "retrieval_calls": 14, "execution_calls": 5})
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    text = evaluate.report(str(path))
    assert "Paired over 6 runs" in text and "Cost" in text and "-30.0%" in text
    assert "Retrieval within +10%: NO" in text
