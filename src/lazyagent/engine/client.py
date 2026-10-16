"""Reference asyncio client for protocol v1 (standard library only).

::

    client = await Client.connect(path, client="cli")
    worktrees = await client.request("list_worktrees")
    await client.request("subscribe", since=0)
    stream = await client.attach(session_id, on_output=lambda off, data: ...)
    client.send_input(stream.stream_id, b"echo hi\\r")
    async for event in client.events():
        ...
    await client.close()

:func:`discover` finds a repo's socket from its root. :meth:`Client.connect`
performs the ``hello`` handshake (unless asked not to). Responses are
matched to requests by id, so requests may overlap.
Events are queued for :meth:`Client.events` in arrival order, unknown ones
included: the caller decides what to ignore. Output frames go to the
``on_output(offset, data)`` callback of their stream; ``stream.resized``,
``stream.reset`` and ``stream.closed`` update the :class:`Stream` and reach
its ``on_event`` callback as well as the event queue.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazyagent.engine.runtime import INFO_NAME, read_info, runtime_dir
from lazyagent.protocol import framing, handshake
from lazyagent.protocol.envelope import (
    ErrorCode,
    ErrorInfo,
    Event,
    ProtocolError,
    Request,
    Response,
    parse_message,
)

OutputCallback = Callable[[int, bytes], None]
StreamEventCallback = Callable[[Event], None]

_READ_SIZE = 64 * 1024
_STREAM_EVENTS = ("stream.resized", "stream.reset", "stream.closed")


def discover(
    repo_root: str | os.PathLike[str], environ: Mapping[str, str] = os.environ
) -> Path | None:
    """The v1 socket of the engine serving ``repo_root``, or None.

    ``repo_root`` is the repo's top level as ``git rev-parse --show-toplevel``
    prints it: the runtime dir is named after a hash of that exact string.
    The answer comes from the ``engine.json`` the engine writes there; None
    when there is none, it names another repo, or its socket is gone.
    """
    root = os.fspath(repo_root)
    info = read_info(runtime_dir(root, environ) / INFO_NAME)
    if info is None or info.get("repo_root") != root:
        return None
    socket = info.get("socket")
    if not isinstance(socket, str) or not Path(socket).exists():
        return None
    return Path(socket)


class RequestError(Exception):
    """An error response; ``code`` is an :class:`ErrorCode` (open set)."""

    def __init__(self, error: ErrorInfo) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.code = error.code
        self.message = error.message
        self.data = error.data


class ConnectionClosed(ConnectionError):
    """The engine closed the connection."""


@dataclass
class Stream:
    """An attached PTY stream, as ``attach_stream`` answered it."""

    stream_id: int
    session_id: str
    rows: int
    cols: int
    mode: str  # "snapshot" or "tail"
    offset: int  # where the output frames start
    snapshot: str | None = None
    on_output: OutputCallback | None = None
    on_event: StreamEventCallback | None = None
    # Offset after the last output byte received.
    received: int = 0
    # Set once the stream is gone (reset, closed or detached).
    ended: str | None = None
    gaps: list[tuple[int, int]] = field(default_factory=list)


class Client:
    """One connection to an engine's v1 socket."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[Response]] = {}
        self._attaching: dict[
            str, tuple[OutputCallback | None, StreamEventCallback | None]
        ] = {}
        self._streams: dict[int, Stream] = {}
        self._events: asyncio.Queue[Event | None] = asyncio.Queue()
        # Error responses with no request id (bad frames, input problems).
        self.errors: list[ErrorInfo] = []
        self.hello: handshake.HelloReply | None = None
        self._closed: BaseException | None = None
        self._task = asyncio.get_running_loop().create_task(self._read_loop())

    @classmethod
    async def connect(
        cls,
        path: str | os.PathLike[str],
        *,
        client: str = "cli",
        client_version: str = "",
        hello: bool = True,
        timeout: float = 5.0,
    ) -> Client:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout
        )
        self = cls(reader, writer)
        if hello:
            try:
                await self.say_hello(client=client, client_version=client_version)
            except BaseException:
                await self.close()
                raise
        return self

    async def say_hello(
        self,
        *,
        client: str = "cli",
        client_version: str = "",
        protocol: int = handshake.PROTOCOL_VERSION,
        token: str | None = None,
    ) -> handshake.HelloReply:
        params = handshake.Hello(protocol, client, client_version, token).to_params()
        result = await self.request("hello", **params)
        self.hello = handshake.parse_hello_reply(result)
        return self.hello

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Response:
        """Send a request and return its response, error or not."""
        request_id = str(next(self._ids))
        future = self._send_request(request_id, method, params or {})
        return await future

    async def request(self, method: str, **params: Any) -> Any:
        """Send a request; return its result or raise :class:`RequestError`."""
        response = await self.call(method, params)
        if response.error is not None:
            raise RequestError(response.error)
        return response.result

    def send(self, method: str, **params: Any) -> asyncio.Future[Response]:
        """Send a request now and return the future of its response. Requests
        sent one after the other are on the wire in that order, whoever
        awaits them."""
        return self._send_request(str(next(self._ids)), method, params)

    def _send_request(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> asyncio.Future[Response]:
        if self._closed is not None:
            raise ConnectionClosed("connection closed") from self._closed
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self.send_raw(
            framing.encode_control(Request(request_id, method, params).to_dict())
        )
        return future

    def send_raw(self, data: bytes) -> None:
        """Write bytes as they are (tests use it for malformed frames)."""
        self._writer.write(data)

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------

    async def attach(
        self,
        session_id: str,
        on_output: OutputCallback | None = None,
        *,
        from_offset: int | None = None,
        on_event: StreamEventCallback | None = None,
    ) -> Stream:
        """Attach to a session's PTY output.

        The stream is registered before any of its output frames is read,
        so ``on_output`` sees every byte from :attr:`Stream.offset` on.
        """
        params: dict[str, Any] = {"session_id": session_id}
        if from_offset is not None:
            params["from_offset"] = from_offset
        request_id = str(next(self._ids))
        self._attaching[request_id] = (on_output, on_event)
        try:
            response = await self._send_request(request_id, "attach_stream", params)
        finally:
            self._attaching.pop(request_id, None)
        if response.error is not None:
            raise RequestError(response.error)
        return self._streams[response.result["stream_id"]]

    async def detach(self, stream_id: int) -> None:
        await self.request("detach_stream", stream_id=stream_id)
        stream = self._streams.pop(stream_id, None)
        if stream is not None and stream.ended is None:
            stream.ended = "detached"

    def stream(self, stream_id: int) -> Stream | None:
        return self._streams.get(stream_id)

    def send_input(self, stream_id: int, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode()
        self.send_raw(framing.encode_stream(framing.StreamFrame.input(stream_id, data)))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def events(self) -> AsyncIterator[Event]:
        """Every pushed event, in order, until the connection closes."""
        while True:
            event = await self._events.get()
            if event is None:
                self._events.put_nowait(None)
                return
            yield event

    async def next_event(
        self, predicate: Callable[[Event], bool] = lambda e: True, timeout: float = 5.0
    ) -> Event:
        """The next queued event that matches (earlier ones are dropped)."""

        async def find() -> Event:
            async for event in self.events():
                if predicate(event):
                    return event
            raise ConnectionClosed("connection closed")

        return await asyncio.wait_for(find(), timeout)

    def pending_events(self) -> list[Event]:
        """Drain the events queued so far without waiting."""
        out = []
        while not self._events.empty():
            event = self._events.get_nowait()
            if event is None:
                self._events.put_nowait(None)
                break
            out.append(event)
        return out

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed is not None

    async def wait_closed(self) -> None:
        await asyncio.shield(self._task)

    async def _read_loop(self) -> None:
        decoder = framing.FrameDecoder()
        error: BaseException = ConnectionClosed("connection closed by the engine")
        try:
            while True:
                data = await self._reader.read(_READ_SIZE)
                if not data:
                    break
                decoder.feed(data)
                for frame in decoder:
                    if isinstance(frame, framing.ControlFrame):
                        self._on_control(frame.message)
                    else:
                        self._on_stream(frame)
        except asyncio.CancelledError:
            error = ConnectionClosed("connection closed")
        except (OSError, framing.FrameError, ProtocolError) as e:
            error = e
        finally:
            self._closed = error
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionClosed(str(error)))
            self._pending.clear()
            self._events.put_nowait(None)

    def _on_control(self, obj: dict[str, Any]) -> None:
        message = parse_message(obj)
        if isinstance(message, Response):
            self._on_response(message)
        elif isinstance(message, Event):
            self._on_event(message)

    def _on_response(self, response: Response) -> None:
        future = self._pending.pop(response.id, None)
        if future is None:
            if response.error is not None:
                self.errors.append(response.error)
            return
        callbacks = self._attaching.get(response.id)
        if callbacks is not None and response.error is None:
            # Registered now, before the frames that follow are read.
            result = response.result
            stream = Stream(
                stream_id=result["stream_id"],
                session_id=result["session_id"],
                rows=result["size"]["rows"],
                cols=result["size"]["cols"],
                mode=result["mode"],
                offset=result["offset"],
                snapshot=result.get("snapshot"),
                on_output=callbacks[0],
                on_event=callbacks[1],
                received=result["offset"],
            )
            self._streams[stream.stream_id] = stream
        if not future.done():
            future.set_result(response)

    def _on_event(self, event: Event) -> None:
        if event.event in _STREAM_EVENTS and isinstance(event.data, dict):
            stream = self._streams.get(event.data.get("stream_id"))
            if stream is not None:
                if event.event == "stream.resized":
                    stream.rows = event.data["rows"]
                    stream.cols = event.data["cols"]
                else:
                    stream.ended = event.event.removeprefix("stream.")
                    del self._streams[stream.stream_id]
                if stream.on_event is not None:
                    stream.on_event(event)
        self._events.put_nowait(event)

    def _on_stream(self, frame: framing.StreamFrame) -> None:
        stream = self._streams.get(frame.stream_id)
        if stream is None or frame.offset is None:
            return
        if frame.offset != stream.received:
            stream.gaps.append((stream.received, frame.offset))
        stream.received = frame.offset + len(frame.data)
        if stream.on_output is not None:
            stream.on_output(frame.offset, frame.data)

    async def close(self) -> None:
        if not self._writer.is_closing():
            self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


__all__ = [
    "Client",
    "ConnectionClosed",
    "ErrorCode",
    "RequestError",
    "Stream",
    "discover",
]
