"""The server side of WebSocket (RFC 6455), standard library only.

Enough for the host page: the opening handshake, text and binary messages
(fragmented or not), ping/pong and the closing handshake. No extensions and
no subprotocols. Frames from the client must be masked; frames we send are
not. A message over ``max_message`` bytes closes the connection with 1009.

::

    request = await read_request(reader)
    problem = upgrade_problem(request)       # None when it is a valid upgrade
    writer.write(handshake_response(request.header("sec-websocket-key")))
    ws = WebSocket(reader, writer)
    message = await ws.recv()                # str (text) or bytes (binary)
    ws.send_text("…"); ws.send_binary(b"…")
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass, field

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
_DATA_OPCODES = (OP_CONTINUATION, OP_TEXT, OP_BINARY)
_CONTROL_OPCODES = (OP_CLOSE, OP_PING, OP_PONG)

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_NO_STATUS = 1005
CLOSE_ABNORMAL = 1006
CLOSE_INVALID_DATA = 1007
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_INTERNAL = 1011
CLOSE_TRY_AGAIN = 1013

MAX_REQUEST_HEAD = 16 * 1024
DEFAULT_MAX_MESSAGE = 1024 * 1024


class HttpError(Exception):
    """A request we cannot parse; ``status`` is the HTTP answer to give."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class HttpRequest:
    method: str
    target: str
    version: str
    # Lower-cased names; repeated headers joined with ", ".
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]


async def read_request(reader: asyncio.StreamReader) -> HttpRequest:
    """Read and parse one HTTP/1.1 request head (no body)."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise HttpError(431, "request head too large") from None
    except asyncio.IncompleteReadError:
        raise HttpError(400, "incomplete request") from None
    if len(head) > MAX_REQUEST_HEAD:
        raise HttpError(431, "request head too large")
    lines = head[:-4].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise HttpError(400, "bad request line")
    request = HttpRequest(parts[0], parts[1], parts[2])
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip() or line[0] in " \t":
            raise HttpError(400, "bad header line")
        key = name.lower()
        value = value.strip()
        if key in request.headers:
            request.headers[key] += ", " + value
        else:
            request.headers[key] = value
    return request


def _tokens(value: str | None) -> set[str]:
    return {t.strip().lower() for t in (value or "").split(",") if t.strip()}


def accept_key(key: str) -> str:
    """``Sec-WebSocket-Accept`` for a client's ``Sec-WebSocket-Key``."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def is_upgrade(request: HttpRequest) -> bool:
    return "websocket" in _tokens(request.header("upgrade"))


def upgrade_problem(request: HttpRequest) -> str | None:
    """Why ``request`` is not a valid WebSocket opening handshake, or None."""
    if request.method != "GET":
        return "method must be GET"
    if request.version != "HTTP/1.1":
        return "HTTP/1.1 required"
    if not is_upgrade(request):
        return "Upgrade: websocket required"
    if "upgrade" not in _tokens(request.header("connection")):
        return "Connection: Upgrade required"
    if request.header("sec-websocket-version") != "13":
        return "Sec-WebSocket-Version: 13 required"
    key = request.header("sec-websocket-key") or ""
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            return "bad Sec-WebSocket-Key"
    except (binascii.Error, ValueError):
        return "bad Sec-WebSocket-Key"
    return None


def handshake_response(key: str, headers: dict[str, str] | None = None) -> bytes:
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(key)}",
        *(f"{k}: {v}" for k, v in (headers or {}).items()),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _apply_mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return data
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def encode_frame(
    opcode: int, payload: bytes, *, fin: bool = True, mask: bytes | None = None
) -> bytes:
    """One frame. ``mask`` (4 bytes) is for clients, i.e. tests."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    mask_bit = 0x80 if mask is not None else 0
    n = len(payload)
    if n < 126:
        head.append(mask_bit | n)
    elif n < 1 << 16:
        head.append(mask_bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mask_bit | 127)
        head += struct.pack("!Q", n)
    if mask is not None:
        return bytes(head) + mask + _apply_mask(payload, mask)
    return bytes(head) + payload


def close_payload(code: int, reason: str = "") -> bytes:
    return struct.pack("!H", code) + reason.encode()[:123]


def _valid_close_code(code: int) -> bool:
    if 1000 <= code <= 1014:
        return code not in (1004, CLOSE_NO_STATUS, CLOSE_ABNORMAL)
    return 3000 <= code <= 4999


class ConnectionClosed(Exception):
    """The connection is closed; ``code`` is the close code (1006 when the
    TCP connection simply went away)."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"{code} {reason}".strip())
        self.code = code
        self.reason = reason


class WebSocket:
    """An accepted connection (the handshake response is already sent)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_message: int = DEFAULT_MAX_MESSAGE,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.max_message = max_message
        self.close_sent = False
        self.closed: ConnectionClosed | None = None

    # -- sending -----------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode())

    def send_binary(self, data: bytes) -> None:
        self._send(OP_BINARY, data)

    def ping(self, data: bytes = b"") -> None:
        self._send(OP_PING, data[:125])

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.close_sent or self._writer.is_closing():
            raise ConnectionClosed(
                self.closed.code if self.closed else CLOSE_ABNORMAL, "closed"
            )
        self._writer.write(encode_frame(opcode, payload))

    @property
    def buffered(self) -> int:
        """Bytes written but not yet taken by the socket."""
        return self._writer.transport.get_write_buffer_size()

    async def drain(self) -> None:
        await self._writer.drain()

    async def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """Send a close frame (once) and close the TCP connection."""
        if not self.close_sent and not self._writer.is_closing():
            self.close_sent = True
            self._writer.write(encode_frame(OP_CLOSE, close_payload(code, reason)))
        if self.closed is None:
            self.closed = ConnectionClosed(code, reason)
        if not self._writer.is_closing():
            try:
                await asyncio.wait_for(self._writer.drain(), 1.0)
            except (ConnectionError, asyncio.TimeoutError):
                pass
            self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _fail(self, code: int, reason: str) -> ConnectionClosed:
        await self.close(code, reason)
        assert self.closed is not None
        return self.closed

    # -- receiving ---------------------------------------------------------

    async def recv(self) -> str | bytes:
        """The next data message; answers pings, and raises
        :class:`ConnectionClosed` once the connection is closed."""
        if self.closed is not None:
            raise self.closed
        opcode = None
        parts: list[bytes] = []
        size = 0
        while True:
            fin, op, payload = await self._read_frame()
            if op in _CONTROL_OPCODES:
                await self._on_control(op, payload)
                continue
            if op == OP_CONTINUATION:
                if opcode is None:
                    raise await self._fail(
                        CLOSE_PROTOCOL_ERROR, "unexpected continuation"
                    )
            elif opcode is not None:
                raise await self._fail(CLOSE_PROTOCOL_ERROR, "expected continuation")
            else:
                opcode = op
            size += len(payload)
            if size > self.max_message:
                raise await self._fail(CLOSE_TOO_BIG, "message too big")
            parts.append(payload)
            if fin:
                break
        data = b"".join(parts)
        if opcode == OP_TEXT:
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                raise await self._fail(CLOSE_INVALID_DATA, "invalid UTF-8") from None
        return data

    async def _on_control(self, op: int, payload: bytes) -> None:
        if op == OP_PING:
            if not self.close_sent:
                self._writer.write(encode_frame(OP_PONG, payload))
        elif op == OP_CLOSE:
            if len(payload) == 1:
                raise await self._fail(CLOSE_PROTOCOL_ERROR, "bad close frame")
            code, reason = CLOSE_NO_STATUS, ""
            if payload:
                (code,) = struct.unpack("!H", payload[:2])
                if not _valid_close_code(code):
                    raise await self._fail(CLOSE_PROTOCOL_ERROR, "bad close code")
                try:
                    reason = payload[2:].decode("utf-8")
                except UnicodeDecodeError:
                    raise await self._fail(
                        CLOSE_INVALID_DATA, "invalid UTF-8"
                    ) from None
            # Echo the code, as the closing handshake asks.
            await self.close(CLOSE_NORMAL if code == CLOSE_NO_STATUS else code)
            self.closed = ConnectionClosed(code, reason)
            raise self.closed

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        try:
            b0, b1 = await self._reader.readexactly(2)
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            if b0 & 0x70:
                raise await self._fail(CLOSE_PROTOCOL_ERROR, "reserved bits set")
            if op not in _DATA_OPCODES and op not in _CONTROL_OPCODES:
                raise await self._fail(CLOSE_PROTOCOL_ERROR, "unknown opcode")
            if not b1 & 0x80:
                raise await self._fail(
                    CLOSE_PROTOCOL_ERROR, "client frames must be masked"
                )
            length = b1 & 0x7F
            if op in _CONTROL_OPCODES and (not fin or length > 125):
                raise await self._fail(CLOSE_PROTOCOL_ERROR, "bad control frame")
            if length == 126:
                (length,) = struct.unpack("!H", await self._reader.readexactly(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", await self._reader.readexactly(8))
                if length >> 63:
                    raise await self._fail(CLOSE_PROTOCOL_ERROR, "bad length")
            if length > self.max_message:
                raise await self._fail(CLOSE_TOO_BIG, "message too big")
            mask = await self._reader.readexactly(4)
            payload = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed = ConnectionClosed(CLOSE_ABNORMAL, "connection lost")
            self._writer.close()
            raise self.closed from None
        return fin, op, _apply_mask(payload, mask)
