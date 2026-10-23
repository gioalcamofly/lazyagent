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


async def test_the_orchestrator_releases_an_item_passed_to_it(engine, monkeypatch):
    feat = engine.feat
    orchestrator = await engine.spawn_agent("", kind="orchestrator")
    await engine.spawn_agent(feat)
    engine.claims.claim("repo:feat")
    _as_agent(engine, orchestrator, monkeypatch)
    [row] = (await mcp_server.status_list())["rows"]
    assert (row["state"], row["claim"]["holder"]) == ("orchestrator · looking", "orchestrator")

    result = await mcp_server.orchestrator_release("repo:feat", "returned", "pick a db")

    assert (result["outcome"], result["claim"]["key"]) == ("returned", "repo:feat")
    [row] = (await mcp_server.status_list())["rows"]
    assert (row["level"], row["state"], row["claim"]) == ("round", "pick a db", None)
    monkeypatch.delenv(AGENT_TOKEN_ENV)
    with pytest.raises(Exception, match="Only the orchestrator"):
        await mcp_server.orchestrator_release("repo:feat", "done")


async def test_the_orchestrator_holds_an_item_while_it_verifies_it(engine, monkeypatch):
    feat = engine.feat
    orchestrator = await engine.spawn_agent("", kind="orchestrator")
    await engine.spawn_agent(feat)
    _as_agent(engine, orchestrator, monkeypatch)

    result = await mcp_server.orchestrator_hold("repo:feat", "verifying tests", 600)

    assert (result["hold"]["note"], result["hold"]["by"]) == ("verifying tests", "orchestrator")
    [row] = (await mcp_server.status_list())["rows"]
    assert (row["zone"], row["state"]) == ("in_flight", "orchestrator · verifying tests")
    result = await mcp_server.orchestrator_release("repo:feat", "done", "tests pass")
    assert (result["claim"], result["hold"]["key"]) == (None, "repo:feat")
    [row] = (await mcp_server.status_list())["rows"]
    assert (row["hold"], row["note"]["text"]) == (None, "orchestrator: tests pass")
    monkeypatch.delenv(AGENT_TOKEN_ENV)
    with pytest.raises(Exception, match="Only the orchestrator holds"):
        await mcp_server.orchestrator_hold("repo:feat", "mine")


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


async def test_a_worker_reports_and_asks_for_a_decision(engine, monkeypatch):
    feat = engine.feat
    worker = await engine.spawn_agent(feat)
    _as_agent(engine, worker, monkeypatch)

    report = await mcp_server.status_report(
        note="PR up, waiting for QA", ball="others",
        outward={"kind": "pr_create", "ref": "https://example.com/pull/3", "text": "opened"},
    )
    assert (report["key"], report["report"]["ball"]) == ("repo:feat", "others")
    assert report["outward"]["actor"] == f"agent:{worker.agent_id}"

    asked = await mcp_server.request_decision(
        question="Keep the old endpoint?",
        options=[{"key": "A", "label": "Keep it a release"}, {"key": "B", "label": "Drop it"}],
        recommendation="A",
        tried="grep: 2 callers left",
        evidence=[{"kind": "file", "path": "api/routes.py", "lines": "10-20"}],
        reversible="partly",
        impact_if_wrong="a client breaks",
    )
    decision = asked["decision"]
    assert (decision["id"], decision["class"], decision["valid"]) == ("d1", "deep", True)
    assert "To sit down with" in asked["message"]
    [item] = [i for i in (await mcp_server.actionables_list())["items"] if i["kind"] == "decision"]
    assert item["level"] == "deep"

    withdrawn = await mcp_server.decision_withdraw("d1")
    assert withdrawn["decision"]["state"] == "withdrawn"

    monkeypatch.delenv(AGENT_TOKEN_ENV)  # a session lazyagent did not spawn
    with pytest.raises(Exception, match="needs the agent's token"):
        await mcp_server.status_report(note="x")
