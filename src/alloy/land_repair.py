"""Helpers for epic land-repair beads (parallel regression gate and acceptance bundle)."""

from __future__ import annotations

import subprocess
from pathlib import Path

EPIC_BRANCH = "alloy/alloy-8by"
EIGHT_BY_REPAIR_BEAD_ID = "alloy-0zj"
JJG_EPIC_BRANCH = "alloy/alloy-jjg"
JJG_REPAIR_BEAD_ID = "alloy-088"
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


def eight_by_repair_bead_id() -> str:
    """Bead id for the alloy-8by landing repair."""
    return EIGHT_BY_REPAIR_BEAD_ID


def eight_by_epic_branch() -> str:
    """Epic branch name for the alloy-8by trial merge."""
    return EPIC_BRANCH


def epic_acceptance_bundle_command() -> tuple[str, ...]:
    """Serial pytest command covering the alloy-8by child acceptance modules."""
    return ("uv", "run", "pytest", "-n", "0", "-q", *EPIC_ACCEPTANCE_TEST_PATHS)


def epic_acceptance_bundle_gate_succeeded(*, repo_root: Path) -> bool:
    """Return True when the epic acceptance bundle exits 0 at ``repo_root``."""
    result = subprocess.run(
        list(epic_acceptance_bundle_command()),
        cwd=repo_root,
        check=False,
    )
    return result.returncode == 0


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
