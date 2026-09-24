"""A full-suite pytest launched from inside the suite must be refused, not recurse."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_nested_full_suite_run_is_refused():
    env = {**os.environ, "ALLOY_FULL_SUITE_ACTIVE": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "0", "-q"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "recursion guard" in result.stdout + result.stderr
