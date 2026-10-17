"""Protocol v1 server on a Unix socket, in front of an :class:`Engine`.

It runs on the engine's loop next to the legacy NDJSON server
(:mod:`lazyagent.ipc`) and answers the same ten methods through
:class:`~lazyagent.engine.methods.EngineMethods`, plus the v1 additions:
pushed events (``subscribe``), PTY streams (``attach_stream`` and binary
input frames), PTY size arbitration (``resize`` / ``claim_size``) and the
read-mostly helpers the TUI uses in-process (``open_shell``,
``refresh_worktrees``, ``get_diff``, …). The wire format is
:mod:`lazyagent.protocol`; ``docs/design/engine-split.md`` §4 is the spec.

Discovery. The socket lives in the repo's runtime dir,
``$XDG_RUNTIME_DIR/lazyagent/<sha1(repo_root)[:12]>/`` (or
``/tmp/lazyagent-<uid>/<hash>/``), dir 0700 and socket 0600, next to an
``engine.json`` (pid, version, protocol, socket) written atomically and
removed on stop. Peers must have this process's uid (``SO_PEERCRED``); no
token is needed on a Unix socket. When another live engine already serves
the socket, :meth:`V1Server.start` leaves it alone and returns False.

Streams. ``attach_stream`` answers with either a raw tail (the ring still
holds ``from_offset`` and it is small) or an ANSI snapshot of the headless
screen at offset N; output frames follow from that offset with no gap,
because the reply and the viewer registration happen in one loop step.
Each stream has a bounded queue: a client that falls behind by more than
:data:`STREAM_QUEUE_LIMIT` loses the stream and gets ``stream.reset``; the
engine never waits for a client.

Size. One owner per session, the latest client to claim it (``claim_size``,
or any input frame). Only the owner may ``resize``. The in-process TUI
resizes the session directly; that counts as the TUI claiming it, so a v1
client has to claim again. Every change is announced to each attached
stream as ``stream.resized``.
"""

from __future__ import annotations

import asyncio
import errno
import itertools
import json
import logging
import os
import socket
import struct
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent import __version__
from lazyagent.engine.events import EngineEvent
from lazyagent.engine.methods import EngineMethods, check_env, classify
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.runtime import (  # noqa: F401  (re-exported)
    INFO_NAME,
    SOCKET_NAME,
    read_info,
    remove_empty_dirs,
    runtime_dir,
    socket_path,
)
from lazyagent.protocol import UI_COMMAND_METHODS, framing, handshake
from lazyagent.protocol.envelope import (
    ErrorCode,
    ErrorInfo,
    Event,
    ProtocolError,
    Request,
    Response,
    parse_message,
)
from lazyagent.protocol.hosted import HOST_ENV

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine
    from lazyagent.engine.session import TerminalSession

log = logging.getLogger(__name__)

V1_SERVER_ENV = "LAZYAGENT_V1_SERVER"
UI_MODALS = ("spawn", "create", "remove")
_VIEW_FIELDS = ("worktree_path", "orchestrator", "agent_id", "focus", "modal")

# Output a client may have queued per stream before the stream is dropped.
STREAM_QUEUE_LIMIT = 1024 * 1024
# Control messages a client may have queued before it is disconnected.
CONTROL_QUEUE_LIMIT = 16 * 1024 * 1024
# Largest raw tail answered instead of a snapshot.
TAIL_LIMIT = STREAM_QUEUE_LIMIT // 2
# Output is split into frames of at most this many bytes.
FRAME_CHUNK = 64 * 1024
_READ_SIZE = 64 * 1024
_FLUSH_TIMEOUT = 0.5
_PROBE_TIMEOUT = 1.0

FEATURES: tuple[str, ...] = (
    "events.replay",
    "stream.snapshot",
    "stream.tail",
    "stream.input",
    "stream.closed",
    "size.latest",
)

STREAM_RESIZED = "stream.resized"
STREAM_RESET = "stream.reset"
STREAM_CLOSED = "stream.closed"


def enabled(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether to run the server (``LAZYAGENT_V1_SERVER=0`` turns it off)."""
    return environ.get(V1_SERVER_ENV, "1").strip() != "0"


def peer_uid(sock: Any) -> int | None:
    """The uid of the process at the other end of a Unix socket."""
    try:
        creds = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except (AttributeError, OSError):
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def private_dir(path: Path) -> None:
    """Create ``path`` (and missing parents) as 0700 dirs owned by us."""
    missing = []
    node = path
    while not node.exists():
        missing.append(node)
        node = node.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    for directory in {path, path.parent, *missing}:
        st = directory.stat()
        if st.st_uid != os.getuid():
            raise PermissionError(f"{directory} is not owned by this user")
        os.chmod(directory, 0o700)


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


async def probe(path: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """Whether some process accepts connections on the socket at ``path``.

    A socket file nobody listens on (refused, or not a socket) is dead. One
    that accepts, or that is too busy to accept in time, is alive.
    """
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path), timeout
        )
    except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError):
        return False
    except asyncio.TimeoutError:
        return True
    except OSError:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


class _Reply:
    """A result plus work to do once the response is queued (frames and
    events that must follow it on the wire)."""

    __slots__ = ("after", "result")

    def __init__(self, result: Any, after: Callable[[], None]) -> None:
        self.result = result
        self.after = after


@dataclass(eq=False)
class _Stream:
    stream_id: int
    session_id: str
    session: TerminalSession
    conn: _Connection
    detach: Callable[[], None] = lambda: None
    pending: int = 0


@dataclass(eq=False)
class _SessionState:
    """What the server tracks per session: its streams and size owner."""

    session: TerminalSession
    remove_listener: Callable[[], None]
    streams: set[_Stream] = field(default_factory=set)
    owner: _Connection | None = None


Handler = Callable[["_Connection", dict], Awaitable[Any]]


class _Connection:
    """One client: its frame reader, ordered outgoing queue, subscription,
    streams and interest set."""

    def __init__(
        self,
        server: V1Server,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn_id: int,
    ) -> None:
        self.server = server
        self.conn_id = conn_id
        self.interest_client = f"v1:{conn_id}"
        self.hello: handshake.Hello | None = None
        self.streams: dict[int, _Stream] = {}
        self.closed = False
        self._reader = reader
        self._writer = writer
        self._decoder = framing.FrameDecoder()
        # (frame bytes, stream id or None for control, output offset)
        self._queue: deque[tuple[bytes, int | None, int]] = deque()
        self._control_pending = 0
        self._wakeup = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._stream_ids = itertools.count(1)
        self._unsubscribe: Callable[[], None] | None = None
        # Set while this client is a hosted UI (attach_ui).
        self.detach_ui: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writer_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._writer_task = asyncio.get_running_loop().create_task(self._write_loop())
        try:
            while not self.closed:
                data = await self._reader.read(_READ_SIZE)
                if not data:
                    break
                self._decoder.feed(data)
                if not self._drain_frames():
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            self.close()

    def _drain_frames(self) -> bool:
        """Handle every complete frame; False when the stream is unusable."""
        while not self.closed:
            try:
                frame = self._decoder.next_frame()
            except framing.FrameError as e:
                if self._decoder.failed:
                    log.warning("v1 client %s: %s; closing", self.conn_id, e)
                    return False
                self.send_error("", ErrorCode.PROTOCOL_ERROR, str(e))
                continue
            if frame is None:
                return True
            try:
                if isinstance(frame, framing.ControlFrame):
                    self._on_control(frame.message)
                else:
                    self._on_stream(frame)
            except Exception:
                log.exception("v1 client %s: frame handling failed", self.conn_id)
        return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Nothing queued will be written any more: flush() must not wait.
        self._queue.clear()
        self._drained.set()
        for task in list(self._tasks):
            task.cancel()
        if self._writer_task is not None:
            self._writer_task.cancel()
        self.unsubscribe()
        for stream in list(self.streams.values()):
            self.server.drop_stream(stream)
        self.server.forget(self)
        try:
            self._writer.close()
        except (ConnectionError, OSError, RuntimeError):
            pass

    async def flush(self, timeout: float = _FLUSH_TIMEOUT) -> None:
        """Wait (bounded) for everything queued to be written."""
        try:
            await asyncio.wait_for(self._drained.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def _write_loop(self) -> None:
        try:
            while True:
                while not self._queue:
                    self._drained.set()
                    self._wakeup.clear()
                    await self._wakeup.wait()
                frame, stream_id, _offset = self._queue.popleft()
                self._writer.write(frame)
                await self._writer.drain()
                if stream_id is None:
                    self._control_pending -= len(frame)
                elif (stream := self.streams.get(stream_id)) is not None:
                    stream.pending -= len(frame)
        except (ConnectionError, OSError):
            self.close()

    def _enqueue(self, frame: bytes, stream_id: int | None, offset: int = 0) -> None:
        self._queue.append((frame, stream_id, offset))
        self._drained.clear()
        self._wakeup.set()

    def send_message(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            frame = framing.encode_control(message)
        except ValueError as e:
            if message.get("type") != "res":
                log.warning("v1 client %s: dropped an oversized message", self.conn_id)
                return
            frame = framing.encode_control(
                Response(
                    message["id"], error=ErrorInfo(ErrorCode.INTERNAL_ERROR, str(e))
                ).to_dict()
            )
        if self._control_pending + len(frame) > CONTROL_QUEUE_LIMIT:
            log.warning("v1 client %s is not reading; disconnecting", self.conn_id)
            self.close()
            return
        self._control_pending += len(frame)
        self._enqueue(frame, None)

    def send_error(
        self, request_id: str, code: str, message: str, data: Any = None
    ) -> None:
        self.send_message(
            Response(request_id, error=ErrorInfo(code, message, data)).to_dict()
        )

    def send_event(self, name: str, data: Any, seq: int | None = None) -> None:
        if seq is None:
            seq = self.server.engine.events.cursor
        self.send_message(Event(seq, name, data).to_dict())

    def send_output(self, stream: _Stream, offset: int, data: bytes) -> None:
        if self.closed or self.streams.get(stream.stream_id) is not stream:
            return
        limit = self.server.stream_queue_limit
        for start in range(0, len(data), FRAME_CHUNK):
            piece = data[start : start + FRAME_CHUNK]
            frame = framing.encode_stream(
                framing.StreamFrame.output(stream.stream_id, offset + start, piece)
            )
            if stream.pending + len(frame) > limit:
                self.reset_stream(stream, offset + start)
                return
            stream.pending += len(frame)
            self._enqueue(frame, stream.stream_id, offset + start)

    def reset_stream(self, stream: _Stream, offset: int) -> None:
        """Drop a stream that fell behind: unsent output is discarded and
        the client is told where to resume from."""
        kept: deque[tuple[bytes, int | None, int]] = deque()
        for item in self._queue:
            if item[1] == stream.stream_id:
                offset = min(offset, item[2])
            else:
                kept.append(item)
        self._queue = kept
        self.server.drop_stream(stream)
        log.info(
            "v1 client %s: stream %s fell behind at offset %s; reset",
            self.conn_id,
            stream.stream_id,
            offset,
        )
        self.send_event(STREAM_RESET, self.server.geometry(stream, offset))

    # ------------------------------------------------------------------
    # Incoming
    # ------------------------------------------------------------------

    def _on_control(self, obj: dict[str, Any]) -> None:
        request_id = obj.get("id") if isinstance(obj.get("id"), str) else ""
        try:
            message = parse_message(obj)
        except ProtocolError as e:
            self.send_error(request_id, e.code, e.message, e.data)
            return
        if not isinstance(message, Request):
            self.send_error(
                request_id, ErrorCode.PROTOCOL_ERROR, "clients may only send requests"
            )
            return
        if message.method == handshake.HELLO_METHOD:
            self._on_hello(message)
            return
        if self.hello is None:
            self.send_error(
                message.id,
                ErrorCode.PROTOCOL_ERROR,
                "the first request on a connection must be hello",
            )
            return
        task = asyncio.get_running_loop().create_task(self._handle(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_hello(self, request: Request) -> None:
        try:
            self.hello = handshake.parse_hello(request.params)
        except ProtocolError as e:
            self.send_error(request.id, e.code, e.message, e.data)
            return
        self.send_message(
            Response(request.id, result=self.server.hello_result()).to_dict()
        )

    async def _handle(self, request: Request) -> None:
        try:
            result = await self.server.dispatch(self, request.method, request.params)
        except asyncio.CancelledError:
            raise
        except ProtocolError as e:
            self.send_error(request.id, e.code, e.message, e.data)
            return
        except Exception as e:
            code, message = classify(e)
            if code == ErrorCode.INTERNAL_ERROR:
                log.exception("v1 handler error for method=%s", request.method)
            self.send_error(request.id, code, message)
            return
        if isinstance(result, _Reply):
            self.send_message(Response(request.id, result=result.result).to_dict())
            result.after()
        else:
            self.send_message(Response(request.id, result=result).to_dict())

    def _on_stream(self, frame: framing.StreamFrame) -> None:
        if self.hello is None:
            self.send_error(
                "",
                ErrorCode.PROTOCOL_ERROR,
                "the first request on a connection must be hello",
            )
            return
        if frame.type != framing.STREAM_INPUT:
            self.send_error(
                "", ErrorCode.PROTOCOL_ERROR, "clients may only send input frames"
            )
            return
        stream = self.streams.get(frame.stream_id)
        if stream is None:
            self.send_error(
                "",
                ErrorCode.PROTOCOL_ERROR,
                f"input for unknown stream {frame.stream_id}",
            )
            return
        self.server.claim(stream.session, self)
        try:
            stream.session.write(frame.data)
        except RuntimeError as e:
            self.send_error(
                "", ErrorCode.VALIDATION_ERROR, f"stream {frame.stream_id}: {e}"
            )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def subscribe(
        self, since: int | None, names: frozenset[str] | None
    ) -> tuple[int, list[EngineEvent], bool]:
        """Push future events (filtered by ``names``); return the cursor and
        the history after ``since`` to replay first."""
        self.unsubscribe()
        bus = self.server.engine.events

        def deliver(event: EngineEvent) -> None:
            if names is None or event.event in names:
                self.send_event(event.event, event.data, event.seq)

        replay: list[EngineEvent] = []
        truncated = False
        if since is not None:
            replay, truncated = bus.replay(since)
            if names is not None:
                replay = [e for e in replay if e.event in names]
        self._unsubscribe = bus.subscribe(deliver)
        return bus.cursor, replay, truncated

    def unsubscribe(self) -> bool:
        if self._unsubscribe is None:
            return False
        self._unsubscribe()
        self._unsubscribe = None
        return True

    def new_stream_id(self) -> int:
        return next(self._stream_ids)


class V1Server:
    """Protocol v1 over a Unix socket; see the module docstring."""

    def __init__(
        self,
        engine: Engine,
        path: str | os.PathLike[str],
        *,
        engine_id: str | None = None,
        stream_queue_limit: int = STREAM_QUEUE_LIMIT,
    ) -> None:
        self.engine = engine
        self.socket_path = str(path)
        self.info_path = Path(self.socket_path).with_name(INFO_NAME)
        self.engine_id = engine_id or uuid.uuid4().hex
        self.stream_queue_limit = stream_queue_limit
        self._server: asyncio.AbstractServer | None = None
        self._inode: int | None = None
        self._connections: set[_Connection] = set()
        self._conn_ids = itertools.count(1)
        self._sessions: dict[str, _SessionState] = {}
        # The client whose resize request is being applied, to tell it apart
        # from the in-process TUI resizing the same session.
        self._resizing: _Connection | None = None

        legacy = EngineMethods(engine).handlers
        self._handlers: dict[str, Handler] = {
            name: self._legacy(handler) for name, handler in legacy.items()
        }
        self._handlers.update(
            {
                "list_worktrees": self.handle_list_worktrees,
                "spawn_agent": self._spawn_agent(legacy["spawn_agent"]),
                "list_agents": self._list_agents(legacy["list_agents"]),
                "subscribe": self.handle_subscribe,
                "unsubscribe": self.handle_unsubscribe,
                "attach_stream": self.handle_attach_stream,
                "detach_stream": self.handle_detach_stream,
                "resize": self.handle_resize,
                "claim_size": self.handle_claim_size,
                "open_shell": self.handle_open_shell,
                "refresh_worktrees": self.handle_refresh_worktrees,
                "refresh_git_status": self.handle_refresh_git_status,
                "get_diff": self.handle_get_diff,
                "get_pr_info": self.handle_get_pr_info,
                "set_interest": self.handle_set_interest,
                "get_config": self.handle_get_config,
                "get_sidebar_state": self.handle_get_sidebar_state,
                "attach_ui": self.handle_attach_ui,
                "detach_ui": self.handle_detach_ui,
                "publish_view": self.handle_publish_view,
                "shutdown": self.handle_shutdown,
                "ui.get_view": self.handle_ui_get_view,
                "ui.select_worktree": self.handle_ui_select_worktree,
                "ui.select_orchestrator": self.handle_ui_select_orchestrator,
                "ui.focus_agent": self.handle_ui_focus_agent,
                "ui.open_shell": self.handle_ui_open_shell,
                "ui.show_diff": self.handle_ui_show_diff,
                "ui.open_modal": self.handle_ui_open_modal,
                "ui.blank": self.handle_ui_blank,
            }
        )

    @property
    def methods(self) -> list[str]:
        return sorted(self._handlers)

    @property
    def connections(self) -> int:
        return len(self._connections)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Listen, unless another live engine already serves the socket.

        Returns False (and touches nothing) in that case. A dead socket
        file is replaced. Raises OSError when the socket cannot be created.
        """
        path = Path(self.socket_path)
        private_dir(path.parent)
        if path.exists() or path.is_symlink():
            if await probe(self.socket_path):
                return False
            path.unlink(missing_ok=True)
        try:
            self._server = await asyncio.start_unix_server(
                self._on_connect, path=self.socket_path
            )
        except OSError as e:
            if e.errno == errno.EADDRINUSE:  # another engine won the race
                return False
            raise
        os.chmod(self.socket_path, 0o600)
        self._inode = os.stat(self.socket_path).st_ino
        _write_atomic(
            self.info_path,
            {
                "pid": os.getpid(),
                "version": _engine_version(),
                "protocol": handshake.PROTOCOL_VERSION,
                "socket": self.socket_path,
                "engine_id": self.engine_id,
                "repo_root": self.engine.repo_root,
                # false: the engine lives inside a TUI and stops with it.
                "detached": self.engine.shutdown_handler is not None,
                "started_at": time.time(),
            },
        )
        log.info("v1 server listening on %s", self.socket_path)
        return True

    async def stop(self) -> None:
        """Close every connection (after a short flush), then remove the
        socket and ``engine.json`` if they are still ours."""
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()
        connections = list(self._connections)
        await asyncio.gather(*(conn.flush() for conn in connections))
        for conn in connections:
            conn.close()
        await server.wait_closed()
        for state in list(self._sessions.values()):
            state.remove_listener()
        self._sessions.clear()
        try:
            if os.stat(self.socket_path).st_ino == self._inode:
                os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        info = read_info(self.info_path)
        if info is not None and info.get("pid") == os.getpid():
            self.info_path.unlink(missing_ok=True)
        # Kept while the engine holds its lock (engine.lock is in it); the
        # engine removes it after releasing the lock.
        remove_empty_dirs(self.info_path.parent)

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        uid = peer_uid(writer.get_extra_info("socket"))
        if uid != os.getuid():
            log.warning("v1 server: refused a connection from uid %s", uid)
            writer.close()
            return
        conn = _Connection(self, reader, writer, next(self._conn_ids))
        self._connections.add(conn)
        await conn.run()

    def forget(self, conn: _Connection) -> None:
        self._connections.discard(conn)
        self.engine.set_interest((), conn.interest_client)
        if conn.detach_ui is not None:
            conn.detach_ui()
            conn.detach_ui = None
        for state in list(self._sessions.values()):
            if state.owner is conn:
                state.owner = None
                self._prune(state)

    def hello_result(self) -> dict[str, Any]:
        methods = self.methods
        if not self.engine.ui_attached:
            methods = [m for m in methods if m not in UI_COMMAND_METHODS]
        return handshake.HelloReply(
            protocol=handshake.PROTOCOL_VERSION,
            engine_version=_engine_version(),
            repo_root=self.engine.repo_root,
            engine_id=self.engine_id,
            capabilities=(*methods, *FEATURES),
        ).to_result()

    async def dispatch(self, conn: _Connection, method: str, params: dict) -> Any:
        handler = self._handlers.get(method)
        if handler is None:
            raise ProtocolError(ErrorCode.UNKNOWN_METHOD, f"Unknown method: {method}")
        if method in UI_COMMAND_METHODS and not self.engine.ui_attached:
            raise ProtocolError(
                ErrorCode.NO_HOSTED_UI,
                "No hosted UI is attached to this engine: start the TUI with "
                f"{HOST_ENV} set to carry out {method}",
            )
        return await handler(conn, params)

    # ------------------------------------------------------------------
    # Legacy methods
    # ------------------------------------------------------------------

    @staticmethod
    def _legacy(handler: Callable[[dict], Awaitable[Any]]) -> Handler:
        async def call(conn: _Connection, params: dict) -> Any:
            return await handler(params)

        return call

    def _spawn_agent(self, handler: Callable[[dict], Awaitable[Any]]) -> Handler:
        async def call(conn: _Connection, params: dict) -> Any:
            result = await handler(params)
            info = self.engine.get_agent(result["worktree_path"], result["agent_id"])
            if info is not None:
                result["session_id"] = info.session_id
            return result

        return call

    def _list_agents(self, handler: Callable[[dict], Awaitable[Any]]) -> Handler:
        async def call(conn: _Connection, params: dict) -> Any:
            result = await handler(params)
            key = result["worktree_path"]
            # The v1 list has every agent the engine runs, "starting" ones too.
            result["agents"] = self.engine.agents_payload(key, v1=True)
            for agent in result["agents"]:
                info = self.engine.get_agent(key, agent["agent_id"])
                session = self.engine.get_session(key, agent["agent_id"])
                if info is None or session is None:
                    continue
                state = self._sessions.get(info.session_id)
                agent["session_id"] = info.session_id
                agent["kind"] = info.kind
                agent["size"] = {"rows": session.rows, "cols": session.cols}
                agent["attached_clients"] = len(
                    {s.conn for s in state.streams} if state else ()
                )
            return result

        return call

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def handle_subscribe(self, conn: _Connection, params: dict) -> Any:
        since = params.get("since")
        if since is not None and not (_is_int(since) and since >= 0):
            raise ValueError("since must be a non-negative integer cursor")
        names = params.get("events")
        if names is not None and not (
            isinstance(names, list) and all(isinstance(n, str) for n in names)
        ):
            raise ValueError("events must be a list of event names")
        cursor, replay, truncated = conn.subscribe(
            since, frozenset(names) if names is not None else None
        )

        def send_replay() -> None:
            for event in replay:
                conn.send_event(event.event, event.data, event.seq)

        return _Reply(
            {"cursor": cursor, "replayed": len(replay), "truncated": truncated},
            send_replay,
        )

    async def handle_unsubscribe(self, conn: _Connection, params: dict) -> Any:
        return {"unsubscribed": conn.unsubscribe()}

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------

    def _session(self, params: dict) -> TerminalSession:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        session = self.engine.find_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def _state(self, session: TerminalSession) -> _SessionState:
        state = self._sessions.get(session.session_id)
        if state is None or state.session is not session:
            if state is not None:
                state.remove_listener()
            state = _SessionState(
                session,
                session.on_resize(
                    lambda rows, cols, offset: self._on_resized(
                        session, rows, cols, offset
                    )
                ),
            )
            self._sessions[session.session_id] = state
        return state

    def _prune(self, state: _SessionState) -> None:
        if state.streams or state.owner is not None:
            return
        state.remove_listener()
        if self._sessions.get(state.session.session_id) is state:
            del self._sessions[state.session.session_id]

    @staticmethod
    def geometry(stream: _Stream, offset: int) -> dict[str, Any]:
        return {
            "stream_id": stream.stream_id,
            "session_id": stream.session_id,
            "rows": stream.session.rows,
            "cols": stream.session.cols,
            "offset": offset,
        }

    async def handle_attach_stream(self, conn: _Connection, params: dict) -> Any:
        session = self._session(params)
        if not session.running:
            raise ValueError(f"Session {session.session_id} is not running")
        from_offset = params.get("from_offset")
        if from_offset is not None and not (_is_int(from_offset) and from_offset >= 0):
            raise ValueError("from_offset must be a non-negative integer")

        # From here to the reply nothing awaits: the snapshot (or tail) and
        # the first live chunk are cut at the same offset.
        ring = session.ring
        stream = _Stream(conn.new_stream_id(), session.session_id, session, conn)
        result: dict[str, Any] = {
            "stream_id": stream.stream_id,
            "session_id": session.session_id,
            "size": {"rows": session.rows, "cols": session.cols},
        }
        if (
            from_offset is not None
            and ring.start_offset <= from_offset <= ring.end_offset
            and ring.end_offset - from_offset <= TAIL_LIMIT
        ):
            offset = from_offset
            result["mode"] = "tail"
        else:
            # The screen as parsed so far, exact at its offset; the backlog the
            # engine has not parsed yet follows as output frames, for the
            # client to parse (nothing is parsed here, so an attach right
            # after a burst costs no more than any other). A backlog further
            # behind than a raw tail may reach is cut first, as the feeder
            # cuts one it falls far behind on: the snapshot's scrollback would
            # not have reached back that far anyway.
            session.bound_pending(min(TAIL_LIMIT, self.stream_queue_limit // 2))
            offset, snapshot = session.snapshot(flush=False)
            result["mode"] = "snapshot"
            result["snapshot"] = snapshot
        result["offset"] = offset
        backlog = ring.read(offset)

        state = self._state(session)
        state.streams.add(stream)
        conn.streams[stream.stream_id] = stream

        def closed() -> None:
            if conn.streams.get(stream.stream_id) is stream:
                self.drop_stream(stream)
                conn.send_event(
                    STREAM_CLOSED,
                    {
                        "stream_id": stream.stream_id,
                        "session_id": stream.session_id,
                        "offset": session.ring.end_offset,
                        "exit_code": session.exit_code,
                    },
                )

        stream.detach = session.attach(
            lambda off, data: conn.send_output(stream, off, data),
            watching=lambda: False,
            on_close=closed,
        )

        def send_backlog() -> None:
            if backlog:
                conn.send_output(stream, offset, backlog)

        return _Reply(result, send_backlog)

    def drop_stream(self, stream: _Stream) -> None:
        stream.detach()
        if stream.conn.streams.get(stream.stream_id) is stream:
            del stream.conn.streams[stream.stream_id]
        state = self._sessions.get(stream.session_id)
        if state is not None:
            state.streams.discard(stream)
            self._prune(state)

    async def handle_detach_stream(self, conn: _Connection, params: dict) -> Any:
        stream_id = params.get("stream_id")
        stream = conn.streams.get(stream_id) if _is_int(stream_id) else None
        if stream is None:
            raise ValueError(f"Unknown stream: {stream_id!r}")
        self.drop_stream(stream)
        return {"stream_id": stream_id, "detached": True}

    # ------------------------------------------------------------------
    # Size
    # ------------------------------------------------------------------

    def claim(self, session: TerminalSession, conn: _Connection) -> None:
        state = self._state(session)
        if state.owner is not conn:
            state.owner = conn
            log.debug(
                "v1 client %s owns the size of %s", conn.conn_id, session.session_id
            )

    def _apply_size(
        self, session: TerminalSession, conn: _Connection, params: dict
    ) -> None:
        rows, cols = params.get("rows"), params.get("cols")
        if not (_is_int(rows) and _is_int(cols) and rows > 0 and cols > 0):
            raise ValueError("rows and cols must be positive integers")
        self._resizing = conn
        try:
            session.resize(rows, cols)
        finally:
            self._resizing = None

    def _on_resized(
        self, session: TerminalSession, rows: int, cols: int, offset: int
    ) -> None:
        state = self._sessions.get(session.session_id)
        if state is None:
            return
        if self._resizing is None:
            # The in-process TUI resized it: it is the latest to claim.
            state.owner = None
        for stream in list(state.streams):
            stream.conn.send_event(STREAM_RESIZED, self.geometry(stream, offset))
        self._prune(state)

    def _size_result(
        self, session: TerminalSession, conn: _Connection
    ) -> dict[str, Any]:
        state = self._sessions.get(session.session_id)
        return {
            "session_id": session.session_id,
            "rows": session.rows,
            "cols": session.cols,
            "owner": state is not None and state.owner is conn,
        }

    async def handle_resize(self, conn: _Connection, params: dict) -> Any:
        session = self._session(params)
        # The backlog is parsed at the size it was produced for, in slices,
        # before anything is decided: nothing awaits between the ownership
        # check and the resize.
        await session.catch_up()
        state = self._sessions.get(session.session_id)
        if state is None or state.owner is not conn:
            raise ProtocolError(
                ErrorCode.NOT_SIZE_OWNER,
                f"Another client owns the size of {session.session_id}; claim_size first",
            )
        self._apply_size(session, conn, params)
        return self._size_result(session, conn)

    async def handle_claim_size(self, conn: _Connection, params: dict) -> Any:
        session = self._session(params)
        if ("rows" in params) != ("cols" in params):
            raise ValueError("rows and cols go together")
        if "rows" in params:
            await session.catch_up()  # as for resize
        self.claim(session, conn)
        if "rows" in params:
            self._apply_size(session, conn, params)
        return self._size_result(session, conn)

    # ------------------------------------------------------------------
    # Worktrees, shells, config
    # ------------------------------------------------------------------

    def _worktree(self, params: dict) -> str:
        path = params.get("worktree_path")
        if not isinstance(path, str) or not path:
            raise ValueError("worktree_path is required")
        if self.engine.worktree(path) is None:
            raise ValueError(f"Unknown worktree: {path}")
        return path

    async def handle_open_shell(self, conn: _Connection, params: dict) -> Any:
        path = self._worktree(params)
        shell = self.engine.open_shell(path, env=check_env(params.get("env")))
        return {
            "worktree_path": path,
            "session_id": shell.session_id,
            "size": {"rows": shell.rows, "cols": shell.cols},
        }

    async def handle_list_worktrees(self, conn: _Connection, params: dict) -> Any:
        return self.engine.worktrees_payload(v1=True)

    async def handle_refresh_worktrees(self, conn: _Connection, params: dict) -> Any:
        if not self.engine.refresh_worktrees():
            raise ProtocolError(
                ErrorCode.WORKTREE_ERROR,
                "Could not list the worktrees (see the engine.notice event)",
            )
        return self.engine.worktrees_payload(v1=True)

    async def handle_refresh_git_status(self, conn: _Connection, params: dict) -> Any:
        paths = params.get("paths")
        if paths is not None and not (
            isinstance(paths, list) and all(isinstance(p, str) for p in paths)
        ):
            raise ValueError("paths must be a list of worktree paths")
        await self.engine.refresh_git_status(paths)
        statuses = self.engine.git_statuses
        wanted = paths if paths is not None else list(statuses)
        return {"statuses": {p: asdict(statuses[p]) for p in wanted if p in statuses}}

    async def handle_get_diff(self, conn: _Connection, params: dict) -> Any:
        path = self._worktree(params)
        return {"worktree_path": path, "diff": await self.engine.get_diff(path)}

    async def handle_get_pr_info(self, conn: _Connection, params: dict) -> Any:
        path = self._worktree(params)
        refresh = params.get("refresh", False)
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        if refresh or not self.engine.has_pr_info(path):
            await self.engine.refresh_pr_info([path])
        info = self.engine.get_pr_info(path)
        return {
            "worktree_path": path,
            "pr_info": asdict(info) if info is not None else None,
            "fetched": self.engine.has_pr_info(path),
        }

    async def handle_set_interest(self, conn: _Connection, params: dict) -> Any:
        paths = params.get("worktree_paths")
        if not (isinstance(paths, list) and all(isinstance(p, str) for p in paths)):
            raise ValueError("worktree_paths must be a list of worktree paths")
        self.engine.set_interest(paths, conn.interest_client)
        return {
            "worktree_paths": sorted(set(paths)),
            "interest": sorted(self.engine.worktree_service.interest),
        }

    async def handle_get_config(self, conn: _Connection, params: dict) -> Any:
        return asdict(self.engine.get_config())

    async def handle_get_sidebar_state(self, conn: _Connection, params: dict) -> Any:
        engine = self.engine
        pr_info = {}
        for wt in engine.worktrees:
            if engine.has_pr_info(wt.path):
                info = engine.get_pr_info(wt.path)
                pr_info[wt.path] = asdict(info) if info is not None else None
        return {
            "worktrees": engine.worktrees_payload(v1=True),
            "orchestrator": {
                "agents": engine.agents_payload(ORCHESTRATOR_KEY, v1=True)
            },
            "pr_info": pr_info,
            "git_statuses": {
                path: asdict(status) for path, status in engine.git_statuses.items()
            },
            "view": engine.ui_view,
            "cursor": engine.events.cursor,
        }

    # ------------------------------------------------------------------
    # Hosted UI
    # ------------------------------------------------------------------

    async def handle_attach_ui(self, conn: _Connection, params: dict) -> Any:
        """This client carries out ``ui.command`` events (a hosted TUI on a
        remote engine) until it detaches or disconnects."""
        if conn.detach_ui is None:
            conn.detach_ui = self.engine.attach_ui()
        return {"attached": True}

    async def handle_detach_ui(self, conn: _Connection, params: dict) -> Any:
        if conn.detach_ui is not None:
            conn.detach_ui()
            conn.detach_ui = None
        return {"attached": False}

    async def handle_publish_view(self, conn: _Connection, params: dict) -> Any:
        """A UI announces what it shows (``ui.view_changed``)."""
        view = params.get("view")
        if not (isinstance(view, dict) and all(k in view for k in _VIEW_FIELDS)):
            raise ValueError(f"view must be an object with {', '.join(_VIEW_FIELDS)}")
        return {"published": self.engine.publish_view(view)}

    async def handle_shutdown(self, conn: _Connection, params: dict) -> Any:
        """Stop a detached engine (``lazyagent engine stop``), after the
        reply. ``stop_agents: false`` only stops one that runs no agent:
        an agent cannot outlive its engine (its PTY closes)."""
        stop_agents = params.get("stop_agents", True)
        if not isinstance(stop_agents, bool):
            raise ValueError("stop_agents must be a boolean")
        handler = self.engine.shutdown_handler
        if handler is None:
            raise ValueError(
                f"This engine runs inside a lazyagent TUI (pid {os.getpid()}); "
                "quit that TUI to stop it"
            )
        agents = self.engine.agent_count()
        if agents and not stop_agents:
            raise ValueError(
                f"{agents} agent{'s' if agents != 1 else ''} still running; "
                "stopping the engine would stop them too"
            )
        return _Reply({"stopping": True, "agents": agents}, handler)

    async def handle_ui_get_view(self, conn: _Connection, params: dict) -> Any:
        return {"view": self.engine.ui_view}

    def _ui_target(self, params: dict, *, orchestrator: bool = True) -> str | None:
        """The worktree a ui command is about: ``worktree_path``, the
        orchestrator (None) when ``orchestrator`` is true, else whatever the
        UI shows."""
        if (
            params.get("orchestrator") is not None
            and type(params["orchestrator"]) is not bool
        ):
            raise ValueError("orchestrator must be a boolean")
        if params.get("orchestrator"):
            if "worktree_path" in params:
                raise ValueError("pass worktree_path or orchestrator, not both")
            if not orchestrator:
                raise ValueError("The orchestrator has no shell or diff")
            return None
        if "worktree_path" in params:
            return self._worktree(params)
        view = self.engine.ui_view or {}
        if view.get("orchestrator"):
            if not orchestrator:
                raise ValueError("The orchestrator has no shell or diff")
            return None
        path = view.get("worktree_path")
        if not path:
            raise ValueError("The UI shows no worktree; pass worktree_path")
        return path

    def _ui_command(self, command: str, **args: Any) -> dict[str, Any]:
        event = self.engine.ui_command(command, **args)
        return {"seq": event.seq, **event.data}

    async def handle_ui_select_worktree(self, conn: _Connection, params: dict) -> Any:
        return self._ui_command("select_worktree", worktree_path=self._worktree(params))

    async def handle_ui_select_orchestrator(
        self, conn: _Connection, params: dict
    ) -> Any:
        return self._ui_command("select_orchestrator", worktree_path=None)

    async def handle_ui_focus_agent(self, conn: _Connection, params: dict) -> Any:
        path = self._ui_target(params)
        key = ORCHESTRATOR_KEY if path is None else path
        agent_ids = self.engine.agent_ids(key)
        if not agent_ids:
            where = "the orchestrator" if path is None else path
            raise ValueError(f"No running agent in {where}")
        agent_id = params.get("agent_id")
        if agent_id is not None:
            if not isinstance(agent_id, str):
                raise ValueError("agent_id must be a string")
            if agent_id not in agent_ids:
                raise ValueError(f"No agent {agent_id!r} there. Available: {agent_ids}")
        return self._ui_command("focus_agent", worktree_path=path, agent_id=agent_id)

    async def handle_ui_open_shell(self, conn: _Connection, params: dict) -> Any:
        path = self._ui_target(params, orchestrator=False)
        return self._ui_command("open_shell", worktree_path=path)

    async def handle_ui_show_diff(self, conn: _Connection, params: dict) -> Any:
        path = self._ui_target(params, orchestrator=False)
        return self._ui_command("show_diff", worktree_path=path)

    async def handle_ui_blank(self, conn: _Connection, params: dict) -> Any:
        return self._ui_command("blank", worktree_path=None)

    async def handle_ui_open_modal(self, conn: _Connection, params: dict) -> Any:
        modal = params.get("modal")
        if modal not in UI_MODALS:
            raise ValueError(f"modal must be one of {', '.join(UI_MODALS)}")
        view = self.engine.ui_view or {}
        if view.get("focus") == "modal":
            raise ValueError(f"The {view.get('modal')} modal is already open")
        if modal == "create":
            path = None
        else:
            path = self._ui_target(params, orchestrator=modal == "spawn")
        if modal == "remove":
            wt = self.engine.worktree(path)
            if wt is not None and wt.is_main:
                raise ValueError("Cannot remove the main worktree")
            if self.engine.has_active_agents(path):
                raise ValueError("An agent is running in this worktree; stop it first")
        return self._ui_command("open_modal", worktree_path=path, modal=modal)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _engine_version() -> str:
    return __version__
