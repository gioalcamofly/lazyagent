from __future__ import annotations

from lazyagent.models import AgentStatus
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal


class MonitoredTerminal(ScrollableTerminal):
    """View of an agent's terminal (an engine
    :class:`~lazyagent.engine.session.AgentSession` in-process).

    Status detection lives in the session, and its status and exit reach the
    app through the engine's events; this view only renders the session and
    exposes its identity.
    """

    def __init__(self, session, **kwargs) -> None:
        super().__init__(session, **kwargs)

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
