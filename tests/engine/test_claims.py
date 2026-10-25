"""Pass to orchestrator (proposal 0013 §5c, I3): a claim with a TTL that the
user makes, the orchestrator releases, and that lapses back to the user's
Round when it expires or its orchestrator exits."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.engine.claims import CLAIM_CREATED, CLAIM_LAPSED, CLAIM_RELEASED
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
FIX = "/tmp/repo-fix"
KEY = "repo:feat"


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
        session.send_input = AsyncMock()
        add_session(engine, session)


def _drop(engine, path, agent_id):
    engine.registry.states.get(path, {}).pop(agent_id, None)
    engine._agents.get(path, {}).pop(agent_id, None)


def _orchestrator(engine, status="running"):
    _agent(engine, ORCHESTRATOR_KEY, "", status)
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")


def _row(engine, key=KEY):
    return next(r for r in engine.status.list(include_done=True)["rows"] if r["key"] == key)


def _events(engine, names):
    seen = []
    engine.events.subscribe(lambda e: seen.append((e.event, e.data)), names)
    return seen


async def test_there_is_nothing_to_pass_to_with_no_orchestrator():
    engine, _ = _engine()
    with pytest.raises(ValueError, match="No orchestrator is running"):
        await EngineMethods(engine).status_gesture({"key": KEY, "kind": "delegate"})


async def test_a_pass_takes_the_item_out_of_the_inbox_but_not_its_approvals():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "completed")
    assert _row(engine)["level"] == "deep"
    seen = _events(engine, {CLAIM_CREATED})

    result = await EngineMethods(engine).status_gesture(
        {"key": KEY, "kind": "delegate", "to": "orchestrator"}
    )
    claim = result["claim"]
    assert (claim["holder"], claim["by"], claim["until"]) == ("orchestrator", "user", NOW + 7200)
    assert "merge_default_branch" not in claim["allowed"]
    row = _row(engine)
    assert (row["zone"], row["ball"], row["state"], row["claim"]) == (
        "in_flight", "agent", "orchestrator · looking", claim,
    )
    assert result["row"] == row
    assert seen == [(CLAIM_CREATED, {"claim": claim})]
    # The queue still has the item; only the user's inbox lost it.
    assert [i["kind"] for i in engine.actionables.list()["items"]] == ["done"]

    # A tool approval is never the orchestrator's: it stays in Now.
    _agent(engine, FEAT, "a2", "waiting_for_approval", "Bash")
    row = _row(engine)
    assert (row["level"], row["state"], row["needs_you"]["count"]) == (
        "now", "approve: Bash", 1,
    )


async def test_an_expired_claim_comes_back_to_the_round_until_dismissed():
    engine, clock = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    engine.claims.claim(KEY, ttl_s=600)
    seen = _events(engine, {CLAIM_LAPSED})

    clock.now += 599
    assert _row(engine)["claim"] is not None
    clock.now += 1
    row = _row(engine)
    assert (row["level"], row["state"], row["claim"]) == ("round", "delegation lapsed", None)
    assert [(e, d["reason"]) for e, d in seen] == [(CLAIM_LAPSED, "expired")]
    [item] = engine.actionables.list()["items"]
    assert (item["kind"], item["title"], item["work_item"]) == (
        "delegation", "Back from the orchestrator", KEY,
    )

    engine.actionables.mark(item["id"], "dismiss")
    assert engine.claims.returned == {}
    assert _row(engine)["ball"] == "agent"


async def test_the_orchestrator_exiting_lapses_its_claims():
    engine, _ = _engine()
    _orchestrator(engine)
    engine.claims.claim(KEY)
    seen = _events(engine, {CLAIM_LAPSED})
    _drop(engine, ORCHESTRATOR_KEY, "")
    assert _row(engine)["state"] == "delegation lapsed"
    assert [d["reason"] for _, d in seen] == ["orchestrator_gone"]


async def test_the_orchestrator_releases_done_or_returned():
    engine, _ = _engine()
    _orchestrator(engine)
    methods = EngineMethods(engine)
    seen = _events(engine, {CLAIM_RELEASED})
    engine.claims.claim(KEY)
    engine.claims.claim("repo:fix")

    done = await methods.orchestrator_release(
        {"key": KEY, "outcome": "done", "caller": "tok-orch"}
    )
    assert done["outcome"] == "done"
    assert _row(engine)["claim"] is None and _row(engine)["zone"] == "in_flight"

    await methods.orchestrator_release(
        {"key": "repo:fix", "outcome": "returned", "note": "needs  a product\ncall",
         "caller": "tok-orch"}
    )
    row = _row(engine, "repo:fix")
    assert (row["level"], row["state"]) == ("round", "needs a product call")
    # Both releases leave the note on the row and the timeline.
    assert row["note"]["text"] == "orchestrator: needs a product call"
    assert [e["text"] for e in engine.overlay.entries("repo:fix") if e["kind"] == "delegate"][-1] \
        == "orchestrator handed back: needs a product call"
    assert [(d["key"], d["outcome"], d["note"]) for _, d in seen] == [
        (KEY, "done", ""), ("repo:fix", "returned", "needs a product call"),
    ]
    # Released already: nothing to release, not an error.
    again = await methods.orchestrator_release({"key": KEY, "outcome": "done", "caller": "tok-orch"})
    assert again["outcome"] == "nothing_to_release"


async def test_who_may_do_what():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    engine._tokens["tok-a1"] = (FEAT, "a1")
    methods = EngineMethods(engine)
    for caller in ("tok-a1", "tok-orch"):
        with pytest.raises(ValueError, match="Only the user"):
            await methods.status_gesture({"key": KEY, "kind": "delegate", "caller": caller})
    engine.claims.claim(KEY)
    for params in ({}, {"caller": "tok-a1"}):
        with pytest.raises(ValueError, match="Only the orchestrator"):
            await methods.orchestrator_release({"key": KEY, "outcome": "done", **params})
    with pytest.raises(ValueError, match="outcome must be one of"):
        await methods.orchestrator_release({"key": KEY, "outcome": "merged", "caller": "tok-orch"})
    with pytest.raises(ValueError, match="kind must be one of"):
        await methods.status_gesture({"key": KEY, "kind": "stamp"})
    with pytest.raises(ValueError, match="No work item"):
        await methods.status_gesture({"key": "repo:nope", "kind": "delegate"})
    with pytest.raises(ValueError, match="ttl_s must be between"):
        await methods.status_gesture({"key": KEY, "kind": "delegate", "ttl_s": 5})


async def test_a_done_item_cannot_be_passed():
    engine, _ = _engine()
    _orchestrator(engine)
    engine.worktree_service._pr_infos[FEAT] = PrInfo(7, "x", "MERGED", [])
    with pytest.raises(ValueError, match="is done"):
        engine.claims.claim(KEY)


async def test_the_user_takes_it_back():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "completed")
    engine.claims.claim(KEY)
    seen = _events(engine, {CLAIM_RELEASED})
    result = await EngineMethods(engine).status_gesture({"key": KEY, "kind": "clear"})
    assert result["claim"] is None and result["row"]["level"] == "deep"
    assert [d["outcome"] for _, d in seen] == ["taken_back"]
    assert engine.claims.returned == {}, "taking it back raises nothing"
    with pytest.raises(ValueError, match="not with the orchestrator"):
        engine.claims.clear(KEY)


async def test_the_orchestrator_is_woken_once_its_turn_is_over():
    engine, clock = _engine()
    _orchestrator(engine, "running")
    session = engine.get_session(ORCHESTRATOR_KEY, "")
    engine.claims.claim(KEY)
    engine.status.flush()
    await asyncio.sleep(0)
    session.send_input.assert_not_called()

    _agent(engine, ORCHESTRATOR_KEY, "", "waiting")
    engine.status.flush()
    await engine.wait_idle()
    session.send_input.assert_awaited_once()
    text = session.send_input.call_args.args[0]
    assert text.startswith(f"Item {KEY}: passed to you by the user until ")
    assert "orchestrator_release" in text
    # Once only.
    clock.now += 60
    engine.status.flush()
    await engine.wait_idle()
    assert session.send_input.await_count == 1


async def test_a_removed_work_item_drops_its_claim():
    engine, _ = _engine()
    _orchestrator(engine)
    engine.claims.claim(KEY)
    engine.world.worktrees = [w for w in engine.world.worktrees if w.path != FEAT]
    engine.refresh_worktrees()
    engine.status.flush()
    engine.status.flush()
    assert engine.claims.active == {} and engine.claims.returned == {}
