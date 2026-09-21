"""Shared fixtures.

Two principles here: no test ever calls a real model, and the parts that are
cheap to run for real (git, sqlite, the `bd` CLI, subprocess execution) are run
for real, because those are exactly where the interesting bugs live.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_SOURCE = Path(__file__).parent / "fakebin" / "_fake.py"
FAKE_RUNNERS = ("claude", "codex", "cursor-agent", "pi")

PASSING_TEST = '''
from mypkg import slugify


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"
'''

IMPLEMENTATION = '''
import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small, real git repository with a real (failing) pytest suite."""
    repo = tmp_path / "project"
    (repo / "mypkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / "README.md").write_text("# project\n", encoding="utf-8")

    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "alloy@test"], repo)
    _git(["config", "user.name", "Alloy Test"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "initial"], repo)
    return repo


@pytest.fixture
def alloy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "alloy-home"
    home.mkdir()
    monkeypatch.setenv("ALLOY_HOME", str(home))
    return home


@pytest.fixture
def fake_harnesses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Put stand-in harness CLIs first on PATH and return a control handle."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name in FAKE_RUNNERS:
        target = bindir / name
        shutil.copy(FAKE_SOURCE, target)
        target.chmod(0o755)

    workdir = tmp_path / "fake-state"
    workdir.mkdir()
    config_path = workdir / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ALLOY_FAKE_DIR", str(workdir))
    monkeypatch.setenv("ALLOY_FAKE_CONFIG", str(config_path))

    return FakeHarnesses(bindir=bindir, workdir=workdir, config_path=config_path)


class FakeHarnesses:
    def __init__(self, bindir: Path, workdir: Path, config_path: Path) -> None:
        self.bindir = bindir
        self.workdir = workdir
        self.config_path = config_path

    def configure(self, config: dict) -> None:
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def remove(self, name: str) -> None:
        """Simulate a harness that is not installed."""
        (self.bindir / name).unlink(missing_ok=True)

    @property
    def calls(self) -> list[dict]:
        path = self.workdir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def calls_for(self, role: str) -> list[dict]:
        return [call for call in self.calls if call["role"] == role]

    def reset_calls(self) -> None:
        (self.workdir / "calls.jsonl").unlink(missing_ok=True)
        (self.workdir / "counters.json").unlink(missing_ok=True)


# -- scripted agent behaviour ------------------------------------------------


TEST_COMMAND = f"{sys.executable} -m pytest -q"


def context_entry(test_command: str = TEST_COMMAND) -> dict:
    return {
        "structured": {
            "summary": "A small python package with a pytest suite.",
            "relevant_files": ["mypkg/__init__.py"],
            "test_command": test_command,
            "conventions": ["snake_case"],
            "risks": [],
        }
    }


def write_tests_entry() -> dict:
    return {
        "text": "Added tests/test_slugify.py covering the acceptance criteria.",
        "write": [{"path": "tests/test_slugify.py", "content": PASSING_TEST}],
    }


def implement_entry(succeed: bool = True) -> dict:
    """A working implementation, or a deliberately broken one."""
    body = IMPLEMENTATION if succeed else "def slugify(text):\n    return None\n"
    return {
        "text": "Implemented slugify in mypkg/__init__.py.",
        "write": [{"path": "mypkg/__init__.py", "content": body}],
    }


def judge_entry(decision: str, reason: str = "", instructions: str = "") -> dict:
    return {
        "structured": {
            "decision": decision,
            "reason": reason or f"scripted {decision}",
            "next_instructions": instructions,
            "confidence": 0.9,
        }
    }


def critic_entry(root_cause: str = "slugify returns None") -> dict:
    return {
        "structured": {
            "root_cause": root_cause,
            "evidence": "test output shows None",
            "suggested_fix": "return the slug string",
            "confidence": 0.8,
        }
    }


def synthesize_entry(text: str = "Return the slug string from slugify.") -> dict:
    return {"text": text}


@pytest.fixture
def happy_path_script() -> dict:
    """Context, tests, one implementation, judge says done."""
    return {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done", "tests pass and the diff matches the criteria")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }


@pytest.fixture
def python_bin() -> str:
    return sys.executable


@pytest.fixture
def beads_project(project: Path) -> Path:
    """The project with a real Beads database initialized in it."""
    if shutil.which("bd") is None:
        pytest.skip("bd (Beads) is not installed")
    subprocess.run(["bd", "init", "--prefix", "t"], cwd=str(project),
                   check=True, capture_output=True, text=True)
    from alloy.beads import BeadsClient

    client = BeadsClient(repo=project)
    client.ensure_statuses()
    return project


def bd_create(repo: Path, title: str, *, priority: int = 2, **metadata) -> str:
    """Create a bead and return its id."""
    proc = subprocess.run(["bd", "q", title], cwd=str(repo),
                          check=True, capture_output=True, text=True)
    bead_id = proc.stdout.strip().splitlines()[-1].strip()
    args = ["bd", "update", bead_id, "-p", str(priority)]
    for key, value in metadata.items():
        args += ["--set-metadata", f"{key}={value}"]
    subprocess.run(args, cwd=str(repo), check=True, capture_output=True, text=True)
    return bead_id
