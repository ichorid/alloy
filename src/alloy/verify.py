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

from alloy.models import TestReport, clip
from alloy.procs import terminate_process_tree

DEFAULT_TIMEOUT_S = 900.0

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


def detect_command(worktree: Path) -> str | None:
    for marker, command in AUTODETECT:
        if (worktree / marker).exists():
            return command
    return None


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


def resolve_command(
    worktree: Path, *, configured: str | None = None, from_context: str | None = None
) -> str | None:
    """Pick a test command that can actually run.

    Explicit configuration beats the context agent's suggestion, which beats
    autodetection -- but a candidate whose program is not installed loses to one
    that is, because an unrunnable command is indistinguishable from a broken
    test suite once it has run.
    """
    candidates = [
        normalize_command(candidate)
        for candidate in (configured, from_context, detect_command(worktree))
        if candidate
    ]
    if not candidates:
        return None
    for candidate in candidates:
        if is_runnable(candidate):
            return candidate
    return candidates[0]


async def run_tests(
    command: str,
    worktree: Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    log_dir: Path | None = None,
) -> TestReport:
    started = time.monotonic()
    process = await asyncio.create_subprocess_shell(
        command,
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
        path = log_dir / f"tests-{int(time.time() * 1000)}.log"
        path.write_text(f"$ {command}\nexit={exit_code}\n\n{output}", encoding="utf-8")
        log_path = str(path)

    return TestReport(
        command=command,
        exit_code=exit_code,
        passed=passed,
        failed=failed,
        duration_s=duration,
        tail=clip(output, 3000),
        log_path=log_path,
        timed_out=timed_out,
    )


def command_env(worktree: Path) -> dict[str, str]:
    """Environment for the test command.

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
