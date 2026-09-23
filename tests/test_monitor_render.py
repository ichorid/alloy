"""Pure rendering functions in `alloy.monitor.render`.

`run_rows(snapshot) -> list[tuple[str, ...]]` and `header_line(snapshot) -> str`
are unit-tested here against synthetic snapshot dicts shaped exactly like the
frozen JSON in "Component 2"/"Component 3" of docs/plans/execution-monitor.md.
No Textual involved -- see tests/test_monitor_view.py for the pilot tests.
"""

from __future__ import annotations

from alloy.monitor.render import COLUMNS, header_line, run_rows

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
    parent_run_id=None,
    parent_bead_id=None,
    complexity=None,
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
        "parent_run_id": parent_run_id,
        "parent_bead_id": parent_bead_id,
        "complexity": complexity,
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


COLUMN_COUNT = 13  # parent, bead, recipe, status, stage, i/max, c/max, tests, elapsed, now, tokens, judge, complexity


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

    assert row[1] == "alloy-a1b2"
    assert row[2] == "tdd-loop-jev"
    assert row[3] == "running"
    assert row[4] == "implement"


def test_row_formats_iteration_and_consilium_progress_as_i_over_max():
    snapshot = _snapshot(runs=[_run(iteration=2, max_iterations=5, consiliums=1, max_consiliums=3)])

    row = run_rows(snapshot)[0]

    assert row[5] == "2/5"
    assert row[6] == "1/3"


def test_row_with_no_current_calls_shows_a_dash_in_the_now_column():
    snapshot = _snapshot(runs=[_run(current_calls=[])])

    row = run_rows(snapshot)[0]

    assert row[9] == "-"


def test_row_with_two_current_calls_joins_them_with_a_plus_in_the_now_column():
    calls = [
        _call(role="implement", effective_runner="codex", elapsed_seconds=42.1),
        _call(role="critic", effective_runner="claude", elapsed_seconds=3.4),
    ]
    snapshot = _snapshot(runs=[_run(current_calls=calls)])

    row = run_rows(snapshot)[0]

    assert " + " in row[9]
    assert "implement" in row[9]
    assert "codex" in row[9]
    assert "critic" in row[9]
    assert "claude" in row[9]


def test_row_with_null_judge_shows_a_dash_in_the_judge_column():
    snapshot = _snapshot(runs=[_run(judge=None)])

    row = run_rows(snapshot)[0]

    assert row[11] == "-"


def test_row_with_matching_judge_shows_only_the_single_decision():
    snapshot = _snapshot(runs=[_run(judge=_judge(
        raw_decision="done", effective_decision="done", matches=True,
    ))])

    row = run_rows(snapshot)[0]

    assert "done" in row[11]
    assert "→" not in row[11]


def test_row_with_differing_raw_and_effective_judge_shows_both_decisions():
    snapshot = _snapshot(runs=[_run(judge=_judge(
        raw_decision="done", effective_decision="retry", matches=False,
    ))])

    row = run_rows(snapshot)[0]

    assert "done" in row[11]
    assert "retry" in row[11]
    assert row[11] != "done"
    assert row[11] != "retry"


def test_row_carries_the_tests_summary_verbatim():
    snapshot = _snapshot(runs=[_run(tests_summary="3 passed, 1 failed")])

    row = run_rows(snapshot)[0]

    assert row[7] == "3 passed, 1 failed"


def test_row_elapsed_column_reflects_elapsed_minutes():
    snapshot = _snapshot(runs=[_run(elapsed_minutes=7)])

    row = run_rows(snapshot)[0]

    assert "7" in row[8]


def test_row_tokens_column_reflects_total_tokens_when_no_split_is_available():
    snapshot = _snapshot(runs=[_run(tokens={
        "input_tokens": None, "output_tokens": None, "total_tokens": 8635, "cost_usd": None,
    })])

    row = run_rows(snapshot)[0]

    assert "8635" in row[10]


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


# -- alloy-w9d.10: parent column, limits_lines, session totals --------------


def test_columns_start_with_parent():
    assert COLUMNS[0] == "parent"


def test_parent_column_shows_parent_bead_id_for_child_run():
    parent = _run(
        run_id="parent-run", bead_id="alloy-parent", parent_bead_id=None, complexity="simple",
    )
    child = _run(
        run_id="child-run", bead_id="alloy-child", parent_run_id="parent-run",
        parent_bead_id="alloy-parent",
    )
    rows = run_rows(_snapshot(runs=[parent, child]))

    child_row = next(row for row in rows if row[1] == "alloy-child")
    parent_row = next(row for row in rows if row[1] == "alloy-parent")
    assert child_row[0] == "alloy-parent"
    assert "└" not in child_row[1]
    assert parent_row[0] == "-"


def test_top_level_run_parent_column_is_dash():
    row = run_rows(_snapshot(runs=[_run(parent_bead_id=None)]))[0]
    assert row[0] == "-"


def test_limits_lines_renders_claude_windows_and_unavailable_codex():
    from alloy.limits import window
    from alloy.monitor.render import limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = {
        "claude": {
            "harness": "claude",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "oauth-usage-api",
            "error": None,
            "status": None,
            "windows": [
                window("five_hour", "5h", 42.0, None),
                window("seven_day", "weekly", 61.0, None),
                window("seven_day_fable", "weekly fable", 12.0, None, model="fable"),
                window("seven_day_opus", "weekly opus", 80.0, None, model="opus"),
            ],
        },
        "codex": {
            "harness": "codex",
            "installed": True,
            "available": False,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": None,
            "source": None,
            "error": "no local sample",
            "status": "workspace_member_credits_depleted",
            "windows": [],
        },
    }

    lines = limits_lines(snapshot)

    assert len(lines) == 2
    claude_line, codex_line = lines
    assert claude_line.index("5h") < claude_line.index("weekly 61%")
    assert claude_line.index("42%") < claude_line.index("weekly 61%")
    assert claude_line.index("weekly 61%") < claude_line.index("weekly fable 12%")
    assert claude_line.index("weekly fable 12%") < claude_line.index("weekly opus 80%")
    assert "unavailable: no local sample" in codex_line
    assert "[workspace_member_credits_depleted]" in codex_line


def test_limits_lines_on_empty_limits_returns_empty_list():
    from alloy.monitor.render import limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = {}

    assert limits_lines(snapshot) == []


def test_limits_lines_align_first_window_across_harnesses():
    from alloy.limits import window
    from alloy.monitor.render import LIMITS_HARNESS_WIDTH, limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = {
        "claude": {
            "harness": "claude",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "oauth-usage-api",
            "error": None,
            "status": None,
            "windows": [window("five_hour", "5h", 42.0, None)],
        },
        "codex": {
            "harness": "codex",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "session-rollout",
            "error": None,
            "status": None,
            "windows": [window("primary", "5h", 7.0, None)],
        },
        "cursor": {
            "harness": "cursor",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "dashboard-api",
            "error": None,
            "status": None,
            "windows": [window("total", "cycle", 25.0, None)],
        },
    }

    lines = limits_lines(snapshot)
    labels = ("claude", "codex", "cursor")

    assert len(lines) == 3
    for line, harness in zip(lines, labels, strict=True):
        assert line.startswith(harness.ljust(LIMITS_HARNESS_WIDTH) + "  ")
    bar_starts = [_first_usage_bar_index(line) for line in lines]
    assert bar_starts[0] == bar_starts[1] == bar_starts[2]


def _first_usage_bar_index(line: str) -> int:
    indices = _usage_bar_indices(line)
    if not indices:
        raise AssertionError(f"no usage bar in: {line}")
    return indices[0]


def _usage_bar_indices(line: str) -> list[int]:
    import re

    return [match.start() for match in re.finditer(r"\[[█░]", line)]


def test_limits_lines_align_weekly_window_across_harnesses():
    from alloy.limits import window
    from alloy.monitor.render import limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = {
        "claude": {
            "harness": "claude",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "oauth-usage-api",
            "error": None,
            "status": None,
            "windows": [
                window("five_hour", "5h", 42.0, None),
                window("seven_day", "weekly", 98.0, None),
            ],
        },
        "codex": {
            "harness": "codex",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "session-rollout",
            "error": None,
            "status": None,
            "windows": [
                window("primary", "5h", 7.0, None),
                window("secondary", "weekly", 4.0, None),
            ],
        },
        "cursor": {
            "harness": "cursor",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "dashboard-api",
            "error": None,
            "status": None,
            "windows": [window("total", "cycle", 30.0, None)],
        },
    }

    lines = limits_lines(snapshot)
    weekly_bars = [_usage_bar_indices(line)[1] for line in lines[:2]]
    assert weekly_bars[0] == weekly_bars[1]
    assert "weekly" in lines[0] and "weekly" in lines[1]


# -- alloy-o89.2: color-coded threshold bars in limits panel ------------------


def _available_claude_limits(*windows) -> dict:
    return {
        "claude": {
            "harness": "claude",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "oauth-usage-api",
            "error": None,
            "status": None,
            "windows": list(windows),
        },
    }


def _claude_limits_line(*windows) -> str:
    from alloy.monitor.render import limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = _available_claude_limits(*windows)
    return limits_lines(snapshot)[0]


def _assert_bracketed_usage_bar(line: str, percent: int, color: str) -> None:
    assert f"[{color}]" in line and f"{percent}%" in line
    for start in range(len(line)):
        if line[start] != "[":
            continue
        end = line.index("]", start)
        bar = line[start + 1 : end]
        if bar and ("█" in bar or "░" in bar) and all(ch in "█░" for ch in bar):
            return
    raise AssertionError(f"no bracketed usage bar in: {line}")


def test_limits_line_at_42_percent_renders_green_bracketed_bar():
    from alloy.limits import window

    line = _claude_limits_line(window("five_hour", "5h", 42.0, None))

    _assert_bracketed_usage_bar(line, 42, "#7ee787")


def test_limits_line_at_71_percent_renders_yellow_bracketed_bar():
    from alloy.limits import window

    line = _claude_limits_line(window("five_hour", "5h", 71.0, None))

    _assert_bracketed_usage_bar(line, 71, "#e3b341")


def test_limits_line_at_92_percent_renders_red_bracketed_bar():
    from alloy.limits import window

    line = _claude_limits_line(window("five_hour", "5h", 92.0, None))

    _assert_bracketed_usage_bar(line, 92, "#f85149")


def test_limits_line_stale_window_renders_stale_badge():
    from alloy.limits import window

    win = window("five_hour", "5h", 42.0, None)
    win["stale"] = True
    line = _claude_limits_line(win)

    assert "[#e3b341][stale][/]" in line


def test_limits_line_unavailable_harness_renders_error_in_red():
    from alloy.monitor.render import limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = {
        "codex": {
            "harness": "codex",
            "installed": True,
            "available": False,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": None,
            "source": None,
            "error": "no local sample",
            "status": None,
            "windows": [],
        },
    }

    line = limits_lines(snapshot)[0]

    assert "[#f85149]unavailable: no local sample[/]" in line


def test_header_line_includes_session_totals_when_present():
    snapshot = _snapshot()
    snapshot["session_totals"] = {"done": 1, "failed": 1, "cancelled": 0}

    line = header_line(snapshot)

    assert "this session: done 1 failed 1 cancelled 0" in line


def test_header_line_omits_session_totals_when_key_absent():
    snapshot = _snapshot(lifetime={"done": 12, "failed": 2, "cancelled": 1})

    line = header_line(snapshot)

    assert "this session" not in line


# -- alloy-0uc.9: complexity in render ---------------------------------------


def test_parent_run_row_shows_complexity_when_set():
    parent = _run(complexity="simple")
    row = run_rows(_snapshot(runs=[parent]))[0]

    assert "simple" in row


# -- alloy-o89.3: status -> color for DataTable badges -------------------------


def test_status_color_maps_running_to_cyan():
    from alloy.monitor.render import status_color

    assert status_color("running") == "#56b6c2"


def test_status_color_maps_judge_to_magenta():
    from alloy.monitor.render import status_color

    assert status_color("judge") == "#d2a8ff"


def test_status_color_maps_blocked_to_red():
    from alloy.monitor.render import status_color

    assert status_color("blocked") == "#f85149"


def test_status_color_maps_done_to_green():
    from alloy.monitor.render import status_color

    assert status_color("done") == "#7ee787"


def test_status_color_maps_ready_to_dim_gray():
    from alloy.monitor.render import status_color

    assert status_color("ready") == "#8b949e"


# -- alloy-o89.4: width-tiered column visibility --------------------------------


def test_breakpoint_constants_are_named_and_exported():
    from alloy.monitor.render import COMFORTABLE_WIDTH, WIDE_WIDTH

    assert COMFORTABLE_WIDTH == 80
    assert WIDE_WIDTH == 100


def test_column_tiers_label_wide_only_columns():
    from alloy.monitor.render import column_tier

    for name in ("parent", "stage", "cons", "complexity"):
        assert column_tier(name) == "wide"


def test_column_tiers_label_comfortable_only_column():
    from alloy.monitor.render import column_tier

    assert column_tier("recipe") == "comfortable"


def test_column_tiers_label_always_shown_columns():
    from alloy.monitor.render import column_tier

    always = {"bead", "status", "iter", "tests", "elapsed", "now", "tokens", "judge"}
    for name in always:
        assert column_tier(name) == "always"


def test_visible_columns_at_wide_width_includes_every_column():
    from alloy.monitor.render import COLUMNS, WIDE_WIDTH, visible_columns

    assert visible_columns(WIDE_WIDTH) == COLUMNS


def test_visible_columns_at_comfortable_width_omits_wide_only_columns():
    from alloy.monitor.render import COLUMNS, COMFORTABLE_WIDTH, visible_columns

    expected = tuple(name for name in COLUMNS if name not in {"parent", "stage", "cons", "complexity"})
    assert visible_columns(COMFORTABLE_WIDTH) == expected
    assert visible_columns(99) == expected


def test_visible_columns_below_comfortable_width_also_omits_recipe():
    from alloy.monitor.render import COLUMNS, visible_columns

    expected = tuple(
        name for name in COLUMNS if name not in {"parent", "stage", "cons", "complexity", "recipe"}
    )
    assert visible_columns(79) == expected
