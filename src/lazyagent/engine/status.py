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
import functools
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
from lazyagent.engine.work_items import EXT_PREFIX
from lazyagent.engine.registry import ORCHESTRATOR_KEY
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
STATE_WORDS = 5
# Not a §3 row: the user passed the item to the orchestrator (claims.py).
CLAIM_RULE = 13
CLAIM_STATE = "orchestrator · looking"
# Not a §3 row either: the orchestrator holds a routed brief or question
# (engine/orchestrator.py) and nothing else is the user's.
HELD_RULE = 14
HELD_STATE = "orchestrator · looking"
# The orchestrator holds the item itself (orchestrator_hold, claims.py).
HOLD_RULE = 15
# The user delegated an inbox item to the branch's agent (delegation.py).
DELEGATED_RULE = 16
# The park gesture: the whole work item, out of the inbox and in flight.
PARK_RULE = 7
# Turn over (or a question to the user): an agent's report holds until it
# runs again after one of these.
TURN_OVER = _IDLE | {AgentStatus.WAITING_FOR_USER.value}
_PARK_TEXT = {
    "ci_done": "CI ends",
    "agent_stopped": "agent stops",
    "human_review": "a review",
    "change": "something changes",
}
# The branches an open PR's head may already be in ("already merged").
MERGED_INTO = ("develop", "main")


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
    # "orchestrator" while it holds an open routed item here.
    holder: str | None = None
    # The worktree's head commit.
    head: str | None = None
    # The orchestrator's own hold on the item (orchestrator_hold).
    hold: dict[str, Any] | None = None
    # An inbox item the user delegated to the branch's agent.
    delegation: dict[str, Any] | None = None
    # The branch an open PR's head is already contained in.
    merged_into: str | None = None
    # The orchestrator's last release of a pass or hold: {outcome, note, at}.
    released: dict[str, Any] | None = None
    # agent id → who spawned it (the orchestrator's workers).
    spawned_by: dict[str, str] = field(default_factory=dict)
    # The feeder's work item linked to the branch (work_items.py, public).
    feeder: dict[str, Any] | None = None
    # The newest sign of work on the item (StatusProjection._activity): a
    # commit, a notes edit, a report, a step by someone other than the
    # engine, an agent status change the engine saw happen.
    activity_at: float | None = None


@dataclass(frozen=True)
class Derived:
    rule: int
    ball: str
    state: str
    level: str | None = None
    blocks_agent: bool = False
    # A hint for the user (never done for them), e.g. "close the PR".
    suggestion: str | None = None


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


def until_text(until: float, now: float | None = None) -> str:
    """``08:00`` today, ``Tue 08:00`` within the week, else ``Oct 30``."""
    now = time.time() if now is None else now
    at, today = time.localtime(until), time.localtime(now)
    if (at.tm_year, at.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M", at)
    if until - now < 6 * 86400:
        return time.strftime("%a %H:%M", at)
    return time.strftime("%b %d", at)


def park_text(until: Any, reason: str | None = None, now: float | None = None) -> str:
    if reason:
        return clip_words(f"parked: {reason}")
    if until == "indefinite":
        return "parked"
    if isinstance(until, str):
        return f"parked until {_PARK_TEXT.get(until, until)}"
    return "parked until " + until_text(until, now)


def derive(facts: Facts, now: float, last_change: float, *, stalled_hours: float) -> Derived:
    """The ball, state and level of one work item (plan §3, first match
    wins), from the rules catalog (engine/rules, ``builtin.toml``). With the
    inbox queue, rule 2 is its open items and rules 3, 5 and 6 are kinds of
    it: a dismissed or parked item no longer puts the row in the inbox. The
    user's gestures: close is rule 1, park rule 7 and label rule 8; both keep
    the state a fresh fact (Q-B7). A linked tracker task, or the agent's
    reported ball, decides the ball only where the engine's own facts do not
    (Q-B20): after rule 9, before rule 10; the tracker's inbox items
    (``tracker_status``) are the queue's."""
    return _catalog().derive(facts, now, last_change, stalled_hours)


@functools.cache
def _catalog() -> Any:
    # Imported here: the rules package reads this module's helpers.
    from lazyagent.engine.rules import builtin

    return builtin()


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
    if facts.holder is not None:
        data.append(["holder", facts.holder])
    if facts.hold is not None:
        data.append(["hold", facts.hold["at"]])
    if facts.delegation is not None:
        data.append(["delegation", facts.delegation["at"], facts.delegation.get("phase")])
    if facts.merged_into is not None:
        data.append(["merged_into", facts.merged_into])
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
    for name in ("ball", "text", "until", "how", "waiting", "source"):
        if name in gesture:
            out[name] = gesture[name]
    return out


def row_note(facts: Facts) -> dict[str, Any] | None:
    """The newest of the orchestrator's release note, the agent's own report
    and the notes' bullet."""
    found = []
    if facts.note is not None:
        found.append(facts.note)
    report = facts.report
    if report is not None and report.get("note"):
        found.append({
            "text": report["note"],
            "by": f"agent:{report['agent']}",
            "at": report["at"],
            "agent_id": report["agent"],
        })
    released = facts.released
    if released is not None and released.get("note"):
        found.append({"text": f"orchestrator: {released['note']}", "by": "orchestrator",
                      "at": released["at"]})
    # The later one wins; on a tie the report over the notes, as before.
    return max(reversed(found), key=lambda n: n.get("at") or 0, default=None)


def holder_of(facts: Facts, derived: Derived, zone: str) -> str | None:
    """Who has the ball now: ``user`` for an inbox row (and a parked one
    whose items still wait on the user); ``orchestrator`` while it holds the
    item (a pass, its hold, a routed brief or question); ``agent`` while an
    agent has it directly (it works, idles on the item, reported the ball,
    or was delegated the item); else None (others, stalled, done)."""
    if zone == "inbox":
        return "user"
    if facts.holder == "orchestrator" or derived.rule in (CLAIM_RULE, HELD_RULE, HOLD_RULE):
        return "orchestrator"
    if zone == "parked":
        return "user" if facts.actionables else None
    if derived.rule == DELEGATED_RULE:
        return "agent"
    if derived.ball == "agent" and facts.agents:
        if derived.rule == 9 and not any(a.status in _RUNNING for a in facts.agents):
            return None  # CI running, nobody working on it
        return "agent"
    return None


def last_step(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest timeline entry that is a step (not a ball or state
    change): ``{at, kind, actor, text}``."""
    for entry in reversed(entries):
        if entry["kind"] not in ("ball", "state"):
            return {k: entry[k] for k in ("at", "kind", "actor", "text")}
    return None


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
    if derived.rule == PARK_RULE:
        zone = "parked"
    else:
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
        "priority": (facts.feeder or {}).get("priority")
        or (facts.tracker.priority if facts.tracker is not None else None)
        or DEFAULT_PRIORITY,
        "state": derived.state,
        "rule": derived.rule,
        "blocks_agent": derived.blocks_agent,
        "needs_you": {"count": len(facts.actionables), "top": facts.actionables[0]["id"]}
        if facts.actionables
        else None,
        "agents": [
            {"id": a.agent_id, "status": a.status, "since": a.since}
            | ({"spawned_by": facts.spawned_by[a.agent_id]}
               if a.agent_id in facts.spawned_by else {})
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
        "hold": facts.hold,
        "delegation": facts.delegation,
        "suggestion": derived.suggestion,
        "stale": stale,
        "changed_at": changed_at,
        "fingerprint": fp,
        "class": None,
        "seen_at": seen_at,
        "last_event_at": last_event_at,
        "holder": holder_of(facts, derived, zone),
        "activity_at": facts.activity_at,
        # The newest timeline entry but ball / state (StatusProjection).
        "last_step": None,
    }
    if zone == "inbox":
        row["class"] = "deep" if derived.level == "deep" else "quick"
    feeder = facts.feeder
    if feeder is not None:
        if feeder["url"] not in row["urls"]:
            row["urls"].append(feeder["url"])
        if row["title"] in (facts.branch, facts.key):
            row["title"] = feeder["title"]
        row["project"] = feeder["project"]
        row["depends_on"] = feeder["depends_on"]
        # Filled in once every row is known (StatusProjection._projects).
        row["waits_on"] = []
        row["feeder"] = feeder
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
    zone = 2 if row["zone"] == "parked" else 1
    return (zone, BALL_ORDER.get(row["ball"], 9), priority, -row["changed_at"], 0, row["key"])


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
        "parked": sum(r["zone"] == "parked" for r in rows),
        # Parked rows whose items still wait on the user (not routed).
        "parked_needs_you": sum(r["zone"] == "parked" and bool(r["needs_you"]) for r in rows),
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
        # worktree path → when the engine last saw one of its agents change
        # status, start or go (not the first sighting when it starts).
        self._saw: dict[str, float] = {}
        self._primed = False
        self._commit_times: dict[str, float] = {}
        # (key, head) → the branch that head is already in, or None.
        self._merged: dict[tuple[str, str], str | None] = {}
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
        self._poll_actions()
        self.flush()

    def _on_event(self, event: EngineEvent) -> None:
        if event.event == "worktrees.changed":
            self._worktrees_changed()
        elif event.event == "pr_info.updated":
            # A new PR head may already be in develop (cached per head).
            self._spawn(self.refresh_merged())
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

    def path_of(self, key: str) -> str | None:
        """The worktree path of a live work item key."""
        repo = repo_name(self._engine)
        wt = next((w for w in self._members() if work_item_key(repo, w) == key), None)
        return wt.path if wt is not None else None

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
        self._poll_actions()

    def _poll_actions(self) -> None:
        """Poll GitHub Actions when due (a new branch at once): the
        members' branches but the merge targets, one poll at a time, once
        the PR poller has found ``gh`` working."""
        hub = getattr(self._engine, "actions", None)
        if hub is None or not hub.enabled:
            return
        if self._engine.worktree_service._gh_available is not True:
            # The PR poller finds out whether gh works (its first tick).
            return
        targets = set(self.merge_targets()) | {"main", "master"}
        branches = [wt.branch for wt in self._members() if wt.branch and wt.branch not in targets]

        async def poll() -> None:
            if await hub.poll(branches):
                self.mark_dirty()

        self._spawn(poll())

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
            await self.refresh_merged()

        self._spawn(refresh())

    def merge_targets(self) -> tuple[str, ...]:
        """develop, main and the configured default branch."""
        default = self._engine.config.default_branch
        return tuple(dict.fromkeys((*MERGED_INTO, *([default] if default else []))))

    async def refresh_merged(self) -> None:
        """Which open PRs' heads are already in a merge target: git, once
        per head, off the loop."""
        repo_root = self._engine.repo_root
        if not repo_root:
            return
        repo = repo_name(self._engine)
        targets = self.merge_targets()
        known: dict[tuple[str, str], str | None] = {}
        todo: list[tuple[tuple[str, str], tuple[str, ...]]] = []
        for wt in self._members():
            pr = self._engine.get_pr_info(wt.path)
            head = (pr.head_sha or wt.head) if pr is not None and pr.state == "OPEN" else ""
            if not head:
                continue
            slot = (work_item_key(repo, wt), head)
            if slot in self._merged:
                known[slot] = self._merged[slot]
            else:
                todo.append((slot, tuple(t for t in targets if t != wt.branch)))
        if todo:
            found = await asyncio.to_thread(
                lambda: {slot: merged_into(repo_root, slot[1], others) for slot, others in todo}
            )
            known.update(found)
        if known != self._merged:
            self._merged = known
            self.mark_dirty()

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
        unstarted = self.not_started_items()
        result = summarize(
            rows,
            include_done=include_done,
            not_started=len(unstarted),
            deep_max=self._engine.config.inbox.deep_max,
        )
        if self._engine.work_items.records:
            # The feeders' (and trackers') tasks with no branch yet.
            result["not_started_items"] = unstarted
        now = self.clock()
        responder = self._engine.first_responder
        result["counts"].update(responder.counts(now))
        result["presence"] = responder.presence.to_dict(now)
        result["orchestrator"] = responder.report()
        result["generated_at"] = now
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
        delegations = self._engine.delegations
        work_items = self._engine.work_items
        claims.check(now)
        self._engine.first_responder.check(now)
        overlay.expire_reports(now)
        work_items.expire(now)
        rows: dict[str, dict[str, Any]] = {}
        facts_by_key: dict[str, Facts] = {}
        live_agents: set[tuple[str, str]] = set()
        queue: dict[str, list[dict[str, Any]]] = {}
        visible_items = self._engine.actionables.flush(now)
        claims.auto_hold(visible_items, now)
        delegations.check(now)
        for item in visible_items:
            if item["work_item"] is not None:
                queue.setdefault(item["work_item"], []).append(item)
        notes_ids = self._restorable_notes()
        for facts in self._facts(now):
            key = facts.key
            items = delegations.filter(key, claims.filter(key, queue.get(key, ())))
            gesture = overlay.gesture(facts, now)
            if gesture is None or gesture["kind"] == "label":
                # The feeder's Waiting lane parks it; the user's park or
                # close wins, and their label comes back once it ends.
                record = work_items.records.get((facts.feeder or {}).get("url", ""))
                gesture = work_items.waiting_gesture(record, facts, now) or gesture
            facts = replace(
                facts,
                actionables=tuple(items),
                claim=claims.get(key),
                hold=claims.get_hold(key),
                delegation=delegations.get(key),
                gesture=gesture,
                report=overlay.reports.get(key),
                holder="orchestrator" if self._engine.first_responder.held(key) else None,
                released=overlay.released_note(key, now),
                spawned_by=overlay.spawned_by(key),
            )
            facts = replace(facts, activity_at=self._activity(facts, notes_ids, now))
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
            changed_at = seen.changed_at
            if derived.ball == "stalled" and facts.activity_at is not None:
                # A stalled row's age is how long nothing happened, not when
                # the engine last noticed a fact (its start, PR info coming).
                changed_at = min(facts.activity_at, now)
            stale = (
                not facts.agents
                and facts.pr is None
                and not facts.restorable
                and now - changed_at > config.stale_days * 86400
            )
            rows[facts.key] = build_row(facts, derived, changed_at, fp, stale=stale)
        self._projects(rows, now)
        self._track(rows, facts_by_key, now)
        self._close(rows, now)
        for key, row in rows.items():
            row["seen_at"] = overlay.seen_at(key)
            row["last_event_at"] = overlay.last_event_at(key)
            row["last_step"] = last_step(overlay.timeline.get(key, ()))
        for key in [k for k in self._seen if k not in rows]:
            del self._seen[key]
        for slot in [s for s in self._since if s not in live_agents]:
            del self._since[slot]
        self._primed = True
        self._facts_by_key = facts_by_key
        self._engine.autodelegate.check(rows, visible_items, now)
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

    def _branches(self) -> set[str]:
        """Every local branch: the worktrees' and every for-each-ref one."""
        branches = set(self._commit_times)
        branches.update(wt.branch for wt in self._members() if wt.branch)
        return branches

    def not_started(self) -> int:
        """Tasks no local branch links to: the trackers' assigned tasks and
        the feeders' work items (not those waiting), once per URL."""
        return len(self.not_started_items())

    def not_started_items(self) -> list[dict[str, Any]]:
        """What :meth:`not_started` counts, highest priority first, then
        oldest."""
        hub = self._engine.trackers
        work_items = self._engine.work_items
        if not hub.enabled and not work_items.records:
            return []
        branches = self._branches()
        out: dict[str, dict[str, Any]] = {}
        for record in work_items.unlinked(branches):
            if work_items.waiting_active(record):
                continue
            item = work_items.public(record)
            item["startable"] = self._startable(record)
            out[record["url"]] = item
        if hub.enabled:
            for adapter, task in hub.not_started_tasks(branches):
                url = task.url or f"{adapter}:{task.key}"
                out.setdefault(url, {
                    "url": url,
                    "title": task.title,
                    "source": f"tracker:{adapter}",
                    "priority": task.priority if task.priority in PRIORITY_ORDER
                    else DEFAULT_PRIORITY,
                    "ticket": task.key,
                })
        return sorted(out.values(), key=lambda i: (
            PRIORITY_ORDER.get(i["priority"], 1), i.get("created_at") or 0, i["url"]))

    def priority_of(self, work_item: str | None) -> str | None:
        """The priority a work item's inbox items inherit: its feeder's, else
        its tracker task's; None: the default."""
        if not work_item:
            return None
        branch = work_item.partition(":")[2]
        record = self._engine.work_items.link(branch)
        if record is not None:
            return record["priority"]
        fact = self._engine.trackers.fact(branch)
        return fact.priority if fact is not None else None

    def prioritize(self, items: dict[str, Any]) -> None:
        """An inbox queue filter: items take their work item's priority."""
        for item in items.values():
            priority = self.priority_of(item.work_item)
            if priority is not None:
                item.priority = priority

    # --- Projects ---

    def _dependency(self, url: str, rows: dict[str, dict[str, Any]]
                    ) -> tuple[str, str] | None:
        """``(state, title)`` of a dependency: ``merged`` (a done row, or a
        URL the engine no longer knows: closed or never fed), ``review`` (a
        row with an open PR) or ``open``."""
        for row in rows.values():
            if url in row["urls"]:
                if row["ball"] == "done":
                    return "merged", row["title"]
                state = "review" if row["pr"] and row["pr"]["state"] == "OPEN" else "open"
                return state, row["title"]
        record = self._engine.work_items.records.get(url)
        if record is not None:
            return "open", record["title"]
        return None

    def _waits_on(self, record: dict[str, Any], rows: dict[str, dict[str, Any]]
                  ) -> tuple[list[dict[str, str]], bool]:
        """The open dependencies, and whether every one is merged or in
        review."""
        waits, ready = [], True
        for url in record.get("depends_on") or ():
            dep = self._dependency(url, rows)
            if dep is None or dep[0] == "merged":
                continue
            waits.append({"url": url, "title": dep[1], "state": dep[0]})
            ready = ready and dep[0] == "review"
        return waits, ready

    def _startable(self, record: dict[str, Any]) -> bool:
        """A task with dependencies, all merged or in review, in a project
        that continues while review."""
        return bool(
            record.get("depends_on")
            and self._engine.work_items.continues_while_review(record.get("project"))
            and self._waits_on(record, self._rows)[1]
        )

    def _projects(self, rows: dict[str, dict[str, Any]], now: float) -> None:
        """"waits on <title>" on rows with open dependencies; a wake for the
        orchestrator when a project task becomes startable under
        ``continue_while_review``."""
        work_items = self._engine.work_items
        if not work_items.records:
            return
        for row in rows.values():
            feeder = row.get("feeder")
            record = work_items.records.get(feeder["url"]) if feeder else None
            if record is not None:
                row["waits_on"] = self._waits_on(record, rows)[0]
        responder = self._engine.first_responder
        for record in work_items.unlinked(self._branches()):
            deps = record.get("depends_on") or []
            if not deps or not work_items.continues_while_review(record.get("project")):
                continue
            if work_items.waiting_active(record) or not self._waits_on(record, rows)[1]:
                continue
            if record.get("woke") == deps or not responder.alive():
                continue
            key = EXT_PREFIX + record["url"]
            verdict, _ = responder.verdict("next_project_task", key, now)
            project = work_items.project(record.get("project")) or {}
            responder.mailbox.post(
                (ORCHESTRATOR_KEY, ""),
                project_wake_text(project.get("title", ""), record, verdict == "allow"),
                tag=f"project:{record['url']}",
            )
            work_items.set_woke(record["url"], list(deps))
        responder.mailbox.flush()

    def _seed(self, facts: Facts, now: float) -> float:
        """When a row first seen last changed: its branch's last commit, or
        its agents' last status change, whichever is later; else now."""
        times = [a.since for a in facts.agents]
        if facts.commit_time is not None:
            times.append(facts.commit_time)
        return min(max(times), now) if times else now

    def _restorable_notes(self) -> dict[str, list[str]]:
        """worktree path → the notes ids of its restorable agents."""
        engine = self._engine
        out: dict[str, list[str]] = {}
        if engine.manifest is not None:
            for entry in engine._offerable():
                if entry.notes_id:
                    out.setdefault(entry.worktree_path, []).append(entry.notes_id)
        return out

    def _activity(self, facts: Facts, notes_ids: dict[str, list[str]], now: float
                  ) -> float | None:
        """The newest sign of work on the item: its branch's last commit,
        its agents' (live or restorable) notes edits, the agent's report, a
        timeline step by someone other than the engine, and the agent status
        changes this engine saw happen. None when nothing is known."""
        engine = self._engine
        path = facts.worktree_path or ""
        times = [facts.commit_time] if facts.commit_time is not None else []
        store = engine.notes
        if store is not None:
            ids = [engine.notes_id(path, a.agent_id) for a in facts.agents]
            ids += notes_ids.get(path, [])
            times += [store.get(i).meta.updated_at for i in ids if i is not None and i in store]
        if facts.report is not None:
            times.append(facts.report["at"])
        for entry in reversed(engine.overlay.timeline.get(facts.key, ())):
            if entry["actor"] != "engine":
                times.append(entry["at"])
                break
        if path in self._saw:
            times.append(self._saw[path])
        times = [t for t in times if t]
        return min(max(times), now) if times else None

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
            pr = engine.get_pr_info(wt.path)
            head = getattr(wt, "head", None)
            merged = None
            if pr is not None and pr.state == "OPEN":
                merged = self._merged.get((key, pr.head_sha or head or ""))
            out.append(
                Facts(
                    key=key,
                    repo=repo,
                    branch=branch,
                    worktree_path=wt.path,
                    agents=agents,
                    pr=pr,
                    commit_time=self._commit_times.get(branch) if branch else None,
                    restorable=restorable.get(wt.path, 0),
                    note=self._note(wt.path, agents),
                    tracker=engine.trackers.fact(branch),
                    feeder=self._feeder(branch),
                    head=head,
                    merged_into=merged,
                )
            )
        return out

    def _feeder(self, branch: str | None) -> dict[str, Any] | None:
        record = self._engine.work_items.link(branch)
        return self._engine.work_items.public(record) if record is not None else None

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
                if known is not None or self._primed:
                    # Seen happen, not found running when the engine started.
                    self._saw[path] = now
                known = self._since[slot] = (status, now)
            facts.append(
                AgentFact(agent_id, status, state.detail if state else "", known[1])
            )
        if any(p == path and a not in ids for p, a in self._since):
            # An agent went: something happened there.
            self._saw[path] = now
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


def project_wake_text(project: str, record: dict[str, Any], allowed: bool) -> str:
    """The orchestrator's wake line for a startable project task."""
    head = (f"Project {project}: task {record['title']} ({record['url']}) can start: "
            "its dependencies are merged or in review (continue_while_review).")
    if allowed:
        return head + " The user is away: start it within the spawn caps, or say why not."
    return head + " The user is present: propose it to them; do not start it alone."


def merged_into(repo_root: str, head: str, targets: Iterable[str]) -> str | None:
    """The first of ``targets`` (its remote-tracking ref, else the local
    branch) that already contains ``head``; None when none does or git
    cannot tell."""
    import subprocess

    def git(*args: str) -> int:
        try:
            return subprocess.run(
                ["git", "-C", repo_root, *args],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return -1

    for target in targets:
        for ref in (f"refs/remotes/origin/{target}", f"refs/heads/{target}"):
            if git("rev-parse", "--verify", "--quiet", ref) != 0:
                continue
            if git("merge-base", "--is-ancestor", head, ref) == 0:
                return target
            break
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
    if row.get("waits_on") and row["ball"] != "done":
        state += " · waits on " + ", ".join(w["title"] for w in row["waits_on"])
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
    if counts.get("parked"):
        head += f"   PARKED {counts['parked']}"
        if counts.get("parked_needs_you"):
            head += f" ({counts['parked_needs_you']} need you)"
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
            row["level"] or ("parked" if row.get("zone") == "parked" else "-"),
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
