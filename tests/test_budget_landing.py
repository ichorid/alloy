"""Budget-stop auto-land: a run that runs out of budget while every check is
green on an unchanged tree, and the acceptance gate only wanted *more*
verification, finishes as done -- marked `budget-landed` for a later audit --
instead of parking as needs-human. Anything less keeps the park."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import acceptance_entry, bd_create, implement_entry, judge_entry, verifier_run_entry, verifier_stop_entry
from support import load_config, make_harness
from workflow_support import FULL_SUITE, script

from alloy import beads as bd
from alloy.config import Limits
from alloy.engine import Engine
from alloy.models import CheckResult, JudgeDecision
from alloy.recipes.shared_verification import VERIFY_MORE_BREACH_PREFIX
from alloy.recipes.workflow_nodes import budget_landing
from alloy.worktree import tree_fingerprint

GREEN_VERIFY = [verifier_run_entry(FULL_SUITE, kind="regression"), verifier_stop_entry("regression suite green")]
WANTS_MORE = [acceptance_entry("verify_more", "would like an extra lint run")]


def _config(**limits):
    base = dict(max_iterations=5, max_consiliums=0, max_agent_calls=100, max_cheap_agent_calls=4)
    base.update(limits)
    return replace(load_config(), limits=Limits(**base))


async def test_green_budget_stop_lands_as_done(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script(verifier=GREEN_VERIFY, acceptance=WANTS_MORE))
    harness = make_harness(project, alloy_home, config=_config())
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" not in final
    assert final["outcome"] == "done"
    assert "max_cheap_agent_calls" in final["limit_hit"]
    landed = final["budget_landed"]
    assert landed["regression"] == FULL_SUITE
    assert FULL_SUITE in landed["green_checks"]
    assert final["outcome_reason"].startswith("budget-landed:")
    assert fake_harnesses.calls_for("judge") == []


async def test_red_regression_does_not_land(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            verifier=GREEN_VERIFY,
            acceptance=WANTS_MORE,
            judge=[judge_entry("retry", "still broken")],
        )
    )
    harness = make_harness(project, alloy_home, config=_config(max_iterations=2, max_cheap_agent_calls=100))
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in final
    assert final.get("budget_landed") is None
    assert final["limit_hit"]


async def test_judge_reason_is_ambiguous_and_does_not_land(project, alloy_home, fake_harnesses):
    """The acceptance gate escalated and the judge asked for a retry: its free
    text may name a defect, so a budget stop there still parks."""
    fake_harnesses.configure(
        script(
            verifier=GREEN_VERIFY,
            acceptance=[acceptance_entry("escalate", "unsure")],
            judge=[judge_entry("retry", "needs more verification of the edge cases")],
        )
    )
    harness = make_harness(project, alloy_home, config=_config(max_cheap_agent_calls=5))
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in final
    assert final.get("budget_landed") is None
    assert "max_cheap_agent_calls" in final["limit_hit"]


# -- the rule itself, condition by condition ---------------------------------


def _check(command: str, *, kind: str = "targeted", exit_code: int = 0, iteration: int = 1, fingerprint: str = ""):
    return CheckResult(
        command=command, kind=kind, exit_code=exit_code, iteration=iteration, fingerprint=fingerprint
    ).model_dump(mode="json")


def _rule_ctx(project, *, has_changes: bool = True):
    return SimpleNamespace(
        worktree=SimpleNamespace(path=project),
        worktrees=SimpleNamespace(has_changes=lambda worktree: has_changes),
    )


def _wants_more(breach: str = "max_agent_calls reached (24/24)") -> JudgeDecision:
    return JudgeDecision(decision="retry", reason=f"{VERIFY_MORE_BREACH_PREFIX}{breach}")


@pytest.fixture
def dirty_project(project):
    (project / "mypkg" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    return project


def _state(project, **overrides):
    fingerprint = tree_fingerprint(project)
    state = {
        "iteration": 1,
        "acceptance": {"decision": "verify_more", "reason": "wants a lint run", "confidence": 0.8},
        "checks": [
            _check("pytest -q tests/test_x.py"),
            _check("pytest -q", kind="regression", fingerprint=fingerprint),
        ],
    }
    state.update(overrides)
    return state


def test_rule_lands_when_every_condition_holds(dirty_project):
    landed = budget_landing(
        _rule_ctx(dirty_project), _state(dirty_project), "max_agent_calls reached (24/24)", _wants_more()
    )
    assert landed == {
        "limit": "max_agent_calls reached (24/24)",
        "regression": "pytest -q",
        "green_checks": ["pytest -q tests/test_x.py", "pytest -q"],
        "acceptance_reason": "wants a lint run",
    }
    for breach in (
        "max_iterations reached (5/5)",
        "max_wall_time reached (93m/90m)",
        "max_cheap_agent_calls reached (80/80)",
    ):
        assert budget_landing(_rule_ctx(dirty_project), _state(dirty_project), breach, _wants_more(breach))


def test_rule_refuses_a_non_budget_stop(dirty_project):
    breach = "max_total_checks reached"
    assert budget_landing(_rule_ctx(dirty_project), _state(dirty_project), breach, _wants_more(breach)) is None


def test_rule_refuses_when_the_tree_changed_after_the_regression(dirty_project):
    state = _state(dirty_project)
    (dirty_project / "mypkg" / "__init__.py").write_text("X = 2\n", encoding="utf-8")
    assert budget_landing(_rule_ctx(dirty_project), state, "max_agent_calls reached (24/24)", _wants_more()) is None


def test_rule_refuses_a_red_or_missing_regression(dirty_project):
    fingerprint = tree_fingerprint(dirty_project)
    red = _state(dirty_project, checks=[_check("pytest -q", kind="regression", exit_code=1, fingerprint=fingerprint)])
    none = _state(dirty_project, checks=[_check("pytest -q tests/test_x.py")])
    for state in (red, none):
        assert budget_landing(_rule_ctx(dirty_project), state, "max_agent_calls reached (24/24)", _wants_more()) is None


def test_rule_refuses_a_failed_check_in_the_final_iteration(dirty_project):
    fingerprint = tree_fingerprint(dirty_project)
    state = _state(
        dirty_project,
        checks=[
            _check("pytest -q", kind="regression", fingerprint=fingerprint),
            _check("dart analyze", kind="lint", exit_code=1),
        ],
    )
    assert budget_landing(_rule_ctx(dirty_project), state, "max_agent_calls reached (24/24)", _wants_more()) is None


def test_rule_refuses_anything_but_a_pure_verify_more(dirty_project):
    breach = "max_agent_calls reached (24/24)"
    judge = JudgeDecision(decision="retry", reason="the edge case in foo() is wrong")
    assert budget_landing(_rule_ctx(dirty_project), _state(dirty_project), breach, judge) is None
    repair = _state(dirty_project, acceptance={"decision": "repair", "reason": "bug"})
    assert budget_landing(_rule_ctx(dirty_project), repair, breach, _wants_more()) is None


def test_rule_refuses_an_empty_diff(project):
    assert (
        budget_landing(
            _rule_ctx(project, has_changes=False), _state(project), "max_agent_calls reached (24/24)", _wants_more()
        )
        is None
    )


# -- the engine: same done path, plus the audit marks ------------------------


async def test_engine_lands_and_marks_the_bead(beads_project, alloy_home, fake_harnesses, monkeypatch):
    engine = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(engine, "load_config", lambda name: _config())
    fake_harnesses.configure(script(verifier=GREEN_VERIFY, acceptance=WANTS_MORE))
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    record = engine.store.get_run(result.run_id)
    assert record["status"] == "done"
    assert record["committed_sha"] not in (None, "none")
    bead = engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_DONE
    assert bd.LABEL_BUDGET_LANDED in bead.labels
    assert bd.metadata_flag(bead.metadata, bd.META_BUDGET_LANDED)
    audit = engine.beads._json(["list", "--all", "--label", bd.LABEL_BUDGET_LANDED, "--limit", "0", "--flat"])
    assert [row["id"] for row in audit] == [bead_id]
    notes = json.dumps(engine.beads._show_rows(bead_id))
    assert "budget-landed" in notes and FULL_SUITE in notes
    events = [json.loads(line) for line in (alloy_home / "events.jsonl").read_text().splitlines()]
    landed = [event for event in events if event["event"] == "budget-landed"]
    assert landed and landed[0]["bead"] == bead_id
    assert landed[0]["regression"] == FULL_SUITE
    assert any(event["event"] == "done" and event["bead"] == bead_id for event in events)
