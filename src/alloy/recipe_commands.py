"""recipe commands domain behavior for the CLI."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from alloy import recipes
from alloy.cli_common import _emit, _fail, _run_async, console
from alloy.config import (
    ConfigError,
    RoleSpec,
    discover_recipes,
    load_recipe,
)
from alloy.paths import AlloyPaths
from alloy.runners import RunnerRegistry, RunnerUnavailable
from alloy.status_display import (
    _role_label,
)


def _recipe_entries(found, paths, repo_path, probe):
    entries: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    probed: set[tuple[str, str | None, str | None]] = set()
    for name, path in sorted(found.items()):
        entry: dict[str, Any] = {
            "name": name,
            "config": str(path),
            "graph": name in recipes.REGISTRY,
        }
        try:
            config = load_recipe(name, alloy_root=paths.shared_root, project=repo_path)
        except ConfigError as exc:
            entry["error"] = str(exc)
            entries.append(entry)
            continue
        # The recipe's own `runners:` block (api keys, binaries) decides what
        # is available -- a bare registry would call Jev "missing" forever.
        registry = RunnerRegistry(config.runners, log_dir=paths.logs / "probes" / name)

        def describe(spec: "RoleSpec") -> dict[str, Any]:
            info: dict[str, Any] = {
                "runner": spec.runner,
                "model": spec.model,
                "effort": spec.effort,
                "available": registry.available(spec.runner),
            }
            if spec.fallback is not None:
                info["fallback"] = describe(spec.fallback)
            return info

        entry["roles"] = {role: describe(spec) for role, spec in config.roles.items()}
        tiers: dict[str, list[dict[str, Any]]] = {}
        for tier, head in config.complexity.tiers.items():
            chain = []
            spec = head
            while spec is not None:
                chain.append(
                    {
                        "runner": spec.runner,
                        "model": spec.model,
                        "effort": spec.effort,
                        "available": registry.available(spec.runner),
                    }
                )
                spec = spec.fallback
            tiers[tier] = chain
        entry["complexity"] = {
            "routing": config.complexity.routing,
            "escalate_after_retries": config.complexity.escalate_after_retries,
            "tiers": tiers,
        }
        if probe:
            probes.extend(_run_async(_probe_tiers(registry, tiers, repo_path, probed)))
        entry["critics"] = [
            {"runner": spec.runner, "available": registry.available(spec.runner)} for spec in config.consilium.critics
        ]
        entry["limits"] = config.limits.__dict__
        entry["landing"] = {
            "mode": config.landing.mode,
            "target": config.landing.target,
        }
        entries.append(entry)
    return entries, probes


def _show_probe_table(probes):
    table = Table(title="Tier probes")
    for column in (
        "Tier",
        "Runner",
        "Model",
        "Effort",
        "Result",
        "Error",
        "Seconds",
    ):
        table.add_column(column)
    for row in probes:
        table.add_row(
            row["tier"],
            row["runner"],
            row["model"] or "-",
            row["effort"] or "-",
            "ok" if row["ok"] else "error",
            row["error"] or "-",
            f"{row['duration_s']:.1f}",
        )
    console.print(table)


def _show_recipe_entry(entry):
    console.print(f"[bold]{entry['name']}[/bold]  ({entry['config']})")
    if entry.get("error"):
        console.print(f"  [red]{entry['error']}[/red]")
        return
    if not entry["graph"]:
        console.print("  [yellow]no graph registered for this name[/yellow]")
    for role, info in entry.get("roles", {}).items():
        console.print(f"  {role:<10} {_role_label(info)}")
    complexity = entry["complexity"]
    console.print(
        f"  complexity routing={complexity['routing']} escalate_after_retries={complexity['escalate_after_retries']}"
    )
    for tier, chain in complexity["tiers"].items():
        console.print(f"    {tier:<8} " + " [dim]->[/dim] ".join(_role_label(info) for info in chain))
    critics = ", ".join(f"{c['runner']}{'' if c['available'] else '(missing)'}" for c in entry["critics"])
    console.print(f"  critics    {critics or '(none)'}")
    console.print(f"  limits     {entry['limits']}")


def _show_recipes(entries, probes, probe, json):
    if json:
        payload = {"recipes": entries}
        if probe:
            payload["probe"] = probes
        _emit(payload, True)
        if probe and (any(not row["ok"] for row in probes) or any("error" in e for e in entries)):
            raise typer.Exit(1)
        return
    for entry in entries:
        _show_recipe_entry(entry)
    if probe:
        _show_probe_table(probes)
    if any(not row["ok"] for row in probes) or any("error" in e for e in entries):
        raise typer.Exit(1)


def recipes_command_impl(repo, root, json, probe, recipe):
    """List recipes, their configured roles, and whether those harnesses exist."""
    repo_path = (repo or Path.cwd()).resolve()
    paths = AlloyPaths.resolve(root, project=repo_path)
    found = discover_recipes(paths.shared_root, repo_path)
    if recipe is not None:
        if recipe not in found:
            _fail(f"unknown recipe: {recipe}")
        found = {recipe: found[recipe]}

    entries, probes = _recipe_entries(found, paths, repo_path, probe)
    _show_recipes(entries, probes, probe, json)


async def _probe_tiers(
    registry: RunnerRegistry,
    tiers: dict[str, list[dict[str, Any]]],
    repo: Path,
    seen: set[tuple[str, str | None, str | None]],
) -> list[dict[str, Any]]:
    rows = []
    for tier, chain in tiers.items():
        for entry in chain:
            key = (entry["runner"], entry["model"], entry["effort"])
            if key in seen:
                continue
            seen.add(key)
            row = {"tier": tier, "runner": key[0], "model": key[1], "effort": key[2]}
            started = time.monotonic()
            try:
                result = await registry.get(key[0]).run(
                    "Reply with the single word OK.",
                    cwd=repo,
                    model=key[1],
                    effort=key[2],
                    timeout=timedelta(minutes=2),
                )
                row.update(ok=result.ok, error=result.error, duration_s=result.duration_s)
            except RunnerUnavailable as exc:
                row.update(ok=False, error=str(exc), duration_s=time.monotonic() - started)
            rows.append(row)
    return rows
