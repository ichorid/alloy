"""Real-bubblewrap behaviour of the per-run sandbox (alloy.sandbox).

Skipped automatically when bwrap, nsenter or unprivileged user namespaces
are unavailable. Run with `-s` to see the capability report of the last test.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from alloy import sandbox as sb
from alloy.models import CheckRequest
from alloy.sandbox import RunSandbox, SandboxSpec
from alloy.verify import run_check

MB = 1024 * 1024


def _functional() -> str | None:
    result = sb.probe(refresh=True)
    if not result.ok:
        return result.reason
    box = RunSandbox(run_id="probe", spec=SandboxSpec(mode="bwrap", tmp_size="16M"))
    try:
        box.start()
        argv, env = box.wrap(["true"], "/", os.environ)
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return f"nsenter into the holder failed: {proc.stderr.strip()}"
    except Exception as exc:  # noqa: BLE001
        return f"holder failed: {exc}"
    finally:
        box.stop()
    return None


_WHY = _functional() if sys.platform.startswith("linux") else "not linux"
pytestmark = pytest.mark.skipif(_WHY is not None, reason=f"bwrap sandbox unavailable: {_WHY}")


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv(sb.ENV_MODE, raising=False)


@pytest.fixture
def box(tmp_path):
    sandbox = RunSandbox(
        run_id=f"t{uuid.uuid4().hex[:8]}",
        spec=SandboxSpec(mode="bwrap", tmp_size="32M"),
        extra_share=(str(tmp_path),),  # pytest's tmp_path lives under /tmp
    ).start()
    assert sandbox.kind == "bwrap", sandbox.note
    yield sandbox
    sandbox.stop()


def run_in(box: RunSandbox, script: str, cwd: Path | str = "/", **kwargs) -> subprocess.CompletedProcess:
    argv, env = box.wrap(["/bin/sh", "-c", script], cwd, os.environ)
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, **kwargs)


def test_tmp_is_private_shared_within_the_run_and_gone_after(box):
    marker = f"alloy-sbx-{uuid.uuid4().hex}"
    assert run_in(box, f"echo hello > /tmp/{marker}").returncode == 0
    # A second process of the same run sees it: one namespace per run.
    assert run_in(box, f"cat /tmp/{marker}").stdout == "hello\n"
    # The host never does.
    assert not Path("/tmp", marker).exists()
    box.stop()
    box.start()
    assert run_in(box, f"test -e /tmp/{marker}").returncode == 1  # a fresh run starts empty
    assert not Path("/tmp", marker).exists()


def test_tmp_size_cap_is_enforced(box):
    proc = run_in(box, "head -c 48M /dev/zero > /tmp/big")
    assert proc.returncode != 0
    assert "No space left" in proc.stderr
    df = run_in(box, "df -k /tmp | tail -1").stdout.split()
    assert int(df[1]) == 32 * 1024


def test_tmpdir_is_set_and_var_tmp_shm_are_private(box):
    out = run_in(box, 'echo "$TMPDIR"; stat -c %a /tmp; df -k /var/tmp /dev/shm | tail -2 | wc -l').stdout.split()
    assert out[0] == "/tmp" and out[1] == "1777" and out[2] == "2"
    marker = f"alloy-sbx-shm-{uuid.uuid4().hex}"
    assert run_in(box, f"touch /dev/shm/{marker} /var/tmp/{marker}").returncode == 0
    assert not Path("/dev/shm", marker).exists() and not Path("/var/tmp", marker).exists()


def test_exit_code_and_pid_are_the_commands_own(box):
    assert run_in(box, "exit 7").returncode == 7
    argv, env = box.wrap(["/bin/sh", "-c", "echo $$; sleep 30"], "/", os.environ)
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert int(proc.stdout.readline()) == proc.pid  # nsenter exec'd: same pid, same group
        os.killpg(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()


def test_repo_and_home_stay_readable_and_writable(box, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert run_in(box, "echo inside > file.txt && git init -q . && git status --short", cwd=repo).returncode == 0
    assert (repo / "file.txt").read_text() == "inside\n"
    home_dir = Path.home() / ".cache" / f"alloy-sbx-test-{uuid.uuid4().hex[:8]}"
    try:
        assert run_in(box, f"mkdir -p {home_dir} && echo h > {home_dir}/f").returncode == 0
        assert (home_dir / "f").read_text() == "h\n"
    finally:
        shutil.rmtree(home_dir, ignore_errors=True)


def test_holder_dies_with_its_parent(tmp_path):
    """--die-with-parent: when the process that owns the run dies, the
    namespace holder dies with it."""
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
        from alloy.sandbox import RunSandbox, SandboxSpec
        box = RunSandbox(run_id="parent", spec=SandboxSpec(mode="bwrap", tmp_size="16M")).start()
        print(box.holder_pid, flush=True)
        time.sleep(60)
        """
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        holder = int(parent.stdout.readline())
        os.kill(holder, 0)
        parent.kill()
        parent.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(holder):
            time.sleep(0.1)
        assert not _alive(holder)
    finally:
        if parent.poll() is None:
            parent.kill()


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


async def test_checks_run_inside_the_run_namespace(box, tmp_path):
    marker = f"alloy-sbx-check-{uuid.uuid4().hex}"
    with sb.activate(box):
        result = await run_check(
            CheckRequest(command=f'echo "tmp=$TMPDIR"; touch /tmp/{marker}; pwd', purpose="p", kind="custom"),
            tmp_path,
            log_dir=tmp_path / "logs",
        )
    assert result.exit_code == 0
    text = Path(result.log_path).read_text()
    assert "tmp=/tmp\n" in text and f"{tmp_path}\n" in text
    assert run_in(box, f"test -e /tmp/{marker}").returncode == 0
    assert not Path("/tmp", marker).exists()


TOOLS = [
    ["git", "--version"],
    ["bd", "--version"],
    ["dart", "--version"],
    ["flutter", "--version"],
    ["docker", "ps", "--format", "{{.Names}}"],
    ["codex", "--version"],
    ["claude", "--version"],
    ["agent", "--version"],
    ["bwrap", "--bind", "/", "/", "--tmpfs", "/tmp", "true"],  # nested: claude/codex sandbox their tools
]


def test_capability_report_inside_the_sandbox(box, capsys):
    """Prints what works inside; only git (Alloy itself needs it) must pass."""
    lines = []
    for argv in TOOLS:
        if shutil.which(argv[0]) is None:
            lines.append(f"  {argv[0]:8} not installed")
            continue
        wrapped, env = box.wrap(argv, str(Path.home()), os.environ)
        try:
            proc = subprocess.run(wrapped, env=env, capture_output=True, text=True, timeout=120)
            out = (proc.stdout or proc.stderr).strip().splitlines()
            lines.append(f"  {argv[0]:8} exit {proc.returncode}: {out[0][:100] if out else ''}")
            if argv[0] == "git":
                assert proc.returncode == 0
        except subprocess.TimeoutExpired:
            lines.append(f"  {argv[0]:8} timed out")
    with capsys.disabled():
        print("\nsandbox capability report:\n" + "\n".join(lines))


_GETCWD_SYSCALL = {"x86_64": 79, "aarch64": 17}.get(os.uname().machine)


@pytest.mark.skipif(_GETCWD_SYSCALL is None, reason="raw getcwd syscall number unknown on this arch")
def test_cwd_is_reachable_inside_the_namespace(box, tmp_path):
    """Regression (codex exec: "No such file or directory (os error 2)"):
    nsenter --wd left the cwd in the host mount tree, so the raw getcwd(2)
    returned "(unreachable)/..." -- glibc/sh hid it, Rust's current_dir did
    not. The raw syscall is the stand-in for that failure class."""
    script = (
        "import ctypes; b = ctypes.create_string_buffer(4096); "
        f"ctypes.CDLL(None).syscall({_GETCWD_SYSCALL}, b, 4096); print(b.value.decode())"
    )
    for cwd in (tmp_path, Path.home()):
        argv, env = box.wrap([sys.executable, "-c", script], cwd, os.environ)
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(cwd)


def _codex_logged_in() -> str | None:
    codex = shutil.which("codex")
    if codex is None or "fakebin" in codex:
        return "codex is not installed"
    try:
        proc = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"codex login status failed: {exc}"
    if proc.returncode != 0:
        return "codex is not logged in"
    return None


def test_real_codex_exec_runs_inside_the_sandbox(box, tmp_path):
    """The real harness through the sandboxed command builder, once per
    session (a few tokens). Skipped without an installed, logged-in codex."""
    why = _codex_logged_in()
    if why:
        pytest.skip(why)
    argv, env = box.wrap(
        [shutil.which("codex"), "exec", "--skip-git-repo-check", "--json", "reply with the single word ok"],
        tmp_path,
        os.environ,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])
    assert '"agent_message"' in proc.stdout and "ok" in proc.stdout.lower()
