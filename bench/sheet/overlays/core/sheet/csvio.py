"""CSV export: the sheet's values from A1 to its last used cell.

The export must be lossless: importing it again has to give back exactly the
same numbers, so every digit is kept.
"""

import csv

from sheet.values import fmt


def export_values(workbook, path: str) -> None:
    last_col, last_row = workbook.extent()
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in range(1, last_row + 1):
            writer.writerow([fmt(workbook.value(col, row)) for col in range(1, last_col + 1)])
