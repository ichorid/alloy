"""Alloy's command line.

Every command is also an API: `--json` output is stable enough for a manager
agent to drive Alloy without a human in the loop.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Coroutine, Optional, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from alloy import beads as bd
from alloy import recipes
from alloy.config import ConfigError, RecipeConfig, RoleSpec, discover_recipes, load_recipe
from alloy.engine import Engine, EngineError
from alloy.monitor import build_snapshot
from alloy.paths import AlloyPaths
from alloy.runners import BUILTIN, RunnerRegistry
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

T = TypeVar("T")


def _engine(repo: Optional[Path], root: Optional[Path]) -> Engine:
    return Engine.open(repo or Path.cwd(), root)


def _setup_logging(verbose: bool = True) -> None:
    """Progress on stderr for the long-running commands; `--json` stays clean
    because JSON goes to stdout."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _run_async(coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` with SIGINT/SIGTERM turned into a cancellation.

    Cancellation unwinds through the graph into the runner, which kills the
    harness's process group, and leaves the run marked running with a dead
    pid -- i.e. resumable. The default SIGTERM disposition would have killed
    only this process and left the harness editing the worktree.
    """

    async def main() -> T:
        task = asyncio.ensure_future(coro)
        loop = asyncio.get_running_loop()
        installed = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, task.cancel)
                installed.append(sig)
            except (NotImplementedError, ValueError):
                pass
        try:
            return await task
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)

    return asyncio.run(main())


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
    for name in BUILTIN:
        if name != "generic":
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
    _setup_logging(verbose=not json)
    engine = _engine(repo, root)
    try:
        result = _run_async(engine.run(bead_id, recipe_name=recipe))
    except (EngineError, ConfigError, bd.BeadsError) as exc:
        _fail(str(exc))
        return
    except asyncio.CancelledError:
        err.print(f"[yellow]interrupted[/yellow] {bead_id}; resume with "
                  f"[bold]alloy run {bead_id}[/bold]")
        raise typer.Exit(130)

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
    _setup_logging(verbose=not json)
    engine = _engine(repo, root)
    try:
        result = _run_async(engine.resume(bead_id, message))
    except (EngineError, ConfigError, bd.BeadsError) as exc:
        _fail(str(exc))
        return
    except asyncio.CancelledError:
        err.print(f"[yellow]interrupted[/yellow] {bead_id}; it stays resumable")
        raise typer.Exit(130)
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
    """Stop the run (process included) and return the bead to ready. The
    worktree is kept."""
    _setup_logging(verbose=not json)
    engine = _engine(repo, root)
    try:
        cancelled = engine.cancel(bead_id)
    except EngineError as exc:
        _fail(str(exc))
        return
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
        pid = spawn_detached(engine.repo, engine.paths.root, poll,
                             log_file=engine.paths.scheduler_log)
        console.print(f"scheduler started (pid {pid}); log: {engine.paths.scheduler_log}")
        return
    _setup_logging()
    scheduler = Scheduler(engine=engine, poll_seconds=poll, recipe_filter=recipe)
    try:
        asyncio.run(scheduler.serve())
    except SchedulerBusy as exc:
        _fail(str(exc))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


@app.command()
def stop(
    root: Optional[Path] = RootOption,
    now: bool = typer.Option(False, "--now", help="Also cancel the task being run; "
                                                   "it stays resumable"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Signal the scheduler to stop after the current task (or right away)."""
    paths = AlloyPaths.resolve(root)
    pid = signal_stop(paths.scheduler_pid, now=now)
    if json:
        _emit({"stopped": pid is not None, "pid": pid, "now": now}, True)
        return
    console.print(f"sent stop{' --now' if now else ''} to pid {pid}" if pid
                  else "scheduler is not running")


@app.command()
def status(
    bead_id: Optional[str] = typer.Argument(None, help="Limit to one bead"),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    limit: int = typer.Option(50, "--limit", help="Cap when listing every bead"),
) -> None:
    """Show every bead Alloy tracks -- queued, running, or finished -- with its
    place in the schedule. Designed to be read by humans and by agents."""
    engine = _engine(repo, root)
    scheduler_pid = read_pid(engine.paths.scheduler_pid)

    if bead_id:
        try:
            beads_list = [engine.beads.show(bead_id)]
        except bd.BeadsError as exc:
            _fail(str(exc))
            return
    else:
        beads_list = engine.beads.alloy_beads()

    ready_ids = {b.id for b in engine.beads.ready(limit=max(limit, 1000))}
    queue_order = sorted((b for b in beads_list if b.id in ready_ids),
                         key=lambda b: (b.priority, b.id))
    queue_position = {b.id: index + 1 for index, b in enumerate(queue_order)}

    rows = [_bead_row(engine, b, ready_ids) for b in beads_list]
    for row in rows:
        row["queue_position"] = queue_position.get(row["bead"])
    rows.sort(key=_status_sort_key)
    if not bead_id:
        rows = rows[:limit]

    payload = {
        "root": str(engine.paths.root),
        "repo": str(engine.repo),
        "scheduler": {"running": scheduler_pid is not None, "pid": scheduler_pid},
        "beads": rows,
    }
    if json:
        _emit(payload, True)
        return

    console.print(
        f"scheduler: [{'green' if scheduler_pid else 'yellow'}]"
        f"{'running (pid ' + str(scheduler_pid) + ')' if scheduler_pid else 'stopped'}[/]"
    )
    if not rows:
        console.print("alloy is not tracking any beads yet")
        return
    table = Table(show_header=True, header_style="bold", expand=True)
    # One line per bead, whatever the terminal width: the title gives way
    # first, identifiers and numbers keep their minimum widths.
    for column, min_width in (("bead", 12), ("title", 12), ("pri", 3), ("queue", 5),
                              ("status", 9), ("stage", 9), ("agent", 14), ("iter", 4),
                              ("tests", 18), ("elapsed", 7)):
        table.add_column(column, no_wrap=True, overflow="ellipsis", min_width=min_width,
                         ratio=4 if column == "title" else (2 if column == "agent" else None))
    for row in rows:
        queue = str(row["queue_position"]) if row["queue_position"] else "-"
        max_iter = row["max_iterations"] if row["max_iterations"] is not None else "-"
        table.add_row(
            row["bead"], _truncate(row["title"], 32), str(row["priority"]), queue,
            _coloured(row["status"] or row["bead_status"]), row["stage"] or "-",
            _agent_label(row), f"{row['iteration']}/{max_iter}",
            row["tests"] or "-", row["elapsed"],
        )
    console.print(table)


@app.command()
def monitor(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    once: bool = typer.Option(False, "--once", help="Take one snapshot and exit"),
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Live view of the scheduler, the queue and every active run.

    `--once --json` prints a single snapshot (the frozen shape from
    docs/plans/execution-monitor.md) and exits; the interactive view is the
    default without those flags."""
    engine = _engine(repo, root)
    if once and json:
        _emit(build_snapshot(engine), True)
        return
    _fail("the interactive monitor is not available yet; use `alloy monitor --once --json`")


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
        # The recipe's own `runners:` block (api keys, binaries) decides what
        # is available -- a bare registry would call Jev "missing" forever.
        registry = RunnerRegistry(config.runners)

        def describe(spec: "RoleSpec") -> dict[str, Any]:
            info: dict[str, Any] = {"runner": spec.runner, "model": spec.model,
                                    "available": registry.available(spec.runner)}
            if spec.fallback is not None:
                info["fallback"] = describe(spec.fallback)
            return info

        entry["roles"] = {role: describe(spec) for role, spec in config.roles.items()}
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
            console.print(f"  {role:<10} {_role_label(info)}")
        critics = ", ".join(
            f"{c['runner']}{'' if c['available'] else '(missing)'}" for c in entry["critics"]
        )
        console.print(f"  critics    {critics or '(none)'}")
        console.print(f"  limits     {entry['limits']}")


# --------------------------------------------------------------------------


def _role_label(info: dict[str, Any]) -> str:
    mark = "" if info["available"] else " [yellow](runner missing)[/yellow]"
    model = f":{info['model']}" if info.get("model") else ""
    label = f"{info['runner']}{model}{mark}"
    if info.get("fallback"):
        label += f"  [dim]-> fallback[/dim] {_role_label(info['fallback'])}"
    return label


_STAGE_ROLES = ("context", "tests", "implement", "judge")


def _current_agent(
    engine: Engine, config: RecipeConfig | None, record: dict[str, Any]
) -> dict[str, Any]:
    """The role/runner/model actually behind this run right now.

    `record["stage"]` names the *current* graph node (set by `ctx.set_stage`
    as each node starts), which can be well ahead of the *last completed*
    agent call in `agent_calls` -- e.g. stage "implement" while the most
    recently finished call was still "tests", because the implement call
    itself hasn't returned yet. Showing the last completed call there would
    claim "tests" is still going on when it manifestly isn't (the stage
    already says otherwise), so: when the stage names one of the roles this
    recipe configures directly, trust the stage and show *that* role's
    configured runner/model (resolving the `astra`-style alias to what will
    actually execute) -- it is either running right now or about to be. Only
    fall back to the last completed call for stages with no directly agent
    (`verify`/`baseline` are deterministic Python, not an agent call) or once
    the run has moved somewhere this function doesn't special-case.
    """
    from alloy.runners import ALIASES
    from alloy.store import TERMINAL_RUN_STATUSES

    stage = record.get("stage")
    if record.get("status") in TERMINAL_RUN_STATUSES or record.get("status") == "waiting-human":
        return {"role": None, "runner": None, "model": None}  # nothing is running

    if config and stage in _STAGE_ROLES:
        spec = config.roles.get(stage)
        if spec:
            # A failed primary call for this very stage/iteration means the
            # fallback is what is running now.
            calls = engine.store.agent_calls(record["run_id"])
            last = calls[-1] if calls else None
            while (
                spec.fallback is not None and last is not None
                and last["role"] == stage and not last["ok"]
                and last["iteration"] == record.get("iteration", 0)
                and ALIASES.get(spec.runner, spec.runner) == ALIASES.get(last["runner"], last["runner"])
            ):
                spec = spec.fallback
                calls = [c for c in calls if c is not last]
                last = calls[-1] if calls else None
            runner = ALIASES.get(spec.runner, spec.runner)
            return {"role": stage, "runner": runner, "model": spec.model}
    if config and stage == "consilium":
        return {"role": stage, "runner": "multiple critics", "model": None}
    if config and stage == "synthesize":
        spec = config.consilium.synthesizer
        runner = ALIASES.get(spec.runner, spec.runner)
        return {"role": stage, "runner": runner, "model": spec.model}

    calls = engine.store.agent_calls(record["run_id"])
    if calls:
        last = calls[-1]
        return {"role": last["role"], "runner": last["runner"], "model": last["model"]}
    return {"role": stage, "runner": None, "model": None}


def _status_row(engine: Engine, record: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timezone

    try:
        config = engine.load_config(record["recipe"])
        max_iterations = config.limits.max_iterations
    except ConfigError:
        config, max_iterations = None, 0

    agent = _current_agent(engine, config, record)

    started = _parse(record["started_at"])
    ended = _parse(record["ended_at"]) if record["ended_at"] else None
    paused_at = _parse(record.get("paused_at")) if record.get("paused_at") else None
    reference = ended or paused_at or datetime.now(timezone.utc)
    paused_s = float(record.get("paused_s") or 0)  # time parked or dead: not work
    elapsed = (
        max(0, int(((reference - started).total_seconds() - paused_s) // 60)) if started else 0
    )

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
        "agent_role": agent["role"],
        "runner": agent["runner"],
        "model": agent["model"],
        "worktree": record["worktree"],
        "branch": record["branch"],
        "log_dir": record["log_dir"],
        "outcome": record["outcome"],
        "outcome_reason": record["outcome_reason"],
    }


_EMPTY_RUN_FIELDS: dict[str, Any] = {
    "run_id": None, "status": None, "stage": None, "iteration": 0,
    "max_iterations": None, "consiliums": 0, "agent_calls": 0, "tests": None,
    "elapsed": "-", "elapsed_minutes": 0, "agent_role": None, "runner": None,
    "model": None, "worktree": None, "branch": None, "log_dir": None,
    "outcome": None, "outcome_reason": None,
}

# Beads not currently offered by `bd ready` (running, waiting on a human, or
# already terminal) sort first by this rank; a bead that *is* ready sorts by
# its queue position instead (see `_status_sort_key`).
_STATUS_RANK = {
    bd.STATUS_IMPLEMENTING: 0,
    bd.STATUS_WAITING_HUMAN: 1,
    bd.STATUS_REVIEW_READY: 3,
    bd.STATUS_FAILED: 4,
    bd.STATUS_DONE: 5,
}


def _bead_row(engine: Engine, bead: bd.Bead, ready_ids: set[str]) -> dict[str, Any]:
    """One bead, merged with its most recent run (if it has ever been run)."""
    row: dict[str, Any] = {
        "bead": bead.id,
        "title": bead.title,
        "priority": bead.priority,
        "bead_status": bead.status,
        "ready": bead.id in ready_ids,
        "recipe": bead.recipe,
    }
    record = engine.store.latest_run_for_bead(bead.id)
    if record:
        run_row = _status_row(engine, record)
        run_row.pop("bead", None)
        run_row.pop("recipe", None)
        row.update(run_row)
    else:
        row.update(_EMPTY_RUN_FIELDS)
    return row


def _status_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    if row["ready"]:
        rank = 2
    else:
        rank = _STATUS_RANK.get(row["bead_status"], 2)
    return (rank, row["priority"], row["bead"])


def _agent_label(row: dict[str, Any]) -> str:
    if not row.get("runner"):
        return "-"
    model = f":{row['model']}" if row.get("model") else ""
    return f"{row.get('agent_role') or '?'}:{row['runner']}{model}"


def _truncate(text: str | None, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


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
