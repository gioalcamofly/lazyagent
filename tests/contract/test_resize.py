"""PTY size: one owner per session, the latest client to claim it."""

from __future__ import annotations

import pytest

from lazyagent.engine.client import RequestError
from lazyagent.protocol import validation
from tests.contract.harness import NotSupported
from tests.contract.helpers import (
    Output,
    checked,
    events_until,
    spawn,
    until,
    wait_for_text,
)

pytestmark = pytest.mark.asyncio


async def _size(client, worktree_path) -> dict:
    listed = await checked(client, "list_agents", worktree_path=worktree_path)
    return listed["agents"][0]["size"]


async def _not_owner(client, session_id, rows=30, cols=100):
    with pytest.raises(RequestError) as e:
        await client.request("resize", session_id=session_id, rows=rows, cols=cols)
    assert e.value.code == "NOT_SIZE_OWNER"


async def test_resize_needs_the_size(harness, client):
    agent = await spawn(client, harness.feat)
    await _not_owner(client, agent["session_id"])
    claimed = await checked(client, "claim_size", session_id=agent["session_id"])
    assert claimed == {
        "session_id": agent["session_id"],
        "rows": 24,
        "cols": 80,
        "owner": True,
    }
    resized = await checked(
        client, "resize", session_id=agent["session_id"], rows=30, cols=100
    )
    assert (resized["rows"], resized["cols"]) == (30, 100)
    assert await _size(client, harness.feat) == {"rows": 30, "cols": 100}


async def test_the_child_sees_the_new_size(harness, client):
    agent = await spawn(client, harness.feat)
    await checked(
        client, "claim_size", session_id=agent["session_id"], rows=33, cols=111
    )
    stream = await client.attach(agent["session_id"], Output())
    client.send_input(stream.stream_id, b"size\r")
    await wait_for_text(client, agent, "size 33x111")


async def test_latest_claim_wins_and_everyone_hears_the_size(harness, client):
    agent = await spawn(client, harness.feat)
    other = await harness.connect()
    sa = await client.attach(agent["session_id"], Output())
    sb = await other.attach(agent["session_id"], Output())

    await checked(client, "claim_size", session_id=agent["session_id"])
    await checked(client, "resize", session_id=agent["session_id"], rows=30, cols=100)
    for conn, stream in ((client, sa), (other, sb)):
        [event] = await events_until(conn, lambda e: e.event == "stream.resized")
        validation.validate_event("stream.resized", event.data, strict=True)
        assert event.data["stream_id"] == stream.stream_id
        assert (event.data["rows"], event.data["cols"]) == (30, 100)
        assert (stream.rows, stream.cols) == (30, 100)
    await _not_owner(other, agent["session_id"])

    await checked(other, "claim_size", session_id=agent["session_id"], rows=20, cols=70)
    await _not_owner(client, agent["session_id"])
    assert await _size(client, harness.feat) == {"rows": 20, "cols": 70}


async def test_input_claims_the_size(harness, client):
    agent = await spawn(client, harness.feat)
    other = await harness.connect()
    await checked(client, "claim_size", session_id=agent["session_id"])
    stream = await other.attach(agent["session_id"], Output())
    other.send_input(stream.stream_id, b"x")
    await until(lambda: True)
    # Frames and requests share the connection, so the claim is in by now.
    await checked(other, "resize", session_id=agent["session_id"], rows=25, cols=90)
    await _not_owner(client, agent["session_id"])


async def test_the_size_survives_its_clients(harness, client):
    agent = await spawn(client, harness.feat)
    other = await harness.connect()
    await checked(
        other, "claim_size", session_id=agent["session_id"], rows=40, cols=120
    )
    await other.close()
    assert await _size(client, harness.feat) == {"rows": 40, "cols": 120}
    # Its owner left: anyone may claim it now, but must claim first.
    await _not_owner(client, agent["session_id"])
    await checked(client, "claim_size", session_id=agent["session_id"])


async def test_resize_validates_the_size(harness, client):
    agent = await spawn(client, harness.feat)
    await checked(client, "claim_size", session_id=agent["session_id"])
    response = await client.call(
        "resize", {"session_id": agent["session_id"], "rows": 0, "cols": 10}
    )
    assert response.error.code == "VALIDATION_ERROR"


async def test_an_in_process_resize_takes_the_size_back(harness, client):
    agent = await spawn(client, harness.feat)
    stream = await client.attach(agent["session_id"], Output())
    await checked(client, "claim_size", session_id=agent["session_id"])
    try:
        harness.local_resize(agent["session_id"], 50, 160)
    except NotSupported:
        pytest.skip("no in-process client to resize with")
    [event] = await events_until(client, lambda e: e.event == "stream.resized")
    assert (event.data["rows"], event.data["cols"]) == (50, 160)
    assert (stream.rows, stream.cols) == (50, 160)
    await _not_owner(client, agent["session_id"])
