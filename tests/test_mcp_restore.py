"""The MCP restore tools end to end: the tools of lazyagent.mcp_server, over
the legacy IPC socket, to an engine that took over its predecessor's restart
manifest (real PTYs, the fake agent)."""

from __future__ import annotations

import asyncio
import time

import pytest

from lazyagent import mcp_server
from tests.engine.fakes import make_engine, worktree
from tests.engine.test_restore import _provider

pytestmark = pytest.mark.asyncio


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


async def test_mcp_lists_restores_and_reports_through_list_events(
    tmp_path, monkeypatch
):
    repo, feat = tmp_path / "repo", tmp_path / "repo-feat"
    repo.mkdir()
    feat.mkdir()
    worktrees = [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)]
    manifest = tmp_path / "manifest.json"
    # One shim for both engines: rewriting it while the first engine's agent
    # still runs it would fail (ETXTBSY).
    provider = _provider(tmp_path)

    def engine():
        e = make_engine(list(worktrees), repo_root=str(repo), provider=provider)
        e.open_manifest(manifest)
        return e

    first = engine()
    agent = await first.spawn_agent(str(feat), label="Reviewer")
    await first.stop()

    second = engine()
    await second.start()  # with the legacy IPC server, as a TUI or engine runs it
    try:
        monkeypatch.setenv("LAZYAGENT_SOCKET", second.socket_path)
        monkeypatch.setattr(mcp_server, "_cached_client", None)
        cursor = (await mcp_server.list_events())["cursor"]

        [entry] = (await mcp_server.list_restorable())["agents"]
        assert (entry["agent_id"], entry["label"]) == (agent.agent_id, "Reviewer")

        result = await mcp_server.restore_agents(ids=[entry["id"]])
        assert [r["agent_id"] for r in result["restored"]] == [agent.agent_id]
        assert result["failed"] == []
        await _until(lambda: second.get_session(str(feat), agent.agent_id) is not None)

        events = await mcp_server.list_events(since=cursor, statuses=["idle"])
        assert events["restores"] == [{"seq": events["restores"][0]["seq"], **result}]
        assert (await mcp_server.list_restorable())["agents"] == []
        assert await mcp_server.dismiss_restorable() == {"dismissed": []}
        # Nothing restored since: the key is absent, as before restores existed.
        later = await mcp_server.list_events(since=events["cursor"])
        assert "restores" not in later
    finally:
        await second.stop()
