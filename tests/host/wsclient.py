"""A bare WebSocket client for the host tests: it sends exactly the frames a
test asks for (masked, unless told otherwise) and reads raw frames back."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
from typing import Any

from lazyagent.host import websocket as ws


class Refused(Exception):
    """The server answered the upgrade with an HTTP error."""

    def __init__(self, status: int, head: str) -> None:
        super().__init__(f"{status}: {head}")
        self.status = status
        self.head = head


async def http_get(
    port: int, path: str, *, host: str | None = None, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    host = host or f"127.0.0.1:{port}"
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(lines[0].split()[1]), headers, body


class WsClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(
        cls,
        port: int,
        *,
        path: str = "/ws",
        host: str | None = None,
        origin: str | None = "",
        headers: dict[str, str] | None = None,
    ) -> WsClient:
        """``origin=""`` means the server's own; None sends none."""
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        all_headers = {
            "Host": host or f"127.0.0.1:{port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
        }
        if origin is not None:
            all_headers["Origin"] = origin or f"http://127.0.0.1:{port}"
        all_headers.update(headers or {})
        lines = [f"GET {path} HTTP/1.1", *(f"{k}: {v}" for k, v in all_headers.items())]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        status = int(head.split()[1])
        if status != 101:
            writer.close()
            raise Refused(status, head)
        assert f"Sec-WebSocket-Accept: {ws.accept_key(key)}" in head
        return cls(reader, writer)

    def send_frame(
        self,
        opcode: int,
        payload: bytes,
        *,
        fin: bool = True,
        masked: bool = True,
        rsv: int = 0,
    ) -> None:
        frame = bytearray(
            ws.encode_frame(
                opcode, payload, fin=fin, mask=os.urandom(4) if masked else None
            )
        )
        frame[0] |= rsv << 4
        self.writer.write(bytes(frame))

    def send_text(self, text: str) -> None:
        self.send_frame(ws.OP_TEXT, text.encode())

    def send_json(self, message: Any) -> None:
        self.send_text(json.dumps(message))

    def send_binary(self, data: bytes) -> None:
        self.send_frame(ws.OP_BINARY, data)

    def send_close(self, code: int | None = ws.CLOSE_NORMAL, reason: str = "") -> None:
        payload = b"" if code is None else ws.close_payload(code, reason)
        self.send_frame(ws.OP_CLOSE, payload)

    async def recv_frame(self, timeout: float = 10.0) -> tuple[bool, int, bytes]:
        async def read() -> tuple[bool, int, bytes]:
            b0, b1 = await self.reader.readexactly(2)
            assert not b1 & 0x80, "server frames are never masked"
            length = b1 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", await self.reader.readexactly(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", await self.reader.readexactly(8))
            return bool(b0 & 0x80), b0 & 0x0F, await self.reader.readexactly(length)

        return await asyncio.wait_for(read(), timeout)

    async def recv(self, timeout: float = 10.0) -> str | bytes | tuple[int, str]:
        """The next message; a close frame comes back as ``(code, reason)``."""
        _, op, payload = await self.recv_frame(timeout)
        if op == ws.OP_TEXT:
            return payload.decode()
        if op == ws.OP_CLOSE:
            code = (
                struct.unpack("!H", payload[:2])[0] if payload else ws.CLOSE_NO_STATUS
            )
            return code, payload[2:].decode()
        return payload

    async def expect_close(self, timeout: float = 10.0) -> int:
        """Read until the close frame and return its code."""
        while True:
            message = await self.recv(timeout)
            if isinstance(message, tuple):
                return message[0]

    async def at_eof(self, timeout: float = 5.0) -> bool:
        data = await asyncio.wait_for(self.reader.read(), timeout)
        return data == b""

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def tui_output(offset: int, data: bytes) -> bytes:
    """What the host sends for TUI output: an output frame on stream 0."""
    return struct.pack(">BIQ", 1, 0, offset) + data


def input_frame(data: bytes, stream_id: int = 0) -> bytes:
    """Keys for a stream (0: the TUI), as the page sends them."""
    return struct.pack(">BI", 2, stream_id) + data


def output_frame(frame: bytes) -> tuple[int, int, bytes]:
    """(stream_id, offset, data) of an output frame."""
    kind, stream_id, offset = struct.unpack_from(">BIQ", frame)
    assert kind == 1, frame[:16]
    return stream_id, offset, frame[13:]
