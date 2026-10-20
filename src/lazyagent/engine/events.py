"""The engine's event bus: every change a client may react to, in one order.

:class:`EventBus` extends :class:`~lazyagent.agent_events.AgentEventBus`, the
cursor log behind the MCP ``list_events`` / ``send_agent_input`` tools, with
the other engine events (worktrees, git status, PR info, spawned and exited
agents, jobs, notices). All of them share one ``seq``, so a client can order
anything it is told. ``since`` / ``wait`` still return agent status
transitions only: an MCP cursor is just a ``seq``, and skipping the other
events keeps it valid.

Subscribers get :class:`EngineEvent` objects whose ``data`` is the JSON
payload described in ``protocol/schema/v1/events.json``.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from lazyagent.agent_events import AgentEvent, AgentEventBus
from lazyagent.engine.worktrees import (  # noqa: F401  (re-exported event names)
    GIT_STATUS_UPDATED,
    PR_INFO_UPDATED,
    WORKTREES_CHANGED,
)

log = logging.getLogger(__name__)

AGENT_STATUS = "agent.status"
AGENT_UPDATED = "agent.updated"
AGENT_SPAWNED = "agent.spawned"
AGENT_EXITED = "agent.exited"
ENGINE_NOTICE = "engine.notice"
ENGINE_SHUTDOWN = "engine.shutdown"
ENGINE_RESTORED = "engine.restored"
RESTORABLE_CHANGED = "restorable.changed"
NOTES_UPDATED = "notes.updated"
NOTES_REMOVED = "notes.removed"
REVIEW_REPLIED = "review.replied"
JOB_STARTED = "job.started"
JOB_OUTPUT = "job.output"
JOB_FINISHED = "job.finished"
UI_COMMAND = "ui.command"
UI_VIEW_CHANGED = "ui.view_changed"

_DEFAULT_LOG_MAXLEN = 1000


@dataclass(frozen=True)
class EngineEvent:
    """One engine event: its place in the engine-wide order, name and payload."""

    seq: int
    event: str
    data: Any


EventCallback = Callable[[EngineEvent], None]


class EventBus(AgentEventBus):
    """Agent event log plus the engine's other events, under one ``seq``."""

    def __init__(
        self, maxlen: int = 1000, log_maxlen: int = _DEFAULT_LOG_MAXLEN
    ) -> None:
        super().__init__(maxlen)
        self._log: deque[EngineEvent] = deque(maxlen=log_maxlen)
        self._subscribers: list[tuple[EventCallback, frozenset[str] | None]] = []

    def subscribe(
        self, callback: EventCallback, events: Iterable[str] | None = None
    ) -> Callable[[], None]:
        """Call ``callback`` with every event (or only those named in
        ``events``); returns an unsubscribe function."""
        entry = (callback, frozenset(events) if events is not None else None)
        self._subscribers.append(entry)

        def unsubscribe() -> None:
            if entry in self._subscribers:
                self._subscribers.remove(entry)

        return unsubscribe

    def emit(self, event: str, data: Any) -> EngineEvent:
        """Record and deliver one event other than ``agent.status``."""
        return self._deliver(EngineEvent(self._next_seq(), event, data))

    def history(self, cursor: int = 0) -> list[EngineEvent]:
        """Every event still held with ``seq > cursor``, oldest first."""
        return [e for e in self._log if e.seq > cursor]

    def replay(self, cursor: int) -> tuple[list[EngineEvent], bool]:
        """:meth:`history`, and whether events after ``cursor`` were already
        evicted (the replay has a gap)."""
        truncated = bool(self._log) and self._log[0].seq > cursor + 1
        return self.history(cursor), truncated

    def _published(self, event: AgentEvent) -> None:
        self._deliver(EngineEvent(event.seq, AGENT_STATUS, event.to_dict()))

    def _deliver(self, event: EngineEvent) -> EngineEvent:
        self._log.append(event)
        for callback, names in list(self._subscribers):
            if names is not None and event.event not in names:
                continue
            try:
                callback(event)
            except Exception:
                log.exception("event subscriber failed on %s", event.event)
        return event
