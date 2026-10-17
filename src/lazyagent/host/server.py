"""The host's web server: the page, and one WebSocket per open page.

Listens on 127.0.0.1 only. Every request must name us in ``Host``
(127.0.0.1 or localhost with our port), which defeats DNS rebinding; the
WebSocket upgrade must also come from our own ``Origin`` (cross-site
WebSocket hijacking), and its first message must carry the token printed in
the URL. Responses carry a self-only Content-Security-Policy.

Binary messages both ways are protocol v1 stream frame payloads
(``u8 type · u32 stream_id · u64 offset`` for output, ``u8 type · u32
stream_id`` for input, then the bytes; :mod:`lazyagent.protocol.framing`).
Stream 0 is the hosted TUI's PTY; any other id is a stream this page
attached through ``attach_stream`` on its own engine connection. Output
frames are passed on as the engine sent them (re-framed, not decoded), and
input frames are spliced to the engine unchanged, after a check that the
page owns the stream.

WebSocket messages, page → host:

- binary: input frames (keystrokes) for the TUI (stream 0) or a stream;
- ``{"type": "auth", "token"}``: first, and only once;
- ``{"type": "resize", "rows", "cols", "redraw"?}``: the terminal panel's
  size (``redraw`` asks the TUI to repaint even when it is unchanged);
- ``{"type": "req", "id", "method", "params"}``: a v1 request, forwarded to
  the engine when ``method`` is in :data:`ALLOWED_METHODS`.

Host → page:

- binary: output frames of the TUI (stream 0) and of the page's streams;
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
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazyagent import __version__
from lazyagent.engine.client import Client
from lazyagent.engine.client import ConnectionClosed as EngineClosed
from lazyagent.engine.client import RequestError, Stream
from lazyagent.host import websocket as ws
from lazyagent.host.tui import HostedTui
from lazyagent.protocol import UI_COMMAND_METHODS, framing

log = logging.getLogger(__name__)

# What a page may ask the engine: views, read-only data, its own terminals
# (streams, sizes, worktree shells), and the actions of the TUI's dialogs
# (ui-plan Q2: the page could already type them into the TUI).
ALLOWED_METHODS = frozenset(
    {
        "get_sidebar_state",
        "get_config",
        "list_worktrees",
        "list_agents",
        "refresh_worktrees",
        "refresh_git_status",
        "get_pr_info",
        "get_diff",
        "create_worktree",
        "remove_worktree",
        "spawn_agent",
        "stop_agent",
        "subscribe",
        "unsubscribe",
        "attach_stream",
        "detach_stream",
        "claim_size",
        "resize",
        "open_shell",
        "ui.get_view",
        *UI_COMMAND_METHODS,
    }
)

# Methods that start processes: the host puts its own environment in them
# (D5: the host is the client, started from the user's shell, exactly as
# the TUI passes its own), whatever the page sent.
ENV_METHODS = frozenset({"spawn_agent", "create_worktree", "remove_worktree", "open_shell"})

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
# A page this far behind on its streams loses the one that overflows (it
# gets stream.reset and re-attaches); the TUI's output keeps MAX_BUFFERED.
MAX_STREAM_BUFFERED = 2 * 1024 * 1024
TUI_STREAM = 0
_OUTPUT_HEADER = struct.Struct(">BIQ")
_INPUT_HEADER = struct.Struct(">BI")
_FRAME_HEADER = struct.Struct(">IB")


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
    # Streams this page dropped for being too slow: their late frames are
    # not sent.
    dropped: set[int] = field(default_factory=set)


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
        client_env: dict[str, str] | None = None,
    ) -> None:
        self.tui = tui
        self.client_env = dict(client_env) if client_env is not None else None
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
        # Bytes of TUI output so far: the offset of stream 0's next frame.
        self.tui_offset = 0

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
        frame = _OUTPUT_HEADER.pack(framing.STREAM_OUTPUT, TUI_STREAM, self.tui_offset) + data
        self.tui_offset += len(data)
        for peer in list(self.peers):
            if peer.socket.buffered > MAX_BUFFERED:
                self._drop(peer, ws.CLOSE_TRY_AGAIN, "too slow")
                continue
            self._send_binary(peer, frame)

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
            # Not TUI output: offset 0 marks it as a preamble.
            self._send_binary(
                peer, _OUTPUT_HEADER.pack(framing.STREAM_OUTPUT, TUI_STREAM, 0) + modes
            )
        self.peers.add(peer)
        self._spawn(peer, self._bridge_engine(peer))
        try:
            while True:
                message = await socket.recv()
                if isinstance(message, bytes):
                    self._on_input(peer, message)
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

    def _on_input(self, peer: Peer, frame: bytes) -> None:
        """An input frame: keys for the TUI, or spliced to the engine."""
        if len(frame) < _INPUT_HEADER.size or frame[0] != framing.STREAM_INPUT:
            self._error(peer, "", "PROTOCOL_ERROR", "expected an input stream frame")
            return
        _, stream_id = _INPUT_HEADER.unpack_from(frame)
        if stream_id == TUI_STREAM:
            self.tui.write(memoryview(frame)[_INPUT_HEADER.size :])
        elif peer.engine is not None and peer.engine.stream(stream_id) is not None:
            peer.engine.send_raw(
                _FRAME_HEADER.pack(len(frame), framing.KIND_STREAM) + frame
            )
        else:
            self._error(peer, "", "PROTOCOL_ERROR", f"no stream {stream_id} here")

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
            elif method == "attach_stream":
                self._spawn(peer, self._attach(peer, request_id, params))
            elif method == "detach_stream":
                self._spawn(peer, self._detach(peer, request_id, params))
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
        if method in ENV_METHODS:
            params = {k: v for k, v in params.items() if k != "env"}
            if self.client_env is not None:
                params["env"] = self.client_env
        try:
            response = await peer.engine.call(method, params)
        except EngineClosed:
            self._error(
                peer, request_id, "NOT_READY", "the engine connection is closed"
            )
            return
        self._send(peer, {**response.to_dict(), "id": request_id})

    async def _attach(self, peer: Peer, request_id: str, params: dict) -> None:
        """attach_stream on the page's engine connection; its output frames
        follow the reply (the ones read with it wait until it is sent)."""
        assert peer.engine is not None
        held: list[tuple[int, bytes]] | None = []
        stream_id = -1

        def on_output(offset: int, data: bytes) -> None:
            if held is not None:
                held.append((offset, data))
            elif stream_id in peer.dropped:
                return
            elif peer.socket.buffered > MAX_STREAM_BUFFERED:
                self._drop_stream(peer, stream, offset)
            else:
                self._send_binary(
                    peer,
                    _OUTPUT_HEADER.pack(framing.STREAM_OUTPUT, stream_id, offset) + data,
                )

        session_id = params.get("session_id")
        from_offset = params.get("from_offset")
        try:
            stream = await peer.engine.attach(
                session_id,  # type: ignore[arg-type]  (the engine validates)
                on_output,
                from_offset=from_offset,
            )
        except RequestError as e:
            self._error(peer, request_id, e.code, e.message)
            return
        except EngineClosed:
            self._error(peer, request_id, "NOT_READY", "the engine connection is closed")
            return
        stream_id = stream.stream_id
        result: dict[str, Any] = {
            "stream_id": stream.stream_id,
            "session_id": stream.session_id,
            "size": {"rows": stream.rows, "cols": stream.cols},
            "mode": stream.mode,
            "offset": stream.offset,
        }
        if stream.snapshot is not None:
            result["snapshot"] = stream.snapshot
        self._send(peer, {"type": "res", "id": request_id, "result": result})
        frames, held = held, None
        for offset, data in frames:
            on_output(offset, data)

    def _drop_stream(self, peer: Peer, stream: Stream, offset: int) -> None:
        """The page cannot keep up with this stream: stop it, and tell the
        page where to re-attach from, as the engine's own stream.reset does."""
        peer.dropped.add(stream.stream_id)
        self._send(
            peer,
            {
                "type": "evt",
                "seq": 0,
                "event": "stream.reset",
                "data": {
                    "stream_id": stream.stream_id,
                    "session_id": stream.session_id,
                    "rows": stream.rows,
                    "cols": stream.cols,
                    "offset": offset,
                },
            },
        )
        if peer.engine is not None:
            engine = peer.engine

            async def detach() -> None:
                try:
                    await engine.detach(stream.stream_id)
                except (RequestError, EngineClosed):
                    pass

            self._spawn(peer, detach())

    async def _detach(self, peer: Peer, request_id: str, params: dict) -> None:
        assert peer.engine is not None
        stream_id = params.get("stream_id")
        if not isinstance(stream_id, int) or peer.engine.stream(stream_id) is None:
            # Gone already (closed, reset, dropped): nothing to detach.
            self._send(
                peer,
                {"type": "res", "id": request_id, "result": {"stream_id": stream_id, "detached": False}},
            )
            return
        try:
            await peer.engine.detach(stream_id)
        except RequestError as e:
            self._error(peer, request_id, e.code, e.message)
            return
        except EngineClosed:
            self._error(peer, request_id, "NOT_READY", "the engine connection is closed")
            return
        self._send(
            peer,
            {"type": "res", "id": request_id, "result": {"stream_id": stream_id, "detached": True}},
        )

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
