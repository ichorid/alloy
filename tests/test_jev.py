"""Jev is a fixed-label classifier, not a free-text generator: it only accepts
requests carrying a schema's enum values as candidate labels, and only ever
returns a choice + confidence over that same fixed set."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from alloy.models import RunnerUnavailable
from alloy.runners.jev import JevRunner

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["done", "retry", "consilium", "human", "abort"]},
        "reason": {"type": "string"},
    },
    "required": ["decision"],
}


def _runner(handler, **kwargs) -> JevRunner:
    return JevRunner(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_jev_classifies_into_the_schema_enum_and_returns_confidence(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": "retry",
                        "confidence": 0.83,
                        "probabilities": {
                            "done": 0.05,
                            "retry": 0.83,
                            "consilium": 0.07,
                            "human": 0.03,
                            "abort": 0.02,
                        },
                    }
                },
                "usage": {"input_tokens": 120, "output_tokens": 4},
            },
        )

    runner = _runner(handler, log_dir=tmp_path / "logs")
    result = await runner.run("tests are still failing on foo.py", Path("."), structured_schema=SCHEMA)

    assert result.ok
    assert result.structured == {"decision": "retry", "confidence": 0.83}
    assert result.usage == {"input_tokens": 120, "output_tokens": 4}
    assert captured["auth"] == "Bearer test-key"
    assert captured["payload"]["questions"]["decision"]["type"] == "choice"
    assert set(captured["payload"]["questions"]["decision"]["criteria"]) == {
        "done", "retry", "consilium", "human", "abort",
    }
    assert Path(result.log_path).exists()
    logged = json.loads(Path(result.log_path).read_text())
    assert logged["probabilities"]["retry"] == 0.83


async def test_jev_surfaces_http_errors_without_raising(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    runner = _runner(handler, log_dir=tmp_path / "logs")
    result = await runner.run("state", Path("."), structured_schema=SCHEMA)

    assert not result.ok
    assert result.exit_code == 500
    assert "500" in result.error


async def test_jev_requires_a_schema_with_an_enum_field():
    runner = _runner(lambda request: httpx.Response(200, json={}))
    with pytest.raises(RunnerUnavailable):
        await runner.run("state", Path("."), structured_schema=None)
    with pytest.raises(RunnerUnavailable):
        await runner.run(
            "state", Path("."),
            structured_schema={"type": "object", "properties": {"reason": {"type": "string"}}},
        )


def test_jev_unavailable_without_a_key():
    runner = JevRunner()
    assert not runner.available()


def test_jev_available_from_key_file(tmp_path):
    key_file = tmp_path / "jev.key"
    key_file.write_text("apikey_abc123\n")
    runner = JevRunner(api_key_file=str(key_file))
    assert runner.available()
