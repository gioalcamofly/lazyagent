"""Agent registry: who is running where, in what state, and what changed.

The registry is a two-level map: a worktree path (or :data:`ORCHESTRATOR_KEY`)
to agent id to :class:`AgentState`. The orchestrator, and any single-agent
caller, uses agent id ``""``. Every status transition it records, and every
agent it drops, is published on its :class:`AgentEventBus`.

Agent ids are ``a1, a2, …`` per worktree and are never reused, not even
across a restart: the counters live in a small JSON file (see
:class:`AgentIdStore`). Session names are derived from the id, so a new agent
can never be mistaken for an older one that other sessions still address.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lazyagent import diagnostics as diag
from lazyagent.agent_events import EXITED, AgentEventBus
from lazyagent.models import AgentState, AgentStatus, LifecycleConfidence, rollup_status

log = logging.getLogger(__name__)

# Registry key of the orchestrator agent, which has no worktree of its own.
ORCHESTRATOR_KEY = "__orchestrator__"

_ID_RE = re.compile(r"a(\d+)")
_STORE_VERSION = 1


def default_id_store_path() -> Path:
    return diag.state_dir() / "agent_ids.json"


def _id_number(agent_id: str) -> int:
    match = _ID_RE.fullmatch(agent_id)
    return int(match.group(1)) if match else 0


class AgentIdStore:
    """Per-worktree agent id counters, persisted in one JSON file.

    Several lazyagent instances (one per repo) share the file, so every
    allocation is a read-modify-write under an exclusive ``flock`` on a
    sibling ``.lock`` file; the data file itself is swapped in with
    ``os.replace``, which is why the lock cannot live on it.

    A missing or unreadable file counts as empty and an unwritable one is
    logged and skipped: losing the counters only risks reusing an id from a
    previous run, which is no reason to stop spawning agents.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    def allocate(self, key: str, floor: int = 0) -> int:
        """Return the next id number for ``key``, above ``floor`` and every
        number handed out for it before."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                counters = self._read()
                number = max(counters.get(key, 0), floor) + 1
                counters[key] = number
                self._write(counters)
                return number
        except OSError as e:
            log.warning("agent id store %s unavailable (%s); ids not persisted", self.path, e)
            return floor + 1

    def _read(self) -> dict[str, int]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
            counters = data["counters"]
            if not isinstance(counters, dict):
                raise TypeError("counters is not an object")
            return {
                k: v for k, v in counters.items()
                if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            }
        except (ValueError, KeyError, TypeError) as e:
            log.warning("agent id store %s is corrupt (%s); starting over", self.path, e)
            return {}

    def _write(self, counters: dict[str, int]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": _STORE_VERSION, "counters": counters}, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


@dataclass
class _Identity:
    label: str = ""
    session_name: str = ""


class AgentRegistry:
    """Agent states per worktree, their identities, ids and event log."""

    def __init__(
        self,
        *,
        id_store: AgentIdStore | None = None,
        events: AgentEventBus | None = None,
    ) -> None:
        self.states: dict[str, dict[str, AgentState]] = {}
        self.events = events if events is not None else AgentEventBus()
        self._id_store = id_store
        self._identities: dict[str, dict[str, _Identity]] = {}
        self._last_id: dict[str, int] = {}

    # --- Ids and identity ---

    def new_agent_id(self, key: str) -> str:
        """Allocate the next agent id for a worktree (``a1``, ``a2``, …)."""
        live = [
            _id_number(aid)
            for aid in (*self.states.get(key, {}), *self._identities.get(key, {}))
        ]
        floor = max([self._last_id.get(key, 0), *live])
        if self._id_store is None:
            number = floor + 1
        else:
            number = self._id_store.allocate(_store_key(key), floor)
        self._last_id[key] = number
        return f"a{number}"

    def register(self, key: str, agent_id: str, *, label: str = "", session_name: str = "") -> None:
        """Record a spawned agent's tab label and session name."""
        self._identities.setdefault(key, {})[agent_id] = _Identity(label, session_name)
        state = self.states.get(key, {}).get(agent_id)
        if state is not None:
            self._apply_identity(key, agent_id, state)

    def unregister(self, key: str, agent_id: str) -> None:
        """Forget an agent's identity (its tab is gone)."""
        bucket = self._identities.get(key)
        if bucket is not None:
            bucket.pop(agent_id, None)
            if not bucket:
                del self._identities[key]

    def label(self, key: str, agent_id: str) -> str:
        identity = self._identities.get(key, {}).get(agent_id)
        return identity.label if identity else ""

    def session_name(self, key: str, agent_id: str) -> str:
        identity = self._identities.get(key, {}).get(agent_id)
        return identity.session_name if identity else ""

    def _apply_identity(self, key: str, agent_id: str, state: AgentState) -> None:
        identity = self._identities.get(key, {}).get(agent_id)
        if identity is None:
            return
        if not state.label:
            state.label = identity.label
        if not state.session_name:
            state.session_name = identity.session_name

    # --- States ---

    def get(self, key: str, agent_id: str = "") -> AgentState:
        """Get or create the state of one agent."""
        bucket = self.states.setdefault(key, {})
        state = bucket.get(agent_id)
        if state is None:
            state = bucket[agent_id] = AgentState(agent_id=agent_id)
            self._apply_identity(key, agent_id, state)
        return state

    def agents(self, key: str) -> dict[str, AgentState]:
        """All agent states of a worktree / the orchestrator (may be empty)."""
        return self.states.get(key, {})

    def rollup(self, key: str) -> AgentStatus:
        return rollup_status(s.status for s in self.agents(key).values())

    def update_status(
        self,
        key: str,
        agent_id: str,
        status: AgentStatus,
        confidence: LifecycleConfidence,
        detail: str = "",
    ) -> AgentState:
        """Record an agent's status; publish an event if the status changed."""
        state = self.get(key, agent_id)
        previous = state.status
        state.status = status
        state.confidence = confidence
        state.detail = detail
        self._apply_identity(key, agent_id, state)
        if status != previous:
            self.events.publish(
                worktree_path=key,
                agent_id=agent_id,
                label=state.label,
                status=status.value,
                previous_status=previous.value,
                detail=detail,
            )
        return state

    def drop(self, key: str, agent_id: str) -> None:
        """Remove one agent, publishing EXITED once; prunes empty buckets."""
        bucket = self.states.get(key)
        if bucket is None:
            return
        state = bucket.pop(agent_id, None)
        if not bucket:
            self.states.pop(key, None)
        if state is not None:
            self.events.publish(
                worktree_path=key,
                agent_id=agent_id,
                label=state.label,
                status=EXITED,
                previous_status=state.status.value,
            )

    def drop_worktree(self, key: str) -> None:
        """Drop every agent of a worktree that is going away."""
        for agent_id in list(self.agents(key)):
            self.drop(key, agent_id)


def _store_key(key: str) -> str:
    return key if key == ORCHESTRATOR_KEY else os.path.abspath(key)
