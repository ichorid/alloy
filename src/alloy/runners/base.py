"""Common interface and subprocess plumbing for coding-agent harnesses.

Every adapter turns one CLI into an :class:`AgentResult`. Provider quirks --
flag names, JSON envelopes, how structured output is coaxed out -- stop here and
never reach the workflow graph.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from alloy.models import AgentResult, RunnerUnavailable, clip, prompt_hash, utcnow
from alloy.procs import terminate_process_tree

DEFAULT_TIMEOUT = timedelta(minutes=20)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@runtime_checkable
class AgentRunner(Protocol):
    name: str

    async def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        model: str | None = None,
        effort: str | None = None,
        timeout: timedelta | None = None,
        structured_schema: dict | None = None,
        on_spawn: Callable[[int], None] | None = None,
    ) -> AgentResult: ...


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort recovery of a JSON object from free-form agent output."""
    if not text:
        return None
    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    candidates.extend(reversed(_FENCE.findall(text)))
    candidates.extend(reversed(_balanced_objects(text)))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _balanced_objects(text: str) -> list[str]:
    """Every top-level {...} span, in order of appearance."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start : index + 1])
    return spans


def _failure_message(text: str, stderr: str, exit_code: int) -> str:
    """What to record for a failed call: the parsed answer first (for codex
    that is the JSONL `error` event, e.g. "out of credits"), then stderr --
    whose first line is often just noise like "Reading additional input
    from stdin..."."""
    parts = [part.strip() for part in (text, stderr) if part and part.strip()]
    return clip("\n".join(parts), 1000) or f"exit {exit_code}"


def schema_instructions(schema: dict[str, Any]) -> str:
    """Prompt suffix for harnesses without native structured output."""
    return (
        "\n\n---\nRespond with a single JSON object and nothing else -- no prose "
        "before or after, no markdown fence. It must validate against this schema:\n"
        f"{json.dumps(schema, indent=2)}\n"
    )


def _accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    """Subclasses (GenericCLIRunner, user adapters) may predate a keyword; only
    pass it to those that declare it."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


class CLIRunner:
    """Base adapter: builds argv, runs it, records raw output, normalizes the result.

    Subclasses supply :meth:`build_command` and :meth:`parse`.
    """

    name: str = "cli"
    binary: str = ""
    default_model: str | None = None
    supports_native_schema: bool = False

    def __init__(
        self,
        binary: str | None = None,
        *,
        default_model: str | None = None,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.binary = binary or self.binary
        self.default_model = default_model or self.default_model
        self.extra_args = list(extra_args or [])
        self.env_overrides = dict(env or {})
        self.log_dir = log_dir

    # -- capability -------------------------------------------------------

    def resolve_binary(self) -> str | None:
        return shutil.which(self.binary)

    def available(self) -> bool:
        return self.resolve_binary() is not None

    # -- subclass hooks ---------------------------------------------------

    def build_command(
        self,
        prompt: str,
        *,
        model: str | None,
        structured_schema: dict | None,
        effort: str | None = None,
    ) -> list[str]:
        """Arguments after the binary. ``effort`` is only passed to subclasses
        that declare it, so adapters without an effort flag need not know."""
        raise NotImplementedError

    def build_prompt(self, prompt: str, structured_schema: dict | None) -> str:
        if structured_schema and not self.supports_native_schema:
            return prompt + schema_instructions(structured_schema)
        return prompt

    def parse(self, stdout: str, stderr: str, exit_code: int) -> tuple:
        """Return ``(text, structured, usage, session_id)`` or, with a fifth
        element, ``(text, structured, usage, session_id, failed)``.

        ``failed`` lets an adapter report that the harness signalled an error
        inside its output envelope even though the process exited 0 (a cursor
        rate limit, a codex ``error`` event with no answer after it). The call
        is then recorded as not ok so the role's fallback fires."""
        return stdout.strip(), extract_json_object(stdout), {}, None

    # -- execution --------------------------------------------------------

    async def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        model: str | None = None,
        effort: str | None = None,
        timeout: timedelta | None = None,
        structured_schema: dict | None = None,
        on_spawn: Callable[[int], None] | None = None,
    ) -> AgentResult:
        """`on_spawn(pid)` fires once the harness exists (journal 9): the
        caller records the pid so the process group can still be killed if
        `alloy run` itself dies before the call ends."""
        binary_path = self.resolve_binary()
        if binary_path is None:
            raise RunnerUnavailable(f"{self.name}: '{self.binary}' not found on PATH")

        model = model or self.default_model
        effective_prompt = self.build_prompt(prompt, structured_schema)
        build_kwargs: dict[str, Any] = {"model": model, "structured_schema": structured_schema}
        if effort is not None and _accepts_kwarg(self.build_command, "effort"):
            build_kwargs["effort"] = effort
        argv = [binary_path, *self.build_command(effective_prompt, **build_kwargs)]
        digest = prompt_hash(effective_prompt)
        started = utcnow()
        clock = time.monotonic()

        env = {**os.environ, **self.env_overrides}
        limit_s = (timeout or DEFAULT_TIMEOUT).total_seconds()

        timed_out = False
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,  # own process group: see alloy.procs
            )
        except OSError as exc:
            raise RunnerUnavailable(f"{self.name}: cannot execute {binary_path}: {exc}") from exc

        try:
            if on_spawn is not None:
                on_spawn(process.pid)
            raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=limit_s)
        except asyncio.TimeoutError:
            timed_out = True
            raw_out, raw_err = b"", b""
            await terminate_process_tree(process)
        except BaseException:
            # Cancellation (operator stop, scheduler shutdown, SIGTERM): the
            # harness must die with us, not keep editing the worktree.
            await terminate_process_tree(process)
            raise

        stdout = raw_out.decode("utf-8", "replace")
        stderr = raw_err.decode("utf-8", "replace")
        exit_code = -1 if timed_out else (process.returncode or 0)
        duration = time.monotonic() - clock

        log_path = self._write_log(
            digest, argv, effective_prompt, stdout, stderr, exit_code, started=started
        )

        if timed_out:
            return AgentResult(
                runner=self.name, model=model, ok=False, exit_code=exit_code,
                text="", structured=None, started_at=started, ended_at=utcnow(),
                duration_s=duration, log_path=log_path, prompt_hash=digest,
                error=f"timed out after {limit_s:.0f}s",
            )

        text, structured, usage, session_id, failed = self._normalise_parsed(
            self.parse(stdout, stderr, exit_code)
        )
        ok = exit_code == 0 and not failed
        if ok and structured_schema and structured is None:
            structured = extract_json_object(text)
        return AgentResult(
            runner=self.name,
            model=model,
            ok=ok,
            exit_code=exit_code,
            text=clip(text, 20000),
            structured=structured,
            started_at=started,
            ended_at=utcnow(),
            duration_s=duration,
            usage=usage,
            log_path=log_path,
            prompt_hash=digest,
            error=None if ok else _failure_message(text, stderr, exit_code),
            session_id=session_id,
        )

    @staticmethod
    def _normalise_parsed(parsed: tuple) -> tuple[str, dict | None, dict, str | None, bool]:
        """Accept the historical 4-tuple from :meth:`parse` as well as the
        5-tuple with ``failed``."""
        if len(parsed) == 4:
            return (*parsed, False)
        text, structured, usage, session_id, failed = parsed
        return text, structured, usage, session_id, bool(failed)

    def _write_log(
        self,
        digest: str,
        argv: list[str],
        prompt: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        *,
        started: datetime | None = None,
    ) -> str | None:
        """Raw transcripts live on disk, never in graph state.

        The filename carries the call's *start* time so `ls` shows calls in
        the order they began -- a 20-minute call must not sort after the
        test log written a second after it finished.
        """
        if self.log_dir is None:
            return None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = int((started.timestamp() if started else time.time()) * 1000)
        path = self.log_dir / f"{stamp}-{self.name}-{digest}.json"
        payload = {
            "runner": self.name,
            "argv": argv,
            "prompt": prompt,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(path)
