"""The rule language (proposal 0014, "Rule language"): a small expression
grammar in a TOML string, parsed into an AST, type-checked against the fact
registry and compiled into closures, evaluated with three-valued logic.

Grammar, precedence lowest first::

    expr     := choice
    choice   := or ( "?" choice ":" choice )?          # text only
    or       := and ( "or" and )*
    and      := not ( "and" not )*
    not      := "not" not | compare
    compare  := sum ( op sum )?                        # == != < <= > >= in, not in, subset
    sum      := atom ( "+" atom )*                     # text concatenation
    atom     := literal | name | name "(" args ")" | "(" expr ")" | "[" literals "]"

Literals: ``"text"`` (or single quotes), ``12``, ``0.05``, ``true``,
``false``, durations ``90s`` ``10m`` ``24h`` ``7d`` (seconds), lists of
literals. ``#`` starts a comment to the end of the line.

**Unknown.** A fact can be :data:`UNKNOWN` (not linked, not fetched, not
there). A comparison or a function of an unknown is unknown; ``and``,
``or`` and ``not`` follow Kleene's tables; a rule matches only when its
condition is ``True``. ``known(x)`` and ``unknown(x)`` are never unknown.
A choice ``c ? a : b`` takes ``a`` only when ``c`` is true, like a rule.

No ``eval``, no attribute access, no user functions, no loops: evaluation is
linear in the expression size, and the size and depth are capped.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_CHARS = 2000
MAX_DEPTH = 32
MAX_NODES = 256


class _Unknown:
    """The third truth value (and the value of a fact that is not there)."""

    _instance: _Unknown | None = None

    def __new__(cls) -> _Unknown:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        raise TypeError("UNKNOWN has no truth value: compare with `is True`")


UNKNOWN: Any = _Unknown()


class RuleError(ValueError):
    """An expression that does not parse or does not type-check."""

    def __init__(self, message: str, pos: int | None = None) -> None:
        super().__init__(message if pos is None else f"{message} (at {pos})")
        self.pos = pos


# ----------------------------------------------------------------------
# Types
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Type:
    """``bool`` · ``num`` · ``duration`` · ``time`` · ``text`` · ``enum``
    (a closed list of lower-case values) · ``set`` (of an enum's values) ·
    ``list`` (a literal list) · ``raw`` (only passed to a function)."""

    kind: str
    name: str = ""
    values: tuple[str, ...] = ()
    of: Type | None = None

    def __str__(self) -> str:
        if self.kind in ("enum", "set") and self.name:
            return f"{self.kind} {self.name}"
        return self.kind


BOOL = Type("bool")
NUM = Type("num")
DURATION = Type("duration")
TIME = Type("time")
TEXT = Type("text")
RAW = Type("raw")


def enum(name: str, values: tuple[str, ...]) -> Type:
    return Type("enum", name, values)


def set_of(name: str, values: tuple[str, ...]) -> Type:
    return Type("set", name, values)


_ORDERED = ("num", "duration", "time")


def _textual(t: Type) -> bool:
    return t.kind in ("text", "enum")


# ----------------------------------------------------------------------
# What an expression can name
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Function:
    """A function from the closed list. ``args`` are type kinds (``text``,
    ``num``, ``raw``, ``set``, ``any``, or ``literal:<enum name>`` for a
    literal from an enum); ``impl(ctx, *values)`` gets values that may be
    :data:`UNKNOWN` and decides what that means."""

    name: str
    args: tuple[str, ...]
    returns: Type
    impl: Callable[..., Any]
    # An argument's enum, for literal:<enum> checks.
    arg_enum: Type | None = None
    doc: str = ""


class Env:
    """The names an expression may use: facts (name → :class:`Type`) and
    functions. The fact registry builds one per scope."""

    def __init__(self, facts: Mapping[str, Type], functions: Mapping[str, Function]) -> None:
        self.facts = dict(facts)
        self.functions = dict(functions)


# ----------------------------------------------------------------------
# Tokens
# ----------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|\#[^\n]*)
    |(?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
    |(?P<dur>\d+[smhd](?![A-Za-z0-9_]))
    |(?P<num>\d+(?:\.\d+)?(?![A-Za-z_]))
    |(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    |(?P<op>==|!=|<=|>=|<|>|\(|\)|\[|\]|,|\?|:|\+)
    """,
    re.VERBOSE,
)
_KEYWORDS = frozenset({"and", "or", "not", "in", "subset", "true", "false"})
_DURATION = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_COMPARE = frozenset({"==", "!=", "<", "<=", ">", ">=", "in", "not in", "subset"})


@dataclass(frozen=True)
class Token:
    kind: str  # str · dur · num · name · op · kw · end
    text: str
    pos: int
    value: Any = None


def _unescape(body: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), body)


def tokenize(source: str) -> list[Token]:
    if len(source) > MAX_CHARS:
        raise RuleError(f"expression longer than {MAX_CHARS} characters")
    out: list[Token] = []
    pos = 0
    while pos < len(source):
        m = _TOKEN.match(source, pos)
        if m is None:
            raise RuleError(f"unexpected character {source[pos]!r}", pos)
        kind = m.lastgroup or ""
        text = m.group()
        if kind == "str":
            out.append(Token("str", text, pos, _unescape(text[1:-1])))
        elif kind == "dur":
            out.append(Token("dur", text, pos, int(text[:-1]) * _DURATION[text[-1]]))
        elif kind == "num":
            out.append(Token("num", text, pos, float(text) if "." in text else int(text)))
        elif kind == "name":
            out.append(Token("kw" if text in _KEYWORDS else "name", text, pos))
        elif kind == "op":
            out.append(Token("op", text, pos))
        pos = m.end()
    out.append(Token("end", "", pos))
    return out


# ----------------------------------------------------------------------
# AST
# ----------------------------------------------------------------------


@dataclass
class Node:
    op: str  # lit · list · fact · call · not · and · or · cmp · + · ?
    pos: int
    args: list[Node] = field(default_factory=list)
    value: Any = None  # a literal's value, a fact's or function's name, a comparison
    type: Type | None = None  # a literal's
    paren: bool = False


class _Parser:
    def __init__(self, source: str) -> None:
        self.tokens = tokenize(source)
        self.i = 0
        self.depth = 0
        self.nodes = 0

    # --- helpers ---

    def peek(self, k: int = 0) -> Token:
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def take(self) -> Token:
        t = self.tokens[self.i]
        self.i += 1
        return t

    def at(self, kind: str, text: str | None = None, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == kind and (text is None or t.text == text)

    def expect(self, kind: str, text: str) -> Token:
        if not self.at(kind, text):
            t = self.peek()
            raise RuleError(f"expected {text!r}, found {t.text or 'the end'!r}", t.pos)
        return self.take()

    def node(self, op: str, pos: int, args: list[Node] | None = None, **kw: Any) -> Node:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise RuleError(f"expression has more than {MAX_NODES} parts", pos)
        return Node(op, pos, args or [], **kw)

    def enter(self, pos: int) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise RuleError(f"expression nested deeper than {MAX_DEPTH}", pos)

    # --- grammar ---

    def parse(self) -> Node:
        tree = self.choice()
        if not self.at("end"):
            t = self.peek()
            raise RuleError(f"unexpected {t.text!r}", t.pos)
        return tree

    def choice(self) -> Node:
        self.enter(self.peek().pos)
        cond = self.or_()
        if self.at("op", "?"):
            pos = self.take().pos
            yes = self.choice()
            self.expect("op", ":")
            no = self.choice()
            cond = self.node("?", pos, [cond, yes, no])
        self.depth -= 1
        return cond

    def or_(self) -> Node:
        left = self.and_()
        while self.at("kw", "or"):
            pos = self.take().pos
            left = self.node("or", pos, [left, self.and_()])
        return left

    def and_(self) -> Node:
        left = self.not_()
        while self.at("kw", "and"):
            pos = self.take().pos
            left = self.node("and", pos, [left, self.not_()])
        return left

    def not_(self) -> Node:
        if self.at("kw", "not"):
            pos = self.take().pos
            self.enter(pos)
            arg = self.not_()
            self.depth -= 1
            return self.node("not", pos, [arg])
        return self.compare()

    def _compare_op(self) -> str | None:
        t = self.peek()
        if t.kind == "op" and t.text in _COMPARE:
            return t.text
        if t.kind == "kw" and t.text in ("in", "subset"):
            return t.text
        if self.at("kw", "not") and self.at("kw", "in", 1):
            return "not in"
        return None

    def compare(self) -> Node:
        left = self.sum_()
        op = self._compare_op()
        if op is None:
            return left
        pos = self.take().pos
        if op == "not in":
            self.take()
        right = self.sum_()
        if self._compare_op() is not None:
            raise RuleError("comparisons do not chain: use `and`", self.peek().pos)
        return self.node("cmp", pos, [left, right], value=op)

    def sum_(self) -> Node:
        left = self.atom()
        while self.at("op", "+"):
            pos = self.take().pos
            left = self.node("+", pos, [left, self.atom()])
        return left

    def atom(self) -> Node:
        t = self.peek()
        if t.kind == "op" and t.text == "(":
            self.take()
            inner = self.choice()
            self.expect("op", ")")
            inner.paren = True
            return inner
        if t.kind == "op" and t.text == "[":
            self.take()
            items: list[Node] = []
            while not self.at("op", "]"):
                items.append(self.literal())
                if not self.at("op", ","):
                    break
                self.take()
            self.expect("op", "]")
            return self.node("list", t.pos, items)
        if t.kind == "name":
            self.take()
            if self.at("op", "("):
                self.take()
                args: list[Node] = []
                while not self.at("op", ")"):
                    args.append(self.choice())
                    if not self.at("op", ","):
                        break
                    self.take()
                self.expect("op", ")")
                return self.node("call", t.pos, args, value=t.text)
            return self.node("fact", t.pos, value=t.text)
        return self.literal()

    def literal(self) -> Node:
        t = self.take()
        if t.kind == "str":
            return self.node("lit", t.pos, value=t.value, type=TEXT)
        if t.kind == "num":
            return self.node("lit", t.pos, value=t.value, type=NUM)
        if t.kind == "dur":
            return self.node("lit", t.pos, value=t.value, type=DURATION)
        if t.kind == "kw" and t.text in ("true", "false"):
            return self.node("lit", t.pos, value=t.text == "true", type=BOOL)
        raise RuleError(f"unexpected {t.text or 'end of expression'!r}", t.pos)


def parse(source: str) -> Node:
    """The AST of ``source`` (no type check)."""
    return _Parser(source).parse()


# ----------------------------------------------------------------------
# Type check and compile
# ----------------------------------------------------------------------

Fn = Callable[[Any], Any]


@dataclass
class Expr:
    """A compiled expression: ``fn(ctx)`` with ``ctx.fact(name)`` and
    ``ctx.now``."""

    source: str
    type: Type
    fn: Fn
    facts: frozenset[str]
    warnings: list[str]

    def __call__(self, ctx: Any) -> Any:
        return self.fn(ctx)


def _suggest(word: str, choices: Any) -> str:
    close = difflib.get_close_matches(word, list(choices), n=1)
    return f"; did you mean {close[0]!r}?" if close else ""


class _Compiler:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.facts: set[str] = set()
        self.warnings: list[str] = []

    def compile(self, n: Node) -> tuple[Type, Fn]:
        return getattr(self, "_" + {"+": "plus", "?": "choice"}.get(n.op, n.op))(n)

    # --- leaves ---

    def _lit(self, n: Node) -> tuple[Type, Fn]:
        value = n.value
        return n.type or TEXT, lambda ctx: value

    def _list(self, n: Node) -> tuple[Type, Fn]:
        kinds = {a.type.kind for a in n.args if a.type is not None}
        if len(kinds) > 1:
            raise RuleError("a list mixes types", n.pos)
        of = n.args[0].type if n.args else TEXT
        value = frozenset(a.value for a in n.args)
        return Type("list", of=of), lambda ctx: value

    def _fact(self, n: Node) -> tuple[Type, Fn]:
        name = n.value
        t = self.env.facts.get(name)
        if t is None:
            if name in self.env.functions:
                raise RuleError(f"{name} is a function: call it, {name}(...)", n.pos)
            raise RuleError(f"no fact {name!r}" + _suggest(name, self.env.facts), n.pos)
        self.facts.add(name)
        return t, lambda ctx: ctx.fact(name)

    # --- boolean ---

    def _bool_arg(self, n: Node, what: str) -> Fn:
        t, fn = self.compile(n)
        if t.kind != "bool":
            raise RuleError(f"{what} needs a condition, not {t}", n.pos)
        return fn

    def _not(self, n: Node) -> tuple[Type, Fn]:
        a = self._bool_arg(n.args[0], "not")

        def fn(ctx: Any) -> Any:
            x = a(ctx)
            return UNKNOWN if x is UNKNOWN else not x

        return BOOL, fn

    def _mixed(self, n: Node) -> None:
        other = "or" if n.op == "and" else "and"
        if any(a.op == other and not a.paren for a in n.args):
            self.warnings.append(
                f"mixed `and` and `or` without parentheses at {n.pos}: "
                "`and` binds tighter; add parentheses to say what you mean"
            )

    def _and(self, n: Node) -> tuple[Type, Fn]:
        self._mixed(n)
        a, b = (self._bool_arg(x, "and") for x in n.args)

        def fn(ctx: Any) -> Any:
            x = a(ctx)
            if x is False:
                return False
            y = b(ctx)
            if y is False:
                return False
            return True if x is True and y is True else UNKNOWN

        return BOOL, fn

    def _or(self, n: Node) -> tuple[Type, Fn]:
        self._mixed(n)
        a, b = (self._bool_arg(x, "or") for x in n.args)

        def fn(ctx: Any) -> Any:
            x = a(ctx)
            if x is True:
                return True
            y = b(ctx)
            if y is True:
                return True
            return False if x is False and y is False else UNKNOWN

        return BOOL, fn

    # --- comparisons ---

    def _check_value(self, t: Type, lit: Node, fact: Node) -> None:
        """An enum compared with a literal outside its list."""
        if t.kind in ("enum", "set") and lit.op == "lit":
            values = [lit.value]
        elif t.kind in ("enum", "set") and lit.op == "list":
            values = [a.value for a in lit.args]
        else:
            return
        for v in values:
            if v not in t.values:
                what = fact.value if fact.op == "fact" else "it"
                raise RuleError(
                    f"{what} is never {v!r}" + _suggest(str(v), t.values)
                    + f" (one of {', '.join(t.values)})",
                    lit.pos,
                )

    def _cmp(self, n: Node) -> tuple[Type, Fn]:
        op = n.value
        left, right = n.args
        lt, a = self.compile(left)
        rt, b = self.compile(right)
        if op in ("==", "!="):
            if lt.kind in ("enum", "set") or rt.kind in ("enum", "set"):
                enum_side, other = (lt, right) if lt.kind == "enum" else (rt, left)
                if lt.kind == "enum" and rt.kind == "enum" and lt.name != rt.name:
                    raise RuleError(f"cannot compare {lt} with {rt}", n.pos)
                if enum_side.kind == "enum" and not (_textual(lt) and _textual(rt)):
                    raise RuleError(f"cannot compare {lt} with {rt}", n.pos)
                if enum_side.kind == "set":
                    raise RuleError("compare a set with `in`, `subset` or `empty()`", n.pos)
                self._check_value(enum_side, other, left if other is right else right)
            elif not (lt.kind == rt.kind or (_textual(lt) and _textual(rt))):
                raise RuleError(f"cannot compare {lt} with {rt}", n.pos)
            if right.op == "lit":
                lit = right.value
                if op == "==":
                    def fn(ctx: Any) -> Any:
                        x = a(ctx)
                        return UNKNOWN if x is UNKNOWN else x == lit
                else:
                    def fn(ctx: Any) -> Any:
                        x = a(ctx)
                        return UNKNOWN if x is UNKNOWN else x != lit
                return BOOL, fn
            eq = op == "=="

            def fn(ctx: Any) -> Any:
                x = a(ctx)
                if x is UNKNOWN:
                    return UNKNOWN
                y = b(ctx)
                if y is UNKNOWN:
                    return UNKNOWN
                return (x == y) is eq

            return BOOL, fn
        if op in ("<", "<=", ">", ">="):
            if lt.kind not in _ORDERED or lt.kind != rt.kind:
                raise RuleError(f"cannot order {lt} and {rt}", n.pos)
            compare = {
                "<": lambda x, y: x < y,
                "<=": lambda x, y: x <= y,
                ">": lambda x, y: x > y,
                ">=": lambda x, y: x >= y,
            }[op]

            def fn(ctx: Any) -> Any:
                x = a(ctx)
                if x is UNKNOWN:
                    return UNKNOWN
                y = b(ctx)
                if y is UNKNOWN:
                    return UNKNOWN
                return compare(x, y)

            return BOOL, fn
        if op in ("in", "not in"):
            if rt.kind == "list":
                if lt.kind == "enum":
                    self._check_value(lt, right, left)
                elif rt.of is not None and not (
                    lt.kind == rt.of.kind or (_textual(lt) and _textual(rt.of))
                ):
                    raise RuleError(f"{lt} is never in a list of {rt.of}", n.pos)
            elif rt.kind == "set":
                if not _textual(lt):
                    raise RuleError(f"{lt} is never in {rt}", n.pos)
                self._check_value(rt, left, right)
            else:
                raise RuleError(f"`{op}` needs a list or a set on its right, not {rt}", n.pos)
            want = op == "in"

            def fn(ctx: Any) -> Any:
                x = a(ctx)
                if x is UNKNOWN:
                    return UNKNOWN
                y = b(ctx)
                if y is UNKNOWN:
                    return UNKNOWN
                return (x in y) is want

            return BOOL, fn
        # subset
        if lt.kind not in ("set", "list") or rt.kind not in ("set", "list"):
            raise RuleError("`subset` compares two sets", n.pos)
        for side, other in ((lt, right), (rt, left)):
            if side.kind == "set":
                self._check_value(side, other, other)

        def fn(ctx: Any) -> Any:
            x = a(ctx)
            if x is UNKNOWN:
                return UNKNOWN
            y = b(ctx)
            if y is UNKNOWN:
                return UNKNOWN
            return frozenset(x) <= frozenset(y)

        return BOOL, fn

    # --- text ---

    def _plus(self, n: Node) -> tuple[Type, Fn]:
        (lt, a), (rt, b) = (self.compile(x) for x in n.args)
        for t, x in ((lt, n.args[0]), (rt, n.args[1])):
            if not (_textual(t) or t.kind == "num"):
                raise RuleError(f"`+` joins text, not {t}", x.pos)
        if not (_textual(lt) or _textual(rt)):
            raise RuleError("`+` joins text; numbers are not added", n.pos)

        def fn(ctx: Any) -> Any:
            x = a(ctx)
            if x is UNKNOWN:
                return UNKNOWN
            y = b(ctx)
            if y is UNKNOWN:
                return UNKNOWN
            return f"{x}{y}"

        return TEXT, fn

    def _choice(self, n: Node) -> tuple[Type, Fn]:
        c = self._bool_arg(n.args[0], "`?`")
        (yt, yes), (nt, no) = (self.compile(x) for x in n.args[1:])
        if yt.kind == "bool" or nt.kind == "bool":
            raise RuleError("`? :` chooses text, not conditions: use and / or", n.pos)
        if yt != nt and not (_textual(yt) and _textual(nt)):
            raise RuleError(f"`? :` branches differ: {yt} and {nt}", n.pos)
        t = yt if yt == nt else TEXT

        def fn(ctx: Any) -> Any:
            return yes(ctx) if c(ctx) is True else no(ctx)

        return t, fn

    # --- calls ---

    def _call(self, n: Node) -> tuple[Type, Fn]:
        name = n.value
        if name in ("known", "unknown"):
            if len(n.args) != 1:
                raise RuleError(f"{name}() takes one argument", n.pos)
            _, a = self.compile(n.args[0])
            if name == "known":
                return BOOL, lambda ctx: a(ctx) is not UNKNOWN
            return BOOL, lambda ctx: a(ctx) is UNKNOWN
        if name == "empty":
            if len(n.args) != 1:
                raise RuleError("empty() takes one argument", n.pos)
            t, a = self.compile(n.args[0])
            if t.kind not in ("set", "list"):
                raise RuleError(f"empty() takes a set, not {t}", n.pos)

            def fn(ctx: Any) -> Any:
                x = a(ctx)
                return UNKNOWN if x is UNKNOWN else not x

            return BOOL, fn
        f = self.env.functions.get(name)
        if f is None:
            choices = [*self.env.functions, "known", "unknown", "empty"]
            raise RuleError(f"no function {name!r}" + _suggest(name, choices), n.pos)
        if len(n.args) != len(f.args):
            raise RuleError(f"{name}() takes {len(f.args)} argument(s)", n.pos)
        fns = []
        for spec, arg in zip(f.args, n.args):
            if spec.startswith("literal:"):
                if arg.op != "lit" or arg.type != TEXT:
                    raise RuleError(f"{name}() takes a literal text", arg.pos)
                if f.arg_enum is not None:
                    self._check_value(f.arg_enum, arg, Node("fact", arg.pos, value=name))
            t, fn = self.compile(arg)
            if spec == "text" and not _textual(t):
                raise RuleError(f"{name}() takes text, not {t}", arg.pos)
            if spec == "num" and t.kind != "num":
                raise RuleError(f"{name}() takes a number, not {t}", arg.pos)
            fns.append(fn)
        impl = f.impl
        if len(fns) == 1:
            (a,) = fns
            return f.returns, lambda ctx: impl(ctx, a(ctx))
        if len(fns) == 2:
            a, b = fns
            return f.returns, lambda ctx: impl(ctx, a(ctx), b(ctx))
        return f.returns, lambda ctx: impl(ctx, *(g(ctx) for g in fns))


def compile_expr(source: str, env: Env, want: Type | str | None = None) -> Expr:
    """Parse, type-check and compile ``source``. ``want`` is the type the
    caller needs (a :class:`Type`, or a kind such as ``"bool"``)."""
    tree = parse(source)
    c = _Compiler(env)
    t, fn = c.compile(tree)
    if want is not None:
        kind = want if isinstance(want, str) else want.kind
        ok = t.kind == kind or (kind == "text" and _textual(t))
        if isinstance(want, Type) and want.kind == "enum" and t.kind == "enum":
            ok = t.name == want.name
        if not ok:
            raise RuleError(f"expected {want}, the expression is {t}")
    return Expr(source, t, fn, frozenset(c.facts), c.warnings)
