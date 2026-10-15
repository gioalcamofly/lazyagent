"""Agent lifecycle event bus — the push side of the MCP agent tools.

Every agent status change the app records is appended here as an
:class:`AgentEvent` with a monotonically increasing ``seq``. Callers keep the
``seq`` of the last event they saw (a *cursor*) and ask for what happened
after it, optionally blocking until something matching arrives. That is what
lets an orchestrator wait for an agent instead of polling ``get_agent_status``
in a loop, and what makes "wait for the turn I just started" race-free: a
cursor taken before the prompt was sent excludes the stale pre-send state.

The bus lives on the app's event loop (the IPC server shares it), so no
locking is needed; waiters are woken through an ``asyncio.Event`` that is
swapped for a fresh one on every publish.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

from lazyagent.models import AgentStatus

# Pseudo-status for an agent that is gone (process exited or was stopped).
# Not an AgentStatus member: the app drops the agent's state instead of
# storing a status for it, but waiters still need to match on it.
EXITED = "exited"

# Named groups accepted wherever a caller lists target statuses. Raw status
# values are accepted too; these cover the questions an orchestrator asks.
STATUS_GROUPS: dict[str, frozenset[str]] = {
    # Turn finished; the agent sits at its prompt ready for the next one.
    "idle": frozenset({AgentStatus.WAITING.value}),
    # The agent stopped mid-task on a dialog: a permission prompt or a
    # question it asked.
    "blocked": frozenset({
        AgentStatus.WAITING_FOR_APPROVAL.value,
        AgentStatus.WAITING_FOR_USER.value,
    }),
    # The agent's session is over, one way or another.
    "finished": frozenset({
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.INTERRUPTED.value,
        EXITED,
    }),
    # Anything but actively working — the default "wait until it stops".
    "not_running": frozenset(
        {s.value for s in AgentStatus if s is not AgentStatus.RUNNING} | {EXITED}
    ),
}

_ALL_STATUS_VALUES = frozenset(s.value for s in AgentStatus) | {EXITED}

_DEFAULT_MAXLEN = 1000


def resolve_statuses(names: str | Iterable[str] | None) -> frozenset[str] | None:
    """Expand group names / raw status values into a set of status values.

    ``None`` or empty means "no filter". Unknown names raise ``ValueError``
    listing what is accepted.
    """
    if names is None:
        return None
    if isinstance(names, str):
        names = [names]
    resolved: set[str] = set()
    for name in names:
        if name in STATUS_GROUPS:
            resolved |= STATUS_GROUPS[name]
        elif name in _ALL_STATUS_VALUES:
            resolved.add(name)
        else:
            accepted = sorted(STATUS_GROUPS) + sorted(_ALL_STATUS_VALUES)
            raise ValueError(
                f"Unknown status {name!r}. Accepted: {', '.join(accepted)}"
            )
    return frozenset(resolved) or None


@dataclass(frozen=True)
class AgentEvent:
    """One agent status transition."""

    seq: int
    time: float            # wall clock (time.time()), for humans
    worktree_path: str
    agent_id: str
    label: str
    status: str            # AgentStatus value, or EXITED
    previous_status: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


EventFilter = Callable[[AgentEvent], bool]


def make_filter(
    worktree_path: str | None = None,
    agent_id: str | None = None,
    statuses: frozenset[str] | None = None,
) -> EventFilter:
    """Build an event predicate; every ``None`` argument matches anything."""

    def _match(event: AgentEvent) -> bool:
        if worktree_path and event.worktree_path != worktree_path:
            return False
        if agent_id is not None and event.agent_id != agent_id:
            return False
        if statuses is not None and event.status not in statuses:
            return False
        return True

    return _match


class AgentEventBus:
    """Bounded, cursor-addressed log of agent events with async waiting."""

    def __init__(self, maxlen: int = _DEFAULT_MAXLEN) -> None:
        self._events: deque[AgentEvent] = deque(maxlen=maxlen)
        self._seq = 0
        self._changed = asyncio.Event()

    @property
    def cursor(self) -> int:
        """``seq`` of the newest event (0 before the first one)."""
        return self._seq

    def publish(
        self,
        *,
        worktree_path: str,
        agent_id: str,
        status: str,
        previous_status: str,
        label: str = "",
        detail: str = "",
    ) -> AgentEvent:
        self._seq += 1
        event = AgentEvent(
            seq=self._seq,
            time=time.time(),
            worktree_path=worktree_path,
            agent_id=agent_id,
            label=label,
            status=status,
            previous_status=previous_status,
            detail=detail,
        )
        self._events.append(event)
        # Wake every waiter, then give later waiters a fresh Event to block on.
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        return event

    def since(
        self, cursor: int, match: EventFilter | None = None
    ) -> tuple[list[AgentEvent], bool]:
        """Events with ``seq > cursor`` that pass ``match``.

        Also returns whether events after ``cursor`` were already evicted from
        the bounded log, so the caller knows its view has a gap.
        """
        truncated = bool(self._events) and self._events[0].seq > cursor + 1
        events = [
            e for e in self._events
            if e.seq > cursor and (match is None or match(e))
        ]
        return events, truncated

    async def wait(
        self,
        cursor: int,
        match: EventFilter | None = None,
        timeout: float = 0.0,
    ) -> tuple[list[AgentEvent], bool]:
        """Like :meth:`since`, but block up to ``timeout`` seconds for a match.

        Returns as soon as at least one matching event exists after
        ``cursor``; returns an empty list on timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        while True:
            # Grab the Event before checking, so a publish between the check
            # and the wait still wakes us.
            changed = self._changed
            events, truncated = self.since(cursor, match)
            if events:
                return events, truncated
            remaining = deadline - loop.time()
            if remaining <= 0:
                return [], truncated
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except asyncio.TimeoutError:
                return [], truncated
