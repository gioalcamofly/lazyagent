"""The engine's inbox queue (proposal 0013, I2): the desktop attention
queue's tests (desktop/crates/app/src/attention.rs) ported, plus the fixed
levels and order, marks shared by every client and kept on disk, park
events, and the rows' ``needs_you``."""

from __future__ import annotations

import asyncio
import json

import pytest

from lazyagent.engine import actionables as act
from lazyagent.engine import status as status_mod
from lazyagent.engine.actionables import (
    ACTIONABLE_CREATED,
    ACTIONABLE_RESOLVED,
    ACTIONABLE_UPDATED,
    KINDS,
    Item,
    Kind,
    format_items,
    register_kind,
)
from lazyagent.engine.events import AGENT_EXITED, JOB_FINISHED, JOB_STARTED
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
FIX = "/tmp/repo-fix"

GREEN = [CiCheck("lint", "COMPLETED", "SUCCESS")]
RED = [
    CiCheck("lint", "COMPLETED", "SUCCESS"),
    CiCheck("tests", "COMPLETED", "FAILURE", "https://ci/1"),
]
RUNNING_CI = [CiCheck("tests", "IN_PROGRESS", "")]


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _engine():
    engine = make_engine(
        [
            worktree("/tmp/repo"),
            worktree(FEAT, "feat", is_main=False),
            worktree(FIX, "fix-typo", is_main=False),
        ]
    )
    clock = Clock()
    engine.status.clock = clock
    return engine, clock


def _state(engine, path, agent_id, status, detail=""):
    engine.registry.states.setdefault(path, {})[agent_id] = AgentState(
        status=AgentStatus(status), agent_id=agent_id, detail=detail
    )


def _session(path: str, agent_id: str):
    from unittest.mock import MagicMock

    session = MagicMock()
    session.worktree_path, session.agent_id = path, agent_id
    return session


def _agent(engine, path, agent_id, status, detail=""):
    _state(engine, path, agent_id, status, detail)
    if agent_id not in engine.agent_ids(path):
        add_session(engine, _session(path, agent_id))


def _drop(engine, path, agent_id):
    engine.registry.states.get(path, {}).pop(agent_id, None)
    engine._agents.get(path, {}).pop(agent_id, None)


def _pr(checks=GREEN, review="", mergeable="MERGEABLE", state="OPEN") -> PrInfo:
    return PrInfo(
        7, "Login", state, list(checks), "https://example.com/pull/7",
        review_decision=review, mergeable=mergeable,
    )


def _items(engine, **params):
    return engine.actionables.list(**params)["items"]


def _kinds(engine, **params):
    return [i["kind"] for i in _items(engine, **params)]


# --- attention.rs, ported ------------------------------------------------------


def test_levels_then_oldest_first():
    engine, clock = _engine()
    _agent(engine, FEAT, "a1", "completed")
    _agent(engine, FEAT, "a2", "waiting_for_user")
    _agent(engine, FEAT, "a3", "waiting_for_approval")
    _agent(engine, FEAT, "a4", "running")
    # One agent at work: the idle one is not "your turn" yet.
    assert _kinds(engine) == ["approval", "question"]

    _drop(engine, FEAT, "a4")
    assert _kinds(engine) == ["approval", "question", "done"]
    # Oldest first within a level (the desktop showed the newest first).
    clock.now += 10
    _agent(engine, FIX, "a5", "waiting")
    assert [i["id"] for i in _items(engine) if i["kind"] == "done"] == [
        f"Done:{FEAT}/a1",
        f"Done:{FIX}/a5",
    ]


def test_items_clear_themselves_and_dismiss_hides_one_occurrence():
    engine, clock = _engine()
    queue = engine.actionables
    _agent(engine, FEAT, "a1", "waiting_for_user")
    [first] = _items(engine)
    assert first["occurrence"] == f"Question:{FEAT}/a1@{NOW}"
    clock.now += 1
    queue.mark(first["id"], "dismiss")
    assert _items(engine) == []
    # It clears (running), then asks again later: a new occurrence shows.
    clock.now += 1
    _state(engine, FEAT, "a1", "running")
    assert _items(engine) == []
    assert queue._since == {}, "first-seen times are dropped with their condition"
    clock.now += 1
    _state(engine, FEAT, "a1", "waiting_for_user")
    [again] = _items(engine)
    assert again["occurrence"] == f"Question:{FEAT}/a1@{NOW + 3}"
    # Parked until a time.
    queue.mark(again["id"], "park", until=NOW + 100)
    clock.now = NOW + 50
    assert _items(engine) == []
    clock.now = NOW + 150
    assert len(_items(engine)) == 1


def test_open_prs_exits_and_jobs():
    engine, clock = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(
        RED, review="CHANGES_REQUESTED", mergeable="CONFLICTING"
    )
    engine.actionables._exits.append(act.Exit(FIX, "a1", "Agent a1", 2, 5.0, "repo:fix-typo"))
    engine.actionables._jobs += [
        act.JobEnd("j1", "Create x", 1),
        act.JobEnd("j2", "ok", 0),
    ]
    got = _items(engine)
    # Same level, age and rank: by id.
    # Changes requested needs reading: deep, after the round.
    assert [i["kind"] for i in got] == ["exited", "ci", "conflict", "job", "review"]
    ci = got[1]
    assert (ci["detail"], ci["state"], ci["label"]) == ("tests", "CI red: tests", "#7 Login")
    assert ci["pr"] == {"number": 7, "title": "Login", "url": "https://example.com/pull/7"}
    assert ci["work_item"] == "repo:feat"
    assert got[0]["detail"] == "exited with code 2" and got[0]["work_item"] == "repo:fix-typo"
    assert (got[3]["label"], got[3]["detail"], got[3]["work_item"]) == ("Create x", "exit 1", None)
    # A merged PR asks for nothing.
    engine.worktree_service._pr_infos[FEAT] = _pr(RED, state="MERGED")
    assert _kinds(engine) == ["exited", "job"]


def test_dismissals_are_pruned_after_a_week():
    engine, clock = _engine()
    _agent(engine, FEAT, "a1", "failed")
    [item] = _items(engine)
    engine.actionables.mark(item["id"], "dismiss")
    assert item["occurrence"] in engine.actionables._dismissed
    clock.now += 8 * 86400
    _items(engine)
    assert engine.actionables._dismissed == {}


# --- The fixed table ------------------------------------------------------------


def test_every_kind_has_its_fixed_level():
    assert {k: v.level for k, v in KINDS.items()} == {
        "approval": "now", "question": "now", "hanged": "round", "failed": "round",
        "exited": "round", "ci": "round", "ci_failed": "round", "review": "deep",
        "conflict": "round",
        "done": "deep", "job": "round", "delegation": "round", "report": "round",
        "decision": "deep",
        # Its default: an item carries the tracker mapping's level.
        "tracker_status": "round",
        # An item rule's (the built-in Merge): the rule gives its level.
        "rule": "round",
    }
    assert {k for k, v in KINDS.items() if v.blocks_agent} == {"approval", "question"}


def test_an_agent_at_work_answers_ci_and_conflicts_but_not_review():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(
        RED, review="CHANGES_REQUESTED", mergeable="CONFLICTING"
    )
    _agent(engine, FEAT, "a1", "running")
    assert _kinds(engine) == ["review"]


def test_your_turn_needs_no_pr_and_is_never_the_orchestrators():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "completed")
    _agent(engine, ORCHESTRATOR_KEY, "", "waiting")
    assert _kinds(engine) == ["done"]
    engine.worktree_service._pr_infos[FEAT] = _pr()
    assert _kinds(engine) == []
    # The orchestrator's dialogs are the user's, with no work item.
    _state(engine, ORCHESTRATOR_KEY, "", "waiting_for_approval", "Bash")
    [item] = _items(engine)
    assert (item["kind"], item["work_item"], item["state"]) == ("approval", None, "approve: Bash")


def test_blocking_and_priority_sort_before_age(monkeypatch):
    items = [
        {"id": "b", "kind": "failed", "level": "round", "blocks_agent": False,
         "priority": "medium", "since": 1.0},
        {"id": "c", "kind": "failed", "level": "round", "blocks_agent": False,
         "priority": "high", "since": 9.0},
        {"id": "d", "kind": "failed", "level": "round", "blocks_agent": True,
         "priority": "low", "since": 9.0},
        {"id": "a", "kind": "approval", "level": "now", "blocks_agent": True,
         "priority": "low", "since": 9.0},
    ]
    assert [i["id"] for i in sorted(items, key=act.sort_key)] == ["a", "d", "c", "b"]


def test_a_new_kind_plugs_in_with_a_detector():
    engine, _ = _engine()
    kind = register_kind(Kind("tracker_test", "round", "Returned", 8))
    try:
        engine.actionables.add_detector(
            lambda snap: [Item("Tracker:repo:feat", kind.name, "returned by QA",
                               work_item="repo:feat")]
        )
        [item] = _items(engine)
        assert (item["kind"], item["title"], item["level"]) == ("tracker_test", "Returned", "round")
        row = engine.status.list()["rows"][0]
        assert (row["key"], row["state"], row["needs_you"]["top"]) == (
            "repo:feat", "returned by QA", "Tracker:repo:feat",
        )
    finally:
        del KINDS["tracker_test"]
    with pytest.raises(ValueError, match="level"):
        register_kind(Kind("x", "later", "X", 1))


# --- Parks on events ---------------------------------------------------------------


def test_park_until_ci_done():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "failed")
    [item] = _items(engine)
    engine.actionables.mark(item["id"], "park", until="ci_done")
    assert _items(engine) == []
    engine.worktree_service._pr_infos[FEAT] = _pr(RUNNING_CI)
    assert _items(engine) == []
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN)
    assert _kinds(engine) == ["failed"]
    assert engine.actionables._parked == {}


def test_park_until_agent_stopped():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(review="CHANGES_REQUESTED")
    _agent(engine, FEAT, "a1", "running")
    [item] = _items(engine)
    engine.actionables.mark(item["id"], "park", until="agent_stopped")
    _state(engine, FEAT, "a1", "running", "Edit")
    assert _items(engine) == []
    _state(engine, FEAT, "a1", "waiting")
    assert _kinds(engine) == ["review"]

    # One that is gone counts too, even if it never ran while parked.
    engine.actionables.mark(item["id"], "park", until="agent_stopped")
    assert _items(engine) == []
    _drop(engine, FEAT, "a1")
    assert _kinds(engine) == ["review"]


def test_park_until_a_human_reviews():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(mergeable="CONFLICTING")
    [item] = _items(engine)
    engine.actionables.mark(item["id"], "park", until="human_review")
    assert _items(engine) == []
    engine.worktree_service._pr_infos[FEAT] = _pr(mergeable="CONFLICTING", review="REVIEW_REQUIRED")
    assert _kinds(engine) == ["conflict"]


def test_parked_items_are_listed_on_request_and_unparked():
    engine, clock = _engine()
    _agent(engine, FEAT, "a1", "possibly_hanged")
    [item] = _items(engine)
    marked = engine.actionables.mark(item["id"], "park", until=NOW + 60)
    assert marked["park"] == {"until": NOW + 60, "at": NOW, "by": "user"}
    result = engine.actionables.list(include_hidden=True)
    assert [i["park"]["until"] for i in result["items"]] == [NOW + 60]
    assert result["counts"] == {"now": 0, "round": 0, "deep": 0, "deep_max": 5, "parked": 1}
    assert engine.actionables.get(item["id"])["park"] is not None
    engine.actionables.mark(item["id"], "unpark")
    assert [i["park"] for i in _items(engine)] == [None]


def test_marks_are_validated():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "failed")
    [item] = _items(engine)
    queue = engine.actionables
    with pytest.raises(ValueError, match="action"):
        queue.mark(item["id"], "snooze")
    with pytest.raises(ValueError, match="until"):
        queue.mark(item["id"], "park", until="someday")
    with pytest.raises(ValueError, match="until"):
        queue.mark(item["id"], "park", until=NOW - 1)
    with pytest.raises(ValueError, match="until"):
        queue.mark(item["id"], "park")
    with pytest.raises(ValueError, match="No actionable"):
        queue.mark("Failed:/nope/a9", "dismiss")
    queue.mark(item["id"], "dismiss")
    with pytest.raises(ValueError, match="dismissed"):
        queue.get(item["id"])


# --- Exits and jobs -------------------------------------------------------------------


def test_exits_and_failed_jobs_come_from_events_and_go_on_dismiss():
    engine, clock = _engine()
    engine.actionables.start()
    _agent(engine, FEAT, "a1", "running")
    engine.agent_states(FEAT)["a1"]  # labels are learnt while the agent lives
    engine.actionables.flush()
    _drop(engine, FEAT, "a1")
    engine.events.emit(
        AGENT_EXITED,
        {"worktree_path": FEAT, "agent_id": "a1", "session_id": "s", "exit_code": 3,
         "stopped": False},
    )
    engine.events.emit(
        AGENT_EXITED,
        {"worktree_path": FIX, "agent_id": "a2", "session_id": "s", "exit_code": 0,
         "stopped": True},
    )
    engine.events.emit(JOB_STARTED, {"job_id": "j1", "title": "Create x", "command": "x"})
    engine.events.emit(JOB_FINISHED, {"job_id": "j1", "exit_code": 1})
    engine.events.emit(JOB_STARTED, {"job_id": "j2", "title": "ok", "command": "y"})
    engine.events.emit(JOB_FINISHED, {"job_id": "j2", "exit_code": 0})
    got = _items(engine)
    assert [(i["kind"], i["label"]) for i in got] == [("exited", "Agent a1"), ("job", "Create x")]
    assert got[0]["work_item"] == "repo:feat"

    engine.actionables.mark(got[1]["id"], "dismiss")
    assert engine.actionables._jobs == []
    # An agent in that slot again answers the exit.
    _agent(engine, FEAT, "a1", "running")
    assert _items(engine) == []
    engine.actionables.stop()


# --- Events, the store, the rows -------------------------------------------------------


def test_events_say_what_became_visible_changed_or_left():
    engine, clock = _engine()
    seen = []
    engine.events.subscribe(
        lambda e: seen.append((e.event, e.data)),
        {ACTIONABLE_CREATED, ACTIONABLE_UPDATED, ACTIONABLE_RESOLVED},
    )
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    _items(engine)
    _state(engine, FEAT, "a1", "waiting_for_approval", "Edit")
    _items(engine)
    item_id = f"Approval:{FEAT}/a1"
    engine.actionables.mark(item_id, "park", until="agent_stopped")
    engine.actionables.mark(item_id, "unpark")
    engine.actionables.mark(item_id, "dismiss")
    assert [(e, d.get("reason") or d["item"]["detail"]) for e, d in seen] == [
        (ACTIONABLE_CREATED, "Bash"),
        (ACTIONABLE_UPDATED, "Edit"),
        (ACTIONABLE_RESOLVED, "parked"),
        (ACTIONABLE_CREATED, "Edit"),
        (ACTIONABLE_RESOLVED, "dismissed"),
    ]
    seen.clear()
    _agent(engine, FIX, "a2", "failed")
    _items(engine)
    _state(engine, FIX, "a2", "running")
    _items(engine)
    assert [(e, d.get("reason")) for e, d in seen] == [
        (ACTIONABLE_CREATED, None),
        (ACTIONABLE_RESOLVED, "cleared"),
    ]


def test_marks_survive_a_restart(tmp_path):
    path = tmp_path / "actionables" / "x.json"
    engine, clock = _engine()
    engine.actionables.open(path)
    _agent(engine, FEAT, "a1", "waiting_for_user")
    _agent(engine, FIX, "a2", "failed")
    by_id = {i["kind"]: i["id"] for i in _items(engine)}
    engine.actionables.mark(by_id["question"], "dismiss")
    engine.actionables.mark(by_id["failed"], "park", until="ci_done")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    saved = json.loads(path.read_text())
    assert set(saved) == {"version", "since", "dismissed", "parked"}

    # A new engine: the agents are back a little later (restored).
    again, clock2 = _engine()
    clock2.now = NOW + 120
    again.actionables.open(path)
    assert _items(again) == []
    _agent(again, FEAT, "a1", "waiting_for_user")
    _agent(again, FIX, "a2", "failed")
    assert _items(again) == [], "same occurrence: still dismissed; still parked"
    assert again.actionables.list(include_hidden=True)["counts"]["parked"] == 1

    # Past the grace period a condition seen again is a new occurrence.
    third, clock3 = _engine()
    clock3.now = NOW + 120
    third.actionables.open(path)
    _items(third)
    clock3.now += act.CARRY_GRACE + 1
    _agent(third, FEAT, "a1", "waiting_for_user")
    assert _kinds(third) == ["question"]


def test_a_broken_store_is_ignored(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{nope")
    engine, _ = _engine()
    engine.actionables.open(path)
    _agent(engine, FEAT, "a1", "failed")
    assert _kinds(engine) == ["failed"]


def test_status_rows_take_needs_you_from_the_queue():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    rows = {r["key"]: r for r in engine.status.list()["rows"]}
    feat = rows["repo:feat"]
    assert (feat["zone"], feat["level"], feat["state"], feat["rule"]) == (
        "inbox", "now", "approve: Bash", 2,
    )
    assert feat["needs_you"] == {"count": 2, "top": f"Approval:{FEAT}/a1"}
    assert feat["blocks_agent"] is True
    assert rows["repo:fix-typo"]["needs_you"] is None

    # Dismissed: the row's ball moves on (the CI item stays).
    engine.actionables.mark(f"Approval:{FEAT}/a1", "dismiss")
    feat = engine.status.list()["rows"][0]
    assert (feat["level"], feat["state"], feat["needs_you"]["count"]) == ("round", "CI red: tests", 1)
    engine.actionables.mark(f"Ci:{FEAT}:7", "dismiss")
    feat = {r["key"]: r for r in engine.status.list()["rows"]}["repo:feat"]
    assert (feat["zone"], feat["ball"], feat["state"], feat["needs_you"]) == (
        "in_flight", "others", "PR open", None,
    )
    engine.worktree_service._pr_infos.pop(FEAT)
    feat = {r["key"]: r for r in engine.status.list()["rows"]}["repo:feat"]
    assert (feat["ball"], feat["state"]) == ("agent", "waits: approve")


def test_format_items():
    engine, clock = _engine()
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    _agent(engine, ORCHESTRATOR_KEY, "", "waiting_for_user", "which db?")
    engine.actionables.flush()
    clock.now += 120
    assert format_items(engine.actionables.list(), clock.now).splitlines() == [
        "NOW 2   ROUND 0",
        "now    Needs approval: feat · Agent a1 · Bash  2m",
        "now    Question: __orchestrator__ · Orchestrator · which db?  2m",
    ]
    assert format_items({"items": [], "counts": {"now": 0, "round": 0, "parked": 0}}, NOW) == (
        "NOW 0   ROUND 0\nnothing needs you"
    )


# --- Methods -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_methods_list_get_and_mark():
    engine, _ = _engine()
    methods = EngineMethods(engine)
    _agent(engine, FEAT, "a1", "waiting_for_approval")
    result = await methods.actionables_list({})
    [item] = result["items"]
    assert result["counts"] == {"now": 1, "round": 0, "deep": 0, "deep_max": 5, "parked": 0}
    assert result["generated_at"] == NOW
    assert (await methods.actionables_list({"repo": "other"}))["items"] == []
    assert (await methods.actionables_get({"id": item["id"]}))["item"]["id"] == item["id"]
    with pytest.raises(ValueError, match="include_hidden"):
        await methods.actionables_list({"include_hidden": 1})
    with pytest.raises(ValueError, match="id"):
        await methods.actionables_get({})

    # A worker's token may read but not mark; the orchestrator's may.
    engine._tokens["tw"] = (FEAT, "a1")
    engine._tokens["to"] = (ORCHESTRATOR_KEY, "")
    with pytest.raises(ValueError, match="Only the user or the orchestrator"):
        await methods.actionables_mark({"id": item["id"], "action": "dismiss", "caller": "tw"})
    parked = await methods.actionables_mark(
        {"id": item["id"], "action": "park", "until": "agent_stopped", "caller": "to"}
    )
    assert parked["item"]["park"]["by"] == "orchestrator"
    done = await methods.actionables_mark({"id": item["id"], "action": "dismiss"})
    assert done["item"]["park"] is None
    assert (await methods.actionables_list({}))["items"] == []


# --- Acceptance: no UI open --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approval_is_a_now_item_with_no_ui_open(monkeypatch):
    monkeypatch.setattr(status_mod, "COALESCE", 0.02)
    engine, _ = _engine()
    seen = []
    engine.events.subscribe(lambda e: seen.append(e), {ACTIONABLE_CREATED, "status.updated"})
    await engine.start(ipc=False)
    try:
        assert not engine.ui_attached
        _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
        engine.events.emit("agent.updated", {"worktree_path": FEAT})
        await asyncio.sleep(0.1)
        [created] = [e.data["item"] for e in seen if e.event == ACTIONABLE_CREATED]
        assert (created["kind"], created["level"], created["work_item"]) == (
            "approval", "now", "repo:feat",
        )
        [row] = [
            e.data["row"] for e in seen
            if e.event == "status.updated" and e.data["row"]["needs_you"]
        ]
        assert (row["level"], row["needs_you"]["top"]) == ("now", created["id"])
    finally:
        await engine.stop()
