"""alloy-4ef.18: ``alloy memory embed`` — tests only.

Encode acceptance criteria for rendering the alloy:meta:embed set into a
managed HTML-comment block in instruction files. Expected to fail until
memory_embed lands.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy.cli import app
from alloy.config import MemorySpec
from alloy.memory_embed import (
    BEGIN_MARKER,
    END_MARKER,
    render_embed_block,
    splice_managed_block,
)
from alloy.models import EMBED_KEY, LAST_REVIEW_KEY, ProjectMemory, with_provenance

RUN_ID = "run-embed-1"
BEAD_ID = "alloy-4ef.18"
PROVENANCE_DATE = date(2026, 9, 20)
LAST_REVIEW = date(2026, 9, 22)
REVIEW_DUE = "2026-09-29"

LESSON_KEY = "alloy:lesson:b"
LESSON_BODY = "Read memory once at run start"
HUMAN_KEY = "conv"
HUMAN_VALUE = "repo uses pathlib"

INITIAL_AGENTS = "# Agents\n\nFollow these rules.\n"


def _embed_memories() -> dict[str, str]:
    return {
        EMBED_KEY: json.dumps([HUMAN_KEY, LESSON_KEY]),
        LAST_REVIEW_KEY: LAST_REVIEW.isoformat(),
        HUMAN_KEY: HUMAN_VALUE,
        LESSON_KEY: with_provenance(LESSON_BODY, RUN_ID, BEAD_ID, PROVENANCE_DATE),
    }


def _configure_memories(fake_bd, memories: dict[str, str] | None = None) -> None:
    fake_bd.configure({"memories": memories if memories is not None else _embed_memories()})


def _memory(memories: dict[str, str] | None = None) -> ProjectMemory:
    return ProjectMemory.from_raw(
        memories if memories is not None else _embed_memories(),
        MemorySpec(),
    )


def _expected_inner_block() -> str:
    return "\n".join(
        [
            f"reviewed: {LAST_REVIEW.isoformat()}",
            f"review due: {REVIEW_DUE}",
            "",
            f"### {LESSON_KEY}",
            LESSON_BODY,
            "",
            f"### {HUMAN_KEY}",
            HUMAN_VALUE,
        ]
    )


def _expected_managed_block() -> str:
    return "\n".join(
        [
            BEGIN_MARKER,
            _expected_inner_block(),
            END_MARKER,
        ]
    )


def _invoke_embed(project: Path, alloy_home: Path):
    runner = CliRunner()
    return runner.invoke(
        app,
        [
            "memory",
            "embed",
            "--repo",
            str(project),
            "--root",
            str(alloy_home),
        ],
    )


def _git(args: list[str], repo: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _bd_writes(fake_bd) -> list[dict]:
    return [call for call in fake_bd.calls if call.get("command") in {"remember", "forget"}]


# -- pure render -------------------------------------------------------------


def test_render_embed_block_includes_review_dates_and_sorted_keys_without_provenance():
    rendered = render_embed_block(_memory(), MemorySpec())

    assert f"reviewed: {LAST_REVIEW.isoformat()}" in rendered
    assert f"review due: {REVIEW_DUE}" in rendered
    assert f"### {LESSON_KEY}" in rendered
    assert LESSON_BODY in rendered
    assert f"### {HUMAN_KEY}" in rendered
    assert HUMAN_VALUE in rendered
    assert RUN_ID not in rendered
    assert BEAD_ID not in rendered
    assert rendered.index(f"### {LESSON_KEY}") < rendered.index(f"### {HUMAN_KEY}")


def test_render_embed_block_wraps_content_in_managed_markers():
    rendered = render_embed_block(_memory(), MemorySpec())

    assert rendered.startswith(BEGIN_MARKER)
    assert rendered.endswith(END_MARKER)
    assert _expected_inner_block() in rendered


# -- pure splice -------------------------------------------------------------


def test_splice_managed_block_appends_when_markers_absent():
    managed = _expected_managed_block()
    updated, changed = splice_managed_block(INITIAL_AGENTS, managed)

    assert changed is True
    assert updated.startswith(INITIAL_AGENTS.rstrip("\n"))
    assert managed in updated
    assert updated.count(BEGIN_MARKER) == 1


def test_splice_managed_block_preserves_bytes_outside_markers_and_replaces_inside():
    managed = _expected_managed_block()
    embedded = f"{INITIAL_AGENTS}\n{managed}\n"
    edited = embedded.replace(
        "Follow these rules.",
        "Follow these rules.\n\nOperator note outside markers.",
    ).replace(
        LESSON_BODY,
        "stale inside edit",
    )

    refreshed, changed = splice_managed_block(edited, managed)

    assert changed is True
    assert "Operator note outside markers." in refreshed
    assert "stale inside edit" not in refreshed
    assert LESSON_BODY in refreshed
    assert refreshed.startswith("# Agents\n\nFollow these rules.\n\nOperator note outside markers.")


def test_splice_managed_block_is_noop_when_block_already_matches():
    managed = _expected_managed_block()
    embedded = f"{INITIAL_AGENTS}\n{managed}\n"

    updated, changed = splice_managed_block(embedded, managed)

    assert changed is False
    assert updated == embedded


# -- alloy memory embed CLI --------------------------------------------------


def test_memory_embed_acceptance_scenario(project, alloy_home, fake_bd):
    agents = project / "AGENTS.md"
    agents.write_text(INITIAL_AGENTS, encoding="utf-8")
    # AGENTS.md must be tracked for the acceptance check "only AGENTS.md
    # modified": embed itself never runs git, so the test commits it here.
    _git(["add", "AGENTS.md"], project)
    _git(["commit", "-qm", "add AGENTS.md"], project)
    claude = project / "CLAUDE.md"
    _configure_memories(fake_bd)

    first = _invoke_embed(project, alloy_home)
    assert first.exit_code == 0
    assert "AGENTS.md" in first.stdout
    first_bytes = agents.read_bytes()
    assert BEGIN_MARKER in first_bytes.decode("utf-8")
    assert END_MARKER in first_bytes.decode("utf-8")
    assert f"reviewed: {LAST_REVIEW.isoformat()}" in first_bytes.decode("utf-8")
    assert f"review due: {REVIEW_DUE}" in first_bytes.decode("utf-8")
    assert LESSON_BODY in first_bytes.decode("utf-8")
    assert HUMAN_VALUE in first_bytes.decode("utf-8")
    assert not claude.exists()
    assert _git(["status", "--porcelain"], project).rstrip("\n") == " M AGENTS.md"
    assert _git(["diff", "--cached"], project) == ""
    assert _bd_writes(fake_bd) == []

    second = _invoke_embed(project, alloy_home)
    assert second.exit_code == 0
    assert "AGENTS.md" not in second.stdout
    assert agents.read_bytes() == first_bytes
    assert _git(["status", "--porcelain"], project).rstrip("\n") == " M AGENTS.md"
    assert _bd_writes(fake_bd) == []

    outside_note = "Operator note outside markers."
    inside_stale = "stale inside edit"
    third_source = (
        agents.read_text(encoding="utf-8")
        .replace("Follow these rules.", f"Follow these rules.\n\n{outside_note}")
        .replace(LESSON_BODY, inside_stale)
    )
    agents.write_text(third_source, encoding="utf-8")

    third = _invoke_embed(project, alloy_home)
    assert third.exit_code == 0
    third_text = agents.read_text(encoding="utf-8")
    assert outside_note in third_text
    assert inside_stale not in third_text
    assert LESSON_BODY in third_text
    assert _bd_writes(fake_bd) == []


# -- is_block_stale (alloy-4ef.20) -------------------------------------------


def _import_is_block_stale():
    from alloy.memory_embed import is_block_stale

    return is_block_stale


STALE_CHECK_TODAY = date(2026, 9, 23)


def _agents_with_managed_block(
    *,
    reviewed: date = LAST_REVIEW,
    lesson_body: str = LESSON_BODY,
) -> str:
    memory = _memory()
    managed = render_embed_block(memory, MemorySpec())
    managed = managed.replace(f"reviewed: {LAST_REVIEW.isoformat()}", f"reviewed: {reviewed.isoformat()}")
    managed = managed.replace(LESSON_BODY, lesson_body)
    return f"{INITIAL_AGENTS}\n{managed}\n"


@pytest.fixture
def embed_stale_today(monkeypatch):
    """Pin ``date.today()`` inside memory_embed for age-threshold tests."""

    monkeypatch.setattr(
        "alloy.memory_embed.date",
        type(
            "_FixedDate",
            (date,),
            {"today": classmethod(lambda cls: STALE_CHECK_TODAY)},
        ),
    )


def test_is_block_stale_false_when_file_has_no_managed_block():
    is_block_stale = _import_is_block_stale()
    assert is_block_stale(INITIAL_AGENTS, _memory(), LAST_REVIEW, MemorySpec()) is False


def test_is_block_stale_false_when_block_matches_rendered_set(embed_stale_today):
    is_block_stale = _import_is_block_stale()
    file_text = _agents_with_managed_block()

    assert is_block_stale(file_text, _memory(), LAST_REVIEW, MemorySpec()) is False


def test_is_block_stale_true_when_block_content_differs_from_rendered_set(embed_stale_today):
    is_block_stale = _import_is_block_stale()
    file_text = _agents_with_managed_block(lesson_body="hand-edited inside markers")

    assert is_block_stale(file_text, _memory(), LAST_REVIEW, MemorySpec()) is True


def test_is_block_stale_true_when_reviewed_date_older_than_twice_review_every_days(
    embed_stale_today,
):
    is_block_stale = _import_is_block_stale()
    old_review = STALE_CHECK_TODAY.replace(day=1)  # 22 days before STALE_CHECK_TODAY
    file_text = _agents_with_managed_block(reviewed=old_review)

    assert is_block_stale(file_text, _memory(), old_review, MemorySpec()) is True
