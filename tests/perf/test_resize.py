"""What a resize of the engine's screen costs with a full scrollback
(2,000 lines) and a full screen of styled output (§7d, PTY resize path).

A shrink moves at most a screen's worth of rows into the history (encoding
each, as a scroll does), so the cost depends on the screen, not on the
scrollback. Asserted: under 5 ms per resize (the loop budget).
"""

from __future__ import annotations

import time

import pyte
import pytest

from lazyagent.engine.screen import ScrollbackScreen
from tests.perf.stats import summary

pytestmark = pytest.mark.perf

ROWS, COLS = 50, 200


def _full_screen() -> tuple[ScrollbackScreen, pyte.Stream]:
    screen = ScrollbackScreen(COLS, ROWS, max_scrollback=2000)
    stream = pyte.Stream(screen)
    line = "".join(f"\x1b[3{i % 7};1mword{i}\x1b[0m " for i in range(20))
    stream.feed("\r\n".join(line for _ in range(2000 + ROWS)))
    assert len(screen.scrollback) == 2000
    return screen, stream


def test_resize_cost_with_a_full_scrollback(record):
    samples = {"shrink": [], "grow": []}
    for _ in range(30):
        screen, _stream = _full_screen()
        for rows in (20, ROWS, 10, ROWS):
            kind = "shrink" if rows < screen.lines else "grow"
            started = time.perf_counter()
            screen.resize(rows, COLS)
            samples[kind].append((time.perf_counter() - started) * 1000)
    shrink, grow = summary(samples["shrink"]), summary(samples["grow"])
    record("resize, 50x200 screen, 2,000-line scrollback: shrink (ms)", **shrink)
    record("resize, same: grow back (ms)", **grow)
    assert shrink["p95"] <= 5 and grow["p95"] <= 5
