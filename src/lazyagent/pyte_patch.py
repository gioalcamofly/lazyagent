"""Monkey-patch pyte to support SGR 2 (dim/faint) text and to filter newer
escape sequences pyte 0.8.2 doesn't recognize.

Import this module before any pyte usage. It:

1. Replaces ``pyte.screens.Char`` with an extended version that adds a
   ``dim: bool = False`` field (the 10th field).  Since all existing pyte
   code creates Chars with positional args for the original 9 fields (or
   ``_replace()``), adding a 10th field with a default is backward-compatible.

2. Patches ``pyte.graphics.TEXT`` so that SGR 2 maps to ``"+dim"``.

3. Overrides ``pyte.Screen.select_graphic_rendition`` so that SGR 22 resets
   **both** bold and dim (per ANSI spec, SGR 22 = "normal intensity").

4. Wraps ``pyte.streams.Stream.feed`` with a state-machine pre-filter that:
   - Drops DCS sequences (``\\eP...\\e\\``). Claude Code v2.1.x uses these for
     tmux passthrough of OSC 9 notifications; without filtering, the literal
     content (``tmux;]9;Claude is waiting for your input``) leaks into the
     screen because pyte has no DCS dispatch.
   - Strips colon-separated CSI sub-parameters (``\\e[4:3m`` → ``\\e[4m``).
     Pyte's CSI parser only knows ``;`` as a separator; on ``:`` it dispatches
     an empty handler and breaks, leaking the trailing bytes as drawn text.
     Affects kitty extended underline (``\\e[4:Nm``), kitty keyboard reports
     (``\\e[<code>;<mods>:<event>u``), and ITU T.416 separators.
"""

from __future__ import annotations

from typing import NamedTuple

import pyte.graphics
import pyte.screens
import pyte.streams

# ---------------------------------------------------------------------------
# 1. Extended Char with `dim` field
# ---------------------------------------------------------------------------


class Char(NamedTuple):
    data: str
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False
    dim: bool = False


pyte.screens.Char = Char

# Also fix Cursor.__init__'s default `attrs` argument, which was evaluated at
# class-definition time with the original 9-field Char.
pyte.screens.Cursor.__init__.__defaults__ = (Char(" "),)

# ---------------------------------------------------------------------------
# 2. SGR 2 → "+dim" in the TEXT table
# ---------------------------------------------------------------------------

pyte.graphics.TEXT[2] = "+dim"

# ---------------------------------------------------------------------------
# 3. SGR 22 must reset both bold and dim
# ---------------------------------------------------------------------------

_orig_sgr = pyte.screens.Screen.select_graphic_rendition


def _patched_sgr(self: pyte.screens.Screen, *attrs: int) -> None:
    _orig_sgr(self, *attrs)
    # If SGR 22 was in the attrs list, also reset dim (the original only
    # resets bold because TEXT[22] maps to "-bold").
    if 22 in attrs:
        self.cursor.attrs = self.cursor.attrs._replace(dim=False)


pyte.screens.Screen.select_graphic_rendition = _patched_sgr

# ---------------------------------------------------------------------------
# 4. Stream-level pre-filter for DCS and CSI sub-parameters
# ---------------------------------------------------------------------------


class _StreamFilter:
    """Per-stream state machine that scrubs DCS and CSI ``:``-sub-params.

    Holds escape state across ``feed()`` chunks so sequences split mid-byte
    are still handled correctly. Drops complete DCS (``\\eP...\\e\\``)
    sequences entirely. Buffers each CSI sequence and rewrites it as a
    unit, dropping any ``;``-separated atom that contains ``:`` so neither
    the leading number nor its sub-parameters reach pyte.

    Why drop the whole atom: keeping just the leading number is wrong for
    SGR — e.g. ``\\e[4:0m`` semantically means "underline OFF" (extended
    SGR), but rewriting to ``\\e[4m`` would apply legacy "underline ON".
    Dropping the whole atom leaves the underline state unchanged, which
    is the safe behaviour when we cannot honour the sub-parameter.
    """

    NORMAL = 0
    SAW_ESC = 1
    IN_CSI = 2
    IN_DCS = 3
    DCS_SAW_ESC = 4

    __slots__ = ("state", "csi_buf")

    def __init__(self) -> None:
        self.state = self.NORMAL
        self.csi_buf = ""

    def filter(self, data: str) -> str:  # noqa: A003 — match common naming
        out: list[str] = []
        state = self.state
        csi_buf = self.csi_buf
        for ch in data:
            if state == self.NORMAL:
                if ch == "\x1b":
                    state = self.SAW_ESC
                else:
                    out.append(ch)
            elif state == self.SAW_ESC:
                if ch == "[":
                    csi_buf = ""
                    state = self.IN_CSI
                elif ch == "P":
                    # DCS: discard \eP and everything until ST (\e\\).
                    state = self.IN_DCS
                else:
                    # Other ESC-prefixed sequences pass through unchanged
                    # (pyte handles them, e.g. \eD, \eM, \eOA, \e]...).
                    out.append("\x1b" + ch)
                    state = self.NORMAL
            elif state == self.IN_CSI:
                if "\x40" <= ch <= "\x7e":  # CSI final byte (0x40–0x7E)
                    out.append(_rewrite_csi(csi_buf, ch))
                    csi_buf = ""
                    state = self.NORMAL
                else:
                    csi_buf += ch
            elif state == self.IN_DCS:
                if ch == "\x1b":
                    state = self.DCS_SAW_ESC
                # else: content byte, drop
            elif state == self.DCS_SAW_ESC:
                # In tmux DCS passthrough, literal ESCs in the inner content
                # are doubled (\e\e). The terminator is a single \e\\. So
                # \e<not-backslash> means the previous \e was either the
                # first half of a doubled-escape or stray content — either
                # way, we are not at the terminator.
                if ch == "\\":
                    state = self.NORMAL
                else:
                    state = self.IN_DCS
        self.state = state
        self.csi_buf = csi_buf
        return "".join(out)


def _rewrite_csi(params: str, final: str) -> str:
    """Rewrite a CSI sequence so pyte 0.8.2 parses it cleanly.

    ``params`` is everything between ``\\e[`` and the final byte (it may
    include a leading private/intermediate marker). ``final`` is the final
    byte. We do three things:

    1. **Drop entirely any CSI that starts with** ``<``, ``=``, **or** ``>``.
       These prefix bytes mark xterm/kitty extensions pyte does not
       implement. Worse, pyte's parser silently *strips* the ``>`` (it's
       in ``SP_OR_GT``) and dispatches the remainder as if it were a
       standard CSI — so ``\\e[>4m`` (xterm modifyOtherKeys mode) gets
       misread as ``\\e[4m`` = SGR 4 = underline ON, which then bleeds
       across every subsequent blank cell pyte writes (visible as long
       horizontal lines spanning the panel). ``<`` and ``=`` fall through
       to the no-op ``debug`` dispatch and break out of CSI early,
       leaking the trailing bytes as drawn text (e.g. the stray ``u``
       from kitty's ``\\e[<u`` pop). Dropping these sequences entirely
       is the only correct option until pyte gains real support.

    2. Preserve ``?`` private-CSI prefix. Pyte handles it correctly
       (mode set / reset).

    3. Drop ``;``-separated atoms that contain ``:`` (extended SGR
       sub-params). Keeping just the leading number would invert the
       semantics of e.g. ``\\e[4:0m`` (off → on).
    """
    if params and params[0] in "<=>":
        return ""

    prefix = ""
    rest = params
    if rest and rest[0] == "?":
        prefix = "?"
        rest = rest[1:]

    if ":" not in rest:
        return f"\x1b[{prefix}{rest}{final}"

    atoms = rest.split(";")
    kept = [a for a in atoms if ":" not in a]
    if not kept:
        # Bare \e[m would be SGR 0 / full reset — too aggressive.
        # Emit nothing so screen state stays intact.
        return ""
    return f"\x1b[{prefix}{';'.join(kept)}{final}"


_orig_feed = pyte.streams.Stream.feed


def _patched_feed(self: pyte.streams.Stream, data: str) -> None:
    flt: _StreamFilter | None = getattr(self, "_lazyagent_filter", None)
    if flt is None:
        flt = _StreamFilter()
        self._lazyagent_filter = flt
    _orig_feed(self, flt.filter(data))


pyte.streams.Stream.feed = _patched_feed
