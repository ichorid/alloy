"""Acceptance tests for wait-for-role recovery interrupts (alloy-fon.3).

Behaviour is not implemented yet — these must fail until tests/support.py
exports wait_for_role() and await_role(), recovery/scheduler tests drop the
fixed eight-second asyncio.wait_for interrupt pattern, and the three rewritten
interrupt test finishes in under three seconds; recover tests pass with call
time under six seconds (real bd CLI during recover dominates the floor).
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Wall-time checks spawn a serial subprocess; skip under xdist workers so the
# default parallel suite stays green while alloy_test_cmd runs these with -n 0.
if os.environ.get("PYTEST_XDIST_WORKER"):
    pytestmark = pytest.mark.skip(
        reason="timing acceptance runs only in a serial pytest invocation (-n 0)",
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
PYTHON = sys.executable

FAST_INTERRUPT_TEST = "tests/test_recovery.py::test_an_interrupted_run_resumes_without_repeating_finished_stages"
RECOVER_TESTS = [
    "tests/test_scheduler.py::test_recover_adopts_runs_whose_process_died",
    "tests/test_scheduler.py::test_recover_reconciles_inflight_before_adopting_orphaned_runs",
]

FORBIDDEN_TIMEOUT_LITERAL = "timeout=" + "8"


def test_support_exports_wait_for_role_helpers():
    support = importlib.import_module("support")
    assert callable(getattr(support, "wait_for_role", None))
    assert callable(getattr(support, "await_role", None))


def test_tests_have_no_hardcoded_eight_second_timeout():
    matches = []
    for path in TESTS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN_TIMEOUT_LITERAL in line:
                matches.append(f"{path.relative_to(REPO_ROOT)}:{lineno}:{line.strip()}")
    assert matches == []


def _run_node_and_call_seconds(nodeid: str) -> float:
    result = subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:xdist",
            nodeid,
            "--durations=0",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    match = re.search(
        r"^([\d.]+)s\s+call\s+" + re.escape(nodeid) + r"\s*$",
        combined,
        flags=re.MULTILINE,
    )
    assert match is not None, f"no call duration line for {nodeid} in:\n{combined}"
    return float(match.group(1))


def test_interrupt_test_finishes_under_three_seconds_call_time():
    """The recovery interrupt test must reach implement quickly, not after 8s."""
    assert _run_node_and_call_seconds(FAST_INTERRUPT_TEST) < 3.0


@pytest.mark.parametrize("nodeid", RECOVER_TESTS)
def test_recover_tests_finish_under_six_seconds_call_time(nodeid: str):
    """Recover tests still call real bd; six seconds is the relaxed serial bound."""
    assert _run_node_and_call_seconds(nodeid) < 6.0
