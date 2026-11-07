"""The hidden oracle for the sheet bench: protocol-level cases per area.

A case is a list of sessions (each a stdin script run as `python3 -m sheet` in
the toy checkout) and the expected stdout lines of all sessions, in order. A
correct implementation prints nothing for non-GET commands, so a command that
fails (an ERROR line) shifts the output and fails the case: that is intended.
`{tmp}` in a script is a per-case scratch directory.

Expectations are matchers, tolerant where docs/SPEC.md is vague:
  number      -> numerically equal (3 and 3.0 both pass; `format` is strict)
  Exact(s)    -> exactly s
  Code(c)     -> exactly the error code c
  ANY_ERROR   -> any spreadsheet error (starts with '#')
  ERR         -> an ERROR line
  Bool(b)     -> TRUE/FALSE, any case
  str         -> exactly the string
  Alt([...])  -> (whole case) any of several expected output lists
  CsvRows(p, rows) -> after the sessions, file p (relative to {tmp}) holds rows
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Exact:
    text: str


@dataclass(frozen=True)
class Code:
    code: str


@dataclass(frozen=True)
class Bool:
    value: bool


class _AnyError:
    def __repr__(self) -> str:
        return "ANY_ERROR"


class _Err:
    def __repr__(self) -> str:
        return "ERR"


ANY_ERROR = _AnyError()
ERR = _Err()


@dataclass(frozen=True)
class Alt:
    options: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class CsvRows:
    path: str
    rows: tuple[tuple[str, ...], ...]


@dataclass
class Case:
    area: str
    name: str
    sessions: list[str]
    expected: Any  # list of matchers, or Alt
    timeout: float = 10.0
    files: list[CsvRows] = field(default_factory=list)
    setup_files: dict[str, str] = field(default_factory=dict)


CASES: list[Case] = []


def case(area: str, name: str, script: str | list[str], expected: Any, **kw: Any) -> None:
    sessions = [script] if isinstance(script, str) else script
    CASES.append(Case(area, name, [s.strip("\n") + "\n" for s in sessions], expected, **kw))


def lines(*cmds: str) -> str:
    return "\n".join(cmds)


# --- cells ---------------------------------------------------------------
case("cells", "number", lines("SET A1 5", "GET A1"), [5])
case("cells", "text", lines("SET B2 hello", "GET B2"), ["hello"])
case("cells", "empty cell prints empty line", lines("GET C3"), [""])
case("cells", "overwrite", lines("SET A1 1", "SET A1 2", "GET A1"), [2])
case("cells", "decimal", lines("SET A1 2.5", "GET A1"), [2.5])
case("cells", "negative", lines("SET A1 -3", "GET A1"), [-3])
case("cells", "lowercase reference", lines("SET a1 4", "GET A1"), [4])
case("cells", "lowercase command", lines("set A1 4", "get a1"), [4])
case("cells", "text with spaces", lines("SET A1 hello world", "GET A1"), ["hello world"])
case("cells", "blank lines ignored", "\nSET A1 1\n\nGET A1\n", [1])

# --- format (strict number display) --------------------------------------
case("format", "integer", lines("SET A1 3", "GET A1"), [Exact("3")])
case("format", "integer from formula", lines("SET A1 =2*3", "GET A1"), [Exact("6")])
case("format", "half", lines("SET A1 =1/2", "GET A1"), [Exact("0.5")])
case("format", "two and a half", lines("SET A1 =10/4", "GET A1"), [Exact("2.5")])
case("format", "million", lines("SET A1 1000000", "GET A1"), [Exact("1000000")])

# --- arithmetic ----------------------------------------------------------
for name, formula, value in [
    ("precedence", "=1+2*3", 7),
    ("parentheses", "=(1+2)*3", 9),
    ("left to right minus", "=10-4-3", 3),
    ("power", "=2^3", 8),
    ("unary minus", "=-2+5", 3),
    ("left to right divide", "=8/4/2", 1),
    ("decimal operand", "=1.5*2", 3),
    ("spaces", "= 1 + 2", 3),
    ("times negative", "=2*-3", -6),
    ("nested parentheses", "=(((4)))", 4),
    ("division", "=10/4", 2.5),
    ("minus negative", "=5-(-2)", 7),
    ("float sum", "=0.1+0.2", 0.3),
    ("long expression", "=1+2+3+4+5+6+7+8+9+10", 55),
]:
    case("arith", name, lines(f"SET A1 {formula}", "GET A1"), [value])

# --- references and recalculation ----------------------------------------
case("refs", "simple reference", lines("SET A1 2", "SET B1 =A1*3", "GET B1"), [6])
case("refs", "recalc after change", lines("SET A1 2", "SET B1 =A1*3", "SET A1 5", "GET B1"), [15])
case("refs", "chain", lines("SET A1 1", "SET B1 =A1+1", "SET C1 =B1+1", "SET A1 10", "GET C1"), [12])
case("refs", "empty counts as zero", lines("SET B1 =A1+1", "GET B1"), [1])
case("refs", "two references", lines("SET A1 2", "SET B1 3", "SET C1 =A1*B1", "GET C1"), [6])
case("refs", "lowercase in formula", lines("SET A1 2", "SET B1 =a1+1", "GET B1"), [3])
case("refs", "double-letter column", lines("SET AA1 7", "SET A1 =AA1*2", "GET A1"), [14])
case("refs", "large row", lines("SET A100 4", "SET B1 =A100+1", "GET B1"), [5])
case("refs", "set before dependency", lines("SET C1 =A1+B1", "SET A1 1", "SET B1 2", "GET C1"), [3])
case("refs", "middle formula changes", lines("SET A1 1", "SET B1 =A1", "SET C1 =B1*10", "SET B1 =A1+1", "GET C1"), [20])
case("refs", "formula replaced by value", lines("SET A1 1", "SET B1 =A1", "SET C1 =B1+1", "SET B1 7", "GET C1"), [8])
case("refs", "several dependents", lines("SET A1 1", "SET B1 =A1", "SET B2 =A1*2", "SET A1 3", "GET B1", "GET B2"), [3, 6])

# --- ranges and everyday functions --------------------------------------
_nums = lines("SET A1 1", "SET A2 2", "SET A3 3")
case("ranges", "SUM", lines(_nums, "SET B1 =SUM(A1:A3)", "GET B1"), [6])
case("ranges", "AVERAGE", lines(_nums, "SET B1 =AVERAGE(A1:A3)", "GET B1"), [2])
case("ranges", "MIN", lines(_nums, "SET B1 =MIN(A1:A3)", "GET B1"), [1])
case("ranges", "MAX", lines(_nums, "SET B1 =MAX(A1:A3)", "GET B1"), [3])
case("ranges", "COUNT ignores text", lines(_nums, "SET A4 x", "SET B1 =COUNT(A1:A4)", "GET B1"), [3])
case("ranges", "SUM ignores text", lines(_nums, "SET A4 x", "SET B1 =SUM(A1:A4)", "GET B1"), [6])
case("ranges", "2D range", lines("SET A1 1", "SET B1 2", "SET A2 3", "SET B2 4", "SET C1 =SUM(A1:B2)", "GET C1"), [10])
case("ranges", "range plus number", lines(_nums, "SET B1 =SUM(A1:A2,5)", "GET B1"), [8])
case("ranges", "empty range is zero", lines("SET B1 =SUM(A1:A3)", "GET B1"), [0])
case("ranges", "lowercase function", lines(_nums, "SET B1 =sum(a1:a3)", "GET B1"), [6])
case("ranges", "function in expression", lines(_nums, "SET B1 =SUM(A1:A3)*2+1", "GET B1"), [13])
case("ranges", "reversed range", lines(_nums, "SET B1 =SUM(A3:A1)", "GET B1"), [6])
case("ranges", "range recalc", lines(_nums, "SET B1 =SUM(A1:A3)", "SET A2 20", "GET B1"), [24])
case("ranges", "nested functions", lines(_nums, "SET B1 =MAX(A1:A3)-MIN(A1:A3)", "GET B1"), [2])

# --- errors and cycles ---------------------------------------------------
case("errors", "divide by zero", lines("SET A1 =1/0", "GET A1"), [Code("#DIV/0!")])
case("errors", "divide by empty", lines("SET A1 =5/B1", "GET A1"), [Code("#DIV/0!")])
case("errors", "error propagates", lines("SET A1 =1/0", "SET B1 =A1+1", "GET B1"), [Code("#DIV/0!")])
case("errors", "unknown function", lines("SET A1 =FROB(1)", "GET A1"), [Code("#NAME?")])
case("errors", "text in arithmetic", lines("SET A1 abc", "SET B1 =A1+1", "GET B1"), [Code("#VALUE!")])
case("errors", "self reference", lines("SET A1 =A1+1", "GET A1"), [ANY_ERROR], timeout=5)
case("errors", "two-cell cycle", lines("SET A1 =B1", "SET B1 =A1", "GET A1", "GET B1"), [ANY_ERROR, ANY_ERROR], timeout=5)
case("errors", "three-cell cycle", lines("SET A1 =B1", "SET B1 =C1", "SET C1 =A1", "GET C1"), [ANY_ERROR], timeout=5)
case("errors", "cycle broken recovers", lines("SET A1 =B1", "SET B1 =A1", "SET B1 5", "GET A1"), [5], timeout=5)
case("errors", "cycle through range", lines("SET A1 =SUM(A1:A3)", "GET A1"), [ANY_ERROR], timeout=5)
case(
    "errors",
    "malformed formula",
    lines("SET A1 =1+", "GET A1"),
    Alt(((ANY_ERROR,), (ERR, ""), (ERR, ANY_ERROR))),
)
case("errors", "error in range", lines("SET A1 =1/0", "SET A2 1", "SET B1 =SUM(A1:A2)", "GET B1"), [Code("#DIV/0!")])
case("errors", "error fixed recovers", lines("SET A1 0", "SET B1 =1/A1", "SET A1 4", "GET B1"), [0.25])
case("errors", "dependent of cycle", lines("SET A1 =B1", "SET B1 =A1", "SET C1 =A1+1", "GET C1"), [ANY_ERROR], timeout=5)

# --- conditions and text -------------------------------------------------
case("logic", "IF true", lines('SET A1 =IF(1>0,"yes","no")', "GET A1"), ["yes"])
case("logic", "IF on cell", lines("SET A1 12", 'SET B1 =IF(A1>=10,"big","small")', "GET B1"), ["big"])
case("logic", "IF false", lines("SET A1 2", 'SET B1 =IF(A1>=10,"big","small")', "GET B1"), ["small"])
case("logic", "equals", lines("SET A1 =1=1", "GET A1"), [Bool(True)])
case("logic", "not equals", lines("SET A1 =2<>2", "GET A1"), [Bool(False)])
case("logic", "AND", lines("SET A1 =AND(1>0,2>3)", "GET A1"), [Bool(False)])
case("logic", "OR", lines("SET A1 =OR(1>0,2>3)", "GET A1"), [Bool(True)])
case("logic", "NOT", lines("SET A1 =NOT(1>0)", "GET A1"), [Bool(False)])
case("logic", "join text", lines('SET A1 ="a"&"b"', "GET A1"), ["ab"])
case("logic", "join cells", lines("SET A1 hello", "SET B1 world", 'SET C1 =A1&" "&B1', "GET C1"), ["hello world"])
case("logic", "LEN", lines('SET A1 =LEN("hello")', "GET A1"), [5])
case("logic", "UPPER", lines("SET A1 ab", "SET B1 =UPPER(A1)", "GET B1"), ["AB"])
case("logic", "LOWER", lines('SET A1 =LOWER("AbC")', "GET A1"), ["abc"])
case("logic", "IF numeric result", lines("SET A1 3", "SET B1 =IF(A1>2,A1*2,0)", "GET B1"), [6])

# --- copy ----------------------------------------------------------------
_col = lines("SET A1 1", "SET A2 2", "SET A3 3")
case("copy", "relative down", lines(_col, "SET B1 =A1*10", "COPY B1 B2", "GET B2"), [20])
case("copy", "relative right", lines("SET A1 1", "SET B1 2", "SET A2 =A1+1", "COPY A2 B2", "GET B2"), [3])
case("copy", "absolute", lines(_col, "SET B1 =$A$1*10", "COPY B1 B3", "GET B3"), [10])
case("copy", "row anchored", lines(_col, "SET B1 =A$1*10", "COPY B1 B3", "GET B3"), [10])
case("copy", "column anchored", lines("SET A1 1", "SET B1 5", "SET A2 =$A1", "COPY A2 B2", "GET B2"), [1])
case("copy", "plain value", lines("SET A1 7", "COPY A1 C5", "GET C5"), [7])
case("copy", "range shifts", lines(_col, "SET A4 4", "SET B1 =SUM(A1:A2)", "COPY B1 B3", "GET B3"), [7])
case("copy", "copy is independent", lines(_col, "SET B1 =A1", "COPY B1 B2", "SET B1 =A3", "GET B2"), [2])
case("copy", "overwrites target", lines(_col, "SET C1 99", "SET B1 =A1", "COPY B1 C1", "GET C1"), [1])
case("copy", "off the sheet", lines("SET A1 1", "SET B2 =A1", "COPY B2 B1", "GET B1"), [Code("#REF!")])

# --- insert/delete rows --------------------------------------------------
case("rows", "insert moves cells", lines("SET A1 5", "INSERTROW 1", "GET A2", "GET A1"), [5, ""])
case("rows", "insert keeps reference", lines("SET A1 5", "SET B5 =A1*2", "INSERTROW 1", "GET B6"), [10])
case("rows", "insert below keeps", lines("SET A1 5", "SET B1 =A1", "INSERTROW 3", "GET B1"), [5])
case("rows", "delete moves cells up", lines("SET A1 1", "SET A2 2", "DELETEROW 1", "GET A1"), [2])
case("rows", "delete referenced row", lines("SET A2 3", "SET B1 =A2", "DELETEROW 2", "GET B1"), [Code("#REF!")])
case("rows", "delete keeps reference", lines("SET A3 3", "SET B1 =A3", "DELETEROW 2", "GET B1"), [3])
case("rows", "insert inside range grows it", lines(_col, "SET B1 =SUM(A1:A3)", "INSERTROW 2", "SET A2 10", "GET B1"), [16])
case("rows", "delete inside range shrinks it", lines(_col, "SET B1 =SUM(A1:A3)", "DELETEROW 2", "GET B1"), [4])
case("rows", "formula moves with row", lines("SET A1 2", "SET A2 =A1*3", "INSERTROW 1", "GET A3"), [6])
case("rows", "deleted row is gone", lines("SET A1 1", "DELETEROW 1", "GET A1"), [""])

# --- undo ----------------------------------------------------------------
case("undo", "undo set", lines("SET A1 1", "SET A1 2", "UNDO", "GET A1"), [1])
case("undo", "undo to empty", lines("SET A1 1", "UNDO", "GET A1"), [""])
case("undo", "undo twice", lines("SET A1 1", "SET A1 2", "SET A1 3", "UNDO", "UNDO", "GET A1"), [1])
case("undo", "undo recalculates", lines("SET A1 1", "SET B1 =A1*2", "SET A1 5", "UNDO", "GET B1"), [2])
case("undo", "undo copy", lines("SET A1 1", "SET B1 =A1", "COPY B1 B2", "UNDO", "GET B2"), [""])
case("undo", "undo insert row", lines("SET A1 5", "INSERTROW 1", "UNDO", "GET A1"), [5])
case("undo", "undo delete row", lines("SET A1 5", "DELETEROW 1", "UNDO", "GET A1"), [5])
case("undo", "nothing to undo", lines("UNDO", "SET A1 1", "GET A1"), Alt(((1,), (ERR, 1))))

# --- save/load -----------------------------------------------------------
case("files", "values survive", [lines("SET A1 5", "SET B1 hi", "SAVE {tmp}/w"), lines("LOAD {tmp}/w", "GET A1", "GET B1")], [5, "hi"])
case("files", "formulas survive", [lines("SET A1 2", "SET B1 =A1*3", "SAVE {tmp}/w"), lines("LOAD {tmp}/w", "SET A1 4", "GET B1")], [12])
case("files", "load replaces", [lines("SET A1 1", "SAVE {tmp}/w"), lines("SET A2 9", "LOAD {tmp}/w", "GET A2")], [""])
case("files", "missing file", lines("LOAD {tmp}/nope", "SET A1 1", "GET A1"), [ERR, 1])
case("files", "save twice", [lines("SET A1 1", "SAVE {tmp}/w", "SET A1 2", "SAVE {tmp}/w"), lines("LOAD {tmp}/w", "GET A1")], [2])
case("files", "errors survive", [lines("SET A1 =1/0", "SAVE {tmp}/w"), lines("LOAD {tmp}/w", "GET A1")], [Code("#DIV/0!")])
case("files", "ranges survive", [lines("SET A1 1", "SET A2 2", "SET B1 =SUM(A1:A2)", "SAVE {tmp}/w"), lines("LOAD {tmp}/w", "SET A2 5", "GET B1")], [6])
case("files", "text with comma", [lines("SET A1 a, b", "SAVE {tmp}/w"), lines("LOAD {tmp}/w", "GET A1")], ["a, b"])

# --- CSV -----------------------------------------------------------------
case("csv", "export values", lines("SET A1 1", "SET B1 2", "SET A2 x", "EXPORT {tmp}/o.csv"), [], files=[CsvRows("o.csv", (("1", "2"), ("x",)))])
case("csv", "export formula values", lines("SET A1 2", "SET B1 =A1*2", "EXPORT {tmp}/o.csv"), [], files=[CsvRows("o.csv", (("2", "4"),))])
case("csv", "import values", lines("IMPORT {tmp}/in.csv", "GET A1", "GET B2"), [1, "y"], setup_files={"in.csv": "1,x\n2,y\n"})
case("csv", "imported numbers are numbers", lines("IMPORT {tmp}/in.csv", "SET C1 =A1+A2", "GET C1"), [3], setup_files={"in.csv": "1,x\n2,y\n"})
case("csv", "quoted comma", lines("IMPORT {tmp}/in.csv", "GET A1"), ["a, b"], setup_files={"in.csv": '"a, b",1\n'})
case("csv", "round trip", lines("SET A1 3", "SET A2 =A1+1", "EXPORT {tmp}/o.csv", "IMPORT {tmp}/o.csv", "GET A2"), [4])

# --- several sheets ------------------------------------------------------
case("sheets", "cross-sheet reference", lines("SHEET Data", "SET A1 5", "SHEET Main", "SET A1 =Data!A1*2", "GET A1"), [10])
case("sheets", "sheets are separate", lines("SHEET One", "SET A1 1", "SHEET Two", "SET A1 2", "SHEET One", "GET A1"), [1])
case("sheets", "cross-sheet recalc", lines("SHEET Data", "SET A1 5", "SHEET Main", "SET A1 =Data!A1+1", "SHEET Data", "SET A1 9", "SHEET Main", "GET A1"), [10])
case("sheets", "cross-sheet range", lines("SHEET Data", "SET A1 1", "SET A2 2", "SHEET Main", "SET A1 =SUM(Data!A1:A2)", "GET A1"), [3])
case("sheets", "missing sheet", lines("SHEET Main", "SET A1 =Nope!A1", "GET A1"), [ANY_ERROR])
case("sheets", "sheets survive save", [lines("SHEET Data", "SET A1 5", "SHEET Main", "SET A1 =Data!A1", "SAVE {tmp}/w"), lines("LOAD {tmp}/w", "SHEET Main", "GET A1", "SHEET Data", "GET A1")], [5, 5])
case("sheets", "lowercase sheet ref", lines("SHEET Data", "SET A1 5", "SHEET Main", "SET A1 =data!a1", "GET A1"), [5])
case("sheets", "default sheet works", lines("SET A1 4", "SHEET Other", "SET A1 1", "GET A1"), [1])

# --- named ranges --------------------------------------------------------
case("names", "sum over name", lines(_nums, "NAME data A1:A3", "SET B1 =SUM(data)", "GET B1"), [6])
case("names", "name in average", lines(_nums, "NAME data A1:A3", "SET B1 =AVERAGE(data)", "GET B1"), [2])
case("names", "name recalc", lines(_nums, "NAME data A1:A3", "SET B1 =SUM(data)", "SET A1 10", "GET B1"), [15])
case("names", "unknown name", lines("SET B1 =SUM(nothing)", "GET B1"), [Code("#NAME?")])
case("names", "case-insensitive name", lines(_nums, "NAME Data A1:A3", "SET B1 =SUM(DATA)", "GET B1"), [6])
case("names", "single-cell name", lines("SET A1 7", "NAME rate A1", "SET B1 =rate*2", "GET B1"), [14])
case("names", "redefine name", lines(_nums, "NAME data A1:A2", "SET B1 =SUM(data)", "NAME data A1:A3", "GET B1"), [6])

# --- performance ---------------------------------------------------------
_chain = "\n".join(["SET A1 1"] + [f"SET A{i} =A{i - 1}+1" for i in range(2, 10001)])
case("perf", "10k chain", lines(_chain, "GET A10000"), [10000], timeout=20)
case("perf", "10k chain recalc", lines(_chain, "SET A1 2", "GET A10000"), [10001], timeout=20)
_fan = "\n".join(["SET A1 1"] + [f"SET B{i} =A1*{i}" for i in range(1, 5001)])
case("perf", "5k fan-out recalc", lines(_fan, "SET A1 2", "GET B5000"), [10000], timeout=20)
_big = "\n".join(f"SET A{i} {i}" for i in range(1, 10001))
case("perf", "sum of 10k", lines(_big, "SET B1 =SUM(A1:A10000)", "GET B1"), [50005000], timeout=20)

# --- robustness ----------------------------------------------------------
case("robust", "garbage", lines("FROB", "GET A1"), [ERR, ""])
case("robust", "GET without cell", lines("GET", "SET A1 1", "GET A1"), [ERR, 1])
case("robust", "bad reference", lines("SET 11 5", "SET A1 1", "GET A1"), [ERR, 1])
case("robust", "row zero", lines("SET A0 5", "GET B1"), [ERR, ""])
case("robust", "unicode text", lines("SET A1 привет", "GET A1"), ["привет"])
case("robust", "long text", lines("SET A1 " + "x" * 5000, "GET A1"), ["x" * 5000])
case("robust", "keeps going after error", lines("SET A1 =1/0", "FROB", "SET B1 2", "GET B1"), [ERR, 2])
case("robust", "SET without value", lines("SET A1", "SET B1 3", "GET B1"), Alt(((ERR, 3), (3,))))
