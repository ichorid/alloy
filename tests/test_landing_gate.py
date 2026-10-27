"""The landing gate: before any recipe finishes a bead as done, Alloy runs the
recipe's `landing_gate:` checks whose path globs match what the bead's diff
touches -- on the final tree. A red gate check keeps the bead from finishing."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import IMPLEMENTATION, implement_entry
from support import load_config, make_bead, make_harness
from test_fast_track import done_entry, run_check_entry
from workflow_support import verification_script

from alloy.config import ConfigError, Limits, RecipeConfig, load_recipe
from alloy.recipes.landing_gate import gate_checks, path_matches

GATE_FILE = "mypkg/gate.txt"
GATE_CMD = f"test -f {GATE_FILE}"
PROBE = "test -f mypkg/__init__.py"


def gate(paths, cmd=GATE_CMD, purpose="package suite"):
    return RecipeConfig.parse(
        {"name": "x", "landing_gate": [{"paths": paths, "checks": [{"cmd": cmd, "purpose": purpose}]}]}
    ).landing_gate


# -- selection ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "glob", "expected"),
    [
        ("packages/client/lib/a.dart", "packages/client/**", True),
        ("packages/client", "packages/client/**", True),
        ("packages/server/lib/a.dart", "packages/client/**", False),
        ("packages/client/pubspec.yaml", "packages/*/pubspec.yaml", True),
        ("packages/client/lib/pubspec.yaml", "packages/*/pubspec.yaml", False),
        ("docs/a.md", "**/*.md", True),
        ("a.md", "**/*.md", True),
        ("scripts/run.sh", "scripts/*", True),
        ("scripts/sub/run.sh", "scripts/*", False),
    ],
)
def test_path_globs(path, glob, expected):
    assert path_matches(path, glob) is expected


def test_every_matching_entry_is_selected_once_in_order():
    config = RecipeConfig.parse(
        {
            "name": "x",
            "landing_gate": [
                {"paths": ["packages/client/**"], "checks": [{"cmd": "client-gates"}, {"cmd": "shared"}]},
                {"paths": ["packages/server/**"], "checks": [{"cmd": "server-suite"}]},
                {"paths": ["packages/*/pubspec.yaml"], "checks": [{"cmd": "shared"}, {"cmd": "pub-check"}]},
            ],
        }
    )
    selected = gate_checks(config.landing_gate, ["packages/client/pubspec.yaml", "README.md"])
    assert [spec.cmd for spec in selected] == ["client-gates", "shared", "pub-check"]
    assert gate_checks(config.landing_gate, ["README.md"]) == []


def test_landing_gate_entries_need_paths_and_checks():
    with pytest.raises(ConfigError, match="paths"):
        RecipeConfig.parse({"name": "x", "landing_gate": [{"checks": [{"cmd": "x"}]}]})
    with pytest.raises(ConfigError, match="checks"):
        RecipeConfig.parse({"name": "x", "landing_gate": [{"paths": ["a/**"]}]})


def test_no_landing_gate_by_default():
    assert load_config().landing_gate == ()


# -- fast-track ---------------------------------------------------------------


def fast_track(paths, **limits):
    config = load_recipe("fast-track")
    config = replace(config, landing_gate=gate(paths))
    if limits:
        config = replace(config, limits=replace(config.limits, **limits))
    return config


def harness_for(project, alloy_home, config, recipe_name):
    return make_harness(
        project,
        alloy_home,
        bead=make_bead(metadata={"alloy_recipe": recipe_name}),
        config=config,
        recipe_name=recipe_name,
    )


GREEN_FAST_TRACK = [
    run_check_entry(PROBE, write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
    done_entry(reason="verified"),
    run_check_entry(PROBE, reason="final pass check"),
    done_entry(reason="final pass verified"),
]


async def test_fast_track_red_gate_goes_back_to_the_agent_as_a_failed_check(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        {
            "implement": [
                *GREEN_FAST_TRACK,
                run_check_entry(GATE_CMD, write=[{"path": GATE_FILE, "content": "ok"}]),
                done_entry(reason="gate fixed"),
                run_check_entry(PROBE, reason="final pass check"),
                done_entry(reason="final pass verified"),
            ]
        }
    )
    harness = harness_for(project, alloy_home, fast_track(["mypkg/**"], max_iterations=20), "fast-track")
    final = await harness.start()

    assert final["outcome"] == "done"
    gate_runs = [c for c in final["checks"] if c["command"] == GATE_CMD and c["purpose"] == "package suite"]
    assert [c["exit_code"] for c in gate_runs] == [1, 0]
    after_gate = fake_harnesses.calls_for("implement")[4]["prompt"]
    assert "landing gate" in after_gate and GATE_CMD in after_gate


async def test_a_gate_whose_paths_the_diff_does_not_touch_never_runs(project, alloy_home, fake_harnesses):
    fake_harnesses.configure({"implement": GREEN_FAST_TRACK})
    harness = harness_for(project, alloy_home, fast_track(["packages/server/**"]), "fast-track")
    final = await harness.start()

    assert final["outcome"] == "done"
    assert all(c["command"] != GATE_CMD for c in final["checks"])


async def test_a_red_gate_at_the_iteration_limit_parks_for_a_human(project, alloy_home, fake_harnesses):
    fake_harnesses.configure({"implement": GREEN_FAST_TRACK})
    harness = harness_for(project, alloy_home, fast_track(["mypkg/**"], max_iterations=4), "fast-track")
    final = await harness.start()

    assert "__interrupt__" in final
    payload = final["__interrupt__"][0].value
    assert "landing gate" in payload["reason"] and GATE_CMD in payload["reason"]
    assert final.get("outcome") is None


# -- tdd-loop -----------------------------------------------------------------


async def test_tdd_loop_red_gate_is_a_new_implement_iteration(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        verification_script(
            implement=[
                implement_entry(succeed=True),
                {
                    "text": "Added the gate file.",
                    "write": [
                        {"path": "mypkg/__init__.py", "content": IMPLEMENTATION},
                        {"path": GATE_FILE, "content": "ok"},
                    ],
                },
            ]
        )
    )
    harness = harness_for(project, alloy_home, load_config(landing_gate=gate(["mypkg/**"])), "tdd-loop")
    final = await harness.start()

    assert final["outcome"] == "done"
    assert final["iteration"] == 2
    second = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert "landing gate" in second and GATE_CMD in second


async def test_budget_landing_also_needs_the_gate(project, alloy_home, fake_harnesses):
    from conftest import acceptance_entry, verifier_run_entry, verifier_stop_entry
    from workflow_support import FULL_SUITE

    fake_harnesses.configure(
        verification_script(
            verifier=[verifier_run_entry(FULL_SUITE, kind="regression"), verifier_stop_entry("green")],
            acceptance=[acceptance_entry("verify_more", "would like an extra lint run")],
        )
    )
    limits = Limits(max_iterations=5, max_consiliums=0, max_agent_calls=100, max_cheap_agent_calls=4)
    config = load_config(limits=limits, landing_gate=gate(["mypkg/**"]))
    harness = harness_for(project, alloy_home, config, "tdd-loop")
    final = await harness.start()

    assert "__interrupt__" in final, "a red gate must keep a budget stop from landing"
    assert final.get("budget_landed") is None
    assert GATE_CMD in final["__interrupt__"][0].value["reason"]


# -- which paths count --------------------------------------------------------


def _commit(project, files):
    import subprocess

    for path, content in files.items():
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=project, check=True)


def _worktree(project):
    from alloy.worktree import Worktree, WorktreeManager

    manager = WorktreeManager(repo=project, root=project.parent / "wt")
    return manager, Worktree("t-1", project, "", manager.head(project))


@pytest.mark.parametrize("staged", [False, True])
def test_a_moved_file_counts_under_its_old_and_new_path(project, staged):
    import subprocess

    _commit(project, {"packages/server/lib/x.dart": "class X {}\n" * 20})
    manager, worktree = _worktree(project)
    (project / "packages/shared").mkdir(parents=True)
    if staged:
        subprocess.run(["git", "mv", "packages/server/lib/x.dart", "packages/shared/x.dart"], cwd=project, check=True)
    else:
        (project / "packages/server/lib/x.dart").rename(project / "packages/shared/x.dart")

    changed = manager.changed_files(worktree)
    assert "packages/server/lib/x.dart" in changed and "packages/shared/x.dart" in changed


def test_lockfiles_count_for_gate_selection_but_stay_out_of_the_default_list(project):
    _commit(project, {"packages/web/pnpm-lock.yaml": "lockfileVersion: 1\n"})
    manager, worktree = _worktree(project)
    (project / "packages/web/pnpm-lock.yaml").write_text("lockfileVersion: 2\n")

    assert manager.changed_files(worktree) == []
    assert manager.changed_files(worktree, keep_lockfiles=True) == ["packages/web/pnpm-lock.yaml"]


async def test_moving_a_file_out_of_a_gated_package_still_runs_its_gate(project, alloy_home, fake_harnesses):
    _commit(project, {"mypkg/old.py": "OLD = 1\n" * 20})
    move = {"write": [{"path": "other/old.py", "content": "OLD = 1\n" * 20}], "delete": ["mypkg/old.py"]}
    fake_harnesses.configure(
        {
            "implement": [
                {**run_check_entry("test -f other/old.py"), **move},
                done_entry(reason="verified"),
                run_check_entry("test -f other/old.py", reason="final pass check"),
                done_entry(reason="final pass verified"),
            ]
        }
    )
    config = fast_track(["mypkg/**"])
    config = replace(config, landing_gate=gate(["mypkg/**"], cmd="test -f other/old.py"))
    harness = harness_for(project, alloy_home, config, "fast-track")
    final = await harness.start()

    assert final["outcome"] == "done"
    assert any(c["purpose"] == "package suite" for c in final["checks"]), "the mypkg gate must have run"
