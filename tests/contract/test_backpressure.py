"""A client that stops reading loses its stream (stream.reset); the engine
keeps serving everyone else."""

from __future__ import annotations

import asyncio
import time

import pytest

from lazyagent.protocol import framing, validation
from tests.contract.helpers import Output, checked, spawn, until, wait_for_text

pytestmark = pytest.mark.asyncio


class RawClient:
    """Hand-driven connection that reads only when told to."""

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self.decoder = framing.FrameDecoder()
        self.ids = 0

    def request(self, method: str, **params) -> str:
        self.ids += 1
        request_id = f"raw-{self.ids}"
        self.writer.write(
            framing.encode_control(
                {"type": "req", "id": request_id, "method": method, "params": params}
            )
        )
        return request_id

    async def frames(self, timeout: float = 5.0):
        """Frames as they arrive, until ``timeout`` passes without one."""
        while True:
            for frame in self.decoder:
                yield frame
            try:
                data = await asyncio.wait_for(self.reader.read(1 << 16), timeout)
            except asyncio.TimeoutError:
                return
            if not data:
                return
            self.decoder.feed(data)

    async def response(self, request_id: str) -> dict:
        async for frame in self.frames():
            if (
                isinstance(frame, framing.ControlFrame)
                and frame.message.get("id") == request_id
            ):
                return frame.message
        raise AssertionError(f"no response to {request_id}")


async def test_a_slow_client_is_reset_and_the_others_carry_on(harness, client):
    agent = await spawn(client, harness.feat)
    reader, writer = await harness.open_raw()
    slow = RawClient(reader, writer)
    await slow.response(slow.request("hello", protocol=1, client="cli"))
    attached = await slow.response(
        slow.request("attach_stream", session_id=agent["session_id"])
    )
    stream_id = attached["result"]["stream_id"]
    # From here on the slow client reads nothing.

    fast = Output()
    stream = await client.attach(agent["session_id"], fast)
    client.send_input(stream.stream_id, b"spew 3000000\r")
    started = time.monotonic()
    while b"done" not in fast.data:
        # The engine still answers promptly while the slow client is stuck.
        t0 = time.monotonic()
        await checked(client, "list_worktrees")
        assert time.monotonic() - t0 < 2.0
        assert time.monotonic() - started < 60
        await asyncio.sleep(0.05)
    await wait_for_text(client, agent, "done")
    assert fast.contiguous_from(stream.offset) and stream.gaps == []
    assert len(fast.data) > 3000000

    received = 0
    reset = None
    async for frame in slow.frames(timeout=2.0):
        if isinstance(frame, framing.StreamFrame):
            assert frame.offset == attached["result"]["offset"] + received
            received += len(frame.data)
        elif frame.message.get("event") == "stream.reset":
            reset = frame.message
            break
    assert reset is not None, "the slow client was never reset"
    validation.validate_message(reset, strict=True)
    validation.validate_event("stream.reset", reset["data"], strict=True)
    assert reset["data"]["stream_id"] == stream_id
    # It is told where its output stopped, so it can re-attach from there.
    assert reset["data"]["offset"] == attached["result"]["offset"] + received
    assert received < 3000000

    resumed = await slow.response(
        slow.request(
            "attach_stream",
            session_id=agent["session_id"],
            from_offset=reset["data"]["offset"],
        )
    )
    assert resumed["result"]["mode"] in ("tail", "snapshot")
    writer.close()


async def test_a_client_that_vanishes_is_cleaned_up(harness, client):
    agent = await spawn(client, harness.feat)
    other = await harness.connect()
    await other.attach(agent["session_id"], Output())
    await other.close()

    async def attached() -> int:
        listed = await checked(client, "list_agents", worktree_path=harness.feat)
        return listed["agents"][0]["attached_clients"]

    deadline = time.monotonic() + 5
    while await attached() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert await attached() == 0
    await until(lambda: True)
