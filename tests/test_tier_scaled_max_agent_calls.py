"""Per-tier max_agent_calls from calibration and complexity (alloy-8by.5)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from alloy.models import AgentResult, utcnow
from alloy.runtime import RunContext
from alloy.runners import RunnerRegistry
from alloy.store import Store
from support import load_config, make_bead

# Controlled calibration: complex mean well above simple; with a 1.5x multiplier
# and floor at flat max_agent_calls=20, complex should resolve above simple.
_TIER_CALIBRATION = json.dumps(
    {
        "simple": {
            "runs": 10,
            "mean_iterations": 1.0,
            "mean_agent_calls": 8.0,
            "overruns": 0,
        },
        "complex": {
            "runs": 10,
            "mean_iterations": 2.0,
            "mean_agent_calls": 22.0,
            "overruns": 0,
        },
    }
)

_FLAT_MAX_AGENT_CALLS = 20
# One call above the flat floor; only a higher complex-tier ceiling should stay green.
_CALLS_ABOVE_FLAT_FLOOR = _FLAT_MAX_AGENT_CALLS + 1


def _agent_result() -> AgentResult:
    return AgentResult(
        runner="codex",
        model=None,
        ok=True,
        exit_code=0,
        text="ok",
        started_at=utcnow(),
        ended_at=utcnow(),
        duration_s=0.0,
    )


def _seed_calls(store: Store, *, run_id: str, bead_id: str, count: int) -> None:
    for index in range(count):
        call_id = f"{run_id}-{index}"
        store.start_call(
            call_id,
            run_id=run_id,
            bead_id=bead_id,
            role="context",
            runner="codex",
            model=None,
        )
        store.finish_call(
            call_id,
            run_id=run_id,
            bead_id=bead_id,
            role="context",
            iteration=0,
            result=_agent_result(),
        )


def _run_context(
    store: Store,
    tmp_path: Path,
    *,
    run_id: str,
    bead_id: str,
) -> RunContext:
    recipe = replace(
        load_config(),
        limits=replace(
            load_config().limits,
            max_agent_calls=_FLAT_MAX_AGENT_CALLS,
        ),
    )
    return RunContext(
        bead=make_bead(bead_id),
        recipe=recipe,
        run_id=run_id,
        worktree=SimpleNamespace(path=tmp_path),
        worktrees=None,
        registry=RunnerRegistry(recipe.runners, log_dir=tmp_path),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=None,
    )


def _limit_state(complexity: str) -> dict:
    return {
        "iteration": 0,
        "consiliums": 0,
        "complexity": complexity,
        "memory_calibration": _TIER_CALIBRATION,
    }


def test_resolve_max_agent_calls_complex_tier_exceeds_simple():
    """Resolver derives a higher ceiling for complex than simple from calibration."""
    from alloy.config import resolve_max_agent_calls

    config = load_config()
    simple_limit = resolve_max_agent_calls(config, "simple", _TIER_CALIBRATION)
    complex_limit = resolve_max_agent_calls(config, "complex", _TIER_CALIBRATION)
    assert complex_limit > simple_limit
    assert simple_limit >= _FLAT_MAX_AGENT_CALLS


def test_check_limits_uses_higher_ceiling_for_complex_than_simple(tmp_path):
    """At the same call count, simple breaches while complex stays under the tier cap."""
    calibration_state = _limit_state("simple")
    for complexity, expect_breach in (("simple", True), ("complex", False)):
        store = Store(tmp_path / f"{complexity}-budget.db")
        run_id = f"run-{complexity}"
        bead_id = f"bead-{complexity}"
        store.create_run(
            run_id=run_id,
            bead_id=bead_id,
            thread_id=run_id,
            recipe="tdd-loop",
            repo=tmp_path,
            worktree=None,
            branch=None,
            log_dir=None,
        )
        _seed_calls(store, run_id=run_id, bead_id=bead_id, count=_CALLS_ABOVE_FLAT_FLOOR)
        ctx = _run_context(store, tmp_path, run_id=run_id, bead_id=bead_id)
        state = {**calibration_state, "complexity": complexity}
        breach = ctx.check_limits(state)
        if expect_breach:
            assert breach is not None
            assert "max_agent_calls" in breach
        else:
            assert breach is None
