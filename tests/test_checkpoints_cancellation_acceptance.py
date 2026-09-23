"""Acceptance timing for checkpoint cancellation (alloy-i1w).

Serial subprocess runs enforce the cancel+await budget on the harness interrupt
path without relying on the full parallel default suite.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

if os.environ.get("PYTEST_XDIST_WORKER"):
    pytestmark = pytest.mark.skip(
        reason="timing acceptance runs only in a serial pytest invocation (-n 0)",
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

HARNESS_CANCEL_TEST = (
    "tests/test_checkpoints_cancellation.py::"
    "test_harness_start_cancel_finishes_within_checkpointer_budget"
)
CANCEL_AWAIT_BUDGET_S = 6.0


def _run_node_and_call_seconds(nodeid: str) -> float:
    result = subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
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


def test_harness_cancel_test_finishes_under_six_seconds_call_time():
    """Cancel+await on the harness path must stay inside the acceptance budget."""
    assert _run_node_and_call_seconds(HARNESS_CANCEL_TEST) < CANCEL_AWAIT_BUDGET_S
