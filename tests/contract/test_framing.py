"""Malformed input: a bad header closes the connection, a bad payload in a
well-formed frame is reported and the connection carries on."""

from __future__ import annotations

import asyncio
import struct

import pytest

from lazyagent.engine.client import ConnectionClosed
from lazyagent.protocol import framing

pytestmark = pytest.mark.asyncio


def _frame(kind: int, payload: bytes) -> bytes:
    return struct.pack(">IB", len(payload), kind) + payload


@pytest.mark.parametrize(
    "header",
    [
        struct.pack(">IB", 4, 9),  # unknown kind
        struct.pack(">IB", framing.MAX_FRAME_SIZE + 1, 1),  # too long
    ],
    ids=["unknown kind", "oversized"],
)
async def test_a_bad_header_closes_the_connection(client, header):
    client.send_raw(header + b"{}{}")
    await asyncio.wait_for(client.wait_closed(), 5)
    assert client.closed
    with pytest.raises(ConnectionClosed):
        await client.request("list_worktrees")


@pytest.mark.parametrize(
    "frame",
    [
        _frame(1, b"{not json"),
        _frame(1, b"\xff\xfe"),
        _frame(1, b"[1, 2]"),
        _frame(1, b'{"type": "req", "id": 7, "method": "x"}'),
        _frame(1, b'{"type": "res", "id": "1", "result": null}'),
        _frame(1, b'{"type": "what"}'),
        _frame(2, b""),
        _frame(2, b"\x02\x00"),
        _frame(2, b"\x09" + b"\x00" * 12),
        _frame(2, framing.pack_stream(framing.StreamFrame.output(1, 0, b"x"))),
        _frame(2, framing.pack_stream(framing.StreamFrame.input(42, b"x"))),
    ],
    ids=[
        "invalid json",
        "invalid utf-8",
        "not an object",
        "bad request id",
        "a response",
        "unknown message type",
        "empty stream frame",
        "short input frame",
        "unknown stream frame type",
        "output frame from a client",
        "input for an unknown stream",
    ],
)
async def test_a_bad_payload_is_reported_and_the_connection_carries_on(client, frame):
    client.send_raw(frame)
    assert await client.request("list_worktrees")
    assert len(client.errors) == 1
    assert client.errors[0].code == "PROTOCOL_ERROR"
    assert not client.closed


async def test_frames_may_arrive_in_pieces(harness):
    reader, writer = await harness.open_raw()
    hello = framing.encode_control(
        {
            "type": "req",
            "id": "h",
            "method": "hello",
            "params": {"protocol": 1, "client": "cli"},
        }
    )
    request = framing.encode_control(
        {"type": "req", "id": "c", "method": "get_config", "params": {}}
    )
    for byte in hello + request:
        writer.write(bytes([byte]))
        await writer.drain()
    decoder = framing.FrameDecoder()
    replies = []
    while len(replies) < 2:
        decoder.feed(await asyncio.wait_for(reader.read(1 << 16), 5))
        replies += [frame.message for frame in decoder]
    assert [r["id"] for r in replies] == ["h", "c"]
    assert all("result" in r for r in replies)
    writer.close()
