from sheet.workbook import Workbook


def test_dependents_follow_changes():
    book = Workbook()
    book.set(1, 1, "2")
    book.set(2, 1, "=A1*3")
    book.set(1, 1, "5")
    assert book.value(2, 1) == 15


def test_empty_counts_as_zero():
    book = Workbook()
    book.set(2, 1, "=A1+1")
    assert book.value(2, 1) == 1
