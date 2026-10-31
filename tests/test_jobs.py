"""`alloy job`: detached commands an agent polls in bounded waits."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from alloy import jobs
from alloy.procs import pid_alive


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
