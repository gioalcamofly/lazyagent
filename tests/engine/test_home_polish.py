"""Home polish (proposal 0013, I4d): a pass reads "orchestrator · looking"
and leaves its note on release; the orchestrator's own holds (and the
engine's, on its workers' finished work); parks of the whole work item with
dates and events; whole-item park routing; delegation to the branch's agent."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.config import Config, OrchestratorConfig
from lazyagent.engine.actionables import tomorrow_at
from lazyagent.engine.claims import HOLD_CREATED, MAX_HOLD_TTL, NO_RESPONSE
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo, WorktreeInfo
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
KEY = "repo:feat"
GREEN = [CiCheck("tests", "COMPLETED", "SUCCESS")]
RED = [CiCheck("tests", "COMPLETED", "FAILURE")]
RUNNING_CI = [CiCheck("tests", "IN_PROGRESS", "")]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _engine(**orchestrator):
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)],
        config=Config(orchestrator=OrchestratorConfig(**orchestrator)),
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
    return engine.get_session(path, agent_id)


def _drop(engine, path, agent_id):
    engine.registry.states.get(path, {}).pop(agent_id, None)
    engine._agents.get(path, {}).pop(agent_id, None)


def _orchestrator(engine, status="waiting"):
    session = _agent(engine, ORCHESTRATOR_KEY, "", status)
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    return session


def _pr(checks=GREEN, **kw):
    return PrInfo(7, "Feat", "OPEN", list(checks), "https://example.com/pull/7", **kw)


def _row(engine, key=KEY):
    return next(r for r in engine.status.list(include_done=True)["rows"] if r["key"] == key)


def _timeline(engine, kind="delegate"):
    return [e["text"] for e in engine.overlay.entries(KEY) if e["kind"] == kind]


async def _pass(engine):
    engine.status.flush()
    await engine.wait_idle()
    engine.status.flush()
    await engine.wait_idle()


# --- 1. Pass to orchestrator is visible --------------------------------------


async def test_a_pass_reads_looking_in_flight_and_the_release_note_stays_on_the_row():
    engine, clock = _engine()
    _orchestrator(engine)
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    assert _row(engine)["level"] == "round"
    methods = EngineMethods(engine)
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "orchestrator"})
    row = _row(engine)
    assert (row["zone"], row["level"], row["state"]) == ("in_flight", None, "orchestrator · looking")
    assert _timeline(engine)[-1].startswith("passed to the orchestrator until ")

    clock.now += 60
    await methods.orchestrator_release(
        {"key": KEY, "outcome": "done", "note": "re-ran CI; a flaky test", "caller": "tok-orch"}
    )
    row = _row(engine)
    assert row["note"] == {"text": "orchestrator: re-ran CI; a flaky test", "by": "orchestrator",
                           "at": NOW + 60}
    assert _timeline(engine)[-1] == "orchestrator done: re-ran CI; a flaky test"
    # Done, but CI is still red: the row is the user's again, with the note.
    assert row["level"] == "round"

    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "orchestrator"})
    await methods.orchestrator_release(
        {"key": KEY, "outcome": "returned", "note": "needs a product call", "caller": "tok-orch"}
    )
    row = _row(engine)
    assert (row["level"], row["state"]) == ("round", "needs a product call")
    assert row["note"]["text"] == "orchestrator: needs a product call"
    # The note goes after a day.
    clock.now += 86400
    assert _row(engine)["note"] is None


# --- 5. orchestrator_hold ------------------------------------------------------


async def test_orchestrator_hold_is_the_orchestrators_and_reads_its_note_in_flight():
    engine, clock = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "completed")
    engine._tokens["tok-a1"] = (FEAT, "a1")
    methods = EngineMethods(engine)
    for caller in (None, "tok-a1"):
        with pytest.raises(ValueError, match="Only the orchestrator holds"):
            await methods.orchestrator_hold({"key": KEY, "note": "x", "caller": caller})
    with pytest.raises(ValueError, match="note is required"):
        await methods.orchestrator_hold({"key": KEY, "note": " ", "caller": "tok-orch"})
    with pytest.raises(ValueError, match="ttl_s must be between"):
        await methods.orchestrator_hold(
            {"key": KEY, "note": "x", "ttl_s": MAX_HOLD_TTL + 1, "caller": "tok-orch"}
        )
    seen = []
    engine.events.subscribe(lambda e: seen.append(e.data), {HOLD_CREATED})

    result = await methods.orchestrator_hold(
        {"key": KEY, "note": "verifying tests", "caller": "tok-orch"}
    )
    hold = result["hold"]
    assert (hold["until"], hold["by"], hold["auto"]) == (NOW + 7200, "orchestrator", False)
    assert seen == [{"hold": hold}]
    row = _row(engine)
    assert (row["zone"], row["ball"], row["state"], row["hold"]) == (
        "in_flight", "agent", "orchestrator · verifying tests", hold,
    )
    # A hold widens no mandate: the agent's question still shows to the user
    # (no orchestrator routing for it here: the orchestrator is the holder,
    # but the question goes by the usual rules).
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    assert _row(engine)["level"] == "now"
    _agent(engine, FEAT, "a1", "completed")

    # Expired: back to the user, "orchestrator did not respond".
    clock.now += 7200
    row = _row(engine)
    assert (row["level"], row["state"], row["hold"]) == ("round", NO_RESPONSE, None)


async def test_a_released_hold_leaves_its_note_and_the_user_may_take_it_back():
    engine, _ = _engine()
    _orchestrator(engine)
    methods = EngineMethods(engine)
    engine.claims.hold(KEY, "waiting on the merge")
    result = await methods.orchestrator_release(
        {"key": KEY, "outcome": "done", "note": "merged", "caller": "tok-orch"}
    )
    assert (result["claim"], result["hold"]["note"]) == (None, "waiting on the merge")
    assert _row(engine)["note"]["text"] == "orchestrator: merged"
    engine.claims.hold(KEY, "waiting on the merge")
    await methods.status_gesture({"key": KEY, "kind": "clear"})
    assert _row(engine)["hold"] is None


async def test_the_orchestrators_workers_finished_work_is_held_for_it_first():
    engine, clock = _engine(hold_spawned_ttl_min=30)
    orch = _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    row = _row(engine)
    assert row["hold"]["auto"] is True and row["hold"]["until"] == NOW + 1800
    assert (row["zone"], row["state"]) == ("in_flight", "orchestrator · checking agent a1's work")
    assert row["agents"][0]["spawned_by"] == "orchestrator"
    # The orchestrator is woken with one line.
    text = orch.send_input.await_args.args[0]
    assert text.startswith(f"Item {KEY}: agent a1 (spawned by you) — agent finished: review: agent")
    # Not answered in time: the user's, and never held again for it.
    clock.now += 1800
    row = _row(engine)
    assert (row["level"], row["state"]) == ("round", NO_RESPONSE)
    [back] = [i for i in engine.actionables.list()["items"] if i["kind"] == "delegation"]
    engine.actionables.mark(back["id"], "dismiss")
    row = _row(engine)
    assert (row["hold"], row["level"], row["state"]) == (None, "deep", "review: agent finished")


async def test_a_verified_auto_hold_is_not_the_users_any_more():
    engine, _ = _engine()
    _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    _agent(engine, FEAT, "a1", "completed")
    assert _row(engine)["hold"] is not None
    await EngineMethods(engine).orchestrator_release(
        {"key": KEY, "outcome": "done", "note": "tests pass, PR opened", "caller": "tok-orch"}
    )
    row = _row(engine)
    assert (row["zone"], row["needs_you"], row["note"]["text"]) == (
        "in_flight", None, "orchestrator: tests pass, PR opened",
    )


async def test_no_auto_hold_for_the_users_agents_or_when_turned_off():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "completed")
    assert (_row(engine)["hold"], _row(engine)["level"]) == (None, "deep")
    engine, _ = _engine(hold_spawned_ttl_min=0)
    _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    _agent(engine, FEAT, "a1", "completed")
    assert _row(engine)["hold"] is None


async def test_spawn_with_the_orchestrators_token_records_spawned_by():
    engine, _ = _engine()
    _orchestrator(engine)
    engine.spawn_agent = AsyncMock(return_value=SimpleNamespace(agent_id="a7", worktree_path=FEAT))
    await EngineMethods(engine).spawn_agent({"worktree_path": FEAT, "caller": "tok-orch"})
    await EngineMethods(engine).spawn_agent({"worktree_path": FEAT})
    assert engine.overlay.spawned_by(KEY) == {"a7": "orchestrator"}


# --- 3. Parks -----------------------------------------------------------------


async def test_parks_until_tomorrow_a_week_or_indefinitely():
    engine, clock = _engine()
    methods = EngineMethods(engine)
    result = await methods.status_gesture({"key": KEY, "kind": "park", "until": "tomorrow"})
    until = result["gesture"]["until"]
    assert until == tomorrow_at(NOW)
    at = time.localtime(until)
    assert (at.tm_hour, at.tm_min) == (8, 0) and until > NOW
    assert _row(engine)["zone"] == "parked"

    await methods.status_gesture(
        {"key": KEY, "kind": "park", "until": "indefinite", "text": "after the release"}
    )
    clock.now += 30 * 86400
    row = _row(engine)
    assert (row["zone"], row["state"], row["gesture"]["text"]) == (
        "parked", "parked: after the release", "after the release",
    )
    await methods.status_gesture({"key": KEY, "kind": "clear"})
    assert _row(engine)["zone"] == "in_flight"

    await methods.status_gesture({"key": KEY, "kind": "park", "until": clock.now + 7 * 86400})
    clock.now += 7 * 86400
    assert _row(engine)["zone"] == "in_flight"


@pytest.mark.parametrize("change", ["ci", "review", "commit", "agent"])
async def test_a_park_until_something_changes(change):
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN, review_decision="REVIEW_REQUIRED")
    _agent(engine, FEAT, "a1", "waiting")
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "park", "until": "change"})
    assert _row(engine)["state"] == "parked until something changes"
    assert _row(engine)["zone"] == "parked"
    if change == "ci":
        engine.worktree_service._pr_infos[FEAT] = _pr(RUNNING_CI, review_decision="REVIEW_REQUIRED")
    elif change == "review":
        engine.worktree_service._pr_infos[FEAT] = _pr(GREEN, review_decision="APPROVED")
    elif change == "commit":
        engine.worktree_service.worktrees[1] = WorktreeInfo(FEAT, "def456", "feat", False, False)
    else:
        _agent(engine, FEAT, "a1", "running")
    assert _row(engine)["zone"] != "parked"


async def test_an_inbox_item_parks_until_something_changes_too():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    [item] = engine.actionables.list()["items"]
    engine.actionables.mark(item["id"], "park", until="change")
    assert engine.actionables.list()["items"] == []
    engine.worktree_service._pr_infos[FEAT] = _pr(RUNNING_CI)
    engine.actionables.list()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    assert [i["kind"] for i in engine.actionables.list()["items"]] == ["ci"]


async def test_the_whole_work_item_parks_with_its_inbox_items():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "park", "until": "indefinite"})
    result = engine.status.list()
    [row] = [r for r in result["rows"] if r["key"] == KEY]
    assert (row["zone"], row["level"], row["needs_you"]["count"], row["holder"]) == (
        "parked", None, 1, "user",
    )
    counts = result["counts"]
    assert (counts["round"], counts["in_flight"], counts["parked"], counts["parked_needs_you"]) == (
        0, 0, 1, 1,
    )
    assert "PARKED 1 (1 need you)" in status_table(result)


def status_table(result):
    from lazyagent.engine.status import format_table

    return format_table(result, NOW)


async def test_a_parked_items_question_goes_to_the_orchestrator_if_it_may():
    engine, _ = _engine()
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "park", "until": "indefinite"})
    _agent(engine, FEAT, "a1", "waiting_for_user", "Which db?")
    # No orchestrator: the question stays in Parked, marked as needing you.
    result = engine.status.list()
    assert result["counts"]["parked_needs_you"] == 1
    # With one, it is routed (the mandate allows a quick reversible question).
    _drop(engine, FEAT, "a1")
    _orchestrator(engine)
    _agent(engine, FEAT, "a2", "waiting_for_user", "Which db?")
    engine.status.flush()
    result = engine.status.list()
    [row] = [r for r in result["rows"] if r["key"] == KEY]
    assert (row["zone"], row["needs_you"], row["holder"]) == ("parked", None, "orchestrator")
    assert result["counts"]["parked_needs_you"] == 0
    # Kept for the user: no routing; it waits in Parked.
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "keep_for_me"})
    engine.status.flush()
    assert engine.status.list()["counts"]["parked_needs_you"] == 1


# --- 7. Delegation to the branch's agent -------------------------------------


async def _delegated(engine, **params):
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    session = _agent(engine, FEAT, "a1", "waiting")
    result = await EngineMethods(engine).status_gesture(
        {"key": KEY, "kind": "delegate", "to": "agent", **params}
    )
    await _pass(engine)
    return session, result


async def test_a_delegated_item_is_the_agents_until_its_turn_ends_with_the_fact_still_true():
    engine, _ = _engine()
    session, result = await _delegated(engine)
    d = result["delegation"]
    assert (d["agent"], d["item"], d["phase"], d["fact"]) == ("a1", "Ci:/tmp/repo-feat:7", "on_it",
                                                             "CI red: tests")
    assert session.send_input.await_args.args[0].startswith(f"Item {KEY}: CI failing: CI red: tests.")
    row = _row(engine)
    assert (row["zone"], row["ball"], row["state"]) == ("in_flight", "agent", "agent a1 · on it")
    assert _timeline(engine)[-1] == "delegated to agent a1: CI red: tests"
    # The agent works, and ends its turn; CI is still red.
    _agent(engine, FEAT, "a1", "running")
    assert _row(engine)["state"] == "agent a1 · on it"
    _agent(engine, FEAT, "a1", "waiting")
    row = _row(engine)
    assert (row["level"], row["state"]) == ("round", "agent a1 finished; still CI red: tests")
    assert _timeline(engine)[-1] == "agent a1 finished; still CI red: tests"
    # CI goes green: over.
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN)
    engine.status.flush()
    assert engine.delegations.get(KEY) is None


async def test_a_delegation_ends_when_the_fact_clears():
    engine, _ = _engine()
    await _delegated(engine)
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN)
    _agent(engine, FEAT, "a1", "waiting")
    row = _row(engine)
    assert (row["delegation"], row["zone"]) == (None, "in_flight")
    assert _timeline(engine)[-1] == "agent a1 finished: CI red: tests cleared"


async def test_the_agents_question_shows_instead_of_the_delegated_fact():
    engine, _ = _engine()
    await _delegated(engine, text="Fix the red CI, please.")
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    _agent(engine, FEAT, "a1", "waiting_for_user", "Skip the flaky test?")
    row = _row(engine)
    assert (row["level"], row["state"]) == ("now", "answer: Skip the flaky test?")
    assert row["delegation"]["phase"] == "on_it"


async def test_delegation_needs_a_live_agent_and_a_fact():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="No agent runs on feat"):
        await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent"})
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    with pytest.raises(ValueError, match="is answered, not delegated"):
        await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent",
                                      "item": "Approval:/tmp/repo-feat/a1"})
    with pytest.raises(ValueError, match="No inbox item"):
        await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent",
                                      "item": "Ci:/elsewhere:1"})


async def test_the_agent_gone_returns_the_item_and_clear_takes_it_back():
    engine, _ = _engine()
    await _delegated(engine)
    _drop(engine, FEAT, "a1")
    row = _row(engine)
    assert (row["level"], row["state"]) == ("round", "agent a1 gone; still CI red: tests")
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "clear"})
    assert (_row(engine)["delegation"], _row(engine)["state"]) == (None, "CI red: tests")


# --- 6. Already merged, through the projection --------------------------------


async def test_the_projection_asks_git_once_per_head_whether_a_pr_is_already_merged(monkeypatch):
    from lazyagent.engine import status as status_mod

    asked = []

    def fake(repo_root, head, targets):
        asked.append((head, tuple(targets)))
        return "develop" if head == "abc123" else None

    monkeypatch.setattr(status_mod, "merged_into", fake)
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN, review_decision="REVIEW_REQUIRED")
    await engine.status.refresh_merged()
    row = _row(engine)
    assert (row["ball"], row["state"], row["suggestion"]) == (
        "done", "already merged into develop", "close PR #7: its head is in develop",
    )
    await engine.status.refresh_merged()
    assert asked == [("abc123", ("develop", "main", "master"))], "and the default branch"
    # A new head (from the PR) is asked again.
    engine.worktree_service._pr_infos[FEAT] = _pr(GREEN, head_sha="fff999")
    await engine.status.refresh_merged()
    assert asked[-1][0] == "fff999" and _row(engine)["ball"] == "others"
