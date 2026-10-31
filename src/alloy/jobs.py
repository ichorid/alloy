"""Detached commands an agent polls in bounded waits (`alloy job`).

An agent's prompt cache lives about 30 minutes. An agent blocked inside one
long shell command (a test suite, often queued behind other suites) comes
back to a cold cache and pays to re-read its whole context; an agent that
starts the command detached and polls it in waits shorter than the cache
lifetime starts a new turn on every return, which keeps the cache warm.

    alloy job start -- <cmd...>       # prints the job id
    alloy job wait [id] [--max 20m]   # the command's exit code once it ends;
                                      # STILL_RUNNING (3) after --max, DIED (70)
                                      # when the supervisor is gone without one
    alloy job status | log [id] [lines] | kill [id]

`id` defaults to the newest job. Every job is one directory under the jobs
dir (`ALLOY_JOBS_DIR`, else `$XDG_STATE_HOME/alloy/jobs`):

    cmd cwd started abandon  written by start
    supervisor               "<pid> <starttime>" of the detached supervisor
    child                    "<pid> <starttime>" of the command (own session)
    log                      the command's stdout+stderr
    polled                   mtime = the last time anyone waited on the job
    stop                     a stop request; its text is the reason
    rc                       the exit code, published atomically by the supervisor

The supervisor is a small Python process in its own session. It runs the
command, stops it on a stop request or when nobody has waited on it for the
abandonment timeout (`ALLOY_JOB_ABANDON`, default 45 min) -- TERM to the
whole tree, KILL after 5 s -- and always publishes an exit code. Alloy gives
every agent call its own jobs dir and stops whatever is still running there
when the call ends (`reap`): a job started with setsid escapes the
process-group kill that ends the harness, so it must be stopped explicitly.

Only the standard library and `alloy.procs` are imported here: the
supervisor runs this module for as long as the command runs.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from alloy.procs import pid_alive, signal_tree

STILL_RUNNING = 3
DIED = 70
USAGE = 2
DEFAULT_WAIT_MINUTES = 20.0
DEFAULT_ABANDON_MINUTES = 45.0

ENV_DIR = "ALLOY_JOBS_DIR"
ENV_WAIT = "ALLOY_JOB_WAIT"
ENV_ABANDON = "ALLOY_JOB_ABANDON"
ENV_PIDNS = "ALLOY_JOBS_PIDNS"
"""The pid namespace of the Alloy process that set up the call. A shell in a
different one (codex's own sandbox) loses every process it started when the
tool call returns, so `start` runs the command in the foreground there."""
ENV_BIN = "ALLOY_BIN"

STOP_GRACE_S = 5.0
SUPERVISOR_POLL_S = 0.5
WAIT_POLL_S = 1.0
KEEP_FINISHED_S = 2 * 86400
TAIL_FINISHED = 60
TAIL_DIED = 40
TAIL_RUNNING = 5

REASON_KILL = "stopped by `alloy job kill`"
REASON_CALL_ENDED = "stopped: the agent call that started it ended"


def _say(message: str) -> None:
    print(f"[alloy job] {message}", file=sys.stderr, flush=True)


def parse_duration(text: str | float | int) -> float:
    """`20m`, `45s`, `1h`, `1.5m` or plain seconds -> seconds."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(text).lower())
    if not match:
        raise ValueError(f"not a duration: {text!r} (use e.g. 20m, 90s, 1h)")
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def _env_duration(name: str, default_minutes: float, environ: Mapping[str, str] | None = None) -> float:
    raw = (environ if environ is not None else os.environ).get(name, "").strip()
    return parse_duration(raw) if raw else default_minutes * 60


def jobs_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    if env.get(ENV_DIR):
        return Path(env[ENV_DIR])
    state = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "alloy" / "jobs"


def _ago(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 60 else f"{seconds / 60:.0f} min"


# -- process identity --------------------------------------------------------


def _start_time(pid: int) -> str:
    """The kernel start time of `pid` (/proc stat field 22), "" if unreadable;
    with the pid it tells a live process from a later one reusing its pid."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _identity(pid: int) -> str:
    return f"{pid} {_start_time(pid)}".strip()


def _alive(identity: str | None) -> int | None:
    """The pid recorded in `identity` if that very process is still running."""
    if not identity:
        return None
    parts = identity.split()
    try:
        pid = int(parts[0])
    except (ValueError, IndexError):
        return None
    if not pid_alive(pid):
        return None
    if len(parts) > 1 and _start_time(pid) not in ("", parts[1]):
        return None
    return pid


# -- one job -----------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    dir: Path

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def log(self) -> Path:
        return self.dir / "log"

    def _read(self, name: str) -> str | None:
        try:
            return (self.dir / name).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def rc(self) -> int | None:
        raw = self._read("rc")
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    def started(self) -> float:
        try:
            return float(self._read("started") or 0)
        except ValueError:
            return 0.0

    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started())

    def supervisor(self) -> int | None:
        return _alive(self._read("supervisor"))

    def child(self) -> int | None:
        return _alive(self._read("child"))

    def stop_reason(self) -> str | None:
        return self._read("stop")

    def request_stop(self, reason: str) -> None:
        (self.dir / "stop").write_text(reason + "\n", encoding="utf-8")

    def touch_polled(self) -> None:
        (self.dir / "polled").touch()

    def polled_age(self) -> float:
        try:
            return time.time() - (self.dir / "polled").stat().st_mtime
        except OSError:
            return self.elapsed()

    def state(self) -> str:
        rc = self.rc()
        if rc is not None:
            return f"rc={rc}"
        return "RUNNING" if self.supervisor() else "DIED"

    def tail(self, lines: int) -> str:
        try:
            with open(self.log, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 256 * 1024))
                data = handle.read().decode("utf-8", "replace")
        except OSError:
            return ""
        return "\n".join(data.splitlines()[-lines:]) if lines > 0 else ""


def list_jobs(base: Path) -> list[Job]:
    """Every job under `base`, newest first."""
    try:
        dirs = [entry for entry in base.iterdir() if entry.is_dir() and (entry / "started").exists()]
    except OSError:
        return []
    return sorted((Job(path) for path in dirs), key=lambda job: (job.started(), job.id), reverse=True)


def resolve(base: Path, job_id: str | None) -> Job | None:
    if not job_id:
        jobs = list_jobs(base)
        if not jobs:
            _say(f"no jobs in {base}")
            return None
        return jobs[0]
    job = Job(base / job_id)
    if not (job.dir / "started").exists():
        _say(f"no job {job_id} in {base}")
        return None
    return job


# -- start -------------------------------------------------------------------


def _boot_argv(*args: str) -> list[str]:
    """Run this module's `_main` with the interpreter and source tree of the
    running Alloy, whatever `alloy` the venv's default path would import."""
    src = str(Path(__file__).resolve().parents[1])
    boot = f"import sys; sys.path.insert(0, {src!r}); from alloy.jobs import _main; sys.exit(_main(sys.argv[1:]))"
    return [sys.executable, "-c", boot, *args]


def _other_pid_namespace(environ: Mapping[str, str]) -> bool:
    expected = environ.get(ENV_PIDNS, "")
    if not expected:
        return False
    try:
        return os.readlink("/proc/self/ns/pid") != expected
    except OSError:
        return False


def _drop_old(base: Path) -> None:
    cutoff = time.time() - KEEP_FINISHED_S
    for job in list_jobs(base):
        if job.rc() is not None and job.started() < cutoff:
            shutil.rmtree(job.dir, ignore_errors=True)


def _new_job(base: Path) -> Job:
    stem = f"{time.strftime('%m%d-%H%M%S')}-{os.getpid()}"
    for n in range(1, 1000):
        path = base / (stem if n == 1 else f"{stem}-{n}")
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return Job(path)
    raise RuntimeError(f"no free job id under {base}")


def start(
    argv: Sequence[str],
    *,
    base: Path | None = None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """`alloy job start -- <cmd...>`: print the new job's id and return 0."""
    env = environ if environ is not None else os.environ
    if not argv:
        _say("usage: alloy job start -- <command...>")
        return USAGE
    if _other_pid_namespace(env):
        # Every process this shell starts dies with the tool call (a sandbox
        # with its own pid namespace): a detached job would only be killed.
        _say("this shell cannot keep a detached job alive (separate pid namespace); running it in the foreground")
        try:
            rc = subprocess.call(list(argv), cwd=cwd)
        except OSError as exc:
            _say(f"cannot run {shlex.join(argv)}: {exc}")
            return 127
        _say(f"finished rc={rc if rc >= 0 else 128 - rc}")
        return rc if rc >= 0 else 128 - rc
    base = base or jobs_dir(env)
    base.mkdir(parents=True, exist_ok=True)
    _drop_old(base)
    job = _new_job(base)
    cwd = Path(cwd or os.getcwd())
    (job.dir / "cwd").write_text(str(cwd) + "\n", encoding="utf-8")
    (job.dir / "cmd").write_text(shlex.join(argv) + "\n", encoding="utf-8")
    (job.dir / "abandon").write_text(f"{_env_duration(ENV_ABANDON, DEFAULT_ABANDON_MINUTES, env)}\n", encoding="utf-8")
    job.log.touch()
    job.touch_polled()
    (job.dir / "started").write_text(f"{time.time()}\n", encoding="utf-8")
    process = subprocess.Popen(
        _boot_argv("supervise", str(job.dir), *argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (job.dir / "supervisor").write_text(_identity(process.pid) + "\n", encoding="utf-8")
    # Return only once the command exists, so a reap right after `start`
    # finds its pid (or its exit code, if it could not be started).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (job.dir / "child").exists() and job.rc() is None:
        if process.poll() is not None:
            break
        time.sleep(0.02)
    print(job.id, flush=True)
    _say(f"started {job.id}: {shlex.join(argv)}")
    _say(f"next: alloy job wait {job.id}")
    return 0


# -- the supervisor ----------------------------------------------------------


def _publish(job: Job, rc: int) -> None:
    tmp = job.dir / "rc.tmp"
    tmp.write_text(f"{rc}\n", encoding="utf-8")
    tmp.replace(job.dir / "rc")


def _stop_tree(pid: int, process: subprocess.Popen) -> None:
    """TERM the command's whole tree, KILL what is left after STOP_GRACE_S."""
    signal_tree(pid, signal.SIGTERM)
    try:
        process.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    signal_tree(pid, signal.SIGKILL)


def supervise(job: Job, argv: Sequence[str]) -> int:
    # A signal is a stop request too: the harness's own tree kill reaches the
    # supervisor while `alloy job start` is still its parent.
    signals: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda signum, _frame: signals.append(signum))
    try:
        abandon_s = float(job._read("abandon") or DEFAULT_ABANDON_MINUTES * 60)
    except ValueError:
        abandon_s = DEFAULT_ABANDON_MINUTES * 60
    with open(job.log, "ab", buffering=0) as log:

        def note(message: str) -> None:
            log.write(f"[alloy job] {message}\n".encode())

        try:
            process = subprocess.Popen(
                list(argv),
                cwd=job._read("cwd") or None,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own group and session: one signal_tree reaches it all
            )
        except OSError as exc:
            note(f"cannot start {shlex.join(argv)}: {exc}")
            _publish(job, 127)
            return 127
        (job.dir / "child").write_text(_identity(process.pid) + "\n", encoding="utf-8")
        noted = False
        while process.poll() is None:
            reason = job.stop_reason()
            if reason is None and signals:
                reason = f"stopped: the supervisor received {signal.Signals(signals[0]).name}"
            if reason is None and job.polled_age() > abandon_s:
                reason = f"nobody waited on this job for {_ago(abandon_s)}; killing it as abandoned"
            if reason is not None:
                note(reason)
                noted = True
                _stop_tree(process.pid, process)
                break
            time.sleep(SUPERVISOR_POLL_S)
        rc = process.wait()
        if not noted and job.stop_reason():
            note(job.stop_reason() or "")  # stopped from outside (reap) before this loop saw it
    rc = rc if rc >= 0 else 128 - rc
    _publish(job, rc)
    return rc


# -- wait / status / log / kill ----------------------------------------------


def wait(job: Job, max_s: float | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    """`alloy job wait`: the command's exit code, STILL_RUNNING after
    `max_s`, DIED when the supervisor is gone without publishing one."""
    if max_s is None:
        max_s = _env_duration(ENV_WAIT, DEFAULT_WAIT_MINUTES, environ)
    deadline = time.monotonic() + max_s
    job.touch_polled()
    while job.rc() is None:
        if job.supervisor() is None and job.rc() is None:
            _print_tail(job, TAIL_DIED)
            _say(f"{job.id} died without an exit code after {_ago(job.elapsed())}; full log: {job.log}")
            return DIED
        if time.monotonic() >= deadline:
            job.touch_polled()
            _print_tail(job, TAIL_RUNNING)
            _say(f"{job.id} still running after {_ago(job.elapsed())}; next: alloy job wait {job.id}")
            return STILL_RUNNING
        job.touch_polled()
        time.sleep(max(0.05, min(WAIT_POLL_S, deadline - time.monotonic())))
    rc = job.rc()
    assert rc is not None
    _print_tail(job, TAIL_FINISHED)
    _say(f"{job.id} finished rc={rc} after {_ago(job.elapsed())}; full log: {job.log}")
    return rc


def _print_tail(job: Job, lines: int) -> None:
    tail = job.tail(lines)
    if tail:
        print(tail, flush=True)


def status(base: Path) -> int:
    for job in list_jobs(base):
        print(f"{job.id} {job.state()} {_ago(job.elapsed())} {job._read('cwd')}: {job._read('cmd')}")
    return 0


def kill(job: Job) -> int:
    """Ask the supervisor to stop the command; it still publishes an exit code."""
    if job.rc() is not None or job.supervisor() is None:
        _say(f"{job.id} is not running ({job.state()})")
        return 0
    job.request_stop(REASON_KILL)
    _say(f"stopping {job.id}; next: alloy job wait {job.id}")
    return 0


def reap(base: Path, *, reason: str = REASON_CALL_ENDED, grace_s: float = STOP_GRACE_S) -> list[str]:
    """Stop every job still running under `base` and return their ids: TERM
    each command's tree, KILL it after `grace_s`, then give the supervisors a
    moment to publish exit codes and exit (one that does not is killed too)."""
    running = [job for job in list_jobs(base) if job.supervisor() or job.child()]
    stopped = [job for job in running if job.rc() is None]
    for job in stopped:
        job.request_stop(reason)
    for job in running:
        if (pid := job.child()) is not None:
            signal_tree(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(job.child() for job in running):
        time.sleep(0.05)
    for job in running:
        if (pid := job.child()) is not None:
            signal_tree(pid, signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(job.supervisor() for job in running):
        time.sleep(0.05)
    for job in running:
        if (pid := job.supervisor()) is not None:
            signal_tree(pid, signal.SIGKILL)
    return [job.id for job in stopped]


# -- wiring an agent call ----------------------------------------------------


def write_shim(bin_dir: Path) -> Path:
    """`<bin_dir>/alloy`: runs this Alloy installation's CLI. Only this one
    file goes on the agent's PATH, never the venv's bin dir, whose `python`
    and `pip` would shadow the project's own."""
    src = str(Path(__file__).resolve().parents[1])
    boot = f"import sys; sys.path.insert(0, {src!r}); sys.argv[0] = 'alloy'; from alloy.cli import app; app()"
    text = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(boot)} "$@"\n'
    shim = bin_dir / "alloy"
    try:
        if shim.read_text(encoding="utf-8") == text:
            return shim
    except OSError:
        pass
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = bin_dir / f".alloy.{os.getpid()}.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o755)
    tmp.replace(shim)
    return shim


def agent_env(
    call_dir: Path,
    shim: Path,
    *,
    wait_minutes: float,
    abandon_minutes: float,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Variables for one agent call: its private jobs dir, the job limits,
    and `alloy` on PATH (also as $ALLOY_BIN)."""
    path = base_env.get("PATH", os.defpath)
    env = {
        ENV_DIR: str(call_dir),
        ENV_WAIT: f"{wait_minutes:g}m",
        ENV_ABANDON: f"{abandon_minutes:g}m",
        ENV_BIN: str(shim),
        "PATH": f"{shim.parent}{os.pathsep}{path}",
    }
    try:
        env[ENV_PIDNS] = os.readlink("/proc/self/ns/pid")
    except OSError:
        pass
    # Claude Code cuts a shell command at 2 min by default and at 10 min at
    # most; let one `alloy job wait` fit, unless the operator set these.
    bash_ms = str(int((wait_minutes + 1) * 60_000))
    for name in ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS"):
        current = base_env.get(name, "")
        if not (current.isdigit() and int(current) >= int(bash_ms)):
            env[name] = bash_ms
    return env


# -- entry point of the supervisor process -------------------------------------


def _main(args: list[str]) -> int:
    if len(args) >= 2 and args[0] == "supervise":
        job = Job(Path(args[1]))
        try:
            return supervise(job, args[2:])
        except Exception as exc:  # noqa: BLE001 -- the log is the only place anyone looks
            with open(job.log, "a", encoding="utf-8") as log:
                log.write(f"[alloy job] supervisor failed: {exc!r}\n")
            raise
    print("usage: python -m alloy.jobs supervise <job-dir> <command...>", file=sys.stderr)
    return USAGE


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
