"""Verifier diff-derived check hints (alloy-8by.4).

When the worktree diff touches monitor implementation code, every matching
``tests/test_monitor*.py`` module must appear under the verifier's
``Hints from the repository (not yet verified)`` section. Hints are not run
automatically.
"""

from __future__ import annotations

from pathlib import Path

from alloy.recipes.tdd_loop import verifier_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITOR_APP = "src/alloy/monitor/app.py"
HINTS_SECTION = "## Hints from the repository (not yet verified)"


def _monitor_test_paths() -> list[str]:
    return sorted(path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / "tests").glob("test_monitor*.py"))


def _diff_touching_monitor_app() -> str:
    return f"""diff --git a/{MONITOR_APP} b/{MONITOR_APP}
index 1111111..2222222 100644
--- a/{MONITOR_APP}
+++ b/{MONITOR_APP}
@@ -1,3 +1,4 @@
 # monitor app
+pass
"""


def _hints_body(prompt: str) -> str:
    assert HINTS_SECTION in prompt
    body = prompt.split(HINTS_SECTION, 1)[1]
    baseline = "## Baseline commands"
    if baseline in body:
        body = body.split(baseline, 1)[0]
    return body


def _verifier_prompt_for_monitor_touch(**overrides) -> str:
    defaults = {
        "brief": "Adjust monitor app behavior",
        "acceptance": "monitor still renders",
        "context": {"summary": "monitor package", "check_hints": []},
        "diff": _diff_touching_monitor_app(),
        "changed_files": [MONITOR_APP],
        "checks_this_run": [],
        "iteration": 1,
        "checks_left_iteration": 5,
        "checks_left_run": 10,
        "history": [],
        "baseline_checks": [],
        "instructions": "",
    }
    defaults.update(overrides)
    return verifier_prompt(**defaults)


def test_monitor_test_glob_is_non_empty():
    paths = _monitor_test_paths()
    assert paths, "expected at least one tests/test_monitor*.py in this repo"
    assert "tests/test_monitor_detail.py" in paths


def test_verifier_hints_list_every_monitor_test_file_when_diff_touches_monitor_app():
    prompt = _verifier_prompt_for_monitor_touch()
    hints = _hints_body(prompt)
    missing = [path for path in _monitor_test_paths() if path not in hints]
    assert missing == [], f"expected diff-derived verifier hints for each tests/test_monitor*.py; missing: {missing}"


def test_verifier_hints_derive_monitor_tests_without_context_check_hints():
    """Diff-derived hints must not depend on the context role returning check_hints."""
    prompt = _verifier_prompt_for_monitor_touch(
        context={"summary": "monitor package", "check_hints": ["cargo test"]},
    )
    hints = _hints_body(prompt)
    for path in _monitor_test_paths():
        assert path in hints
