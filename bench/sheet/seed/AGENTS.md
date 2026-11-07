# sheet

A command-line spreadsheet engine. What it must do: `docs/SPEC.md`.

- Python 3.11+, standard library only. Run it with `python3 -m sheet`.
- Tests: `python3 -m pytest -q` (all) or `python3 -m pytest -q tests/test_<module>.py`.
- `python3 scripts/affected.py` lists the tests for the files you changed;
  `--run` runs them.
- Code lives in the `sheet/` package, one module per concern; tests in
  `tests/test_<module>.py`.
