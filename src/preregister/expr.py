"""expr.py — a small total expression language, with an exact and an interval semantics.

A decision rule has to be MACHINE-READABLE for the falsifiability check to mean anything. A rule
written in prose ("we will conclude a leak if the AUC is meaningfully above chance") cannot be
analysed, so this package will not accept one. The language here is deliberately tiny: comparisons
and boolean connectives over metric names, plus the arithmetic you need to express a rule and
nothing more.

TWO SEMANTICS, AND THE DIFFERENCE IS THE WHOLE POINT.

    eval_exact(node, env)      -> the rule's value at one concrete assignment
    eval_interval(node, env)   -> the SET of values the rule could take over a region

The interval semantics is an over-approximation: it may report that a branch is reachable when it
is not, but it never reports a branch unreachable when it is. That asymmetry is what lets an
unreachable positive branch be REFUSED soundly while "looks reachable" is only ever reported as
*not proven unfalsifiable* — never as proven fine.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Union

__all__ = ["parse", "eval_exact", "eval_interval", "metric_names", "ParseError",
           "Interval", "TRUE", "FALSE", "MAYBE"]


class ParseError(ValueError):
    """The rule could not be parsed. Never downgraded to a warning: an unparseable rule is an
    unanalysable rule, and this package's entire value is the analysis."""


# --- three-valued booleans for the interval semantics ---------------------------------------------
TRUE, FALSE, MAYBE = "TRUE", "FALSE", "MAYBE"


@dataclass(frozen=True)
class Interval:
    """A closed real interval. `lo > hi` is the empty interval, written EMPTY."""
    lo: float
    hi: float

    def __post_init__(self) -> None:
        if math.isnan(self.lo) or math.isnan(self.hi):
            raise ValueError("NaN bound: an interval that cannot be ordered is not an interval")

    @property
    def empty(self) -> bool:
        return self.lo > self.hi

    def __contains__(self, x: float) -> bool:
        return self.lo <= x <= self.hi


EMPTY = Interval(1.0, -1.0)


# --- AST ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Unary:
    op: str
    operand: "Node"


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Node"
    right: "Node"


Node = Union[Num, Var, Unary, Binary]

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<num>\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+(?:[eE][-+]?\d+)?)
    | (?P<name>[A-Za-z_][A-Za-z_0-9.]*)
    | (?P<op><=|>=|==|!=|&&|\|\||[-+*/()<>!])
    )""", re.VERBOSE)

_KEYWORD_OPS = {"and": "&&", "or": "||", "not": "!"}
_COMPARISONS = {"<", "<=", ">", ">=", "==", "!="}


def _tokenize(src: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    i = 0
    while i < len(src):
        if src[i].isspace():
            i += 1
            continue
        m = _TOKEN.match(src, i)
        if not m or m.end() == i:
            raise ParseError(f"unexpected character {src[i]!r} at position {i} in {src!r}")
        i = m.end()
        if m.group("num") is not None:
            out.append(("num", m.group("num")))
        elif m.group("name") is not None:
            name = m.group("name")
            if name.lower() in _KEYWORD_OPS:
                out.append(("op", _KEYWORD_OPS[name.lower()]))
            elif name.lower() in ("true", "false"):
                out.append(("num", "1" if name.lower() == "true" else "0"))
            else:
                out.append(("name", name))
        else:
            out.append(("op", m.group("op")))
    return out


class _Parser:
    def __init__(self, tokens: List[Tuple[str, str]], src: str) -> None:
        self.t, self.i, self.src = tokens, 0, src

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, op: str) -> bool:
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] == op:
            self.i += 1
            return True
        return False

    def parse(self) -> Node:
        node = self.disjunction()
        if self.i != len(self.t):
            raise ParseError(f"trailing input at token {self.i} in {self.src!r}")
        return node

    def disjunction(self) -> Node:
        node = self.conjunction()
        while self.take("||"):
            node = Binary("||", node, self.conjunction())
        return node

    def conjunction(self) -> Node:
        node = self.negation()
        while self.take("&&"):
            node = Binary("&&", node, self.negation())
        return node

    def negation(self) -> Node:
        if self.take("!"):
            return Unary("!", self.negation())
        return self.comparison()

    def comparison(self) -> Node:
        node = self.additive()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in _COMPARISONS:
            self.i += 1
            right = self.additive()
            after = self.peek()
            if after and after[0] == "op" and after[1] in _COMPARISONS:
                # `0 < x < 1` reads as chained mathematics but evaluates as `(0<x) < 1` in most
                # languages, which is almost never what the author meant. Refuse rather than pick.
                raise ParseError(
                    f"chained comparison in {self.src!r}: write `a < b and b < c` explicitly, "
                    f"because `a < b < c` groups as `(a < b) < c` and would silently mean "
                    f"something else")
            return Binary(tok[1], node, right)
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while True:
            if self.take("+"):
                node = Binary("+", node, self.multiplicative())
            elif self.take("-"):
                node = Binary("-", node, self.multiplicative())
            else:
                return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while True:
            if self.take("*"):
                node = Binary("*", node, self.unary())
            elif self.take("/"):
                node = Binary("/", node, self.unary())
            else:
                return node

    def unary(self) -> Node:
        if self.take("-"):
            return Unary("-", self.unary())
        if self.take("+"):
            return self.unary()
        return self.atom()

    def atom(self) -> Node:
        tok = self.peek()
        if tok is None:
            raise ParseError(f"unexpected end of rule in {self.src!r}")
        if self.take("("):
            node = self.disjunction()
            if not self.take(")"):
                raise ParseError(f"unclosed parenthesis in {self.src!r}")
            return node
        if tok[0] == "num":
            self.i += 1
            return Num(float(tok[1]))
        if tok[0] == "name":
            self.i += 1
            return Var(tok[1])
        raise ParseError(f"unexpected {tok[1]!r} in {self.src!r}")


def parse(src: str) -> Node:
    if not isinstance(src, str) or not src.strip():
        raise ParseError("an empty rule is not a rule")
    return _Parser(_tokenize(src), src).parse()


def metric_names(node: Node) -> Set[str]:
    if isinstance(node, Var):
        return {node.name}
    if isinstance(node, Unary):
        return metric_names(node.operand)
    if isinstance(node, Binary):
        return metric_names(node.left) | metric_names(node.right)
    return set()


# --- exact semantics ------------------------------------------------------------------------------
def eval_exact(node: Node, env: Dict[str, float]) -> float:
    """Evaluate at one assignment. Booleans are 1.0/0.0 so a rule can nest inside arithmetic."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Var):
        if node.name not in env:
            raise KeyError(f"no value for metric {node.name!r}")
        return float(env[node.name])
    if isinstance(node, Unary):
        v = eval_exact(node.operand, env)
        return -v if node.op == "-" else (0.0 if v != 0 else 1.0)
    a, b = eval_exact(node.left, env), eval_exact(node.right, env)
    op = node.op
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            raise ZeroDivisionError("division by zero in the rule")
        return a / b
    if op == "&&":
        return 1.0 if (a != 0 and b != 0) else 0.0
    if op == "||":
        return 1.0 if (a != 0 or b != 0) else 0.0
    cmpf = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
            "==": a == b, "!=": a != b}[op]
    return 1.0 if cmpf else 0.0


# --- interval semantics ---------------------------------------------------------------------------
def _iv_add(x: Interval, y: Interval) -> Interval:
    return Interval(x.lo + y.lo, x.hi + y.hi)


def _iv_sub(x: Interval, y: Interval) -> Interval:
    return Interval(x.lo - y.hi, x.hi - y.lo)


def _iv_mul(x: Interval, y: Interval) -> Interval:
    c = [x.lo * y.lo, x.lo * y.hi, x.hi * y.lo, x.hi * y.hi]
    return Interval(min(c), max(c))


def _iv_div(x: Interval, y: Interval) -> Interval:
    if y.lo <= 0 <= y.hi:
        # An interval spanning zero yields an unbounded quotient. Returning a finite hull here
        # would be unsound in exactly the direction that matters -- it could make a reachable
        # branch look empty.
        return Interval(-math.inf, math.inf)
    c = [x.lo / y.lo, x.lo / y.hi, x.hi / y.lo, x.hi / y.hi]
    return Interval(min(c), max(c))


def eval_interval(node: Node, env: Dict[str, Interval]) -> Union[Interval, str]:
    """Return an Interval for arithmetic, or TRUE/FALSE/MAYBE for a boolean node.

    MAYBE means *this analysis could not decide*, never *it depends*. Callers must treat MAYBE as
    unknown and refuse to draw a conclusion from it.
    """
    if isinstance(node, Num):
        return Interval(node.value, node.value)
    if isinstance(node, Var):
        if node.name not in env:
            raise KeyError(f"no declared support for metric {node.name!r}")
        return env[node.name]
    if isinstance(node, Unary):
        v = eval_interval(node.operand, env)
        if node.op == "-":
            assert isinstance(v, Interval)
            return Interval(-v.hi, -v.lo)
        if v == TRUE:
            return FALSE
        if v == FALSE:
            return TRUE
        return MAYBE

    a = eval_interval(node.left, env)
    b = eval_interval(node.right, env)
    op = node.op

    if op in ("&&", "||"):
        if op == "&&":
            if a == FALSE or b == FALSE:
                return FALSE
            return TRUE if (a == TRUE and b == TRUE) else MAYBE
        if a == TRUE or b == TRUE:
            return TRUE
        return FALSE if (a == FALSE and b == FALSE) else MAYBE

    if not isinstance(a, Interval) or not isinstance(b, Interval):
        # A boolean used where a number is expected: 0/1, so hull it rather than crash.
        a = a if isinstance(a, Interval) else (Interval(1, 1) if a == TRUE else
                                               Interval(0, 0) if a == FALSE else Interval(0, 1))
        b = b if isinstance(b, Interval) else (Interval(1, 1) if b == TRUE else
                                               Interval(0, 0) if b == FALSE else Interval(0, 1))

    if op == "+":
        return _iv_add(a, b)
    if op == "-":
        return _iv_sub(a, b)
    if op == "*":
        return _iv_mul(a, b)
    if op == "/":
        return _iv_div(a, b)

    # Comparisons: decide only when the intervals are disjoint in the required direction.
    if op == "<":
        return TRUE if a.hi < b.lo else (FALSE if a.lo >= b.hi else MAYBE)
    if op == "<=":
        return TRUE if a.hi <= b.lo else (FALSE if a.lo > b.hi else MAYBE)
    if op == ">":
        return TRUE if a.lo > b.hi else (FALSE if a.hi <= b.lo else MAYBE)
    if op == ">=":
        return TRUE if a.lo >= b.hi else (FALSE if a.hi < b.lo else MAYBE)
    if op == "==":
        if a.lo == a.hi == b.lo == b.hi:
            return TRUE
        if a.hi < b.lo or b.hi < a.lo:
            return FALSE
        return MAYBE
    if op == "!=":
        if a.hi < b.lo or b.hi < a.lo:
            return TRUE
        if a.lo == a.hi == b.lo == b.hi:
            return FALSE
        return MAYBE
    raise ParseError(f"unknown operator {op!r}")
