"""Store queries for monitor limits (alloy-w9d.6): models_used and finished_runs_since.

Direct Store tests on a throwaway sqlite file — no Engine, no bd.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from alloy.models import AgentResult
from alloy.store import RUN_CANCELLED, RUN_DONE, RUN_FAILED, RUN_RUNNING, Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "alloy.db")


def _make_run(store: Store, run_id: str, *, repo: Path | str = "/repo") -> None:
    store.create_run(
        run_id=run_id,
        bead_id=f"bead-{run_id}",
        thread_id=run_id,
        recipe="tdd-loop",
        repo=Path(repo),
        worktree=None,
        branch=None,
        log_dir=None,
    )


def _insert_inflight(
    store: Store,
    call_id: str,
    run_id: str,
    *,
    role: str = "implement",
    runner: str = "codex",
    model: str | None = None,
    started_at: str | None = None,
) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO inflight_calls (call_id, run_id, bead_id, role, runner, model, started_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                call_id,
                run_id,
                f"bead-{run_id}",
                role,
                runner,
                model,
                started_at or _iso(2026, 9, 22, 12, 0, 0),
            ),
        )


def _iso(year: int, month: int, day: int, hour: int, minute: int, second: int) -> str:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).isoformat()


def _agent_result(
    *,
    runner: str,
    model: str | None,
    usage: dict,
    started_at: datetime,
) -> AgentResult:
    return AgentResult(
        runner=runner,
        model=model,
        ok=True,
        exit_code=0,
        text="done",
        structured=None,
        started_at=started_at,
        ended_at=started_at,
        duration_s=1.0,
        usage=usage,
        log_path="/tmp/log",
        prompt_hash="deadbeef",
    )


def _finish_call(
    store: Store,
    run_id: str,
    call_id: str,
    *,
    runner: str,
    model: str | None,
    usage: dict,
    started_at: datetime,
    role: str = "implement",
) -> None:
    _insert_inflight(
        store,
        call_id,
        run_id,
        role=role,
        runner=runner,
        model=model,
        started_at=started_at.isoformat(),
    )
    store.finish_call(
        call_id,
        run_id=run_id,
        bead_id=f"bead-{run_id}",
        role=role,
        iteration=0,
        result=_agent_result(
            runner=runner,
            model=model,
            usage=usage,
            started_at=started_at,
        ),
    )


def _set_terminal_run(
    store: Store,
    run_id: str,
    *,
    status: str,
    ended_at: str | None,
) -> None:
    store.update_run(run_id, status=status, ended_at=ended_at, outcome="test")


# -- models_used ----------------------------------------------------------


def test_models_used_groups_finished_calls_and_inflight_by_runner_model(store: Store):
    """Acceptance: distinct (runner, model) pairs ordered by first use, with harness,
    call counts, and normalized token totals; inflight-only pairs have calls=0."""
    _make_run(store, "run-1")
    t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 22, 8, 1, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 22, 9, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)

    _finish_call(
        store,
        "run-1",
        "call-fable-1",
        runner="claude-write",
        model="fable",
        usage={"input_tokens": 100, "output_tokens": 10},
        started_at=t0,
    )
    _finish_call(
        store,
        "run-1",
        "call-fable-2",
        runner="claude-write",
        model="fable",
        usage={"input_tokens": 200, "output_tokens": 20},
        started_at=t1,
    )
    _finish_call(
        store,
        "run-1",
        "call-luna",
        runner="codex",
        model="gpt-5.6-luna",
        usage={"input_tokens": 50, "output_tokens": 5},
        started_at=t2,
    )
    _finish_call(
        store,
        "run-1",
        "call-fable-3",
        runner="claude-write",
        model="fable",
        usage={"input_tokens": 300, "output_tokens": 30},
        started_at=t3,
    )
    _insert_inflight(
        store,
        "call-cursor",
        "run-1",
        runner="cursor",
        model="composer-2.5",
        started_at=_iso(2026, 9, 22, 11, 0, 0),
    )

    entries = store.models_used("run-1")

    assert len(entries) == 3
    assert [(e["runner"], e["model"]) for e in entries] == [
        ("claude-write", "fable"),
        ("codex", "gpt-5.6-luna"),
        ("cursor", "composer-2.5"),
    ]
    assert [e["harness"] for e in entries] == ["claude", "codex", "cursor"]
    assert [e["calls"] for e in entries] == [3, 1, 0]

    fable, luna, cursor = entries
    assert fable["input_tokens"] == 600
    assert fable["output_tokens"] == 60
    assert fable["total_tokens"] == 660
    assert luna["input_tokens"] == 50
    assert luna["output_tokens"] == 5
    assert luna["total_tokens"] == 55
    assert cursor["input_tokens"] == 0
    assert cursor["output_tokens"] == 0
    assert cursor["total_tokens"] == 0


# -- finished_runs_since --------------------------------------------------


def test_finished_runs_since_returns_terminal_runs_for_repo_since_bound(store: Store, tmp_path: Path):
    """Acceptance: terminal runs with ended_at >= since in the given repo, ascending."""
    repo = tmp_path / "my-repo"
    other_repo = tmp_path / "other-repo"
    since = "2026-09-22T10:00:00"

    _make_run(store, "too-early", repo=repo)
    _set_terminal_run(store, "too-early", status=RUN_DONE, ended_at="2026-09-22T09:00:00")

    _make_run(store, "done-a", repo=repo)
    _set_terminal_run(store, "done-a", status=RUN_DONE, ended_at="2026-09-22T10:30:00")

    _make_run(store, "failed-b", repo=repo)
    _set_terminal_run(store, "failed-b", status=RUN_FAILED, ended_at="2026-09-22T11:00:00")

    _make_run(store, "still-running", repo=repo)
    store.update_run("still-running", status=RUN_RUNNING, ended_at=None)

    _make_run(store, "other-repo-done", repo=other_repo)
    _set_terminal_run(
        store,
        "other-repo-done",
        status=RUN_DONE,
        ended_at="2026-09-22T12:00:00",
    )

    _make_run(store, "cancelled-c", repo=repo)
    _set_terminal_run(
        store,
        "cancelled-c",
        status=RUN_CANCELLED,
        ended_at="2026-09-22T13:00:00",
    )

    rows = store.finished_runs_since(since, repo)

    assert [r["run_id"] for r in rows] == ["done-a", "failed-b", "cancelled-c"]
    assert all(r["status"] in {RUN_DONE, RUN_FAILED, RUN_CANCELLED} for r in rows)
    assert all(r["ended_at"] >= since for r in rows)
    assert all(r["repo"] == str(repo) for r in rows)
