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


def _next_bead_id(config: dict) -> str:
    counter = int(config.get("bead_counter") or 0) + 1
    config["bead_counter"] = counter
    return f"beads-{counter}"


def _parse_flag_value(argv: list[str], flag: str, short: str | None = None) -> str | None:
    flags = [flag]
    if short:
        flags.append(short)
    for index, arg in enumerate(argv):
        if arg in flags and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _cmd_create(config: dict, argv: list[str]) -> int:
    _record("create", argv)
    title_parts: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("-"):
            index += 2 if index + 1 < len(argv) else 1
            continue
        title_parts.append(arg)
        index += 1
    title = " ".join(title_parts)
    issue_type = _parse_flag_value(argv, "--type", "-t") or "task"
    labels_raw = _parse_flag_value(argv, "--labels") or ""
    labels = [label for label in labels_raw.split(",") if label]
    bead_id = _next_bead_id(config)
    beads = list(config.get("beads") or [])
    beads.append(
        {
            "id": bead_id,
            "title": title,
            "issue_type": issue_type,
            "labels": labels,
            "status": "open",
        }
    )
    merged = {**config, "beads": beads}
    Path(os.environ["ALLOY_FAKE_CONFIG"]).write_text(json.dumps(merged), encoding="utf-8")
    if "--silent" in argv:
        print(bead_id)
    else:
        print(json.dumps(beads[-1]))
    return 0


def _cmd_list(config: dict, argv: list[str]) -> int:
    _record("list", argv)
    beads = list(config.get("beads") or [])
    label = _parse_flag_value(argv, "--label")
    status = _parse_flag_value(argv, "--status")
    parent = _parse_flag_value(argv, "--parent")
    if label:
        beads = [bead for bead in beads if label in (bead.get("labels") or [])]
    if status:
        beads = [bead for bead in beads if bead.get("status") == status]
    if parent:
        beads = [bead for bead in beads if bead.get("parent") == parent]
    if "--json" in argv:
        print(json.dumps(beads))
    else:
        for bead in beads:
            print(f"{bead.get('id')}: {bead.get('title')} [{bead.get('status')}]")
    return 0


def _cmd_blocked(config: dict, argv: list[str]) -> int:
    rows = list(config.get("blocked") or [])
    if "--json" in argv:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(f"{row.get('id')}: {row.get('title')} [{row.get('status')}]")
    return 0


def _cmd_show(config: dict, argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("show requires a bead id\n")
        return 1
    bead_id = argv[1]
    shows = config.get("shows") or {}
    bead = shows.get(bead_id)
    if bead is None:
        sys.stderr.write(f"bead {bead_id} not found\n")
        return 1
    if "--json" in argv:
        print(json.dumps(bead))
    else:
        print(f"{bead.get('id')}: {bead.get('title')} [{bead.get('status')}]")
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
        "create": _cmd_create,
        "list": _cmd_list,
        "blocked": _cmd_blocked,
        "show": _cmd_show,
    }
    handler = handlers.get(command)
    if handler is None:
        sys.stderr.write(f'unknown command "{command}"\n')
        return 1
    return handler(config, argv)


if __name__ == "__main__":
    sys.exit(main())
