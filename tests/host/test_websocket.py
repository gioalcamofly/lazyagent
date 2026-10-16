"""The stdlib WebSocket server side (lazyagent.host.websocket), against a
raw client that can send any frame, well formed or not."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from lazyagent.host import websocket as ws
from tests.host.wsclient import Refused, WsClient

pytestmark = pytest.mark.asyncio


class EchoServer:
    """Upgrades every request and echoes each message; records how the
    connection ended."""

    def __init__(self, max_message: int = 1024) -> None:
        self.max_message = max_message
        self.closed: list[ws.ConnectionClosed] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=ws.MAX_REQUEST_HEAD
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer) -> None:
        try:
            request = await ws.read_request(reader)
        except ws.HttpError as e:
            writer.write(f"HTTP/1.1 {e.status} No\r\n\r\n".encode())
            writer.close()
            return
        problem = ws.upgrade_problem(request)
        if problem is not None:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            writer.close()
            return
        writer.write(ws.handshake_response(request.header("sec-websocket-key")))
        socket = ws.WebSocket(reader, writer, max_message=self.max_message)
        try:
            while True:
                message = await socket.recv()
                if isinstance(message, str):
                    socket.send_text(message)
                else:
                    socket.send_binary(message)
        except ws.ConnectionClosed as e:
            self.closed.append(e)

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


@pytest_asyncio.fixture
async def server():
    s = EchoServer()
    await s.start()
    yield s
    await s.stop()


@pytest_asyncio.fixture
async def client(server):
    c = await WsClient.connect(server.port)
    yield c
    await c.close()


# ---------------------------------------------------------------- handshake


async def test_accept_key_matches_rfc_example():
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def _request(**overrides) -> ws.HttpRequest:
    headers = {
        "host": "127.0.0.1:1",
        "upgrade": "websocket",
        "connection": "keep-alive, Upgrade",
        "sec-websocket-version": "13",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
    }
    method = overrides.pop("method", "GET")
    version = overrides.pop("version", "HTTP/1.1")
    for name, value in overrides.items():
        name = name.replace("_", "-")
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    return ws.HttpRequest(method, "/ws", version, headers)


async def test_upgrade_problem_accepts_a_valid_request():
    assert ws.upgrade_problem(_request()) is None
    assert ws.upgrade_problem(_request(upgrade="WebSocket")) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "POST"},
        {"version": "HTTP/1.0"},
        {"upgrade": None},
        {"upgrade": "h2c"},
        {"connection": "keep-alive"},
        {"sec_websocket_version": "8"},
        {"sec_websocket_key": None},
        {"sec_websocket_key": "not base64!"},
        {"sec_websocket_key": "c2hvcnQ="},  # 5 bytes, not 16
    ],
)
async def test_upgrade_problem_rejects(overrides):
    assert ws.upgrade_problem(_request(**overrides)) is not None


async def _parse(raw: bytes) -> ws.HttpRequest:
    reader = asyncio.StreamReader(limit=ws.MAX_REQUEST_HEAD)
    reader.feed_data(raw)
    reader.feed_eof()
    return await ws.read_request(reader)


async def test_read_request_parses_headers_case_insensitively():
    request = await _parse(
        b"GET /ws?x=1 HTTP/1.1\r\nHost: a\r\nX-Two: 1\r\nx-two: 2\r\n\r\n"
    )
    assert (request.method, request.path, request.version) == ("GET", "/ws", "HTTP/1.1")
    assert request.header("HOST") == "a"
    assert request.header("x-two") == "1, 2"


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"GET /\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\nno colon\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\nHost: a\r\n folded\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\nHost: a\r\n", 400),
        (b"GET / HTTP/1.1\r\nX: " + b"a" * ws.MAX_REQUEST_HEAD + b"\r\n\r\n", 431),
    ],
)
async def test_read_request_rejects(raw, status):
    with pytest.raises(ws.HttpError) as e:
        await _parse(raw)
    assert e.value.status == status


async def test_bad_upgrade_is_refused(server):
    with pytest.raises(Refused):
        await WsClient.connect(server.port, headers={"Sec-WebSocket-Version": "8"})


# ---------------------------------------------------------------- messages


async def test_text_and_binary_echo(client):
    client.send_text("héllo")
    assert await client.recv() == "héllo"
    client.send_binary(b"\x00\xff\x1b[")
    assert await client.recv() == b"\x00\xff\x1b["
    client.send_binary(b"")
    assert await client.recv() == b""


@pytest.mark.parametrize("size", [125, 126, 65535, 65536, 70000])
async def test_lengths_7_16_and_64_bit(size):
    server = EchoServer(max_message=100_000)
    await server.start()
    client = await WsClient.connect(server.port)
    try:
        payload = bytes(range(256)) * (size // 256) + bytes(size % 256)
        client.send_binary(payload)
        assert await client.recv() == payload
    finally:
        await client.close()
        await server.stop()


async def test_fragmented_message_with_a_ping_in_between(client):
    client.send_frame(ws.OP_TEXT, b"frag", fin=False)
    client.send_frame(ws.OP_CONTINUATION, b"men", fin=False)
    client.send_frame(ws.OP_PING, b"are you there")
    client.send_frame(ws.OP_CONTINUATION, "ted ✓".encode())
    fin, op, payload = await client.recv_frame()
    assert (fin, op, payload) == (True, ws.OP_PONG, b"are you there")
    assert await client.recv() == "fragmented ✓"


async def test_text_split_inside_a_utf8_character_is_reassembled(client):
    data = "✓".encode()
    client.send_frame(ws.OP_TEXT, data[:1], fin=False)
    client.send_frame(ws.OP_CONTINUATION, data[1:])
    assert await client.recv() == "✓"


async def test_unsolicited_pong_is_ignored(client):
    client.send_frame(ws.OP_PONG, b"x")
    client.send_text("still here")
    assert await client.recv() == "still here"


async def test_server_ping_frame(server):
    async def handle(reader, writer):
        await ws.read_request(reader)
        writer.write(ws.handshake_response("dGhlIHNhbXBsZSBub25jZQ=="))
        socket = ws.WebSocket(reader, writer)
        socket.ping(b"hi")
        await socket.drain()
        await socket.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    await reader.readuntil(b"\r\n\r\n")
    client = WsClient(reader, writer)
    assert await client.recv_frame() == (True, ws.OP_PING, b"hi")
    assert await client.expect_close() == ws.CLOSE_NORMAL
    await client.close()
    srv.close()
    await srv.wait_closed()


# ---------------------------------------------------------------- closing


async def _closed_with(client, server, code: int) -> None:
    assert await client.expect_close() == code
    assert await client.at_eof()
    for _ in range(100):
        if server.closed:
            break
        await asyncio.sleep(0.01)
    assert server.closed[-1].code == code


async def test_close_handshake_echoes_the_code(client, server):
    client.send_close(4001, "bye")
    assert await client.expect_close() == 4001
    assert await client.at_eof()
    await asyncio.sleep(0.05)
    assert (server.closed[-1].code, server.closed[-1].reason) == (4001, "bye")


async def test_close_without_a_code(client, server):
    client.send_close(None)
    assert await client.expect_close() == ws.CLOSE_NORMAL
    await asyncio.sleep(0.05)
    assert server.closed[-1].code == ws.CLOSE_NO_STATUS


async def test_abrupt_disconnect_is_1006(client, server):
    client.writer.write(b"\x81")  # half a frame header
    await client.close()
    for _ in range(100):
        if server.closed:
            break
        await asyncio.sleep(0.01)
    assert server.closed[-1].code == ws.CLOSE_ABNORMAL


@pytest.mark.parametrize(
    ("send", "code"),
    [
        (
            lambda c: c.send_frame(ws.OP_TEXT, b"x", masked=False),
            ws.CLOSE_PROTOCOL_ERROR,
        ),
        (lambda c: c.send_frame(ws.OP_TEXT, b"x", rsv=4), ws.CLOSE_PROTOCOL_ERROR),
        (lambda c: c.send_frame(0x3, b"x"), ws.CLOSE_PROTOCOL_ERROR),
        (lambda c: c.send_frame(ws.OP_CONTINUATION, b"x"), ws.CLOSE_PROTOCOL_ERROR),
        (
            lambda c: (
                c.send_frame(ws.OP_TEXT, b"a", fin=False),
                c.send_frame(ws.OP_BINARY, b"b"),
            ),
            ws.CLOSE_PROTOCOL_ERROR,
        ),
        (lambda c: c.send_frame(ws.OP_PING, b"x", fin=False), ws.CLOSE_PROTOCOL_ERROR),
        (lambda c: c.send_frame(ws.OP_PING, b"x" * 126), ws.CLOSE_PROTOCOL_ERROR),
        (lambda c: c.send_frame(ws.OP_CLOSE, b"\x03"), ws.CLOSE_PROTOCOL_ERROR),
        (lambda c: c.send_close(1005), ws.CLOSE_PROTOCOL_ERROR),
        (lambda c: c.send_close(999), ws.CLOSE_PROTOCOL_ERROR),
        (lambda c: c.send_close(2000), ws.CLOSE_PROTOCOL_ERROR),
        (
            lambda c: c.send_frame(ws.OP_CLOSE, ws.close_payload(1000) + b"\xff"),
            ws.CLOSE_INVALID_DATA,
        ),
        (lambda c: c.send_frame(ws.OP_TEXT, b"\xff\xfe"), ws.CLOSE_INVALID_DATA),
        (lambda c: c.send_binary(b"x" * 1025), ws.CLOSE_TOO_BIG),
        (
            lambda c: (
                c.send_frame(ws.OP_BINARY, b"x" * 600, fin=False),
                c.send_frame(ws.OP_CONTINUATION, b"x" * 600),
            ),
            ws.CLOSE_TOO_BIG,
        ),
    ],
    ids=[
        "unmasked",
        "reserved-bits",
        "unknown-opcode",
        "stray-continuation",
        "data-frame-inside-fragmented-message",
        "fragmented-control",
        "long-control",
        "one-byte-close",
        "close-code-1005",
        "close-code-999",
        "close-code-2000",
        "close-reason-not-utf8",
        "text-not-utf8",
        "frame-too-big",
        "fragments-too-big",
    ],
)
async def test_protocol_violations_close_the_connection(client, server, send, code):
    send(client)
    await _closed_with(client, server, code)


async def test_huge_declared_length_is_refused_before_reading(client, server):
    # A 64-bit length the server must not try to buffer.
    client.writer.write(b"\x82\xff" + (1 << 40).to_bytes(8, "big") + b"mask")
    await _closed_with(client, server, ws.CLOSE_TOO_BIG)


async def test_length_with_the_top_bit_set_is_a_protocol_error(client, server):
    client.writer.write(b"\x82\xff" + (1 << 63).to_bytes(8, "big") + b"mask")
    await _closed_with(client, server, ws.CLOSE_PROTOCOL_ERROR)


async def test_send_after_close_raises():
    server_side: list[ws.WebSocket] = []

    async def handle(r, w):
        socket = ws.WebSocket(r, w)
        server_side.append(socket)
        await socket.close(ws.CLOSE_GOING_AWAY, "done")

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = WsClient(reader, writer)
    assert await client.expect_close() == ws.CLOSE_GOING_AWAY
    with pytest.raises(ws.ConnectionClosed):
        server_side[0].send_text("late")
    with pytest.raises(ws.ConnectionClosed) as e:
        await server_side[0].recv()
    assert e.value.code == ws.CLOSE_GOING_AWAY
    await client.close()
    srv.close()
    await srv.wait_closed()
