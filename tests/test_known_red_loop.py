"""Pre-existing and flaky failures inside the check loop (2026-11-07).

With `verification.rerun_tests` set, a red check whose failing tests are not
the bead's -- red the same way on the base commit, or green on an isolated
rerun -- is excused: it does not send the run back to implement, and later
implement prompts list those tests as out of scope. Bench calibration: cheap
implementers "fixed" an unrelated pre-existing test, acceptance called the
change out of scope, and finished beads ran out of iterations."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import IMPLEMENTATION, judge_entry, verifier_run_entry, verifier_stop_entry
from support import load_config, make_bead, make_harness
from test_fast_track import done_entry, run_check_entry
from workflow_support import verification_script

from alloy.config import ConfigError, RecipeConfig, VerificationSpec, load_recipe
from alloy.models import CheckResult
from alloy.recipes.known_red import FLAKY, PRE_EXISTING, excused_note, file_outside_bugs

FAILURES = "mypkg/failures.txt"
CHECK = "sh check.sh"
RERUN = "sh check.sh {files}"
OLD = "FAILED tests/test_old.py::test_old - AssertionError"
NEW = "FAILED tests/test_new.py::test_new - AssertionError"
NOT_OURS = "## Failing tests that are not this task's"


def _commit_base(project: Path, failures: str, script: str | None = None) -> None:
    # The check prints the failure lines it finds and fails when there are any.
    (project / "check.sh").write_text(script or f"cat {FAILURES}\ntest ! -s {FAILURES}\n", encoding="utf-8")
    (project / FAILURES).write_text(failures, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)


def _config(*, rerun: str | None = RERUN, max_iterations: int = 6):
    config = load_recipe("fast-track")
    verification = replace(config.verification, rerun_tests=rerun)
    return replace(config, verification=verification, limits=replace(config.limits, max_iterations=max_iterations))


def _harness(project, alloy_home, config):
    return make_harness(
        project,
        alloy_home,
        bead=make_bead(metadata={"alloy_recipe": "fast-track"}),
        config=config,
        recipe_name="fast-track",
    )


CHECK_SPEC = {"command": CHECK, "purpose": "the suite", "kind": "regression", "required": True}


def _script(*entries):
    """Enough checked `done` answers after the scripted ones for any final pass."""
    return {"implement": [*entries, *(done_entry(checks=[CHECK_SPEC], reason="verified") for _ in range(4))]}


# -- config ----------------------------------------------------------------------


def test_rerun_tests_parses_and_needs_files():
    spec = RecipeConfig.parse({"name": "x", "verification": {"rerun_tests": "pytest -q {files}"}}).verification
    assert spec.rerun_tests == "pytest -q {files}" and spec.base_setup is None
    with pytest.raises(ConfigError, match=r"\{files\}"):
        VerificationSpec.parse({"rerun_tests": "pytest -q"})
    with pytest.raises(ConfigError, match="base_setup without rerun_tests"):
        VerificationSpec.parse({"base_setup": "make"})


# -- the loop --------------------------------------------------------------------


async def test_a_pre_existing_failure_does_not_send_the_run_back(project, alloy_home, fake_harnesses):
    _commit_base(project, OLD + "\n")
    fake_harnesses.configure(
        _script(run_check_entry(CHECK, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]))
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    assert final["known_red"] == {"tests/test_old.py::test_old": PRE_EXISTING}
    check = CheckResult.model_validate(next(c for c in final["checks"] if c["command"] == CHECK))
    assert check.all_excused and "not this task's" in check.headline()
    second = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert NOT_OURS in second and "tests/test_old.py::test_old" in second
    assert not [a for a in final.get("attempts", []) if "required check" in a.get("reason", "")]
    assert any("not this task's" in entry for entry in final["journal"])


async def test_a_flaky_failure_is_excused(project, alloy_home, fake_harnesses, tmp_path):
    flag = tmp_path / "ran-once"
    # Red the first time it runs anywhere, green ever after.
    script = f'if [ -f "{flag}" ]; then exit 0; fi\ntouch "{flag}"\necho "{NEW}"\nexit 1\n'
    _commit_base(project, "", script=script)
    fake_harnesses.configure(
        _script(run_check_entry(CHECK, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]))
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    assert final["known_red"] == {"tests/test_new.py::test_new": FLAKY}


async def test_the_beads_own_failure_still_counts(project, alloy_home, fake_harnesses):
    _commit_base(project, "")
    fake_harnesses.configure(
        _script(
            run_check_entry(
                CHECK,
                write=[
                    {"path": "mypkg/__init__.py", "content": IMPLEMENTATION},
                    {"path": FAILURES, "content": NEW + "\n"},
                ],
            ),
            run_check_entry(CHECK, write=[{"path": FAILURES, "content": ""}]),
        )
    )
    final = await _harness(project, alloy_home, _config()).start()

    assert final["outcome"] == "done"
    assert not final.get("known_red")
    second = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert "test_new" in second and NOT_OURS not in second


async def test_without_rerun_tests_nothing_changes(project, alloy_home, fake_harnesses):
    _commit_base(project, OLD + "\n")
    fake_harnesses.configure(
        _script(
            run_check_entry(CHECK, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
            run_check_entry(CHECK, write=[{"path": FAILURES, "content": ""}]),
        )
    )
    final = await _harness(project, alloy_home, _config(rerun=None)).start()

    assert not final.get("known_red")
    second = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert "test_old" in second and NOT_OURS not in second


async def test_tdd_loop_lets_done_stand_over_a_pre_existing_failure(project, alloy_home, fake_harnesses):
    _commit_base(project, OLD + "\n")
    fake_harnesses.configure(
        verification_script(
            verifier=[verifier_run_entry(CHECK, kind="regression"), verifier_stop_entry("only an old failure")],
            judge=[judge_entry("done"), judge_entry("done")],
        )
    )
    config = load_config()
    config = replace(config, verification=replace(config.verification, rerun_tests=RERUN))
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 1
    assert final["known_red"] == {"tests/test_old.py::test_old": PRE_EXISTING}
    assert not any("overridden by Alloy" in row["reason"] for row in final["attempts"])
    # The verifier is told not to chase it (bench stuck-1: 7 of 20 checks did).
    after = fake_harnesses.calls_for("verifier")[1]["prompt"]
    assert NOT_OURS in after and "Spend no checks investigating them" in after
    # So are the evaluators (bench full-4/toy-8: the judge asked a human to "waive the gate").
    evaluators = [call["prompt"] for role in ("judge", "acceptance") for call in fake_harnesses.calls_for(role)]
    assert evaluators and all(NOT_OURS in p and "ask a human to waive" in p for p in evaluators)


# -- notes and bugs ----------------------------------------------------------------


def test_the_note_names_each_test_and_why():
    note = excused_note({"t.py::a": PRE_EXISTING, "t.py::b": FLAKY}, "abcdef1234567")
    assert note.startswith(NOT_OURS)
    assert "`t.py::a` -- fails the same way on the base commit abcdef123" in note
    assert "`t.py::b` -- failed once and passed when run again" in note
    assert excused_note({}, "abc") == ""


class _Beads:
    def __init__(self, existing):
        self.existing = existing
        self.created: list[dict] = []

    def filed_bugs(self):
        return self.existing

    def create_bug(self, **kwargs):
        self.created.append(kwargs)
        return f"bug-{len(self.created)}"


def _bug_ctx(beads):
    return SimpleNamespace(
        beads=beads,
        run_id="r1",
        bead=SimpleNamespace(id="b-1"),
        worktree=SimpleNamespace(base_commit="abc1234567"),
        recipe=SimpleNamespace(verification=SimpleNamespace(rerun_tests="pytest -q {files}")),
    )


def test_one_bug_per_file_for_tests_one_cause_broke():
    """Bench full-8: a landed bead broke 9 CSV tests; the next bead filed 9 bugs."""
    beads = _Beads([{"title": "x", "description": "- `t_csv.py::old`", "status": "open"}])
    found = {f"t_csv.py::t{n}": PRE_EXISTING for n in range(5)} | {"t_csv.py::old": PRE_EXISTING}
    found["t_units.py::inch"] = PRE_EXISTING

    filed = file_outside_bugs(_bug_ctx(beads), found, CheckResult(command="pytest", exit_code=1))

    assert filed == ["bug-1", "bug-2"]
    csv, units = beads.created
    assert csv["title"] == "Pre-existing failing tests in t_csv.py (5)"
    assert all(f"`t_csv.py::t{n}`" in csv["acceptance"] for n in range(5)) and "::old" not in csv["description"]
    assert "git bisect run pytest -q t_csv.py" in csv["description"]
    assert units["title"] == "Pre-existing failing test t_units.py::inch"

    # A later run seeing the same tests files nothing: the grouped bug names each of them.
    beads.existing = [{"title": csv["title"], "description": csv["description"], "status": "open"}]
    assert (
        file_outside_bugs(_bug_ctx(beads), {"t_csv.py::t3": PRE_EXISTING}, CheckResult(command="p", exit_code=1)) == []
    )


def test_one_bug_per_excused_test_deduplicated_against_open_bugs():
    beads = _Beads(
        [
            {"title": "Pre-existing failing test t.py::a", "status": "open"},
            {"title": "Flaky test t.py::b", "status": "closed"},
        ]
    )
    ctx = _bug_ctx(beads)
    result = CheckResult(command="pytest", exit_code=1)

    filed = file_outside_bugs(ctx, {"t.py::a": PRE_EXISTING, "t.py::b": FLAKY, "t.py::c": PRE_EXISTING}, result)

    assert filed == ["bug-1", "bug-2"]
    titles = [bug["title"] for bug in beads.created]
    assert titles == ["Flaky test t.py::b", "Pre-existing failing test t.py::c"]
    assert all(bug["priority"] == 3 and bug["discovered_from"] == "b-1" for bug in beads.created)


# -- a filed bug that is already fixed (bench full-8/toy-9j2) ----------------------


def _bug_harness(project, alloy_home, keys: str):
    config = load_config()
    config = replace(config, verification=replace(config.verification, rerun_tests=RERUN))
    return make_harness(project, alloy_home, config=config, bead=make_bead(metadata={"alloy_known_red": keys}))


async def test_a_filed_bug_whose_tests_pass_closes_without_work(project, alloy_home, fake_harnesses):
    _commit_base(project, "")  # the filed test passes by now
    fake_harnesses.configure(verification_script())
    harness = _bug_harness(project, alloy_home, "tests/test_old.py::test_old")
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["outcome_reason"].startswith("already fixed: tests/test_old.py::test_old")
    assert fake_harnesses.calls == [], "no agent is called"


async def test_a_filed_bug_whose_tests_still_fail_runs_as_usual(project, alloy_home, fake_harnesses):
    _commit_base(project, OLD + "\n")
    fake_harnesses.configure(verification_script())
    harness = _bug_harness(project, alloy_home, "tests/test_old.py::test_old")
    try:
        await harness.start()
    finally:
        harness.close()

    assert fake_harnesses.calls, "the run goes on to the agents"
