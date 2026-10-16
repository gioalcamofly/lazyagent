"""Tests for the IPC server handlers, run against an Engine (no Textual app)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lazyagent.config import Config, WorktreeConfig
from lazyagent.ipc import IpcServer, _ok, _err
from lazyagent.models import AgentState, AgentStatus, GitStatus, LifecycleConfidence, WorktreeInfo
from lazyagent.worktree_manager import WorktreeManagerError
from tests.engine.fakes import add_session, make_engine, no_pty


def _make_engine(worktrees=None, agent_states=None, git_statuses=None, config=None):
    """An Engine over fake git, holding the given states."""
    engine = make_engine(worktrees, config=config)
    engine.registry.states = agent_states or {}
    engine.worktree_service.git_statuses = git_statuses or {}
    return engine


def _make_worktree(path="/tmp/repo", branch="main", is_main=True):
    return WorktreeInfo(path=path, head="abc123", branch=branch, is_main=is_main, is_bare=False)


def _server(engine):
    return IpcServer(engine, "/tmp/test.sock")


def _custom(create=None, remove=None):
    return Config(worktree=WorktreeConfig(create=create, remove=remove))


def _fake_proc(stdout=b"", stderr=b"", returncode=0):
    """A finished create_subprocess_shell process with canned output."""
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_data(stdout)
    proc.stdout.feed_eof()
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_data(stderr)
    proc.stderr.feed_eof()
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    return proc


def _session(running=True):
    """A stand-in agent session in /tmp/repo, agent a1."""
    session = MagicMock()
    session.worktree_path = "/tmp/repo"
    session.agent_id = "a1"
    session.running = running
    session.send_input = AsyncMock()
    return session


class TestListWorktrees:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_worktrees(self):
        engine = _make_engine()
        server = _server(engine)
        result = await server._dispatch("req-1", "list_worktrees", {})
        assert result == _ok("req-1", [])

    @pytest.mark.asyncio
    async def test_returns_worktrees_with_status(self):
        wt = _make_worktree("/tmp/repo", "main", is_main=True)
        wt2 = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        state = AgentState(status=AgentStatus.RUNNING)
        git_st = GitStatus(dirty_count=2, ahead=1)

        engine = _make_engine(
            worktrees=[wt, wt2],
            agent_states={"/tmp/repo-feat": {"a1": state}},
            git_statuses={"/tmp/repo": git_st},
        )
        server = _server(engine)
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
        engine = _make_engine()
        server = _server(engine)
        result = await server._dispatch("req-1", "create_worktree", {"branch": ""})
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_creates_worktree(self):
        engine = _make_engine()
        server = _server(engine)
        seen = []
        engine.subscribe(seen.append, ["worktrees.changed"])

        result = await server._dispatch("req-1", "create_worktree", {
            "branch": "feat",
            "base_branch": "main",
        })

        assert result == _ok("req-1", {"path": "/tmp/repo-feat", "branch": "feat"})
        assert engine.world.created == [("feat", "main")]
        # The worktree list is refreshed right away.
        assert [e.event for e in seen] == ["worktrees.changed"]
        assert engine.worktree("/tmp/repo-feat") is not None

    @pytest.mark.asyncio
    async def test_propagates_worktree_manager_error(self):
        engine = _make_engine()
        engine.world.create_error = WorktreeManagerError("branch exists")
        server = _server(engine)

        result = await server._dispatch("req-1", "create_worktree", {"branch": "feat"})

        assert "error" in result
        assert result["error"]["code"] == "WORKTREE_ERROR"

    @pytest.mark.asyncio
    async def test_custom_create_runs_subprocess_without_selected_worktree(self):
        """Custom create command must not require a focused worktree in the UI.

        Regression: previously dispatched into the selected worktree's
        terminal pane, which made MCP-driven orchestration impossible when
        no worktree was selected. It runs as an engine job in the repo root.
        """
        engine = _make_engine(config=_custom(create="echo creating {branch}"))
        server = _server(engine)
        proc = _fake_proc(stdout=b"ok\n")

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
        assert result["result"]["path"] == "/tmp/repo-feat"
        assert result["result"]["job_id"]
        assert "creating feat" in _fake_shell.cmd
        assert _fake_shell.kwargs["cwd"] == "/tmp/repo"

    @pytest.mark.asyncio
    async def test_custom_create_surfaces_subprocess_failure(self):
        engine = _make_engine(config=_custom(create="false"))
        server = _server(engine)

        async def _fake_shell(cmd, **kwargs):
            return _fake_proc(stderr=b"boom\n", returncode=2)

        with patch("asyncio.create_subprocess_shell", side_effect=_fake_shell):
            result = await server._dispatch("req-1", "create_worktree", {"branch": "feat"})

        assert "error" in result
        assert result["error"]["code"] == "WORKTREE_ERROR"
        assert "boom" in result["error"]["message"]


class TestRemoveWorktree:
    @pytest.mark.asyncio
    async def test_rejects_main_worktree(self):
        wt = _make_worktree("/tmp/repo", "main", is_main=True)
        engine = _make_engine(worktrees=[wt])
        server = _server(engine)

        result = await server._dispatch("req-1", "remove_worktree", {"worktree_path": "/tmp/repo"})
        assert "error" in result
        assert "main worktree" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_rejects_worktree_with_running_agent(self):
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        state = AgentState(status=AgentStatus.RUNNING)
        engine = _make_engine(worktrees=[wt], agent_states={"/tmp/repo-feat": {"a1": state}})
        server = _server(engine)

        result = await server._dispatch("req-1", "remove_worktree", {"worktree_path": "/tmp/repo-feat"})
        assert "error" in result
        assert "running" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_unknown_worktree(self):
        engine = _make_engine()
        server = _server(engine)

        result = await server._dispatch("req-1", "remove_worktree", {"worktree_path": "/tmp/unknown"})
        assert "error" in result
        assert "Unknown worktree" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_removes_worktree(self):
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        engine = _make_engine(
            worktrees=[wt],
            agent_states={
                "/tmp/repo-feat": {"a1": AgentState(status=AgentStatus.COMPLETED, agent_id="a1")}
            },
        )
        server = _server(engine)

        result = await server._dispatch("req-1", "remove_worktree", {
            "worktree_path": "/tmp/repo-feat",
            "force": True,
        })

        assert result == _ok("req-1", {"removed": "/tmp/repo-feat"})
        assert engine.world.removed == [("/tmp/repo-feat", True)]
        # Each agent of the worktree is reported as exited.
        events, _ = engine.events.since(0)
        assert [(e.worktree_path, e.agent_id, e.status) for e in events] == [
            ("/tmp/repo-feat", "a1", "exited")
        ]
        assert engine.worktree("/tmp/repo-feat") is None

    @pytest.mark.asyncio
    async def test_ignores_a_custom_remove_command(self):
        """MCP removals stay on ``git worktree remove``, as before; only the
        TUI runs the configured command."""
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        engine = _make_engine(worktrees=[wt], config=_custom(remove="false"))
        server = _server(engine)

        with patch("asyncio.create_subprocess_shell") as shell:
            result = await server._dispatch(
                "req-1", "remove_worktree", {"worktree_path": "/tmp/repo-feat"}
            )

        assert result == _ok("req-1", {"removed": "/tmp/repo-feat"})
        shell.assert_not_called()
        assert engine.world.removed == [("/tmp/repo-feat", False)]


class TestSpawnAgent:
    @pytest.mark.asyncio
    async def test_rejects_empty_worktree_path(self):
        engine = _make_engine()
        server = _server(engine)

        result = await server._dispatch("req-1", "spawn_agent", {"worktree_path": ""})
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_rejects_unknown_worktree(self):
        engine = _make_engine()
        server = _server(engine)

        result = await server._dispatch("req-1", "spawn_agent", {"worktree_path": "/tmp/unknown"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_does_not_reject_when_agent_already_running(self, monkeypatch):
        """Duplicate spawn is allowed — a second agent is added."""
        no_pty(monkeypatch)
        wt = _make_worktree("/tmp/repo-feat", "feat", is_main=False)
        state = AgentState(status=AgentStatus.RUNNING, agent_id="a1")
        engine = _make_engine(
            worktrees=[wt], agent_states={"/tmp/repo-feat": {"a1": state}}
        )
        server = _server(engine)

        result = await server._dispatch(
            "req-1", "spawn_agent", {"worktree_path": "/tmp/repo-feat"}
        )

        assert "result" in result, result
        assert result["result"]["agent_id"] == "a2"
        assert engine.agent_ids("/tmp/repo-feat") == ["a2"]

    @pytest.mark.asyncio
    async def test_returns_the_cursor_taken_before_the_spawn(self, monkeypatch):
        no_pty(monkeypatch)
        engine = _make_engine(worktrees=[_make_worktree()])
        engine.events.emit("engine.notice", {"severity": "information", "message": "x"})
        server = _server(engine)

        result = await server._dispatch("req-1", "spawn_agent", {"worktree_path": "/tmp/repo"})

        assert result["result"]["cursor"] == 1
        spawned = [e for e in engine.events.history(1) if e.event == "agent.spawned"]
        assert [e.data["agent_id"] for e in spawned] == ["a1"]

    @pytest.mark.asyncio
    async def test_rejects_a_malformed_env(self):
        engine = _make_engine(worktrees=[_make_worktree()])
        server = _server(engine)

        result = await server._dispatch(
            "req-1", "spawn_agent", {"worktree_path": "/tmp/repo", "env": {"A": 1}}
        )

        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestStopAgent:
    @pytest.mark.asyncio
    async def test_stops_the_named_agent(self, monkeypatch):
        no_pty(monkeypatch)
        engine = _make_engine(worktrees=[_make_worktree()])
        await engine.spawn_agent("/tmp/repo")
        await engine.spawn_agent("/tmp/repo")
        server = _server(engine)

        result = await server._dispatch(
            "req-1", "stop_agent", {"worktree_path": "/tmp/repo", "agent_id": "a2"}
        )

        assert result == _ok(
            "req-1", {"worktree_path": "/tmp/repo", "agent_id": "a2", "status": "stopped"}
        )
        assert engine.agent_ids("/tmp/repo") == ["a1"]

    @pytest.mark.asyncio
    async def test_rejects_a_worktree_without_agents(self):
        engine = _make_engine(worktrees=[_make_worktree()])
        server = _server(engine)

        result = await server._dispatch("req-1", "stop_agent", {"worktree_path": "/tmp/repo"})

        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "No running agent" in result["error"]["message"]


class TestGetAgentStatus:
    @pytest.mark.asyncio
    async def test_returns_no_agent_when_not_tracked(self):
        engine = _make_engine()
        server = _server(engine)

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
        engine = _make_engine(agent_states={"/tmp/repo": {"a1": state}})
        server = _server(engine)

        result = await server._dispatch("req-1", "get_agent_status", {"worktree_path": "/tmp/repo"})
        assert result["result"]["status"] == "waiting_for_user"
        assert result["result"]["confidence"] == "high"
        assert result["result"]["detail"] == "needs input"
        assert result["result"]["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_requires_agent_id_when_multiple(self):
        engine = _make_engine(
            agent_states={
                "/tmp/repo": {
                    "a1": AgentState(status=AgentStatus.RUNNING, agent_id="a1"),
                    "a2": AgentState(status=AgentStatus.RUNNING, agent_id="a2"),
                }
            }
        )
        server = _server(engine)

        result = await server._dispatch(
            "req-1", "get_agent_status", {"worktree_path": "/tmp/repo"}
        )
        assert "error" in result
        assert "specify agent_id" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_selects_specific_agent_when_multiple(self):
        engine = _make_engine(
            agent_states={
                "/tmp/repo": {
                    "a1": AgentState(status=AgentStatus.RUNNING, agent_id="a1"),
                    "a2": AgentState(
                        status=AgentStatus.WAITING_FOR_USER, agent_id="a2"
                    ),
                }
            }
        )
        server = _server(engine)

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
        engine = _make_engine()
        server = _server(engine)

        result = await server._dispatch(
            "req-1", "list_agents", {"worktree_path": "/tmp/repo"}
        )
        assert result["result"] == {"worktree_path": "/tmp/repo", "agents": []}

    @pytest.mark.asyncio
    async def test_lists_agents(self):
        engine = _make_engine(
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
        server = _server(engine)

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
        engine = _make_engine()
        server = _server(engine)

        result = await server._dispatch("req-1", "read_agent_output", {"worktree_path": "/tmp/repo"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_returns_the_session_tail(self):
        """The handler reports the session's headless screen; how the tail
        spans scrollback and screen is tested on the session itself."""
        session = _session()
        session.tail_lines.return_value = ["line 10", "line 11"]
        session.total_lines = 13
        engine = _make_engine()
        add_session(engine, session)

        server = _server(engine)
        result = await server._dispatch(
            "req-1", "read_agent_output", {"worktree_path": "/tmp/repo", "lines": 6}
        )

        session.tail_lines.assert_called_once_with(6)
        assert result["result"]["lines"] == ["line 10", "line 11"]
        assert result["result"]["total_lines"] == 13


class TestSendAgentInput:
    """The handler must go through send_input, which separates the payload
    from the Enter that submits it — a raw ``text + "\\r"`` write leaves the
    instruction sitting unsent in the agent's prompt."""

    @pytest.fixture(autouse=True)
    def _no_confirm_wait(self, monkeypatch):
        # These agents never react; don't sit out the delivery confirmation.
        monkeypatch.setattr("lazyagent.engine.methods._INPUT_CONFIRM_TIMEOUT", 0.0)

    @staticmethod
    def _engine_with_session():
        engine = _make_engine(worktrees=[_make_worktree()])
        session = _session()
        add_session(engine, session)
        return engine, session

    @pytest.mark.asyncio
    async def test_delegates_to_send_input_with_submit(self):
        engine, session = self._engine_with_session()
        server = _server(engine)

        result = await server._dispatch(
            "req-1",
            "send_agent_input",
            {"worktree_path": "/tmp/repo", "text": "run the tests"},
        )

        assert result["result"]["status"] == "sent"
        session.send_input.assert_awaited_once_with("run the tests", submit=True)

    @pytest.mark.asyncio
    async def test_rejects_empty_text(self):
        engine, session = self._engine_with_session()
        server = _server(engine)

        result = await server._dispatch(
            "req-1", "send_agent_input", {"worktree_path": "/tmp/repo", "text": ""}
        )

        assert result["error"]["code"] == "VALIDATION_ERROR"
        session.send_input.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_a_stopped_session(self):
        engine, session = self._engine_with_session()
        session.running = False
        server = _server(engine)

        result = await server._dispatch(
            "req-1", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "hi"}
        )

        assert result["error"]["code"] == "VALIDATION_ERROR"
        session.send_input.assert_not_awaited()


def _set_status(engine, status, *, path="/tmp/repo", agent_id="a1", detail=""):
    """Record a status the way a session report does."""
    engine.registry.update_status(
        path, agent_id, status, LifecycleConfidence.LOW, detail
    )


def _engine_with_agent(status=AgentStatus.WAITING, detail=""):
    engine = _make_engine(worktrees=[_make_worktree()])
    _set_status(engine, status, detail=detail)
    session = _session()
    add_session(engine, session)
    return engine, session


def _react(engine, *statuses, delay=0.0):
    """send_input side effect: the agent moves through ``statuses``."""

    async def _side_effect(text, submit=False):
        async def _later():
            for status in statuses:
                await asyncio.sleep(delay)
                _set_status(engine, status)

        asyncio.get_running_loop().create_task(_later())

    return _side_effect


class TestSendAgentInputDelivery:
    @pytest.mark.asyncio
    async def test_confirmed_when_the_agent_starts_a_turn(self):
        engine, session = _engine_with_agent(AgentStatus.WAITING)
        session.send_input.side_effect = _react(engine, AgentStatus.RUNNING)
        server = _server(engine)

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
        monkeypatch.setattr("lazyagent.engine.methods._INPUT_CONFIRM_TIMEOUT", 0.01)
        engine, session = _engine_with_agent(AgentStatus.WAITING)
        server = _server(engine)

        result = await server._dispatch(
            "r", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "go"}
        )

        res = result["result"]
        assert res["delivery"] == "unconfirmed"
        assert "read_agent_output" in res["warnings"][0]

    @pytest.mark.asyncio
    async def test_busy_agent_is_not_waited_on(self):
        engine, session = _engine_with_agent(AgentStatus.RUNNING)
        server = _server(engine)

        result = await server._dispatch(
            "r", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "go"}
        )

        assert result["result"]["delivery"] == "busy"

    @pytest.mark.asyncio
    async def test_warns_when_the_text_answered_a_dialog(self):
        engine, session = _engine_with_agent(
            AgentStatus.WAITING_FOR_APPROVAL, detail="needs approval"
        )
        session.send_input.side_effect = _react(engine, AgentStatus.RUNNING)
        server = _server(engine)

        result = await server._dispatch(
            "r", "send_agent_input", {"worktree_path": "/tmp/repo", "text": "1"}
        )

        res = result["result"]
        assert res["delivery"] == "confirmed"
        assert "answered that dialog" in res["warnings"][0]

    @pytest.mark.asyncio
    async def test_confirm_false_skips_the_check(self):
        engine, session = _engine_with_agent(AgentStatus.WAITING)
        server = _server(engine)

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
        engine, session = _engine_with_agent(AgentStatus.WAITING)
        session.send_input.side_effect = _react(engine, AgentStatus.RUNNING)
        server = _server(engine)

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
        engine, _ = _engine_with_agent(AgentStatus.RUNNING)
        server = _server(engine)

        result = await server._dispatch("r", method, {"worktree_path": "/tmp/repo"})

        assert result["error"]["code"] == "UNKNOWN_METHOD"


class TestListEvents:
    @pytest.mark.asyncio
    async def test_without_since_starts_from_now(self):
        engine, _ = _engine_with_agent(AgentStatus.WAITING)
        server = _server(engine)

        result = await server._dispatch("r", "list_events", {})

        assert result["result"] == {"events": [], "cursor": 1, "truncated": False}

    @pytest.mark.asyncio
    async def test_filters_and_advances_cursor_past_skipped_events(self):
        engine, _ = _engine_with_agent(AgentStatus.RUNNING)
        _set_status(engine, AgentStatus.WAITING)
        _set_status(engine, AgentStatus.RUNNING, path="/tmp/other")
        server = _server(engine)

        result = await server._dispatch(
            "r", "list_events", {"since": 0, "statuses": ["idle"]}
        )

        res = result["result"]
        assert [e["status"] for e in res["events"]] == ["waiting"]
        assert res["cursor"] == 3

    @pytest.mark.asyncio
    async def test_never_blocks_even_with_a_timeout(self):
        engine, _ = _engine_with_agent(AgentStatus.RUNNING)
        server = _server(engine)

        result = await asyncio.wait_for(
            server._dispatch(
                "r", "list_events", {"statuses": ["idle"], "timeout": 600}
            ),
            1,
        )

        assert result["result"]["events"] == []

    @pytest.mark.asyncio
    async def test_rejects_a_bad_cursor(self):
        engine, _ = _engine_with_agent(AgentStatus.RUNNING)
        server = _server(engine)

        result = await server._dispatch("r", "list_events", {"since": -1})

        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestDispatch:
    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self):
        engine = _make_engine()
        server = _server(engine)

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
