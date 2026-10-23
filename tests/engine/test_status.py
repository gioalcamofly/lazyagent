"""Work items (proposal 0013): the §3 derivation table, the ordering, and the
engine's projection (membership, ages, PR interest, coalesced events)."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.engine import status as status_mod
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.notes import NotesStore
from lazyagent.engine.status import (
    ENGINE_CLIENT,
    STATUS_REMOVED,
    STATUS_UPDATED,
    AgentFact,
    Facts,
    build_row,
    derive,
    fingerprint,
    format_age,
    format_table,
    summarize,
)
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo, WorktreeInfo
from tests.engine.fakes import add_session, make_engine, worktree

NOW = 1_700_000_000.0
HOUR = 3600.0

GREEN = [CiCheck("tests", "COMPLETED", "SUCCESS")]
RED = [CiCheck("lint", "COMPLETED", "SUCCESS"), CiCheck("tests", "COMPLETED", "FAILURE")]
RUNNING_CI = [CiCheck("tests", "IN_PROGRESS", "")]


def pr(state="OPEN", checks=GREEN, review="", mergeable="MERGEABLE") -> PrInfo:
    return PrInfo(
        12, "A title", state, list(checks), "https://example.com/pull/12",
        review_decision=review, mergeable=mergeable,
    )


def agent(status: str, detail: str = "", agent_id: str = "a1") -> AgentFact:
    return AgentFact(agent_id, status, detail, NOW)


def facts(*agents: AgentFact, pr_info: PrInfo | None = None, restorable: int = 0) -> Facts:
    return Facts(
        key="repo:feat", repo="repo", branch="feat", worktree_path="/r-feat",
        agents=agents, pr=pr_info, restorable=restorable,
    )


def run(f: Facts, last_change: float = NOW) -> tuple:
    d = derive(f, NOW, last_change, stalled_hours=24)
    return d.rule, d.ball, d.level, d.state


# --- §3, first match wins ---------------------------------------------------

DERIVATION = {
    "merged": (facts(agent("running"), pr_info=pr("MERGED")), (1, "done", None, "merged")),
    "closed": (facts(pr_info=pr("CLOSED")), (1, "done", None, "PR closed")),
    "approval": (
        facts(agent("running", agent_id="a2"), agent("waiting_for_approval", "Bash")),
        (2, "you", "now", "approve: Bash"),
    ),
    "question": (facts(agent("waiting_for_user")), (2, "you", "now", "answer")),
    "failed": (facts(agent("failed", "boom")), (2, "you", "round", "agent failed")),
    "hung": (facts(agent("possibly_hanged")), (2, "you", "round", "agent may hang")),
    "approval_beats_a_red_ci": (
        facts(agent("waiting_for_approval"), pr_info=pr(checks=RED)),
        (2, "you", "now", "approve"),
    ),
    "merge_ready": (
        facts(pr_info=pr(review="APPROVED")),
        (4, "you", "round", "merge: approved, CI green"),
    ),
    "merge_ready_without_checks": (
        facts(pr_info=pr(checks=[], review="APPROVED")),
        (4, "you", "round", "merge: approved, CI green"),
    ),
    "changes_requested_even_with_an_agent": (
        facts(agent("running"), pr_info=pr(review="CHANGES_REQUESTED")),
        (5, "you", "round", "changes requested"),
    ),
    "conflict_no_agent": (
        facts(pr_info=pr(mergeable="CONFLICTING")),
        (5, "you", "round", "conflict with base"),
    ),
    "conflict_idle_agent": (
        facts(agent("waiting"), pr_info=pr(mergeable="CONFLICTING")),
        (5, "you", "round", "conflict with base"),
    ),
    "conflict_being_fixed": (
        facts(agent("running", "Bash"), pr_info=pr(mergeable="CONFLICTING")),
        (9, "agent", None, "Bash"),
    ),
    "ci_red_no_agent": (facts(pr_info=pr(checks=RED)), (5, "you", "round", "CI red: tests")),
    "ci_red_being_fixed": (
        facts(agent("running", "Edit"), pr_info=pr(checks=RED)),
        (9, "agent", None, "fixing CI"),
    ),
    "finished_no_pr": (facts(agent("completed")), (6, "you", "round", "review: agent finished")),
    "idle_no_pr": (facts(agent("waiting")), (6, "you", "round", "review: agent finished")),
    "one_idle_one_working": (
        facts(agent("waiting"), agent("running", agent_id="a2")),
        (9, "agent", None, "working"),
    ),
    "working": (facts(agent("running", "Bash")), (9, "agent", None, "Bash")),
    "starting": (facts(agent("starting")), (9, "agent", None, "starting")),
    "own_ci_running": (facts(pr_info=pr(checks=RUNNING_CI)), (9, "agent", None, "CI running")),
    "review_requested": (
        facts(pr_info=pr(review="REVIEW_REQUIRED")),
        (10, "others", None, "review requested, CI green"),
    ),
    "review_requested_ci_none": (
        facts(pr_info=pr(checks=[], review="REVIEW_REQUIRED")),
        (10, "others", None, "review requested"),
    ),
    "pr_open_no_review_policy": (
        facts(agent("waiting"), pr_info=pr()),
        (10, "others", None, "PR open, CI green"),
    ),
    "approved_not_mergeable_idle_agent": (
        facts(agent("waiting"), pr_info=pr(review="APPROVED", mergeable="UNKNOWN")),
        (12, "agent", None, "agent idle"),
    ),
    "approved_not_mergeable": (
        facts(pr_info=pr(review="APPROVED", mergeable="UNKNOWN")),
        (12, "stalled", None, "approved, not mergeable"),
    ),
    "nothing_recent": (facts(), (12, "stalled", None, "no agent")),
    "to_restore": (facts(restorable=1), (12, "stalled", None, "agent to restore")),
}


@pytest.mark.parametrize("case", sorted(DERIVATION))
def test_derivation_table(case):
    f, expected = DERIVATION[case]
    assert run(f) == expected


def test_no_activity_for_a_day_is_stalled():
    assert run(facts(), NOW - 25 * HOUR) == (11, "stalled", None, "no activity")
    assert run(facts(), NOW - 23 * HOUR)[0] == 12


def test_blocks_agent_only_for_dialogs():
    assert derive(facts(agent("waiting_for_user")), NOW, NOW, stalled_hours=24).blocks_agent
    assert not derive(facts(agent("failed")), NOW, NOW, stalled_hours=24).blocks_agent


def test_state_is_at_most_five_words():
    long = "Bash: npm run test -- --watch=false --coverage"
    _, _, _, state = run(facts(agent("waiting_for_approval", long)))
    assert len(state.split()) <= 5 + 1  # five words and the ellipsis
    assert state.startswith("approve: Bash: npm run") and state.endswith("…")


def test_fingerprint_ignores_the_detail_but_not_the_status():
    one = facts(agent("running", "Bash"))
    two = facts(agent("running", "Edit"))
    three = facts(agent("waiting"))
    fp = [fingerprint(f, derive(f, NOW, NOW, stalled_hours=24)) for f in (one, two, three)]
    assert fp[0] == fp[1] != fp[2]


# --- Ordering ----------------------------------------------------------------


def _row(key, f: Facts, changed_at, *, stale=False, last_change=NOW):
    d = derive(f, NOW, last_change, stalled_hours=24)
    return build_row(
        Facts(**{**f.__dict__, "key": key, "branch": key}), d, changed_at, "fp", stale=stale
    )


def test_inbox_first_then_in_flight_in_their_orders():
    rows = [
        _row("working-new", facts(agent("running")), NOW - 60),
        _row("working-old", facts(agent("running")), NOW - 600),
        _row("review", facts(pr_info=pr(review="REVIEW_REQUIRED")), NOW - 60),
        _row("stalled", facts(), NOW - 60),
        _row("merge-old", facts(pr_info=pr(review="APPROVED")), NOW - 900),
        _row("hung-older", facts(agent("possibly_hanged")), NOW - 9000),
        _row("question-new", facts(agent("waiting_for_user")), NOW - 10),
        _row("approval-old", facts(agent("waiting_for_approval")), NOW - 500),
        _row("merged", facts(pr_info=pr("MERGED")), NOW),
        _row("old-branch", facts(), NOW - 30 * 86400, stale=True),
    ]
    result = summarize(rows)
    assert [r["key"] for r in result["rows"]] == [
        # now: oldest first
        "approval-old",
        "question-new",
        # round: none blocks an agent, so oldest first
        "hung-older",
        "merge-old",
        # in flight: agent, others, stalled; latest change first
        "working-new",
        "working-old",
        "review",
        "stalled",
    ]
    assert result["counts"] == {
        "now": 2, "round": 2, "in_flight": 4, "done": 1, "stale": 1, "not_started": 0,
    }
    every = summarize(rows, include_done=True)["rows"]
    assert [r["key"] for r in every][-2:] == ["old-branch", "merged"]


def test_blocking_an_agent_sorts_first_inside_a_level():
    rows = [
        _row("failed-old", facts(agent("failed")), NOW - 900),
        # A dialog that is round-level does not exist yet; fake one to check
        # the key order: blocks first, then age.
        {**_row("blocking-new", facts(agent("failed")), NOW - 10), "blocks_agent": True},
    ]
    assert [r["key"] for r in summarize(rows)["rows"]] == ["blocking-new", "failed-old"]


def test_table_shows_level_item_ball_state_and_age():
    rows = [
        _row("login-ttl", facts(agent("waiting_for_approval", "npm test")), NOW - 120),
        _row("export-csv", facts(agent("running", "tests 2/5")), NOW - 30),
        _row("merged-one", facts(pr_info=pr("MERGED")), NOW),
    ]
    rows[0]["pr"] = None
    table = format_table(summarize(rows), NOW)
    lines = table.splitlines()
    assert lines[0] == "NOW 1   ROUND 0   IN FLIGHT 1   (1 done)"
    assert lines[1].split() == ["LEVEL", "ITEM", "BALL", "STATE", "AGE"]
    assert lines[2].split() == ["now", "login-ttl", "you", "approve:", "npm", "test", "2m"]
    assert lines[3].split() == ["-", "export-csv", "agent", "tests", "2/5", "<1m"]
    assert len(lines) == 4
    assert format_table(summarize([]), NOW).endswith("no work items")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "<1m"), (59, "<1m"), (60, "1m"), (3599, "59m"), (3600, "1h"), (47 * HOUR, "47h"),
     (48 * HOUR, "2d")],
)
def test_ages_are_never_zero_seconds(seconds, text):
    assert format_age(seconds) == text


# --- The projection over an engine -------------------------------------------

FEAT = "/tmp/repo-feat"
FIX = "/tmp/repo-fix"


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _engine(*extra: WorktreeInfo):
    engine = make_engine(
        [
            worktree("/tmp/repo"),
            worktree(FEAT, "feat", is_main=False),
            worktree(FIX, "fix-typo", is_main=False),
            *extra,
        ]
    )
    clock = Clock()
    engine.status.clock = clock
    return engine, clock


def _state(engine, path, agent_id, status, detail=""):
    engine.registry.states.setdefault(path, {})[agent_id] = AgentState(
        status=status, agent_id=agent_id, detail=detail
    )


def _session(path: str, agent_id: str):
    from unittest.mock import MagicMock

    session = MagicMock()
    session.worktree_path, session.agent_id = path, agent_id
    return session


def _rows(engine, **params):
    return {r["key"]: r for r in engine.status.list(**params)["rows"]}


def test_every_worktree_is_a_row_with_or_without_an_agent():
    detached = WorktreeInfo("/tmp/repo-detached", "def4567890ab", None, False, False)
    bare = WorktreeInfo("/tmp/repo.git", "abc", None, False, True)
    engine, _ = _engine(detached, bare)
    _state(engine, FEAT, "a1", AgentStatus.RUNNING, "Bash")
    add_session(engine, _session(FEAT, "a1"))

    rows = _rows(engine)
    # The main checkout is not a work item while no agent runs there.
    assert set(rows) == {"repo:feat", "repo:fix-typo", "repo:@def4567890ab"}
    assert (rows["repo:feat"]["ball"], rows["repo:feat"]["state"]) == ("agent", "Bash")
    assert rows["repo:feat"]["agents"] == [{"id": "a1", "status": "running", "since": NOW}]
    assert (rows["repo:fix-typo"]["ball"], rows["repo:fix-typo"]["state"]) == (
        "stalled", "no agent",
    )
    assert rows["repo:@def4567890ab"]["branch"] is None

    _state(engine, "/tmp/repo", "a1", AgentStatus.RUNNING)
    add_session(engine, _session("/tmp/repo", "a1"))
    assert "repo:main" in _rows(engine)


def test_pr_facts_feed_the_row():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = pr(review="APPROVED")
    row = _rows(engine)["repo:feat"]
    assert (row["zone"], row["level"], row["ball"]) == ("inbox", "round", "you")
    assert row["title"] == "A title" and row["urls"] == ["https://example.com/pull/12"]
    assert row["pr"] == {
        "number": 12, "state": "OPEN", "review": "APPROVED", "ci": "green",
        "mergeable": "MERGEABLE",
    }


def test_ages_start_at_the_last_commit_and_move_with_the_facts():
    engine, clock = _engine()
    engine.world.commit_times = {"feat": NOW - 5 * HOUR, "fix-typo": NOW - 30 * 86400}
    engine.status._commit_times = engine.world.manager("/tmp/repo").branch_commit_times()

    result = engine.status.list()
    rows = {r["key"]: r for r in result["rows"]}
    assert rows["repo:feat"]["changed_at"] == NOW - 5 * HOUR
    # Nothing but a branch untouched for 30 days: stale, hidden, counted.
    assert "repo:fix-typo" not in rows and result["counts"]["stale"] == 1
    assert _rows(engine, include_done=True)["repo:fix-typo"]["stale"] is True

    clock.now += 60
    _state(engine, FEAT, "a1", AgentStatus.RUNNING, "Bash")
    add_session(engine, _session(FEAT, "a1"))
    assert _rows(engine)["repo:feat"]["changed_at"] == NOW + 60

    # A new tool call is not a change of facts; a new status is.
    clock.now += 60
    _state(engine, FEAT, "a1", AgentStatus.RUNNING, "Edit")
    row = _rows(engine)["repo:feat"]
    assert (row["state"], row["changed_at"]) == ("Edit", NOW + 60)
    clock.now += 60
    _state(engine, FEAT, "a1", AgentStatus.WAITING)
    row = _rows(engine)["repo:feat"]
    assert (row["ball"], row["changed_at"]) == ("you", NOW + 180)
    assert row["agents"][0]["since"] == NOW + 180


def test_no_change_for_a_day_turns_a_branch_stalled():
    engine, clock = _engine()
    assert _rows(engine)["repo:feat"]["state"] == "no agent"
    clock.now += 25 * HOUR
    assert _rows(engine)["repo:feat"]["state"] == "no activity"


def test_a_restorable_agent_shows_on_its_row(tmp_path):
    engine, _ = _engine()
    engine.open_manifest(tmp_path / "manifest.json")
    from lazyagent.engine.manifest import Entry

    entry = Entry(Entry.new_id(), FEAT, "a1", "agent", "claude", ended_at=NOW)
    engine.manifest.restorable[entry.id] = entry
    assert _rows(engine)["repo:feat"]["state"] == "agent to restore"


def test_the_note_is_the_first_open_pending_item(tmp_path):
    engine, _ = _engine()
    engine.notes = NotesStore(tmp_path / "notes", clock=lambda: NOW)
    _state(engine, FEAT, "a1", AgentStatus.RUNNING)
    add_session(engine, _session(FEAT, "a1"))
    meta = engine.notes.create(FEAT, "a1", notes_id="0a1b2c3d4e51")
    engine._notes_ids[(FEAT, "a1")] = meta.notes_id
    assert _rows(engine)["repo:feat"]["note"] is None

    engine.notes.append(meta.notes_id, "Doubts", "which db?\nwhich port?", "agent")
    assert _rows(engine)["repo:feat"]["note"]["text"] == "which port?"
    engine.notes.append(meta.notes_id, "Pending", "- [x] schema\n- [ ] tests", "agent")
    note = _rows(engine)["repo:feat"]["note"]
    assert note == {"text": "tests", "by": "notes", "at": NOW, "agent_id": "a1"}


def test_notes_section_bullets(tmp_path):
    store = NotesStore(tmp_path / "notes")
    meta = store.create("/w", "a1", notes_id="0a1b2c3d4e51")
    store.append(meta.notes_id, "Decisions", "use sqlite\n  because it is small", "agent")
    assert store.section_bullets(meta.notes_id, "decisions") == ["use sqlite"]
    assert store.section_bullets(meta.notes_id, "Nope") == []
    assert store.section_bullets("missing", "Decisions") == []


@pytest.mark.asyncio
async def test_status_list_method_filters_and_validates():
    engine, _ = _engine()
    methods = EngineMethods(engine)
    result = await methods.status_list({})
    assert {r["key"] for r in result["rows"]} == {"repo:feat", "repo:fix-typo"}
    assert result["generated_at"] == NOW
    assert (await methods.status_list({"repo": "other"}))["rows"] == []
    assert len((await methods.status_list({"repo": "repo"}))["rows"]) == 2
    with pytest.raises(ValueError, match="include_done"):
        await methods.status_list({"include_done": "yes"})
    # A token that is gone does not break a read.
    assert len((await methods.status_list({"caller": "gone"}))["rows"]) == 2


@pytest.mark.asyncio
async def test_the_engine_keeps_pr_interest_in_every_work_item():
    engine, _ = _engine()
    engine.status.start()
    try:
        service = engine.worktree_service
        assert service.pr_interest == {FEAT, FIX}
        # PR only: the git status poller still covers what clients show.
        assert service.interest == frozenset()
        service.set_interest(["/tmp/repo"], "desktop")
        assert service.pr_interest == {FEAT, FIX, "/tmp/repo"}

        engine.world.worktrees.append(worktree("/tmp/repo-new", "new", is_main=False))
        engine.refresh_worktrees()
        assert "/tmp/repo-new" in service.pr_interest
    finally:
        engine.status.stop()
    assert ENGINE_CLIENT not in engine.worktree_service._pr_interests


@pytest.mark.asyncio
async def test_events_are_coalesced_and_only_for_what_changed(monkeypatch):
    monkeypatch.setattr(status_mod, "COALESCE", 0.05)
    engine, _ = _engine()
    seen = []
    engine.events.subscribe(lambda e: seen.append(e), {STATUS_UPDATED, STATUS_REMOVED})
    engine.status.start()
    try:
        # Every row once, at start.
        assert sorted(e.data["row"]["key"] for e in seen) == ["repo:feat", "repo:fix-typo"]
        seen.clear()

        _state(engine, FEAT, "a1", AgentStatus.RUNNING, "Bash")
        add_session(engine, _session(FEAT, "a1"))
        for _ in range(5):
            engine.events.emit("agent.updated", {"worktree_path": FEAT})
        await asyncio.sleep(0.15)
        assert [(e.event, e.data["row"]["key"]) for e in seen] == [
            (STATUS_UPDATED, "repo:feat")
        ]
        assert seen[0].data["row"]["ball"] == "agent"

        seen.clear()
        engine.events.emit("agent.updated", {"worktree_path": FEAT})
        await asyncio.sleep(0.15)
        assert seen == []  # nothing changed

        engine.world.worktrees = [wt for wt in engine.world.worktrees if wt.path != FIX]
        engine.refresh_worktrees()
        await asyncio.sleep(0.15)
        assert [(e.event, e.data) for e in seen] == [
            (STATUS_REMOVED, {"key": "repo:fix-typo"})
        ]
    finally:
        engine.status.stop()


@pytest.mark.asyncio
async def test_an_engine_start_owns_the_projection():
    engine, _ = _engine()
    await engine.start(ipc=False)
    try:
        assert engine.status._started
        assert engine.worktree_service.pr_interest == {FEAT, FIX}
    finally:
        await engine.stop()
    assert not engine.status._started


def test_branch_commit_times_parses_for_each_ref(monkeypatch, tmp_path):
    import subprocess

    from lazyagent import worktree_manager

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["git", "for-each-ref"]
        return subprocess.CompletedProcess(
            cmd, 0, "main\t1700000000\nfeat/x\t1700000100\nbad line\n", ""
        )

    monkeypatch.setattr(worktree_manager, "_run", fake_run)
    manager = worktree_manager.WorktreeManager.__new__(worktree_manager.WorktreeManager)
    manager.repo_path = tmp_path
    assert manager.branch_commit_times() == {"main": 1700000000.0, "feat/x": 1700000100.0}
