"""Bench campaigns: run Alloy on a frozen toy project with the cheapest models
and measure the result with a hidden oracle.

    uv run python -I bench/run.py campaign --scenario full --runs 3 --alloy-ref HEAD
    uv run python -I bench/run.py reset  --scenario full --dir ~/MY_SRC/alloy-toy-sheet-full-1
    uv run python -I bench/run.py start  --dir DIR --alloy-ref <sha>
    uv run python -I bench/run.py wait   --dir DIR --alloy-ref <sha> --hours 8
    uv run python -I bench/run.py report --dir DIR --alloy-ref <sha> [--out NAME]
    uv run python -I bench/run.py smoke  --alloy-ref <sha>     # one call per role runner

Repeatability: every run starts from the same seed (fixed commit dates, so the
seed SHA is identical), the same beads with explicit ids, an empty `.alloy/`,
an empty bd memory except the default recipe, and an isolated ALLOY_HOME.
Alloy itself runs from a git worktree of this repo at `--alloy-ref`, so edits
in the working checkout never change a running campaign. No human acts during
a run: the recipe's `dispatch.park_after_human_minutes` parks held gates.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

BENCH = Path(__file__).resolve().parent
SHEET = BENCH / "sheet"
ALLOY_REPO = BENCH.parent
REF_WORKTREES = Path.home() / "MY_SRC" / "alloy-bench-ref"
TOY_PARENT = Path.home() / "MY_SRC"
FIXED_DATE = "2026-01-01T00:00:00+00:00"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@example.invalid",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@example.invalid",
    "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_DATE": FIXED_DATE,
}
ACTIVE = {"running", "claiming", "waiting-human"}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(args: list[str], cwd: Path | None = None, env: dict | None = None, check: bool = True, **kw):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(args, cwd=cwd, env=full_env, check=check, text=True, capture_output=True, **kw)


# --- alloy at a frozen ref -------------------------------------------------
def resolve_ref(ref: str) -> str:
    return sh(["git", "rev-parse", ref], cwd=ALLOY_REPO).stdout.strip()


def alloy_checkout(sha: str) -> Path:
    """A frozen worktree of Alloy at `sha` with its own venv. Campaigns
    started together share it, so creation is serialised by a lock and only
    a completed checkout (marked `.bench-ready`) is reused."""
    path = REF_WORKTREES / sha[:12]
    REF_WORKTREES.mkdir(parents=True, exist_ok=True)
    with open(REF_WORKTREES / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (path / ".bench-ready").exists():
            if path.exists():
                sh(["git", "worktree", "remove", "--force", str(path)], cwd=ALLOY_REPO, check=False)
                shutil.rmtree(path, ignore_errors=True)
            sh(["git", "worktree", "add", "--detach", str(path), sha], cwd=ALLOY_REPO)
            sh(["uv", "sync", "--quiet"], cwd=path)
            # Agents must not find the oracle or the reference implementation.
            shutil.rmtree(path / "bench", ignore_errors=True)
            (path / ".bench-ready").write_text(sha)
    return path


def toy_env(toy: Path) -> dict:
    """The environment Alloy (and so every agent and check) runs with: an
    isolated ALLOY_HOME, and no Python venv of Alloy's own -- `uv run` puts
    Alloy's venv first on PATH, and then the toy's `python3 -m pytest` ran
    Alloy's interpreter, which has no pytest."""
    path = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if not (entry.endswith("/.venv/bin") and (str(ALLOY_REPO) in entry or str(REF_WORKTREES) in entry))
    ]
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH")}
    env.update({"ALLOY_HOME": str(toy / ".alloy-home"), "PATH": os.pathsep.join(path)})
    return env


def alloy(sha: str, toy: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    binary = alloy_checkout(sha) / ".venv" / "bin" / "alloy"
    return subprocess.run([str(binary), *args], cwd=toy, env=toy_env(toy), check=check, text=True, capture_output=True)


# --- reset -----------------------------------------------------------------
def load_scenario(name: str) -> dict:
    return yaml.safe_load((SHEET / "beads" / f"{name}.yaml").read_text())


def reset(scenario: str, toy: Path, sha: str) -> None:
    spec = load_scenario(scenario)
    if toy.exists():
        alloy(sha, toy, "stop", "--now", check=False)
        shutil.rmtree(toy)
    shutil.copytree(SHEET / "seed", toy, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache"))
    for name in spec.get("overlays", [scenario]):
        overlay = SHEET / "overlays" / name
        if overlay.exists():
            shutil.copytree(overlay, toy, dirs_exist_ok=True)
    git = lambda *a: sh(["git", *a], cwd=toy, env=GIT_ENV)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    sh(["bd", "init", "--prefix", spec["prefix"], "--quiet"], cwd=toy, env=GIT_ENV)
    alloy(sha, toy, "init")
    recipes = toy / ".alloy" / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    for recipe in (SHEET / "recipes").glob("*.yaml"):
        shutil.copy(recipe, recipes / recipe.name)
    bd = lambda *a: sh(["bd", *a], cwd=toy, env=GIT_ENV)  # noqa: E731
    bd("remember", "--key", "alloy:default:recipe", spec["default_recipe"])
    for bead in spec["beads"]:
        args = ["create", "--id", bead["id"], "-t", bead.get("type", "task"), bead["title"], "--silent"]
        if bead.get("description"):
            args += ["-d", bead["description"]]
        if bead.get("acceptance"):
            args += ["--acceptance", bead["acceptance"]]
        if bead.get("labels"):
            args += ["--labels", ",".join(bead["labels"])]
        bd(*args)
    for bead in spec["beads"]:
        if bead.get("parent"):
            bd("update", bead["id"], "--parent", bead["parent"])
        for dep in bead.get("deps", []):
            bd("dep", "add", bead["id"], dep)
        for key, value in (bead.get("metadata") or {}).items():
            bd("update", bead["id"], "--set-metadata", f"{key}={value}")
    git("add", "-A")
    if sh(["git", "status", "--porcelain"], cwd=toy).stdout.strip():
        git("commit", "-q", "-m", "bench: beads and tooling")
    seed_sha = sh(["git", "rev-parse", "HEAD"], cwd=toy).stdout.strip()
    (toy / ".alloy" / "bench.json").write_text(
        json.dumps(
            {"scenario": scenario, "version": spec.get("version"), "alloy_sha": sha, "seed_sha": seed_sha}, indent=2
        )
    )
    log(f"reset {toy} scenario={scenario} seed={seed_sha[:10]} alloy={sha[:10]}")


# --- start / wait ------------------------------------------------------------
def start(toy: Path, sha: str) -> None:
    alloy(sha, toy, "start", "--poll", "15", "--stall-minutes", "20")
    log(f"started scheduler for {toy.name}")


def ready_work(toy: Path) -> list[str]:
    rows = json.loads(sh(["bd", "ready", "--json", "-n", "0"], cwd=toy).stdout or "[]")
    return [r["id"] for r in rows if r.get("issue_type") != "epic" and "manual" not in (r.get("labels") or [])]


def active_runs(toy: Path, sha: str) -> list[str]:
    """Runs that can still make progress on their own. A waiting-human run
    does not: with no operator it only ends by Alloy's auto-park, which fires
    only when it holds other ready work -- so it never counts as active here."""
    db = toy / ".alloy" / "alloy.db"
    if not db.exists():
        return []
    working = tuple(s for s in ACTIVE if s != "waiting-human")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            f"SELECT bead_id FROM runs WHERE status IN ({','.join('?' * len(working))})", working
        ).fetchall()
    return [row[0] for row in rows]


def scheduler_alive(toy: Path) -> bool:
    pid_file = toy / ".alloy" / "scheduler.pid"
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def wait(toys: list[Path], sha: str, hours: float) -> None:
    deadline = time.monotonic() + hours * 3600
    pending = set(toys)
    quiet: dict[Path, int] = {t: 0 for t in toys}
    while pending and time.monotonic() < deadline:
        time.sleep(60)
        for toy in sorted(pending):
            ready, active = ready_work(toy), active_runs(toy, sha)
            alive = scheduler_alive(toy)
            quiet[toy] = quiet[toy] + 1 if not ready and not active else 0
            if quiet[toy] >= 3 or not alive:
                why = "queue empty" if quiet[toy] >= 3 else "scheduler gone"
                log(f"{toy.name}: {why}")
                alloy(sha, toy, "stop", "--now", check=False)
                pending.discard(toy)
    for toy in pending:
        log(f"{toy.name}: deadline reached; stopping")
        alloy(sha, toy, "stop", "--now", check=False)
        (toy / ".alloy" / "bench-deadline").write_text(datetime.now(timezone.utc).isoformat())


# --- report ------------------------------------------------------------------
def oracle(toy: Path) -> dict:
    """Score with the toy's own `python3` -- the interpreter its agents and
    checks use -- never this script's (Alloy's venv may be another version:
    3.13 vs 3.14 once scored a working tree 0.0 on eager annotations)."""
    env = toy_env(toy)
    python = shutil.which("python3", path=env["PATH"]) or "python3"
    proc = sh([python, "-I", str(SHEET / "oracle" / "oracle.py"), "--toy", str(toy), "--json"], env=env)
    return json.loads(proc.stdout)


def audit_transcripts(toy: Path) -> list[str]:
    """Log files that mention the bench's own path: an agent that found the
    oracle or the reference implementation invalidates the run."""
    hits = []
    needles = (str(SHEET), "bench/sheet", "oracle/cases.py", "oracle.py", "reference/sheet")
    for path in (toy / ".alloy" / "logs").rglob("*"):
        if path.is_file():
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if any(n in text for n in needles):
                hits.append(str(path.relative_to(toy)))
    return hits


def report(toy: Path, sha: str, out_name: str | None) -> Path:
    meta = json.loads((toy / ".alloy" / "bench.json").read_text())
    spec = load_scenario(meta["scenario"])
    metrics = json.loads(alloy(sha, toy, "metrics", "--json").stdout)
    beads = json.loads(sh(["bd", "list", "--all", "--json", "-n", "0", "--flat"], cwd=toy).stdout or "[]")
    scored = oracle(toy)
    seed_files = {p.relative_to(SHEET / "seed").as_posix() for p in (SHEET / "seed").rglob("*") if p.is_file()}
    changed = sh(["git", "diff", "--name-only", meta["seed_sha"], "HEAD"], cwd=toy).stdout.split()
    status = {b["id"]: b.get("status") for b in beads}
    areas = scored["areas"]
    closed_but_failing = [
        {"bead": b["id"], "area": a, "fraction": areas[a]["fraction"]}
        for b in spec["beads"]
        for a in b.get("areas", [])
        if status.get(b["id"]) == "closed" and a in areas and areas[a]["fraction"] < 1.0
    ]
    checks = check_stats(toy)
    probe_results = probes(toy, spec, status)
    data = {
        "meta": {
            **meta,
            "toy": str(toy),
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "deadline_hit": (toy / ".alloy" / "bench-deadline").exists(),
            "cli": cli_versions(),
        },
        "oracle": {k: scored[k] for k in ("score", "passed", "total", "areas")},
        "oracle_failures": [c for c in scored["cases"] if not c["ok"]],
        "beads": {
            "by_status": _count(b.get("status") for b in beads if b.get("issue_type") != "epic"),
            "roadmap": {b["id"]: status.get(b["id"]) for b in spec["beads"] if b.get("type", "task") != "epic"},
            "filed_by_alloy": [b["title"] for b in beads if "alloy-bug" in (b.get("labels") or [])],
        },
        "signature": {
            "D1_budget_exhaustion": sum(
                n for c, n in metrics["needs_human"]["by_cause"].items() if c.startswith("max_")
            ),
            "D2_more_verification": sum(
                1
                for e in events(toy)
                if e.get("event") == "needs-human" and "more verification" in str(e.get("reason"))
            ),
            "D3_trap_files_touched": sorted(
                f
                for f in changed
                if f in ("sheet/ids.py", "sheet/units.py", "tests/test_ids.py", "tests/test_units.py")
            ),
            "D3_trap_bugs": [b["title"] for b in beads if "alloy-bug" in (b.get("labels") or []) and _mentions_trap(b)],
            "D3_red_checks_only_traps": checks["red_only_traps"],
            "D4_probes_red_after_close": [
                p["cmd"] for p in probe_results if p["bead_status"] == "closed" and p["exit"] != 0
            ],
            "D4_D8_closed_but_failing": closed_but_failing,
            "D5_baseline": sum(n for c, n in metrics["needs_human"]["by_cause"].items() if c.startswith("baseline")),
            "D6_parks": metrics["parks"],
            "D7_duplicate_bugs": metrics["bugs"]["duplicates"],
        },
        "checks": checks,
        "probes": probe_results,
        "metrics": metrics,
        "seed_files_changed": sorted(f for f in changed if f in seed_files),
        "transcript_audit": audit_transcripts(toy),
    }
    results = BENCH / "sheet" / "results"
    results.mkdir(exist_ok=True)
    name = out_name or f"{datetime.now().strftime('%Y%m%d-%H%M')}-{meta['scenario']}-{toy.name}"
    target = results / f"{name}.json"
    target.write_text(json.dumps(data, indent=2))
    log(f"report {target.name}: oracle {data['oracle']['score']:.3f}, beads {data['beads']['by_status']}")
    return target


TRAP_TESTS = ("tests/test_ids.py", "tests/test_units.py")


def check_stats(toy: Path) -> dict:
    """Every check Alloy ran, from the per-run check logs: count by kind,
    reds, and reds whose only failures are the seed's trap tests."""
    out: dict = {"total": 0, "red": 0, "by_kind": {}, "red_only_traps": 0, "red_with_traps": 0}
    for log_file in (toy / ".alloy" / "logs").glob("*/check-*.log"):
        text = log_file.read_text(errors="replace")
        kind = re.search(r"^kind=(\S+)", text, re.M)
        exit_code = re.search(r"^exit=(-?\d+)", text, re.M)
        out["total"] += 1
        key = kind.group(1) if kind else "?"
        out["by_kind"][key] = out["by_kind"].get(key, 0) + 1
        if not exit_code or exit_code.group(1) == "0":
            continue
        out["red"] += 1
        failed = re.findall(r"^(?:FAILED|ERROR) (\S+?)(?:::|\s|$)", text, re.M)
        traps = [f for f in failed if f in TRAP_TESTS]
        if traps:
            out["red_with_traps"] += 1
            if len(traps) == len(failed):
                out["red_only_traps"] += 1
    return out


def probes(toy: Path, spec: dict, status: dict) -> list[dict]:
    """The scenario's hidden regression probes: commands that must stay green
    once the named bead is closed (a red one is D4: a regression reached done)."""
    results = []
    for probe in spec.get("probes", []):
        proc = subprocess.run(
            probe["cmd"], shell=True, cwd=toy, env=toy_env(toy), capture_output=True, text=True, timeout=300
        )
        results.append(
            {
                "bead": probe["bead"],
                "bead_status": status.get(probe["bead"]),
                "cmd": probe["cmd"],
                "exit": proc.returncode,
                "tail": proc.stdout[-600:],
            }
        )
    return results


def _mentions_trap(bead: dict) -> bool:
    text = f"{bead.get('title', '')} {bead.get('description', '')}".lower()
    return any(k in text for k in ("test_ids", "test_units", "units.py", "ids.py", "inch", "72", "unique"))


def _count(items) -> dict:
    out: dict = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def events(toy: Path) -> list[dict]:
    path = toy / ".alloy" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def cli_versions() -> dict:
    out = {}
    for name, args in (
        ("cursor-agent", ["cursor-agent", "--version"]),
        ("claude", ["claude", "--version"]),
        ("bd", ["bd", "--version"]),
    ):
        try:
            out[name] = sh(args, check=False).stdout.strip()[:80]
        except OSError:
            out[name] = None
    return out


# --- smoke ---------------------------------------------------------------------
def smoke() -> int:
    """One trivial call per runner/model the bench recipes use."""
    checks = [
        ["cursor-agent", "-p", "--model", "composer-2.5", "--output-format", "text", "Reply with the single word OK."],
        ["claude", "-p", "--model", "haiku", "--effort", "low", "Reply with the single word OK."],
    ]
    failed = 0
    for args in checks:
        try:
            proc = sh(args, check=False, timeout=120)
            ok = proc.returncode == 0 and "OK" in proc.stdout.upper()
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, proc = False, exc
        log(f"{'ok  ' if ok else 'FAIL'} {' '.join(args[:4])}")
        failed += not ok
    return 1 if failed else 0


# --- campaign --------------------------------------------------------------------
def campaign(scenario: str, runs: int, sha: str, hours: float, first: int) -> None:
    toys = [TOY_PARENT / f"alloy-toy-sheet-{scenario}-{n}" for n in range(first, first + runs)]
    for toy in toys:
        reset(scenario, toy, sha)
    for toy in toys:
        start(toy, sha)
    wait(toys, sha, hours)
    for toy in toys:
        report(toy, sha, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("reset", "start", "wait", "report", "campaign", "smoke", "score"):
        p = sub.add_parser(name)
        p.add_argument("--alloy-ref", default="HEAD")
        p.add_argument("--dir", type=Path)
        p.add_argument("--scenario", default="full")
        p.add_argument("--runs", type=int, default=1)
        p.add_argument("--first", type=int, default=1)
        p.add_argument("--hours", type=float, default=8.0)
        p.add_argument("--out")
    args = parser.parse_args()
    if args.cmd == "smoke":
        return smoke()
    sha = resolve_ref(args.alloy_ref)
    toy = args.dir.expanduser().resolve() if args.dir else None
    if args.cmd == "reset":
        reset(args.scenario, toy, sha)
    elif args.cmd == "start":
        start(toy, sha)
    elif args.cmd == "wait":
        wait([toy], sha, args.hours)
    elif args.cmd == "report":
        report(toy, sha, args.out)
    elif args.cmd == "score":
        print(json.dumps({k: v for k, v in oracle(toy).items() if k != "cases"}, indent=2))
    elif args.cmd == "campaign":
        campaign(args.scenario, args.runs, sha, args.hours, args.first)
    return 0


if __name__ == "__main__":
    sys.exit(main())
