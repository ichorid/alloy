"""Process-tree control.

Every harness and every test command Alloy starts runs in its own session
(``start_new_session=True``), so the process id doubles as the process-group
id and one ``killpg`` reaches the whole tree -- the node wrapper, the native
binary under it, and whatever shell the agent spawned. Without this, a
timeout or a killed ``alloy run`` left the harness working on its own.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

DEFAULT_GRACE_S = 5.0


def signal_group(pid: int, sig: int) -> bool:
    """Send `sig` to the process group led by `pid`; fall back to the pid alone."""
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False


def tree_pids(root: int) -> set[int]:
    """`root`, every process in its session, and every descendant by ppid.

    The session leader's group is not enough: the codex CLI's children (its
    MCP servers, `codex-linux-sandbox`) call `setpgid`/`setsid` and would
    survive a plain `killpg`. Walking /proc catches them either way.
    """
    children: dict[int, list[int]] = {}
    by_session: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {root}
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        # fields after the comm: state ppid pgrp session ...
        try:
            ppid, session = int(fields[1]), int(fields[3])
        except (ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
        by_session.setdefault(session, []).append(int(entry))
    found = {root}
    queue = [root]
    while queue:
        pid = queue.pop()
        for child in children.get(pid, []):
            if child not in found:
                found.add(child)
                queue.append(child)
    found.update(by_session.get(root, []))
    return found


def signal_tree(root: int, sig: int) -> set[int]:
    """Signal the whole tree under `root` (see `tree_pids`); returns what was hit."""
    pids = tree_pids(root)
    signal_group(root, sig)
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


async def terminate_process_tree(
    process: asyncio.subprocess.Process, *, grace_s: float = DEFAULT_GRACE_S
) -> None:
    """SIGTERM the tree, wait `grace_s`, SIGKILL what is left, reap the leader."""
    if process.returncode is not None:
        return
    signal_tree(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_s)
    except asyncio.TimeoutError:
        pass
    # Descendants that ignored SIGTERM, or were re-parented when the leader
    # died, are still in the session: sweep them.
    survivors = {pid for pid in tree_pids(process.pid) if pid != process.pid and pid_alive(pid)}
    if process.returncode is None or survivors:
        signal_tree(process.pid, signal.SIGKILL)
        if process.returncode is None:
            await process.wait()


def pid_alive(pid: int | None) -> bool:
    """True if `pid` exists and is not a zombie.

    `kill(pid, 0)` succeeds for a zombie -- a process that has exited but whose
    parent has not reaped it -- which would make a dead `alloy run` look busy
    to orphan detection and to `alloy cancel`.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True  # no procfs: trust the signal check
    return state != "Z"


def terminate_pid(pid: int, *, grace_s: float = DEFAULT_GRACE_S) -> bool:
    """Synchronous variant for the CLI: stop one process, escalating.

    Returns True if the process is gone afterwards. The target is expected to
    clean up its own children on SIGTERM (`alloy run` cancels its harness
    group); SIGKILL is the last resort and does not give it that chance.
    """
    if not pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)
