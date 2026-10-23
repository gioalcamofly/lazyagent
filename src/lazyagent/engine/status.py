"""Work items: one row per branch, projected from what the engine knows
(proposal 0013, docs/design/inbox-plan.md §2-§4).

A row is derived, never stored: the worktree list, the agents and their
statuses, the PR info (CI, review, mergeability), the restart manifest, the
agents' session notes and the branches' last commit times. :func:`derive`
is the plan's §3 table (first match wins); it says who has the **ball**
(``you`` · ``agent`` · ``others`` · ``stalled`` · ``done``), a short fact
as the **state**, and for ``you`` rows the inbox **level** (``now`` or
``round``).

:class:`StatusProjection` keeps the rows fresh on its own, with no client
connected: it recomputes on the engine's events (coalesced to one pass per
:data:`COALESCE` seconds), keeps the PR poller interested in every work
item (client :data:`ENGINE_CLIENT`), and emits ``status.updated {row}`` /
``status.removed {key}`` for what changed. The only memory it keeps is when
each row's facts last changed (its age), and when each agent's status did.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from lazyagent.engine import overlay as overlay_mod
from lazyagent.engine.trackers import TrackerFact
from lazyagent.models import AgentStatus, PrInfo

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine
    from lazyagent.engine.events import EngineEvent

log = logging.getLogger(__name__)

STATUS_UPDATED = "status.updated"
STATUS_REMOVED = "status.removed"

# The engine's own PR interest: rows need PR info with no UI open.
ENGINE_CLIENT = "engine:status"

# At most one recompute (and one burst of events) per this many seconds.
COALESCE = 1.0
# Ages cross the stalled / stale lines with no event, so rows are also
# recomputed on a timer; branch commit times are read again every few ticks.
TICK = 60.0
COMMIT_TIMES_EVERY = 5

# What a row is recomputed for.
TRIGGERS = frozenset(
    {
        "agent.status",
        "agent.updated",
        "agent.spawned",
        "agent.exited",
        "worktrees.changed",
        "pr_info.updated",
        "notes.updated",
        "notes.removed",
        "restorable.changed",
    }
)

# now and round are Quick items; deep ones wait in "To sit down with".
LEVELS = ("now", "round", "deep")
BALL_ORDER = {"agent": 0, "others": 1, "stalled": 2, "done": 3}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
DEFAULT_PRIORITY = "medium"

STARTING = "starting"
_RUNNING = frozenset({AgentStatus.RUNNING.value, STARTING})
# Turn over, at its prompt: the next move is someone else's.
_IDLE = frozenset(
    {
        AgentStatus.WAITING.value,
        AgentStatus.COMPLETED.value,
        AgentStatus.INTERRUPTED.value,
    }
)
# §3 rule 2, in this order: (status, level, state, blocks the agent).
_NEEDS_YOU = (
    (AgentStatus.WAITING_FOR_APPROVAL.value, "now", "approve", True),
    (AgentStatus.WAITING_FOR_USER.value, "now", "answer", True),
    (AgentStatus.FAILED.value, "round", "agent failed", False),
    (AgentStatus.POSSIBLY_HANGED.value, "round", "agent may hang", False),
)

STATE_WORDS = 5
# Not a §3 row: the user passed the item to the orchestrator (claims.py).
CLAIM_RULE = 13
CLAIM_STATE = "with orchestrator"
# Turn over (or a question to the user): an agent's report holds until it
# runs again after one of these.
TURN_OVER = _IDLE | {AgentStatus.WAITING_FOR_USER.value}
_PARK_TEXT = {
    "ci_done": "CI ends",
    "agent_stopped": "agent stops",
    "human_review": "a review",
}


@dataclass(frozen=True)
class AgentFact:
    agent_id: str
    status: str
    detail: str = ""
    since: float = 0.0


@dataclass(frozen=True)
class Facts:
    """Everything a row is derived from."""

    key: str
    repo: str
    branch: str | None
    worktree_path: str | None
    agents: tuple[AgentFact, ...] = ()
    pr: PrInfo | None = None
    commit_time: float | None = None
    restorable: int = 0
    note: dict[str, Any] | None = None
    # The work item's open inbox items (engine/actionables.py), top first;
    # None derives rule 2 from the agents' statuses as before the queue.
    actionables: tuple[dict[str, Any], ...] | None = None
    # The orchestrator's claim on the work item (claims.py), if any.
    claim: dict[str, Any] | None = None
    tracker: TrackerFact | None = None
    # The user's gesture (overlay.py: label, park, close), while it holds.
    gesture: dict[str, Any] | None = None
    # The agent's last status_report on this item (overlay.py).
    report: dict[str, Any] | None = None


@dataclass(frozen=True)
class Derived:
    rule: int
    ball: str
    state: str
    level: str | None = None
    blocks_agent: bool = False


@dataclass
class _Seen:
    fingerprint: str
    changed_at: float


def ci_state(pr: PrInfo | None) -> str:
    """``green`` · ``red`` · ``running`` · ``none`` (no PR or no checks)."""
    if pr is None:
        return "none"
    return {"pass": "green", "fail": "red", "pending": "running"}.get(
        pr.overall_status, "none"
    )


def _failing_check(pr: PrInfo) -> str:
    for check in pr.checks:
        if check.conclusion.upper() == "FAILURE":
            return check.name
    return ""


def repo_name(engine: Engine) -> str:
    """A work item key's repo part: the repo root's directory name."""
    return os.path.basename(engine.repo_root.rstrip("/")) if engine.repo_root else ""


def work_item_key(repo: str, wt: Any) -> str:
    """``<repo>:<branch>``, or ``<repo>:@<head>`` when detached."""
    return f"{repo}:{wt.branch}" if wt.branch else f"{repo}:@{wt.short_head}"


def clip_words(text: str, words: int = STATE_WORDS) -> str:
    """At most ``words`` words of ``text``, on one line."""
    parts = text.split()
    if len(parts) <= words:
        return " ".join(parts)
    return " ".join(parts[:words]) + "…"


def _with(prefix: str, detail: str) -> str:
    detail = " ".join(detail.split())
    return clip_words(f"{prefix}: {detail}" if detail else prefix)


def park_text(until: Any) -> str:
    if isinstance(until, str):
        return f"parked until {_PARK_TEXT.get(until, until)}"
    return "parked until " + time.strftime("%H:%M", time.localtime(until))


def derive(facts: Facts, now: float, last_change: float, *, stalled_hours: float) -> Derived:
    """The ball, state and level of one work item (plan §3, first match
    wins). With the inbox queue, rule 2 is its open items and rules 3, 5
    and 6 are kinds of it: a dismissed or parked item no longer puts the
    row in the inbox. The user's gestures: close is rule 1, park rule 7 and
    label rule 8; both keep the state a fresh fact (Q-B7). A linked tracker
    task, or the agent's reported ball, decides the ball only where the
    engine's own facts do not (Q-B20): after rule 9, before rule 10; the
    tracker's inbox items (``tracker_status``) are the queue's."""
    pr = facts.pr
    ci = ci_state(pr)
    agents = facts.agents
    running = [a for a in agents if a.status in _RUNNING]
    idle = [a for a in agents if a.status in _IDLE]
    open_pr = pr is not None and pr.state == "OPEN"
    gesture = facts.gesture or {}

    if pr is not None and pr.state == "MERGED":
        return Derived(1, "done", "merged")
    if pr is not None and pr.state == "CLOSED":
        return Derived(1, "done", "PR closed")
    if gesture.get("kind") == "close":
        how = "dropped" if gesture.get("how") == "dropped" else "closed"
        return Derived(1, "done", f"{how} by you")

    queue = facts.actionables
    if facts.claim is not None:
        # The orchestrator's now; a tool approval still waits on the user.
        approvals = [i for i in queue or () if i["kind"] == "approval"]
        if approvals:
            return Derived(2, "you", approvals[0]["state"], "now", True)
        return Derived(CLAIM_RULE, "agent", CLAIM_STATE)
    if queue:
        top = queue[0]
        blocks = any(i["blocks_agent"] for i in queue)
        return Derived(2, "you", top["state"], top["level"], blocks)
    for status, level, text, blocks in _NEEDS_YOU if queue is None else ():
        agent = next((a for a in agents if a.status == status), None)
        if agent is not None:
            state = _with(text, agent.detail) if blocks else text
            return Derived(2, "you", state, level, blocks)
    report = facts.report
    if queue is None and report is not None and report.get("needs_user"):
        return Derived(3, "you", _with("needs you", report.get("note", "")), "round")

    if open_pr:
        review = pr.review_decision
        if review == "APPROVED" and ci in ("green", "none") and pr.mergeable == "MERGEABLE":
            return Derived(4, "you", "merge: approved, CI green", "round")
        if queue is None and review == "CHANGES_REQUESTED":
            return Derived(5, "you", "changes requested", "deep")
        if queue is None and not running:
            if pr.mergeable == "CONFLICTING":
                return Derived(5, "you", "conflict with base", "round")
            if ci == "red":
                return Derived(5, "you", _with("CI red", _failing_check(pr)), "round")

    if queue is None and idle and not running and pr is None:
        return Derived(6, "you", "review: agent finished", "deep")

    tail = _derive_tail(facts, now, last_change, stalled_hours=stalled_hours)
    if gesture.get("kind") == "park":
        return Derived(7, tail.ball, park_text(gesture.get("until")))
    if gesture.get("kind") == "label":
        # The ball only: the state stays a fact (the table shows the text).
        return Derived(8, gesture["ball"], tail.state)
    return tail


def _derive_tail(
    facts: Facts, now: float, last_change: float, *, stalled_hours: float
) -> Derived:
    """Rules 9-12: who has the ball when the user does not."""
    pr = facts.pr
    ci = ci_state(pr)
    agents = facts.agents
    running = [a for a in agents if a.status in _RUNNING]
    open_pr = pr is not None and pr.state == "OPEN"
    queue = facts.actionables

    if running:
        if ci == "red":
            return Derived(9, "agent", "fixing CI", None)
        agent = running[0]
        state = "starting" if agent.status == STARTING else agent.detail or "working"
        return Derived(9, "agent", clip_words(state))
    if open_pr and ci == "running":
        return Derived(9, "agent", "CI running")

    report = facts.report
    if report is not None and report.get("ball"):
        ball = report["ball"]
        state = clip_words(report.get("note") or f"reported: {ball}")
        return Derived(10 if ball == "others" else 12, ball, state)

    tracker = facts.tracker
    if tracker is not None and tracker.ball is not None:
        state = clip_words(tracker.state)
        if tracker.ball == "you":
            # Without the queue: the level while the task's entered-status
            # item is open. With it, that item is rule 2 (or dismissed).
            if tracker.level is not None and queue is None:
                return Derived(3, "you", state, tracker.level)
        else:
            return Derived(10 if tracker.ball == "others" else 12, tracker.ball, state)

    if open_pr and pr.review_decision in ("REVIEW_REQUIRED", ""):
        state = "review requested" if pr.review_decision else "PR open"
        if ci == "green":
            state += ", CI green"
        return Derived(10, "others", state)

    if agents:
        # An agent still on a dialog whose item was dismissed or parked.
        waits = next(
            (t for st, _, t, _ in _NEEDS_YOU if any(a.status == st for a in agents)),
            None,
        )
        return Derived(12, "agent", f"waits: {waits}" if waits else "agent idle")
    if facts.restorable:
        return Derived(12, "stalled", "agent to restore")
    if now - last_change > stalled_hours * 3600:
        return Derived(11, "stalled", "no activity")
    if open_pr and pr.review_decision == "APPROVED":
        return Derived(12, "stalled", "approved, not mergeable")
    return Derived(12, "stalled", "no agent")


def fingerprint(facts: Facts, derived: Derived) -> str:
    """A hash of the facts that move a row across a rule boundary; free
    text (an agent's detail, the state) is left out, so a tool call does
    not reset the row's age."""
    pr = facts.pr
    data = [
        derived.ball,
        derived.level,
        None
        if pr is None
        else [pr.state, pr.review_decision, pr.mergeable, ci_state(pr)],
        sorted((a.agent_id, a.status) for a in facts.agents),
    ]
    if facts.claim is not None:
        data.append(["claim", facts.claim["at"]])
    if facts.tracker is not None:
        data.append([facts.tracker.status, facts.tracker.level])
    if facts.gesture is not None:
        data.append(["gesture", facts.gesture["kind"], facts.gesture["at"]])
    report = facts.report
    if report is not None and (report.get("needs_user") or report.get("ball")):
        data.append(["report", report.get("needs_user"), report.get("ball"), report["at"]])
    raw = json.dumps(data, sort_keys=True).encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def public_gesture(gesture: dict[str, Any] | None) -> dict[str, Any] | None:
    """A gesture as rows show it: ``{kind, at, by}`` plus ``ball`` and
    ``text`` (label), ``until`` (park) or ``how`` (close)."""
    if gesture is None:
        return None
    out = {"kind": gesture["kind"], "at": gesture["at"], "by": gesture.get("by", "user")}
    for name in ("ball", "text", "until", "how"):
        if name in gesture:
            out[name] = gesture[name]
    return out


def row_note(facts: Facts) -> dict[str, Any] | None:
    """The agent's own report when it is the newest note, else the notes'."""
    report = facts.report
    note = facts.note
    if report is not None and report.get("note"):
        if note is None or (note.get("at") or 0) <= report["at"]:
            return {
                "text": report["note"],
                "by": f"agent:{report['agent']}",
                "at": report["at"],
                "agent_id": report["agent"],
            }
    return note


def build_row(
    facts: Facts,
    derived: Derived,
    changed_at: float,
    fp: str,
    *,
    stale: bool,
    seen_at: float | None = None,
    last_event_at: float | None = None,
) -> dict[str, Any]:
    pr = facts.pr
    zone = "inbox" if derived.ball == "you" else "in_flight"
    row: dict[str, Any] = {
        "key": facts.key,
        "repo": facts.repo,
        "branch": facts.branch,
        "worktree_path": facts.worktree_path,
        "title": (pr.title if pr is not None and pr.title else None)
        or facts.branch
        or facts.key,
        "urls": [pr.url] if pr is not None and pr.url else [],
        "zone": zone,
        "level": derived.level if zone == "inbox" else None,
        "ball": derived.ball,
        "priority": DEFAULT_PRIORITY,
        "state": derived.state,
        "rule": derived.rule,
        "blocks_agent": derived.blocks_agent,
        "needs_you": {"count": len(facts.actionables), "top": facts.actionables[0]["id"]}
        if facts.actionables
        else None,
        "agents": [
            {"id": a.agent_id, "status": a.status, "since": a.since}
            for a in facts.agents
        ],
        "pr": None
        if pr is None
        else {
            "number": pr.number,
            "state": pr.state,
            "review": pr.review_decision,
            "ci": ci_state(pr),
            "mergeable": pr.mergeable,
        },
        "note": row_note(facts),
        "gesture": public_gesture(facts.gesture),
        "claim": facts.claim,
        "stale": stale,
        "changed_at": changed_at,
        "fingerprint": fp,
        "class": None,
        "seen_at": seen_at,
        "last_event_at": last_event_at,
    }
    if zone == "inbox":
        row["class"] = "deep" if derived.level == "deep" else "quick"
    tracker = facts.tracker
    if tracker is not None:
        if tracker.url and tracker.url not in row["urls"]:
            row["urls"].append(tracker.url)
        if row["title"] in (facts.branch, facts.key) and tracker.title:
            row["title"] = tracker.title
        row["tracker"] = {
            "adapter": tracker.adapter,
            "key": tracker.key,
            "url": tracker.url,
            "status": tracker.status,
            "state": clip_words(tracker.state),
            "at": tracker.at,
            "stale": tracker.stale,
        }
    return row


REVIEW_TEXT = {
    "APPROVED": "review: approved",
    "CHANGES_REQUESTED": "review: changes requested",
    "REVIEW_REQUIRED": "review requested",
}
_CLOSE_HOW = {"merged": "merged", "PR closed": "closed", "dropped by you": "dropped"}
_TOMB_STATE = {
    "merged": "merged",
    "closed": "PR closed",
    "done": "closed by you",
    "dropped": "dropped by you",
    "worktree_removed": "worktree removed",
}


def _close_how(row: dict[str, Any]) -> str:
    """A done row's tombstone ``how``: merged · closed · done · dropped."""
    return _CLOSE_HOW.get(row["state"], "done")


def tombstone_row(key: str, tomb: dict[str, Any], repo: str) -> dict[str, Any]:
    """The done row of a closed item whose worktree is gone (24 h)."""
    branch = tomb.get("branch")
    facts = Facts(key=key, repo=repo, branch=branch, worktree_path=None)
    state = _TOMB_STATE.get(tomb.get("how", ""), "closed")
    row = build_row(facts, Derived(1, "done", state), tomb["at"], "", stale=False)
    row["title"] = tomb.get("title") or row["title"]
    row["urls"] = list(tomb.get("urls") or [])
    row["fingerprint"] = hashlib.sha1(f"closed:{tomb['at']}".encode()).hexdigest()[:12]
    return row


def sort_key(row: dict[str, Any]) -> tuple:
    """Inbox first (level; blocks a live agent; priority; oldest first),
    then in flight (ball; priority; most recently changed first)."""
    priority = PRIORITY_ORDER.get(row["priority"], 1)
    if row["zone"] == "inbox":
        return (
            0,
            LEVELS.index(row["level"]) if row["level"] in LEVELS else len(LEVELS),
            not row["blocks_agent"],
            priority,
            row["changed_at"],
            row["key"],
        )
    return (1, BALL_ORDER.get(row["ball"], 9), priority, -row["changed_at"], 0, row["key"])


def visible(row: dict[str, Any], include_done: bool) -> bool:
    return include_done or not (row["ball"] == "done" or row["stale"])


def summarize(
    rows: Iterable[dict[str, Any]],
    *,
    include_done: bool = False,
    not_started: int = 0,
    deep_max: int = 5,
) -> dict[str, Any]:
    """``status_list``'s answer for ``rows`` (sorted, with counts)."""
    rows = list(rows)
    shown = sorted((r for r in rows if visible(r, include_done)), key=sort_key)
    counts = {
        "now": sum(r["level"] == "now" for r in rows),
        "round": sum(r["level"] == "round" for r in rows),
        "deep": sum(r["level"] == "deep" for r in rows),
        "in_flight": sum(
            r["zone"] == "in_flight" and r["ball"] != "done" and not r["stale"]
            for r in rows
        ),
        "done": sum(r["ball"] == "done" for r in rows),
        "stale": sum(r["stale"] and r["ball"] != "done" for r in rows),
        "not_started": not_started,
        "deep_max": deep_max,
    }
    return {"rows": shown, "counts": counts}


class StatusProjection:
    """Keeps an engine's work-item rows current; see the module docstring."""

    def __init__(self, engine: Engine, *, clock: Callable[[], float] = time.time) -> None:
        self._engine = engine
        self.clock = clock
        self._rows: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, _Seen] = {}
        self._since: dict[tuple[str, str], tuple[str, float]] = {}
        self._commit_times: dict[str, float] = {}
        self._facts_by_key: dict[str, Facts] = {}
        # Timeline: what each row looked like at the last pass, and who made
        # the next change (a gesture).
        self._tracked: dict[str, dict[str, Any]] = {}
        self._actors: dict[str, str] = {}
        self._last_rows: dict[str, dict[str, Any]] = {}
        self._handle: asyncio.TimerHandle | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._ticks = 0
        self._started = False

    # --- Lifecycle ---

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._unsubscribe = self._engine.events.subscribe(self._on_event, TRIGGERS)
        self._worktrees_changed()
        self.flush()

    def stop(self) -> None:
        self._started = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._engine.worktree_service.set_pr_interest((), ENGINE_CLIENT)

    def tick(self) -> None:
        """The timer: ages move on; branch times are re-read now and then."""
        self._ticks += 1
        if self._ticks % COMMIT_TIMES_EVERY == 0:
            self._refresh_commit_times()
        self._poll_trackers()
        self.flush()

    def _on_event(self, event: EngineEvent) -> None:
        if event.event == "worktrees.changed":
            self._worktrees_changed()
        elif event.event == "notes.updated":
            self._notes_entry(event.data if isinstance(event.data, dict) else {})
        self.mark_dirty()

    def _notes_entry(self, data: dict[str, Any]) -> None:
        """A notes edit on a work item's timeline: the sections it changed."""
        sections = data.get("sections")
        path = data.get("worktree_path")
        if not sections or not isinstance(path, str):
            return
        key = self.key_of(path)
        if key is None:
            return
        actor = overlay_mod.actor_of(data.get("by", ""), data.get("agent_id") or "")
        self._engine.overlay.add(
            key, "notes", actor, "notes: " + ", ".join(sections), ref=data.get("notes_id")
        )

    def key_of(self, path: str) -> str | None:
        """The work item key of a worktree path."""
        wt = next((w for w in self._engine.worktrees if w.path == path), None)
        return work_item_key(repo_name(self._engine), wt) if wt is not None else None

    def input_sent(self, path: str, agent_id: str) -> None:
        """An agent got input: its report's ``needs_user`` / ball are
        answered."""
        if self._engine.overlay.answer_report(path, agent_id):
            self.mark_dirty()

    def mark_dirty(self) -> None:
        """Recompute within :data:`COALESCE` seconds."""
        if self._handle is not None or not self._started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._handle = loop.call_later(COALESCE, self.flush)

    def _worktrees_changed(self) -> None:
        # The PR poller's next tick covers them: no gh call of our own.
        paths = [wt.path for wt in self._members()]
        self._engine.worktree_service.set_pr_interest(paths, ENGINE_CLIENT)
        self._refresh_commit_times()
        self._poll_trackers()

    def _poll_trackers(self) -> None:
        """Poll the tracker adapters that are due (a new branch's key at
        once); a change shows within :data:`COALESCE`."""
        hub = self._engine.trackers
        if not hub.enabled:
            return
        branches = [wt.branch for wt in self._members() if wt.branch]

        async def poll() -> None:
            if await hub.poll(branches):
                self.mark_dirty()

        self._spawn(poll())

    def _refresh_commit_times(self) -> None:
        repo_root = self._engine.repo_root
        if not repo_root:
            return
        manager = self._engine._manager(repo_root)
        read = getattr(manager, "branch_commit_times", None)
        if read is None:
            return

        async def refresh() -> None:
            times = await asyncio.to_thread(read)
            if times != self._commit_times:
                self._commit_times = dict(times)
                self.mark_dirty()

        self._spawn(refresh())

    def _spawn(self, coro: Any) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        self._engine._spawn(coro)

    # --- The projection ---

    def flush(self) -> dict[str, dict[str, Any]]:
        """Recompute every row now and emit what changed since the last time."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        rows = self.compute()
        events = self._engine.events
        for key, row in rows.items():
            if self._rows.get(key) != row:
                events.emit(STATUS_UPDATED, {"row": row})
        for key in self._rows:
            if key not in rows:
                events.emit(STATUS_REMOVED, {"key": key})
        self._rows = rows
        return rows

    def list(self, *, repo: str | None = None, include_done: bool = False) -> dict[str, Any]:
        """``status_list``: fresh rows, sorted, with counts."""
        rows = self.flush().values()
        if repo is not None:
            rows = [r for r in rows if r["repo"] == repo]
        result = summarize(
            rows,
            include_done=include_done,
            not_started=self.not_started(),
            deep_max=self._engine.config.inbox.deep_max,
        )
        result["generated_at"] = self.clock()
        return result

    def row(self, key: str) -> dict[str, Any] | None:
        """The last computed row of ``key`` (after a pass, see :meth:`flush`)."""
        if key not in self._rows:
            self.flush()
        return self._rows.get(key)

    def keys(self) -> list[str]:
        return list(self._rows)

    def live_keys(self) -> list[str]:
        """The keys of rows with a worktree (not the tombstones)."""
        return [k for k, r in self._rows.items() if r["worktree_path"] is not None]

    def facts(self, key: str) -> Facts | None:
        """The facts ``key``'s row was last derived from."""
        return self._facts_by_key.get(key)

    def compute(self) -> dict[str, dict[str, Any]]:
        now = self.clock()
        config = self._engine.config.status
        claims = self._engine.claims
        overlay = self._engine.overlay
        claims.check(now)
        overlay.expire_reports(now)
        rows: dict[str, dict[str, Any]] = {}
        facts_by_key: dict[str, Facts] = {}
        live_agents: set[tuple[str, str]] = set()
        queue: dict[str, list[dict[str, Any]]] = {}
        for item in self._engine.actionables.flush(now):
            if item["work_item"] is not None:
                queue.setdefault(item["work_item"], []).append(item)
        for facts in self._facts(now):
            items = tuple(queue.get(facts.key, ()))
            claim = claims.get(facts.key)
            if claim is not None:
                items = tuple(i for i in items if i["kind"] == "approval")
            facts = replace(
                facts,
                actionables=items,
                claim=claim,
                gesture=overlay.gesture(facts, now),
                report=overlay.reports.get(facts.key),
            )
            facts_by_key[facts.key] = facts
            live_agents.update((facts.worktree_path or "", a.agent_id) for a in facts.agents)
            seen = self._seen.get(facts.key)
            last_change = seen.changed_at if seen is not None else self._seed(facts, now)
            derived = derive(facts, now, last_change, stalled_hours=config.stalled_hours)
            fp = fingerprint(facts, derived)
            if seen is None:
                seen = self._seen[facts.key] = _Seen(fp, last_change)
            elif seen.fingerprint != fp:
                seen.fingerprint, seen.changed_at = fp, now
            stale = (
                not facts.agents
                and facts.pr is None
                and not facts.restorable
                and now - seen.changed_at > config.stale_days * 86400
            )
            rows[facts.key] = build_row(facts, derived, seen.changed_at, fp, stale=stale)
        self._track(rows, facts_by_key, now)
        self._close(rows, now)
        for key, row in rows.items():
            row["seen_at"] = overlay.seen_at(key)
            row["last_event_at"] = overlay.last_event_at(key)
        for key in [k for k in self._seen if k not in rows]:
            del self._seen[key]
        for slot in [s for s in self._since if s not in live_agents]:
            del self._since[slot]
        self._facts_by_key = facts_by_key
        return rows

    def _track(
        self, rows: dict[str, dict[str, Any]], facts_by_key: dict[str, Facts], now: float
    ) -> None:
        """Add what changed since the last pass to each item's timeline:
        a new commit, a CI run that finished, a review, the ball or the
        state crossing a rule (not a tool line)."""
        overlay = self._engine.overlay
        actors = self._actors
        self._actors = {}
        tracked: dict[str, dict[str, Any]] = {}
        for key, row in rows.items():
            facts = facts_by_key[key]
            pr = facts.pr
            now_facts = {
                "fp": row["fingerprint"],
                "ball": row["ball"],
                "state": row["state"],
                "ci": ci_state(pr),
                "review": (pr.review_decision or "") if pr is not None else "",
                "commit": facts.commit_time,
            }
            tracked[key] = now_facts
            before = self._tracked.get(key)
            if before is None:
                continue
            ref = None
            wt = next((w for w in self._engine.worktrees if w.path == facts.worktree_path), None)
            if wt is not None:
                ref = wt.head
            noted = False
            if now_facts["commit"] and now_facts["commit"] != before["commit"] and (
                before["commit"] is None or now_facts["commit"] > before["commit"]
            ):
                overlay.add(key, "commit", "engine", "commit " + (ref or "")[:7],
                            ref=ref, at=min(now_facts["commit"], now))
            if now_facts["ci"] != before["ci"] and now_facts["ci"] in ("green", "red"):
                text = "CI green" if now_facts["ci"] == "green" else _with("CI red", _failing_check(pr))
                overlay.add(key, "ci", "engine", text)
                noted = True
            if now_facts["review"] != before["review"] and now_facts["review"]:
                overlay.add(key, "review", "engine", REVIEW_TEXT.get(
                    now_facts["review"], now_facts["review"].lower()))
                noted = True
            if now_facts["fp"] == before["fp"]:
                continue
            actor = actors.get(key, "engine")
            if now_facts["ball"] != before["ball"]:
                overlay.add(key, "ball", actor,
                            f"{before['ball']} → {now_facts['ball']}: {now_facts['state']}")
            elif not noted and now_facts["state"] != before["state"]:
                overlay.add(key, "state", actor, now_facts["state"])
        self._tracked = tracked

    def by(self, key: str, actor: str) -> None:
        """The next change of ``key``'s row is ``actor``'s doing (a gesture)."""
        self._actors[key] = actor

    def _close(self, rows: dict[str, dict[str, Any]], now: float) -> None:
        """Tombstones: an item that became done or lost its worktree is
        closed (its open briefs expire); a removed one stays a done row for
        :data:`overlay.TOMBSTONE`, then what was kept for it goes."""
        overlay = self._engine.overlay
        decisions = self._engine.decisions
        for key, row in rows.items():
            if row["ball"] == "done":
                how = _close_how(row)
                if overlay.close(key, how, now, row):
                    decisions.expire_item(key, how)
            elif key in overlay.closed:
                overlay.reopen(key)
        known = set(overlay.gestures) | set(overlay.reports) | set(overlay.timeline)
        known |= {d["key"] for d in overlay.decisions.values() if d.get("state") == "open"}
        known |= set(self._last_rows)
        for key in known - set(rows) - set(overlay.closed):
            if overlay.close(key, "worktree_removed", now, self._last_rows.get(key)):
                decisions.expire_item(key, "worktree_removed")
        for key, tomb in list(overlay.closed.items()):
            if key in rows:
                continue
            if now - tomb["at"] >= overlay_mod.TOMBSTONE:
                overlay.forget(key)
                continue
            rows[key] = tombstone_row(key, tomb, repo_name(self._engine))
        self._last_rows = {
            k: {"title": r["title"], "branch": r["branch"], "urls": r["urls"]}
            for k, r in rows.items()
            if r["worktree_path"] is not None
        }

    def not_started(self) -> int:
        """Tasks assigned to the user that no local branch links to."""
        hub = self._engine.trackers
        if not hub.enabled:
            return 0
        branches = set(self._commit_times)
        branches.update(wt.branch for wt in self._members() if wt.branch)
        return hub.not_started(branches)

    def _seed(self, facts: Facts, now: float) -> float:
        """When a row first seen last changed: its branch's last commit, or
        its agents' last status change, whichever is later; else now."""
        times = [a.since for a in facts.agents]
        if facts.commit_time is not None:
            times.append(facts.commit_time)
        return min(max(times), now) if times else now

    def _members(self) -> list[Any]:
        """The worktrees that are work items: all but bare ones, and the main
        checkout only while an agent runs there."""
        engine = self._engine
        return [
            wt
            for wt in engine.worktrees
            if not wt.is_bare and (not wt.is_main or engine.agent_ids(wt.path))
        ]

    def _facts(self, now: float) -> list[Facts]:
        engine = self._engine
        repo = repo_name(engine)
        restorable: dict[str, int] = {}
        if engine.manifest is not None:
            for entry in engine._offerable():
                restorable[entry.worktree_path] = restorable.get(entry.worktree_path, 0) + 1
        out = []
        for wt in self._members():
            agents = self._agents(wt.path, now)
            branch = wt.branch
            key = work_item_key(repo, wt)
            out.append(
                Facts(
                    key=key,
                    repo=repo,
                    branch=branch,
                    worktree_path=wt.path,
                    agents=agents,
                    pr=engine.get_pr_info(wt.path),
                    commit_time=self._commit_times.get(branch) if branch else None,
                    restorable=restorable.get(wt.path, 0),
                    note=self._note(wt.path, agents),
                    tracker=engine.trackers.fact(branch),
                )
            )
        return out

    def _agents(self, path: str, now: float) -> tuple[AgentFact, ...]:
        engine = self._engine
        states = engine.agent_states(path)
        facts = []
        ids = list(states)
        ids += [a for a in engine.agent_ids(path) if a not in states]
        for agent_id in ids:
            state = states.get(agent_id)
            status = state.status.value if state is not None else STARTING
            if status == AgentStatus.NO_AGENT.value:
                status = STARTING
            slot = (path, agent_id)
            known = self._since.get(slot)
            if known is None or known[0] != status:
                known = self._since[slot] = (status, now)
            facts.append(
                AgentFact(agent_id, status, state.detail if state else "", known[1])
            )
        return tuple(facts)

    def _note(self, path: str, agents: tuple[AgentFact, ...]) -> dict[str, Any] | None:
        """The first open Pending item, else the last Doubt, of the first
        agent with notes."""
        engine = self._engine
        store = engine.notes
        if store is None:
            return None
        for agent in agents:
            notes_id = engine.notes_id(path, agent.agent_id)
            if notes_id is None or notes_id not in store:
                continue
            pending = [
                b[4:].strip() if b.startswith("[ ] ") else b
                for b in store.section_bullets(notes_id, "Pending")
                if not b.lower().startswith("[x]")
            ]
            doubts = store.section_bullets(notes_id, "Doubts")
            text = pending[0] if pending else doubts[-1] if doubts else None
            if text:
                return {
                    "text": text,
                    "by": "notes",
                    "at": store.get(notes_id).meta.updated_at,
                    "agent_id": agent.agent_id,
                }
        return None


# ----------------------------------------------------------------------
# The table (CLI and MCP)
# ----------------------------------------------------------------------


def format_age(seconds: float) -> str:
    """``<1m``, ``4m``, ``2h``, ``3d``: never ``0s``."""
    if seconds < 60:
        return "<1m"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def item_name(row: dict[str, Any]) -> str:
    name = row["branch"] or row["key"].partition(":")[2]
    if row.get("pr"):
        name += f" #{row['pr']['number']}"
    return name


def state_text(row: dict[str, Any], now: float) -> str:
    """The state, with the tracker's text beside it when it says more (and
    its age when the tracker could not be read since)."""
    state = row["state"]
    gesture = row.get("gesture")
    if gesture and gesture.get("kind") == "label" and gesture.get("text"):
        state = f"{gesture['text']} · {state}"
    tracker = row.get("tracker")
    if not tracker or not tracker["state"]:
        return state
    text = tracker["state"]
    if tracker["stale"]:
        text += f" ({format_age(now - tracker['at'])} ago)"
    elif text == state:
        return state
    return f"{state} · {text}" if state else text


@dataclass
class _Column:
    title: str
    width: int = 0
    values: list[str] = field(default_factory=list)


def format_table(result: dict[str, Any], now: float | None = None) -> str:
    """``status_list``'s answer as the plain-text table ``lazyagent status``
    prints (and the orchestrator shows as it is)."""
    now = time.time() if now is None else now
    counts = result["counts"]
    head = (
        f"NOW {counts['now']}   ROUND {counts['round']}   "
        f"IN FLIGHT {counts['in_flight']}"
    )
    if counts.get("not_started"):
        head += f"   NOT STARTED {counts['not_started']}"
    hidden = [
        f"{counts[k]} {k}" for k in ("done", "stale") if counts.get(k)
    ]
    if hidden:
        head += "   (" + ", ".join(hidden) + ")"
    rows = result["rows"]
    if not rows:
        return head + "\nno work items"
    columns = [_Column(t) for t in ("LEVEL", "ITEM", "BALL", "STATE", "AGE")]
    for row in rows:
        values = (
            row["level"] or "-",
            item_name(row),
            row["ball"],
            state_text(row, now),
            format_age(now - row["changed_at"]),
        )
        for column, value in zip(columns, values):
            column.values.append(value)
    for column in columns:
        column.width = max(len(column.title), *(len(v) for v in column.values))
    lines = [head]
    lines.append(
        "  ".join(c.title.ljust(c.width) for c in columns).rstrip()
    )
    for i in range(len(rows)):
        lines.append(
            "  ".join(c.values[i].ljust(c.width) for c in columns).rstrip()
        )
    return "\n".join(lines)
