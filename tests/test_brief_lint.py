"""Bead brief lint: lines in a bead's text that tell the agent to commit, push
or run other git writes are dropped from what agents see (Alloy commits; the
sandbox's .git is read-only, so an agent that tries parks the run). The
stored bd text is never modified, and lines that only mention commits as
behaviour under test are kept."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import IMPLEMENTATION, bd_create
from support import make_bead
from test_fast_track import done_entry, run_check_entry

from alloy.brief_lint import GIT_WRITE_LINE_PATTERNS, lint_bead, lint_text
from alloy.config import load_recipe
from alloy.engine import Engine

DROPPED = [
    "Commit: fix(client): drop stale rows",
    "commit: feat: add slugify",
    "**Commit:** `fix(server): forward policy`",
    "- Commit message: chore(release): 7.3.2",
    "- git commit -am 'fix: x'",
    "Then git push origin main.",
    'Run `git commit -m "fix: y"` when green.',
    "Push to main when the suite is green.",
    "Commit and push to main.",
    "3. Commit the changes.",
]

KEPT = [
    "the commit message includes the bead id",
    "- Acceptance: the commit message includes `fix(client):` and the bead id",
    "Verify that `git commit` in the hook rejects unsigned commits",
    "Do not run git push yourself.",
    "Commits are recorded with the bead id by Alloy.",
    "A push to main triggers the deploy job (behaviour under test).",
    "Add slugify() to mypkg.",
    "- `git merge-base --is-ancestor` returns 0 for the recorded base",
    "- git stash list stays empty after the run",
    "Run git tag -l to see the tags; git tag --list works too.",
    "- git stash show prints nothing",
    "- Commit the changes in a single DB transaction; no partial rows",
    "- Push to remote subscribers only when the flag is on",
]

# Acceptance criteria the old patterns dropped: acceptance is never linted.
REVIEW_ACCEPTANCE = [
    "- `git merge-base --is-ancestor` returns 0 for the recorded base",
    "- git tag v7.3.2 exists after the bump script runs",
    "- git stash list stays empty after the run",
    "- Commit the changes in a single DB transaction; no partial rows",
    "- Push to remote subscribers only when the flag is on",
]


@pytest.mark.parametrize("line", DROPPED)
def test_git_write_instructions_are_dropped(line):
    text, dropped = lint_text(f"Add slugify.\n{line}\nKeep it small.")
    assert dropped == [line.strip()]
    assert text == "Add slugify.\nKeep it small."


@pytest.mark.parametrize("line", KEPT)
def test_lines_that_only_mention_commits_are_kept(line):
    text, dropped = lint_text(line)
    assert dropped == []
    assert text == line


def test_fenced_code_is_never_touched():
    text = "Write scripts/release.sh:\n```bash\ngit push origin main\n```"
    assert lint_text(text) == (text, [])


def test_patterns_live_in_one_named_constant():
    assert set(GIT_WRITE_LINE_PATTERNS) >= {"conventional-commit", "git-write", "push-to-remote", "commit-the-work"}


def test_lint_bead_returns_a_copy_and_flags_version_pins_only_in_a_ship_context():
    bead = make_bead(
        title="ship 7.3.2",
        description="Bump the client to 7.3.2.\nCommit: chore(release): 7.3.2",
        acceptance_criteria="pubspec says 7.3.2\nthe commit message includes the version",
    )
    result = lint_bead(bead, ship_context=True)
    assert result.bead.description == "Bump the client to 7.3.2."
    assert result.bead.acceptance_criteria == bead.acceptance_criteria
    assert bead.description.endswith("Commit: chore(release): 7.3.2"), "the original bead is untouched"
    assert result.dropped == ["Commit: chore(release): 7.3.2"]
    assert result.version_pins == ["7.3.2"]
    assert lint_bead(bead, ship_context=False).version_pins == []


def test_a_clean_bead_is_returned_as_is():
    bead = make_bead()
    result = lint_bead(bead, ship_context=False)
    assert result.bead is bead and not result.dropped and not result.version_pins


async def test_agents_never_see_the_dropped_line_and_one_event_names_it(
    beads_project, alloy_home, fake_harnesses, monkeypatch
):
    engine = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(engine, "load_config", lambda name: load_recipe("fast-track"))
    probe = "test -f mypkg/__init__.py"
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(probe, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
                done_entry(reason="verified"),
                run_check_entry(probe, reason="final pass check"),
                done_entry(reason="final pass verified"),
            ]
        }
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="fast-track")
    description = "Add slugify() to mypkg.\nCommit: fix(mypkg): add slugify"
    subprocess.run(
        ["bd", "update", bead_id, "--description", description], cwd=beads_project, check=True, capture_output=True
    )

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    prompts = [call["prompt"] for call in fake_harnesses.calls_for("implement")]
    assert prompts and all("Add slugify() to mypkg." in p for p in prompts)
    assert not any("Commit: fix(mypkg)" in p for p in prompts)
    assert engine.beads.show(bead_id).description == description, "the stored bd text is not modified"
    events = [json.loads(line) for line in (alloy_home / "events.jsonl").read_text().splitlines()]
    lint = [e for e in events if e["event"] == "brief-lint"]
    assert len(lint) == 1
    assert lint[0]["dropped"] == ["Commit: fix(mypkg): add slugify"]


# -- a commit clause in the middle of a line ----------------------------------

MID_LINE = [
    (
        "Spec and audit: docs/plans/qa-backlog-2026-10-24.md. Commit: fix(qa): tidy the backlog (Part of #666). "
        "Do not close the GitHub issue.",
        "Spec and audit: docs/plans/qa-backlog-2026-10-24.md. Do not close the GitHub issue.",
        "Commit: fix(qa): tidy the backlog (Part of #666).",
    ),
    (
        "Fold the check into the loader instead of bolting it on. Commit: fix(client): load once (Part of #763).",
        "Fold the check into the loader instead of bolting it on.",
        "Commit: fix(client): load once (Part of #763).",
    ),
    (
        "Bump the version and raise kDefaultMinClientVersion. "
        "Commit: chore: release <version>; raise the client floor.",
        "Bump the version and raise kDefaultMinClientVersion. raise the client floor.",
        "Commit: chore: release <version>;",
    ),
]


@pytest.mark.parametrize(("line", "expected", "clause"), MID_LINE)
def test_a_mid_line_commit_clause_is_dropped_and_the_rest_kept(line, expected, clause):
    text, dropped = lint_text(f"Intro.\n{line}\nOutro.")
    assert text == f"Intro.\n{expected}\nOutro."
    assert dropped == [clause]


def test_a_sentence_about_the_commit_message_stays_untouched():
    line = "The commit message includes the bead id."
    assert lint_text(f"Do it.\n{line}") == (f"Do it.\n{line}", [])


def test_a_lowercase_word_after_a_period_does_not_end_the_clause():
    text, dropped = lint_text("Do it. Commit: fix(x): handle e.g. empty rows (Part of #9). Then stop.")
    assert text == "Do it. Then stop."
    assert dropped == ["Commit: fix(x): handle e.g. empty rows (Part of #9)."]


def test_a_commit_clause_after_a_plain_word_is_not_a_sentence_boundary():
    line = "Check the hook Commit: fix(x): y handling stays as is."
    assert lint_text(line) == (line, [])


def test_acceptance_criteria_are_never_linted_only_reported():
    acceptance = "\n".join([*REVIEW_ACCEPTANCE, "Commit: fix(x): y", "git push origin main"])
    bead = make_bead(acceptance_criteria=acceptance)
    result = lint_bead(bead, ship_context=False)
    assert result.bead.acceptance_criteria == acceptance
    assert result.dropped == []
    assert result.suspicious == [
        "- git tag v7.3.2 exists after the bump script runs",
        "Commit: fix(x): y",
        "git push origin main",
    ]
