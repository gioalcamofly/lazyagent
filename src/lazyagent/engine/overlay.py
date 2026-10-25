"""What the work-item rows cannot derive (proposal 0013,
docs/design/inbox-plan.md §2 "Overlay", §3, §5b "Since you looked", I4).

The rows themselves stay a projection (status.py); this store keeps only:

- **gestures**, the user's, one per work item: ``label`` (a ball and a short
  text, e.g. "others: QA"), ``park`` until a time or an event, ``close``
  (done / dropped, for what the engine cannot see). A label and a close
  expire when the item's facts cross a rule boundary (:func:`basis`: the
  PR appears, is merged or closed, its review decision changes, or CI turns
  red anew); a conflict or a passing CI run does not end them. A park ends
  like an inbox park (actionables.py).
- **reports**, an agent's last ``status_report`` on its own work item: a
  note, and optionally a ball and ``needs_user``. The ball and
  ``needs_user`` clear once the agent has ended a turn and runs again, when
  it is sent input, or when it is gone; the note stays as the row's note.
  The ball also lapses at the report's ``until`` (at most
  :data:`MAX_REPORT_FOR`; ball ``agent``, background work, defaults to
  :data:`REPORT_FOR`, the others to none). While it holds, the agent's
  finished turn is not "done": it said it works on in the background.
- **closed**, 24 h tombstones: the item became done (merged, PR closed,
  closed by the user) or its worktree went away. A removed item stays a
  ``done`` row for 24 h; then its timeline, gestures and seen marks go.
- **timeline**, per work item, a ring of facts (:data:`RING`, :data:`KEEP`):
  ``commit``, ``ci``, ``ball``, ``state``, ``notes``, ``decision``,
  ``review``, ``outward``; each ``{at, kind, actor, text, ref?}`` with
  ``actor`` one of ``user``, ``agent:<id>``, ``orchestrator``, ``engine``.
  Nothing in it is generated text: each entry is a fact and its time.
- **seen**, per client: the last look at everything (``*``) and per item.
- **decisions**, the briefs (engine/decisions.py keeps their rules).
- **kept**, the user's "keep for me" per work item: the orchestrator's
  mandate there is none (engine/orchestrator.py).
- **orchestrator**, today's counts of its own decisions and actions (the
  caps on autonomy, ``auto_actions`` / ``auto_reverted``).
- **released**, the orchestrator's last release of a pass or a hold on each
  item (``{outcome, note, at}``): the row's note for :data:`RELEASED_FOR`.
- **spawned**, who spawned each agent of an item (the orchestrator's
  workers, whose finished work it holds first: claims.py).
- **delegations**, inbox items the user delegated to the branch's agent
  (delegation.py).
- **orchestrator_report**, the orchestrator's own last ``status_report``
  (``{note, at}``): what it says it is doing, tied to no work item.

Kept in ``$XDG_STATE_HOME/lazyagent/status/<repo hash>.json`` (0600, atomic)
by the engine that owns the repo, written at most once per :data:`SAVE_EVERY`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent.engine.notes import _atomic_write
from lazyagent.engine.runtime import repo_digest, state_dir

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

STATUS_TIMELINE = "status.timeline"

VERSION = 1
SAVE_EVERY = 1.0
# The timeline ring, per work item.
RING = 50
KEEP = 7 * 86400.0
# Closed items: a removed one is a done row this long, then forgotten.
TOMBSTONE = 86400.0
# Closed briefs are kept this long.
KEEP_DECISIONS = 30 * 86400.0
# The orchestrator's release note stays on its row this long.
RELEASED_FOR = 86400.0

GESTURE_KINDS = ("label", "park", "close")
LABEL_BALLS = ("agent", "others", "stalled")
REPORT_BALLS = ("agent", "others", "stalled")
# A reported ball "agent" (background work) lapses this long after the
# report, unless it says until; "others" / "stalled" have no default.
REPORT_FOR = 2 * 3600.0
MAX_REPORT_FOR = 8 * 3600.0
CLOSE_HOW = ("done", "dropped")
TIMELINE_KINDS = (
    "commit",
    "ci",
    "ball",
    "state",
    "notes",
    "decision",
    "review",
    "outward",
    "action",
    "delegate",
    # The user answered a tool approval from Home (approval_answer).
    "approval",
    # The user took back one gesture (status_gesture undo).
    "undo",
)
LABEL_CHARS = 40
NOTE_CHARS = 200
OUTWARD_KIND_CHARS = 32
OUTWARD_REF_CHARS = 300
TEXT_CHARS = 200
DEFAULT_CLIENT = "user"
CLIENT_CHARS = 64


def one_line(text: str, limit: int) -> str:
    """``text`` on one line, at most ``limit`` characters (``…`` when cut)."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# The engine's own entries a restart would add again: the facts it reads.
REPEATABLE = ("ci", "review", "ball", "state")


def _repeat(ring: list[dict[str, Any]], entry: dict[str, Any]) -> dict[str, Any] | None:
    """The entry ``entry`` repeats, if any: the same commit (by its ref),
    or the newest entry of its kind by the same actor saying the same (a
    CI red after a CI green is news; two CI reds in a row are not)."""
    kind = entry["kind"]
    if kind == "commit" and entry.get("ref"):
        return next((e for e in ring if e["kind"] == "commit" and e.get("ref") == entry["ref"]), None)
    if kind not in REPEATABLE:
        return None
    last = next((e for e in reversed(ring) if e["kind"] == kind), None)
    if last is not None and last["actor"] == entry["actor"] and last["text"] == entry["text"]:
        return last
    return None


def default_until(ball: str | None, at: float) -> float | None:
    """A report's until when it gives none: only background work lapses."""
    return at + REPORT_FOR if ball == "agent" else None


_DURATION = re.compile(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?")


def report_until(value: Any, now: float) -> float | None:
    """A report's ``until``: a Unix time, or a duration from now ("45m",
    "2h", "1h30m", "90s"); None (not given) is the default. At most
    :data:`MAX_REPORT_FOR` ahead."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("until must be a Unix time or a duration such as \"45m\"")
    if isinstance(value, (int, float)):
        until = float(value)
    elif isinstance(value, str):
        m = _DURATION.fullmatch(value.strip())
        if m is None or not any(m.groups()):
            raise ValueError(
                f"until {value!r}: a Unix time or a duration such as \"45m\", \"2h\", \"1h30m\""
            )
        h, mins, secs = (int(g or 0) for g in m.groups())
        until = now + h * 3600 + mins * 60 + secs
    else:
        raise ValueError("until must be a Unix time or a duration such as \"45m\"")
    if until <= now:
        raise ValueError("until is already past")
    if until - now > MAX_REPORT_FOR:
        raise ValueError(f"until is at most {MAX_REPORT_FOR / 3600:g} h ahead")
    return until


def actor_of(by: str, agent_id: str = "") -> str:
    """A timeline actor from a notes ``by`` / caller role."""
    if by == "agent":
        return f"agent:{agent_id}" if agent_id else "agent"
    if by in ("user", "orchestrator"):
        return by
    return "engine"


def basis(facts: Any) -> dict[str, Any]:
    """The facts whose change ends a label or a close: the PR (which one, its
    state, its review decision) and whether CI is red."""
    from lazyagent.engine.status import ci_state

    pr = facts.pr
    return {
        "pr": pr.number if pr is not None else None,
        "pr_state": pr.state if pr is not None else None,
        "review": (pr.review_decision or "") if pr is not None else "",
        "ci_red": ci_state(pr) == "red",
    }


def basis_holds(stored: dict[str, Any], now: dict[str, Any]) -> bool:
    """Whether a label / close made on ``stored`` still holds on ``now``;
    a red CI that cleared is forgotten, so the next red is new."""
    for name in ("pr", "pr_state", "review"):
        if stored.get(name) != now[name]:
            return False
    if now["ci_red"] and not stored.get("ci_red"):
        return False
    stored["ci_red"] = now["ci_red"]
    return True


def _place(facts: Any) -> Any:
    """A work item's facts as a park measures them (actionables.Place)."""
    from lazyagent.engine.actionables import AgentSeen, Place

    return Place(
        facts.worktree_path or "",
        facts.key,
        tuple(AgentSeen(a.agent_id, a.status) for a in facts.agents),
        facts.pr,
        getattr(facts, "head", None),
    )


def overlay_path(repo_root: str) -> Path:
    return state_dir() / "status" / f"{repo_digest(repo_root)}.json"


class Overlay:
    """The store; see the module docstring. The status projection reads and
    expires it on each pass; the methods write it."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self.version = 0
        self._path: Path | None = None
        self.gestures: dict[str, dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.closed: dict[str, dict[str, Any]] = {}
        self.seen: dict[str, dict[str, float]] = {}
        self.timeline: dict[str, list[dict[str, Any]]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.next_decision = 1
        self.kept: dict[str, dict[str, Any]] = {}
        self.orchestrator: dict[str, Any] = {}
        self.released: dict[str, dict[str, Any]] = {}
        self.spawned: dict[str, dict[str, Any]] = {}
        self.delegations: dict[str, dict[str, Any]] = {}
        self.orchestrator_report: dict[str, Any] | None = None
        # key → the agent's turn its report was made in (status.turn); in
        # memory only, so a loaded report falls back to ``turn_over``.
        self._report_turns: dict[str, int] = {}
        self._saved: str | None = None
        self._handle: asyncio.TimerHandle | None = None

    @property
    def clock(self) -> Callable[[], float]:
        status = getattr(self._engine, "status", None)
        return status.clock if status is not None else time.time

    # --- Store ---

    def open(self, path: Path | str) -> None:
        """Keep the overlay in ``path`` (load it now)."""
        self._path = Path(path)
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("could not read the status overlay from %s: %s", self._path, e)
            return
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return

        def table(name: str) -> dict[str, Any]:
            value = data.get(name)
            if not isinstance(value, dict):
                return {}
            return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}

        self.gestures = {
            k: g for k, g in table("gestures").items() if g.get("kind") in GESTURE_KINDS
        }
        self.reports = table("reports")
        self.closed = {
            k: c for k, c in table("closed").items() if isinstance(c.get("at"), (int, float))
        }
        self.seen = {
            client: {k: float(v) for k, v in marks.items() if isinstance(v, (int, float))}
            for client, marks in table("seen").items()
        }
        timeline = data.get("timeline")
        if isinstance(timeline, dict):
            self.timeline = {
                k: [e for e in v if isinstance(e, dict) and isinstance(e.get("at"), (int, float))]
                for k, v in timeline.items()
                if isinstance(v, list)
            }
        self.decisions = table("decisions")
        self.kept = table("kept")
        self.released = {
            k: r for k, r in table("released").items() if isinstance(r.get("at"), (int, float))
        }
        self.spawned = table("spawned")
        self.delegations = {
            k: d for k, d in table("delegations").items()
            if isinstance(d.get("agent"), str) and isinstance(d.get("item"), str)
        }
        orchestrator = data.get("orchestrator")
        if isinstance(orchestrator, dict):
            self.orchestrator = orchestrator
        report = data.get("orchestrator_report")
        if isinstance(report, dict) and isinstance(report.get("note"), str) \
                and isinstance(report.get("at"), (int, float)):
            self.orchestrator_report = {"note": report["note"], "at": report["at"]}
        next_decision = data.get("next_decision")
        if type(next_decision) is int and next_decision > 0:
            self.next_decision = next_decision
        self.prune(self.clock())
        self._saved = self._serialize()

    def _serialize(self) -> str:
        return json.dumps(
            {
                "version": VERSION,
                "gestures": self.gestures,
                "reports": self.reports,
                "closed": self.closed,
                "seen": self.seen,
                "timeline": self.timeline,
                "decisions": self.decisions,
                "next_decision": self.next_decision,
                "kept": self.kept,
                "orchestrator": self.orchestrator,
                "released": self.released,
                "spawned": self.spawned,
                "delegations": self.delegations,
                "orchestrator_report": self.orchestrator_report,
            },
            indent=1,
            sort_keys=True,
        )

    def changed(self) -> None:
        """Write within :data:`SAVE_EVERY` (now, with no event loop)."""
        # The status projection's cached rows end with any change.
        self.version += 1
        if self._path is None or self._handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save()
            return
        self._handle = loop.call_later(SAVE_EVERY, self.save)

    def save(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
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
            log.warning("could not write the status overlay to %s: %s", self._path, e)

    # --- Timeline ---

    def add(
        self,
        key: str,
        kind: str,
        actor: str,
        text: str,
        *,
        ref: str | None = None,
        at: float | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Add a fact to ``key``'s timeline (kept in time order, at most
        :data:`RING`, none older than :data:`KEEP`) and emit
        ``status.timeline {key, entry}``. ``extra``: the orchestrator's
        ``mandate``, ``action``, ``reason`` and ``inverse`` (None left out)."""
        now = self.clock()
        entry: dict[str, Any] = {
            "at": now if at is None else at,
            "kind": kind,
            "actor": actor,
            "text": one_line(text, TEXT_CHARS),
        }
        if ref:
            entry["ref"] = ref
        entry.update({k: v for k, v in extra.items() if v is not None})
        ring = self.timeline.setdefault(key, [])
        same = _repeat(ring, entry)
        if same is not None:
            # An engine restart reads the facts again (the timeline is
            # kept): the same commit, or a fact the item still has, is not
            # news.
            return same
        ring.append(entry)
        ring.sort(key=lambda e: e["at"])
        ring[:] = [e for e in ring if now - e["at"] < KEEP][-RING:]
        if self._engine is not None:
            self._engine.events.emit(STATUS_TIMELINE, {"key": key, "entry": entry})
        self.changed()
        return entry

    def entries(self, key: str, since: float | None = None) -> list[dict[str, Any]]:
        ring = self.timeline.get(key, [])
        return [dict(e) for e in ring if since is None or e["at"] > since]

    def last_event_at(self, key: str) -> float | None:
        ring = self.timeline.get(key)
        return ring[-1]["at"] if ring else None

    # --- Seen ---

    def mark_seen(self, client: str, key: str | None, at: float) -> dict[str, float]:
        marks = self.seen.setdefault(client, {})
        if key is None:
            # Mark all seen: the per-item marks before it say nothing more.
            marks.clear()
            marks["*"] = at
        else:
            marks[key] = at
        self.changed()
        return dict(marks)

    def seen_at(self, key: str, client: str = DEFAULT_CLIENT) -> float | None:
        marks = self.seen.get(client)
        if not marks:
            return None
        found = [t for t in (marks.get("*"), marks.get(key)) if t is not None]
        return max(found) if found else None

    # --- Closing ---

    def close(self, key: str, how: str, now: float, row: dict[str, Any] | None) -> bool:
        """Record that ``key`` closed (once); True when it is new."""
        if key in self.closed:
            return False
        tomb: dict[str, Any] = {"at": now, "how": how}
        if row is not None:
            tomb.update(
                title=row.get("title"), branch=row.get("branch"), urls=row.get("urls") or []
            )
        self.closed[key] = tomb
        self.changed()
        return True

    def reopen(self, key: str) -> None:
        if self.closed.pop(key, None) is not None:
            self.changed()

    def forget(self, key: str) -> None:
        """A tombstone ran out: drop what was kept for the item."""
        for table in (self.gestures, self.reports, self.closed, self.timeline, self.kept,
                      self.released, self.spawned, self.delegations):
            table.pop(key, None)
        for marks in self.seen.values():
            marks.pop(key, None)
        self.changed()

    # --- The orchestrator's ---

    def release(self, key: str, outcome: str, note: str, now: float) -> None:
        self.released[key] = {"outcome": outcome, "note": note, "at": now}
        self.changed()

    def released_note(self, key: str, now: float) -> dict[str, Any] | None:
        r = self.released.get(key)
        if r is None:
            return None
        if now - r["at"] >= RELEASED_FOR:
            del self.released[key]
            self.changed()
            return None
        return r

    def set_spawned(self, key: str, agent_id: str, by: str, now: float) -> None:
        self.spawned.setdefault(key, {})[agent_id] = {"by": by, "at": now}
        self.changed()

    def spawned_by(self, key: str) -> dict[str, str]:
        """agent id → who spawned it (only what was recorded)."""
        return {a: r["by"] for a, r in self.spawned.get(key, {}).items()
                if isinstance(r, dict) and isinstance(r.get("by"), str)}

    def prune(self, now: float) -> None:
        for key, ring in list(self.timeline.items()):
            ring[:] = [e for e in ring if now - e["at"] < KEEP][-RING:]
            if not ring:
                del self.timeline[key]
        for decision_id, record in list(self.decisions.items()):
            closed_at = record.get("closed_at")
            if isinstance(closed_at, (int, float)) and now - closed_at > KEEP_DECISIONS:
                del self.decisions[decision_id]

    # --- Gestures ---

    def gesture(self, facts: Any, now: float) -> dict[str, Any] | None:
        """``facts.key``'s gesture while it holds; one that no longer holds
        is dropped. A label or a close ends when :func:`basis` moves; a
        close also when a new agent starts there (work resumed); a park as
        an inbox park does (a time, or its event)."""
        from lazyagent.engine.actionables import park_holds

        key = facts.key
        g = self.gestures.get(key)
        if g is None:
            return None
        kind = g["kind"]
        holds = True
        if kind in ("label", "close"):
            holds = basis_holds(g.setdefault("basis", basis(facts)), basis(facts))
            if holds and kind == "close":
                from lazyagent.engine.status import _RUNNING

                before = set(g.get("agents", ()))
                holds = not any(
                    a.status in _RUNNING and a.agent_id not in before for a in facts.agents
                )
        elif kind == "park":
            holds = park_holds(g["park"], _place(facts), now)
        if not holds:
            del self.gestures[key]
            self.changed()
            return None
        return dict(g)

    def set_gesture(
        self,
        facts: Any,
        kind: str,
        *,
        now: float,
        ball: Any = None,
        text: Any = None,
        until: Any = None,
        how: Any = None,
        by: str = "user",
    ) -> dict[str, Any]:
        """Put a label, a park or a close on ``facts.key`` (replacing its
        gesture). Raises ValueError on a bad argument."""
        from lazyagent.engine.actionables import check_until, new_park

        g: dict[str, Any] = {"kind": kind, "at": now, "by": by}
        if kind == "label":
            if ball not in LABEL_BALLS:
                raise ValueError(f"ball must be one of {', '.join(LABEL_BALLS)}")
            if text is not None and not isinstance(text, str):
                raise ValueError("text must be a string")
            g.update(ball=ball, text=one_line(text or "", LABEL_CHARS), basis=basis(facts))
        elif kind == "park":
            until = check_until(until, now)
            if text is not None and not isinstance(text, str):
                raise ValueError("text must be a string")
            place = _place(facts)
            g.update(until=until, park=new_park(until, place, now, by))
            if text and text.strip():
                g["text"] = one_line(text, LABEL_CHARS)
        elif kind == "close":
            how = how or "done"
            if how not in CLOSE_HOW:
                raise ValueError(f"how must be one of {', '.join(CLOSE_HOW)}")
            g.update(how=how, basis=basis(facts), agents=sorted(a.agent_id for a in facts.agents))
        else:
            raise ValueError(f"kind must be one of {', '.join(GESTURE_KINDS)}")
        before = self.gestures.get(facts.key)
        if kind == "park" and before is not None:
            # Undoing the park brings back what it replaced (a label).
            g["replaced"] = before.get("replaced") if before["kind"] == "park" else before
        self.gestures[facts.key] = g
        self.changed()
        return g

    def undo_park(self, key: str) -> dict[str, Any]:
        """Take back the user's park on ``key``: the gesture it replaced, if
        any, comes back. Raises ValueError when the user did not park it."""
        g = self.gestures.get(key)
        if g is None or g["kind"] != "park" or g.get("by", "user") != "user":
            raise ValueError(f"{key} is not parked by you: nothing to undo")
        replaced = g.get("replaced")
        if isinstance(replaced, dict) and replaced.get("kind") in GESTURE_KINDS:
            self.gestures[key] = replaced
        else:
            del self.gestures[key]
        self.changed()
        return g

    def report_orchestrator(self, note: str, now: float) -> dict[str, Any]:
        """The orchestrator says what it is doing now (no work item)."""
        self.orchestrator_report = {"note": one_line(note, NOTE_CHARS), "at": now}
        self.changed()
        return dict(self.orchestrator_report)

    def clear_gesture(self, key: str) -> dict[str, Any] | None:
        g = self.gestures.pop(key, None)
        if g is not None:
            self.changed()
        return g

    # --- Agents' reports ---

    def report(
        self,
        key: str,
        worktree_path: str,
        agent_id: str,
        *,
        note: str,
        ball: str | None,
        needs_user: bool,
        label: str,
        now: float,
        until: float | None = None,
        turn: int | None = None,
    ) -> dict[str, Any]:
        """An agent's ``status_report`` on its own work item (replacing its
        previous one), made in its turn ``turn``."""
        r = {
            "agent": agent_id,
            "worktree_path": worktree_path,
            "label": label,
            "note": one_line(note, NOTE_CHARS),
            "ball": ball,
            "needs_user": needs_user,
            "at": now,
            # Its turn has ended since (then running again answers it).
            "turn_over": False,
            "until": default_until(ball, now) if until is None else until,
        }
        self.reports[key] = r
        if turn is None:
            self._report_turns.pop(key, None)
        else:
            self._report_turns[key] = turn
        self.changed()
        return r

    def working(self, key: str) -> dict[str, Any] | None:
        """``key``'s report while its ball holds (the agent said where the
        ball is: working on in the background, waiting on others, stalled)."""
        r = self.reports.get(key)
        return r if r is not None and r.get("ball") else None

    def expire_reports(self, now: float) -> None:
        """A report's ``needs_user`` and ball last until its agent has ended
        a turn and runs again, or is gone; the ball also lapses at the
        report's ``until``."""
        if self._engine is None:
            return
        from lazyagent.engine.status import _RUNNING, TURN_OVER

        for key, r in self.reports.items():
            if not (r.get("needs_user") or r.get("ball")):
                continue
            path, agent_id = r["worktree_path"], r["agent"]
            state = self._engine.agent_states(path).get(agent_id)
            until = r.get("until")
            if not isinstance(until, (int, float)):
                until = default_until(r.get("ball"), r["at"])
            if r.get("ball") and until is not None and now >= until:
                # Past until: the engine's own derivation is back.
                r["ball"] = None
                self.changed()
            turn = self._report_turns.get(key)
            if agent_id not in self._engine.agent_ids(path):
                self._answered(r)
            elif turn is not None and self._engine.status.turn(path, agent_id) > turn:
                # A turn after the report's: it has not reported again.
                self._answered(r)
            elif state is not None and state.status.value in TURN_OVER:
                if not r.get("turn_over"):
                    r["turn_over"] = True
                    self.changed()
            elif turn is None and state is not None and state.status.value in _RUNNING \
                    and r.get("turn_over"):
                self._answered(r)

    def answer_report(self, worktree_path: str, agent_id: str) -> bool:
        """The agent was sent input: its report is answered."""
        found = False
        for r in self.reports.values():
            if (r["worktree_path"], r["agent"]) == (worktree_path, agent_id) and (
                r.get("needs_user") or r.get("ball")
            ):
                self._answered(r)
                found = True
        return found

    def _answered(self, r: dict[str, Any]) -> None:
        r["needs_user"] = False
        r["ball"] = None
        self.changed()

    def items(self, snap: Any) -> Any:
        """Inbox items: a report with ``needs_user`` (Round, Quick)."""
        from lazyagent.engine.actionables import REPORT, Item, _id
        from lazyagent.engine.status import _with

        for key, r in self.reports.items():
            if not r.get("needs_user"):
                continue
            path = r["worktree_path"]
            place = snap.place(path)
            if place is None or place.work_item != key:
                continue
            yield Item(
                _id(REPORT, f"{path}/{r['agent']}"),
                REPORT.name,
                _with("needs you", r["note"]),
                label=r.get("label") or f"Agent {r['agent']}",
                detail=r["note"],
                work_item=key,
                worktree_path=path,
                agent_id=r["agent"],
            )
