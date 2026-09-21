"""Pure rendering functions in `alloy.monitor.render`.

`run_rows(snapshot) -> list[tuple[str, ...]]` and `header_line(snapshot) -> str`
are unit-tested here against synthetic snapshot dicts shaped exactly like the
frozen JSON in "Component 2"/"Component 3" of docs/plans/execution-monitor.md.
No Textual involved -- see tests/test_monitor_view.py for the pilot tests.
"""

from __future__ import annotations

from alloy.monitor.render import header_line, run_rows

EMPTY_TOKENS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None}


def _snapshot(*, runs=(), scheduler=None, ready_count=0, lifetime=None) -> dict:
    return {
        "root": "/home/vader/.alloy",
        "repo": "/home/vader/MY_SRC/alloy",
        "scheduler": scheduler or {"running": False, "pid": None},
        "ready_count": ready_count,
        "ready_capped_at": 1000,
        "lifetime": lifetime or {"done": 0, "failed": 0, "cancelled": 0},
        "runs": list(runs),
    }


def _run(
    *,
    bead_id="alloy-a1b2",
    run_id="run-1",
    recipe="tdd-loop-jev",
    status="running",
    stage="implement",
    iteration=2,
    max_iterations=5,
    consiliums=0,
    max_consiliums=1,
    tests_summary="3 passed, 1 failed",
    elapsed_minutes=7,
    current_calls=(),
    tokens=None,
    tokens_by_role=None,
    judge=None,
    worktree="/home/vader/.alloy/worktrees/alloy-a1b2",
    branch="alloy/alloy-a1b2",
) -> dict:
    return {
        "bead_id": bead_id,
        "run_id": run_id,
        "recipe": recipe,
        "status": status,
        "stage": stage,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "consiliums": consiliums,
        "max_consiliums": max_consiliums,
        "tests_summary": tests_summary,
        "elapsed_minutes": elapsed_minutes,
        "current_calls": list(current_calls),
        "tokens": tokens or dict(EMPTY_TOKENS),
        "tokens_by_role": tokens_by_role or {},
        "judge": judge,
        "worktree": worktree,
        "branch": branch,
    }


def _call(role="implement", requested_runner="astra", effective_runner="codex",
          requested_model=None, effective_model=None, elapsed_seconds=42.1) -> dict:
    return {
        "role": role,
        "requested_runner": requested_runner,
        "effective_runner": effective_runner,
        "requested_model": requested_model,
        "effective_model": effective_model,
        "elapsed_seconds": elapsed_seconds,
    }


def _judge(raw_decision="retry", raw_confidence=0.61, effective_decision="retry",
           effective_reason="a specific fix remains", matches=True) -> dict:
    return {
        "raw": {"decision": raw_decision, "confidence": raw_confidence},
        "effective": {"decision": effective_decision, "reason": effective_reason},
        "matches_effective": matches,
    }


COLUMN_COUNT = 11  # bead, recipe, status, stage, i/max, c/max, tests, elapsed, now, tokens, judge


# -- run_rows -----------------------------------------------------------------


def test_zero_runs_yields_no_rows():
    assert run_rows(_snapshot(runs=[])) == []


def test_one_row_per_run_with_the_documented_column_count():
    snapshot = _snapshot(runs=[_run(run_id="run-1"), _run(run_id="run-2", bead_id="alloy-c3d4")])

    rows = run_rows(snapshot)

    assert len(rows) == 2
    for row in rows:
        assert isinstance(row, tuple)
        assert len(row) == COLUMN_COUNT


def test_row_identifies_bead_recipe_status_and_stage():
    snapshot = _snapshot(runs=[_run(
        bead_id="alloy-a1b2", recipe="tdd-loop-jev", status="running", stage="implement",
    )])

    row = run_rows(snapshot)[0]

    assert row[0] == "alloy-a1b2"
    assert row[1] == "tdd-loop-jev"
    assert row[2] == "running"
    assert row[3] == "implement"


def test_row_formats_iteration_and_consilium_progress_as_i_over_max():
    snapshot = _snapshot(runs=[_run(iteration=2, max_iterations=5, consiliums=1, max_consiliums=3)])

    row = run_rows(snapshot)[0]

    assert row[4] == "2/5"
    assert row[5] == "1/3"


def test_row_with_no_current_calls_shows_a_dash_in_the_now_column():
    snapshot = _snapshot(runs=[_run(current_calls=[])])

    row = run_rows(snapshot)[0]

    assert row[8] == "-"


def test_row_with_two_current_calls_joins_them_with_a_plus_in_the_now_column():
    calls = [
        _call(role="implement", effective_runner="codex", elapsed_seconds=42.1),
        _call(role="critic", effective_runner="claude", elapsed_seconds=3.4),
    ]
    snapshot = _snapshot(runs=[_run(current_calls=calls)])

    row = run_rows(snapshot)[0]

    assert " + " in row[8]
    assert "implement" in row[8]
    assert "codex" in row[8]
    assert "critic" in row[8]
    assert "claude" in row[8]


def test_row_with_null_judge_shows_a_dash_in_the_judge_column():
    snapshot = _snapshot(runs=[_run(judge=None)])

    row = run_rows(snapshot)[0]

    assert row[10] == "-"


def test_row_with_matching_judge_shows_only_the_single_decision():
    snapshot = _snapshot(runs=[_run(judge=_judge(
        raw_decision="done", effective_decision="done", matches=True,
    ))])

    row = run_rows(snapshot)[0]

    assert "done" in row[10]
    assert "→" not in row[10]


def test_row_with_differing_raw_and_effective_judge_shows_both_decisions():
    snapshot = _snapshot(runs=[_run(judge=_judge(
        raw_decision="done", effective_decision="retry", matches=False,
    ))])

    row = run_rows(snapshot)[0]

    assert "done" in row[10]
    assert "retry" in row[10]
    assert row[10] != "done"
    assert row[10] != "retry"


def test_row_carries_the_tests_summary_verbatim():
    snapshot = _snapshot(runs=[_run(tests_summary="3 passed, 1 failed")])

    row = run_rows(snapshot)[0]

    assert row[6] == "3 passed, 1 failed"


def test_row_elapsed_column_reflects_elapsed_minutes():
    snapshot = _snapshot(runs=[_run(elapsed_minutes=7)])

    row = run_rows(snapshot)[0]

    assert "7" in row[7]


def test_row_tokens_column_reflects_total_tokens_when_no_split_is_available():
    snapshot = _snapshot(runs=[_run(tokens={
        "input_tokens": None, "output_tokens": None, "total_tokens": 8635, "cost_usd": None,
    })])

    row = run_rows(snapshot)[0]

    assert "8635" in row[9]


# -- header_line ----------------------------------------------------------


def test_header_line_reports_scheduler_not_running():
    snapshot = _snapshot(scheduler={"running": False, "pid": None})

    line = header_line(snapshot)

    assert isinstance(line, str)
    assert "12345" not in line


def test_header_line_reports_scheduler_running_with_pid():
    snapshot = _snapshot(scheduler={"running": True, "pid": 12345})

    line = header_line(snapshot)

    assert "12345" in line


def test_header_line_reports_ready_count_and_its_cap():
    snapshot = _snapshot(ready_count=3)

    line = header_line(snapshot)

    assert "3" in line
    assert "1000" in line


def test_header_line_reports_lifetime_totals():
    snapshot = _snapshot(lifetime={"done": 12, "failed": 2, "cancelled": 1})

    line = header_line(snapshot)

    assert "12" in line
    assert "2" in line
    assert "1" in line
