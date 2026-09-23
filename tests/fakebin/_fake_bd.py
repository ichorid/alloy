#!/usr/bin/env python3
"""Stand-in for the `bd` CLI in unit tests.

Behaviour comes from ``$ALLOY_FAKE_CONFIG``. ``remember`` and ``forget``
invocations are appended to ``$ALLOY_FAKE_DIR/calls.jsonl`` so workflow tests
can assert on project-memory writes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _config() -> dict:
    return json.loads(Path(os.environ["ALLOY_FAKE_CONFIG"]).read_text())


def _workdir() -> Path:
    path = Path(os.environ["ALLOY_FAKE_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record(command: str, argv: list[str]) -> None:
    record = {"command": command, "argv": argv}
    with (_workdir() / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _cmd_memories(config: dict, argv: list[str]) -> int:
    if config.get("memories_unsupported"):
        sys.stderr.write('unknown command "memories"\n')
        return 1
    payload = dict(config.get("memories") or {})
    if "--json" in argv:
        print(json.dumps(payload))
    else:
        for key, value in payload.items():
            if key != "schema_version":
                print(f"{key}: {value}")
    return 0


def _cmd_remember(config: dict, argv: list[str]) -> int:
    _record("remember", argv)
    if config.get("remember_fail"):
        sys.stderr.write("remember failed\n")
        return 1
    store = dict(config.get("memories") or {})
    key = None
    content_parts: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--key" and index + 1 < len(argv):
            key = argv[index + 1]
            index += 2
            continue
        if not arg.startswith("-"):
            content_parts.append(arg)
        index += 1
    if key is not None:
        store["schema_version"] = store.get("schema_version", 1)
        store[key] = " ".join(content_parts)
        config_path = Path(os.environ["ALLOY_FAKE_CONFIG"])
        merged = {**config, "memories": store}
        config_path.write_text(json.dumps(merged), encoding="utf-8")
    return 0


def _cmd_note(config: dict, argv: list[str]) -> int:
    _record("note", argv)
    return 0


def _cmd_forget(config: dict, argv: list[str]) -> int:
    _record("forget", argv)
    if len(argv) < 2:
        sys.stderr.write("forget requires a key\n")
        return 1
    key = argv[1]
    store = dict(config.get("memories") or {})
    store.pop(key, None)
    merged = {**config, "memories": store}
    Path(os.environ["ALLOY_FAKE_CONFIG"]).write_text(json.dumps(merged), encoding="utf-8")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: bd <command> ...\n")
        return 2

    config = _config()
    command = sys.argv[1]
    argv = sys.argv[1:]
    handlers = {
        "memories": _cmd_memories,
        "remember": _cmd_remember,
        "note": _cmd_note,
        "forget": _cmd_forget,
    }
    handler = handlers.get(command)
    if handler is None:
        sys.stderr.write(f'unknown command "{command}"\n')
        return 1
    return handler(config, argv)


if __name__ == "__main__":
    sys.exit(main())
