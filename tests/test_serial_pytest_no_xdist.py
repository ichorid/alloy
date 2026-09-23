"""Regression tests for README serial pytest override (alloy-jht).

Behaviour is not implemented yet — these must fail until `pytest -p no:xdist`
works with the default `addopts = ["-n", "auto"]` from pyproject.toml without
also passing `-n 0` or `-o addopts=`, and serial timing helpers drop the
`-o addopts=` workaround.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
PYPROJECT = REPO_ROOT / "pyproject.toml"

BEADS_ACCEPTANCE_TARGET = "tests/test_beads.py"
BEADS_SMOKE_NODEID = "tests/test_beads.py::test_alloy_statuses_are_registered"

SERIAL_TIMING_ACCEPTANCE_FILES = (
    REPO_ROOT / "tests/test_wait_for_role_acceptance.py",
    REPO_ROOT / "tests/test_checkpoints_cancellation_acceptance.py",
)


def _run_documented_no_xdist(target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "pytest", "-q", target, "-p", "no:xdist"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_no_xdist_serial_override_on_beads_acceptance():
    """Exact acceptance command must not error on `-n` from addopts."""
    result = _run_documented_no_xdist(BEADS_ACCEPTANCE_TARGET)
    combined = result.stdout + result.stderr
    assert "unrecognized arguments: -n" not in combined
    assert result.returncode == 0, combined


def test_no_xdist_serial_override_smoke_single_bead_test():
    """Faster smoke: one beads nodeid with default addopts and no `-n 0`."""
    result = _run_documented_no_xdist(BEADS_SMOKE_NODEID)
    combined = result.stdout + result.stderr
    assert "unrecognized arguments: -n" not in combined
    assert result.returncode == 0, combined


def test_serial_timing_helpers_do_not_clear_addopts():
    """Serial timing subprocesses must rely on `-p no:xdist` alone."""
    offenders = []
    for path in SERIAL_TIMING_ACCEPTANCE_FILES:
        text = path.read_text(encoding="utf-8")
        if "addopts=" in text:
            offenders.append(path.name)
    assert offenders == []


def test_pyproject_comment_documents_no_xdist_serial_override():
    """pyproject.toml should document the same serial override as README."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "no:xdist" in text
