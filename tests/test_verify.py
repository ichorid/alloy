"""Verification is deterministic: Alloy runs the suite, not an LLM."""

from __future__ import annotations

import sys
from pathlib import Path

from alloy.verify import detect_command, parse_counts, resolve_command, run_tests

PYTEST = f"{sys.executable} -m pytest -q"


async def test_failing_suite_is_reported_with_its_exit_code(project, tmp_path):
    (project / "tests" / "test_x.py").write_text("def test_x():\n    assert False\n")
    report = await run_tests(PYTEST, project, log_dir=tmp_path / "logs")

    assert not report.ok
    assert report.exit_code != 0
    assert report.failed == 1
    assert "assert False" in report.tail
    assert Path(report.log_path).exists()


async def test_passing_suite_is_reported_as_ok(project, tmp_path):
    (project / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    report = await run_tests(PYTEST, project, log_dir=tmp_path / "logs")

    assert report.ok
    assert report.passed == 1
    assert report.headline() == "1 passed, 0 failed"


async def test_a_hanging_suite_times_out_rather_than_blocking_the_run(project):
    report = await run_tests(f"{sys.executable} -c 'import time; time.sleep(30)'",
                             project, timeout_s=1.0)
    assert report.timed_out
    assert not report.ok
    assert "timed out" in report.headline()


async def test_output_kept_in_state_is_bounded(project, tmp_path):
    (project / "tests" / "test_x.py").write_text(
        "def test_x():\n    print('y' * 100000)\n    assert False\n"
    )
    report = await run_tests(PYTEST, project, log_dir=tmp_path / "logs")

    assert len(report.tail) < 5000
    assert len(Path(report.log_path).read_text()) > 50_000  # the full output is on disk


def test_command_is_autodetected_from_project_layout(tmp_path):
    (tmp_path / "Cargo.toml").write_text("")
    assert detect_command(tmp_path) == "cargo test"

    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text("{}")
    assert detect_command(node) == "npm test --silent"

    assert detect_command(tmp_path / "nothing") is None


def test_explicit_configuration_beats_the_context_agents_guess(project):
    assert resolve_command(project, configured="make check", from_context="npm test") == \
        "make check"
    assert resolve_command(project, from_context="npm test") == "npm test"
    assert resolve_command(project).endswith(" -m pytest -q")  # autodetected


def test_counts_are_parsed_from_the_common_runners():
    assert parse_counts("== 3 passed, 2 failed in 1.2s ==") == (3, 2)
    assert parse_counts("test result: FAILED. 7 passed; 1 failed") == (7, 1)
    assert parse_counts("Tests:       2 failed, 9 passed, 11 total") == (9, 2)
    assert parse_counts("nothing recognizable") == (None, None)


def test_xdist_style_pytest_output_is_parsed_from_the_final_summary():
    output = "bringing up nodes...\n........\n279 passed in 98.50s"
    passed, failed = parse_counts(output)
    assert passed == 279
    assert (failed or 0) == 0


def test_a_bare_python_is_repointed_at_an_interpreter_that_exists(monkeypatch):
    """Agents suggest `python -m pytest` on machines that only have python3."""
    import shutil

    from alloy.verify import normalize_command

    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda name, *a, **k: None if name == "python" else real_which(name, *a, **k),
    )

    normalized = normalize_command("python -m pytest -q")

    assert not normalized.startswith("python ")
    assert normalized.endswith(" -m pytest -q")
    assert normalize_command("npm test") == "npm test"


def test_an_unrunnable_suggestion_loses_to_one_that_works(project):
    from alloy.verify import resolve_command

    chosen = resolve_command(project, configured="definitely-not-installed test",
                             from_context="npm test --silent")
    assert chosen == "npm test --silent"


def test_a_runnable_suggestion_is_kept_even_when_others_exist(project):
    from alloy.verify import resolve_command

    assert resolve_command(project, configured="git --version") == "git --version"


def test_shell_expressions_are_left_to_the_shell(project):
    from alloy.verify import is_runnable

    assert is_runnable("cd sub && make test")
    assert not is_runnable("definitely-not-installed")
