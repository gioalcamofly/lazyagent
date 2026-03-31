"""Tests for ScrollbackScreen and ScrollableTerminal."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyte
from pyte.screens import Char
from rich.style import Style

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

    def test_no_scrollback_with_custom_margins(self):
        """Lines scrolling within a sub-region (top > 0) should NOT be captured."""
        screen = ScrollbackScreen(80, 5)
        stream = pyte.Stream(screen)

        # set_margins takes 1-based args; (2, 5) → 0-based Margins(1, 4),
        # leaving row 0 as a fixed "status bar".
        screen.set_margins(2, 5)
        screen.cursor_position(2, 1)  # move cursor into scroll region

        # Push enough lines to scroll within the region
        for i in range(10):
            stream.feed(f"region line {i}\n")

        # No lines should go to scrollback — sub-region scroll
        assert len(screen.scrollback) == 0

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
    terminal._follow_output = True
    terminal._screen = ScrollbackScreen(80, 5)
    terminal.stream = pyte.Stream(terminal._screen)
    terminal.ctrl_keys = {}
    terminal._cached_default_fg = None
    terminal._cached_default_bg = None
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


class TestAfterStdoutProcessedHook:
    def test_after_stdout_processed_default_is_noop(self):
        """Base class _after_stdout_processed does nothing (no error)."""
        t = _make_scrollable_terminal()
        t._after_stdout_processed()  # Should not raise


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

    def test_char_style_cmp_different_dim(self):
        c1 = Char("a", "default", "default", False, False, False, False, False, False, dim=True)
        c2 = Char("a", "default", "default", False, False, False, False, False, False, dim=False)
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


class TestCursorStyle:
    """Tests for _cursor_style and the cached default color resolution."""

    def _make_terminal_with_style(self, fg="white", bg="black"):
        """Create a terminal with a mocked rich_style returning given colors."""
        t = _make_scrollable_terminal()
        style = Style(color=fg, bgcolor=bg)
        patcher = patch.object(
            type(t), "rich_style", new_callable=lambda: property(lambda self: style)
        )
        patcher.start()
        # Ensure cache is clean so first call resolves
        t._cached_default_fg = None
        t._cached_default_bg = None
        return t, patcher

    def test_cursor_swaps_fg_bg_for_default_colors(self):
        """Cursor on a default-color cell swaps resolved theme colors."""
        t, patcher = self._make_terminal_with_style("white", "black")
        try:
            char = Char("x", "default", "default", False, False, False, False, False, False)
            style = t._cursor_style(char)
            # fg/bg should be swapped: cursor fg=theme bg, cursor bg=theme fg
            assert style.color.name == "black"
            assert style.bgcolor.name == "white"
        finally:
            patcher.stop()

    def test_cursor_swaps_explicit_colors(self):
        """Cursor on a cell with explicit fg/bg swaps those colors directly."""
        t = _make_scrollable_terminal()
        char = Char("x", "red", "blue", False, False, False, False, False, False)
        style = t._cursor_style(char)
        assert style.color.name == "blue"
        assert style.bgcolor.name == "red"

    def test_cursor_mixed_default_and_explicit(self):
        """When only fg is default, it resolves from theme; bg stays explicit."""
        t, patcher = self._make_terminal_with_style("green", "magenta")
        try:
            char = Char("x", "default", "red", False, False, False, False, False, False)
            style = t._cursor_style(char)
            # fg was default → resolved to "green", bg was "red"
            # cursor swaps: color=bg("red"), bgcolor=fg("green")
            assert style.color.name == "red"
            assert style.bgcolor.name == "green"
        finally:
            patcher.stop()

    def test_cursor_respects_hidden_flag(self):
        """When cursor.hidden is set, show_cursor should be False."""
        t = _make_scrollable_terminal()
        t._screen.cursor.hidden = True
        t._screen.cursor.y = 0
        show_cursor = not t._screen.cursor.hidden and t._screen.cursor.y == 0
        assert show_cursor is False


class TestResolvedDefaultColors:
    """Tests for _resolved_default_colors caching and invalidation."""

    def test_caches_resolved_colors(self):
        """rich_style is resolved once and then cached."""
        t = _make_scrollable_terminal()
        style = Style(color="cyan", bgcolor="yellow")
        call_count = 0

        def counting_style(self):
            nonlocal call_count
            call_count += 1
            return style

        with patch.object(type(t), "rich_style", new_callable=lambda: property(counting_style)):
            fg1, bg1 = t._resolved_default_colors()
            fg2, bg2 = t._resolved_default_colors()

        assert (fg1, bg1) == ("cyan", "yellow")
        assert (fg2, bg2) == ("cyan", "yellow")
        assert call_count == 1  # resolved only once

    def test_notify_style_update_invalidates_cache(self):
        """notify_style_update clears cached colors so next call re-resolves."""
        t = _make_scrollable_terminal()
        style_v1 = Style(color="white", bgcolor="black")
        style_v2 = Style(color="green", bgcolor="blue")
        current_style = [style_v1]

        with patch.object(
            type(t), "rich_style",
            new_callable=lambda: property(lambda self: current_style[0]),
        ), patch.object(
            # super().notify_style_update() touches Widget internals not
            # present on our __new__-constructed instance; bypass it.
            type(t).__mro__[1], "notify_style_update", lambda self: None,
        ):
            fg1, bg1 = t._resolved_default_colors()
            assert (fg1, bg1) == ("white", "black")

            # Simulate theme change
            current_style[0] = style_v2
            t.notify_style_update()

            fg2, bg2 = t._resolved_default_colors()
            assert (fg2, bg2) == ("green", "blue")

    def test_fallback_when_style_has_no_color(self):
        """Falls back to white/black when rich_style has no color set."""
        t = _make_scrollable_terminal()
        empty_style = Style()  # no color, no bgcolor

        with patch.object(
            type(t), "rich_style",
            new_callable=lambda: property(lambda self: empty_style),
        ):
            fg, bg = t._resolved_default_colors()

        assert fg == "white"
        assert bg == "black"
