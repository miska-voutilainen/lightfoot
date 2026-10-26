# Benchmark

A paired, reproducible benchmark of what Lightfoot saves and whether it costs any quality, on long tasks in pinned open-source repositories.

---

## Tasks

Version 1 has three tasks. Each is long on purpose, because Lightfoot's saving comes from sessions past 150k tokens.

| Task | Repository and commit | Work | Passes when |
| --- | --- | --- | --- |
| `boltons-types` | [mahmoud/boltons](https://github.com/mahmoud/boltons) at `4e5faa3d7e40` | Type-annotate `iterutils.py` and `strutils.py`, about 3,000 lines | `mypy --strict` reports no errors in them, all 525 tests pass, `tests/` unchanged |
| `toolz-types` | [pytoolz/toolz](https://github.com/pytoolz/toolz) at `451af60dec59` | Type-annotate `itertoolz.py` and `dicttoolz.py` | `mypy --strict` reports no errors in them, all 193 tests pass, test files unchanged |
| `toolz-split` | the same | Split `functoolz.py` into a package of at least four modules, keeping every import path | `functoolz.py` is gone, the package exists, all tests pass, test files unchanged |

Every check is mechanical, and none can be met by editing the tests: each one first verifies that the test files are identical to the pinned commit. `lightfoot eval check bench/tasks.toml` confirms, without any model calls, that each task's setup works and its check fails before any change. All three passed on 2026-10-25.

---

## Method

| Choice | Why |
| --- | --- |
| Two arms: `baseline` (proxy in passthrough) and `proxy` (live) | Both go through the proxy, so both are measured the same way |
| The same model, `claude-opus-5-5`, in both arms | Model differences don't leak into the comparison |
| Runs interleaved, arm by arm | Slow drift (load, model updates) hits both arms equally |
| 5 runs per task and arm, paired by task and run index | Task difficulty cancels out in the differences |
| 95% bootstrap intervals, fixed seed | The report is reproducible from the raw results |
| Pass rate, cost, turns, and retrieval calls (reads, searches) per task | Compression can hide its cost in extra re-reads rather than in failures |

Each run gets a fresh git worktree and its own virtualenv (`.bench-venv`, created by [benchenv.py](benchenv.py)), so nothing is installed into your Python and runs can't affect each other.

---

## Running it

```sh
lightfoot eval check bench/tasks.toml          # free: validates every task
lightfoot eval run bench/tasks.toml --runs 1   # a pilot pair, to measure the cost per run
lightfoot eval run bench/tasks.toml            # the full benchmark: 5 runs per task and arm
lightfoot eval report                          # the results, with 95% intervals
```

The tasks run with `permission_mode = "bypassPermissions"`, because they must run mypy and pytest headless. Every run happens inside a throwaway worktree.

---

## Cost

Running the benchmark spends real model usage: 3 tasks × 2 arms × 5 runs is 30 long Claude Code sessions. A long session on Claude Opus 5.5 can cost between about $5 and $30 at API prices, so the full run is likely $150 to $900. Run one pilot pair first and multiply. On a Pro or Max subscription it uses your usage limits instead.

---

## Publishing results

A result is worth publishing when it includes the raw `~/.lightfoot/eval/<run>.jsonl`, the Lightfoot and Claude Code versions, and the output of `lightfoot eval report`. Numbers without the paired intervals aren't comparable.
