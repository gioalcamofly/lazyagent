"""The budget (proposal 0014 "Performance"): a status pass over 100 rows
evaluates in under 2 ms.

Robust on slow or noisy runners: the 100 rows are fixed (seeded), the pass
is warmed up and timed with the garbage collector off, and a round takes the
**median of 31 passes** (one slow pass or a context switch does not count).
Noise only ever adds time, so up to 5 rounds run and the first under budget
passes (a busy machine, e.g. the rest of the suite in parallel, gets more
chances; a real regression fails all five). The budget is multiplied by
each round's allowance (tests/bench.py): the CI factor, or how slow the
machine runs right now. The old derive is timed alongside, for the record
only."""

from __future__ import annotations

import gc
import random
import statistics
import time

from lazyagent.engine.status import derive
from tests.bench import allowance
from tests.engine.rules import oracle
from tests.engine.rules.test_oracle import NOW, random_facts

ROWS = 100
RUNS = 31
ROUNDS = 5
BUDGET = 0.002


def median_pass(fn, rows) -> float:
    def one() -> float:
        start = time.perf_counter()
        for facts, last_change in rows:
            fn(facts, NOW, last_change, stalled_hours=24)
        return time.perf_counter() - start

    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(3):
            one()
        return statistics.median(one() for _ in range(RUNS))
    finally:
        if enabled:
            gc.enable()


def test_a_pass_over_100_rows_is_under_2_ms(capsys):
    r = random.Random(100)
    rows = [random_facts(r) for _ in range(ROWS)]
    rounds, budgets = [], []
    for _ in range(ROUNDS):
        budgets.append(BUDGET * allowance())
        rounds.append(median_pass(derive, rows))
        if rounds[-1] < budgets[-1]:
            break
    old = median_pass(oracle.derive, rows)
    with capsys.disabled():
        print(f"\nBENCH rules: {ROWS} rows, median of {RUNS}: catalog "
              f"{', '.join(f'{t * 1e3:.3f}' for t in rounds)} ms "
              f"(round{'s' if len(rounds) > 1 else ''}), old derive {old * 1e3:.3f} ms, "
              f"budget {budgets[-1] * 1e3:.1f} ms")
    assert rounds[-1] < budgets[-1], (rounds, budgets)
