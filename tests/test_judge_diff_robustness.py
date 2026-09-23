"""alloy-5wb.1: judge diff excludes generated files and clips per file.

Tests encode the acceptance criteria for JUNK_PATTERNS git excludes in
WorktreeManager, clip_diff_per_file in alloy.diffs, and per-file diff clipping
at every prompt site in tdd_loop.py. Expected to fail until implemented.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alloy.recipes.tdd_loop import (
    MAX_DIFF_CHARS,
    acceptance_prompt,
    judge_prompt,
    verifier_prompt,
)
from alloy.worktree import WorktreeManager

REPO_ROOT = Path(__file__).resolve().parents[1]
TDD_LOOP = REPO_ROOT / "src" / "alloy" / "recipes" / "tdd_loop.py"

SMALL_MARKER = "SMALL_UNIQUE_MARKER_5wb1"
PER_FILE_CLIP = 4000

@pytest.fixture
def manager(project, tmp_path):
    return WorktreeManager(repo=project, root=tmp_path / "worktrees")


EXPECTED_JUNK_PATTERNS = {
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "*.pyc",
    "__pycache__/",
    ".serena/",
    ".DS_Store",
    "*.egg-info/",
}


def _file_diff(path: str, body: str) -> str:
    lines = body.splitlines() or [""]
    header = (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
    )
    hunk = f"@@ -0,0 +1,{len(lines)} @@\n"
    content = "\n".join(f"+{line}" for line in lines)
    return header + hunk + content + "\n"


def _two_file_diff() -> str:
    """One 30k-char file then a 500-char file -- the layout that hides real hunks."""
    small = f"{SMALL_MARKER}\n" + ("b" * 480)
    big = "A" * 30000
    return _file_diff("big.txt", big) + _file_diff("small.txt", small)


def _diff_from_prompt(prompt: str) -> str:
    start = prompt.index("```diff\n") + len("```diff\n")
    end = prompt.index("\n```", start)
    return prompt[start:end]


def _expected_clipped_diff(diff: str) -> str:
    from alloy.diffs import clip_diff_per_file

    return clip_diff_per_file(diff, per_file=PER_FILE_CLIP, total=MAX_DIFF_CHARS)


# ---------------------------------------------------------------------------
# WorktreeManager excludes junk from diff() and changed_files()
# ---------------------------------------------------------------------------


def test_junk_patterns_constant_lists_expected_globs():
    from alloy.worktree import JUNK_PATTERNS

    assert EXPECTED_JUNK_PATTERNS <= set(JUNK_PATTERNS)


def test_diff_and_changed_files_exclude_junk_but_keep_real_src_file(manager):
    worktree = manager.ensure("bd-1")
    (worktree.path / "uv.lock").write_text("lock " * 500, encoding="utf-8")
    (worktree.path / ".serena").mkdir(parents=True)
    (worktree.path / ".serena" / "project.yml").write_text("name: junk\n", encoding="utf-8")
    (worktree.path / "src").mkdir(parents=True)
    (worktree.path / "src" / "real.py").write_text("REAL = 1\n", encoding="utf-8")

    diff = manager.diff(worktree)
    changed = manager.changed_files(worktree)

    assert "uv.lock" not in diff
    assert ".serena" not in diff
    assert "src/real.py" in diff
    assert "REAL = 1" in diff
    assert "src/real.py" in changed
    assert "uv.lock" not in changed
    assert not any(".serena" in path for path in changed)


# ---------------------------------------------------------------------------
# clip_diff_per_file (alloy.diffs)
# ---------------------------------------------------------------------------


def test_clip_diff_per_file_keeps_small_file_clips_big_and_lists_cut_file():
    from alloy.diffs import clip_diff_per_file

    diff = _two_file_diff()
    clipped = clip_diff_per_file(diff, per_file=PER_FILE_CLIP, total=MAX_DIFF_CHARS)

    assert SMALL_MARKER in clipped
    assert _file_diff("small.txt", f"{SMALL_MARKER}\n" + ("b" * 480)).strip() in clipped
    assert "A" * 8000 not in clipped
    assert re.search(r"big\.txt", clipped.splitlines()[-1])
    assert len(clipped) <= MAX_DIFF_CHARS + 200


# ---------------------------------------------------------------------------
# Prompt builders embed per-file clipped diffs with small hunks intact
# ---------------------------------------------------------------------------


@pytest.fixture
def two_file_diff() -> str:
    return _two_file_diff()


def test_judge_prompt_embeds_per_file_clipped_diff_with_small_hunk_intact(two_file_diff):
    prompt = judge_prompt(
        "brief",
        "acceptance",
        {},
        two_file_diff,
        [],
        [],
        1,
        "within limits",
    )

    assert _diff_from_prompt(prompt) == _expected_clipped_diff(two_file_diff)
    assert SMALL_MARKER in prompt


def test_acceptance_prompt_embeds_per_file_clipped_diff_with_small_hunk_intact(two_file_diff):
    prompt = acceptance_prompt(
        "acceptance",
        two_file_diff,
        changed_tests=[],
        checks_this_iteration=[],
        verifier_stop=None,
    )

    assert _diff_from_prompt(prompt) == _expected_clipped_diff(two_file_diff)
    assert SMALL_MARKER in prompt


def test_verifier_prompt_embeds_per_file_clipped_diff_with_small_hunk_intact(two_file_diff):
    prompt = verifier_prompt(
        brief="brief",
        acceptance="acceptance",
        context={"summary": "demo"},
        diff=two_file_diff,
        changed_files=["small.txt"],
        checks_this_run=[],
        iteration=1,
        checks_left_iteration=3,
        checks_left_run=5,
        history=[],
    )

    assert _diff_from_prompt(prompt) == _expected_clipped_diff(two_file_diff)
    assert SMALL_MARKER in prompt


# ---------------------------------------------------------------------------
# tdd_loop must not call clip(diff, MAX_DIFF_CHARS) directly
# ---------------------------------------------------------------------------


def test_tdd_loop_does_not_clip_whole_diff_directly():
    offenders: list[str] = []
    for lineno, line in enumerate(TDD_LOOP.read_text(encoding="utf-8").splitlines(), start=1):
        if "clip(diff, MAX_DIFF_CHARS)" in line:
            offenders.append(f"{TDD_LOOP.relative_to(REPO_ROOT)}:{lineno}:{line.strip()}")
    assert offenders == []
