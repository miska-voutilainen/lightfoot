"""scripts/hook_watch.py: each agent's hook output fields, parsed from its source (offline here)."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hook_watch.py"
spec = importlib.util.spec_from_file_location("hook_watch", SCRIPT)
hook_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook_watch)

GEMINI = """### `BeforeTool`

- **Relevant Output Fields**:
  - `decision`: before only.

### `AfterTool`

Fires after a tool executes.

- **Input Fields**:
  - `tool_name`: (`string`)
- **Relevant Output Fields**:
  - `decision`: Set to `"deny"` to hide the real tool output.
  - `reason`: Replaces the result.
  - `hookSpecificOutput.additionalContext`: Appended.

---

## Agent hooks
"""

CODEX = """pub(crate) struct PostToolUseCommandOutputWire {
    #[serde(default)]
    pub decision: Option<BlockDecisionWire>,
}

pub(crate) struct PostToolUseHookSpecificOutputWire {
    #[serde(default)]
    #[serde(rename = "updatedMCPToolOutput")]
    pub updated_mcp_tool_output: Option<Value>,
}
"""

CLINE = """### Output (via stdout as JSON)

```json
{
  "cancel": boolean,
  "contextModification": "string"
}
```

### Examples
"""


def test_each_parser_reads_only_the_after_tool_hooks_output_fields():
    assert hook_watch._gemini(GEMINI) == {"decision", "reason", "hookSpecificOutput.additionalContext"}
    assert hook_watch._codex(CODEX) == {
        "PostToolUseCommandOutputWire.decision",
        "PostToolUseHookSpecificOutputWire.updated_mcp_tool_output",
        "updatedMCPToolOutput",
    }
    assert hook_watch._cline(CLINE) == {"cancel", "contextModification"}


def test_the_baseline_covers_every_watched_agent():
    import json

    baseline = json.loads(hook_watch.BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) == set(hook_watch.SOURCES)
