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


def test_readme_documents_project_memory():
    text = README.read_text(encoding="utf-8")
    for needle in _SHARED_MEMORY_DOC_STRINGS:
        assert needle in text


def test_agents_md_documents_project_memory():
    text = AGENTS_MD.read_text(encoding="utf-8")
    for needle in _SHARED_MEMORY_DOC_STRINGS:
        assert needle in text
    assert "--remember" in text
