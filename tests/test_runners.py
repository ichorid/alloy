"""Adapters normalize four different CLIs into one result shape."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from types import SimpleNamespace

from alloy.config import RoleSpec
from alloy.models import RunnerUnavailable
from alloy.runners import RunnerRegistry
from alloy.runners.base import extract_json_object, schema_instructions
from alloy.runtime import RunContext
from alloy.store import Store
from support import make_bead


SCHEMA = {"type": "object", "properties": {"decision": {"type": "string"}}}


async def test_claude_adapter_returns_native_structured_output(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"judge": {"structured": {"decision": "done"}}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("claude").run(
        "You are judging whether this is complete.", project, structured_schema=SCHEMA
    )
    assert result.ok
    assert result.structured == {"decision": "done"}
    assert result.usage["output_tokens"] == 20
    assert result.runner == "claude"
    assert Path(result.log_path).exists()


async def test_codex_adapter_reads_jsonl_stream(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"text": "changed one file"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project)
    assert result.ok
    assert result.text == "changed one file"
    assert result.usage["input_tokens"] == 100
    assert result.session_id == "fake-thread"


async def test_cursor_adapter_reads_result_envelope(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"context": {"text": "a python package"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("cursor").run("You are gathering context.", project)
    assert result.ok
    assert result.text == "a python package"


async def test_cursor_is_error_envelope_with_exit_zero_is_a_failed_call(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"context": {"exit": 0, "is_error": True, "text": "rate limited"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("cursor").run("You are gathering context.", project)
    assert not result.ok
    assert result.exit_code == 0
    assert "rate limited" in (result.error or "")


async def test_cursor_success_envelope_with_exit_zero_stays_ok(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"context": {"exit": 0, "is_error": False, "text": "all good"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("cursor").run("You are gathering context.", project)
    assert result.ok
    assert result.text == "all good"


async def test_codex_error_event_without_agent_message_and_exit_zero_fails(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {
            "implement": {
                "exit": 0,
                "codex_error": "Your workspace is out of credits.",
                "codex_error_only": True,
            }
        }
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project)
    assert not result.ok
    assert result.exit_code == 0
    assert "out of credits" in (result.error or "")


async def test_codex_agent_message_after_error_event_with_exit_zero_succeeds(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {
            "implement": {
                "exit": 0,
                "codex_error": "transient glitch",
                "text": "recovered answer",
            }
        }
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project)
    assert result.ok
    assert result.text == "recovered answer"


async def test_claude_is_error_envelope_with_exit_zero_is_a_failed_call(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"judge": {"exit": 0, "is_error": True, "text": "boom"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("claude").run("You are judging whether this is complete.", project)
    assert not result.ok
    assert result.exit_code == 0
    assert "boom" in (result.error or "")


async def test_structured_output_recovered_from_unstructured_harness(fake_harnesses, project, tmp_path):
    """Codex has no --json-schema, so the schema rides in the prompt and the
    JSON is parsed back out of the answer."""
    fake_harnesses.configure({"judge": {"structured": {"decision": "retry"}}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run(
        "You are judging whether this is complete.", project, structured_schema=SCHEMA
    )
    assert result.structured == {"decision": "retry"}
    call = fake_harnesses.calls_for("judge")[0]
    assert "decision" in call["prompt"]  # the schema was appended to the prompt


async def test_nonzero_exit_is_a_failed_result_not_an_exception(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"exit": 2, "stderr": "codex: rate limited", "text": ""}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project)
    assert not result.ok
    assert result.exit_code == 2
    assert "rate limited" in result.error


async def test_codex_error_event_leads_the_failure_message(fake_harnesses, project, tmp_path):
    """codex prints chatter on stderr; the JSONL error event is the real reason."""
    fake_harnesses.configure(
        {
            "implement": {
                "exit": 1,
                "stderr": "Reading additional input from stdin...\n",
                "text": "Your workspace is out of credits.",
            }
        }
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project)
    assert result.error.startswith("Your workspace is out of credits.")


async def test_timeout_is_reported_not_raised(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"sleep": 5}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project, timeout=timedelta(seconds=1))
    assert not result.ok
    assert "timed out" in result.error


async def test_missing_binary_raises_runner_unavailable(fake_harnesses, project):
    fake_harnesses.remove("pi")
    registry = RunnerRegistry()
    assert not registry.available("pi")
    with pytest.raises(RunnerUnavailable):
        await registry.get("pi").run("hello", project)


async def test_agents_always_run_inside_the_given_directory(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"text": "ok"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    await registry.get("codex").run("Implement the smallest change.", project)
    assert fake_harnesses.calls[0]["cwd"] == str(project.resolve())


def test_alias_and_registry_resolution():
    registry = RunnerRegistry()
    assert registry.get("astra").name == "codex"
    assert registry.get("cursor-plan").read_only is True
    assert registry.get("codex-readonly").sandbox == "read-only"


def test_generic_runner_can_be_defined_purely_in_config(tmp_path):
    registry = RunnerRegistry({"myagent": {"binary": "echo", "args": ["{prompt}"]}})
    runner = registry.get("myagent")
    assert runner.name == "myagent"
    assert runner.build_command("hi", model=None, structured_schema=None) == ["hi"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('prose\n```json\n{"a": 2}\n```\nmore', {"a": 2}),
        ('before {"a": 3} after', {"a": 3}),
        ('{"a": {"b": "}"}}', {"a": {"b": "}"}}),
        ("no json here", None),
    ],
)
def test_json_extraction_survives_chatty_agents(text, expected):
    assert extract_json_object(text) == expected


def test_schema_instructions_include_the_schema():
    assert "decision" in schema_instructions(SCHEMA)


ROLE_PROMPT = "You are judging whether this is complete."


@pytest.mark.parametrize(
    "runner_name",
    ["codex", "cursor", "cursor-plan", "codex-readonly"],
)
def test_build_prompt_prepends_schema_for_non_native_runners(runner_name):
    runner = RunnerRegistry().get(runner_name)
    built = runner.build_prompt(ROLE_PROMPT, SCHEMA)
    schema_block = schema_instructions(SCHEMA)
    assert built.startswith(schema_block)
    assert built.endswith(ROLE_PROMPT)


def test_claude_build_prompt_unchanged_with_structured_schema():
    runner = RunnerRegistry().get("claude")
    assert runner.build_prompt(ROLE_PROMPT, SCHEMA) == ROLE_PROMPT
    assert runner.build_prompt(ROLE_PROMPT, None) == ROLE_PROMPT


def test_extract_json_object_parses_fixture_with_schema_preamble():
    output = schema_instructions(SCHEMA) + '{"decision": "done"}'
    assert extract_json_object(output) == {"decision": "done"}


async def test_text_schema_runner_prompt_starts_with_schema_instructions(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"judge": {"structured": {"decision": "retry"}}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    await registry.get("codex").run(ROLE_PROMPT, project, structured_schema=SCHEMA)
    call = fake_harnesses.calls_for("judge")[0]
    schema_block = schema_instructions(SCHEMA)
    assert call["prompt"].startswith(schema_block)
    assert call["prompt"].endswith(ROLE_PROMPT)


def test_claude_build_command_includes_effort_when_set():
    runner = RunnerRegistry().get("claude")
    argv = runner.build_command("hi", model="haiku", effort="low", structured_schema=None)
    idx = argv.index("--effort")
    assert argv[idx + 1] == "low"


def test_claude_build_command_omits_effort_when_none():
    runner = RunnerRegistry().get("claude")
    argv = runner.build_command("hi", model="haiku", effort=None, structured_schema=None)
    assert "--effort" not in argv


def test_codex_build_command_includes_model_reasoning_effort_when_set():
    runner = RunnerRegistry().get("codex")
    argv = runner.build_command("hi", model="gpt-5.6-terra", effort="high", structured_schema=None)
    idx = argv.index("-c")
    assert argv[idx + 1] == 'model_reasoning_effort="high"'


def test_codex_build_command_omits_model_reasoning_effort_when_none():
    runner = RunnerRegistry().get("codex")
    argv = runner.build_command("hi", model="gpt-5.6-terra", effort=None, structured_schema=None)
    assert not any("model_reasoning_effort" in arg for arg in argv)


async def test_run_context_passes_role_effort_to_claude_write_argv(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"text": "ok"}})
    store = Store(tmp_path / "alloy.db")
    run_id = "run-effort"
    store.create_run(
        run_id=run_id,
        bead_id="t-1",
        thread_id=run_id,
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    ctx = RunContext(
        bead=make_bead(),
        recipe=None,
        run_id=run_id,
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=RunnerRegistry(log_dir=tmp_path / "logs"),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=None,
    )
    spec = RoleSpec.parse({"runner": "claude-write", "model": "haiku", "effort": "low"})
    result = await ctx.call("implement", spec, "Implement the smallest change.")
    assert result.ok
    argv = fake_harnesses.calls_for("implement")[0]["argv"]
    idx = argv.index("--effort")
    assert argv[idx + 1] == "low"


# -- runner session resume (alloy-21u.2) ---------------------------------------


def _run_context_for_resume_tests(fake_harnesses, project, tmp_path, *, run_id: str = "run-resume") -> RunContext:
    store = Store(tmp_path / "alloy.db")
    store.create_run(
        run_id=run_id,
        bead_id="t-1",
        thread_id=run_id,
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    return RunContext(
        bead=make_bead(),
        recipe=None,
        run_id=run_id,
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=RunnerRegistry(log_dir=tmp_path / "logs"),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=None,
    )


def test_claude_build_command_includes_resume_before_prompt_flag():
    runner = RunnerRegistry().get("claude")
    argv = runner.build_command("p", model="sonnet", structured_schema=None, resume_session="abc")
    resume_idx = argv.index("--resume")
    assert argv[resume_idx + 1] == "abc"
    assert resume_idx < argv.index("-p")


def test_claude_write_build_command_includes_resume_before_prompt_flag():
    runner = RunnerRegistry().get("claude-write")
    argv = runner.build_command("p", model="sonnet", structured_schema=None, resume_session="abc")
    resume_idx = argv.index("--resume")
    assert argv[resume_idx + 1] == "abc"
    assert resume_idx < argv.index("-p")


def test_codex_build_command_uses_exec_resume_subcommand():
    runner = RunnerRegistry().get("codex")
    prompt = "do it"
    argv = runner.build_command(prompt, model=None, structured_schema=None, resume_session="t1")
    assert argv[:3] == ["exec", "resume", "t1"]
    assert "--json" in argv
    assert argv[-1] == prompt


def test_codex_readonly_build_command_uses_exec_resume_subcommand():
    runner = RunnerRegistry().get("codex-readonly")
    prompt = "review this"
    argv = runner.build_command(prompt, model=None, structured_schema=None, resume_session="t1")
    assert argv[:3] == ["exec", "resume", "t1"]
    assert "--json" in argv
    assert argv[-1] == prompt


def test_cursor_build_command_appends_resume_flag():
    runner = RunnerRegistry().get("cursor")
    argv = runner.build_command("hi", model=None, structured_schema=None, resume_session="c1")
    idx = argv.index("--resume")
    assert argv[idx + 1] == "c1"


def test_cursor_plan_build_command_appends_resume_flag():
    runner = RunnerRegistry().get("cursor-plan")
    argv = runner.build_command("hi", model=None, structured_schema=None, resume_session="c1")
    idx = argv.index("--resume")
    assert argv[idx + 1] == "c1"


def test_generic_runner_renders_resume_args_template_token():
    runner = RunnerRegistry(
        {
            "myagent": {
                "binary": "echo",
                "args": ["{resume_args}", "{prompt}"],
                "resume_flag": "--resume",
            }
        }
    ).get("myagent")
    argv = runner.build_command("hi", model=None, structured_schema=None, resume_session="s1")
    assert argv[:2] == ["--resume", "s1"]
    assert argv[-1] == "hi"


@pytest.mark.parametrize(
    "runner_name,kwargs",
    [
        ("claude", {"model": "sonnet"}),
        ("claude-write", {"model": "haiku"}),
        ("codex", {"model": None}),
        ("codex-readonly", {"model": None}),
        ("cursor", {"model": None}),
        ("cursor-plan", {"model": None}),
        ("pi", {"model": "gpt-5"}),
    ],
)
def test_build_command_argv_unchanged_when_resume_session_is_none(runner_name, kwargs):
    runner = RunnerRegistry().get(runner_name)
    baseline = runner.build_command("hi", structured_schema=None, **kwargs)
    unchanged = runner.build_command("hi", structured_schema=None, resume_session=None, **kwargs)
    assert unchanged == baseline


async def test_run_context_passes_resume_session_through_fake_harness(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"text": "ok"}})
    ctx = _run_context_for_resume_tests(fake_harnesses, project, tmp_path)
    spec = RoleSpec.parse({"runner": "codex"})
    result = await ctx.call(
        "implement",
        spec,
        "Implement the smallest change.",
        resume_session="abc",
    )
    assert result.ok
    call = fake_harnesses.calls_for("implement")[0]
    assert call["resume"] == "abc"
    ledger_row = ctx.store.agent_calls(ctx.run_id)[-1]
    usage = json.loads(ledger_row["usage_json"])
    assert usage["resumed"] is True


async def test_run_context_drops_resume_session_on_fallback(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {
            "implement@codex": [{"exit": 2, "stderr": "codex: rate limited", "text": ""}],
            "implement@claude": [{"text": "ok"}],
        }
    )
    ctx = _run_context_for_resume_tests(fake_harnesses, project, tmp_path)
    spec = RoleSpec.parse(
        {
            "runner": "codex",
            "fallback": {"runner": "claude-write", "model": "fable"},
        }
    )
    result = await ctx.call(
        "implement",
        spec,
        "Implement the smallest change.",
        resume_session="abc",
    )
    assert result.ok
    calls = fake_harnesses.calls_for("implement")
    assert calls[0]["resume"] == "abc"
    assert "resume" not in calls[1]
    implement_calls = [c for c in ctx.store.agent_calls(ctx.run_id) if c["role"] == "implement"]
    assert json.loads(implement_calls[0]["usage_json"])["resumed"] is True
    assert json.loads(implement_calls[1]["usage_json"])["resumed"] is False


# -- oversized stdin prompts (alloy-5wb.6) ------------------------------------


IMPLEMENT_MARKER = "Implement the smallest change."
STDIN_PROMPT_THRESHOLD = 60_000
OVERSIZED_PROMPT_LEN = 200_000


def _prompt_with_marker(total_len: int) -> str:
    if total_len < len(IMPLEMENT_MARKER):
        raise ValueError(f"total_len must be at least {len(IMPLEMENT_MARKER)}")
    return IMPLEMENT_MARKER + ("x" * (total_len - len(IMPLEMENT_MARKER)))


def _prompt_in_argv(call: dict, prompt: str) -> bool:
    return any(arg == prompt for arg in call["argv"])


def _codex_argv_uses_stdin_sentinel(argv: list[str]) -> bool:
    return bool(argv) and argv[-1] == "-"


def _claude_argv_uses_stdin_prompt_flag(argv: list[str]) -> bool:
    """Claude reads the prompt from stdin when ``-p`` is not followed by text."""
    if "-p" not in argv:
        return False
    p_idx = argv.index("-p")
    if p_idx + 1 >= len(argv):
        return True
    return argv[p_idx + 1].startswith("-")


def test_cli_runner_stdin_prompt_threshold_on_codex_and_claude():
    codex = RunnerRegistry().get("codex")
    claude = RunnerRegistry().get("claude")
    assert codex.stdin_prompt_threshold == STDIN_PROMPT_THRESHOLD
    assert claude.stdin_prompt_threshold == STDIN_PROMPT_THRESHOLD


def test_codex_build_command_stdin_replaces_trailing_prompt_with_dash():
    runner = RunnerRegistry().get("codex")
    argv = runner.build_command_stdin(model="gpt-5.6-terra", structured_schema=None, effort="high")
    assert argv is not None
    assert argv[-1] == "-"
    assert "ignored" not in argv
    idx = argv.index("-c")
    assert argv[idx + 1] == 'model_reasoning_effort="high"'


def test_codex_build_command_stdin_honours_resume_session():
    runner = RunnerRegistry().get("codex")
    argv = runner.build_command_stdin(model=None, structured_schema=None, resume_session="abc")
    assert argv is not None
    assert argv[:3] == ["exec", "resume", "abc"]
    assert argv[-1] == "-"
    assert "ignored" not in argv


def test_claude_build_command_stdin_leaves_p_flag_without_prompt_argument():
    runner = RunnerRegistry().get("claude")
    argv = runner.build_command_stdin(model="sonnet", structured_schema=None, effort="low")
    assert argv is not None
    assert "ignored" not in argv
    assert _claude_argv_uses_stdin_prompt_flag(argv)
    resume_idx = argv.index("--effort")
    assert argv[resume_idx + 1] == "low"


def test_claude_build_command_stdin_honours_resume_session():
    runner = RunnerRegistry().get("claude")
    argv = runner.build_command_stdin(model="sonnet", structured_schema=None, resume_session="abc")
    assert argv is not None
    resume_idx = argv.index("--resume")
    assert argv[resume_idx + 1] == "abc"
    assert resume_idx < argv.index("-p")
    assert _claude_argv_uses_stdin_prompt_flag(argv)
    assert "ignored" not in argv


async def test_codex_oversized_prompt_delivered_via_stdin(fake_harnesses, project, tmp_path):
    prompt = _prompt_with_marker(OVERSIZED_PROMPT_LEN)
    assert len(prompt) > STDIN_PROMPT_THRESHOLD
    fake_harnesses.configure({"implement": {"text": "ok"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run(prompt, project)
    assert result.ok
    call = fake_harnesses.calls_for("implement")[0]
    assert IMPLEMENT_MARKER in call["prompt"]
    assert _codex_argv_uses_stdin_sentinel(call["argv"])
    assert not _prompt_in_argv(call, prompt)


async def test_claude_oversized_prompt_delivered_via_stdin(fake_harnesses, project, tmp_path):
    prompt = _prompt_with_marker(OVERSIZED_PROMPT_LEN)
    assert len(prompt) > STDIN_PROMPT_THRESHOLD
    fake_harnesses.configure({"implement": {"text": "ok"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("claude").run(prompt, project)
    assert result.ok
    call = fake_harnesses.calls_for("implement")[0]
    assert IMPLEMENT_MARKER in call["prompt"]
    assert _claude_argv_uses_stdin_prompt_flag(call["argv"])
    assert not _prompt_in_argv(call, prompt)


async def test_codex_oversized_prompt_with_resume_records_session(fake_harnesses, project, tmp_path):
    prompt = _prompt_with_marker(OVERSIZED_PROMPT_LEN)
    fake_harnesses.configure({"implement": {"text": "ok"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run(prompt, project, resume_session="abc")
    assert result.ok
    call = fake_harnesses.calls_for("implement")[0]
    assert IMPLEMENT_MARKER in call["prompt"]
    assert call["resume"] == "abc"
    assert _codex_argv_uses_stdin_sentinel(call["argv"])


async def test_claude_oversized_prompt_with_resume_records_session(fake_harnesses, project, tmp_path):
    prompt = _prompt_with_marker(OVERSIZED_PROMPT_LEN)
    fake_harnesses.configure({"implement": {"text": "ok"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("claude").run(prompt, project, resume_session="abc")
    assert result.ok
    call = fake_harnesses.calls_for("implement")[0]
    assert IMPLEMENT_MARKER in call["prompt"]
    assert call["resume"] == "abc"
    assert _claude_argv_uses_stdin_prompt_flag(call["argv"])


async def test_codex_subthreshold_prompt_stays_in_argv(fake_harnesses, project, tmp_path):
    prompt = _prompt_with_marker(1_000)
    assert len(prompt) <= STDIN_PROMPT_THRESHOLD
    fake_harnesses.configure({"implement": {"text": "ok"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run(prompt, project)
    assert result.ok
    call = fake_harnesses.calls_for("implement")[0]
    assert _prompt_in_argv(call, prompt)
    assert not _codex_argv_uses_stdin_sentinel(call["argv"])


async def test_claude_subthreshold_prompt_stays_in_argv(fake_harnesses, project, tmp_path):
    prompt = _prompt_with_marker(1_000)
    assert len(prompt) <= STDIN_PROMPT_THRESHOLD
    fake_harnesses.configure({"implement": {"text": "ok"}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("claude").run(prompt, project)
    assert result.ok
    call = fake_harnesses.calls_for("implement")[0]
    assert _prompt_in_argv(call, prompt)
    assert not _claude_argv_uses_stdin_prompt_flag(call["argv"])
