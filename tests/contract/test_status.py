"""Work items over protocol v1 (proposal 0013): the ``status`` feature, the
status_list method and the status.updated / status.removed events,
validated strictly against protocol/schema/v1."""

from __future__ import annotations

import pytest

from lazyagent.protocol import validation
from tests.contract.helpers import checked, spawn

pytestmark = pytest.mark.asyncio


async def test_the_status_feature_is_offered(client):
    assert {"status", "status_list"} <= set(client.hello.capabilities)


async def test_status_list_has_a_row_per_worktree_branch(client, harness):
    result = await checked(client, "status_list")
    [row] = result["rows"]  # the main checkout runs no agent: not a work item
    assert (row["branch"], row["worktree_path"]) == ("feat", harness.feat)
    assert (row["zone"], row["ball"], row["agents"]) == ("in_flight", "stalled", [])
    assert result["counts"]["in_flight"] == 1

    assert (await checked(client, "status_list", repo="elsewhere"))["rows"] == []
    assert (await checked(client, "status_list", include_done=True))["rows"] == [row]


async def test_a_row_change_is_pushed_as_status_updated(client, harness):
    await checked(client, "subscribe", events=["status.updated", "status.removed"])
    agent = await spawn(client, harness.feat)

    event = await client.next_event(
        lambda e: e.event == "status.updated"
        and [a["id"] for a in e.data["row"]["agents"]] == [agent["agent_id"]],
        timeout=10.0,
    )
    validation.validate_event("status.updated", event.data, strict=True)
    validation.validate_result(
        "status_list",
        {"rows": [event.data["row"]], "counts": {k: 0 for k in (
            "now", "round", "deep", "in_flight", "done", "stale", "not_started",
            "deep_max")},
         "generated_at": 0.0},
        strict=True,
    )
    [row] = (await checked(client, "status_list"))["rows"]
    assert row["agents"][0]["id"] == agent["agent_id"]


FIELDS = ["rule_id", "verb", "action", "delegable", "delegates", "passable", "approvable",
          "review_commits"]


async def test_row_updates_carry_the_fields_status_list_asked_for(client, harness):
    """I4j: the opt-in fields of this connection's last status_list ride on
    its status.updated rows (they were dropped, and Home lost Approve and
    Delegate all after any change); a status_list without them stops it."""
    assert "row_fields_events" in client.hello.capabilities
    await checked(client, "subscribe", events=["status.updated"])
    await checked(client, "status_list", fields=FIELDS)
    agent = await spawn(client, harness.feat)
    event = await client.next_event(
        lambda e: e.event == "status.updated"
        and [a["id"] for a in e.data["row"]["agents"]] == [agent["agent_id"]],
        timeout=10.0,
    )
    validation.validate_event("status.updated", event.data, strict=True)
    row = event.data["row"]
    assert set(FIELDS) <= set(row)
    assert row["approvable"] is None and row["review_commits"] is None

    await checked(client, "status_list")
    await checked(client, "status_gesture", key=row["key"], kind="label", ball="others",
                  text="QA")
    event = await client.next_event(
        lambda e: e.event == "status.updated" and e.data["row"]["gesture"] is not None,
        timeout=10.0,
    )
    assert "rule_id" not in event.data["row"]


async def test_a_gesture_reply_carries_the_fields_asked_for(client, harness):
    await spawn(client, harness.feat)
    [row] = (await checked(client, "status_list"))["rows"]
    reply = await checked(client, "status_gesture", key=row["key"], kind="label",
                          ball="others", text="QA", fields=FIELDS)
    assert set(FIELDS) <= set(reply["row"])
    reply = await checked(client, "status_gesture", key=row["key"], kind="clear")
    assert "rule_id" not in reply["row"]
