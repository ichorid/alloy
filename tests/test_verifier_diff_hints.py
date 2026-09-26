"""Verifier diff-derived check hints (alloy-8by.4).

When the worktree diff touches monitor implementation code, every matching
``tests/test_monitor*.py`` module must appear under the verifier's
``Hints from the repository (not yet verified)`` section. Hints are not run
automatically.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from support import make_bead, make_harness
from workflow_support import script

from alloy.recipes.shared_verification import verifier_context
from alloy.recipes.tdd_loop import verifier_prompt
from alloy.verify import diff_derived_test_paths

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


def test_workflow_source_suggests_cross_cutting_tests_once():
    paths = diff_derived_test_paths(
        ["src/alloy/recipes/tdd_loop.py", "src/alloy/recipes/role_prompts.py"],
        REPO_ROOT,
    )
    for name in ("test_workflow.py", "test_prompts.py", "test_triage.py", "test_verify.py"):
        assert f"tests/{name}" in paths
    assert len(paths) == len(set(paths))


def test_workflow_hints_are_advisory_commands():
    prompt = _verifier_prompt_for_monitor_touch(
        changed_files=["src/alloy/recipes/tdd_loop.py"],
        diff="diff --git a/src/alloy/recipes/tdd_loop.py b/src/alloy/recipes/tdd_loop.py\n",
    )
    hints = _hints_body(prompt)
    assert "uv run pytest -n 0 -q tests/test_workflow.py" in hints
    assert "Hints from the repository (not yet verified)" in prompt


def test_no_context_verifier_keeps_bead_memory_and_autodetected_hints():
    ctx = SimpleNamespace(
        bead=SimpleNamespace(check_hint="uv run pytest -n 0 -q tests/test_workflow.py"),
        worktree=SimpleNamespace(path=REPO_ROOT),
        worktrees=SimpleNamespace(repo=REPO_ROOT),
    )
    context = verifier_context(
        {"memory_check_hints": "targeted: uv run pytest -n 0 -q tests/test_prompts.py"},
        ctx,
    )
    hints = context["check_hints"]
    assert hints[0] == "uv run pytest -n 0 -q tests/test_workflow.py"
    assert "uv run pytest -n 0 -q tests/test_prompts.py" in hints
    assert any("pytest" in hint for hint in hints)
    prompt = _verifier_prompt_for_monitor_touch(context=context)
    body = _hints_body(prompt)
    assert hints[0] in body
    assert "uv run pytest -n 0 -q tests/test_prompts.py" in body


async def test_no_context_recipe_delivers_all_hint_sources_to_verifier(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    bead = make_bead(
        metadata={
            "alloy_recipe": "tdd-loop-sonnet-no-context",
            "alloy_test_cmd": "uv run pytest -n 0 -q tests/test_slugify.py",
        }
    )
    harness = make_harness(
        project,
        alloy_home,
        bead=bead,
        recipe_name="tdd-loop-sonnet-no-context",
        initial_state_overrides={"memory_check_hints": "targeted: uv run pytest -n 0 -q tests/test_other.py"},
    )
    try:
        final = await harness.start()
    finally:
        harness.close()
    roles = [call["role"] for call in fake_harnesses.calls]
    assert "context" not in roles
    prompt = next(call["prompt"] for call in fake_harnesses.calls if call["role"] == "verifier")
    hints = _hints_body(prompt)
    assert "uv run pytest -n 0 -q tests/test_slugify.py" in hints
    assert "uv run pytest -n 0 -q tests/test_other.py" in hints
    assert "python -m pytest -q" in hints
    assert all("test_other.py" not in check["command"] for check in final["checks"])
