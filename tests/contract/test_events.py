"""subscribe: live events in seq order, replay with since, filters, and
unknown events passing through."""

from __future__ import annotations

import pytest

from lazyagent.protocol import validation
from tests.contract.harness import NotSupported
from tests.contract.helpers import checked, events_until, spawn

pytestmark = pytest.mark.asyncio


def _valid(events):
    for event in events:
        validation.validate_message(event.to_dict(), strict=True)
        if event.event in validation.event_names():
            validation.validate_event(event.event, event.data, strict=True)
    return events


async def test_live_events_arrive_in_seq_order(harness, client):
    sub = await checked(client, "subscribe")
    agent = await spawn(client, harness.feat)
    await checked(client, "stop_agent", worktree_path=harness.feat)
    events = _valid(await events_until(client, lambda e: e.event == "agent.exited"))
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(seq > sub["cursor"] for seq in seqs)
    names = [e.event for e in events]
    assert names.index("agent.spawned") < names.index("agent.exited")
    spawned = next(e for e in events if e.event == "agent.spawned")
    assert spawned.data["session_id"] == agent["session_id"]


async def test_since_replays_history_then_goes_live(harness, client):
    await spawn(client, harness.feat)
    other = await harness.connect()
    sub = await checked(other, "subscribe", since=0)
    assert sub["replayed"] > 0 and sub["truncated"] is False
    replayed = _valid([await other.next_event() for _ in range(sub["replayed"])])
    assert [e.seq for e in replayed] == sorted(e.seq for e in replayed)
    assert replayed[-1].seq == sub["cursor"]
    assert "agent.spawned" in [e.event for e in replayed]

    await checked(client, "stop_agent", worktree_path=harness.feat)
    live = _valid(await events_until(other, lambda e: e.event == "agent.exited"))
    # One ordered log: the live events carry on right after the replay.
    assert live[0].seq == sub["cursor"] + 1
    assert [e.seq for e in live] == list(range(live[0].seq, live[0].seq + len(live)))


async def test_since_in_the_middle(harness, client):
    await checked(client, "subscribe")
    await spawn(client, harness.feat)
    everything = _valid(client.pending_events())
    middle = everything[len(everything) // 2].seq
    other = await harness.connect()
    sub = await checked(other, "subscribe", since=middle)
    replayed = [await other.next_event() for _ in range(sub["replayed"])]
    assert [e.seq for e in replayed] == [e.seq for e in everything if e.seq > middle]


async def test_event_filter(harness, client):
    await checked(client, "subscribe", events=["agent.spawned"])
    await spawn(client, harness.feat)
    await checked(client, "stop_agent", worktree_path=harness.feat)
    await checked(client, "list_worktrees")
    first = await client.next_event()
    assert first.event == "agent.spawned"
    assert [e.event for e in client.pending_events()] == []


async def test_resubscribe_replaces_and_unsubscribe_stops(harness, client):
    await checked(client, "subscribe", events=["agent.spawned"])
    await checked(client, "subscribe", events=["agent.exited"])
    await spawn(client, harness.feat)
    await checked(client, "stop_agent", worktree_path=harness.feat)
    event = await client.next_event()
    assert event.event == "agent.exited"
    await checked(client, "unsubscribe")
    await spawn(client, harness.feat)
    await checked(client, "list_worktrees")
    assert client.pending_events() == []


async def test_unknown_events_reach_the_client_and_nothing_breaks(harness, client):
    await checked(client, "subscribe")
    try:
        harness.emit("future.event", {"anything": [1, 2, 3]})
    except NotSupported:
        pytest.skip("the engine cannot be made to emit an arbitrary event")
    unknown = await client.next_event()
    assert unknown.event == "future.event"
    validation.validate_message(unknown.to_dict(), strict=True)
    await spawn(client, harness.feat)
    await client.next_event(lambda e: e.event == "agent.spawned")


async def test_two_subscribers_see_the_same_order(harness, client):
    other = await harness.connect()
    await checked(client, "subscribe")
    await checked(other, "subscribe")
    await spawn(client, harness.feat)
    await checked(client, "stop_agent", worktree_path=harness.feat)
    exited = lambda e: e.event == "agent.exited"
    a = [(e.seq, e.event) for e in await events_until(client, exited)]
    b = [(e.seq, e.event) for e in await events_until(other, exited)]
    assert a == b and a
