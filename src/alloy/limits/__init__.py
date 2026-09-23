"""Harness usage limits: shapes, runner mapping, installed detection, cache.

Component A1 of docs/plans/monitor-limits.md. A *harness* is one of the
CLIs Alloy drives (`claude`, `codex`, `cursor`); several runner names map
onto each. The shapes here are plain dicts with a fixed key set so they
round-trip through `limits.json` unchanged. Probes live in sibling modules;
this module holds only what they and the monitor share.

`parse_retry_at` (reading "come back later" out of harness errors) lives in
`alloy.limits.retry` and is re-exported here for existing importers.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from alloy.limits.retry import DEFAULT_RATE_LIMIT_WAIT, parse_retry_at
from alloy.paths import AlloyPaths
from alloy.runners import RunnerRegistry

HARNESSES: tuple[str, ...] = ("claude", "codex", "cursor")
"""Harnesses the monitor knows about, in display order."""

RUNNER_HARNESS: dict[str, str] = {
    "claude": "claude",
    "claude-write": "claude",
    "codex": "codex",
    "codex-readonly": "codex",
    "astra": "codex",
    "cursor": "cursor",
    "cursor-plan": "cursor",
}
"""Runner name -> harness. Runners not listed belong to no harness."""

WINDOW_KEYS: tuple[str, ...] = ("key", "label", "used_percent", "resets_at", "model")
HARNESS_LIMITS_KEYS: tuple[str, ...] = (
    "harness",
    "installed",
    "available",
    "fetched_at",
    "as_of",
    "source",
    "error",
    "status",
    "windows",
)


def harness_for_runner(name: str) -> str | None:
    """Which harness a runner name belongs to, or None for unrelated runners."""
    return RUNNER_HARNESS.get(name)


def installed_harnesses(registry: RunnerRegistry) -> list[str]:
    """Harnesses whose canonical runner binary is on PATH, in HARNESSES order."""
    return [harness for harness in HARNESSES if registry.available(harness)]


def window(
    key: str,
    label: str,
    used_percent: float,
    resets_at: str | None,
    model: str | None = None,
) -> dict[str, Any]:
    """One usage gauge; always carries every Window key."""
    return {
        "key": key,
        "label": label,
        "used_percent": float(used_percent),
        "resets_at": resets_at,
        "model": model,
    }


def unavailable(harness: str, error: str, *, installed: bool = True) -> dict[str, Any]:
    """A HarnessLimits sample for a harness whose probe produced no windows."""
    return {
        "harness": harness,
        "installed": installed,
        "available": False,
        "fetched_at": None,
        "as_of": None,
        "source": None,
        "error": error,
        "status": None,
        "windows": [],
    }


def write_cache(paths: AlloyPaths, samples: dict[str, dict[str, Any]]) -> None:
    """Replace `limits.json` atomically: readers see the old or the new file, never a torn one."""
    target = paths.limits_cache
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".limits-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(samples, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_cache(paths: AlloyPaths) -> dict[str, dict[str, Any]]:
    """The cached samples, or {} when the file is missing or unreadable."""
    try:
        data = json.loads(paths.limits_cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _probe_harness(harness: str, home: Path, fetch: Any) -> dict[str, Any]:
    """Run one harness probe; codex ignores ``fetch``."""
    if harness == "claude":
        from alloy.limits import claude

        resolved = fetch if fetch is not None else claude.default_fetch
        return claude.probe(home, resolved)
    if harness == "codex":
        from alloy.limits import codex

        return codex.probe(home)
    if harness == "cursor":
        from alloy.limits import cursor

        resolved = fetch if fetch is not None else cursor.default_fetch
        return cursor.probe(home, resolved)
    raise ValueError(f"unknown harness: {harness}")


def probe_all(
    paths: AlloyPaths,
    registry: RunnerRegistry,
    *,
    fetch: Any = None,
    home: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Probe every installed harness, refresh ``limits.json``, return the mapping."""
    resolved_home = home or Path.home()
    result: dict[str, dict[str, Any]] = {}
    for harness in installed_harnesses(registry):
        try:
            result[harness] = _probe_harness(harness, resolved_home, fetch)
        except Exception as exc:
            result[harness] = unavailable(harness, f"{type(exc).__name__}: {exc}")
    write_cache(paths, result)
    return result


__all__ = [
    "DEFAULT_RATE_LIMIT_WAIT",
    "HARNESSES",
    "HARNESS_LIMITS_KEYS",
    "RUNNER_HARNESS",
    "WINDOW_KEYS",
    "harness_for_runner",
    "installed_harnesses",
    "parse_retry_at",
    "probe_all",
    "read_cache",
    "unavailable",
    "window",
    "write_cache",
]
