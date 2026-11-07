"""List (or run) the tests for the files changed since HEAD.

A changed `sheet/<name>.py` selects `tests/test_<name>.py`; a changed test file
selects itself. Untracked files count as changed. `--run` runs the selection
with pytest (and succeeds when nothing is selected).
"""

import subprocess
import sys
from pathlib import Path


def changed() -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]

    return sorted(set(git("diff", "--name-only", "HEAD")) | set(git("ls-files", "--others", "--exclude-standard")))


def selected(paths: list[str]) -> list[str]:
    tests = set()
    for path in paths:
        p = Path(path)
        if p.parts[:1] == ("tests",) and p.name.startswith("test_") and p.suffix == ".py":
            tests.add(path)
        elif p.parts[:1] == ("sheet",) and p.suffix == ".py":
            candidate = Path("tests") / f"test_{p.stem}.py"
            if candidate.exists():
                tests.add(str(candidate))
    return sorted(t for t in tests if Path(t).exists())


def main() -> int:
    tests = selected(changed())
    if "--run" not in sys.argv:
        print("\n".join(tests))
        return 0
    if not tests:
        print("no affected tests")
        return 0
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *tests]).returncode


if __name__ == "__main__":
    sys.exit(main())
