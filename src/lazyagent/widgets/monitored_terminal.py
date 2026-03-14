from __future__ import annotations

import asyncio
import re
import time

from textual import log

from lazyagent.agent_observers import AgentObserver, TerminalSentinelObserver
from lazyagent.agent_providers import SENTINEL_TEXT
from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentStatus
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal

_HANG_SECONDS = 600  # 10 minutes
_SCAN_DEBOUNCE_SECONDS = 0.15


class MonitoredTerminal(ScrollableTerminal):
    """ScrollableTerminal subclass that intercepts pty output for agent status detection.

    Scans raw chars for a sentinel phrase ("your turn") and tracks output
    timing for hang detection.
    """

    def __init__(
        self,
        command: str,
        worktree_path: str,
        observer: AgentObserver | None = None,
        **kwargs,
    ) -> None:
        super().__init__(command=command, **kwargs)
        self.worktree_path = worktree_path
        self._status = AgentStatus.NO_AGENT
        self._last_output_time: float | None = None
        self._observer = observer or TerminalSentinelObserver(SENTINEL_TEXT)
        self._scan_timer: asyncio.TimerHandle | None = None

    @property
    def agent_status(self) -> AgentStatus:
        return self._status

    @property
    def last_output_time(self) -> float | None:
        return self._last_output_time

    def _set_status(self, new_status: AgentStatus) -> None:
        if new_status != self._status:
            self._status = new_status
            if not self._stopped:
                self.post_message(AgentStatusChanged(self.worktree_path, new_status))

    def _on_pty_output(self, chars: str) -> None:
        """Track output timing. Called from recv() on each stdout chunk."""
        self._last_output_time = time.monotonic()

        # First output transitions from NO_AGENT to RUNNING
        if self._status == AgentStatus.NO_AGENT:
            self._set_status(AgentStatus.RUNNING)
        self._apply_events(self._observer.on_terminal_output(chars))

    def _apply_events(self, events) -> None:
        for event in events:
            self._set_status(event.status)

    def _rendered_screen_text(self) -> str:
        lines: list[str] = []
        for y in range(self._screen.lines):
            row = self._screen.buffer[y]
            lines.append(
                "".join(row[x].data for x in range(self._screen.columns))
            )
        return "\n".join(lines)

    def _scan_screen(self) -> None:
        """Check the rendered pyte screen buffer for the sentinel phrase.

        Uses the already-parsed screen content so ANSI codes don't interfere.
        Keeps WAITING as long as the sentinel is visible on screen.
        """
        screen_text = self._rendered_screen_text()
        self._apply_events(
            self._observer.on_screen_update(
                screen_text,
                current_status=self._status,
            )
        )
        self._apply_events(self._observer.poll())

    def _on_recv_disconnect(self) -> None:
        """Handle pty disconnect."""
        self._apply_events(self._observer.on_disconnect())
        self._observer.cleanup()
        self._status = AgentStatus.NO_AGENT
        if not self._stopped:
            self.post_message(AgentExited(self.worktree_path))

    def check_hang(self) -> None:
        """Called periodically by the app timer. Posts POSSIBLY_HANGED if stale."""
        self._apply_events(self._observer.poll())
        if (
            self._status == AgentStatus.RUNNING
            and self._last_output_time is not None
            and time.monotonic() - self._last_output_time >= _HANG_SECONDS
        ):
            self._set_status(AgentStatus.POSSIBLY_HANGED)

    def start(self) -> None:
        """Start the terminal and set initial status to RUNNING."""
        super().start()
        self._set_status(AgentStatus.RUNNING)
        self._apply_events(self._observer.on_process_started())

    def _on_stdout(self, chars: str) -> None:
        """Hook from ScrollableTerminal.recv() — intercept for monitoring."""
        self._on_pty_output(chars)

    def _after_stdout_processed(self) -> None:
        """Debounced sentinel scanning after stdout is processed."""
        if self._scan_timer is not None:
            self._scan_timer.cancel()
        loop = asyncio.get_running_loop()
        self._scan_timer = loop.call_later(
            _SCAN_DEBOUNCE_SECONDS, self._scan_screen
        )

    def stop(self) -> None:
        """Cancel pending scan timer, then stop the terminal."""
        if self._scan_timer is not None:
            self._scan_timer.cancel()
            self._scan_timer = None
        super().stop()
