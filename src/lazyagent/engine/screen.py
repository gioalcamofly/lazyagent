"""Headless terminal screen: a pyte ``Screen`` that keeps a scrollback.

The engine feeds one of these per session whatever any client shows; the TUI
renders straight from it in-process, and :func:`ansi_snapshot` serialises it
for a client that attaches over the protocol.
"""

from __future__ import annotations

from collections import deque

# The patch replaces pyte.screens.Char, so it has to land before the names
# below are bound and before any Screen is built.
# isort: off
import lazyagent.pyte_patch  # noqa: F401
import pyte
import pyte.graphics
from pyte.screens import Char, Margins
# isort: on

_DEFAULT_MAX_SCROLLBACK = 5000

# Upper bound on a style cache or intern table. A real screen has a few dozen
# distinct styles, but a truecolor-heavy app (image renderer, gradient) can
# generate one per cell — cap the cache so it cannot grow without bound.
_MAX_STYLE_CACHE = 4096


def _style_key(char: Char) -> tuple:
    """Return every field of a pyte ``Char`` except ``data`` as a plain tuple.

    Used both as the key of the rendered-``Style`` cache and as the style
    element of the compact scrollback rows.

    Slicing a ``Char`` is deliberate on two counts. It is a single C-level
    operation instead of eight attribute loads, which matters because this
    runs once per cell per repaint. And slicing a tuple subclass yields an
    *exact* ``tuple``, which is what lets the GC untrack it (see
    :class:`ScrollbackScreen`) — do not "clean this up" into a NamedTuple.

    The first eight fields are the ones the renderer honours;
    :meth:`ScrollableTerminal._cached_style` ignores any extra (today just
    ``dim``, which pyte_patch tracks but the renderer has never applied).
    """
    return char[1:]


def _encode_scrollback_row(
    row: dict[int, Char],
    columns: int,
    default_char: Char,
    intern: dict[tuple, tuple],
) -> tuple[str, tuple]:
    """Encode a pyte row into the compact scrollback representation.

    Returns ``(text, runs)`` where ``runs`` is a tuple of
    ``(start, end, style_key)`` with offsets into ``text``.

    Trailing cells that are blank *and* default-styled are dropped — the
    renderer pads short rows back out with exactly those cells, so this is
    lossless — and style keys are interned, so a screenful of scrolled-off
    lines shares a handful of key tuples.
    """
    if not row:
        return "", ()

    default_key = _style_key(default_char)
    get = row.get

    last = -1
    for x in row:
        if last < x < columns:
            last = x
    while last >= 0:
        char = get(last)
        if char is not None and (char[0] != " " or char[1:] != default_key):
            break
        last -= 1
    if last < 0:
        return "", ()

    parts: list[str] = []
    runs: list[tuple[int, int, tuple]] = []
    buf: list[str] = []
    cur_key: tuple | None = None
    start = 0

    for x in range(last + 1):
        char = get(x, default_char)
        key = char[1:]
        if key != cur_key:
            if buf:
                run = "".join(buf)
                parts.append(run)
                end = start + len(run)
                runs.append((start, end, cur_key))
                start = end
                buf.clear()
            # Interning keeps one key tuple per distinct style instead of one
            # per run per line. Capped so a truecolor-heavy app cannot grow
            # the table without bound.
            cur_key = (
                intern.setdefault(key, key) if len(intern) < _MAX_STYLE_CACHE else key
            )
        buf.append(char[0])

    if buf:
        run = "".join(buf)
        parts.append(run)
        runs.append((start, start + len(run), cur_key))

    return "".join(parts), tuple(runs)


class ScrollbackScreen(pyte.Screen):
    """pyte Screen that captures lines scrolled off the top into a deque.

    Only overrides ``index()`` (the method called when the cursor is at the
    bottom margin and a new line is needed).  No ``__getattribute__`` wrapper,
    no ``before_event``/``after_event``.

    Scrolled-off lines are stored in the compact form produced by
    :func:`_encode_scrollback_row` — a ``str`` plus plain tuples of
    primitives — rather than as a ``dict[int, Char]``.

    **The tuples must stay plain tuples.**  CPython untracks a tuple whose
    items are all untracked (``_PyTuple_MaybeUntrack``), but it gates that on
    ``PyTuple_CheckExact``, so a NamedTuple subclass such as pyte's ``Char``
    is *never* untracked.  Every cell kept as a ``Char`` is therefore
    traversed by every gen2 collection for as long as it is in scrollback:
    six terminals with full scrollback measured 583 MB RSS and ~560 ms
    collection pauses, which the user sees as a freeze.  Swapping these
    tuples for a NamedTuple or a dataclass would silently bring that back —
    ``test_scrollback_entries_are_gc_untracked`` guards it.
    """

    def __init__(
        self,
        columns: int,
        lines: int,
        max_scrollback: int = _DEFAULT_MAX_SCROLLBACK,
    ) -> None:
        super().__init__(columns, lines)
        self.scrollback: deque[tuple[str, tuple]] = deque(maxlen=max_scrollback)
        self._style_key_intern: dict[tuple, tuple] = {}

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
            self.scrollback.append(
                _encode_scrollback_row(
                    self.buffer[0],
                    self.columns,
                    self.default_char,
                    self._style_key_intern,
                )
            )
        super().index()

    # ------------------------------------------------------------------
    # Text access — used by selection, double/triple click and the IPC
    # read_agent_output tool, so row→text lives in one place.
    # ------------------------------------------------------------------

    def scrollback_text(self, index: int) -> str:
        """Return the text of a scrollback line, trailing blanks stripped."""
        return self.scrollback[index][0].rstrip()

    def screen_text(self, y: int) -> str:
        """Return the text of a live screen row, trailing blanks stripped."""
        row = self.buffer[y]
        default_char = self.default_char
        return "".join(
            row.get(x, default_char).data for x in range(self.columns)
        ).rstrip()

    def line_text(self, virtual_y: int) -> str:
        """Return the text of a virtual line (scrollback first, then screen)."""
        scrollback_len = len(self.scrollback)
        if virtual_y < scrollback_len:
            return self.scrollback_text(virtual_y)
        return self.screen_text(virtual_y - scrollback_len)


# ---------------------------------------------------------------------------
# ANSI snapshot
# ---------------------------------------------------------------------------

SNAPSHOT_SCROLLBACK_LINES = 2000

_PRIVATE_SHIFT = 5
_ALT_SCREEN = 1049
_DECTCEM = 25
_DECAWM = 7
_DECOM = 6
_DECCOLM = 3

_FG_CODES = {name: code for code, name in pyte.graphics.FG_ANSI.items()}
_FG_CODES.update({name: code for code, name in pyte.graphics.FG_AIXTERM.items()})
_BG_CODES = {name: code for code, name in pyte.graphics.BG_ANSI.items()}
_BG_CODES.update({name: code for code, name in pyte.graphics.BG_AIXTERM.items()})

# Style key fields (see _style_key) after fg/bg, with their SGR set codes.
_FLAG_CODES = (1, 3, 4, 9, 7, 5, 2)  # bold italics underscore strike reverse blink dim


def _color(value: str, codes: dict[str, int], extended: int) -> str | None:
    if value == "default":
        return None
    code = codes.get(value)
    if code is not None:
        return str(code)
    if len(value) == 6:
        try:
            r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None
        return f"{extended};2;{r};{g};{b}"
    return None


def _sgr(key: tuple) -> str:
    """The SGR sequence that sets style ``key`` from scratch."""
    parts = ["0"]
    fg = _color(key[0], _FG_CODES, 38)
    bg = _color(key[1], _BG_CODES, 48)
    if fg is not None:
        parts.append(fg)
    if bg is not None:
        parts.append(bg)
    parts.extend(str(code) for flag, code in zip(key[2:], _FLAG_CODES) if flag)
    return f"\x1b[{';'.join(parts)}m"


def _render_line(text: str, runs: tuple) -> str:
    out = [_sgr(key) + text[start:end] for start, end, key in runs]
    if out:
        out.append("\x1b[0m")
    return "".join(out)


def ansi_snapshot(
    screen: ScrollbackScreen, max_scrollback: int = SNAPSHOT_SCROLLBACK_LINES
) -> str:
    """Serialise ``screen`` to ANSI that rebuilds it in an empty terminal of
    the same size.

    In order: a full reset, the last ``max_scrollback`` scrollback lines
    (scrolled off into the receiving terminal's history), the alternate
    screen (DECSET 1049) when the child is on it, every screen row at its
    absolute position, the scroll margins, the terminal modes the child has
    set (mouse tracking, bracketed paste, application cursor keys, cursor
    visibility, autowrap, origin mode, insert mode), the cursor position and
    the current pen.

    Fidelity is what pyte models: no images, hyperlinks, titles, saved
    cursor or charsets, and a cursor parked past the last column (pending
    wrap) is restored on the last column.
    """
    rows, cols = screen.lines, screen.columns
    default = screen.default_char
    intern: dict[tuple, tuple] = {}
    out = ["\x1bc"]

    scrollback = list(screen.scrollback)[-max_scrollback:] if max_scrollback > 0 else []
    if scrollback:
        out.append("\r\n".join(_render_line(*line) for line in scrollback))
        # Push every line into the history: the screen below starts blank.
        out.append("\r\n" * rows)
        out.append("\x1b[H")

    private = {m >> _PRIVATE_SHIFT for m in screen.mode if m >> _PRIVATE_SHIFT}
    public = {m for m in screen.mode if not m >> _PRIVATE_SHIFT}
    if _ALT_SCREEN in private:
        out.append(f"\x1b[?{_ALT_SCREEN}h\x1b[H\x1b[2J")

    for y in range(rows):
        line = _render_line(
            *_encode_scrollback_row(screen.buffer[y], cols, default, intern)
        )
        if line:
            out.append(f"\x1b[{y + 1};1H{line}")

    margins = screen.margins
    if margins is not None and (margins.top, margins.bottom) != (0, rows - 1):
        out.append(f"\x1b[{margins.top + 1};{margins.bottom + 1}r")

    for mode in sorted(private - {_ALT_SCREEN, _DECTCEM, _DECAWM, _DECCOLM}):
        out.append(f"\x1b[?{mode}h")
    for mode in sorted(public):
        out.append(f"\x1b[{mode}h")
    if _DECAWM not in private:
        out.append(f"\x1b[?{_DECAWM}l")
    if screen.cursor.hidden:
        out.append(f"\x1b[?{_DECTCEM}l")

    y = screen.cursor.y
    if _DECOM in private and margins is not None:
        y -= margins.top
    x = min(screen.cursor.x, cols - 1)
    out.append(f"\x1b[{y + 1};{x + 1}H")
    out.append(_sgr(_style_key(screen.cursor.attrs)))
    return "".join(out)
