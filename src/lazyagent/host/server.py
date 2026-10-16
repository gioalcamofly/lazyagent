"""The host's web server: the page, and one WebSocket per open page.

Listens on 127.0.0.1 only. Every request must name us in ``Host``
(127.0.0.1 or localhost with our port), which defeats DNS rebinding; the
WebSocket upgrade must also come from our own ``Origin`` (cross-site
WebSocket hijacking), and its first message must carry the token printed in
the URL. Responses carry a self-only Content-Security-Policy.

WebSocket messages, page → host:

- binary: keystrokes for the TUI;
- ``{"type": "auth", "token"}``: first, and only once;
- ``{"type": "resize", "rows", "cols", "redraw"?}``: the terminal panel's
  size (``redraw`` asks the TUI to repaint even when it is unchanged);
- ``{"type": "req", "id", "method", "params"}``: a v1 request, forwarded to
  the engine when ``method`` is in :data:`ALLOWED_METHODS`.

Host → page:

- binary: TUI output;
- ``{"type": "hello", "repo_root", "host", "engine": bool}`` after the auth;
- ``{"type": "engine", "ready": bool}`` when the engine connection opens or
  closes;
- ``{"type": "res", …}`` / ``{"type": "evt", …}``: v1 responses and the
  events of this page's own engine connection;
- ``{"type": "exit", "code"}``: the TUI is gone (the host stops next).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazyagent import __version__
from lazyagent.engine.client import Client
from lazyagent.engine.client import ConnectionClosed as EngineClosed
from lazyagent.host import websocket as ws
from lazyagent.host.tui import HostedTui
from lazyagent.protocol import UI_COMMAND_METHODS

log = logging.getLogger(__name__)

# What a page may ask the engine: views and read-only data. Anything that
# changes worktrees or agents goes through the TUI's own modals.
ALLOWED_METHODS = frozenset(
    {
        "get_sidebar_state",
        "list_worktrees",
        "get_pr_info",
        "get_diff",
        "subscribe",
        "unsubscribe",
        "ui.get_view",
        *UI_COMMAND_METHODS,
    }
)

CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; "
    # xterm.js writes its theme and cell metrics into <style> elements.
    "style-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
_REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    426: "Upgrade Required",
    431: "Request Header Fields Too Large",
}

AUTH_TIMEOUT = 10.0
MAX_MESSAGE = 1024 * 1024
# A page that falls this far behind the TUI's output is dropped; it can
# reload and gets a full repaint.
MAX_BUFFERED = 8 * 1024 * 1024
MAX_SIZE = 1000


def static_files(root: Path) -> dict[str, Path]:
    """URL path → file, for every servable file under ``root``."""
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in _TYPES:
            files["/" + path.relative_to(root).as_posix()] = path
    if "/index.html" in files:
        files["/"] = files["/index.html"]
    return files


@dataclass(eq=False)
class Peer:
    """One authenticated page."""

    socket: ws.WebSocket
    engine: Client | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)


class HostServer:
    def __init__(
        self,
        tui: HostedTui,
        *,
        token: str,
        static_root: Path,
        repo_root: str,
        host_id: str,
        port: int = 0,
    ) -> None:
        self.tui = tui
        self.token = token
        self.files = static_files(static_root)
        self.repo_root = repo_root
        self.host_id = host_id
        self.port = port
        self.peers: set[Peer] = set()
        self.engine_socket: str | None = None
        self._engine_ready = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task] = set()
        self.exit_code: int | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.port, limit=ws.MAX_REQUEST_HEAD
        )
        self.port = self._server.sockets[0].getsockname()[1]

    @property
    def _hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @property
    def _origins(self) -> set[str]:
        return {f"http://{h}" for h in self._hosts}

    # -- engine ------------------------------------------------------------

    def set_engine(self, socket: str) -> None:
        """The engine's v1 socket is known: pages connect to it."""
        if self.engine_socket is not None:
            return
        self.engine_socket = socket
        self._engine_ready.set()

    # -- TUI output --------------------------------------------------------

    def broadcast_output(self, data: bytes) -> None:
        for peer in list(self.peers):
            if peer.socket.buffered > MAX_BUFFERED:
                self._drop(peer, ws.CLOSE_TRY_AGAIN, "too slow")
                continue
            self._send_binary(peer, data)

    def _drop(self, peer: Peer, code: int, reason: str) -> None:
        self.peers.discard(peer)
        task = asyncio.get_running_loop().create_task(peer.socket.close(code, reason))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def tui_exited(self, code: int) -> None:
        """Tell every page and close them."""
        self.exit_code = code
        for peer in list(self.peers):
            self._send(peer, {"type": "exit", "code": code})
        await asyncio.gather(
            *(
                p.socket.close(ws.CLOSE_GOING_AWAY, "lazyagent exited")
                for p in self.peers
            ),
            return_exceptions=True,
        )
        self.peers.clear()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    # -- HTTP --------------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            await self._serve(reader, writer)
        except (ConnectionError, ws.ConnectionClosed):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("host connection failed")
        finally:
            self._handlers.discard(task)
            if not writer.is_closing():
                writer.close()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(ws.read_request(reader), AUTH_TIMEOUT)
        except ws.HttpError as e:
            await self._respond(writer, e.status, str(e))
            return
        except asyncio.TimeoutError:
            return
        if request.header("host") not in self._hosts:
            await self._respond(writer, 403, "unknown Host")
            return
        if request.path == "/ws":
            if request.header("origin") not in self._origins:
                await self._respond(writer, 403, "bad Origin")
                return
            problem = ws.upgrade_problem(request)
            if problem is not None:
                await self._respond(
                    writer, 426 if not ws.is_upgrade(request) else 400, problem
                )
                return
            key = request.header("sec-websocket-key")
            assert key is not None
            writer.write(ws.handshake_response(key))
            await self._session(ws.WebSocket(reader, writer, max_message=MAX_MESSAGE))
            return
        if request.method not in ("GET", "HEAD"):
            await self._respond(writer, 405, "method not allowed")
            return
        path = self.files.get(request.path)
        if path is None:
            await self._respond(writer, 404, "not found")
            return
        body = path.read_bytes()
        content_type = _TYPES.get(path.suffix) or mimetypes.guess_type(path.name)[0]
        await self._respond(
            writer,
            200,
            body,
            content_type=content_type or "application/octet-stream",
            head=request.method == "HEAD",
        )

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes | str,
        *,
        content_type: str = "text/plain; charset=utf-8",
        head: bool = False,
    ) -> None:
        if isinstance(body, str):
            body = (body + "\n").encode()
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Connection": "close",
            **SECURITY_HEADERS,
        }
        lines = [f"HTTP/1.1 {status} {_REASONS.get(status, '')}"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        if not head:
            writer.write(body)
        await writer.drain()

    # -- WebSocket session -------------------------------------------------

    def _send(self, peer: Peer, message: dict[str, Any]) -> None:
        try:
            peer.socket.send_text(json.dumps(message, separators=(",", ":")))
        except ws.ConnectionClosed:
            self.peers.discard(peer)

    def _send_binary(self, peer: Peer, data: bytes) -> None:
        try:
            peer.socket.send_binary(data)
        except ws.ConnectionClosed:
            self.peers.discard(peer)

    async def _authenticate(self, socket: ws.WebSocket) -> bool:
        try:
            first = await asyncio.wait_for(socket.recv(), AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            first = None
        token = None
        if isinstance(first, str):
            try:
                message = json.loads(first)
            except ValueError:
                message = None
            if isinstance(message, dict) and message.get("type") == "auth":
                token = message.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode(), self.token.encode()
        ):
            await socket.close(ws.CLOSE_POLICY, "unauthorized")
            return False
        return True

    async def _session(self, socket: ws.WebSocket) -> None:
        if not await self._authenticate(socket):
            return
        if self.exit_code is not None:
            socket.send_text(json.dumps({"type": "exit", "code": self.exit_code}))
            await socket.close(ws.CLOSE_GOING_AWAY, "lazyagent exited")
            return
        peer = Peer(socket)
        self._send(
            peer,
            {
                "type": "hello",
                "repo_root": self.repo_root,
                "host": self.host_id,
                "version": __version__,
                "engine": self.engine_socket is not None,
            },
        )
        # A fresh xterm.js has none of the modes the TUI set at its start.
        modes = self.tui.mode_sequence()
        if modes:
            self._send_binary(peer, modes)
        self.peers.add(peer)
        self._spawn(peer, self._bridge_engine(peer))
        try:
            while True:
                message = await socket.recv()
                if isinstance(message, bytes):
                    self.tui.write(message)
                else:
                    self._on_text(peer, message)
        except ws.ConnectionClosed:
            pass
        finally:
            self.peers.discard(peer)
            for task in list(peer.tasks):
                task.cancel()
            await asyncio.gather(*peer.tasks, return_exceptions=True)
            if peer.engine is not None:
                await peer.engine.close()
            if socket.closed is None:
                await socket.close(ws.CLOSE_GOING_AWAY)

    def _spawn(self, peer: Peer, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        peer.tasks.add(task)
        task.add_done_callback(peer.tasks.discard)

    def _on_text(self, peer: Peer, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            self._error(peer, "", "PROTOCOL_ERROR", "expected a JSON object")
            return
        kind = message.get("type")
        if kind == "resize":
            rows, cols = message.get("rows"), message.get("cols")
            if not all(type(n) is int and 1 <= n <= MAX_SIZE for n in (rows, cols)):
                self._error(peer, "", "VALIDATION_ERROR", "bad resize")
                return
            if (rows, cols) != (self.tui.rows, self.tui.cols):
                self.tui.resize(rows, cols)
            elif message.get("redraw"):
                self.tui.redraw()
        elif kind == "req":
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(request_id, str) or not isinstance(method, str):
                self._error(peer, "", "PROTOCOL_ERROR", "a request needs id and method")
            elif method not in ALLOWED_METHODS:
                self._error(
                    peer,
                    request_id,
                    "FORBIDDEN",
                    f"{method} is not available to the page",
                )
            elif not isinstance(params, dict):
                self._error(
                    peer, request_id, "VALIDATION_ERROR", "params must be an object"
                )
            elif peer.engine is None:
                self._error(peer, request_id, "NOT_READY", "no engine connection yet")
            else:
                self._spawn(peer, self._forward(peer, request_id, method, params))
        else:
            self._error(peer, "", "PROTOCOL_ERROR", f"unknown message type {kind!r}")

    def _error(self, peer: Peer, request_id: str, code: str, message: str) -> None:
        self._send(
            peer,
            {
                "type": "res",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
        )

    async def _forward(
        self, peer: Peer, request_id: str, method: str, params: dict
    ) -> None:
        assert peer.engine is not None
        try:
            response = await peer.engine.call(method, params)
        except EngineClosed:
            self._error(
                peer, request_id, "NOT_READY", "the engine connection is closed"
            )
            return
        self._send(peer, {**response.to_dict(), "id": request_id})

    async def _bridge_engine(self, peer: Peer) -> None:
        """Open this page's own engine connection and relay its events."""
        await self._engine_ready.wait()
        assert self.engine_socket is not None
        try:
            peer.engine = await Client.connect(
                self.engine_socket, client="web", client_version=__version__
            )
        except (OSError, EngineClosed, asyncio.TimeoutError) as e:
            log.warning("host: engine connection failed: %s", e)
            self._send(peer, {"type": "engine", "ready": False, "error": str(e)})
            return
        self._send(peer, {"type": "engine", "ready": True})
        async for event in peer.engine.events():
            self._send(peer, event.to_dict())
        self._send(peer, {"type": "engine", "ready": False})
