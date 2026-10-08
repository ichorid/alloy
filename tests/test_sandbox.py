"""Per-run sandbox (alloy.sandbox): config, tmpfs sizing, fallback, wiring.

Everything here runs without bubblewrap; the real-bwrap behaviour lives in
tests/test_sandbox_bwrap.py (skipped when bwrap/userns are unavailable).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alloy import sandbox as sb
from alloy.config import ConfigError, RecipeConfig, load_recipe
from alloy.models import CheckRequest
from alloy.sandbox import RunSandbox, SandboxError, SandboxSpec, resolve_tmp_size
from alloy.verify import run_check

G = 1024**3


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv(sb.ENV_MODE, raising=False)


def _probe(ok: bool, reason: str = "ok"):
    return lambda **_: sb.ProbeResult(ok, reason, bwrap="/usr/bin/bwrap" if ok else None, nsenter="/usr/bin/nsenter")


# -- config --------------------------------------------------------------------


def test_spec_defaults_to_off_with_auto_size():
    spec = SandboxSpec.parse(None)
    assert (spec.mode, spec.tmp_size, spec.share) == ("off", "auto", ())


def test_spec_parses_yaml_off_boolean_and_full_block():
    assert SandboxSpec.parse(False).mode == "off"
    assert SandboxSpec.parse({"mode": False}).mode == "off"
    spec = SandboxSpec.parse({"mode": "bwrap", "tmp_size": "8G", "share": ["/tmp/shared"]})
    assert (spec.mode, spec.tmp_size, spec.share) == ("bwrap", "8G", ("/tmp/shared",))


@pytest.mark.parametrize(
    "raw",
    [{"mode": "yes"}, {"tmp_size": "lots"}, {"tmp_size": "150%"}, {"share": "/tmp/x"}, {"share": ["relative"]}],
)
def test_spec_rejects_bad_values(raw):
    with pytest.raises(ConfigError):
        SandboxSpec.parse(raw)


def test_env_override_wins_over_the_recipe(monkeypatch):
    spec = SandboxSpec(mode="auto")
    monkeypatch.setenv(sb.ENV_MODE, "off")
    assert spec.effective_mode() == "off"
    monkeypatch.setenv(sb.ENV_MODE, "bwrap")
    assert spec.effective_mode() == "bwrap"
    monkeypatch.setenv(sb.ENV_MODE, "garbage")
    assert spec.effective_mode() == "auto"


def test_sol_no_context_recipe_defaults_to_auto_and_others_stay_off():
    assert load_recipe("tdd-loop-sol-no-context").sandbox == SandboxSpec(mode="auto", tmp_size="auto")
    assert load_recipe("tdd-loop").sandbox.mode == "off"
    assert RecipeConfig.parse({"name": "x", "sandbox": {"mode": "auto"}}).sandbox.mode == "auto"


# -- tmpfs size -------------------------------------------------------------------


def test_auto_size_is_half_the_slice_limit():
    size = resolve_tmp_size("auto", slice_limit=(40 * G, "cgroup /alloy.slice memory.max"))
    assert size.bytes == 20 * G
    assert "50% of 40.0G" in size.source and "alloy.slice" in size.source
    assert size.warning is None


def test_percentage_is_of_the_slice_limit():
    assert resolve_tmp_size("40%", slice_limit=(10 * G, "x")).bytes == 4 * G


def test_explicit_size_is_taken_as_is_and_warns_above_the_slice_limit():
    assert resolve_tmp_size("8G", slice_limit=(40 * G, "x")).bytes == 8 * G
    big = resolve_tmp_size("64G", slice_limit=(40 * G, "x"))
    assert big.bytes == 64 * G and "exceeds the slice memory limit" in big.warning


def test_no_slice_limit_falls_back_to_a_quarter_of_ram_with_a_warning():
    size = resolve_tmp_size("auto", slice_limit=(None, "no memory limit found"), total=64 * G)
    assert size.bytes == 16 * G
    assert "25% of MemTotal" in size.source
    assert "no memory limit found" in size.warning


@pytest.mark.parametrize("text,expected", [("8G", 8 * G), ("512M", 512 * 1024**2), ("1.5G", int(1.5 * G))])
def test_parse_size(text, expected):
    assert sb.parse_size(text) == expected


def test_slice_limit_is_the_tightest_memory_max_on_the_cgroup_path(tmp_path):
    root = tmp_path / "cg"
    leaf = root / "user.slice" / "alloy.slice" / "alloy-run.scope"
    leaf.mkdir(parents=True)
    (root / "user.slice" / "memory.max").write_text("max\n")
    (root / "user.slice" / "alloy.slice" / "memory.max").write_text(f"{40 * G}\n")
    (leaf / "memory.max").write_text("max\n")
    proc = tmp_path / "cgroup"
    proc.write_text("0::/user.slice/alloy.slice/alloy-run.scope\n")
    limit, source = sb.slice_memory_limit(proc_cgroup=proc, cgroup_root=root, systemctl=None)
    assert limit == 40 * G
    assert source == "cgroup /user.slice/alloy.slice memory.max"


def test_slice_limit_absent(tmp_path):
    proc = tmp_path / "cgroup"
    proc.write_text("0::/user.slice\n")
    (tmp_path / "cg" / "user.slice").mkdir(parents=True)
    limit, source = sb.slice_memory_limit(proc_cgroup=proc, cgroup_root=tmp_path / "cg", systemctl=None)
    assert limit is None and "no memory limit" in source


# -- run sandbox lifecycle ----------------------------------------------------------


def test_off_mode_wraps_nothing():
    box = RunSandbox(run_id="r1", spec=SandboxSpec(mode="off")).start()
    assert box.kind == "off"
    argv, env = box.wrap(["true"], "/", {"A": "1"})
    assert argv == ["true"] and env == {"A": "1"}


def test_auto_falls_back_to_a_per_run_tmpdir_that_is_removed(monkeypatch, caplog):
    monkeypatch.setattr(sb, "probe", _probe(False, "bwrap is not installed"))
    box = RunSandbox(run_id="run-fallback-1", spec=SandboxSpec(mode="auto")).start()
    try:
        assert box.kind == "tmpdir"
        assert box.tmpdir is not None and box.tmpdir.is_dir()
        assert "bwrap is not installed" in caplog.text
        argv, env = box.wrap(["true"], "/", {})
        assert argv == ["true"]
        assert env["TMPDIR"] == str(box.tmpdir) == env["TMP"] == env["TEMP"]
        tmpdir = box.tmpdir
    finally:
        box.stop()
    assert not tmpdir.exists()


def test_bwrap_mode_refuses_without_bwrap(monkeypatch):
    monkeypatch.setattr(sb, "probe", _probe(False, "bwrap is not installed"))
    with pytest.raises(SandboxError, match="bwrap is not installed"):
        RunSandbox(run_id="r", spec=SandboxSpec(mode="bwrap")).start()


def test_holder_failure_falls_back_in_auto(monkeypatch):
    monkeypatch.setattr(sb, "probe", _probe(True))

    def boom(self):
        raise sb.SandboxError("holder failed: nope")

    monkeypatch.setattr(RunSandbox, "_start_holder", boom)
    box = RunSandbox(run_id="r-holder", spec=SandboxSpec(mode="auto")).start()
    try:
        assert box.kind == "tmpdir" and "holder failed" in box.note
    finally:
        box.stop()


def test_holder_argv_layout():
    box = RunSandbox(
        run_id="r",
        spec=SandboxSpec(mode="bwrap", share=["/tmp/docker-shared", "/dev/kvm"]),
        extra_share=("/tmp/pytest-x/project", "/home/someone/repo", "/tmp"),
    )
    box._bwrap = "/usr/bin/bwrap"
    box.tmp_size = sb.TmpSize(4 * G, "explicit 4G", None)
    argv = box._holder_argv()
    joined = " ".join(argv)
    assert "--die-with-parent" in argv
    assert "--unshare-pid" not in argv and "--proc" not in argv  # host pids stay valid
    assert "--bind / /" in joined and "--dev /dev" in joined
    assert f"--perms 1777 --size {4 * G} --tmpfs /tmp" in joined
    assert f"--size {G} --tmpfs /var/tmp" in joined and f"--size {G} --tmpfs /dev/shm" in joined
    assert "--bind-try /tmp/docker-shared /tmp/docker-shared" in joined
    assert "--dev-bind-try /dev/kvm /dev/kvm" in joined
    # The run's own checkout under /tmp is bound through; paths elsewhere and /tmp itself are not.
    assert "--bind-try /tmp/pytest-x/project /tmp/pytest-x/project" in joined
    assert "/home/someone/repo" not in joined
    assert "--bind-try /tmp /tmp" not in joined
    # Shares come after the tmpfs they punch through.
    assert joined.index("--tmpfs /tmp") < joined.index("--bind-try /tmp/docker-shared")


def test_bwrap_wrap_joins_the_holder_with_nsenter():
    box = RunSandbox(run_id="r", spec=SandboxSpec(mode="bwrap"), kind="bwrap", holder_pid=4242)
    box._nsenter = "/usr/bin/nsenter"
    box._ensure_holder = lambda: True  # type: ignore[method-assign]
    argv, env = box.wrap(["codex", "exec", "x"], "/work/tree", {"TMPDIR": "/somewhere"})
    assert argv == [
        "/usr/bin/nsenter",
        "-t",
        "4242",
        "-U",
        "-m",
        "--preserve-credentials",
        "--root",
        "--wd=/work/tree",
        "--",
        "codex",
        "exec",
        "x",
    ]
    assert env["TMPDIR"] == "/tmp"


def test_wrap_command_is_a_no_op_without_an_active_sandbox():
    assert sb.current() is None
    assert sb.wrap_command(["true"], "/", None) == (["true"], None)


async def test_checks_run_with_the_run_tmpdir(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "probe", _probe(False, "no bwrap here"))
    box = RunSandbox(run_id="r-check", spec=SandboxSpec(mode="auto")).start()
    tmpdir = box.tmpdir
    try:
        with sb.activate(box):
            result = await run_check(
                CheckRequest(command='echo "tmp=$TMPDIR"; exit 3', purpose="p", kind="custom"),
                tmp_path,
                log_dir=tmp_path / "logs",
            )
    finally:
        box.stop()
    assert result.exit_code == 3
    assert f"tmp={tmpdir}\n" in Path(result.log_path).read_text()


def test_status_file_round_trip(tmp_path):
    sb.write_status(tmp_path, host={"bwrap_available": True})
    sb.write_status(tmp_path, run={"run_id": "live", "pid": os.getpid()})
    sb.write_status(tmp_path, run={"run_id": "dead", "pid": 2**22 + 12345})
    data = sb.read_status(tmp_path)
    assert data["host"] == {"bwrap_available": True}
    assert list(data["runs"]) == ["live"]
    sb.write_status(tmp_path, drop_run="live")
    assert sb.read_status(tmp_path)["runs"] == {}


# -- engine wiring -------------------------------------------------------------------


async def test_engine_runs_a_bead_in_the_fallback_sandbox_and_cleans_up(
    beads_project, alloy_home, fake_harnesses, monkeypatch
):
    """A whole fake run with `sandbox: auto` on a host whose probe fails: the
    run still finishes, the per-run TMPDIR is gone, nothing stays recorded."""
    from dataclasses import replace

    from conftest import bd_create
    from support import load_config
    from test_engine import script

    from alloy.engine import Engine

    monkeypatch.setattr(sb, "probe", _probe(False, "probe failed for the test"))
    created: list[RunSandbox] = []
    real_start = RunSandbox.start

    def tracking_start(self):
        created.append(self)
        return real_start(self)

    monkeypatch.setattr(RunSandbox, "start", tracking_start)
    engine = Engine.open(beads_project, alloy_home)
    config = replace(load_config(), sandbox=SandboxSpec(mode="auto"))
    monkeypatch.setattr(engine, "load_config", lambda name: config)
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert len(created) == 1 and created[0].kind == "tmpdir"
    assert created[0].tmpdir is None  # removed at run end
    assert sb.read_status(engine.paths.root).get("runs") == {}


async def test_engine_refuses_a_bwrap_only_run_without_bwrap(beads_project, alloy_home, fake_harnesses, monkeypatch):
    from dataclasses import replace

    from conftest import bd_create
    from support import load_config
    from test_engine import script

    from alloy import beads as bd
    from alloy.engine import Engine, EngineError

    monkeypatch.setattr(sb, "probe", _probe(False, "bwrap is not installed"))
    engine = Engine.open(beads_project, alloy_home)
    config = replace(load_config(), sandbox=SandboxSpec(mode="bwrap"))
    monkeypatch.setattr(engine, "load_config", lambda name: config)
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    with pytest.raises(EngineError, match="bwrap is not installed"):
        await engine.run(bead_id)
    assert engine.beads.show(bead_id).status == bd.STATUS_READY
    assert fake_harnesses.calls == []


# -- reporting -----------------------------------------------------------------------


def test_scheduler_start_logs_the_resolved_size_and_status_json_shows_it(
    beads_project, alloy_home, monkeypatch, caplog
):
    import json
    import logging

    from typer.testing import CliRunner

    from alloy.cli import app
    from alloy.engine import Engine
    from alloy.scheduler import Scheduler

    monkeypatch.setattr(sb, "probe", _probe(True))
    monkeypatch.setattr(sb, "slice_memory_limit", lambda **_: (40 * G, "cgroup /alloy.slice memory.max"))
    engine = Engine.open(beads_project, alloy_home)
    with caplog.at_level(logging.INFO, logger="alloy.scheduler"):
        Scheduler(engine)._report_sandbox()
    assert "sandbox: bwrap available" in caplog.text
    assert "20.0G (50% of 40.0G (cgroup /alloy.slice memory.max))" in caplog.text

    result = CliRunner().invoke(app, ["status", "--repo", str(beads_project), "--root", str(alloy_home), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    host = payload["sandbox"]["host"]
    assert host["bwrap_available"] is True
    assert host["tmp_size"]["bytes"] == 20 * G
    assert "alloy.slice" in host["tmp_size"]["source"]
    assert payload["sandbox"]["runs"] == []
