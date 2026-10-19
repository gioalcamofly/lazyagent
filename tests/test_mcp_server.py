"""Tests for the MCP server IpcClient."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from lazyagent.mcp_server import IpcClient, _get_client


class TestIpcClient:
    @pytest.mark.asyncio
    async def test_call_sends_correct_json_and_returns_result(self, tmp_path):
        """Start a fake Unix socket server and verify IpcClient sends correct format."""
        socket_path = str(tmp_path / "test.sock")
        received_requests: list[dict] = []

        async def handle_client(reader, writer):
            line = await reader.readline()
            request = json.loads(line)
            received_requests.append(request)
            response = {"id": request["id"], "result": [{"path": "/tmp/wt"}]}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handle_client, path=socket_path)

        try:
            client = IpcClient(socket_path)
            result = await client.call("list_worktrees", {"foo": "bar"})

            assert result == [{"path": "/tmp/wt"}]
            assert len(received_requests) == 1

            req = received_requests[0]
            assert req["method"] == "list_worktrees"
            assert req["params"] == {"foo": "bar"}
            assert "id" in req
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_call_raises_on_error_response(self, tmp_path):
        socket_path = str(tmp_path / "test.sock")

        async def handle_client(reader, writer):
            line = await reader.readline()
            request = json.loads(line)
            response = {
                "id": request["id"],
                "error": {"code": "VALIDATION_ERROR", "message": "bad input"},
            }
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handle_client, path=socket_path)

        try:
            client = IpcClient(socket_path)
            with pytest.raises(RuntimeError, match="VALIDATION_ERROR.*bad input"):
                await client.call("create_worktree", {"branch": ""})
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_call_raises_on_closed_connection(self, tmp_path):
        socket_path = str(tmp_path / "test.sock")

        async def handle_client(reader, writer):
            # Close immediately without responding
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handle_client, path=socket_path)

        try:
            client = IpcClient(socket_path)
            with pytest.raises(ConnectionError):
                await client.call("list_worktrees")
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_call_with_no_params(self, tmp_path):
        """Calling with no params sends empty dict."""
        socket_path = str(tmp_path / "test.sock")
        received_requests: list[dict] = []

        async def handle_client(reader, writer):
            line = await reader.readline()
            request = json.loads(line)
            received_requests.append(request)
            response = {"id": request["id"], "result": []}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handle_client, path=socket_path)

        try:
            client = IpcClient(socket_path)
            await client.call("list_worktrees")
            assert received_requests[0]["params"] == {}
        finally:
            server.close()
            await server.wait_closed()


class TestGetClient:
    def test_raises_when_no_socket_found(self):
        with patch.dict(os.environ, {}, clear=True):
            # Ensure LAZYAGENT_SOCKET is not set
            os.environ.pop("LAZYAGENT_SOCKET", None)
            with patch("lazyagent.mcp_server._discover_socket", return_value=None):
                with pytest.raises(RuntimeError, match="No running lazyagent"):
                    _get_client()

    def test_returns_client_when_env_set(self):
        with patch.dict(os.environ, {"LAZYAGENT_SOCKET": "/tmp/test.sock"}):
            client = _get_client()
            assert isinstance(client, IpcClient)
            assert client._socket_path == "/tmp/test.sock"


@pytest.mark.asyncio
async def test_create_worktree_leaves_the_base_to_the_engine(monkeypatch):
    """No base_branch given: the tool sends none, and the engine picks the
    repo's (not a hard-coded "main")."""
    from lazyagent import mcp_server

    calls = []

    class Client:
        async def call(self, method, params=None):
            calls.append((method, params))
            return {}

    monkeypatch.setattr(mcp_server, "_get_client", lambda: Client())
    await mcp_server.create_worktree("feat")
    await mcp_server.create_worktree("feat2", base_branch="develop")
    assert calls == [
        ("create_worktree", {"branch": "feat"}),
        ("create_worktree", {"branch": "feat2", "base_branch": "develop"}),
    ]


class TestNotesTools:
    """The notes tools forward the optional target, and who calls: this
    agent's token from its env, when lazyagent spawned it."""

    @pytest.fixture
    def calls(self, monkeypatch):
        from lazyagent import mcp_server

        calls = []

        class Client:
            async def call(self, method, params=None):
                calls.append((method, params))
                return {}

        monkeypatch.setattr(mcp_server, "_get_client", lambda: Client())
        return calls

    @pytest.mark.asyncio
    async def test_the_token_goes_as_caller(self, calls, monkeypatch):
        from lazyagent import mcp_server

        monkeypatch.setenv("LAZYAGENT_AGENT_TOKEN", "tok-1")
        await mcp_server.notes_read()
        await mcp_server.notes_append("Decisions", "use sqlite")
        await mcp_server.notes_replace_section("Pending", "- [x] tests", 3)
        assert calls == [
            ("notes_get", {"caller": "tok-1"}),
            (
                "notes_append",
                {"section": "Decisions", "text": "use sqlite", "caller": "tok-1"},
            ),
            (
                "notes_replace_section",
                {
                    "section": "Pending",
                    "text": "- [x] tests",
                    "if_rev": 3,
                    "caller": "tok-1",
                },
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_target_is_forwarded(self, calls, monkeypatch):
        from lazyagent import mcp_server

        monkeypatch.setenv("LAZYAGENT_AGENT_TOKEN", "tok-1")
        await mcp_server.notes_read("/wt", "a2")
        await mcp_server.notes_append("Doubts", "which db?", worktree_path="/wt")
        assert calls == [
            ("notes_get", {"worktree_path": "/wt", "agent_id": "a2", "caller": "tok-1"}),
            (
                "notes_append",
                {
                    "section": "Doubts",
                    "text": "which db?",
                    "worktree_path": "/wt",
                    "caller": "tok-1",
                },
            ),
        ]

    @pytest.mark.asyncio
    async def test_no_token_no_caller(self, calls, monkeypatch):
        from lazyagent import mcp_server

        monkeypatch.delenv("LAZYAGENT_AGENT_TOKEN", raising=False)
        await mcp_server.notes_read("/wt")
        assert calls == [("notes_get", {"worktree_path": "/wt"})]

    @pytest.mark.asyncio
    async def test_spawn_and_stop_carry_the_notes_options(self, calls):
        from lazyagent import mcp_server

        await mcp_server.spawn_agent("/wt", notes_mode="free")
        await mcp_server.stop_agent("/wt")
        await mcp_server.stop_agent("/wt", notes="keep")
        assert calls[0][1]["notes_mode"] == "free"
        assert calls[1:] == [
            ("stop_agent", {"worktree_path": "/wt"}),
            ("stop_agent", {"worktree_path": "/wt", "notes": "keep"}),
        ]
