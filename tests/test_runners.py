"""Adapters normalize four different CLIs into one result shape."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from alloy.models import RunnerUnavailable
from alloy.runners import RunnerRegistry
from alloy.runners.base import extract_json_object, schema_instructions


SCHEMA = {"type": "object", "properties": {"decision": {"type": "string"}}}


async def test_claude_adapter_returns_native_structured_output(
    fake_harnesses, project, tmp_path
):
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


async def test_cursor_is_error_envelope_with_exit_zero_is_a_failed_call(
    fake_harnesses, project, tmp_path
):
    fake_harnesses.configure(
        {"context": {"exit": 0, "is_error": True, "text": "rate limited"}}
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("cursor").run("You are gathering context.", project)
    assert not result.ok
    assert result.exit_code == 0
    assert "rate limited" in (result.error or "")


async def test_cursor_success_envelope_with_exit_zero_stays_ok(
    fake_harnesses, project, tmp_path
):
    fake_harnesses.configure(
        {"context": {"exit": 0, "is_error": False, "text": "all good"}}
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("cursor").run("You are gathering context.", project)
    assert result.ok
    assert result.text == "all good"


async def test_codex_error_event_without_agent_message_and_exit_zero_fails(
    fake_harnesses, project, tmp_path
):
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


async def test_codex_agent_message_after_error_event_with_exit_zero_succeeds(
    fake_harnesses, project, tmp_path
):
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


async def test_claude_is_error_envelope_with_exit_zero_is_a_failed_call(
    fake_harnesses, project, tmp_path
):
    fake_harnesses.configure(
        {"judge": {"exit": 0, "is_error": True, "text": "boom"}}
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("claude").run(
        "You are judging whether this is complete.", project
    )
    assert not result.ok
    assert result.exit_code == 0
    assert "boom" in (result.error or "")


async def test_structured_output_recovered_from_unstructured_harness(
    fake_harnesses, project, tmp_path
):
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


async def test_nonzero_exit_is_a_failed_result_not_an_exception(
    fake_harnesses, project, tmp_path
):
    fake_harnesses.configure(
        {"implement": {"exit": 2, "stderr": "codex: rate limited", "text": ""}}
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project)
    assert not result.ok
    assert result.exit_code == 2
    assert "rate limited" in result.error


async def test_codex_error_event_leads_the_failure_message(fake_harnesses, project, tmp_path):
    """codex prints chatter on stderr; the JSONL error event is the real reason."""
    fake_harnesses.configure(
        {"implement": {"exit": 1, "stderr": "Reading additional input from stdin...\n",
                       "text": "Your workspace is out of credits."}}
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run("Implement the smallest change.", project)
    assert result.error.startswith("Your workspace is out of credits.")


async def test_timeout_is_reported_not_raised(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"sleep": 5}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")
    result = await registry.get("codex").run(
        "Implement the smallest change.", project, timeout=timedelta(seconds=1)
    )
    assert not result.ok
    assert "timed out" in result.error


async def test_missing_binary_raises_runner_unavailable(fake_harnesses, project):
    fake_harnesses.remove("pi")
    registry = RunnerRegistry()
    assert not registry.available("pi")
    with pytest.raises(RunnerUnavailable):
        await registry.get("pi").run("hello", project)


async def test_agents_always_run_inside_the_given_directory(
    fake_harnesses, project, tmp_path
):
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
