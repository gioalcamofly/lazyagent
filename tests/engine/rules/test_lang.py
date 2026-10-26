"""The rule language (proposal 0014 "Rule language"): parsing and
precedence, Kleene's tables, unknown never matching, type errors with
suggestions, caps; the fact registry and the catalog loader."""

from __future__ import annotations

import itertools
import zipfile
from pathlib import Path

import pytest

from lazyagent.engine.rules import CatalogError, builtin, builtin_text, load
from lazyagent.engine.rules.facts import FACTS, ROW_ENV, RowContext
from lazyagent.engine.rules.lang import (
    BOOL,
    MAX_CHARS,
    MAX_DEPTH,
    MAX_NODES,
    NUM,
    TEXT,
    UNKNOWN,
    Env,
    RuleError,
    compile_expr,
    enum,
    parse,
    set_of,
    tokenize,
)
from lazyagent.engine.status import AgentFact, Facts
from lazyagent.models import CiCheck, PrInfo

COLOR = enum("color", ("red", "green", "blue"))
ENV = Env(
    {"a": BOOL, "b": BOOL, "c": BOOL, "n": NUM, "t": TEXT, "color": COLOR,
     "colors": set_of("color", ("red", "green", "blue"))},
    {},
)


class Ctx:
    def __init__(self, **values):
        self.values = values
        self.now = 0.0

    def fact(self, name):
        return self.values.get(name, UNKNOWN)


def ev(source: str, env: Env = ENV, **values):
    return compile_expr(source, env)(Ctx(**values))


V = (True, False, UNKNOWN)


# --- Parsing ------------------------------------------------------------------


def test_tokens_cover_literals_durations_and_comments():
    kinds = [(t.kind, t.value) for t in tokenize('x == "a\\"b" and 90s < 2h # note\n or 0.5')]
    assert kinds == [
        ("name", None), ("op", None), ("str", 'a"b'), ("kw", None), ("dur", 90),
        ("op", None), ("dur", 7200), ("kw", None), ("num", 0.5), ("end", None),
    ]
    assert tokenize("7d")[0].value == 7 * 86400
    assert tokenize("10m")[0].value == 600


def test_and_binds_tighter_than_or_and_not_tighter_than_and():
    tree = parse("a or b and c")
    assert tree.op == "or" and tree.args[1].op == "and"
    tree = parse("not a and b")
    assert tree.op == "and" and tree.args[0].op == "not"
    tree = parse("not a == b")
    assert tree.op == "not" and tree.args[0].op == "cmp"
    assert ev("a or b and c", a=True, b=False, c=False) is True
    assert ev("(a or b) and c", a=True, b=False, c=False) is False


def test_a_mixed_and_or_without_parentheses_warns():
    assert compile_expr("a or b and c", ENV).warnings
    assert not compile_expr("a or (b and c)", ENV).warnings
    assert not compile_expr("a and b and c", ENV).warnings


@pytest.mark.parametrize("source", [
    "a ==", "(a", "a b", '"x" "y"', "n < 1 < 2", "[n]", "a ? ", "@", "not",
])
def test_syntax_errors(source):
    with pytest.raises(RuleError):
        compile_expr(source, ENV)


def test_size_and_depth_are_capped():
    with pytest.raises(RuleError, match="longer than"):
        tokenize("a" * (MAX_CHARS + 1))
    with pytest.raises(RuleError, match="deeper"):
        parse("(" * (MAX_DEPTH + 1) + "a" + ")" * (MAX_DEPTH + 1))
    with pytest.raises(RuleError, match="parts"):
        parse(" and ".join(["a"] * (MAX_NODES + 1)))


# --- Three-valued logic ----------------------------------------------------------


def _kleene_and(x, y):
    if x is False or y is False:
        return False
    return True if x is True and y is True else UNKNOWN


def _kleene_or(x, y):
    if x is True or y is True:
        return True
    return False if x is False and y is False else UNKNOWN


@pytest.mark.parametrize("x,y", list(itertools.product(V, V)))
def test_kleene_tables(x, y):
    def values(**kw):
        return {k: v for k, v in kw.items() if v is not UNKNOWN}

    assert ev("a and b", **values(a=x, b=y)) is _kleene_and(x, y)
    assert ev("a or b", **values(a=x, b=y)) is _kleene_or(x, y)


def test_not_unknown_is_unknown_and_known_is_never_unknown():
    assert ev("not a") is UNKNOWN
    assert ev("not a", a=True) is False
    assert ev("known(a)") is False and ev("unknown(a)") is True
    assert ev("known(color)", color="red") is True
    assert ev('known(t) and t == "x"') is False


def test_a_comparison_with_an_unknown_is_unknown():
    for source in ('color == "red"', 'color != "red"', 'color in ["red"]',
                   'color not in ["red"]', "n > 1", 't + "x" == "x"', '"red" in colors',
                   "empty(colors)"):
        assert ev(source) is UNKNOWN, source


def test_comparisons_and_membership():
    assert ev('color == "red"', color="red") is True
    assert ev('color != "red"', color="red") is False
    assert ev('color in ["red", "blue"]', color="blue") is True
    assert ev('color not in ["red", "blue"]', color="blue") is False
    assert ev('"green" in colors', colors=frozenset({"green"})) is True
    assert ev('colors subset ["green", "red"]', colors=frozenset({"green"})) is True
    assert ev("empty(colors)", colors=frozenset()) is True
    assert ev("n >= 2 and n < 3", n=2) is True
    assert ev("n == 2", n=2.0) is True


def test_choice_takes_the_first_branch_only_when_true():
    assert ev('a ? "yes" : "no"', a=True) == "yes"
    assert ev('a ? "yes" : "no"', a=False) == "no"
    assert ev('a ? "yes" : "no"') == "no"
    assert ev('a ? "1" : b ? "2" : "3"', a=False, b=True) == "2"


def test_text_joins_text_and_numbers():
    assert ev('"#" + n + " " + t', n=12, t="x") == "#12 x"
    assert ev('t + "x"') is UNKNOWN


# --- Types --------------------------------------------------------------------


@pytest.mark.parametrize("source,message", [
    ('colour == "red"', "did you mean 'color'"),
    ('color == "rde"', "never 'rde'; did you mean 'red'"),
    ('color in ["red", "gren"]', "did you mean 'green'"),
    ('"pink" in colors', "never 'pink'"),
    ("n == t", "cannot compare"),
    ("n < t", "cannot order"),
    ("a and n", "needs a condition"),
    ("n + 1", "numbers are not added"),
    ('a ? a : b', "chooses text"),
    ("colors == colors", "compare a set"),
    ("frob(a)", "no function"),
    ("known(a, b)", "one argument"),
    ("empty(n)", "takes a set"),
    ("n in n", "needs a list or a set"),
])
def test_type_errors_name_the_problem(source, message):
    with pytest.raises(RuleError, match=message.replace("(", r"\(").replace("?", r"\?")):
        compile_expr(source, ENV)


def test_compile_checks_the_wanted_type():
    with pytest.raises(RuleError, match="expected bool"):
        compile_expr('t + "x"', ENV, "bool")
    assert compile_expr('t + "x"', ENV, "text").type == TEXT
    assert compile_expr('color', ENV, "text").type == COLOR


def test_compiled_expressions_list_the_facts_they_read():
    assert compile_expr('a and color == "red"', ENV).facts == {"a", "color"}


# --- The fact registry -----------------------------------------------------------


def test_every_fact_has_metadata_and_a_getter():
    assert FACTS
    for fact in FACTS.values():
        assert fact.phrase and fact.source and fact.freshness, fact.name
        assert fact.cost in ("free", "poll", "extra"), fact.name
        assert fact.row is not None or fact.item is not None, fact.name
        if fact.type.kind == "enum":
            assert all(v == v.lower() for v in fact.values), fact.name


def _row(**kw) -> RowContext:
    base = dict(key="repo:feat", repo="repo", branch="feat", worktree_path="/r")
    return RowContext(Facts(**{**base, **kw}), 100.0, 40.0, 24)


def test_row_facts_are_lower_case_and_unknown_when_absent():
    pr = PrInfo(3, "t", "OPEN", [CiCheck("x", "COMPLETED", "FAILURE")],
                review_decision="APPROVED", mergeable="CONFLICTING")
    c = _row(pr=pr, agents=(AgentFact("a1", "starting"), AgentFact("a2", "waiting")))
    assert [c.fact(n) for n in ("pr.state", "pr.review", "pr.mergeable", "ci.state")] == [
        "open", "approved", "conflicting", "red"]
    assert c.fact("ci.first_failing") == "x"
    assert c.fact("pr.review_requests") is UNKNOWN
    assert (c.fact("agents.running"), c.fact("agents.idle")) == (1, 1)
    assert c.fact("agents.first_running") == "starting"
    assert c.fact("item.idle") == 60.0
    empty = _row()
    assert empty.fact("pr.state") == "none"
    assert empty.fact("pr.review") is UNKNOWN
    assert empty.fact("tracker.ball") is UNKNOWN
    assert empty.fact("items.count") is UNKNOWN
    assert empty.fact("gesture.kind") == "none"
    assert empty.fact("report.needs_user") is False


def test_facts_are_memoised_per_context():
    c = _row()
    calls = []
    real = RowContext.getters["item.key"]
    RowContext.getters["item.key"] = lambda ctx: calls.append(1) or real(ctx)
    try:
        assert c.fact("item.key") == c.fact("item.key") == "repo:feat"
    finally:
        RowContext.getters["item.key"] = real
    assert calls == [1]


def test_registry_errors_suggest_facts_and_values():
    with pytest.raises(RuleError, match="did you mean 'pr.review'"):
        compile_expr('pr.reviw == "approved"', ROW_ENV)
    with pytest.raises(RuleError, match="did you mean 'approved'"):
        compile_expr('pr.review == "aproved"', ROW_ENV)
    with pytest.raises(RuleError, match="never 'sleeping'"):
        compile_expr('agents.detail_of("sleeping") == ""', ROW_ENV)


# --- The catalog -----------------------------------------------------------------


def test_the_builtin_catalog_has_the_old_numbers_and_the_kinds():
    catalog = builtin()
    assert sorted({r.num for r in catalog.rules}) == list(range(1, 17))
    assert [r.id for r in catalog.rules if r.locked] == [
        "closed.merged", "closed.pr_closed", "closed.already_merged", "closed.by_you",
        "park", "queue", "claim", "hold", "held", "delegated"]
    assert len(catalog.kinds) == 18
    assert catalog.rules[-1].id == "no_agent"
    for rule in catalog.rules:
        assert not rule.when.warnings, rule.id


_MINIMAL = '''
[[rule]]
id = "r"
num = 1
when = "true"
[rule.then]
ball = "you"
state = "x"
'''


@pytest.mark.parametrize("change,message", [
    (("ball = \"you\"", "ball = \"me\""), "ball must be one of"),
    (('when = "true"', 'when = "pr.reviw"'), "rule r when: no fact"),
    (('when = "true"', 'when = "pr.state"'), "expected bool"),
    (('state = "x"', 'level = "soon"'), "level must be one of"),
    (('state = "x"', ""), "then needs state"),
    (('num = 1', 'num = "1"'), "num must be an integer"),
    (('id = "r"', 'id = "R R"'), "id must match"),
    (('num = 1', 'num = 1\npriority = 3'), "unknown key"),
    (('state = "x"', 'state = "x"\nstate_expr = "\\"y\\""'), "not both"),
])
def test_catalog_errors_name_the_rule_and_key(change, message):
    with pytest.raises(CatalogError, match=message):
        load(_MINIMAL.replace(*change))


def test_a_rule_with_inherit_takes_the_field_from_below():
    catalog = load(_MINIMAL.replace('id = "r"', 'id = "top"').replace(
        'ball = "you"', 'inherit = ["ball"]').replace('state = "x"', 'state = "top"')
        + _MINIMAL.replace('num = 1', 'num = 2').replace('state = "x"', 'state = "below"'))
    d = catalog.derive(Facts("k", "r", "b", "/p"), 0.0, 0.0, 24)
    assert (d.rule, d.ball, d.state) == (1, "you", "top")


def test_kind_detectors_with_conditions_need_them():
    with pytest.raises(CatalogError, match="needs when"):
        load('[[kind]]\nid = "k"\nlevel = "now"\ntitle = "K"\nrank = 1\n'
             'detector = "agents"\nstate = "s"\n')
    with pytest.raises(CatalogError, match="fed by code"):
        load('[[kind]]\nid = "k"\nlevel = "now"\ntitle = "K"\nrank = 1\n'
             'detector = "jobs"\nwhen = "true"\n')
    with pytest.raises(CatalogError, match="no fact 'pr.review_requests'"):
        load('[[kind]]\nid = "k"\nlevel = "now"\ntitle = "K"\nrank = 1\n'
             'detector = "pr"\nstate = "s"\nwhen = "pr.review_requests == 0"\n')


def test_builtin_toml_ships_as_package_data(tmp_path, monkeypatch):
    """The wheel carries builtin.toml next to the code that loads it."""
    monkeypatch.delenv("LAZYAGENT_BUILD_DESKTOP", raising=False)
    hatchling = pytest.importorskip("hatchling.builders.wheel")
    root = Path(__file__).resolve().parents[3]
    builder = hatchling.WheelBuilder(str(root))
    wheel = next(iter(builder.build(directory=str(tmp_path), versions=["standard"])))
    with zipfile.ZipFile(wheel) as z:
        text = z.read("lazyagent/engine/rules/builtin.toml").decode()
    assert text == builtin_text()
