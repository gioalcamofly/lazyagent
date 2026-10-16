from __future__ import annotations

from lazyagent.engine.session import (
    AgentSession,
    SessionEvent,
    SessionExited,
    StatusChanged,
)
from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentStatus
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal


class MonitoredTerminal(ScrollableTerminal):
    """View of an :class:`AgentSession`.

    Status detection lives in the session; this widget re-posts the session's
    status and exit events as Textual messages so they reach the app.
    """

    session: AgentSession

    def __init__(self, session: AgentSession, **kwargs) -> None:
        super().__init__(session, **kwargs)
        self._remove_listener = session.add_listener(self._on_session_event)

    @property
    def worktree_path(self) -> str:
        return self.session.worktree_path

    @property
    def agent_id(self) -> str:
        return self.session.agent_id

    @property
    def agent_status(self) -> AgentStatus:
        return self.session.status

    @property
    def last_output_time(self) -> float | None:
        return self.session.last_output_time

    def _on_session_event(self, event: SessionEvent) -> None:
        if isinstance(event, StatusChanged):
            self.post_message(
                AgentStatusChanged(
                    event.worktree_path,
                    event.status,
                    confidence=event.confidence,
                    detail=event.detail,
                    agent_id=event.agent_id,
                    last_output_time=event.last_output_time,
                )
            )
        elif isinstance(event, SessionExited):
            self.post_message(
                AgentExited(
                    event.worktree_path,
                    agent_id=event.agent_id,
                    exit_code=event.exit_code,
                )
            )

    def on_unmount(self) -> None:
        super().on_unmount()
        self._remove_listener()
