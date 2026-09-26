"""Limits core: shapes, runner mapping, installed detection, cache (alloy-w9d.1)."""

from __future__ import annotations

import pytest

from alloy.limits import (
    harness_for_runner,
    installed_harnesses,
    read_cache,
    unavailable,
    window,
    write_cache,
)
from alloy.paths import AlloyPaths
from alloy.runners import RunnerRegistry

WINDOW_KEYS = frozenset({"key", "label", "used_percent", "resets_at", "model"})
HARNESS_LIMITS_KEYS = frozenset(
    {
        "harness",
        "installed",
        "available",
        "fetched_at",
        "as_of",
        "source",
        "error",
        "status",
        "windows",
    }
)


def test_harness_for_runner_mapping():
    assert harness_for_runner("claude") == "claude"
    assert harness_for_runner("claude-write") == "claude"
    assert harness_for_runner("codex") == "codex"
    assert harness_for_runner("codex-readonly") == "codex"
    assert harness_for_runner("astra") == "codex"
    assert harness_for_runner("cursor") == "cursor"
    assert harness_for_runner("cursor-plan") == "cursor"
    assert harness_for_runner("pi") is None
    assert harness_for_runner("jev") is None
    assert harness_for_runner("nope") is None


def test_installed_harnesses_only_codex(fake_harnesses):
    fake_harnesses.remove("claude")
    fake_harnesses.remove("cursor-agent")
    registry = RunnerRegistry()
    assert installed_harnesses(registry) == ["codex"]


def test_installed_harnesses_all_three_in_order(fake_harnesses):
    registry = RunnerRegistry()
    assert installed_harnesses(registry) == ["claude", "codex", "cursor"]


def test_window_shape():
    result = window("five_hour", "5h", 42.0, None)
    assert set(result) == WINDOW_KEYS
    assert result["key"] == "five_hour"
    assert result["label"] == "5h"
    assert result["used_percent"] == 42.0
    assert result["resets_at"] is None
    assert result["model"] is None


def test_unavailable_shape():
    result = unavailable("codex", "no local sample")
    assert set(result) == HARNESS_LIMITS_KEYS
    assert result["harness"] == "codex"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "no local sample"


def test_write_cache_read_cache_round_trip(alloy_home):
    paths = AlloyPaths.resolve(alloy_home).ensure()
    samples = {
        "claude": unavailable("claude", "no credentials"),
        "codex": unavailable("codex", "no local sample"),
    }
    write_cache(paths, samples)
    assert read_cache(paths) == samples


def test_read_cache_missing_file_returns_empty_dict(alloy_home):
    paths = AlloyPaths.resolve(alloy_home).ensure()
    assert read_cache(paths) == {}


def test_read_cache_unparsable_file_returns_empty_dict(alloy_home):
    paths = AlloyPaths.resolve(alloy_home).ensure()
    paths.limits_cache.write_text("not json", encoding="utf-8")
    assert read_cache(paths) == {}


def test_write_cache_leaves_no_tmp_files(alloy_home):
    paths = AlloyPaths.resolve(alloy_home).ensure()
    samples = {"codex": unavailable("codex", "no local sample")}
    write_cache(paths, samples)
    assert list(paths.root.glob("*.tmp")) == []
