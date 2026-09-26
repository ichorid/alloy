"""`alloy recipes` complexity display/probe and `alloy status --json` complexity fields.

Acceptance tests for alloy-0uc.4. Behaviour is not implemented yet — these must
fail until recipes_command exports complexity, --probe smoke-tests tier entries,
status --json surfaces complexity fields, and AGENTS.md documents the workflow.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    estimate_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import make_bead, make_harness
from typer.testing import CliRunner

from alloy.cli import app

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"

PROBE_OK_CONFIG = {"unknown": {"text": "OK"}}

# Distinct (runner, model, effort) tier entries across tdd-loop's three tiers.
EXPECTED_PROBE_KEYS = {
    ("cursor", "composer-2.5", None),
    ("codex", "gpt-6-luna", None),
    ("claude-write", "haiku", "low"),
    ("codex", "gpt-6-sol", "high"),
    ("claude-write", "sonnet", "high"),
    ("cursor", "kimi-k3-high", None),
    ("claude-write", "opus", None),
    ("astra", None, None),
}


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


def _tdd_loop_recipe(payload: dict) -> dict:
    for entry in payload["recipes"]:
        if entry["name"] == "tdd-loop":
            return entry
    raise AssertionError("tdd-loop recipe not found in recipes output")


def _estimate_script(**overrides):
    base = {
        "context": context_entry(),
        "estimate": estimate_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


# -- alloy recipes --json complexity -----------------------------------------


def test_recipes_json_exports_landing_mode_and_target(project, alloy_home):
    result = _invoke("recipes", "--json", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    for entry in payload["recipes"]:
        landing = entry["landing"]
        assert landing["mode"] == "auto"
        assert landing["target"] == "main"


def test_recipes_json_exports_complexity_tiers(project, alloy_home, fake_harnesses):
    result = _invoke("recipes", "--json", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    entry = _tdd_loop_recipe(payload)
    complexity = entry["complexity"]

    assert complexity["routing"] == "live"
    assert complexity["escalate_after_retries"] == 2

    simple = complexity["tiers"]["simple"]
    assert len(simple) == 3
    assert simple[0] == {
        "runner": "cursor",
        "model": "composer-2.5",
        "effort": None,
        "available": fake_harnesses.bindir.joinpath("cursor-agent").exists(),
    }
    assert simple[2] == {
        "runner": "claude-write",
        "model": "haiku",
        "effort": "low",
        "available": fake_harnesses.bindir.joinpath("claude").exists(),
    }

    complex_tier = complexity["tiers"]["complex"]
    assert complex_tier[0] == {
        "runner": "cursor",
        "model": "kimi-k3-high",
        "effort": None,
        "available": fake_harnesses.bindir.joinpath("cursor-agent").exists(),
    }
    assert complex_tier[2]["runner"] == "astra"


def test_recipes_table_lists_routing_tiers_models_and_effort(project, alloy_home):
    result = _invoke("recipes", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    output = result.stdout
    for needle in (
        "live",
        "simple",
        "medium",
        "complex",
        "composer-2.5",
        "kimi-k3-high",
        "@low",
    ):
        assert needle in output


def test_recipes_json_exports_per_tier_max_agent_calls(project, alloy_home):
    """alloy recipes --json lists a higher max_agent_calls for complex than simple."""
    result = _invoke("recipes", "--json", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    entry = _tdd_loop_recipe(payload)
    by_tier = entry["limits"]["max_agent_calls_by_tier"]
    assert by_tier["complex"] > by_tier["simple"]
    assert by_tier["simple"] >= entry["limits"]["max_agent_calls"]


def test_recipes_table_shows_per_tier_max_agent_calls(project, alloy_home):
    result = _invoke("recipes", project=project, alloy_home=alloy_home)

    assert result.exit_code == 0
    assert "max_agent_calls_by_tier" in result.stdout


# -- alloy recipes --probe ---------------------------------------------------


def test_recipes_probe_json_smoke_tests_every_distinct_tier_entry(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(PROBE_OK_CONFIG)

    result = _invoke(
        "recipes",
        "--probe",
        "--json",
        "--recipe",
        "tdd-loop",
        project=project,
        alloy_home=alloy_home,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    probe = payload["probe"]
    assert len(probe) == len(EXPECTED_PROBE_KEYS)

    seen = {(row["runner"], row.get("model"), row.get("effort")) for row in probe}
    assert seen == EXPECTED_PROBE_KEYS
    assert all(row["ok"] for row in probe)

    haiku_calls = [call for call in fake_harnesses.calls if call["runner"] == "claude" and "haiku" in call["argv"]]
    assert haiku_calls
    assert "--effort" in haiku_calls[0]["argv"]
    assert "low" in haiku_calls[0]["argv"]


def test_recipes_probe_nonzero_exit_when_codex_missing(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(PROBE_OK_CONFIG)
    fake_harnesses.remove("codex")

    result = _invoke(
        "recipes",
        "--probe",
        "--json",
        "--recipe",
        "tdd-loop",
        project=project,
        alloy_home=alloy_home,
    )

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    codex_rows = [row for row in payload["probe"] if row["runner"] == "codex"]
    assert len(codex_rows) == 2
    assert all(not row["ok"] for row in codex_rows)
    assert all("not found" in (row.get("error") or "").lower() for row in codex_rows)


# -- alloy status --json complexity ------------------------------------------


async def test_status_json_shows_complexity_and_dispatch_tier_in_live_mode(beads_project, alloy_home, fake_harnesses):
    from alloy.engine import Engine

    fake_harnesses.configure(_estimate_script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    await engine.run(bead_id)

    result = _invoke("status", bead_id, "--json", project=beads_project, alloy_home=alloy_home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    row = payload["beads"][0]
    assert row["bead"] == bead_id
    assert row["complexity"] == "simple"
    assert row["dispatch_tier"] == "simple"


def test_status_json_null_complexity_when_estimate_never_ran(beads_project, alloy_home):
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    make_harness(beads_project, alloy_home, bead=make_bead(id=bead_id))

    result = _invoke("status", bead_id, "--json", project=beads_project, alloy_home=alloy_home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    row = payload["beads"][0]
    assert row["complexity"] is None


# -- AGENTS.md documentation -------------------------------------------------


def test_agents_md_documents_complexity_overrides_and_probe():
    text = AGENTS_MD.read_text(encoding="utf-8")
    for needle in (
        "alloy_complexity",
        "alloy_complexity_estimated",
        "tiered: true",
        "effort:",
        "--probe",
    ):
        assert needle in text
