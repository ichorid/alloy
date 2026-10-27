"""Fast-track escalation: a mis-sized fast-track bead is rerouted to a fuller
recipe -- exactly like `alloy reroute` -- instead of spinning to
max_iterations. Off unless the recipe has an `escalate:` block."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import IMPLEMENTATION, bd_create
from support import load_config, make_bead, make_harness
from test_fast_track import run_check_entry, verdict_entry

from alloy import beads as bd
from alloy.config import ConfigError, RecipeConfig, load_recipe
from alloy.engine import Engine

TARGET = "tdd-loop-medium-no-context"


def escalating(**escalate):
    config = load_recipe("fast-track")
    raw = {"name": "fast-track", "escalate": {"to": TARGET, **escalate}}
    return replace(config, escalate=RecipeConfig.parse(raw).escalate)


def retry_entry(n):
    return verdict_entry(
        "retry",
        reason=f"attempt {n} not there yet",
        write=[{"path": "mypkg/__init__.py", "content": f"ATTEMPT = {n}\n"}],
    )


def harness_for(project, alloy_home, config):
    return make_harness(
        project,
        alloy_home,
        bead=make_bead(metadata={"alloy_recipe": "fast-track"}),
        config=config,
        recipe_name="fast-track",
    )


# -- config -------------------------------------------------------------------


def test_escalate_block_parses():
    spec = RecipeConfig.parse(
        {
            "name": "fast-track",
            "escalate": {
                "after_iterations": 2,
                "when_packages_touched_over": 1,
                "package_roots": ["packages/*"],
                "to": TARGET,
            },
        }
    ).escalate
    assert spec.after_iterations == 2
    assert spec.when_packages_touched_over == 1
    assert spec.package_roots == ("packages/*",)
    assert spec.to == TARGET


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"after_iterations": 2}, "to"),
        ({"to": TARGET}, "after_iterations"),
        ({"to": TARGET, "when_packages_touched_over": 1}, "package_roots"),
    ],
)
def test_escalate_block_errors(block, message):
    with pytest.raises(ConfigError, match=message):
        RecipeConfig.parse({"name": "fast-track", "escalate": block})


def test_escalation_is_off_by_default():
    assert load_recipe("fast-track").escalate is None


# -- graph --------------------------------------------------------------------


async def test_rounds_without_done_reroute_the_bead(project, alloy_home, fake_harnesses):
    fake_harnesses.configure({"implement": [retry_entry(1), retry_entry(2), retry_entry(3)]})
    harness = harness_for(project, alloy_home, escalating(after_iterations=2))
    final = await harness.start()

    assert "__interrupt__" not in final
    assert final["outcome"] == "cancelled"
    assert final["reroute"]["recipe"] == TARGET
    assert "2 implement round(s) ended without done" in final["reroute"]["reason"]
    assert len(fake_harnesses.calls_for("implement")) == 2


async def test_touching_more_package_roots_than_allowed_reroutes(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(
                    "test -f pkgs/a/x.py",
                    write=[
                        {"path": "pkgs/a/x.py", "content": "A = 1\n"},
                        {"path": "pkgs/b/y.py", "content": "B = 1\n"},
                        {"path": "mypkg/__init__.py", "content": IMPLEMENTATION},
                    ],
                ),
            ]
        }
    )
    config = escalating(when_packages_touched_over=1, package_roots=["pkgs/*", "mypkg"])
    harness = harness_for(project, alloy_home, config)
    final = await harness.start()

    assert final["outcome"] == "cancelled"
    reason = final["reroute"]["reason"]
    assert "3 package roots" in reason and "mypkg" in reason and "pkgs/a" in reason and "pkgs/b" in reason
    assert len(fake_harnesses.calls_for("implement")) == 1


async def test_within_bounds_the_run_finishes_as_usual(project, alloy_home, fake_harnesses):
    probe = "test -f mypkg/__init__.py"
    fake_harnesses.configure(
        {
            "implement": [
                run_check_entry(probe, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
                verdict_entry("done", reason="verified"),
                run_check_entry(probe, reason="final pass check"),
                verdict_entry("done", reason="final pass verified"),
            ]
        }
    )
    config = escalating(after_iterations=1, when_packages_touched_over=1, package_roots=["pkgs/*"])
    harness = harness_for(project, alloy_home, config)
    final = await harness.start()

    assert final["outcome"] == "done"
    assert final.get("reroute") is None


# -- engine -------------------------------------------------------------------


async def test_escalation_reroutes_like_alloy_reroute(beads_project, alloy_home, fake_harnesses, monkeypatch):
    config = escalating(after_iterations=1)
    engine = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(engine, "load_config", lambda name: config if name == "fast-track" else load_config())
    fake_harnesses.configure({"implement": [retry_entry(1), retry_entry(2)]})
    bead_id = bd_create(beads_project, "one-line fix", alloy_recipe="fast-track")

    result = await engine.run(bead_id)

    assert result.outcome == "cancelled"
    bead = engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_READY
    assert bead.recipe == TARGET
    assert (beads_project / "mypkg" / "__init__.py").read_text() == "ATTEMPT = 1\n", "the checkout's edits are kept"
    assert engine.store.get_run(result.run_id)["status"] == "cancelled"
    events = [json.loads(line) for line in (alloy_home / "events.jsonl").read_text().splitlines()]
    escalated = [e for e in events if e["event"] == "escalated"]
    assert len(escalated) == 1 and escalated[0]["to"] == TARGET and escalated[0]["from"] == "fast-track"
    notes = json.dumps(engine.beads._show_rows(bead_id))
    assert f"escalated from fast-track to {TARGET}" in notes
