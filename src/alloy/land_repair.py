"""Helpers for epic land-repair beads (parallel regression gate and acceptance bundle)."""

from __future__ import annotations

import subprocess
from pathlib import Path

EPIC_BRANCH = "alloy/alloy-8by"

EPIC_ACCEPTANCE_TEST_PATHS: tuple[str, ...] = (
    "tests/test_runtime_per_run_agent_budget.py",
    "tests/test_verifier_multi_check.py",
    "tests/test_remediate_agent_call_headroom.py",
    "tests/test_verifier_diff_hints.py",
    "tests/test_tier_scaled_max_agent_calls.py",
    "tests/test_monitor_render_landing_merge.py",
)


def parallel_regression_command() -> tuple[str, ...]:
    """Command that matches the land-repair bead acceptance criteria."""
    return ("uv", "run", "pytest", "-n", "8", "-q")


def parallel_regression_gate_succeeded(*, repo_root: Path) -> bool:
    """Return True when the parallel regression gate exits 0 at ``repo_root``."""
    result = subprocess.run(
        list(parallel_regression_command()),
        cwd=repo_root,
        check=False,
    )
    return result.returncode == 0


def epic_acceptance_test_paths() -> list[str]:
    """Relative paths to pytest modules that cover alloy-8by child acceptance."""
    return list(EPIC_ACCEPTANCE_TEST_PATHS)
