"""`alloy metrics`: the pure report, the cause reading, and the ledger query."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alloy.metrics_command import compute_metrics, needs_human_cause
from alloy.models import AgentResult, utcnow
from alloy.store import Store


@pytest.mark.parametrize(
    "reason, cause",
    [
        ("Alloy stopped the loop: max_iterations reached (5/5). Last judge reason: x", "max_iterations"),
        ("Alloy stopped the loop: max_wall_time reached (91m/90m). Last judge reason: y", "max_wall_time"),
        ("Alloy stopped the loop: max_total_checks reached. Last judge reason: the run's", "max_total_checks"),
        ("baseline not runnable: no baseline command given", "baseline-not-runnable"),
        ("baseline unexpectedly green: `echo x`", "baseline-green"),
        ("the tests role failed: timed out after 1200s", "tests-role-failed"),
        ("bug 'Foo breaks' (t-1) needs a human: jev p: 0.4", "bug-needs-human"),
        ("agent call headroom too low to start remediation of blocking bug 'x'", "blocking-bug"),
        ("landing gate check `pytest` failed on the final tree (exit 1)", "landing-gate"),
        ("the judge answered with placeholder text twice ('test'); no verdict was reached", "judge-glitch"),
        ("The required lander check rewrites the source placeholder.", "judge-human"),
        ("", "unknown"),
    ],
)
def test_needs_human_cause_reads_alloys_own_reason_templates(reason: str, cause: str) -> None:
    assert needs_human_cause({"event": "needs-human", "reason": reason}) == cause


def test_an_explicit_cause_field_wins_over_the_reason_text() -> None:
    record = {"event": "needs-human", "reason": "Alloy stopped the loop: max_iterations reached", "cause": "stalled"}
    assert needs_human_cause(record) == "stalled"


def _run(run_id: str, bead: str, status: str, *, recipe: str = "r", parent: str | None = None, **extra) -> dict:
    return {
        "run_id": run_id,
        "bead_id": bead,
        "status": status,
        "outcome": status,
        "recipe": recipe,
        "parent_run_id": parent,
        "iteration": extra.get("iteration", 1),
        "agent_calls": extra.get("agent_calls", 4),
        "started_at": "2026-11-01T10:00:00+00:00",
        "ended_at": extra.get("ended_at", "2026-11-01T10:30:00+00:00"),
        "paused_s": extra.get("paused_s", 0),
    }


def _call(run_id: str, role: str, *, runner: str = "claude", model: str = "haiku", ok: int = 1, usage=None) -> dict:
    return {
        "run_id": run_id,
        "role": role,
        "runner": runner,
        "model": model,
        "ok": ok,
        "duration_s": 360.0,
        "usage_json": json.dumps(usage or {}),
    }


def test_compute_metrics_aggregates_runs_calls_events_and_bugs() -> None:
    runs = [
        _run("a", "t-1", "done", iteration=1, paused_s=600),  # 30 min wall, 10 paused -> 20
        _run("b", "t-2", "cancelled"),
        _run("c", "t-2", "done", iteration=3, agent_calls=10),
        _run("d", "t-3", "done", parent="c"),  # a remediation child is not a bead outcome
    ]
    calls = [
        _call("a", "implement", runner="cursor", model="composer-2.5", usage={"input_tokens": 10, "output_tokens": 5}),
        _call("a", "judge", usage={"input_tokens": 1, "output_tokens": 1, "total_cost_usd": 0.25}),
        _call("c", "implement", runner="cursor", model="composer-2.5", ok=0),
        _call("zzz", "implement"),  # a run outside the window is ignored
    ]
    events = [
        {"event": "needs-human", "run": "b", "reason": "Alloy stopped the loop: max_iterations reached (5/5)."},
        {"event": "needs-human", "run": "c", "reason": "baseline not runnable: x"},
        {"event": "stalled", "run": "b", "stage": "parked", "reason": "parked after 5 min"},
        {"event": "stalled", "run": "c", "stage": "dispatch-hold", "reason": "held"},
        {"event": "flaky-check", "run": "c", "reason": "pytest"},
    ]
    bugs = [
        {"title": "Shared dev DB is stale (schema_version 0217)", "status": "closed"},
        {"title": "Shared dev DB is stale (schema_version 0203)", "status": "open"},
        {"title": "Something else", "status": "closed"},
    ]

    report = compute_metrics(runs, calls, events, bugs)

    assert report["runs"]["top_level"] == 3
    assert report["runs"]["remediation"] == 1
    assert report["runs"]["by_status"] == {"done": 2, "cancelled": 1}
    assert report["beads"] == {"total": 2, "done": 2, "with_multiple_runs": 1}
    assert report["done_runs"]["iterations_mean"] == 2.0
    assert report["done_runs"]["wall_minutes_p50"] == 30.0
    assert report["agents"]["calls"] == 3
    assert report["agents"]["failed"] == 1
    assert report["agents"]["tokens"] == 17
    assert report["agents"]["cost_usd"] == 0.25
    assert report["agents"]["by_role"]["implement"]["calls"] == 2
    assert report["agents"]["by_model"]["cursor/composer-2.5"]["cost_usd"] is None
    assert report["needs_human"] == {
        "total": 2,
        "by_cause": {"max_iterations": 1, "baseline-not-runnable": 1},
    }
    assert report["parks"] == 1
    assert report["flaky_checks"] == 1
    assert report["bugs"]["filed"] == 3
    assert report["bugs"]["open"] == 1
    assert report["bugs"]["duplicates"] == [{"title": "shared dev db is stale (schema_version #)", "count": 2}]


def test_compute_metrics_recipe_filter_scopes_runs_calls_and_events() -> None:
    runs = [_run("a", "t-1", "done", recipe="cheap"), _run("b", "t-2", "done", recipe="other")]
    calls = [_call("a", "implement"), _call("b", "implement")]
    events = [{"event": "needs-human", "run": "b", "reason": "x"}]

    report = compute_metrics(runs, calls, events, [], recipe="cheap")

    assert report["runs"]["top_level"] == 1
    assert report["agents"]["calls"] == 1
    assert report["needs_human"]["total"] == 0


def test_metrics_rows_scopes_by_repo_and_start_time(tmp_path: Path) -> None:
    store = Store(tmp_path / "alloy.db")
    for run_id, repo in (("r1", "/repo"), ("r2", "/other")):
        store.create_run(
            run_id=run_id,
            bead_id=f"b-{run_id}",
            thread_id=run_id,
            recipe="tdd-loop",
            repo=Path(repo),
            worktree=None,
            branch=None,
            log_dir=None,
        )
        now = utcnow()
        store.finish_call(
            f"call-{run_id}",
            run_id=run_id,
            bead_id=f"b-{run_id}",
            role="implement",
            iteration=1,
            result=AgentResult(
                runner="cursor",
                model="composer-2.5",
                ok=True,
                exit_code=0,
                started_at=now,
                ended_at=now,
                duration_s=1.0,
                prompt_hash="h",
            ),
        )

    runs, calls = store.metrics_rows(Path("/repo"))
    assert [run["run_id"] for run in runs] == ["r1"]
    assert [call["run_id"] for call in calls] == ["r1"]

    runs, calls = store.metrics_rows(Path("/repo"), since="2999-01-01T00:00:00+00:00")
    assert runs == [] and calls == []
