import subprocess
import sys


def run(script: str) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "sheet"], input=script, capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def test_unknown_command_reports_an_error_and_goes_on():
    out = run("FROB\nFROB\n")
    assert len(out) == 2 and all(line.startswith("ERROR") for line in out)
