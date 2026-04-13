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
    def test_raises_when_env_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            # Ensure LAZYAGENT_SOCKET is not set
            os.environ.pop("LAZYAGENT_SOCKET", None)
            with pytest.raises(RuntimeError, match="LAZYAGENT_SOCKET"):
                _get_client()

    def test_returns_client_when_env_set(self):
        with patch.dict(os.environ, {"LAZYAGENT_SOCKET": "/tmp/test.sock"}):
            client = _get_client()
            assert isinstance(client, IpcClient)
            assert client._socket_path == "/tmp/test.sock"
