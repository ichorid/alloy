"""Score a sheet checkout against the hidden oracle.

    python3 -I bench/sheet/oracle/oracle.py --toy DIR [--json] [--area A] [--verbose]

Runs every case in cases.py as `python3 -m sheet` sessions with cwd=DIR and
prints per-area pass counts. The score is the mean of the per-area pass
fractions (every area weighs the same), plus the raw fraction of cases.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import ANY_ERROR, CASES, ERR, Alt, Bool, Case, Code, CsvRows, Exact  # noqa: E402


def _match(expected, got: str) -> bool:
    got = got.rstrip("\r")
    if expected is ANY_ERROR:
        return got.strip().startswith("#")
    if expected is ERR:
        return got.startswith("ERROR")
    if isinstance(expected, Exact):
        return got == expected.text
    if isinstance(expected, Code):
        return got.strip() == expected.code
    if isinstance(expected, Bool):
        return got.strip().upper() == ("TRUE" if expected.value else "FALSE")
    if isinstance(expected, (int, float)):
        try:
            value = float(got.strip())
        except ValueError:
            return False
        return math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-9)
    return got == expected


def _lines_match(expected: list, got: list[str]) -> bool:
    return len(expected) == len(got) and all(_match(e, g) for e, g in zip(expected, got))


def _csv_ok(spec: CsvRows, tmp: Path) -> bool:
    path = tmp / spec.path
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [tuple(_trim(row)) for row in csv.reader(handle)]
    while rows and not rows[-1]:
        rows.pop()
    want = [tuple(row) for row in spec.rows]
    if len(rows) != len(want):
        return False
    return all(
        len(r) == len(w) and all(_match(_as_matcher(wc), rc) for wc, rc in zip(w, r)) for r, w in zip(rows, want)
    )


def _as_matcher(cell: str):
    try:
        return float(cell)
    except ValueError:
        return cell


def _trim(row: list[str]) -> list[str]:
    while row and row[-1] == "":
        row = row[:-1]
    return row


def run_case(case: Case, toy: Path) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="sheet-oracle-") as tmp_name:
        tmp = Path(tmp_name)
        for name, content in case.setup_files.items():
            (tmp / name).write_text(content, encoding="utf-8")
        got: list[str] = []
        detail = ""
        for script in case.sessions:
            try:
                proc = subprocess.run(
                    [sys.executable if Path(sys.executable).name.startswith("python3") else "python3", "-m", "sheet"],
                    input=script.replace("{tmp}", str(tmp)),
                    capture_output=True,
                    text=True,
                    cwd=toy,
                    timeout=case.timeout,
                )
            except subprocess.TimeoutExpired:
                return _result(case, False, started, "timeout")
            if proc.returncode != 0:
                detail = f"exit {proc.returncode}: {proc.stderr.strip()[-300:]}"
            got.extend(proc.stdout.splitlines())
        if isinstance(case.expected, Alt):
            ok = any(_lines_match(list(option), got) for option in case.expected.options)
        else:
            ok = _lines_match(list(case.expected), got)
        if ok and case.files:
            ok = all(_csv_ok(spec, tmp) for spec in case.files)
            if not ok:
                detail = "csv file mismatch"
        if not ok and not detail:
            detail = f"got {got[:6]!r}"
        return _result(case, ok, started, detail)


def _result(case: Case, ok: bool, started: float, detail: str) -> dict:
    return {
        "area": case.area,
        "name": case.name,
        "ok": ok,
        "seconds": round(time.monotonic() - started, 2),
        "detail": "" if ok else detail,
    }


def score(toy: Path, area: str | None = None, workers: int = 8) -> dict:
    cases = [c for c in CASES if area is None or c.area == area]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda c: run_case(c, toy), cases))
    areas: dict[str, dict] = {}
    for result in results:
        entry = areas.setdefault(result["area"], {"passed": 0, "total": 0})
        entry["total"] += 1
        entry["passed"] += int(result["ok"])
    for entry in areas.values():
        entry["fraction"] = round(entry["passed"] / entry["total"], 3)
    passed = sum(int(r["ok"]) for r in results)
    return {
        "score": round(sum(e["fraction"] for e in areas.values()) / len(areas), 3) if areas else 0.0,
        "passed": passed,
        "total": len(results),
        "areas": areas,
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--toy", required=True, type=Path)
    parser.add_argument("--area")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    report = score(args.toy.resolve(), args.area)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for name, entry in report["areas"].items():
        print(f"{name:8} {entry['passed']:3}/{entry['total']:<3} {entry['fraction']:.2f}")
    print(f"score {report['score']:.3f}  ({report['passed']}/{report['total']} cases)")
    if args.verbose:
        for result in report["cases"]:
            if not result["ok"]:
                print(f"  FAIL {result['area']}/{result['name']}: {result['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
