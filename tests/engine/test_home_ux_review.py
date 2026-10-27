"""Home UX fixes from a second UX review (proposal 0013, I4f): a deep item
under a quicker top one still counts in To sit down with, ages from real
activity (never the engine's start), stale rows with their dirty files,
parked rows that say until when, a detached head named by its directory,
the red runs of a busy head, checks that are not Actions, the timeline after
a restart, and the Why? answer in words."""

from __future__ import annotations

import pytest

from lazyagent.config import Config, OrchestratorConfig
from lazyagent.engine import actions
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.rules import doc
from lazyagent.engine.status import format_table, item_name
from lazyagent.models import CiCheck, GitStatus, PrInfo
from tests.engine.fake_gh import SHA_A, FakeGh, jobs_json, run_json
from tests.engine.fakes import make_engine, worktree
from tests.engine.test_actions import _hub

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 86400.0
FEAT = "/tmp/repo-feat"
KEY = "repo:feat"
RED = [CiCheck("tests", "COMPLETED", "FAILURE")]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _engine(*extra):
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False), *extra],
        config=Config(orchestrator=OrchestratorConfig()),
    )
    clock = Clock()
    engine.status.clock = clock
    return engine, clock


def _pr(checks=(), **kw):
    return PrInfo(7, "Feat", "OPEN", list(checks), "https://example.com/pull/7", **kw)


def _rows(engine):
    return {r["key"]: r for r in engine.status.list(include_done=True)["rows"]}


# --- 1. A deep item under a round top ---------------------------------------------


async def test_changes_requested_under_a_red_ci_counts_in_to_sit_down_with():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED, review_decision="CHANGES_REQUESTED")
    listing = engine.status.list()
    [row] = [r for r in listing["rows"] if r["key"] == KEY]
    # The red CI leads the row (Round); the review waits under it.
    assert (row["level"], row["needs_you"]["count"]) == ("round", 2)
    assert [(i["kind"], i["level"]) for i in row["needs_you"]["items"]] == [
        ("ci", "round"), ("review", "deep"),
    ]
    assert all(i["since"] and i["state"] for i in row["needs_you"]["items"])
    # Counted where the user sits down with it, as the desktop shows it.
    assert (listing["counts"]["round"], listing["counts"]["deep"]) == (1, 1)


async def test_a_parked_row_with_a_deep_item_is_not_counted_in_the_inbox():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED, review_decision="CHANGES_REQUESTED")
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "park", "until": "indefinite"})
    listing = engine.status.list()
    assert listing["counts"]["deep"] == 0
    assert listing["counts"]["parked"] == 1


# --- 2. Ages from real activity ---------------------------------------------------


async def test_a_merge_item_found_at_the_start_dates_from_the_last_activity():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - 20 * DAY}
    engine.status.list()  # the first pass: no PR info yet
    clock.now += 60  # the PR poller's first answer
    engine.worktree_service._pr_infos[FEAT] = _pr(
        [CiCheck("tests", "COMPLETED", "SUCCESS")], review_decision="APPROVED", mergeable="MERGEABLE",
    )
    row = _rows(engine)[KEY]
    [item] = engine.actionables.list()["items"]
    assert item["kind"] == "rule"
    assert item["since"] == NOW - 20 * DAY, "approved for 20 days, not since the engine started"
    assert row["needs_you"]["items"][0]["since"] == NOW - 20 * DAY


async def test_an_item_that_happens_while_the_engine_runs_dates_from_then():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - 20 * DAY}
    engine.worktree_service._pr_infos[FEAT] = _pr()
    engine.status.list()
    clock.now += 600
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    engine.status.list()
    [item] = engine.actionables.list()["items"]
    assert (item["kind"], item["since"]) == ("ci", NOW + 600)


async def test_an_in_flight_row_whose_pr_info_comes_late_keeps_its_age():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - 3 * DAY}
    engine.worktree_service._pr_infos[FEAT] = None
    engine.status.list()
    clock.now += 60
    engine.worktree_service._pr_infos[FEAT] = _pr(review_requests=1, reviews=0)
    row = _rows(engine)[KEY]
    assert (row["ball"], row["changed_at"]) == ("others", NOW - 3 * DAY)
    # A real change later is now.
    clock.now += 600
    engine.worktree_service._pr_infos[FEAT] = _pr(RED, review_requests=1, reviews=0)
    assert _rows(engine)[KEY]["changed_at"] == NOW + 660


async def test_a_parked_row_is_aged_from_its_park_and_says_until_when():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - 3 * DAY}
    engine.worktree_service._pr_infos[FEAT] = _pr(mergeable="CONFLICTING")
    engine.status.list()
    clock.now += 5 * HOUR
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "park", "until": "indefinite"})
    clock.now += HOUR
    row = _rows(engine)[KEY]
    assert (row["zone"], row["state"], row["changed_at"]) == (
        "parked", "parked indefinitely", NOW + 5 * HOUR,
    )
    # Its item is parked with it (R4c).
    assert row["needs_you"] is None
    assert [i["state"] for i in engine.actionables.list(include_hidden=True)["items"]] == [
        "conflict with base"]


# --- Stale rows and detached heads ------------------------------------------------


async def test_a_stale_row_carries_its_dirty_files_and_is_counted():
    old = worktree("/tmp/repo-old", "old", is_main=False)
    engine, _ = _engine(old)
    engine.status._commit_times = {"feat": NOW, "old": NOW - 60 * DAY}
    engine.worktree_service.git_statuses["/tmp/repo-old"] = GitStatus(dirty_count=13)
    listing = engine.status.list(include_done=True)
    old_row = next(r for r in listing["rows"] if r["key"] == "repo:old")
    assert (old_row["stale"], old_row["dirty"]) == (True, 13)
    assert "dirty" not in next(r for r in listing["rows"] if r["key"] == KEY)
    assert listing["counts"]["stale"] == 1
    # Hidden from the plain list, as before; the desktop folds them.
    assert all(r["key"] != "repo:old" for r in engine.status.list()["rows"])


async def test_a_detached_head_is_named_by_its_directory_and_aged_by_its_commit():
    detached = worktree("/tmp/repo-hotfix-premerge-6bf0c80", None, is_main=False)
    engine, _ = _engine(detached)
    engine.status._head_times[detached.head] = NOW - 2 * DAY
    row = next(r for r in _rows(engine).values() if r["branch"] is None and r["worktree_path"])
    assert item_name(row) == "repo-hotfix-premerge-6bf0c80"
    assert row["changed_at"] == NOW - 2 * DAY
    assert "repo-hotfix-premerge-6bf0c80" in format_table(engine.status.list(include_done=True), NOW)


# --- 4. The red runs of a busy head (I7) ------------------------------------------


def _busy_gh() -> FakeGh:
    """The real case: the repo's latest runs are newer green runs of a
    quick workflow; the head's red runs are older than the window."""
    window = [
        run_json(900 + i, "feat", SHA_A, conclusion="success", workflow="Quick lint",
                 workflow_id=3, created=f"2026-10-24T12:{i:02d}:00Z")
        for i in range(5)
    ]
    gh = FakeGh(window)
    gh.head_runs[SHA_A] = [
        *window,
        run_json(801, "feat", SHA_A, workflow="label-gate", workflow_id=1,
                 created="2026-10-24T11:00:00Z"),
        run_json(802, "feat", SHA_A, workflow="evals", workflow_id=2, created="2026-10-24T10:00:00Z"),
        run_json(799, "feat", SHA_A, conclusion="success", workflow="label-gate", workflow_id=1,
                 created="2026-10-24T09:00:00Z"),
    ]
    gh.per_page = 3
    gh.jobs[801] = jobs_json(("check", "Require a label"))
    gh.jobs[802] = jobs_json(("evals", "Run the evals"))
    return gh


def _red_pr():
    return PrInfo(7, "Feat", "OPEN", [
        CiCheck("label-gate", "COMPLETED", "FAILURE", "https://github.com/o/r/actions/runs/801/job/1"),
        CiCheck("evals", "COMPLETED", "FAILURE", "https://github.com/o/r/actions/runs/802/job/2"),
        CiCheck("Quick lint", "COMPLETED", "SUCCESS", "https://github.com/o/r/actions/runs/904/job/3"),
    ], head_sha=SHA_A)


async def test_a_busy_heads_red_runs_are_read_on_their_own_paginated():
    gh = _busy_gh()
    hub = _hub(gh)
    # Only the window: green.
    await hub.poll(["feat"])
    assert hub.verdict("feat", SHA_A).state == "green"
    # The red PR's head, read on its own (3 pages of 3).
    hub.clock.now += 60
    await hub.poll(["feat"], red_heads={"feat": SHA_A})
    v = hub.verdict("feat", SHA_A)
    assert v.state == "red"
    assert sorted(r.workflow for r in v.red) == ["evals", "label-gate"]
    pages = [c[-1] for c in gh.head_calls()]
    assert len(pages) == 3 and pages[1].endswith("&page=2") and pages[2].endswith("&page=3")
    # Its jobs are fetched for both red runs.
    assert {r.failed_jobs[0].name for r in v.red} == {"check", "evals"}
    # Unchanged next poll: one call, a 304.
    hub.clock.now += 60
    await hub.poll(["feat"], red_heads={"feat": SHA_A})
    assert len(gh.head_calls()) == 4
    assert hub.verdict("feat", SHA_A).state == "red"
    # The PR goes green: the head is no longer read, its runs dropped.
    hub.clock.now += 60
    await hub.poll(["feat"], red_heads={})
    assert len(gh.head_calls()) == 4 and hub.head_runs == {}


async def test_the_projection_reads_a_red_prs_head_and_its_log_summary_names_both_runs():
    gh = _busy_gh()
    engine = make_engine([worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)])
    clock = Clock()
    engine.status.clock = clock
    hub = _hub(gh, clock=clock)
    engine.actions = hub
    engine.worktree_service._pr_infos[FEAT] = _red_pr()
    engine.worktree_service._gh_available = True
    engine.status._poll_actions()
    await engine.wait_idle()
    [item] = [i for i in engine.actionables.list()["items"] if i["kind"] == "ci"]
    assert item["run"] is not None and item["run"]["workflow"] in ("label-gate", "evals")
    gh.logs[801] = "check\tRequire a label\t2026-10-24T10:01:00.1Z no label\n"
    gh.logs[802] = "evals\tRun the evals\t2026-10-24T10:01:00.1Z 3 evals failed\n"
    out = await actions.log_summary(engine, key=KEY)
    assert "error" not in out
    assert sorted(r["id"] for r in out["runs"]) == [801, 802]
    assert out["checks"] == []


async def test_a_red_check_that_is_not_actions_says_whose_it_is():
    engine = make_engine([worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)])
    engine.actions = _hub(FakeGh([run_json(11, "feat", SHA_A, conclusion="success")]))
    engine.worktree_service._pr_infos[FEAT] = PrInfo(7, "Feat", "OPEN", [
        CiCheck("e2e-suite", "COMPLETED", "FAILURE", "https://app.circleci.com/pipelines/gh/o/r/1"),
        CiCheck("deploy-preview", "", "ERROR", ""),
    ], head_sha=SHA_A)
    await engine.actions.poll(["feat"])
    engine.status.flush()
    out = await actions.log_summary(engine, key=KEY)
    assert out["runs"] == [] and out["jobs"] == []
    assert out["checks"] == [
        {"name": "e2e-suite", "provider": "CircleCI", "url": "https://app.circleci.com/pipelines/gh/o/r/1"},
        {"name": "deploy-preview", "provider": "a commit status with no link", "url": ""},
    ]
    assert out["error"] == (
        "no Actions log: check e2e-suite from CircleCI; "
        "check deploy-preview from a commit status with no link"
    )


async def test_providers_are_named_by_their_host():
    assert actions.provider_of("https://buildkite.com/o/p/builds/1") == "Buildkite"
    assert actions.provider_of("https://github.com/o/r/runs/123") == "a GitHub App (not Actions)"
    assert actions.provider_of("https://ci.example.com/b/1") == "ci.example.com"


# --- The timeline after a restart -------------------------------------------------


async def test_a_restart_does_not_add_the_same_commit_or_fact_again():
    engine, clock = _engine()
    overlay = engine.overlay
    overlay.add(KEY, "commit", "engine", "commit ebf48b6", ref="ebf48b6aa", at=NOW - 2 * DAY)
    overlay.add(KEY, "ci", "engine", "CI red: label-gate")
    overlay.add(KEY, "review", "engine", "review requested")
    overlay.add(KEY, "ball", "engine", "stalled → you: CI red: label-gate")
    clock.now += DAY
    # What a restarted projection adds when the facts come in again.
    overlay.add(KEY, "commit", "engine", "commit ebf48b6", ref="ebf48b6aa", at=NOW - 2 * DAY)
    overlay.add(KEY, "ci", "engine", "CI red: label-gate")
    overlay.add(KEY, "review", "engine", "review requested")
    overlay.add(KEY, "ball", "engine", "stalled → you: CI red: label-gate")
    assert [e["kind"] for e in overlay.entries(KEY)] == ["commit", "ci", "review", "ball"]
    # A real change still is news: green, then red again.
    overlay.add(KEY, "ci", "engine", "CI green")
    overlay.add(KEY, "ci", "engine", "CI red: label-gate")
    assert [e["text"] for e in overlay.entries(KEY) if e["kind"] == "ci"] == [
        "CI red: label-gate", "CI green", "CI red: label-gate",
    ]


async def test_a_restarted_projection_tracks_no_duplicate_entries():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - DAY}
    engine.worktree_service._pr_infos[FEAT] = None
    engine.status.list()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    clock.now += 60
    engine.status.list()
    before = engine.overlay.entries(KEY)
    # The engine restarts: a new projection over the same overlay, whose
    # first passes see the PR info go from unknown to red again.
    from lazyagent.engine.status import StatusProjection

    engine.status = StatusProjection(engine, clock=clock)
    engine.actionables.found_at = {}
    engine.worktree_service._pr_infos[FEAT] = None
    engine.status.list()
    clock.now += 60
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    engine.status.list()
    assert engine.overlay.entries(KEY) == before


# --- 5. The Why? answer -------------------------------------------------------------


async def test_why_says_in_one_sentence_with_human_values():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - (2 * DAY + 6 * HOUR + 45 * 60)}
    engine.status.list()
    out = engine.status.explain(KEY)
    assert out["rule"]["id"] == "no_activity"
    assert out["answer"] == (
        "“no activity”: the time since the last sign of work (2 d 6 h) is over "
        "the stalled threshold (24 h)."
    )
    shown = {f["name"]: f["shown"] for f in out["facts"]}
    assert shown == {"config.stalled_after": "24 h", "item.idle": "2 d 6 h"}


async def test_why_on_the_queue_names_the_item_that_leads_and_the_others():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED, review_decision="CHANGES_REQUESTED")
    engine.status.list()
    out = engine.status.explain(KEY)
    assert out["rule"]["id"] == "queue"
    assert out["answer"] == "“CI red: tests” leads: it is the first of the open inbox items; also open: changes requested."
    assert [i["kind"] for i in out["items"]] == ["ci", "review"]


async def test_durations_read_as_people_say_them():
    assert [doc.human_duration(s) for s in (5, 600, 86400, 5400, 197132, 3 * DAY)] == [
        "5 s", "10 min", "24 h", "1 h 30 min", "2 d 6 h", "3 d",
    ]
    assert doc.shown("time", NOW - 3 * HOUR, NOW) == "3 h ago"
    assert doc.shown("enum", "changes_requested", NOW) == "changes requested"
    assert doc.shown("duration", None, NOW) == "unknown"


async def test_the_orchestrators_rules_read_orchestrator_on_the_page():
    payload = doc.builtin_payload()
    balls = {r["id"]: r["ball_text"] for r in payload["rules"]}
    assert (balls["claim"], balls["hold"], balls["held"], balls["delegated"]) == (
        "orchestrator", "orchestrator", "orchestrator", "agent",
    )
