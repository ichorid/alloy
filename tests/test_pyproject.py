"""Acceptance tests for parallel pytest defaults (alloy-fon.1).

Behaviour is not implemented yet — these must fail until pyproject.toml lists
pytest-xdist in the dev extra, enables parallel workers via addopts, and
README.md documents the serial override.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_dev_extra_lists_pytest_xdist():
    dev = _load_pyproject()["project"]["optional-dependencies"]["dev"]
    assert any(dep.startswith("pytest-xdist") for dep in dev)


def test_pytest_addopts_enable_parallel_workers():
    addopts = _load_pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
    assert "-n" in addopts


def test_readme_documents_parallel_default_and_serial_override():
    text = README.read_text(encoding="utf-8")
    tests_section = text.split("## Tests", 1)[1]
    assert "parallel" in tests_section.lower()
    assert "-n 0" in tests_section or "-p no:xdist" in tests_section


def test_plain_pytest_invocation_uses_xdist_workers():
    """Default `pytest -q` must inherit addopts and show the xdist worker header."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{__file__}::test_dev_extra_lists_pytest_xdist",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert "bringing up nodes" in combined.lower()
    assert result.returncode == 0
