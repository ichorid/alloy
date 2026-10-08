"""Per-run bubblewrap sandbox with a private, size-capped tmpfs /tmp.

Agents and check commands leave multi-GB scratch in the system /tmp (a RAM
tmpfs shared by everything). Each bead run therefore gets ONE mount namespace
whose /tmp, /var/tmp and /dev/shm are private tmpfs mounts with a size cap;
when the run's processes are gone, so is everything they wrote there.

Design -- a namespace *holder*, joined with nsenter
---------------------------------------------------
Runs execute inside the scheduler (or `alloy run`) process; its pid is the
run's recorded pid, and adoption, `alloy cancel`, `alloy stop --now`, stall
detection ("run process is gone") and harness process-group killing are all
built on that. Moving the engine into a bwrap child would change every one of
those paths. Instead, at run start Alloy launches a tiny holder:

    bwrap --die-with-parent --bind / / --dev /dev \
          --perms 1777 --size N --tmpfs /tmp  (+ /var/tmp, /dev/shm) \
          [--bind-try P P ...] -- /bin/sh -c 'echo $$; read -r _'   # lives until its stdin pipe closes

and every agent subprocess and check command of the run is started as
``nsenter -t <holder> -U -m --preserve-credentials --root --wd=<cwd> -- argv``:
it joins the holder's user + mount namespace, so all of them share the same
private /tmp. nsenter only needs to fork for a pid namespace, which is not
used, so it *execs* the command: the pid the runner records, the process
group it kills and the exit code it reads are the command's own.

* No ``--unshare-pid``: pids stay host pids, so the ledger's harness pids,
  ``kill``/``killpg`` and ``_pid_alive`` keep working, and no ``--proc`` is
  needed (host /proc comes through the ``--bind / /``).
* The holder dies with the process that runs the run: it blocks reading a
  pipe only that process holds, so it exits on EOF however that process dies
  (``--die-with-parent`` is set too, but in bwrap 0.11 it only takes bwrap
  down, not the sandboxed child). The namespace (and its tmpfs) lives on only
  as long as some process is still inside it; when the last one exits the
  kernel frees the tmpfs.
* The host filesystem stays visible and writable as before (repo, $HOME
  caches, ~/.codex ~/.claude ~/.cursor, bd's dolt, git, /run/user/<uid>,
  the docker socket). ``--dev /dev`` is a fresh minimal /dev (null, zero,
  random, urandom, tty, pts, shm): host devices such as /dev/kvm or /dev/dri
  are not there unless listed in ``sandbox.share``.
* Mounts in a user namespace are nosuid: setuid helpers (sudo, ping) do
  not work inside.

Docker caveat: dockerd resolves bind-mount sources in the HOST mount
namespace, so ``docker run -v /tmp/x:/x`` from inside the sandbox mounts the
host's /tmp/x, not the sandbox's. Paths that must be host-shared go in
``sandbox.share`` (bound through from the host over the private /tmp).

Fallback: when bwrap is unavailable or its startup probe fails, ``auto``
logs a warning and runs unsandboxed with a per-run TMPDIR (removed at run
end). ``bwrap`` mode refuses to start the run instead. ``off`` does nothing.
``ALLOY_SANDBOX=off|auto|bwrap`` in the environment overrides every recipe.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

log = logging.getLogger("alloy.sandbox")

SANDBOX_MODES = ("auto", "bwrap", "off")
ENV_MODE = "ALLOY_SANDBOX"
DEFAULT_TMP_SIZE = "auto"
AUTO_SLICE_FRACTION = 0.5
"""`tmp_size: auto` = half of the memory limit of the slice Alloy runs in."""
NO_SLICE_FRACTION = 0.25
"""Without a detectable slice limit, `auto` falls back to this share of RAM."""
SIDE_TMPFS_FRACTION = 0.25
"""/var/tmp and /dev/shm are each capped at this share of the /tmp cap."""
ALWAYS_SHARED = ("/tmp/.X11-unix", "/tmp/.ICE-unix")
"""Host sockets under /tmp that stay visible inside (only when they exist)."""
SLICE_NAME = "alloy.slice"
PROBE_TIMEOUT_S = 10.0
HOLDER_READY_TIMEOUT_S = 10.0


class SandboxError(RuntimeError):
    pass


# -- configuration -----------------------------------------------------------


@dataclass(frozen=True)
class SandboxSpec:
    """`sandbox:` block of a recipe."""

    mode: str = "off"
    tmp_size: str = DEFAULT_TMP_SIZE
    """`auto` (half the slice limit), a percentage of the slice limit
    (`40%`), or an absolute size (`8G`, `512M`, bytes)."""
    share: tuple[str, ...] = ()
    """Absolute host paths bound through over the private mounts (e.g. a
    directory under /tmp that a docker container bind-mounts, or /dev/kvm)."""

    @classmethod
    def parse(cls, raw: Any) -> "SandboxSpec":
        from alloy.config import ConfigError

        if raw is None:
            return cls()
        if raw is False:  # YAML 1.1 reads a bare `off` as false
            return cls(mode="off")
        if isinstance(raw, str):
            raw = {"mode": raw}
        if not isinstance(raw, dict):
            raise ConfigError(f"sandbox must be a mapping: {raw!r}")
        raw_mode = raw.get("mode", "off")
        mode = "off" if raw_mode is False else str(raw_mode).strip().lower()
        if mode not in SANDBOX_MODES:
            raise ConfigError(f"sandbox.mode must be one of {', '.join(SANDBOX_MODES)}: {mode!r}")
        tmp_size = str(raw.get("tmp_size", DEFAULT_TMP_SIZE)).strip()
        try:
            _parse_tmp_size(tmp_size)
        except ValueError as exc:
            raise ConfigError(f"sandbox.tmp_size: {exc}") from exc
        share = raw.get("share") or []
        if isinstance(share, str) or not all(isinstance(p, str) and p.startswith("/") for p in share):
            raise ConfigError(f"sandbox.share must be a list of absolute paths: {share!r}")
        return cls(mode=mode, tmp_size=tmp_size, share=tuple(share))

    def effective_mode(self, environ: Mapping[str, str] | None = None) -> str:
        """The recipe's mode unless `ALLOY_SANDBOX` overrides it."""
        override = (environ if environ is not None else os.environ).get(ENV_MODE, "").strip().lower()
        if override in SANDBOX_MODES:
            return override
        if override in ("0", "false", "no", "disabled"):
            return "off"
        return self.mode


# -- tmpfs size ----------------------------------------------------------------

_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(text: str) -> int:
    """`8G`, `512M`, `1.5G`, `1048576` -> bytes (binary units)."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTB]?)(?:I?B)?\s*", str(text).upper())
    if not match:
        raise ValueError(f"not a size: {text!r} (use e.g. 8G, 512M, 40% or auto)")
    value = float(match.group(1)) * _UNITS[match.group(2)]
    if value < 1024 * 1024:
        raise ValueError(f"size {text!r} is below 1M")
    return int(value)


def _parse_tmp_size(text: str) -> tuple[str, float]:
    """('fraction', f) for auto / N%; ('bytes', n) for absolute sizes."""
    text = text.strip().lower()
    if text == "auto":
        return "fraction", AUTO_SLICE_FRACTION
    if text.endswith("%"):
        try:
            pct = float(text[:-1])
        except ValueError as exc:
            raise ValueError(f"not a percentage: {text!r}") from exc
        if not 0 < pct <= 100:
            raise ValueError(f"percentage out of range: {text!r}")
        return "fraction", pct / 100
    return "bytes", float(parse_size(text))


def format_size(n: int) -> str:
    for unit in ("T", "G", "M", "K"):
        if n >= _UNITS[unit]:
            return f"{n / _UNITS[unit]:.1f}{unit}"
    return f"{n}B"


def mem_total(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _limit_value(raw: str) -> int | None:
    raw = raw.strip()
    if not raw or raw in ("max", "infinity"):
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # cgroup v1 / systemd report "no limit" as a huge sentinel.
    return value if value < 2**60 else None


def _cgroup_limit(proc_cgroup: Path, cgroup_root: Path) -> tuple[int, str] | None:
    """The tightest numeric memory.max from this process's cgroup up to the root."""
    try:
        lines = proc_cgroup.read_text().splitlines()
    except OSError:
        return None
    relative = next((entry[3:] for entry in lines if entry.startswith("0::")), "").strip().lstrip("/")
    if not relative:
        return None
    best: tuple[int, str] | None = None
    current = cgroup_root / relative
    while current == cgroup_root or cgroup_root in current.parents:
        try:
            value = _limit_value((current / "memory.max").read_text())
        except OSError:
            value = None
        if value is not None and (best is None or value < best[0]):
            best = (value, f"cgroup /{current.relative_to(cgroup_root)} memory.max")
        if current == cgroup_root:
            break
        current = current.parent
    return best


def _systemctl_limit(systemctl: str | None) -> tuple[int, str] | None:
    if not systemctl or not shutil.which(systemctl):
        return None
    try:
        proc = subprocess.run(
            [systemctl, "--user", "show", SLICE_NAME, "-p", "MemoryMax", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = _limit_value(proc.stdout) if proc.returncode == 0 else None
    return (value, f"systemctl --user show {SLICE_NAME} MemoryMax") if value is not None else None


def slice_memory_limit(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    systemctl: str | None = "systemctl",
) -> tuple[int | None, str]:
    """(limit bytes, source): the tightest memory.max on this process's cgroup
    path (alloy.slice when started via scripts/alloy-startup), else
    `systemctl --user show alloy.slice -p MemoryMax`, else (None, why)."""
    found = _cgroup_limit(proc_cgroup, cgroup_root) or _systemctl_limit(systemctl)
    if found is not None:
        return found
    return None, f"no memory limit found on this cgroup or {SLICE_NAME}"


@dataclass(frozen=True)
class TmpSize:
    bytes: int
    source: str
    slice_limit: int | None
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "human": format_size(self.bytes),
            "source": self.source,
            "slice_limit": self.slice_limit,
            "warning": self.warning,
        }


def resolve_tmp_size(
    spec: str,
    *,
    slice_limit: tuple[int | None, str] | None = None,
    total: int | None = None,
) -> TmpSize:
    """Resolve `sandbox.tmp_size` against the slice memory limit (or RAM)."""
    kind, value = _parse_tmp_size(spec)
    limit, limit_source = slice_limit if slice_limit is not None else slice_memory_limit()
    if kind == "bytes":
        size = int(value)
        warning = None
        if limit is not None and size > limit:
            warning = (
                f"sandbox tmp_size {spec} ({format_size(size)}) exceeds the slice memory limit "
                f"{format_size(limit)}: a full /tmp would hit the OOM killer first"
            )
        return TmpSize(size, f"explicit {spec}", limit, warning)
    if limit is not None:
        return TmpSize(int(limit * value), f"{value * 100:g}% of {format_size(limit)} ({limit_source})", limit)
    ram = total if total is not None else mem_total()
    if ram is None:
        size = 4 * 1024**3
        return TmpSize(size, "4G (no slice limit, MemTotal unreadable)", None, f"{limit_source}; using 4G")
    size = int(ram * NO_SLICE_FRACTION)
    return TmpSize(
        size,
        f"{NO_SLICE_FRACTION * 100:g}% of MemTotal {format_size(ram)}",
        None,
        f"{limit_source} (not started via scripts/alloy-startup?); "
        f"private /tmp capped at {NO_SLICE_FRACTION * 100:g}% of RAM instead",
    )


# -- capability probe ------------------------------------------------------------

_PROBE_CACHE: dict[tuple[str, str], "ProbeResult"] = {}


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    reason: str
    bwrap: str | None = None
    nsenter: str | None = None


def probe(*, refresh: bool = False) -> ProbeResult:
    """Can this host run the sandbox? `bwrap --bind / / --tmpfs /tmp true`
    plus nsenter's presence; cached per process (one ~20 ms spawn)."""
    bwrap = shutil.which("bwrap")
    nsenter = shutil.which("nsenter")
    key = (bwrap or "", nsenter or "")
    if not refresh and key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    if bwrap is None:
        result = ProbeResult(False, "bwrap is not installed")
    elif nsenter is None:
        result = ProbeResult(False, "nsenter (util-linux) is not installed", bwrap=bwrap)
    else:
        try:
            proc = subprocess.run(
                [bwrap, "--bind", "/", "/", "--tmpfs", "/tmp", "true"],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode == 0:
                result = ProbeResult(True, "ok", bwrap=bwrap, nsenter=nsenter)
            else:
                why = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
                result = ProbeResult(False, f"bwrap probe failed: {why}", bwrap=bwrap, nsenter=nsenter)
        except (OSError, subprocess.SubprocessError) as exc:
            result = ProbeResult(False, f"bwrap probe failed: {exc}", bwrap=bwrap, nsenter=nsenter)
    _PROBE_CACHE[key] = result
    return result


def host_info(spec: SandboxSpec | None = None) -> dict[str, Any]:
    """What `alloy status --json` and the scheduler start line report."""
    spec = spec or SandboxSpec(mode="auto")
    result = probe()
    size = resolve_tmp_size(spec.tmp_size)
    return {
        "env_override": os.environ.get(ENV_MODE) or None,
        "bwrap_available": result.ok,
        "probe": result.reason,
        "tmp_size": size.as_dict(),
    }


# -- per-run sandbox ---------------------------------------------------------------


@dataclass
class RunSandbox:
    """The sandbox of one run: `bwrap` (holder namespace), `tmpdir` (fallback:
    per-run TMPDIR only) or `off`."""

    run_id: str
    spec: SandboxSpec
    kind: str = "off"
    tmp_size: TmpSize | None = None
    holder: subprocess.Popen | None = None
    holder_pid: int | None = None
    tmpdir: Path | None = None
    note: str = ""
    extra_share: tuple[str, ...] = ()
    """The run's own paths (worktree, repo, log dir, alloy root); bound
    through only when they live under a private mount (/tmp, /var/tmp,
    /dev/shm) -- otherwise the run could not see its own checkout."""
    _nsenter: str | None = field(default=None, repr=False)
    _bwrap: str | None = field(default=None, repr=False)

    # -- lifecycle -----------------------------------------------------

    def start(self) -> "RunSandbox":
        mode = self.spec.effective_mode()
        if mode == "off":
            self.kind = "off"
            self.note = "sandbox off"
            return self
        result = probe()
        if result.ok:
            try:
                self.tmp_size = resolve_tmp_size(self.spec.tmp_size)
                if self.tmp_size.warning:
                    log.warning("sandbox: %s", self.tmp_size.warning)
                self._bwrap, self._nsenter = result.bwrap, result.nsenter
                self._start_holder()
                self.kind = "bwrap"
                self.note = (
                    f"bwrap sandbox: private /tmp {format_size(self.tmp_size.bytes)} "
                    f"({self.tmp_size.source}), holder pid {self.holder_pid}"
                )
                log.info("run %s: %s", self.run_id, self.note)
                return self
            except Exception as exc:  # noqa: BLE001 -- fall through to the fallback
                reason = f"could not start the bwrap holder: {exc}"
                self._kill_holder()
        else:
            reason = result.reason
        if mode == "bwrap":
            raise SandboxError(f"sandbox.mode is bwrap but {reason}")
        self._start_tmpdir()
        self.kind = "tmpdir"
        self.note = f"sandbox unavailable ({reason}); running unsandboxed with TMPDIR={self.tmpdir}"
        log.warning("run %s: %s", self.run_id, self.note)
        return self

    def stop(self) -> None:
        self._kill_holder()
        if self.tmpdir is not None:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    def _holder_argv(self) -> list[str]:
        assert self.tmp_size is not None and self._bwrap is not None
        size = self.tmp_size.bytes
        side = max(int(size * SIDE_TMPFS_FRACTION), 64 * 1024 * 1024)
        argv = [self._bwrap, "--die-with-parent", "--bind", "/", "/", "--dev", "/dev"]
        for dest, cap in (("/tmp", size), ("/var/tmp", side), ("/dev/shm", side)):
            argv += ["--perms", "1777", "--size", str(cap), "--tmpfs", dest]
        for path in shared_paths(self.spec, self.extra_share):
            flag = "--dev-bind-try" if path.startswith("/dev/") else "--bind-try"
            argv += [flag, path, path]
        # The holder lives exactly as long as its stdin pipe: the write end
        # belongs to the process running the run (and is not inherited by
        # anything it spawns), so the holder exits when that process exits,
        # however it dies. bwrap's --die-with-parent alone is not enough: in
        # 0.11 it kills bwrap, but the sandboxed child survives it.
        argv += ["--", "/bin/sh", "-c", "echo $$; read -r _ || true"]
        return argv

    def _start_holder(self) -> None:
        # Popen from the thread that runs the run: --die-with-parent is a
        # parent-death signal, tied to the spawning thread.
        self.holder = subprocess.Popen(
            self._holder_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert self.holder.stdout is not None
        import selectors

        with selectors.DefaultSelector() as selector:
            selector.register(self.holder.stdout, selectors.EVENT_READ)
            if not selector.select(HOLDER_READY_TIMEOUT_S):
                raise SandboxError("holder did not become ready")
        line = self.holder.stdout.readline().decode().strip()
        if not line.isdigit():
            err = self.holder.stderr.read().decode() if self.holder.poll() is not None and self.holder.stderr else ""
            raise SandboxError(f"holder failed: {err.strip() or line or 'no output'}")
        # The holder only prints its pid once bwrap finished setting up the
        # mounts, so joining it from now on sees the final namespace.
        self.holder_pid = int(line)

    def _kill_holder(self) -> None:
        if self.holder is not None and self.holder.stdin is not None:
            with contextlib.suppress(OSError):
                self.holder.stdin.close()
        if self.holder_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self.holder_pid, signal.SIGKILL)
        if self.holder is not None:
            try:
                self.holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.holder.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.holder.wait(timeout=5)
            for stream in (self.holder.stdout, self.holder.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        self.holder = None
        self.holder_pid = None

    def _start_tmpdir(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix=f"alloy-run-{self.run_id[:12]}-"))

    def holder_alive(self) -> bool:
        return self.holder is not None and self.holder.poll() is None

    def _ensure_holder(self) -> bool:
        """Restart a holder that died mid-run (new, empty /tmp); False if the
        sandbox could not be restored, in which case commands run unsandboxed
        with a per-run TMPDIR rather than fail on a dead namespace."""
        if self.holder_alive():
            return True
        log.warning("run %s: sandbox holder is gone; starting a new one (/tmp starts empty)", self.run_id)
        self._kill_holder()
        try:
            self._start_holder()
            return True
        except Exception as exc:  # noqa: BLE001
            self._kill_holder()
            self.kind = "tmpdir"
            self._start_tmpdir()
            self.note = f"sandbox holder could not be restarted ({exc}); unsandboxed with TMPDIR={self.tmpdir}"
            log.warning("run %s: %s", self.run_id, self.note)
            return False

    # -- wrapping --------------------------------------------------------

    def wrap(
        self, argv: list[str], cwd: str | os.PathLike | None, env: Mapping[str, str] | None
    ) -> tuple[list[str], dict[str, str]]:
        """argv/env to spawn `argv` inside this run's sandbox."""
        env = dict(env if env is not None else os.environ)
        if self.kind == "bwrap" and self._ensure_holder():
            env.update(TMPDIR="/tmp", TMP="/tmp", TEMP="/tmp")
            env["ALLOY_SANDBOX_ACTIVE"] = "bwrap"
            wd = str(cwd) if cwd is not None else os.getcwd()
            wrapped = [
                self._nsenter or "nsenter",
                "-t",
                str(self.holder_pid),
                "-U",
                "-m",
                "--preserve-credentials",
                "--root",
                f"--wd={wd}",
                "--",
                *argv,
            ]
            return wrapped, env
        if self.kind == "tmpdir" and self.tmpdir is not None:
            env.update(TMPDIR=str(self.tmpdir), TMP=str(self.tmpdir), TEMP=str(self.tmpdir))
        return list(argv), env

    def info(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "mode": self.spec.effective_mode(),
            "holder_pid": self.holder_pid,
            "tmpdir": str(self.tmpdir) if self.tmpdir else ("/tmp (private tmpfs)" if self.kind == "bwrap" else None),
            "tmp_size": self.tmp_size.as_dict() if self.tmp_size else None,
            "share": list(shared_paths(self.spec, self.extra_share)) if self.kind == "bwrap" else [],
            "note": self.note,
            "pid": os.getpid(),
            "started_at": time.time(),
        }


PRIVATE_MOUNTS = ("/tmp", "/var/tmp", "/dev/shm")


def _under_private_mount(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in PRIVATE_MOUNTS)


def shared_paths(spec: SandboxSpec, extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    paths = [p for p in ALWAYS_SHARED if os.path.exists(p)]
    for raw in extra:
        path = os.path.realpath(str(raw))
        if _under_private_mount(path) and path not in PRIVATE_MOUNTS and path not in paths:
            paths.append(path)
    sock = os.environ.get("SSH_AUTH_SOCK", "")
    if sock.startswith("/tmp/"):
        # ssh-agent sockets usually live in /tmp/ssh-XXXX/: keep git+ssh working.
        paths.append(str(Path(sock).parent))
    for path in spec.share:
        if path not in paths:
            paths.append(path)
    return paths


# -- the active sandbox of the current run -----------------------------------------

_CURRENT: contextvars.ContextVar[RunSandbox | None] = contextvars.ContextVar("alloy_sandbox", default=None)


def current() -> RunSandbox | None:
    return _CURRENT.get()


@contextlib.contextmanager
def activate(sandbox: RunSandbox | None) -> Iterator[RunSandbox | None]:
    """Make `sandbox` the one every runner and check of this run spawns in
    (a ContextVar: asyncio tasks and LangGraph's executor threads inherit it)."""
    token = _CURRENT.set(sandbox)
    try:
        yield sandbox
    finally:
        _CURRENT.reset(token)


def wrap_command(
    argv: list[str], cwd: str | os.PathLike | None, env: Mapping[str, str] | None
) -> tuple[list[str], dict[str, str] | None]:
    """`argv`/`env` for a subprocess of the current run; unchanged (env None
    stays None) when no sandbox is active."""
    sandbox = current()
    if sandbox is None or sandbox.kind == "off":
        return list(argv), (dict(env) if env is not None else None)
    return sandbox.wrap(argv, cwd, env)


# -- status file ----------------------------------------------------------------------


def status_path(root: Path) -> Path:
    return Path(root) / "sandbox.json"


def write_status(
    root: Path, *, host: dict[str, Any] | None = None, run: dict[str, Any] | None = None, drop_run: str | None = None
) -> None:
    """Best-effort record for `alloy status --json`: the host resolution at
    scheduler start and the sandbox of each active run."""
    path = status_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    runs = data.get("runs") if isinstance(data.get("runs"), dict) else {}
    if host is not None:
        data["host"] = host
    if run is not None:
        runs[run["run_id"]] = run
    if drop_run is not None:
        runs.pop(drop_run, None)
    data["runs"] = runs
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("could not write %s", path, exc_info=True)


def read_status(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(status_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    runs = data.get("runs") if isinstance(data.get("runs"), dict) else {}
    # Only runs whose owning process is still alive are active.
    data["runs"] = {rid: info for rid, info in runs.items() if _alive(info.get("pid"))}
    return data


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True
