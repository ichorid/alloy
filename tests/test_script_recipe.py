"""The `script` graph: a deterministic recipe with no agent calls.

`prepare:` commands run first, then every `checks:` entry on its own, in
order. A failing check gets one isolated rerun: green on the rerun is
recorded as flaky and the run goes on; red again parks the run for a human.
A green run finishes `done` and the engine commits it like any other recipe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest
from conftest import bd_create
from support import make_bead, make_harness

from alloy import beads as bd
from alloy.config import ConfigError, RecipeConfig
from alloy.engine import Engine
from alloy.models import utcnow

PY = sys.executable


def script_config(*, prepare=None, checks=None, limits=None, name="ship") -> RecipeConfig:
    raw = {
        "name": name,
        "graph": "script",
        "prepare": prepare or [],
        "checks": checks if checks is not None else [{"cmd": "test -f README.md", "purpose": "readme exists"}],
    }
    if limits:
        raw["limits"] = limits
    return RecipeConfig.parse(raw)


class NoteBeads:
    """The slice of BeadsClient the script graph touches."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, bead_id: str, text: str) -> None:
        self.notes.append(text)

    def set_metadata(self, bead_id: str, values: dict) -> None:
        pass


def harness_for(project, alloy_home, config, beads=None):
    return make_harness(
        project,
        alloy_home,
        bead=make_bead(metadata={"alloy_recipe": config.name}),
        config=config,
        recipe_name="script",
        beads=beads,
    )


def events(alloy_home):
    path = alloy_home / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- config -------------------------------------------------------------------


def test_script_recipe_parses_prepare_and_checks():
    config = script_config(
        prepare=[{"cmd": "bash scripts/bump.sh", "purpose": "bump version"}, "true-ish --flag"],
        checks=[{"cmd": "./run_tests.sh gates", "purpose": "gates", "timeout_minutes": 15}],
    )
    assert [p.cmd for p in config.prepare] == ["bash scripts/bump.sh", "true-ish --flag"]
    assert config.prepare[0].purpose == "bump version"
    assert config.checks[0].cmd == "./run_tests.sh gates"
    assert config.checks[0].timeout_minutes == 15


def test_script_recipe_without_checks_is_a_config_error():
    with pytest.raises(ConfigError, match="checks"):
        script_config(checks=[])


def test_a_check_entry_without_cmd_is_a_config_error():
    with pytest.raises(ConfigError, match="cmd"):
        script_config(checks=[{"purpose": "nothing to run"}])


# -- graph --------------------------------------------------------------------


async def test_prepare_then_each_check_runs_separately_and_the_run_finishes_done(project, alloy_home, fake_harnesses):
    config = script_config(
        prepare=[{"cmd": "printf 1.2.4 > VERSION", "purpose": "bump"}],
        checks=[
            {"cmd": "grep -q 1.2.4 VERSION", "purpose": "version bumped"},
            {"cmd": "test -f README.md && test -f VERSION", "purpose": "files exist"},
        ],
    )
    harness = harness_for(project, alloy_home, config)
    final = await harness.start()

    assert final["outcome"] == "done"
    assert [c["command"] for c in final["checks"]] == ["grep -q 1.2.4 VERSION", "test -f README.md && test -f VERSION"]
    assert all(c["exit_code"] == 0 for c in final["checks"])
    logs = sorted((alloy_home / "logs" / harness.run_id).glob("check-*.log"))
    # One log per prepare command and per check: never chained into one command.
    assert len(logs) == 3
    assert fake_harnesses.calls == [], "the script graph never calls an agent"


async def test_failing_prepare_parks_for_a_human_with_its_output(project, alloy_home, fake_harnesses):
    config = script_config(prepare=[{"cmd": "echo bump-broke >&2; exit 3", "purpose": "bump"}])
    harness = harness_for(project, alloy_home, config)
    final = await harness.start()

    assert "__interrupt__" in final
    payload = final["__interrupt__"][0].value
    assert "bump" in payload["reason"]
    assert "bump-broke" in payload["reason"]
    assert not final.get("checks"), "no check runs after a failed prepare step"


async def test_a_check_that_passes_on_its_isolated_rerun_is_recorded_flaky(
    project, alloy_home, fake_harnesses, tmp_path
):
    marker = tmp_path / "flaked-once"
    flaky = f"test -f {marker} || {{ touch {marker}; exit 1; }}"
    config = script_config(checks=[{"cmd": flaky, "purpose": "flaky suite"}, {"cmd": "test -f README.md"}])
    beads = NoteBeads()
    harness = harness_for(project, alloy_home, config, beads=beads)
    final = await harness.start()

    assert final["outcome"] == "done"
    assert [c["exit_code"] for c in final["checks"]] == [1, 0, 0]
    assert any("flaky" in note and "flaky suite" in note for note in beads.notes)
    flaky_events = [e for e in events(alloy_home) if e["event"] == "flaky-check"]
    assert len(flaky_events) == 1
    assert flaky_events[0]["command"] == flaky


async def test_a_check_red_twice_parks_naming_the_check_and_its_log(project, alloy_home, fake_harnesses):
    config = script_config(
        checks=[
            {"cmd": "test -f README.md"},
            {"cmd": "test -f no-such-gate", "purpose": "client gates"},
            {"cmd": "test -d mypkg"},
        ]
    )
    harness = harness_for(project, alloy_home, config)
    final = await harness.start()

    assert "__interrupt__" in final
    reason = final["__interrupt__"][0].value["reason"]
    assert "client gates" in reason
    last = final["checks"][-1]
    assert last["log_path"] and last["log_path"] in reason
    assert [c["command"] for c in final["checks"]] == [
        "test -f README.md",
        "test -f no-such-gate",
        "test -f no-such-gate",
    ]


async def test_resume_after_a_parked_check_reruns_that_check_and_never_the_prepare(
    project, alloy_home, fake_harnesses, tmp_path
):
    gate = tmp_path / "fixed"
    config = script_config(
        prepare=[{"cmd": "echo x >> BUMPS", "purpose": "bump"}],
        checks=[{"cmd": f"test -f {gate}", "purpose": "gate"}],
    )
    harness = harness_for(project, alloy_home, config)
    first = await harness.start()
    assert "__interrupt__" in first

    gate.write_text("ok")
    from langgraph.types import Command

    final = await harness.resume(Command(resume={"instructions": "fixed the gate"}))

    assert final["outcome"] == "done"
    assert (project / "BUMPS").read_text().count("x") == 1, "prepare must not run again on resume"


async def test_wall_time_counts_only_check_time(project, alloy_home, fake_harnesses):
    config = script_config(
        checks=[{"cmd": "test -f README.md"}, {"cmd": "test -d mypkg"}],
        limits={"max_wall_time_minutes": 1},
    )
    harness = harness_for(project, alloy_home, config)
    # The run row says it started two hours ago (a long queue wait, say): that
    # is not check time and must not stop the run.
    harness.store.update_run(harness.run_id, started_at=(utcnow() - timedelta(hours=2)).isoformat())
    final = await harness.start()
    assert final["outcome"] == "done"


async def test_check_time_past_max_wall_time_parks_before_the_next_check(project, alloy_home, fake_harnesses):
    slow = f'{PY} -c "import time; time.sleep(0.5)"'
    config = script_config(
        checks=[{"cmd": slow, "purpose": "slow"}, {"cmd": "test -d mypkg", "purpose": "never reached"}],
        limits={"max_wall_time_minutes": 0.005},
    )
    harness = harness_for(project, alloy_home, config)
    final = await harness.start()

    assert "__interrupt__" in final
    payload = final["__interrupt__"][0].value
    assert "max_wall_time" in payload["reason"]
    assert [c["command"] for c in final["checks"]] == [slow]


async def test_a_check_honours_its_own_timeout(project, alloy_home, fake_harnesses):
    slow = f'{PY} -c "import time; time.sleep(5)"'
    config = script_config(checks=[{"cmd": slow, "purpose": "hangs", "timeout_minutes": 0.005}])
    harness = harness_for(project, alloy_home, config)
    final = await harness.start()

    assert "__interrupt__" in final
    assert all(c["timed_out"] for c in final["checks"])


# -- engine -------------------------------------------------------------------


async def test_a_script_bead_commits_with_its_id_like_any_other_recipe(
    beads_project, alloy_home, fake_harnesses, monkeypatch
):
    config = script_config(
        prepare=[{"cmd": "printf 2.0.1 > VERSION", "purpose": "bump"}],
        checks=[{"cmd": "grep -q 2.0.1 VERSION", "purpose": "bumped"}],
    )
    engine = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(engine, "load_config", lambda name: config)
    bead_id = bd_create(beads_project, "ship 2.0.1", alloy_recipe="ship")

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert engine.beads.show(bead_id).status == bd.STATUS_DONE
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=beads_project, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert subject.startswith(f"{bead_id}:")
    assert engine.store.agent_calls(result.run_id) == []
