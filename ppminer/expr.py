"""FASTEXPR parser + Power Pool shape analysis.

Parses BRAIN fast expressions (function calls, infix arithmetic/comparison,
ternary, unary minus/not, keyword args, `name = expr;` statements) into a small
AST so we can count operators and data fields the way the Power Pool rules do:

  * every operator occurrence counts, repeats included
  * ts_backfill / group_backfill are free
  * grouping fields (country, industry, ...) are not data fields
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FREE_OPS = {"ts_backfill", "group_backfill"}
GROUPING_FIELDS = {"country", "industry", "subindustry", "currency", "market", "sector", "exchange"}

INFIX_NAME = {
    "+": "add", "-": "subtract", "*": "multiply", "/": "divide", "^": "power",
    "<": "less", ">": "greater", "<=": "less_equal", ">=": "greater_equal",
    "==": "equal", "!=": "not_equal", "&&": "and", "||": "or",
}
PRECEDENCE = [("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/"), ("^",)]

_TOKEN = re.compile(r"""
    (?P<num>\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+(?:[eE][-+]?\d+)?)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<op><=|>=|==|!=|&&|\|\||[-+*/^<>?:(),;=!])
  | (?P<ws>\s+)
""", re.VERBOSE)


class ParseError(ValueError):
    pass


@dataclass
class Node:
    kind: str                      # call | num | str | name | kw
    value: str = ""
    args: list["Node"] = field(default_factory=list)
    infix: str = ""                # original infix symbol, if any


def _tokenize(s: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m:
            raise ParseError(f"bad character {s[pos]!r} at {pos}")
        pos = m.end()
        if m.lastgroup != "ws":
            out.append((m.lastgroup, m.group()))
    return out


class _Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else ("eof", "")

    def take(self, val=None):
        tok = self.peek()
        if val is not None and tok[1] != val:
            raise ParseError(f"expected {val!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def program(self) -> list[tuple[str | None, Node]]:
        stmts = []
        while self.peek()[0] != "eof":
            if self.peek()[1] == ";":
                self.take(); continue
            target = None
            if self.peek()[0] == "name" and self.peek(1)[1] == "=":
                target = self.take()[1]; self.take("=")
            stmts.append((target, self.ternary()))
            if self.peek()[1] not in (";", ""):
                raise ParseError(f"unexpected {self.peek()[1]!r}")
        if not stmts:
            raise ParseError("empty expression")
        return stmts

    def ternary(self) -> Node:
        cond = self.binary(0)
        if self.peek()[1] == "?":
            self.take()
            a = self.ternary(); self.take(":"); b = self.ternary()
            return Node("call", "if_else", [cond, a, b], infix="?:")
        return cond

    def binary(self, level: int) -> Node:
        if level == len(PRECEDENCE):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek()[0] == "op" and self.peek()[1] in PRECEDENCE[level]:
            op = self.take()[1]
            right = self.binary(level + 1)
            left = Node("call", INFIX_NAME[op], [left, right], infix=op)
        return left

    def unary(self) -> Node:
        tok = self.peek()
        if tok[1] in ("-", "+", "!"):
            self.take()
            inner = self.unary()
            if tok[1] == "+":
                return inner
            if tok[1] == "-" and inner.kind == "num":
                return Node("num", "-" + inner.value)
            return Node("call", "reverse" if tok[1] == "-" else "not", [inner], infix=tok[1])
        return self.atom()

    def atom(self) -> Node:
        kind, val = self.take()
        if kind == "num":
            return Node("num", val)
        if kind == "str":
            return Node("str", val)
        if kind == "op" and val == "(":
            n = self.ternary(); self.take(")")
            return n
        if kind == "name":
            if self.peek()[1] == "(":
                self.take("(")
                args = []
                while self.peek()[1] != ")":
                    if self.peek()[0] == "name" and self.peek(1)[1] == "=":
                        k = self.take()[1]; self.take("=")
                        args.append(Node("kw", k, [self.ternary()]))
                    else:
                        args.append(self.ternary())
                    if self.peek()[1] == ",":
                        self.take()
                    elif self.peek()[1] != ")":
                        raise ParseError(f"expected , or ) got {self.peek()[1]!r}")
                self.take(")")
                return Node("call", val, args)
            return Node("name", val)
        raise ParseError(f"unexpected token {val!r}")


def parse(expr: str) -> list[tuple[str | None, Node]]:
    p = _Parser(_tokenize(expr))
    return p.program()


def _walk(n: Node):
    yield n
    for a in n.args:
        yield from _walk(a)


@dataclass
class Shape:
    ok: bool                       # parsed
    error: str = ""
    n_ops: int = 0
    ops: list[str] = field(default_factory=list)
    names: set[str] = field(default_factory=set)   # identifiers that may be data fields
    variables: set[str] = field(default_factory=set)
    canonical: str = ""


def analyze(expr: str) -> Shape:
    try:
        stmts = parse(expr)
    except ParseError as e:
        return Shape(ok=False, error=str(e))
    variables = {t for t, _ in stmts if t}
    ops, names = [], set()
    for _, root in stmts:
        for n in _walk(root):
            if n.kind == "call" and n.value not in FREE_OPS:
                ops.append(n.value)
            elif n.kind == "name":
                names.add(n.value)
        # keyword values like filter=true / rettype=... are names but not fields
        for n in _walk(root):
            if n.kind == "kw":
                for a in n.args:
                    if a.kind == "name":
                        names.discard(a.value)
    names -= variables
    names -= {"true", "false", "nan", "NaN", "inf"}
    canonical = "; ".join((f"{t} = " if t else "") + render(r) for t, r in stmts)
    return Shape(ok=True, n_ops=len(ops), ops=ops, names=names, variables=variables, canonical=canonical)


VECTOR_OPS = {"vec_sum", "vec_avg", "vec_max", "vec_min", "vec_count", "vec_stddev", "vec_range", "vec_ir"}
PAIR_OPS = {"ts_corr", "ts_covariance", "ts_regression", "subtract", "divide"}


def lint(expr: str, is_vector) -> list[str]:
    """Structural mistakes BRAIN rejects or that make the signal meaningless."""
    try:
        stmts = parse(expr)
    except ParseError as e:
        return [f"parse: {e}"]
    problems = []

    def visit(n: Node, parent: Node | None):
        if n.kind == "name" and is_vector(n.value) and not (parent and parent.value in VECTOR_OPS):
            problems.append(f"vector field {n.value} not wrapped in vec_*")
        if n.kind == "str" and parent and parent.value.startswith("group_"):
            problems.append(f"quoted group {n.value} (use the bare name)")
        if n.kind == "num" and ("e" in n.value or "E" in n.value):
            problems.append(f"scientific notation {n.value} (BRAIN rejects it; write 0.00000001)")
        if n.kind == "call" and n.value in PAIR_OPS:
            pos = [a for a in n.args if a.kind != "kw"]
            if len(pos) >= 2 and render(pos[0]) == render(pos[1]):
                problems.append(f"{n.value} of identical inputs")
        for a in n.args:
            visit(a, n)

    for _, root in stmts:
        visit(root, None)
    return problems


def render(n: Node) -> str:
    """Deterministic text form (whitespace-normalised, infix preserved)."""
    if n.kind in ("num", "str", "name"):
        return n.value
    if n.kind == "kw":
        return f"{n.value}={render(n.args[0])}"
    if n.infix == "?:":
        return f"({render(n.args[0])} ? {render(n.args[1])} : {render(n.args[2])})"
    if n.infix in ("-", "!") and len(n.args) == 1:
        return f"{n.infix}{render(n.args[0])}"
    if n.infix:
        return f"({render(n.args[0])} {n.infix} {render(n.args[1])})"
    return f"{n.value}({', '.join(render(a) for a in n.args)})"


def skeleton(expr: str, field_tag) -> str:
    """Structure with fields replaced by field_tag(name) and numbers by N.
    Used to cluster near-duplicates (same idea, different window/wrapper)."""
    try:
        stmts = parse(expr)
    except ParseError:
        return ""

    def sk(n: Node) -> str:
        if n.kind == "num":
            return "N"
        if n.kind == "str":
            return "S"
        if n.kind == "name":
            return field_tag(n.value)
        if n.kind == "kw":
            return f"{n.value}=" + sk(n.args[0])
        return f"{n.value}(" + ",".join(sk(a) for a in n.args) + ")"

    return ";".join(sk(r) for _, r in stmts)
