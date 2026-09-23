"""Shared fixtures.

Two principles here: no test ever calls a real model, and the parts that are
cheap to run for real (git, sqlite, the `bd` CLI, subprocess execution) are run
for real, because those are exactly where the interesting bugs live.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

# Longer than the cancel+await budget; short enough to drain in test cleanup.
SIMULATED_CLOSE_STALL_S = 8.0

FAKE_SOURCE = Path(__file__).parent / "fakebin" / "_fake.py"
FAKE_BD_SOURCE = Path(__file__).parent / "fakebin" / "_fake_bd.py"
FAKE_RUNNERS = ("claude", "codex", "cursor-agent", "jev", "pi")

PASSING_TEST = '''
from mypkg import slugify


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"
'''

PASSING_TEST_GREEN = '''
def test_slugify_placeholder():
    assert True
'''

HELPER_TEST = '''
def test_helper_placeholder():
    assert True
'''

IMPLEMENTATION = '''
import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    """Keep `pytest -p no:xdist` working alongside the `-n auto` in addopts.

    Blocking the plugin also drops the `-n` option it defines, so the ini
    addopts would be rejected as unrecognized. Let xdist register its options
    anyway through its public hook; with the plugin blocked nothing reads
    them, so the run is serial exactly as requested.
    """
    if pluginmanager.hasplugin("xdist"):
        return
    try:
        from xdist.plugin import pytest_addoption as register_xdist_options
    except ImportError:  # xdist not installed: let pytest report the bad addopts
        return
    register_xdist_options(parser)


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
        """Simulate a harness that is not installed.

        The fake bindir is prepended to the real PATH, so unlinking the fake
        alone would expose a host-installed copy (and really run it). Drop
        every PATH entry that provides `name` too; the fixture's monkeypatch
        restores PATH at teardown.
        """
        (self.bindir / name).unlink(missing_ok=True)
        keep = [
            entry for entry in os.environ["PATH"].split(os.pathsep)
            if entry == str(self.bindir) or shutil.which(name, path=entry) is None
        ]
        os.environ["PATH"] = os.pathsep.join(keep)

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


@pytest.fixture
def fake_bd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: Path):
    """Put a stand-in ``bd`` first on PATH and return a control handle."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    binary = bindir / "bd"
    shutil.copy(FAKE_BD_SOURCE, binary)
    binary.chmod(0o755)

    workdir = tmp_path / "fake-bd-state"
    workdir.mkdir()
    config_path = workdir / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ALLOY_FAKE_DIR", str(workdir))
    monkeypatch.setenv("ALLOY_FAKE_CONFIG", str(config_path))

    return FakeBdHarness(
        bindir=bindir,
        binary=binary,
        workdir=workdir,
        config_path=config_path,
        repo=project,
    )


class FakeBdHarness:
    def __init__(
        self,
        bindir: Path,
        binary: Path,
        workdir: Path,
        config_path: Path,
        repo: Path,
    ) -> None:
        self.bindir = bindir
        self.binary = binary
        self.workdir = workdir
        self.config_path = config_path
        self.repo = repo

    def configure(self, config: dict) -> None:
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.workdir / "calls.jsonl").unlink(missing_ok=True)

    @property
    def calls(self) -> list[dict]:
        path = self.workdir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- scripted agent behaviour ------------------------------------------------


TEST_COMMAND = f"{sys.executable} -m pytest -q"


def context_entry(check_hints: list[str] | None = None) -> dict:
    return {
        "structured": {
            "summary": "A small python package with a pytest suite.",
            "relevant_files": ["mypkg/__init__.py"],
            "check_hints": [TEST_COMMAND] if check_hints is None else check_hints,
            "conventions": ["snake_case"],
            "risks": [],
        }
    }


def estimate_entry(
    complexity: str = "simple",
    reason: str = "single-file helper with obvious tests",
    confidence: float = 0.9,
) -> dict:
    return {
        "structured": {
            "complexity": complexity,
            "reason": reason,
            "confidence": confidence,
        }
    }


def write_tests_entry(
    *,
    baseline_checks: list[dict[str, str]] | None = None,
    passing: bool = False,
) -> dict:
    if baseline_checks is None:
        baseline_checks = [
            {
                "command": f"{sys.executable} -m pytest -q tests/test_slugify.py",
                "purpose": "Confirm slugify tests fail before implementation",
            }
        ]
    content = PASSING_TEST_GREEN if passing else PASSING_TEST
    return {
        "text": "Added tests/test_slugify.py covering the acceptance criteria.",
        "write": [{"path": "tests/test_slugify.py", "content": content}],
        "structured": {
            "summary": "Added tests/test_slugify.py for slugify acceptance criteria.",
            "baseline_checks": baseline_checks,
        },
    }


def write_tests_with_helper_entry(
    *,
    baseline_checks: list[dict[str, str]] | None = None,
    passing: bool = False,
) -> dict:
    """Tests role adds slugify tests plus an untouched helper file."""
    entry = write_tests_entry(baseline_checks=baseline_checks, passing=passing)
    entry["write"].append({"path": "tests/test_helper.py", "content": HELPER_TEST})
    return entry


def implement_entry(succeed: bool = True) -> dict:
    """A working implementation, or a deliberately broken one."""
    body = IMPLEMENTATION if succeed else "def slugify(text):\n    return None\n"
    return {
        "text": "Implemented slugify in mypkg/__init__.py.",
        "write": [{"path": "mypkg/__init__.py", "content": body}],
    }


def verifier_run_entry(
    command: str,
    *,
    kind: str = "targeted",
    purpose: str = "",
    required: bool = True,
) -> dict:
    return {
        "structured": {
            "action": "run",
            "command": command,
            "purpose": purpose or f"Run {kind} verification",
            "kind": kind,
            "required": required,
            "reason": "",
            "remaining_risks": [],
        }
    }


def acceptance_entry(
    decision: str,
    reason: str = "",
    confidence: float = 0.9,
) -> dict:
    return {
        "structured": {
            "decision": decision,
            "reason": reason or f"acceptance classified as {decision}",
            "confidence": confidence,
        }
    }


def implement_empty_diff_entry() -> dict:
    """Revert the worktree to match the base commit (no diff)."""
    return {
        "text": "Reverted all changes from the base commit.",
        "write": [{"path": "mypkg/__init__.py", "content": ""}],
        "delete": ["tests/test_slugify.py"],
    }


def implement_rewrite_tests_entry(*, succeed: bool = True) -> dict:
    """Implement slugify and rewrite the tests role's file (fingerprint change)."""
    body = IMPLEMENTATION if succeed else "def slugify(text):\n    return None\n"
    rewritten = PASSING_TEST.replace(
        'assert slugify("Hello World") == "hello-world"',
        'assert slugify("Hello World") == "hello-world"  # implementer fixed assertion',
    )
    return {
        "text": "Implemented slugify and adjusted the failing test assertion.",
        "write": [
            {"path": "mypkg/__init__.py", "content": body},
            {"path": "tests/test_slugify.py", "content": rewritten},
        ],
    }


def implement_rewrite_helper_test_entry(*, succeed: bool = True) -> dict:
    """Implement slugify and rewrite only the helper test the baseline does not run."""
    body = IMPLEMENTATION if succeed else "def slugify(text):\n    return None\n"
    rewritten = HELPER_TEST.replace(
        "assert True",
        "assert True  # implementer touched helper only",
    )
    return {
        "text": "Implemented slugify and adjusted the helper test file.",
        "write": [
            {"path": "mypkg/__init__.py", "content": body},
            {"path": "tests/test_helper.py", "content": rewritten},
        ],
    }


def implement_add_test_entry() -> dict:
    """Implement slugify and add a test file absent after prove_red."""
    return {
        "text": "Implemented slugify and added an extra regression test.",
        "write": [
            {"path": "mypkg/__init__.py", "content": IMPLEMENTATION},
            {
                "path": "tests/test_extra.py",
                "content": (
                    "from mypkg import slugify\n\n\n"
                    "def test_slugify_single_char():\n"
                    '    assert slugify("a") == "a"\n'
                ),
            },
        ],
    }


def verifier_stop_entry(
    reason: str = "verification evidence is sufficient",
    *,
    risks: tuple[str, ...] = (),
) -> dict:
    return {
        "structured": {
            "action": "stop",
            "command": "",
            "purpose": "",
            "kind": "custom",
            "required": True,
            "reason": reason,
            "remaining_risks": list(risks),
        }
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


def triage_entry(
    severity: str,
    reason: str = "",
    confidence: float = 0.9,
) -> dict:
    return {
        "structured": {
            "severity": severity,
            "reason": reason or f"classified as {severity}",
            "confidence": confidence,
        }
    }


def scope_entry(
    verdict: str,
    reason: str = "",
    confidence: float = 0.9,
) -> dict:
    return {
        "structured": {
            "verdict": verdict,
            "reason": reason or f"scope classified as {verdict}",
            "confidence": confidence,
        }
    }


def harvest_entry(
    *,
    scope: str = "repo",
    key: str = "lesson-x",
    lesson: str = "Always verify slugify with targeted tests before the full suite.",
    confidence: float = 0.9,
) -> dict:
    return {
        "structured": {
            "scope": scope,
            "key": key,
            "lesson": lesson,
            "confidence": confidence,
        }
    }


def memory_reviewer_entry(
    verdicts: list[dict[str, str]] | None = None,
    *,
    malformed: bool = False,
) -> dict:
    """Scripted memory_reviewer structured output for review-plan tests."""
    if malformed:
        return {"structured": {"unexpected": "not a review verdict list"}}
    return {
        "structured": {
            "verdicts": verdicts if verdicts is not None else [],
        }
    }


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


@pytest.fixture(scope="session")
def beads_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A `.beads` directory initialised once per session (per xdist worker).

    `bd init` plus `ensure_statuses()` costs ~2s; the resulting embedded Dolt
    store contains no absolute paths, so it can be copied into any fresh git
    repo in milliseconds. Returns the template's `.beads` directory.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd (Beads) is not installed")
    repo = tmp_path_factory.mktemp("beads-template") / "project"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "alloy@test"], repo)
    _git(["config", "user.name", "Alloy Test"], repo)
    _git(["commit", "-q", "--allow-empty", "-m", "initial"], repo)
    subprocess.run(["bd", "init", "--prefix", "t"], cwd=str(repo),
                   check=True, capture_output=True, text=True)
    from alloy.beads import BeadsClient

    BeadsClient(repo=repo).ensure_statuses()
    return repo / ".beads"


@pytest.fixture
def beads_project(project: Path, beads_template: Path) -> Path:
    """The project with a real Beads database initialized in it."""
    shutil.copytree(beads_template, project / ".beads", symlinks=True)
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


@pytest.fixture
def simulate_slow_sqlite_close_under_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make aiosqlite ``close()`` block when unwinding a cancelled task."""
    real_close = aiosqlite.Connection.close

    async def slow_close(self: aiosqlite.Connection) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # Shield mimics busy_timeout blocking that ignores cancellation.
            await asyncio.shield(asyncio.sleep(SIMULATED_CLOSE_STALL_S))
        await real_close(self)

    monkeypatch.setattr(aiosqlite.Connection, "close", slow_close)
