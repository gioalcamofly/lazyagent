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
    delegable = false             # the agent can resolve its item: Delegate (d)
    passable = true               # the orchestrator can act on it: Pass (⇧O)

A kind::

    [[kind]]
    id = "approval"
    level = "now"
    title = "Needs approval"
    rank = 1
    blocks_agent = true
    detector = "agents"           # the code that finds candidates (agents, pr, actions,
                                  # review_requests, exits, jobs, tracker, report, decision)
    when = 'agent.status == "waiting_for_approval"'   # only for agents, pr, actions,
                                                      # review_requests
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
    REVIEW_FACTS,
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
    {*THEN_FIELDS, *(f"{f}_expr" for f in THEN_FIELDS), "inherit", "verb", "action", "item",
     "delegable", "passable"}
)
INHERITABLE = ("ball", "state")
KIND_KEYS = frozenset(
    {"id", "level", "title", "rank", "blocks_agent", "detector", "says", "when",
     "state", "state_expr", "verb", "action", "delegable", "passable"}
)
# The user actions (proposal 0014, "Actions"): id → (key, what it does).
# Actions are code; a rule only names its row's primary one.
ACTIONS: dict[str, tuple[str, str]] = {
    "answer": ("Enter", "Expands the line: approve (a) or deny (⇧D) a command there, type an answer to a question"),
    "expand": ("Enter", "Expands the line"),
    "open_terminal": ("o", "The agent's terminal"),
    "open_notes": ("n", "The agent's notes"),
    "open_pr": ("p", "The PR (you merge it, or review it, there)"),
    "open_tracker": ("p", "The tracker task"),
    "dismiss": ("x", "Dismiss this occurrence of the item"),
    "park": ("z", "Park the item or the work item"),
    "label": ("l", "Put a ball and a text on the row"),
    "close": ("Shift+X", "Close the work item (done / dropped) and stop its agent"),
    "take_back": ("u", "Take back a pass, or clear your gesture"),
    "pass": ("Shift+O", "Pass to orchestrator"),
    "delegate": ("d", "Send the item to the branch's agent"),
    "decision_view": ("v", "Open the brief"),
    "explain": ("y", "Explain this"),
    "explain_restore": ("Shift+Y", "Explain this (restore)"),
    "rebrief": ("b", "Request a new brief"),
    "seen": ("m", "Mark seen"),
    "all_seen": ("Shift+M", "Mark all seen"),
    "revert": ("Shift+U", "Revert the orchestrator's decision"),
    "why": ("w", "Why is this row in this state?"),
    "fold": ("f", "Fold In flight"),
    "fold_parked": ("Shift+Z", "Fold Parked"),
}
# The verb words the built-ins use; a user rule may add its own word.
VERBS = (
    "Approve", "Answer", "Merge", "Review", "Fix CI", "Sync", "Confirm", "Assign",
    "Decide", "Read", "Relaunch", "Check agent", "Fix job", "Update task",
    "Address review", "Resolve conflict", "Request reviewer", "Look",
)
# Who else can act on an item, by its verb, unless the kind or the rule says
# (``delegable``, ``passable``): Delegate (d) hands it to the branch's agent,
# so only what an agent can resolve on the branch; Pass (⇧O) hands the row to
# the orchestrator, so not what only the user may do (approve, answer,
# decide, review, merge, confirm). A verb of the user's own: neither
# delegable nor kept from the orchestrator, until the rule says.
DELEGABLE_VERBS = frozenset({
    "Fix CI", "Sync", "Address review", "Resolve conflict", "Relaunch", "Check agent",
    "Fix job", "Update task",
})
USERS_VERBS = frozenset({"Approve", "Answer", "Decide", "Review", "Merge", "Confirm"})


def can_delegate(verb: str | None, given: bool | None = None) -> bool:
    return given if given is not None else verb in DELEGABLE_VERBS


def can_pass(verb: str | None, given: bool | None = None) -> bool:
    return given if given is not None else verb not in USERS_VERBS


VERB_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 '-]{0,11}$")
# The detectors whose candidates a kind's condition decides, and the scope
# its facts are read in. The others are fed by events or by their own
# stores (exits, jobs, tracker items, reports, briefs).
CONDITION_DETECTORS = {"agents": "item", "pr": "item", "actions": "item",
                       "review_requests": "item", "attest": "item"}
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
    # An inbox item (Q-R22): with the queue on, the condition makes an item
    # ``Rule:<id>:<work item>`` and the row reaches the inbox through the
    # locked ``queue`` rule; the row rule itself is then skipped.
    item: bool = False
    verb: str | None = None
    action: str | None = None
    # builtin · global · repo, and where the definition is.
    origin: str = "builtin"
    file: str | None = None
    line: int | None = None
    # The definition this one replaced (same id), if any.
    base: Rule | None = None
    # A condition added to the base's own (``and_when``).
    and_when: Expr | None = None
    # Given in ``then``; None: by the verb (can_delegate, can_pass).
    delegable: bool | None = None
    passable: bool | None = None
    test: Any = field(init=False, repr=False)
    cond: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.and_when is not None and self.base is not None:
            self.cond = _kleene_and(self.base.cond, self.and_when.fn)
        else:
            self.cond = self.when.fn
        self.test = _queue_gate(self.cond) if self.item else self.cond

    @property
    def can_delegate(self) -> bool:
        return can_delegate(self.verb, self.delegable)

    @property
    def can_pass(self) -> bool:
        return can_pass(self.verb, self.passable)

    @property
    def facts(self) -> frozenset[str]:
        """Every fact the rule reads: its condition and its outputs."""
        names = set(self.when.facts)
        if self.and_when is not None:
            names |= self.and_when.facts
        for out in self.outs.values():
            if out.expr is not None:
                names |= out.expr.facts
        return frozenset(names)

    def conditions(self) -> list[tuple[str, Any]]:
        """The condition's top-level ``and`` terms (text, test), and_when's
        last: what the explain trace walks."""
        from lazyagent.engine.rules.lang import conjuncts

        terms = conjuncts(self.when.source, SCOPES["row"])
        if self.and_when is not None:
            terms += conjuncts(self.and_when.source, SCOPES["row"])
        return terms


def _kleene_and(a: Any, b: Any) -> Any:
    def fn(ctx: Any) -> Any:
        x = a(ctx)
        if x is False:
            return False
        y = b(ctx)
        if y is False:
            return False
        return True if x is True and y is True else UNKNOWN

    return fn


def _queue_gate(cond: Any) -> Any:
    def fn(ctx: Any) -> Any:
        if ctx.fact("queue.enabled") is True:
            return False
        return cond(ctx)

    return fn


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
    verb, action = _verb_action(where, then)
    item = then.get("item", False)
    if not isinstance(item, bool):
        raise CatalogError(f"{where}: item must be true or false")
    delegable, passable = capabilities(where, then)
    rule = Rule(rid, num, when, outs, tuple(inherit), bool(raw.get("locked", False)),
                str(raw.get("says", "")), item, verb, action,
                delegable=delegable, passable=passable)
    if item:
        _check_item(where, rule)
    return rule


def _verb_action(where: str, then: Mapping[str, Any]) -> tuple[str | None, str | None]:
    verb, action = then.get("verb"), then.get("action")
    if verb is not None and (not isinstance(verb, str)
                             or (verb not in VERBS and not VERB_RE.match(verb))):
        raise CatalogError(
            f"{where}: verb is one word of at most 12 characters (letters, digits, "
            f"spaces, ' and -), e.g. {', '.join(VERBS[:6])}"
        )
    if action is not None and action not in ACTIONS:
        raise CatalogError(
            f"{where}: no action {action!r}" + _close(action, ACTIONS)
            + f" (one of {', '.join(ACTIONS)})"
        )
    return verb, action


def capabilities(where: str, raw: Mapping[str, Any]) -> tuple[bool | None, bool | None]:
    """``delegable`` and ``passable`` as given (None: by the verb)."""
    out = []
    for name in ("delegable", "passable"):
        value = raw.get(name)
        if value is not None and not isinstance(value, bool):
            raise CatalogError(f"{where}: {name} must be true or false")
        out.append(value)
    return out[0], out[1]


def _close(word: Any, choices: Any) -> str:
    import difflib

    close = difflib.get_close_matches(str(word), list(choices), n=1)
    return f"; did you mean {close[0]!r}?" if close else ""


# What an item rule cannot read: the queue it feeds, and the first
# responder's state, which the queue's pass sees only after it.
ITEM_FORBIDDEN = ("items.", "queue.", "claim.", "hold.", "holder", "delegation.")


def _check_item(where: str, rule: Rule) -> None:
    ball = rule.outs.get("ball")
    if ball is None or ball.expr is not None or ball.value != "you" or rule.inherit:
        raise CatalogError(f"{where}: an inbox item (item = true) needs ball = \"you\"")
    bad = sorted(f for f in rule.facts if f.startswith(ITEM_FORBIDDEN))
    if bad:
        raise CatalogError(
            f"{where}: an inbox item cannot read {', '.join(bad)} "
            "(the queue's pass comes before them)"
        )


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
    verb: str | None = None
    action: str | None = None
    delegable: bool | None = None
    passable: bool | None = None

    @property
    def can_delegate(self) -> bool:
        return can_delegate(self.verb, self.delegable)

    @property
    def can_pass(self) -> bool:
        return can_pass(self.verb, self.passable)

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
    verb, action = _verb_action(where, raw)
    delegable, passable = capabilities(where, raw)
    return KindRule(kid, level, title, rank, blocks, detector, when, state,
                    str(raw.get("says", "")), verb, action, delegable, passable)


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

    def __init__(self, rules: list[Rule], kinds: list[KindRule],
                 not_human: frozenset[str] = frozenset(), ci: Any = None) -> None:
        from lazyagent.engine.ci_tests import NO_CONFIG

        self.rules = rules
        self.kinds = kinds
        # [review] not_human: logins pr.human_approvals leaves out (lower case).
        self.not_human = not_human
        # [ci.check] and [ci_unrelated] (engine/ci_tests.CiConfig).
        self.ci = NO_CONFIG if ci is None else ci
        # A rule reads a fact the PR poll fetches only on demand.
        self.reads_review = any(r.facts & REVIEW_FACTS for r in rules)
        self.kind = {k.id: k for k in kinds}
        self.by_id = {r.id: r for r in rules}
        self._tests = [r.test for r in rules]
        # Where the rules a user may move or replace start.
        self.unlocked = next((i for i, r in enumerate(rules) if not r.locked), len(rules))
        self.item_rules = [r for r in rules if r.item]

    # --- rows ---

    def derive(self, facts: Any, now: float, last_change: float, stalled_hours: float) -> Any:
        """The row's :class:`~lazyagent.engine.status.Derived`."""
        return self.evaluate(RowContext(facts, now, last_change, stalled_hours,
                                        self.not_human))

    def match(self, ctx: RowContext, start: int = 0) -> int:
        """The index of the first rule from ``start`` whose condition is true."""
        tests = self._tests
        first = getattr(ctx, "first", None)
        if first is not None and start <= self.unlocked:
            # first_item already ran the rules below the locked prefix on
            # these facts: none was true before its index (an item rule's
            # test is its condition behind the queue gate, so never true
            # when the condition is not).
            for i in range(start, self.unlocked):
                if tests[i](ctx) is True:
                    return i
            at, item = first
            if at < 0 or not item:
                return at
            start = at  # an item rule's gate decides; on from there
        for i in range(start, len(tests)):
            if tests[i](ctx) is True:
                return i
        return -1

    def evaluate(self, ctx: RowContext, start: int = 0) -> Any:
        i = self.match(ctx, start)
        if i < 0:
            return _derived()(0, "stalled", "no rule matched")
        return self.outputs(self.rules[i], ctx, i)

    def outputs(self, rule: Rule, ctx: RowContext, i: int | None = None) -> Any:
        """``rule``'s :class:`Derived` in ``ctx`` (its condition aside)."""
        below = None
        if rule.inherit:
            below = self.evaluate(ctx, (self.rules.index(rule) if i is None else i) + 1)
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
            elif rule.origin != "builtin":
                state = _clip(state)
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
            rule.id,
        )

    def first_item(self, ctx: RowContext) -> Rule | None:
        """The item rule that is the first match below the locked prefix,
        its queue gate aside, if any: the queue's pass makes its item. A
        rule above it that matches (``before = "merge"``) keeps it away."""
        for i in range(self.unlocked, len(self.rules)):
            rule = self.rules[i]
            if (rule.cond if rule.item else rule.test)(ctx) is True:
                ctx.first = (i, rule.item)
                return rule if rule.item else None
        ctx.first = (-1, False)
        return None

    # --- explain ---

    def explain(self, ctx: RowContext) -> dict[str, Any]:
        """The rule that matched, the facts it read, and for each rule above
        it the first condition that was not true (``rules_explain``)."""
        from lazyagent.engine.rules.facts import FACTS

        i = self.match(ctx)
        above = []
        for rule in self.rules[: i if i >= 0 else len(self.rules)]:
            entry = {"id": rule.id, "origin": rule.origin, **_failed(rule, ctx)}
            if rule.file is not None:
                # Yours: where it is, and what it saw.
                entry |= {"file": rule.file, "line": rule.line,
                          "facts": [fact_value(FACTS[n], ctx) for n in sorted(rule.facts)]}
            above.append(entry)
        result: dict[str, Any] = {"rule": None, "then": None, "facts": [], "above": above}
        if i < 0:
            return result
        rule = self.rules[i]
        derived = self.outputs(rule, ctx, i)
        result["rule"] = describe(rule)
        result["then"] = {
            "ball": derived.ball, "level": derived.level, "state": derived.state,
            "verb": rule.verb, "action": rule.action, "item": rule.item,
        }
        if rule.inherit:
            below = self.match(ctx, i + 1)
            result["inherited_from"] = self.rules[below].id if below >= 0 else None
        result["facts"] = [fact_value(FACTS[n], ctx) for n in sorted(rule.facts)]
        queue = ctx.facts.actionables or ()
        item_rule = self.by_id.get(queue[0].get("rule_id") or "") if queue else None
        if rule.id == "queue" and item_rule is not None:
            # The top item is a rule's (Merge): that rule, and what it read.
            # The rules between the locked prefix and it, each with the
            # first condition that was not true (as above, for the item).
            at = self.rules.index(item_rule)
            item_above = []
            for r in self.rules[self.unlocked:at]:
                entry = {"id": r.id, "origin": r.origin, **_failed(r, ctx)}
                if r.file is not None:
                    entry |= {"file": r.file, "line": r.line,
                              "facts": [fact_value(FACTS[n], ctx) for n in sorted(r.facts)]}
                item_above.append(entry)
            result["item_rule"] = describe(item_rule) | {
                "facts": [fact_value(FACTS[n], ctx) for n in sorted(item_rule.facts)],
                "above": item_above}
        return result

    # --- kinds ---

    def kinds_of(self, detector: str) -> list[KindRule]:
        return [k for k in self.kinds if k.detector == detector]


def _clip(text: str) -> str:
    from lazyagent.engine.status import clip_words

    return clip_words(text)


def _failed(rule: Rule, ctx: RowContext) -> dict[str, Any]:
    """The first top-level condition of ``rule`` that is not true."""
    for text, test in rule.conditions():
        value = test(ctx)
        if value is not True:
            return {"failed": text, "said": _said(text),
                    "value": "unknown" if value is UNKNOWN else "false"}
    if rule.item:
        # True, but the queue makes the item: shown there, or dismissed or
        # parked.
        return {"failed": "an inbox item: the queue shows it (or it was dismissed or parked)",
                "value": "item"}
    return {"failed": "", "value": "true"}


def _said(text: str) -> str:
    """The condition in English (the "Why?" view shows it)."""
    from lazyagent.engine.rules.doc import sentence

    try:
        return sentence(text)
    except Exception:  # noqa: BLE001 - a term the sentence maker does not know: its source
        return text


def plain(value: Any) -> Any:
    """A fact's value as JSON: sets sorted, unknown as None."""
    if value is UNKNOWN:
        return None
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def fact_value(fact: Any, ctx: Any) -> dict[str, Any]:
    from lazyagent.engine.rules.doc import shown

    try:
        value = ctx.fact(fact.name)
    except Exception:  # a getter bug: the fact reads as unknown
        value = UNKNOWN
    raw = plain(value)
    return {
        "name": fact.name,
        "value": raw,
        "unknown": value is UNKNOWN,
        "source": fact.source,
        "freshness": fact.freshness,
        # For people: "2 d 6 h", "24 h", "3 h ago" (the Why? view).
        "shown": shown(fact.type.kind, raw, getattr(ctx, "now", 0.0)),
    }


def describe(rule: Rule) -> dict[str, Any]:
    """A rule as rules_catalog and rules_explain show it."""
    out: dict[str, Any] = {
        "id": rule.id,
        "num": rule.num,
        "origin": rule.origin,
        "file": rule.file,
        "line": rule.line,
        "locked": rule.locked,
        "says": rule.says,
        "when": " ".join(rule.when.source.split()),
        "and_when": None if rule.and_when is None else " ".join(rule.and_when.source.split()),
        "item": rule.item,
        "verb": rule.verb,
        "action": rule.action,
        "delegable": rule.can_delegate,
        "passable": rule.can_pass,
        "replaces": None,
    }
    if rule.base is not None:
        out["replaces"] = {"origin": rule.base.origin, "file": rule.base.file,
                           "line": rule.base.line}
    return out


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
