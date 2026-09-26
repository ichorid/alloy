"""Land recipe: trial merge, verifier check loop, judge on the merged tree.

Acceptance tests for alloy-vrh.7. Behaviour is not implemented yet — these must
fail until land is registered, land.yaml exists, and the graph produces the
conflict / done / red outcomes described in docs/plans/auto-land.md.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from alloy import recipes
from alloy.config import load_recipe
from conftest import acceptance_entry, judge_entry, verifier_run_entry, verifier_stop_entry
from support import LAND_RECIPE_NAME, load_land_config, make_bead, make_harness

FULL_SUITE = f"{sys.executable} -m pytest -q"
FAILING_CHECK = 'sh -c "exit 1"'


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _head_parent_count(cwd: Path) -> int:
    parts = _git(cwd, "rev-list", "--parents", "-n", "1", "HEAD").stdout.strip().split()
    return len(parts) - 1


def _land_script(**overrides):
    """Verifier scripting for the land recipe's post-merge check loop."""
    base = {
        "verifier": [
            verifier_run_entry(FULL_SUITE, kind="regression"),
            verifier_stop_entry("regression suite green on merged tree"),
        ],
        "acceptance": [acceptance_entry("accept", confidence=0.9)],
        "judge": [judge_entry("done")],
    }
    base.update(overrides)
    return base


def _prepare_clean_merge(project: Path, harness) -> tuple[Path, str]:
    """Bead branch with a commit; main advanced separately; returns worktree path."""
    worktree = harness.worktrees.ensure(harness.bead.id)
    (worktree.path / "feature.txt").write_text("bead work\n", encoding="utf-8")
    # The project fixture ships an empty tests/ dir, where `pytest -q` exits 5
    # (no tests collected) -- the bead branch must carry a passing test so the
    # verifier-chosen full suite can exit 0, as the acceptance criteria assume.
    # (git does not track the empty dir, so the worktree has no tests/ yet.)
    (worktree.path / "tests").mkdir(exist_ok=True)
    (worktree.path / "tests" / "test_placeholder.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead commit")

    (project / "main.txt").write_text("main advance\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main advance")

    return worktree, _head(project)


def _prepare_merge_conflict(project: Path, harness, conflict_path: str = "mypkg/__init__.py"):
    """Bead and main both edit the same file so trial_merge conflicts."""
    worktree = harness.worktrees.ensure(harness.bead.id)
    (worktree.path / conflict_path).write_text("bead = 1\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead change")

    (project / conflict_path).write_text("main = 2\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main change")

    return worktree, _head(worktree.path), _head(project)


# -- registry and config ----------------------------------------------------


def test_recipes_names_includes_land():
    assert LAND_RECIPE_NAME in recipes.names()


def test_load_land_recipe_yaml_and_registry():
    config = load_recipe(LAND_RECIPE_NAME)
    assert config.name == LAND_RECIPE_NAME
    recipe = recipes.get(LAND_RECIPE_NAME)
    assert recipe.build_graph is not None
    assert recipe.initial_state is not None


# -- graph outcomes ---------------------------------------------------------


async def test_land_clean_merge_green_checks_ends_done_with_merge_commit(
    project,
    alloy_home,
    fake_harnesses,
):
    """Clean trial merge + green verifier check ends done; bead branch has merge commit."""
    fake_harnesses.configure(_land_script())
    bead = make_bead(id="land-done", status="review-ready")
    harness = make_harness(
        project,
        alloy_home,
        bead=bead,
        recipe_name=LAND_RECIPE_NAME,
    )
    worktree, primary_head_before = _prepare_clean_merge(project, harness)
    bead_head_before = _head(worktree.path)

    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert _head_parent_count(worktree.path) == 2
    assert _head(worktree.path) != bead_head_before
    assert _head(project) == primary_head_before


async def test_land_merge_conflict_ends_conflict_lists_file_without_merge_commit(
    project,
    alloy_home,
    fake_harnesses,
):
    """A conflicting target ends conflict with the file listed; no merge commit."""
    conflict_path = "mypkg/__init__.py"
    fake_harnesses.configure(_land_script())
    bead = make_bead(id="land-conflict", status="review-ready")
    harness = make_harness(
        project,
        alloy_home,
        bead=bead,
        recipe_name=LAND_RECIPE_NAME,
    )
    worktree, bead_head_before, primary_head_before = _prepare_merge_conflict(
        project,
        harness,
        conflict_path,
    )

    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "conflict"
    conflict_files = final.get("conflict_files") or []
    assert conflict_path in conflict_files
    assert _head(worktree.path) == bead_head_before
    assert _head_parent_count(worktree.path) == 1
    assert _head(project) == primary_head_before


async def test_land_red_required_check_ends_red_naming_command(
    project,
    alloy_home,
    fake_harnesses,
):
    """A required check that exits 1 ends red naming the command; primary untouched."""
    fake_harnesses.configure(
        _land_script(
            verifier=[
                verifier_run_entry(FAILING_CHECK, kind="regression"),
                verifier_stop_entry("should not reach stop after red check"),
            ],
        )
    )
    bead = make_bead(id="land-red", status="review-ready")
    harness = make_harness(
        project,
        alloy_home,
        bead=bead,
        recipe_name=LAND_RECIPE_NAME,
    )
    _prepare_clean_merge(project, harness)
    primary_head_before = _head(project)

    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "red"
    last_check = final.get("last_check") or {}
    named = last_check.get("command") or final.get("outcome_reason") or ""
    assert FAILING_CHECK in named
    assert _head(project) == primary_head_before


async def test_land_config_loads_for_harness(project, alloy_home):
    """land_config helper loads the shipped land recipe YAML."""
    config = load_land_config()
    assert config.name == LAND_RECIPE_NAME
    assert config.landing.target == "main"
