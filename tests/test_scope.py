"""Project context packet and scope merge gate (alloy-0uc.13)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_project_brief_prefers_alloy_project_md(project: Path):
    from alloy.paths import project_brief

    brief_dir = project / ".alloy"
    brief_dir.mkdir()
    (brief_dir / "project.md").write_text("# Operator brief\nDo OAuth first.\n", encoding="utf-8")

    assert project_brief(project) == "# Operator brief\nDo OAuth first.\n"


def test_project_brief_falls_back_to_readme_head(project: Path):
    from alloy.paths import project_brief

    project.joinpath("README.md").write_text(
        "# README title\n" + "line\n" * 80,
        encoding="utf-8",
    )

    brief = project_brief(project)
    assert brief.startswith("# README title")
    assert brief.count("\n") <= 60


def test_project_brief_missing_files_returns_placeholder(tmp_path: Path):
    from alloy.paths import project_brief

    repo = tmp_path / "empty"
    repo.mkdir()
    assert project_brief(repo) == "(no project brief)"


def test_render_project_context_clips_large_snapshots():
    from alloy.models import Attempt, ProjectSnapshot
    from alloy.recipes.tdd_loop import PROJECT_CONTEXT_CHARS, render_project_context

    open_beads = [f"t-{index}: bead title {index}" for index in range(500)]
    snapshot = ProjectSnapshot(
        brief_source="readme",
        open_beads=open_beads,
        epic="Epic: OAuth login\nShip OAuth for the API",
        filed_bugs=[f"bug-{index}: stale token {index}" for index in range(50)],
        stats={"open_issues": 500, "closed_issues": 0},
    )
    brief = "Project brief:\n" + ("context line\n" * 200)
    history = [Attempt(iteration=i, implementer="codex").render() for i in range(20)]
    remediations = [{"bead_id": f"rem-{i}", "outcome": "merged"} for i in range(20)]

    rendered = render_project_context(snapshot, brief, history, remediations)

    assert len(rendered) <= PROJECT_CONTEXT_CHARS
    assert "chars elided" in rendered


def test_scope_prompt_includes_brief_epic_bug_and_diffstat():
    from alloy.recipes.tdd_loop import scope_prompt

    prompt = scope_prompt(
        project_context="## Bead graph\nEpic: OAuth login — Ship OAuth for the API",
        parent_brief="# parent-1: add slugify\nAdd slugify() to mypkg.",
        parent_acceptance="slugify('Hello World') == 'hello-world'",
        bug_brief="# bug-1: stale refresh token\nTokens never rotate.",
        diffstat="2 files changed, 14 insertions(+), 3 deletions(-)",
        diff="diff --git a/mypkg/auth.py b/mypkg/auth.py\n",
    )

    assert prompt.startswith("You are deciding whether a bug fix is safe to merge")
    assert "Add slugify() to mypkg." in prompt
    assert "OAuth login" in prompt
    assert "stale refresh token" in prompt
    assert "2 files changed, 14 insertions(+), 3 deletions(-)" in prompt


async def test_scope_gate_merge_verdict(project, alloy_home, fake_harnesses):
    from alloy.checkpoints import open_checkpointer
    from alloy.recipes.tdd_loop import scope_gate
    from conftest import scope_entry
    from support import make_bead, make_harness, scope_config

    fake_harnesses.configure({"scope": scope_entry("merge", "minimal auth fix")})
    harness = make_harness(project, alloy_home, config=scope_config())
    try:
        async with open_checkpointer(harness.alloy_home / "workflows.db") as checkpointer:
            ctx = harness.context(checkpointer)
            bug = make_bead("bug-1", title="stale refresh token")
            verdict = await scope_gate(ctx, bug, "diff --git a/auth.py\n")
    finally:
        harness.close()

    assert verdict.verdict == "merge"
    assert verdict.reason == "minimal auth fix"


async def test_scope_gate_too_broad_verdict(project, alloy_home, fake_harnesses):
    from alloy.checkpoints import open_checkpointer
    from alloy.recipes.tdd_loop import scope_gate
    from conftest import scope_entry
    from support import make_bead, make_harness, scope_config

    reason = "rewrites the storage layer"
    fake_harnesses.configure({"scope": scope_entry("too-broad", reason)})
    harness = make_harness(project, alloy_home, config=scope_config())
    try:
        async with open_checkpointer(harness.alloy_home / "workflows.db") as checkpointer:
            ctx = harness.context(checkpointer)
            bug = make_bead("bug-2", title="overbroad fix")
            verdict = await scope_gate(ctx, bug, "diff --git a/store.py\n")
    finally:
        harness.close()

    assert verdict.verdict == "too-broad"
    assert reason in verdict.reason


async def test_scope_gate_runner_missing_defaults_to_too_broad(project, alloy_home, fake_harnesses):
    from alloy.checkpoints import open_checkpointer
    from alloy.recipes.tdd_loop import scope_gate
    from support import make_bead, make_harness, scope_config

    config = scope_config(runner="missing-scope-runner", fallback=None)
    harness = make_harness(project, alloy_home, config=config)
    try:
        async with open_checkpointer(harness.alloy_home / "workflows.db") as checkpointer:
            ctx = harness.context(checkpointer)
            bug = make_bead("bug-3", title="unscoped fix")
            verdict = await scope_gate(ctx, bug, "diff --git a/auth.py\n")
    finally:
        harness.close()

    assert verdict.verdict == "too-broad"
    assert verdict.reason.startswith("scope role failed:")


async def test_scope_merge_gate_adapts_verdict_to_bool(project, alloy_home, fake_harnesses):
    from alloy.checkpoints import open_checkpointer
    from alloy.recipes.tdd_loop import scope_merge_gate
    from conftest import scope_entry
    from support import make_bead, make_harness, scope_config

    fake_harnesses.configure({"scope": scope_entry("merge", "ok to land")})
    harness = make_harness(project, alloy_home, config=scope_config())
    try:
        async with open_checkpointer(harness.alloy_home / "workflows.db") as checkpointer:
            ctx = harness.context(checkpointer)
            bug = make_bead("bug-4", title="small fix")
            ok, reason = await scope_merge_gate(ctx, bug, "diff\n")
    finally:
        harness.close()

    assert ok is True
    assert reason == "ok to land"


async def test_scope_merge_gate_rejects_non_merge_verdict(project, alloy_home, fake_harnesses):
    from alloy.checkpoints import open_checkpointer
    from alloy.recipes.tdd_loop import scope_merge_gate
    from conftest import scope_entry
    from support import make_bead, make_harness, scope_config

    detail = "introduces a new public API"
    fake_harnesses.configure({"scope": scope_entry("too-broad", detail)})
    harness = make_harness(project, alloy_home, config=scope_config())
    try:
        async with open_checkpointer(harness.alloy_home / "workflows.db") as checkpointer:
            ctx = harness.context(checkpointer)
            bug = make_bead("bug-5", title="wide fix")
            ok, reason = await scope_merge_gate(ctx, bug, "diff\n")
    finally:
        harness.close()

    assert ok is False
    assert "too-broad" in reason
    assert detail in reason


def test_scope_verdict_schema_puts_verdict_first_for_jev():
    from alloy.models import ScopeVerdict

    schema = ScopeVerdict.schema_for_agents()
    assert list(schema["properties"].keys())[0] == "verdict"
    assert set(schema["properties"]["verdict"]["enum"]) == {
        "merge",
        "too-broad",
        "subverts-task",
    }
