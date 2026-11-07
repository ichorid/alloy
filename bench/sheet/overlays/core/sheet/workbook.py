"""The workbook: what each cell holds and what it shows."""

from sheet.formulas import evaluate
from sheet.values import ERROR, literal


class Workbook:
    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], str] = {}

    def set(self, col: int, row: int, raw: str) -> None:
        if raw == "":
            self.cells.pop((col, row), None)
        else:
            self.cells[(col, row)] = raw

    def raw(self, col: int, row: int) -> str | None:
        return self.cells.get((col, row))

    def value(self, col: int, row: int):
        """The cell's current value, computed from its inputs on every read
        (so dependents are always up to date)."""
        raw = self.cells.get((col, row))
        if raw is None:
            return None
        if raw.startswith("="):
            try:
                return evaluate(raw[1:], self.value)
            except RecursionError:
                return ERROR
        return literal(raw)

    def extent(self) -> tuple[int, int]:
        """(last column, last row) holding anything; (0, 0) when empty."""
        if not self.cells:
            return 0, 0
        return max(c for c, _ in self.cells), max(r for _, r in self.cells)
