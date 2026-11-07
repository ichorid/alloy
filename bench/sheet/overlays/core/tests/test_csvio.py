import csv

from sheet.csvio import export_values
from sheet.workbook import Workbook


def rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def test_export_values(tmp_path):
    book = Workbook()
    book.set(1, 1, "1")
    book.set(2, 2, "=A1*2")
    export_values(book, tmp_path / "out.csv")
    assert rows(tmp_path / "out.csv") == [["1", ""], ["", "2"]]


def test_export_keeps_every_digit(tmp_path):
    book = Workbook()
    book.set(1, 1, "=1/3")
    book.set(2, 1, "=2/3*1000000")
    export_values(book, tmp_path / "out.csv")
    a, b = rows(tmp_path / "out.csv")[0]
    assert float(a) == 1 / 3
    assert float(b) == 2 / 3 * 1000000
