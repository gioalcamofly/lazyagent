"""The MCP status_list tool, and the spawn caps through spawn_agent, end to
end: an agent's own lazyagent MCP server (its token from the mcp-config the
engine wrote) over the legacy IPC socket, to an engine running real PTYs."""

from __future__ import annotations

import pytest

from lazyagent import mcp_server
from lazyagent.agent_providers import AGENT_TOKEN_ENV
from lazyagent.config import Config, OrchestratorConfig
from lazyagent.models import AgentState, AgentStatus
from tests.test_mcp_notes import _as_agent, engine  # noqa: F401 (fixture)

pytestmark = pytest.mark.asyncio


async def test_status_list_answers_with_rows_and_a_ready_table(engine, monkeypatch):
    feat = engine.feat
    orchestrator = await engine.spawn_agent("", kind="orchestrator")
    await engine.spawn_agent(feat)
    _as_agent(engine, orchestrator, monkeypatch)

    result = await mcp_server.status_list()

    [row] = result["rows"]
    assert (row["branch"], row["worktree_path"]) == ("feat", feat)
    assert row["ball"] in ("agent", "you")
    assert result["counts"]["not_started"] == 0
    lines = result["table"].splitlines()
    assert lines[1].split() == ["LEVEL", "ITEM", "BALL", "STATE", "AGE"]
    assert lines[2].split()[1] == "feat"

    monkeypatch.delenv(AGENT_TOKEN_ENV)  # a session lazyagent did not spawn
    assert [r["key"] for r in (await mcp_server.status_list())["rows"]] == [row["key"]]


async def test_actionables_list_shows_the_inbox_with_a_table(engine, monkeypatch):
    feat = engine.feat
    orchestrator = await engine.spawn_agent("", kind="orchestrator")
    _as_agent(engine, orchestrator, monkeypatch)
    engine.registry.states.setdefault(feat, {})["a9"] = AgentState(
        status=AgentStatus.WAITING_FOR_APPROVAL, agent_id="a9", detail="Bash"
    )

    result = await mcp_server.actionables_list()

    [item] = [i for i in result["items"] if i["agent_id"] == "a9"]
    assert (item["kind"], item["level"], item["work_item"]) == ("approval", "now", "repo:feat")
    assert result["table"].splitlines()[0].startswith("NOW 1")
    assert "Needs approval: feat · Agent a9 · Bash" in result["table"]
    [row] = (await mcp_server.status_list())["rows"]
    assert row["needs_you"]["top"] == item["id"]


async def test_spawns_by_an_agent_are_capped_and_the_user_is_not(engine, monkeypatch):
    engine.config = Config(orchestrator=OrchestratorConfig(max_concurrent_agents=1))
    feat = engine.feat
    orchestrator = await engine.spawn_agent("", kind="orchestrator")
    _as_agent(engine, orchestrator, monkeypatch)

    await mcp_server.spawn_agent(feat)
    with pytest.raises(RuntimeError, match=r"\[VALIDATION_ERROR\] spawn refused.*max_concurrent_agents = 1"):
        await mcp_server.spawn_agent(feat)
    assert len(engine.agent_ids(feat)) == 1

    monkeypatch.delenv(AGENT_TOKEN_ENV)
    await mcp_server.spawn_agent(feat)
    assert len(engine.agent_ids(feat)) == 2
