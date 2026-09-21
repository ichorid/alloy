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

ROLE_MARKERS = [
    ("context", "You are gathering context"),
    ("tests", "Write failing tests"),
    ("implement", "Implement the smallest change"),
    ("judge", "You are judging whether"),
    ("critic", "You are one of several independent critics"),
    ("synthesize", "Several independent critics reviewed"),
]


def find_prompt(argv: list[str]) -> tuple[str, str]:
    """The prompt is whichever argument carries a role marker; a schema argument
    sits alongside it and must not be mistaken for it."""
    for arg in argv:
        for role, marker in ROLE_MARKERS:
            if marker in arg:
                return role, arg
    candidates = [a for a in argv if not a.startswith("-")]
    return "unknown", max(candidates, key=len) if candidates else ""


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
    for spec in entry.get("write", []):
        path = Path(spec["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec["content"], encoding="utf-8")
    for path in entry.get("delete", []):
        Path(path).unlink(missing_ok=True)
    if entry.get("sleep"):
        time.sleep(float(entry["sleep"]))


def main() -> int:
    runner = Path(sys.argv[0]).name
    argv = sys.argv[1:]
    role, prompt = find_prompt(argv)

    workdir = Path(os.environ["ALLOY_FAKE_DIR"])
    workdir.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(os.environ["ALLOY_FAKE_CONFIG"]).read_text())
    entry = take(config, role, runner, workdir / "counters.json")

    with (workdir / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "runner": runner, "role": role, "cwd": os.getcwd(),
            "argv": argv, "prompt": prompt,
        }) + "\n")

    apply_side_effects(entry)

    text = entry.get("text", f"{runner} handled {role}")
    structured = entry.get("structured")
    exit_code = int(entry.get("exit", 0))
    if entry.get("stderr"):
        sys.stderr.write(entry["stderr"])

    if runner.startswith("claude"):
        envelope = {
            "type": "result", "subtype": "success", "is_error": exit_code != 0,
            "result": text, "session_id": "fake-session", "num_turns": 1,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        if structured is not None:
            envelope["structured_output"] = structured
        print(json.dumps(envelope))
    elif runner.startswith("codex"):
        print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}))
        body = text if structured is None else json.dumps(structured)
        print(json.dumps({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": body},
        }))
        print(json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }))
    elif runner.startswith("cursor"):
        body = text if structured is None else json.dumps(structured)
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": exit_code != 0,
            "duration_ms": 10, "result": body, "session_id": "fake-session",
            "usage": {"inputTokens": 100, "outputTokens": 20},
        }))
    else:  # pi and any generic templated runner
        body = text if structured is None else json.dumps(structured)
        print(json.dumps({"result": body, "usage": {"input_tokens": 100}}))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
