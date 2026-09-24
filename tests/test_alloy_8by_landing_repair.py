"""Acceptance tests for alloy-0zj: Fix landing check for alloy-8by.

Land repair bead: make ``uv run pytest -n 8 -q`` exit 0 on ``alloy/alloy-8by``
after the trial merge of main is committed. Behaviour is not implemented yet —
these must fail until ``alloy.land_repair`` exposes the parallel regression
gate used to close this repair bead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
EPIC_BRANCH = "alloy/alloy-8by"
ACCEPTANCE_PARALLEL_CMD = ("uv", "run", "pytest", "-n", "8", "-q")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_land_repair_parallel_regression_command_matches_acceptance():
    from alloy.land_repair import parallel_regression_command

    assert parallel_regression_command() == ACCEPTANCE_PARALLEL_CMD


# The full ``uv run pytest -n 8 -q`` gate is deliberately not run from inside the
# suite: it would collect this very test and recurse (each level spawning 8 more
# workers). The verifier runs that gate as an external check instead.


def test_land_repair_epic_acceptance_bundle_lists_child_modules():
    from alloy.land_repair import epic_acceptance_test_paths

    paths = epic_acceptance_test_paths()
    required = {
        "tests/test_runtime_per_run_agent_budget.py",
        "tests/test_verifier_multi_check.py",
        "tests/test_remediate_agent_call_headroom.py",
        "tests/test_verifier_diff_hints.py",
        "tests/test_tier_scaled_max_agent_calls.py",
        "tests/test_monitor_render_landing_merge.py",
    }
    assert required.issubset(set(paths))
    for rel in paths:
        assert (REPO_ROOT / rel).is_file()


def test_trial_merge_of_main_recorded_on_epic_branch():
    """Only meaningful on the epic branch; after landing the branch is main."""
    branch = _git("branch", "--show-current")
    assert branch.returncode == 0
    if branch.stdout.strip() != EPIC_BRANCH:
        pytest.skip("not on the epic branch (already landed)")

    log = _git("log", "--oneline", "-20")
    assert log.returncode == 0
    assert "Merge branch 'main' into alloy/alloy-8by" in log.stdout


def test_monitor_render_py_has_no_merge_conflict_markers():
    """Post-trial-merge tree must not leave conflict markers in the merged file."""
    path = TESTS_DIR / "test_monitor_render.py"
    text = path.read_text(encoding="utf-8")
    open_marker = "<" * 7
    close_marker = ">" * 7
    assert open_marker not in text
    assert close_marker not in text
    assert "\n=======\n" not in text
