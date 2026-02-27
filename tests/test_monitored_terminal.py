from __future__ import annotations

import time
from unittest.mock import MagicMock

import pyte

from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentStatus
from lazyagent.widgets.monitored_terminal import MonitoredTerminal, _HANG_SECONDS
from lazyagent.widgets.scrollable_terminal import ScrollbackScreen


WT_PATH = "/home/user/repo-worktree"


def _make_terminal() -> MonitoredTerminal:
    """Create a MonitoredTerminal without starting the emulator."""
    terminal = MonitoredTerminal.__new__(MonitoredTerminal)
    terminal.worktree_path = WT_PATH
    terminal._status = AgentStatus.NO_AGENT
    terminal._last_output_time = None
    terminal._stopped = False
    terminal.post_message = MagicMock()
    # Set up pyte screen for sentinel detection via _scan_screen
    terminal._screen = ScrollbackScreen(80, 24)
    terminal.stream = pyte.Stream(terminal._screen)
    return terminal


class TestOnPtyOutput:
    def test_first_output_sets_running(self):
        t = _make_terminal()
        t._on_pty_output("hello world")
        assert t._status == AgentStatus.RUNNING
        assert t._last_output_time is not None
        t.post_message.assert_called_once()
        msg = t.post_message.call_args[0][0]
        assert isinstance(msg, AgentStatusChanged)
        assert msg.status == AgentStatus.RUNNING

    def test_running_output_no_duplicate_status_post(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.post_message.reset_mock()
        t._on_pty_output("hello")
        # Status didn't change (already RUNNING), so no message posted
        t.post_message.assert_not_called()

    def test_updates_last_output_time(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        before = time.monotonic()
        t._on_pty_output("data")
        assert t._last_output_time >= before


class TestScanScreen:
    """Sentinel detection using the pyte screen buffer."""

    def test_sentinel_sets_waiting(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.post_message.reset_mock()
        t.stream.feed("some output\nyour turn\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING
        msg = t.post_message.call_args[0][0]
        assert isinstance(msg, AgentStatusChanged)
        assert msg.status == AgentStatus.WAITING

    def test_sentinel_case_insensitive(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.stream.feed("Your Turn\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING

    def test_no_sentinel_stays_running(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.post_message.reset_mock()
        t.stream.feed("some other output\n")
        t._scan_screen()
        assert t._status == AgentStatus.RUNNING
        t.post_message.assert_not_called()

    def test_sentinel_disappears_resumes_running(self):
        """When sentinel scrolls off screen, status goes back to RUNNING."""
        t = _make_terminal()
        t._status = AgentStatus.WAITING
        # Fill screen with new content that doesn't contain sentinel
        t.stream.feed("\n".join(["line"] * 30) + "\n")
        t.post_message.reset_mock()
        t._scan_screen()
        assert t._status == AgentStatus.RUNNING

    def test_sentinel_stays_on_screen_keeps_waiting(self):
        """Repeated screen refreshes with sentinel visible stay WAITING."""
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.stream.feed("your turn\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING

        # Simulate another screen refresh (e.g. cursor blink) — sentinel still visible
        t.post_message.reset_mock()
        t._scan_screen()
        assert t._status == AgentStatus.WAITING
        # No new message since status didn't change
        t.post_message.assert_not_called()

    def test_sentinel_with_ansi_colors(self):
        """Sentinel wrapped in ANSI color codes is detected via pyte screen."""
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.stream.feed("\x1b[32myour turn\x1b[0m\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING

    def test_sentinel_with_bold_and_per_word_colors(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.stream.feed("\x1b[1;32myour\x1b[0m \x1b[1;33mturn\x1b[0m\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING

    def test_possibly_hanged_resumes_when_sentinel_gone(self):
        t = _make_terminal()
        t._status = AgentStatus.POSSIBLY_HANGED
        t.stream.feed("\n".join(["new content"] * 30) + "\n")
        t.post_message.reset_mock()
        t._scan_screen()
        assert t._status == AgentStatus.RUNNING


class TestOnDisconnect:
    def test_disconnect_sets_no_agent_and_posts_exited(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._on_recv_disconnect()
        assert t._status == AgentStatus.NO_AGENT
        msg = t.post_message.call_args[0][0]
        assert isinstance(msg, AgentExited)
        assert msg.worktree_path == WT_PATH


class TestCheckHang:
    def test_no_hang_when_recent_output(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._last_output_time = time.monotonic()
        t.check_hang()
        assert t._status == AgentStatus.RUNNING
        t.post_message.assert_not_called()

    def test_hang_after_timeout(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._last_output_time = time.monotonic() - _HANG_SECONDS - 1
        t.check_hang()
        assert t._status == AgentStatus.POSSIBLY_HANGED
        msg = t.post_message.call_args[0][0]
        assert isinstance(msg, AgentStatusChanged)
        assert msg.status == AgentStatus.POSSIBLY_HANGED

    def test_no_hang_when_not_running(self):
        t = _make_terminal()
        t._status = AgentStatus.WAITING
        t._last_output_time = time.monotonic() - _HANG_SECONDS - 1
        t.check_hang()
        assert t._status == AgentStatus.WAITING
        t.post_message.assert_not_called()

    def test_no_hang_when_no_output_time(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._last_output_time = None
        t.check_hang()
        assert t._status == AgentStatus.RUNNING
        t.post_message.assert_not_called()


