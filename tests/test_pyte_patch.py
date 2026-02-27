"""Tests for the pyte monkey-patch (dim/faint support)."""

from __future__ import annotations

import lazyagent.pyte_patch  # noqa: F401 — must be imported before pyte.screens.Char

import pyte
import pyte.graphics
from pyte.screens import Char


class TestCharPatch:
    def test_char_has_10_fields(self):
        assert len(Char._fields) == 10

    def test_dim_field_exists(self):
        assert "dim" in Char._fields

    def test_dim_defaults_to_false(self):
        c = Char(data="x")
        assert c.dim is False

    def test_replace_dim(self):
        c = Char(data="x")
        c2 = c._replace(dim=True)
        assert c2.dim is True
        assert c2.data == "x"

    def test_backward_compat_9_positional_args(self):
        """Creating Char with original 9 positional args still works."""
        c = Char("a", "red", "blue", True, False, True, False, True, False)
        assert c.data == "a"
        assert c.fg == "red"
        assert c.bg == "blue"
        assert c.bold is True
        assert c.italics is False
        assert c.underscore is True
        assert c.strikethrough is False
        assert c.reverse is True
        assert c.blink is False
        assert c.dim is False  # default


class TestGraphicsPatch:
    def test_sgr2_in_text_table(self):
        assert 2 in pyte.graphics.TEXT
        assert pyte.graphics.TEXT[2] == "+dim"


class TestSGRIntegration:
    """Integration tests: feed ANSI sequences through pyte and check results."""

    def test_sgr2_sets_dim(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[2mhello")
        char = screen.buffer[0][0]
        assert char.dim is True
        assert char.data == "h"

    def test_sgr22_resets_dim_and_bold(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[1;2mbold+dim\x1b[22mnormal")
        # "n" of "normal" should have both bold and dim reset
        # "bold+dim" starts at col 0, "normal" starts at col 8
        dim_char = screen.buffer[0][0]
        assert dim_char.bold is True
        assert dim_char.dim is True

        normal_char = screen.buffer[0][8]
        assert normal_char.bold is False
        assert normal_char.dim is False

    def test_sgr0_resets_dim(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[2mdim\x1b[0mnormal")
        dim_char = screen.buffer[0][0]
        assert dim_char.dim is True

        normal_char = screen.buffer[0][3]
        assert normal_char.dim is False
