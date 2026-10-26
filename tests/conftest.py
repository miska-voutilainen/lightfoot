import copy
import time

import pytest

from lightfoot.config import DEFAULTS
from lightfoot.store import Store
from lightfoot.transform import Context


@pytest.fixture(autouse=True)
def lightfoot_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LIGHTFOOT_HOME", str(home))
    return home


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def lift_budgets(project, epochs=""):
    """Give a launch's project the cfg fixture's time budgets, for tests through the real server.

    The server loads each launch's own config, so without this a loaded machine can push a
    request over the real budgets and the fail-safe (rightly) drops the rewrite. `epochs` adds
    lines to the [epochs] table.
    """
    (project / ".lightfoot").mkdir(parents=True, exist_ok=True)
    (project / ".lightfoot" / "config.toml").write_text(
        f"rewrite_budget_ms = 10000\n\n[epochs]\nbudget_ms = 10000\n{epochs}", encoding="utf-8"
    )
    return project


@pytest.fixture
def cfg():
    config = copy.deepcopy(DEFAULTS)
    # Time budgets depend on machine load; tests that check budgets set their own.
    config["rewrite_budget_ms"] = 10_000
    config["epochs"]["budget_ms"] = 10_000
    return config


@pytest.fixture
def make_ctx(store, cfg, tmp_path):
    def make(mode="live", session=None, enforced=False, graphify=None, **overrides):
        c = Context(
            cfg=overrides.pop("config", cfg),
            store=store,
            mode=mode,
            session=session,
            spill_dir=tmp_path / "proj" / ".lightfoot" / "spill",
            graphify_report=graphify,
            enforced=enforced,
            started=time.perf_counter(),
            now=overrides.pop("now", time.time()),
        )
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    return make


# --- request builders -----------------------------------------------------------

SYSTEM = [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}]


def user_text(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def tool_use(tid, name, inp, thinking=False, text=None):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": f"sig-{tid}"})
    if text:
        content.append({"type": "text", "text": text})
    content.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
    return {"role": "assistant", "content": content}


def tool_result(tid, content, is_error=False, extra=None):
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    if extra:
        block.update(extra)
    return {"role": "user", "content": [block]}


def request(messages, model="claude-opus-5-5", **extra):
    body = {
        "model": model,
        "max_tokens": 32000,
        "system": copy.deepcopy(SYSTEM),
        "messages": messages,
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        "stream": True,
    }
    body.update(extra)
    return body


def big_output(lines=2000, error_at=(1000,)):
    out = []
    for i in range(lines):
        if i in error_at:
            out.append(f"FAILED tests/test_mod.py::test_case_{i} - AssertionError: expected 3, got 4")
        else:
            out.append(f"\x1b[32mPASSED\x1b[0m tests/test_mod.py::test_case_{i} in 0.0{i % 10}s")
    return "\n".join(out)


def read_output(n_lines=120, start=1, arrow=True):
    sep = "→" if arrow else "\t"
    return "\n".join(f"{i:>6}{sep}line {i} of the file with some content here" for i in range(start, start + n_lines))
