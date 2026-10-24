"""The status overlay (proposal 0013 I4): the user's gestures (label, park,
close), agents' reports, 24 h tombstones, the per-item timeline and seen
marks, and their store."""

from __future__ import annotations

import json
import stat
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.engine import overlay as ov
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.overlay import STATUS_TIMELINE, Overlay, overlay_path
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.status import format_table
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
FIX = "/tmp/repo-fix"
KEY = "repo:feat"
GREEN = [CiCheck("tests", "COMPLETED", "SUCCESS")]
RED = [CiCheck("tests", "COMPLETED", "FAILURE")]
RUNNING_CI = [CiCheck("tests", "IN_PROGRESS", "")]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine():
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False),
         worktree(FIX, "fix", is_main=False)]
    )
    clock = Clock()
    engine.status.clock = clock
    return engine, clock


def _agent(engine, path, agent_id, status, detail=""):
    engine.registry.states.setdefault(path, {})[agent_id] = AgentState(
        status=AgentStatus(status), agent_id=agent_id, detail=detail
    )
    if agent_id not in engine.agent_ids(path):
        session = MagicMock()
        session.worktree_path, session.agent_id = path, agent_id
        session.running = True
        session.send_input = AsyncMock()
        add_session(engine, session)
        engine._tokens[f"tok-{agent_id}"] = (path, agent_id)


def _drop(engine, path, agent_id):
    engine.registry.states.get(path, {}).pop(agent_id, None)
    engine._agents.get(path, {}).pop(agent_id, None)


def _pr(checks=GREEN, state="OPEN", review="", mergeable="MERGEABLE", number=7):
    return PrInfo(number, "Login", state, list(checks), "https://example.com/pull/7",
                  review_decision=review, mergeable=mergeable)


def _row(engine, key=KEY):
    return next(r for r in engine.status.list(include_done=True)["rows"] if r["key"] == key)


async def _gesture(engine, **params):
    return await EngineMethods(engine).status_gesture({"key": KEY, **params})


def _timeline(engine, key=KEY):
    return [(e["kind"], e["actor"], e["text"]) for e in engine.overlay.entries(key)]


# --- Label -------------------------------------------------------------------


async def test_a_label_keeps_through_a_conflict_and_goes_on_merge():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr()
    assert _row(engine)["ball"] == "others"  # review requested
    result = await _gesture(engine, kind="label", ball="others", text="QA")
    assert result["gesture"]["text"] == "QA"
    row = _row(engine)
    assert (row["rule"], row["ball"], row["state"]) == (8, "others", "PR open, CI green")
    line = next(x for x in format_table(engine.status.list(), NOW).splitlines() if "feat" in x)
    assert line.split()[3:8] == ["others", "QA", "·", "PR", "open,"]

    # A conflict with no agent puts the item in the inbox (rule 5 first)...
    engine.worktree_service._pr_infos[FEAT] = _pr(mergeable="CONFLICTING")
    assert (_row(engine)["ball"], _row(engine)["gesture"]["text"]) == ("you", "QA")
    # ...an agent fixes it, and the label is still there.
    engine.worktree_service._pr_infos[FEAT] = _pr()
    assert (_row(engine)["rule"], _row(engine)["ball"]) == (8, "others")

    engine.worktree_service._pr_infos[FEAT] = _pr(state="MERGED")
    row = _row(engine)
    assert (row["ball"], row["gesture"]) == ("done", None)
    assert KEY not in engine.overlay.gestures


async def test_a_label_goes_with_a_new_red_ci_not_an_old_one():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    await _gesture(engine, kind="label", ball="others", text="waiting on infra")
    # Red when labelled: it holds.
    assert _row(engine)["gesture"] is not None
    engine.worktree_service._pr_infos[FEAT] = _pr(RUNNING_CI)
    assert _row(engine)["gesture"] is not None
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN)
    assert _row(engine)["gesture"] is not None
    # Red again after green: a new red ends it.
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    assert _row(engine)["gesture"] is None


async def test_a_label_goes_when_the_review_changes_or_a_pr_appears():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    await _gesture(engine, kind="label", ball="stalled")
    assert (_row(engine)["ball"], _row(engine)["state"]) == ("stalled", "working")
    engine.worktree_service._pr_infos[FEAT] = _pr()
    assert _row(engine)["gesture"] is None

    await _gesture(engine, kind="label", ball="others", text="x")
    engine.worktree_service._pr_infos[FEAT] = _pr(review="CHANGES_REQUESTED")
    assert _row(engine)["gesture"] is None


async def test_label_arguments_are_checked_and_capped():
    engine, _ = _engine()
    with pytest.raises(ValueError, match="ball must be one of"):
        await _gesture(engine, kind="label", ball="you")
    await _gesture(engine, kind="label", ball="others", text="x" * 80)
    assert len(_row(engine)["gesture"]["text"]) == ov.LABEL_CHARS
    with pytest.raises(ValueError, match="No work item"):
        await EngineMethods(engine).status_gesture({"key": "repo:nope", "kind": "label",
                                                   "ball": "others"})


async def test_gestures_are_the_users_only():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    _agent(engine, ORCHESTRATOR_KEY, "", "running")
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    for caller in ("tok-a1", "tok-orch"):
        with pytest.raises(ValueError, match="Only the user"):
            await _gesture(engine, kind="label", ball="others", caller=caller)


# --- Park and close ------------------------------------------------------------


async def test_a_park_moves_the_row_to_parked_until_its_time():
    engine, clock = _engine()
    await _gesture(engine, kind="park", until=NOW + 600)
    row = _row(engine)
    assert (row["rule"], row["ball"], row["zone"]) == (7, "stalled", "parked")
    assert row["state"].startswith("parked until ")
    assert row["gesture"]["until"] == NOW + 600
    clock.now += 600
    assert (_row(engine)["rule"], _row(engine)["gesture"]) == (12, None)


async def test_a_park_until_ci_ends_and_a_bad_until():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RUNNING_CI)
    await _gesture(engine, kind="park", until="ci_done")
    assert _row(engine)["state"] == "parked until CI ends"
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN)
    assert _row(engine)["gesture"] is None
    with pytest.raises(ValueError, match="until must be"):
        await _gesture(engine, kind="park", until="someday")


async def test_close_makes_it_done_until_work_starts_again():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "completed")
    assert _row(engine)["level"] == "deep"
    await _gesture(engine, kind="close")
    row = _row(engine)
    assert (row["rule"], row["ball"], row["state"]) == (1, "done", "closed by you")
    assert engine.overlay.closed[KEY]["how"] == "done"
    assert KEY not in [r["key"] for r in engine.status.list()["rows"]]

    _agent(engine, FEAT, "a2", "running")
    row = _row(engine)
    assert (row["ball"], row["gesture"]) == ("agent", None)
    assert KEY not in engine.overlay.closed

    await _gesture(engine, kind="close", how="dropped")
    assert _row(engine)["state"] == "dropped by you"
    with pytest.raises(ValueError, match="how must be one of"):
        await _gesture(engine, kind="close", how="merged")


async def test_clear_drops_the_gesture():
    engine, _ = _engine()
    await _gesture(engine, kind="label", ball="others", text="QA")
    result = await _gesture(engine, kind="clear")
    assert (result["gesture"], result["row"]["rule"]) == (None, 12)
    with pytest.raises(ValueError, match="no gesture"):
        await _gesture(engine, kind="clear")


async def test_a_gesture_is_on_the_timeline_as_the_users():
    engine, _ = _engine()
    await _gesture(engine, kind="label", ball="others", text="QA")
    assert _timeline(engine) == [("ball", "user", "stalled → others: no agent")]


# --- Tombstones -------------------------------------------------------------------


async def test_a_removed_worktree_is_a_done_row_for_a_day():
    engine, clock = _engine()
    await _gesture(engine, kind="label", ball="others", text="QA")
    engine.worktree_service.worktrees = [
        w for w in engine.worktree_service.worktrees if w.path != FEAT
    ]
    row = _row(engine)
    assert (row["ball"], row["state"], row["worktree_path"], row["title"]) == (
        "done", "worktree removed", None, "feat",
    )
    assert engine.overlay.closed[KEY]["how"] == "worktree_removed"
    clock.now += 86400
    assert KEY not in [r["key"] for r in engine.status.list(include_done=True)["rows"]]
    assert KEY not in engine.overlay.gestures and KEY not in engine.overlay.timeline


async def test_a_merge_is_a_tombstone_that_reopens_never():
    engine, clock = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(state="MERGED")
    _row(engine)
    assert engine.overlay.closed[KEY]["how"] == "merged"
    # The worktree stays: so does its done row and its tombstone.
    clock.now += 2 * 86400
    assert _row(engine)["ball"] == "done"
    assert KEY in engine.overlay.closed


# --- Reports ------------------------------------------------------------------------


async def _report(engine, agent="a1", **params):
    return await EngineMethods(engine).status_report({"caller": f"tok-{agent}", **params})


async def test_a_report_asking_for_the_user_is_a_round_item_until_the_agent_runs_again():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    result = await _report(engine, note="pick a reviewer", needs_user=True)
    assert result["key"] == KEY
    row = _row(engine)
    assert (row["level"], row["class"], row["state"]) == ("round", "quick", "needs you: pick a reviewer")
    assert row["note"] == {"text": "pick a reviewer", "by": "agent:a1", "at": NOW, "agent_id": "a1"}
    [item] = engine.actionables.list()["items"]
    assert (item["kind"], item["label"]) == ("report", "Agent a1")

    # Still mid-turn: it holds; its turn ends: it holds; running again: gone.
    _agent(engine, FEAT, "a1", "running")
    assert _row(engine)["level"] == "round"
    _agent(engine, FEAT, "a1", "waiting_for_user")
    _row(engine)
    _agent(engine, FEAT, "a1", "running")
    row = _row(engine)
    assert (row["ball"], row["note"]["text"]) == ("agent", "pick a reviewer")
    assert engine.actionables.list()["items"] == []


async def test_input_or_the_agent_leaving_answers_a_report():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    await _report(engine, note="?", needs_user=True)
    await engine.send_agent_input(FEAT, "a1", "go on")
    assert _row(engine)["level"] is None

    await _report(engine, note="waiting for QA", ball="others")
    _agent(engine, FEAT, "a1", "waiting")
    _row(engine)
    assert engine.overlay.reports[KEY]["ball"] == "others"
    _drop(engine, FEAT, "a1")
    _row(engine)
    assert engine.overlay.reports[KEY]["ball"] is None


async def test_a_reported_ball_shows_where_the_engine_cannot_see():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    await _report(engine, note="waiting for QA", ball="others")
    # Running wins (rule 9)...
    assert _row(engine)["ball"] == "agent"
    # ...then its turn is over with a PR: the report says whose ball it is.
    engine.worktree_service._pr_infos[FEAT] = _pr()
    _agent(engine, FEAT, "a1", "waiting")
    row = _row(engine)
    assert (row["rule"], row["ball"], row["state"]) == (10, "others", "waiting for QA")


async def test_report_rules():
    engine, _ = _engine()
    methods = EngineMethods(engine)
    _agent(engine, FEAT, "a1", "running")
    with pytest.raises(ValueError, match="needs the agent's token"):
        await methods.status_report({"note": "x"})
    _agent(engine, ORCHESTRATOR_KEY, "", "running")
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    # The orchestrator reports only what it is doing now, on no work item.
    mine = await methods.status_report({"note": "verifying feat", "caller": "tok-orch"})
    assert (mine["key"], mine["report"]["note"]) == (None, "verifying feat")
    with pytest.raises(ValueError, match="reports only a note"):
        await methods.status_report({"note": "x", "ball": "others", "caller": "tok-orch"})
    with pytest.raises(ValueError, match="use needs_user"):
        await _report(engine, note="x", ball="you")
    with pytest.raises(ValueError, match="Nothing to report"):
        await _report(engine, note="  ")
    with pytest.raises(ValueError, match="outward must be"):
        await _report(engine, outward={"ref": "x"})
    result = await _report(engine, note="y" * 300)
    assert len(result["report"]["note"]) == ov.NOTE_CHARS


async def test_an_outward_action_is_on_the_timeline_as_the_agents():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    seen = []
    engine.events.subscribe(lambda e: seen.append(e.data), {STATUS_TIMELINE})
    result = await _report(
        engine, outward={"kind": "tracker", "ref": "https://tracker/1", "text": "moved to QA"}
    )
    assert result["report"] is None
    entry = {"at": NOW, "kind": "outward", "actor": "agent:a1",
             "text": "tracker: moved to QA", "ref": "https://tracker/1"}
    assert result["outward"] == entry
    assert seen == [{"key": KEY, "entry": entry}]


# --- Timeline and seen marks -----------------------------------------------------------


async def test_the_timeline_lists_what_happened_while_the_user_was_away():
    engine, clock = _engine()
    _agent(engine, FEAT, "a1", "running")
    engine.worktree_service._pr_infos[FEAT] = _pr(RUNNING_CI)
    _row(engine)
    await EngineMethods(engine).status_seen({})

    clock.now += 60
    engine.status._commit_times = {"feat": NOW + 30}
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    _row(engine)
    clock.now += 60
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN, review="APPROVED")
    _agent(engine, FEAT, "a1", "waiting")
    row = _row(engine)

    result = await EngineMethods(engine).status_timeline({"key": KEY})
    assert result["since"] == NOW
    assert [(e["kind"], e["text"], e["at"]) for e in result["entries"]] == [
        ("commit", "commit abc123", NOW + 30),
        ("ci", "CI red: tests", NOW + 60),
        ("ci", "CI green", NOW + 120),
        ("review", "review: approved", NOW + 120),
        ("ball", "agent → you: merge: approved, CI green", NOW + 120),
    ]
    assert result["entries"][0]["ref"] == "abc123"
    assert {e["actor"] for e in result["entries"]} == {"engine"}
    assert (row["seen_at"], row["last_event_at"]) == (NOW, NOW + 120)

    await EngineMethods(engine).status_seen({"key": KEY})
    assert (await EngineMethods(engine).status_timeline({"key": KEY}))["entries"] == []
    assert _row(engine)["seen_at"] == NOW + 120
    every = await EngineMethods(engine).status_timeline({"key": KEY, "since": 0})
    assert len(every["entries"]) == 5


async def test_a_tool_line_is_not_a_timeline_entry():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running", "Bash")
    _row(engine)
    _agent(engine, FEAT, "a1", "running", "Edit")
    assert _row(engine)["state"] == "working"
    assert _timeline(engine) == []


async def test_notes_edits_name_their_sections(tmp_path):
    from lazyagent.engine.notes import NotesStore

    engine, _ = _engine()
    engine.notes = NotesStore(tmp_path / "notes", listener=engine._on_notes_event)
    meta = engine.notes.create(FEAT, "a1")
    engine.status.start()
    try:
        engine.notes.append(meta.notes_id, "Decisions", "use sqlite", by="agent")
        engine.notes.append(meta.notes_id, "Pending", "[ ] tests", by="orchestrator")
    finally:
        engine.status.stop()
    assert [(e["actor"], e["text"]) for e in engine.overlay.entries(KEY)] == [
        ("agent:a1", "notes: Decisions"),
        ("orchestrator", "notes: Pending"),
    ]


async def test_the_ring_keeps_50_entries_and_a_week():
    overlay = Overlay()
    t0 = time.time() - 100
    for i in range(60):
        overlay.add(KEY, "state", "engine", f"s{i}", at=t0 + i)
    ring = overlay.entries(KEY)
    assert len(ring) == ov.RING and ring[0]["text"] == "s10"
    assert [e["text"] for e in overlay.entries(KEY, since=t0 + 58)] == ["s59"]


async def test_old_entries_are_pruned():
    overlay = Overlay()
    overlay.add(KEY, "state", "engine", "old", at=0.0)
    assert overlay.entries(KEY) == []
    overlay.add(KEY, "state", "engine", "x" * 300)
    assert len(overlay.entries(KEY)[0]["text"]) == ov.TEXT_CHARS


async def test_seen_marks_per_item_and_all_per_client():
    engine, clock = _engine()
    methods = EngineMethods(engine)
    await methods.status_seen({"key": KEY, "at": NOW - 10})
    assert _row(engine)["seen_at"] == NOW - 10
    assert _row(engine, "repo:fix")["seen_at"] is None
    clock.now += 5
    result = await methods.status_seen({})
    assert result == {"client": "user", "seen": {"*": NOW + 5}}
    assert _row(engine, "repo:fix")["seen_at"] == NOW + 5
    # Never in the future; another client keeps its own.
    result = await methods.status_seen({"key": KEY, "at": NOW + 999, "client": "desk-2"})
    assert result["seen"] == {KEY: NOW + 5}
    with pytest.raises(ValueError, match="at must be"):
        await methods.status_seen({"at": "now"})


# --- The store -----------------------------------------------------------------------


async def test_the_overlay_is_kept_in_the_state_dir(tmp_path):
    engine, _ = _engine()
    path = overlay_path("/tmp/repo")
    assert str(path).startswith(str(tmp_path / "state"))
    engine.overlay.open(path)
    await _gesture(engine, kind="label", ball="others", text="QA")
    engine.overlay.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["gestures"][KEY]["text"] == "QA"
    assert data["timeline"][KEY][0]["actor"] == "user"

    again, _ = _engine()
    again.overlay.open(path)
    row = _row(again)
    assert (row["rule"], row["gesture"]["text"]) == (8, "QA")


async def test_a_bad_store_is_ignored(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{not json")
    overlay = Overlay()
    overlay.open(path)
    assert overlay.gestures == {}
    path.write_text(json.dumps({"version": 1, "gestures": {"k": {"kind": "dance"}},
                                "timeline": {"k": [{"at": "x"}]}, "next_decision": 7}))
    overlay.open(path)
    assert (overlay.gestures, overlay.timeline, overlay.next_decision) == ({}, {}, 7)

