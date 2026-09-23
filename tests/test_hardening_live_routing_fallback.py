"""Acceptance tests for alloy-ezo: journal-18 implement fallback vs live routing.

Commit 5301c3f turned on live tier dispatch for implement; tests/test_hardening.py
still scripts implement@codex and asserts codex-first runner order. These tests
encode the fix: pin shadow routing in the journal-18 fallback regressions so
per-role fallback stays codex-primary instead of inheriting the live tier chain.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARDENING_SOURCE = (_REPO_ROOT / "tests" / "test_hardening.py").read_text(encoding="utf-8")

_JOURNAL_18_FALLBACK_TESTS = (
    "test_fallback_runner_takes_over_when_the_primary_fails",
    "test_no_fallback_means_the_failure_stands",
)


def _function_source(function_name: str) -> str:
    marker = f"async def {function_name}"
    start = _HARDENING_SOURCE.index(marker)
    rest = _HARDENING_SOURCE[start + len(marker) :]
    next_def = rest.find("\nasync def ")
    next_section = rest.find("\n\n# --")
    end_candidates = [i for i in (next_def, next_section) if i != -1]
    end = start + len(marker) + (min(end_candidates) if end_candidates else len(rest))
    return _HARDENING_SOURCE[start:end]


def test_hardening_journal_18_fallback_tests_pin_shadow_routing():
    """Role-level fallback regressions must not inherit live implement tier dispatch."""
    for name in _JOURNAL_18_FALLBACK_TESTS:
        source = _function_source(name)
        assert (
            'routing="shadow"' in source
            or "routing='shadow'" in source
            or "_shadow_config" in source
            or "shadow_config" in source
        ), f"{name} must pin shadow routing so implement stays codex-primary"


def test_hardening_journal_18_fallback_tests_pass():
    """The journal-18 fallback regressions in test_hardening.py must stay green."""
    env = os.environ.copy()
    env["TYPESAFE_API_KEY"] = "ambient-host-key"
    nodeids = [f"tests/test_hardening.py::{name}" for name in _JOURNAL_18_FALLBACK_TESTS]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "0", "-q", *nodeids],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
