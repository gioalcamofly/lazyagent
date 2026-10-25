"""Shared by the timing tests: the budget factor for slow or busy runners.

A timing test takes the median of several runs per round (one slow run or a
context switch does not count) and up to a few rounds, the first under budget
passing: noise only ever adds time, so a busy machine gets more chances and a
real regression fails them all."""

from __future__ import annotations

import os


def factor() -> float:
    """1 locally, 5 when ``CI`` is set (GitHub Actions sets it), or
    ``LAZYAGENT_BENCH_FACTOR`` when given."""
    given = os.environ.get("LAZYAGENT_BENCH_FACTOR")
    if given:
        return float(given)
    return 5.0 if os.environ.get("CI") else 1.0
