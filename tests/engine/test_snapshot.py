"""ANSI snapshots of the headless screen: rendering one into an empty screen
of the same size rebuilds the original."""

from __future__ import annotations

import pyte
import pytest

from lazyagent.engine.screen import ScrollbackScreen, ansi_snapshot
from lazyagent.engine.session import TerminalSession


def _screen(text: str, cols: int = 40, rows: int = 8) -> ScrollbackScreen:
    screen = ScrollbackScreen(cols, rows)
    pyte.Stream(screen).feed(text)
    return screen


def _rebuilt(screen: ScrollbackScreen, *, then: str = "") -> ScrollbackScreen:
    copy = ScrollbackScreen(screen.columns, screen.lines)
    pyte.Stream(copy).feed(ansi_snapshot(screen) + then)
    return copy


def _cells(screen: ScrollbackScreen) -> list[list[tuple]]:
    return [
        [tuple(screen.buffer[y][x]) for x in range(screen.columns)]
        for y in range(screen.lines)
    ]


def assert_same(a: ScrollbackScreen, b: ScrollbackScreen) -> None:
    assert _cells(b) == _cells(a)
    assert list(b.scrollback) == list(a.scrollback)
    assert (b.cursor.x, b.cursor.y, b.cursor.hidden) == (
        a.cursor.x,
        a.cursor.y,
        a.cursor.hidden,
    )
    assert tuple(b.cursor.attrs) == tuple(a.cursor.attrs)
    assert b.mode == a.mode
    assert b.margins == a.margins


CASES = {
    "plain": "hello\r\nworld",
    "colors": (
        "\x1b[31mred\x1b[0m \x1b[1;44mbold on blue\x1b[0m\r\n"
        "\x1b[38;5;208m256\x1b[0m \x1b[38;2;10;20;30;48;2;200;100;0mtrue\x1b[0m\r\n"
        "\x1b[92mbright\x1b[0m \x1b[105mbg\x1b[0m"
    ),
    "flags": "\x1b[2mdim\x1b[22m \x1b[3mit\x1b[23m \x1b[4mun\x1b[24m \x1b[9mst\x1b[29m \x1b[7mrev\x1b[5mblink",
    "pen left set": "abc\x1b[1;32m",
    "background run": "x\x1b[41m     \x1b[0m",
    "wide chars": "日本語 ok\r\nnext",
    "full width row": "x" * 40 + "\r\nafter",
    "cursor mid screen": "one\r\ntwo\r\nthree\x1b[2;2H",
    "hidden cursor and modes": "\x1b[?25l\x1b[?2004h\x1b[?1000h\x1b[?1006h\x1b[?1hmodes",
    "alt screen": "main\r\n\x1b[?1049h\x1b[2J\x1b[Halt content\x1b[5;3H",
    "margins": "\x1b[2;6rinside\x1b[4;1Hrow4",
    "origin mode": "\x1b[3;7r\x1b[?6h\x1b[2;4Hom",
    "no autowrap": "\x1b[?7lnowrap",
    "insert mode": "\x1b[4hins",
    "scrolled": "".join(f"line {i}\r\n" for i in range(30)) + "tail",
    "scrolled colors": "".join(f"\x1b[3{i % 8}mline {i}\x1b[0m\r\n" for i in range(20)),
}


@pytest.mark.parametrize("text", CASES.values(), ids=CASES.keys())
def test_snapshot_rebuilds_the_screen(text):
    screen = _screen(text)
    assert_same(screen, _rebuilt(screen))


@pytest.mark.parametrize("text", CASES.values(), ids=CASES.keys())
def test_output_after_the_snapshot_lands_the_same(text):
    more = "\x1b[0mmore\r\n\x1b[33mand more"
    screen = _screen(text)
    copy = _rebuilt(screen, then=more)
    pyte.Stream(screen).feed(more)
    assert_same(screen, copy)


def test_scrollback_is_capped():
    screen = _screen("".join(f"{i}\r\n" for i in range(50)), rows=4)
    snapshot = ansi_snapshot(screen, max_scrollback=10)
    copy = ScrollbackScreen(screen.columns, screen.lines)
    pyte.Stream(copy).feed(snapshot)
    assert [line[0] for line in copy.scrollback] == [
        line[0] for line in list(screen.scrollback)[-10:]
    ]
    assert _cells(copy) == _cells(screen)


def test_snapshot_starts_with_a_reset():
    assert ansi_snapshot(_screen("x")).startswith("\x1bc")


@pytest.mark.asyncio
async def test_session_snapshot_offset_excludes_an_unfinished_character():
    session = TerminalSession("true")
    session._on_chunk("añ".encode()[:2])  # "a" plus the first byte of "ñ"
    offset, snapshot = session.snapshot()
    assert offset == 2 - 1
    copy = ScrollbackScreen(session.cols, session.rows)
    stream = pyte.Stream(copy)
    stream.feed(snapshot)
    stream.feed((session.ring.read(offset) + "ñ".encode()[1:]).decode())
    session._on_chunk("ñ".encode()[1:])
    session.flush()
    assert _cells(copy) == _cells(session.screen)


@pytest.mark.asyncio
async def test_resize_tells_size_listeners_the_offset():
    session = TerminalSession("true")
    seen = []
    remove = session.on_resize(
        lambda rows, cols, offset: seen.append((rows, cols, offset))
    )
    session._on_chunk(b"12345")
    session.resize(30, 100)
    session.resize(30, 100)
    remove()
    session.resize(10, 10)
    assert seen == [(30, 100, 5)]
