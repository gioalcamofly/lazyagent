"""ScrollableTerminal — a terminal widget with scrollback buffer.

A view of a terminal session — the engine's
:class:`~lazyagent.engine.session.TerminalSession` in-process, or a
:class:`~lazyagent.backend.remote_session.RemoteSession` for a remote
engine: the session owns the screen (scrollback included) at the PTY's
size; this widget renders that screen with Textual's native scrolling
(``virtual_size``, ``render_line``, scrollbars), cropping or padding it to
its own width, and turns keys, pastes and mouse events into bytes for the
session. It is attached to the session's output only while it is shown, so
a remote session streams only to visible views.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
import time
from collections.abc import Callable

# The patch replaces pyte.screens.Char, so it has to land before the names
# below are bound and before any Screen is built.
import lazyagent.pyte_patch  # noqa: F401
from pyte.screens import Char

from rich.color import ColorParseError
from rich.control import strip_control_codes
from rich.segment import Segment
from rich.style import Style

from textual import events, log
from textual.geometry import Offset, Region, Size
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip

from lazyagent import diagnostics as diag
from lazyagent.engine.screen import (
    _MAX_STYLE_CACHE,
    ScrollbackScreen,
    _encode_scrollback_row,
    _style_key,
)
from lazyagent.engine.session import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    SUBMIT_KEY,
    TerminalSession,
)
from lazyagent.styles import SCROLLBAR_CSS

__all__ = [
    "BRACKETED_PASTE_END",
    "BRACKETED_PASTE_START",
    "SUBMIT_KEY",
    "ScrollableTerminal",
    "ScrollbackScreen",
    "_encode_scrollback_row",
    "_style_key",
]

# `log` in this module is Textual's logger, so the diagnostic logger gets its
# own name.
_log = logging.getLogger(__name__)



def _sgr_mouse(button: int, x: int, y: int, *, release: bool = False) -> str:
    """SGR (1006) mouse report for a 0-based cell; press, plus release if asked."""
    report = f"\x1b[<{button};{x + 1};{y + 1}M"
    if release:
        report += f"\x1b[<{button};{x + 1};{y + 1}m"
    return report


# ---------------------------------------------------------------------------
# ScrollableTerminal — ScrollView-based terminal widget
# ---------------------------------------------------------------------------


class ScrollableTerminal(ScrollView, can_focus=True):
    """Terminal widget with a scrollback buffer.

    Uses :class:`ScrollView` (Line API) for native scrollbar support and
    the :class:`ScrollbackScreen` to capture scrolled-off lines.
    """

    ALLOW_SELECT = False  # Disable Textual's cross-widget selection

    _clipboard_cmd: list[str] | None = None
    _clipboard_cmd_resolved: bool = False

    DEFAULT_CSS = f"""
    ScrollableTerminal {{
        overflow-y: auto;
        overflow-x: hidden;
        background: $background;
{SCROLLBAR_CSS}
    }}
    """

    def __init__(
        self,
        session: TerminalSession,
        default_colors: str | None = "system",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)

        self.session = session
        self.default_colors = default_colors

        # The size this view last gave the PTY. A view only resizes the PTY
        # when its own size really changed (see on_resize).
        self.ncol = session.cols
        self.nrow = session.rows
        self._follow_output = True  # tracks auto-scroll intent across visibility
        self._detach_output: Callable[[], None] | None = None

        # Diagnostics only — see lazyagent.diagnostics.
        self._diag_render_s: float = 0.0
        self._diag_render_lines: int = 0
        self._diag_repaint: str = ""
        self._diag_dirty_rows: int = 0

        # Widget-local text selection (replaces Textual's cross-widget system)
        self._sel_start: Offset | None = None
        self._sel_end: Offset | None = None
        self._is_selecting: bool = False
        self._cached_selection: Selection | None = None
        self._cached_selection_style: Style | None = None

        # The session's headless screen, rendered directly (shared in-process).
        self._screen: ScrollbackScreen = session.screen

        # Cached resolved default colors (invalidated on theme change)
        self._cached_default_colors: tuple[str, str] | None = None

        # style key (see _style_key) → rich.Style, invalidated on theme change
        self._style_cache: dict[tuple, Style] = {}

        # Partial-repaint bookkeeping (see _refresh_dirty_rows). pyte does not
        # track the cursor in Screen.dirty, and a scroll invalidates every row.
        self._last_cursor: tuple[int, int, bool] = (0, 0, False)
        self._last_scrollback_len: int = 0

        # Per-render-pass hoists (see render_lines). None/-1 when not rendering.
        self._frame_style: Style | None = None
        self._frame_width: int = -1

        # Key translation table (same as textual-terminal)
        self.ctrl_keys = {
            "up": "\x1bOA",
            "down": "\x1bOB",
            "right": "\x1bOC",
            "left": "\x1bOD",
            "home": "\x1bOH",
            "end": "\x1b[F",
            "delete": "\x1b[3~",
            "shift+tab": "\x1b[Z",
            "f1": "\x1bOP",
            "f2": "\x1bOQ",
            "f3": "\x1bOR",
            "f4": "\x1bOS",
            "f5": "\x1b[15~",
            "f6": "\x1b[17~",
            "f7": "\x1b[18~",
            "f8": "\x1b[19~",
            "f9": "\x1b[20~",
            "f10": "\x1b[21~",
            "f11": "\x1b[23~",
            "f12": "\x1b[24~",
            "f13": "\x1b[25~",
            "f14": "\x1b[26~",
            "f15": "\x1b[28~",
            "f16": "\x1b[29~",
            "f17": "\x1b[31~",
            "f18": "\x1b[32~",
            "f19": "\x1b[33~",
            "f20": "\x1b[34~",
        }

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.session.running

    @property
    def mouse_tracking(self) -> bool:
        return self.session.mouse_tracking

    @property
    def bracketed_paste(self) -> bool:
        return self.session.bracketed_paste

    def start(self) -> None:
        """Start the session's process (no-op if it is running)."""
        self.session.start()

    def stop(self) -> None:
        """Stop the session's process."""
        self.session.stop()

    def _attach_output(self) -> None:
        if self._detach_output is None:
            self._detach_output = self.session.attach(
                self._on_session_output, watching=self._is_watching
            )

    def _stop_output(self) -> None:
        if self._detach_output is not None:
            self._detach_output()
            self._detach_output = None

    def on_hide(self) -> None:
        self._stop_output()

    def on_unmount(self) -> None:
        self._stop_output()

    # ------------------------------------------------------------------
    # Session output
    # ------------------------------------------------------------------

    def _is_watching(self) -> bool:
        return self.size.height > 0

    def _on_session_output(self, offset: int, data: bytes) -> None:
        """Repaint after a chunk the session has already fed to the screen.

        A hidden view does nothing: the session batches the feed, and
        ``on_show`` catches up.
        """
        if not self._is_watching():
            return
        # Use the flag instead of is_vertical_scroll_end which returns
        # unreliable values when the widget is hidden (e.g. after a
        # ContentSwitcher worktree change).
        was_at_bottom = self._follow_output
        post_start = time.perf_counter() if diag.HOT else 0.0
        self._update_virtual_size()
        self._refresh_dirty_rows()
        if was_at_bottom:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

        if diag.HOT:
            # `render_*` is whatever render_line() cost since the last chunk —
            # the actual painting happens on Textual's frame, not inside this
            # handler, so it is attributed to the chunk that dirtied the rows.
            # `repaint`/`dirty_rows` say which repaint path this chunk took and
            # over how many rows.
            diag.debug_event(
                _log,
                "term.chunk",
                widget=self.id,
                bytes=len(data),
                offset=offset,
                post_ms=round((time.perf_counter() - post_start) * 1000, 3),
                render_ms=round(self._diag_render_s * 1000, 3),
                render_lines=self._diag_render_lines,
                repaint=self._diag_repaint,
                dirty_rows=self._diag_dirty_rows,
            )
            self._diag_render_s = 0.0
            self._diag_render_lines = 0

    # ------------------------------------------------------------------
    # Auto-scroll tracking
    # ------------------------------------------------------------------

    def on_show(self) -> None:
        """Catch up after being hidden: drain buffered output, sync size, scroll."""
        self._attach_output()
        # Drain anything that arrived while hidden so the first paint
        # reflects the latest state.
        self.session.flush()

        def _restore() -> None:
            self._update_virtual_size()
            self.refresh()
            self._sync_dirty_state()
            if self._follow_output:
                self.scroll_end(animate=False, immediate=True, x_axis=False)

        # Defer until after the first layout pass so the widget has
        # a real size and scrollable_content_region is valid.
        self.call_after_refresh(_restore)

    # ------------------------------------------------------------------
    # Virtual size management
    # ------------------------------------------------------------------

    def _update_virtual_size(self) -> None:
        """Set virtual_size to reflect scrollback + live screen."""
        total_lines = len(self._screen.scrollback) + self._screen.lines
        self.virtual_size = Size(self.ncol, total_lines)

    def _refresh_dirty_rows(self) -> None:
        """Repaint only the screen rows the last feed actually changed.

        A bare ``refresh()`` clears Textual's per-line strip cache, so every
        visible row is re-rendered. Agents overwhelmingly emit tiny chunks — a
        spinner tick, a few streamed tokens — and a real session measured 95%
        of chunks at under 100 characters, each triggering a full repaint of
        36 rows. Rendering was 43s against 1.2s of parsing.

        pyte already records which rows changed in ``Screen.dirty``; it is the
        consumer's job to read and clear it.
        """
        screen = self._screen
        dirty = screen.dirty
        cursor = screen.cursor
        # x matters as much as y: the cursor is drawn as a block on one cell,
        # so sliding it along a row leaves the old cell painted unless that row
        # is redrawn.
        cursor_state = (cursor.y, cursor.x, cursor.hidden)
        scrollback_len = len(screen.scrollback)

        # A scroll shifts every virtual row, so nothing cached is reusable.
        if scrollback_len != self._last_scrollback_len:
            self._last_scrollback_len = scrollback_len
            self._last_cursor = cursor_state
            dirty.clear()
            self._diag_repaint = "full:scrolled"
            self.refresh()
            return

        # pyte does not mark a row dirty when only the cursor moved, so both
        # the row it left and the row it arrived on have to be redrawn.
        if cursor_state != self._last_cursor:
            dirty.add(self._last_cursor[0])
            dirty.add(cursor_state[0])
            self._last_cursor = cursor_state

        # Which branch this took, and over how many rows, is the interesting
        # part of the trace — recorded unconditionally because it is two
        # attribute stores and no formatting. Emitting it is still gated on
        # diag.HOT, in recv().
        self._diag_dirty_rows = len(dirty)

        if not dirty:
            self._diag_repaint = "none"
            return

        # Past a certain fraction, one whole-widget repaint beats a pile of
        # single-line regions.
        if len(dirty) * 2 >= screen.lines:
            dirty.clear()
            self._diag_repaint = "full:threshold"
            self.refresh()
            return

        # Only rows actually on screen are worth a region; refreshing an
        # off-viewport row still schedules a repaint pass for nothing.
        top = self.scroll_offset.y
        bottom = top + self.scrollable_content_region.height
        onscreen = 0
        for screen_y in dirty:
            virtual_y = scrollback_len + screen_y
            if top <= virtual_y < bottom:
                # refresh_line takes a virtual row and subtracts scroll itself.
                self.refresh_line(virtual_y)
                onscreen += 1
        dirty.clear()
        self._diag_repaint = "partial"
        self._diag_dirty_rows = onscreen

    def _sync_dirty_state(self) -> None:
        """Re-baseline partial-repaint bookkeeping after a full repaint."""
        screen = self._screen
        screen.dirty.clear()
        self._last_cursor = (screen.cursor.y, screen.cursor.x, screen.cursor.hidden)
        self._last_scrollback_len = len(screen.scrollback)

    # ------------------------------------------------------------------
    # Line API rendering
    # ------------------------------------------------------------------

    def render_lines(self, crop: Region) -> list[Strip]:
        """Render a block of lines, hoisting what is constant for the pass.

        ``rich_style`` walks the DOM ancestor chain and combines styles on
        every access — measured at ~50µs, against ~35µs for encoding a whole
        row — and ``scrollable_content_region`` recomputes region arithmetic.
        Both are fixed for the duration of one render pass but were being
        recomputed for every line of it.
        """
        self._frame_style = self.rich_style
        self._frame_width = self.scrollable_content_region.width
        try:
            return super().render_lines(crop)
        finally:
            self._frame_style = None
            self._frame_width = -1

    def render_line(self, y: int) -> Strip:
        """Render a single line.

        Per-line timing would be one log record per line per frame, so the cost
        is accumulated here and reported once per output chunk instead.
        """
        if not diag.HOT:
            return self._render_line(y)
        start = time.perf_counter()
        try:
            return self._render_line(y)
        finally:
            self._diag_render_s += time.perf_counter() - start
            self._diag_render_lines += 1

    def _render_line(self, y: int) -> Strip:
        """Render a single line.

        ``y`` is a widget-local coordinate (0 = top of visible area).
        We add ``scroll_offset.y`` to map into virtual space, then
        dispatch to scrollback or live screen rendering.
        """
        # Populated by render_lines; fall back for direct calls (tests, and
        # anything that renders a line outside a full pass).
        style = self._frame_style
        if style is None:
            style = self.rich_style
        width = self._frame_width
        if width < 0:
            width = self.scrollable_content_region.width

        scroll_x, scroll_y = self.scroll_offset
        virtual_y = scroll_y + y
        scrollback_len = len(self._screen.scrollback)

        if virtual_y < scrollback_len:
            strip = self._render_scrollback_line(virtual_y, width, virtual_y)
        else:
            screen_y = virtual_y - scrollback_len
            strip = self._render_screen_line(screen_y, width, virtual_y)

        return strip.crop_extend(scroll_x, scroll_x + width, style)

    def _render_scrollback_line(self, index: int, width: int, virtual_y: int) -> Strip:
        """Render a line from the scrollback buffer.

        Scrollback is already run-length encoded, so this skips the per-cell
        pass the live screen needs.
        """
        text, runs = self._screen.scrollback[index]
        ncols = max(width, self._screen.columns)
        return self._runs_to_strip(text, runs, ncols, virtual_y=virtual_y)

    def _render_screen_line(self, screen_y: int, width: int, virtual_y: int) -> Strip:
        """Render a line from the live pyte screen buffer."""
        if screen_y < 0 or screen_y >= self._screen.lines:
            return Strip.blank(width, self.rich_style)
        row = self._screen.buffer[screen_y]
        show_cursor = (
            not self._screen.cursor.hidden
            and self._screen.cursor.y == screen_y
        )
        return self._row_to_strip(row, width, show_cursor=show_cursor, virtual_y=virtual_y)

    def _row_to_strip(
        self,
        row: dict[int, Char],
        width: int,
        *,
        show_cursor: bool = False,
        virtual_y: int = -1,
    ) -> Strip:
        """Convert a pyte row (dict of column→Char) to a textual Strip."""
        ncols = max(width, self._screen.columns)
        text, runs = self._encode_row(row, ncols)

        cursor_x = -1
        cursor_style: Style | None = None
        if show_cursor and 0 <= self._screen.cursor.x < ncols:
            cursor_x = self._screen.cursor.x
            cursor_style = self._cursor_style(
                row.get(cursor_x, self._screen.default_char)
            )

        return self._runs_to_strip(
            text,
            runs,
            ncols,
            cursor_x=cursor_x,
            cursor_style=cursor_style,
            virtual_y=virtual_y,
        )

    def _encode_row(
        self, row: dict[int, Char], ncols: int
    ) -> tuple[str, list[tuple[int, int, tuple]]]:
        """Run-length encode a live pyte row into ``(text, runs)``.

        ``runs`` is a list of ``(start, end, style_key)`` with offsets into
        ``text``. One pass, one tuple per cell, and no ``rich`` objects — the
        old per-cell ``Text.append`` + ``stylize`` version was 53% of the
        widget's CPU under a streaming agent.
        """
        default_char = self._screen.default_char
        get = row.get
        parts: list[str] = []
        runs: list[tuple[int, int, tuple]] = []
        buf: list[str] = []
        cur_key: tuple | None = None
        start = 0

        for x in range(ncols):
            char = get(x, default_char)
            # Index instead of attribute access, and _style_key inlined: this
            # runs once per cell per repaint, so descriptor lookups show up.
            key = char[1:]
            if key != cur_key:
                if buf:
                    run = "".join(buf)
                    parts.append(run)
                    end = start + len(run)
                    runs.append((start, end, cur_key))
                    start = end
                    buf.clear()
                cur_key = key
            buf.append(char[0])

        if buf:
            run = "".join(buf)
            parts.append(run)
            runs.append((start, start + len(run), cur_key))

        return "".join(parts), runs

    def _runs_to_strip(
        self,
        text: str,
        runs: list[tuple[int, int, tuple]] | tuple[tuple[int, int, tuple], ...],
        ncols: int,
        *,
        cursor_x: int = -1,
        cursor_style: Style | None = None,
        virtual_y: int = -1,
    ) -> Strip:
        """Build a Strip from run-length encoded style runs.

        Shared by the live-screen and scrollback paths. ``text`` may be
        shorter than ``ncols`` (scrollback rows drop trailing blanks); the
        remainder is padded with default-styled spaces, which is what the
        live path produces for unwritten cells.
        """
        pad = ncols - len(text)
        if pad > 0:
            default_key = _style_key(self._screen.default_char)
            if runs and runs[-1][2] == default_key:
                # The stored row already ends in default-styled cells — extend
                # that run rather than emitting a second identical segment.
                runs = [*runs[:-1], (runs[-1][0], ncols, default_key)]
            else:
                runs = [*runs, (len(text), ncols, default_key)]
            text = text + " " * pad

        # Overlays are applied in insertion order, mirroring the order the
        # old implementation called Text.stylize() in: character styles
        # first, then the cursor, then the selection. Later overlays win on
        # the attributes they set, so the cursor's fg/bg swap is never
        # overwritten by a character style.
        overlays: list[tuple[int, int, Style]] = []
        if cursor_style is not None:
            overlays.append((cursor_x, cursor_x + 1, cursor_style))

        selection = self._cached_selection
        if selection is not None and virtual_y >= 0:
            span = selection.get_span(virtual_y)
            if span is not None:
                start, end = span
                if end == -1:
                    end = len(text)
                if self._cached_selection_style is None:
                    self._cached_selection_style = (
                        self.screen.get_component_rich_style("screen--selection")
                    )
                overlays.append((start, end, self._cached_selection_style))

        cached_style = self._cached_style
        if not overlays:
            return Strip(
                [
                    Segment(strip_control_codes(text[start:end]), cached_style(key))
                    for start, end, key in runs
                ]
            )

        segments: list[Segment] = []
        for start, end, key in runs:
            base = cached_style(key)
            cuts = {start, end}
            for over_start, over_end, _ in overlays:
                if start < over_start < end:
                    cuts.add(over_start)
                if start < over_end < end:
                    cuts.add(over_end)
            bounds = sorted(cuts)
            for piece_start, piece_end in zip(bounds, bounds[1:]):
                style = base
                for over_start, over_end, over_style in overlays:
                    if over_start <= piece_start and piece_end <= over_end:
                        style = style + over_style
                segments.append(
                    Segment(strip_control_codes(text[piece_start:piece_end]), style)
                )

        return Strip(segments)

    # ------------------------------------------------------------------
    # Style helpers (ported from textual-terminal Terminal)
    # ------------------------------------------------------------------

    def notify_style_update(self) -> None:
        self._cached_default_colors = None
        self._cached_selection_style = None
        self._style_cache.clear()
        super().notify_style_update()

    def _cached_style(self, key: tuple) -> Style:
        """Return the ``rich.Style`` for a style key, memoized.

        A screenful of a real TUI resolves to a few dozen distinct keys, so a
        plain dict removes essentially every ``Style`` construction from the
        render path.
        """
        style = self._style_cache.get(key)
        if style is None:
            # key[:8] rather than a full unpack: _style_key carries every
            # non-data Char field, and pyte_patch's `dim` is not rendered.
            fg, bg, bold, italics, underscore, strikethrough, reverse, blink = key[:8]
            try:
                style = Style(
                    color=self._detect_color(fg),
                    bgcolor=self._detect_color(bg),
                    bold=bold,
                    italic=italics,
                    underline=underscore,
                    strike=strikethrough,
                    reverse=reverse,
                    blink=blink,
                )
            except ColorParseError as error:
                log.warning("color parse error:", error)
                style = Style()
            if len(self._style_cache) >= _MAX_STYLE_CACHE:
                self._style_cache.clear()
            self._style_cache[key] = style
        return style

    def _resolved_default_colors(self) -> tuple[str, str]:
        """Return resolved (fg, bg) for default/inherited colors, cached."""
        if self._cached_default_colors is None:
            wstyle = self.rich_style
            fg = wstyle.color.name if wstyle.color and wstyle.color.name else "white"
            bg = wstyle.bgcolor.name if wstyle.bgcolor and wstyle.bgcolor.name else "black"
            self._cached_default_colors = (fg, bg)
        return self._cached_default_colors

    @staticmethod
    def _char_style_cmp(given: Char, other: Char) -> bool:
        """Return True if two pyte Chars have the same style."""
        return (
            given.fg == other.fg
            and given.bg == other.bg
            and given.bold == other.bold
            and given.italics == other.italics
            and given.underscore == other.underscore
            and given.strikethrough == other.strikethrough
            and given.reverse == other.reverse
            and given.blink == other.blink
        )

    @staticmethod
    def _detect_color(color: str) -> str:
        """Fix colour names/hex for Rich compatibility."""
        if color == "brown":
            return "yellow"
        if color == "brightblack":
            return "#808080"
        if re.match("[0-9a-f]{6}", color, re.IGNORECASE):
            return f"#{color}"
        return color

    def _cursor_style(self, char: Char) -> Style:
        """Build a block-cursor style for the given cell.

        Swaps the cell's fg/bg so the cursor appears as a solid block.
        When the cell uses default (inherited) colors, resolves actual
        colors from the cached widget theme so the swap produces visible
        contrast — bare ``reverse`` on two default colors is a no-op.
        """
        cell_fg = self._detect_color(char.fg)
        cell_bg = self._detect_color(char.bg)

        if cell_fg == "default" or cell_bg == "default":
            default_fg, default_bg = self._resolved_default_colors()
            if cell_fg == "default":
                cell_fg = default_fg
            if cell_bg == "default":
                cell_bg = default_bg

        # Cursor = swapped fg/bg
        return Style(color=cell_bg, bgcolor=cell_fg)

    def _char_rich_style(self, char: Char) -> Style:
        """Convert a pyte Char's attributes to a ``rich.Style``."""
        return self._cached_style(_style_key(char))

    # Keep these as instance methods for MonitoredTerminal compatibility
    # (MonitoredTerminal.recv references self.char_rich_style etc.)
    def char_rich_style(self, char: Char) -> Style:
        return self._char_rich_style(char)

    def char_style_cmp(self, given: Char, other: Char) -> bool:
        return self._char_style_cmp(given, other)

    def detect_color(self, color: str) -> str:
        return self._detect_color(color)

    # ------------------------------------------------------------------
    # Programmatic input
    # ------------------------------------------------------------------

    def frame_input(self, text: str) -> str:
        """Frame ``text`` as a paste, per the session's current modes."""
        return self.session.frame_input(text)

    async def send_input(self, text: str, *, submit: bool = False) -> None:
        """Write ``text`` into the child, optionally pressing Enter after it."""
        await self.session.send_input(text, submit=submit)

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        if not self.session.running:
            return

        if event.key == "ctrl+f1":
            self.app.set_focus(None)
            return

        # PageUp/Down scroll the widget (history) instead of sending to PTY
        if event.key == "pageup":
            event.stop()
            self._follow_output = False
            self.scroll_page_up(animate=False)
            return
        if event.key == "pagedown":
            event.stop()
            self.scroll_page_down(animate=False)
            self._follow_output = self.is_vertical_scroll_end
            return

        if event.key == "ctrl+shift+c":
            self._copy_selection_to_clipboard()
            return

        event.stop()
        char = self.ctrl_keys.get(event.key) or event.character
        if char:
            self._claim_size()
            self.session.write(char)

    def _claim_size(self) -> None:
        """Typing here takes the PTY size back from a protocol client that
        resized it (the latest client to type owns the size)."""
        if (self.session.rows, self.session.cols) != (self.nrow, self.ncol):
            self.session.resize(self.nrow, self.ncol)

    async def on_paste(self, event: events.Paste) -> None:
        if not self.session.running:
            return
        if event.text:
            self._claim_size()
            # Re-frame as a paste for the child; it asked for the markers, and
            # forwarding raw text lets embedded newlines act as keypresses.
            self.session.write(self.frame_input(event.text))
        event.stop()

    async def on_click(self, event: events.Click) -> None:
        if not self.session.running:
            return
        if self.mouse_tracking:
            if event.button == 1:
                self.session.write(_sgr_mouse(0, event.x, event.y, release=True))
            return
        if event.button != 1:
            return
        if event.chain == 2:
            self._select_word(event)
        elif event.chain >= 3:
            self._select_line(event)

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if not self.session.running:
            return
        if self.mouse_tracking:
            # Forward to PTY (vim, less, etc.)
            self.session.write(_sgr_mouse(65, event.x, event.y))
        else:
            # Default ScrollView behavior — scroll the widget
            event.stop()
            self.scroll_down()
            self._follow_output = self.is_vertical_scroll_end

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if not self.session.running:
            return
        if self.mouse_tracking:
            # Forward to PTY
            self.session.write(_sgr_mouse(64, event.x, event.y))
        else:
            # Default ScrollView behavior
            event.stop()
            self._follow_output = False
            self.scroll_up()

    # ------------------------------------------------------------------
    # Resize
    # ------------------------------------------------------------------

    async def on_resize(self, event: events.Resize) -> None:
        if not self.session.running:
            return

        ncol = self.scrollable_content_region.width or self.size.width
        nrow = self.scrollable_content_region.height or self.size.height

        # Skip zero dimensions — happens when widget is hidden by
        # ContentSwitcher.  Sending 0×0 to the PTY would cause the child
        # process to format output for a zero-column terminal, corrupting
        # the scrollback captured during that window.
        if ncol == 0 or nrow == 0:
            return

        # Textual fires Resize whenever a widget enters the "shown" set,
        # even if its dimensions are unchanged from the last visible state.
        # Avoid the work — and avoid SIGWINCH'ing the child, which makes
        # full-screen TUIs (Claude Code, etc.) repaint their whole UI and
        # burst kilobytes of ANSI on every worktree switch.
        if self.ncol == ncol and self.nrow == nrow:
            return

        # The session flushes output batched while hidden BEFORE the screen
        # geometry changes, so it is applied at the size it was produced for.
        self.ncol = ncol
        self.nrow = nrow
        self.session.resize(self.nrow, self.ncol)
        self._update_virtual_size()

    # ------------------------------------------------------------------
    # Selection support — widget-local (no cross-pane leaking)
    # ------------------------------------------------------------------

    def _update_cached_selection(self) -> None:
        """Recompute cached Selection from current start/end offsets."""
        if self._sel_start is None or self._sel_end is None:
            self._cached_selection = None
        else:
            self._cached_selection = Selection.from_offsets(
                self._sel_start, self._sel_end
            )

    def _clear_selection(self) -> None:
        """Clear the current selection."""
        if self._sel_start is not None or self._sel_end is not None:
            self._sel_start = None
            self._sel_end = None
            self._cached_selection = None
            self.refresh()

    def _selected_text(self) -> str | None:
        """Extract selected text, or None if nothing selected."""
        if self._cached_selection is None:
            return None
        result = self._extract_selection(self._cached_selection)
        if result is None:
            return None
        text, _ = result
        return text if text else None

    def _extract_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract text from scrollback + screen buffer for a selection."""
        screen = self._screen
        lines = [
            screen.scrollback_text(index) for index in range(len(screen.scrollback))
        ]
        for y in range(screen.lines):
            lines.append(screen.screen_text(y))
        full_text = "\n".join(lines)
        return selection.extract(full_text), "\n"

    @classmethod
    def _detect_clipboard_cmd(cls) -> list[str] | None:
        """Detect the system clipboard command (cached at class level)."""
        if cls._clipboard_cmd_resolved:
            return cls._clipboard_cmd
        system = platform.system()
        if system == "Darwin":
            cls._clipboard_cmd = ["pbcopy"]
        elif system == "Linux":
            if shutil.which("xclip"):
                cls._clipboard_cmd = ["xclip", "-selection", "clipboard"]
            elif shutil.which("xsel"):
                cls._clipboard_cmd = ["xsel", "--clipboard", "--input"]
            elif shutil.which("wl-copy"):
                cls._clipboard_cmd = ["wl-copy"]
        cls._clipboard_cmd_resolved = True
        return cls._clipboard_cmd

    @staticmethod
    def _copy_to_system_clipboard(text: str, cmd: list[str]) -> None:
        """Copy text to system clipboard."""
        try:
            subprocess.run(cmd, input=text.encode(), check=False, timeout=5)
        except Exception:
            pass

    def _copy_selection_to_clipboard(self) -> None:
        """Copy current selection to system clipboard if available."""
        text = self._selected_text()
        cmd = self._detect_clipboard_cmd()
        if text and cmd:
            self._copy_to_system_clipboard(text, cmd)

    # ------------------------------------------------------------------
    # Mouse-driven selection
    # ------------------------------------------------------------------

    def _mouse_to_virtual(self, event: events.MouseEvent) -> Offset:
        """Convert mouse event coordinates to clamped virtual position."""
        scroll_y = self.scroll_offset.y
        virtual_y = scroll_y + event.y
        width = self.scrollable_content_region.width or self.ncol
        total_lines = len(self._screen.scrollback) + self._screen.lines

        x = max(0, min(event.x, width - 1))
        virtual_y = max(0, min(virtual_y, total_lines - 1))
        return Offset(x, virtual_y)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self.mouse_tracking:
            return
        if event.button != 1:
            return
        self._sel_start = self._mouse_to_virtual(event)
        self._sel_end = None
        self._cached_selection = None
        self._is_selecting = True
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._is_selecting:
            return
        new_end = self._mouse_to_virtual(event)
        if new_end == self._sel_end:
            return
        self._sel_end = new_end
        self._update_cached_selection()
        self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._is_selecting:
            return
        self._is_selecting = False
        self.release_mouse()
        # A click with no drag — clear the selection
        if self._sel_end is None or self._sel_start == self._sel_end:
            self._clear_selection()
            return
        # Auto-copy selected text to system clipboard on mouse release
        self._copy_selection_to_clipboard()

    # ------------------------------------------------------------------
    # Double-click / triple-click selection helpers
    # ------------------------------------------------------------------

    def _select_word(self, event: events.Click) -> None:
        """Select the word under the cursor (double-click)."""
        pos = self._mouse_to_virtual(event)
        line = self._screen.line_text(pos.y)
        x = pos.x
        if x >= len(line) or not line[x].strip():
            return
        for match in re.finditer(r"\w+", line):
            if match.start() <= x < match.end():
                self._sel_start = Offset(match.start(), pos.y)
                self._sel_end = Offset(match.end(), pos.y)
                self._update_cached_selection()
                self._copy_selection_to_clipboard()
                self.refresh()
                return

    def _select_line(self, event: events.Click) -> None:
        """Select the entire line (triple-click)."""
        pos = self._mouse_to_virtual(event)
        line = self._screen.line_text(pos.y)
        self._sel_start = Offset(0, pos.y)
        self._sel_end = Offset(len(line), pos.y)
        self._update_cached_selection()
        self._copy_selection_to_clipboard()
        self.refresh()
