"""B2 at the engine (§7d-1): a key's echo over the v1 socket while hidden
agents are chatty.

One agent is attached and typed into; five more print about 200 kB/s each
with nobody watching them, so the engine's feeder parses their output on the
same loop. Budget (B2, end to end in the browser) p95 ≤ 8 ms; the engine's
share is asserted at 2× of it (§7d-7).
"""

from __future__ import annotations

import asyncio
import string
import time

import pytest
import pytest_asyncio

from tests.contract.harness import PythonHarness
from tests.contract.helpers import spawn
from tests.perf.stats import summary

pytestmark = [pytest.mark.asyncio, pytest.mark.perf]

HIDDEN = 5
RATE = 200_000  # bytes per second per hidden agent
WARMUP = 30
SAMPLES = 300


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = PythonHarness("unix")
    await h.start(tmp_path)
    try:
        yield h
    finally:
        await h.stop()


async def test_engine_echo_with_chatty_hidden_agents(harness, record):
    client = await harness.connect()
    agent = await spawn(client, harness.feat)
    hidden = [await spawn(client, harness.feat) for _ in range(HIDDEN)]
    driver = await harness.connect()
    drivers = [await driver.attach(h["session_id"]) for h in hidden]

    async def chatter() -> None:
        # Each hidden agent prints RATE / 4 bytes four times a second. The
        # driver's streams are not watching: the engine batches and feeds
        # that output as it would for a hidden tab.
        while True:
            for stream in drivers:
                driver.send_input(stream.stream_id, f"spew {RATE // 4}\r".encode())
            await asyncio.sleep(0.25)

    arrived = asyncio.Event()
    expected = [b""]

    def on_output(offset: int, data: bytes) -> None:
        if expected[0] and data.endswith(expected[0]):
            arrived.set()

    stream = await client.attach(agent["session_id"], on_output)
    task = asyncio.ensure_future(chatter())
    samples = []
    try:
        await asyncio.sleep(1.0)  # the hidden agents are in full flow
        letters = string.ascii_lowercase
        for i in range(WARMUP + SAMPLES):
            key = letters[i % len(letters)].encode()
            expected[0] = key
            arrived.clear()
            start = time.perf_counter()
            client.send_input(stream.stream_id, key)
            await asyncio.wait_for(arrived.wait(), 5)
            if i >= WARMUP:
                samples.append((time.perf_counter() - start) * 1000)
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    result = summary(samples)
    record("B2 engine echo, 5 hidden agents at 200 kB/s (ms)", **result)
    assert result["p95"] <= 16.0
