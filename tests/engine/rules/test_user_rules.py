"""User rule files (proposal 0014 R2): precedence, before/after, and_when,
locked rules, validation with per-rule fallback, the last good copy, the
repo allow by hash, the events, --why, status_list fields and the CLI.

HOME and every XDG dir are under the test's tmp_path (conftest.py)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from lazyagent.engine.rules import builtin
from lazyagent.engine.rules.export import format_explain
from lazyagent.engine.rules.facts import RowContext
from lazyagent.engine.rules.hub import RULES_CHANGED, RULES_ERRORS, RulesHub, digest
from lazyagent.engine.rules.user import compose, parse_file
from lazyagent.engine.status import AgentFact, Facts
from lazyagent.models import CiCheck, PrInfo
from tests.protocol.golden_cases import (
    GLOBAL_RULES,
    REPO_RULES,
    _call,
    _rules_files,
    _status_engine,
)

NOW = 1_800_000_000.0
GREEN = [CiCheck("tests", "COMPLETED", "SUCCESS")]


def ready(branch: str = "feat", **kw) -> Facts:
    """An approved, green, mergeable PR with no agent (the queue on)."""
    pr = PrInfo(7, "Retry the export", "OPEN", GREEN, "https://example.com/pull/7",
                review_decision="APPROVED", mergeable="MERGEABLE")
    return Facts(key=f"repo:{branch}", repo="repo", branch=branch, worktree_path=f"/r-{branch}",
                 pr=pr, **({"actionables": ()} | kw))


def catalog_of(*files: tuple[str, str]):
    """The effective catalog of ``(scope, text)`` files, and its errors."""
    parsed = [parse_file(text, scope, f"/{scope}/rules.toml") for scope, text in files]
    composed = compose(builtin(), parsed)
    return composed.catalog, composed.errors, composed.warnings


def ids(catalog) -> list[str]:
    return [r.id for r in catalog.rules]


def rule(id: str, when: str = "true", **then) -> str:
    then = then or {"ball": "others", "state": "x"}
    body = "\n".join(f"{k} = {json.dumps(v)}" for k, v in then.items())
    return f"[[rule]]\nid = \"{id}\"\nwhen = '{when}'\n[rule.then]\n{body}\n"


# --- Precedence ---------------------------------------------------------------


def test_new_rules_go_after_the_locked_prefix_repo_before_global_before_the_builtins():
    cat, errors, _ = catalog_of(("global", rule("g1") + rule("g2")), ("repo", rule("r1")))
    assert errors == []
    order = ids(cat)
    first_open = cat.unlocked
    assert all(r.locked for r in cat.rules[:first_open])
    assert order[first_open:first_open + 3] == ["r1", "g1", "g2"]
    assert order[first_open + 3] == "approve"  # the first non-locked built-in
    assert {r.id: r.origin for r in cat.rules}["g1"] == "global"
    assert cat.by_id["r1"].file == "/repo/rules.toml" and cat.by_id["r1"].line == 1


def test_the_same_id_replaces_in_place_repo_over_global_over_builtin():
    g = rule("merge", 'pr.state == "open"', ball="you", state="merge: global")
    r = rule("merge", 'pr.state == "open"', ball="you", state="merge: repo")
    cat, errors, _ = catalog_of(("global", g), ("repo", r))
    assert errors == []
    assert ids(cat) == ids(builtin()), "same place, nothing added"
    merge = cat.by_id["merge"]
    assert (merge.origin, merge.num, merge.base.origin, merge.base.base.origin) == (
        "repo", 4, "global", "builtin")
    ctx = RowContext(ready(actionables=None), NOW, NOW, 24)
    assert cat.evaluate(ctx).state == "merge: repo"


def test_before_and_after_place_a_rule_and_repo_placements_come_last():
    cat, errors, _ = catalog_of(
        ("global", rule("qa", **{"ball": "others", "state": "waiting for QA"})
         .replace("[[rule]]\n", "[[rule]]\nafter = \"merge\"\n")
         + rule("first").replace("[[rule]]\n", "[[rule]]\nbefore = \"changes_requested\"\n")),
    )
    assert errors == []
    order = ids(cat)
    assert order.index("qa") == order.index("merge") + 1
    assert order.index("first") == order.index("changes_requested") - 1


@pytest.mark.parametrize("placement, message", [
    ('before = "queue"', "a locked rule cannot be preceded"),
    ('after = "nope"', "no such rule"),
    ('before = "c1"', "itself"),
    ('before = "merge"\nafter = "merge"', "before or after, not both"),
])
def test_bad_placements_skip_the_rule(placement, message):
    text = rule("c1").replace("[[rule]]\n", f"[[rule]]\n{placement}\n")
    cat, errors, _ = catalog_of(("global", text))
    assert "c1" not in cat.by_id
    assert len(errors) == 1 and message in errors[0]["message"], errors
    assert errors[0]["line"] in (2, 3)


def test_a_cycle_of_placements_is_an_error_and_keeps_file_order():
    a = rule("a").replace("[[rule]]\n", '[[rule]]\nbefore = "b"\n')
    b = rule("b").replace("[[rule]]\n", '[[rule]]\nbefore = "a"\n')
    cat, errors, _ = catalog_of(("global", a + b))
    assert {e["rule"] for e in errors} == {"a", "b"}
    assert all("cycle" in e["message"] for e in errors)
    assert ids(cat)[cat.unlocked:cat.unlocked + 2] == ["a", "b"]


# --- and_when, disabled, locked -------------------------------------------------


def test_and_when_narrows_a_builtin_in_place_and_keeps_its_outputs():
    text = '[[rule]]\nid = "merge"\nand_when = \'item.branch != "release"\'\n'
    cat, errors, _ = catalog_of(("global", text))
    assert errors == []
    merge = cat.by_id["merge"]
    assert (merge.verb, merge.action, merge.item, merge.num) == ("Merge", "open_pr", True, 4)
    no_queue = dict(actionables=None)
    on = RowContext(ready("feat", **no_queue), NOW, NOW, 24)
    off = RowContext(ready("release", **no_queue), NOW, NOW, 24)
    assert cat.evaluate(on).rule_id == "merge"
    assert cat.evaluate(off).rule_id != "merge"
    # The explain trace names the added condition.
    trace = {a["id"]: a for a in cat.explain(off)["above"]}
    assert trace["merge"]["failed"] == 'item.branch != "release"'
    assert trace["merge"]["file"] == "/global/rules.toml"


def test_and_when_with_no_rule_to_narrow_is_an_error():
    _, errors, _ = catalog_of(("global", '[[rule]]\nid = "mine"\nand_when = "true"\n'))
    assert "there is no 'mine' to narrow" in errors[0]["message"]


def test_disabled_turns_a_builtin_off():
    cat, errors, _ = catalog_of(("global", '[[rule]]\nid = "no_reviewer"\ndisabled = true\n'))
    assert errors == [] and "no_reviewer" not in cat.by_id


@pytest.mark.parametrize("text", [
    rule("queue"),
    '[[rule]]\nid = "park"\ndisabled = true\n',
    '[[rule]]\nid = "closed.merged"\nand_when = "true"\n',
])
def test_locked_rules_cannot_be_replaced_narrowed_turned_off_or_moved(text):
    cat, errors, _ = catalog_of(("global", text))
    assert ids(cat) == ids(builtin())
    assert "locked" in errors[0]["message"]
    assert errors[0]["fallback"] == "skipped; the built-in is in force"


def test_locked_and_num_are_not_user_keys():
    _, errors, _ = catalog_of(("global", rule("mine").replace("[[rule]]\n", "[[rule]]\nlocked = true\n")))
    assert "locked and num are the built-ins' own" in errors[0]["message"]


# --- Validation and fallback ----------------------------------------------------


def test_a_rule_with_an_unknown_fact_is_skipped_and_the_builtin_stays_in_force():
    text = """version = 1

[[rule]]
id = "merge"
when = '''
pr.state == "open" and pr.review == "approved"
and tracker.qa in ["tested", "not_required"]
'''
[rule.then]
ball = "you"
state = "merge: QA ok"

[[rule]]
id = "fine"
when = 'pr.state == "open"'
[rule.then]
ball = "others"
state = "loaded"
"""
    cat, errors, _ = catalog_of(("global", text))
    assert cat.by_id["merge"].origin == "builtin"
    assert cat.by_id["fine"].origin == "global", "the other rules of the file load"
    [e] = errors
    assert (e["rule"], e["line"], e["fallback"]) == ("merge", 7, "skipped; the built-in is in force")
    assert "no fact 'tracker.qa'" in e["message"] and "(at " not in e["message"]


def test_an_overridden_global_rule_stays_when_the_repo_rule_is_bad():
    g = rule("mine", ball="others", state="global")
    r = rule("mine", "pr.nope", ball="others", state="repo")
    cat, errors, _ = catalog_of(("global", g), ("repo", r))
    assert cat.by_id["mine"].origin == "global"
    assert errors[0]["fallback"] == "skipped; the global rule is in force"


@pytest.mark.parametrize("then, message", [
    ({"ball": "you", "state": "x", "verb": "Merge it now please"}, "verb is one word"),
    ({"ball": "you", "state": "x", "action": "open_pull"}, "did you mean 'open_pr'"),
    ({"ball": "nobody", "state": "x"}, "ball must be one of"),
    ({"ball": "others", "state": "x", "item": True}, 'needs ball = "you"'),
])
def test_outputs_are_validated(then, message):
    _, errors, _ = catalog_of(("global", rule("mine", **then)))
    assert message in errors[0]["message"], errors


def test_an_item_rule_cannot_read_the_queue():
    _, errors, _ = catalog_of(("global", rule("mine", "items.count == 0", ball="you", state="x")))
    assert "cannot read items.count" in errors[0]["message"]


def test_user_rules_with_ball_you_are_items_with_level_round_and_states_clipped():
    cat, errors, _ = catalog_of(
        ("global", rule("mine", 'pr.state == "open"', ball="you", state="one two three four five six")))
    assert errors == []
    mine = cat.by_id["mine"]
    assert mine.item and mine.outs["level"].value == "round"
    ctx = RowContext(ready(actionables=None), NOW, NOW, 24)
    assert cat.evaluate(ctx).state == "one two three four five…"


@pytest.mark.parametrize("when", [
    '__import__("os").system("true")',
    'pr.state.__class__ == "x"',
    'open("/etc/passwd")',
    'exec("1")',
])
def test_rules_are_data_never_code(when):
    _, errors, _ = catalog_of(("global", rule("evil", when)))
    assert errors and errors[0]["rule"] == "evil"


def test_the_caps_hold_on_user_files():
    long = " or ".join(['pr.state == "open"'] * 120)
    deep = "(" * 40 + "true" + ")" * 40
    wide = " and ".join(["true"] * 200)  # 1795 characters, 399 parts
    _, errors, _ = catalog_of(("global", rule("a", long) + rule("b", deep) + rule("c", wide)))
    messages = {e["rule"]: e["message"] for e in errors}
    assert "longer than 2000" in messages["a"]
    assert "deeper than 32" in messages["b"]
    assert "more than 256" in messages["c"]
    big = parse_file("# " + "x" * (256 * 1024), "global", "/g")
    assert not big.ok and "larger than 256 KB" in big.errors[0]["message"]
    many = parse_file("".join(rule(f"r{i}") for i in range(201)), "global", "/g")
    assert len(many.entries) == 200 and "more than 200 rules" in many.errors[0]["message"]


def test_file_level_problems():
    bad = parse_file("[[rule]\nid = 1", "global", "/g")
    assert not bad.ok and bad.errors[0]["line"] == 1 and "not valid TOML" in bad.errors[0]["message"]
    assert not parse_file("version = 2", "global", "/g").ok
    later = parse_file('version = 1\n[ci]\nnondeterministic = ["eval"]\n', "global", "/g")
    assert later.ok and "later version" in later.warnings[0]["message"]
    dup = parse_file(rule("a") + rule("a"), "global", "/g")
    assert len(dup.entries) == 1 and "used twice" in dup.errors[0]["message"]


# --- The hub: files, last good, allow by hash, events ---------------------------


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / ".lazyagent").mkdir(parents=True)
    return root


def global_file() -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "lazyagent" / "rules.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_nothing_is_read_or_written_outside_the_test_dirs(tmp_path):
    assert str(global_file()).startswith(str(tmp_path))
    hub = RulesHub(str(tmp_path / "repo"))
    hub.check_result()
    assert all(str(f["path"]).startswith(str(tmp_path)) for f in hub.files)


def test_a_broken_global_file_keeps_its_last_good_version_across_restarts(repo):
    path = global_file()
    path.write_text(rule("mine", ball="others", state="good"))
    hub = RulesHub(str(repo))
    assert hub.catalog.by_id["mine"].origin == "global"
    path.write_text("[[rule]\nbroken")
    assert hub.check()
    assert "mine" in hub.catalog.by_id
    [e] = hub.errors
    assert e["fallback"] == "the last good version is in force"
    assert hub.files[0]["applied"] == "last_good"
    # A new engine (a restart): the copy under state_dir()/rules/ holds it.
    again = RulesHub(str(repo))
    assert "mine" in again.catalog.by_id
    copy = Path(os.environ["XDG_STATE_HOME"]) / "lazyagent" / "rules" / "global.toml"
    assert "good" in copy.read_text()
    path.write_text(rule("mine", ball="others", state="fixed"))
    assert hub.check() and hub.errors == []


def test_a_repo_file_applies_only_once_its_hash_is_allowed(repo):
    path = repo / ".lazyagent" / "rules.toml"
    v1 = rule("repo_rule", ball="others", state="v1")
    path.write_text(v1)
    hub = RulesHub(str(repo))
    assert "repo_rule" not in hub.catalog.by_id
    [e] = hub.errors
    assert e["pending"] == digest(v1) and e["fallback"] == "not applied"
    assert "not allowed yet" in e["message"] and digest(v1)[:12] in e["message"]
    with pytest.raises(ValueError, match="it changed"):
        hub.allow("0" * 12)
    with pytest.raises(ValueError, match="at least 12"):
        hub.allow(digest(v1)[:6])
    hub.allow(digest(v1)[:12])
    assert hub.catalog.by_id["repo_rule"].origin == "repo" and hub.errors == []
    # Changed: the last allowed version stays until it is allowed again.
    v2 = rule("repo_rule", ball="others", state="v2") + rule("sneaky", ball="you", state="x")
    path.write_text(v2)
    assert hub.check()
    assert "sneaky" not in hub.catalog.by_id
    assert hub.catalog.by_id["repo_rule"].outs["state"].value == "v1"
    assert "changed since you allowed it" in hub.errors[0]["message"]
    assert hub.files[1]["applied"] == "last_allowed"
    # Across a restart too.
    assert RulesHub(str(repo)).catalog.by_id["repo_rule"].outs["state"].value == "v1"
    hub.allow(digest(v2))
    assert "sneaky" in hub.catalog.by_id and hub.errors == []


def test_the_hub_emits_rules_changed_and_rules_errors(repo):
    events: list[tuple[str, dict]] = []
    path = global_file()
    path.write_text(rule("mine"))
    hub = RulesHub(str(repo), lambda name, data: events.append((name, data)))
    hub.check()
    assert [n for n, _ in events] == [RULES_CHANGED]
    events.clear()
    path.write_text(rule("mine") + rule("bad", "nope.fact"))
    hub.check()
    [(name, data)] = [e for e in events if e[0] == RULES_ERRORS]
    assert data["errors"][0]["rule"] == "bad"
    events.clear()
    assert not hub.check(), "unchanged files: nothing re-read"
    path.write_text(rule("mine"))
    hub.check()
    assert (RULES_ERRORS, {"errors": []}) in events


# --- The engine: Merge as an item, a global rule, --why, fields -----------------


def _rows(engine) -> dict:
    return {r["key"]: r for r in engine.status.list()["rows"]}


def test_the_builtin_merge_is_an_inbox_item_that_can_be_dismissed():
    engine = _status_engine()
    with _rules_files({}):
        row = _rows(engine)["repo:docs-index"]
        assert (row["rule"], row["zone"], row["state"], row["needs_you"]["top"]) == (
            2, "inbox", "merge: approved, CI green", "Rule:merge:repo:docs-index")
        item = engine.actionables.get("Rule:merge:repo:docs-index")
        assert (item["kind"], item["verb"], item["rule_id"], item["title"]) == (
            "rule", "Merge", "merge", "Merge")
        engine.actionables.mark("Rule:merge:repo:docs-index", "dismiss")
        row = _rows(engine)["repo:docs-index"]
        assert (row["zone"], row["ball"], row["state"]) == (
            "in_flight", "stalled", "approved, CI green")


def test_a_global_rule_that_requires_a_fact_makes_merge_disappear_and_why_names_it():
    text = """version = 1

# No Merge on a branch without a linked tracker task.
[[rule]]
id = "merge"
and_when = 'tracker.linked'
"""
    engine = _status_engine()
    with _rules_files({GLOBAL_RULES: text}):
        row = _rows(engine)["repo:docs-index"]
        assert row["zone"] == "in_flight" and row["needs_you"] is None
        assert "Rule:merge:repo:docs-index" not in [
            i["id"] for i in engine.actionables.list()["items"]]
        result = asyncio.run(_call(engine, "rules_explain", {"key": "docs-index"}))[2]["result"]
    merge = next(a for a in result["above"] if a["id"] == "merge")
    assert (merge["failed"], merge["value"], merge["file"], merge["line"]) == (
        "tracker.linked", "false", GLOBAL_RULES, 4)
    assert {f["name"]: f["value"] for f in merge["facts"]}["tracker.linked"] is False
    text = format_explain(result)
    assert f"merge                    false: tracker.linked  (your global rules.toml ({GLOBAL_RULES}, line 4))" in text
    assert "tracker.linked = false" in text


def test_why_on_a_merge_row_shows_the_item_rule_and_its_facts():
    engine = _status_engine()
    with _rules_files({}):
        result = asyncio.run(_call(engine, "rules_explain", {"key": "repo:docs-index"}))[2]["result"]
    assert result["rule"]["id"] == "queue"
    assert result["item_rule"]["id"] == "merge"
    assert {f["name"]: f["value"] for f in result["item_rule"]["facts"]} == {
        "ci.state": "green", "pr.mergeable": "mergeable", "pr.review": "approved",
        "pr.state": "open"}
    assert "item   from rule merge  (built-in)" in format_explain(result)


def test_a_bad_global_rule_reaches_rules_check_and_the_builtin_stays():
    text = rule("merge", 'tracker.qa == "tested"', ball="you", state="merge: QA ok")
    engine = _status_engine()
    with _rules_files({GLOBAL_RULES: "version = 1\n\n" + text}):
        check = asyncio.run(_call(engine, "rules_check", {}))[2]["result"]
        assert _rows(engine)["repo:docs-index"]["state"] == "merge: approved, CI green"
    [e] = check["errors"]
    assert (e["file"], e["line"], e["rule"]) == (GLOBAL_RULES, 5, "merge")
    assert e["fallback"] == "skipped; the built-in is in force"


def test_a_changed_repo_file_is_not_applied_until_allowed():
    text = "version = 1\n" + rule("merge", "false", ball="you", state="never")
    engine = _status_engine()
    with _rules_files({REPO_RULES: text}):
        assert _rows(engine)["repo:docs-index"]["state"] == "merge: approved, CI green"
        allowed = asyncio.run(_call(engine, "rules_allow", {"hash": digest(text)}))[2]
        assert allowed["result"]["allowed"] is True
        assert _rows(engine)["repo:docs-index"]["zone"] == "in_flight"


def test_rules_allow_is_the_users_only():
    engine = _status_engine()
    with _rules_files({REPO_RULES: rule("a")}):
        response = asyncio.run(_call(engine, "rules_allow",
                                     {"hash": digest(rule("a")), "caller": "tok"}))[2]
    assert "error" in response


def test_status_list_fields_are_opt_in():
    engine = _status_engine()
    with _rules_files({}):
        plain = _rows(engine)
        assert "verb" not in plain["repo:docs-index"]
        result = asyncio.run(_call(engine, "status_list", {"fields": ["verb", "rule_id"]}))
    rows = {r["key"]: r for r in result[2]["result"]["rows"]}
    assert (rows["repo:docs-index"]["verb"], rows["repo:docs-index"]["rule_id"]) == ("Merge", "queue")
    assert rows["repo:login-ttl"]["verb"] == "Approve"
    assert "action" not in rows["repo:docs-index"]
    bad = asyncio.run(_call(engine, "status_list", {"fields": ["nope"]}))[2]
    assert "error" in bad


def test_a_user_rules_facts_move_the_rows_age():
    from lazyagent.engine.status import fingerprint, user_facts

    cat, _, _ = catalog_of(("global", rule("mine", "agents.count > 0", ball="others", state="x")))
    base = Facts("repo:f", "repo", "f", "/f", agents=(AgentFact("a1", "waiting"),))
    two = Facts("repo:f", "repo", "f", "/f",
                agents=(AgentFact("a1", "waiting"), AgentFact("a2", "waiting")))

    def fp(facts):
        ctx = RowContext(facts, NOW, NOW, 24)
        derived = cat.evaluate(ctx)
        return fingerprint(facts, derived, user_facts(cat, ctx, derived.rule_id))

    assert user_facts(cat, RowContext(base, NOW, NOW, 24), "mine") == ["mine", [["agents.count", 1]]]
    assert fp(base) != fp(two)
    # The built-ins keep their inputs: no user part.
    assert user_facts(builtin(), RowContext(base, NOW, NOW, 24), "agent_idle") is None


# --- CLI ---------------------------------------------------------------------


def test_cli_rules_check_show_doc_and_allow(repo, capsys):
    from lazyagent.engine.cli import rules_main

    global_file().write_text(rule("mine") + rule("bad", "nope.fact"))
    path = repo / ".lazyagent" / "rules.toml"
    path.write_text(rule("repo_rule"))
    assert rules_main(["check", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "error   " in out and "rule bad" in out and "skipped" in out
    assert f"not allowed (lazyagent rules allow {digest(rule('repo_rule'))[:12]})" in out
    assert rules_main(["show", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "mine" in out and "global" in out and "error:" in out
    assert rules_main(["doc", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("# How lazyagent works")
    # Without a hash: the file and its hash, nothing allowed.
    assert rules_main(["allow", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "repo_rule" in out and digest(rule("repo_rule"))[:12] in out
    assert "repo_rule" not in RulesHub(str(repo)).catalog.by_id
    assert rules_main(["allow", str(repo), digest(rule("repo_rule"))[:12]]) == 0
    assert "allowed" in capsys.readouterr().out
    assert "repo_rule" in RulesHub(str(repo)).catalog.by_id
    global_file().write_text(rule("mine"))
    assert rules_main(["check", str(repo)]) == 0
    assert "no errors" in capsys.readouterr().out


def test_cli_status_why_formats_rules_explain(monkeypatch, capsys):
    from lazyagent.engine import cli

    engine = _status_engine()
    with _rules_files({}):
        explained = asyncio.run(_call(engine, "rules_explain", {"key": "docs-index"}))[2]["result"]
    calls = []

    async def request(socket, method, **params):
        calls.append((method, params))
        return explained

    monkeypatch.setattr(cli, "_request", request)
    monkeypatch.setattr(cli.launcher, "find", lambda root: Path("/sock"))
    assert cli.status_main(["/tmp/repo", "--why", "docs-index"]) == 0
    assert calls == [("rules_explain", {"key": "docs-index"})]
    out = capsys.readouterr().out
    assert out.startswith("repo:docs-index\n  rule   queue")
    assert "item   from rule merge" in out


def test_the_lazyagent_command_routes_rules(monkeypatch):
    from lazyagent import cli as top
    from lazyagent.engine import cli

    seen = []
    monkeypatch.setattr(cli, "rules_main", lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as exit_:
        top.main(["rules", "check"])
    assert exit_.value.code == 0 and seen == [["check"]]


def test_the_readme_examples_do_what_the_readme_says():
    import re

    readme = (Path(__file__).resolve().parents[3] / "README.md").read_text()
    section = readme.split("### Your rules (rules.toml)")[1].split("\n### ")[0]
    blocks = re.findall(r"```toml\n(.*?)```", section, re.S)
    assert len(blocks) == 2
    _, errors, _ = catalog_of(("global", blocks[0]))
    assert {e["rule"] for e in errors} == {"merge", "waiting_for_qa"}
    assert all("no fact 'tracker.qa'" in e["message"] for e in errors)
    assert errors[0]["fallback"] == "skipped; the built-in is in force"
    cat, errors, _ = catalog_of(("repo", blocks[1]))
    assert errors == [] and cat.by_id["merge"].and_when is not None
