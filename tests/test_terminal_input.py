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

from unittest.mock import AsyncMock

import pytest

from lazyagent.engine.session import TerminalSession
from lazyagent.widgets.scrollable_terminal import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    ScrollableTerminal,
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
# The widget forwards input to its session
# ---------------------------------------------------------------------------
#
# Framing, submit sequencing and mode tracking belong to the session and are
# tested in tests/engine/test_session.py.


class _RecordingSession(TerminalSession):
    def __init__(self, *, bracketed_paste: bool = False, mouse_tracking: bool = False):
        super().__init__("unused")
        self.bracketed_paste = bracketed_paste
        self.mouse_tracking = mouse_tracking
        self.writes: list[str] = []

    @property
    def running(self) -> bool:
        return True

    def write(self, data) -> None:
        self.writes.append(data)


def _make_terminal(session: TerminalSession) -> ScrollableTerminal:
    t = ScrollableTerminal.__new__(ScrollableTerminal)
    t.session = session
    t.ctrl_keys = {"up": "\x1bOA"}
    t._follow_output = True
    return t


class TestWidgetInput:
    @pytest.mark.asyncio
    async def test_keys_are_written_to_the_session(self):
        from textual import events

        session = _RecordingSession()
        t = _make_terminal(session)
        await t.on_key(events.Key("a", "a"))
        await t.on_key(events.Key("up", None))
        assert session.writes == ["a", "\x1bOA"]

    @pytest.mark.asyncio
    async def test_paste_is_framed_by_the_session_modes(self):
        from textual import events

        session = _RecordingSession(bracketed_paste=True)
        t = _make_terminal(session)
        await t.on_paste(events.Paste("one\ntwo"))
        assert session.writes == [
            BRACKETED_PASTE_START + "one\ntwo" + BRACKETED_PASTE_END
        ]

    @pytest.mark.asyncio
    async def test_send_input_goes_through_the_session(self):
        session = _RecordingSession()
        session.send_input = AsyncMock()
        t = _make_terminal(session)
        await t.send_input("hi", submit=True)
        session.send_input.assert_awaited_once_with("hi", submit=True)

    def test_mouse_reports_are_sgr_encoded(self):
        from lazyagent.widgets.scrollable_terminal import _sgr_mouse

        assert _sgr_mouse(0, 4, 2, release=True) == "\x1b[<0;5;3M\x1b[<0;5;3m"
        assert _sgr_mouse(64, 0, 0) == "\x1b[<64;1;1M"
