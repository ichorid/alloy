"""Failures that are not this bead's: pre-existing and flaky tests.

A red check can fail on tests the bead never broke: tests already red on the
run's base commit (pre-existing) and tests that pass when simply run again
(flaky). Both cost cheap models whole iterations -- the implementer "fixes" a
test far outside its task, acceptance calls that change out of scope, and the
run swings between the two until a limit stops finished work (bench
calibration 2026-11-07; about half of Tentura's alloy-bug beads).

Two users share the machinery here:

- the landing gate (`landing_gate.rerun_on_base` per gate check): a red gate
  check whose every failing test fails the same way on the base is let
  through;
- the check loop (`verification.rerun_tests`, a command template with
  `{files}` such as `pytest -q {files}`): every red check whose log names
  failing tests gets one more look. Failing tests outside the files this
  bead changed run once more on the current tree -- a pass is *flaky* -- and
  the tests still red run on the base commit in a throwaway worktree -- a
  failure there is *pre-existing*. A check whose every failure is one of the
  two is *excused*: it no longer counts as red, its headline names the tests,
  and later implement prompts list them as not this bead's to touch. One
  alloy-bug bead per such test is filed (deduplicated against open ones).

Never excused: load/compile failures, logs without test names, timeouts,
failures of a test in a file this bead changed that pass on the base.
Pre-existing verdicts are kept for the run (state `known_red`), so a test
runs on the base at most once.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterable

from alloy import verify
from alloy.models import CheckRequest, CheckResult, clip
from alloy.recipes.role_prompts import log

PRE_EXISTING = "pre-existing"
FLAKY = "flaky"
MAX_RERUN_FILES = 20
"""A red check naming more failing test files than this is left as it is:
that many failures are a broken tree, not a few strays."""

# -- failing test names ---------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DART_FAILURE = re.compile(r"^\d+:\d+ \+\d+(?: ~\d+)? -\d+: (?P<body>.+?) \[E\]\s*$")
_DART_FILE = re.compile(r"^(?P<file>\S+?_test\.dart): (?P<name>.+)$")
_PYTEST_FAILURE = re.compile(r"^(?P<kind>FAILED|ERROR) (?P<id>\S+)")
_PACKAGE_TEST_PATH = re.compile(r"(?:^|/)((?:test|integration_test)/\S+)$")
LOAD_FAILURE = "<load>"
"""Name of a test file that failed to load or compile: never pre-existing."""


def _relative(path: str, roots: Iterable[Path]) -> str:
    for root in roots:
        prefix = str(root).rstrip("/") + "/"
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    # Dart prints a package's tests relative to the package when run inside
    # it and absolute otherwise; both reduce to `test/...` from the package.
    match = _PACKAGE_TEST_PATH.search(path) if path.endswith(".dart") else None
    return match.group(1) if match else path


def failing_tests(
    text: str, *, roots: Iterable[Path] = (), single_file: str | None = None
) -> set[tuple[str | None, str]]:
    """(test file, test name) for every failing test a dart/flutter or pytest
    log reports. A load/compile failure has the name LOAD_FAILURE. Dart omits
    the file when one file ran: `single_file` names it."""
    roots = tuple(roots)
    found: set[tuple[str | None, str]] = set()
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        dart = _DART_FAILURE.match(line)
        if dart:
            body = dart.group("body")
            if body.startswith("loading "):
                found.add((_relative(body[len("loading ") :].strip(), roots), LOAD_FAILURE))
                continue
            named = _DART_FILE.match(body)
            if named:
                found.add((_relative(named.group("file"), roots), named.group("name").strip()))
            else:
                found.add((single_file, body.strip()))
            continue
        pytest = _PYTEST_FAILURE.match(line)
        if pytest:
            ident = pytest.group("id")
            file, _, name = ident.partition("::")
            if pytest.group("kind") == "ERROR" and not name:
                name = LOAD_FAILURE
            found.add((_relative(file, roots), name or LOAD_FAILURE))
    return found


def read_log(result: CheckResult) -> str:
    if result.log_path:
        try:
            return Path(result.log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return result.output_tail or ""


def failure_key(test: tuple[str | None, str]) -> str:
    return f"{test[0] or ''}::{test[1]}"


def _rerun_command(template: str, files: list[str]) -> str:
    return template.replace("{files}", " ".join(shlex.quote(f) for f in files))


async def rerun_on_base(
    ctx, template: str, files: list[str], *, base_setup: str | None, timeout_s: float, purpose: str
) -> dict[str, Any]:
    """Run `template` with `files` on the run's base commit in a throwaway
    worktree: {"ran": True, "failures": [test keys]} or {"ran": False, "why"}."""
    worktree = ctx.worktree
    repo = ctx.worktrees.repo
    target = Path(ctx.worktrees.root) / f"base-{ctx.run_id[:16]}"

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=300)

    await asyncio.to_thread(git, "worktree", "remove", "--force", str(target))
    added = await asyncio.to_thread(git, "worktree", "add", "--detach", "--force", str(target), worktree.base_commit)
    if added.returncode != 0:
        return {"ran": False, "why": f"git worktree add failed: {added.stderr.strip()[:200]}"}
    try:
        index = _log_index(ctx)
        if base_setup:
            setup = await verify.run_check(
                CheckRequest(command=base_setup, purpose=f"{purpose} base setup", kind="regression"),
                target,
                timeout_s=timeout_s,
                log_dir=ctx.log_dir,
                index=index,
            )
            if not setup.ok:
                return {"ran": False, "why": f"base setup {setup.headline()}"}
            index += 1
        command = _rerun_command(template, files)
        rerun = await verify.run_check(
            CheckRequest(command=command, purpose=f"{purpose} rerun on the base commit", kind="regression"),
            target,
            timeout_s=timeout_s,
            log_dir=ctx.log_dir,
            index=index,
        )
        if not rerun.runnable:
            return {"ran": False, "why": f"base rerun {rerun.headline()}"}
        failures = failing_tests(read_log(rerun), roots=(target,), single_file=files[0] if len(files) == 1 else None)
        log.info(
            "%s: base rerun of `%s` on %s: %d failing", ctx.bead.id, command, worktree.base_commit[:9], len(failures)
        )
        return {"ran": True, "files": files, "failures": sorted(failure_key(test) for test in failures)}
    finally:
        await asyncio.to_thread(git, "worktree", "remove", "--force", str(target))
        await asyncio.to_thread(git, "worktree", "prune")


def _log_index(ctx) -> int:
    return len(list(ctx.log_dir.glob("check-*.log"))) if ctx.log_dir.is_dir() else 0


# -- the check loop -------------------------------------------------------------


async def excuse_outside_failures(
    ctx, result: CheckResult, known: dict[str, str]
) -> tuple[CheckResult, dict[str, str]]:
    """`result` with `excused` filled in for its flaky and pre-existing
    failing tests, and the verdicts new to `known` (test key -> kind).
    Unchanged when `verification.rerun_tests` is unset or nothing qualifies."""
    template = ctx.recipe.verification.rerun_tests
    if not template or result.ok or not result.runnable:
        return result, {}
    current = failing_tests(read_log(result), roots=(ctx.worktree.path,))
    if not current or any(name == LOAD_FAILURE or not file for file, name in current):
        return result, {}
    if len({file for file, _ in current}) > MAX_RERUN_FILES:
        return result, {}
    keys = {failure_key(t) for t in current}
    excused = {key: PRE_EXISTING for key in keys if known.get(key) == PRE_EXISTING}
    changed = set(ctx.worktrees.changed_files(ctx.worktree))
    # Flaky: a test in a file this bead did not touch that passes when run again.
    outside = sorted(t for t in current if failure_key(t) not in excused and t[0] not in changed)
    found = {key: FLAKY for key in await _passing_on_rerun(ctx, template, outside)}
    # Pre-existing: still red here, and red the same way on the base commit.
    still_red = keys - set(excused) - set(found)
    found.update({key: PRE_EXISTING for key in await _red_on_base(ctx, template, still_red, result)})
    excused.update(found)
    if not excused:
        return result, {}
    return result.model_copy(update={"excused": excused, "all_excused": keys <= set(excused)}), found


async def _passing_on_rerun(ctx, template: str, tests: list[tuple[str | None, str]]) -> set[str]:
    if not tests:
        return set()
    files = sorted({file for file, _ in tests if file})
    rerun = await verify.run_check(
        CheckRequest(
            command=_rerun_command(template, files), purpose="isolated rerun of failing tests", kind="regression"
        ),
        ctx.worktree.path,
        timeout_s=ctx.recipe.verification.max_command_timeout_minutes * 60,
        log_dir=ctx.log_dir,
        index=_log_index(ctx),
    )
    if not rerun.runnable:
        return set()
    again = {failure_key(t) for t in failing_tests(read_log(rerun), roots=(ctx.worktree.path,))}
    return {failure_key(t) for t in tests} - again


async def _red_on_base(ctx, template: str, keys: set[str], result: CheckResult) -> set[str]:
    if not keys:
        return set()
    base = await rerun_on_base(
        ctx,
        template,
        sorted({key.split("::", 1)[0] for key in keys}),
        base_setup=ctx.recipe.verification.base_setup,
        timeout_s=ctx.recipe.verification.max_command_timeout_minutes * 60,
        purpose="check loop",
    )
    if not base.get("ran"):
        log.info("%s: base rerun for `%s` did not run: %s", ctx.bead.id, result.command, base.get("why"))
        return set()
    return keys & set(base.get("failures") or ())


VERIFIER_ADVICE = (
    "Alloy has already rerun each of these, files each as a separate bug, and does not count them "
    "against this task. Spend no checks investigating them; a suite run where only these fail is green "
    "for this task."
)


def excused_note(known: dict[str, str], base_commit: str, *, advice: str = "") -> str:
    """The prompt section naming the failures that are not this bead's; ""
    when there are none. `advice` replaces the implementer's closing
    instructions (the verifier gets `VERIFIER_ADVICE`)."""
    if not known:
        return ""
    lines = []
    for key, kind in sorted(known.items()):
        why = (
            f"fails the same way on the base commit {base_commit[:9]}"
            if kind == PRE_EXISTING
            else "failed once and passed when run again"
        )
        lines.append(f"- `{key}` -- {why}")
    advice = advice or (
        "Alloy has filed each as a separate bug. Leave these tests and the code they cover exactly as "
        "they are at the base commit: a change there is out of scope for this task even when it turns them "
        "green. If an earlier attempt changed them only to make them pass, revert that part."
    )
    return "## Failing tests that are not this task's\n" + "\n".join(lines) + "\n\n" + advice


def file_outside_bugs(ctx, found: dict[str, str], result: CheckResult) -> list[str]:
    """One alloy-bug bead per newly excused test, unless an open alloy-bug
    already names that test. Returns the ids filed."""
    if ctx.beads is None or not found:
        return []
    from alloy.beads import LABEL_BUG, META_DISCOVERED_IN_RUN

    try:
        open_titles = " ".join(
            str(row.get("title") or "") for row in ctx.beads.filed_bugs() if row.get("status") != "closed"
        )
    except Exception:
        log.warning("could not list filed bugs; skipping dedupe", exc_info=True)
        open_titles = ""
    filed: list[str] = []
    for key, kind in sorted(found.items()):
        if key in open_titles:
            continue
        title = f"{'Pre-existing failing' if kind == PRE_EXISTING else 'Flaky'} test {key}"
        what = (
            f"fails the same way on the base commit {ctx.worktree.base_commit[:9]}"
            if kind == PRE_EXISTING
            else "failed in a check and passed when run again on the same tree"
        )
        try:
            bug_id = ctx.beads.create_bug(
                title=title,
                description=(
                    f"`{key}` {what}. Seen by run {ctx.run_id} of {ctx.bead.id} in `{result.command}` "
                    f"(log {result.log_path or '(none)'}). Not caused by {ctx.bead.id}; filed so it is fixed "
                    f"once, on its own.\n\n{clip(result.output_tail, 1500)}"
                ),
                acceptance=f"`{key}` passes reliably; no assertion is weakened or skipped.",
                discovered_from=ctx.bead.id,
                priority=3,
                labels=[LABEL_BUG],
                metadata={META_DISCOVERED_IN_RUN: ctx.run_id},
                claim=False,
            )
        except Exception:
            log.warning("could not file a bug for %s", key, exc_info=True)
            continue
        filed.append(bug_id)
        open_titles += " " + title
    return filed


async def review_red_check(
    ctx, result: CheckResult, known: dict[str, str], iteration: int
) -> tuple[CheckResult, dict[str, str], list[str]]:
    """The check loop's one call: excuse what is not this bead's, file a bug
    per newly excused test, and say so. Returns the (maybe marked) result,
    the run's updated `known_red` and journal lines."""
    marked, found = await excuse_outside_failures(ctx, result, known)
    if not found:
        return marked, known, []
    from alloy.events import EVENT_FLAKY_CHECK

    filed = file_outside_bugs(ctx, found, marked)
    names = ", ".join(f"{key} ({kind})" for key, kind in sorted(found.items()))
    line = (
        f"iteration {iteration}: `{marked.command}` failed on tests that are not this task's: {clip(names, 600)}"
        + (f"; filed {', '.join(filed)}" if filed else "")
        + ("; the check counts as green" if marked.all_excused else "")
    )
    log.warning("%s: %s", ctx.bead.id, line)
    for key, kind in found.items():
        if kind == FLAKY:
            ctx.store.events.emit(
                EVENT_FLAKY_CHECK,
                bead=ctx.bead.id,
                run=ctx.run_id,
                reason=f"{key}: red in `{marked.command}`, green on an isolated rerun",
                command=marked.command,
                logs=[marked.log_path],
            )
    if ctx.beads is not None:
        try:
            ctx.beads.note(ctx.bead.id, f"alloy: {line}")
        except Exception:
            log.warning("could not note excused failures on %s", ctx.bead.id, exc_info=True)
    return marked, {**known, **found}, [line]
