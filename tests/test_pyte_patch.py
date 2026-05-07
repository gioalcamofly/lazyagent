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


def _row_text(screen: "pyte.Screen", row: int = 0) -> str:
    """Return the visible contents of a screen row as a string."""
    line = screen.buffer[row]
    cols = max(line.keys()) + 1 if line else 0
    return "".join(line[x].data for x in range(cols))


class TestDCSStripping:
    """DCS passthrough sequences (\\eP...\\e\\) must be discarded entirely.

    Claude Code v2.1.x wraps OSC 9 notifications inside tmux DCS passthrough.
    Without filtering, the inner content (``tmux;]9;Claude is waiting...``)
    leaks onto the screen because pyte 0.8.2 has no DCS handler.
    """

    def test_simple_dcs_dropped(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # Plain DCS with single ST terminator (\e\\)
        stream.feed("before\x1bPanything\x1b\\after")
        assert _row_text(screen).rstrip() == "beforeafter"

    def test_tmux_passthrough_dropped(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # The actual sequence Claude Code emits: tmux DCS wrapping OSC 9.
        # Inner ESCs are doubled per tmux passthrough rules.
        stream.feed(
            "X"
            "\x1bPtmux;\x1b\x1b]9;Claude is waiting for your input\x1b\x1b\\\x1b\\"
            "Y"
        )
        assert _row_text(screen).rstrip() == "XY"

    def test_dcs_split_across_chunks(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("hi\x1bPtmux;")
        stream.feed("payload")
        stream.feed("\x1b\\done")
        assert _row_text(screen).rstrip() == "hidone"

    def test_lone_esc_split_across_chunks(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("a\x1b")  # ESC at end of chunk
        stream.feed("[31mred")  # CSI continues in next chunk
        # Should render "ared" with red fg, not "a[31mred" as plain text
        assert _row_text(screen).rstrip() == "ared"
        assert screen.buffer[0][1].fg == "red"


class TestCSISubparamStripping:
    """CSI ``:`` sub-parameters must be stripped before reaching pyte.

    Pyte's CSI parser only knows ``;`` as a separator. When it hits ``:`` it
    dispatches an empty handler and breaks out of the parser, leaking the
    trailing bytes (e.g. ``3m``, ``1u``) as drawn text.
    """

    def test_kitty_extended_underline_does_not_leak(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # \e[4:3m = curly underline (kitty/VTE extended SGR). Without the
        # patch, "3mhello" would appear; with it, "hello" without underline
        # (we drop the atom entirely rather than risk wrong-direction toggle).
        stream.feed("\x1b[4:3mhello")
        assert _row_text(screen).rstrip() == "hello"
        assert screen.buffer[0][0].underscore is False

    def test_extended_underline_off_does_not_turn_on(self):
        """Regression: \\e[4:0m must NOT enable underline.

        \\e[4:0m is extended SGR "underline OFF". A naive rewrite to
        \\e[4m would flip the meaning to "underline ON", causing a
        subsequent screen-clear to fill every cell with underlined
        spaces (the long horizontal lines bug).
        """
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[4:0mhi")
        assert screen.buffer[0][0].underscore is False
        assert screen.buffer[0][1].underscore is False

    def test_clear_screen_with_active_underline_does_not_smear(self):
        """If \\e[4:0m wrongly turned underline on, \\e[2J would stamp
        underline=True on every cell. Verify clean state after clear."""
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # Turn on legacy underline, then "turn off" via extended SGR,
        # then clear screen and write text.
        stream.feed("\x1b[4mUNDER\x1b[4:0m\x1b[2J\x1b[Hclean")
        # Cells beyond "clean" should not be underlined
        assert screen.buffer[0][10].underscore is False
        assert screen.buffer[5][20].underscore is False

    def test_kitty_keyboard_report_does_not_leak(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # \e[97;5:1u — the ;5:1 atom contains ':' and is dropped, leaving
        # \e[97u which is a pyte 'restore cursor' (no-op without saved pos).
        stream.feed("text\x1b[97;5:1umore")
        assert "1u" not in _row_text(screen)
        assert "more" in _row_text(screen)

    def test_subparam_atom_dropped_in_middle(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # \e[1;4:3;31m → bold, curly-underline (atom dropped), red fg.
        # After filtering: \e[1;31m → bold + red, no underline.
        stream.feed("\x1b[1;4:3;31mX")
        ch = screen.buffer[0][0]
        assert ch.data == "X"
        assert ch.bold is True
        assert ch.underscore is False
        assert ch.fg == "red"

    def test_all_atoms_dropped_emits_nothing(self):
        """\\e[4:0m alone (single atom, all subparams) must emit nothing,
        not a bare \\e[m which pyte interprets as SGR 0 (reset all)."""
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # Set bold + red, then a single-atom subparam'd SGR, then write.
        # If we wrongly emitted \e[m, bold and red would be reset.
        stream.feed("\x1b[1;31m\x1b[4:0mX")
        ch = screen.buffer[0][0]
        assert ch.bold is True
        assert ch.fg == "red"

    def test_plain_csi_unchanged(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[1;31mbold red")
        ch = screen.buffer[0][0]
        assert ch.bold is True
        assert ch.fg == "red"

    def test_private_csi_with_subparam(self):
        """Private CSI (\\e[?...) with a subparam must keep the '?'."""
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # \e[?25h shows cursor, plain — must still work
        stream.feed("\x1b[?25h")
        assert screen.cursor.hidden is False
        stream.feed("\x1b[?25l")
        assert screen.cursor.hidden is True

    def test_subparam_split_across_chunks(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[4:")  # CSI starts, hits ':' at boundary
        stream.feed("3mhi")
        # Atom dropped, so "hi" rendered without underline
        assert _row_text(screen).rstrip() == "hi"
        assert screen.buffer[0][0].underscore is False


class TestPrivateCSIMarkers:
    """``<`` and ``=`` private CSI markers must not leak.

    Pyte's CSI parser only knows ``?`` (private mode) and ``>`` (in
    SP_OR_GT). For ``<`` and ``=`` it falls through to the debug no-op
    and breaks out of CSI parsing, leaking the rest of the sequence as
    drawn text. Claude Code's kitty-keyboard exit (``\\e[<u``) hits this
    and was the source of the stray "u" in the input area.
    """

    def test_kitty_keyboard_pop_does_not_leak_u(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("hello\x1b[<uworld")
        # Without the patch, "uworld" appears; with it, just "helloworld"
        # because \e[<u is rewritten to \e[u (restore_cursor with no args).
        assert _row_text(screen).rstrip() == "helloworld"

    def test_kitty_keyboard_pop_with_count_does_not_leak(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("a\x1b[<5ub")
        assert _row_text(screen).rstrip() == "ab"

    def test_kitty_keyboard_push_still_works(self):
        """``\\e[>4u`` (kitty keyboard push) must be a no-op, not a leak."""
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("a\x1b[>4ub")
        assert _row_text(screen).rstrip() == "ab"

    def test_xterm_modifyotherkeys_does_not_set_underline(self):
        """Regression: ``\\e[>4m`` (xterm modifyOtherKeys reset) must NOT
        enable underline. Pyte's parser silently strips ``>`` and would
        otherwise dispatch SGR 4. Claude Code emits this on every prompt
        cycle; misdispatch causes underline to leak across every blank
        cell pyte writes (long horizontal lines spanning the panel).
        """
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[>4m hello \x1b[2J\x1b[Hclean")
        # Cells beyond "clean" must not be underlined
        assert screen.buffer[0][10].underscore is False
        assert screen.buffer[10][50].underscore is False

    def test_xterm_modifyotherkeys_set_does_not_set_underline_or_dim(self):
        """``\\e[>4;2m`` would otherwise dispatch as SGR 4, 2 (underline + dim)."""
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[>4;2mX")
        ch = screen.buffer[0][0]
        assert ch.data == "X"
        assert ch.underscore is False
        assert ch.dim is False

    def test_equals_private_marker_does_not_leak(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # \e[=Nc would normally leak "Nc" — verify it's eaten cleanly.
        stream.feed("a\x1b[=1cb")
        assert _row_text(screen).rstrip() == "ab"

    def test_question_private_csi_still_works(self):
        """``?`` private CSI must keep its behaviour (mode set/reset)."""
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[?25h")
        assert screen.cursor.hidden is False
        stream.feed("\x1b[?25l")
        assert screen.cursor.hidden is True
