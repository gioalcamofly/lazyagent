"""Reliable delegation (proposal 0013, I4i): Delegate sends every delegable
item of the row in one letter; a second delegation merges into the letter
still waiting (or follows it), never drops it; a letter held by a busy or
possibly hanged agent says so on the row, and Send anyway types it."""

from __future__ import annotations

import pytest

from lazyagent.engine.methods import EngineMethods
from tests.engine.test_home_polish import (
    FEAT,
    GREEN,
    KEY,
    RED,
    _agent,
    _engine,
    _pass,
    _pr,
    _row,
    _timeline,
)

pytestmark = pytest.mark.asyncio

CI = f"Ci:{FEAT}:7"
CONFLICT = f"Conflict:{FEAT}:7"


def _both(engine, checks=RED):
    """CI red and a conflict with the base on the PR."""
    engine.worktree_service._pr_infos[FEAT] = _pr(checks, mergeable="CONFLICTING")


async def _delegate(engine, **params):
    result = await EngineMethods(engine).status_gesture(
        {"key": KEY, "kind": "delegate", "to": "agent", **params}
    )
    await _pass(engine)
    return result["delegation"]


def _ids(d):
    return [i["id"] for i in d["items"]]


def _sent(session):
    return [c.args[0] for c in session.send_input.await_args_list]


async def test_delegate_all_sends_every_delegable_item_in_one_numbered_letter():
    engine, _ = _engine()
    _both(engine)
    session = _agent(engine, FEAT, "a1", "waiting")
    assert {i["id"] for i in _row(engine)["needs_you"]["items"]} == {CI, CONFLICT}
    d = await _delegate(engine)
    assert sorted(_ids(d)) == sorted([CI, CONFLICT])
    [letter] = _sent(session)
    assert letter.startswith(f"The user delegated these 2 items of {KEY} to you")
    assert "\n1. CI failing: CI red: tests." in letter and "\n2. Merge conflict" in letter
    row = _row(engine)
    assert (row["needs_you"], row["zone"]) == (None, "in_flight")
    assert row["state"].startswith("agent a1 · fixing")


async def test_one_clears_and_the_other_comes_back():
    engine, _ = _engine()
    _both(engine)
    _agent(engine, FEAT, "a1", "waiting")
    await _delegate(engine)
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    # The agent fixed the conflict; CI is still red.
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    _agent(engine, FEAT, "a1", "waiting")
    row = _row(engine)
    assert row["delegation"]["phase"] == "returned"
    assert _ids(row["delegation"]) == [CI]
    assert [i["id"] for i in row["needs_you"]["items"]] == [CI]
    assert row["state"] == "agent a1 finished; still CI red: tests"
    last = _timeline(engine)[-1]
    assert last.startswith("agent a1 finished: ") and "cleared; still CI red: tests" in last


async def test_one_item_alone_from_its_own_line():
    engine, _ = _engine()
    _both(engine)
    session = _agent(engine, FEAT, "a1", "waiting")
    d = await _delegate(engine, item=CONFLICT)
    assert _ids(d) == [CONFLICT]
    assert _sent(session)[0].startswith(f"Item {KEY}: ")
    # The other one stays the user's.
    assert [i["id"] for i in _row(engine)["needs_you"]["items"]] == [CI]


async def test_a_second_delegation_merges_into_the_letter_still_waiting():
    engine, _ = _engine()
    _both(engine)
    session = _agent(engine, FEAT, "a1", "possibly_hanged")
    await _delegate(engine, item=CONFLICT)
    mailbox = engine.first_responder.mailbox
    assert mailbox.has(f"delegate:{KEY}")
    d = await _delegate(engine, item=CI)
    # One record, both items, one letter: nothing dropped.
    assert sorted(_ids(d)) == sorted([CI, CONFLICT])
    assert {i["letter"] for i in d["items"]} == {1}
    assert not mailbox.has(f"delegate:{KEY}:2")
    assert session.send_input.await_count == 0
    # The agent is free again: the one letter, with both.
    _agent(engine, FEAT, "a1", "waiting")
    mailbox.flush()
    await _pass(engine)
    [letter] = _sent(session)
    assert "these 2 items" in letter


async def test_a_delegation_after_the_letter_went_follows_it():
    engine, _ = _engine()
    _both(engine)
    session = _agent(engine, FEAT, "a1", "waiting")
    await _delegate(engine, item=CONFLICT)
    _agent(engine, FEAT, "a1", "possibly_hanged")
    engine.status.flush()
    d = await _delegate(engine, item=CI)
    assert sorted(_ids(d)) == sorted([CI, CONFLICT])
    assert [i["letter"] for i in d["items"]] == [1, 2]
    assert d["held"] == "waiting for a1 to be free (possibly hanged)"
    _agent(engine, FEAT, "a1", "waiting")
    engine.first_responder.mailbox.flush()
    await _pass(engine)
    first, follow_up = _sent(session)
    assert first.startswith(f"Item {KEY}: Merge conflict")
    assert follow_up.startswith(f"Item {KEY}: CI failing")
    # Not finished until the agent ran after the follow-up.
    assert engine.delegations.get(KEY)["phase"] == "on_it"


async def test_no_pending_letter_is_dropped_silently():
    engine, _ = _engine()
    _both(engine)
    _agent(engine, FEAT, "a1", "possibly_hanged")
    await _delegate(engine, item=CONFLICT)
    with pytest.raises(ValueError, match="already has it"):
        await _delegate(engine, item=CONFLICT)
    assert engine.first_responder.mailbox.has(f"delegate:{KEY}")
    # Another agent while a1 has it: refused, not replaced.
    _agent(engine, FEAT, "a2", "waiting")
    with pytest.raises(ValueError, match="is with agent a1"):
        await _delegate(engine, item=CI, agent_id="a2")
    assert _ids(engine.delegations.get(KEY)) == [CONFLICT]


async def test_a_held_letter_says_so_on_the_row_and_send_anyway_types_it():
    engine, clock = _engine()
    _both(engine, GREEN)
    session = _agent(engine, FEAT, "a1", "possibly_hanged")
    d = await _delegate(engine)
    assert d["held"] == "waiting for a1 to be free (possibly hanged)"
    assert d["held_since"] == clock.now
    # Its own Hanged? leads the row; the delegation says why it waits.
    row = _row(engine)
    assert (row["state"], row["delegation"]["held"]) == ("agent may hang", d["held"])
    # With nothing else on the row, the row says it.
    _agent(engine, FEAT, "a1", "running")
    assert _row(engine)["state"] == "waiting for a1 to be free (running)"
    assert session.send_input.await_count == 0
    d = (await EngineMethods(engine).status_gesture(
        {"key": KEY, "kind": "delegate", "to": "agent", "send_now": True}))["delegation"]
    await _pass(engine)
    assert session.send_input.await_count == 1
    assert engine.delegations.get(KEY)["held"] == ""
    assert _timeline(engine)[-1] == "sent anyway to agent a1 (running)"
    assert _row(engine)["state"].startswith("agent a1 · fixing")
    with pytest.raises(ValueError, match="already has it"):
        await EngineMethods(engine).status_gesture(
            {"key": KEY, "kind": "delegate", "to": "agent", "send_now": True})


async def test_a_letter_lost_with_the_engine_is_posted_again():
    engine, _ = _engine()
    _both(engine, GREEN)
    session = _agent(engine, FEAT, "a1", "possibly_hanged")
    await _delegate(engine)
    # A restart: the mailbox is in memory, the delegation is not.
    engine.first_responder.mailbox.drop(f"delegate:{KEY}")
    _agent(engine, FEAT, "a1", "waiting")
    engine.status.flush()
    engine.first_responder.mailbox.flush()
    await _pass(engine)
    assert session.send_input.await_count == 1


async def test_pressing_delegate_again_and_again_is_one_timeline_line():
    engine, _ = _engine()
    _both(engine, GREEN)
    _agent(engine, FEAT, "a1", "possibly_hanged")
    await _delegate(engine)
    for _ in range(2):
        await EngineMethods(engine).status_gesture({"key": KEY, "kind": "clear"})
        await _delegate(engine)
    [entry] = [e for e in engine.overlay.entries(KEY) if e["kind"] == "delegate"]
    assert entry["count"] == 3


async def test_a_dismissed_item_leaves_the_delegation_and_undo_brings_it_back():
    engine, _ = _engine()
    _both(engine)
    _agent(engine, FEAT, "a1", "possibly_hanged")
    d = await _delegate(engine)
    # Its own Hanged? is not sent to it.
    assert sorted(_ids(d)) == sorted([CI, CONFLICT])
    engine.delegations.clear(KEY)
    engine.status.flush()
    assert sorted(engine.status.fields_of(KEY)["delegates"]) == sorted([CI, CONFLICT])
    await _delegate(engine)
    undo = engine.delegations._dismissed(engine.actionables.found(CI))
    assert _ids(engine.delegations.get(KEY)) == [CONFLICT]
    undo()
    assert sorted(_ids(engine.delegations.get(KEY))) == sorted([CI, CONFLICT])


async def test_a_record_from_before_i4i_still_reads():
    engine, _ = _engine()
    _both(engine)
    _agent(engine, FEAT, "a1", "waiting")
    engine.overlay.delegations[KEY] = {
        "agent": "a1", "item": CI, "occurrence": f"{CI}@1", "kind": "ci",
        "fact": "CI red: tests", "at": 1.0, "by": "user", "phase": "on_it",
        "sent_at": 2.0, "ran": False, "state": "",
    }
    d = engine.delegations.get(KEY)
    assert (_ids(d), d["held"]) == ([CI], "")
    assert [i["id"] for i in _row(engine)["needs_you"]["items"]] == [CONFLICT]
