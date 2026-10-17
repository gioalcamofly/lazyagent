"""B3: engine echo over the v1 socket with no UI (§7d-1).

A key sent as an input frame to the fake agent, until the output frame
that redraws it comes back. Budget idle p95 ≤ 3 ms; asserted at 2× (§7d-7).
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


async def test_engine_echo(harness, record):
    client = await harness.connect()
    agent = await spawn(client, harness.feat)
    arrived = asyncio.Event()
    expected = [b""]

    def on_output(offset: int, data: bytes) -> None:
        if expected[0] and data.endswith(expected[0]):
            arrived.set()

    stream = await client.attach(agent["session_id"], on_output)
    samples = []
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
        await asyncio.sleep(0.002)
    result = summary(samples)
    record("B3 engine echo, idle (ms)", **result)
    assert result["p95"] <= 6.0
