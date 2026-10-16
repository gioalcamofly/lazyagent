"""Framing round-trips, partial reads and malformed frames."""

from __future__ import annotations

import random
import struct

import pytest

from lazyagent.protocol.framing import (
    HEADER_SIZE,
    KIND_CONTROL,
    KIND_STREAM,
    MAX_FRAME_SIZE,
    STREAM_INPUT,
    STREAM_OUTPUT,
    ControlFrame,
    FrameDecoder,
    FrameError,
    StreamFrame,
    encode,
    encode_control,
    pack_stream,
    unpack_stream,
)

FRAMES = [
    ControlFrame({"type": "req", "id": "1", "method": "list_worktrees", "params": {}}),
    StreamFrame.output(7, 0, b"\x1b[31mhello\x1b[0m\r\n"),
    StreamFrame.input(7, b"\x03"),
    ControlFrame({"type": "evt", "seq": 1, "event": "engine.notice", "data": {"m": "ünï ✓"}}),
    StreamFrame.output(2**32 - 1, 2**64 - 1, b""),
    StreamFrame.output(1, 1234567890123, bytes(range(256)) * 40),
    StreamFrame.input(0, b""),
]


def _decode_all(chunks):
    decoder = FrameDecoder()
    out = []
    for chunk in chunks:
        decoder.feed(chunk)
        out.extend(decoder)
    decoder.eof()
    return out


def test_round_trip_each_frame():
    for frame in FRAMES:
        assert _decode_all([encode(frame)]) == [frame]


def test_round_trip_concatenated():
    wire = b"".join(encode(f) for f in FRAMES)
    assert _decode_all([wire]) == FRAMES


def test_byte_at_a_time():
    wire = b"".join(encode(f) for f in FRAMES)
    assert _decode_all([wire[i : i + 1] for i in range(len(wire))]) == FRAMES


def test_random_chunk_boundaries():
    wire = b"".join(encode(f) for f in FRAMES)
    rng = random.Random(1234)
    for _ in range(200):
        cuts = sorted(rng.sample(range(1, len(wire)), rng.randint(1, 20)))
        chunks = [wire[a:b] for a, b in zip([0, *cuts], [*cuts, len(wire)])]
        assert _decode_all(chunks) == FRAMES


def test_partial_header_and_payload_wait_for_more():
    wire = encode(FRAMES[1])
    decoder = FrameDecoder()
    decoder.feed(wire[: HEADER_SIZE - 1])
    assert decoder.next_frame() is None
    decoder.feed(wire[HEADER_SIZE - 1 : -1])
    assert decoder.next_frame() is None
    assert decoder.buffered == len(wire) - 1
    decoder.feed(wire[-1:])
    assert decoder.next_frame() == FRAMES[1]
    assert decoder.buffered == 0


def test_header_layout():
    payload = b'{"a":1}'
    assert encode_control({"a": 1}) == struct.pack(">IB", len(payload), KIND_CONTROL) + payload
    frame = StreamFrame.output(5, 9, b"xy")
    assert pack_stream(frame) == struct.pack(">BIQ", STREAM_OUTPUT, 5, 9) + b"xy"
    assert encode(frame)[:HEADER_SIZE] == struct.pack(">IB", 13 + 2, KIND_STREAM)
    assert pack_stream(StreamFrame.input(5, b"k")) == struct.pack(">BI", STREAM_INPUT, 5) + b"k"


def test_stream_payload_round_trip_for_websocket():
    for frame in FRAMES:
        if isinstance(frame, StreamFrame):
            assert unpack_stream(pack_stream(frame)) == frame


def test_eof_with_incomplete_frame_is_an_error():
    decoder = FrameDecoder()
    decoder.feed(encode(FRAMES[0])[:-3])
    assert list(decoder) == []
    with pytest.raises(FrameError, match="incomplete frame"):
        decoder.eof()


def test_unknown_kind_poisons_the_decoder():
    decoder = FrameDecoder()
    decoder.feed(struct.pack(">IB", 2, 9) + b"{}")
    with pytest.raises(FrameError, match="unknown frame kind 9"):
        decoder.next_frame()
    assert decoder.failed
    with pytest.raises(FrameError):
        decoder.feed(encode(FRAMES[0]))


def test_oversized_length_is_rejected_before_buffering():
    decoder = FrameDecoder()
    decoder.feed(struct.pack(">IB", MAX_FRAME_SIZE + 1, KIND_CONTROL))
    with pytest.raises(FrameError, match="exceeds the maximum"):
        decoder.next_frame()


def test_bad_payload_is_skipped_and_decoding_continues():
    bad = struct.pack(">IB", 3, KIND_CONTROL) + b"{{{"
    decoder = FrameDecoder()
    decoder.feed(bad + encode(FRAMES[0]))
    with pytest.raises(FrameError, match="not valid JSON"):
        decoder.next_frame()
    assert not decoder.failed
    assert decoder.next_frame() == FRAMES[0]


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"\xff\xfe", "not valid UTF-8"),
        (b"[1,2]", "JSON object"),
        (b"", "not valid JSON"),
    ],
)
def test_bad_control_payloads(payload, match):
    decoder = FrameDecoder()
    decoder.feed(struct.pack(">IB", len(payload), KIND_CONTROL) + payload)
    with pytest.raises(FrameError, match=match):
        decoder.next_frame()


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"", "empty stream frame"),
        (b"\x01\x00\x00", "output stream frame too short"),
        (b"\x02\x00", "input stream frame too short"),
        (b"\x07rest", "unknown stream frame type 7"),
    ],
)
def test_bad_stream_payloads(payload, match):
    with pytest.raises(FrameError, match=match):
        unpack_stream(payload)


@pytest.mark.parametrize(
    "frame",
    [
        StreamFrame(STREAM_OUTPUT, 1, b"", None),
        StreamFrame(STREAM_OUTPUT, 1, b"", -1),
        StreamFrame(STREAM_OUTPUT, 2**32, b"", 0),
        StreamFrame(STREAM_INPUT, 1, b"", 5),
        StreamFrame(9, 1, b""),
    ],
)
def test_encoding_rejects_invalid_stream_frames(frame):
    with pytest.raises(ValueError):
        pack_stream(frame)
