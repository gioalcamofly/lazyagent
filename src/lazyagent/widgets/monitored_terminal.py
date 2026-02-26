from __future__ import annotations

import asyncio
import re
import time

from textual import log

from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentStatus
from lazyagent.widgets.scrollable_terminal import (
    DECSET_PREFIX,
    ScrollableTerminal,
    _re_ansi_sequence,
)

_SENTINEL = "your turn"
_HANG_SECONDS = 600  # 10 minutes


class MonitoredTerminal(ScrollableTerminal):
    """ScrollableTerminal subclass that intercepts pty output for agent status detection.

    Scans raw chars for a sentinel phrase ("your turn") and tracks output
    timing for hang detection.
    """

    def __init__(
        self,
        command: str,
        worktree_path: str,
        **kwargs,
    ) -> None:
        super().__init__(command=command, **kwargs)
        self.worktree_path = worktree_path
        self._status = AgentStatus.NO_AGENT
        self._last_output_time: float | None = None

    @property
    def agent_status(self) -> AgentStatus:
        return self._status

    @property
    def last_output_time(self) -> float | None:
        return self._last_output_time

    def _set_status(self, new_status: AgentStatus) -> None:
        if new_status != self._status:
            self._status = new_status
            self.post_message(AgentStatusChanged(self.worktree_path, new_status))

    def _on_pty_output(self, chars: str) -> None:
        """Track output timing. Called from recv() on each stdout chunk."""
        self._last_output_time = time.monotonic()

        # First output transitions from NO_AGENT to RUNNING
        if self._status == AgentStatus.NO_AGENT:
            self._set_status(AgentStatus.RUNNING)

    def _scan_screen(self) -> None:
        """Check the rendered pyte screen buffer for the sentinel phrase.

        Uses the already-parsed screen content so ANSI codes don't interfere.
        Keeps WAITING as long as the sentinel is visible on screen.
        """
        lines: list[str] = []
        for y in range(self._screen.lines):
            row = self._screen.buffer[y]
            lines.append(
                "".join(row[x].data for x in range(self._screen.columns))
            )
        text = "\n".join(lines).lower()

        if _SENTINEL in text:
            self._set_status(AgentStatus.WAITING)
        elif self._status in (AgentStatus.WAITING, AgentStatus.POSSIBLY_HANGED):
            self._set_status(AgentStatus.RUNNING)

    def _on_recv_disconnect(self) -> None:
        """Handle pty disconnect."""
        self._status = AgentStatus.NO_AGENT
        self.post_message(AgentExited(self.worktree_path))

    def check_hang(self) -> None:
        """Called periodically by the app timer. Posts POSSIBLY_HANGED if stale."""
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

    def _on_stdout(self, chars: str) -> None:
        """Hook from ScrollableTerminal.recv() — intercept for monitoring."""
        self._on_pty_output(chars)

    async def recv(self) -> None:
        """Override to add sentinel scanning after each stdout chunk."""
        try:
            while True:
                message = await self.recv_queue.get()
                cmd = message[0]

                if cmd == "setup":
                    await self.send_queue.put(["set_size", self.nrow, self.ncol])

                elif cmd == "stdout":
                    chars = message[1]

                    # Monitoring hook
                    self._on_stdout(chars)

                    # Detect mouse tracking toggles
                    for sep_match in re.finditer(_re_ansi_sequence, chars):
                        sequence = sep_match.group(0)
                        if sequence.startswith(DECSET_PREFIX):
                            parameters = sequence.removeprefix(
                                DECSET_PREFIX
                            ).split(";")
                            if "1000h" in parameters:
                                self.mouse_tracking = True
                            if "1000l" in parameters:
                                self.mouse_tracking = False

                    # Remember whether we're at the bottom before feeding
                    was_at_bottom = self.is_vertical_scroll_end

                    # Feed to pyte
                    try:
                        self.stream.feed(chars)
                    except TypeError as error:
                        log.warning("could not feed:", error)

                    # Update virtual size and repaint
                    self._update_virtual_size()
                    self.refresh()

                    # Auto-scroll to bottom if we were there before
                    if was_at_bottom:
                        self.scroll_end(animate=False)

                    # Sentinel detection (uses rendered screen, immune to ANSI)
                    self._scan_screen()

                elif cmd == "disconnect":
                    self._on_recv_disconnect()
                    self.stop()

        except asyncio.CancelledError:
            pass
