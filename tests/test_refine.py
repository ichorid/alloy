"""The refine role: testable criteria for everything a vague bead names
(bench calibration D8: "Conditions and text" closed with only IF built)."""

from __future__ import annotations

from dataclasses import replace

from support import load_config, make_bead, make_harness
from workflow_support import script

from alloy.config import RoleSpec
from alloy.models import RefinedSpec
from alloy.recipes.workflow_nodes import REFINED_HEADING, refined_acceptance

ANSWER = {
    "decision": "refined",
    "criteria": ["slugify('A B') returns 'a-b'", "slugify('') returns ''"],
    "assumptions": ["an empty string stays empty"],
    "reason": "the description names empty input",
}


def _config():
    config = load_config()
    return replace(config, roles={**config.roles, "refine": RoleSpec(runner="claude", model="haiku")})


def test_refined_acceptance_keeps_the_operators_text_first():
    text = refined_acceptance("Slugs are lowercase.", RefinedSpec.model_validate(ANSWER))
    assert text.startswith("Slugs are lowercase.\n\n" + REFINED_HEADING)
    assert "- slugify('') returns ''" in text
    assert "Assumptions made where the description is vague:\n- an empty string stays empty" in text


async def test_refined_criteria_reach_the_tests_role(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script(refine=[{"structured": ANSWER}]))
    harness = make_harness(project, alloy_home, config=_config())
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["refined"]["decision"] == "refined"
    roles = [call["role"] for call in fake_harnesses.calls]
    assert roles.index("refine") < roles.index("estimate") < roles.index("tests")
    tests_prompt = fake_harnesses.calls_for("tests")[0]["prompt"]
    assert REFINED_HEADING in tests_prompt and "slugify('') returns ''" in tests_prompt
    assert any(line.startswith("refine: refined -- 2 criteria") for line in final["journal"])


async def test_a_refined_bead_is_not_refined_again(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script(refine=[{"structured": ANSWER}]))
    bead = make_bead(metadata={"alloy_refined": "refined"})
    harness = make_harness(project, alloy_home, bead=bead, config=_config())
    try:
        await harness.start()
    finally:
        harness.close()

    assert not fake_harnesses.calls_for("refine")


async def test_without_a_refine_role_nothing_changes(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert not fake_harnesses.calls_for("refine") and not final.get("refined")


class _Beads:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_acceptance(self, bead_id, text):
        self.calls.append(("acceptance", bead_id, text))

    def set_metadata(self, bead_id, values):
        self.calls.append(("metadata", bead_id, values))

    def add_label(self, bead_id, label):
        self.calls.append(("label", bead_id, label))

    def note(self, bead_id, text):
        self.calls.append(("note", bead_id, text))


async def test_refinement_is_written_to_the_bead(project, alloy_home, fake_harnesses):
    beads = _Beads()
    fake_harnesses.configure(script(refine=[{"structured": ANSWER}]))
    harness = make_harness(project, alloy_home, config=_config(), beads=beads)
    try:
        await harness.start()
    finally:
        harness.close()

    acceptance = [call for call in beads.calls if call[0] == "acceptance"]
    assert len(acceptance) == 1 and REFINED_HEADING in acceptance[0][2]
    assert ("metadata", acceptance[0][1], {"alloy_refined": "refined"}) in beads.calls


async def test_an_unclear_bead_is_labelled_and_the_run_goes_on(project, alloy_home, fake_harnesses):
    beads = _Beads()
    unclear = {"decision": "unclear", "criteria": [], "assumptions": [], "reason": "no goal stated"}
    fake_harnesses.configure(script(refine=[{"structured": unclear}]))
    harness = make_harness(project, alloy_home, config=_config(), beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert any(call[0] == "label" and call[2] == "alloy-needs-spec" for call in beads.calls)
    assert not any(call[0] == "acceptance" for call in beads.calls)
