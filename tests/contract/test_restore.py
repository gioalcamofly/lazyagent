"""list_restorable / restore_agents / dismiss_restorable across an engine
restart, validated strictly against protocol/schema/v1."""

from __future__ import annotations

import pytest

from lazyagent.engine.client import RequestError
from lazyagent.protocol import validation
from tests.contract.harness import NotSupported
from tests.contract.helpers import checked, spawn, wait_for_text

pytestmark = pytest.mark.asyncio


async def _restarted(harness):
    try:
        await harness.restart()
    except NotSupported as e:
        pytest.skip(str(e))
    return await harness.connect()


async def test_nothing_to_restore_on_a_fresh_engine(client):
    assert await checked(client, "list_restorable") == {"agents": []}
    assert await checked(client, "restore_agents") == {"restored": [], "failed": []}
    assert await checked(client, "dismiss_restorable") == {"dismissed": []}


async def test_agents_come_back_after_a_restart(harness, client):
    agent = await spawn(client, harness.feat)
    await checked(
        client,
        "send_agent_input",
        worktree_path=harness.feat,
        agent_id=agent["agent_id"],
        text="remember me",
    )
    client = await _restarted(harness)
    [entry] = (await checked(client, "list_restorable"))["agents"]
    assert entry["worktree_path"] == harness.feat
    assert entry["agent_id"] == agent["agent_id"]
    assert entry["kind"] == "agent"
    assert entry["instruction"] == "remember me"

    await checked(client, "subscribe", events=["engine.restored"])
    result = await checked(client, "restore_agents", ids=[entry["id"]])
    [restored] = result["restored"]
    assert result["failed"] == []
    assert restored["id"] == entry["id"]
    assert restored["agent_id"] == agent["agent_id"]
    assert restored["resumed"] == "last"
    event = await client.next_event(lambda e: e.event == "engine.restored")
    validation.validate_event("engine.restored", event.data, strict=True)
    assert event.data == result
    await wait_for_text(client, restored, "your turn>")
    listed = await checked(client, "list_agents", worktree_path=harness.feat)
    assert [a["agent_id"] for a in listed["agents"]] == [agent["agent_id"]]
    assert (await checked(client, "list_restorable")) == {"agents": []}


async def test_an_unknown_id_fails_alone(harness, client):
    await spawn(client, harness.feat)
    client = await _restarted(harness)
    [entry] = (await checked(client, "list_restorable"))["agents"]
    result = await checked(client, "restore_agents", ids=["nope", entry["id"]])
    assert [r["id"] for r in result["restored"]] == [entry["id"]]
    assert [f["id"] for f in result["failed"]] == ["nope"]


async def test_dismissed_agents_are_no_longer_offered(harness, client):
    await spawn(client, harness.feat)
    await spawn(client, harness.feat)
    client = await _restarted(harness)
    first, second = (await checked(client, "list_restorable"))["agents"]
    assert await checked(client, "dismiss_restorable", ids=[first["id"]]) == {
        "dismissed": [first["id"]]
    }
    assert [a["id"] for a in (await checked(client, "list_restorable"))["agents"]] == [
        second["id"]
    ]
    assert await checked(client, "dismiss_restorable") == {"dismissed": [second["id"]]}


async def test_ids_must_be_strings(client):
    with pytest.raises(RequestError) as e:
        await client.request("restore_agents", ids="all")
    assert e.value.code == "VALIDATION_ERROR"
