"""alloy-ehd: memory CLI UTC date alignment — tests only.

Encode acceptance criteria that memory list ``age_days`` and memory review
``--apply`` ``LAST_REVIEW_KEY`` stamps use ``utcnow().date()`` (UTC), not
``date.today()`` (local). Production already passes UTC via ``cli.py``; the
flake is in test expectations at ``test_cli_memory.py:124`` and
``test_memory_review.py:417``. Expected to fail until those assertions align
with ``utcnow().date()``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy.cli import app
from alloy.models import with_provenance
from conftest import FAKE_BD_SOURCE, FAKE_RUNNERS, FAKE_SOURCE, memory_reviewer_entry
from test_cli_memory import (
    LESSON_KEY,
    PROVENANCE_DATE,
    _configure_memories,
    _index_by_key,
)
from test_memory_review import (
    APPLY_ALLOY_EMBED_KEY,
    APPLY_ALLOY_FORGET_KEY,
    APPLY_HUMAN_FORGET_KEY,
    APPLY_META_EMBED_KEY,
    APPLY_RECENT_DATE,
    BEAD_ID as REVIEW_BEAD_ID,
    LAST_REVIEW_KEY,
    RUN_ID as REVIEW_RUN_ID,
    FakeMemoryReviewHarness,
)

# 00:09 CEST = 22:09 UTC previous calendar day — the reported failure window.
UTC_CLOCK = datetime(2026, 9, 23, 22, 9, 0, tzinfo=timezone.utc)
UTC_TODAY = UTC_CLOCK.date()
LOCAL_TODAY = date(2026, 9, 24)


@pytest.fixture
def fake_memory_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name in FAKE_RUNNERS:
        target = bindir / name
        shutil.copy(FAKE_SOURCE, target)
        target.chmod(0o755)
    bd_binary = bindir / "bd"
    shutil.copy(FAKE_BD_SOURCE, bd_binary)
    bd_binary.chmod(0o755)

    workdir = tmp_path / "fake-state"
    workdir.mkdir()
    config_path = workdir / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ALLOY_FAKE_DIR", str(workdir))
    monkeypatch.setenv("ALLOY_FAKE_CONFIG", str(config_path))

    return FakeMemoryReviewHarness(bindir=bindir, workdir=workdir, config_path=config_path)


@pytest.fixture
def memory_utc_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin CLI to UTC today while ``date.today()`` returns the local calendar day."""
    monkeypatch.setattr("alloy.cli.utcnow", lambda: UTC_CLOCK)
    monkeypatch.setattr("alloy.models.utcnow", lambda: UTC_CLOCK)
    skewed_date = type(
        "_SkewedDate",
        (date,),
        {"today": classmethod(lambda cls: LOCAL_TODAY)},
    )
    monkeypatch.setattr("datetime.date", skewed_date)
    # This module's own ``date`` binding was imported by name, so patching
    # ``datetime.date`` above does not reach it; rebind it here or the
    # cross-check assertions below read the real wall clock.
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "date", skewed_date)


def _invoke_memory_list(*, project, alloy_home):
    runner = CliRunner()
    return runner.invoke(
        app,
        ["memory", "list", "--json", "--repo", str(project), "--root", str(alloy_home)],
    )


def _invoke_review_apply(*, project, alloy_home):
    runner = CliRunner()
    return runner.invoke(
        app,
        [
            "memory",
            "review",
            "--apply",
            "--json",
            "--repo",
            str(project),
            "--root",
            str(alloy_home),
        ],
    )


def _apply_memories() -> dict[str, str]:
    return {
        APPLY_ALLOY_FORGET_KEY: with_provenance(
            "lesson a body",
            REVIEW_RUN_ID,
            REVIEW_BEAD_ID,
            APPLY_RECENT_DATE,
        ),
        APPLY_HUMAN_FORGET_KEY: "human memory to forget",
        APPLY_ALLOY_EMBED_KEY: with_provenance(
            "lesson b body",
            REVIEW_RUN_ID,
            REVIEW_BEAD_ID,
            APPLY_RECENT_DATE,
        ),
        APPLY_META_EMBED_KEY: "meta x body",
    }


def _apply_verdicts() -> list[dict[str, str]]:
    return [
        {"action": "forget", "key": APPLY_ALLOY_FORGET_KEY, "reason": "stale lesson a"},
        {"action": "forget", "key": APPLY_HUMAN_FORGET_KEY, "reason": "human should propose"},
        {"action": "embed", "key": APPLY_ALLOY_EMBED_KEY, "reason": "belongs in AGENTS.md"},
        {
            "action": "embed",
            "key": APPLY_META_EMBED_KEY,
            "reason": "meta keys excluded from embed list",
        },
    ]


def _configure_review_apply(harness: FakeMemoryReviewHarness) -> None:
    harness.configure(
        {
            "memories": _apply_memories(),
            "memory_reviewer": memory_reviewer_entry(_apply_verdicts()),
        }
    )


# -- acceptance: existing test assertions must use utcnow().date() -------------


def test_cli_memory_age_days_assertion_uses_utcnow_date() -> None:
    text = (Path(__file__).parent / "test_cli_memory.py").read_text(encoding="utf-8")
    assert 'assert lesson["age_days"] == (utcnow().date() - PROVENANCE_DATE).days' in text


def test_memory_review_last_review_assertion_uses_utcnow_date() -> None:
    text = (Path(__file__).parent / "test_memory_review.py").read_text(encoding="utf-8")
    assert 'assert remembers[LAST_REVIEW_KEY] == utcnow().date().isoformat()' in text


# -- acceptance: CLI output matches UTC today under local/UTC skew ------------


def test_memory_list_age_days_matches_utc_today_when_local_date_differs(
    project, alloy_home, fake_bd, memory_utc_skew,
):
    _configure_memories(fake_bd)

    result = _invoke_memory_list(project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    lesson = _index_by_key(json.loads(result.stdout))[LESSON_KEY]
    utc_expected = (UTC_TODAY - PROVENANCE_DATE).days
    local_expected = (LOCAL_TODAY - PROVENANCE_DATE).days

    assert UTC_TODAY != LOCAL_TODAY
    assert lesson["age_days"] == utc_expected
    assert lesson["age_days"] != local_expected


def test_memory_review_apply_stamps_last_review_with_utc_today_when_local_date_differs(
    project, alloy_home, fake_memory_review, memory_utc_skew,
):
    _configure_review_apply(fake_memory_review)

    result = _invoke_review_apply(project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    remembers = fake_memory_review.remember_by_key()
    assert LAST_REVIEW_KEY in remembers
    assert remembers[LAST_REVIEW_KEY] == UTC_TODAY.isoformat()
    assert remembers[LAST_REVIEW_KEY] != LOCAL_TODAY.isoformat()
