"""The inbox queue: what needs the user, kept by the engine (proposal 0013,
docs/design/inbox-plan.md I2; proposal 0012 A1 without the score).

An **actionable** lives while its condition holds (an agent waits for an
approval, an open PR is red, an agent exited on its own, a job failed) and
resolves itself when the condition clears: the desktop's attention queue
(desktop/crates/app/src/attention.rs), now derived once, in the engine, with
no UI open. Its ``occurrence`` is ``id@since``: dismissing hides that
occurrence, not a later one.

Each kind has a fixed level (:data:`KINDS`: ``now`` or ``round``, no score).
Inside a level items sort by blocks a live agent, the work item's priority
(medium until a feeder sets one) and age, oldest first (Q-B8).

The user's marks are the engine's too, so every client sees them: dismiss
(one occurrence), park until a time or an event (``ci_done``,
``agent_stopped``, ``human_review``), unpark. Marks and first-seen times are
kept in ``$XDG_STATE_HOME/lazyagent/actionables/<repo hash>.json`` (0600) by
the engine that owns the repo; agent exits and failed jobs are this engine's
only, as they were the desktop's.

New kinds plug in with :func:`register_kind` and a detector
(:meth:`ActionableQueue.add_detector`): a function from a :class:`Snapshot`
to items.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent.engine.events import AGENT_EXITED, JOB_FINISHED, JOB_STARTED
from lazyagent.engine.notes import _atomic_write
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.runtime import repo_digest, state_dir
from lazyagent.engine.status import (
    _RUNNING,
    DEFAULT_PRIORITY,
    LEVELS,
    PRIORITY_ORDER,
    STARTING,
    _failing_check,
    _with,
    ci_state,
    clip_words,
    format_age,
    repo_name,
    work_item_key,
)
from lazyagent.models import AgentStatus, PrInfo

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine
    from lazyagent.engine.events import EngineEvent
    from lazyagent.engine.trackers import TrackerHub

log = logging.getLogger(__name__)

ACTIONABLE_CREATED = "actionable.created"
ACTIONABLE_UPDATED = "actionable.updated"
ACTIONABLE_RESOLVED = "actionable.resolved"

VERSION = 1
# Dismissals older than this can no longer matter.
KEEP_DISMISSED = 7 * 86400.0
# After a restart, a condition seen again within this long keeps its first-
# seen time (and so its occurrence: a dismissal holds across restarts). PR
# info takes one poll to come back.
CARRY_GRACE = 300.0
# Agent exits and failed jobs kept, newest last.
MAX_EVENTS = 50

PARK_EVENTS = ("ci_done", "agent_stopped", "human_review")
MARK_ACTIONS = ("dismiss", "park", "unpark")


@dataclass(frozen=True)
class Kind:
    """A kind of actionable and its fixed place in the inbox."""

    name: str
    level: str
    title: str
    # Inside a level, after blocks / priority / age: the old attention rank.
    rank: int
    # An agent waits on the answer: first in its level.
    blocks_agent: bool = False


KINDS: dict[str, Kind] = {}


def register_kind(kind: Kind) -> Kind:
    if kind.level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    KINDS[kind.name] = kind
    return kind


# The attention queue's ten kinds (0013 "Kinds → level").
APPROVAL = register_kind(Kind("approval", "now", "Needs approval", 1, True))
QUESTION = register_kind(Kind("question", "now", "Question", 2, True))
HANGED = register_kind(Kind("hanged", "round", "Hanged?", 3))
FAILED = register_kind(Kind("failed", "round", "Failed", 4))
EXITED = register_kind(Kind("exited", "round", "Exited", 4))
CI = register_kind(Kind("ci", "round", "CI failing", 5))
REVIEW = register_kind(Kind("review", "round", "Changes requested", 5))
CONFLICT = register_kind(Kind("conflict", "round", "Merge conflict", 5))
DONE = register_kind(Kind("done", "round", "Your turn", 6))
JOB = register_kind(Kind("job", "round", "Job failed", 7))
# A linked tracker task entered a status mapped with a level (its own).
TRACKER_STATUS = register_kind(Kind("tracker_status", "round", "Tracker", 5))


@dataclass
class Item:
    """One actionable, as a detector finds it."""

    id: str
    kind: str
    # The work item row's state while this is its top item (≤ 5 words).
    state: str
    label: str = ""
    detail: str = ""
    work_item: str | None = None
    worktree_path: str | None = None
    agent_id: str | None = None
    pr: dict[str, Any] | None = None
    job_id: str | None = None
    exit_code: int | None = None
    priority: str = DEFAULT_PRIORITY
    since: float = 0.0
    # The kind's level unless the item carries its own (tracker_status).
    level: str | None = None

    @property
    def occurrence(self) -> str:
        return f"{self.id}@{self.since}"

    def to_dict(self, park: dict[str, Any] | None = None) -> dict[str, Any]:
        kind = KINDS[self.kind]
        return {
            "id": self.id,
            "occurrence": self.occurrence,
            "kind": self.kind,
            "level": self.level or kind.level,
            "title": kind.title,
            "label": self.label,
            "detail": self.detail,
            "state": self.state,
            "work_item": self.work_item,
            "worktree_path": self.worktree_path,
            "agent_id": self.agent_id,
            "pr": self.pr,
            "job_id": self.job_id,
            "exit_code": self.exit_code,
            "blocks_agent": kind.blocks_agent,
            "priority": self.priority,
            "since": self.since,
            "park": park,
        }


def sort_key(item: dict[str, Any]) -> tuple:
    """Level; blocks a live agent; priority; oldest first (Q-B8)."""
    kind = KINDS.get(item["kind"])
    return (
        LEVELS.index(item["level"]) if item["level"] in LEVELS else len(LEVELS),
        not item["blocks_agent"],
        PRIORITY_ORDER.get(item["priority"], 1),
        item["since"],
        kind.rank if kind is not None else 99,
        item["id"],
    )


# ----------------------------------------------------------------------
# What the detectors see
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSeen:
    agent_id: str
    status: str
    detail: str = ""
    label: str = ""


@dataclass(frozen=True)
class Place:
    """A worktree (or the orchestrator) and what is going on in it."""

    path: str
    work_item: str | None
    agents: tuple[AgentSeen, ...] = ()
    pr: PrInfo | None = None

    @property
    def running(self) -> bool:
        return any(a.status in _RUNNING for a in self.agents)


@dataclass(frozen=True)
class Exit:
    """An agent that exited on its own."""

    path: str
    agent_id: str
    label: str
    exit_code: int | None
    at: float
    work_item: str | None = None


@dataclass(frozen=True)
class JobEnd:
    job_id: str
    title: str
    exit_code: int | None


@dataclass
class Snapshot:
    places: list[Place] = field(default_factory=list)
    exits: list[Exit] = field(default_factory=list)
    jobs: list[JobEnd] = field(default_factory=list)

    def place(self, path: str | None) -> Place | None:
        return next((p for p in self.places if p.path == path), None)


Detector = Callable[[Snapshot], Iterable[Item]]

_AGENT_KINDS = {
    AgentStatus.WAITING_FOR_APPROVAL.value: (APPROVAL, "approve"),
    AgentStatus.WAITING_FOR_USER.value: (QUESTION, "answer"),
    AgentStatus.POSSIBLY_HANGED.value: (HANGED, "agent may hang"),
    AgentStatus.FAILED.value: (FAILED, "agent failed"),
}
# Turn over, at its prompt (status.py's idle): the user's turn.
_DONE_STATUSES = frozenset(
    {
        AgentStatus.COMPLETED.value,
        AgentStatus.WAITING.value,
        AgentStatus.INTERRUPTED.value,
    }
)


def _id(kind: Kind, rest: str) -> str:
    # The desktop's ids (attention.rs): "Approval:/path/a1".
    return f"{kind.name.capitalize()}:{rest}"


def _default_label(path: str, agent_id: str) -> str:
    return "Orchestrator" if path == ORCHESTRATOR_KEY else f"Agent {agent_id}"


def agent_items(snap: Snapshot) -> Iterable[Item]:
    """Agents waiting on the user, failed, hung, or done with their turn.
    "Your turn" only when nothing else runs there and there is no PR yet
    (then the PR says what is next), and never for the orchestrator, whose
    prompt is where it waits."""
    for place in snap.places:
        for a in place.agents:
            found = _AGENT_KINDS.get(a.status)
            if found is not None:
                kind, text = found
                state = _with(text, a.detail) if kind.blocks_agent else text
            elif (
                a.status in _DONE_STATUSES
                and not place.running
                and place.pr is None
                and place.path != ORCHESTRATOR_KEY
            ):
                kind, state = DONE, "review: agent finished"
            else:
                continue
            yield Item(
                _id(kind, f"{place.path}/{a.agent_id}"),
                kind.name,
                state,
                label=a.label or _default_label(place.path, a.agent_id),
                detail=a.detail or a.status,
                work_item=place.work_item,
                worktree_path=place.path,
                agent_id=a.agent_id,
            )


def pr_items(snap: Snapshot) -> Iterable[Item]:
    """An open PR that is red or conflicting with no agent at work on it, or
    whose reviewer asked for changes."""
    for place in snap.places:
        pr = place.pr
        if pr is None or pr.state.upper() != "OPEN":
            continue
        found = []
        if ci_state(pr) == "red" and not place.running:
            failed = [c.name for c in pr.checks if c.conclusion.upper() == "FAILURE"]
            found.append((CI, _with("CI red", _failing_check(pr)), ", ".join(failed)))
        if (pr.review_decision or "").upper() == "CHANGES_REQUESTED":
            found.append((REVIEW, "changes requested", "a reviewer asked for changes"))
        if (pr.mergeable or "").upper() == "CONFLICTING" and not place.running:
            found.append((CONFLICT, "conflict with base", "conflicts with the base branch"))
        for kind, state, detail in found:
            yield Item(
                _id(kind, f"{place.path}:{pr.number}"),
                kind.name,
                state,
                label=f"#{pr.number} {pr.title}",
                detail=detail,
                work_item=place.work_item,
                worktree_path=place.path,
                pr={"number": pr.number, "title": pr.title, "url": pr.url},
            )


def _exit_id(e: Exit) -> str:
    return _id(EXITED, f"{e.path}/{e.agent_id}:{e.at}")


def exit_items(snap: Snapshot) -> Iterable[Item]:
    for e in snap.exits:
        yield Item(
            _exit_id(e),
            EXITED.name,
            "agent exited",
            label=e.label,
            detail="exited" if e.exit_code is None else f"exited with code {e.exit_code}",
            work_item=e.work_item,
            worktree_path=e.path,
            agent_id=e.agent_id,
            exit_code=e.exit_code,
        )


def job_items(snap: Snapshot) -> Iterable[Item]:
    for j in snap.jobs:
        if j.exit_code == 0:
            continue
        yield Item(
            _id(JOB, j.job_id),
            JOB.name,
            "job failed",
            label=j.title,
            detail="killed" if j.exit_code is None else f"exit {j.exit_code}",
            job_id=j.job_id,
            exit_code=j.exit_code,
        )


DEFAULT_DETECTORS: tuple[Detector, ...] = (agent_items, pr_items, exit_items, job_items)


def tracker_items(hub: TrackerHub, snap: Snapshot) -> Iterable[Item]:
    """The tracker items open now (engine/trackers: a task that entered a
    status mapped with a level, until it leaves), on the work item whose
    branch links the task. The id stays while the task stays, so a
    dismissal holds; entering again is a new occurrence."""
    if not hub.items:
        return
    for place in snap.places:
        branch = (place.work_item or "").rpartition(":")[2]
        if not branch or branch.startswith("@"):
            continue
        for feed, key in hub.links(branch):
            t = hub.items.get((feed.name, key))
            if t is None:
                continue
            yield Item(
                t.id,
                TRACKER_STATUS.name,
                clip_words(t.state),
                label=t.title,
                detail=t.url,
                work_item=place.work_item,
                worktree_path=place.path,
                level=t.level,
            )


def derive(
    snap: Snapshot, detectors: Iterable[Detector] = DEFAULT_DETECTORS
) -> dict[str, Item]:
    """Every actionable whose condition holds now, by id (first detector
    wins on a duplicate id)."""
    found: dict[str, Item] = {}
    for detect in detectors:
        for item in detect(snap):
            found.setdefault(item.id, item)
    return found


# ----------------------------------------------------------------------
# Parks
# ----------------------------------------------------------------------


def check_until(until: Any, now: float) -> float | str:
    """A park's ``until``: a future Unix time or one of :data:`PARK_EVENTS`."""
    if isinstance(until, str) and until in PARK_EVENTS:
        return until
    if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
        return float(until)
    raise ValueError(
        f"until must be a future Unix time or one of {', '.join(PARK_EVENTS)}"
    )


def new_park(until: float | str, place: Place | None, now: float, by: str) -> dict[str, Any]:
    """A park, with what its event is measured against."""
    park: dict[str, Any] = {"until": until, "at": now, "by": by}
    if until == "ci_done":
        park["seen"] = place is not None and ci_state(place.pr) == "running"
    elif until == "agent_stopped":
        park["seen"] = place is not None and place.running
        park["agents"] = sorted(a.agent_id for a in place.agents) if place else []
    elif until == "human_review":
        park["review"] = (place.pr.review_decision or "") if place and place.pr else ""
    return park


def park_holds(park: dict[str, Any], place: Place | None, now: float) -> bool:
    """Whether a park still hides its item; updates what it has seen.

    ``ci_done``: CI ran (at park time or since) and is no longer running.
    ``agent_stopped``: an agent ran and none runs now, or one of the agents
    there at park time is gone. ``human_review``: the PR's review decision
    changed."""
    until = park["until"]
    if not isinstance(until, str):
        return now < until
    if until == "ci_done":
        running = place is not None and ci_state(place.pr) == "running"
        park["seen"] = park.get("seen", False) or running
        return running or not park["seen"]
    if until == "agent_stopped":
        live = {a.agent_id for a in place.agents} if place else set()
        if set(park.get("agents", ())) - live:
            return False
        running = place is not None and place.running
        park["seen"] = park.get("seen", False) or running
        return running or not park["seen"]
    if until == "human_review":
        review = (place.pr.review_decision or "") if place and place.pr else ""
        return review == park.get("review", "")
    return False


def _public_park(park: dict[str, Any] | None) -> dict[str, Any] | None:
    if park is None:
        return None
    return {"until": park["until"], "at": park["at"], "by": park.get("by", "user")}


# ----------------------------------------------------------------------
# The queue
# ----------------------------------------------------------------------


def actionables_path(repo_root: str) -> Path:
    return state_dir() / "actionables" / f"{repo_digest(repo_root)}.json"


class ActionableQueue:
    """An engine's inbox queue; see the module docstring.

    :meth:`flush` re-derives it and emits ``actionable.created`` (an item
    became visible: new, or unparked), ``actionable.updated`` (a visible
    item changed) and ``actionable.resolved {id, occurrence, reason}``
    (``cleared`` · ``dismissed`` · ``parked``). The status projection
    flushes it on each of its passes, so it moves with the same events.
    """

    def __init__(self, engine: Engine, *, clock: Callable[[], float] | None = None) -> None:
        self._engine = engine
        self._clock = clock
        self.detectors: list[Detector] = list(DEFAULT_DETECTORS)
        self._path: Path | None = None
        # id → first seen, for the conditions that hold now.
        self._since: dict[str, float] = {}
        # Loaded from disk, waiting to be seen again (CARRY_GRACE).
        self._carried: dict[str, float] = {}
        self._carry_until = 0.0
        self._dismissed: dict[str, float] = {}
        self._parked: dict[str, dict[str, Any]] = {}
        self._exits: list[Exit] = []
        self._jobs: list[JobEnd] = []
        self._job_titles: dict[str, str] = {}
        self._labels: dict[tuple[str, str], str] = {}
        self._items: dict[str, Item] = {}
        self._visible: dict[str, dict[str, Any]] = {}
        self._saved: str | None = None
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def clock(self) -> Callable[[], float]:
        # The projection's clock unless given one: tests move one clock.
        if self._clock is not None:
            return self._clock
        status = getattr(self._engine, "status", None)
        return status.clock if status is not None else time.time

    @clock.setter
    def clock(self, clock: Callable[[], float]) -> None:
        self._clock = clock

    def add_detector(self, detector: Detector) -> None:
        if detector not in self.detectors:
            self.detectors.append(detector)

    # --- Lifecycle ---

    def start(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self._engine.events.subscribe(
                self._on_event, (AGENT_EXITED, JOB_STARTED, JOB_FINISHED)
            )

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def open(self, path: Path | str) -> None:
        """Keep marks and first-seen times in ``path`` (load them now)."""
        self._path = Path(path)
        now = self.clock()
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("could not read actionables from %s: %s", self._path, e)
            return
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return

        def numbers(value: Any) -> dict[str, float]:
            if not isinstance(value, dict):
                return {}
            return {
                k: float(v)
                for k, v in value.items()
                if isinstance(k, str) and isinstance(v, (int, float))
            }

        self._carried = numbers(data.get("since"))
        self._carry_until = now + CARRY_GRACE
        self._dismissed = {
            k: at
            for k, at in numbers(data.get("dismissed")).items()
            if now - at < KEEP_DISMISSED
        }
        parked = data.get("parked")
        if isinstance(parked, dict):
            for item_id, park in parked.items():
                try:
                    if not isinstance(park, dict) or not isinstance(park.get("at"), (int, float)):
                        continue
                    until = park.get("until")
                    if isinstance(until, str):
                        check_until(until, now)
                    elif not isinstance(until, (int, float)) or until <= now:
                        continue
                    self._parked[item_id] = park
                except ValueError:
                    continue
        self._saved = self._serialize()

    def _on_event(self, event: EngineEvent) -> None:
        data = event.data if isinstance(event.data, dict) else {}
        if event.event == JOB_STARTED:
            self._job_titles[data.get("job_id", "")] = data.get("title") or ""
        elif event.event == JOB_FINISHED:
            job_id = data.get("job_id", "")
            title = self._job_titles.pop(job_id, "") or job_id
            if data.get("exit_code") != 0:
                self._jobs = [*self._jobs, JobEnd(job_id, title, data.get("exit_code"))]
                self._jobs = self._jobs[-MAX_EVENTS:]
        elif event.event == AGENT_EXITED and data.get("stopped") is False:
            path, agent_id = data.get("worktree_path", ""), data.get("agent_id", "")
            label = self._labels.get((path, agent_id)) or _default_label(path, agent_id)
            exit_ = Exit(path, agent_id, label, data.get("exit_code"), self.clock(),
                         self._work_item(path))
            self._exits = [*self._exits, exit_][-MAX_EVENTS:]
        else:
            return
        self._engine.status.mark_dirty()

    # --- Deriving ---

    def _work_item(self, path: str) -> str | None:
        engine = self._engine
        wt = next((w for w in engine.worktrees if w.path == path), None)
        return work_item_key(repo_name(engine), wt) if wt is not None else None

    def snapshot(self) -> Snapshot:
        engine = self._engine
        repo = repo_name(engine)
        # The live agents' labels, for the exit that may come next.
        self._labels = {}
        places = [Place(ORCHESTRATOR_KEY, None, self._agents(ORCHESTRATOR_KEY))]
        for wt in engine.worktrees:
            if wt.is_bare:
                continue
            places.append(
                Place(
                    wt.path,
                    work_item_key(repo, wt),
                    self._agents(wt.path),
                    engine.get_pr_info(wt.path),
                )
            )
        # An exit is answered once an agent runs in its slot again.
        live = {(p.path, a.agent_id) for p in places for a in p.agents}
        self._exits = [e for e in self._exits if (e.path, e.agent_id) not in live]
        return Snapshot(places, list(self._exits), list(self._jobs))

    def _agents(self, path: str) -> tuple[AgentSeen, ...]:
        engine = self._engine
        states = engine.agent_states(path)
        ids = list(states) + [a for a in engine.agent_ids(path) if a not in states]
        out = []
        for agent_id in ids:
            state = states.get(agent_id)
            status = state.status.value if state is not None else STARTING
            if status == AgentStatus.NO_AGENT.value:
                status = STARTING
            info = engine.get_agent(path, agent_id)
            label = info.label if info is not None else ""
            self._labels[(path, agent_id)] = label
            out.append(AgentSeen(agent_id, status, state.detail if state else "", label))
        return tuple(out)

    def flush(self, now: float | None = None) -> list[dict[str, Any]]:
        """Re-derive the queue, emit what changed, and return the visible
        items, sorted."""
        now = self.clock() if now is None else now
        snap = self.snapshot()
        found = derive(snap, self.detectors)
        if now >= self._carry_until:
            self._carried.clear()
        since = {}
        for item_id, item in found.items():
            at = self._since.get(item_id)
            if at is None:
                at = self._carried.pop(item_id, now)
            item.since = since[item_id] = at
        self._since = since
        self._items = found

        visible: dict[str, dict[str, Any]] = {}
        reasons: dict[str, str] = {}
        for item_id, item in found.items():
            if item.occurrence in self._dismissed:
                reasons[item_id] = "dismissed"
                continue
            park = self._parked.get(item_id)
            if park is not None:
                if park_holds(park, snap.place(item.worktree_path), now):
                    reasons[item_id] = "parked"
                    continue
                del self._parked[item_id]
            visible[item_id] = item.to_dict()
        restarting = now < self._carry_until
        for item_id, park in list(self._parked.items()):
            # A park on an event goes with its item (unless it may still come
            # back after a restart); one until a time stays until then.
            until = park["until"]
            if isinstance(until, str):
                if item_id not in found and not restarting:
                    del self._parked[item_id]
            elif until <= now:
                del self._parked[item_id]
        self._dismissed = {
            k: at for k, at in self._dismissed.items() if now - at < KEEP_DISMISSED
        }

        events = self._engine.events
        for item_id, item in visible.items():
            before = self._visible.get(item_id)
            if before is None:
                events.emit(ACTIONABLE_CREATED, {"item": item})
            elif before != item:
                events.emit(ACTIONABLE_UPDATED, {"item": item})
        for item_id, before in self._visible.items():
            if item_id not in visible:
                events.emit(
                    ACTIONABLE_RESOLVED,
                    {
                        "id": item_id,
                        "occurrence": before["occurrence"],
                        "reason": reasons.get(item_id)
                        or (
                            "dismissed"
                            if before["occurrence"] in self._dismissed
                            else "cleared"
                        ),
                    },
                )
        self._visible = visible
        self._save()
        return sorted(visible.values(), key=sort_key)

    # --- Reads and marks ---

    def list(
        self, *, repo: str | None = None, include_hidden: bool = False
    ) -> dict[str, Any]:
        """``actionables_list``: the visible items (and the parked ones with
        ``include_hidden``), sorted, with counts."""
        now = self.clock()
        visible = self._refresh(now)
        items = list(visible)
        parked = [
            self._items[i].to_dict(_public_park(self._parked[i]))
            for i in self._parked
            if i in self._items and i not in self._visible
            and self._items[i].occurrence not in self._dismissed
        ]
        if include_hidden:
            items = sorted(items + parked, key=sort_key)
        if repo is not None:
            items = [i for i in items if (i["work_item"] or "").startswith(f"{repo}:")]
        return {
            "items": items,
            "counts": {
                "now": sum(i["level"] == "now" for i in visible),
                "round": sum(i["level"] == "round" for i in visible),
                "parked": len(parked),
            },
            "generated_at": now,
        }

    def get(self, item_id: str) -> dict[str, Any]:
        self._refresh(self.clock())
        item = self._items.get(item_id)
        if item is None or item.occurrence in self._dismissed:
            raise ValueError(f"No actionable {item_id!r}: it is resolved or dismissed")
        return item.to_dict(_public_park(self._parked.get(item_id)))

    def mark(
        self, item_id: str, action: str, *, until: Any = None, by: str = "user"
    ) -> dict[str, Any]:
        """Dismiss the item's current occurrence, park it until ``until``,
        or unpark it. Returns the item (``park`` set while parked)."""
        if action not in MARK_ACTIONS:
            raise ValueError(f"action must be one of {', '.join(MARK_ACTIONS)}")
        now = self.clock()
        self._refresh(now)
        item = self._items.get(item_id)
        if item is None or item.occurrence in self._dismissed:
            raise ValueError(f"No actionable {item_id!r}: it is resolved or dismissed")
        if action == "dismiss":
            self._dismissed[item.occurrence] = now
            self._parked.pop(item_id, None)
            # Nothing else clears these: forget them.
            self._exits = [e for e in self._exits if _exit_id(e) != item_id]
            self._jobs = [j for j in self._jobs if _id(JOB, j.job_id) != item_id]
        elif action == "park":
            until = check_until(until, now)
            place = self.snapshot().place(item.worktree_path)
            self._parked[item_id] = new_park(until, place, now, by)
        else:
            self._parked.pop(item_id, None)
        self._refresh(now)
        park = self._parked.get(item_id)
        return item.to_dict(_public_park(park))

    def _refresh(self, now: float) -> list[dict[str, Any]]:
        """:meth:`flush`, and the rows follow when the queue changed."""
        before = self._visible
        visible = self.flush(now)
        if self._visible != before:
            self._engine.status.mark_dirty()
        return visible

    # --- Store ---

    def _serialize(self) -> str:
        return json.dumps(
            {
                "version": VERSION,
                "since": dict(sorted({**self._carried, **self._since}.items())),
                "dismissed": dict(sorted(self._dismissed.items())),
                "parked": dict(sorted(self._parked.items())),
            },
            indent=1,
        )

    def _save(self) -> None:
        if self._path is None:
            return
        text = self._serialize()
        if text == self._saved:
            return
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_write(self._path, text.encode())
            self._saved = text
        except OSError as e:
            log.warning("could not write actionables to %s: %s", self._path, e)



def format_items(result: dict[str, Any], now: float | None = None) -> str:
    """``actionables_list``'s answer as plain text, one item a line."""
    now = time.time() if now is None else now
    counts = result["counts"]
    head = f"NOW {counts['now']}   ROUND {counts['round']}"
    if counts.get("parked"):
        head += f"   ({counts['parked']} parked)"
    lines = [head]
    for item in result["items"]:
        where = item["work_item"] or item["worktree_path"] or ""
        where = where.partition(":")[2] if item["work_item"] else where
        text = " · ".join(p for p in (where, item["label"], item["detail"]) if p)
        parked = " (parked)" if item.get("park") else ""
        lines.append(
            f"{item['level']:<6} {item['title']}: {text}{parked}  "
            f"{format_age(now - item['since'])}"
        )
    if len(lines) == 1:
        lines.append("nothing needs you")
    return "\n".join(lines)
