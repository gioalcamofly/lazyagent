"""review_reply end to end: an agent's own lazyagent MCP server (its token
from the mcp-config the engine wrote for it) over the legacy IPC socket, to
an engine running real PTYs with the fake agent."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent import mcp_server
from lazyagent.agent_providers import AGENT_TOKEN_ENV
from lazyagent.engine.events import REVIEW_REPLIED
from tests.test_mcp_notes import _as_agent, engine  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio


async def test_an_agent_answers_a_comment_it_was_sent(engine, monkeypatch):  # noqa: F811,E501
    feat = engine.feat
    agent = await engine.spawn_agent(feat)
    seen = []
    engine.subscribe(seen.append, [REVIEW_REPLIED])
    await engine_call(engine, "review_register", worktree_path=feat,
                      agent_id=agent.agent_id, comment_ids=["c1a2b3"])
    _as_agent(engine, agent, monkeypatch)

    result = await mcp_server.review_reply("c1a2b3", "Renamed it to parse_rows.")

    assert result["comment_id"] == "c1a2b3"
    [event] = seen
    assert event.data == {
        "reply_id": result["reply_id"],
        "comment_id": "c1a2b3",
        "worktree_path": feat,
        "agent_id": agent.agent_id,
        "text": "Renamed it to parse_rows.",
        "at": event.data["at"],
    }
    kept = await engine_call(engine, "review_replies", worktree_path=feat)
    assert [r["text"] for r in kept["replies"]] == ["Renamed it to parse_rows."]


async def test_another_agent_cannot_answer_it(engine, monkeypatch):  # noqa: F811
    feat = engine.feat
    sent_to = await engine.spawn_agent(feat)
    other = await engine.spawn_agent(feat)
    await engine_call(engine, "review_register", worktree_path=feat,
                      agent_id=sent_to.agent_id, comment_ids=["c1a2b3"])
    _as_agent(engine, other, monkeypatch)

    with pytest.raises(RuntimeError, match="No review comment .* was sent to you"):
        await mcp_server.review_reply("c1a2b3", "I did it")


async def test_without_a_token_nobody_can_answer(engine, monkeypatch):  # noqa: F811
    feat = engine.feat
    agent = await engine.spawn_agent(feat)
    await engine_call(engine, "review_register", worktree_path=feat,
                      agent_id=agent.agent_id, comment_ids=["c1a2b3"])
    monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)

    with pytest.raises(RuntimeError, match="needs the agent's token"):
        await mcp_server.review_reply("c1a2b3", "spoofed")


async def test_comments_register_only_for_a_live_agent(engine):  # noqa: F811
    with pytest.raises(RuntimeError, match="No agent 'a9'"):
        await engine_call(engine, "review_register", worktree_path=engine.feat,
                          agent_id="a9", comment_ids=["c1"])


async def test_an_agent_started_with_comments_can_answer_at_once(engine):  # noqa: F811
    """The fake agent answers the ids in its first prompt as it starts: they
    are registered before it runs, so there is no window to lose a reply."""
    feat = engine.feat
    seen = []
    engine.subscribe(seen.append, [REVIEW_REPLIED])
    result = await engine_call(
        engine, "spawn_agent", worktree_path=feat,
        instruction="Review comments:\n- app.py:3 [comment_id: c1]\n  why?",
        review_comment_ids=["c1"],
    )
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert [(e.data["comment_id"], e.data["agent_id"]) for e in seen] == [
        ("c1", result["agent_id"])
    ]
    assert seen[0].data["text"] == "A fake answer to c1."


async def test_spawn_refuses_bad_comment_ids(engine):  # noqa: F811
    with pytest.raises(RuntimeError, match="comment_id"):
        await engine_call(engine, "spawn_agent", worktree_path=engine.feat,
                          review_comment_ids=["no spaces"])


async def engine_call(running, method, **params):
    return await mcp_server.IpcClient(running.socket_path).call(method, params)
