"""alloy-4ef.16: ``alloy memory review`` CLI — tests only.

Encode acceptance criteria for a read-only review plan: deterministic hygiene
(expired alloy-owned keys, orphan contradiction flags, newer duplicate bodies)
merged with memory_reviewer verdicts. Expected to fail until the command lands.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy.cli import app
from alloy.models import CONTRADICTION_KEY_PREFIX, EMBED_KEY as META_EMBED_KEY, PROPOSAL_KEY_PREFIX, with_provenance
from conftest import FAKE_BD_SOURCE, FAKE_RUNNERS, FAKE_SOURCE, memory_reviewer_entry

RUN_ID = "run-review-1"
BEAD_ID = "alloy-4ef.16"

STALE_LESSON_KEY = "alloy:lesson:stale"
STALE_DATE = date.today() - timedelta(days=100)

ORPHAN_CONTRADICTION_KEY = f"{CONTRADICTION_KEY_PREFIX}gone"

DUP_OLD_KEY = "alloy:lesson:dup-a"
DUP_NEW_KEY = "alloy:lesson:dup-b"
DUP_BODY = "identical lesson body"
DUP_OLD_DATE = date.today() - timedelta(days=30)
DUP_NEW_DATE = date.today() - timedelta(days=10)

KEEP_KEY = "conv"
KEEP_VALUE = "repo uses pathlib"

UPDATE_KEY = "alloy:lesson:fresh"
UPDATE_DATE = date.today() - timedelta(days=5)
UPDATE_BODY = "verify slugify before the full suite"
UPDATE_REASON = "refined wording"
UPDATE_CONTENT = "always verify slugify with targeted tests first"

EMBED_KEY = "style"
EMBED_VALUE = "prefer snake_case names"
EMBED_REASON = "belongs in AGENTS.md"

UNKNOWN_REVIEW_KEY = "ghost-key"


def _review_memories() -> dict[str, str]:
    return {
        KEEP_KEY: KEEP_VALUE,
        STALE_LESSON_KEY: with_provenance(
            "snapshot memory once per run",
            RUN_ID,
            BEAD_ID,
            STALE_DATE,
        ),
        ORPHAN_CONTRADICTION_KEY: "subject key was removed",
        DUP_OLD_KEY: with_provenance(DUP_BODY, RUN_ID, BEAD_ID, DUP_OLD_DATE),
        DUP_NEW_KEY: with_provenance(DUP_BODY, RUN_ID, BEAD_ID, DUP_NEW_DATE),
        UPDATE_KEY: with_provenance(UPDATE_BODY, RUN_ID, BEAD_ID, UPDATE_DATE),
        EMBED_KEY: EMBED_VALUE,
    }


def _reviewer_verdicts() -> list[dict[str, str]]:
    return [
        {"action": "keep", "key": KEEP_KEY, "reason": "still accurate"},
        {
            "action": "update",
            "key": UPDATE_KEY,
            "reason": UPDATE_REASON,
            "new_content": UPDATE_CONTENT,
        },
        {"action": "embed", "key": EMBED_KEY, "reason": EMBED_REASON},
        {"action": "forget", "key": UNKNOWN_REVIEW_KEY, "reason": "unknown key"},
    ]


class FakeMemoryReviewHarness:
    """fake_bd and fake harness runners sharing one bindir and config file."""

    def __init__(self, bindir: Path, workdir: Path, config_path: Path) -> None:
        self.bindir = bindir
        self.workdir = workdir
        self.config_path = config_path

    def configure(self, config: dict) -> None:
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.workdir / "calls.jsonl").unlink(missing_ok=True)
        (self.workdir / "counters.json").unlink(missing_ok=True)

    @property
    def calls(self) -> list[dict]:
        path = self.workdir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def bd_writes(self) -> list[dict]:
        return [
            call
            for call in self.calls
            if call.get("command") in {"remember", "forget"}
        ]

    def bd_creates(self) -> list[dict]:
        return [call for call in self.calls if call.get("command") == "create"]

    def forget_keys(self) -> list[str]:
        keys: list[str] = []
        for call in self.calls:
            if call.get("command") != "forget":
                continue
            argv = call.get("argv") or []
            if len(argv) >= 2:
                keys.append(argv[1])
        return keys

    def remember_by_key(self) -> dict[str, str]:
        remembered: dict[str, str] = {}
        for call in self.calls:
            if call.get("command") != "remember":
                continue
            argv = call.get("argv") or []
            key: str | None = None
            content_parts: list[str] = []
            index = 1
            while index < len(argv):
                arg = argv[index]
                if arg == "--key" and index + 1 < len(argv):
                    key = argv[index + 1]
                    index += 2
                    continue
                if not arg.startswith("-"):
                    content_parts.append(arg)
                index += 1
            if key is not None:
                remembered[key] = " ".join(content_parts)
        return remembered

    def reviewer_calls(self) -> list[dict]:
        return [call for call in self.calls if call.get("role") == "memory_reviewer"]


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


def _invoke_review(project: Path, alloy_home: Path):
    runner = CliRunner()
    return runner.invoke(
        app,
        [
            "memory",
            "review",
            "--json",
            "--repo",
            str(project),
            "--root",
            str(alloy_home),
        ],
    )


def _invoke_review_apply(project: Path, alloy_home: Path):
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


def _plan_items(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        return payload
    return payload["items"]


def _index_plan_by_key(items: list[dict]) -> dict[str, dict]:
    return {item["key"]: item for item in items}


def _configure_review(
    harness: FakeMemoryReviewHarness,
    *,
    memories: dict[str, str] | None = None,
    reviewer_verdicts: list[dict[str, str]] | None = None,
    malformed_reviewer: bool = False,
) -> None:
    harness.configure(
        {
            "memories": memories if memories is not None else _review_memories(),
            "memory_reviewer": memory_reviewer_entry(
                reviewer_verdicts,
                malformed=malformed_reviewer,
            ),
        }
    )


# -- alloy memory review --json ------------------------------------------------


def test_memory_review_json_emits_hygiene_and_reviewer_plan_items(
    project, alloy_home, fake_memory_review,
):
    _configure_review(
        fake_memory_review,
        reviewer_verdicts=_reviewer_verdicts(),
    )

    result = _invoke_review(project, alloy_home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    items = _plan_items(payload)
    by_key = _index_plan_by_key(items)

    for item in items:
        assert set(item) >= {"key", "action", "reason", "source"}
        assert item["source"] in {"hygiene", "reviewer"}

    stale = by_key[STALE_LESSON_KEY]
    assert stale["action"] == "forget"
    assert stale["source"] == "hygiene"
    assert stale["reason"]

    orphan = by_key[ORPHAN_CONTRADICTION_KEY]
    assert orphan["action"] == "forget"
    assert orphan["source"] == "hygiene"
    assert orphan["reason"]

    newer_dup = by_key[DUP_NEW_KEY]
    assert newer_dup["action"] == "forget"
    assert newer_dup["source"] == "hygiene"
    assert newer_dup["reason"]
    assert DUP_OLD_KEY not in by_key

    keep = by_key[KEEP_KEY]
    assert keep["action"] == "keep"
    assert keep["source"] == "reviewer"
    assert keep["reason"] == "still accurate"

    update = by_key[UPDATE_KEY]
    assert update["action"] == "update"
    assert update["source"] == "reviewer"
    assert update["reason"] == UPDATE_REASON
    assert update.get("new_content") == UPDATE_CONTENT

    embed = by_key[EMBED_KEY]
    assert embed["action"] == "embed"
    assert embed["source"] == "reviewer"
    assert embed["reason"] == EMBED_REASON

    assert UNKNOWN_REVIEW_KEY not in by_key


def test_memory_review_malformed_reviewer_emits_hygiene_only(
    project, alloy_home, fake_memory_review,
):
    _configure_review(fake_memory_review, malformed_reviewer=True)

    result = _invoke_review(project, alloy_home)

    assert result.exit_code == 0
    by_key = _index_plan_by_key(_plan_items(json.loads(result.stdout)))

    assert by_key[STALE_LESSON_KEY]["source"] == "hygiene"
    assert by_key[ORPHAN_CONTRADICTION_KEY]["source"] == "hygiene"
    assert by_key[DUP_NEW_KEY]["source"] == "hygiene"

    assert KEEP_KEY not in by_key
    assert UPDATE_KEY not in by_key
    assert EMBED_KEY not in by_key
    assert all(item["source"] == "hygiene" for item in by_key.values())


def test_memory_review_does_not_write_to_bd(
    project, alloy_home, fake_memory_review,
):
    _configure_review(
        fake_memory_review,
        reviewer_verdicts=_reviewer_verdicts(),
    )

    result = _invoke_review(project, alloy_home)

    assert result.exit_code == 0
    assert fake_memory_review.bd_writes() == []
    assert len(fake_memory_review.reviewer_calls()) == 1


# -- alloy memory review --apply (alloy-4ef.17) --------------------------------

APPLY_ALLOY_FORGET_KEY = "alloy:lesson:a"
APPLY_HUMAN_FORGET_KEY = "human-k"
APPLY_ALLOY_EMBED_KEY = "alloy:lesson:b"
APPLY_META_EMBED_KEY = "alloy:meta:x"
APPLY_RECENT_DATE = date.today() - timedelta(days=5)
LAST_REVIEW_KEY = "alloy:meta:last-review"
MEMORY_REVIEW_LABEL = "alloy-memory-review"


def _apply_memories() -> dict[str, str]:
    return {
        APPLY_ALLOY_FORGET_KEY: with_provenance(
            "lesson a body",
            RUN_ID,
            BEAD_ID,
            APPLY_RECENT_DATE,
        ),
        APPLY_HUMAN_FORGET_KEY: "human memory to forget",
        APPLY_ALLOY_EMBED_KEY: with_provenance(
            "lesson b body",
            RUN_ID,
            BEAD_ID,
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


def _create_labels(argv: list[str]) -> list[str]:
    if "--labels" not in argv:
        return []
    index = argv.index("--labels") + 1
    if index >= len(argv):
        return []
    return [label for label in argv[index].split(",") if label]


def test_memory_review_apply_executes_alloy_forget_proposals_human_and_writes_meta(
    project, alloy_home, fake_memory_review,
):
    _configure_review(
        fake_memory_review,
        memories=_apply_memories(),
        reviewer_verdicts=_apply_verdicts(),
    )

    result = _invoke_review_apply(project, alloy_home)

    assert result.exit_code == 0

    forgets = fake_memory_review.forget_keys()
    assert forgets.count(APPLY_ALLOY_FORGET_KEY) == 1
    assert APPLY_HUMAN_FORGET_KEY not in forgets

    remembers = fake_memory_review.remember_by_key()
    proposal_key = f"{PROPOSAL_KEY_PREFIX}{APPLY_HUMAN_FORGET_KEY}"
    assert proposal_key in remembers
    proposal = json.loads(remembers[proposal_key])
    assert proposal == {
        "action": "forget",
        "reason": "human should propose",
    }

    creates = fake_memory_review.bd_creates()
    assert len(creates) == 1
    create_argv = creates[0]["argv"]
    assert MEMORY_REVIEW_LABEL in _create_labels(create_argv)
    type_flag = "--type" if "--type" in create_argv else "-t"
    type_index = create_argv.index(type_flag) + 1
    assert create_argv[type_index] == "task"

    assert META_EMBED_KEY in remembers
    assert json.loads(remembers[META_EMBED_KEY]) == [APPLY_ALLOY_EMBED_KEY]

    assert LAST_REVIEW_KEY in remembers
    assert remembers[LAST_REVIEW_KEY] == date.today().isoformat()


def test_memory_review_apply_second_run_reuses_open_review_bead(
    project, alloy_home, fake_memory_review,
):
    _configure_review(
        fake_memory_review,
        memories=_apply_memories(),
        reviewer_verdicts=_apply_verdicts(),
    )

    first = _invoke_review_apply(project, alloy_home)
    assert first.exit_code == 0
    assert len(fake_memory_review.bd_creates()) == 1

    second = _invoke_review_apply(project, alloy_home)
    assert second.exit_code == 0
    assert len(fake_memory_review.bd_creates()) == 1
