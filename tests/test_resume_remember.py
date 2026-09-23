"""alloy-4ef.11: alloy resume --remember stores human guidance for harvest.

Tests encode the CLI remember path and harvest prompt acceptance criteria.
They are expected to fail until the implementation lands.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy.beads import BeadsClient
from alloy.cli import app
from alloy.engine import Engine, RunResult
from alloy.models import parse_provenance
from support import load_config
from conftest import (
    FAKE_BD_SOURCE,
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)


@pytest.fixture
def engine(beads_project, alloy_home, monkeypatch):
    opened = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(opened, "load_config", lambda name: load_config())
    return opened


@pytest.fixture
def parked_human_gate(engine, fake_harnesses, beads_project):
    return asyncio.run(_park_at_human_gate(engine, fake_harnesses, beads_project))


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done", "tests pass and the diff is right")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


def _remember_calls(workdir: Path) -> list[dict]:
    path = workdir / "calls.jsonl"
    if not path.exists():
        return []
    return [
        __import__("json").loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _human_remember_calls(workdir: Path, bead_id: str) -> list[dict]:
    default_key = f"alloy:human:{bead_id}"
    return [
        call
        for call in _remember_calls(workdir)
        if call.get("command") == "remember"
        and default_key in call.get("argv", [])
    ]


def _remember_key_and_body(call: dict) -> tuple[str, str]:
    argv = call["argv"]
    return argv[argv.index("--key") + 1], argv[1]


def _install_fake_bd_remember(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Route BeadsClient.remember through the fake-bd stub for call recording."""
    bindir = tmp_path / "fake-bd-bin"
    bindir.mkdir(exist_ok=True)
    binary = bindir / "bd"
    shutil.copy(FAKE_BD_SOURCE, binary)
    binary.chmod(0o755)

    workdir = tmp_path / "fake-bd-state"
    workdir.mkdir()
    config_path = workdir / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("ALLOY_FAKE_DIR", str(workdir))
    monkeypatch.setenv("ALLOY_FAKE_CONFIG", str(config_path))

    def remember_via_fake(self: BeadsClient, key: str, content: str) -> None:
        subprocess.run(
            [str(binary), "remember", content, "--key", key],
            cwd=str(self.repo),
            check=True,
            capture_output=True,
            text=True,
        )

    monkeypatch.setattr(BeadsClient, "remember", remember_via_fake)
    return workdir


async def _park_at_human_gate(engine, fake_harnesses, beads_project) -> tuple[str, RunResult]:
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[
                judge_entry("human", "which unicode normalization?"),
                judge_entry("done"),
            ],
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    paused = await engine.run(bead_id)
    assert paused.outcome == "waiting-human"
    return bead_id, paused


def _noop_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resume_without_graph(self: Engine, bead_id: str, instructions: str = "") -> RunResult:
        run_id = self.store.latest_run_for_bead(bead_id)["run_id"]
        return RunResult(bead_id=bead_id, run_id=run_id, outcome="done")

    monkeypatch.setattr(Engine, "resume", resume_without_graph)


def test_cli_resume_with_remember_records_default_human_key(
    parked_human_gate,
    beads_project,
    alloy_home,
    tmp_path,
    monkeypatch,
):
    """--remember with -m records alloy:human:<bead-id> plus provenance."""
    bead_id, paused = parked_human_gate
    workdir = _install_fake_bd_remember(tmp_path, monkeypatch)
    _noop_resume(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "resume",
            bead_id,
            "-m",
            "use NFKD",
            "--remember",
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
    )

    remembers = _human_remember_calls(workdir, bead_id)
    assert len(remembers) == 1, (
        f"exit={result.exit_code} stderr={result.stderr!r} remembers={remembers}"
    )

    key, stored = _remember_key_and_body(remembers[0])
    assert key == f"alloy:human:{bead_id}"
    body, run_id, note_bead_id, at = parse_provenance(stored)
    assert "use NFKD" in body
    assert run_id == paused.run_id
    assert note_bead_id == bead_id
    assert at is not None


def test_cli_resume_with_remember_custom_key(
    parked_human_gate,
    beads_project,
    alloy_home,
    tmp_path,
    monkeypatch,
):
    """--remember my-key stores under the supplied key."""
    bead_id, _paused = parked_human_gate
    workdir = _install_fake_bd_remember(tmp_path, monkeypatch)
    _noop_resume(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "resume",
            bead_id,
            "-m",
            "use NFKD",
            "--remember",
            "my-key",
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
    )

    remembers = [
        call
        for call in _remember_calls(workdir)
        if call.get("command") == "remember" and "my-key" in call.get("argv", [])
    ]
    assert len(remembers) == 1, (
        f"exit={result.exit_code} stderr={result.stderr!r} remembers={remembers}"
    )
    key, stored = _remember_key_and_body(remembers[0])
    assert key == "my-key"
    body, _run_id, note_bead_id, at = parse_provenance(stored)
    assert "use NFKD" in body
    assert note_bead_id == bead_id
    assert at is not None


def test_cli_resume_without_remember_makes_no_remember_calls(
    parked_human_gate,
    beads_project,
    alloy_home,
    tmp_path,
    monkeypatch,
):
    """Resume without --remember must not invoke bd remember on the CLI path."""
    bead_id, _paused = parked_human_gate
    workdir = _install_fake_bd_remember(tmp_path, monkeypatch)
    _noop_resume(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "resume",
            bead_id,
            "-m",
            "use NFKD",
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
    )

    assert result.exit_code == 0, (
        f"exit={result.exit_code!r} stdout={result.stdout!r} "
        f"stderr={result.stderr!r} exc={result.exception!r}"
    )
    assert _remember_calls(workdir) == []
