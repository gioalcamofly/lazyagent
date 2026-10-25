"""The ``tracker.qa`` fact (proposal 0014 R3): ``[<adapter>.qa]`` in
trackers.toml for Jira (status, or ``qa_field``) and GitHub Issues (labels),
a feeder's ``facts``, the adapter winning over a feeder (Q-R15), and the
README's Merge gate: Merge appears only once QA is tested or not required,
never on an unknown. With no ``[*.qa]`` nothing changes. No network.

HOME and every XDG dir are under the test's tmp_path (conftest.py)."""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path

import pytest

from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.rules import builtin
from lazyagent.engine.rules.facts import FACTS, RowContext
from lazyagent.engine.rules.lang import UNKNOWN
from lazyagent.engine.rules.user import compose, parse_file
from lazyagent.engine.status import Facts
from lazyagent.engine.trackers import (
    QA_VALUES,
    StatusRule,
    TrackerConfig,
    TrackerFact,
    TrackerHub,
    config_dir,
    load_trackers,
)
from lazyagent.engine.trackers.github_issues import GithubIssues
from lazyagent.engine.trackers.jira import FIELDS, Jira
from lazyagent.engine.work_items import WorkItems
from lazyagent.models import CiCheck, PrInfo
from lazyagent.protocol import validation
from tests.engine.fakes import make_engine, worktree
from tests.engine.trackers.fake_gh import FakeGh, issue
from tests.engine.trackers.fake_jira import BASE_URL, FakeJira
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

NOW = 1_700_000_000.0
BRANCH = "SHOP-12-login-sso"
WT = "/tmp/repo-shop-12"
KEY = f"repo:{BRANCH}"
MERGE_ITEM = f"Rule:merge:{KEY}"
GREEN = [CiCheck("tests", "COMPLETED", "SUCCESS")]
RED = [CiCheck("tests", "COMPLETED", "FAILURE")]

JIRA_QA = {
    "QA passed": "tested",
    "Ready to ship": "tested",
    "No QA": "not_required",
    "In QA": "pending",
    "QA failed": "failed",
}


def readme_rules() -> str:
    """The owner's Merge gate, as the README writes it."""
    readme = (Path(__file__).resolve().parents[3] / "README.md").read_text()
    section = readme.split("### Your rules (rules.toml)")[1].split("\n### ")[0]
    return re.findall(r"```toml\n(.*?)```", section, re.S)[0]


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-api-token")


def jira_config(qa: dict[str, str] | None = None, **qa_keys) -> TrackerConfig:
    config = TrackerConfig(
        "jira", True, assigned_to_me=False,
        status={"In Review": StatusRule("others", "code review")},
        options={"base_url": BASE_URL, "email_env": "JIRA_EMAIL", "token_env": "JIRA_TOKEN"},
    )
    config.qa = dict(qa or {})
    for name, value in qa_keys.items():
        setattr(config, name, value)
    return config


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine(fake: FakeJira | None = None, config: TrackerConfig | None = None):
    """A repo with the SHOP-12 branch: its PR approved, green, mergeable."""
    engine = make_engine([worktree("/tmp/repo"), worktree(WT, BRANCH, is_main=False)])
    engine.worktree_service._pr_infos[WT] = PrInfo(
        12, "Login fails with SSO", "OPEN", GREEN, "https://example.com/pull/12",
        review_decision="APPROVED", mergeable="MERGEABLE",
    )
    clock = Clock()
    engine.status.clock = clock
    if fake is not None:
        config = config or jira_config(JIRA_QA)
        engine.trackers = TrackerHub([(Jira(config, "/tmp/repo", http=fake), config)],
                                     clock=clock)
        asyncio.run(engine.trackers.poll([BRANCH]))
    return engine, clock


def row(engine) -> dict:
    result = engine.status.list()
    validation.validate_result("status_list", result, strict=True)
    return {r["key"]: r for r in result["rows"]}[KEY]


def item_ids(engine) -> list[str]:
    return [i["id"] for i in engine.actionables.list()["items"]]


# --- trackers.toml ----------------------------------------------------------


def test_qa_tables_load_from_trackers_toml_and_a_repo_overrides_only_mappings(tmp_path, caplog):
    config_dir().mkdir(parents=True)
    (config_dir() / "trackers.toml").write_text(
        f'[jira]\nenabled = true\nbase_url = "{BASE_URL}"\n'
        '[jira.qa]\n"QA passed" = "tested"\n"In QA" = "pending"\n"Backlog" = "unknown"\n'
        '"Odd" = "maybe"\nqa_field = "customfield_10042"\n'
        'qa_field_values = { "Tested" = "tested", "N/A" = "not_required" }\n'
        '[github_issues]\nenabled = true\n[github_issues.qa]\n"qa-passed" = "tested"\n'
    )
    repo = tmp_path / "repo"
    (repo / ".lazyagent").mkdir(parents=True)
    (repo / ".lazyagent" / "trackers.toml").write_text(
        '[jira.qa]\n"In QA" = "failed"\n"Signed off" = "tested"\n'
        '[github_issues]\nenabled = false\ntoken_env = "STOLEN"\n'
        '[github_issues.qa]\n"no-qa" = "not_required"\n'
    )
    configs = {c.name: c for c in load_trackers(repo)}
    jira = configs["jira"]
    assert jira.qa == {"QA passed": "tested", "In QA": "failed", "Signed off": "tested"}
    assert jira.qa_field == "customfield_10042"
    assert jira.qa_field_values == {"Tested": "tested", "N/A": "not_required"}
    assert "qa" not in jira.options and "qa_field" not in jira.options
    assert "'Odd' = 'maybe' is not one of" in caplog.text
    gh = configs["github_issues"]
    assert gh.enabled and gh.qa == {"qa-passed": "tested", "no-qa": "not_required"}
    assert "token_env" not in gh.options


def test_a_bad_qa_field_is_ignored_with_a_warning(caplog):
    config_dir().mkdir(parents=True)
    (config_dir() / "trackers.toml").write_text(
        f'[jira]\nenabled = true\nbase_url = "{BASE_URL}"\n'
        '[jira.qa]\nqa_field = "summary) OR (1=1"\n'
    )
    [jira] = load_trackers()
    assert jira.qa_field is None and jira.qa == {}
    assert "bad qa_field" in caplog.text


# --- The adapters -----------------------------------------------------------


@pytest.mark.asyncio
async def test_jira_maps_the_status_name_else_its_category():
    fake = FakeJira("issue_qa_passed", "issue_in_review", "issue_reopened")
    jira = Jira(jira_config({**JIRA_QA, "In Progress": "pending"}), "/tmp/repo", http=fake)
    assert [t.qa for t in await jira.fetch(["SHOP-12"])] == ["tested"]
    # "In Review" is not mapped; its category "In Progress" is.
    assert [t.qa for t in await jira.fetch(["SHOP-12"])] == ["pending"]
    # "Reopened" (category "To Do"): nothing maps it.
    assert [t.qa for t in await jira.fetch(["SHOP-12"])] == [None]
    assert {r[1]["fields"] for r in fake.requests} == {FIELDS}


@pytest.mark.asyncio
async def test_jira_reads_qa_field_instead_of_the_status():
    fake = FakeJira("issue_qa_field", "issue_qa_passed")
    jira = Jira(
        jira_config(JIRA_QA, qa_field="customfield_10042",
                    qa_field_values={"Not required": "not_required", "Tested": "tested"}),
        "/tmp/repo", http=fake,
    )
    assert [t.qa for t in await jira.fetch(["SHOP-12"])] == ["not_required"]
    # The field is empty: unknown, even though the status "QA passed" is mapped.
    assert [t.qa for t in await jira.fetch(["SHOP-12"])] == [None]
    assert fake.requests[0][1]["fields"] == FIELDS + ",customfield_10042"


@pytest.mark.parametrize("value, qa", [
    ("Tested", "tested"),
    ("tested", "tested"),
    ({"value": "Tested", "id": "1"}, "tested"),
    ([{"value": "Other"}, {"value": "Not required"}], "not_required"),
    ([], None),
    ({"value": "Other"}, None),
    (7, None),
])
def test_jira_qa_field_shapes(value, qa):
    jira = Jira(
        jira_config(qa_field="customfield_10042",
                    qa_field_values={"Tested": "tested", "Not required": "not_required"}),
        "/tmp/repo", http=FakeJira(),
    )
    assert jira.qa_of({"customfield_10042": value}) == qa


@pytest.mark.asyncio
@pytest.mark.parametrize("status_from", ["labels", "state"])
async def test_github_issues_maps_labels_in_the_mapping_order(status_from):
    config = TrackerConfig("github_issues", True, options={"status_from": status_from})
    config.qa = {"qa-failed": "failed", "qa-passed": "tested", "qa": "pending"}
    gh = FakeGh(issue(1, labels=("QA", "qa-passed")), issue(2, labels=("bug",)),
                issue(3, labels=("qa", "qa-failed")))
    tasks = await GithubIssues(config, "/tmp/repo", run=gh).fetch(["1", "2", "3"])
    assert {t.key: t.qa for t in tasks} == {"1": "tested", "2": None, "3": "failed"}


@pytest.mark.asyncio
async def test_with_no_qa_mapping_no_task_has_qa():
    tasks = await GithubIssues(
        TrackerConfig("github_issues", True), "/tmp/repo", run=FakeGh(issue(1, labels=("qa",)))
    ).fetch(["1"])
    assert [t.qa for t in tasks] == [None]


# --- The fact ---------------------------------------------------------------


def test_the_fact_is_registered_for_rows_with_its_values():
    fact = FACTS["tracker.qa"]
    assert fact.values == QA_VALUES == ("tested", "not_required", "pending", "failed")
    assert fact.row is not None and fact.item is None
    assert (fact.cost, fact.freshness) == ("poll", "poll_s")
    assert fact.phrase.startswith("the task's QA")


def _facts(tracker_qa: str | None = "absent", feeder: dict | None = None) -> Facts:
    tracker = None if tracker_qa == "absent" else TrackerFact(
        "jira", "SHOP-12", "", "", "QA passed", None, "QA passed", None, NOW, False,
        qa=tracker_qa)
    return Facts(key=KEY, repo="repo", branch=BRANCH, worktree_path=WT, tracker=tracker,
                 feeder=feeder)


def _read(facts: Facts, name: str):
    return RowContext(facts, NOW, NOW, 24).fact(name)


def test_the_adapter_wins_over_a_feeder_and_a_feeder_fills_in_for_no_adapter():
    feeder = {"url": "https://t.example.com/1",
              "facts": {"tracker.qa": "tested", "tracker.status": "Ready to ship"}}
    assert _read(_facts("pending", feeder), "tracker.qa") == "pending"
    assert _read(_facts("pending", feeder), "tracker.status") == "QA passed"
    # Linked, but nothing maps the task's QA: the feeder's.
    assert _read(_facts(None, feeder), "tracker.qa") == "tested"
    # No adapter task.
    assert _read(_facts("absent", feeder), "tracker.qa") == "tested"
    assert _read(_facts("absent", feeder), "tracker.status") == "Ready to ship"
    assert _read(_facts("absent", feeder), "tracker.linked") is True
    # Neither.
    assert _read(_facts("absent", {"url": "x"}), "tracker.qa") is UNKNOWN
    assert _read(_facts("absent", {"url": "x"}), "tracker.linked") is False
    assert _read(_facts("absent"), "tracker.status") is UNKNOWN
    assert _read(_facts(None), "tracker.qa") is UNKNOWN


def _catalog():
    composed = compose(builtin(), [parse_file(readme_rules(), "global", GLOBAL_RULES)])
    assert composed.errors == []
    return composed.catalog


def test_an_unknown_qa_never_makes_merge_match():
    """Three-valued: over seeded rows that are otherwise ready to merge, the
    README's merge rule matches exactly when QA is tested or not required."""
    catalog = _catalog()
    merge = catalog.by_id["merge"]
    rng = random.Random(14)
    seen = set()
    for _ in range(400):
        review = rng.choice(["APPROVED", "APPROVED", "CHANGES_REQUESTED", ""])
        checks = rng.choice([GREEN, [], RED])
        pr = PrInfo(12, "t", "OPEN", checks, "u", review_decision=review,
                    mergeable=rng.choice(["MERGEABLE", "MERGEABLE", "CONFLICTING", "UNKNOWN"]))
        adapter = rng.choice(["absent", None, *QA_VALUES])
        feeder = rng.choice([None, {"url": "x"}, *({"url": "x", "facts": {"tracker.qa": q}}
                                                   for q in QA_VALUES)])
        facts = Facts(
            key=KEY, repo="repo", branch=BRANCH, worktree_path=WT, pr=pr,
            tracker=_facts(adapter).tracker, feeder=feeder, actionables=None,
        )
        ctx = RowContext(facts, NOW, NOW, 24)
        qa = ctx.fact("tracker.qa")
        matched = merge.test(ctx) is True
        ready = review == "APPROVED" and pr.mergeable == "MERGEABLE" and checks is not RED
        if qa is UNKNOWN or qa in ("pending", "failed"):
            assert not matched, (qa, facts)
        elif matched:
            assert ready and qa in ("tested", "not_required")
        seen.add((qa if qa is not UNKNOWN else "?", matched))
    assert ("?", False) in seen and ("tested", True) in seen and ("not_required", True) in seen


# --- The owner's Merge gate, end to end -------------------------------------


def test_a_jira_status_mapped_to_tested_makes_the_owners_merge_rule_match():
    engine, _ = _engine(FakeJira("issue_qa_passed"))
    with _rules_files({GLOBAL_RULES: readme_rules()}):
        r = row(engine)
        assert r["tracker"]["qa"] == "tested"
        assert (r["zone"], r["state"], r["needs_you"]["top"]) == (
            "inbox", "merge: approved, QA ok", MERGE_ITEM)
        item = engine.actionables.get(MERGE_ITEM)
        assert (item["verb"], item["rule_id"]) == ("Merge", "merge")


def test_a_pending_qa_reads_waiting_for_qa_and_an_unmapped_one_has_no_merge():
    engine, _ = _engine(FakeJira("issue_in_qa"))
    with _rules_files({GLOBAL_RULES: readme_rules()}):
        r = row(engine)
        assert (r["ball"], r["state"], r["tracker"]["qa"]) == (
            "others", "approved · waiting for QA", "pending")
        assert MERGE_ITEM not in item_ids(engine)

    # "In Review" is not in [jira.qa]: unknown, no Merge, and not "waiting
    # for QA" either (that rule's condition is unknown too).
    engine, _ = _engine(FakeJira("issue_in_review"))
    with _rules_files({GLOBAL_RULES: readme_rules()}):
        r = row(engine)
        assert "qa" not in r["tracker"]
        assert MERGE_ITEM not in item_ids(engine)
        assert r["needs_you"] is None and r["state"] != "approved · waiting for QA"


async def _upsert(engine, **kw):
    return await EngineMethods(engine).work_items_upsert({
        "url": "https://planner.example.com/SHOP-12", "title": "Login fails with SSO",
        "source": "planner", "ticket": "SHOP-12", **kw,
    })


def test_a_feeders_not_required_does_the_same_for_a_row_with_no_adapter():
    engine, _ = _engine()
    assert not engine.trackers.enabled
    with _rules_files({GLOBAL_RULES: readme_rules()}):
        assert MERGE_ITEM not in item_ids(engine)
        result = asyncio.run(_upsert(engine, facts={"tracker.qa": "not_required"}))
        assert result["key"] == KEY
        assert result["item"]["facts"] == {"tracker.qa": "not_required"}
        r = row(engine)
        assert (r["zone"], r["state"], r["needs_you"]["top"]) == (
            "inbox", "merge: approved, QA ok", MERGE_ITEM)
        assert "tracker" not in r
        # The next upsert without facts clears them: no Merge.
        asyncio.run(_upsert(engine))
        assert MERGE_ITEM not in item_ids(engine)


def test_the_adapters_qa_wins_over_the_feeders():
    engine, _ = _engine(FakeJira("issue_in_qa"))
    with _rules_files({GLOBAL_RULES: readme_rules()}):
        asyncio.run(_upsert(engine, facts={"tracker.qa": "tested"}))
        r = row(engine)
        assert (r["state"], r["tracker"]["qa"]) == ("approved · waiting for QA", "pending")
        assert MERGE_ITEM not in item_ids(engine)


def test_with_no_qa_mapping_the_output_is_byte_identical():
    """A Jira adapter with no [jira.qa] and one whose mapping names nothing
    this task has: the same bytes, the same request, and no qa anywhere."""

    def dump(config):
        fake = FakeJira("issue_in_review")
        engine, _ = _engine(fake, config)
        out = json.dumps(engine.status.list(include_done=True), sort_keys=True)
        items = json.dumps(engine.actionables.list(), sort_keys=True)
        return out, items, fake.requests[0][1]["fields"]

    plain = dump(jira_config())
    assert plain == dump(jira_config({"QA passed": "tested"}))
    assert plain[2] == FIELDS
    assert '"qa"' not in plain[0]


# --- The feeder's facts -----------------------------------------------------


@pytest.mark.parametrize("facts, message", [
    ("tested", "facts must be an object"),
    ({"tracker.ball": "you"}, "no fact 'tracker.ball' a feeder can give"),
    ({"pr.review": "approved"}, "no fact 'pr.review'"),
    ({"tracker.qa": "passed"}, "tracker.qa is never 'passed'"),
    ({"tracker.status": 3}, "facts.tracker.status must be a string"),
])
def test_feeder_facts_refuse_other_names_and_values(facts, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        WorkItems(clock=lambda: NOW).upsert(
            {"url": "https://t.example.com/1", "title": "x", "source": "p", "facts": facts}, NOW)


def test_feeder_facts_null_or_unknown_stay_unknown_and_none_means_no_key():
    items = WorkItems(clock=lambda: NOW)
    record, _ = items.upsert({"url": "https://t.example.com/1", "title": "x", "source": "p",
                              "facts": {"tracker.qa": "unknown", "tracker.status": None}}, NOW)
    assert "facts" not in record
    record, _ = items.upsert({"url": "https://t.example.com/1", "title": "x", "source": "p",
                              "facts": {"tracker.qa": "failed", "tracker.status": "  QA  "}}, NOW)
    assert record["facts"] == {"tracker.qa": "failed", "tracker.status": "QA"}
    validation.validate_result(
        "work_items_upsert", {"item": record, "created": False, "key": None}, strict=True)
    validation.validate_params(
        "work_items_upsert",
        {"url": "https://t.example.com/1", "title": "x", "source": "p",
         "facts": {"tracker.qa": "unknown"}},
    )


def test_the_readme_trackers_qa_example_loads():
    readme = (Path(__file__).resolve().parents[3] / "README.md").read_text()
    section = readme.split("### Issue trackers (optional)")[1].split("\n### ")[0]
    block = next(b for b in re.findall(r"```toml\n(.*?)```", section, re.S) if "[jira.qa]" in b)
    config_dir().mkdir(parents=True)
    (config_dir() / "trackers.toml").write_text(
        f'[jira]\nenabled = true\nbase_url = "{BASE_URL}"\n'
        "[github_issues]\nenabled = true\n" + block
    )
    configs = {c.name: c for c in load_trackers()}
    assert configs["jira"].qa == JIRA_QA
    assert list(configs["github_issues"].qa) == ["qa-failed", "qa-passed", "no-qa", "qa"]
