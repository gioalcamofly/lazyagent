"""Every method, with params and results validated strictly against
protocol/schema/v1."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.engine.client import RequestError
from lazyagent.protocol import validation
from tests.contract.helpers import checked, spawn, wait_for_text

pytestmark = pytest.mark.asyncio


async def _error(client, method, **params) -> RequestError:
    with pytest.raises(RequestError) as e:
        await client.request(method, **params)
    response = {
        "type": "res",
        "id": "x",
        "error": {"code": e.value.code, "message": e.value.message},
    }
    validation.validate_message(response, strict=True)
    return e.value


async def test_list_worktrees(harness, client):
    result = await checked(client, "list_worktrees")
    assert [wt["path"] for wt in result] == [harness.repo, harness.feat]
    assert [wt["is_main"] for wt in result] == [True, False]


async def test_refresh_worktrees(harness, client):
    result = await checked(client, "refresh_worktrees")
    assert [wt["path"] for wt in result] == [harness.repo, harness.feat]


async def test_create_and_remove_worktree(harness, client):
    created = await checked(client, "create_worktree", branch="new")
    assert created["branch"] == "new"
    paths = [wt["path"] for wt in await checked(client, "list_worktrees")]
    assert created["path"] in paths
    removed = await checked(client, "remove_worktree", worktree_path=created["path"])
    assert removed == {"removed": created["path"]}
    assert created["path"] not in [
        wt["path"] for wt in await checked(client, "list_worktrees")
    ]


async def test_create_worktree_needs_a_branch(client):
    assert (
        await _error(client, "create_worktree", branch=" ")
    ).code == "VALIDATION_ERROR"


async def test_remove_main_worktree_fails(harness, client):
    error = await _error(client, "remove_worktree", worktree_path=harness.repo)
    assert error.code == "VALIDATION_ERROR"


async def test_agent_lifecycle(harness, client):
    agent = await spawn(client, harness.feat)
    assert agent["status"] == "spawned" and agent["session_id"]

    status = await checked(client, "get_agent_status", worktree_path=harness.feat)
    assert status["agent_id"] == agent["agent_id"]

    listed = await checked(client, "list_agents", worktree_path=harness.feat)
    [entry] = listed["agents"]
    assert entry["session_id"] == agent["session_id"]
    assert entry["kind"] == "agent"
    assert entry["size"] == {"rows": 24, "cols": 80}
    assert entry["attached_clients"] == 0

    output = await checked(
        client, "read_agent_output", worktree_path=harness.feat, lines=24
    )
    assert any("your turn>" in line for line in output["lines"])

    sent = await checked(
        client,
        "send_agent_input",
        worktree_path=harness.feat,
        text="hello",
        confirm=False,
    )
    assert sent["status"] == "sent"
    await wait_for_text(client, agent, "got: 'hello'")

    stopped = await checked(client, "stop_agent", worktree_path=harness.feat)
    assert stopped["status"] == "stopped"
    assert (await checked(client, "list_agents", worktree_path=harness.feat))[
        "agents"
    ] == []
    status = await checked(client, "get_agent_status", worktree_path=harness.feat)
    assert status["status"] == "no_agent"


async def test_spawn_unknown_worktree(client):
    error = await _error(client, "spawn_agent", worktree_path="/nowhere")
    assert error.code == "VALIDATION_ERROR"


async def test_spawn_passes_the_client_env(harness, client):
    agent = await checked(
        client,
        "spawn_agent",
        worktree_path=harness.feat,
        instruction="--env=CONTRACT_VAR",
        env={"CONTRACT_VAR": "from-the-client", "PATH": "/usr/bin:/bin"},
    )
    await wait_for_text(client, agent, "env CONTRACT_VAR=from-the-client")


async def test_spawn_the_orchestrator(harness, client):
    agent = await checked(client, "spawn_agent", kind="orchestrator")
    assert agent["session_id"]
    assert agent["agent_id"] == ""
    await checked(client, "stop_agent", worktree_path=agent["worktree_path"])


async def test_list_events(harness, client):
    start = await checked(client, "list_events")
    await spawn(client, harness.feat)
    result = await checked(client, "list_events", since=start["cursor"])
    assert result["events"]
    assert all(e["worktree_path"] == harness.feat for e in result["events"])
    assert result["cursor"] >= result["events"][-1]["seq"]


async def test_open_shell_is_get_or_create(harness, client):
    first = await checked(client, "open_shell", worktree_path=harness.feat)
    again = await checked(client, "open_shell", worktree_path=harness.feat)
    assert first["session_id"] == again["session_id"]
    assert first["size"] == {"rows": 24, "cols": 80}


async def test_open_shell_unknown_worktree(client):
    assert (
        await _error(client, "open_shell", worktree_path="/nowhere")
    ).code == "VALIDATION_ERROR"


async def test_refresh_git_status(harness, client):
    result = await checked(client, "refresh_git_status", paths=[harness.feat])
    assert list(result["statuses"]) == [harness.feat]
    everything = await checked(client, "refresh_git_status")
    assert set(everything["statuses"]) == {harness.repo, harness.feat}


async def test_get_diff(harness, client):
    result = await checked(client, "get_diff", worktree_path=harness.feat)
    assert result["worktree_path"] == harness.feat
    assert isinstance(result["diff"], str)


async def test_get_pr_info(harness, client):
    result = await checked(client, "get_pr_info", worktree_path=harness.feat)
    assert result["worktree_path"] == harness.feat
    if not result["fetched"]:
        assert result["pr_info"] is None


async def test_set_interest_is_per_client(harness, client):
    other = await harness.connect()
    mine = await checked(client, "set_interest", worktree_paths=[harness.feat])
    assert mine == {"worktree_paths": [harness.feat], "interest": [harness.feat]}
    theirs = await checked(other, "set_interest", worktree_paths=[harness.repo])
    assert theirs["interest"] == sorted([harness.repo, harness.feat])
    await other.close()

    # A client that goes away takes its interest set with it.
    async def union():
        return (await checked(client, "set_interest", worktree_paths=[harness.feat]))[
            "interest"
        ]

    seen = [await union()]
    while seen[-1] != [harness.feat] and len(seen) < 100:
        await asyncio.sleep(0.02)
        seen.append(await union())
    assert seen[-1] == [harness.feat]


async def test_get_config(client):
    result = await checked(client, "get_config")
    assert result["has_custom_create"] is False


async def test_unsubscribe_without_subscription(client):
    assert await checked(client, "unsubscribe") == {"unsubscribed": False}
    await checked(client, "subscribe")
    assert await checked(client, "unsubscribe") == {"unsubscribed": True}


async def test_detach_unknown_stream(client):
    assert (
        await _error(client, "detach_stream", stream_id=99)
    ).code == "VALIDATION_ERROR"


@pytest.mark.parametrize("method", ["no_such_method", "shutdown"])
async def test_unknown_method(client, method):
    assert (await _error(client, method)).code == "UNKNOWN_METHOD"


async def test_requests_may_overlap(harness, client):
    results = await asyncio.gather(
        client.request("list_worktrees"),
        client.request("get_config"),
        client.request("list_agents", worktree_path=harness.feat),
        client.request("get_diff", worktree_path=harness.feat),
    )
    assert [type(r) for r in results] == [list, dict, dict, dict]
    assert results[2]["worktree_path"] == results[3]["worktree_path"] == harness.feat
