"""Regression tests for serial pytest override without conftest masking (alloy-lfp).

Behaviour is not implemented yet — these must fail until the serial ``-p
no:xdist`` fix lives outside ``tests/conftest.py`` (so subprocess regressions
are not pre-satisfied by the alloy-fon.3 hook), timing helpers drop the
``-o addopts=`` workaround, and ``pyproject.toml`` documents the same override
as README.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
PYPROJECT = REPO_ROOT / "pyproject.toml"
CONFTEST = REPO_ROOT / "tests" / "conftest.py"
ISOLATED_DIR = REPO_ROOT / "tests" / "isolated"
ISOLATED_SMOKE = ISOLATED_DIR / "test_serial_smoke.py"

SERIAL_TIMING_ACCEPTANCE_FILES = (
    REPO_ROOT / "tests" / "test_wait_for_role_acceptance.py",
    REPO_ROOT / "tests" / "test_checkpoints_cancellation_acceptance.py",
)


def _run_no_xdist_without_conftest(target: str) -> subprocess.CompletedProcess[str]:
    """Subprocess pytest that inherits repo addopts but not tests/conftest.py."""
    return subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            target,
            "-p",
            "no:xdist",
            "--confcutdir",
            str(ISOLATED_DIR),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_conftest_does_not_workaround_no_xdist_addopts_conflict():
    """The alloy-fon.3 hook must not mask missing production/config fixes."""
    text = CONFTEST.read_text(encoding="utf-8")
    assert "register_xdist_options" not in text


def test_no_xdist_serial_override_without_conftest():
    """Isolated smoke: ``-p no:xdist`` must work with default addopts and no hook."""
    result = _run_no_xdist_without_conftest(str(ISOLATED_SMOKE.relative_to(REPO_ROOT)))
    combined = result.stdout + result.stderr
    assert "unrecognized arguments: -n" not in combined
    assert result.returncode == 0, combined


def test_serial_timing_helpers_do_not_clear_addopts():
    """Serial timing subprocesses must rely on ``-p no:xdist`` alone."""
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
