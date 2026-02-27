"""Tests for ScrollbackScreen and ScrollableTerminal."""
from __future__ import annotations

from unittest.mock import MagicMock

import pyte
from pyte.screens import Char

from lazyagent.widgets.scrollable_terminal import ScrollableTerminal, ScrollbackScreen


# ---------------------------------------------------------------------------
# ScrollbackScreen tests
# ---------------------------------------------------------------------------


class TestScrollbackScreen:
    def test_index_captures_top_line(self):
        """When cursor is at bottom margin and index() fires, top row is saved."""
        screen = ScrollbackScreen(80, 5)
        stream = pyte.Stream(screen)

        # Fill all 5 lines then push one more to trigger scrolling
        for i in range(6):
            stream.feed(f"line {i}\n")

        # First line ("line 0") should be in scrollback
        assert len(screen.scrollback) >= 1
        first_line = "".join(
            screen.scrollback[0].get(x, screen.default_char).data
            for x in range(80)
        ).rstrip()
        assert first_line.startswith("line 0")

    def test_index_no_capture_when_not_at_bottom(self):
        """No scrollback capture when cursor isn't at the bottom margin."""
        screen = ScrollbackScreen(80, 24)
        stream = pyte.Stream(screen)

        # Write a few lines without filling the screen
        stream.feed("hello\nworld\n")
        assert len(screen.scrollback) == 0

    def test_scrollback_max_size(self):
        """Deque maxlen is respected."""
        screen = ScrollbackScreen(80, 5, max_scrollback=3)
        stream = pyte.Stream(screen)

        # Push 10 lines through a 5-line screen → 5 lines scroll off
        for i in range(10):
            stream.feed(f"line {i}\n")

        # But only keep 3 (maxlen)
        assert len(screen.scrollback) == 3

    def test_set_margins_strips_private(self):
        """TERM=linux compat: private kwarg is stripped."""
        screen = ScrollbackScreen(80, 24)
        # Should not raise
        screen.set_margins(0, 23, private=True)


# ---------------------------------------------------------------------------
# ScrollableTerminal tests (unit, no real PTY)
# ---------------------------------------------------------------------------


def _make_scrollable_terminal() -> ScrollableTerminal:
    """Create a ScrollableTerminal without starting the emulator."""
    terminal = ScrollableTerminal.__new__(ScrollableTerminal)
    terminal.command = "echo test"
    terminal.default_colors = "system"
    terminal.ncol = 80
    terminal.nrow = 5
    terminal.mouse_tracking = False
    terminal.emulator = None
    terminal.send_queue = None
    terminal.recv_queue = None
    terminal.recv_task = None
    terminal._stopped = False
    terminal._screen = ScrollbackScreen(80, 5)
    terminal.stream = pyte.Stream(terminal._screen)
    terminal.ctrl_keys = {}
    return terminal


class TestVirtualSizeCalculation:
    def test_total_lines_no_scrollback(self):
        """Total lines equals screen lines when no scrollback."""
        t = _make_scrollable_terminal()
        total = len(t._screen.scrollback) + t._screen.lines
        assert total == 5

    def test_total_lines_with_scrollback(self):
        """Total lines includes scrollback lines."""
        t = _make_scrollable_terminal()
        # Push lines through to build scrollback
        for i in range(10):
            t.stream.feed(f"line {i}\n")
        scrollback_len = len(t._screen.scrollback)
        assert scrollback_len > 0
        total = scrollback_len + t._screen.lines
        assert total == scrollback_len + 5


class TestOnStdoutHook:
    def test_on_stdout_default_is_noop(self):
        """Base class _on_stdout does nothing (no error)."""
        t = _make_scrollable_terminal()
        t._on_stdout("hello world")  # Should not raise


class TestRowToStrip:
    def test_render_default_char_row(self):
        """Rendering a row of default chars produces a strip."""
        from unittest.mock import patch
        from rich.console import Console

        t = _make_scrollable_terminal()
        mock_app = MagicMock()
        mock_app.console = Console()

        with patch.object(type(t), "app", new_callable=lambda: property(lambda self: mock_app)):
            row = t._screen.buffer[0]  # Default empty row
            strip = t._row_to_strip(row, 80)
            assert strip is not None
            assert strip.cell_length > 0


class TestStyleHelpers:
    def test_char_style_cmp_same(self):
        """Identical chars should compare equal."""
        c1 = Char("a", "default", "default", False, False, False, False, False, False)
        c2 = Char("b", "default", "default", False, False, False, False, False, False)
        assert ScrollableTerminal._char_style_cmp(c1, c2) is True

    def test_char_style_cmp_different_fg(self):
        c1 = Char("a", "red", "default", False, False, False, False, False, False)
        c2 = Char("a", "blue", "default", False, False, False, False, False, False)
        assert ScrollableTerminal._char_style_cmp(c1, c2) is False

    def test_detect_color_brown(self):
        assert ScrollableTerminal._detect_color("brown") == "yellow"

    def test_detect_color_brightblack(self):
        assert ScrollableTerminal._detect_color("brightblack") == "#808080"

    def test_detect_color_hex(self):
        assert ScrollableTerminal._detect_color("ff0000") == "#ff0000"

    def test_detect_color_passthrough(self):
        assert ScrollableTerminal._detect_color("red") == "red"
        assert ScrollableTerminal._detect_color("default") == "default"
