"""Byte framing for protocol v1 over a stream socket.

Every frame is ``u32 length`` · ``u8 kind`` · payload, big-endian, where
``length`` counts the payload only (not the 5-byte header):

- ``kind = 1`` (:data:`KIND_CONTROL`): UTF-8 JSON, one envelope message.
- ``kind = 2`` (:data:`KIND_STREAM`): a binary PTY stream frame.

A stream frame payload is ``u8 type`` · ``u32 stream_id`` · ``u64 offset``
(output only) · data bytes. ``offset`` is the position of the first data
byte in the session's output since the session started, so a client can
detect gaps and resume with ``attach_stream(from_offset=…)``. Input frames
carry no offset.

Over WebSocket the outer header is dropped: text frames carry the JSON and
binary frames carry the stream payload (:func:`pack_stream` /
:func:`unpack_stream`).
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

KIND_CONTROL = 1
KIND_STREAM = 2

STREAM_OUTPUT = 1
STREAM_INPUT = 2

# Upper bound on one frame's payload. A larger length prefix almost always
# means the peer is not speaking this protocol, so we fail instead of
# buffering it.
MAX_FRAME_SIZE = 16 * 1024 * 1024

_HEADER = struct.Struct(">IB")
_OUTPUT_HEADER = struct.Struct(">BIQ")
_INPUT_HEADER = struct.Struct(">BI")
HEADER_SIZE = _HEADER.size

_U32_MAX = 2**32 - 1
_U64_MAX = 2**64 - 1


class FrameError(Exception):
    """Bytes that cannot be decoded as a frame."""


@dataclass(frozen=True)
class ControlFrame:
    message: dict[str, Any]


@dataclass(frozen=True)
class StreamFrame:
    type: int
    stream_id: int
    data: bytes
    offset: int | None = None   # set for STREAM_OUTPUT, None for STREAM_INPUT

    @classmethod
    def output(cls, stream_id: int, offset: int, data: bytes) -> StreamFrame:
        return cls(STREAM_OUTPUT, stream_id, data, offset)

    @classmethod
    def input(cls, stream_id: int, data: bytes) -> StreamFrame:
        return cls(STREAM_INPUT, stream_id, data)


Frame = ControlFrame | StreamFrame


def pack_stream(frame: StreamFrame) -> bytes:
    """Stream frame payload, without the outer length/kind header."""
    if not 0 <= frame.stream_id <= _U32_MAX:
        raise ValueError(f"stream_id out of u32 range: {frame.stream_id}")
    if frame.type == STREAM_OUTPUT:
        if frame.offset is None or not 0 <= frame.offset <= _U64_MAX:
            raise ValueError(f"output frame needs a u64 offset, got {frame.offset!r}")
        return _OUTPUT_HEADER.pack(STREAM_OUTPUT, frame.stream_id, frame.offset) + frame.data
    if frame.type == STREAM_INPUT:
        if frame.offset is not None:
            raise ValueError("input frames carry no offset")
        return _INPUT_HEADER.pack(STREAM_INPUT, frame.stream_id) + frame.data
    raise ValueError(f"unknown stream frame type: {frame.type}")


def unpack_stream(payload: bytes) -> StreamFrame:
    """Inverse of :func:`pack_stream`; raises :class:`FrameError`."""
    if not payload:
        raise FrameError("empty stream frame")
    frame_type = payload[0]
    if frame_type == STREAM_OUTPUT:
        if len(payload) < _OUTPUT_HEADER.size:
            raise FrameError(
                f"output stream frame too short: {len(payload)} bytes, "
                f"need at least {_OUTPUT_HEADER.size}"
            )
        _, stream_id, offset = _OUTPUT_HEADER.unpack_from(payload)
        return StreamFrame(STREAM_OUTPUT, stream_id, bytes(payload[_OUTPUT_HEADER.size:]), offset)
    if frame_type == STREAM_INPUT:
        if len(payload) < _INPUT_HEADER.size:
            raise FrameError(
                f"input stream frame too short: {len(payload)} bytes, "
                f"need at least {_INPUT_HEADER.size}"
            )
        _, stream_id = _INPUT_HEADER.unpack_from(payload)
        return StreamFrame(STREAM_INPUT, stream_id, bytes(payload[_INPUT_HEADER.size:]))
    raise FrameError(f"unknown stream frame type {frame_type}; expected 1 (output) or 2 (input)")


def decode_control(payload: bytes) -> dict[str, Any]:
    """JSON control payload → object; raises :class:`FrameError`."""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise FrameError(f"control frame is not valid UTF-8: {e}") from None
    except ValueError as e:
        raise FrameError(f"control frame is not valid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise FrameError(f"control frame must hold a JSON object, got {type(obj).__name__}")
    return obj


def _wrap(kind: int, payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError(f"frame payload of {len(payload)} bytes exceeds {MAX_FRAME_SIZE}")
    return _HEADER.pack(len(payload), kind) + payload


def encode_control(message: dict[str, Any]) -> bytes:
    """A complete control frame for a JSON message object."""
    return _wrap(KIND_CONTROL, json.dumps(message, separators=(",", ":")).encode())


def encode_stream(frame: StreamFrame) -> bytes:
    """A complete stream frame."""
    return _wrap(KIND_STREAM, pack_stream(frame))


def encode(frame: Frame) -> bytes:
    if isinstance(frame, ControlFrame):
        return encode_control(frame.message)
    return encode_stream(frame)


class FrameDecoder:
    """Incremental decoder: :meth:`feed` bytes as they arrive, pull frames
    with :meth:`next_frame` (or iterate).

    A bad header (unknown kind, oversized length) makes the byte stream
    unrecoverable, so the decoder stays failed and keeps raising. A bad
    payload inside a well-delimited frame is consumed before raising, so the
    caller may log it and continue with the next frame.
    """

    def __init__(self, max_frame_size: int = MAX_FRAME_SIZE) -> None:
        self._buf = bytearray()
        self._max = max_frame_size
        self._failed: FrameError | None = None

    @property
    def buffered(self) -> int:
        """Bytes received but not yet returned as a frame."""
        return len(self._buf)

    @property
    def failed(self) -> bool:
        return self._failed is not None

    def feed(self, data: bytes) -> None:
        if self._failed is not None:
            raise self._failed
        self._buf += data

    def next_frame(self) -> Frame | None:
        """The next complete frame, or None if more bytes are needed."""
        if self._failed is not None:
            raise self._failed
        if len(self._buf) < HEADER_SIZE:
            return None
        length, kind = _HEADER.unpack_from(self._buf)
        if kind not in (KIND_CONTROL, KIND_STREAM):
            self._failed = FrameError(
                f"unknown frame kind {kind}; expected 1 (control) or 2 (stream)"
            )
            raise self._failed
        if length > self._max:
            self._failed = FrameError(
                f"frame length {length} exceeds the maximum of {self._max} bytes"
            )
            raise self._failed
        end = HEADER_SIZE + length
        if len(self._buf) < end:
            return None
        payload = bytes(self._buf[HEADER_SIZE:end])
        del self._buf[:end]
        if kind == KIND_CONTROL:
            return ControlFrame(decode_control(payload))
        return unpack_stream(payload)

    def __iter__(self):
        while (frame := self.next_frame()) is not None:
            yield frame

    def eof(self) -> None:
        """Signal end of input; raises if a partial frame is left over."""
        if self._buf:
            raise FrameError(
                f"connection closed with {len(self._buf)} bytes of an incomplete frame"
            )
