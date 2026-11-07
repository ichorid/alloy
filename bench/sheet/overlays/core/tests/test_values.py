from sheet.values import fmt, literal


def test_literal():
    assert literal("5") == 5.0
    assert literal("hello") == "hello"
    assert literal("nan") == "nan"


def test_fmt_numbers():
    assert fmt(3.0) == "3"
    assert fmt(0.5) == "0.5"
    assert fmt(None) == ""
    assert fmt("hi") == "hi"
