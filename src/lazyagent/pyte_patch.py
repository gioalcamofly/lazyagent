"""Monkey-patch pyte to support SGR 2 (dim/faint) text.

Import this module before any pyte usage. It:

1. Replaces ``pyte.screens.Char`` with an extended version that adds a
   ``dim: bool = False`` field (the 10th field).  Since all existing pyte
   code creates Chars with positional args for the original 9 fields (or
   ``_replace()``), adding a 10th field with a default is backward-compatible.

2. Patches ``pyte.graphics.TEXT`` so that SGR 2 maps to ``"+dim"``.

3. Overrides ``pyte.Screen.select_graphic_rendition`` so that SGR 22 resets
   **both** bold and dim (per ANSI spec, SGR 22 = "normal intensity").
"""

from __future__ import annotations

from typing import NamedTuple

import pyte.graphics
import pyte.screens

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
