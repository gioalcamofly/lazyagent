"""The ball principle (proposal 0013, I4i): an item the branch's agent can
resolve, while that agent has the ball (it runs, or its status_report holds
it), does not interrupt the user; it folds into the row in flight as
``agent a1 · CI red · <note> · until HH:MM``. Approvals, questions and
briefs still interrupt; the item is back when the until lapses."""

from __future__ import annotations

import time

import pytest

from lazyagent.engine import actionables as act
from tests.engine.test_home_polish import FEAT, KEY, RED, _agent, _engine, _pr, _row

pytestmark = pytest.mark.asyncio


def _kinds(engine):
    engine.status.flush()
    return sorted(i["kind"] for i in engine.actionables.list()["items"] if i["work_item"] == KEY)


def _report(engine, now, ball="agent", until=3600.0, note="handling the conflict"):
    engine.overlay.report(KEY, FEAT, "a1", note=note, ball=ball, needs_user=False,
                          label="Agent a1", now=now, until=None if until is None else now + until)
    engine.status.mark_dirty()


def _hhmm(at):
    return time.strftime("%H:%M", time.localtime(at))


async def test_ci_red_and_a_conflict_fold_while_the_agents_report_holds_the_ball():
    engine, clock = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED, mergeable="CONFLICTING")
    _agent(engine, FEAT, "a1", "waiting")
    assert _kinds(engine) == ["ci", "conflict"]
    _report(engine, clock.now)
    assert _kinds(engine) == []
    row = _row(engine)
    assert (row["zone"], row["ball"]) == ("in_flight", "agent")
    assert row["state"] == (
        f"agent a1 · conflict + CI red · handling the conflict · until {_hhmm(clock.now + 3600)}"
    )
    # The until lapses: back to the user's Round.
    clock.now += 3601
    assert _kinds(engine) == ["ci", "conflict"]
    assert _row(engine)["level"] == "round"


async def test_a_running_agent_folds_them_too():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED, mergeable="CONFLICTING")
    _agent(engine, FEAT, "a1", "running")
    assert _kinds(engine) == []


async def test_ball_others_folds_only_with_an_until():
    engine, clock = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(mergeable="CONFLICTING")
    _agent(engine, FEAT, "a1", "waiting")
    _report(engine, clock.now, ball="others", until=None, note="waiting for QA")
    assert _kinds(engine) == ["conflict"]
    _report(engine, clock.now, ball="others", until=1800.0, note="waiting for QA")
    assert _kinds(engine) == []


async def test_what_the_user_must_answer_still_interrupts():
    engine, clock = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    _agent(engine, FEAT, "a1", "waiting")
    _report(engine, clock.now)
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    assert _kinds(engine) == ["approval"]
    assert _row(engine)["level"] == "now"


async def test_no_hanged_while_its_report_holds():
    engine, clock = _engine()
    _agent(engine, FEAT, "a1", "possibly_hanged")
    assert _kinds(engine) == ["hanged"]
    _report(engine, clock.now, note="long build")
    assert _kinds(engine) == []


async def test_no_exited_while_another_agent_runs_there():
    engine, _ = _engine()
    _agent(engine, FEAT, "a2", "running")
    engine.actionables._exits.append(act.Exit(FEAT, "a1", "Agent a1", 0, 5.0, KEY))
    assert "exited" not in _kinds(engine)


async def test_a_delegation_is_not_done_while_its_agents_report_folds_the_item():
    from lazyagent.engine.methods import EngineMethods
    from tests.engine.test_home_polish import _pass

    engine, clock = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    _agent(engine, FEAT, "a1", "waiting")
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "delegate", "to": "agent"})
    await _pass(engine)
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    # It ends its turn waiting on a long build: CI is folded, not cleared.
    _report(engine, clock.now + 1, note="waiting on the build")
    _agent(engine, FEAT, "a1", "waiting")
    engine.status.flush()
    assert engine.delegations.get(KEY)["phase"] == "on_it"
