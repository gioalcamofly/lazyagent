"""Tests for the IPC server handlers."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from lazyagent.ipc import IpcServer, _ok, _err
from lazyagent.models import AgentState, AgentStatus, GitStatus, LifecycleConfidence, WorktreeInfo
from lazyagent.worktree_manager import WorktreeManagerError


def _make_app(worktrees=None, agent_states=None, git_statuses=None):
    """Create a mock LazyAgent app."""
    app = MagicMock()
    app.worktrees = worktrees or []
    app._agent_states = agent_states or {}
    app._git_statuses = git_statuses or {}
    app._repo_root = "/tmp/repo"
    app._config.agent.provider = "claude"
    app._config.has_custom_create = False
    app._get_agent_state = MagicMock(side_effect=lambda p: app._agent_states.setdefault(p, AgentState()))
    return app


def _make_worktree(path="/tmp/repo", branch="main", is_main=True):
    return WorktreeInfo(path=path, head="abc123", branch=branch, is_main=is_main, is_bare=False)


class TestListWorktrees:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_worktrees(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")
        result = await server._dispatch("req-1", "list_worktrees", {})
        assert result == _ok("req-1", [])

    @pytest.mark.asyncio
    async def test_returns_worktrees_with_status(self):
        wt = _make_worktree("/tmp/repo", "main", is_main=True)
        wt2 = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        state = AgentState(status=AgentStatus.RUNNING)
        git_st = GitStatus(dirty_count=2, ahead=1)

        app = _make_app(
            worktrees=[wt, wt2],
            agent_states={"/tmp/repo-feat": state},
            git_statuses={"/tmp/repo": git_st},
        )
        server = IpcServer(app, "/tmp/test.sock")
        result = await server._dispatch("req-2", "list_worktrees", {})

        data = result["result"]
        assert len(data) == 2

        # Main worktree
        assert data[0]["path"] == "/tmp/repo"
        assert data[0]["is_main"] is True
        assert data[0]["agent_status"] == "no_agent"
        assert data[0]["git_status"]["dirty_count"] == 2

        # Feature worktree
        assert data[1]["path"] == "/tmp/repo-feat"
        assert data[1]["agent_status"] == "running"


class TestCreateWorktree:
    @pytest.mark.asyncio
    async def test_rejects_empty_branch(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")
        result = await server._dispatch("req-1", "create_worktree", {"branch": ""})
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_creates_worktree(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        with patch("lazyagent.ipc.WorktreeManager") as MockWM:
            instance = MockWM.return_value
            instance.create.return_value = "/tmp/repo-feat"

            result = await server._dispatch("req-1", "create_worktree", {
                "branch": "feat",
                "base_branch": "main",
            })

        assert result == _ok("req-1", {"path": "/tmp/repo-feat", "branch": "feat"})
        instance.create.assert_called_once_with("feat", "main")
        app.call_later.assert_called_once()

    @pytest.mark.asyncio
    async def test_propagates_worktree_manager_error(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        with patch("lazyagent.ipc.WorktreeManager") as MockWM:
            instance = MockWM.return_value
            instance.create.side_effect = WorktreeManagerError("branch exists")

            result = await server._dispatch("req-1", "create_worktree", {"branch": "feat"})

        assert "error" in result
        assert result["error"]["code"] == "WORKTREE_ERROR"


class TestRemoveWorktree:
    @pytest.mark.asyncio
    async def test_rejects_main_worktree(self):
        wt = _make_worktree("/tmp/repo", "main", is_main=True)
        app = _make_app(worktrees=[wt])
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "remove_worktree", {"worktree_path": "/tmp/repo"})
        assert "error" in result
        assert "main worktree" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_rejects_worktree_with_running_agent(self):
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        state = AgentState(status=AgentStatus.RUNNING)
        app = _make_app(worktrees=[wt], agent_states={"/tmp/repo-feat": state})
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "remove_worktree", {"worktree_path": "/tmp/repo-feat"})
        assert "error" in result
        assert "running" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_unknown_worktree(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "remove_worktree", {"worktree_path": "/tmp/unknown"})
        assert "error" in result
        assert "Unknown worktree" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_removes_worktree(self):
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        app = _make_app(worktrees=[wt])
        server = IpcServer(app, "/tmp/test.sock")

        with patch("lazyagent.ipc.WorktreeManager") as MockWM:
            instance = MockWM.return_value

            result = await server._dispatch("req-1", "remove_worktree", {
                "worktree_path": "/tmp/repo-feat",
                "force": True,
            })

        assert result == _ok("req-1", {"removed": "/tmp/repo-feat"})
        instance.remove.assert_called_once_with("/tmp/repo-feat", True)


class TestSpawnAgent:
    @pytest.mark.asyncio
    async def test_rejects_empty_worktree_path(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "spawn_agent", {"worktree_path": ""})
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_rejects_unknown_worktree(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "spawn_agent", {"worktree_path": "/tmp/unknown"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_rejects_duplicate_spawn(self):
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        state = AgentState(status=AgentStatus.RUNNING)
        app = _make_app(worktrees=[wt], agent_states={"/tmp/repo-feat": state})
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "spawn_agent", {"worktree_path": "/tmp/repo-feat"})
        assert "error" in result
        assert "already running" in result["error"]["message"]


class TestGetAgentStatus:
    @pytest.mark.asyncio
    async def test_returns_no_agent_when_not_tracked(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "get_agent_status", {"worktree_path": "/tmp/repo"})
        assert result["result"]["status"] == "no_agent"

    @pytest.mark.asyncio
    async def test_returns_current_status(self):
        state = AgentState(
            status=AgentStatus.WAITING_FOR_USER,
            confidence=LifecycleConfidence.HIGH,
            detail="needs input",
        )
        app = _make_app(agent_states={"/tmp/repo": state})
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "get_agent_status", {"worktree_path": "/tmp/repo"})
        assert result["result"]["status"] == "waiting_for_user"
        assert result["result"]["confidence"] == "high"
        assert result["result"]["detail"] == "needs input"


class TestReadAgentOutput:
    @pytest.mark.asyncio
    async def test_rejects_missing_terminal(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        # Mock query_one to return a CenterPanel mock that returns no panel
        mock_center = MagicMock()
        mock_center.get_panel.return_value = None
        app.query_one.return_value = mock_center

        result = await server._dispatch("req-1", "read_agent_output", {"worktree_path": "/tmp/repo"})
        assert "error" in result


class TestDispatch:
    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "nonexistent", {})
        assert "error" in result
        assert result["error"]["code"] == "UNKNOWN_METHOD"


class TestHelpers:
    def test_ok_format(self):
        assert _ok("id-1", {"key": "val"}) == {"id": "id-1", "result": {"key": "val"}}

    def test_err_format(self):
        assert _err("id-1", "CODE", "msg") == {
            "id": "id-1",
            "error": {"code": "CODE", "message": "msg"},
        }
