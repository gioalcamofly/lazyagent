"""GitHub failing (github_health.py): the backoff, the rows that keep their
PR facts, ``status_list.github`` and ``github.updated``, and the Actions and
review requests polls that come back with it. Never the real gh; HOME and
the XDG dirs are the test's (conftest.py)."""

from __future__ import annotations

import pytest

from lazyagent.engine.actions import ActionsHub
from lazyagent.engine.github_health import BACKOFF_MAX, GithubHealth, backoff, short
from lazyagent.engine.review_requests import ReviewRequestHub
from lazyagent.engine.rules.facts import RowContext
from lazyagent.engine.rules.lang import UNKNOWN
from lazyagent.models import AgentState, AgentStatus, PrInfo
from lazyagent.protocol import validation
from lazyagent.worktree_manager import GithubRepo

from tests.engine.fake_gh import FakeGh, run_json
from tests.engine.fake_search import FakeSearch, pr_node
from tests.engine.fakes import make_engine, worktree

NOW = 1_800_000_000.0
FEAT = "/tmp/repo-feat"
OFFLINE = "error connecting to api.github.com"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine():
    engine = make_engine([worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)])
    clock = Clock()
    engine.status.clock = clock
    engine.actionables.clock = clock
    engine.worktree_service.clock = clock
    engine.worktree_service.github.set_installed(True)
    return engine, clock


def _finished(engine, path=FEAT):
    engine.registry.states.setdefault(path, {})["a1"] = AgentState(
        status=AgentStatus.COMPLETED, agent_id="a1")


def _rows(engine):
    result = engine.status.list()
    validation.validate_result("status_list", result, strict=True)
    return result, {r["key"]: r for r in result["rows"]}


async def _pass(engine):
    engine.status.flush()
    engine.status.tick()
    await engine.wait_idle()
    engine.status.flush()


# --- The state ------------------------------------------------------------------


def test_backoff_doubles_from_a_minute_up_to_ten():
    assert [backoff(n) for n in range(1, 8)] == [60, 120, 240, 480, 600, 600, 600]
    assert BACKOFF_MAX == 600


def test_failures_in_the_same_round_count_once():
    clock = Clock()
    health = GithubHealth(clock)
    health.failed("a", NOW)
    health.failed("b", NOW + 5)  # another poll, same round
    assert health.failures == 1 and health.retry_at == NOW + 60
    assert not health.due(NOW + 20) and health.due(NOW + 55)
    health.failed("c", NOW + 60)
    assert health.failures == 2 and health.retry_at == NOW + 180


def test_the_error_is_one_short_line():
    assert short("a\n  b\tc") == "a b c"
    assert len(short("x" * 1000)) == 200


def test_changes_are_announced_once_each():
    seen = []
    health = GithubHealth(Clock())
    health.on_change = lambda: seen.append(health.to_dict()["available"])
    health.succeeded()
    health.failed("down", NOW)
    health.failed("down", NOW + 60)
    health.succeeded()
    assert seen == [False, True]


# --- The rows -------------------------------------------------------------------


def test_status_list_says_whether_github_answers_and_the_event_follows():
    engine, clock = _engine()
    events = []
    engine.subscribe(events.append, ["github.updated"])
    result, _ = _rows(engine)
    assert result["github"] == {"available": True}

    engine.worktree_service.github.failed(OFFLINE)
    result, _ = _rows(engine)
    assert result["github"] == {"available": False, "since": NOW, "error": OFFLINE,
                                "retry_at": NOW + 60}
    [event] = events
    assert event.data == result["github"]
    validation.validate_event("github.updated", event.data)


def test_a_startup_outage_does_not_make_a_finished_agent_look_prless():
    engine, _ = _engine()
    _finished(engine)
    engine.worktree_service.github.failed(OFFLINE)
    _, rows = _rows(engine)
    row = rows["repo:feat"]
    assert row["state"] != "review: agent finished"
    assert not any(i["kind"] == "done" for i in engine.actionables.list()["items"])
    facts = engine.status.facts("repo:feat")
    assert RowContext(facts, NOW, NOW, 24).fact("pr.exists") is UNKNOWN
    assert RowContext(facts, NOW, NOW, 24).fact("pr.state") is UNKNOWN

    # Control: GitHub answers and the branch has no PR.
    engine.worktree_service.github.succeeded()
    engine.worktree_service._pr_infos[FEAT] = None
    _, rows = _rows(engine)
    assert rows["repo:feat"]["state"] == "review: agent finished"


def test_rows_keep_the_last_pr_facts_during_an_outage():
    engine, _ = _engine()
    _finished(engine)
    engine.worktree_service._pr_infos[FEAT] = PrInfo(7, "Feat", "OPEN", [], url="u")
    _, before = _rows(engine)
    engine.worktree_service.github.failed(OFFLINE)
    _, during = _rows(engine)
    row = during["repo:feat"]
    assert row["pr"]["number"] == 7
    assert (row["ball"], row["state"]) == (before["repo:feat"]["ball"],
                                          before["repo:feat"]["state"])
    assert row["ball"] != "stalled"


# --- The other polls ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_review_requests_search_backs_off_and_recovers():
    engine, clock = _engine()
    gh = FakeSearch([pr_node(12, "alice-fix", requested_at=NOW - 3600)])
    engine.review_requests = ReviewRequestHub("/tmp/repo", run=gh, clock=clock)
    engine.worktree_service._github_repo = GithubRepo("o", "r")
    gh.queue = [(1, "", OFFLINE)]
    await _pass(engine)
    assert len(gh.calls) == 1 and not engine.worktree_service.github.available

    clock.now += 30  # its own cadence would not call yet anyway
    await _pass(engine)
    assert len(gh.calls) == 1

    clock.now += 30
    await _pass(engine)
    assert len(gh.calls) == 2, "the retry came"
    assert engine.worktree_service.github.available
    assert engine.review_requests.known


@pytest.mark.asyncio
async def test_actions_are_not_switched_off_by_an_early_failure():
    engine, clock = _engine()
    gh = FakeGh([run_json(11, "feat", "a" * 40)])
    hub = ActionsHub("/tmp/repo", poll_s=60, run=gh, read_heads=lambda: {"feat": "a" * 40},
                     clock=clock)
    engine.actions = hub
    health = engine.worktree_service.github
    health.failed(OFFLINE, NOW - 60)  # the PR poller's first round failed
    health.failed(OFFLINE, NOW)       # and its first retry: next in 2 minutes
    engine.status._poll_actions()
    await engine.wait_idle()
    assert gh.calls == []

    clock.now += 120
    engine.status._poll_actions()
    await engine.wait_idle()
    assert len(gh.runs_calls()) == 1 and [r.id for r in hub.runs] == [11]
    assert health.available


@pytest.mark.asyncio
async def test_an_actions_call_that_gets_no_answer_counts_as_an_outage():
    engine, clock = _engine()
    gh = FakeGh([])
    hub = ActionsHub("/tmp/repo", poll_s=60, run=gh, read_heads=lambda: {"feat": "a" * 40},
                     clock=clock)
    engine.actions = hub
    gh.queue = [(1, "", "error connecting to api.github.com")]
    engine.status._poll_actions()
    await engine.wait_idle()
    assert not engine.worktree_service.github.available
    assert hub.error is not None
