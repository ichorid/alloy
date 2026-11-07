"""Cell values: turning input text into a value, and a value into text.

`fmt` is the one place a value becomes text: GET shows it and CSV export
writes it, so both stay consistent.
"""


class Err(str):
    """A spreadsheet error value such as `#DIV/0!`."""


DIV0 = Err("#DIV/0!")
VALUE = Err("#VALUE!")
NAME = Err("#NAME?")
ERROR = Err("#ERROR!")


def literal(text: str):
    """A number when the text looks like one, otherwise the text itself."""
    try:
        number = float(text)
    except ValueError:
        return text
    if number != number or number in (float("inf"), float("-inf")):
        return text
    return number


def fmt(value) -> str:
    """How a value is written: `3` not `3.0`, `0.5`, text as is, empty as ''."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)
