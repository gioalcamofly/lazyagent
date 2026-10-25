"""Shared by the timing tests: how much slack a slow or busy runner gets.

A timing test takes the median of several runs per round (one slow run or a
context switch does not count) and up to a few rounds, the first under budget
passing: noise only ever adds time, so a busy machine gets more chances and a
real regression fails them all. The budget is multiplied by
:func:`allowance`: the CI factor, or how slow the machine is right now (a
fixed workload timed in the same round against its time on an idle desktop
core), whichever is larger. A loaded machine slows both alike; an idle one
keeps the budget as written."""

from __future__ import annotations

import gc
import os
import statistics
import time

# reference_work's median on an idle desktop core, in seconds.
REFERENCE_IDLE = 0.009


def factor() -> float:
    """1 locally, 5 when ``CI`` is set (GitHub Actions sets it), or
    ``LAZYAGENT_BENCH_FACTOR`` when given."""
    given = os.environ.get("LAZYAGENT_BENCH_FACTOR")
    if given:
        return float(given)
    return 5.0 if os.environ.get("CI") else 1.0


def median_time(fn, runs: int = 21, warmup: int = 3) -> float:
    """The median of ``runs`` calls of ``fn``, in seconds, the garbage
    collector off."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(warmup):
            fn()
        times = []
        for _ in range(runs):
            started = time.perf_counter()
            fn()
            times.append(time.perf_counter() - started)
        return statistics.median(times)
    finally:
        if enabled:
            gc.enable()


def reference_work() -> None:
    """A fixed pure-Python workload, ~9 ms on an idle desktop core."""
    counts: dict[int, int] = {}
    for i in range(100_000):
        counts[i % 977] = counts.get(i % 977, 0) + i


def slowness() -> float:
    """How many times slower than idle the machine runs right now (1 at
    least): reference_work's median of 5 over :data:`REFERENCE_IDLE`."""
    return max(1.0, median_time(reference_work, runs=5, warmup=1) / REFERENCE_IDLE)


def allowance() -> float:
    """What a budget is multiplied by this round."""
    return max(factor(), slowness())
