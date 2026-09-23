"""alloy-vrh.11: Auto-land documentation in AGENTS.md and alloy-manager skill.

Behaviour is not implemented yet — these must fail until operator-facing docs
describe landing (``alloy land``, recipe ``landing:`` blocks) and the
alloy-manager skill drops the manual-only merge claim.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
ALLOY_MANAGER_SKILL = (
    REPO_ROOT / ".claude" / "skills" / "alloy-manager" / "SKILL.md"
)


def test_agents_md_documents_alloy_land_command():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "alloy land" in text


def test_agents_md_documents_landing_recipe_block():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "landing:" in text


def test_alloy_manager_skill_documents_alloy_land_command():
    text = ALLOY_MANAGER_SKILL.read_text(encoding="utf-8")
    assert "alloy land" in text


def test_alloy_manager_skill_documents_landing_recipe_block():
    text = ALLOY_MANAGER_SKILL.read_text(encoding="utf-8")
    assert "landing:" in text


def test_alloy_manager_skill_no_longer_claims_nothing_merges_automatically():
    text = ALLOY_MANAGER_SKILL.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "nothing merges or cleans up automatically" not in normalized
