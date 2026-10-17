"""AgentSession / TerminalSession: output, screen, status, input and exit.

Unit tests drive a session through a fake emulator (no process); the
``TestRealPty`` tests run tests/engine/fake_agent.py under a real PTY.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lazyagent.agent_observers import AgentLifecycleEvent, AgentObserver
from lazyagent.engine import session as session_mod
from lazyagent.engine.session import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    HANG_SECONDS,
    UNWATCHED_FEED_INTERVAL,
    AgentSession,
    ByteRing,
    RingOffsetError,
    SessionExited,
    StatusChanged,
    TerminalSession,
)
from lazyagent.models import AgentStatus, LifecycleConfidence

WT_PATH = "/home/user/repo-worktree"
FAKE_AGENT = Path(__file__).with_name("fake_agent.py")

LOW = LifecycleConfidence.LOW
HIGH = LifecycleConfidence.HIGH


class _FakeEmulator:
    """Stands in for PtyEmulator: records writes, spawns nothing."""

    pid = 0
    returncode = None

    def __init__(self) -> None:
        self.recv_queue: asyncio.Queue = asyncio.Queue()
        self.send_queue: asyncio.Queue = asyncio.Queue()

    def stop(self) -> None:
        pass


def _writes(emulator: _FakeEmulator) -> list:
    items = []
    while not emulator.recv_queue.empty():
        items.append(emulator.recv_queue.get_nowait())
    return [item[1] for item in items if item[0] == "stdin"]


def _observer() -> MagicMock:
    observer = MagicMock(spec=AgentObserver)
    observer.on_terminal_output.return_value = []
    observer.on_screen_update.return_value = []
    observer.on_disconnect.return_value = []
    observer.on_process_started.return_value = []
    observer.poll.return_value = []
    return observer


def _agent(*, rows: int = 24, cols: int = 80, running: bool = True):
    """An AgentSession with a mock observer and a fake emulator; returns
    ``(session, observer, events)``."""
    observer = _observer()
    session = AgentSession(
        "unused",
        worktree_path=WT_PATH,
        agent_id="a1",
        observer=observer,
        rows=rows,
        cols=cols,
    )
    if running:
        session._emulator = _FakeEmulator()
    events: list = []
    session.add_listener(events.append)
    return session, observer, events


def _statuses(events) -> list[AgentStatus]:
    return [e.status for e in events if isinstance(e, StatusChanged)]


def _waiting_when(phrase: str):
    """on_screen_update side effect: WAITING while ``phrase`` is on screen."""

    def scan(text, *, current_status, current_detail):
        if phrase in text.lower():
            return [AgentLifecycleEvent(AgentStatus.WAITING, LOW, "sentinel visible")]
        if current_status in (AgentStatus.WAITING, AgentStatus.POSSIBLY_HANGED):
            return [AgentLifecycleEvent(AgentStatus.RUNNING, LOW)]
        return []

    return scan


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


class TestByteRing:
    def test_offsets_are_contiguous(self):
        ring = ByteRing(64)
        assert ring.append(b"abc") == 0
        assert ring.append(b"") == 3
        assert ring.append(b"defg") == 3
        assert ring.end_offset == 7
        assert ring.read(0) == b"abcdefg"
        assert ring.read(3, 2) == b"de"

    def test_wraps_and_keeps_the_newest_bytes(self):
        ring = ByteRing(8)
        ring.append(b"0123456")
        ring.append(b"789ab")  # 12 bytes written, ring holds the last 8
        assert (ring.start_offset, ring.end_offset) == (4, 12)
        assert ring.read(4) == b"456789ab"
        assert ring.read(9, 2) == b"9a"
        ring.append(b"cdefghij")  # wraps again, exactly one full ring
        assert ring.read(ring.start_offset) == b"cdefghij"

    def test_a_chunk_bigger_than_the_ring_keeps_its_tail(self):
        ring = ByteRing(4)
        ring.append(b"xy")
        assert ring.append(b"0123456789") == 2
        assert (ring.start_offset, ring.end_offset) == (8, 12)
        assert ring.read(8) == b"6789"

    def test_reading_outside_the_ring_raises(self):
        ring = ByteRing(4)
        ring.append(b"0123456789")
        with pytest.raises(RingOffsetError):
            ring.read(5)  # evicted
        with pytest.raises(RingOffsetError):
            ring.read(11)  # not written yet
        assert ring.read(10) == b""  # the end itself is a valid, empty read

    def test_storage_grows_with_the_data(self):
        ring = ByteRing(1024)
        ring.append(b"x" * 10)
        assert len(ring._buf) == 10


# ---------------------------------------------------------------------------
# Output → screen
# ---------------------------------------------------------------------------


class TestOutput:
    @pytest.mark.asyncio
    async def test_chunks_reach_the_ring_and_viewers_with_their_offset(self):
        session, _, _ = _agent()
        seen = []
        session.attach(lambda offset, data: seen.append((offset, data)))
        session._on_chunk(b"hello ")
        session._on_chunk(b"world")
        assert seen == [(0, b"hello "), (6, b"world")]
        assert session.ring.read(0) == b"hello world"

    @pytest.mark.asyncio
    async def test_utf8_split_across_chunks_is_decoded_whole(self):
        session, observer, _ = _agent()
        data = "héllo €".encode()
        cut = data.index("€".encode()) + 1  # inside the 3-byte euro sign
        session._on_chunk(data[:2])  # inside the 2-byte é
        session._on_chunk(data[2:cut])
        session._on_chunk(data[cut:])
        assert session.tail_lines(1) != []
        assert session.screen.screen_text(0) == "héllo €"
        text = "".join(c.args[0] for c in observer.on_terminal_output.call_args_list)
        assert text == "héllo €"
        assert "�" not in text

    @pytest.mark.asyncio
    async def test_unwatched_output_is_batched_then_fed(self):
        """No viewer: the screen catches up within the feed interval."""
        session, _, _ = _agent()
        session._on_chunk(b"first ")
        session._on_chunk(b"second")
        assert session.screen.screen_text(0) == ""
        await asyncio.sleep(UNWATCHED_FEED_INTERVAL + 0.05)
        assert session.screen.screen_text(0) == "first second"

    @pytest.mark.asyncio
    async def test_a_burst_of_hidden_output_does_not_hold_the_loop(self):
        """Batched output is parsed in slices: the loop keeps turning.

        Each gap between two turns of this ticker is measured in this
        thread's CPU time, which is the work the loop did in between: time
        the process spent preempted by a busy machine does not count, so the
        bound can follow the feeder's own budget: a turn may overrun it by
        one slice and some loop overhead, never by a multiple of it (a turn
        budget of 100 ms, or the batch parsed in one go, shows up here).
        """
        session, _, _ = _agent()
        line = b"x" * 78 + b"\r\n"
        for _ in range(40):
            session._on_chunk(line * 100)  # 320 kB in all
        gaps = []
        walls = []

        async def tick() -> None:
            last, last_wall = time.thread_time(), time.perf_counter()
            while session._pending_feed or session._queued_on is not None:
                await asyncio.sleep(0)
                now, wall = time.thread_time(), time.perf_counter()
                gaps.append(now - last)
                walls.append(wall - last_wall)
                last, last_wall = now, wall

        await asyncio.sleep(UNWATCHED_FEED_INTERVAL + 0.01)
        await asyncio.wait_for(tick(), 30)
        assert session.screen.screen_text(0).startswith("x" * 78)
        assert len(gaps) > 10
        assert max(gaps) < 0.012, (  # 3 x the 4 ms turn budget (FEED_BUDGET)
            f"a loop turn took {max(gaps) * 1000:.1f} ms of CPU "
            f"({max(walls) * 1000:.1f} ms wall at most)"
        )
        assert session.feed_dropped == 0

    @pytest.mark.asyncio
    async def test_a_backlog_past_the_limit_keeps_only_its_newest_lines(
        self, monkeypatch
    ):
        monkeypatch.setattr(session_mod, "PENDING_FEED_LIMIT", 1000)
        monkeypatch.setattr(session_mod, "PENDING_FEED_KEEP", 300)
        session, _, _ = _agent()
        for i in range(60):
            session._on_chunk(f"line {i:02d} ".encode() + b"y" * 30 + b"\r\n")
        assert 0 < session.feed_dropped
        assert session._pending_chars <= 1000
        session.flush()
        lines = session.tail_lines(100)
        assert any("line 59" in text for text in lines)
        assert not any("line 00" in text for text in lines)
        # The kept part starts at a line: no half line at the top.
        kept = [text for text in lines if text.strip()]
        assert kept[0].startswith("line ")

    @pytest.mark.asyncio
    async def test_flush_still_feeds_everything_at_once(self):
        session, _, _ = _agent()
        session._on_chunk(b"a" * 5000)
        session.flush()
        assert session._pending_chars == 0
        assert session.screen.screen_text(0) == "a" * 80

    @pytest.mark.asyncio
    async def test_a_watching_viewer_gets_a_fed_screen_per_chunk(self):
        session, _, _ = _agent()
        seen = []
        session.attach(
            lambda offset, data: seen.append(session.screen.screen_text(0)),
            watching=lambda: True,
        )
        session._on_chunk(b"abc")
        session._on_chunk(b"def")
        assert seen == ["abc", "abcdef"]

    @pytest.mark.asyncio
    async def test_a_hidden_viewer_does_not_force_a_feed(self):
        session, _, _ = _agent()
        session.attach(lambda offset, data: None, watching=lambda: False)
        session._on_chunk(b"abc")
        assert session.screen.screen_text(0) == ""
        session.flush()
        assert session.screen.screen_text(0) == "abc"

    @pytest.mark.asyncio
    async def test_detached_viewer_gets_nothing(self):
        session, _, _ = _agent()
        seen = []
        detach = session.attach(lambda offset, data: seen.append(data))
        detach()
        session._on_chunk(b"abc")
        assert seen == []

    @pytest.mark.asyncio
    async def test_viewers_hear_the_close_whether_it_exits_or_is_stopped(self):
        exits = TerminalSession("sh -c 'printf bye'")
        stopped = TerminalSession("sleep 30")
        closed = []
        exits.attach(lambda o, d: None, on_close=lambda: closed.append("exited"))
        stopped.attach(lambda o, d: None, on_close=lambda: closed.append("stopped"))
        exits.start()
        stopped.start()
        deadline = time.monotonic() + 5
        while "exited" not in closed and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        stopped.stop()
        assert closed == ["exited", "stopped"]

    @pytest.mark.asyncio
    async def test_resize_applies_batched_output_at_the_old_size_first(self):
        session, _, _ = _agent(rows=3, cols=10)
        session._on_chunk(b"0123456789abc")  # wraps at 10 columns
        session.resize(3, 20)
        assert session.tail_lines(3)[:2] == ["0123456789", "abc"]
        emulator = session._emulator
        assert ["set_size", 3, 20] in [
            emulator.recv_queue.get_nowait() for _ in range(emulator.recv_queue.qsize())
        ]


class TestTailLines:
    @pytest.mark.asyncio
    async def test_spans_scrollback_and_screen_oldest_first(self):
        session, _, _ = _agent(rows=4, cols=40)
        session._on_chunk("".join(f"line {i}\r\n" for i in range(12)).encode())
        session.flush()
        assert len(session.screen.scrollback) > 0
        # The trailing "\r\n" leaves the cursor on a blank final row.
        assert session.tail_lines(6) == [
            "line 7",
            "line 8",
            "line 9",
            "line 10",
            "line 11",
            "",
        ]
        assert session.total_lines == len(session.screen.scrollback) + 4

    @pytest.mark.asyncio
    async def test_asking_for_more_than_exists_returns_everything(self):
        session, _, _ = _agent(rows=2, cols=10)
        session._on_chunk(b"a\r\nb\r\nc")
        assert session.tail_lines(100) == ["a", "b", "c"]
        assert session.tail_lines(0) == []


class TestModeTracking:
    def test_bracketed_paste_enable_and_disable(self):
        session = TerminalSession("unused")
        session._scan_terminal_modes("\x1b[?2004h")
        assert session.bracketed_paste is True
        session._scan_terminal_modes("\x1b[?2004l")
        assert session.bracketed_paste is False

    def test_mode_in_a_multi_parameter_decset(self):
        session = TerminalSession("unused")
        session._scan_terminal_modes("\x1b[?1000;1006;2004h")
        assert session.bracketed_paste is True
        assert session.mouse_tracking is True

    def test_mouse_tracking(self):
        session = TerminalSession("unused")
        session._scan_terminal_modes("\x1b[?1000h")
        assert session.mouse_tracking is True
        session._scan_terminal_modes("\x1b[?1000l")
        assert session.mouse_tracking is False

    def test_unrelated_sequences_are_ignored(self):
        session = TerminalSession("unused")
        session._scan_terminal_modes("\x1b[2J\x1b[?25l\x1b[1;5H")
        assert session.bracketed_paste is False
        assert session.mouse_tracking is False

    @pytest.mark.asyncio
    async def test_modes_are_tracked_with_nobody_watching(self):
        """A hidden agent is the normal MCP target; its modes must stay current."""
        session, _, _ = _agent()
        session._on_chunk(b"\x1b[?2004h")
        assert session.bracketed_paste is True
        assert session._output_event.is_set()


# ---------------------------------------------------------------------------
# Status detection
# ---------------------------------------------------------------------------


class TestOutputStatus:
    @pytest.mark.asyncio
    async def test_first_output_sets_running(self):
        session, _, events = _agent()
        session._on_chunk(b"hello world")
        assert session.status == AgentStatus.RUNNING
        assert session.last_output_time is not None
        assert events == [
            StatusChanged(
                WT_PATH, "a1", AgentStatus.RUNNING, LOW, "", session.last_output_time
            )
        ]

    @pytest.mark.asyncio
    async def test_running_output_emits_nothing_new(self):
        session, _, events = _agent()
        session.status = AgentStatus.RUNNING
        session._on_chunk(b"hello")
        assert events == []

    @pytest.mark.asyncio
    async def test_updates_last_output_time(self):
        session, _, _ = _agent()
        before = time.monotonic()
        session._on_chunk(b"data")
        assert session.last_output_time >= before

    @pytest.mark.asyncio
    async def test_observer_output_events_are_applied(self):
        session, observer, events = _agent()
        session.status = AgentStatus.RUNNING
        observer.on_terminal_output.return_value = [
            AgentLifecycleEvent(AgentStatus.WAITING, LOW)
        ]
        session._on_chunk(b"hello")
        assert _statuses(events) == [AgentStatus.WAITING]


class TestScanScreen:
    """Screen-based detection reads the session's headless screen."""

    def _session(self, status: AgentStatus, phrase: str = "your turn"):
        session, observer, events = _agent(running=False)
        session.status = status
        observer.on_screen_update.side_effect = _waiting_when(phrase)
        return session, observer, events

    def _show(self, session, text: str) -> None:
        session._stream.feed(text)

    def test_sentinel_sets_waiting(self):
        session, _, events = self._session(AgentStatus.RUNNING)
        self._show(session, "some output\nyour turn\n")
        session.scan_screen()
        assert session.status == AgentStatus.WAITING
        assert events[-1].status == AgentStatus.WAITING
        assert events[-1].confidence == LOW
        assert events[-1].detail == "sentinel visible"

    @pytest.mark.parametrize(
        "text",
        [
            "Your Turn\n",
            "\x1b[32myour turn\x1b[0m\n",
            "\x1b[1;32myour\x1b[0m \x1b[1;33mturn\x1b[0m\n",
        ],
    )
    def test_sentinel_seen_through_case_and_colours(self, text):
        session, _, _ = self._session(AgentStatus.RUNNING)
        self._show(session, text)
        session.scan_screen()
        assert session.status == AgentStatus.WAITING

    def test_no_sentinel_stays_running(self):
        session, _, events = self._session(AgentStatus.RUNNING)
        self._show(session, "some other output\n")
        session.scan_screen()
        assert session.status == AgentStatus.RUNNING
        assert events == []

    @pytest.mark.parametrize(
        "status", [AgentStatus.WAITING, AgentStatus.POSSIBLY_HANGED]
    )
    def test_sentinel_gone_resumes_running(self, status):
        session, _, _ = self._session(status)
        self._show(session, "\n".join(["new content"] * 30) + "\n")
        session.scan_screen()
        assert session.status == AgentStatus.RUNNING

    def test_sentinel_still_visible_emits_nothing_new(self):
        session, _, events = self._session(AgentStatus.RUNNING)
        self._show(session, "your turn\n")
        session.scan_screen()
        events.clear()
        session.scan_screen()
        assert session.status == AgentStatus.WAITING
        assert events == []

    def test_screen_text_is_passed_to_observer(self):
        session, observer, _ = self._session(AgentStatus.RUNNING)
        observer.on_screen_update.side_effect = None
        self._show(session, "hello observer\n")
        session.scan_screen()
        assert "hello observer" in observer.on_screen_update.call_args.args[0]

    def test_scan_applies_polled_events(self):
        session, observer, _ = self._session(AgentStatus.RUNNING)
        observer.poll.return_value = [AgentLifecycleEvent(AgentStatus.WAITING, HIGH)]
        session.scan_screen()
        assert session.status == AgentStatus.WAITING

    @pytest.mark.asyncio
    async def test_unwatched_output_is_scanned_after_the_batched_feed(self):
        """Detection runs with no widget at all."""
        session, observer, events = _agent()
        observer.on_screen_update.side_effect = _waiting_when("your turn")
        session._on_chunk(b"thinking...\r\nyour turn> ")
        assert _statuses(events) == [AgentStatus.RUNNING]
        await asyncio.sleep(
            UNWATCHED_FEED_INTERVAL + session_mod._SCAN_DEBOUNCE_SECONDS + 0.1
        )
        assert _statuses(events) == [AgentStatus.RUNNING, AgentStatus.WAITING]


class TestCheckHang:
    def test_poll_events_are_applied_before_hang_check(self):
        session, observer, _ = _agent(running=False)
        session.status = AgentStatus.RUNNING
        observer.poll.return_value = [AgentLifecycleEvent(AgentStatus.WAITING, HIGH)]
        session.check_hang()
        assert session.status == AgentStatus.WAITING

    def test_no_hang_when_recent_output(self):
        session, _, events = _agent(running=False)
        session.status = AgentStatus.RUNNING
        session.last_output_time = time.monotonic()
        session.check_hang()
        assert session.status == AgentStatus.RUNNING
        assert events == []

    def test_hang_after_timeout(self):
        session, _, events = _agent(running=False)
        session.status = AgentStatus.RUNNING
        session.last_output_time = time.monotonic() - HANG_SECONDS - 1
        session.check_hang()
        assert session.status == AgentStatus.POSSIBLY_HANGED
        assert events[-1].status == AgentStatus.POSSIBLY_HANGED
        assert events[-1].confidence == LOW

    @pytest.mark.parametrize(
        ("status", "last_output"),
        [(AgentStatus.WAITING, -HANG_SECONDS - 1), (AgentStatus.RUNNING, None)],
    )
    def test_no_hang(self, status, last_output):
        session, _, events = _agent(running=False)
        session.status = status
        session.last_output_time = (
            None if last_output is None else time.monotonic() + last_output
        )
        session.check_hang()
        assert session.status == status
        assert events == []


class TestListeners:
    def test_a_failing_listener_does_not_stop_the_others(self):
        session, _, events = _agent(running=False)
        session._listeners.insert(0, MagicMock(side_effect=RuntimeError("boom")))
        session._set_status(AgentStatus.WAITING)
        assert _statuses(events) == [AgentStatus.WAITING]

    def test_removed_listener_gets_nothing(self):
        session, _, _ = _agent(running=False)
        seen = []
        remove = session.add_listener(seen.append)
        remove()
        session._set_status(AgentStatus.WAITING)
        assert seen == []

    def test_no_events_after_stop(self):
        session, _, events = _agent()
        session.stop()
        session._set_status(AgentStatus.WAITING)
        assert events == []
        assert not session.running


# ---------------------------------------------------------------------------
# Programmatic input
# ---------------------------------------------------------------------------

LONG = "please review the retry logic in the worker and tell me if it is right"


class TestFrameInput:
    def test_wraps_in_paste_markers_when_child_asked_for_them(self):
        session = TerminalSession("unused")
        session.bracketed_paste = True
        assert session.frame_input("hello") == (
            BRACKETED_PASTE_START + "hello" + BRACKETED_PASTE_END
        )

    def test_no_markers_when_child_did_not_enable_them(self):
        """Unrequested markers would be inserted as literal junk."""
        assert TerminalSession("unused").frame_input("hello") == "hello"

    def test_carriage_returns_in_body_become_newlines(self):
        assert TerminalSession("unused").frame_input("a\r\nb\rc") == "a\nb\nc"

    def test_embedded_end_marker_is_stripped(self):
        """Otherwise the payload could close its own paste and inject keys."""
        session = TerminalSession("unused")
        session.bracketed_paste = True
        framed = session.frame_input(f"safe{BRACKETED_PASTE_END}\rrm -rf /")
        assert framed.count(BRACKETED_PASTE_END) == 1
        assert framed.endswith(BRACKETED_PASTE_END)


class TestSendInput:
    def _session(self, *, bracketed_paste: bool = True) -> TerminalSession:
        session = TerminalSession("unused")
        session.bracketed_paste = bracketed_paste
        session._emulator = _FakeEmulator()
        return session

    @pytest.mark.asyncio
    async def test_submit_key_is_a_separate_write(self):
        session = self._session()
        session._output_event.set()  # stale; cleared by send_input
        task = asyncio.create_task(session.send_input(LONG, submit=True))
        await asyncio.sleep(0)
        session._output_event.set()  # the child redrew
        await task
        assert _writes(session._emulator) == [
            BRACKETED_PASTE_START + LONG + BRACKETED_PASTE_END,
            "\r",
        ]

    @pytest.mark.asyncio
    async def test_unframed_without_bracketed_paste(self):
        session = self._session(bracketed_paste=False)
        await session.send_input("a\r\nb", submit=False)
        assert _writes(session._emulator) == ["a\nb"]

    @pytest.mark.asyncio
    async def test_no_submit_key_when_not_submitting(self):
        session = self._session()
        await session.send_input("hello", submit=False)
        assert len(_writes(session._emulator)) == 1

    @pytest.mark.asyncio
    async def test_submit_waits_for_the_child_to_acknowledge(self):
        """The CR is held until the child produces output after our write."""
        session = self._session()
        task = asyncio.create_task(session.send_input(LONG, submit=True))
        await asyncio.sleep(0.05)
        assert _writes(session._emulator) == [
            BRACKETED_PASTE_START + LONG + BRACKETED_PASTE_END
        ]
        session._on_chunk(b"redraw")  # the child has read the paste
        await task
        assert _writes(session._emulator) == ["\r"]

    @pytest.mark.asyncio
    async def test_submits_anyway_when_the_child_stays_silent(self, monkeypatch):
        """A quiet agent must not swallow the instruction entirely."""
        monkeypatch.setattr(session_mod, "SUBMIT_ACK_TIMEOUT", 0.05)
        session = self._session()
        await session.send_input(LONG, submit=True)
        assert _writes(session._emulator)[-1] == "\r"

    @pytest.mark.asyncio
    async def test_stale_output_does_not_count_as_acknowledgement(self):
        session = self._session()
        session._output_event.set()  # output from before we wrote anything
        task = asyncio.create_task(session.send_input(LONG, submit=True))
        await asyncio.sleep(0)
        assert session._output_event.is_set() is False
        session._output_event.set()
        await task

    @pytest.mark.asyncio
    async def test_raises_when_not_running(self):
        session = TerminalSession("unused")
        with pytest.raises(RuntimeError):
            await session.send_input("hello", submit=True)
        with pytest.raises(RuntimeError):
            session.write("hello")


# ---------------------------------------------------------------------------
# Real PTY
# ---------------------------------------------------------------------------


def _fake_agent_command(*args: str) -> str:
    return shlex.join([sys.executable, str(FAKE_AGENT), *args])


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _shown(session) -> list[str]:
    return [line for line in session.tail_lines(session.total_lines) if line]


class _PromptObserver(AgentObserver):
    """WAITING while the fake agent's prompt is the last thing on screen."""

    def on_screen_update(self, screen_text, *, current_status, current_detail=""):
        lines = [line.strip() for line in screen_text.splitlines() if line.strip()]
        at_prompt = bool(lines) and lines[-1].startswith("your turn>")
        if at_prompt and current_status != AgentStatus.WAITING:
            return [AgentLifecycleEvent(AgentStatus.WAITING, HIGH, "prompt")]
        if not at_prompt and current_status == AgentStatus.WAITING:
            return [AgentLifecycleEvent(AgentStatus.RUNNING, HIGH)]
        return []


class TestRealPty:
    @pytest.mark.asyncio
    async def test_status_input_and_exit_with_no_widget(self):
        session = AgentSession(
            _fake_agent_command("--paste"),
            worktree_path=WT_PATH,
            agent_id="a1",
            observer=_PromptObserver(),
        )
        events: list = []
        session.add_listener(events.append)
        session.start()
        try:
            await _until(lambda: AgentStatus.WAITING in _statuses(events))
            assert session.bracketed_paste is True
            assert _shown(session) == ["fake agent ready", "your turn>"]

            await session.send_input("hello\nthere", submit=True)
            await _until(lambda: "got:" in session.screen_text())
            await _until(lambda: _statuses(events)[-1] == AgentStatus.WAITING)
            assert _shown(session)[1] == "got: 'hello\\nthere' paste=True"
            assert _statuses(events) == [
                AgentStatus.RUNNING,
                AgentStatus.WAITING,
                AgentStatus.RUNNING,
                AgentStatus.WAITING,
            ]

            # Offsets cover every byte the child wrote, in order.
            raw = session.ring.read(0)
            assert session.ring.end_offset == len(raw)
            assert b"fake agent ready" in raw and b"got: " in raw

            await session.send_input("exit 7", submit=True)
            await _until(lambda: any(isinstance(e, SessionExited) for e in events))
        finally:
            session.stop()

        assert events[-1] == SessionExited(WT_PATH, "a1", 7)
        assert session.exit_code == 7
        assert not session.running
        assert session.status == AgentStatus.NO_AGENT

    @pytest.mark.asyncio
    async def test_no_paste_markers_for_a_child_that_never_asked(self):
        session = AgentSession(_fake_agent_command(), worktree_path=WT_PATH)
        session.start()
        try:
            await _until(lambda: "your turn>" in session.screen_text())
            assert session.bracketed_paste is False
            await session.send_input("plain", submit=True)
            await _until(lambda: "got:" in session.screen_text())
            assert "got: 'plain' paste=False" in session.screen_text()
        finally:
            session.stop()

    @pytest.mark.asyncio
    async def test_explicit_stop_emits_no_exit_event(self):
        session = AgentSession(_fake_agent_command(), worktree_path=WT_PATH)
        events: list = []
        session.add_listener(events.append)
        session.start()
        await _until(lambda: "your turn>" in session.screen_text())
        session.stop()
        await asyncio.sleep(0.1)
        assert not any(isinstance(e, SessionExited) for e in events)
        assert session.exit_code is not None  # killed and reaped

    @pytest.mark.asyncio
    async def test_shell_session_runs_a_command(self):
        session = TerminalSession(
            "bash --norc --noprofile -i", worktree_path=WT_PATH, rows=10, cols=60
        )
        exits: list = []
        session.add_listener(exits.append)
        session.start()
        try:
            session.write("echo hola-$((40+2))\n")
            await _until(lambda: "hola-42" in "\n".join(session.tail_lines(10)))
            session.write("exit 3\n")
            await _until(lambda: bool(exits))
        finally:
            session.stop()
        assert exits == [SessionExited(WT_PATH, "", 3)]
