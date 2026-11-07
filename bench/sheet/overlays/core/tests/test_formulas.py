from sheet.formulas import evaluate


def no_cells(col, row):
    return None


def test_precedence():
    assert evaluate("1+2*3", no_cells) == 7
    assert evaluate("(1+2)*3", no_cells) == 9
    assert evaluate("-2^2", no_cells) == 4  # unary minus binds tighter, like spreadsheets


def test_division_by_zero():
    assert evaluate("1/0", no_cells) == "#DIV/0!"


def test_references():
    cells = {(1, 1): 2.0}
    assert evaluate("A1*3", lambda c, r: cells.get((c, r))) == 6
