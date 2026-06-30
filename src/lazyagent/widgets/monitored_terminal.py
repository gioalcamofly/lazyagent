from __future__ import annotations

import asyncio
import time

from lazyagent.agent_observers import AgentObserver
from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentStatus, LifecycleConfidence
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal

_HANG_SECONDS = 600  # 10 minutes
_SCAN_DEBOUNCE_SECONDS = 0.15
# Lower bound between two consecutive screen scans. The debounce alone lets
# scans fire every ~150 ms when output is bursty; this caps the dispatch rate
# regardless of stream cadence so a long stream of short bursts doesn't keep
# scanning at full speed.
_SCAN_MIN_INTERVAL = 0.5


class MonitoredTerminal(ScrollableTerminal):
    """ScrollableTerminal subclass that intercepts pty output for agent status detection.

    Delegates lifecycle detection to a provider-specific observer and tracks
    output timing for hang detection.
    """

    def __init__(
        self,
        command: str,
        worktree_path: str,
        observer: AgentObserver | None = None,
        agent_id: str = "",
        **kwargs,
    ) -> None:
        super().__init__(command=command, **kwargs)
        self.worktree_path = worktree_path
        self.agent_id = agent_id
        self._status = AgentStatus.NO_AGENT
        self._detail = ""
        self._last_output_time: float | None = None
        self._observer = observer or AgentObserver()
        self._scan_timer: asyncio.TimerHandle | None = None
        # Predicted dispatch time of the latest scheduled scan; used in
        # _after_stdout_processed to enforce _SCAN_MIN_INTERVAL.
        self._next_scan_dispatch: float = 0.0

    @property
    def agent_status(self) -> AgentStatus:
        return self._status

    @property
    def last_output_time(self) -> float | None:
        return self._last_output_time

    def _set_status(
        self,
        new_status: AgentStatus,
        confidence: LifecycleConfidence = LifecycleConfidence.LOW,
        detail: str = "",
    ) -> None:
        if new_status != self._status or detail != self._detail:
            self._status = new_status
            self._detail = detail
            if not self._stopped:
                self.post_message(
                    AgentStatusChanged(
                        self.worktree_path,
                        new_status,
                        confidence=confidence,
                        detail=detail,
                        agent_id=self.agent_id,
                    )
                )

    def _on_pty_output(self, chars: str) -> None:
        """Track output timing. Called from recv() on each stdout chunk."""
        self._last_output_time = time.monotonic()

        # First output transitions from NO_AGENT to RUNNING
        if self._status == AgentStatus.NO_AGENT:
            self._set_status(AgentStatus.RUNNING)
        self._apply_events(self._observer.on_terminal_output(chars))

    def _apply_events(self, events) -> None:
        for event in events:
            self._set_status(
                event.status,
                confidence=event.confidence,
                detail=event.detail,
            )

    def _rendered_screen_text(self) -> str:
        lines: list[str] = []
        for y in range(self._screen.lines):
            row = self._screen.buffer[y]
            lines.append(
                "".join(row[x].data for x in range(self._screen.columns))
            )
        return "\n".join(lines)

    def _scan_screen(self) -> None:
        """Delegate screen-based lifecycle detection to the observer.

        Uses the already-parsed screen content so ANSI codes don't interfere.
        Also polls the observer for any file-based events.
        """
        screen_text = self._rendered_screen_text()
        self._apply_events(
            self._observer.on_screen_update(
                screen_text,
                current_status=self._status,
                current_detail=self._detail,
            )
        )
        self._apply_events(self._observer.poll())

    def _on_recv_disconnect(self) -> None:
        """Handle pty disconnect."""
        self._apply_events(self._observer.on_disconnect())
        self._apply_events(self._observer.poll())
        self._observer.cleanup()
        self._status = AgentStatus.NO_AGENT
        if not self._stopped:
            self.post_message(AgentExited(self.worktree_path, agent_id=self.agent_id))

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
        """Schedule a debounced screen scan, throttled to _SCAN_MIN_INTERVAL.

        Without the floor, bursty output causes scans to fire every ~150 ms
        whenever the stream briefly pauses. Capping the dispatch rate keeps
        the full-screen text join from running more than ~2 Hz regardless
        of output cadence.
        """
        if self._scan_timer is not None:
            self._scan_timer.cancel()
        loop = asyncio.get_running_loop()
        now = loop.time()
        delay = max(
            _SCAN_DEBOUNCE_SECONDS,
            self._next_scan_dispatch - now,
        )
        self._next_scan_dispatch = now + delay + _SCAN_MIN_INTERVAL
        self._scan_timer = loop.call_later(delay, self._scan_screen)

    def stop(self) -> None:
        """Cancel pending scan timer, then stop the terminal."""
        if self._scan_timer is not None:
            self._scan_timer.cancel()
            self._scan_timer = None
        super().stop()
