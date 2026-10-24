"""The catalog (proposal 0014, "Rule language"): row rules, first match
wins, and the inbox kinds with their levels and conditions, compiled once
from TOML into closures over the fact registry.

A row rule::

    [[rule]]
    id = "merge"
    num = 4                       # the row's ``rule`` integer (today's numbers)
    locked = false                # the locked prefix (closed, park, queue, first responder)
    when = '''pr.state == "open" and pr.review == "approved" ...'''
    [rule.then]
    ball = "you"                  # or ball_expr = '...'
    level = "round"               # or level_expr; no level when left out
    state = "merge: approved, CI green"   # or state_expr
    blocks_agent = false          # or blocks_agent_expr
    suggestion = "..."            # or suggestion_expr
    inherit = ["ball"]            # take that field from the rules below

A kind::

    [[kind]]
    id = "approval"
    level = "now"
    title = "Needs approval"
    rank = 1
    blocks_agent = true
    detector = "agents"           # the code that finds candidates (agents, pr, actions,
                                  # exits, jobs, tracker, report, decision)
    when = 'agent.status == "waiting_for_approval"'   # only for agents, pr, actions
    state_expr = 'with("approve", agent.detail)'      # or state

An output that evaluates to unknown reads as empty text, no level, ball
``stalled`` or ``false``. Evaluation never raises on facts; a TOML or
expression error raises :class:`CatalogError` at load time.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from lazyagent.engine.rules.facts import (
    BALLS,
    LEVEL_VALUES,
    SCOPES,
    ItemContext,
    RowContext,
)
from lazyagent.engine.rules.lang import UNKNOWN, Expr, RuleError, compile_expr

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the tomli dependency on 3.10
    import tomli as tomllib

ID_RE = re.compile(r"^[a-z0-9_.-]{1,48}$")
RULE_KEYS = frozenset({"id", "num", "locked", "says", "when", "then"})
THEN_FIELDS = ("ball", "level", "state", "blocks_agent", "suggestion")
THEN_KEYS = frozenset(
    {*THEN_FIELDS, *(f"{f}_expr" for f in THEN_FIELDS), "inherit"}
)
INHERITABLE = ("ball", "state")
KIND_KEYS = frozenset(
    {"id", "level", "title", "rank", "blocks_agent", "detector", "says", "when",
     "state", "state_expr"}
)
# The detectors whose candidates a kind's condition decides, and the scope
# its facts are read in. The others are fed by events or by their own
# stores (exits, jobs, tracker items, reports, briefs).
CONDITION_DETECTORS = {"agents": "item", "pr": "item", "actions": "item"}
DETECTORS = frozenset({*CONDITION_DETECTORS, "exits", "jobs", "tracker", "report", "decision"})


class CatalogError(ValueError):
    """A catalog that does not load: the message names the rule and key."""


# ----------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Out:
    """One ``then`` field: a constant, or an expression."""

    value: Any = None
    expr: Expr | None = None

    def get(self, ctx: Any) -> Any:
        return self.value if self.expr is None else self.expr.fn(ctx)


_WANT = {"ball": "text", "level": "text", "state": "text", "blocks_agent": "bool",
         "suggestion": "text"}


def _check_const(where: str, name: str, value: Any) -> None:
    if name == "ball" and value not in BALLS:
        raise CatalogError(f"{where}: ball must be one of {', '.join(BALLS)}")
    if name == "level" and value not in LEVEL_VALUES:
        raise CatalogError(f"{where}: level must be one of {', '.join(LEVEL_VALUES)}")
    if name in ("state", "suggestion") and not isinstance(value, str):
        raise CatalogError(f"{where}: {name} must be text")
    if name == "blocks_agent" and not isinstance(value, bool):
        raise CatalogError(f"{where}: blocks_agent must be true or false")


def _compile(where: str, source: Any, scope: str, want: str) -> Expr:
    if not isinstance(source, str):
        raise CatalogError(f"{where}: an expression is a string")
    try:
        return compile_expr(source, SCOPES[scope], want)
    except RuleError as e:
        raise CatalogError(f"{where}: {e}") from None


# ----------------------------------------------------------------------
# Row rules
# ----------------------------------------------------------------------


@dataclass
class Rule:
    id: str
    num: int
    when: Expr
    outs: dict[str, Out]
    inherit: tuple[str, ...] = ()
    locked: bool = False
    says: str = ""
    test: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.test = self.when.fn


def _rule(raw: Mapping[str, Any], index: int) -> Rule:
    rid = raw.get("id")
    where = f"rule {rid or index + 1}"
    if not isinstance(rid, str) or not ID_RE.match(rid):
        raise CatalogError(f"{where}: id must match [a-z0-9_.-]{{1,48}}")
    extra = set(raw) - RULE_KEYS
    if extra:
        raise CatalogError(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
    num = raw.get("num")
    if not isinstance(num, int) or isinstance(num, bool):
        raise CatalogError(f"{where}: num must be an integer")
    if "when" not in raw:
        raise CatalogError(f"{where}: when is required")
    when = _compile(f"{where} when", raw["when"], "row", "bool")
    then = raw.get("then")
    if not isinstance(then, dict):
        raise CatalogError(f"{where}: then is required (a table)")
    extra = set(then) - THEN_KEYS
    if extra:
        raise CatalogError(f"{where}: unknown then key(s) {', '.join(sorted(extra))}")
    inherit = then.get("inherit", [])
    if not isinstance(inherit, list) or any(f not in INHERITABLE for f in inherit):
        raise CatalogError(f"{where}: inherit takes {INHERITABLE}")
    outs: dict[str, Out] = {}
    for name in THEN_FIELDS:
        key = f"{name}_expr"
        if name in then and key in then:
            raise CatalogError(f"{where}: {name} or {key}, not both")
        if key in then:
            outs[name] = Out(expr=_compile(f"{where} {key}", then[key], "row", _WANT[name]))
        elif name in then:
            _check_const(where, name, then[name])
            outs[name] = Out(then[name])
    for name in ("ball", "state"):
        if name not in outs and name not in inherit:
            raise CatalogError(f"{where}: then needs {name} (or {name}_expr, or inherit)")
    return Rule(rid, num, when, outs, tuple(inherit), bool(raw.get("locked", False)),
                str(raw.get("says", "")))


# ----------------------------------------------------------------------
# Kinds
# ----------------------------------------------------------------------


@dataclass
class KindRule:
    id: str
    level: str
    title: str
    rank: int
    blocks_agent: bool
    detector: str
    when: Expr | None
    state: Out | None
    says: str = ""

    def matches(self, ctx: ItemContext) -> bool:
        return self.when is not None and self.when.fn(ctx) is True

    def state_of(self, ctx: ItemContext) -> str:
        if self.state is None:
            raise ValueError(f"kind {self.id} has no state: its detector sets one")
        value = self.state.get(ctx)
        return "" if value is UNKNOWN or value is None else value


def _kind(raw: Mapping[str, Any], index: int) -> KindRule:
    kid = raw.get("id")
    where = f"kind {kid or index + 1}"
    if not isinstance(kid, str) or not ID_RE.match(kid):
        raise CatalogError(f"{where}: id must match [a-z0-9_.-]{{1,48}}")
    extra = set(raw) - KIND_KEYS
    if extra:
        raise CatalogError(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
    level = raw.get("level")
    _check_const(where, "level", level)
    title, rank = raw.get("title"), raw.get("rank")
    if not isinstance(title, str) or not title:
        raise CatalogError(f"{where}: title is required")
    if not isinstance(rank, int) or isinstance(rank, bool):
        raise CatalogError(f"{where}: rank must be an integer")
    blocks = raw.get("blocks_agent", False)
    _check_const(where, "blocks_agent", blocks)
    detector = raw.get("detector")
    if detector not in DETECTORS:
        raise CatalogError(f"{where}: detector must be one of {', '.join(sorted(DETECTORS))}")
    scope = CONDITION_DETECTORS.get(detector)
    when = None
    if scope is not None:
        if "when" not in raw:
            raise CatalogError(f"{where}: the {detector} detector needs when")
        when = _compile(f"{where} when", raw["when"], scope, "bool")
    elif "when" in raw:
        raise CatalogError(f"{where}: the {detector} detector is fed by code: no when")
    state = None
    if "state" in raw and "state_expr" in raw:
        raise CatalogError(f"{where}: state or state_expr, not both")
    if "state_expr" in raw:
        if scope is None:
            raise CatalogError(f"{where}: state_expr needs a condition detector")
        state = Out(expr=_compile(f"{where} state_expr", raw["state_expr"], scope, "text"))
    elif "state" in raw:
        _check_const(where, "state", raw["state"])
        state = Out(raw["state"])
    if scope is not None and state is None:
        raise CatalogError(f"{where}: state or state_expr is required")
    return KindRule(kid, level, title, rank, blocks, detector, when, state,
                    str(raw.get("says", "")))


# ----------------------------------------------------------------------
# The catalog
# ----------------------------------------------------------------------


@cache
def _derived() -> Any:
    # status imports this package lazily; the class is looked up once.
    from lazyagent.engine.status import Derived

    return Derived


class Catalog:
    """The ordered row rules and the kinds."""

    def __init__(self, rules: list[Rule], kinds: list[KindRule]) -> None:
        self.rules = rules
        self.kinds = kinds
        self.kind = {k.id: k for k in kinds}
        self._tests = [r.test for r in rules]

    # --- rows ---

    def derive(self, facts: Any, now: float, last_change: float, stalled_hours: float) -> Any:
        """The row's :class:`~lazyagent.engine.status.Derived`."""
        return self.evaluate(RowContext(facts, now, last_change, stalled_hours))

    def match(self, ctx: RowContext, start: int = 0) -> int:
        """The index of the first rule from ``start`` whose condition is true."""
        tests = self._tests
        for i in range(start, len(tests)):
            if tests[i](ctx) is True:
                return i
        return -1

    def evaluate(self, ctx: RowContext, start: int = 0) -> Any:
        i = self.match(ctx, start)
        if i < 0:
            return _derived()(0, "stalled", "no rule matched")
        rule = self.rules[i]
        below = self.evaluate(ctx, i + 1) if rule.inherit else None
        outs = rule.outs
        if below is not None and "ball" in rule.inherit:
            ball = below.ball
        else:
            ball = outs["ball"].get(ctx)
            if ball is UNKNOWN:
                ball = "stalled"
        if below is not None and "state" in rule.inherit:
            state = below.state
        else:
            state = outs["state"].get(ctx)
            if state is UNKNOWN or state is None:
                state = ""
        level = outs["level"].get(ctx) if "level" in outs else None
        blocks = outs["blocks_agent"].get(ctx) if "blocks_agent" in outs else False
        suggestion = outs["suggestion"].get(ctx) if "suggestion" in outs else None
        return _derived()(
            rule.num,
            ball,
            state,
            None if level is UNKNOWN else level,
            blocks is True,
            None if suggestion is UNKNOWN else suggestion,
        )

    # --- kinds ---

    def kinds_of(self, detector: str) -> list[KindRule]:
        return [k for k in self.kinds if k.detector == detector]


def load(text: str) -> Catalog:
    """A catalog from TOML text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CatalogError(f"not valid TOML: {e}") from None
    extra = set(data) - {"version", "rule", "kind"}
    if extra:
        raise CatalogError(f"unknown top-level key(s) {', '.join(sorted(extra))}")
    if data.get("version", 1) != 1:
        raise CatalogError("version must be 1")
    rules = [_rule(r, i) for i, r in enumerate(data.get("rule", []))]
    kinds = [_kind(k, i) for i, k in enumerate(data.get("kind", []))]
    for what, items in (("rule", rules), ("kind", kinds)):
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                raise CatalogError(f"{what} {item.id}: the id is used twice")
            seen.add(item.id)
    return Catalog(rules, kinds)
