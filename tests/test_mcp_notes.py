"""The MCP notes tools end to end: an agent's own lazyagent MCP server (its
token from the mcp-config the engine wrote for it) over the legacy IPC
socket, to an engine running real PTYs with the fake agent."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from lazyagent import mcp_server
from lazyagent.agent_providers import AGENT_TOKEN_ENV, MCP_SERVER_NAME
from tests.engine.fakes import hooked_fake_agent_provider, hooks_dir, make_engine, worktree

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine(tmp_path, monkeypatch):
    repo, feat = tmp_path / "repo", tmp_path / "repo-feat"
    repo.mkdir()
    feat.mkdir()
    engine = make_engine(
        [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
        repo_root=str(repo),
        provider=hooked_fake_agent_provider(tmp_path),
    )
    engine.open_manifest(tmp_path / "manifest.json")
    await engine.start()  # with the legacy IPC server the MCP server talks to
    engine.feat = str(feat)  # type: ignore[attr-defined]
    monkeypatch.setenv("LAZYAGENT_SOCKET", engine.socket_path)
    monkeypatch.setattr(mcp_server, "_cached_client", None)
    try:
        yield engine
    finally:
        await engine.stop()


def _as_agent(engine, agent, monkeypatch) -> None:
    """Run the MCP tools as ``agent``'s MCP server would: with the env the
    engine put in its mcp-config."""
    session = engine.get_session(agent.worktree_path, agent.agent_id)
    config = json.loads((hooks_dir(session) / "mcp-config.json").read_text())
    env = config["mcpServers"][MCP_SERVER_NAME]["env"]
    assert env["LAZYAGENT_SOCKET"] == engine.socket_path
    monkeypatch.setenv(AGENT_TOKEN_ENV, env[AGENT_TOKEN_ENV])


async def test_an_agent_appends_to_its_own_notes(engine, monkeypatch):
    feat = engine.feat
    first = await engine.spawn_agent(feat)
    agent = await engine.spawn_agent(feat)
    _as_agent(engine, agent, monkeypatch)

    result = await mcp_server.notes_append("Decisions", "use sqlite")

    assert (result["agent_id"], result["by"], result["rev"]) == (agent.agent_id, "agent", 1)
    notes = await mcp_server.notes_read()
    assert "## Decisions\n- use sqlite\n" in notes["text"]
    assert notes["notes_id"] == engine.notes_id(feat, agent.agent_id)
    assert engine.notes.get(engine.notes_id(feat, first.agent_id)).empty

    replaced = await mcp_server.notes_replace_section(
        "Pending", "- [ ] tests", if_rev=notes["rev"]
    )
    assert replaced["rev"] == 2
    with pytest.raises(RuntimeError, match=r"\[CONFLICT\] notes changed \(rev 2\)"):
        await mcp_server.notes_replace_section("Pending", "- [x] tests", if_rev=1)


async def test_a_peer_may_read_but_not_write(engine, monkeypatch):
    feat = engine.feat
    worker = await engine.spawn_agent(feat)
    peer = await engine.spawn_agent(feat)
    _as_agent(engine, peer, monkeypatch)

    with pytest.raises(RuntimeError, match="only write its own notes"):
        await mcp_server.notes_append("Decisions", "x", feat, worker.agent_id)
    read = await mcp_server.notes_read(feat, worker.agent_id)
    assert read["agent_id"] == worker.agent_id and read["rev"] == 0


async def test_the_orchestrator_writes_a_workers_notes(engine, monkeypatch):
    feat = engine.feat
    worker = await engine.spawn_agent(feat, notes_mode="free")
    orchestrator = await engine.spawn_agent("", kind="orchestrator")

    _as_agent(engine, orchestrator, monkeypatch)
    by_token = await mcp_server.notes_append(
        "Constraints (don't touch)", "no migrations", feat
    )
    assert (by_token["agent_id"], by_token["by"]) == (worker.agent_id, "orchestrator")

    # An outside session (the orchestrator skill): no token at all.
    monkeypatch.delenv(AGENT_TOKEN_ENV)
    outside = await mcp_server.notes_append("Doubts", "- [ ] which db?", feat)
    assert outside["by"] == "orchestrator"
    with pytest.raises(RuntimeError, match="no target"):
        await mcp_server.notes_read()

    notes = await mcp_server.notes_read(feat)
    assert notes["mode"] == "free"
    stopped = await mcp_server.stop_agent(feat)
    assert stopped["notes"] == notes["text"]
    assert engine.notes_id(feat, worker.agent_id) is None


async def test_a_dead_agents_token_is_refused(engine, monkeypatch):
    agent = await engine.spawn_agent(engine.feat)
    _as_agent(engine, agent, monkeypatch)
    engine.stop_agent(engine.feat, agent.agent_id)
    with pytest.raises(RuntimeError, match="Unknown caller token"):
        await mcp_server.notes_read()
