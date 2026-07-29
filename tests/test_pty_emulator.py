"""Tests for PtyEmulator's write path."""

from __future__ import annotations

import pytest

from lazyagent.pty_emulator import PtyEmulator


class _ShortWriter:
    """Stand-in for the unbuffered PTY file object.

    ``buffering=0`` means ``write`` is one ``write(2)`` and may report a short
    count; ``max_chunk`` reproduces that.
    """

    def __init__(self, max_chunk: int, block_once: bool = False):
        self.max_chunk = max_chunk
        self.written = bytearray()
        self._block_once = block_once

    def write(self, data) -> int | None:
        if self._block_once:
            self._block_once = False
            return None  # would block
        chunk = bytes(data[: self.max_chunk])
        self.written += chunk
        return len(chunk)


def _make_emulator(writer) -> PtyEmulator:
    emulator = PtyEmulator.__new__(PtyEmulator)
    emulator.p_out = writer
    return emulator


class TestWriteAll:
    @pytest.mark.asyncio
    async def test_short_writes_are_resumed_not_dropped(self):
        """A truncated payload eats part of the instruction, or the Enter."""
        writer = _ShortWriter(max_chunk=8)
        payload = b"a long instruction that will not fit in one write\r"

        await _make_emulator(writer)._write_all(payload)

        assert bytes(writer.written) == payload

    @pytest.mark.asyncio
    async def test_retries_when_the_write_would_block(self):
        writer = _ShortWriter(max_chunk=64, block_once=True)

        await _make_emulator(writer)._write_all(b"hello\r")

        assert bytes(writer.written) == b"hello\r"

    @pytest.mark.asyncio
    async def test_gives_up_when_no_progress_is_possible(self):
        """Must not spin forever on a writer that accepts nothing."""
        writer = _ShortWriter(max_chunk=0)

        await _make_emulator(writer)._write_all(b"hello")

        assert bytes(writer.written) == b""
