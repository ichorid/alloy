"""Formula evaluation: numbers, cell references, + - * / ^, unary minus, parentheses."""

import re

from sheet.refs import parse_ref
from sheet.values import DIV0, ERROR, NAME, VALUE, Err

TOKEN_RE = re.compile(r"\s*(?:(?P<num>\d+(?:\.\d+)?|\.\d+)|(?P<op>[-+*/^()])|(?P<word>[A-Za-z_][A-Za-z0-9_]*))")


def tokenize(src: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while src[pos:].strip():
        m = TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            raise SyntaxError(src[pos:])
        pos = m.end()
        tokens.append((m.lastgroup, m.group(m.lastgroup)))
    return tokens


class _Eval:
    def __init__(self, src: str, lookup):
        self.toks = tokenize(src)
        self.i = 0
        self.lookup = lookup

    def peek(self, *texts: str) -> bool:
        return self.i < len(self.toks) and self.toks[self.i][1] in texts

    def take(self) -> tuple[str, str]:
        if self.i >= len(self.toks):
            raise SyntaxError("unexpected end")
        self.i += 1
        return self.toks[self.i - 1]

    def run(self):
        value = self.additive()
        if self.i != len(self.toks):
            raise SyntaxError("trailing input")
        return value

    def additive(self):
        value = self.term()
        while self.peek("+", "-"):
            value = _arith(self.take()[1], value, self.term())
        return value

    def term(self):
        value = self.power()
        while self.peek("*", "/"):
            value = _arith(self.take()[1], value, self.power())
        return value

    def power(self):
        value = self.unary()
        while self.peek("^"):
            self.take()
            value = _arith("^", value, self.unary())
        return value

    def unary(self):
        if self.peek("-"):
            self.take()
            return _arith("-", 0.0, self.unary())
        if self.peek("+"):
            self.take()
            return self.unary()
        return self.primary()

    def primary(self):
        kind, text = self.take()
        if kind == "num":
            return float(text)
        if text == "(":
            value = self.additive()
            if self.take()[1] != ")":
                raise SyntaxError(")")
            return value
        if kind == "word":
            ref = parse_ref(text)
            return self.lookup(*ref) if ref else NAME
        raise SyntaxError(text)


def _number(value):
    if isinstance(value, Err):
        return value
    if value is None or value == "":
        return 0.0
    if isinstance(value, float):
        return value
    return VALUE


def _arith(op: str, left, right):
    a, b = _number(left), _number(right)
    for v in (a, b):
        if isinstance(v, Err):
            return v
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return DIV0 if b == 0 else a / b
    try:
        return float(a**b)
    except (OverflowError, ZeroDivisionError, TypeError):
        return VALUE


def evaluate(src: str, lookup):
    """Evaluate formula text (without the leading `=`). `lookup(col, row)`
    returns another cell's value. Errors come back as `Err` values."""
    try:
        return _Eval(src, lookup).run()
    except SyntaxError:
        return ERROR
