"""Detail pane content and responsive layout for `alloy.monitor`.

`detail_lines` content rules come from the "Panels" paragraph (Component 3) and
the raw-vs-effective judge rules (Component 1, item 3) in
docs/plans/execution-monitor.md. Width-aware formatting (`format_detail`) and
Pilot-based layout tests for alloy-o89.5 live here; toggle/selection behaviour
remains in tests/test_monitor_view.py.
"""

from __future__ import annotations

import pytest
from textual.widgets import Static

from alloy.monitor.app import MonitorApp
from alloy.monitor.render import COMFORTABLE_WIDTH, detail_lines

DISABLED_INTERVAL = 1000.0

_METADATA_PREFIXES = ("bead:", "worktree:", "branch:", "logs:")

EMPTY_TOKENS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None}


def _run(
    *,
    current_calls=(),
    tokens_by_role=None,
    models_used=None,
    judge=None,
    worktree="/home/vader/.alloy/worktrees/alloy-a1b2",
    branch="alloy/alloy-a1b2",
) -> dict:
    return {
        "bead_id": "alloy-a1b2",
        "run_id": "run-1",
        "recipe": "tdd-loop-jev",
        "status": "running",
        "stage": "implement",
        "iteration": 2,
        "max_iterations": 5,
        "consiliums": 0,
        "max_consiliums": 1,
        "tests_summary": None,
        "elapsed_minutes": 7,
        "current_calls": list(current_calls),
        "tokens": dict(EMPTY_TOKENS),
        "tokens_by_role": tokens_by_role or {},
        "models_used": models_used or [],
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


# -- current_calls --------------------------------------------------------


def test_two_inflight_calls_with_differing_requested_and_effective_runner():
    calls = [
        _call(role="implement", requested_runner="astra", effective_runner="codex",
              elapsed_seconds=42.1),
        _call(role="critic", requested_runner="claude", effective_runner="cursor",
              elapsed_seconds=3.9),
    ]
    lines = detail_lines(_run(current_calls=calls))

    implement_lines = [line for line in lines if line.startswith("implement:")]
    critic_lines = [line for line in lines if line.startswith("critic:")]
    assert len(implement_lines) == 1
    assert len(critic_lines) == 1

    implement_line = implement_lines[0]
    assert "astra" in implement_line
    assert "codex" in implement_line
    assert "->" in implement_line
    assert "42s" in implement_line

    critic_line = critic_lines[0]
    assert "claude" in critic_line
    assert "cursor" in critic_line
    assert "3s" in critic_line


def test_current_call_includes_requested_and_effective_model_when_present():
    calls = [_call(role="implement", requested_runner="astra", requested_model="o3",
                    effective_runner="codex", effective_model="gpt-5", elapsed_seconds=10.0)]

    lines = detail_lines(_run(current_calls=calls))

    line = next(line for line in lines if line.startswith("implement:"))
    assert "astra:o3" in line
    assert "codex:gpt-5" in line


def test_no_current_calls_yields_no_call_lines():
    lines = detail_lines(_run(current_calls=[]))

    assert not any("->" in line for line in lines)


# -- judge ------------------------------------------------------------------


def test_matching_raw_and_effective_judge_is_a_single_line():
    lines = detail_lines(_run(judge=_judge(
        raw_decision="done", effective_decision="done", matches=True,
    )))

    judge_lines = [line for line in lines if "done" in line]
    assert len(judge_lines) == 1
    assert "judge said" not in judge_lines[0]
    assert "Alloy did" not in judge_lines[0]


def test_differing_raw_and_effective_judge_shows_both_with_confidence_and_reason():
    lines = detail_lines(_run(judge=_judge(
        raw_decision="done", raw_confidence=0.61, effective_decision="retry",
        effective_reason="tests are red", matches=False,
    )))

    judge_line = next(line for line in lines if "judge said" in line)
    assert "done" in judge_line
    assert "0.61" in judge_line
    assert "Alloy did" in judge_line
    assert "retry" in judge_line
    assert "tests are red" in judge_line


def test_raw_null_with_effective_set_yields_no_parseable_verdict():
    judge = {
        "raw": {"decision": None, "confidence": None},
        "effective": {"decision": "retry", "reason": "tests are red"},
        "matches_effective": False,
    }

    lines = detail_lines(_run(judge=judge))

    assert "judge: no parseable verdict" in lines


def test_judge_none_does_not_mention_judge_said_or_no_parseable_verdict():
    lines = detail_lines(_run(judge=None))

    assert not any("judge said" in line for line in lines)
    assert not any("no parseable verdict" in line for line in lines)


# -- tokens_by_role -----------------------------------------------------------


def test_tokens_by_role_has_one_line_per_role_with_total_and_in_out_split():
    tokens_by_role = {
        "implement": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": 0.01},
        "critic": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "cost_usd": 0.002},
    }

    lines = detail_lines(_run(tokens_by_role=tokens_by_role))

    implement_line = next(line for line in lines if line.startswith("implement:"))
    assert "150" in implement_line
    assert "100" in implement_line
    assert "50" in implement_line

    critic_line = next(line for line in lines if line.startswith("critic:"))
    assert "30" in critic_line
    assert "20" in critic_line
    assert "10" in critic_line


def test_empty_tokens_by_role_yields_no_token_lines_and_does_not_raise():
    lines = detail_lines(_run(tokens_by_role={}))

    assert isinstance(lines, list)


# -- worktree / branch --------------------------------------------------------


# -- models_used (alloy-w9d.10) -----------------------------------------------


def test_detail_lines_models_used_includes_runner_model_calls_tokens_and_windows():
    from alloy.limits import window

    models_used = [{
        "runner": "claude-write",
        "model": "fable",
        "calls": 3,
        "total_tokens": 12000,
        "windows": {
            "five_hour": window("five_hour", "5h", 42.0, None),
            "seven_day": window("seven_day", "weekly", 61.0, None),
            "seven_day_fable": window("seven_day_fable", "weekly fable", 12.0, None, model="fable"),
        },
    }]

    lines = detail_lines(_run(models_used=models_used))

    model_line = next(line for line in lines if "claude-write:fable" in line)
    assert "calls 3" in model_line
    assert "12000" in model_line
    assert "5h 42%" in model_line
    assert "weekly fable 12%" in model_line


def test_detail_lines_models_used_with_empty_windows_has_no_percent():
    models_used = [{
        "runner": "codex",
        "model": "gpt-5",
        "calls": 1,
        "total_tokens": 100,
        "windows": {},
    }]

    lines = detail_lines(_run(models_used=models_used))

    model_line = next(line for line in lines if "codex:gpt-5" in line)
    assert "%" not in model_line


# -- worktree / branch --------------------------------------------------------


def test_worktree_and_branch_appear_in_the_detail_lines():
    lines = detail_lines(_run(
        worktree="/home/vader/.alloy/worktrees/alloy-a1b2",
        branch="alloy/alloy-a1b2",
    ))

    text = "\n".join(lines)
    assert "/home/vader/.alloy/worktrees/alloy-a1b2" in text
    assert "alloy/alloy-a1b2" in text


# -- responsive layout (alloy-o89.5) ------------------------------------------


def _layout_run() -> dict:
    return _run(
        current_calls=[
            _call(
                role="implement",
                requested_runner="astra",
                effective_runner="codex",
                elapsed_seconds=42.1,
            ),
        ],
        judge=_judge(raw_decision="retry", effective_decision="retry", matches=True),
        tokens_by_role={
            "implement": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "cost_usd": 0.01,
            },
        },
        worktree="/home/vader/.alloy/worktrees/alloy-detail-layout",
        branch="alloy/alloy-detail-layout",
    )


def _layout_snapshot() -> dict:
    return {
        "root": "/home/vader/.alloy",
        "repo": "/home/vader/MY_SRC/alloy",
        "scheduler": {"running": False, "pid": None},
        "ready_count": 0,
        "ready_capped_at": 1000,
        "lifetime": {"done": 0, "failed": 0, "cancelled": 0},
        "runs": [_layout_run()],
    }


def _activity_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.startswith(_METADATA_PREFIXES)]


def _metadata_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith(_METADATA_PREFIXES)]


def _assert_stacked_detail(text: str, lines: list[str]) -> None:
    assert text == "\n".join(lines)
    bead_row = next(row for row in text.split("\n") if "bead:" in row)
    assert bead_row.strip().startswith("bead:")


def _assert_side_by_side_detail(text: str, lines: list[str]) -> None:
    activity = _activity_lines(lines)
    metadata = _metadata_lines(lines)
    rows = text.split("\n")
    assert len(rows) == max(len(activity), len(metadata))
    bead_row = next(row for row in rows if "bead:" in row)
    if activity:
        assert bead_row.index("bead:") > 0
    for line in lines:
        assert line in text


def _detail_text(app: MonitorApp) -> str:
    detail = app.query_one("#detail", Static)
    return str(getattr(detail, "_Static__content", ""))


def test_format_detail_below_comfortable_width_stacks_single_column():
    from alloy.monitor.render import format_detail

    run = _layout_run()
    log_dir = "/home/vader/.alloy/logs/run-1"
    lines = detail_lines(run, log_dir)

    text = format_detail(run, COMFORTABLE_WIDTH - 1, log_dir)

    _assert_stacked_detail(text, lines)


def test_format_detail_at_comfortable_lower_bound_uses_two_columns():
    from alloy.monitor.render import format_detail

    run = _layout_run()
    log_dir = "/home/vader/.alloy/logs/run-1"
    lines = detail_lines(run, log_dir)

    text = format_detail(run, COMFORTABLE_WIDTH, log_dir)

    _assert_side_by_side_detail(text, lines)


async def test_detail_pane_at_comfortable_width_uses_two_column_layout():
    app = MonitorApp(snapshot_source=lambda: _layout_snapshot(), interval=DISABLED_INTERVAL)
    async with app.run_test(size=(COMFORTABLE_WIDTH, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        run = _layout_run()
        log_dir = "/home/vader/.alloy/logs/run-1"
        lines = detail_lines(run, log_dir)
        _assert_side_by_side_detail(_detail_text(app), lines)


async def test_detail_pane_below_comfortable_width_stacks_single_column():
    app = MonitorApp(snapshot_source=lambda: _layout_snapshot(), interval=DISABLED_INTERVAL)
    async with app.run_test(size=(COMFORTABLE_WIDTH - 1, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        run = _layout_run()
        log_dir = "/home/vader/.alloy/logs/run-1"
        lines = detail_lines(run, log_dir)
        _assert_stacked_detail(_detail_text(app), lines)


async def test_detail_pane_layout_updates_when_terminal_is_resized():
    app = MonitorApp(snapshot_source=lambda: _layout_snapshot(), interval=DISABLED_INTERVAL)
    async with app.run_test(size=(COMFORTABLE_WIDTH, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        run = _layout_run()
        log_dir = "/home/vader/.alloy/logs/run-1"
        lines = detail_lines(run, log_dir)
        wide_text = _detail_text(app)
        _assert_side_by_side_detail(wide_text, lines)

        await pilot.resize_terminal(COMFORTABLE_WIDTH - 1, 24)
        await pilot.pause()

        narrow_text = _detail_text(app)
        _assert_stacked_detail(narrow_text, lines)
        assert narrow_text != wide_text


# -- alloy-3g0.9: nerd detail pane labelled two-column layout -----------------


_TOKEN_BAR_CHARS = "█▉▊▋▌▍▎▏"


def _token_bar_length(line: str) -> int:
    return sum(1 for char in line if char in _TOKEN_BAR_CHARS)


def _right_column_segments(text: str) -> list[str]:
    segments: list[str] = []
    for row in text.split("\n"):
        if "  " not in row:
            continue
        right = row.rsplit("  ", 1)[-1].strip()
        if right:
            segments.append(right)
    return segments


def _nerd_detail_run() -> dict:
    return _run(
        current_calls=[
            _call(
                role="implement",
                requested_runner="astra",
                requested_model="o3",
                effective_runner="codex",
                effective_model="gpt-5",
                elapsed_seconds=42.0,
            ),
        ],
        judge=_judge(raw_decision="retry", effective_decision="retry", matches=True),
        tokens_by_role={
            "implement": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "cost_usd": 0.01,
            },
            "critic": {
                "input_tokens": 200,
                "output_tokens": 100,
                "total_tokens": 300,
                "cost_usd": 0.002,
            },
            "context": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": None,
            },
        },
        worktree="/home/vader/.alloy/worktrees/alloy-3g0.9",
        branch="alloy/alloy-3g0.9",
    )


_ASCII_DETAIL_FIXTURES: dict[str, tuple[dict, list[str]]] = {
    "empty": (
        _run(),
        [
            "bead: alloy-a1b2",
            "worktree: /home/vader/.alloy/worktrees/alloy-a1b2",
            "branch: alloy/alloy-a1b2",
        ],
    ),
    "calls": (
        _run(
            current_calls=[
                _call(),
                _call(
                    role="critic",
                    requested_runner="claude",
                    effective_runner="cursor",
                    elapsed_seconds=3.9,
                ),
            ],
        ),
        [
            "implement: astra -> codex (42s)",
            "critic: claude -> cursor (3s)",
            "bead: alloy-a1b2",
            "worktree: /home/vader/.alloy/worktrees/alloy-a1b2",
            "branch: alloy/alloy-a1b2",
        ],
    ),
    "tokens": (
        _run(
            tokens_by_role={
                "implement": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "cost_usd": 0.01,
                },
                "critic": {
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "total_tokens": 30,
                    "cost_usd": 0.002,
                },
            },
        ),
        [
            "implement: 150 (100/50)",
            "critic: 30 (20/10)",
            "bead: alloy-a1b2",
            "worktree: /home/vader/.alloy/worktrees/alloy-a1b2",
            "branch: alloy/alloy-a1b2",
        ],
    ),
    "layout": (
        _layout_run(),
        [
            "implement: astra -> codex (42s)",
            "judge: retry (confidence 0.61)",
            "implement: 150 (100/50)",
            "bead: alloy-a1b2",
            "worktree: /home/vader/.alloy/worktrees/alloy-detail-layout",
            "branch: alloy/alloy-detail-layout",
        ],
    ),
}


def test_detail_lines_ascii_matches_existing_fixture_output():
    log_dir = "/home/vader/.alloy/logs/run-1"
    for name, (run, expected_prefix) in _ASCII_DETAIL_FIXTURES.items():
        lines = detail_lines(run, log_dir, mode="ascii")
        assert lines[: len(expected_prefix)] == expected_prefix, name
        assert lines[-1] == f"logs: {log_dir}", name


def test_format_detail_nerd_per_role_token_bars_scale_to_largest_role():
    from alloy.monitor.render import format_detail

    run = _nerd_detail_run()
    log_dir = "/home/vader/.alloy/logs/run-1"
    text = format_detail(run, 100, log_dir, mode="nerd")

    role_lines = {
        role: next(line for line in text.split("\n") if role in line)
        for role in ("implement", "critic", "context")
    }
    implement_bar = _token_bar_length(role_lines["implement"])
    critic_bar = _token_bar_length(role_lines["critic"])
    context_bar = _token_bar_length(role_lines["context"])

    assert "█" in role_lines["implement"]
    assert implement_bar > critic_bar > 0
    assert context_bar == 0


def test_format_detail_nerd_right_column_labels_worktree_branch_logs():
    from alloy.monitor.render import format_detail

    run = _nerd_detail_run()
    log_dir = "/home/vader/.alloy/logs/run-1"
    text = format_detail(run, 100, log_dir, mode="nerd")

    rights = _right_column_segments(text)
    assert any(segment.startswith("worktree") for segment in rights)
    assert any(segment.startswith("branch") for segment in rights)
    assert any(segment.startswith("logs") for segment in rights)


def test_format_detail_nerd_at_width_80_uses_two_columns():
    from alloy.monitor.render import format_detail

    run = _nerd_detail_run()
    log_dir = "/home/vader/.alloy/logs/run-1"
    text = format_detail(run, COMFORTABLE_WIDTH, log_dir, mode="nerd")

    assert any("  " in row and row.rsplit("  ", 1)[-1].strip() for row in text.split("\n"))


async def test_detail_panel_border_subtitle_contains_running_for_running_run(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ALLOY_MONITOR_ICONS", "nerd")
    app = MonitorApp(snapshot_source=lambda: _layout_snapshot(), interval=DISABLED_INTERVAL)
    async with app.run_test(size=(COMFORTABLE_WIDTH, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        detail = app.query_one("#detail", Static)
        subtitle = detail.border_subtitle or ""
        assert "running" in subtitle


# -- alloy-byo.6: queue and epic detail pane ----------------------------------


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


def _tree_run(run_id: str, bead_id: str | None = None, epic_id: str | None = None) -> dict:
    return {
        "bead_id": bead_id or f"alloy-{run_id}",
        "run_id": run_id,
        "recipe": "tdd-loop",
        "status": "running",
        "stage": "implement",
        "iteration": 1,
        "max_iterations": 5,
        "consiliums": 0,
        "max_consiliums": 1,
        "tests_summary": None,
        "elapsed_minutes": 1,
        "current_calls": [],
        "tokens": dict(EMPTY_TOKENS),
        "tokens_by_role": {},
        "judge": None,
        "worktree": f"/home/vader/.alloy/worktrees/{run_id}",
        "branch": f"alloy/{run_id}",
        "epic_id": epic_id,
    }


def _tree_snapshot(
    *,
    runs=(),
    queue: dict | None = None,
    epics=(),
) -> dict:
    return {
        "root": "/home/vader/.alloy",
        "repo": "/home/vader/MY_SRC/alloy",
        "scheduler": {"running": False, "pid": None},
        "ready_count": 0,
        "ready_capped_at": 1000,
        "lifetime": {"done": 0, "failed": 0, "cancelled": 0},
        "runs": list(runs),
        "queue": queue if queue is not None else _empty_queue(),
        "epics": list(epics),
    }


def _queue_detail_snapshot() -> dict:
    return _tree_snapshot(
        queue={
            "ready": [_ready_bead("alloy-x", title="next up")],
            "ready_total": 7,
            "blocked": [
                _blocked_bead("blocked-1", ["a"]),
                _blocked_bead("blocked-2", ["b"]),
                _blocked_bead("blocked-3", ["c"]),
            ],
        },
        runs=[_tree_run("run-1", bead_id="bead-one")],
    )


def _rendered_detail(lines: list[str] | str) -> str:
    return "\n".join(lines) if isinstance(lines, list) else lines


def test_queue_detail_contains_ready_and_blocked_counts():
    from alloy.monitor.render import queue_detail

    text = _rendered_detail(queue_detail(_queue_detail_snapshot()))

    assert "7 ready" in text
    assert "3 blocked" in text


def test_epic_detail_contains_title_progress_and_done_ids():
    from alloy.monitor.render import epic_detail

    epic = _epic(
        "alloy-o89",
        title="Monitor TUI redesign",
        total=9,
        done=4,
        done_ids=("alloy-o89.1", "alloy-o89.2", "alloy-o89.3", "alloy-o89.4"),
        running=2,
        judge=1,
    )

    text = _rendered_detail(epic_detail(epic))

    assert "Monitor TUI redesign" in text
    assert "4/9 done" in text
    for done_id in epic["done_ids"]:
        assert done_id in text


async def test_detail_pane_on_queue_row_shows_ready_counts_not_run_fields():
    snapshot = _queue_detail_snapshot()
    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        detail = app.query_one("#detail", Static)

        await pilot.press("j", "enter")
        await pilot.pause()
        assert detail.display is True
        assert "bead:" in _detail_text(app)

        await pilot.press("k")
        await pilot.pause()
        assert detail.display is True

        queue_detail_text = _detail_text(app)
        assert "7 ready" in queue_detail_text
        assert "bead:" not in queue_detail_text
