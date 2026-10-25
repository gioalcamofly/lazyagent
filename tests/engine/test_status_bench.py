"""status_list's cost: a repeated call with nothing changed reuses the last
pass (StatusProjection.fresh), and a pass stays linear in the rows.

Budgets, CI- and load-tolerant (tests/bench.py): the median of several runs
per round, up to ROUNDS rounds, the first under budget passing, the budget
times that round's allowance. A pass over 300 rows took ~29 ms before the
per-path indexes and the shared contexts (now ~15 ms); a cached call at 60
rows ~0.04 ms against a ~3-4 ms pass."""

from __future__ import annotations

import json

from lazyagent.engine import status as status_mod
from tests.bench import allowance, median_time
from tests.engine.status_bench import build, time_compute

ROUNDS = 5
CACHED_BUDGET = 0.0002  # a status_list call with no change, 60 rows
COMPUTE_BUDGET = 0.020  # a pass over 300 rows


def _started(n: int):
    engine = build(n)
    engine.status.start()
    # The first status_list starts the orchestrator's day counts (an overlay
    # change): the next one passes again, then the cache holds.
    engine.status.list()
    engine.status.list()
    return engine


def _dump(result) -> str:
    return json.dumps(result, sort_keys=True)


def _rounds(measure, budget: float) -> list[tuple[float, float]]:
    """(time, budget this round) per round, up to the first under budget."""
    rounds = []
    for _ in range(ROUNDS):
        allowed = budget * allowance()
        rounds.append((measure(), allowed))
        if rounds[-1][0] < allowed:
            break
    return rounds


def _check(capsys, what: str, rounds: list[tuple[float, float]]) -> None:
    with capsys.disabled():
        print(f"\nBENCH {what}: "
              + ", ".join(f"{t * 1e3:.3f} ms (budget {b * 1e3:.2f})" for t, b in rounds))
    took, allowed = rounds[-1]
    assert took < allowed, rounds


def test_a_repeated_status_list_reuses_the_pass(monkeypatch):
    engine = _started(5)
    try:
        status = engine.status
        first = status.list()
        calls = []
        monkeypatch.setattr(status, "compute", lambda: calls.append(1) or {})
        assert _dump(status.list()) == _dump(first)
        assert calls == []
    finally:
        engine.status.stop()


def test_the_cache_ends_with_any_change(monkeypatch):
    engine = _started(5)
    try:
        status = engine.status
        real = status.compute
        calls = []
        monkeypatch.setattr(status, "compute", lambda: calls.append(1) or real())
        status.list()
        assert calls == []

        engine.overlay.changed()
        status.list()
        assert len(calls) == 1
        status.list()
        assert len(calls) == 1

        engine.work_items.changed()
        status.list()
        assert len(calls) == 2

        status.clock.now += status_mod.COALESCE
        status.list()
        assert len(calls) == 3

        # A recompute pending (mark_dirty's timer): the pass runs now.
        status._handle = _Handle()
        status.list()
        assert len(calls) == 4 and status._handle is None
    finally:
        engine.status.stop()


class _Handle:
    def cancel(self) -> None:
        pass


def test_a_stopped_projection_always_recomputes(monkeypatch):
    engine = build(3)
    status = engine.status
    real = status.compute
    calls = []
    monkeypatch.setattr(status, "compute", lambda: calls.append(1) or real())
    status.list()
    status.list()
    assert len(calls) == 2


def test_the_cached_rows_are_what_a_pass_returns():
    engine = _started(60)
    try:
        status = engine.status
        cached = _dump(status.list(include_done=True))
        status.flush()
        status._passed_at = None  # the next call recomputes
        assert _dump(status.list(include_done=True)) == cached
    finally:
        engine.status.stop()


def test_a_repeated_status_list_at_60_rows_is_under_0_2_ms(capsys):
    engine = _started(60)
    try:
        status = engine.status
        status.list()
        rounds = _rounds(lambda: median_time(status.list, runs=101), CACHED_BUDGET)
        _check(capsys, "status_list cached, 60 rows, median of 101", rounds)
    finally:
        engine.status.stop()


def test_a_pass_over_300_rows_is_under_20_ms(capsys):
    engine = build(300)
    rounds = _rounds(lambda: time_compute(engine, runs=11), COMPUTE_BUDGET)
    _check(capsys, "status compute, 300 rows, median of 11", rounds)


def test_evolve_is_dataclasses_replace():
    import dataclasses
    import random

    import pytest

    from lazyagent.engine.status import evolve
    from tests.engine.rules.test_oracle import random_facts

    r = random.Random(7)
    for _ in range(200):
        facts, _ = random_facts(r)
        changes = {"actionables": (), "holder": "orchestrator", "activity_at": 5.0}
        assert evolve(facts, **changes) == dataclasses.replace(facts, **changes)
        assert evolve(facts) == facts and evolve(facts) is not facts
    with pytest.raises(TypeError):
        evolve(facts, nope=1)


def test_a_match_after_first_item_is_a_fresh_match():
    # The pass hands first_item's context on to the row's evaluation.
    import random

    from lazyagent.engine.rules.facts import RowContext
    from tests.engine.rules.test_oracle import NOW, random_facts

    catalog = status_mod._catalog()
    r = random.Random(11)
    for _ in range(2000):
        facts, last_change = random_facts(r)
        seen = RowContext(facts, NOW, last_change, 24)
        catalog.first_item(seen)
        fresh = RowContext(facts, NOW, last_change, 24)
        assert catalog.match(seen) == catalog.match(fresh)
        assert catalog.evaluate(seen) == catalog.evaluate(fresh)
