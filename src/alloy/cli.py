"""Alloy's command line.

Every command is also an API: `--json` output is stable enough for a manager
agent to drive Alloy without a human in the loop.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.table import Table
from typer.core import TyperCommand

from alloy import beads as bd
from alloy.cli_common import T as T
from alloy.cli_common import _emit as _emit
from alloy.cli_common import _engine as _engine
from alloy.cli_common import _fail as _fail
from alloy.cli_common import _run_async as _run_async
from alloy.cli_common import console as console
from alloy.cli_common import err as err
from alloy.config import (
    ConfigError,
    discover_recipes,
)
from alloy.engine import Engine, EngineError
from alloy.events import ATTENTION_EVENTS, EventLog, format_line, parse_since
from alloy.limits import probe_all
from alloy.memory_commands import _apply_review as _apply_review
from alloy.memory_commands import _review_plan as _review_plan
from alloy.memory_commands import memory_embed_impl as memory_embed_impl
from alloy.memory_commands import memory_list_impl as memory_list_impl
from alloy.memory_commands import memory_review_impl as memory_review_impl
from alloy.memory_schedule import (
    MEMORY_REVIEW_RECIPE,
)
from alloy.models import (
    utcnow,
    with_provenance,
)
from alloy.monitor import build_snapshot
from alloy.monitor.app import MonitorApp
from alloy.monitor.icons import resolve_mode
from alloy.monitor.render import (
    COLUMNS,
    activity_line,
    header_line,
    limits_lines,
    run_rows,
)
from alloy.paths import AlloyPaths
from alloy.recipe_commands import _probe_tiers as _probe_tiers
from alloy.recipe_commands import recipes_command_impl as recipes_command_impl
from alloy.runners import BUILTIN, RunnerRegistry
from alloy.scheduler import (
    DEFAULT_STALL_MINUTES,
    Scheduler,
    SchedulerBusy,
    read_pid,
    signal_stop,
    spawn_detached,
)
from alloy.status_command import status_impl as status_impl
from alloy.status_display import _EMPTY_RUN_FIELDS as _EMPTY_RUN_FIELDS
from alloy.status_display import _STAGE_ROLES as _STAGE_ROLES
from alloy.status_display import _STATUS_RANK as _STATUS_RANK
from alloy.status_display import _agent_label as _agent_label
from alloy.status_display import _bead_row as _bead_row
from alloy.status_display import _coloured as _coloured
from alloy.status_display import _current_agent as _current_agent
from alloy.status_display import _landing_of as _landing_of
from alloy.status_display import _parse as _parse
from alloy.status_display import _role_label as _role_label
from alloy.status_display import _status_row as _status_row
from alloy.status_display import _status_sort_key as _status_sort_key
from alloy.status_display import _truncate as _truncate
from alloy.store import Store
from alloy.verify import check_logs

app = typer.Typer(
    name="alloy",
    help="Durable, adaptive coding-agent workflows over Beads + git worktrees.",
    no_args_is_help=True,
    add_completion=False,
)

RepoOption = typer.Option(None, "--repo", help="Repository root (default: cwd)")
RootOption = typer.Option(
    None,
    "--root",
    help="Project state directory (default: $ALLOY_ROOT or <repo>/.alloy)",
)


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


# --------------------------------------------------------------------------


@app.command()
def init(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Create the project's state directory and register its statuses with Beads."""
    repo_path = (repo or Path.cwd()).resolve()
    paths = AlloyPaths.resolve(root, project=repo_path).ensure()
    Store(paths.alloy_db)

    report: dict[str, Any] = {
        "root": str(paths.root),
        "repo": str(repo_path),
        "recipes": sorted(discover_recipes(paths.shared_root, repo_path)),
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

    console.print(f"[bold]Project state[/bold]  {paths.root}")
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
        err.print(f"[yellow]interrupted[/yellow] {bead_id}; resume with [bold]alloy run {bead_id}[/bold]")
        raise typer.Exit(130)

    payload = {
        "bead": result.bead_id,
        "run_id": result.run_id,
        "outcome": result.outcome,
        "reason": result.reason,
        "worktree": result.worktree,
        "interrupt": result.interrupt,
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


class _OptionalValueCommand(TyperCommand):
    """Typer has no optional-value options. Let ``--remember`` stand alone by
    rewriting a bare flag (followed by another option or nothing) to
    ``--remember=`` before Click parses it."""

    optional_value_opts = ("--remember",)

    def parse_args(self, ctx, args):
        fixed = []
        for i, arg in enumerate(args):
            nxt = args[i + 1] if i + 1 < len(args) else None
            if arg in self.optional_value_opts and (nxt is None or nxt.startswith("-")):
                arg = f"{arg}="
            fixed.append(arg)
        return super().parse_args(ctx, fixed)


@app.command()
def land(
    bead_id: str = typer.Argument(..., help="Review-ready bead or finished epic to land"),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Land finished work: run the land recipe, then merge the bead branch
    into the primary checkout. Parks at waiting-human when the primary
    checkout refuses the merge."""
    _setup_logging(verbose=not json)
    engine = _engine(repo, root)
    try:
        result = _run_async(engine.land(bead_id))
    except (EngineError, ConfigError, bd.BeadsError) as exc:
        _fail(str(exc))
        return
    except asyncio.CancelledError:
        err.print(f"[yellow]interrupted[/yellow] {bead_id}; the bead stays review-ready")
        raise typer.Exit(130)
    bead = engine.beads.show(bead_id)
    payload = {
        "bead": bead_id,
        "run_id": result.run_id,
        "outcome": result.outcome,
        "reason": result.reason,
        "landing": _landing_of(bead),
    }
    if json:
        _emit(payload, True)
        return
    console.print(f"[green]landed[/green] {bead_id} -- {payload['landing']['sha']}")


@app.command(cls=_OptionalValueCommand)
def resume(
    bead_id: str = typer.Argument(...),
    message: str = typer.Option("", "--message", "-m", help="Guidance for the human gate"),
    remember: Optional[str] = typer.Option(
        None,
        "--remember",
        metavar="[KEY]",
        help="Store the -m message in project memory before resuming; "
        "optional KEY overrides the default alloy:human:<bead-id>",
    ),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Continue a paused or crashed run from its last checkpoint."""
    _setup_logging(verbose=not json)
    engine = _engine(repo, root)
    try:
        if remember is not None:
            _remember_human_note(engine, bead_id, message, remember)
        result = _run_async(engine.resume(bead_id, message))
    except (EngineError, ConfigError, bd.BeadsError) as exc:
        _fail(str(exc))
        return
    except asyncio.CancelledError:
        err.print(f"[yellow]interrupted[/yellow] {bead_id}; it stays resumable")
        raise typer.Exit(130)
    payload = {
        "bead": result.bead_id,
        "run_id": result.run_id,
        "outcome": result.outcome,
        "reason": result.reason,
    }
    if json:
        _emit(payload, True)
        return
    console.print(f"{result.outcome} {result.bead_id} -- {result.reason}")


def _remember_human_note(engine: Engine, bead_id: str, message: str, key: str) -> None:
    """Persist the resume guidance as a provenance-stamped project memory."""
    if not message.strip():
        raise EngineError("--remember needs a -m/--message to store")
    record = engine.store.latest_run_for_bead(bead_id)
    if record is None:
        raise EngineError(f"no run found for {bead_id}")
    engine.beads.remember(
        key or f"alloy:human:{bead_id}",
        with_provenance(message.strip(), record["run_id"], bead_id, utcnow().date()),
    )


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
    stall_minutes: float = typer.Option(
        DEFAULT_STALL_MINUTES,
        "--stall-minutes",
        help="Announce a running run with no activity for this long (0 disables)",
    ),
    recipe: Optional[str] = typer.Option(
        None,
        "--recipe",
        help="Force this recipe as the session default for unassigned beads "
        "(overrides alloy:default:recipe memory; errors if unknown)",
    ),
    foreground: bool = typer.Option(False, "--foreground", help="Do not detach"),
) -> None:
    """Start the scheduler: poll for ready beads and run them one at a time."""
    engine = _engine(repo, root)
    if recipe is not None:
        try:
            engine.validate_recipe(recipe)
        except (EngineError, ConfigError) as exc:
            _fail(str(exc))
            return
    if not foreground:
        existing = read_pid(engine.paths.scheduler_pid)
        if existing:
            _fail(f"scheduler already running (pid {existing})")
        pid = spawn_detached(
            engine.repo,
            engine.paths.root,
            poll,
            stall_minutes=stall_minutes,
            recipe=recipe,
            log_file=engine.paths.scheduler_log,
        )
        console.print(f"scheduler started (pid {pid}); log: {engine.paths.scheduler_log}")
        return
    _setup_logging()
    scheduler = Scheduler(
        engine=engine,
        poll_seconds=poll,
        recipe_filter=recipe,
        stall_minutes=stall_minutes,
    )
    try:
        asyncio.run(scheduler.serve())
    except SchedulerBusy as exc:
        _fail(str(exc))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


@app.command()
def events(
    root: Optional[Path] = RootOption,
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new events as they happen"),
    since: Optional[str] = typer.Option(
        None, "--since", help="History from this far back (30m, 2h, 1d) or an ISO time"
    ),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated kinds: needs-human,failed,stalled,done,resumed,cancelled",
    ),
    attention: bool = typer.Option(False, "--attention", help="Shortcut for --only needs-human,failed,stalled"),
    json: bool = typer.Option(False, "--json", help="One JSON object per line"),
) -> None:
    """The attention feed: what needs a human, failed, stalled or finished.

    Without flags, prints the whole history. `--follow` waits for new events
    (add `--since 1h` to replay recent ones first), one line per event, so a
    supervising agent can block on it instead of polling `alloy status`."""
    paths = AlloyPaths.resolve(root)
    log_ = EventLog(paths.root / "events.jsonl")
    kinds = frozenset(k.strip() for k in only.split(",") if k.strip()) if only else None
    if attention:
        kinds = ATTENTION_EVENTS if kinds is None else kinds | ATTENTION_EVENTS
    try:
        start = parse_since(since) if since else None
    except ValueError as exc:
        _fail(str(exc))
        return

    def show(record: dict[str, Any]) -> None:
        line = jsonlib.dumps(record, default=str) if json else format_line(record)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    if not follow:
        for record in log_.read(since=start, only=kinds):
            show(record)
        return
    try:
        for record in log_.follow(since=start, only=kinds):
            show(record)
    except KeyboardInterrupt:
        pass


@app.command()
def stop(
    root: Optional[Path] = RootOption,
    now: bool = typer.Option(False, "--now", help="Also cancel the task being run; it stays resumable"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Signal the scheduler to stop after the current task (or right away)."""
    paths = AlloyPaths.resolve(root)
    pid = signal_stop(paths.scheduler_pid, now=now)
    if json:
        _emit({"stopped": pid is not None, "pid": pid, "now": now}, True)
        return
    console.print(f"sent stop{' --now' if now else ''} to pid {pid}" if pid else "scheduler is not running")


@app.command()
def status(
    bead_id: Optional[str] = typer.Argument(None, help="Limit to one bead"),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    limit: int = typer.Option(50, "--limit", help="Cap when listing every bead"),
) -> None:
    return status_impl(bead_id, repo, root, json, limit)


@app.command()
def monitor(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    once: bool = typer.Option(False, "--once", help="Take one snapshot and exit"),
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    interval: float = typer.Option(1.0, "--interval", help="Refresh interval in seconds"),
    limits_interval: float = typer.Option(
        60.0,
        "--limits-interval",
        help="Limits probe interval in seconds",
    ),
    limits_style: str = typer.Option(
        "remaining",
        "--limits-style",
        help="Show harness capacity as remaining or spent",
    ),
    no_limits: bool = typer.Option(False, "--no-limits", help="Skip limits probing"),
) -> None:
    """Live view of the scheduler, the queue and every active run.

    `--once --json` prints a single snapshot (the frozen shape from
    docs/plans/execution-monitor.md) and exits; `--once` alone prints it as a
    plain table; the interactive Textual view is the default without flags."""
    if limits_style not in ("remaining", "spent"):
        raise typer.BadParameter("must be 'remaining' or 'spent'", param_hint="--limits-style")
    engine = _engine(repo, root)
    if once and json:
        _emit(build_snapshot(engine), True)
        return
    if once:
        snapshot = build_snapshot(engine)
        mode = resolve_mode(interactive=False)
        console.print(header_line(snapshot, mode))
        rendered_limits = limits_lines(
            snapshot,
            mode=mode,
            width=console.width,
            usage_style=limits_style,
        )
        if rendered_limits:
            console.print(f"limits ({limits_style})")
        for line in rendered_limits:
            console.print(line)
        console.print(activity_line(snapshot))
        table = Table(show_header=True, header_style="bold")
        for column in COLUMNS:
            table.add_column(column)
        for row in run_rows(snapshot):
            table.add_row(*row)
        console.print(table)
        return
    limits_source = None
    if not no_limits:

        def limits_source():
            return probe_all(engine.paths, RunnerRegistry(), home=Path.home())

    app = MonitorApp(
        snapshot_source=lambda: build_snapshot(engine),
        interval=interval,
        limits_source=limits_source,
        limits_interval=limits_interval,
        limits_style=limits_style,
    )
    app.run()
    # In-flight refresh workers (a bd call or the limits probe) are non-daemon
    # threads; joining them at interpreter exit made quitting take many seconds.
    # The terminal is already restored and the view holds no state to flush.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


@app.command()
def limits(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Probe installed harness usage limits and refresh limits.json."""
    engine = _engine(repo, root)
    samples = probe_all(engine.paths, RunnerRegistry(), home=Path.home())
    if json:
        _emit(samples, True)
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("harness", "window", "used", "resets", "as of", "note"):
        table.add_column(column)
    for harness, sample in sorted(samples.items()):
        note = sample.get("error") or sample.get("status") or ""
        as_of = sample.get("as_of") or ""
        windows = sample.get("windows") or []
        if not windows:
            table.add_row(harness, "", "", "", as_of, note)
            continue
        for win in windows:
            used = f"{win['used_percent']:.0f}%"
            resets = win.get("resets_at") or ""
            table.add_row(harness, win.get("label", ""), used, resets, as_of, note)
    console.print(table)


@app.command()
def logs(
    bead_id: str = typer.Argument(...),
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
    tail: int = typer.Option(20, "--tail", help="Most recent agent calls to show"),
) -> None:
    """List the agent calls and the checks of a run and where their raw
    transcripts and outputs live."""
    engine = _engine(repo, root)
    record = engine.store.latest_run_for_bead(bead_id)
    if record is None:
        _fail(f"no run recorded for {bead_id}")
        return
    runs = [record, *engine.store.children_of(record["run_id"])]
    calls = sorted(
        (call for run in runs for call in engine.store.agent_calls(run["run_id"])),
        key=lambda call: call["id"],
    )[-tail:]
    checks = check_logs(record["log_dir"])
    if json:
        _emit(
            {
                "run_id": record["run_id"],
                "log_dir": record["log_dir"],
                "calls": calls,
                "checks": checks,
            },
            True,
        )
        return
    console.print(f"[bold]run[/bold] {record['run_id']}   [bold]logs[/bold] {record['log_dir']}")
    table = Table(show_header=True, header_style="bold")
    for column in (
        "#",
        "bead",
        "role",
        "runner",
        "model",
        "iter",
        "secs",
        "exit",
        "prefix",
        "artifact",
    ):
        table.add_column(column)
    for index, call in enumerate(calls, 1):
        table.add_row(
            str(index),
            call["bead_id"],
            call["role"],
            call["runner"],
            call["model"] or "-",
            str(call["iteration"]),
            f"{call['duration_s']:.1f}",
            str(call["exit_code"]),
            (call.get("prefix_hash") or "-")[:12],
            call["log_path"] or "-",
        )
    console.print(table)
    if not checks:
        return
    table = Table(show_header=True, header_style="bold", title="checks (in start order)")
    for column in ("#", "kind", "exit", "command", "artifact"):
        table.add_column(column)
    for index, check in enumerate(checks, 1):
        exit_code = check["exit_code"]
        table.add_row(
            str(index),
            check["kind"],
            "-" if exit_code is None else str(exit_code),
            check["command"],
            check["log_path"],
        )
    console.print(table)


memory_app = typer.Typer(help="Inspect project memory (bd memories) as Alloy sees it.", no_args_is_help=True)
app.add_typer(memory_app, name="memory")


@memory_app.command(name="list")
def memory_list(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    return memory_list_impl(repo, root, json)


@memory_app.command(name="review")
def memory_review(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    recipe: str = typer.Option(
        MEMORY_REVIEW_RECIPE,
        "--recipe",
        help="Recipe whose memory settings and memory_reviewer role to use",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Execute the plan: alloy-owned verdicts are applied, human-owned ones become proposals on a review bead",
    ),
    json: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    return memory_review_impl(repo, root, recipe, apply, json)


@memory_app.command(name="embed")
def memory_embed(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    recipe: str = typer.Option(MEMORY_REVIEW_RECIPE, "--recipe", help="Recipe whose memory settings to use"),
) -> None:
    return memory_embed_impl(repo, root, recipe)


@app.command(name="recipes")
def recipes_command(
    repo: Optional[Path] = RepoOption,
    root: Optional[Path] = RootOption,
    json: bool = typer.Option(False, "--json"),
    probe: bool = typer.Option(False, "--probe", help="Smoke-test each distinct tier entry"),
    recipe: Optional[str] = typer.Option(None, "--recipe", help="Limit to one recipe"),
) -> None:
    return recipes_command_impl(repo, root, json, probe, recipe)


# --------------------------------------------------------------------------


# Beads not currently offered by `bd ready` (running, waiting on a human, or
# already terminal) sort first by this rank; a bead that *is* ready sorts by
# its queue position instead (see `_status_sort_key`).


if __name__ == "__main__":
    app()
