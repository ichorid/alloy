"""Cell references: `A1` <-> (column, row), both 1-based."""

import re

CELL_RE = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]*)$")


def col_num(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def col_name(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def parse_ref(text: str) -> tuple[int, int] | None:
    """(column, row) for a cell name like `B12` (any case); None if it is not one."""
    m = CELL_RE.match(text.strip())
    return (col_num(m.group(1)), int(m.group(2))) if m else None


def ref_name(col: int, row: int) -> str:
    return f"{col_name(col)}{row}"
