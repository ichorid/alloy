"""Cached check answers do not spend `max_total_checks`.

A check whose exact command already passed in this run against an unchanged
worktree is answered from the check cache (`CheckResult.cached`): it is
recorded -- the verifier and judge see it, `alloy logs` lists it -- but nothing
ran, so it must not count toward the run's verification budget (tentura-s7gl.37
run 96e7203d: 7 of 18 checks were cached repeats and the run still parked on
'max_total_checks reached')."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import bd_create, verifier_run_entry, verifier_stop_entry
from support import load_config, make_bead, make_harness
from test_fast_track import IMPLEMENTATION, done_entry, fast_track_config, run_check_entry
from test_runner_breaker import ScriptedRunner, _Registry
from typer.testing import CliRunner
from workflow_support import verification_script

from alloy.cli import app
from alloy.config import RoleSpec
from alloy.models import CheckRequest, utcnow
from alloy.monitor.render import _tests
from alloy.runtime import RunContext
from alloy.store import Store
from alloy.verify import check_logs, checks_label, checks_summary


def _record(command: str, *, cached: bool = False, legacy: bool = False) -> dict:
    record = {"command": command, "kind": "custom", "exit_code": 0, "iteration": 1}
    if not legacy:
        record["cached"] = cached
    return record


@pytest.fixture
def ctx(project: Path, tmp_path: Path) -> RunContext:
    store = Store(tmp_path / "alloy.db")
    store.create_run(
        run_id="run-1",
        bead_id="t-1",
        thread_id="run-1",
        recipe="tdd-loop",
        repo=project,
        worktree=project,
        branch="",
        log_dir=tmp_path / "logs",
    )
    config = load_config()
    return RunContext(
        bead=make_bead(),
        recipe=replace(config, verification=replace(config.verification, max_total_checks=4)),
        run_id="run-1",
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=None,
        store=store,
        checkpointer=None,
        log_dir=tmp_path / "logs",
    )


# -- the limit gate -----------------------------------------------------------


def test_cached_records_do_not_count_toward_max_total_checks(ctx):
    """3 real checks (1 baseline + 2 verifier) + 5 cached repeats stays under 4."""
    state = {
        "baseline": [_record("pytest -q")],
        "checks": [
            _record("pytest -q tests/a"),
            _record("pytest -q tests/b"),
            *[_record("pytest -q tests/a", cached=True) for _ in range(5)],
        ],
    }
    assert ctx.total_checks(state) == 3
    assert ctx.check_limits(state) is None


def test_real_checks_alone_still_reach_max_total_checks(ctx):
    state = {
        "baseline": [_record("pytest -q")],
        "checks": [_record(f"pytest -q tests/{n}") for n in range(3)] + [_record("x", cached=True)],
    }
    assert ctx.total_checks(state) == 4
    assert ctx.check_limits(state) == "max_total_checks reached"


def test_old_records_without_the_flag_count_as_run(ctx):
    """Checkpoints written before `cached` existed carry no key: they spent budget."""
    state = {"checks": [_record(f"c{n}", legacy=True) for n in range(4)]}
    assert ctx.total_checks(state) == 4
    assert ctx.check_limits(state) == "max_total_checks reached"


async def test_run_check_marks_the_cached_record_and_it_is_free(ctx, tmp_path):
    counter = tmp_path / "executions.txt"
    command = f"echo ran >> {counter}"
    records = [(await ctx.run_check(CheckRequest(command=command))).model_dump(mode="json") for _ in range(6)]
    assert len(counter.read_text().splitlines()) == 1
    assert [r["cached"] for r in records] == [False] + [True] * 5
    assert ctx.total_checks({"checks": records}) == 1
    logs = check_logs(tmp_path / "logs")
    assert [entry["cached"] for entry in logs] == [False] + [True] * 5


# -- status / monitor / logs --------------------------------------------------


def test_status_summary_and_monitor_show_the_cached_count_separately():
    state = {
        "checks": [_record("a"), _record("b"), _record("a", cached=True), _record("b", cached=True)],
        "iteration_checks": 4,
    }
    summary = checks_summary(state)
    assert summary["total"] == 2 and summary["cached"] == 2
    assert checks_label(state["checks"]) == "2 checks (+2 cached)"
    assert _tests({"checks": summary}) == "2 checks (+2 cached)"
    assert _tests({"checks": {**summary, "cached": 0}}) == "2 checks"
    assert _tests({"checks": summary}, "unicode").endswith("2+2c")


# -- the verification loop ----------------------------------------------------


async def test_tdd_loop_with_cached_repeats_stays_under_the_budget(project, alloy_home, fake_harnesses):
    """N real checks + M cached repeats finish under max_total_checks = N + 1.

    Without the fix the 6 verifier records plus the baseline park the run."""
    a, b = 'sh -c "exit 0; : a"', 'sh -c "exit 0; : b"'
    repeats = [verifier_run_entry(cmd, kind="custom") for cmd in (a, b, a, b, a, b)]
    fake_harnesses.configure(verification_script(verifier=[*repeats, verifier_stop_entry("green")]))
    config = load_config()
    # Baseline (1) + a + b = 3 real checks; the final pass's verifier repeats
    # cached answers or stops, so the budget has exactly one spare.
    config = replace(
        config,
        verification=replace(config.verification, max_total_checks=4, max_checks_per_iteration=20),
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert not final.get("limit_hit"), final.get("limit_hit")
    assert "__interrupt__" not in final
    assert final["outcome"] == "done"
    records = [*(final.get("baseline") or []), *final["checks"]]
    cached = sum(1 for record in records if record.get("cached"))
    assert cached >= 4
    assert len(records) - cached <= 3 < len(records)


async def test_fast_track_cached_repeats_do_not_spend_its_check_budget(project, alloy_home, fake_harnesses):
    command = f'{sys.executable} -c "print(1)"'
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(command, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
                *[run_check_entry(command, reason="again") for _ in range(4)],
                done_entry(reason="checked"),
                run_check_entry(command, reason="final pass check"),
                done_entry(reason="final pass verified"),
            ]
        }
    )
    config = fast_track_config()
    config = replace(
        config,
        limits=replace(config.limits, max_iterations=10, max_agent_calls=100, max_agent_calls_by_tier={}),
        verification=replace(config.verification, max_total_checks=2),
    )
    harness = make_harness(
        project,
        alloy_home,
        bead=make_bead(id="ft-cached", metadata={"alloy_recipe": "fast-track"}),
        config=config,
        recipe_name="fast-track",
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" not in final, final.get("limit_hit")
    assert not final.get("limit_hit")
    assert final["outcome"] == "done"
    flags = [record.get("cached") for record in final["checks"] if record["command"] == command]
    assert flags[0] is False and flags.count(True) >= 4


async def test_status_json_and_logs_show_the_cached_count(beads_project, alloy_home, fake_harnesses):
    from alloy.engine import Engine

    a = 'sh -c "exit 0; : a"'
    fake_harnesses.configure(
        verification_script(
            verifier=[
                verifier_run_entry(a, kind="custom"),
                verifier_run_entry(a, kind="custom"),
                verifier_stop_entry("green"),
            ]
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    await engine.run(bead_id)

    common = ["--repo", str(beads_project), "--root", str(alloy_home), "--json"]
    status = CliRunner().invoke(app, ["status", bead_id, *common])
    assert status.exit_code == 0, status.stdout
    checks = json.loads(status.stdout)["beads"][0]["checks"]
    assert checks["cached"] >= 1
    logs = CliRunner().invoke(app, ["logs", bead_id, *common])
    assert logs.exit_code == 0, logs.stdout
    payload = json.loads(logs.stdout)
    assert payload["checks_cached"] >= 1
    assert payload["checks_cached"] == sum(1 for check in payload["checks"] if check["cached"])
    table = CliRunner().invoke(app, ["logs", bead_id, "--repo", str(beads_project), "--root", str(alloy_home)])
    assert "cached)" in table.stdout


# -- agent-call budgets and the runner breaker --------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "alloy.db")
    store.create_run(
        run_id="run-1",
        bead_id="t-1",
        thread_id="run-1",
        recipe="tdd-loop",
        repo=tmp_path,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    return store


@pytest.mark.parametrize("role,cheap", [("implement", False), ("verifier", True)])
async def test_breaker_skipped_runners_spend_neither_agent_call_budget(store, tmp_path, role, cheap):
    """A harness the breaker skips leaves no ledger row: neither
    max_agent_calls nor max_cheap_agent_calls is spent on it."""
    store.mark_runner_unavailable("codex", utcnow() + timedelta(hours=1), reason="usage limit")
    codex = ScriptedRunner("codex")
    claude = ScriptedRunner("claude-write")
    config = load_config()
    ctx = RunContext(
        bead=make_bead(),
        recipe=replace(config, limits=replace(config.limits, max_agent_calls=1, max_cheap_agent_calls=1)),
        run_id="run-1",
        worktree=SimpleNamespace(path=tmp_path),
        worktrees=None,
        registry=_Registry({"codex": codex, "claude-write": claude}),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
    )
    chain = RoleSpec.parse({"runner": "codex", "fallback": {"runner": "claude-write"}})
    for _ in range(3):
        store.mark_runner_unavailable("codex", utcnow() + timedelta(hours=1), reason="usage limit")
        ctx._skip_blocked(role, chain)  # a pure skip spends nothing
    assert store.call_count("run-1") == 0 and store.call_count("run-1", cheap=True) == 0

    result = await ctx.call(role, chain, "prompt")
    assert result.ok and codex.calls == 0 and claude.calls == 1
    assert store.call_count("run-1", cheap=cheap) == 1
    assert store.call_count("run-1", cheap=not cheap) == 0
