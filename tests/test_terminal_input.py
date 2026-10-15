"""Tests for programmatic terminal input (send_agent_input / send_input).

The bug these cover: text sent to an agent CLI landed in its prompt but was
never submitted, so a human had to press Enter. Agent CLIs built on Ink
(Claude Code, Codex, Gemini) split a control character out of a stdin chunk
as its own key event *only while the chunk is short*; past that threshold the
trailing CR is absorbed into the surrounding text run and inserted as content.
Framing the payload as a bracketed paste puts the CR outside that run no
matter how the writes coalesce.
"""

from __future__ import annotations

import asyncio

import pyte
import pytest

from lazyagent.widgets.scrollable_terminal import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    ScrollableTerminal,
    ScrollbackScreen,
)


# ---------------------------------------------------------------------------
# Minimal model of the CLI-side tokeniser
# ---------------------------------------------------------------------------

# Chunk length past which the agent CLI stops treating control characters as
# their own key and folds them into the surrounding text run.
_CONTROL_SPLIT_LIMIT = 64


def tokenize(chunk: str) -> list[tuple[str, str]]:
    """Split a stdin chunk the way an Ink-based agent CLI does.

    Reduced to the two rules that matter here: CSI sequences terminate a text
    run, and a control character only becomes its own token while the whole
    chunk is under ``_CONTROL_SPLIT_LIMIT``. Returns ``(kind, value)`` pairs
    with kind in ``{"text", "sequence"}``.
    """
    tokens: list[tuple[str, str]] = []
    run_start = 0
    i = 0

    def flush(end: int) -> None:
        nonlocal run_start
        if end > run_start:
            tokens.append(("text", chunk[run_start:end]))
        run_start = end

    while i < len(chunk):
        ch = chunk[i]
        if ch == "\x1b" and chunk[i + 1 : i + 2] == "[":
            flush(i)
            end = i + 2
            while end < len(chunk) and not chunk[end].isalpha() and chunk[end] != "~":
                end += 1
            end += 1
            tokens.append(("sequence", chunk[i:end]))
            i = run_start = end
            continue
        if ch < " " and len(chunk) < _CONTROL_SPLIT_LIMIT:
            flush(i)
            tokens.append(("text", ch))
            i = run_start = i + 1
            continue
        i += 1

    flush(len(chunk))
    return tokens


def submits(chunk: str) -> bool:
    """True if the CLI would see a bare Enter outside any pasted content."""
    in_paste = False
    for kind, value in tokenize(chunk):
        if kind == "sequence":
            if value == BRACKETED_PASTE_START:
                in_paste = True
            elif value == BRACKETED_PASTE_END:
                in_paste = False
            continue
        if not in_paste and value == "\r":
            return True
    return False


LONG = "please review the retry logic in the worker and tell me if it is right"
SHORT = "yes"


class TestTokenizerModel:
    def test_short_payload_submits_even_unframed(self):
        """Why the bug looked intermittent: short messages did work."""
        assert len(SHORT + "\r") < _CONTROL_SPLIT_LIMIT
        assert submits(SHORT + "\r") is True

    def test_long_payload_does_not_submit_unframed(self):
        """The old ``text + "\\r"`` payload — CR swallowed into the text run."""
        assert len(LONG + "\r") >= _CONTROL_SPLIT_LIMIT
        assert submits(LONG + "\r") is False

    def test_bracketed_paste_submits_even_when_coalesced(self):
        """The fix holds even if the tty merges both writes into one read."""
        chunk = BRACKETED_PASTE_START + LONG + BRACKETED_PASTE_END + "\r"
        assert len(chunk) >= _CONTROL_SPLIT_LIMIT
        assert submits(chunk) is True

    def test_bracketed_paste_body_is_not_read_as_keys(self):
        """Newlines inside the payload stay content, they don't submit early."""
        body = "line one\nline two\nline three"
        chunk = BRACKETED_PASTE_START + body + BRACKETED_PASTE_END + "\r"
        pasted = [
            value
            for kind, value in tokenize(chunk)
            if kind == "text" and value != "\r"
        ]
        assert "".join(pasted) == body
        assert submits(chunk) is True


# ---------------------------------------------------------------------------
# ScrollableTerminal framing + submit sequencing
# ---------------------------------------------------------------------------


def _make_terminal(*, bracketed_paste: bool = True) -> ScrollableTerminal:
    """A ScrollableTerminal with a fake emulator and a real send queue."""
    t = ScrollableTerminal.__new__(ScrollableTerminal)
    t.ncol = 80
    t.nrow = 5
    t.mouse_tracking = False
    t.bracketed_paste = bracketed_paste
    t._output_event = asyncio.Event()
    t.emulator = object()
    t.send_queue = asyncio.Queue()
    t._screen = ScrollbackScreen(80, 5)
    t.stream = pyte.Stream(t._screen)
    return t


def _drain(queue: asyncio.Queue) -> list[list]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class TestFrameInput:
    def test_wraps_in_paste_markers_when_child_asked_for_them(self):
        t = _make_terminal(bracketed_paste=True)
        assert t.frame_input("hello") == (
            BRACKETED_PASTE_START + "hello" + BRACKETED_PASTE_END
        )

    def test_no_markers_when_child_did_not_enable_them(self):
        """Unrequested markers would be inserted as literal junk."""
        t = _make_terminal(bracketed_paste=False)
        assert t.frame_input("hello") == "hello"

    def test_carriage_returns_in_body_become_newlines(self):
        t = _make_terminal(bracketed_paste=False)
        assert t.frame_input("a\r\nb\rc") == "a\nb\nc"

    def test_embedded_end_marker_is_stripped(self):
        """Otherwise the payload could close its own paste and inject keys."""
        t = _make_terminal(bracketed_paste=True)
        framed = t.frame_input(f"safe{BRACKETED_PASTE_END}\rrm -rf /")
        assert framed.count(BRACKETED_PASTE_END) == 1
        assert framed.endswith(BRACKETED_PASTE_END)


class TestSendInput:
    @pytest.mark.asyncio
    async def test_submit_key_is_a_separate_write(self):
        t = _make_terminal()
        t._output_event.set()  # child already acknowledged

        await t.send_input(LONG, submit=True)

        writes = _drain(t.send_queue)
        assert writes == [
            ["stdin", BRACKETED_PASTE_START + LONG + BRACKETED_PASTE_END],
            ["stdin", "\r"],
        ]

    @pytest.mark.asyncio
    async def test_no_submit_key_when_not_submitting(self):
        t = _make_terminal()
        await t.send_input("hello", submit=False)
        assert len(_drain(t.send_queue)) == 1

    @pytest.mark.asyncio
    async def test_submit_waits_for_the_child_to_acknowledge(self):
        """The CR is held until the child produces output after our write."""
        t = _make_terminal()

        task = asyncio.create_task(t.send_input(LONG, submit=True))
        await asyncio.sleep(0.05)

        assert [w[1] for w in _drain(t.send_queue)] == [
            BRACKETED_PASTE_START + LONG + BRACKETED_PASTE_END
        ]

        t._output_event.set()  # child redrew — it has read the paste
        await task
        assert [w[1] for w in _drain(t.send_queue)] == ["\r"]

    @pytest.mark.asyncio
    async def test_submits_anyway_when_the_child_stays_silent(self, monkeypatch):
        """A quiet agent must not swallow the instruction entirely."""
        monkeypatch.setattr(
            "lazyagent.widgets.scrollable_terminal.SUBMIT_ACK_TIMEOUT", 0.05
        )
        t = _make_terminal()

        await t.send_input(LONG, submit=True)

        assert [w[1] for w in _drain(t.send_queue)][-1] == "\r"

    @pytest.mark.asyncio
    async def test_stale_output_does_not_count_as_acknowledgement(self):
        """Output from before our write must not release the submit key."""
        t = _make_terminal()
        t._output_event.set()  # output from before we wrote anything

        task = asyncio.create_task(t.send_input(LONG, submit=True))
        await asyncio.sleep(0)
        # send_input clears the flag before writing, so this is a fresh wait.
        assert t._output_event.is_set() is False
        t._output_event.set()
        await task

    @pytest.mark.asyncio
    async def test_raises_when_the_terminal_is_not_running(self):
        t = _make_terminal()
        t.emulator = None
        with pytest.raises(RuntimeError):
            await t.send_input("hello", submit=True)


class TestModeTracking:
    def test_bracketed_paste_enable_and_disable(self):
        t = _make_terminal(bracketed_paste=False)
        t._scan_terminal_modes("\x1b[?2004h")
        assert t.bracketed_paste is True
        t._scan_terminal_modes("\x1b[?2004l")
        assert t.bracketed_paste is False

    def test_mode_in_a_multi_parameter_decset(self):
        t = _make_terminal(bracketed_paste=False)
        t._scan_terminal_modes("\x1b[?1000;1006;2004h")
        assert t.bracketed_paste is True
        assert t.mouse_tracking is True

    def test_mouse_tracking_still_tracked(self):
        t = _make_terminal()
        t._scan_terminal_modes("\x1b[?1000h")
        assert t.mouse_tracking is True
        t._scan_terminal_modes("\x1b[?1000l")
        assert t.mouse_tracking is False

    def test_unrelated_sequences_are_ignored(self):
        t = _make_terminal(bracketed_paste=False)
        t._scan_terminal_modes("\x1b[2J\x1b[?25l\x1b[1;5H")
        assert t.bracketed_paste is False
        assert t.mouse_tracking is False


class TestModeTrackingWhileHidden:
    """A hidden agent is the normal MCP target; its modes must stay current."""

    @pytest.mark.asyncio
    async def test_recv_tracks_modes_for_a_zero_height_widget(self):
        from unittest.mock import PropertyMock, patch

        from textual.geometry import Size

        t = _make_terminal(bracketed_paste=False)
        t._stopped = False
        t._hidden_feed_buffer = []
        t._hidden_feed_handle = None
        t.recv_queue = asyncio.Queue()
        t.recv_queue.put_nowait(["stdout", "\x1b[?2004h"])

        with patch.object(
            type(t), "size", new_callable=PropertyMock, return_value=Size(0, 0)
        ):
            task = asyncio.create_task(t.recv())
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert t.bracketed_paste is True
        assert t._output_event.is_set() is True
