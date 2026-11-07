# Bench: Alloy on a frozen toy project with the cheapest models

`bench/sheet/` is a deliberately vague, deliberately too-hard target project (a
command-line spreadsheet engine) that Alloy builds with composer-2.5 and
claude haiku only. A hidden oracle (`sheet/oracle/`, ~150 protocol cases in
16 areas) measures how far the factory got; `sheet/reference/` is a reference
implementation that proves the oracle measures the spec (it scores 1.0, the
seed 0.0). Neither is ever copied into a toy checkout.

- `sheet/seed/` -- the starting repository, with traps: a flaky test
  (`tests/test_ids.py`), a long-red unrelated test (`tests/test_units.py`) and a
  name-based affected-test selector (`scripts/affected.py`).
- `sheet/beads/<scenario>.yaml` -- frozen bead sets (explicit ids, deps, the
  oracle areas each bead is meant to deliver); `sheet/overlays/<scenario>/` --
  files laid over the seed for targeted scenarios.
- `sheet/recipes/` -- `toy-cheap` (tdd-loop) and `toy-cheap-fast` (fast-track).
- `sheet/results/` -- one JSON report per run.

```bash
uv run python -I bench/run.py smoke                                   # both cheap models answer
uv run python -I bench/run.py campaign --scenario full --runs 3 --alloy-ref HEAD
uv run python -I bench/run.py report --dir ~/MY_SRC/alloy-toy-sheet-full-1 --alloy-ref <sha>
python3 -I bench/sheet/oracle/oracle.py --toy <dir> --verbose          # score any checkout
```

Every run starts from the same seed, the same beads, an empty `.alloy/`, an
empty bd memory (except the default recipe) and an isolated `ALLOY_HOME`.
Alloy runs from a worktree at `--alloy-ref` (`~/MY_SRC/alloy-bench-ref/<sha>`,
with `bench/` removed), so working-checkout edits never touch a running
campaign. Nobody intervenes: the recipe's `dispatch.park_after_human_minutes`
parks held human gates. A report whose `transcript_audit` is non-empty saw the
oracle and is invalid.
