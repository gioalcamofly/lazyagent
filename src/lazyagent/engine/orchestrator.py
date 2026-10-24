"""The orchestrator as first responder (proposal 0013 "The orchestrator",
docs/design/inbox-plan.md §5c, I4).

The engine owns state; the orchestrator is a delegate that acts on one item
at a time inside limits the engine enforces here. With no orchestrator
running, everything goes to the user, as before.

- **Routing.** A worker's brief (``request_decision``) or plain question (an
  agent in ``waiting_for_user``) goes to the orchestrator first when one is
  alive, the item's mandate is not ``none`` (the user's "keep for me"), the
  answer is not ``reversible: no`` (the mandate says escalate in both
  columns, so the user gets it at once), and the caps are not reached. The
  brief is then held by the orchestrator (``holder: "orchestrator"``): it
  leaves the user's inbox, the row reads "orchestrator · looking", and the
  orchestrator is woken through the same queue as review replies (sent
  when its turn is over, never mid-turn) with one fixed line. Nothing
  wakes it on a timer. A tool-permission approval is never routed.
- **Resolution.** ``decision_answer`` (checked against the mandate and the
  caps) or ``decision_escalate`` (to the user's zone by class, with an
  optional suggestion). After ``[orchestrator] escalate_after_min`` (10)
  the engine escalates on its own ("orchestrator did not answer"); at once
  when the orchestrator exits or the user keeps the item.
- **Presence and mandate.** ``present`` while a client reported user input
  in the last ``away_after_min`` (15) minutes, or the user's override; no
  UI connected is ``away``. The allowlist per presence is
  :data:`AWAY_ACTIONS` / :data:`PRESENT_ACTIONS`; ``[orchestrator]
  away_allow`` and ``allow_outward`` only shorten it. :data:`NEVER_ALONE`
  is fixed here. A claim (Pass to orchestrator) carries the away list it
  was made with. The engine enforces only its own methods (answers,
  spawns, worktree removal); direct tracker or ``gh`` calls bypass it.
- **Log and revert.** Every orchestrator answer or action adds a timeline
  entry ``{actor: orchestrator, mandate, action, reason, inverse?}`` and a
  line in the worker's notes (Decisions). ``decision_revert`` (the user's)
  sends the worker the override and runs the recorded inverses: stop an
  agent the orchestrator spawned for the item; reset the branch to the
  commit it had when the answer was given, only while those commits are on
  no remote, the worktree has no running agent and ``git reset --keep``
  can keep local changes (otherwise it is recorded as skipped).
- **Caps.** ``max_decisions_per_item_day`` (3) and ``max_decisions_day``
  (20): past either, items go to the user (To sit down with) for the rest
  of the (local) day. ``auto_actions`` and ``auto_reverted`` are counted
  per day and shown in ``status_list`` counts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lazyagent.engine.overlay import one_line
from lazyagent.engine.registry import ORCHESTRATOR_KEY

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

DECISION_ANSWERED = "decision.answered"

# §5c's mandate table, by action. Present: what the orchestrator does on
# its own while the user is around; the rest of AWAY_ACTIONS it proposes
# (decision_escalate with a suggestion).
AWAY_ACTIONS = (
    "answer_quick",  # reversible: yes, class quick
    "answer_reversible",  # reversible: yes, class deep
    "answer_partly",  # reversible: partly (or not given)
    "relay",
    "restore_owner",
    "ci_rerun",
    "rebase_via_owner",
    "spawn_worker",
    "next_project_task",
)
PRESENT_ACTIONS = ("answer_quick", "relay", "restore_owner", "ci_rerun")
# Outward but routine, as its instructions say (best effort: the engine
# cannot see these calls).
OUTWARD_KINDS = ("tracker", "github_comment", "pr_create")
# Never alone, whatever the config says.
NEVER_ALONE = (
    "merge_default_branch",
    "publish",
    "force_push_shared",
    "delete_unpushed",
    "close_reopen_pr",
)
MODES = ("auto", "present", "away")

REASON_CHARS = 300
COMMENT_CHARS = 500
NOTE_CHARS = 200
CHOICE_CHARS = 200

LOOKING = "orchestrator · looking"
DID_NOT_ANSWER = "orchestrator did not answer"
GONE = "orchestrator gone"
KEPT = "kept for you"


def wake_text(record: dict[str, Any]) -> str:
    kind = "question" if record.get("source") == "question" else "decision"
    return f"Item {record['key']}: {kind} {record['id']}. Read it with status_list / decision_get."


def answer_action(record: dict[str, Any]) -> str | None:
    """The mandate row an answer to ``record`` falls in; None: escalate
    (``reversible: no``). Not given counts as ``partly``."""
    reversible = record["brief"].get("reversible")
    if reversible == "no":
        return None
    if reversible == "yes":
        return "answer_quick" if record["class"] == "quick" else "answer_reversible"
    return "answer_partly"


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


class Presence:
    """Whether the user is around: ``auto`` follows the clients' input
    reports, ``present`` / ``away`` is the user's override; no UI
    connected is always away."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.mode = "auto"
        self.input_at: float | None = None
        self._effective: str | None = None
        self.since: float | None = None
        # Tests: whether a UI is connected (else the engine's servers say).
        self.connected: Callable[[], bool] | None = None

    def _connected(self) -> bool:
        if self.connected is not None:
            return self.connected()
        return self._engine.ui_connected()

    def report(self, input_at: Any, now: float) -> None:
        if isinstance(input_at, bool) or not isinstance(input_at, (int, float)):
            raise ValueError("input_at must be a Unix time (seconds)")
        at = min(float(input_at), now)
        if self.input_at is None or at > self.input_at:
            self.input_at = at

    def set(self, mode: Any) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.mode = mode

    def effective(self, now: float) -> str:
        if not self._connected():
            effective = "away"
        elif self.mode != "auto":
            effective = self.mode
        else:
            window = self._engine.config.orchestrator.away_after_min * 60
            recent = self.input_at is not None and now - self.input_at < window
            effective = "present" if recent else "away"
        if effective != self._effective:
            self._effective = effective
            self.since = now
        return effective

    def to_dict(self, now: float) -> dict[str, Any]:
        effective = self.effective(now)
        return {"mode": self.mode, "effective": effective, "since": self.since}


@dataclass
class _Letter:
    slot: tuple[str, str]
    text: str
    tag: str | None = None
    on_sent: Callable[[], None] | None = None
    on_gone: Callable[[], None] | None = None


class Mailbox:
    """Lines for agents, sent once the agent's turn is over (never in the
    middle of one): the review-reply delivery path."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._letters: list[_Letter] = []
        self._sending: set[tuple[str, str]] = set()

    def post(self, slot: tuple[str, str], text: str, **kw: Any) -> None:
        self._letters.append(_Letter(slot, text, **kw))

    def drop(self, tag: str) -> None:
        self._letters = [x for x in self._letters if x.tag != tag]

    def has(self, tag: str) -> bool:
        return any(x.tag == tag for x in self._letters)

    def _idle(self, slot: tuple[str, str]) -> bool | None:
        """True idle, False busy, None gone."""
        from lazyagent.engine.status import _IDLE

        path, agent_id = slot
        if agent_id not in self._engine.agent_ids(path):
            return None
        state = self._engine.agent_states(path).get(agent_id)
        if state is None:
            return False
        idle = set(_IDLE)
        if path != ORCHESTRATOR_KEY:
            # A worker at a question waits for exactly this.
            idle.add("waiting_for_user")
        return state.status.value in idle

    def flush(self) -> None:
        slots: dict[tuple[str, str], list[_Letter]] = {}
        for letter in self._letters:
            slots.setdefault(letter.slot, []).append(letter)
        for slot, letters in slots.items():
            if slot in self._sending:
                continue
            idle = self._idle(slot)
            if idle is None:
                self._letters = [x for x in self._letters if x.slot != slot]
                for letter in letters:
                    if letter.on_gone is not None:
                        letter.on_gone()
                continue
            if not idle:
                continue
            self._send(slot, letters)

    def _send(self, slot: tuple[str, str], letters: list[_Letter]) -> None:
        self._letters = [x for x in self._letters if x.slot != slot]
        self._sending.add(slot)
        text = "\n".join(x.text for x in letters)
        engine = self._engine

        async def send() -> None:
            try:
                await engine.send_agent_input(slot[0], slot[1], text)
            except (ValueError, OSError) as e:
                log.warning("could not send to %s/%s: %s", slot[0], slot[1] or "orchestrator", e)
                self._letters = letters + self._letters
            else:
                for letter in letters:
                    if letter.on_sent is not None:
                        letter.on_sent()
            finally:
                self._sending.discard(slot)

        try:
            engine._spawn(send())
        except RuntimeError:  # no loop (a sync test): on the next pass
            self._sending.discard(slot)
            self._letters = letters + self._letters


class FirstResponder:
    """The engine's first-responder rules; see the module docstring."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.presence = Presence(engine)
        self.mailbox = Mailbox(engine)

    @property
    def clock(self) -> Callable[[], float]:
        return self._engine.status.clock

    @property
    def _config(self) -> Any:
        return self._engine.config.orchestrator

    def alive(self) -> bool:
        return self._engine.has_agent(ORCHESTRATOR_KEY)

    # --- Mandate ---

    def away_allowed(self) -> tuple[str, ...]:
        allow = self._config.away_allow
        return AWAY_ACTIONS if allow is None else tuple(a for a in AWAY_ACTIONS if a in allow)

    def mandate(self, key: str, now: float | None = None) -> str:
        """``none`` (kept for the user), ``claim`` (passed to the
        orchestrator), else the presence: ``present`` or ``away``."""
        if key in self._engine.overlay.kept:
            return "none"
        if self._engine.claims.get(key) is not None:
            return "claim"
        return self.presence.effective(self.clock() if now is None else now)

    def verdict(self, action: str, key: str, now: float | None = None) -> tuple[str, str]:
        """``(allow | propose | refuse, mandate)`` for ``action`` on ``key``."""
        mandate = self.mandate(key, now)
        if action in NEVER_ALONE or mandate == "none":
            return "refuse", mandate
        if action in OUTWARD_KINDS:
            return ("allow" if action in self._config.allow_outward else "refuse"), mandate
        if mandate == "claim":
            claim = self._engine.claims.get(key) or {}
            return ("allow" if action in claim.get("allowed", ()) else "propose"), mandate
        if action not in self.away_allowed():
            return "propose", mandate
        if mandate == "away" or action in PRESENT_ACTIONS:
            return "allow", mandate
        return "propose", mandate

    # --- Today's counts ---

    def _today(self, now: float) -> dict[str, Any]:
        overlay = self._engine.overlay
        counts = overlay.orchestrator
        if counts.get("day") != _day(now):
            counts.clear()
            counts.update(day=_day(now), decisions=0, auto_actions=0, auto_reverted=0, items={})
            overlay.changed()
        return counts

    def counts(self, now: float) -> dict[str, int]:
        today = self._today(now)
        return {"auto_actions": today["auto_actions"], "auto_reverted": today["auto_reverted"]}

    def capped(self, key: str, now: float) -> str | None:
        """Why the orchestrator may decide nothing more on ``key`` today."""
        today = self._today(now)
        per_item = self._config.max_decisions_per_item_day
        per_day = self._config.max_decisions_day
        if per_item and today["items"].get(key, 0) >= per_item:
            return f"{per_item} orchestrator decisions on this item today"
        if per_day and today["decisions"] >= per_day:
            return f"{per_day} orchestrator decisions today"
        return None

    def _count(self, now: float, *, key: str | None = None, decision: bool = False,
               reverted: bool = False) -> None:
        today = self._today(now)
        if reverted:
            today["auto_reverted"] += 1
        else:
            today["auto_actions"] += 1
        if decision and key is not None:
            today["decisions"] += 1
            today["items"][key] = today["items"].get(key, 0) + 1
        self._engine.overlay.changed()

    # --- Routing ---

    def route(self, record: dict[str, Any], now: float) -> None:
        """Called for a new brief before it is announced: hold it for the
        orchestrator when the rules allow, else leave it to the user (a
        capped one in To sit down with)."""
        key = record["key"]
        if not self.alive() or self.mandate(key, now) == "none":
            return
        if answer_action(record) is None:
            return
        capped = self.capped(key, now)
        if capped is not None:
            record["class"] = "deep"
            record["capped"] = capped
            return
        record["holder"] = "orchestrator"
        record["routed"] = {"at": now, "mandate": self.mandate(key, now)}
        self.mailbox.post((ORCHESTRATOR_KEY, ""), wake_text(record), tag=f"wake:{record['id']}")

    def held(self, key: str) -> bool:
        return any(
            r["key"] == key and r["state"] == "open" and r.get("holder") == "orchestrator"
            for r in self._engine.overlay.decisions.values()
        )

    def check(self, now: float) -> None:
        """On each status pass: route agents' plain questions, close the
        ones answered elsewhere, escalate what waited too long, send what
        waits to be sent."""
        engine = self._engine
        decisions = engine.decisions
        alive = self.alive()
        waiting: dict[tuple[str, str], str] = {}
        for wt in engine.worktrees:
            for agent_id, state in engine.agent_states(wt.path).items():
                if state.status.value == "waiting_for_user":
                    waiting[(wt.path, agent_id)] = state.detail
        records = list(engine.overlay.decisions.values())
        for record in records:
            slot = (record["worktree_path"], record["agent"])
            if record.get("source") == "question" and record["state"] == "open" \
                    and slot not in waiting:
                decisions.close(record, "resolved", now, "engine", "answered in the terminal")
        covered = {
            (r["worktree_path"], r["agent"])
            for r in records
            if r["state"] == "open"
            or (r.get("source") == "question" and r["state"] == "answered"
                and not (r.get("answer") or {}).get("delivered_at"))
        }
        if alive:
            for slot, detail in waiting.items():
                if slot in covered:
                    continue
                key = engine.status.key_of(slot[0])
                if key is None or self.mandate(key, now) == "none" or self.capped(key, now):
                    continue
                decisions.question(
                    key=key, worktree_path=slot[0], agent_id=slot[1], detail=detail, now=now
                )
        window = self._config.escalate_after_min * 60
        for record in engine.overlay.decisions.values():
            if record["state"] != "open" or record.get("holder") != "orchestrator":
                continue
            if not alive:
                self._escalate(record, now, "engine", GONE)
            elif self.mandate(record["key"], now) == "none":
                self._escalate(record, now, "engine", KEPT)
            elif now - record["routed"]["at"] >= window:
                self._escalate(record, now, "engine", DID_NOT_ANSWER)
        self._redeliver()
        engine.explainer.prune(now)
        for record in engine.overlay.decisions.values():
            tag = f"wake:{record['id']}"
            if (record["state"] != "open" or record.get("holder") != "orchestrator") \
                    and self.mailbox.has(tag):
                self.mailbox.drop(tag)
        self.mailbox.flush()

    # --- Escalation ---

    def _escalate(
        self, record: dict[str, Any], now: float, actor: str, note: str,
        suggestion: str | None = None,
    ) -> None:
        record["holder"] = "user"
        record["escalation"] = {"by": actor, "note": note, "suggestion": suggestion, "at": now}
        self.mailbox.drop(f"wake:{record['id']}")
        text = f"escalated to you: {note}"
        if suggestion:
            text += f" (suggests {suggestion})"
        self._engine.overlay.add(
            record["key"], "decision", actor, text, ref=record["id"],
            mandate=(record.get("routed") or {}).get("mandate") if actor == "orchestrator" else None,
            action="escalate" if actor == "orchestrator" else None,
        )
        self._engine.decisions.changed()

    def escalate(self, decision_id: str, note: Any, suggestion: Any, now: float) -> dict[str, Any]:
        """The orchestrator hands a brief it holds to the user."""
        record = self._held(decision_id)
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        note = one_line(note, NOTE_CHARS) or "escalated by the orchestrator"
        if suggestion is not None:
            suggestion = self._choice(record, suggestion)[0]
        self._escalate(record, now, "orchestrator", note, suggestion)
        self._engine.status.by(record["key"], "orchestrator")
        return dict(record)

    def _held(self, decision_id: str) -> dict[str, Any]:
        record = self._engine.overlay.decisions.get(decision_id)
        if record is None:
            raise ValueError(f"No decision {decision_id!r}")
        if record["state"] != "open":
            raise ValueError(f"Decision {decision_id} is {record['state']}")
        if record.get("holder") != "orchestrator":
            raise ValueError(f"Decision {decision_id} is the user's now, not yours")
        return record

    # --- Answers ---

    @staticmethod
    def _choice(record: dict[str, Any], choice: Any) -> tuple[str, str | None]:
        """``(choice, its label)``: an option's key, or free text when the
        brief has no options (a plain question)."""
        if not isinstance(choice, str) or not choice.strip():
            raise ValueError("choice is required: an option's key (or, with no options, the answer)")
        options = record["brief"]["options"]
        if not options:
            return one_line(choice, CHOICE_CHARS), None
        for option in options:
            if option["key"].casefold() == choice.strip().casefold():
                return option["key"], option["label"]
        raise ValueError(
            f"choice must be one of {', '.join(o['key'] for o in options)}"
        )

    async def answer(
        self,
        decision_id: str,
        choice: Any,
        *,
        comment: Any = None,
        reason: Any = None,
        by: str,
        now: float,
    ) -> dict[str, Any]:
        """``decision_answer``: by the orchestrator (a brief it holds,
        within the mandate and caps, with a reason) or by the user (any open
        brief)."""
        engine = self._engine
        if comment is not None and not isinstance(comment, str):
            raise ValueError("comment must be a string")
        comment = one_line(comment or "", COMMENT_CHARS)
        if by == "orchestrator":
            record = self._held(decision_id)
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("reason is required: why this choice (shown to the user)")
            reason = one_line(reason, REASON_CHARS)
            key = record["key"]
            action = answer_action(record)
            if action is None:
                raise ValueError(
                    f"Decision {decision_id} is not reversible: never yours to answer. "
                    "Call decision_escalate with your suggestion."
                )
            verdict, mandate = self.verdict(action, key, now)
            if verdict != "allow":
                why = "the user is present" if mandate == "present" else f"mandate {mandate}"
                raise ValueError(
                    f"Not within your mandate ({why}; {action.replace('_', ' ')}): call "
                    f"decision_escalate(id={decision_id!r}, note, suggestion) instead."
                )
            capped = self.capped(key, now)
            if capped is not None:
                raise ValueError(
                    f"Cap reached ({capped}): call decision_escalate; the rest of today's "
                    "items on it go to the user."
                )
            presence = "claim" if mandate == "claim" else mandate
        else:
            record = engine.overlay.decisions.get(decision_id)
            if record is None:
                raise ValueError(f"No decision {decision_id!r}")
            if record["state"] != "open":
                raise ValueError(f"Decision {decision_id} is {record['state']}")
            action, mandate, presence, reason = None, None, None, None
        choice, label = self._choice(record, choice)
        key = record["key"]
        answer: dict[str, Any] = {"choice": choice, "at": now}
        if label is not None:
            answer["label"] = label
        if comment:
            answer["comment"] = comment
        if reason:
            answer["reason"] = reason
        if presence is not None:
            answer["presence"] = presence
        record["state"] = "answered"
        record["closed_at"] = now
        record["answer"] = answer
        record["answered_by"] = by
        record["revertible"] = by == "orchestrator"
        self.mailbox.drop(f"wake:{decision_id}")
        inverse: list[dict[str, Any]] = []
        if by == "orchestrator":
            head = await engine.head_sha(record["worktree_path"])
            wt = engine.worktree(record["worktree_path"])
            if head:
                inverse.append({"kind": "git_reset", "sha": head,
                                "branch": wt.branch if wt is not None else None})
            record["inverse"] = inverse
            self._count(now, key=key, decision=True)
        # Delivered like a review reply: when the agent is idle.
        self._deliver(record)
        actor = by
        question = record["brief"]["question"]
        who = f"orchestrator, {presence}" if by == "orchestrator" else "user"
        self._notes(record, f"{_date(now)} · {question} → {choice} ({who}): "
                    f"{reason if by == 'orchestrator' else comment}".rstrip(": "), by)
        timeline = (
            f"decided by orchestrator ({presence}): {question} → {choice}"
            if by == "orchestrator" else f"answered: {question} → {choice}"
        )
        engine.overlay.add(
            key, "decision", actor, timeline, ref=decision_id,
            mandate=mandate, action=action, reason=reason,
            inverse=inverse or None if by == "orchestrator" else None,
        )
        engine.status.by(key, actor)
        engine.events.emit(DECISION_ANSWERED, {"decision": dict(record)})
        engine.decisions.changed()
        self.mailbox.flush()
        return dict(record)

    def _deliver(self, record: dict[str, Any]) -> None:
        """Post the answer's line for the agent, sent once its turn is over.
        An agent gone by then gets it when it is back (restored)."""
        engine = self._engine
        answer = record["answer"]

        def sent() -> None:
            answer["delivered_at"] = self.clock()
            answer.pop("delivery", None)
            if record.get("source") == "question":
                engine.status.mark_dirty()
            engine.overlay.changed()

        def gone() -> None:
            answer["delivery"] = "agent gone"
            engine.overlay.changed()

        self.mailbox.post((record["worktree_path"], record["agent"]), answer_text(record),
                          tag=f"answer:{record['id']}", on_sent=sent, on_gone=gone)

    def _redeliver(self) -> None:
        """Answers that found their agent gone: posted again once an agent
        with that id runs there (a restore). Its notes already have them."""
        engine = self._engine
        for record in engine.overlay.decisions.values():
            answer = record.get("answer") or {}
            if record["state"] != "answered" or answer.get("delivered_at") \
                    or answer.get("delivery") != "agent gone":
                continue
            if self.mailbox.has(f"answer:{record['id']}"):
                continue
            session = engine.get_session(record["worktree_path"], record["agent"])
            if session is not None and session.running:
                answer["delivery"] = "after restore"
                self._deliver(record)

    def _notes_id(self, record: dict[str, Any]) -> str | None:
        """The worker's notes: its live ones, else its restorable entry's
        (they come back with it)."""
        engine = self._engine
        notes_id = engine.notes_id(record["worktree_path"], record["agent"])
        if notes_id is not None or engine.manifest is None:
            return notes_id
        for entry in engine.manifest.restorable.values():
            if (entry.worktree_path, entry.agent_id) == (record["worktree_path"], record["agent"]):
                return entry.notes_id
        return None

    def _notes(self, record: dict[str, Any], line: str, by: str) -> None:
        """Append to the worker's notes, section Decisions (append only)."""
        engine = self._engine
        notes_id = self._notes_id(record)
        if engine.notes is None or notes_id is None or notes_id not in engine.notes:
            return
        try:
            engine.notes.append(notes_id, "Decisions", line, by)
        except Exception as e:  # notes are a convenience: never fail the answer
            log.warning("could not log decision %s in notes: %s", record["id"], e)

    # --- Actions on held items ---

    def holds(self, key: str) -> bool:
        """The orchestrator acts on ``key`` as a delegate: it holds a brief
        there, or the item was passed to it."""
        return self.held(key) or self._engine.claims.get(key) is not None

    def check_action(self, action: str, key: str) -> str:
        """Raise unless the orchestrator may do ``action`` on ``key`` now;
        returns the mandate."""
        verdict, mandate = self.verdict(action, key)
        if verdict == "allow":
            return mandate
        if verdict == "propose":
            raise ValueError(
                f"Not within your mandate ({mandate}): propose "
                f"{action.replace('_', ' ')} to the user (decision_escalate, or "
                "orchestrator_release with a note) instead of doing it."
            )
        raise ValueError(f"{action.replace('_', ' ')} on {key} is never yours to do alone")

    def record_action(
        self, key: str, action: str, *, mandate: str, inverse: dict[str, Any] | None,
        text: str, reason: str | None = None,
    ) -> None:
        """An action the orchestrator took as a delegate: on the timeline,
        counted, and its inverse attached to its latest revertible answer on
        the item (Revert runs it)."""
        now = self.clock()
        engine = self._engine
        if inverse is not None:
            latest = None
            for record in engine.overlay.decisions.values():
                if record["key"] == key and record.get("revertible"):
                    if latest is None or record["answer"]["at"] > latest["answer"]["at"]:
                        latest = record
            if latest is not None:
                latest.setdefault("inverse", []).append(inverse)
        engine.overlay.add(key, "action", "orchestrator", text, mandate=mandate,
                           action=action, reason=reason, inverse=inverse)
        self._count(now)
        engine.decisions.changed()

    # --- Revert ---

    async def revert(self, decision_id: str, choice: Any, comment: Any, now: float) -> dict[str, Any]:
        """The user overrides an orchestrator answer: the worker is told,
        and the recorded inverses run where safe."""
        engine = self._engine
        record = engine.overlay.decisions.get(decision_id)
        if record is None:
            raise ValueError(f"No decision {decision_id!r}")
        if record.get("answered_by") != "orchestrator":
            raise ValueError(f"Decision {decision_id} was not answered by the orchestrator")
        if not record.get("revertible"):
            raise ValueError(f"Decision {decision_id} was already reverted")
        if comment is not None and not isinstance(comment, str):
            raise ValueError("comment must be a string")
        comment = one_line(comment or "", COMMENT_CHARS)
        label = None
        if choice is not None:
            choice, label = self._choice(record, choice)
        results = []
        inverses = record.get("inverse") or []
        for inverse in [i for i in inverses if i["kind"] == "stop_agent"]:
            results.append(self._stop(inverse))
        for inverse in [i for i in inverses if i["kind"] == "git_reset"]:
            results.append(await self._reset(record, inverse))
        text = f"Decision {decision_id} overridden by the user"
        if choice is not None:
            text += f": {choice}" + (f" — {label}" if label else "")
        text += "." + (f" {comment}" if comment else "")
        self.mailbox.post((record["worktree_path"], record["agent"]), text,
                          tag=f"revert:{decision_id}")
        record["revertible"] = False
        record["reverted"] = {"at": now, "choice": choice, "comment": comment, "results": results}
        self._count(now, reverted=True)
        question = record["brief"]["question"]
        self._notes(record, f"{_date(now)} · {question} → {choice or 'reverted'} "
                    f"(user, revert): {comment}".rstrip(": "), "user")
        engine.overlay.add(
            record["key"], "decision", "user",
            f"reverted orchestrator's {record['answer']['choice']}"
            + (f" → {choice}" if choice else ""),
            ref=decision_id, action="revert",
        )
        engine.status.by(record["key"], "user")
        engine.events.emit(DECISION_ANSWERED, {"decision": dict(record)})
        engine.decisions.changed()
        self.mailbox.flush()
        return dict(record)

    def _stop(self, inverse: dict[str, Any]) -> str:
        path, agent_id = inverse["worktree_path"], inverse["agent_id"]
        if agent_id not in self._engine.agent_ids(path):
            return f"agent {agent_id} already gone"
        try:
            self._engine.stop_agent(path, agent_id, notes="keep")
        except ValueError as e:
            return f"could not stop agent {agent_id}: {e}"
        return f"stopped agent {agent_id}"

    async def _reset(self, record: dict[str, Any], inverse: dict[str, Any]) -> str:
        """``git reset --keep`` to the pre-answer commit, only when safe."""
        from lazyagent.engine.status import _RUNNING

        engine = self._engine
        path, sha = record["worktree_path"], inverse["sha"]
        wt = engine.worktree(path)
        if wt is None:
            return "reset skipped: the worktree is gone"
        if inverse.get("branch") and wt.branch != inverse["branch"]:
            return "reset skipped: the worktree is on another branch"
        if any(s.status.value in _RUNNING for s in engine.agent_states(path).values()):
            return "reset skipped: an agent is working there (it was told)"
        head = await _git(path, "rev-parse", "HEAD")
        if head is None:
            return "reset skipped: no HEAD"
        if head == sha:
            return "nothing to reset: no commit since the answer"
        if await _git(path, "merge-base", "--is-ancestor", sha, "HEAD") is None:
            return "reset skipped: the answer's commit is not under HEAD"
        total = await _git(path, "rev-list", "--count", f"{sha}..HEAD")
        local = await _git(path, "rev-list", "--count", f"{sha}..HEAD", "--not", "--remotes")
        if total is None or local is None or total != local:
            return "reset skipped: commits since the answer were pushed"
        if await _git(path, "reset", "--keep", sha) is None:
            return "reset skipped: local changes would be lost"
        return f"reset to {sha[:7]} (was {head[:7]})"

    # --- Never alone (the engine's own methods) ---

    async def check_remove(self, path: str) -> None:
        """The orchestrator may not delete a worktree with unpushed work."""
        why = await unpushed(path)
        if why is not None:
            raise ValueError(
                f"Never alone: deleting a worktree with unpushed work ({why}). Ask the user."
            )


def answer_text(record: dict[str, Any]) -> str:
    """``Decision <id>: <choice> — <label>. <comment>``."""
    answer = record["answer"]
    text = f"Decision {record['id']}: {answer['choice']}"
    if answer.get("label"):
        text += f" — {answer['label']}"
    return text + "." + (f" {answer['comment']}" if answer.get("comment") else "")


def _date(now: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(now))


async def _git(path: str, *args: str) -> str | None:
    """Run git in ``path``: its stdout stripped, or None when it failed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10.0)
        except asyncio.TimeoutError:
            proc.kill()
            return None
    except OSError:
        return None
    return out.decode().strip() if proc.returncode == 0 else None


async def unpushed(path: str) -> str | None:
    """Why ``path`` holds work that is on no remote, or None."""
    status = await _git(path, "status", "--porcelain")
    if status is None:
        return "git status failed"
    if status:
        return "uncommitted changes"
    count = await _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if count is None:
        return "git rev-list failed"
    if count != "0":
        return f"{count} commits on no remote"
    return None
