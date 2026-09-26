#!/usr/bin/env python3
"""Stand-in for a coding-agent CLI.

Emits the exact JSON envelope of the harness it impersonates, so the real
adapters are exercised, and records every invocation. Behaviour comes from
$ALLOY_FAKE_CONFIG, keyed by the role inferred from the prompt -- which keeps
tests readable without coupling them to argv order.
"""

import json
import os
import sys
import time
from pathlib import Path

DEFAULT_ESTIMATE_STRUCTURED = {
    "complexity": "simple",
    "reason": "single-file helper with obvious tests",
    "confidence": 0.9,
}

ROLE_MARKERS = [
    ("context", "You are gathering context"),
    ("estimate", "You are estimating how hard this task is"),
    ("tests_review", "You are an independent reviewer of the tests"),
    ("tests", "Write failing tests"),
    ("triage", "You are triaging a bug report"),
    ("scope", "You are deciding whether a bug fix is safe to merge"),
    ("implement", "Implement the smallest change"),
    ("verifier", "You are choosing the next verification check"),
    ("acceptance", "You are deciding whether there is enough evidence"),
    ("judge", "You are judging whether"),
    ("critic", "You are one of several independent critics"),
    ("synthesize", "Several independent critics reviewed"),
    ("harvest", "You are harvesting a durable lesson"),
    ("memory_reviewer", "You are reviewing project memory"),
]


def find_prompt(argv: list[str]) -> tuple[str, str]:
    """The prompt is whichever argument carries a role marker; a schema argument
    sits alongside it and must not be mistaken for it."""
    candidates = [sys.stdin.read()] if "-" in argv else argv
    for arg in candidates:
        for role, marker in ROLE_MARKERS:
            if marker in arg:
                return role, arg
    prompt = candidates[0] if "-" in argv else sys.stdin.read()
    if prompt:
        for role, marker in ROLE_MARKERS:
            if marker in prompt:
                return role, prompt
        return "unknown", prompt
    candidates = [a for a in argv if not a.startswith("-")]
    return "unknown", max(candidates, key=len) if candidates else ""


def find_resume(argv: list[str]) -> str | None:
    """The session id passed to `--resume <id>` (claude, cursor) or
    `exec resume <id>` (codex), if any."""
    if argv[:2] == ["exec", "resume"] and len(argv) > 2:
        return argv[2]
    for index, arg in enumerate(argv[:-1]):
        if arg == "--resume":
            return argv[index + 1]
    return None


def take(config: dict, role: str, runner: str, counters: Path) -> dict:
    entry = config.get(f"{role}@{runner}", config.get(role))
    if entry is None:
        return {}
    if isinstance(entry, dict):
        return entry
    key = f"{role}@{runner}"
    state = json.loads(counters.read_text()) if counters.exists() else {}
    index = state.get(key, 0)
    state[key] = index + 1
    counters.write_text(json.dumps(state))
    return entry[min(index, len(entry) - 1)] if entry else {}


def apply_side_effects(entry: dict) -> None:
    if entry.get("pidfile"):
        Path(entry["pidfile"]).write_text(str(os.getpid()), encoding="utf-8")
    if entry.get("detached_child_pidfile"):
        # Like codex's sandbox helper: a grandchild in its own process group.
        import subprocess

        child = subprocess.Popen(["sleep", "300"], start_new_session=False, preexec_fn=os.setpgrp)
        Path(entry["detached_child_pidfile"]).write_text(str(child.pid), encoding="utf-8")
    for spec in entry.get("write", []):
        path = Path(spec["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec["content"], encoding="utf-8")
    for path in entry.get("delete", []):
        Path(path).unlink(missing_ok=True)
    if entry.get("sleep"):
        time.sleep(float(entry["sleep"]))


def _is_error(entry: dict, exit_code: int) -> bool:
    return bool(entry["is_error"]) if "is_error" in entry else exit_code != 0


def main() -> int:
    runner = Path(sys.argv[0]).name
    argv = sys.argv[1:]
    role, prompt = find_prompt(argv)
    resume = find_resume(argv)

    workdir = Path(os.environ["ALLOY_FAKE_DIR"])
    workdir.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(os.environ["ALLOY_FAKE_CONFIG"]).read_text())
    entry = take(config, role, runner, workdir / "counters.json")
    if role == "estimate" and "structured" not in entry:
        entry = {**entry, "structured": DEFAULT_ESTIMATE_STRUCTURED}

    record = {
        "runner": runner,
        "role": role,
        "cwd": os.getcwd(),
        "argv": argv,
        "prompt": prompt,
    }
    if resume is not None:
        record["resume"] = resume
    with (workdir / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    apply_side_effects(entry)

    text = entry.get("text", f"{runner} handled {role}")
    session_id = str(entry.get("session_id", "fake-session"))
    structured = entry.get("structured")
    exit_code = int(entry.get("exit", 0))
    if entry.get("stderr"):
        sys.stderr.write(entry["stderr"])

    envelope_is_error = _is_error(entry, exit_code)

    if runner.startswith("claude"):
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": envelope_is_error,
            "result": text,
            "session_id": session_id,
            "num_turns": 1,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        if structured is not None:
            envelope["structured_output"] = structured
        print(json.dumps(envelope))
    elif runner.startswith("codex"):
        thread_id = str(entry.get("session_id", "fake-thread"))
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
        if entry.get("codex_error") is not None:
            print(
                json.dumps(
                    {
                        "type": "error",
                        "message": str(entry["codex_error"]),
                    }
                )
            )
        if not entry.get("codex_error_only"):
            body = text if structured is None else json.dumps(structured)
            print(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message", "text": body},
                    }
                )
            )
        print(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                }
            )
        )
    elif runner.startswith("cursor"):
        body = text if structured is None else json.dumps(structured)
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": envelope_is_error,
                    "duration_ms": 10,
                    "result": body,
                    "session_id": session_id,
                    "usage": {"inputTokens": 100, "outputTokens": 20},
                }
            )
        )
    else:  # pi and any generic templated runner
        body = text if structured is None else json.dumps(structured)
        print(json.dumps({"result": body, "usage": {"input_tokens": 100}}))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
