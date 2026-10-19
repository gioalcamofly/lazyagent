"""IPC server — the legacy Unix domain socket server of the engine.

Receives JSON requests from the MCP server process and dispatches them to
the methods of :class:`~lazyagent.engine.methods.EngineMethods`, which call
the :class:`~lazyagent.engine.engine.Engine`'s public API only. It runs on
the engine's loop, so the methods call it directly. The protocol v1 server
(:mod:`lazyagent.engine.server`) answers the same methods.

Protocol: newline-delimited JSON.

    Request:  {"id": "uuid", "method": "list_worktrees", "params": {}}
    Success:  {"id": "uuid", "result": [...]}
    Error:    {"id": "uuid", "error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any

from lazyagent.engine.methods import EngineMethods, classify

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)


def _ok(request_id: str, result: Any) -> dict:
    return {"id": request_id, "result": result}


def _err(request_id: str, code: str, message: str) -> dict:
    return {"id": request_id, "error": {"code": code, "message": message}}


class IpcServer:
    """Asyncio Unix domain socket server in front of an :class:`Engine`."""

    def __init__(self, engine: Engine, socket_path: str) -> None:
        self._engine = engine
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._handlers = EngineMethods(engine).handlers

    @property
    def socket_path(self) -> str:
        return self._socket_path

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
        )
        log.info("IPC server listening on %s", self._socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        # Remove the parent directory if empty
        parent = os.path.dirname(self._socket_path)
        try:
            os.rmdir(parent)
        except OSError:
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection (one request per line)."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue

                request_id = request.get("id", "")
                method = request.get("method", "")
                params = request.get("params", {})

                response = await self._dispatch(request_id, method, params)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _dispatch(self, request_id: str, method: str, params: dict) -> dict:
        handler = self._handlers.get(method)

        if handler is None:
            return _err(request_id, "UNKNOWN_METHOD", f"Unknown method: {method}")

        try:
            return _ok(request_id, await handler(params))
        except Exception as e:
            code, message, _ = classify(e)
            if code == "INTERNAL_ERROR":
                log.exception("IPC handler error for method=%s", method)
            return _err(request_id, code, message)


async def start_ipc_server(engine: Engine) -> tuple[IpcServer, str]:
    """Factory: create and start an IPC server for the given engine.

    Returns ``(server, socket_path)``.
    Socket is created at ``/tmp/lazyagent-{pid}/ipc.sock`` with 0o700 dir
    and 0o600 socket perms so other users on the host cannot connect to it
    (the IPC handlers can spawn agents and remove worktrees, so an
    unauthenticated cross-user connection is privilege escalation).
    """
    socket_dir = os.path.join(tempfile.gettempdir(), f"lazyagent-{os.getpid()}")
    os.makedirs(socket_dir, exist_ok=True)
    # Re-apply mode in case the directory already existed with looser perms.
    os.chmod(socket_dir, 0o700)
    socket_path = os.path.join(socket_dir, "ipc.sock")

    # Remove stale socket if present
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    server = IpcServer(engine, socket_path)
    await server.start()
    # start_unix_server doesn't accept a mode arg; chmod after bind.
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    return server, socket_path
