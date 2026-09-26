"""Static check for one of the acceptance criteria that explicitly asks to be
verified "by reading the diff, not just by test": `Store.reconcile_inflight()`
must be called from exactly `Scheduler.recover()` and the resume path of
`Engine._execute()`, and from nowhere else -- specifically not from any future
monitor code.

This does not replace reading the diff; it is a cheap regression guard so a
later change cannot silently add a third call site (e.g. from monitor code)
without a test noticing.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "alloy"


def _call_sites() -> list[tuple[Path, int, str]]:
    sites = []
    for path in SRC.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"\breconcile_inflight\s*\(", line) and "def reconcile_inflight" not in line:
                sites.append((path, lineno, line.strip()))
    return sites


def test_reconcile_inflight_has_exactly_two_call_sites():
    sites = _call_sites()
    assert sites, "expected reconcile_inflight() to be called from scheduler.py and engine.py"
    assert len(sites) == 2, f"unexpected number of call sites: {sites}"


def test_reconcile_inflight_is_only_called_from_scheduler_and_engine():
    allowed = {"scheduler.py", "engine.py"}
    sites = _call_sites()
    offenders = [(path, lineno, line) for path, lineno, line in sites if path.name not in allowed]
    assert offenders == [], f"reconcile_inflight() called from an unexpected file: {offenders}"


def test_reconcile_inflight_is_not_called_from_any_monitor_module():
    sites = _call_sites()
    offenders = [(path, lineno, line) for path, lineno, line in sites if "monitor" in path.name.lower()]
    assert offenders == []
