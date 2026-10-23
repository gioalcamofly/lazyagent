"""Pass to orchestrator: the user hands one work item to the orchestrator for
a while (proposal 0013 §"The orchestrator", docs/design/inbox-plan.md §5c,
I3).

A **claim** is ``{key, holder, at, until, allowed, by}``: the gesture
``status_gesture {key, kind: "delegate", to: "orchestrator", ttl_s?}``
creates it (user only, and only while an orchestrator runs), and the
orchestrator is woken with one fixed line once it is idle. While the claim
holds, the work item's row is the orchestrator's (``with orchestrator``)
and its inbox items leave the user's inbox, except tool approvals, which
always stay in Now. The orchestrator ends it with ``orchestrator_release
{key, outcome, note}``; the user takes it back with ``status_gesture {kind:
"clear"}``. A claim that expires, or whose orchestrator exits, **lapses**:
the item comes back to the user's Round as ``delegation lapsed`` (a
``delegation`` actionable, dismissed like any other). A release with
``outcome: "returned"`` does the same with the orchestrator's note.

A **hold** is the orchestrator's own claim (``orchestrator_hold {key, note,
ttl_s}``, its token only; default :data:`HOLD_TTL`, at most
:data:`MAX_HOLD_TTL`): it verifies the item or waits on a merge. The row
reads ``orchestrator · <note>`` in flight and never reaches Round, but a
hold widens no mandate: questions and briefs still follow the first
responder's routing. The engine holds on its own, for the orchestrator, the
``done`` and ``review`` items of agents it spawned (``[orchestrator]
hold_spawned_ttl_min``, 60; 0 turns it off); a hold that expires comes back
to the user as "orchestrator did not respond". ``orchestrator_release``
ends a pass or a hold; its note stays on the row (``orchestrator: <note>``)
and on the timeline.

While a pass or a hold lasts, only what is still the user's stays in the
user's inbox: tool approvals, and questions and briefs the routing left to
the user (an automatic hold hides only the items it holds).

Claims and holds live in the engine's memory only: an engine restart ends
them, and the rows fall back to their own derivation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lazyagent.engine.actionables import Item, Kind, Snapshot, register_kind
from lazyagent.engine.orchestrator import AWAY_ACTIONS, NEVER_ALONE  # noqa: F401
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.status import _IDLE, clip_words

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

CLAIM_CREATED = "orchestrator.claim"
CLAIM_RELEASED = "orchestrator.released"
CLAIM_LAPSED = "orchestrator.lapsed"
HOLD_CREATED = "orchestrator.hold"

HOLDER = "orchestrator"
MAX_TTL = 86400.0
MIN_TTL = 60.0
HOLD_TTL = 7200.0
MAX_HOLD_TTL = 8 * 3600.0
HOLD_NOTE_CHARS = 60
# What a pass or a hold leaves in the user's inbox: what the orchestrator
# may not answer (approvals) or was not routed (questions, briefs).
USERS_KINDS = frozenset({"approval", "question", "decision"})
# The finished work of the orchestrator's own workers it holds first.
AUTO_HOLD_KINDS = frozenset({"done", "review"})
NO_RESPONSE = "orchestrator did not respond"
# orchestrator_release: done (nothing for the user), returned (back to the
# user's Round with the note).
OUTCOMES = ("done", "returned")
# What a claim allows (§5c, the away column: the user handed it over,
# shortened by [orchestrator] away_allow), plus marking its inbox items.
# The never-alone list stays out (engine/orchestrator.py).
ALLOWED = (*AWAY_ACTIONS, "mark")
NOTE_CHARS = 200

DELEGATION = register_kind(Kind("delegation", "round", "Back from the orchestrator", 5))


@dataclass
class Claim:
    key: str
    worktree_path: str | None
    at: float
    until: float
    by: str = "user"
    holder: str = HOLDER
    allowed: tuple[str, ...] = ALLOWED
    # pass (the user's) · hold (the orchestrator's own)
    kind: str = "pass"
    note: str = ""
    # An automatic hold hides only these items (their ids).
    items: tuple[str, ...] = ()

    @property
    def auto(self) -> bool:
        return bool(self.items)

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "hold":
            return {
                "key": self.key,
                "holder": self.holder,
                "at": self.at,
                "until": self.until,
                "by": self.by,
                "note": self.note,
                "auto": self.auto,
            }
        return {
            "key": self.key,
            "holder": self.holder,
            "at": self.at,
            "until": self.until,
            "by": self.by,
            "allowed": list(self.allowed),
        }


@dataclass
class Returned:
    """A claim that came back to the user."""

    claim: Claim
    # expired · orchestrator_gone · returned
    reason: str
    at: float
    note: str = ""

    @property
    def state(self) -> str:
        if self.reason == "returned":
            return clip_words(self.note) if self.note else "returned by orchestrator"
        if self.claim.kind == "hold" and self.reason == "expired":
            return NO_RESPONSE
        return "delegation lapsed"

    @property
    def detail(self) -> str:
        if self.reason == "expired" and self.claim.kind == "hold":
            return f"{NO_RESPONSE}: its hold ({self.claim.note}) expired"
        if self.reason == "expired":
            return "the claim expired before the orchestrator released it"
        if self.reason == "orchestrator_gone":
            return "the orchestrator exited while it held the item"
        return self.note or "the orchestrator handed it back"


@dataclass
class _Wake:
    key: str
    text: str
    at: float = field(default_factory=time.time)


def hold_wake_text(claim: Claim, what: str) -> str:
    until = time.strftime("%H:%M", time.localtime(claim.until))
    return (
        f"Item {claim.key}: {what}; held for you until {until}. Verify it, then call "
        "orchestrator_release (done, or returned with a note for the user); "
        f"at {until} it goes to the user."
    )


def wake_text(claim: Claim) -> str:
    until = time.strftime("%H:%M", time.localtime(claim.until))
    return (
        f"Item {claim.key}: passed to you by the user until {until}. Read it "
        "with status_list; when you are done, or to hand it back, call "
        "orchestrator_release."
    )


class Claims:
    """The engine's claims; see the module docstring."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.active: dict[str, Claim] = {}
        self.holds: dict[str, Claim] = {}
        self.returned: dict[str, Returned] = {}
        # Item occurrences already held once on their own (not again).
        self._auto_seen: set[str] = set()
        self._wakes: list[_Wake] = []
        self._waking = False
        engine.actionables.add_detector(self.items)
        engine.actionables.on_dismiss.append(self._dismissed)

    @property
    def clock(self) -> Callable[[], float]:
        return self._engine.status.clock

    def orchestrator_alive(self) -> bool:
        return self._engine.has_agent(ORCHESTRATOR_KEY)

    def get(self, key: str) -> dict[str, Any] | None:
        claim = self.active.get(key)
        return claim.to_dict() if claim is not None else None

    def get_hold(self, key: str) -> dict[str, Any] | None:
        hold = self.holds.get(key)
        return hold.to_dict() if hold is not None else None

    def filter(self, key: str, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """``key``'s inbox items left to the user while a pass or a hold
        lasts (all of them otherwise)."""
        # What the orchestrator handed back is the freshest word on the item.
        items = sorted(items, key=lambda i: i["kind"] != DELEGATION.name)
        hold = self.holds.get(key)
        if hold is not None and hold.auto and key not in self.active:
            return [i for i in items if i["id"] not in hold.items]
        if key in self.active or hold is not None:
            return [i for i in items if i["kind"] in USERS_KINDS]
        return items

    # --- Gestures ---

    def claim(self, key: str, *, ttl_s: Any = None, by: str = "user") -> Claim:
        """Hand ``key`` to the orchestrator for ``ttl_s`` seconds (the
        config's ``claim_ttl_s`` by default). A second pass renews it."""
        if not self.orchestrator_alive():
            raise ValueError(
                "No orchestrator is running: start one, then pass the item to it"
            )
        row = self._engine.status.row(key)
        if row is None:
            raise ValueError(f"No work item {key!r}")
        if row["ball"] == "done":
            raise ValueError(f"{key} is done: nothing to pass")
        if ttl_s is None:
            ttl_s = self._engine.config.orchestrator.claim_ttl_s
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)):
            raise ValueError("ttl_s must be a number of seconds")
        if not MIN_TTL <= ttl_s <= MAX_TTL:
            raise ValueError(f"ttl_s must be between {MIN_TTL:g} and {MAX_TTL:g}")
        now = self.clock()
        allowed = (*self._engine.first_responder.away_allowed(), "mark")
        claim = Claim(key, row["worktree_path"], now, now + float(ttl_s), by, allowed=allowed)
        self.active[key] = claim
        self.returned.pop(key, None)
        self._engine.events.emit(CLAIM_CREATED, {"claim": claim.to_dict()})
        actor = "user" if by == "user" else "engine"
        self._engine.overlay.add(key, "delegate", actor, "passed to the orchestrator until "
                                 + time.strftime("%H:%M", time.localtime(claim.until)))
        self._wakes = [w for w in self._wakes if w.key != key]
        self._wakes.append(_Wake(key, wake_text(claim), now))
        self._changed()
        return claim

    def hold(
        self, key: str, note: Any, *, ttl_s: Any = None, by: str = "orchestrator",
        items: tuple[str, ...] = (), wake: str | None = None,
        row: dict[str, Any] | None = None,
    ) -> Claim:
        """The orchestrator holds ``key`` itself for ``ttl_s`` seconds
        (:data:`HOLD_TTL` by default, at most :data:`MAX_HOLD_TTL`) while it
        verifies or waits; a second hold renews it."""
        if not self.orchestrator_alive():
            raise ValueError("No orchestrator is running")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("note is required: what you are doing (shown on the row)")
        if row is None:
            row = self._engine.status.row(key)
        if row is None:
            raise ValueError(f"No work item {key!r}")
        if row["ball"] == "done":
            raise ValueError(f"{key} is done: nothing to hold")
        if key in self._engine.overlay.kept:
            raise ValueError(f"{key} is kept for the user: not yours to hold")
        if ttl_s is None:
            ttl_s = HOLD_TTL
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)):
            raise ValueError("ttl_s must be a number of seconds")
        if not MIN_TTL <= ttl_s <= MAX_HOLD_TTL:
            raise ValueError(f"ttl_s must be between {MIN_TTL:g} and {MAX_HOLD_TTL:g}")
        now = self.clock()
        note = " ".join(note.split())[:HOLD_NOTE_CHARS]
        hold = Claim(key, row["worktree_path"], now, now + float(ttl_s), by,
                     kind="hold", note=note, items=items)
        self.holds[key] = hold
        self.returned.pop(key, None)
        self._engine.events.emit(HOLD_CREATED, {"hold": hold.to_dict()})
        until = time.strftime("%H:%M", time.localtime(hold.until))
        self._engine.overlay.add(key, "delegate", "orchestrator" if by == "orchestrator" else "engine",
                                 f"held by the orchestrator until {until}: {note}")
        if wake is not None:
            self._wakes = [w for w in self._wakes if w.key != key]
            self._wakes.append(_Wake(key, hold_wake_text(hold, wake), now))
        self._changed()
        return hold

    def auto_hold(self, items: Iterable[dict[str, Any]], now: float) -> None:
        """Hold for the orchestrator the finished work of the agents it
        spawned (``done``, ``review``), once per occurrence."""
        ttl = self._engine.config.orchestrator.hold_spawned_ttl_min * 60
        if ttl <= 0:
            return
        overlay = self._engine.overlay
        alive: bool | None = None
        for item in items:
            key = item["work_item"]
            if item["kind"] not in AUTO_HOLD_KINDS or key is None:
                continue
            if item["occurrence"] in self._auto_seen:
                continue
            spawned = overlay.spawned_by(key)
            workers = [a for a, by in spawned.items() if by == "orchestrator"]
            agent = item.get("agent_id")
            if not workers or (agent is not None and agent not in workers):
                continue
            if key in self.active or key in self.holds or key in overlay.kept:
                continue
            if alive is None:
                alive = self.orchestrator_alive()
            if not alive:
                return
            self._auto_seen.add(item["occurrence"])
            who = agent or workers[-1]
            what = "agent finished" if item["kind"] == "done" else "changes requested"
            try:
                self.hold(
                    key, f"checking agent {who}'s work", ttl_s=max(MIN_TTL, min(ttl, MAX_HOLD_TTL)),
                    by="engine", items=(item["id"],),
                    row={"worktree_path": item["worktree_path"], "ball": "you"},
                    wake=f"agent {who} (spawned by you) — {what}: {item['state']}",
                )
            except ValueError as e:
                log.info("not holding %s for the orchestrator: %s", key, e)

    def clear(self, key: str) -> Claim:
        """The user takes the item back: the pass or the hold ends, nothing
        is raised."""
        claim = self.active.pop(key, None)
        if claim is None:
            claim = self.holds.pop(key, None)
        if claim is None:
            raise ValueError(f"{key} is not with the orchestrator")
        self._wakes = [w for w in self._wakes if w.key != key]
        self._engine.events.emit(
            CLAIM_RELEASED,
            {"key": key, "outcome": "taken_back", "note": "", "claim": claim.to_dict()},
        )
        self._changed()
        return claim

    def release(self, key: str, outcome: str, note: str = "") -> Claim:
        """The orchestrator ends its pass or hold: ``done``, or ``returned``
        to the user's Round with ``note``. The note shows on the row and on
        the timeline either way."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        claim = self.active.pop(key, None)
        if claim is None:
            claim = self.holds.pop(key, None)
        if claim is None:
            raise ValueError(f"{key} is not with the orchestrator (no pass or hold, or it lapsed)")
        note = " ".join(note.split())[:NOTE_CHARS]
        now = self.clock()
        self._wakes = [w for w in self._wakes if w.key != key]
        overlay = self._engine.overlay
        if outcome == "returned":
            self.returned[key] = Returned(claim, "returned", now, note)
        elif claim.auto:
            # Verified: what it held is not the user's any more.
            for item_id in claim.items:
                try:
                    self._engine.actionables.mark(item_id, "dismiss", by=HOLDER)
                except ValueError:
                    pass
        overlay.release(key, outcome, note, now)
        what = "handed back" if outcome == "returned" else "done"
        overlay.add(key, "delegate", "orchestrator", f"orchestrator {what}: {note}" if note
                    else f"orchestrator {what}")
        self._engine.status.by(key, "orchestrator")
        self._engine.events.emit(
            CLAIM_RELEASED,
            {"key": key, "outcome": outcome, "note": note, "claim": claim.to_dict()},
        )
        self._changed()
        return claim

    def _changed(self) -> None:
        self._engine.status.mark_dirty()

    # --- On each status pass ---

    def check(self, now: float) -> None:
        """Lapse what expired or lost its orchestrator; wake the orchestrator
        if it is idle and something waits for it."""
        if not self.active and not self.holds:
            self._wakes.clear()
            return
        alive = self.orchestrator_alive()
        keys = set(self._engine.status.live_keys())
        for table in (self.active, self.holds):
            for key, claim in list(table.items()):
                if not alive:
                    self._lapse(key, "orchestrator_gone", now, table)
                elif now >= claim.until:
                    self._lapse(key, "expired", now, table)
                elif keys and key not in keys:
                    # The work item is gone (worktree removed): nothing to return.
                    del table[key]
        if alive and self._wakes:
            self._wake()

    def _lapse(self, key: str, reason: str, now: float,
               table: dict[str, Claim] | None = None) -> None:
        claim = (self.active if table is None else table).pop(key)
        self._wakes = [w for w in self._wakes if w.key != key]
        self.returned[key] = Returned(claim, reason, now)
        back = self.returned[key]
        self._engine.overlay.add(key, "delegate", "engine", f"back to you: {back.state}")
        self._engine.events.emit(
            CLAIM_LAPSED, {"key": key, "reason": reason, "claim": claim.to_dict()}
        )
        self._engine.status.mark_dirty()

    def _orchestrator_idle(self) -> bool:
        states = self._engine.agent_states(ORCHESTRATOR_KEY)
        statuses = {s.status.value for s in states.values()}
        return bool(statuses) and statuses <= _IDLE

    def _wake(self) -> None:
        """Send the waiting lines once the orchestrator's turn is over:
        never in the middle of one."""
        if self._waking or not self._orchestrator_idle():
            return
        wakes, self._wakes = self._wakes, []
        agent_ids = self._engine.agent_ids(ORCHESTRATOR_KEY)
        if not agent_ids:
            return
        text = "\n".join(w.text for w in wakes)
        self._waking = True

        async def send() -> None:
            try:
                await self._engine.send_agent_input(ORCHESTRATOR_KEY, agent_ids[0], text)
            except (ValueError, OSError) as e:
                log.warning("could not wake the orchestrator: %s", e)
            finally:
                self._waking = False

        try:
            self._engine._spawn(send())
        except RuntimeError:  # no loop (a sync test): try on the next pass
            self._waking = False
            self._wakes = wakes + self._wakes

    # --- What comes back to the user ---

    def items(self, snap: Snapshot) -> Iterable[Item]:
        for key, back in self.returned.items():
            path = back.claim.worktree_path
            yield Item(
                f"Delegation:{path or key}",
                DELEGATION.name,
                back.state,
                label="Orchestrator",
                detail=back.detail,
                work_item=key,
                worktree_path=path,
            )

    def _dismissed(self, item: Item) -> None:
        if item.kind == DELEGATION.name and item.work_item is not None:
            self.returned.pop(item.work_item, None)
