"""Garbage in, clear errors out: the decoders never crash on bad bytes.

Seeded stdlib ``random`` so failures reproduce.
"""

from __future__ import annotations

import random

import pytest

from lazyagent.protocol import envelope
from lazyagent.protocol.framing import (
    ControlFrame,
    FrameDecoder,
    FrameError,
    StreamFrame,
    encode,
    unpack_stream,
)

SEED = 20261015
ROUNDS = 2000

VALID_WIRE = b"".join(
    encode(f)
    for f in [
        ControlFrame({"type": "req", "id": "1", "method": "list_agents", "params": {"worktree_path": "/r"}}),
        StreamFrame.output(3, 100, b"\x1b[2Jhi"),
        StreamFrame.input(3, b"q"),
        ControlFrame({"type": "res", "id": "1", "result": {"agents": []}}),
    ]
)


def _drain(data: bytes, rng: random.Random) -> None:
    """Feed ``data`` in random chunks; only FrameError may escape a frame."""
    decoder = FrameDecoder(max_frame_size=1 << 16)
    pos = 0
    while pos < len(data):
        step = rng.randint(1, 64)
        try:
            decoder.feed(data[pos : pos + step])
        except FrameError:
            assert decoder.failed
            return
        pos += step
        while True:
            try:
                frame = decoder.next_frame()
            except FrameError as e:
                assert str(e)
                if decoder.failed:
                    return
                continue
            if frame is None:
                break
            if isinstance(frame, ControlFrame):
                try:
                    envelope.parse_message(frame.message)
                except envelope.ProtocolError as e:
                    assert e.code == envelope.ErrorCode.PROTOCOL_ERROR and e.message
    try:
        decoder.eof()
    except FrameError as e:
        assert "incomplete frame" in str(e)


def test_random_bytes():
    rng = random.Random(SEED)
    for _ in range(ROUNDS):
        _drain(rng.randbytes(rng.randint(0, 512)), rng)


def test_truncated_valid_streams():
    rng = random.Random(SEED + 1)
    for _ in range(ROUNDS):
        _drain(VALID_WIRE[: rng.randint(0, len(VALID_WIRE))], rng)


def test_mutated_valid_streams():
    rng = random.Random(SEED + 2)
    for _ in range(ROUNDS):
        data = bytearray(VALID_WIRE)
        for _ in range(rng.randint(1, 8)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        _drain(bytes(data), rng)


def test_random_stream_payloads():
    rng = random.Random(SEED + 3)
    for _ in range(ROUNDS):
        payload = rng.randbytes(rng.randint(0, 40))
        try:
            frame = unpack_stream(payload)
        except FrameError as e:
            assert str(e)
        else:
            assert isinstance(frame, StreamFrame)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"null",
        b"[]",
        b'"req"',
        b"{}",
        b'{"type":"req"}',
        b'{"type":"req","id":1,"method":"x"}',
        b'{"type":"req","id":"1","method":"x","params":[]}',
        b'{"type":"res","id":"1"}',
        b'{"type":"res","id":"1","result":1,"error":{}}',
        b'{"type":"res","id":"1","error":"boom"}',
        b'{"type":"res","id":"1","error":{"code":1,"message":"m"}}',
        b'{"type":"evt","seq":-1,"event":"x"}',
        b'{"type":"evt","seq":true,"event":"x"}',
        b'{"type":"evt","seq":1}',
        b'{"type":"nope"}',
        b'{"type":"req","id":"1","method":"x"',
        b"\xff\xfe{}",
    ],
)
def test_malformed_envelopes_give_protocol_errors(payload):
    with pytest.raises(envelope.ProtocolError) as info:
        envelope.loads(payload)
    assert info.value.code == envelope.ErrorCode.PROTOCOL_ERROR
    assert info.value.message


def test_random_json_values_never_crash_parse_message():
    rng = random.Random(SEED + 4)
    atoms = [None, True, 0, -1, 1.5, "", "req", "res", "evt", [], {}]
    keys = ["type", "id", "method", "params", "result", "error", "seq", "event", "data"]
    for _ in range(ROUNDS):
        obj = {k: rng.choice(atoms) for k in rng.sample(keys, rng.randint(0, len(keys)))}
        try:
            envelope.parse_message(obj)
        except envelope.ProtocolError as e:
            assert e.message
