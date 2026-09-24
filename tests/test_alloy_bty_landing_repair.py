"""Acceptance tests for alloy-bty: Fix landing check for alloy-jjg.

Land repair bead: make ``uv run pytest -n 8 -q`` exit 0 on ``alloy/alloy-jjg``
after the trial merge of main is committed. Behaviour is not implemented yet —
these must fail until ``alloy.land_repair`` exposes the alloy-bty parallel
regression gate and acceptance bundle used to close this repair bead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BTY_EPIC_BRANCH = "alloy/alloy-jjg"
REPAIR_BEAD_ID = "alloy-bty"
ACCEPTANCE_PARALLEL_CMD = ("uv", "run", "pytest", "-n", "8", "-q")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_land_repair_bty_repair_bead_id_matches_acceptance():
    from alloy.land_repair import bty_repair_bead_id

    assert bty_repair_bead_id() == REPAIR_BEAD_ID


def test_land_repair_bty_epic_branch_matches_worktree():
    from alloy.land_repair import bty_epic_branch

    assert bty_epic_branch() == BTY_EPIC_BRANCH


def test_land_repair_bty_parallel_regression_command_matches_acceptance():
    from alloy.land_repair import bty_parallel_regression_command

    assert bty_parallel_regression_command() == ACCEPTANCE_PARALLEL_CMD


# The full ``uv run pytest -n 8 -q`` gate is deliberately not run from inside the
# suite: it would collect this very test and recurse (each level spawning 8 more
# workers). The verifier runs that gate as an external check instead.


def test_land_repair_bty_acceptance_bundle_lists_jjg_landing_modules():
    from alloy.land_repair import bty_acceptance_test_paths

    paths = bty_acceptance_test_paths()
    required = {
        "tests/test_alloy_jjg_monitor_render_conflict.py",
        "tests/test_monitor_render_landing_merge.py",
        "tests/test_alloy_bty_landing_repair.py",
    }
    assert required.issubset(set(paths))
    for rel in paths:
        assert (REPO_ROOT / rel).is_file()


def test_land_repair_bty_parallel_gate_succeeded_reports_full_regression():
    """Proxy for the bead acceptance gate without nesting a full parallel pytest run."""
    from alloy.land_repair import bty_parallel_gate_succeeded

    assert bty_parallel_gate_succeeded(repo_root=REPO_ROOT) is True


def test_trial_merge_of_main_recorded_on_bty_epic_branch():
    """Only meaningful on the epic branch; after landing the branch is main."""
    branch = _git("branch", "--show-current")
    assert branch.returncode == 0
    if branch.stdout.strip() != BTY_EPIC_BRANCH:
        pytest.skip("not on the epic branch (already landed)")

    log = _git("log", "--oneline", "-30")
    assert log.returncode == 0
    lowered = log.stdout.lower()
    assert "merge branch 'main'" in lowered or "merge main" in lowered
