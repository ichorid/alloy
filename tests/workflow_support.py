"""Shared scripted responses for workflow integration tests."""

import json
import sys
from pathlib import Path

from conftest import (
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
)


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


TARGETED_SLUGIFY = f"{sys.executable} -m pytest -q tests/test_slugify.py"

FULL_SUITE = f"{sys.executable} -m pytest -q"


def verification_script(**overrides):
    """Happy-path verifier scripting for the dynamic verification loop."""
    base = script(
        verifier=[
            verifier_run_entry(FULL_SUITE, kind="regression"),
            verifier_stop_entry("regression suite green"),
        ],
    )
    base.update(overrides)
    return base


CHECK_HINTS_KEY = "alloy:check-hints"

TARGETED_PYTEST = "pytest -q tests/test_slugify.py"

REGRESSION_PYTEST = "pytest -q"

AUTODETECT_PYTEST = "python -m pytest -q"

HINTS_HEADING = "## Hints from the repository (not yet verified)"


class FakeWorkflow:
    """Fake harness runners and fake bd sharing one bindir and config file."""

    def __init__(self, bindir: Path, workdir: Path, config_path: Path) -> None:
        self.bindir = bindir
        self.workdir = workdir
        self.config_path = config_path

    @property
    def bd(self) -> Path:
        return self.bindir / "bd"

    def configure(
        self,
        agent_script: dict,
        *,
        memories: dict[str, str] | None = None,
    ) -> None:
        config = dict(agent_script)
        if memories is not None:
            config["memories"] = memories
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.workdir / "calls.jsonl").unlink(missing_ok=True)
        (self.workdir / "counters.json").unlink(missing_ok=True)

    @property
    def calls(self) -> list[dict]:
        path = self.workdir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def calls_for(self, role: str) -> list[dict]:
        return [call for call in self.calls if call.get("role") == role]
