"""alloy-21u.7: remove the fixed-command verification path (tests only).

These tests encode the acceptance criteria for replacing ContextPacket.test_command,
resolve_command, and related legacy symbols with check_hints and verifier-driven
checks. They are expected to fail until the implementation lands.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from conftest import (
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
)
from support import make_harness

from alloy.models import ContextPacket
from alloy.recipes.tdd_loop import verifier_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ALLOY = REPO_ROOT / "src" / "alloy"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
README_MD = REPO_ROOT / "README.md"
PRODUCT_DESIGNER_SKILLS = (
    REPO_ROOT / ".claude" / "skills" / "alloy-product-designer" / "SKILL.md",
    REPO_ROOT / ".cursor" / "skills" / "alloy-product-designer" / "SKILL.md",
)

FULL_SUITE = f"{sys.executable} -m pytest -q"
HINTS_SECTION = "## Hints from the repository (not yet verified)"
REMOVED_SYMBOLS = re.compile(r"test_command|resolve_command|TestReport|VerifySpec")


def _context_without_hints() -> dict:
    return {
        "structured": {
            "summary": "A Rust crate with a Cargo.toml at the repo root.",
            "relevant_files": ["Cargo.toml", "src/main.rs"],
            "conventions": ["cargo for build and test"],
            "risks": [],
        }
    }


def _verification_script(**overrides):
    base = {
        "context": _context_without_hints(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "verifier": [
            verifier_run_entry(FULL_SUITE, kind="regression"),
            verifier_stop_entry("regression suite green"),
        ],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


def _verifier_prompt_kwargs(**overrides):
    defaults = {
        "brief": "Implement feature X",
        "acceptance": "feature X works",
        "context": {"summary": "rust crate", "check_hints": ["cargo test"]},
        "diff": "",
        "changed_files": ["src/main.rs"],
        "checks_this_run": [],
        "iteration": 1,
        "checks_left_iteration": 5,
        "checks_left_run": 10,
        "history": [],
        "baseline_checks": [],
        "instructions": "",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# src/alloy must not reference removed symbols (alloy-21u.7)
# ---------------------------------------------------------------------------


def test_no_module_references_test_command():
    """grep src/alloy for removed verification symbols; expect zero hits."""
    offenders: list[str] = []
    for path in sorted(SRC_ALLOY.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if REMOVED_SYMBOLS.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}:{line}")
    assert offenders == []


# ---------------------------------------------------------------------------
# ContextPacket.check_hints
# ---------------------------------------------------------------------------


def test_context_packet_compact_includes_check_hints():
    packet = ContextPacket(check_hints=["make test"])
    assert packet.compact()["check_hints"] == ["make test"]


def test_verifier_prompt_lists_repository_hints_section():
    prompt = verifier_prompt(**_verifier_prompt_kwargs())
    assert HINTS_SECTION in prompt
    hints_body = prompt.split(HINTS_SECTION, 1)[1]
    assert "cargo test" in hints_body


# ---------------------------------------------------------------------------
# Autodetected hints reach the verifier when context returns none
# ---------------------------------------------------------------------------


async def test_verifier_prompt_includes_autodetected_cargo_hint_when_context_has_none(
    project,
    alloy_home,
    fake_harnesses,
):
    (project / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    fake_harnesses.configure(_verification_script())
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    verifier_calls = fake_harnesses.calls_for("verifier")
    assert verifier_calls, "expected at least one verifier call"
    prompt = verifier_calls[0]["prompt"]
    assert HINTS_SECTION in prompt
    hints_body = prompt.split(HINTS_SECTION, 1)[1]
    assert "cargo test" in hints_body


# ---------------------------------------------------------------------------
# Operator-facing docs describe check-based verification
# ---------------------------------------------------------------------------


def test_agents_md_documents_check_based_verification():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "verifier" in text
    assert re.search(r"checks", text, re.IGNORECASE)
    assert "one test command" not in text
    assert 'alloy_test_cmd="' not in text


def test_readme_documents_check_based_verification():
    text = README_MD.read_text(encoding="utf-8")
    assert re.search(r"checks", text, re.IGNORECASE)
    assert "one test command" not in text


@pytest.mark.parametrize("skill_path", PRODUCT_DESIGNER_SKILLS, ids=["claude", "cursor"])
def test_product_designer_skill_no_longer_pins_test_command(skill_path: Path):
    text = skill_path.read_text(encoding="utf-8")
    assert "Pin the test command" not in text
