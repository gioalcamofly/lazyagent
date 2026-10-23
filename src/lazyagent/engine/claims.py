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

Claims live in the engine's memory only: an engine restart ends them, and
the rows fall back to their own derivation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lazyagent.engine.actionables import Item, Kind, Snapshot, register_kind
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.status import _IDLE, clip_words

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

CLAIM_CREATED = "orchestrator.claim"
CLAIM_RELEASED = "orchestrator.released"
CLAIM_LAPSED = "orchestrator.lapsed"

HOLDER = "orchestrator"
MAX_TTL = 86400.0
MIN_TTL = 60.0
# orchestrator_release: done (nothing for the user), returned (back to the
# user's Round with the note).
OUTCOMES = ("done", "returned")
# What a claim allows (§5c, the away column: the user handed it over). The
# engine checks only its own methods; the never-alone list stays out.
ALLOWED = (
    "answer_reversible",
    "relay",
    "restore_owner",
    "ci_rerun",
    "rebase_via_owner",
    "spawn_worker",
    "mark",
)
NEVER_ALONE = (
    "merge_default_branch",
    "publish",
    "force_push_shared",
    "delete_unpushed",
    "close_reopen_pr",
)
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

    def to_dict(self) -> dict[str, Any]:
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
            return clip_words(f"returned: {self.note}" if self.note else "returned by orchestrator")
        return "delegation lapsed"

    @property
    def detail(self) -> str:
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
        self.returned: dict[str, Returned] = {}
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
        claim = Claim(key, row["worktree_path"], now, now + float(ttl_s), by)
        self.active[key] = claim
        self.returned.pop(key, None)
        self._engine.events.emit(CLAIM_CREATED, {"claim": claim.to_dict()})
        self._wakes = [w for w in self._wakes if w.key != key]
        self._wakes.append(_Wake(key, wake_text(claim), now))
        self._changed()
        return claim

    def clear(self, key: str) -> Claim:
        """The user takes the item back: the claim ends, nothing is raised."""
        claim = self.active.pop(key, None)
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
        """The orchestrator ends its claim: ``done``, or ``returned`` to the
        user's Round with ``note``."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        claim = self.active.pop(key, None)
        if claim is None:
            raise ValueError(f"{key} is not with the orchestrator (no claim, or it lapsed)")
        note = " ".join(note.split())[:NOTE_CHARS]
        self._wakes = [w for w in self._wakes if w.key != key]
        if outcome == "returned":
            self.returned[key] = Returned(claim, "returned", self.clock(), note)
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
        if not self.active:
            self._wakes.clear()
            return
        alive = self.orchestrator_alive()
        keys = set(self._engine.status.keys())
        for key, claim in list(self.active.items()):
            if not alive:
                self._lapse(key, "orchestrator_gone", now)
            elif now >= claim.until:
                self._lapse(key, "expired", now)
            elif keys and key not in keys:
                # The work item is gone (worktree removed): nothing to return.
                del self.active[key]
        if alive and self._wakes:
            self._wake()

    def _lapse(self, key: str, reason: str, now: float) -> None:
        claim = self.active.pop(key)
        self._wakes = [w for w in self._wakes if w.key != key]
        self.returned[key] = Returned(claim, reason, now)
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
