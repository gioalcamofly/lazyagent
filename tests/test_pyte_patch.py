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


class TestDoubledEscape:
    """Doubled ESC outside DCS context (xterm cancel-and-restart).

    A naive filter would emit both ESCs literally and return to NORMAL,
    bypassing the colon-stripping pass for any CSI that follows. Verify
    the second ESC is treated as the start of a new escape and the
    sub-param filter still fires.
    """

    def test_doubled_esc_then_subparam_does_not_leak(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        # \x1b\x1b[4:0m — second ESC must be picked up as a new escape
        # introducer and the colon atom dropped. Without the fix, "4:0m"
        # could leak as drawn text or wrongly enable underline.
        stream.feed("a\x1b\x1b[4:0mb")
        assert _row_text(screen).rstrip() == "ab"
        assert screen.buffer[0][0].underscore is False
        assert screen.buffer[0][1].underscore is False

    def test_doubled_esc_then_csi_color(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b\x1b[31mred")
        assert _row_text(screen).rstrip() == "red"
        assert screen.buffer[0][0].fg == "red"

    def test_triple_esc_then_csi(self):
        """Three ESCs in a row: first two cancel, third starts escape."""
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b\x1b\x1b[31mred")
        assert _row_text(screen).rstrip() == "red"
        assert screen.buffer[0][0].fg == "red"


class TestRunawayBuffers:
    """A malformed stream that opens CSI/DCS and never closes must not
    grow memory unboundedly. We assert against filter state directly
    because the recovery side-effect (flushing accumulated bytes as text)
    can scroll the visible screen, making screen-based assertions awkward.
    """

    def _filter(self):
        from lazyagent.pyte_patch import _StreamFilter
        return _StreamFilter()

    def test_csi_buffer_is_bounded(self):
        flt = self._filter()
        # Feed a CSI that never finalises, well past the cap.
        flt.filter("\x1b[" + "9;" * 500)
        assert len(flt.csi_buf) <= flt.MAX_CSI_LEN

    def test_csi_runaway_recovers_to_normal(self):
        flt = self._filter()
        flt.filter("\x1b[" + "9;" * 500)
        # After the cap is hit the filter must abort CSI and return to
        # NORMAL so subsequent input is processed correctly.
        out = flt.filter("\x1b[31mX")
        assert "\x1b[31mX" in out
        assert flt.state == flt.NORMAL

    def test_dcs_runaway_recovers_to_normal(self):
        flt = self._filter()
        # Feed a DCS that never terminates, well past the 64 KB cap.
        flt.filter("\x1bP" + "x" * 100000)
        # After the cap is hit the filter must abort DCS and return to
        # NORMAL. Subsequent properly-terminated DCS is dropped, and text
        # after it is rendered.
        out = flt.filter("\x1bPpayload\x1b\\after")
        assert "after" in out
        assert flt.state == flt.NORMAL


class TestDrawFastPath:
    """The draw() fast path must be indistinguishable from stock pyte.

    Each case feeds identical input to two screens — one running the patched
    draw, one running the original — and compares the entire resulting state.
    The cases deliberately cover everything the fast path bails out of.
    """

    @staticmethod
    def _state(screen):
        return (
            {y: dict(screen.buffer[y]) for y in range(screen.lines)},
            screen.cursor.x,
            screen.cursor.y,
            sorted(screen.dirty),
        )

    def _compare(self, feeds, columns=20, lines=4, use_utf8=True):
        import lazyagent.pyte_patch as pp

        patched = pyte.Screen(columns, lines)
        stock = pyte.Screen(columns, lines)

        patched_stream = pyte.Stream(patched)
        patched_stream.use_utf8 = use_utf8
        for chunk in feeds:
            patched_stream.feed(chunk)

        original = pyte.screens.Screen.draw
        pyte.screens.Screen.draw = pp._orig_draw
        try:
            stock_stream = pyte.Stream(stock)
            stock_stream.use_utf8 = use_utf8
            for chunk in feeds:
                stock_stream.feed(chunk)
        finally:
            pyte.screens.Screen.draw = original

        assert self._state(patched) == self._state(stock)
        return patched

    def test_plain_ascii(self):
        self._compare(["hello world"])

    def test_empty_draw(self):
        self._compare(["\x1b[0m"])

    def test_styled_runs(self):
        self._compare(["\x1b[1;31mred\x1b[0m \x1b[4;42mgreen\x1b[0m tail"])

    def test_run_exactly_fills_the_line(self):
        self._compare(["x" * 20])

    def test_run_overflows_the_line(self):
        """Wrapping is delegated to the original implementation."""
        self._compare(["y" * 55])

    def test_draw_at_the_right_margin(self):
        """The pending-wrap state (cursor.x == columns) must still wrap."""
        self._compare(["\x1b[1;20Hab"])

    def test_autowrap_disabled(self):
        self._compare(["\x1b[?7l", "z" * 40])

    def test_insert_mode(self):
        """IRM shifts existing cells right — never the fast path."""
        self._compare(["abcdef", "\x1b[1;3H", "\x1b[4h", "XY"])

    def test_vt100_box_drawing_charset(self):
        r"""\e(0 remaps ASCII to box drawing; the translate pass is required.

        pyte ignores charset selection while ``use_utf8`` is set (the default
        for ``pyte.Stream``), so this drives it with ``use_utf8=False`` to
        actually reach the branch the fast path bails out of.
        """
        screen = self._compare(["\x1b(0qqlkmj\x1b(B"], use_utf8=False)
        # Sanity: the mapping really did happen (not just "both are wrong")
        assert screen.buffer[0][0].data == "─"

    def test_g1_charset_via_shift_out(self):
        screen = self._compare(["\x1b)0\x0eqqq\x0f"], use_utf8=False)
        assert screen.buffer[0][0].data == "─"

    def test_charset_selection_ignored_under_utf8(self):
        """Documents why the charset guard rarely fires in lazyagent.

        ``pyte.Stream`` defaults to ``use_utf8=True``, under which both SCS
        (``\\e(0``) and SO/SI are dropped — the active map stays Latin-1. The
        guard stays in place because the fast path must not depend on a flag
        set somewhere else.
        """
        screen = self._compare(["\x1b(0qqq"])
        assert screen.buffer[0][0].data == "q"

    def test_wide_characters(self):
        """CJK is two cells wide and leaves a stub — not the fast path."""
        self._compare(["你好 ok"])

    def test_combining_characters(self):
        """Combining marks merge into the preceding cell."""
        self._compare(["éclair"])

    def test_non_ascii_latin1(self):
        self._compare(["heló wörld"])

    def test_del_character_is_not_printable(self):
        self._compare(["ab\x7fcd"])

    def test_mixed_stream(self):
        self._compare([
            "\x1b[2J\x1b[H",
            "\x1b[1;36mplain ascii line\x1b[0m\r\n",
            "你好 wide\r\n",
            "\x1b(0qqqq\x1b(B\r\n",
            "tail" * 12,
        ])

    def test_repeated_characters_share_one_char_object(self):
        """The per-call cache shares cells; Char is immutable so this is safe."""
        screen = pyte.Screen(20, 2)
        pyte.Stream(screen).feed("aaab")
        assert screen.buffer[0][0] is screen.buffer[0][1]
        assert screen.buffer[0][0] is not screen.buffer[0][3]
        assert screen.buffer[0][0].data == "a"
        assert screen.buffer[0][3].data == "b"
