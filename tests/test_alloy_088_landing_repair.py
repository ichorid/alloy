"""Acceptance tests for alloy-088: Fix landing check for alloy-jjg.

Land repair bead: make ``uv run pytest -n 0 -q tests/test_monitor_render_landing_merge.py``
exit 0 on ``alloy/alloy-jjg`` after the trial merge of main is committed. Behaviour is
not implemented yet — these must fail until ``alloy.land_repair`` exposes the landing-merge
regression gate used to close this repair bead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
JJG_EPIC_BRANCH = "alloy/alloy-jjg"
REPAIR_BEAD_ID = "alloy-088"
LANDING_MERGE_ACCEPTANCE_CMD = (
    "uv",
    "run",
    "pytest",
    "-n",
    "0",
    "-q",
    "tests/test_monitor_render_landing_merge.py",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_land_repair_jjg_repair_bead_id_matches_acceptance():
    from alloy.land_repair import jjg_repair_bead_id

    assert jjg_repair_bead_id() == REPAIR_BEAD_ID


def test_land_repair_jjg_epic_branch_matches_worktree():
    from alloy.land_repair import jjg_epic_branch

    assert jjg_epic_branch() == JJG_EPIC_BRANCH


def test_land_repair_landing_merge_acceptance_command_matches_bead():
    from alloy.land_repair import landing_merge_acceptance_command

    assert landing_merge_acceptance_command() == LANDING_MERGE_ACCEPTANCE_CMD


def test_land_repair_landing_merge_gate_succeeds_on_merged_worktree():
    from alloy.land_repair import landing_merge_gate_succeeded

    assert landing_merge_gate_succeeded(repo_root=REPO_ROOT) is True


def test_trial_merge_of_main_recorded_on_jjg_epic_branch():
    branch = _git("branch", "--show-current")
    assert branch.returncode == 0
    if branch.stdout.strip() != JJG_EPIC_BRANCH:
        pytest.skip("not on the epic branch (already landed)")

    log = _git("log", "--oneline", "-30")
    assert log.returncode == 0
    lowered = log.stdout.lower()
    assert "merge branch 'main'" in lowered or "merge main" in lowered
