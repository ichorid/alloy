"""`alloy job`: detached commands an agent polls in bounded waits, and the
per-call wiring that stops whatever an agent left running."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import load_config, make_bead

from alloy import jobs
from alloy.config import ConfigError, Limits, RoleSpec
from alloy.procs import pid_alive
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import Store


@pytest.fixture
def job_cli(tmp_path: Path):
    """Runs `alloy job ...` through the shim an agent gets, in a private jobs dir."""
    shim = jobs.write_shim(tmp_path / "bin")
    base = tmp_path / "jobs"
    env = {**os.environ, jobs.ENV_DIR: str(base)}
    env.pop(jobs.ENV_PIDNS, None)

    def run(*args: str, extra_env: dict[str, str] | None = None, timeout: float = 60) -> SimpleNamespace:
        started = time.monotonic()
        proc = subprocess.run(
            [str(shim), "job", *args],
            env={**env, **(extra_env or {})},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SimpleNamespace(
            rc=proc.returncode,
            out=proc.stdout + proc.stderr,
            stdout=proc.stdout.strip(),
            took=time.monotonic() - started,
        )

    yield SimpleNamespace(run=run, base=base)
    jobs.reap(base, grace_s=1)


def _until(condition, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


# -- lifecycle ---------------------------------------------------------------


def test_wait_returns_the_commands_exit_code_output_and_log_path(job_cli):
    job_id = job_cli.run("start", "--", "bash", "-c", "echo hello-from-job; exit 7").stdout
    result = job_cli.run("wait", job_id, "--max", "30s")
    assert result.rc == 7
    assert "hello-from-job" in result.out
    assert str(job_cli.base / job_id / "log") in result.out


def test_bounded_wait_returns_still_running_within_max_and_names_the_next_call(job_cli):
    job_id = job_cli.run("start", "--", "sleep", "60").stdout
    result = job_cli.run("wait", "--max", "1s")  # no id: the newest job
    assert result.rc == jobs.STILL_RUNNING
    assert result.took < 10
    assert f"alloy job wait {job_id}" in result.out


def test_wait_without_an_id_takes_the_newest_job(job_cli):
    job_cli.run("start", "--", "bash", "-c", "exit 4")
    newest = job_cli.run("start", "--", "bash", "-c", "exit 5").stdout
    result = job_cli.run("wait", "--max", "30s")
    assert result.rc == 5
    assert newest in result.out


def test_kill_stops_the_command_and_still_publishes_an_exit_code(job_cli):
    job_id = job_cli.run("start", "--", "sleep", "60").stdout
    job_cli.run("kill", job_id)
    result = job_cli.run("wait", job_id, "--max", "30s")
    assert result.rc not in (0, jobs.STILL_RUNNING, jobs.DIED)
    assert jobs.REASON_KILL in (job_cli.base / job_id / "log").read_text()


def test_a_job_nobody_waits_on_is_killed_as_abandoned(job_cli):
    job_id = job_cli.run("start", "--", "sleep", "60", extra_env={jobs.ENV_ABANDON: "1s"}).stdout
    job = jobs.Job(job_cli.base / job_id)
    assert _until(lambda: job.rc() is not None)
    assert "killing it as abandoned" in job.log.read_text()


def test_a_dead_supervisor_is_reported_as_died(job_cli):
    job_id = job_cli.run("start", "--", "sleep", "60").stdout
    job = jobs.Job(job_cli.base / job_id)
    supervisor = job.supervisor()
    assert supervisor is not None
    os.kill(supervisor, 9)
    assert _until(lambda: job.supervisor() is None)
    result = job_cli.run("wait", job_id, "--max", "10s")
    assert result.rc == jobs.DIED
    os.kill(job.child(), 9)


def test_status_lists_every_job_with_its_state(job_cli):
    done = job_cli.run("start", "--", "true").stdout
    job_cli.run("wait", done, "--max", "30s")
    running = job_cli.run("start", "--", "sleep", "60").stdout
    lines = job_cli.run("status").stdout.splitlines()
    assert [line.split()[:2] for line in lines] == [[running, "RUNNING"], [done, "rc=0"]]


def test_start_runs_in_the_foreground_from_a_shell_in_another_pid_namespace(job_cli):
    # A sandbox with its own pid namespace (codex's) kills every process a
    # tool call started when it returns: a detached job would only die.
    result = job_cli.run("start", "--", "bash", "-c", "echo inline; exit 6", extra_env={jobs.ENV_PIDNS: "pid:[1]"})
    assert result.rc == 6
    assert "inline" in result.out
    assert not job_cli.base.exists()


def test_reap_stops_every_running_job_and_publishes_its_exit_code(tmp_path: Path):
    base = tmp_path / "jobs"
    env = {**os.environ, jobs.ENV_DIR: str(base)}
    env.pop(jobs.ENV_PIDNS, None)
    assert jobs.start(["sleep", "60"], base=base, cwd=tmp_path, environ=env) == 0
    assert jobs.start(["true"], base=base, cwd=tmp_path, environ=env) == 0
    sleeper, finished = sorted(jobs.list_jobs(base), key=lambda job: job.started())
    assert _until(lambda: finished.rc() is not None)
    child = sleeper.child()
    assert child is not None

    assert jobs.reap(base) == [sleeper.id]
    assert not pid_alive(child)
    assert sleeper.rc() not in (None, 0)
    assert jobs.REASON_CALL_ENDED in sleeper.log.read_text()


# -- one agent call ------------------------------------------------------------

FAKE_AGENT = """#!/bin/sh
env > "$(dirname "$0")/env-$1.txt"
alloy job start -- sleep 60 > "$(dirname "$0")/job-$1.txt"
[ "$1" = slow ] && sleep 30
echo finished
"""


def _agent_context(tmp_path: Path, **limits) -> RunContext:
    script = tmp_path / "agent" / "fake-agent"
    script.parent.mkdir()
    script.write_text(FAKE_AGENT)
    script.chmod(0o755)
    config = load_config()
    recipe = replace(config, limits=replace(config.limits, **limits))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return RunContext(
        bead=make_bead("t-jobs"),
        recipe=recipe,
        run_id="run-jobs",
        worktree=SimpleNamespace(path=tmp_path),
        worktrees=None,
        registry=RunnerRegistry({"fakeagent": {"binary": str(script), "args": ["{prompt}"]}}, log_dir=log_dir),
        store=Store(tmp_path / "alloy.db"),
        checkpointer=None,
        log_dir=log_dir,
    )


def _job_of(tmp_path: Path, mode: str, env: dict[str, str]) -> jobs.Job:
    job_id = (tmp_path / "agent" / f"job-{mode}.txt").read_text().strip()
    return jobs.Job(Path(env[jobs.ENV_DIR]) / job_id)


def _env_of(tmp_path: Path, mode: str) -> dict[str, str]:
    lines = (tmp_path / "agent" / f"env-{mode}.txt").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


@pytest.mark.parametrize(
    "mode,timeout_minutes",
    [("fast", 1.0), ("slow", 0.1)],
    ids=["call-ends", "call-times-out"],
)
async def test_a_job_the_agent_left_running_is_stopped_when_the_call_ends(tmp_path, mode, timeout_minutes):
    ctx = _agent_context(tmp_path)
    spec = RoleSpec(runner="fakeagent", timeout_minutes=timeout_minutes)
    result = await ctx.call("implement", spec, mode)
    assert result.ok is (mode == "fast")

    job = _job_of(tmp_path, mode, _env_of(tmp_path, mode))
    assert job.rc() is not None
    assert job.supervisor() is None
    assert job.child() is None
    assert jobs.REASON_CALL_ENDED in job.log.read_text()


async def test_a_job_is_stopped_when_its_call_is_cancelled(tmp_path):
    ctx = _agent_context(tmp_path)
    task = asyncio.ensure_future(ctx.call("implement", RoleSpec(runner="fakeagent", timeout_minutes=1), "slow"))
    job_file = tmp_path / "agent" / "job-slow.txt"
    deadline = time.monotonic() + 20
    while not (job_file.exists() and job_file.read_text().strip()) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    job = _job_of(tmp_path, "slow", _env_of(tmp_path, "slow"))
    assert job.rc() is not None
    assert job.child() is None and job.supervisor() is None


async def test_each_call_gets_its_own_jobs_dir_alloy_on_path_and_the_job_limits(tmp_path):
    ctx = _agent_context(tmp_path, job_wait_minutes=12, job_abandon_minutes=30)
    spec = RoleSpec(runner="fakeagent", timeout_minutes=1)
    await ctx.call("implement", spec, "fast")
    env = _env_of(tmp_path, "fast")

    jobs_dir = Path(env[jobs.ENV_DIR])
    assert jobs_dir.parent == ctx.log_dir / "jobs"
    assert env[jobs.ENV_WAIT] == "12m"
    assert env[jobs.ENV_ABANDON] == "30m"
    assert env[jobs.ENV_PIDNS] == os.readlink("/proc/self/ns/pid")
    shim_dir = env["PATH"].split(os.pathsep)[0]
    # Only `alloy` goes on the agent's PATH, never the venv's python or pip.
    assert sorted(os.listdir(shim_dir)) == ["alloy"]
    assert env[jobs.ENV_BIN] == str(Path(shim_dir) / "alloy")
    assert int(env["BASH_DEFAULT_TIMEOUT_MS"]) > 12 * 60_000

    await ctx.call("implement", spec, "fast")
    assert Path(_env_of(tmp_path, "fast")[jobs.ENV_DIR]) != jobs_dir


# -- configuration and prompts -------------------------------------------------


def test_job_limits_have_documented_defaults_and_must_leave_room_between_waits():
    limits = Limits.parse({})
    assert (limits.job_wait_minutes, limits.job_abandon_minutes) == (20.0, 45.0)
    assert Limits.parse({"job_wait_minutes": 10, "job_abandon_minutes": 15}).job_wait_minutes == 10
    with pytest.raises(ConfigError):
        Limits.parse({"job_wait_minutes": 50})
