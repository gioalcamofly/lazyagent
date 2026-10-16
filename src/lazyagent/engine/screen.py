"""Headless terminal screen: a pyte ``Screen`` that keeps a scrollback.

The engine feeds one of these per session whatever any client shows; the TUI
renders straight from it in-process.
"""

from __future__ import annotations

from collections import deque

# The patch replaces pyte.screens.Char, so it has to land before the names
# below are bound and before any Screen is built.
# isort: off
import lazyagent.pyte_patch  # noqa: F401
import pyte
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
