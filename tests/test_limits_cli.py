"""`probe_all`, limits cache refresh, and `alloy limits [--json]` (alloy-w9d.5).

Acceptance tests for docs/plans/monitor-limits.md section A5. Behaviour is not
implemented yet — these must fail until probe_all orchestrates installed
harness probes, writes limits.json, the CLI prints JSON/table output, and
AGENTS.md and README.md document the command.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_limits_claude import RecordingFetch, _usage_payload, write_credentials
from typer.testing import CliRunner

from alloy.cli import app
from alloy.limits import HARNESS_LIMITS_KEYS, read_cache
from alloy.paths import AlloyPaths
from alloy.runners import RunnerRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
README = REPO_ROOT / "README.md"

FIVE_HOUR_USED_PERCENT = 42.0


def _invoke(*args: str, project: Path, alloy_home: Path) -> object:
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


def _claude_codex_only(fake_harnesses) -> None:
    fake_harnesses.remove("cursor-agent")


def _claude_fixture_fetch() -> RecordingFetch:
    return RecordingFetch(status=200, body=json.dumps(_usage_payload()))


def _claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    write_credentials(home)
    return home


def _codex_probe_raises_runtime_error(_home: Path) -> dict:
    raise RuntimeError("simulated codex probe failure")


@pytest.fixture
def probe_home(tmp_path: Path) -> Path:
    return _claude_home(tmp_path)


# -- probe_all -----------------------------------------------------------------


def test_probe_all_installed_harnesses_codex_runtime_error_writes_cache(
    alloy_home: Path,
    fake_harnesses,
    probe_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from alloy.limits import probe_all

    _claude_codex_only(fake_harnesses)
    monkeypatch.setattr("alloy.limits.codex.probe", _codex_probe_raises_runtime_error)

    paths = AlloyPaths.resolve(alloy_home).ensure()
    registry = RunnerRegistry()
    fetch = _claude_fixture_fetch()

    result = probe_all(paths, registry, fetch=fetch, home=probe_home)

    assert set(result) == {"claude", "codex"}
    assert result["codex"]["available"] is False
    assert "RuntimeError" in (result["codex"]["error"] or "")
    assert read_cache(paths) == result


# -- CLI: `alloy limits --json` ------------------------------------------------


def test_cli_limits_json_prints_installed_harness_samples(
    project: Path,
    alloy_home: Path,
    fake_harnesses,
    probe_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _claude_codex_only(fake_harnesses)
    monkeypatch.setattr("alloy.limits.codex.probe", _codex_probe_raises_runtime_error)
    monkeypatch.setattr("alloy.limits.claude.default_fetch", _claude_fixture_fetch())
    monkeypatch.setattr(Path, "home", lambda: probe_home)

    result = _invoke("limits", "--json", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"claude", "codex"}
    assert set(payload["claude"]) == set(HARNESS_LIMITS_KEYS)


# -- CLI: `alloy limits` (plain table) -----------------------------------------


def test_cli_limits_table_shows_harness_five_hour_and_used_percent(
    project: Path,
    alloy_home: Path,
    fake_harnesses,
    probe_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _claude_codex_only(fake_harnesses)
    monkeypatch.setattr("alloy.limits.codex.probe", _codex_probe_raises_runtime_error)
    monkeypatch.setattr("alloy.limits.claude.default_fetch", _claude_fixture_fetch())
    monkeypatch.setattr(Path, "home", lambda: probe_home)

    result = _invoke("limits", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0, result.stdout + result.stderr
    text = result.stdout
    assert "harness" in text
    assert "5h" in text
    assert str(int(FIVE_HOUR_USED_PERCENT)) in text


# -- AGENTS.md and README.md documentation -------------------------------------


def test_agents_md_documents_alloy_limits_command():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "alloy limits" in text


def test_readme_documents_alloy_limits_command():
    text = README.read_text(encoding="utf-8")
    assert "alloy limits" in text
