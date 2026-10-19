"""Session notes over protocol v1 (proposal 0011): the notes_* methods,
their events and the CONFLICT error, validated strictly against
protocol/schema/v1."""

from __future__ import annotations

import pytest

from lazyagent.engine.client import RequestError
from lazyagent.protocol import validation
from tests.contract.harness import NotSupported
from tests.contract.helpers import checked, spawn

pytestmark = pytest.mark.asyncio


def _target(agent: dict) -> dict:
    return {"worktree_path": agent["worktree_path"], "agent_id": agent["agent_id"]}


async def _event(client, name: str, **match):
    event = await client.next_event(
        lambda e: e.event == name
        and all(e.data.get(k) == v for k, v in match.items())
    )
    validation.validate_event(name, event.data, strict=True)
    return event.data


async def test_get_and_put_round_trip(client, harness):
    agent = await spawn(client, harness.feat)
    notes = await checked(client, "notes_get", **_target(agent))
    assert notes["empty"] and notes["rev"] == 0 and notes["by"] is None
    assert notes["state"] == "live" and notes["max_bytes"] == 8192
    assert "## Decisions" in notes["text"]

    await checked(client, "subscribe", events=["notes.updated"])
    text = notes["text"].replace("## Goal\n", "## Goal\n- ship notes\n")
    put = await checked(client, "notes_put", **_target(agent), text=text, if_rev=0)
    assert (put["rev"], put["by"], put["notes_id"]) == (1, "user", notes["notes_id"])
    assert put["size"] == len(text.encode())
    updated = await _event(client, "notes.updated", notes_id=notes["notes_id"])
    assert (updated["rev"], updated["by"]) == (1, "user")

    again = await checked(client, "notes_get", **_target(agent))
    assert (again["text"], again["rev"], again["empty"]) == (text, 1, False)


async def test_a_stale_put_is_a_conflict_with_the_current_text(client, harness):
    agent = await spawn(client, harness.feat)
    await checked(
        client, "notes_append", **_target(agent), section="Decisions", text="use sqlite"
    )
    with pytest.raises(RequestError) as e:
        await client.request("notes_put", **_target(agent), text="## Goal\n", if_rev=0)
    assert e.value.code == "CONFLICT"
    assert e.value.data["rev"] == 1
    assert "- use sqlite" in e.value.data["text"]
    # Nothing was overwritten.
    notes = await checked(client, "notes_get", **_target(agent))
    assert notes["text"] == e.value.data["text"]


async def test_the_cap_refuses_a_write(client, harness):
    agent = await spawn(client, harness.feat)
    big = await checked(
        client, "notes_append", **_target(agent), section="Pending", text="x" * 6600
    )
    assert "warning" in big
    with pytest.raises(RequestError) as e:
        await client.request(
            "notes_append", **_target(agent), section="Pending", text="y" * 2000
        )
    assert e.value.code == "VALIDATION_ERROR" and "condense" in e.value.message


async def test_stop_returns_the_final_notes(client, harness):
    agent = await spawn(client, harness.feat)
    empty = await spawn(client, harness.feat)
    await checked(
        client, "notes_append", **_target(agent), section="Pending", text="- [ ] tests"
    )
    stopped = await checked(client, "stop_agent", **_target(agent))
    assert "- [ ] tests" in stopped["notes"]
    assert "notes" not in await checked(client, "stop_agent", **_target(empty))
    assert (await checked(client, "notes_list"))["notes"] == []


async def test_an_agent_that_exits_leaves_an_orphan_to_adopt(client, harness):
    # Already running: a fresh spawn would prune the worktree's ended notes.
    agent = await spawn(client, harness.feat)
    crashed = await spawn(client, harness.feat)
    await checked(client, "subscribe", events=["notes.removed", "agent.exited"])
    await checked(
        client, "notes_append", **_target(crashed), section="Decisions", text="use sqlite"
    )
    old = (await checked(client, "notes_get", **_target(crashed)))["notes_id"]
    await checked(client, "send_agent_input", **_target(crashed), text="exit 3")
    removed = await _event(client, "notes.removed", notes_id=old)
    assert removed["reason"] == "orphaned"

    listed = (await checked(client, "notes_list", worktree_path=harness.feat))["notes"]
    assert [n["state"] for n in listed] == ["orphan", "live"]  # newest first
    orphan = listed[0]
    assert (orphan["notes_id"], orphan["empty"]) == (old, False)
    assert orphan["expires_at"] == orphan["ended_at"] + 7 * 86400
    by_id = await checked(client, "notes_get", notes_id=old)
    assert "- use sqlite" in by_id["text"] and by_id["state"] == "orphan"

    adopted = await checked(
        client, "notes_adopt", notes_id=old, **_target(agent), if_rev=0
    )
    assert (adopted["agent_id"], adopted["rev"], adopted["by"]) == (
        agent["agent_id"],
        1,
        "user",
    )
    gone = await _event(client, "notes.removed", notes_id=old, reason="adopted")
    assert gone["agent_id"] == crashed["agent_id"]
    notes = await checked(client, "notes_get", **_target(agent))
    assert notes["text"] == by_id["text"] and notes["notes_id"] == adopted["notes_id"]
    listed = (await checked(client, "notes_list"))["notes"]
    assert [n["notes_id"] for n in listed] == [notes["notes_id"]]


async def test_ended_notes_are_listed_after_a_restart_and_deleted(client, harness):
    agent = await spawn(client, harness.feat)
    await checked(
        client, "notes_append", **_target(agent), section="Doubts", text="- [ ] which db?"
    )
    try:
        await harness.restart()
    except NotSupported as e:
        pytest.skip(str(e))
    client = await harness.connect()
    [ended] = (await checked(client, "notes_list"))["notes"]
    assert ended["state"] == "restorable" and ended["expires_at"] is None
    with pytest.raises(RequestError, match="No notes"):
        await client.request("notes_delete", notes_id="000000000000")
    assert await checked(client, "notes_delete", notes_id=ended["notes_id"]) == {
        "deleted": ended["notes_id"]
    }
    assert (await checked(client, "notes_list"))["notes"] == []


async def test_a_live_agents_notes_cannot_be_deleted_or_adopted(client, harness):
    agent = await spawn(client, harness.feat)
    other = await spawn(client, harness.feat)
    notes_id = (await checked(client, "notes_get", **_target(agent)))["notes_id"]
    with pytest.raises(RequestError, match="live agent"):
        await client.request("notes_delete", notes_id=notes_id)
    with pytest.raises(RequestError, match="live agent"):
        await client.request("notes_adopt", notes_id=notes_id, **_target(other))


async def test_spawn_takes_a_notes_mode(client, harness):
    result = await checked(
        client, "spawn_agent", worktree_path=harness.feat, notes_mode="free"
    )
    notes = await checked(client, "notes_get", **_target(result))
    assert notes["mode"] == "free"
    with pytest.raises(RequestError) as e:
        await client.request("spawn_agent", worktree_path=harness.feat, notes_mode="x")
    assert e.value.code == "VALIDATION_ERROR"


async def test_no_target_and_no_caller(client, harness):
    await spawn(client, harness.feat)
    with pytest.raises(RequestError, match="no target"):
        await client.request("notes_get")
    with pytest.raises(RequestError, match="Unknown caller token"):
        await client.request("notes_get", caller="forged")
