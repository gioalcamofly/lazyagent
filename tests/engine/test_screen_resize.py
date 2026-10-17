"""ScrollbackScreen.resize behaves like a terminal: a shrink drops rows
below the cursor first and scrolls into the history only the rows above the
cursor that must leave; nothing is lost, nothing is duplicated."""

from __future__ import annotations

import pyte
import pytest

from lazyagent.engine.screen import ScrollbackScreen, ansi_snapshot


def _screen(rows: int = 10, cols: int = 20, lines: int = 0, alt: bool = False):
    screen = ScrollbackScreen(cols, rows)
    stream = pyte.Stream(screen)
    if alt:
        stream.feed("\x1b[?1049h\x1b[H")
    if lines:
        stream.feed("\r\n".join(f"line {i}" for i in range(lines)))
    return screen, stream


def _rows(screen: ScrollbackScreen) -> list[str]:
    return [screen.screen_text(y) for y in range(screen.lines)]


def _history(screen: ScrollbackScreen) -> list[str]:
    return [screen.scrollback_text(i) for i in range(len(screen.scrollback))]


def _everything(screen: ScrollbackScreen) -> list[str]:
    return [line for line in _history(screen) + _rows(screen) if line]


def test_cursor_at_the_top_keeps_every_row_and_history_untouched():
    screen, _ = _screen(lines=3)  # rows 0-2, cursor on row 2
    screen.resize(4, 20)
    assert _rows(screen)[:3] == ["line 0", "line 1", "line 2"]
    assert _history(screen) == []
    assert screen.cursor.y == 2


def test_cursor_in_the_middle_scrolls_only_what_must_leave():
    screen, _ = _screen(lines=8)  # cursor on row 7 of 10
    screen.resize(5, 20)
    assert _history(screen) == ["line 0", "line 1", "line 2"]
    assert _rows(screen) == ["line 3", "line 4", "line 5", "line 6", "line 7"]
    assert screen.cursor.y == 4


def test_cursor_at_the_bottom_scrolls_the_top_rows_into_history():
    screen, _ = _screen(lines=15)  # 5 already in history, cursor on row 9
    screen.resize(4, 20)
    assert _history(screen) == [f"line {i}" for i in range(11)]
    assert _rows(screen) == ["line 11", "line 12", "line 13", "line 14"]
    assert screen.cursor.y == 3


def test_output_after_a_shrink_lands_at_the_cursor():
    screen, stream = _screen(lines=8)
    screen.resize(5, 20)
    stream.feed("\r\nafter")
    assert _rows(screen)[-1] == "after"
    assert _everything(screen) == [f"line {i}" for i in range(8)] + ["after"]


def test_the_alternate_screen_keeps_no_history():
    """A full-screen program redraws on SIGWINCH; its rows are not history."""
    screen, _ = _screen(lines=8, alt=True)
    screen.resize(5, 20)
    assert _history(screen) == []
    assert _rows(screen) == ["line 3", "line 4", "line 5", "line 6", "line 7"]


def test_columns_shrink_and_grow_with_the_rows():
    screen, _ = _screen(lines=8)
    screen.resize(5, 4)
    assert (screen.lines, screen.columns) == (5, 4)
    assert _rows(screen)[0] == "line"
    screen.resize(10, 20)
    assert (screen.lines, screen.columns) == (10, 20)


@pytest.mark.parametrize("lines", [3, 8, 25])
def test_repeated_shrink_and_grow_never_loses_or_duplicates_a_row(lines):
    screen, stream = _screen(lines=lines)
    for rows in (6, 10, 2, 9, 1, 10, 4, 10):
        screen.resize(rows, 20)
    assert _everything(screen) == [f"line {i}" for i in range(lines)]
    stream.feed("\r\nnext")
    assert _everything(screen)[-1] == "next"


def test_margins_are_reset_to_the_new_size():
    screen, stream = _screen(lines=8)
    stream.feed("\x1b[2;5r")  # a scroll region
    screen.resize(5, 20)
    assert screen.margins is None or (screen.margins.top, screen.margins.bottom) == (
        0,
        4,
    )


def test_a_snapshot_after_a_shrink_rebuilds_the_same_screen():
    screen, _ = _screen(lines=30)
    screen.resize(6, 20)
    copy = ScrollbackScreen(20, 6)
    pyte.Stream(copy).feed(ansi_snapshot(screen))
    assert _rows(copy) == _rows(screen)
    assert _history(copy)[-len(_history(screen)) :] == _history(screen)


@pytest.mark.asyncio
async def test_a_session_snapshot_after_shrinks_matches_read_agent_output():
    """What a client rebuilds from a snapshot taken after the view shrank
    and grew is what read_agent_output (tail_lines) reports."""
    from tests.engine.test_session import _agent

    session, _, _ = _agent(rows=24, cols=80)
    session._on_chunk(
        "".join(f"\x1b[3{i % 7}mrow {i}\x1b[0m\r\n" for i in range(60)).encode()
    )
    session._on_chunk(b"prompt> ")
    for rows, cols in ((10, 60), (24, 80), (5, 40), (24, 80)):
        session.resize(rows, cols)
    offset, snapshot = session.snapshot()
    assert offset == session.ring.end_offset
    copy = ScrollbackScreen(session.cols, session.rows)
    pyte.Stream(copy).feed(snapshot)
    rebuilt = [line for line in _everything(copy)]
    engine = [line for line in session.tail_lines(session.total_lines) if line]
    assert rebuilt[-len(engine) :] == engine
    assert engine[-1] == "prompt>" and "row 0" in engine[0]
