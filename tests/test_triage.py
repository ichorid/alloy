"""Bug triage node (alloy-0uc.7): route untriaged <bug> reports through Jev before verify."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from conftest import (
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    triage_entry,
    write_tests_entry,
)
from langgraph.types import Command
from support import load_config, make_bead, make_harness

from alloy import beads as bd
from alloy.config import RoleSpec


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "triage": [triage_entry("non-blocking")],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


def triage_config(**overrides) -> object:
    """Recipe config with a triage role (jev primary, cursor fallback)."""
    config = load_config()
    roles = dict(config.roles)
    fallback = overrides.pop(
        "fallback",
        RoleSpec(runner="cursor", model="composer-2.5", timeout_minutes=5),
    )
    roles["triage"] = RoleSpec(
        runner=overrides.pop("runner", "jev"),
        model=overrides.pop("model", "jev-latest"),
        timeout_minutes=5,
        fallback=fallback,
    )
    return replace(config, roles=roles)


def bug_block(
    title: str,
    *,
    where: str = "mypkg/__init__.py:1",
    evidence: str = "noticed while editing",
    blocks_task: str = "no",
) -> str:
    return f"""<bug>
title: {title}
where: {where}
evidence: {evidence}
blocks_task: {blocks_task}
</bug>"""


def implement_with_bugs(
    *bugs: str,
    succeed: bool = True,
) -> dict:
    entry = implement_entry(succeed=succeed)
    if not bugs:
        return entry
    return {**entry, "text": f"{entry['text']}\n\n" + "\n\n".join(bugs)}


def implement_stopped_with_bug(title: str) -> dict:
    return {
        "text": f"Cannot continue until this is resolved.\n\n{bug_block(title, blocks_task='yes')}",
    }


class RecordingBeadsClient:
    """Records create_bug / add_dependency for harness-level triage tests."""

    def __init__(self, bug_ids: list[str] | None = None) -> None:
        self.create_bug_calls: list[dict] = []
        self.add_dependency_calls: list[tuple[str, str]] = []
        self._ids = bug_ids or ["bug-001", "bug-002", "bug-003", "bug-004"]
        self._counter = 0

    def create_bug(self, **kwargs) -> str:
        self.create_bug_calls.append(kwargs)
        bug_id = self._ids[min(self._counter, len(self._ids) - 1)]
        self._counter += 1
        return bug_id

    def add_dependency(self, bead_id: str, depends_on_id: str) -> None:
        self.add_dependency_calls.append((bead_id, depends_on_id))

    def note(self, bead_id: str, text: str) -> None:
        pass


def _triage_ledger_rows(harness) -> list[dict]:
    return [c for c in harness.store.agent_calls(harness.run_id) if c["role"] == "triage"]


def _implement_ledger_rows(harness) -> list[dict]:
    return [c for c in harness.store.agent_calls(harness.run_id) if c["role"] == "implement"]


def _agent_roles(fake_harnesses) -> list[str]:
    return [call["role"] for call in fake_harnesses.calls]


# -- non-blocking: file at P3, continue to verify, run ends done ----------------


async def test_non_blocking_bug_is_filed_and_run_completes(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient()
    bead = make_bead(
        id="parent-1",
        metadata={"alloy_recipe": "tdd-loop", "alloy_test_cmd": "pytest -q"},
    )
    fake_harnesses.configure(
        script(
            implement=[
                implement_with_bugs(bug_block("Unrelated legacy typo")),
            ],
            triage=[triage_entry("non-blocking", "pre-existing defect")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), bead=bead, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert len(_triage_ledger_rows(harness)) == 1
    assert len(beads.create_bug_calls) == 1

    filed = beads.create_bug_calls[0]
    assert filed["priority"] == 3
    assert filed["labels"] == [bd.LABEL_BUG]
    assert filed["claim"] is False
    assert filed["discovered_from"] == "parent-1"
    assert beads.add_dependency_calls == []

    roles = _agent_roles(fake_harnesses)
    assert roles.count("implement") == 1
    assert roles.index("triage") > roles.index("implement")
    assert roles.index("judge") > roles.index("triage")


# -- blocking: file at P1 claimed, route to remediate; no remediator -> human gate --


async def test_blocking_bug_routes_to_remediate_and_parks_without_remediator(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient(bug_ids=["bug-blocking"])
    bead = make_bead(id="parent-2", metadata={"alloy_recipe": "tdd-loop"})
    fake_harnesses.configure(
        script(
            implement=[implement_stopped_with_bug("Race in worker pool")],
            triage=[triage_entry("blocking", "reproduces on CI")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), bead=bead, beads=beads)
    try:
        paused = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in paused
    assert len(_triage_ledger_rows(harness)) == 1
    assert len(beads.create_bug_calls) == 1

    filed = beads.create_bug_calls[0]
    assert filed["priority"] == 1
    assert filed["labels"] == [bd.LABEL_BUG]
    assert filed["claim"] is True
    assert "no longer reproduces" in filed["acceptance"]

    assert harness.store.get_run(harness.run_id)["stage"] == "remediate:bug-blocking"
    remediations = paused.get("remediations") or []
    assert [(r["bead_id"], r["outcome"]) for r in remediations] == [("bug-blocking", "failed")]
    reason = paused["__interrupt__"][0].value["reason"]
    assert "bug-blocking" in reason


# -- needs-human: file with human label, block parent, resume -> implement --------


async def test_needs_human_blocks_parent_and_resume_returns_to_implement(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient(bug_ids=["bug-human"])
    bead = make_bead(id="parent-3", metadata={"alloy_recipe": "tdd-loop"})
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("Public API must change"),
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("needs-human", "schema change required")],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), bead=bead, beads=beads)
    try:
        paused = await harness.start()
        assert "__interrupt__" in paused
        reason = paused["__interrupt__"][0].value["reason"]
        assert "bug-human" in reason
        assert "Public API must change" in reason

        final = await harness.resume(Command(resume={"instructions": "architect approved"}))
    finally:
        harness.close()

    filed = beads.create_bug_calls[0]
    assert bd.LABEL_HUMAN in filed["labels"]
    assert beads.add_dependency_calls == [("parent-3", "bug-human")]

    roles = _agent_roles(fake_harnesses)
    triage_idx = roles.index("triage")
    resume_implement_idx = roles.index("implement", triage_idx + 1)
    assert resume_implement_idx > triage_idx
    assert final["outcome"] == "done"


# -- not-a-bug: implementer stopped, re-run implement with rejection text ---------


async def test_not_a_bug_re_runs_implement_with_rejection_instruction(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient()
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("Failing slugify test"),
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("not-a-bug", "this is the task under test")],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert beads.create_bug_calls == []
    roles = _agent_roles(fake_harnesses)
    assert roles.count("implement") == 2
    assert roles.index("triage") < roles.index("implement", roles.index("triage"))
    second_prompt = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert "Triage rejected your report" in second_prompt


# -- duplicate: title already filed, nothing new filed, run proceeds --------------


async def test_duplicate_verdict_files_nothing_and_run_proceeds(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient()
    fake_harnesses.configure(
        script(
            implement=[implement_with_bugs(bug_block("Already filed defect"))],
            triage=[triage_entry("duplicate", "matches bug-001")],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(
        project,
        alloy_home,
        config=triage_config(),
        beads=beads,
        initial_state_overrides={
            "filed_bugs": [
                {
                    "bead_id": "bug-001",
                    "title": "Already filed defect",
                    "where": "legacy.py:9",
                    "severity": "non-blocking",
                }
            ],
        },
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert beads.create_bug_calls == []
    assert len(_triage_ledger_rows(harness)) == 1


# -- triage prompt lists filed bugs and remediations for later reports ------------


async def test_triage_prompt_lists_filed_bugs_and_remediations(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient(bug_ids=["bug-first", "bug-second"])
    fake_harnesses.configure(
        script(
            implement=[
                implement_with_bugs(
                    bug_block("First defect"),
                    bug_block("Second defect"),
                ),
            ],
            triage=[
                triage_entry("non-blocking", "file first"),
                triage_entry("non-blocking", "file second"),
            ],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(
        project,
        alloy_home,
        config=triage_config(),
        beads=beads,
        initial_state_overrides={
            "remediations": [
                {"bead_id": "rem-1", "outcome": "merged on parent branch"},
            ],
        },
    )
    try:
        await harness.start()
    finally:
        harness.close()

    triage_prompts = [call["prompt"] for call in fake_harnesses.calls_for("triage")]
    assert len(triage_prompts) == 2
    second_prompt = triage_prompts[1]
    assert "bug-first" in second_prompt
    assert "First defect" in second_prompt
    assert "rem-1" in second_prompt
    assert "merged on parent branch" in second_prompt


# -- triage runner missing: human gate, nothing filed -----------------------------


async def test_missing_triage_runner_parks_at_human_gate_without_filing(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient()
    config = triage_config(
        runner="missing-triage-runner",
        fallback=None,
    )
    fake_harnesses.configure(
        script(
            implement=[implement_with_bugs(bug_block("Harness outage defect"))],
        )
    )
    harness = make_harness(project, alloy_home, config=config, beads=beads)
    try:
        paused = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in paused
    assert "Harness outage defect" in paused["__interrupt__"][0].value["reason"]
    assert beads.create_bug_calls == []
    assert _triage_ledger_rows(harness) == []


# -- same title in iteration 2: no second triage or create_bug -------------------


async def test_same_bug_title_in_iteration_two_skips_re_triage(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient(bug_ids=["bug-once"])
    title = "Flaky import in utils"
    fake_harnesses.configure(
        script(
            implement=[
                implement_with_bugs(bug_block(title)),
                implement_with_bugs(bug_block(title)),
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("non-blocking")],
            judge=[judge_entry("retry", "still red"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert len(_triage_ledger_rows(harness)) == 1
    assert len(beads.create_bug_calls) == 1


# -- runs without <bug> blocks never invoke triage --------------------------------


async def test_happy_path_has_no_triage_row(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home, config=triage_config())
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert "triage" not in _agent_roles(fake_harnesses)
    assert _triage_ledger_rows(harness) == []


# -- AGENTS.md documents the bug protocol -----------------------------------------


def test_agents_md_documents_bug_triage_protocol():
    text = Path(__file__).resolve().parents[1].joinpath("AGENTS.md").read_text(encoding="utf-8")
    for needle in (
        "<bug>",
        "not-a-bug",
        "needs-human",
        "discovered-from",
        ".alloy/project.md",
        "bd human list",
    ):
        assert needle in text
