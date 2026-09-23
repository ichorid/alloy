"""`detail_lines` in `alloy.monitor.render`: the selected-run detail pane content.

Content rules come from the "Panels" paragraph (Component 3) and the
raw-vs-effective judge rules (Component 1, item 3) in
docs/plans/execution-monitor.md. Pure function, no Textual involved -- see
tests/test_monitor_view.py for the pilot tests that show/hide the pane.
"""

from __future__ import annotations

from alloy.monitor.render import detail_lines

EMPTY_TOKENS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None}


def _run(
    *,
    current_calls=(),
    tokens_by_role=None,
    models_used=None,
    judge=None,
    worktree="/home/vader/.alloy/worktrees/alloy-a1b2",
    branch="alloy/alloy-a1b2",
) -> dict:
    return {
        "bead_id": "alloy-a1b2",
        "run_id": "run-1",
        "recipe": "tdd-loop-jev",
        "status": "running",
        "stage": "implement",
        "iteration": 2,
        "max_iterations": 5,
        "consiliums": 0,
        "max_consiliums": 1,
        "tests_summary": None,
        "elapsed_minutes": 7,
        "current_calls": list(current_calls),
        "tokens": dict(EMPTY_TOKENS),
        "tokens_by_role": tokens_by_role or {},
        "models_used": models_used or [],
        "judge": judge,
        "worktree": worktree,
        "branch": branch,
    }


def _call(role="implement", requested_runner="astra", effective_runner="codex",
          requested_model=None, effective_model=None, elapsed_seconds=42.1) -> dict:
    return {
        "role": role,
        "requested_runner": requested_runner,
        "effective_runner": effective_runner,
        "requested_model": requested_model,
        "effective_model": effective_model,
        "elapsed_seconds": elapsed_seconds,
    }


def _judge(raw_decision="retry", raw_confidence=0.61, effective_decision="retry",
           effective_reason="a specific fix remains", matches=True) -> dict:
    return {
        "raw": {"decision": raw_decision, "confidence": raw_confidence},
        "effective": {"decision": effective_decision, "reason": effective_reason},
        "matches_effective": matches,
    }


# -- current_calls --------------------------------------------------------


def test_two_inflight_calls_with_differing_requested_and_effective_runner():
    calls = [
        _call(role="implement", requested_runner="astra", effective_runner="codex",
              elapsed_seconds=42.1),
        _call(role="critic", requested_runner="claude", effective_runner="cursor",
              elapsed_seconds=3.9),
    ]
    lines = detail_lines(_run(current_calls=calls))

    implement_lines = [line for line in lines if line.startswith("implement:")]
    critic_lines = [line for line in lines if line.startswith("critic:")]
    assert len(implement_lines) == 1
    assert len(critic_lines) == 1

    implement_line = implement_lines[0]
    assert "astra" in implement_line
    assert "codex" in implement_line
    assert "->" in implement_line
    assert "42s" in implement_line

    critic_line = critic_lines[0]
    assert "claude" in critic_line
    assert "cursor" in critic_line
    assert "3s" in critic_line


def test_current_call_includes_requested_and_effective_model_when_present():
    calls = [_call(role="implement", requested_runner="astra", requested_model="o3",
                    effective_runner="codex", effective_model="gpt-5", elapsed_seconds=10.0)]

    lines = detail_lines(_run(current_calls=calls))

    line = next(line for line in lines if line.startswith("implement:"))
    assert "astra:o3" in line
    assert "codex:gpt-5" in line


def test_no_current_calls_yields_no_call_lines():
    lines = detail_lines(_run(current_calls=[]))

    assert not any("->" in line for line in lines)


# -- judge ------------------------------------------------------------------


def test_matching_raw_and_effective_judge_is_a_single_line():
    lines = detail_lines(_run(judge=_judge(
        raw_decision="done", effective_decision="done", matches=True,
    )))

    judge_lines = [line for line in lines if "done" in line]
    assert len(judge_lines) == 1
    assert "judge said" not in judge_lines[0]
    assert "Alloy did" not in judge_lines[0]


def test_differing_raw_and_effective_judge_shows_both_with_confidence_and_reason():
    lines = detail_lines(_run(judge=_judge(
        raw_decision="done", raw_confidence=0.61, effective_decision="retry",
        effective_reason="tests are red", matches=False,
    )))

    judge_line = next(line for line in lines if "judge said" in line)
    assert "done" in judge_line
    assert "0.61" in judge_line
    assert "Alloy did" in judge_line
    assert "retry" in judge_line
    assert "tests are red" in judge_line


def test_raw_null_with_effective_set_yields_no_parseable_verdict():
    judge = {
        "raw": {"decision": None, "confidence": None},
        "effective": {"decision": "retry", "reason": "tests are red"},
        "matches_effective": False,
    }

    lines = detail_lines(_run(judge=judge))

    assert "judge: no parseable verdict" in lines


def test_judge_none_does_not_mention_judge_said_or_no_parseable_verdict():
    lines = detail_lines(_run(judge=None))

    assert not any("judge said" in line for line in lines)
    assert not any("no parseable verdict" in line for line in lines)


# -- tokens_by_role -----------------------------------------------------------


def test_tokens_by_role_has_one_line_per_role_with_total_and_in_out_split():
    tokens_by_role = {
        "implement": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": 0.01},
        "critic": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "cost_usd": 0.002},
    }

    lines = detail_lines(_run(tokens_by_role=tokens_by_role))

    implement_line = next(line for line in lines if line.startswith("implement:"))
    assert "150" in implement_line
    assert "100" in implement_line
    assert "50" in implement_line

    critic_line = next(line for line in lines if line.startswith("critic:"))
    assert "30" in critic_line
    assert "20" in critic_line
    assert "10" in critic_line


def test_empty_tokens_by_role_yields_no_token_lines_and_does_not_raise():
    lines = detail_lines(_run(tokens_by_role={}))

    assert isinstance(lines, list)


# -- worktree / branch --------------------------------------------------------


# -- models_used (alloy-w9d.10) -----------------------------------------------


def test_detail_lines_models_used_includes_runner_model_calls_tokens_and_windows():
    from alloy.limits import window

    models_used = [{
        "runner": "claude-write",
        "model": "fable",
        "calls": 3,
        "total_tokens": 12000,
        "windows": {
            "five_hour": window("five_hour", "5h", 42.0, None),
            "seven_day": window("seven_day", "weekly", 61.0, None),
            "seven_day_fable": window("seven_day_fable", "weekly fable", 12.0, None, model="fable"),
        },
    }]

    lines = detail_lines(_run(models_used=models_used))

    model_line = next(line for line in lines if "claude-write:fable" in line)
    assert "calls 3" in model_line
    assert "12000" in model_line
    assert "5h 42%" in model_line
    assert "weekly fable 12%" in model_line


def test_detail_lines_models_used_with_empty_windows_has_no_percent():
    models_used = [{
        "runner": "codex",
        "model": "gpt-5",
        "calls": 1,
        "total_tokens": 100,
        "windows": {},
    }]

    lines = detail_lines(_run(models_used=models_used))

    model_line = next(line for line in lines if "codex:gpt-5" in line)
    assert "%" not in model_line


# -- worktree / branch --------------------------------------------------------


def test_worktree_and_branch_appear_in_the_detail_lines():
    lines = detail_lines(_run(
        worktree="/home/vader/.alloy/worktrees/alloy-a1b2",
        branch="alloy/alloy-a1b2",
    ))

    text = "\n".join(lines)
    assert "/home/vader/.alloy/worktrees/alloy-a1b2" in text
    assert "alloy/alloy-a1b2" in text
