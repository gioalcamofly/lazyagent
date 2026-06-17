"""ScrollableTerminal — a terminal widget with scrollback buffer.

Replaces textual-terminal's ``Terminal(Widget)`` with a ``ScrollView``-based
widget that captures lines scrolled off the top of the pyte screen into a
deque, and uses Textual's native scrolling (``virtual_size``, ``render_line``,
scrollbars) to let users scroll through history.
"""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import subprocess
from collections import deque

import pyte
from pyte.screens import Char, Margins

from rich.color import ColorParseError
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from textual import events, log
from textual.geometry import Offset, Size
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip

from lazyagent.pty_emulator import DECSET_PREFIX, RE_ANSI_SEQUENCE, PtyEmulator
from lazyagent.styles import SCROLLBAR_CSS

# ---------------------------------------------------------------------------
# ScrollbackScreen — lightweight pyte Screen subclass
# ---------------------------------------------------------------------------

_DEFAULT_MAX_SCROLLBACK = 5000

# When the widget is hidden (size 0×0), defer pyte.Stream.feed and coalesce
# chunks. Hidden widgets don't need an up-to-the-millisecond screen; the
# observer's lifecycle scan can read a slightly-stale state. Burning CPU on
# every stdout chunk for off-screen agents was the dominant idle cost.
_HIDDEN_FEED_INTERVAL = 0.25


class ScrollbackScreen(pyte.Screen):
    """pyte Screen that captures lines scrolled off the top into a deque.

    Only overrides ``index()`` (the method called when the cursor is at the
    bottom margin and a new line is needed).  No ``__getattribute__`` wrapper,
    no ``before_event``/``after_event``.  Cost: one ``dict()`` copy per line
    scrolled off.
    """

    def __init__(
        self,
        columns: int,
        lines: int,
        max_scrollback: int = _DEFAULT_MAX_SCROLLBACK,
    ) -> None:
        super().__init__(columns, lines)
        self.scrollback: deque[dict[int, Char]] = deque(maxlen=max_scrollback)

    def set_margins(self, *args, **kwargs):
        """TERM=linux compat — strip the ``private`` kwarg that pyte passes."""
        kwargs.pop("private", None)
        return super().set_margins(*args, **kwargs)

    def index(self):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0:
            # Only capture to scrollback when the scroll region starts at
            # row 0 (full-screen scroll).  When an app sets custom margins
            # (DECSTBM), lines scrolling within a sub-region should not be
            # saved — matching the behaviour of kitty, xterm, etc.
            self.scrollback.append(dict(self.buffer[0]))
        super().index()


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
        command: str,
        default_colors: str | None = "system",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)

        self.command = command
        self.default_colors = default_colors

        # Default terminal dimensions — updated on resize
        self.ncol = 80
        self.nrow = 24
        self.mouse_tracking = False

        # PTY emulator (created in start())
        self.emulator: PtyEmulator | None = None
        self.send_queue: asyncio.Queue | None = None
        self.recv_queue: asyncio.Queue | None = None
        self.recv_task: asyncio.Task | None = None
        self._stopped = False
        self._follow_output = True  # tracks auto-scroll intent across visibility

        # Deferred pyte feed while hidden. See recv() / _flush_hidden_feed.
        self._hidden_feed_buffer: list[str] = []
        self._hidden_feed_handle: asyncio.TimerHandle | None = None

        # Widget-local text selection (replaces Textual's cross-widget system)
        self._sel_start: Offset | None = None
        self._sel_end: Offset | None = None
        self._is_selecting: bool = False
        self._cached_selection: Selection | None = None
        self._cached_selection_style: Style | None = None

        # pyte screen + stream
        self._screen = ScrollbackScreen(self.ncol, self.nrow)
        self.stream = pyte.Stream(self._screen)

        # Cached resolved default colors (invalidated on theme change)
        self._cached_default_colors: tuple[str, str] | None = None

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
    # PTY lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the PTY subprocess and begin the recv loop."""
        if self.emulator is not None:
            return
        self._stopped = False
        self.emulator = PtyEmulator(command=self.command)
        self.emulator.start()
        self.send_queue = self.emulator.recv_queue
        self.recv_queue = self.emulator.send_queue
        self.recv_task = asyncio.create_task(self.recv())

    def stop(self) -> None:
        """Kill the PTY subprocess and cancel the recv loop."""
        if self.emulator is None:
            return
        self._stopped = True
        self.recv_task.cancel()
        self.emulator.stop()
        self.emulator = None
        if self._hidden_feed_handle is not None:
            self._hidden_feed_handle.cancel()
            self._hidden_feed_handle = None
        self._hidden_feed_buffer.clear()

    def _flush_hidden_feed(self) -> None:
        """Feed any chars buffered while hidden into pyte in one batch.

        Called from the deferred timer, from ``on_show`` (immediate catch-up),
        and from ``on_resize`` (so a pending resize doesn't reorder against
        buffered output). Idempotent; safe to call with an empty buffer.
        """
        if self._hidden_feed_handle is not None:
            self._hidden_feed_handle.cancel()
            self._hidden_feed_handle = None
        if not self._hidden_feed_buffer:
            return
        chars = "".join(self._hidden_feed_buffer)
        self._hidden_feed_buffer.clear()
        try:
            self.stream.feed(chars)
        except TypeError as error:
            log.warning("could not feed:", error)
        # Fire the post-stdout hook once per batch (debounced scan).
        self._after_stdout_processed()

    # ------------------------------------------------------------------
    # Recv loop — reads PTY output and updates screen + scrollback
    # ------------------------------------------------------------------

    async def recv(self) -> None:
        try:
            while True:
                message = await self.recv_queue.get()
                if self._stopped:
                    break
                cmd = message[0]

                if cmd == "setup":
                    await self.send_queue.put(["set_size", self.nrow, self.ncol])

                elif cmd == "stdout":
                    chars = message[1]

                    # Hook for subclasses (e.g. MonitoredTerminal) — runs per
                    # chunk so hang detection updates last_output_time
                    # promptly even while we're hidden.
                    self._on_stdout(chars)

                    if self.size.height > 0:
                        # Visible: process immediately so the screen and any
                        # observer scan reflect the latest output.
                        for sep_match in re.finditer(RE_ANSI_SEQUENCE, chars):
                            sequence = sep_match.group(0)
                            if sequence.startswith(DECSET_PREFIX):
                                parameters = sequence.removeprefix(
                                    DECSET_PREFIX
                                ).split(";")
                                if "1000h" in parameters:
                                    self.mouse_tracking = True
                                if "1000l" in parameters:
                                    self.mouse_tracking = False

                        # Use the flag instead of is_vertical_scroll_end
                        # which returns unreliable values when the widget is
                        # hidden (e.g. after a ContentSwitcher worktree
                        # change).
                        was_at_bottom = self._follow_output

                        # Feed to pyte (may trigger index() → scrollback
                        # capture)
                        try:
                            self.stream.feed(chars)
                        except TypeError as error:
                            log.warning("could not feed:", error)

                        self._update_virtual_size()
                        self.refresh()
                        if was_at_bottom:
                            self.scroll_end(
                                animate=False, immediate=True, x_axis=False
                            )

                        # Post-processing hook for subclasses (e.g. sentinel
                        # scanning) — only runs when visible because the
                        # hidden path coalesces and fires the hook once per
                        # flush instead of per chunk.
                        self._after_stdout_processed()
                    else:
                        # Hidden: buffer chunks and flush every
                        # _HIDDEN_FEED_INTERVAL seconds. Avoids pyte parsing
                        # cost per chunk for off-screen agents (e.g. running
                        # Claude Code instances). Mouse-tracking + refresh +
                        # scroll are all visibility-dependent, so we skip
                        # them entirely until the flush.
                        self._hidden_feed_buffer.append(chars)
                        if self._hidden_feed_handle is None:
                            loop = asyncio.get_running_loop()
                            self._hidden_feed_handle = loop.call_later(
                                _HIDDEN_FEED_INTERVAL,
                                self._flush_hidden_feed,
                            )

                elif cmd == "disconnect":
                    self._on_recv_disconnect()
                    self.stop()

        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def _on_stdout(self, chars: str) -> None:
        """Called on each PTY stdout chunk, before processing.

        Override in subclasses for monitoring (e.g. agent status detection).
        """

    def _on_recv_disconnect(self) -> None:
        """Called when the PTY disconnects.

        Override in subclasses for cleanup.
        """

    def _after_stdout_processed(self) -> None:
        """Called after each stdout chunk is fed and scroll is updated.

        Override in subclasses for post-processing (e.g. sentinel scanning).
        """

    # ------------------------------------------------------------------
    # Auto-scroll tracking
    # ------------------------------------------------------------------

    def on_show(self) -> None:
        """Catch up after being hidden: drain buffered output, sync size, scroll."""
        # Drain anything that arrived while hidden so the first paint
        # reflects the latest state.
        self._flush_hidden_feed()

        def _restore() -> None:
            self._update_virtual_size()
            self.refresh()
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

    # ------------------------------------------------------------------
    # Line API rendering
    # ------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        """Render a single line.

        ``y`` is a widget-local coordinate (0 = top of visible area).
        We add ``scroll_offset.y`` to map into virtual space, then
        dispatch to scrollback or live screen rendering.
        """
        scroll_x, scroll_y = self.scroll_offset
        virtual_y = scroll_y + y
        scrollback_len = len(self._screen.scrollback)
        width = self.scrollable_content_region.width

        if virtual_y < scrollback_len:
            strip = self._render_scrollback_line(virtual_y, width, virtual_y)
        else:
            screen_y = virtual_y - scrollback_len
            strip = self._render_screen_line(screen_y, width, virtual_y)

        return strip.crop_extend(scroll_x, scroll_x + width, self.rich_style)

    def _render_scrollback_line(self, index: int, width: int, virtual_y: int) -> Strip:
        """Render a line from the scrollback buffer."""
        row = self._screen.scrollback[index]
        return self._row_to_strip(row, width, show_cursor=False, virtual_y=virtual_y)

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
        text = Text()
        ncols = max(width, self._screen.columns)
        style_change_pos: int = 0

        for x in range(ncols):
            char: Char = row.get(x, self._screen.default_char)
            text.append(char.data)

            if x > 0:
                last_char: Char = row.get(x - 1, self._screen.default_char)
                if (
                    not self._char_style_cmp(char, last_char)
                    or x == ncols - 1
                ):
                    last_style = self._char_rich_style(last_char)
                    text.stylize(last_style, style_change_pos, x + 1)
                    style_change_pos = x

        # Apply cursor style AFTER all character styles so it is never
        # overwritten by a later stylize() call that covers the same range.
        if show_cursor and 0 <= self._screen.cursor.x < ncols:
            cx = self._screen.cursor.x
            cursor_char: Char = row.get(cx, self._screen.default_char)
            text.stylize(self._cursor_style(cursor_char), cx, cx + 1)

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
                text.stylize(self._cached_selection_style, start, end)

        segments = list(text.render(self.app.console))
        return Strip(segments)

    # ------------------------------------------------------------------
    # Style helpers (ported from textual-terminal Terminal)
    # ------------------------------------------------------------------

    def notify_style_update(self) -> None:
        self._cached_default_colors = None
        self._cached_selection_style = None
        super().notify_style_update()

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
        foreground = self._detect_color(char.fg)
        background = self._detect_color(char.bg)

        try:
            style = Style(
                color=foreground,
                bgcolor=background,
                bold=char.bold,
                italic=char.italics,
                underline=char.underscore,
                strike=char.strikethrough,
                reverse=char.reverse,
                blink=char.blink,
            )
        except ColorParseError as error:
            log.warning("color parse error:", error)
            style = Style()

        return style

    # Keep these as instance methods for MonitoredTerminal compatibility
    # (MonitoredTerminal.recv references self.char_rich_style etc.)
    def char_rich_style(self, char: Char) -> Style:
        return self._char_rich_style(char)

    def char_style_cmp(self, given: Char, other: Char) -> bool:
        return self._char_style_cmp(given, other)

    def detect_color(self, color: str) -> str:
        return self._detect_color(color)

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        if self.emulator is None:
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
            await self.send_queue.put(["stdin", char])

    async def on_paste(self, event: events.Paste) -> None:
        if self.emulator is None:
            return
        if event.text:
            await self.send_queue.put(["stdin", event.text])
        event.stop()

    async def on_click(self, event: events.Click) -> None:
        if self.emulator is None:
            return
        if self.mouse_tracking:
            await self.send_queue.put(["click", event.x, event.y, event.button])
            return
        if event.button != 1:
            return
        if event.chain == 2:
            self._select_word(event)
        elif event.chain >= 3:
            self._select_line(event)

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.emulator is None:
            return
        if self.mouse_tracking:
            # Forward to PTY (vim, less, etc.)
            await self.send_queue.put(["scroll", "down", event.x, event.y])
        else:
            # Default ScrollView behavior — scroll the widget
            event.stop()
            self.scroll_down()
            self._follow_output = self.is_vertical_scroll_end

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.emulator is None:
            return
        if self.mouse_tracking:
            # Forward to PTY
            await self.send_queue.put(["scroll", "up", event.x, event.y])
        else:
            # Default ScrollView behavior
            event.stop()
            self._follow_output = False
            self.scroll_up()

    # ------------------------------------------------------------------
    # Resize
    # ------------------------------------------------------------------

    async def on_resize(self, event: events.Resize) -> None:
        if self.emulator is None:
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

        # Real resize: flush any chars buffered while hidden BEFORE pyte's
        # screen geometry changes, so the buffered output is applied at the
        # dimensions it was produced for.
        self._flush_hidden_feed()

        self.ncol = ncol
        self.nrow = nrow
        await self.send_queue.put(["set_size", self.nrow, self.ncol])
        self._screen.resize(self.nrow, self.ncol)
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

    def _row_to_text(self, row_data: dict[int, Char]) -> str:
        """Convert a pyte row dict to a stripped text line."""
        return "".join(
            row_data.get(x, self._screen.default_char).data
            for x in range(self._screen.columns)
        ).rstrip()

    def _extract_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract text from scrollback + screen buffer for a selection."""
        lines = [self._row_to_text(row) for row in self._screen.scrollback]
        for y in range(self._screen.lines):
            lines.append(self._row_to_text(self._screen.buffer[y]))
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

    def _get_row_at(self, virtual_y: int) -> dict[int, Char]:
        """Return the row dict for a given virtual y coordinate."""
        scrollback_len = len(self._screen.scrollback)
        if virtual_y < scrollback_len:
            return self._screen.scrollback[virtual_y]
        return self._screen.buffer[virtual_y - scrollback_len]

    def _select_word(self, event: events.Click) -> None:
        """Select the word under the cursor (double-click)."""
        pos = self._mouse_to_virtual(event)
        row = self._get_row_at(pos.y)
        line = self._row_to_text(row)
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
        row = self._get_row_at(pos.y)
        line = self._row_to_text(row)
        self._sel_start = Offset(0, pos.y)
        self._sel_end = Offset(len(line), pos.y)
        self._update_cached_selection()
        self._copy_selection_to_clipboard()
        self.refresh()
