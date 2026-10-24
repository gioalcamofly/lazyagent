"""Work items with the GitHub Issues adapter on (fake ``gh``): rows gain the
linked issue's state, a mapped "returned" label makes a Round item, assigned
issues with no branch count as "not started", and with no adapter the
answer is what it was."""

from __future__ import annotations

import asyncio
import json

import pytest

from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.status import format_table
from lazyagent.engine.trackers import (
    StatusRule,
    TrackerConfig,
    TrackerError,
    TrackerHub,
    config_dir,
)
from lazyagent.engine.trackers.github_issues import GithubIssues
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo
from lazyagent.protocol import validation
from tests.engine.fakes import add_session, make_engine, worktree
from tests.engine.trackers.fake_gh import FakeGh, issue

NOW = 1_700_000_000.0
FIX = "/tmp/repo-fix"
FEAT = "/tmp/repo-feat"

STATUS = {
    "qa": StatusRule("others", "in QA"),
    "qa-returned": StatusRule("you", "returned by QA", "round"),
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine(gh: FakeGh | None):
    engine = make_engine(
        [
            worktree("/tmp/repo"),
            worktree(FIX, "fix-123-login", is_main=False),
            worktree(FEAT, "feat", is_main=False),
        ]
    )
    clock = Clock()
    engine.status.clock = clock
    if gh is not None:
        config = TrackerConfig("github_issues", True, status=dict(STATUS))
        engine.trackers = TrackerHub(
            [(GithubIssues(config, "/tmp/repo", run=gh), config)], clock=clock
        )
    return engine, clock


def poll(engine, clock, seconds: float = 0.0):
    clock.now += seconds
    branches = [wt.branch for wt in engine.worktrees if wt.branch]
    return asyncio.run(engine.trackers.poll(branches))


def listing(engine, **params):
    result = engine.status.list(**params)
    validation.validate_result("status_list", result, strict=True)
    return result


def rows(engine):
    return {r["key"]: r for r in listing(engine)["rows"]}


def test_a_returned_label_yields_a_round_item_and_leaving_clears_it():
    gh = FakeGh(issue(123, "Login fails", labels=("qa",)))
    engine, clock = _engine(gh)
    poll(engine, clock)
    row = rows(engine)["repo:fix-123-login"]
    assert (row["ball"], row["zone"], row["state"]) == ("others", "in_flight", "in QA")
    assert row["title"] == "Login fails"
    assert row["urls"] == ["https://github.com/acme/widgets/issues/123"]
    assert row["tracker"] == {
        "adapter": "github_issues", "key": "123",
        "url": "https://github.com/acme/widgets/issues/123",
        "status": "qa", "state": "in QA", "at": NOW, "stale": False,
    }

    gh.set(123, labels=["qa", "qa-returned"])
    poll(engine, clock, 300)
    row = rows(engine)["repo:fix-123-login"]
    assert (row["ball"], row["zone"], row["level"], row["state"]) == (
        "you", "inbox", "round", "returned by QA",
    )
    assert engine.trackers.items[("github_issues", "123")].id == (
        "tracker:github_issues:123:qa-returned"
    )
    assert listing(engine)["counts"]["round"] == 1
    assert row["needs_you"] == {"count": 1, "top": "tracker:github_issues:123:qa-returned"}
    [item] = engine.actionables.list()["items"]
    assert (item["kind"], item["level"], item["state"], item["work_item"]) == (
        "tracker_status", "round", "returned by QA", "repo:fix-123-login",
    )
    assert (item["label"], item["detail"]) == (
        "Login fails", "https://github.com/acme/widgets/issues/123",
    )
    validation.validate_result("actionables_list", engine.actionables.list(), strict=True)

    # Dismissed: it stays dismissed while the issue stays returned; the
    # row keeps the fact, out of the inbox.
    engine.actionables.mark(item["id"], "dismiss")
    poll(engine, clock, 300)
    row = rows(engine)["repo:fix-123-login"]
    assert (row["zone"], row["needs_you"], row["tracker"]["status"]) == (
        "in_flight", None, "qa-returned",
    )
    assert engine.actionables.list()["items"] == []

    gh.set(123, labels=["qa"])
    poll(engine, clock, 300)
    assert rows(engine)["repo:fix-123-login"]["ball"] == "others"
    assert not engine.trackers.items

    # Entering again: a new occurrence, back in the inbox.
    gh.set(123, labels=["qa-returned"])
    poll(engine, clock, 300)
    assert rows(engine)["repo:fix-123-login"]["level"] == "round"


def test_engine_facts_win_the_ball_and_the_tracker_text_shows_beside():
    gh = FakeGh(issue(123, labels=("qa",)))
    engine, clock = _engine(gh)
    engine.registry.states.setdefault(FIX, {})["a1"] = AgentState(
        status=AgentStatus.RUNNING, agent_id="a1", detail="Bash"
    )
    from unittest.mock import MagicMock

    session = MagicMock()
    session.worktree_path, session.agent_id = FIX, "a1"
    add_session(engine, session)
    engine.worktree_service._pr_infos[FIX] = PrInfo(
        9, "Fix login", "OPEN", [CiCheck("tests", "COMPLETED", "SUCCESS")],
        "https://github.com/acme/widgets/pull/9",
    )
    poll(engine, clock)
    row = rows(engine)["repo:fix-123-login"]
    assert (row["ball"], row["state"]) == ("agent", "working")
    assert row["title"] == "Fix login"  # the PR title first
    assert row["urls"] == [
        "https://github.com/acme/widgets/pull/9",
        "https://github.com/acme/widgets/issues/123",
    ]
    table = format_table(listing(engine), NOW)
    assert "working · in QA" in table

    # An inbox item is the queue's: it puts the row in the inbox even while
    # the agent works.
    gh.set(123, labels=["qa-returned"])
    poll(engine, clock, 300)
    row = rows(engine)["repo:fix-123-login"]
    assert (row["ball"], row["level"], row["rule"]) == ("you", "round", 2)


def test_unmapped_status_shows_raw_text_with_the_ball_unchanged():
    gh = FakeGh(issue(123, state="CLOSED"))
    engine, clock = _engine(gh)
    poll(engine, clock)
    row = rows(engine)["repo:fix-123-login"]
    assert (row["ball"], row["state"], row["tracker"]["state"]) == (
        "stalled", "no agent", "closed",
    )
    assert "no agent · closed" in format_table(listing(engine), NOW)


def test_a_tracker_error_keeps_the_last_value_with_its_age():
    gh = FakeGh(issue(123, labels=("qa",)))
    engine, clock = _engine(gh)
    poll(engine, clock)
    gh.graphql_error = "gh: HTTP 502"
    poll(engine, clock, 600)
    engine.status.clock.now = clock.now
    row = rows(engine)["repo:fix-123-login"]
    assert (row["ball"], row["tracker"]["stale"], row["tracker"]["at"]) == ("others", True, NOW)
    assert "in QA (10m ago)" in format_table(listing(engine), clock.now)


def test_assigned_issues_with_no_branch_are_not_started():
    gh = FakeGh(
        issue(123, assignees=("octo",)),  # has a worktree
        issue(124, assignees=("octo",)),  # has a local branch only
        issue(125, assignees=("octo",)),
        issue(126, assignees=("someone",)),
    )
    engine, clock = _engine(gh)
    engine.status._commit_times = {"main": NOW, "fix-123-login": NOW, "124-dark-mode": NOW}
    poll(engine, clock)
    result = listing(engine)
    assert result["counts"]["not_started"] == 1
    assert format_table(result, NOW).splitlines()[0].endswith("NOT STARTED 1")
    # Not rows.
    assert set(rows(engine)) == {"repo:fix-123-login", "repo:feat"}


def test_with_no_adapter_status_list_is_byte_identical():
    plain, _ = _engine(None)
    # On, but nothing linked or assigned: the same bytes too.
    quiet, clock = _engine(FakeGh(issue(1)))
    quiet.world.worktrees = [wt for wt in quiet.world.worktrees if wt.path != FIX]
    quiet.worktree_service.worktrees = list(quiet.world.worktrees)
    plain.world.worktrees = list(quiet.world.worktrees)
    plain.worktree_service.worktrees = list(quiet.world.worktrees)
    poll(quiet, clock)

    def dump(engine):
        return json.dumps(engine.status.list(include_done=True), sort_keys=True)

    assert not plain.trackers.enabled
    assert dump(plain) == dump(quiet)
    assert "tracker" not in dump(plain)
    assert format_table(plain.status.list(), NOW) == format_table(quiet.status.list(), NOW)


@pytest.mark.asyncio
async def test_the_engine_loads_trackers_from_the_global_config():
    (config_dir()).mkdir(parents=True)
    (config_dir() / "trackers.toml").write_text(
        '[github_issues]\nenabled = true\n[github_issues.status]\n"qa" = { ball = "others" }\n'
    )
    engine, _ = _engine(None)
    engine._tracker_loader = __import__(
        "lazyagent.engine.trackers", fromlist=["load_trackers"]
    ).load_trackers
    engine.load_config()
    assert engine.trackers.enabled
    [feed] = engine.trackers._feeds
    assert isinstance(feed.adapter, GithubIssues) and feed.config.status["qa"].ball == "others"


@pytest.mark.asyncio
async def test_the_projection_polls_on_its_tick_and_refreshes_rows(monkeypatch):
    from lazyagent.engine import status as status_mod

    monkeypatch.setattr(status_mod, "COALESCE", 0.01)
    gh = FakeGh(issue(123, labels=("qa",)))
    engine, clock = _engine(gh)
    engine.status.start()
    try:
        await engine.wait_idle()
        await asyncio.sleep(0.05)
        assert engine.status._rows["repo:fix-123-login"]["ball"] == "others"
        gh.set(123, labels=["qa-returned"])
        clock.now += 300
        engine.status.tick()
        await engine.wait_idle()
        await asyncio.sleep(0.05)
        assert engine.status._rows["repo:fix-123-login"]["level"] == "round"
        result = await EngineMethods(engine).status_list({})
        assert result["counts"]["round"] == 1
    finally:
        engine.status.stop()


def test_a_failing_adapter_never_breaks_the_rows():
    class Broken:
        name = "broken"

        def key_pattern(self):
            return None

        async def fetch(self, keys):
            raise TrackerError("down")

        async def assigned_to_me(self):
            raise TrackerError("down")

    engine, clock = _engine(None)
    engine.trackers = TrackerHub([(Broken(), TrackerConfig("broken", True))], clock=clock)
    engine.world.worktrees.append(worktree("/tmp/repo-x", "ABC-1-x", is_main=False))
    engine.worktree_service.worktrees = list(engine.world.worktrees)
    poll(engine, clock)
    assert rows(engine)["repo:ABC-1-x"]["ball"] == "stalled"
    assert "tracker" not in rows(engine)["repo:ABC-1-x"]


def test_a_now_mapping_makes_a_now_item():
    gh = FakeGh(issue(123, labels=("blocker",)))
    engine, clock = _engine(gh)
    [(_, config)] = [(f.adapter, f.config) for f in engine.trackers._feeds]
    config.status["blocker"] = StatusRule("you", "blocks a release", "now")
    poll(engine, clock)
    [item] = engine.actionables.list()["items"]
    assert (item["kind"], item["level"]) == ("tracker_status", "now")
    row = rows(engine)["repo:fix-123-login"]
    assert (row["level"], row["state"]) == ("now", "blocks a release")
