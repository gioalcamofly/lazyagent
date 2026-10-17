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
flush. The times are asserted at 2× (§7d-7). The flush is asserted in a way
a busy machine cannot fail: the attach parses nothing (the screen's parsed
offset does not move), and the engine's handling of the request, which
never yields, so its CPU time is how long it holds the loop, costs at most
twice what encoding the snapshot costs at that moment (measured on the same
screen) plus 10 ms. A flush of the backlog costs 70 ms to over a second;
encoding the snapshot is 3 ms on a quiet machine and 10-23 ms of CPU at
load 15, where the CPU is shared. The loop stall around the attach is
recorded too, but it also counts this test's client (same process), which
parses the reply and takes the backlog in.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from lazyagent.engine.server import _Connection
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


class Tail:
    """A stream's output, keeping only its newest bytes: what the test needs
    to see the burst end, at a constant cost per frame (joining every frame
    of a 15 MB run to look for the end cost the loop tens of ms each time,
    which the stall measurement then blamed on the engine)."""

    def __init__(self, keep: int = 4096) -> None:
        self.keep = keep
        self.tail = b""

    def __call__(self, offset: int, data: bytes) -> None:
        self.tail = (self.tail + data[-self.keep :])[-self.keep :]

    def mark(self) -> None:
        self.tail = b""


class Ticker:
    """The longest the loop went without running this task: in this
    thread's CPU time (the work the loop did in between, which is what a
    stall is; time the process spent preempted by a busy machine does not
    count) and in wall time (recorded, not asserted)."""

    def __init__(self) -> None:
        self.worst = 0.0
        self.worst_wall = 0.0
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        last, last_wall = time.thread_time(), time.perf_counter()
        while True:
            await asyncio.sleep(0.001)
            now, wall = time.thread_time(), time.perf_counter()
            self.worst = max(self.worst, now - last)
            self.worst_wall = max(self.worst_wall, wall - last_wall - 0.001)
            last, last_wall = now, wall

    def reset(self) -> None:
        self.worst = self.worst_wall = 0.0

    def stop(self) -> None:
        self._task.cancel()


async def _attach_ms(client, session_id: str) -> float:
    start = time.perf_counter()
    stream = await client.attach(session_id, Output())
    elapsed = (time.perf_counter() - start) * 1000
    await client.detach(stream.stream_id)
    return elapsed


async def test_attach_after_a_burst(harness, record, monkeypatch):
    blocks: list[float] = []
    handle = _Connection._handle

    async def timed_handle(conn, request):
        started = time.thread_time()
        await handle(conn, request)
        if request.method == "attach_stream":
            blocks.append((time.thread_time() - started) * 1000)

    monkeypatch.setattr(_Connection, "_handle", timed_handle)

    client = await harness.connect()
    agent = await spawn(client, harness.feat)
    session = harness.engine.find_session(agent["session_id"])
    watcher = await harness.connect()
    seen = Tail()
    driver = await watcher.attach(agent["session_id"], seen)
    other = await harness.connect()

    firsts, rounds, stalls, behind, feeder_stalls = [], [], [], [], []
    wall_stalls, encodes, engine_blocks = [], [], []
    dropped = [session.feed_dropped]
    ticker = Ticker()
    try:
        for _ in range(BURSTS):
            await wait_for_text(client, agent, "your turn>")
            seen.mark()
            ticker.reset()
            watcher.send_input(driver.stream_id, f"spew {BURST}\r".encode())
            # Until the last byte of the burst is out (in the ring).
            deadline = time.monotonic() + 60
            while b"done" not in seen.tail:
                assert time.monotonic() < deadline, "the burst never ended"
                await asyncio.sleep(0.005)
            # The loop during the burst: the feeder's turns (PR 7), no attach.
            feeder_stalls.append(ticker.worst * 1000)
            behind.append(session.ring.end_offset - _parsed(session))
            # What encoding this screen's snapshot costs right now (no flush:
            # it is the attach's own unavoidable work).
            started = time.thread_time()
            session.snapshot(flush=False)
            encode = (time.thread_time() - started) * 1000
            parsed = _parsed(session)
            ticker.reset()
            blocks.clear()
            firsts.append(await _attach_ms(other, agent["session_id"]))
            engine_blocks.append(blocks[0])
            stalls.append(ticker.worst * 1000)
            wall_stalls.append(ticker.worst_wall * 1000)
            encodes.append(encode)
            # Nothing was parsed for the attach (only the cut moved it on).
            assert _parsed(session) - parsed <= session.feed_dropped - dropped[-1]
            dropped.append(session.feed_dropped)
            for _ in range(ROUNDS):
                rounds.append(await _attach_ms(other, agent["session_id"]))
    finally:
        ticker.stop()

    first = summary(firsts)
    steady = summary(rounds)
    record(
        "B8 first attach right after a 5 MB burst (ms)",
        **first,
        loop_stall_max_cpu_ms=round(max(stalls), 2),
        loop_stall_max_wall_ms=round(max(wall_stalls), 2),
        engine_attach_cpu_ms=[round(b, 2) for b in engine_blocks],
        snapshot_encode_cpu_ms=[round(e, 2) for e in encodes],
        unparsed_kib=[round(b / 1024) for b in behind],
        burst_loop_stall_max_cpu_ms=round(max(feeder_stalls), 2),
    )
    record("B8 attach, the rounds after it (ms)", **steady)
    assert first["max"] <= 200
    assert steady["p95"] <= 100
    for block, encode in zip(engine_blocks, encodes, strict=True):
        assert block <= 2 * encode + 10, (block, encode)


def _parsed(session) -> int:
    """How far into the output the engine's screen has parsed."""
    parsed = getattr(session, "parsed_offset", None)
    if parsed is not None:
        return parsed
    return session.ring.end_offset - session._pending_chars
