"""Tests for ScrollbackScreen and ScrollableTerminal."""
from __future__ import annotations

import gc
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
        assert screen.scrollback_text(0).startswith("line 0")

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

    def test_oldest_line_is_evicted_first(self):
        """Eviction is FIFO — the surviving lines are the most recent ones."""
        screen = ScrollbackScreen(80, 2, max_scrollback=3)
        stream = pyte.Stream(screen)

        for i in range(8):
            stream.feed(f"line {i}\r\n")

        texts = [screen.scrollback_text(i) for i in range(len(screen.scrollback))]
        assert texts == ["line 4", "line 5", "line 6"]


class TestScrollbackEncoding:
    """The compact ``(text, runs)`` representation of scrolled-off lines."""

    @staticmethod
    def _screen_with(*feeds: str, columns: int = 20) -> ScrollbackScreen:
        """Feed lines through a 2-row screen so the first one scrolls off."""
        screen = ScrollbackScreen(columns, 2)
        stream = pyte.Stream(screen)
        for chunk in feeds:
            stream.feed(chunk)
        return screen

    def test_round_trip_text_and_styles(self):
        """Text and per-run styles survive the trip into scrollback."""
        screen = self._screen_with("\x1b[31mred\x1b[0m plain\r\n", "second\r\n")

        text, runs = screen.scrollback[0]
        assert text == "red plain"
        assert [(start, end) for start, end, _ in runs] == [(0, 3), (3, 9)]
        assert runs[0][2][0] == "red"  # fg of the styled run
        assert runs[1][2][0] == "default"

    def test_attributes_round_trip(self):
        """Bold/underline/reverse and background survive too."""
        screen = self._screen_with("\x1b[1;4;7;44mx\r\n", "second\r\n")

        _, runs = screen.scrollback[0]
        fg, bg, bold, italics, underscore, _strike, reverse, _blink = runs[0][2][:8]
        assert (bg, bold, underscore, reverse) == ("blue", True, True, True)

    def test_trailing_blanks_are_dropped(self):
        """Unwritten/blank tail cells are not stored — the renderer re-pads."""
        screen = self._screen_with("hi\r\n", "second\r\n")

        text, runs = screen.scrollback[0]
        assert text == "hi"
        assert len(runs) == 1

    def test_gaps_are_filled_with_default_cells(self):
        """A cursor jump leaves unwritten cells; they encode as default blanks."""
        screen = self._screen_with("a\x1b[1;5Hb\r\n", "second\r\n")

        text, _ = screen.scrollback[0]
        assert text == "a   b"

    def test_blank_line_encodes_empty(self):
        """A line with nothing on it costs a bare empty entry."""
        screen = self._screen_with("\r\n", "second\r\n")
        assert screen.scrollback[0] == ("", ())

    def test_scrollback_entries_are_gc_untracked(self):
        """Scrollback must never hold GC-tracked containers.

        pyte's ``Char`` is a NamedTuple *subclass*, and CPython only untracks
        tuples that pass ``PyTuple_CheckExact`` — so storing Chars keeps every
        cell in scrollback tracked forever, and each gen2 collection walks all
        of them (measured: ~560 ms pauses with six full terminals).  Plain
        tuples of primitives get untracked and cost nothing.

        This asserts the property, not the implementation: swapping the tuples
        for a NamedTuple or dataclass "for readability" would reintroduce the
        pauses silently, and this test is what catches it.
        """
        screen = ScrollbackScreen(40, 2)
        stream = pyte.Stream(screen)
        for i in range(30):
            stream.feed(f"\x1b[3{i % 8}mline {i}\x1b[0m\r\n")
        assert len(screen.scrollback) > 10

        # Untracking cascades one nesting level per collection (a tuple is
        # only untracked once its items already are), so collect a few times.
        for _ in range(4):
            gc.collect()

        for entry in screen.scrollback:
            assert not gc.is_tracked(entry)
            text, runs = entry
            assert not gc.is_tracked(runs)
            for run in runs:
                assert not gc.is_tracked(run)
                assert not gc.is_tracked(run[2])
                # Exact tuples only — a subclass would never untrack.
                assert type(run) is tuple
                assert type(run[2]) is tuple

    def test_style_keys_are_interned(self):
        """Lines sharing a style share one key tuple, not one per line."""
        screen = ScrollbackScreen(40, 2)
        stream = pyte.Stream(screen)
        for i in range(20):
            stream.feed(f"\x1b[31mline {i}\x1b[0m\r\n")

        keys = {id(run[2]) for _, runs in screen.scrollback for run in runs}
        assert len(keys) == len(screen._style_key_intern)
        assert len(keys) <= 2  # "red" and the default trailing style


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
    terminal._sel_start = None
    terminal._sel_end = None
    terminal._is_selecting = False
    terminal._cached_selection = None
    terminal._cached_selection_style = None
    terminal._screen = ScrollbackScreen(80, 5)
    terminal.stream = pyte.Stream(terminal._screen)
    terminal.ctrl_keys = {}
    terminal._cached_default_colors = None
    terminal._style_cache = {}
    terminal._last_cursor = (0, 0, False)
    terminal._last_scrollback_len = 0
    terminal._frame_style = None
    terminal._frame_width = -1
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


def _cells(strip) -> list[tuple[str, Style]]:
    """Flatten a Strip into one (character, style) pair per rendered cell."""
    return [(ch, segment.style) for segment in strip for ch in segment.text]


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

    def test_row_is_run_length_encoded(self):
        """Cells sharing a style collapse into a single segment."""
        t = _make_scrollable_terminal()
        t.stream.feed("\x1b[31mred\x1b[0mplain")

        strip = t._row_to_strip(t._screen.buffer[0], 80)
        segments = list(strip)

        assert segments[0].text == "red"
        assert segments[0].style.color.name == "red"
        # "plain" plus the default-styled remainder of the row
        assert segments[1].text.startswith("plain")
        assert len(segments) == 2

    def test_styles_match_source_chars(self):
        """Every rendered cell carries the style of its own pyte Char."""
        t = _make_scrollable_terminal()
        t.stream.feed("\x1b[1;31ma\x1b[0m\x1b[4;32mb\x1b[0mc")

        cells = _cells(t._row_to_strip(t._screen.buffer[0], 80))

        assert cells[0][0] == "a"
        assert cells[0][1].color.name == "red"
        assert cells[0][1].bold is True
        assert cells[1][0] == "b"
        assert cells[1][1].color.name == "green"
        assert cells[1][1].underline is True
        assert cells[2][0] == "c"
        assert cells[2][1].bold is not True

    def test_cursor_style_overrides_char_style(self):
        """The cursor cell keeps its char attributes but swaps fg/bg."""
        t = _make_scrollable_terminal()
        t.stream.feed("\x1b[1;31;44mabc")
        t._screen.cursor.x = 1
        t._screen.cursor.y = 0

        cells = _cells(t._row_to_strip(t._screen.buffer[0], 80, show_cursor=True))

        # Cursor is applied after character styles: fg/bg swapped, bold kept.
        assert cells[1][0] == "b"
        assert cells[1][1].color.name == "blue"
        assert cells[1][1].bgcolor.name == "red"
        assert cells[1][1].bold is True
        # Neighbours are untouched
        assert cells[0][1].color.name == "red"
        assert cells[2][1].color.name == "red"

    def test_cursor_at_last_column(self):
        """A cursor on the final column still renders swapped."""
        t = _make_scrollable_terminal()
        t._screen.cursor.x = 79
        t._screen.cursor.y = 0
        t._cached_default_colors = ("white", "black")

        cells = _cells(t._row_to_strip(t._screen.buffer[0], 80, show_cursor=True))

        assert cells[79][1].color.name == "black"
        assert cells[79][1].bgcolor.name == "white"

    def test_selection_style_applied_over_char_style(self):
        """Selected cells combine the selection style on top of char style."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("\x1b[31mhello world")
        t._cached_selection_style = Style(bgcolor="blue")
        t._sel_start = Offset(0, 0)
        t._sel_end = Offset(5, 0)
        t._update_cached_selection()

        cells = _cells(t._row_to_strip(t._screen.buffer[0], 80, virtual_y=0))

        # Inside the selection: char fg preserved, selection bg applied
        assert cells[0][1].color.name == "red"
        assert cells[0][1].bgcolor.name == "blue"
        assert cells[4][1].bgcolor.name == "blue"
        # Outside the selection: untouched
        assert cells[5][1].bgcolor.name == "default"

    def test_selection_and_cursor_on_same_row(self):
        """Selection wins over the cursor where they overlap, as before."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("hello")
        t._cached_default_colors = ("white", "black")
        t._cached_selection_style = Style(bgcolor="blue")
        t._screen.cursor.x = 1
        t._screen.cursor.y = 0
        t._sel_start = Offset(0, 0)
        t._sel_end = Offset(3, 0)
        t._update_cached_selection()

        cells = _cells(
            t._row_to_strip(t._screen.buffer[0], 80, show_cursor=True, virtual_y=0)
        )

        # Cursor cell is inside the selection — selection bg is applied last
        assert cells[1][1].bgcolor.name == "blue"
        # ...but the cursor's fg swap survives (selection only sets bgcolor)
        assert cells[1][1].color.name == "black"


class TestRenderScrollbackLine:
    def test_matches_live_screen_rendering(self):
        """A line renders the same before and after it scrolls off."""
        t = _make_scrollable_terminal()
        t.stream.feed("\x1b[1;31mred\x1b[0m \x1b[4;32mgreen\x1b[0m tail")

        live = _cells(t._row_to_strip(t._screen.buffer[0], 80))

        # Push the line into scrollback
        for _ in range(6):
            t.stream.feed("\r\n")
        assert t._screen.scrollback_text(0) == "red green tail"

        scrolled = _cells(t._render_scrollback_line(0, 80, -1))
        assert scrolled == live

    def test_short_line_is_padded_to_full_width(self):
        """Dropped trailing blanks come back as default-styled padding."""
        t = _make_scrollable_terminal()
        t.stream.feed("hi")
        for _ in range(6):
            t.stream.feed("\r\n")

        cells = _cells(t._render_scrollback_line(0, 80, -1))
        assert len(cells) == 80
        assert "".join(ch for ch, _ in cells).rstrip() == "hi"
        assert cells[40][1] == t._char_rich_style(t._screen.default_char)

    def test_selection_highlights_scrollback_line(self):
        """Selection spans apply to scrollback rows as well as live ones."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("\x1b[31mhello world")
        for _ in range(6):
            t.stream.feed("\r\n")
        t._cached_selection_style = Style(bgcolor="blue")
        t._sel_start = Offset(0, 0)
        t._sel_end = Offset(5, 0)
        t._update_cached_selection()

        cells = _cells(t._render_scrollback_line(0, 80, 0))

        assert cells[0][1].color.name == "red"
        assert cells[0][1].bgcolor.name == "blue"
        assert cells[4][1].bgcolor.name == "blue"
        assert cells[5][1].bgcolor.name != "blue"


class TestStyleCache:
    def test_style_is_memoized_per_key(self):
        """The same style key resolves to the identical Style object."""
        t = _make_scrollable_terminal()
        c1 = Char("a", "red", "default", True, False, False, False, False, False)
        c2 = Char("z", "red", "default", True, False, False, False, False, False)

        assert t._char_rich_style(c1) is t._char_rich_style(c2)
        assert len(t._style_cache) == 1

    def test_notify_style_update_clears_style_cache(self):
        """Theme changes drop cached styles alongside the other style state."""
        t = _make_scrollable_terminal()
        t._char_rich_style(Char("a", "red"))
        assert t._style_cache

        with patch.object(type(t).__mro__[1], "notify_style_update", lambda self: None):
            t.notify_style_update()

        assert t._style_cache == {}

    def test_bad_color_falls_back_to_empty_style(self):
        """A colour rich cannot parse degrades to a blank style, not a crash."""
        t = _make_scrollable_terminal()
        style = t._char_rich_style(Char("a", "not-a-color"))
        assert style == Style()


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


# ---------------------------------------------------------------------------
# Selection support tests
# ---------------------------------------------------------------------------


class TestGetSelection:
    def test_extract_selection_returns_text_from_buffer(self):
        """_extract_selection() returns text from scrollback + screen buffer."""
        from textual.selection import Selection

        t = _make_scrollable_terminal()
        t.stream.feed("hello\nworld\n")

        # Create a selection covering the full text
        selection = Selection(start=None, end=None)
        result = t._extract_selection(selection)

        assert result is not None
        text, ending = result
        assert ending == "\n"
        assert "hello" in text
        assert "world" in text

    def test_extract_selection_includes_scrollback(self):
        """_extract_selection() includes lines from scrollback buffer."""
        from textual.selection import Selection

        t = _make_scrollable_terminal()
        # Push enough lines to create scrollback (screen is 5 lines)
        for i in range(10):
            t.stream.feed(f"line {i}\n")

        assert len(t._screen.scrollback) > 0

        selection = Selection(start=None, end=None)
        result = t._extract_selection(selection)

        assert result is not None
        text, _ = result
        # Scrollback lines should be in the output
        assert "line 0" in text


class TestLocalSelection:
    def test_cached_selection_none_when_no_selection(self):
        """_cached_selection is None when nothing is selected."""
        t = _make_scrollable_terminal()
        assert t._cached_selection is None

    def test_update_cached_selection_returns_selection(self):
        """_update_cached_selection builds a Selection from start/end."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t._sel_start = Offset(0, 0)
        t._sel_end = Offset(5, 0)
        t._update_cached_selection()
        assert t._cached_selection is not None
        assert t._cached_selection.get_span(0) == (0, 5)

    def test_clear_selection(self):
        """_clear_selection resets start, end, and cached selection."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t._sel_start = Offset(0, 0)
        t._sel_end = Offset(5, 0)
        t._update_cached_selection()
        # Patch refresh since we're not in a running app
        t.refresh = MagicMock()
        t._clear_selection()
        assert t._sel_start is None
        assert t._sel_end is None
        assert t._cached_selection is None

    def test_selected_text_extracts_content(self):
        """_selected_text() returns the text covered by the selection."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("hello world")
        # Select "hello" (columns 0-5 on virtual row 0, which is screen row 0)
        t._sel_start = Offset(0, 0)
        t._sel_end = Offset(5, 0)
        t._update_cached_selection()
        text = t._selected_text()
        assert text == "hello"

    def test_allow_select_is_false(self):
        """ALLOW_SELECT should be False to prevent cross-widget selection."""
        assert ScrollableTerminal.ALLOW_SELECT is False


class TestOnKeyCopy:
    def test_ctrl_shift_c_does_not_forward_to_pty(self):
        """ctrl+shift+c should not be forwarded to the PTY."""
        import asyncio
        from textual import events

        t = _make_scrollable_terminal()
        t.emulator = MagicMock()
        t.send_queue = asyncio.Queue()

        mock_app = MagicMock()

        event = events.Key("ctrl+shift+c", None)

        from unittest.mock import patch
        with patch.object(type(t), "app", new_callable=lambda: property(lambda self: mock_app)):
            asyncio.get_event_loop().run_until_complete(t.on_key(event))

        # Nothing should be in the send queue
        assert t.send_queue.empty()


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
        t._cached_default_colors = None
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


# ---------------------------------------------------------------------------
# line_text tests
# ---------------------------------------------------------------------------


class TestLineText:
    def test_returns_screen_buffer_row(self):
        """line_text returns a screen buffer row when no scrollback."""
        t = _make_scrollable_terminal()
        t.stream.feed("hello")
        assert t._screen.line_text(0) == "hello"

    def test_returns_scrollback_row(self):
        """line_text returns a scrollback row for indices within scrollback."""
        t = _make_scrollable_terminal()
        for i in range(10):
            t.stream.feed(f"line {i}\n")
        assert len(t._screen.scrollback) > 0
        assert t._screen.line_text(0) == "line 0"

    def test_returns_screen_row_after_scrollback(self):
        """line_text returns a screen row for indices past scrollback."""
        t = _make_scrollable_terminal()
        for i in range(10):
            t.stream.feed(f"line {i}\n")
        scrollback_len = len(t._screen.scrollback)
        # First screen row is at virtual_y == scrollback_len
        assert t._screen.line_text(scrollback_len) == t._screen.screen_text(0)

    def test_trailing_whitespace_is_stripped(self):
        """Both halves strip trailing blanks, as the old row→text did."""
        t = _make_scrollable_terminal()
        for i in range(10):
            t.stream.feed(f"line {i}   \n")
        assert t._screen.line_text(0) == "line 0"


# ---------------------------------------------------------------------------
# Double-click / triple-click selection tests
# ---------------------------------------------------------------------------


class TestSelectWord:
    def test_double_click_selects_word(self):
        """Double-click on a word sets _sel_start/_sel_end to word boundaries."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("hello world")
        t.refresh = MagicMock()

        # Click on 'w' (column 6) of "world"
        with patch.object(t, "_mouse_to_virtual", return_value=Offset(6, 0)), \
             patch.object(t, "_detect_clipboard_cmd", return_value=None):
            mock_event = MagicMock()
            t._select_word(mock_event)

        assert t._sel_start == Offset(6, 0)
        assert t._sel_end == Offset(11, 0)

    def test_double_click_selects_first_word(self):
        """Double-click on the first word selects it correctly."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("hello world")
        t.refresh = MagicMock()

        with patch.object(t, "_mouse_to_virtual", return_value=Offset(2, 0)), \
             patch.object(t, "_detect_clipboard_cmd", return_value=None):
            mock_event = MagicMock()
            t._select_word(mock_event)

        assert t._sel_start == Offset(0, 0)
        assert t._sel_end == Offset(5, 0)

    def test_double_click_on_whitespace_does_nothing(self):
        """Double-click on whitespace does not create a selection."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("hello world")
        t.refresh = MagicMock()

        with patch.object(t, "_mouse_to_virtual", return_value=Offset(5, 0)), \
             patch.object(t, "_detect_clipboard_cmd", return_value=None):
            mock_event = MagicMock()
            t._select_word(mock_event)

        assert t._sel_start is None
        assert t._sel_end is None

    def test_double_click_beyond_text_does_nothing(self):
        """Double-click beyond end of text does not create a selection."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("hi")
        t.refresh = MagicMock()

        with patch.object(t, "_mouse_to_virtual", return_value=Offset(50, 0)), \
             patch.object(t, "_detect_clipboard_cmd", return_value=None):
            mock_event = MagicMock()
            t._select_word(mock_event)

        assert t._sel_start is None
        assert t._sel_end is None


class TestSelectLine:
    def test_triple_click_selects_full_line(self):
        """Triple-click selects the entire line."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.stream.feed("hello world")
        t.refresh = MagicMock()

        with patch.object(t, "_mouse_to_virtual", return_value=Offset(3, 0)), \
             patch.object(t, "_detect_clipboard_cmd", return_value=None):
            mock_event = MagicMock()
            t._select_line(mock_event)

        assert t._sel_start == Offset(0, 0)
        assert t._sel_end == Offset(11, 0)

    def test_triple_click_on_empty_line(self):
        """Triple-click on an empty line sets zero-width selection."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        t.refresh = MagicMock()

        # Row 0 is empty by default
        with patch.object(t, "_mouse_to_virtual", return_value=Offset(0, 0)), \
             patch.object(t, "_detect_clipboard_cmd", return_value=None):
            mock_event = MagicMock()
            t._select_line(mock_event)

        assert t._sel_start == Offset(0, 0)
        assert t._sel_end == Offset(0, 0)

    def test_triple_click_in_scrollback(self):
        """Triple-click on a scrollback line selects it correctly."""
        from textual.geometry import Offset

        t = _make_scrollable_terminal()
        for i in range(10):
            t.stream.feed(f"scrollback line {i}\n")
        t.refresh = MagicMock()

        # Click on the first scrollback line (virtual_y=0)
        with patch.object(t, "_mouse_to_virtual", return_value=Offset(5, 0)), \
             patch.object(t, "_detect_clipboard_cmd", return_value=None):
            mock_event = MagicMock()
            t._select_line(mock_event)

        assert t._sel_start == Offset(0, 0)
        assert t._sel_end is not None
        assert t._sel_end.x > 0  # line has content


class TestPartialRepaint:
    """Only the rows a chunk actually changed should be repainted.

    A bare refresh() clears Textual's per-line strip cache, so every visible
    row re-renders. Agents mostly emit tiny chunks — a real session logged 95%
    under 100 characters, each repainting 36 rows — so this is the difference
    between ~42 and ~2 rendered lines per chunk.
    """

    @staticmethod
    def _prepare(t, height=10, scroll_y=0):
        """Wire up the geometry _refresh_dirty_rows needs, and record calls."""
        from textual.geometry import Offset, Region, Size

        t.refresh = MagicMock()
        t.refresh_line = MagicMock()
        patchers = [
            patch.object(
                type(t), "scroll_offset",
                new_callable=lambda: property(lambda self: Offset(0, scroll_y)),
            ),
            patch.object(
                type(t), "scrollable_content_region",
                new_callable=lambda: property(
                    lambda self: Region(0, 0, 80, height)
                ),
            ),
        ]
        for p in patchers:
            p.start()
        return patchers

    @staticmethod
    def _stop(patchers):
        for p in patchers:
            p.stop()

    def test_only_changed_rows_are_refreshed(self):
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 10)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t)
        try:
            t.stream.feed("\x1b[3;1H")        # park the cursor on row 2 first
            t._sync_dirty_state()
            t.stream.feed("hello")            # touches row 2 only
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        t.refresh.assert_not_called()
        refreshed = {call.args[0] for call in t.refresh_line.call_args_list}
        assert refreshed == {2}

    def test_cursor_move_within_a_row_repaints_it(self):
        """The cursor is a block on one cell; pyte does not mark that dirty."""
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 10)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t)
        try:
            t.stream.feed("\x1b[5;5H")
            t._sync_dirty_state()
            t.stream.feed("\x1b[5;40H")       # same row, different column
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        refreshed = {call.args[0] for call in t.refresh_line.call_args_list}
        assert refreshed == {4}

    def test_cursor_move_between_rows_repaints_both(self):
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 10)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t)
        try:
            t.stream.feed("\x1b[2;1H")
            t._sync_dirty_state()
            t.stream.feed("\x1b[7;1H")
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        refreshed = {call.args[0] for call in t.refresh_line.call_args_list}
        assert refreshed == {1, 6}

    def test_cursor_visibility_toggle_repaints_the_row(self):
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 10)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t)
        try:
            t.stream.feed("\x1b[4;1H")
            t._sync_dirty_state()
            t.stream.feed("\x1b[?25l")
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        refreshed = {call.args[0] for call in t.refresh_line.call_args_list}
        assert 3 in refreshed

    def test_scrolling_falls_back_to_a_full_repaint(self):
        """Scrollback growth shifts every virtual row — nothing is reusable."""
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 3)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t)
        try:
            t._sync_dirty_state()
            for i in range(6):
                t.stream.feed(f"line {i}\r\n")
            assert len(t._screen.scrollback) > 0
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        t.refresh.assert_called_once()
        t.refresh_line.assert_not_called()

    def test_widespread_changes_fall_back_to_a_full_repaint(self):
        """Past half the screen, one repaint beats a pile of line regions."""
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 10)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t)
        try:
            t._sync_dirty_state()
            for row in range(1, 9):
                t.stream.feed(f"\x1b[{row};1Hrow {row}")
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        t.refresh.assert_called_once()
        t.refresh_line.assert_not_called()

    def test_offscreen_rows_are_not_refreshed(self):
        """A row scrolled out of view needs no repaint pass scheduled."""
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 20)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t, height=5, scroll_y=0)
        try:
            t.stream.feed("\x1b[18;1H")   # cursor already below the fold
            t._sync_dirty_state()
            t.stream.feed("way below the fold")
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        t.refresh_line.assert_not_called()

    def test_dirty_set_is_consumed(self):
        """pyte expects the consumer to clear Screen.dirty."""
        t = _make_scrollable_terminal()
        t._screen = ScrollbackScreen(80, 10)
        t.stream = pyte.Stream(t._screen)
        patchers = self._prepare(t)
        try:
            t._sync_dirty_state()
            t.stream.feed("\x1b[2;1Hx")
            assert t._screen.dirty
            t._refresh_dirty_rows()
        finally:
            self._stop(patchers)

        assert not t._screen.dirty
