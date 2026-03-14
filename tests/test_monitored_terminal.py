from __future__ import annotations

import time
from unittest.mock import MagicMock

import pyte

from lazyagent.agent_observers import (
    AgentLifecycleEvent,
    AgentObserver,
)
from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentStatus, LifecycleConfidence
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
    terminal._observer = MagicMock(spec=AgentObserver)
    terminal._observer.on_terminal_output.return_value = []
    terminal._observer.on_screen_update.return_value = []
    terminal._observer.on_disconnect.return_value = []
    terminal._observer.on_process_started.return_value = []
    terminal._observer.cleanup.return_value = None
    terminal._follow_output = True
    terminal.post_message = MagicMock()
    # Set up pyte screen for sentinel detection via _scan_screen
    terminal._screen = ScrollbackScreen(80, 24)
    terminal.stream = pyte.Stream(terminal._screen)
    terminal._scan_timer = None
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
        assert msg.confidence == LifecycleConfidence.LOW
        assert msg.detail == ""

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

    def test_observer_output_events_are_applied(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._observer.on_terminal_output.return_value = [
            AgentLifecycleEvent(
                status=AgentStatus.WAITING,
                confidence=LifecycleConfidence.LOW,
            )
        ]
        t._on_pty_output("hello")
        assert t._status == AgentStatus.WAITING


class TestScanScreen:
    """Sentinel detection using the pyte screen buffer."""

    def test_sentinel_sets_waiting(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t.post_message.reset_mock()
        t._observer.on_screen_update.side_effect = (
            lambda text, *, current_status: [
                AgentLifecycleEvent(
                    status=AgentStatus.WAITING,
                    confidence=LifecycleConfidence.LOW,
                    detail="sentinel visible",
                )
            ]
            if "your turn" in text.lower()
            else []
        )
        t.stream.feed("some output\nyour turn\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING
        msg = t.post_message.call_args[0][0]
        assert isinstance(msg, AgentStatusChanged)
        assert msg.status == AgentStatus.WAITING
        assert msg.confidence == LifecycleConfidence.LOW
        assert msg.detail == "sentinel visible"

    def test_sentinel_case_insensitive(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._observer.on_screen_update.side_effect = (
            lambda text, *, current_status: [
                AgentLifecycleEvent(
                    status=AgentStatus.WAITING,
                    confidence=LifecycleConfidence.LOW,
                )
            ]
            if "your turn" in text.lower()
            else []
        )
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
        t._observer.on_screen_update.side_effect = (
            lambda text, *, current_status: [
                AgentLifecycleEvent(
                    status=AgentStatus.RUNNING,
                    confidence=LifecycleConfidence.LOW,
                )
            ]
            if "your turn" not in text.lower()
            and current_status in (AgentStatus.WAITING, AgentStatus.POSSIBLY_HANGED)
            else []
        )
        # Fill screen with new content that doesn't contain sentinel
        t.stream.feed("\n".join(["line"] * 30) + "\n")
        t.post_message.reset_mock()
        t._scan_screen()
        assert t._status == AgentStatus.RUNNING

    def test_sentinel_stays_on_screen_keeps_waiting(self):
        """Repeated screen refreshes with sentinel visible stay WAITING."""
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._observer.on_screen_update.side_effect = (
            lambda text, *, current_status: [
                AgentLifecycleEvent(
                    status=AgentStatus.WAITING,
                    confidence=LifecycleConfidence.LOW,
                )
            ]
            if "your turn" in text.lower()
            else []
        )
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
        t._observer.on_screen_update.side_effect = (
            lambda text, *, current_status: [
                AgentLifecycleEvent(
                    status=AgentStatus.WAITING,
                    confidence=LifecycleConfidence.LOW,
                )
            ]
            if "your turn" in text.lower()
            else []
        )
        t.stream.feed("\x1b[32myour turn\x1b[0m\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING

    def test_sentinel_with_bold_and_per_word_colors(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._observer.on_screen_update.side_effect = (
            lambda text, *, current_status: [
                AgentLifecycleEvent(
                    status=AgentStatus.WAITING,
                    confidence=LifecycleConfidence.LOW,
                )
            ]
            if "your turn" in text.lower()
            else []
        )
        t.stream.feed("\x1b[1;32myour\x1b[0m \x1b[1;33mturn\x1b[0m\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING

    def test_possibly_hanged_resumes_when_sentinel_gone(self):
        t = _make_terminal()
        t._status = AgentStatus.POSSIBLY_HANGED
        t._observer.on_screen_update.side_effect = (
            lambda text, *, current_status: [
                AgentLifecycleEvent(
                    status=AgentStatus.RUNNING,
                    confidence=LifecycleConfidence.LOW,
                )
            ]
            if "your turn" not in text.lower()
            and current_status in (AgentStatus.WAITING, AgentStatus.POSSIBLY_HANGED)
            else []
        )
        t.stream.feed("\n".join(["new content"] * 30) + "\n")
        t.post_message.reset_mock()
        t._scan_screen()
        assert t._status == AgentStatus.RUNNING

    def test_screen_text_is_passed_to_observer(self):
        t = _make_terminal()
        t.stream.feed("hello observer\n")
        t._scan_screen()
        assert t._observer.on_screen_update.called
        screen_text = t._observer.on_screen_update.call_args.args[0]
        assert "hello observer" in screen_text

    def test_scan_screen_applies_polled_events(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._observer.poll.return_value = [
            AgentLifecycleEvent(
                status=AgentStatus.WAITING,
                confidence=LifecycleConfidence.HIGH,
            )
        ]
        t.stream.feed("hello observer\n")
        t._scan_screen()
        assert t._status == AgentStatus.WAITING


class TestOnDisconnect:
    def test_disconnect_sets_no_agent_and_posts_exited(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._on_recv_disconnect()
        assert t._status == AgentStatus.NO_AGENT
        t._observer.cleanup.assert_called_once()
        msg = t.post_message.call_args[0][0]
        assert isinstance(msg, AgentExited)
        assert msg.worktree_path == WT_PATH


class TestCheckHang:
    def test_poll_events_are_applied_before_hang_check(self):
        t = _make_terminal()
        t._status = AgentStatus.RUNNING
        t._observer.poll.return_value = [
            AgentLifecycleEvent(
                status=AgentStatus.WAITING,
                confidence=LifecycleConfidence.HIGH,
            )
        ]
        t.check_hang()
        assert t._status == AgentStatus.WAITING

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
        assert msg.confidence == LifecycleConfidence.LOW

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
