"""Reference implementation of the sheet bench, used only to validate the
oracle (bench/sheet/oracle). Never copied into a toy checkout."""

from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass

COLS_RE = r"[A-Za-z]{1,3}"
REF_RE = re.compile(
    r"(?:(?P<sheet>[A-Za-z_][A-Za-z0-9_]*)!)?(?P<cabs>\$?)(?P<col>[A-Za-z]{1,3})(?P<rabs>\$?)(?P<row>[0-9]+)(?![A-Za-z0-9_(!])"
)
CELL_RE = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]*)$")


class Err(str):
    pass


DIV0, VALUE, NAME, REF, CYCLE, ERROR = (Err(c) for c in ("#DIV/0!", "#VALUE!", "#NAME?", "#REF!", "#CYCLE!", "#ERROR!"))


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


def parse_cell(text: str) -> tuple[int, int] | None:
    m = CELL_RE.match(text.strip())
    return (col_num(m.group(1)), int(m.group(2))) if m else None


def fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return format(value, ".15g")
    return str(value)


def literal(text: str):
    try:
        number = float(text)
    except ValueError:
        return text
    return text if number != number or number in (float("inf"), float("-inf")) else number


# --- formula parsing -------------------------------------------------------
TOKEN_RE = re.compile(
    r"\s*(?:(?P<err>#[A-Z/0!?]+)|(?P<str>\"[^\"]*\")|(?P<num>\d+(?:\.\d+)?|\.\d+)|(?P<op><=|>=|<>|[-+*/^&=<>(),:])|(?P<word>[A-Za-z_$][A-Za-z0-9_.$!]*))"
)


@dataclass
class Tok:
    kind: str
    text: str


def tokenize(src: str) -> list[Tok]:
    tokens: list[Tok] = []
    pos = 0
    while pos < len(src):
        if src[pos:].strip() == "":
            break
        m = TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            raise SyntaxError(src[pos:])
        pos = m.end()
        kind = m.lastgroup
        tokens.append(Tok(kind, m.group(kind)))
    return tokens


class Parser:
    def __init__(self, src: str, sheet: str):
        self.toks = tokenize(src)
        self.i = 0
        self.sheet = sheet

    def peek(self, text: str | None = None) -> bool:
        if self.i >= len(self.toks):
            return False
        return text is None or self.toks[self.i].text == text

    def take(self) -> Tok:
        if self.i >= len(self.toks):
            raise SyntaxError("unexpected end")
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self):
        node = self.comparison()
        if self.i != len(self.toks):
            raise SyntaxError("trailing")
        return node

    def comparison(self):
        node = self.concat()
        while self.peek() and self.toks[self.i].text in ("=", "<>", "<", ">", "<=", ">="):
            op = self.take().text
            node = ("bin", op, node, self.concat())
        return node

    def concat(self):
        node = self.additive()
        while self.peek("&"):
            self.take()
            node = ("bin", "&", node, self.additive())
        return node

    def additive(self):
        node = self.term()
        while self.peek() and self.toks[self.i].text in ("+", "-"):
            op = self.take().text
            node = ("bin", op, node, self.term())
        return node

    def term(self):
        node = self.power()
        while self.peek() and self.toks[self.i].text in ("*", "/"):
            op = self.take().text
            node = ("bin", op, node, self.power())
        return node

    def power(self):
        node = self.unary()
        while self.peek("^"):
            self.take()
            node = ("bin", "^", node, self.unary())
        return node

    def unary(self):
        if self.peek("-"):
            self.take()
            return ("neg", self.unary())
        if self.peek("+"):
            self.take()
            return self.unary()
        return self.primary()

    def primary(self):
        tok = self.take()
        if tok.kind == "num":
            return ("lit", float(tok.text))
        if tok.kind == "err":
            return ("lit", Err(tok.text))
        if tok.kind == "str":
            return ("lit", tok.text[1:-1])
        if tok.text == "(":
            node = self.comparison()
            if self.take().text != ")":
                raise SyntaxError(")")
            return node
        if tok.kind == "word":
            if self.peek("("):
                self.take()
                args = []
                if not self.peek(")"):
                    args.append(self.comparison())
                    while self.peek(","):
                        self.take()
                        args.append(self.comparison())
                if self.take().text != ")":
                    raise SyntaxError(")")
                return ("call", tok.text.upper(), args)
            ref = self.ref(tok.text)
            if ref is not None:
                if self.peek(":"):
                    self.take()
                    end = self.ref(self.take().text, default_sheet=ref[0])
                    if end is None:
                        raise SyntaxError("range")
                    return ("range", ref[0], min(ref[1], end[1]), min(ref[2], end[2]), max(ref[1], end[1]), max(ref[2], end[2]))
                return ("ref", *ref)
            if tok.text.upper() in ("TRUE", "FALSE"):
                return ("lit", tok.text.upper() == "TRUE")
            if tok.text.upper() == "#REF!":
                return ("lit", REF)
            return ("name", tok.text.lower())
        if tok.text == "#":
            raise SyntaxError("#")
        raise SyntaxError(tok.text)

    def ref(self, text: str, default_sheet: str | None = None):
        m = REF_RE.fullmatch(text)
        if not m:
            return None
        sheet = (m.group("sheet") or default_sheet or self.sheet).lower()
        return (sheet, col_num(m.group("col")), int(m.group("row")))


# --- reference rewriting (copy, insert, delete) ----------------------------
WORD_REF_RE = re.compile(
    r"(?P<pre>(?:[A-Za-z_][A-Za-z0-9_]*!)?)(?P<cabs>\$?)(?P<col>[A-Za-z]{1,3})(?P<rabs>\$?)(?P<row>[0-9]+)(?![A-Za-z0-9_(!])"
)


def rewrite(formula: str, own_sheet: str, fn) -> str:
    """Apply fn(sheet, cabs, col, rabs, row, is_range_end, partner) to each ref outside strings."""
    parts = formula.split('"')
    for i in range(0, len(parts), 2):
        parts[i] = _rewrite_part(parts[i], own_sheet, fn)
    return '"'.join(parts)


def _rewrite_part(text: str, own_sheet: str, fn) -> str:
    out, last = [], 0
    for m in WORD_REF_RE.finditer(text):
        start = m.start()
        if start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_."):
            continue
        sheet = m.group("pre")[:-1].lower() if m.group("pre") else own_sheet
        new = fn(sheet, m.group("cabs"), col_num(m.group("col")), m.group("rabs"), int(m.group("row")))
        if new is None:
            continue
        out.append(text[last:start])
        out.append(m.group("pre") + new if new != "#REF!" else "#REF!")
        last = m.end()
    out.append(text[last:])
    return "".join(out)


class Workbook:
    def __init__(self):
        self.sheets: dict[str, dict[tuple[int, int], str]] = {"sheet1": {}}
        self.display: dict[str, str] = {"sheet1": "Sheet1"}
        self.names: dict[str, tuple[str, int, int, int, int]] = {}
        self.current = "sheet1"
        self.undo: list = []
        self.cache: dict = {}
        self.parsed: dict = {}

    # state helpers
    def snapshot(self):
        return (
            {k: dict(v) for k, v in self.sheets.items()},
            dict(self.display),
            dict(self.names),
            self.current,
        )

    def restore(self, snap):
        sheets, display, names, current = snap
        self.sheets = {k: dict(v) for k, v in sheets.items()}
        self.display, self.names, self.current = dict(display), dict(names), current
        self.changed()

    def changed(self):
        self.cache.clear()
        self.parsed.clear()

    # evaluation
    def ast(self, key):
        if key not in self.parsed:
            raw = self.sheets[key[0]].get((key[1], key[2]))
            try:
                self.parsed[key] = Parser(raw[1:], key[0]).parse()
            except (SyntaxError, IndexError):
                self.parsed[key] = ("lit", ERROR)
        return self.parsed[key]

    def deps(self, node, out: list):
        kind = node[0]
        if kind == "ref":
            out.append(node[1:])
        elif kind == "range":
            _, sheet, c1, r1, c2, r2 = node
            cells = self.sheets.get(sheet, {})
            for (c, r) in cells:
                if c1 <= c <= c2 and r1 <= r <= r2:
                    out.append((sheet, c, r))
        elif kind == "name":
            target = self.names.get(node[1])
            if target:
                self.deps(("range", *target), out)
        elif kind == "bin":
            self.deps(node[2], out)
            self.deps(node[3], out)
        elif kind == "neg":
            self.deps(node[1], out)
        elif kind == "call":
            for arg in node[2]:
                self.deps(arg, out)
        return out

    def is_formula(self, key) -> bool:
        raw = self.sheets.get(key[0], {}).get((key[1], key[2]))
        return isinstance(raw, str) and raw.startswith("=")

    def value(self, key):
        if key in self.cache:
            return self.cache[key]
        stack, state = [key], {}
        while stack:
            k = stack[-1]
            if k in self.cache:
                stack.pop()
                continue
            if k[0] not in self.sheets:
                self.cache[k] = REF
                stack.pop()
                continue
            if not self.is_formula(k):
                raw = self.sheets[k[0]].get((k[1], k[2]))
                self.cache[k] = None if raw is None else literal(raw)
                stack.pop()
                continue
            deps = self.deps(self.ast(k), [])
            if state.get(k) is None:
                state[k] = "open"
                if any(state.get(d) == "open" and d not in self.cache for d in deps):
                    self.cache[k] = CYCLE
                    stack.pop()
                    continue
                pending = [d for d in deps if d not in self.cache]
                if pending:
                    stack.extend(pending)
                    continue
            missing = [d for d in deps if d not in self.cache]
            if missing:
                # a dependency on the open path: a cycle
                self.cache[k] = CYCLE
            else:
                self.cache[k] = self.evaluate(self.ast(k), k[0])
            state[k] = "done"
            stack.pop()
        return self.cache[key]

    def evaluate(self, node, sheet):
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "ref":
            if node[1] not in self.sheets:
                return REF
            return self.cache.get(node[1:], None)
        if kind == "range":
            return ("rangevals", self.range_values(node))
        if kind == "name":
            target = self.names.get(node[1])
            if target is None:
                return NAME
            _, c1, r1, c2, r2 = target
            if (c1, r1) == (c2, r2):
                return self.cache.get((target[0], c1, r1))
            return ("rangevals", self.range_values(("range", *target)))
        if kind == "neg":
            v = num(self.evaluate(node[1], sheet))
            return v if isinstance(v, Err) else -v
        if kind == "bin":
            return binop(node[1], self.evaluate(node[2], sheet), self.evaluate(node[3], sheet))
        if kind == "call":
            return call(node[1], [self.evaluate(a, sheet) for a in node[2]])
        return ERROR

    def range_values(self, node):
        _, sheet, c1, r1, c2, r2 = node
        if sheet not in self.sheets:
            return [REF]
        cells = self.sheets[sheet]
        return [self.cache.get((sheet, c, r)) for (c, r) in sorted(cells) if c1 <= c <= c2 and r1 <= r <= r2]


def scalar(v):
    if isinstance(v, tuple) and v and v[0] == "rangevals":
        return VALUE
    return v


def num(v):
    v = scalar(v)
    if isinstance(v, Err):
        return v
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, float):
        return v
    return VALUE


def text(v):
    v = scalar(v)
    return v if isinstance(v, Err) else fmt(v)


def binop(op, a, b):
    if op == "&":
        a, b = text(a), text(b)
        for x in (a, b):
            if isinstance(x, Err):
                return x
        return a + b
    if op in ("=", "<>", "<", ">", "<=", ">="):
        a, b = scalar(a), scalar(b)
        for x in (a, b):
            if isinstance(x, Err):
                return x
        a = 0.0 if a is None else a
        b = 0.0 if b is None else b
        if isinstance(a, str) and isinstance(b, str):
            a, b = a.lower(), b.lower()
        elif isinstance(a, str) or isinstance(b, str):
            return op == "<>"
        return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
    a, b = num(a), num(b)
    for x in (a, b):
        if isinstance(x, Err):
            return x
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return DIV0 if b == 0 else a / b
    if op == "^":
        try:
            return float(a**b)
        except (OverflowError, ZeroDivisionError):
            return VALUE
    return ERROR


def flat(args):
    for a in args:
        if isinstance(a, tuple) and a and a[0] == "rangevals":
            for v in a[1]:
                yield v, True
        else:
            yield a, False


def numbers(args):
    out = []
    for v, from_range in flat(args):
        if isinstance(v, Err):
            return v
        if isinstance(v, float):
            out.append(v)
        elif not from_range and isinstance(v, bool):
            out.append(1.0 if v else 0.0)
    return out


def call(name, args):
    if name in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT"):
        if name == "COUNT":
            return float(sum(1 for v, _ in flat(args) if isinstance(v, float)))
        values = numbers(args)
        if isinstance(values, Err):
            return values
        if name == "SUM":
            return float(sum(values))
        if not values:
            return DIV0 if name == "AVERAGE" else 0.0
        return {"AVERAGE": sum(values) / len(values), "MIN": min(values), "MAX": max(values)}[name]
    if name == "IF":
        if len(args) not in (2, 3):
            return VALUE
        cond = scalar(args[0])
        if isinstance(cond, Err):
            return cond
        truth = bool(num(cond)) if not isinstance(cond, str) else bool(cond)
        if truth:
            return scalar(args[1])
        return scalar(args[2]) if len(args) == 3 else False
    if name in ("AND", "OR"):
        values = []
        for v, _ in flat(args):
            if isinstance(v, Err):
                return v
            if v is None or isinstance(v, str):
                continue
            values.append(bool(v))
        return all(values) if name == "AND" else any(values)
    if name == "NOT":
        v = num(args[0]) if args else VALUE
        return v if isinstance(v, Err) else not bool(v)
    if name in ("LEN", "UPPER", "LOWER"):
        v = text(args[0]) if args else VALUE
        if isinstance(v, Err):
            return v
        return float(len(v)) if name == "LEN" else (v.upper() if name == "UPPER" else v.lower())
    if name in ("CONCAT", "CONCATENATE"):
        parts = []
        for v, _ in flat(args):
            t = text(v)
            if isinstance(t, Err):
                return t
            parts.append(t)
        return "".join(parts)
    return NAME


class Session:
    def __init__(self):
        self.wb = Workbook()

    def key(self, ref: str):
        cell = parse_cell(ref)
        if cell is None:
            raise ValueError(f"bad cell {ref!r}")
        return (self.wb.current, *cell)

    def handle(self, line: str) -> str | None:
        parts = line.strip().split(None, 2)
        if not parts:
            return None
        cmd = parts[0].upper()
        args = parts[1:]
        try:
            handler = getattr(self, f"cmd_{cmd.lower()}", None)
            if handler is None:
                raise ValueError(f"unknown command {cmd}")
            return handler(args)
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
            return f"ERROR {exc}"

    def remember(self):
        self.wb.undo.append(self.wb.snapshot())

    def cmd_set(self, args):
        if len(args) < 2:
            raise ValueError("SET needs a cell and a value")
        key = self.key(args[0])
        self.wb.undo.append(("cell", key, self.wb.sheets[key[0]].get(key[1:])))
        self.wb.sheets[key[0]][key[1:]] = args[1]
        self.wb.changed()

    def cmd_get(self, args):
        if len(args) != 1:
            raise ValueError("GET needs a cell")
        value = self.wb.value(self.key(args[0]))
        return fmt(scalar(value))

    def cmd_copy(self, args):
        if len(args) != 2:
            raise ValueError("COPY needs two cells")
        src, dst = self.key(args[0]), self.key(args[1].split()[0])
        raw = self.wb.sheets[src[0]].get(src[1:])
        self.remember()
        dc, dr = dst[1] - src[1], dst[2] - src[2]
        if raw is None:
            self.wb.sheets[dst[0]].pop(dst[1:], None)
        elif raw.startswith("="):

            def shift(sheet, cabs, col, rabs, row):
                c = col if cabs else col + dc
                r = row if rabs else row + dr
                if c < 1 or r < 1:
                    return "#REF!"
                return f"{cabs}{col_name(c)}{rabs}{r}"

            self.wb.sheets[dst[0]][dst[1:]] = rewrite(raw, src[0], shift)
        else:
            self.wb.sheets[dst[0]][dst[1:]] = raw
        self.wb.changed()

    def _rows(self, n: int, delta: int):
        sheet = self.wb.current
        self.remember()
        cells = self.wb.sheets[sheet]
        moved = {}
        for (c, r), raw in cells.items():
            if delta < 0 and r == n:
                continue
            moved[(c, r + delta if r >= n and not (delta < 0 and r == n) else r)] = raw
        if delta < 0:
            moved = {(c, (r - 1 if r > n else r)): raw for (c, r), raw in cells.items() if r != n}
        self.wb.sheets[sheet] = moved

        def fix(ref_sheet, cabs, col, rabs, row):
            if ref_sheet != sheet:
                return None
            if delta > 0:
                return f"{cabs}{col_name(col)}{rabs}{row + 1 if row >= n else row}"
            if row == n:
                return "#REF!"
            return f"{cabs}{col_name(col)}{rabs}{row - 1 if row > n else row}"

        for sname, scells in self.wb.sheets.items():
            for pos, raw in list(scells.items()):
                if raw.startswith("="):
                    scells[pos] = self._fix_formula(raw, sname, sheet, n, delta, fix)
        for name, (s, c1, r1, c2, r2) in list(self.wb.names.items()):
            if s == sheet:
                r1, r2 = _shift_span(r1, r2, n, delta)
                if r1 is None:
                    del self.wb.names[name]
                else:
                    self.wb.names[name] = (s, c1, r1, c2, r2)
        self.wb.changed()

    def _fix_formula(self, raw, own_sheet, sheet, n, delta, fix):
        # ranges first: rewrite A1:B3 spans as a whole, then single refs
        def span(m):
            pre = m.group("pre") or ""
            s = pre[:-1].lower() if pre else own_sheet
            if s != sheet:
                return m.group(0)
            r1, r2 = _shift_span(int(m.group("r1")), int(m.group("r2")), n, delta)
            if r1 is None:
                return "#REF!"
            return f"{pre}{m.group('c1abs')}{m.group('c1')}{m.group('r1abs')}{r1}:{m.group('c2abs')}{m.group('c2')}{m.group('r2abs')}{r2}"

        parts = raw.split('"')
        for i in range(0, len(parts), 2):
            parts[i] = _fix_outside_ranges(RANGE_RE.sub(span, parts[i]), own_sheet, fix)
        return '"'.join(parts)

    def cmd_insertrow(self, args):
        self._rows(_row_arg(args), +1)

    def cmd_deleterow(self, args):
        self._rows(_row_arg(args), -1)

    def cmd_undo(self, args):
        if not self.wb.undo:
            return None
        item = self.wb.undo.pop()
        if isinstance(item, tuple) and item and item[0] == "cell":
            _, key, old = item
            if old is None:
                self.wb.sheets[key[0]].pop(key[1:], None)
            else:
                self.wb.sheets[key[0]][key[1:]] = old
            self.wb.changed()
        else:
            self.wb.restore(item)

    def cmd_save(self, args):
        if len(args) < 1:
            raise ValueError("SAVE needs a path")
        path = " ".join(args)
        data = {
            "sheets": {
                self.wb.display[k]: {f"{col_name(c)}{r}": raw for (c, r), raw in cells.items()}
                for k, cells in self.wb.sheets.items()
            },
            "names": {k: list(v) for k, v in self.wb.names.items()},
            "current": self.wb.display[self.wb.current],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def cmd_load(self, args):
        path = " ".join(args)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.remember()
        wb = self.wb
        wb.sheets, wb.display = {}, {}
        for name, cells in data["sheets"].items():
            wb.sheets[name.lower()] = {parse_cell(ref): raw for ref, raw in cells.items()}
            wb.display[name.lower()] = name
        wb.names = {k: tuple(v) for k, v in data.get("names", {}).items()}
        wb.current = data.get("current", next(iter(wb.display.values()))).lower()
        wb.changed()

    def cmd_export(self, args):
        path = " ".join(args)
        sheet = self.wb.current
        cells = self.wb.sheets[sheet]
        rows = max((r for _, r in cells), default=0)
        cols = max((c for c, _ in cells), default=0)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for r in range(1, rows + 1):
                writer.writerow([fmt(scalar(self.wb.value((sheet, c, r)))) for c in range(1, cols + 1)])

    def cmd_import(self, args):
        path = " ".join(args)
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.remember()
        cells = {}
        for r, row in enumerate(rows, start=1):
            for c, value in enumerate(row, start=1):
                if value != "":
                    cells[(c, r)] = value
        self.wb.sheets[self.wb.current] = cells
        self.wb.changed()

    def cmd_sheet(self, args):
        if len(args) != 1:
            raise ValueError("SHEET needs a name")
        key = args[0].lower()
        if key not in self.wb.sheets:
            self.wb.sheets[key] = {}
            self.wb.display[key] = args[0]
        self.wb.current = key
        self.wb.changed()

    def cmd_name(self, args):
        if len(args) != 2:
            raise ValueError("NAME needs a name and a range")
        target = args[1]
        sheet = self.wb.current
        if "!" in target:
            sheet, target = target.split("!", 1)
            sheet = sheet.lower()
        a, _, b = target.partition(":")
        start, end = parse_cell(a), parse_cell(b or a)
        if start is None or end is None:
            raise ValueError("bad range")
        self.remember()
        self.wb.names[args[0].lower()] = (
            sheet,
            min(start[0], end[0]),
            min(start[1], end[1]),
            max(start[0], end[0]),
            max(start[1], end[1]),
        )
        self.wb.changed()


RANGE_RE = re.compile(
    r"(?P<pre>(?:[A-Za-z_][A-Za-z0-9_]*!)?)(?P<c1abs>\$?)(?P<c1>[A-Za-z]{1,3})(?P<r1abs>\$?)(?P<r1>[0-9]+)"
    r":(?P<c2abs>\$?)(?P<c2>[A-Za-z]{1,3})(?P<r2abs>\$?)(?P<r2>[0-9]+)"
)


def _fix_outside_ranges(text: str, own_sheet: str, fix) -> str:
    out, last = [], 0
    for m in RANGE_RE.finditer(text):
        out.append(_rewrite_part(text[last : m.start()], own_sheet, fix))
        out.append(m.group(0))
        last = m.end()
    out.append(_rewrite_part(text[last:], own_sheet, fix))
    return "".join(out)


def _shift_span(r1: int, r2: int, n: int, delta: int):
    if delta > 0:
        return (r1 + 1 if r1 >= n else r1), (r2 + 1 if r2 >= n else r2)
    if r1 == n and r2 == n:
        return None, None
    return (r1 - 1 if r1 > n else r1), (r2 - 1 if r2 >= n and r2 > r1 or r2 > n else r2)


def _row_arg(args) -> int:
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) < 1:
        raise ValueError("needs a row number")
    return int(args[0])


def main() -> None:
    sys.setrecursionlimit(10000)
    session = Session()
    for line in sys.stdin:
        out = session.handle(line.rstrip("\n"))
        if out is not None:
            print(out)


if __name__ == "__main__":
    main()
