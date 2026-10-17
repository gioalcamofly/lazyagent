"""B8: open a session over the v1 socket (§7d-1): ``attach_stream`` with a
snapshot, also right after a burst of hidden output, and what that does to
the engine's loop.

An agent nobody watches prints 5 MB as fast as it can (its screen falls
behind: the feeder parses hidden output in slices, and keeps only the
newest part of a backlog it is far behind on). The moment the last byte is
out, a second client attaches with a snapshot: that first attach is the
worst case. Then 20 more attach/detach rounds. Meanwhile a ticker on the
loop records how long the loop went without turning.

Budget: p95 ≤ 50 ms, worst ≤ 100 ms; the loop never blocked > 5 ms by a
flush. Asserted at 2× (§7d-7).
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from tests.contract.harness import PythonHarness
from tests.contract.helpers import Output, spawn, wait_for_text
from tests.perf.stats import summary

pytestmark = [pytest.mark.asyncio, pytest.mark.perf]

BURST = 5_000_000
BURSTS = 3
ROUNDS = 20


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = PythonHarness("unix")
    await h.start(tmp_path)
    try:
        yield h
    finally:
        await h.stop()


class Ticker:
    """The longest the loop went without running this task."""

    def __init__(self) -> None:
        self.worst = 0.0
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            self.worst = max(self.worst, now - last - 0.001)
            last = now

    def reset(self) -> None:
        self.worst = 0.0

    def stop(self) -> None:
        self._task.cancel()


async def _attach_ms(client, session_id: str) -> float:
    start = time.perf_counter()
    stream = await client.attach(session_id, Output())
    elapsed = (time.perf_counter() - start) * 1000
    await client.detach(stream.stream_id)
    return elapsed


async def test_attach_after_a_burst(harness, record):
    client = await harness.connect()
    agent = await spawn(client, harness.feat)
    session = harness.engine.find_session(agent["session_id"])
    watcher = await harness.connect()
    seen = Output()
    driver = await watcher.attach(agent["session_id"], seen)
    other = await harness.connect()

    firsts, rounds, stalls, behind, feeder_stalls = [], [], [], [], []
    ticker = Ticker()
    try:
        for _ in range(BURSTS):
            await wait_for_text(client, agent, "your turn>")
            mark = len(seen.data)
            ticker.reset()
            watcher.send_input(driver.stream_id, f"spew {BURST}\r".encode())
            # Until the last byte of the burst is out (in the ring).
            deadline = time.monotonic() + 60
            while b"done" not in seen.data[mark:][-4096:]:
                assert time.monotonic() < deadline, "the burst never ended"
                await asyncio.sleep(0.005)
            # The loop during the burst: the feeder's turns (PR 7), no attach.
            feeder_stalls.append(ticker.worst * 1000)
            behind.append(session.ring.end_offset - _parsed(session))
            ticker.reset()
            firsts.append(await _attach_ms(other, agent["session_id"]))
            stalls.append(ticker.worst * 1000)
            for _ in range(ROUNDS):
                rounds.append(await _attach_ms(other, agent["session_id"]))
    finally:
        ticker.stop()

    first = summary(firsts)
    steady = summary(rounds)
    record(
        "B8 first attach right after a 5 MB burst (ms)",
        **first,
        loop_stall_max_ms=round(max(stalls), 2),
        unparsed_kib=[round(b / 1024) for b in behind],
        burst_loop_stall_max_ms=round(max(feeder_stalls), 2),
    )
    record("B8 attach, the rounds after it (ms)", **steady)
    assert first["max"] <= 200
    assert steady["p95"] <= 100
    assert max(stalls) <= 10


def _parsed(session) -> int:
    """How far into the output the engine's screen has parsed."""
    parsed = getattr(session, "parsed_offset", None)
    if parsed is not None:
        return parsed
    return session.ring.end_offset - session._pending_chars
