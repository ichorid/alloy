"""Helpers for epic land-repair beads (parallel regression gate and acceptance bundle)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Self-referential bty acceptance test shells out to the parallel gate; exclude it
# from in-suite gate probes so nested full-suite runs do not recurse (conftest).
BTY_PARALLEL_GATE_IGNORE = "tests/test_alloy_bty_landing_repair.py"
_SUITE_ACTIVE_ENV = "ALLOY_FULL_SUITE_ACTIVE"

EPIC_BRANCH = "alloy/alloy-8by"
JJG_EPIC_BRANCH = "alloy/alloy-jjg"
JJG_REPAIR_BEAD_ID = "alloy-088"
BTY_REPAIR_BEAD_ID = "alloy-bty"
BTY_ACCEPTANCE_TEST_PATHS: tuple[str, ...] = (
    "tests/test_alloy_jjg_monitor_render_conflict.py",
    "tests/test_monitor_render_landing_merge.py",
    "tests/test_alloy_bty_landing_repair.py",
)
LANDING_MERGE_ACCEPTANCE_CMD: tuple[str, ...] = (
    "uv",
    "run",
    "pytest",
    "-n",
    "0",
    "-q",
    "tests/test_monitor_render_landing_merge.py",
)

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


def jjg_repair_bead_id() -> str:
    """Bead id for the alloy-jjg landing-merge repair."""
    return JJG_REPAIR_BEAD_ID


def jjg_epic_branch() -> str:
    """Epic branch name for the alloy-jjg trial merge."""
    return JJG_EPIC_BRANCH


def landing_merge_acceptance_command() -> tuple[str, ...]:
    """Command that matches alloy-088 acceptance (serial landing_merge gate)."""
    return LANDING_MERGE_ACCEPTANCE_CMD


def landing_merge_gate_succeeded(*, repo_root: Path) -> bool:
    """Return True when the landing_merge acceptance gate exits 0 at ``repo_root``."""
    result = subprocess.run(
        list(landing_merge_acceptance_command()),
        cwd=repo_root,
        check=False,
    )
    return result.returncode == 0


def bty_repair_bead_id() -> str:
    """Bead id for the alloy-bty full parallel regression land-repair."""
    return BTY_REPAIR_BEAD_ID


def bty_epic_branch() -> str:
    """Epic branch name for the alloy-jjg trial merge (alloy-bty gate)."""
    return JJG_EPIC_BRANCH


def bty_parallel_regression_command() -> tuple[str, ...]:
    """Command that matches alloy-bty acceptance (full parallel regression)."""
    return parallel_regression_command()


def bty_acceptance_test_paths() -> list[str]:
    """Relative paths to pytest modules that cover alloy-bty / alloy-jjg landing."""
    return list(BTY_ACCEPTANCE_TEST_PATHS)


def bty_parallel_gate_succeeded(*, repo_root: Path) -> bool:
    """Return True when the alloy-bty parallel regression gate exits 0 at ``repo_root``."""
    cmd = [
        *bty_parallel_regression_command(),
        f"--ignore={BTY_PARALLEL_GATE_IGNORE}",
    ]
    env = {k: v for k, v in os.environ.items() if k != _SUITE_ACTIVE_ENV}
    result = subprocess.run(cmd, cwd=repo_root, check=False, env=env)
    return result.returncode == 0
