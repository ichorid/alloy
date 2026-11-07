"""The recipe `dispatch:` policy and the dirty-checkout hold (2026-11-07).

- `require_test_cmd`: a ready bead without `alloy_test_cmd` is labelled and
  skipped -- it never starts a run that parks at "baseline not runnable" and
  holds the queue.
- `stale_base_upstream`: a bead whose work already landed upstream (its id or
  its epic's id in an upstream-only commit subject) is labelled and skipped.
- `park_after_human_minutes`: a run that has held the queue at the human gate
  that long is parked -- patch saved, bead deferred, run cancelled, tree
  stashed -- unless the judge had already called its diff complete.
- A dirty primary checkout holds dispatch instead of booking a refused run,
  and a refused start no longer lets the next pick sweep the foreign changes in.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta

import pytest
from conftest import bd_create

from alloy import beads as bd
from alloy.config import ConfigError, RecipeConfig
from alloy.engine import Engine, EngineError
from alloy.models import utcnow
from alloy.scheduler import LABEL_NEEDS_TEST_CMD, LABEL_STALE_BASE, PRIMARY_CHECKOUT_HOLDER, Scheduler
from alloy.store import RUN_CANCELLED, RUN_WAITING_HUMAN

BUILTIN_TDD_LOOP = __import__("alloy.config", fromlist=["BUILTIN_RECIPE_DIR"]).BUILTIN_RECIPE_DIR / "tdd-loop.yaml"


def _policy_recipe(project, name: str, dispatch: dict) -> str:
    """A project recipe running the tdd-loop graph with a `dispatch:` block."""
    import yaml

    raw = yaml.safe_load(BUILTIN_TDD_LOOP.read_text(encoding="utf-8"))
    raw.update({"name": name, "graph": "tdd-loop", "dispatch": dispatch})
    recipes = project / ".alloy" / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    (recipes / f"{name}.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    return name


@pytest.fixture
def scheduler(beads_project, alloy_home):
    return Scheduler(engine=Engine.open(beads_project, alloy_home), poll_seconds=0.01, once=True)


def _labels(scheduler, bead_id):
    return scheduler.engine.beads.show(bead_id).labels


# -- config ------------------------------------------------------------------


def test_dispatch_block_parses_and_rejects_typos():
    spec = RecipeConfig.parse(
        {"name": "x", "dispatch": {"require_test_cmd": True, "stale_base_upstream": "origin/main",
                                   "park_after_human_minutes": 45}}
    ).dispatch
    assert spec.require_test_cmd and spec.stale_base_upstream == "origin/main"
    assert spec.park_after_human_minutes == 45
    assert RecipeConfig.parse({"name": "x"}).dispatch.park_after_human_minutes is None
    with pytest.raises(ConfigError, match="unknown dispatch keys"):
        RecipeConfig.parse({"name": "x", "dispatch": {"park_after_minutes": 45}})
    with pytest.raises(ConfigError, match="remote"):
        RecipeConfig.parse({"name": "x", "dispatch": {"stale_base_upstream": "main"}})


# -- require_test_cmd ----------------------------------------------------------


def test_a_bead_without_a_test_cmd_is_labelled_and_skipped_not_held(scheduler, beads_project):
    recipe = _policy_recipe(beads_project, "strict", {"require_test_cmd": True})
    missing = bd_create(beads_project, "no test cmd", priority=0, alloy_recipe=recipe)
    ready = bd_create(beads_project, "has one", priority=1, alloy_recipe=recipe, alloy_test_cmd="pytest -q")

    picked = scheduler.next_task()

    assert picked is not None and picked.id == ready
    assert LABEL_NEEDS_TEST_CMD in _labels(scheduler, missing)
    assert LABEL_NEEDS_TEST_CMD not in _labels(scheduler, ready)


def test_without_the_policy_a_bead_without_a_test_cmd_still_runs(scheduler, beads_project):
    only = bd_create(beads_project, "no test cmd", alloy_recipe="tdd-loop")
    assert scheduler.next_task().id == only


# -- stale_base_upstream -----------------------------------------------------------


def test_a_bead_whose_epic_already_landed_upstream_is_skipped(scheduler, beads_project, monkeypatch):
    recipe = _policy_recipe(beads_project, "fresh-base", {"stale_base_upstream": "origin/main"})
    landed = bd_create(beads_project, "unit twelve", priority=0, alloy_recipe=recipe)
    other = bd_create(beads_project, "unrelated", priority=1, alloy_recipe=recipe)
    monkeypatch.setattr(
        scheduler,
        "_upstream_only_subjects",
        lambda policy: [f"{landed}.7: client unit seven (1040)", "fix: something else"],
    )
    monkeypatch.setattr(scheduler.engine.beads, "epic_root", lambda bead_id: landed if bead_id == landed else None)

    picked = scheduler.next_task()

    assert picked is not None and picked.id == other
    assert LABEL_STALE_BASE in _labels(scheduler, landed)

    # Once the checkout has the upstream work, the bead dispatches and loses the label.
    monkeypatch.setattr(scheduler, "_upstream_only_subjects", lambda policy: [])
    assert scheduler.next_task().id == landed
    assert LABEL_STALE_BASE not in _labels(scheduler, landed)


def test_an_id_prefix_is_not_a_match(scheduler, beads_project, monkeypatch):
    recipe = _policy_recipe(beads_project, "fresh-base", {"stale_base_upstream": "origin/main"})
    bead = bd_create(beads_project, "prefix", alloy_recipe=recipe)
    monkeypatch.setattr(scheduler, "_upstream_only_subjects", lambda policy: [f"{bead}x: other bead", f"x{bead}: other"])
    assert scheduler.next_task().id == bead


# -- dirty checkout ------------------------------------------------------------


async def test_a_dirty_checkout_holds_dispatch_without_booking_a_run(scheduler, beads_project):
    bead = bd_create(beads_project, "would sweep", alloy_recipe="tdd-loop")
    (beads_project / "mypkg" / "__init__.py").write_text("SOMEONE_ELSES_WIP = True\n", encoding="utf-8")

    assert await scheduler.tick() is False
    assert await scheduler.tick() is False

    assert scheduler.engine.store.runs_for_bead(bead) == []
    assert scheduler._hold is not None and scheduler._hold[0] == PRIMARY_CHECKOUT_HOLDER
    assert "uncommitted tracked changes" in scheduler._hold[1]


async def test_a_refused_start_does_not_let_the_next_start_sweep_the_changes(beads_project, alloy_home, fake_harnesses):
    engine = Engine.open(beads_project, alloy_home)
    bead = bd_create(beads_project, "would sweep", alloy_recipe="tdd-loop")
    (beads_project / "mypkg" / "__init__.py").write_text("SOMEONE_ELSES_WIP = True\n", encoding="utf-8")

    for _ in range(2):
        with pytest.raises(EngineError, match="uncommitted tracked changes"):
            await engine.run(bead)
    assert fake_harnesses.calls == []


# -- auto-park -------------------------------------------------------------------


def _seed_waiting_run(scheduler, repo, bead_id, recipe, *, minutes_ago, reason="Alloy stopped the loop: max_wall_time"):
    run_id = f"run-{bead_id}"
    store = scheduler.engine.store
    store.create_run(
        run_id=run_id, bead_id=bead_id, thread_id=run_id, recipe=recipe, repo=repo,
        worktree=str(repo), branch="", log_dir=None,
    )
    store.update_run(
        run_id,
        status=RUN_WAITING_HUMAN,
        stage=RUN_WAITING_HUMAN,
        outcome_reason=reason,
        paused_at=(utcnow() - timedelta(minutes=minutes_ago)).isoformat(),
    )
    scheduler.engine.beads.set_status(bead_id, bd.STATUS_WAITING_HUMAN)
    return run_id


def _dirty(repo):
    (repo / "mypkg" / "__init__.py").write_text("PARKED_WORK = 1\n", encoding="utf-8")
    (repo / "mypkg" / "new_module.py").write_text("NEW = 2\n", encoding="utf-8")


def test_a_run_waiting_too_long_is_parked_and_the_queue_moves_on(scheduler, beads_project, alloy_home):
    recipe = _policy_recipe(beads_project, "parky", {"park_after_human_minutes": 45})
    stuck = bd_create(beads_project, "stuck", priority=0, alloy_recipe=recipe)
    other = bd_create(beads_project, "next", priority=1, alloy_recipe=recipe)
    run_id = _seed_waiting_run(scheduler, beads_project, stuck, recipe, minutes_ago=50)
    _dirty(beads_project)

    assert scheduler.next_task() is None, "the parking tick dispatches nothing itself"

    assert scheduler.engine.beads.show(stuck).status == "deferred"
    assert scheduler.engine.store.get_run(run_id)["status"] == RUN_CANCELLED
    assert scheduler.engine.primary_checkout_dirty() is None, "the in-place work is stashed"
    assert (beads_project / ".beads").is_dir(), "Alloy's own state is never stashed"
    patches = list((scheduler.engine.paths.root / "parked").glob(f"{stuck}-*.patch"))
    assert len(patches) == 1
    text = patches[0].read_text()
    assert "PARKED_WORK" in text and "new_module.py" in text
    stash = subprocess.run(["git", "stash", "list"], cwd=beads_project, capture_output=True, text=True).stdout
    assert f"alloy-park {stuck}" in stash
    # The saved patch restores the work exactly.
    subprocess.run(["git", "apply", str(patches[0])], cwd=beads_project, check=True)
    assert (beads_project / "mypkg" / "new_module.py").read_text() == "NEW = 2\n"
    subprocess.run(["git", "checkout", "--", "."], cwd=beads_project, check=True)
    (beads_project / "mypkg" / "new_module.py").unlink()

    picked = scheduler.next_task()
    assert picked is not None and picked.id == other


def test_a_run_waiting_less_than_the_threshold_is_left_alone(scheduler, beads_project):
    recipe = _policy_recipe(beads_project, "parky", {"park_after_human_minutes": 45})
    stuck = bd_create(beads_project, "stuck", priority=0, alloy_recipe=recipe)
    bd_create(beads_project, "next", priority=1, alloy_recipe=recipe)
    _seed_waiting_run(scheduler, beads_project, stuck, recipe, minutes_ago=10)

    assert scheduler.next_task() is None
    assert scheduler.engine.beads.show(stuck).status == bd.STATUS_WAITING_HUMAN


@pytest.mark.parametrize("complete", ["landing-gate", "judge-done"])
def test_a_stop_whose_diff_was_called_complete_is_not_parked(scheduler, beads_project, complete):
    recipe = _policy_recipe(beads_project, "parky", {"park_after_human_minutes": 45})
    stuck = bd_create(beads_project, "stuck", priority=0, alloy_recipe=recipe)
    bd_create(beads_project, "next", priority=1, alloy_recipe=recipe)
    reason = (
        "landing gate check `./scripts/run_tests.sh server-affected` failed on the final tree"
        if complete == "landing-gate"
        else "Alloy stopped the loop: max_wall_time reached (151m/150m)"
    )
    run_id = _seed_waiting_run(scheduler, beads_project, stuck, recipe, minutes_ago=90, reason=reason)
    if complete == "judge-done":
        store = scheduler.engine.store
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO agent_calls (run_id, bead_id, role, runner, model, prompt_hash, started_at, ended_at,"
                " duration_s, exit_code, ok, structured_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, stuck, "judge", "claude", "sonnet", "h", utcnow().isoformat(), utcnow().isoformat(),
                 1.0, 0, 1, json.dumps({"decision": "done", "reason": "complete"})),
            )

    assert scheduler.next_task() is None
    assert scheduler.engine.beads.show(stuck).status == bd.STATUS_WAITING_HUMAN
    assert scheduler.engine.store.get_run(run_id)["status"] == RUN_WAITING_HUMAN
