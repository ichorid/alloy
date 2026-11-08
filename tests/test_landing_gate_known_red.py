"""Known-red landing gate checks (2026-11-07).

A gate check with `rerun_on_base` that fails only on tests which fail the same
way on the run's base commit is pre-existing: it does not send the bead back
to implement. Tentura lost whole iterations to unrelated red pg tests that
were already red on main. A test that passes on the base, a load/compile
failure, or a log without test names keeps the check red, and the base rerun
happens at most once per command per run."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import IMPLEMENTATION
from support import make_bead, make_harness
from test_fast_track import done_entry, run_check_entry

from alloy.config import ConfigError, RecipeConfig, load_recipe
from alloy.recipes.landing_gate import LOAD_FAILURE, failing_tests

# -- parsing -------------------------------------------------------------------


def test_dart_failure_lines_name_file_and_test():
    log = "\n".join(
        [
            "01:05 +285 -1: test/a_pg_test.dart: group a test [E]",
            "01:06 +285 ~2 -2: /work/tree/packages/client/test/b_test.dart: b test (phone, ru) [E]",
            "01:07 +286 -2: test/c_test.dart: passing test",
            "00:00 +0 -3: loading /work/tree/packages/client/test/d_test.dart [E]",
            "\x1b[31m01:09 +286 -4: test/a_pg_test.dart: group a test [E]\x1b[0m",
        ]
    )
    assert failing_tests(log, roots=(Path("/work/tree"),)) == {
        ("test/a_pg_test.dart", "group a test"),
        ("test/b_test.dart", "b test (phone, ru)"),
        ("test/d_test.dart", LOAD_FAILURE),
    }


def test_a_single_file_dart_run_omits_the_file():
    log = "00:03 +0 -1: some group some test [E]\n"
    assert failing_tests(log, single_file="test/a_test.dart") == {("test/a_test.dart", "some group some test")}


def test_pytest_failures_and_collection_errors():
    log = "FAILED tests/test_a.py::test_one - AssertionError\nERROR tests/test_b.py\n"
    assert failing_tests(log) == {("tests/test_a.py", "test_one"), ("tests/test_b.py", LOAD_FAILURE)}


def test_rerun_keys_parse():
    spec = RecipeConfig.parse(
        {"name": "x", "landing_gate": [{"paths": ["**"], "checks": [{"cmd": "a", "rerun_on_base": "a {files}"}]}]}
    ).landing_gate[0].checks[0]
    assert spec.rerun_on_base == "a {files}" and spec.base_setup is None
    with pytest.raises(ConfigError, match="base_setup without rerun_on_base"):
        RecipeConfig.parse(
            {"name": "x", "landing_gate": [{"paths": ["**"], "checks": [{"cmd": "a", "base_setup": "make"}]}]}
        )


# -- the gate ------------------------------------------------------------------

PROBE = "test -f mypkg/__init__.py"
FAILURES = "mypkg/failures.txt"
CHECK = "sh check.sh"
RERUN = "sh check.sh {files}"
OLD = "00:01 +0 -1: test/old_test.dart: old broken [E]"
NEW = "00:02 +0 -2: test/new_test.dart: new broken [E]"
LOADING = "00:00 +0 -1: loading test/old_test.dart [E]"


def _commit_base(project: Path, failures: str) -> None:
    # The check prints the failure lines it finds and fails when there are any.
    (project / "check.sh").write_text(f"cat {FAILURES}\ntest ! -s {FAILURES}\n", encoding="utf-8")
    (project / FAILURES).write_text(failures, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "base with a red test"], cwd=project, check=True)


def _config(max_iterations=20):
    config = load_recipe("fast-track")
    gate = RecipeConfig.parse(
        {
            "name": "x",
            "landing_gate": [
                {"paths": ["mypkg/**"], "checks": [{"cmd": CHECK, "purpose": "suite", "rerun_on_base": RERUN}]}
            ],
        }
    ).landing_gate
    return replace(config, landing_gate=gate, limits=replace(config.limits, max_iterations=max_iterations))


def _harness(project, alloy_home, config):
    return make_harness(
        project, alloy_home, bead=make_bead(metadata={"alloy_recipe": "fast-track"}), config=config,
        recipe_name="fast-track",
    )


def _base_reruns(final):
    return [c for c in final.get("checks", []) if "rerun on the base" in (c.get("purpose") or "")]


def _rerun_logs(alloy_home: Path) -> list[Path]:
    return [p for p in alloy_home.rglob("check-*.log") if "rerun on the base" in p.read_text(errors="replace")]


async def test_a_failure_already_red_on_the_base_does_not_hold_the_bead(project, alloy_home, fake_harnesses):
    _commit_base(project, OLD + "\n")
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(PROBE, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
                done_entry(reason="verified"),
            ]
        }
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    assert len(fake_harnesses.calls_for("implement")) == 2, "a pre-existing failure is not a new iteration"
    assert any("pre-existing" in entry and "old broken" in entry for entry in final["journal"])
    assert len(_rerun_logs(alloy_home)) == 1
    worktrees = subprocess.run(["git", "worktree", "list"], cwd=project, capture_output=True, text=True).stdout
    assert len(worktrees.splitlines()) == 1, "the base worktree is removed"


async def test_a_new_failure_goes_back_to_implement_and_the_base_reruns_once(project, alloy_home, fake_harnesses):
    _commit_base(project, OLD + "\n")
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(
                    PROBE,
                    write=[
                        {"path": "mypkg/__init__.py", "content": IMPLEMENTATION},
                        {"path": FAILURES, "content": f"{OLD}\n{NEW}\n"},
                    ],
                ),
                done_entry(reason="verified"),
                run_check_entry(PROBE, write=[{"path": FAILURES, "content": OLD + "\n"}]),
                done_entry(reason="new failure fixed"),
            ]
        }
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    calls = fake_harnesses.calls_for("implement")
    assert len(calls) == 4
    assert "new broken" in calls[2]["prompt"], "the bead's own failure goes back to the agent"
    assert any("pre-existing" in entry for entry in final["journal"])
    assert len(_rerun_logs(alloy_home)) == 1, "the base result is reused for the second red gate"


async def test_a_load_failure_is_never_pre_existing(project, alloy_home, fake_harnesses):
    _commit_base(project, LOADING + "\n")
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(PROBE, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
                done_entry(reason="verified"),
                run_check_entry(PROBE, write=[{"path": FAILURES, "content": ""}]),
                done_entry(reason="load fixed"),
            ]
        }
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    assert len(fake_harnesses.calls_for("implement")) == 4
    assert _rerun_logs(alloy_home) == []


# -- flaky at the gate (bench full-11, 2026-11-08) -----------------------------


def _commit_flaky_base(project: Path, marker: Path) -> None:
    # The check fails once (the first run anywhere) on a test that passes on the base and on any rerun.
    (project / "check.sh").write_text(
        f'if [ ! -e "{marker}" ]; then touch "{marker}"; echo "{NEW}"; exit 1; fi\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "base with a flaky test"], cwd=project, check=True)


async def test_a_flaky_failure_outside_the_diff_does_not_hold_the_bead(project, alloy_home, fake_harnesses, tmp_path):
    _commit_flaky_base(project, tmp_path / "ran-once")
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(PROBE, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
                done_entry(reason="verified"),
            ]
        }
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    assert len(fake_harnesses.calls_for("implement")) == 2, "a flaky failure is not a new iteration"
    assert any("flaky" in entry and "new broken" in entry for entry in final["journal"])
    assert final["known_red"] == {"test/new_test.dart::new broken": "flaky"}


async def test_a_failure_in_a_file_the_bead_changed_is_not_rerun_as_flaky(
    project, alloy_home, fake_harnesses, tmp_path
):
    _commit_flaky_base(project, tmp_path / "ran-once")
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(
                    PROBE,
                    write=[
                        {"path": "mypkg/__init__.py", "content": IMPLEMENTATION},
                        {"path": "test/new_test.dart", "content": "// changed by the bead\n"},
                    ],
                ),
                done_entry(reason="verified"),
                run_check_entry(PROBE),
                done_entry(reason="fixed"),
            ]
        }
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    calls = fake_harnesses.calls_for("implement")
    assert len(calls) == 4
    assert "new broken" in calls[2]["prompt"], "the bead's own test file stays its failure"
    assert not (final.get("known_red") or {})
