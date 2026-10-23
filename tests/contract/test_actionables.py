"""The engine's inbox queue over protocol v1 (proposal 0013, I2): the
``actionables`` feature, actionables_list / _get / _mark and the
actionable.* events, validated strictly against protocol/schema/v1."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.protocol import validation
from tests.contract.helpers import checked, spawn

pytestmark = pytest.mark.asyncio


async def test_the_actionables_feature_is_offered(client):
    assert {"actionables", "actionables_list", "actionables_mark"} <= set(
        client.hello.capabilities
    )


async def test_dismissing_in_one_client_hides_it_in_the_other(client, harness):
    other = await harness.connect()
    await checked(other, "subscribe", events=["actionable.created", "actionable.resolved"])
    agent = await spawn(client, harness.feat)
    await checked(client, "send_agent_input", worktree_path=harness.feat,
                  agent_id=agent["agent_id"], text="exit 3")

    created = await other.next_event(
        lambda e: e.event == "actionable.created" and e.data["item"]["kind"] == "exited",
        timeout=10.0,
    )
    validation.validate_event("actionable.created", created.data, strict=True)
    item = created.data["item"]
    assert (item["level"], item["agent_id"], item["exit_code"]) == ("round", agent["agent_id"], 3)
    assert item["id"] in [i["id"] for i in (await checked(other, "actionables_list"))["items"]]
    assert (await checked(client, "actionables_get", id=item["id"]))["item"]["id"] == item["id"]

    await checked(client, "actionables_mark", id=item["id"], action="dismiss")
    resolved = await other.next_event(
        lambda e: e.event == "actionable.resolved" and e.data["id"] == item["id"], timeout=10.0
    )
    validation.validate_event("actionable.resolved", resolved.data, strict=True)
    assert resolved.data["reason"] == "dismissed"
    assert item["id"] not in [i["id"] for i in (await checked(other, "actionables_list"))["items"]]


async def test_a_parked_item_is_listed_only_on_request(client, harness):
    agent = await spawn(client, harness.feat)
    await checked(client, "send_agent_input", worktree_path=harness.feat,
                  agent_id=agent["agent_id"], text="exit 4")
    for _ in range(500):
        items = [
            i for i in (await checked(client, "actionables_list"))["items"]
            if i["kind"] == "exited"
        ]
        if items:
            break
        await asyncio.sleep(0.02)
    [item] = items
    parked = await checked(
        client, "actionables_mark", id=item["id"], action="park", until=4102444800
    )
    assert parked["item"]["park"]["until"] == 4102444800
    listed = await checked(client, "actionables_list", include_hidden=True)
    assert item["id"] in [i["id"] for i in listed["items"]] and listed["counts"]["parked"] == 1
    assert item["id"] not in [i["id"] for i in (await checked(client, "actionables_list"))["items"]]
