"""ProjectMemory model (alloy-4ef.3): namespace, provenance, caps, deterministic render."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from alloy.config import MemorySpec
from alloy.models import ProjectMemory, with_provenance

# Render contract pinned by these tests (stable for downstream golden prompts).
_RENDER_HEADER = "## Project memory"


def _spec(**overrides) -> MemorySpec:
    return replace(MemorySpec(), **overrides)


def _expected_render(entries: dict[str, str]) -> str:
    if not entries:
        return ""
    lines = [_RENDER_HEADER, ""]
    for key in sorted(entries):
        lines.append(f"### {key}")
        lines.append(entries[key])
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def _rendered_keys(rendered: str) -> list[str]:
    return [line.removeprefix("### ") for line in rendered.splitlines() if line.startswith("### ")]


# ---------------------------------------------------------------------------
# Deterministic render
# ---------------------------------------------------------------------------


def test_render_is_byte_identical_regardless_of_dict_insertion_order():
    memories = {
        "human-b": "beta",
        "alloy:lesson:z": "lesson z",
        "human-a": "alpha",
    }
    reversed_memories = dict(reversed(list(memories.items())))
    spec = MemorySpec()

    first = ProjectMemory.from_raw(memories, spec).render()
    second = ProjectMemory.from_raw(reversed_memories, spec).render()

    assert first == second
    assert first == _expected_render(
        {
            "human-a": "alpha",
            "human-b": "beta",
            "alloy:lesson:z": "lesson z",
        }
    )


# ---------------------------------------------------------------------------
# Render exclusions
# ---------------------------------------------------------------------------


def test_render_excludes_meta_review_and_calibration_keys():
    memories = {
        "alloy:meta:private": "meta body",
        "alloy:review:stale": "review body",
        "alloy:calibration": "calibration body",
        "human:visible": "shown",
    }

    rendered = ProjectMemory.from_raw(memories, MemorySpec()).render()

    assert "alloy:meta" not in rendered
    assert "alloy:review" not in rendered
    assert "alloy:calibration" not in rendered
    assert "meta body" not in rendered
    assert "review body" not in rendered
    assert "calibration body" not in rendered
    assert rendered == _expected_render({"human:visible": "shown"})


# ---------------------------------------------------------------------------
# Owner classification
# ---------------------------------------------------------------------------


def test_classifies_alloy_prefix_as_alloy_owned_and_rest_as_human():
    memory = ProjectMemory.from_raw(
        {"human-note": "h", "alloy:fact": "a"},
        MemorySpec(),
    )

    assert memory.entries["human-note"].owner == "human"
    assert memory.entries["alloy:fact"].owner == "alloy"


# ---------------------------------------------------------------------------
# Provenance parse / strip
# ---------------------------------------------------------------------------


def test_alloy_owned_entry_parses_provenance_fields_and_render_omits_trailer():
    raw = "text [alloy run=r1 bead=b1 at=2026-09-22]"
    memory = ProjectMemory.from_raw({"alloy:lesson:test": raw}, MemorySpec())
    entry = memory.entries["alloy:lesson:test"]

    assert entry.body == "text"
    assert entry.run_id == "r1"
    assert entry.bead_id == "b1"
    assert entry.date == date(2026, 9, 22)

    rendered = memory.render()
    assert "text" in rendered
    assert "run=r1" not in rendered
    assert "bead=b1" not in rendered
    assert "2026-09-22" not in rendered


def test_with_provenance_round_trips_through_parser():
    stamped = with_provenance("text", "r1", "b1", date(2026, 9, 22))
    assert stamped == "text [alloy run=r1 bead=b1 at=2026-09-22]"

    entry = ProjectMemory.from_raw({"alloy:harvest:x": stamped}, MemorySpec()).entries["alloy:harvest:x"]
    assert entry.body == "text"
    assert entry.run_id == "r1"
    assert entry.bead_id == "b1"
    assert entry.date == date(2026, 9, 22)


def test_human_owned_content_does_not_parse_provenance_trailer():
    raw = "notes [alloy run=r1 bead=b1 at=2026-09-22]"
    entry = ProjectMemory.from_raw({"operator-note": raw}, MemorySpec()).entries["operator-note"]

    assert entry.body == raw
    assert entry.run_id is None
    assert entry.bead_id is None
    assert entry.date is None


def test_bracket_text_not_at_end_is_not_treated_as_provenance():
    raw = "see [alloy run=old bead=old at=2020-01-01] for history [alloy run=r1 bead=b1 at=2026-09-22]"
    entry = ProjectMemory.from_raw({"alloy:lesson:refs": raw}, MemorySpec()).entries["alloy:lesson:refs"]

    assert entry.body == "see [alloy run=old bead=old at=2020-01-01] for history"
    assert entry.run_id == "r1"
    assert entry.bead_id == "b1"
    assert entry.date == date(2026, 9, 22)


# ---------------------------------------------------------------------------
# Caps: max_items priority
# ---------------------------------------------------------------------------


def test_max_items_keeps_two_human_entries_sorted_by_key():
    memories = {"human-c": "c", "human-a": "a", "human-b": "b"}
    rendered = ProjectMemory.from_raw(memories, _spec(max_items=2)).render()

    assert _rendered_keys(rendered) == ["human-a", "human-b"]
    assert rendered == _expected_render({"human-a": "a", "human-b": "b"})


def test_max_items_prefers_human_then_lesson_then_other_alloy():
    memories = {
        "alloy:other:low": "other",
        "alloy:lesson:mid": "lesson",
        "human:high": "human",
        "alloy:meta:hidden": "meta",
    }

    rendered = ProjectMemory.from_raw(memories, _spec(max_items=2)).render()

    # Cap picks human then lesson; the rendered block itself is sorted by key.
    assert _rendered_keys(rendered) == ["alloy:lesson:mid", "human:high"]
    assert "alloy:other:low" not in rendered


def test_max_items_includes_all_groups_when_budget_allows():
    memories = {
        "alloy:other:z": "other z",
        "alloy:lesson:a": "lesson a",
        "human:z": "human z",
    }

    rendered = ProjectMemory.from_raw(memories, _spec(max_items=3)).render()

    assert _rendered_keys(rendered) == ["alloy:lesson:a", "alloy:other:z", "human:z"]


# ---------------------------------------------------------------------------
# Caps: max_chars
# ---------------------------------------------------------------------------


def test_max_chars_drops_lowest_priority_entries_first():
    memories = {
        "human:keep": "hi",
        "alloy:lesson:keep": "ok",
        "alloy:fact:drop": "this lower-priority alloy entry is dropped first",
    }
    # Tight budget: header + one short human entry fits; lesson/alloy extras do not.
    rendered = ProjectMemory.from_raw(memories, _spec(max_items=10, max_chars=45)).render()

    assert "human:keep" in rendered
    assert "alloy:fact:drop" not in rendered
    assert len(rendered) <= 45


def test_max_chars_empty_render_when_nothing_fits():
    memories = {"human:only": "x" * 200}
    rendered = ProjectMemory.from_raw(memories, _spec(max_items=10, max_chars=20)).render()

    assert rendered == ""
