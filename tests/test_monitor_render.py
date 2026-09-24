"""Pure rendering functions in `alloy.monitor.render`.

`run_rows(snapshot) -> list[tuple[str, ...]]` and `header_line(snapshot) -> str`
are unit-tested here against synthetic snapshot dicts shaped exactly like the
frozen JSON in "Component 2"/"Component 3" of docs/plans/execution-monitor.md.
No Textual involved -- see tests/test_monitor_view.py for the pilot tests.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alloy.monitor.icons import icon
from alloy.monitor.render import COLUMNS, header_line, run_rows

EMPTY_TOKENS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None}
WIDE_WIDTH = 100


def _empty_queue() -> dict:
    return {"ready": [], "ready_total": 0, "blocked": []}


def _ready_bead(
    bead_id: str,
    *,
    title: str = "",
    recipe: str = "tdd-loop",
    priority: int = 2,
    complexity: str | None = None,
    epic_id: str | None = None,
) -> dict:
    return {
        "bead_id": bead_id,
        "title": title or bead_id,
        "recipe": recipe,
        "priority": priority,
        "complexity": complexity,
        "epic_id": epic_id,
    }


def _blocked_bead(
    bead_id: str,
    blocked_by: list[str],
    *,
    title: str = "",
    epic_id: str | None = None,
) -> dict:
    return {
        "bead_id": bead_id,
        "title": title or bead_id,
        "blocked_by": list(blocked_by),
        "epic_id": epic_id,
    }


def _epic(
    epic_id: str,
    *,
    title: str = "",
    total: int = 0,
    done: int = 0,
    done_ids: tuple[str, ...] = (),
    running: int = 0,
    judge: int = 0,
) -> dict:
    return {
        "epic_id": epic_id,
        "title": title or epic_id,
        "total": total,
        "done": done,
        "done_ids": list(done_ids),
        "running": running,
        "judge": judge,
    }


def _snapshot(
    *,
    runs=(),
    scheduler=None,
    ready_count=0,
    lifetime=None,
    queue=None,
    epics=(),
) -> dict:
    return {
        "root": "/home/vader/.alloy",
        "repo": "/home/vader/MY_SRC/alloy",
        "scheduler": scheduler or {"running": False, "pid": None},
        "ready_count": ready_count,
        "ready_capped_at": 1000,
        "lifetime": lifetime or {"done": 0, "failed": 0, "cancelled": 0},
        "runs": list(runs),
        "queue": queue if queue is not None else _empty_queue(),
        "epics": list(epics),
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
    epic_id=None,
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
        "epic_id": epic_id,
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


COLUMN_COUNT = 12  # bead, recipe, status, stage, i/max, c/max, tests, elapsed, now, tokens, judge, complexity


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


# -- alloy-w9d.10: parent column, limits_lines, session totals --------------


def test_columns_start_with_bead():
    assert COLUMNS[0] == "bead"


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
    # Labels are padded to align the bars; compare on collapsed whitespace.
    claude_line = " ".join(claude_line.split())
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


def test_limits_lines_cursor_cycle_from_probe_on_one_row(tmp_path: Path):
    from test_limits_cursor import (
        RecordingFetch,
        _dashboard_payload_with_api,
        write_auth,
    )

    from alloy.limits.cursor import probe
    from alloy.monitor.render import limits_lines

    home = tmp_path / "home"
    write_auth(home)
    fetch = RecordingFetch(status=200, body=json.dumps(_dashboard_payload_with_api()))

    cursor_sample = probe(home, fetch)
    snapshot = _snapshot()
    snapshot["limits"] = {"cursor": cursor_sample}

    lines = limits_lines(snapshot)
    assert len(lines) == 1
    line = " ".join(lines[0].split())
    assert "cycle" in line
    assert "25%" in line or "24%" in line
    assert len(_usage_bar_indices(lines[0])) == 1


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


# -- alloy-gek: consumed Codex quota + compact local reset suffix --------------


_AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def _freeze_render_now(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    import alloy.monitor.render as render_mod

    real_datetime = render_mod.datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return when.astimezone().replace(tzinfo=None)
            return when.astimezone(tz)

    monkeypatch.setattr(render_mod, "datetime", _FrozenDatetime)


def _limits_line_for_single_window(
    label: str,
    resets_at: str,
    *,
    harness: str = "claude",
    used_percent: float = 42.0,
    key: str = "five_hour",
) -> str:
    from alloy.limits import window
    from alloy.monitor.render import limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = {
        harness: {
            "harness": harness,
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-24T08:00:00+00:00",
            "as_of": "2026-09-24T08:00:00+00:00",
            "source": "oauth-usage-api",
            "error": None,
            "status": None,
            "windows": [window(key, label, used_percent, resets_at)],
        },
    }
    lines = limits_lines(snapshot)
    assert len(lines) == 1
    return lines[0]


def test_limits_line_5h_reset_suffix_is_local_hhmm_without_resets_word(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window("5h", "2026-09-24T14:00:00+00:00")

    assert "resets" not in line
    assert "(16:00)" in line


def test_limits_line_5h_reset_suffix_uses_local_time_on_different_calendar_day(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 22, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window("5h", "2026-09-24T23:00:00+00:00")

    assert "resets" not in line
    assert "(01:00)" in line
    assert "2026-09-25" not in line


def test_limits_line_weekly_reset_suffix_same_local_day_shows_hhmm(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window(
        "weekly",
        "2026-09-24T18:00:00+00:00",
        key="seven_day",
    )

    assert "resets" not in line
    assert "(20:00)" in line
    assert "2026-09-24" not in line


def test_limits_line_weekly_reset_suffix_other_local_day_shows_date(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window(
        "weekly",
        "2026-09-24T23:00:00+00:00",
        key="seven_day",
    )

    assert "resets" not in line
    assert "(2026-09-25)" in line


def test_limits_line_cycle_reset_suffix_matches_weekly_date_rule(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window(
        "cycle",
        "2026-09-24T23:00:00+00:00",
        harness="cursor",
        key="total",
    )

    assert "resets" not in line
    assert "(2026-09-25)" in line


@pytest.fixture
def codex_home(tmp_path):
    return tmp_path / "home"


def test_limits_lines_codex_rollout_shows_consumed_percent_with_compact_reset_suffix(
    codex_home,
    monkeypatch: pytest.MonkeyPatch,
):
    from alloy.limits.codex import probe
    from alloy.monitor.render import limits_lines

    from test_limits_codex import (
        CODEX_TS,
        RESETS_AT_EPOCH,
        _primary,
        _token_count_line,
        write_rollout,
    )

    write_rollout(
        codex_home,
        "session-a",
        "rollout-a.jsonl",
        [
            _token_count_line(
                CODEX_TS,
                limit_id="codex",
                primary=_primary(53, 300, RESETS_AT_EPOCH),
                secondary=_primary(51, 10080, RESETS_AT_EPOCH),
            ),
        ],
    )
    sample = probe(codex_home)
    snapshot = _snapshot()
    snapshot["limits"] = {"codex": sample}
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 12, 5, 0, tzinfo=_AMSTERDAM),
    )

    line = limits_lines(snapshot)[0]

    assert sample["windows"][0]["used_percent"] == 47.0
    assert "47%" in line
    assert "53%" not in line
    assert "resets" not in line
    assert "(05:32)" in line


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

    for name in ("stage", "cons", "complexity", "now"):
        assert column_tier(name) == "wide"


def test_column_tiers_label_comfortable_only_column():
    from alloy.monitor.render import column_tier

    assert column_tier("recipe") == "comfortable"


def test_column_tiers_label_always_shown_columns():
    from alloy.monitor.render import column_tier

    always = {"bead", "status", "iter", "tests", "elapsed", "tokens", "judge"}
    for name in always:
        assert column_tier(name) == "always"


def test_visible_columns_at_wide_width_includes_every_column():
    from alloy.monitor.render import COLUMNS, WIDE_WIDTH, visible_columns

    assert visible_columns(WIDE_WIDTH) == COLUMNS


def test_visible_columns_at_comfortable_width_omits_wide_only_columns():
    from alloy.monitor.render import COLUMNS, COMFORTABLE_WIDTH, visible_columns

    expected = tuple(name for name in COLUMNS if name not in {"stage", "cons", "complexity", "now"})
    assert visible_columns(COMFORTABLE_WIDTH) == expected
    assert visible_columns(99) == expected


def test_visible_columns_below_comfortable_width_also_omits_recipe():
    from alloy.monitor.render import COLUMNS, visible_columns

    expected = tuple(
        name for name in COLUMNS if name not in {"stage", "cons", "complexity", "now", "recipe"}
    )
    assert visible_columns(79) == expected


# -- alloy-3g0.2: Powerline title and stats lines -----------------------------


_PRIVATE_USE_MIN = 0xE000
_PRIVATE_USE_MAX = 0xF8FF


def _powerline_acceptance_snapshot() -> dict:
    return _snapshot(
        scheduler={"running": True, "pid": 48213},
        ready_count=4,
        lifetime={"done": 128, "failed": 3, "cancelled": 1},
    )


def _contains_private_use_area(text: str) -> bool:
    return any(_PRIVATE_USE_MIN <= ord(ch) <= _PRIVATE_USE_MAX for ch in text)


def test_header_line_ascii_matches_legacy_format_for_acceptance_fixture():
    snap = _powerline_acceptance_snapshot()
    expected = (
        "scheduler running (pid 48213)  |  ready 4 (capped at 1000)  |  "
        "done 128  failed 3  cancelled 1"
    )
    assert header_line(snap, "ascii") == expected


def test_header_line_nerd_uses_powerline_arrow_and_counts_without_pipe_separators():
    line = header_line(_powerline_acceptance_snapshot(), "nerd")
    assert "\ue0b0" in line
    assert "pid 48213" in line
    assert "ready 4" in line
    assert "128" in line
    assert "3" in line
    assert "1" in line
    assert "|" not in line


def test_header_line_unicode_avoids_private_use_area():
    line = header_line(_powerline_acceptance_snapshot(), "unicode")
    assert not _contains_private_use_area(line)


def test_title_line_nerd_includes_alloy_glyph_within_width_budget():
    from rich.cells import cell_len

    from alloy.monitor.render import title_line

    line = title_line(_powerline_acceptance_snapshot(), 100, "nerd")
    assert "\uf0c3" in line
    assert cell_len(line) <= 100


# -- alloy-3g0.5: column alignment and mode-aware tests cell ------------------


def test_right_aligned_columns_constant():
    from alloy.monitor.render import RIGHT_ALIGNED

    assert RIGHT_ALIGNED == frozenset({"iter", "cons", "tests", "elapsed", "tokens"})


def test_column_align_right_for_numeric_columns():
    from alloy.monitor.render import column_align

    for name in ("iter", "cons", "tests", "elapsed", "tokens"):
        assert column_align(name) == "right"


def test_column_align_left_for_text_columns():
    from alloy.monitor.render import column_align

    for name in ("bead", "status", "stage", "now"):
        assert column_align(name) == "left"


def test_run_rows_nerd_formats_tests_summary_with_pass_and_fail_counts():
    ok = icon("test_ok", "nerd")
    fail = icon("test_fail", "nerd")
    snapshot = _snapshot(runs=[_run(tests_summary="3 passed, 1 failed")])

    row = run_rows(snapshot, mode="nerd")[0]

    assert row[6] == f"{ok}3 {fail}1"


def test_run_rows_nerd_formats_tests_summary_with_pass_only():
    ok = icon("test_ok", "nerd")
    snapshot = _snapshot(runs=[_run(tests_summary="3 passed")])

    row = run_rows(snapshot, mode="nerd")[0]

    assert row[6] == f"{ok}3"


def test_run_rows_nerd_leaves_unparseable_tests_summary_verbatim():
    snapshot = _snapshot(runs=[_run(tests_summary="flaky")])

    row = run_rows(snapshot, mode="nerd")[0]

    assert row[6] == "flaky"


def test_run_rows_nerd_checks_with_exit_code_zero_shows_ok_glyph_and_total():
    ok = icon("test_ok", "nerd")
    run = _run(tests_summary=None)
    run["checks"] = {"total": 14, "last": {"exit_code": 0}}

    row = run_rows(_snapshot(runs=[run]), mode="nerd")[0]

    assert row[6] == f"{ok}14"


def test_run_rows_nerd_checks_with_exit_code_one_shows_fail_glyph_and_total():
    fail = icon("test_fail", "nerd")
    run = _run(tests_summary=None)
    run["checks"] = {"total": 14, "last": {"exit_code": 1}}

    row = run_rows(_snapshot(runs=[run]), mode="nerd")[0]

    assert row[6] == f"{fail}14"


def test_run_rows_ascii_checks_still_shows_n_checks():
    run = _run(tests_summary=None)
    run["checks"] = {"total": 14, "last": {"exit_code": 0}}

    row = run_rows(_snapshot(runs=[run]))[0]

    assert row[6] == "14 checks"


def test_run_rows_ascii_mode_keeps_tests_summary_verbatim():
    snapshot = _snapshot(runs=[_run(tests_summary="3 passed, 1 failed")])

    row = run_rows(snapshot, mode="ascii")[0]

    assert row[6] == "3 passed, 1 failed"


# -- alloy-3g0.6: complexity glyph bar in nerd/ascii modes ---------------------


def test_run_rows_nerd_simple_complexity_shows_one_block_glyph():
    row = run_rows(_snapshot(runs=[_run(complexity="simple")]), mode="nerd")[0]

    assert row[11] == "▂"


def test_run_rows_nerd_medium_complexity_shows_two_block_glyph():
    row = run_rows(_snapshot(runs=[_run(complexity="medium")]), mode="nerd")[0]

    assert row[11] == "▂▄"


def test_run_rows_nerd_complex_complexity_shows_three_block_glyph():
    row = run_rows(_snapshot(runs=[_run(complexity="complex")]), mode="nerd")[0]

    assert row[11] == "▂▄▆"


def test_run_rows_nerd_none_complexity_shows_dash():
    row = run_rows(_snapshot(runs=[_run(complexity=None)]), mode="nerd")[0]

    assert row[11] == "-"


def test_run_rows_ascii_mode_keeps_complexity_as_word():
    for level in ("simple", "medium", "complex"):
        row = run_rows(_snapshot(runs=[_run(complexity=level)]), mode="ascii")[0]

        assert row[11] == level


# -- alloy-3g0.7: status pills -------------------------------------------------


_STATUS_WORDS = ("running", "judge", "blocked", "done", "ready")
_ICON_MODES = ("nerd", "unicode", "ascii")


def test_status_badge_running_nerd_plain_matches_acceptance_fixture():
    from alloy.monitor.render import status_badge

    badge = status_badge("running", "nerd")

    assert badge.plain == "\ue0b6\uf04b running\ue0b4"


def test_status_badge_done_unicode_contains_word_without_private_use_area():
    from alloy.monitor.render import status_badge

    badge = status_badge("done", "unicode")

    assert "done" in badge.plain
    assert not _contains_private_use_area(badge.plain)


def test_status_badge_blocked_ascii_plain_is_the_status_word():
    from alloy.monitor.render import status_badge

    badge = status_badge("blocked", "ascii")

    assert badge.plain == "blocked"


def test_status_badge_includes_every_status_word_in_all_modes():
    from alloy.monitor.render import status_badge

    for status in _STATUS_WORDS:
        for mode in _ICON_MODES:
            badge = status_badge(status, mode)
            assert status in badge.plain


# -- alloy-3g0.8: epic/QUEUE tree styling, drop parent column -----------------


def _task_tree_rows(snapshot, expanded=(), width=WIDE_WIDTH, mode=None):
    from alloy.monitor.render import task_tree_rows

    kwargs = {"mode": mode} if mode is not None else {}
    return task_tree_rows(snapshot, set(expanded), width, **kwargs)


def _cell_plain(value) -> str:
    if hasattr(value, "plain"):
        return value.plain
    return str(value)


def _tree_row_by_key(rows, key: str):
    return next(row for row in rows if row.key == key)


def _tree_styling_fixture() -> dict:
    return _snapshot(
        queue={
            "ready": [_ready_bead("alloy-x", title="next up")],
            "ready_total": 7,
            "blocked": [
                _blocked_bead("blocked-1", ["a"]),
                _blocked_bead("blocked-2", ["b"]),
                _blocked_bead("blocked-3", ["c"]),
            ],
        },
        epics=[_epic("E", title="Monitor epic", total=9, done=4, running=2, judge=1)],
        runs=[_run(run_id="run-under-e", bead_id="running-under-e", epic_id="E", status="running")],
    )


def test_parent_column_removed_from_columns():
    assert "parent" not in COLUMNS


def test_visible_columns_at_wide_width_includes_now_without_parent():
    from alloy.monitor.render import visible_columns

    cols = visible_columns(WIDE_WIDTH)

    assert "now" in cols
    assert "parent" not in cols


def test_visible_columns_below_wide_width_hides_now_stage_and_cons():
    from alloy.monitor.render import visible_columns

    cols_99 = visible_columns(WIDE_WIDTH - 1)

    assert "now" not in cols_99
    assert "stage" not in cols_99
    assert "cons" not in cols_99


def test_task_tree_nerd_epic_row_bead_cell_has_folder_icon_and_epic_id():
    rows = _task_tree_rows(_tree_styling_fixture(), expanded=(), mode="nerd")
    epic = _tree_row_by_key(rows, "epic/E")
    bead = _cell_plain(epic.cells["bead"])

    assert "\uf07b" in bead
    assert "E" in bead


def test_task_tree_nerd_epic_row_progress_in_right_aligned_iter_cell():
    from alloy.monitor.render import column_align

    rows = _task_tree_rows(_tree_styling_fixture(), expanded=(), mode="nerd")
    epic = _tree_row_by_key(rows, "epic/E")

    assert column_align("iter") == "right"
    assert epic.cells["iter"] == "4/9"


def test_task_tree_nerd_queue_row_has_ready_queue_icon():
    rows = _task_tree_rows(_tree_styling_fixture(), expanded=(), mode="nerd")
    queue = _tree_row_by_key(rows, "queue")
    bead = _cell_plain(queue.cells["bead"])

    assert "\uf0ae" in bead


def _children_after(rows, parent_key: str) -> list:
    idx = next(i for i, row in enumerate(rows) if row.key == parent_key)
    parent_depth = rows[idx].depth
    children = []
    for row in rows[idx + 1 :]:
        if row.depth <= parent_depth:
            break
        children.append(row)
    return children


def test_task_tree_ascii_mode_matches_byo4_expectations():
    snap = _snapshot(
        queue={
            "ready": [_ready_bead("alloy-x", title="next up", epic_id="E")],
            "ready_total": 7,
            "blocked": [_blocked_bead("blocked-1", ["blocker-a"])],
        },
        epics=[
            _epic(
                "E",
                title="Epic E",
                total=4,
                done=1,
                done_ids=("done-1",),
                running=1,
                judge=0,
            ),
        ],
        runs=[_run(run_id="run-under-e", bead_id="running-under-e", epic_id="E", status="running")],
    )

    rows = _task_tree_rows(snap, expanded={"queue", "epic/E"}, mode="ascii")

    queue = _tree_row_by_key(rows, "queue")
    assert queue.cells["bead"] == "▾ QUEUE"
    assert queue.cells["status"] == "7 ready · 1 blocked · next: alloy-x"

    epic = _tree_row_by_key(rows, "epic/E")
    assert epic.cells["bead"] == "▾ E  Epic E"
    assert epic.cells["status"] == "1 running · 0 judge · 1/4 done"

    queue_children = _children_after(rows, "queue")
    assert [child.kind for child in queue_children] == ["queued", "blocked"]
    assert queue_children[0].cells["bead"] == "alloy-x"
    assert queue_children[0].cells["tests"] == "queued #1"
    assert queue_children[1].cells["bead"] == "⊘ blocked-1"
    assert queue_children[1].cells["status"] == "by blocker-a"

    epic_children = _children_after(rows, "epic/E")
    assert [child.kind for child in epic_children] == ["run", "queued", "done_fold"]
    assert epic_children[0].key == "run/run-under-e"
    assert epic_children[1].key == "epic/E/queued/alloy-x"
    assert epic_children[0].cells["bead"] == "├─ running-under-e"
    assert epic_children[1].cells["bead"] == "├─ alloy-x"
    assert epic_children[2].cells["bead"] == "└─ ✓ 1 done  (done-1)"


def test_task_tree_queued_bead_appears_under_queue_and_epic_when_both_expanded():
    bead_id = "alloy-shared"
    snap = _snapshot(
        epics=[_epic("E", title="Epic E", total=2, done=0)],
        queue={
            "ready": [_ready_bead(bead_id, epic_id="E")],
            "ready_total": 1,
            "blocked": [],
        },
    )

    rows = _task_tree_rows(snap, expanded={"queue", "epic/E"})

    queue_children = _children_after(rows, "queue")
    epic_children = _children_after(rows, "epic/E")

    queue_queued = [row for row in queue_children if row.kind == "queued" and row.key == f"queue/{bead_id}"]
    epic_queued = [
        row
        for row in epic_children
        if row.kind == "queued" and row.key == f"epic/E/queued/{bead_id}"
    ]

    assert len(queue_queued) == 1
    assert len(epic_queued) == 1
    assert queue_queued[0].key != epic_queued[0].key


def test_task_tree_expanded_queue_and_epics_yield_unique_row_keys():
    bead_id = "alloy-q.1"
    snap = _snapshot(
        epics=[_epic("E", title="Epic E"), _epic("F", title="Epic F")],
        queue={
            "ready": [_ready_bead(bead_id, epic_id="E")],
            "ready_total": 1,
            "blocked": [],
        },
    )

    rows = _task_tree_rows(snap, expanded={"queue", "epic/E", "epic/F"})
    keys = [row.key for row in rows]

    assert len(keys) == len(set(keys)), f"duplicate keys: {[k for k in keys if keys.count(k) > 1]}"
