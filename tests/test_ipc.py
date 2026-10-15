"""Tests for the IPC server handlers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lazyagent.agent_events import AgentEventBus
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
    app.agent_events = AgentEventBus()
    app._config.agent.provider = "claude"
    app._config.has_custom_create = False
    app._get_agent_state = MagicMock(
        side_effect=lambda p, aid="": app._agent_states.setdefault(p, {}).setdefault(
            aid, AgentState(agent_id=aid)
        )
    )
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
            agent_states={"/tmp/repo-feat": {"a1": state}},
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
        assert data[0]["agents"] == []
        assert data[0]["git_status"]["dirty_count"] == 2

        # Feature worktree — scalar roll-up plus per-agent detail
        assert data[1]["path"] == "/tmp/repo-feat"
        assert data[1]["agent_status"] == "running"
        assert data[1]["agents"] == [
            {
                "agent_id": "a1",
                "label": "",
                "session_name": "",
                "status": "running",
                "confidence": "low",
                "detail": "",
            }
        ]


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

    @pytest.mark.asyncio
    async def test_custom_create_runs_subprocess_without_selected_worktree(self):
        """Custom create command must not require a focused worktree in the UI.

        Regression: previously dispatched into the selected worktree's
        terminal pane, which made MCP-driven orchestration impossible when
        no worktree was selected.
        """
        app = _make_app()
        app._config.has_custom_create = True
        app._config.worktree.create = "echo creating {branch}"
        app._get_selected_worktree = MagicMock(return_value=None)
        server = IpcServer(app, "/tmp/test.sock")

        proc = MagicMock()
        proc.communicate = MagicMock(return_value=asyncio.Future())
        proc.communicate.return_value.set_result((b"ok\n", b""))
        proc.returncode = 0

        async def _fake_shell(cmd, **kwargs):
            _fake_shell.cmd = cmd
            _fake_shell.kwargs = kwargs
            return proc

        with patch("asyncio.create_subprocess_shell", side_effect=_fake_shell):
            result = await server._dispatch("req-1", "create_worktree", {
                "branch": "feat",
                "base_branch": "main",
            })

        assert "result" in result, result
        assert result["result"]["custom_command"] is True
        assert result["result"]["branch"] == "feat"
        assert "creating feat" in _fake_shell.cmd
        assert _fake_shell.kwargs["cwd"] == "/tmp/repo"
        app._get_selected_worktree.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_create_surfaces_subprocess_failure(self):
        app = _make_app()
        app._config.has_custom_create = True
        app._config.worktree.create = "false"
        server = IpcServer(app, "/tmp/test.sock")

        proc = MagicMock()
        proc.communicate = MagicMock(return_value=asyncio.Future())
        proc.communicate.return_value.set_result((b"", b"boom\n"))
        proc.returncode = 2

        async def _fake_shell(cmd, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_shell", side_effect=_fake_shell):
            result = await server._dispatch("req-1", "create_worktree", {"branch": "feat"})

        assert "error" in result
        assert result["error"]["code"] == "WORKTREE_ERROR"
        assert "boom" in result["error"]["message"]


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
        app = _make_app(worktrees=[wt], agent_states={"/tmp/repo-feat": {"a1": state}})
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
        # Through the app, so each agent is reported as exited.
        app._drop_worktree_agents.assert_called_once_with("/tmp/repo-feat")


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
    async def test_does_not_reject_when_agent_already_running(self):
        """Duplicate spawn is now allowed — a second agent is added.

        The guard that used to reject a running worktree is gone, so the
        handler proceeds past validation into the Textual-thread spawn. We
        stub call_later (so the UI work is never scheduled) and wait_for (so
        it resolves to the new agent id), and assert no "already running"
        validation error.
        """
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        state = AgentState(status=AgentStatus.RUNNING, agent_id="a1")
        app = _make_app(
            worktrees=[wt], agent_states={"/tmp/repo-feat": {"a1": state}}
        )
        app.call_later = MagicMock()
        server = IpcServer(app, "/tmp/test.sock")

        # asyncio.wait_for is async, so patch() auto-uses an AsyncMock.
        with patch("asyncio.wait_for", return_value="a2"):
            result = await server._dispatch(
                "req-1", "spawn_agent", {"worktree_path": "/tmp/repo-feat"}
            )

        assert "result" in result, result
        assert result["result"]["agent_id"] == "a2"
        assert app.call_later.called


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
            agent_id="a1",
        )
        app = _make_app(agent_states={"/tmp/repo": {"a1": state}})
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("req-1", "get_agent_status", {"worktree_path": "/tmp/repo"})
        assert result["result"]["status"] == "waiting_for_user"
        assert result["result"]["confidence"] == "high"
        assert result["result"]["detail"] == "needs input"
        assert result["result"]["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_requires_agent_id_when_multiple(self):
        app = _make_app(
            agent_states={
                "/tmp/repo": {
                    "a1": AgentState(status=AgentStatus.RUNNING, agent_id="a1"),
                    "a2": AgentState(status=AgentStatus.RUNNING, agent_id="a2"),
                }
            }
        )
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "req-1", "get_agent_status", {"worktree_path": "/tmp/repo"}
        )
        assert "error" in result
        assert "specify agent_id" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_selects_specific_agent_when_multiple(self):
        app = _make_app(
            agent_states={
                "/tmp/repo": {
                    "a1": AgentState(status=AgentStatus.RUNNING, agent_id="a1"),
                    "a2": AgentState(
                        status=AgentStatus.WAITING_FOR_USER, agent_id="a2"
                    ),
                }
            }
        )
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "req-1",
            "get_agent_status",
            {"worktree_path": "/tmp/repo", "agent_id": "a2"},
        )
        assert result["result"]["agent_id"] == "a2"
        assert result["result"]["status"] == "waiting_for_user"


class TestListAgents:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_agents(self):
        app = _make_app()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "req-1", "list_agents", {"worktree_path": "/tmp/repo"}
        )
        assert result["result"] == {"worktree_path": "/tmp/repo", "agents": []}

    @pytest.mark.asyncio
    async def test_lists_agents(self):
        app = _make_app(
            agent_states={
                "/tmp/repo": {
                    "a1": AgentState(
                        status=AgentStatus.RUNNING, agent_id="a1", label="Agent 1"
                    ),
                    "a2": AgentState(
                        status=AgentStatus.WAITING_FOR_USER,
                        agent_id="a2",
                        label="Agent 2",
                    ),
                }
            }
        )
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "req-1", "list_agents", {"worktree_path": "/tmp/repo"}
        )
        agents = result["result"]["agents"]
        assert [a["agent_id"] for a in agents] == ["a1", "a2"]
        assert agents[1]["label"] == "Agent 2"
        assert agents[1]["status"] == "waiting_for_user"


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

    @pytest.mark.asyncio
    async def test_returns_scrollback_and_live_lines(self):
        """Output spans scrollback history and the live screen, oldest first."""
        import pyte
        from lazyagent.widgets.scrollable_terminal import ScrollbackScreen

        screen = ScrollbackScreen(40, 4)
        stream = pyte.Stream(screen)
        for i in range(12):
            stream.feed(f"line {i}\r\n")
        assert len(screen.scrollback) > 0  # some lines scrolled off

        terminal = MagicMock()
        terminal._screen = screen
        panel = MagicMock()
        panel.agent_ids = ["a1"]
        panel.get_agent.return_value = terminal

        app = _make_app()
        mock_center = MagicMock()
        mock_center.get_panel.return_value = panel
        app.query_one.return_value = mock_center

        server = IpcServer(app, "/tmp/test.sock")
        result = await server._dispatch(
            "req-1", "read_agent_output", {"worktree_path": "/tmp/repo", "lines": 6}
        )

        lines = result["result"]["lines"]
        # The trailing "\r\n" leaves the cursor on a blank final row.
        assert lines == ["line 7", "line 8", "line 9", "line 10", "line 11", ""]
        assert result["result"]["total_lines"] == len(screen.scrollback) + 4


class TestSendAgentInput:
    """The handler must go through send_input, which separates the payload
    from the Enter that submits it — a raw ``text + "\\r"`` write leaves the
    instruction sitting unsent in the agent's prompt."""

    @pytest.fixture(autouse=True)
    def _no_confirm_wait(self, monkeypatch):
        # These agents never react; don't sit out the delivery confirmation.
        monkeypatch.setattr("lazyagent.ipc._INPUT_CONFIRM_TIMEOUT", 0.0)

    @staticmethod
    def _app_with_terminal():
        app = _make_app(worktrees=[_make_worktree()])
        terminal = MagicMock()
        terminal.send_input = AsyncMock()
        panel = MagicMock()
        panel.agent_ids = ["a1"]
        panel.get_agent.return_value = terminal
        center = MagicMock()
        center.get_panel.return_value = panel
        app.query_one.return_value = center
        return app, terminal

    @pytest.mark.asyncio
    async def test_delegates_to_send_input_with_submit(self):
        app, terminal = self._app_with_terminal()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "req-1",
            "send_agent_input",
            {"worktree_path": "/tmp/repo", "text": "run the tests"},
        )

        assert result["result"]["status"] == "sent"
        terminal.send_input.assert_awaited_once_with("run the tests", submit=True)

    @pytest.mark.asyncio
    async def test_rejects_empty_text(self):
        app, terminal = self._app_with_terminal()
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "req-1", "send_agent_input", {"worktree_path": "/tmp/repo", "text": ""}
        )

        assert result["error"]["code"] == "VALIDATION_ERROR"
        terminal.send_input.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_a_stopped_terminal(self):
        """stop() clears the emulator but leaves send_queue pointing at it."""
        app, terminal = self._app_with_terminal()
        terminal.emulator = None
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "req-1", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "hi"}
        )

        assert result["error"]["code"] == "VALIDATION_ERROR"
        terminal.send_input.assert_not_awaited()


def _set_status(app, status, *, path="/tmp/repo", agent_id="a1", detail=""):
    """Record a status the way the app does: update the state and publish."""
    bucket = app._agent_states.setdefault(path, {})
    state = bucket.setdefault(agent_id, AgentState(agent_id=agent_id))
    previous = state.status
    state.status = status
    state.detail = detail
    app.agent_events.publish(
        worktree_path=path,
        agent_id=agent_id,
        status=status.value,
        previous_status=previous.value,
        detail=detail,
    )


def _app_with_agent(status=AgentStatus.WAITING, detail=""):
    app = _make_app(worktrees=[_make_worktree()])
    _set_status(app, status, detail=detail)
    terminal = MagicMock()
    terminal.send_input = AsyncMock()
    panel = MagicMock()
    panel.agent_ids = ["a1"]
    panel.get_agent.return_value = terminal
    center = MagicMock()
    center.get_panel.return_value = panel
    app.query_one.return_value = center
    return app, terminal


def _react(app, *statuses, delay=0.0):
    """send_input side effect: the agent moves through ``statuses``."""

    async def _side_effect(text, submit=False):
        async def _later():
            for status in statuses:
                await asyncio.sleep(delay)
                _set_status(app, status)

        asyncio.get_running_loop().create_task(_later())

    return _side_effect


class TestSendAgentInputDelivery:
    @pytest.mark.asyncio
    async def test_confirmed_when_the_agent_starts_a_turn(self):
        app, terminal = _app_with_agent(AgentStatus.WAITING)
        terminal.send_input.side_effect = _react(app, AgentStatus.RUNNING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "r", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "go"}
        )

        res = result["result"]
        assert res["delivery"] == "confirmed"
        assert res["status_before"] == "waiting"
        assert res["cursor"] == 1
        assert "warnings" not in res

    @pytest.mark.asyncio
    async def test_unconfirmed_when_the_agent_does_not_react(self, monkeypatch):
        monkeypatch.setattr("lazyagent.ipc._INPUT_CONFIRM_TIMEOUT", 0.01)
        app, terminal = _app_with_agent(AgentStatus.WAITING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "r", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "go"}
        )

        res = result["result"]
        assert res["delivery"] == "unconfirmed"
        assert "read_agent_output" in res["warnings"][0]

    @pytest.mark.asyncio
    async def test_busy_agent_is_not_waited_on(self):
        app, terminal = _app_with_agent(AgentStatus.RUNNING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "r", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "go"}
        )

        assert result["result"]["delivery"] == "busy"

    @pytest.mark.asyncio
    async def test_warns_when_the_text_answered_a_dialog(self):
        app, terminal = _app_with_agent(
            AgentStatus.WAITING_FOR_APPROVAL, detail="needs approval"
        )
        terminal.send_input.side_effect = _react(app, AgentStatus.RUNNING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "r", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "1"}
        )

        res = result["result"]
        assert res["delivery"] == "confirmed"
        assert "answered that dialog" in res["warnings"][0]

    @pytest.mark.asyncio
    async def test_confirm_false_skips_the_check(self):
        app, terminal = _app_with_agent(AgentStatus.WAITING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "r",
            "send_agent_input",
            {"worktree_path": "/tmp/repo", "text": "go", "confirm": False},
        )

        assert result["result"]["delivery"] == "unchecked"

    @pytest.mark.asyncio
    async def test_wait_until_is_gone(self):
        # The blocking wait was removed; a stale caller still gets its text
        # delivered and no extra keys in the result.
        app, terminal = _app_with_agent(AgentStatus.WAITING)
        terminal.send_input.side_effect = _react(app, AgentStatus.RUNNING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "r",
            "send_agent_input",
            {"worktree_path": "/tmp/repo", "text": "go", "wait_until": "idle"},
        )

        res = result["result"]
        assert res["delivery"] == "confirmed"
        assert "matched" not in res


class TestRemovedWaitTools:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["wait_for_agent", "wait_for_events"])
    async def test_blocking_methods_are_unknown(self, method):
        app, _ = _app_with_agent(AgentStatus.RUNNING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("r", method, {"worktree_path": "/tmp/repo"})

        assert result["error"]["code"] == "UNKNOWN_METHOD"


class TestListEvents:
    @pytest.mark.asyncio
    async def test_without_since_starts_from_now(self):
        app, _ = _app_with_agent(AgentStatus.WAITING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("r", "list_events", {})

        assert result["result"] == {"events": [], "cursor": 1, "truncated": False}

    @pytest.mark.asyncio
    async def test_filters_and_advances_cursor_past_skipped_events(self):
        app, _ = _app_with_agent(AgentStatus.RUNNING)
        _set_status(app, AgentStatus.WAITING)
        _set_status(app, AgentStatus.RUNNING, path="/tmp/other")
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch(
            "r", "list_events", {"since": 0, "statuses": ["idle"]}
        )

        res = result["result"]
        assert [e["status"] for e in res["events"]] == ["waiting"]
        assert res["cursor"] == 3

    @pytest.mark.asyncio
    async def test_never_blocks_even_with_a_timeout(self):
        app, _ = _app_with_agent(AgentStatus.RUNNING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await asyncio.wait_for(
            server._dispatch(
                "r", "list_events", {"statuses": ["idle"], "timeout": 600}
            ),
            1,
        )

        assert result["result"]["events"] == []

    @pytest.mark.asyncio
    async def test_rejects_a_bad_cursor(self):
        app, _ = _app_with_agent(AgentStatus.RUNNING)
        server = IpcServer(app, "/tmp/test.sock")

        result = await server._dispatch("r", "list_events", {"since": -1})

        assert result["error"]["code"] == "VALIDATION_ERROR"


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
