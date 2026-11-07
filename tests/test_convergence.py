"""Convergence: limit stops say why they happened, and a loop that fixes
nothing for two iterations asks the critics instead of retrying again
(bench calibration 2026-11-07: cheap implementers repeated one failing
approach until a budget ran out; finished work was parked on the check cap)."""

from __future__ import annotations

import subprocess

import pytest
from conftest import IMPLEMENTATION, implement_entry, verifier_run_entry, verifier_stop_entry
from support import make_harness
from workflow_support import script

from alloy.models import CheckResult, JudgeDecision
from alloy.recipes.workflow_nodes import classify_exhaustion, red_signature, stalled


def _check(command: str, exit_code: int, iteration: int, *, log: str = "", tmp=None) -> dict:
    log_path = None
    if log and tmp is not None:
        log_path = tmp / f"{abs(hash(command))}-{iteration}.log"
        log_path.write_text(log, encoding="utf-8")
    return CheckResult(
        command=command, exit_code=exit_code, iteration=iteration, log_path=str(log_path) if log_path else None
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "previous, final, reason, expected",
    [
        ([], [("a", 0), ("b", 0)], "", "nearly-done"),
        ([("a", 1)], [("a", 1)], "", "stalled"),
        ([("a", 1), ("b", 0)], [("a", 1), ("b", 1)], "", "stalled"),
        ([("a", 1), ("b", 1)], [("a", 1), ("b", 0)], "", "progressing"),
        ([], [("a", 1)], "", "progressing"),
        ([], [("a", 0)], "implement's primary and fallback unavailable", "infrastructure"),
    ],
)
def test_limit_stops_are_classified(previous, final, reason, expected):
    checks = [_check(c, e, 1) for c, e in previous] + [_check(c, e, 2) for c, e in final]
    state = {"iteration": 2, "checks": checks}
    assert classify_exhaustion(state, JudgeDecision(decision="retry", reason=reason)) == expected


def test_an_unrunnable_final_check_is_infrastructure():
    broken = CheckResult(command="a", exit_code=127, iteration=1, runnable=False).model_dump(mode="json")
    state = {"iteration": 1, "checks": [broken]}
    assert classify_exhaustion(state, JudgeDecision(decision="retry", reason="x")) == "infrastructure"


def test_progress_is_measured_in_failing_tests_not_commands(tmp_path):
    two = "FAILED tests/test_x.py::test_a - x\nFAILED tests/test_x.py::test_b - y\n"
    one = "FAILED tests/test_x.py::test_b - y\n"
    state = {
        "iteration": 2,
        "checks": [
            _check("pytest tests/test_x.py", 1, 1, log=two, tmp=tmp_path),
            # A narrower command, but test_b is still red: no progress.
            _check("pytest tests/test_x.py::test_b", 1, 2, log=one, tmp=tmp_path),
        ],
    }
    assert red_signature(state, 2) == {"tests/test_x.py::test_b"}
    assert not stalled(state)  # test_a got fixed
    state["checks"][0] = _check("pytest tests/test_x.py", 1, 1, log=one, tmp=tmp_path)
    assert stalled(state)
    assert not stalled(state, 2)  # iteration 0 had nothing red


CHECK = "sh check.sh"


async def test_two_iterations_that_fix_nothing_call_the_critics(project, alloy_home, fake_harnesses):
    (project / "check.sh").write_text(
        'test -f fixed && exit 0\necho "FAILED tests/test_s.py::test_a - boom"\nexit 1\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "check"], cwd=project, check=True)
    fix = {
        "text": "Fixed it.",
        "write": [{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}, {"path": "fixed", "content": "1"}],
    }
    run = verifier_run_entry(CHECK, kind="targeted")
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)] * 3 + [fix],
            verifier=[run, run, run, run, verifier_stop_entry("green"), run, verifier_stop_entry("green")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["consiliums"] == 1
    assert len(fake_harnesses.calls_for("synthesize")) == 1
    assert any("stayed red for 3 iterations" in line for line in final["journal"])
    # The consilium came after the third red iteration, not before.
    roles = [call["role"] for call in fake_harnesses.calls]
    assert roles.index("critic") > [i for i, r in enumerate(roles) if r == "implement"][2]
