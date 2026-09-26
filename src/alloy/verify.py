"""Deterministic verification.

Alloy runs the test command itself. An LLM is never asked to "run the tests and
tell me what happened" -- the exit code is the fact, and the agents only ever see
a summary of it.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from alloy.models import CheckRequest, CheckResult, VerifierAction, clip
from alloy.procs import terminate_process_tree

DEFAULT_TIMEOUT_S = 900.0


def verifier_check_requests(action: VerifierAction) -> list[CheckRequest]:
    """Expand a verifier response into the checks Alloy should run.

    A batch response lists several entries in ``checks``; a legacy response
    names one command on the action itself."""
    if action.checks:
        return [
            CheckRequest(
                command=check.command.strip(),
                purpose=check.purpose,
                kind=check.kind,
                required=check.required,
            )
            for check in action.checks
            if check.command.strip()
        ]
    if action.action == "run" and action.command.strip():
        return [action.to_request()]
    return []


# Ordered: first marker file whose project layout matches wins.
AUTODETECT: list[tuple[str, str]] = [
    ("pytest.ini", "python -m pytest -q"),
    ("pyproject.toml", "python -m pytest -q"),
    ("setup.cfg", "python -m pytest -q"),
    ("package.json", "npm test --silent"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("Makefile", "make test"),
]

_PYTEST = re.compile(r"(?:(\d+) passed)|(?:(\d+) failed)|(?:(\d+) errors?)")
# pytest's final line: "3 passed, 1 failed in 0.42s" (quiet) or wrapped in
# "=====" (verbose). Only that line is authoritative -- "1 error during
# collection" earlier in the output must not be counted a second time.
_PYTEST_SUMMARY = re.compile(
    r"^[=\s]*(?P<body>[^\n]*?\b(?:passed|failed|errors?|skipped|no tests ran)\b[^\n]*?)"
    r"\s+in\s+[\d.]+s\b",
    re.MULTILINE,
)
_JEST = re.compile(r"Tests:\s+(?:(\d+) failed,\s*)?(?:\d+ skipped,\s*)?(\d+) passed")
_CARGO = re.compile(r"test result: \w+\. (\d+) passed; (\d+) failed")


_ALLOY_SRC = "src/alloy/"

# Cross-cutting workflow code is exercised by tests whose names do not match
# the source module. These remain suggestions for the verifier, not checks.
_WORKFLOW_TESTS = {
    "tdd_loop.py": ("test_workflow.py", "test_prompts.py", "test_triage.py", "test_verify.py"),
    "state.py": ("test_workflow.py", "test_memory_injection.py"),
    "role_prompts.py": ("test_prompts.py", "test_workflow.py"),
    "shared_verification.py": ("test_verify.py", "test_workflow.py", "test_land.py"),
    "bug_triage.py": ("test_triage.py", "test_remediate.py", "test_workflow.py"),
}


def test_stems_from_changed_alloy_src(changed_files: list[str]) -> set[str]:
    """Map touched ``src/alloy`` modules to ``tests/test_<stem>*.py`` stems.

    Top-level ``src/alloy/foo.py`` maps to ``foo``. Subpackages use the first
    directory, e.g. ``src/alloy/monitor/app.py`` -> ``monitor``.
    """
    stems: set[str] = set()
    for path in changed_files:
        if not path.startswith(_ALLOY_SRC) or not path.endswith(".py"):
            continue
        rel = path[len(_ALLOY_SRC) :]
        parts = rel.split("/")
        if len(parts) == 1:
            stems.add(Path(parts[0]).stem)
        else:
            stems.add(parts[0])
    return stems


def diff_derived_test_paths(changed_files: list[str], repo_root: Path) -> list[str]:
    """Test modules that likely cover changed ``src/alloy`` code (hints only)."""
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for stem in sorted(test_stems_from_changed_alloy_src(changed_files)):
        for match in sorted(tests_dir.glob(f"test_{stem}*.py")):
            rel = match.relative_to(repo_root).as_posix()
            if rel not in seen:
                seen.add(rel)
                paths.append(rel)
    for source in changed_files:
        if not source.startswith("src/alloy/recipes/"):
            continue
        for name in _WORKFLOW_TESTS.get(Path(source).name, ()):
            rel = f"tests/{name}"
            if rel not in seen and (repo_root / rel).is_file():
                seen.add(rel)
                paths.append(rel)
    return paths


def detect_commands(worktree: Path) -> list[str]:
    """Every command the project layout suggests, best guess first. Hints for
    the verifier; a repo with both a pyproject and a Cargo.toml gets both."""
    commands: list[str] = []
    for marker, command in AUTODETECT:
        if (worktree / marker).exists() and command not in commands:
            commands.append(command)
    return commands


def detect_command(worktree: Path) -> str | None:
    commands = detect_commands(worktree)
    return commands[0] if commands else None


def executable_of(command: str) -> str | None:
    """The program a shell command would actually run, if it is a simple one."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    return tokens[0] if tokens else None


def is_runnable(command: str) -> bool:
    program = executable_of(command)
    if program is None:
        return False
    if any(character in command for character in "|&;<>$`"):
        return True  # a real shell expression; let the shell decide
    return shutil.which(program) is not None


def normalize_command(command: str) -> str:
    """Repoint a bare `python` at an interpreter that exists.

    Agents suggest `python -m pytest` constantly, and on a machine that only has
    `python3` that is exit 127 -- which looks exactly like a failing test suite
    and would burn the whole retry budget chasing it.
    """
    program = executable_of(command)
    if program != "python" or shutil.which("python"):
        return command
    replacement = shutil.which("python3") or sys.executable
    return command.replace("python", replacement, 1)


async def run_check(
    request: CheckRequest,
    worktree: Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    log_dir: Path | None = None,
    index: int = 0,
) -> CheckResult:
    """Run one shell command, capture what happened, persist the evidence."""
    started = time.monotonic()
    process = await asyncio.create_subprocess_shell(
        request.command,
        cwd=str(worktree),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=command_env(worktree),
        start_new_session=True,
    )
    timed_out = False
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        raw = b""
        await terminate_process_tree(process)
    except BaseException:
        await terminate_process_tree(process)
        raise

    output = raw.decode("utf-8", "replace")
    duration = time.monotonic() - started
    exit_code = -1 if timed_out else (process.returncode or 0)
    passed, failed = parse_counts(output)

    log_path: str | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"check-{index}-{request.kind}-{int(time.time() * 1000)}.log"
        header = f"$ {request.command}\npurpose={request.purpose}\nkind={request.kind}\nexit={exit_code}\n\n"
        path.write_text(header + output, encoding="utf-8")
        log_path = str(path)

    return CheckResult(
        command=request.command,
        purpose=request.purpose,
        kind=request.kind,
        required=request.required,
        exit_code=exit_code,
        duration_s=duration,
        timed_out=timed_out,
        output_tail=clip(output, 3000),
        log_path=log_path,
        passed=passed,
        failed=failed,
    )


def checks_summary(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `checks` object of `alloy status --json` and the monitor snapshot.

    Read from the graph state: how many verifier checks the run has executed,
    how many of those in the current iteration, and what the last one said.
    None until the first check has run."""
    checks = list(state.get("checks") or [])
    if not checks:
        return None
    last = CheckResult.model_validate(checks[-1])
    return {
        "total": len(checks),
        "iteration": int(state.get("iteration_checks", 0) or 0),
        "last": {
            "command": last.command,
            "kind": last.kind,
            "exit_code": last.exit_code,
            "headline": last.headline(),
        },
    }


def check_logs(log_dir: Path | str | None) -> list[dict[str, Any]]:
    """Every check-*.log a run wrote, in start order, with the kind and exit
    code from its header (`check-{index}-{kind}-{ms}.log`, see `run_check`)."""
    if not log_dir or not Path(log_dir).is_dir():
        return []
    entries: list[tuple[int, int, dict[str, Any]]] = []
    for path in Path(log_dir).glob("check-*.log"):
        parts = path.stem.split("-")
        try:
            index, started_ms = int(parts[1]), int(parts[-1])
        except (IndexError, ValueError):
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:4]
        header = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
        command = lines[0] if lines else ""
        entries.append(
            (
                index,
                started_ms,
                {
                    "index": index,
                    "command": command[2:] if command.startswith("$ ") else command,
                    "kind": header.get("kind") or "-".join(parts[2:-1]) or "custom",
                    "exit_code": int(header["exit"]) if header.get("exit", "").lstrip("-").isdigit() else None,
                    "log_path": str(path),
                },
            )
        )
    return [entry for _, _, entry in sorted(entries, key=lambda item: item[:2])]


def command_env(worktree: Path) -> dict[str, str]:
    """Environment for a check command.

    The worktree's own code must be what gets imported. A Python project
    installed in editable mode into a shared virtualenv points that venv at
    the *main* checkout via a `.pth` file, so a plain `python -m pytest`
    inside a worktree silently tests code the agents never touched. Putting
    the worktree's `src/` (and its root, for flat layouts) first on
    PYTHONPATH wins over the `.pth` entry; for non-Python projects the
    variable is simply ignored.
    """
    entries = [str(worktree / "src")] if (worktree / "src").is_dir() else []
    entries.append(str(worktree))
    if os.environ.get("PYTHONPATH"):
        entries.append(os.environ["PYTHONPATH"])
    return {
        **os.environ,
        "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(entries),
    }


def parse_counts(output: str) -> tuple[int | None, int | None]:
    """Extract pass/fail counts from common runners; None when unrecognized."""
    cargo = _CARGO.search(output)
    if cargo:
        return int(cargo.group(1)), int(cargo.group(2))

    jest = _JEST.search(output)
    if jest:
        return int(jest.group(2)), int(jest.group(1) or 0)

    summaries = list(_PYTEST_SUMMARY.finditer(output))
    if summaries:
        output = summaries[-1].group("body")  # the last summary line is the verdict

    passed = failed = None
    for match in _PYTEST.finditer(output):
        if match.group(1):
            passed = int(match.group(1))
        elif match.group(2):
            failed = (failed or 0) + int(match.group(2))
        elif match.group(3):
            failed = (failed or 0) + int(match.group(3))
    return passed, failed
