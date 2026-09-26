"""alloy-4ef.21: Project memory documentation in README.md and AGENTS.md.

Behaviour is not implemented yet — these must fail until both operator-facing
docs document memory commands, the alloy: namespace, managed-block markers,
prefix_hash in logs, and AGENTS.md covers ``alloy resume --remember``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

_SHARED_MEMORY_DOC_STRINGS = (
    "alloy memory review",
    "alloy memory embed",
    "alloy:memory:begin",
    "alloy:check-hints",
    "prefix_hash",
)


def test_agents_has_one_beads_block_and_intact_memory_markers():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert text.count("<!-- BEGIN BEADS INTEGRATION") == 1
    assert text.count("<!-- END BEADS INTEGRATION -->") == 1
    assert "<!-- BEGIN BEADS CODEX SETUP" not in text
    assert text.count("<!-- alloy:memory:begin -->") == 1
    assert text.count("<!-- alloy:memory:end -->") == 1
    assert text.index("<!-- alloy:memory:begin -->") < text.index("<!-- alloy:memory:end -->")


def test_readme_explains_beads_setup_and_verification_commands():
    text = README.read_text(encoding="utf-8")
    assert "bd setup codex" in text
    assert "uv run pytest -n 0 -q <test-file>" in text
    assert "uv run pytest -n 8 -q" in text


def test_readme_documents_project_memory():
    text = README.read_text(encoding="utf-8")
    for needle in _SHARED_MEMORY_DOC_STRINGS:
        assert needle in text


def test_agents_md_documents_project_memory():
    text = AGENTS_MD.read_text(encoding="utf-8")
    for needle in _SHARED_MEMORY_DOC_STRINGS:
        assert needle in text
    assert "--remember" in text
