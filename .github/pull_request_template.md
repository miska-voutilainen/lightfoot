## What and why

<!-- What does this change, and what problem does it solve? -->

## Quality and safety

- [ ] Only tool output or a validated prune changes in requests; everything else is byte-identical (the validator passes)
- [ ] Rewrites are deterministic and replayed frozen (same input, same bytes)
- [ ] Any problem drops *new* rewrites only; stored ones keep being replayed
- [ ] Not applicable: this change doesn't touch request rewriting

## Evidence

<!-- Tests added. For rule or threshold changes: `lightfoot estimate` or `lightfoot stats` numbers before and after. -->

- [ ] `pytest`, `ruff check` and `ruff format --check` pass
- [ ] CHANGELOG.md updated under "Unreleased"
