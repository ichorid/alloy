"""`python3 -m sheet`: read commands from stdin, answer on stdout."""

import sys

from sheet.csvio import export_values
from sheet.refs import parse_ref
from sheet.values import fmt
from sheet.workbook import Workbook


class CommandError(Exception):
    pass


def _cell(text: str) -> tuple[int, int]:
    ref = parse_ref(text)
    if ref is None:
        raise CommandError(f"not a cell: {text}")
    return ref


def run_command(book: Workbook, line: str) -> str | None:
    """Carry out one command line; the text to print, or None."""
    parts = line.strip().split(maxsplit=2)
    word, args = parts[0].upper(), parts[1:]
    if word == "SET" and args:
        book.set(*_cell(args[0]), args[1] if len(args) > 1 else "")
        return None
    if word == "GET" and len(args) == 1:
        return fmt(book.value(*_cell(args[0])))
    if word == "EXPORT" and args:
        export_values(book, " ".join(args))
        return None
    raise CommandError(f"cannot do: {line.strip()}")


def main() -> None:
    book = Workbook()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            out = run_command(book, line)
        except (CommandError, OSError) as exc:
            out = f"ERROR {exc}"
        if out is not None:
            print(out, flush=True)


if __name__ == "__main__":
    main()
