"""Alloy's command line.

Every command is also an API: `--json` output is stable enough for a manager
agent to drive Alloy without a human in the loop.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from alloy import beads as bd
from alloy import recipes
from alloy.config import ConfigError, discover_recipes, load_recipe
from alloy.engine import Engine, EngineError
from alloy.paths import AlloyPaths
from alloy.runners import RunnerRegistry
from alloy.scheduler import Scheduler, SchedulerBusy, read_pid, signal_stop, spawn_detached
from alloy.store import Store

app = typer.Typer(
    name="alloy",
    help="Durable, adaptive coding-agent workflows over Beads + git worktrees.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

RepoOption = typer.Option(None, "--repo", help="Repository root (default: cwd)")
RootOption = typer.Option(None, "--root", help="Alloy home (default: $ALLOY_HOME or ~/.alloy)")


def _engine(repo: Optional[Path], root: Optional[Path]) -> Engine:
    return Engine.open(repo or Path.cwd(), root)


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))


def _fail(message: str) -> None:
    err.print(f"[red]error:[/red] {message}")
    raise typer.Exit(1)


# --------------------------------------------------------------------------


@app.command()
def init(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Create Alloy's home directory and register its statuses with Beads."""
    paths = AlloyPaths.resolve(root).ensure()
    Store(paths.alloy_db)
    repo_path = (repo or Path.cwd()).resolve()

    report: dict[str, Any] = {
        "root": str(paths.root),
        "repo": str(repo_path),
        "recipes": sorted(discover_recipes(paths.root, repo_path)),
        "beads": "ok",
        "runners": {},
    }

    client = bd.BeadsClient(repo=repo_path)
    if not client.available():
        report["beads"] = "missing: install with `npm install -g @beads/bd`"
    else:
        try:
            client.ensure_statuses()
        except bd.BeadsError as exc:
            report["beads"] = f"not initialized here ({exc}); run `bd init` in {repo_path}"

    registry = RunnerRegistry()
    for name in ("claude", "codex", "cursor", "pi"):
        report["runners"][name] = registry.available(name)

    if json:
        _emit(report, True)
        return

    console.print(f"[bold]Alloy home[/bold]  {paths.root}")
    console.print(f"[bold]Repository[/bold]  {repo_path}")
    console.print(f"[bold]Recipes[/bold]     {', '.join(report['recipes']) or '(none)'}")
    console.print(f"[bold]Beads[/bold]       {report['beads']}")
    for name, ok in report["runners"].items():
        mark = "[green]available[/green]" if ok else "[yellow]not installed[/yellow]"
        console.print(f"  runner {name:<8} {mark}")


@app.command()
def run(
    bead_id: str = typer.Argument(..., help="Bead to execute, e.g. bd-179"),
    recipe: Optional[str] = typer.Option(None, "--recipe", help="Override the bead's recipe"),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Run one bead's workflow to completion, a human gate, or failure."""
    engine = _engine(repo, root)
    try:
        result = asyncio.run(engine.run(bead_id, recipe_name=recipe))
    except (EngineError, ConfigError, bd.BeadsError) as exc:
        _fail(str(exc))
        return

    payload = {
        "bead": result.bead_id, "run_id": result.run_id, "outcome": result.outcome,
        "reason": result.reason, "worktree": result.worktree, "interrupt": result.interrupt,
    }
    if json:
        _emit(payload, True)
        return
    colour = {"done": "green", "waiting-human": "yellow"}.get(result.outcome, "red")
    console.print(f"[{colour}]{result.outcome}[/{colour}] {result.bead_id} -- {result.reason}")
    console.print(f"worktree: {result.worktree}")
    if result.paused:
        console.print(f"resume with: [bold]alloy resume {result.bead_id} -m '...'[/bold]")
    if result.outcome not in ("done", "waiting-human"):
        raise typer.Exit(1)


@app.command()
def resume(
    bead_id: str = typer.Argument(...),
    message: str = typer.Option("", "--message", "-m", help="Guidance for the human gate"),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Continue a paused or crashed run from its last checkpoint."""
    engine = _engine(repo, root)
    try:
        result = asyncio.run(engine.resume(bead_id, message))
    except (EngineError, ConfigError, bd.BeadsError) as exc:
        _fail(str(exc))
        return
    payload = {"bead": result.bead_id, "run_id": result.run_id,
               "outcome": result.outcome, "reason": result.reason}
    if json:
        _emit(payload, True)
        return
    console.print(f"{result.outcome} {result.bead_id} -- {result.reason}")


@app.command()
def cancel(
    bead_id: str = typer.Argument(...),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Stop tracking a run and return the bead to ready. The worktree is kept."""
    engine = _engine(repo, root)
    cancelled = engine.cancel(bead_id)
    if json:
        _emit({"bead": bead_id, "cancelled": cancelled}, True)
        return
    console.print(f"{'cancelled' if cancelled else 'nothing to cancel for'} {bead_id}")


@app.command()
def start(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    poll: float = typer.Option(15.0, "--poll", help="Seconds between Beads polls"),
    recipe: Optional[str] = typer.Option(None, "--recipe", help="Only run this recipe"),
    foreground: bool = typer.Option(False, "--foreground", help="Do not detach"),
) -> None:
    """Start the scheduler: poll for ready beads and run them one at a time."""
    engine = _engine(repo, root)
    if not foreground:
        existing = read_pid(engine.paths.scheduler_pid)
        if existing:
            _fail(f"scheduler already running (pid {existing})")
        pid = spawn_detached(engine.repo, engine.paths.root, poll)
        console.print(f"scheduler started (pid {pid})")
        return
    scheduler = Scheduler(engine=engine, poll_seconds=poll, recipe_filter=recipe)
    try:
        asyncio.run(scheduler.serve())
    except SchedulerBusy as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        pass


@app.command()
def stop(
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Signal the scheduler to stop after the current task."""
    paths = AlloyPaths.resolve(root)
    pid = signal_stop(paths.scheduler_pid)
    if json:
        _emit({"stopped": pid is not None, "pid": pid}, True)
        return
    console.print(f"sent stop to pid {pid}" if pid else "scheduler is not running")


@app.command()
def status(
    bead_id: Optional[str] = typer.Argument(None, help="Limit to one bead"),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Show what Alloy is doing. Designed to be read by humans and by agents."""
    engine = _engine(repo, root)
    scheduler_pid = read_pid(engine.paths.scheduler_pid)

    runs = ([engine.store.latest_run_for_bead(bead_id)] if bead_id
            else engine.store.all_runs(limit))
    runs = [record for record in runs if record]

    rows = [_status_row(engine, record) for record in runs]
    payload = {
        "root": str(engine.paths.root),
        "repo": str(engine.repo),
        "scheduler": {"running": scheduler_pid is not None, "pid": scheduler_pid},
        "runs": rows,
    }
    if json:
        _emit(payload, True)
        return

    console.print(
        f"scheduler: [{'green' if scheduler_pid else 'yellow'}]"
        f"{'running (pid ' + str(scheduler_pid) + ')' if scheduler_pid else 'stopped'}[/]"
    )
    if not rows:
        console.print("no runs yet")
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("bead", "recipe", "status", "stage", "iter", "tests", "elapsed", "runner"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["bead"], row["recipe"], _coloured(row["status"]), row["stage"] or "-",
            f"{row['iteration']}/{row['max_iterations']}", row["tests"] or "-",
            row["elapsed"], row["runner"] or "-",
        )
    console.print(table)


@app.command()
def logs(
    bead_id: str = typer.Argument(...),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
    tail: int = typer.Option(20, "--tail", help="Most recent agent calls to show"),
) -> None:
    """List the agent calls of a run and where their raw transcripts live."""
    engine = _engine(repo, root)
    record = engine.store.latest_run_for_bead(bead_id)
    if record is None:
        _fail(f"no run recorded for {bead_id}")
        return
    calls = engine.store.agent_calls(record["run_id"])[-tail:]
    if json:
        _emit({"run_id": record["run_id"], "log_dir": record["log_dir"], "calls": calls}, True)
        return
    console.print(f"[bold]run[/bold] {record['run_id']}   [bold]logs[/bold] {record['log_dir']}")
    table = Table(show_header=True, header_style="bold")
    for column in ("#", "role", "runner", "model", "iter", "secs", "exit", "artifact"):
        table.add_column(column)
    for index, call in enumerate(calls, 1):
        table.add_row(
            str(index), call["role"], call["runner"], call["model"] or "-",
            str(call["iteration"]), f"{call['duration_s']:.1f}",
            str(call["exit_code"]), call["log_path"] or "-",
        )
    console.print(table)


@app.command(name="recipes")
def recipes_command(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """List recipes, their configured roles, and whether those harnesses exist."""
    paths = AlloyPaths.resolve(root)
    repo_path = (repo or Path.cwd()).resolve()
    found = discover_recipes(paths.root, repo_path)
    registry = RunnerRegistry()

    entries: list[dict[str, Any]] = []
    for name, path in sorted(found.items()):
        entry: dict[str, Any] = {"name": name, "config": str(path),
                                 "graph": name in recipes.REGISTRY}
        try:
            config = load_recipe(name, alloy_root=paths.root, project=repo_path)
        except ConfigError as exc:
            entry["error"] = str(exc)
            entries.append(entry)
            continue
        entry["roles"] = {
            role: {"runner": spec.runner, "model": spec.model,
                   "available": registry.available(spec.runner)}
            for role, spec in config.roles.items()
        }
        entry["critics"] = [
            {"runner": spec.runner, "available": registry.available(spec.runner)}
            for spec in config.consilium.critics
        ]
        entry["limits"] = config.limits.__dict__
        entries.append(entry)

    if json:
        _emit({"recipes": entries}, True)
        return
    for entry in entries:
        console.print(f"[bold]{entry['name']}[/bold]  ({entry['config']})")
        if entry.get("error"):
            console.print(f"  [red]{entry['error']}[/red]")
            continue
        if not entry["graph"]:
            console.print("  [yellow]no graph registered for this name[/yellow]")
        for role, info in entry.get("roles", {}).items():
            mark = "" if info["available"] else " [yellow](runner missing)[/yellow]"
            model = f":{info['model']}" if info["model"] else ""
            console.print(f"  {role:<10} {info['runner']}{model}{mark}")
        critics = ", ".join(
            f"{c['runner']}{'' if c['available'] else '(missing)'}" for c in entry["critics"]
        )
        console.print(f"  critics    {critics or '(none)'}")
        console.print(f"  limits     {entry['limits']}")


# --------------------------------------------------------------------------


def _status_row(engine: Engine, record: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timezone

    try:
        config = engine.load_config(record["recipe"])
        max_iterations = config.limits.max_iterations
        implement_runner = config.roles.get("implement")
        runner = implement_runner.runner if implement_runner else None
    except ConfigError:
        max_iterations, runner = 0, None

    started = _parse(record["started_at"])
    ended = _parse(record["ended_at"]) if record["ended_at"] else None
    reference = ended or datetime.now(timezone.utc)
    elapsed = int((reference - started).total_seconds() // 60) if started else 0

    return {
        "bead": record["bead_id"],
        "run_id": record["run_id"],
        "recipe": record["recipe"],
        "status": record["status"],
        "stage": record["stage"],
        "iteration": record["iteration"],
        "max_iterations": max_iterations,
        "consiliums": record["consiliums"],
        "agent_calls": record["agent_calls"],
        "tests": record["tests_summary"],
        "elapsed": f"{elapsed}m",
        "elapsed_minutes": elapsed,
        "runner": runner,
        "worktree": record["worktree"],
        "branch": record["branch"],
        "log_dir": record["log_dir"],
        "outcome": record["outcome"],
        "outcome_reason": record["outcome_reason"],
    }


def _parse(value: str | None):
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coloured(status: str) -> str:
    colour = {
        "running": "cyan", "done": "green",
        "waiting-human": "yellow", "failed": "red", "cancelled": "dim",
    }.get(status, "white")
    return f"[{colour}]{status}[/{colour}]"


if __name__ == "__main__":
    app()
