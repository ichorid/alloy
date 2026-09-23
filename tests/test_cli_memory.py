"""alloy-4ef.15: ``alloy memory list`` CLI — tests only.

Encode acceptance criteria for listing project memories with owner, provenance,
age, and flags derived from alloy:review:contradiction:* and alloy:meta:embed.
Expected to fail until the memory typer group lands.
"""

from __future__ import annotations

import json
from datetime import date

from typer.testing import CliRunner

from alloy.cli import app
from alloy.models import with_provenance

HUMAN_KEY = "conv"
HUMAN_VALUE = "repo uses pathlib"
LESSON_KEY = "alloy:lesson:snapshot"
LESSON_BODY = "Read project memory once at run start"
RUN_ID = "run-abc123"
BEAD_ID = "alloy-4ef.8"
PROVENANCE_DATE = date(2026, 9, 20)
CONTRADICTION_KEY = f"alloy:review:contradiction:{HUMAN_KEY}"
EMBED_KEY = "alloy:meta:embed"


def _invoke(*args: str, project, alloy_home):
    runner = CliRunner()
    return runner.invoke(
        app,
        [
            *args,
            "--repo",
            str(project),
            "--root",
            str(alloy_home),
        ],
    )


def _sample_memories() -> dict[str, str]:
    return {
        HUMAN_KEY: HUMAN_VALUE,
        LESSON_KEY: with_provenance(LESSON_BODY, RUN_ID, BEAD_ID, PROVENANCE_DATE),
        CONTRADICTION_KEY: "repo uses os.path instead",
        EMBED_KEY: LESSON_KEY,
    }


def _configure_memories(fake_bd, memories: dict[str, str] | None = None) -> None:
    fake_bd.configure({"memories": memories if memories is not None else _sample_memories()})


def _index_by_key(payload: list[dict]) -> dict[str, dict]:
    return {row["key"]: row for row in payload}


# -- alloy memory list --json ------------------------------------------------


def test_memory_list_json_emits_one_object_per_non_meta_key_with_fields(
    project, alloy_home, fake_bd,
):
    _configure_memories(fake_bd)

    result = _invoke("memory", "list", "--json", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 2

    by_key = _index_by_key(payload)
    assert set(by_key) == {HUMAN_KEY, LESSON_KEY}

    for row in payload:
        assert set(row) == {
            "key",
            "owner",
            "run_id",
            "bead_id",
            "date",
            "age_days",
            "flags",
        }
        assert row["owner"] in {"alloy", "human"}
        assert isinstance(row["flags"], list)


def test_memory_list_json_human_key_has_contradiction_flag(
    project, alloy_home, fake_bd,
):
    _configure_memories(fake_bd)

    result = _invoke("memory", "list", "--json", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    human = _index_by_key(json.loads(result.stdout))[HUMAN_KEY]

    assert human["owner"] == "human"
    assert human["run_id"] is None
    assert human["bead_id"] is None
    assert human["date"] is None
    assert human["age_days"] is None
    assert human["flags"] == ["contradiction"]


def test_memory_list_json_lesson_has_provenance_embedded_flag_and_age(
    project, alloy_home, fake_bd,
):
    _configure_memories(fake_bd)

    result = _invoke("memory", "list", "--json", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    lesson = _index_by_key(json.loads(result.stdout))[LESSON_KEY]

    assert lesson["owner"] == "alloy"
    assert lesson["run_id"] == RUN_ID
    assert lesson["bead_id"] == BEAD_ID
    assert lesson["date"] == PROVENANCE_DATE.isoformat()
    assert lesson["age_days"] == (date.today() - PROVENANCE_DATE).days
    assert lesson["flags"] == ["embedded"]


# -- alloy memory list (table) -----------------------------------------------


def test_memory_list_table_contains_every_non_meta_key(
    project, alloy_home, fake_bd,
):
    _configure_memories(fake_bd)

    result = _invoke("memory", "list", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    output = result.stdout
    assert HUMAN_KEY in output
    assert LESSON_KEY in output
    assert CONTRADICTION_KEY not in output
    assert EMBED_KEY not in output
