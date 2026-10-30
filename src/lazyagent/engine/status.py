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
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from lazyagent.engine import overlay as overlay_mod
from lazyagent.engine.trackers import TrackerFact
from lazyagent.engine.work_items import EXT_PREFIX
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.rules.facts import RowContext
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
# Not a §3 row either: the orchestrator holds a routed brief or question
# (engine/orchestrator.py) and nothing else is the user's.
HELD_RULE = 14
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
# The fields of each open inbox item a row lists (``needs_you.items``).
ITEM_FIELDS = ("id", "kind", "level", "state", "since")
# What a row shows of a review request (status_row.review_request).
REQUEST_FIELDS = ("number", "title", "url", "author", "requested_at", "team")
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
    # False while GitHub fails and the PR poller never answered for this
    # worktree: a None pr is then unknown, not "no PR".
    pr_known: bool = True
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
    # The authenticated GitHub user's login (pr.mine); None: not known.
    me: str | None = None
    # The open request for the user's review on this row's PR
    # (engine/review_requests.py, ReviewRequest.to_dict), and whether the
    # hub's search answered (else pr.review_requested_from_me is unknown).
    review_request: dict[str, Any] | None = None
    requests_known: bool = False
    # Mechanism A on the head (engine/ci_tests.py): None while no check has
    # tests_from.
    ci_tests: Any = None
    # The user's approval of the PR's head (engine/owner_approvals.py,
    # ApprovalFact), if one stands.
    owner_approval: Any = None


def evolve(facts: Facts, **changes: Any) -> Facts:
    """``dataclasses.replace(facts, **changes)`` without the generated
    ``__init__`` over every field: a pass makes a few per row. Facts has no
    ``__post_init__`` and every field is an init field, so the copy is the
    same value."""
    unknown = changes.keys() - _FACT_FIELDS
    if unknown:
        raise TypeError(f"Facts has no field {sorted(unknown)[0]!r}")
    new = object.__new__(Facts)
    new.__dict__.update(facts.__dict__, **changes)
    return new


_FACT_FIELDS = frozenset(f.name for f in fields(Facts))


@dataclass(frozen=True)
class Derived:
    rule: int
    ball: str
    state: str
    level: str | None = None
    blocks_agent: bool = False
    # A hint for the user (never done for them), e.g. "close the PR".
    suggestion: str | None = None
    # The catalog rule's id (``status_list {fields}``, rules_explain): not
    # part of the row's identity, so equal rows compare equal.
    rule_id: str = field(default="", compare=False, repr=False)


@dataclass
class _Seen:
    fingerprint: str
    changed_at: float
    # The row had PR info on its last pass: PR info that comes later is
    # the engine learning a fact (its poller's first answer), not a change.
    pr: bool = False


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


def required_sentence(tests: Any) -> str:
    """Why?'s line on the gating (R5a): "Required checks (branch protection
    of main): unit, lint; e2e red · not gating." """
    r = tests.required
    if r is None:
        return "Gating from [ci.check] in the rule files."
    where = (f"branch protection of {r.base}" if r.source == "protection"
             else f"{r.read_from}'s branch protection; {r.base} requires none")
    text = f"Required checks ({where}): {', '.join(r.names)}."
    nongating = tests.nongating_failures
    if nongating:
        text = f"{text[:-1]}; red but not gating: {tests._names(nongating)}."
    return text


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
        return "parked indefinitely"
    if isinstance(until, str):
        return f"parked until {_PARK_TEXT.get(until, until)}"
    return "parked until " + until_text(until, now)


def derive(facts: Facts, now: float, last_change: float, *, stalled_hours: float,
           catalog: Any = None) -> Derived:
    """The ball, state and level of one work item (plan §3, first match
    wins), from the rules catalog (engine/rules, ``builtin.toml``). With the
    inbox queue, rule 2 is its open items and rules 3, 5 and 6 are kinds of
    it: a dismissed or parked item no longer puts the row in the inbox. The
    user's gestures: close is rule 1, park rule 7 and label rule 8; both keep
    the state a fresh fact (Q-B7). A linked tracker task, or the agent's
    reported ball, decides the ball only where the engine's own facts do not
    (Q-B20): after rule 9, before rule 10; the tracker's inbox items
    (``tracker_status``) are the queue's. ``catalog`` is the effective one
    (the engine's rule files, engine/rules/hub.py); the built-in by default."""
    return (catalog or _catalog()).derive(facts, now, last_change, stalled_hours)


@functools.cache
def _catalog() -> Any:
    # Imported here: the rules package reads this module's helpers.
    from lazyagent.engine.rules import builtin

    return builtin()


def fingerprint(facts: Facts, derived: Derived, user: list[Any] | None = None) -> str:
    """A hash of the facts that move a row across a rule boundary; free
    text (an agent's detail, the state) is left out, so a tool call does
    not reset the row's age. ``user``: a user rule matched, its id and the
    values of the facts it read (:func:`user_facts`), so a change of one of
    them moves the age too; the built-ins' inputs stay as they were."""
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
    if facts.owner_approval is not None:
        a = facts.owner_approval
        data.append(["owner_approval", a.sha, a.at, a.state, a.after])
    if facts.tracker is not None:
        data.append([facts.tracker.status, facts.tracker.level])
    if facts.gesture is not None:
        data.append(["gesture", facts.gesture["kind"], facts.gesture["at"]])
    report = facts.report
    if report is not None and (report.get("needs_user") or report.get("ball")):
        data.append(["report", report.get("needs_user"), report.get("ball"), report["at"]])
    if user is not None:
        data.append(["user_rule", *user])
    raw = json.dumps(data, sort_keys=True).encode()
    return hashlib.sha1(raw).hexdigest()[:12]


# A line whose rule reads these is the owner's review: Home offers Approve.
APPROVAL_FACTS = frozenset({"pr.commits_after_ready", "pr.commits_after_owner_approval"})

# Facts whose value moves on its own (the clock): never a fingerprint input.
_CLOCK_KINDS = ("duration", "time")


def user_facts(catalog: Any, ctx: Any, rule_id: str) -> list[Any] | None:
    """``[rule id, [fact, value] ...]`` when a user rule decided the row."""
    from lazyagent.engine.rules.catalog import plain
    from lazyagent.engine.rules.facts import FACTS

    rule = catalog.by_id.get(rule_id)
    if rule is None or rule.origin == "builtin":
        return None
    values = [[n, plain(ctx.fact(n))] for n in sorted(rule.facts)
              if FACTS[n].type.kind not in _CLOCK_KINDS]
    return [rule_id, values]


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
        "needs_you": {
            "count": len(facts.actionables),
            "top": facts.actionables[0]["id"],
            # Every open item, top first: a deep one under a quicker top
            # still counts in To sit down with (has_deep).
            "items": [{k: i.get(k) for k in ITEM_FIELDS} for i in facts.actionables],
        }
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
    if facts.review_request is not None:
        row["review_request"] = {k: facts.review_request[k] for k in REQUEST_FIELDS}
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
        if tracker.qa is not None:
            row["tracker"]["qa"] = tracker.qa
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


def has_deep(row: dict[str, Any]) -> bool:
    """An inbox row whose level is deep, or with a deep item under a
    quicker top one (a changes requested under a red CI)."""
    if row["zone"] != "inbox":
        return False
    if row["level"] == "deep":
        return True
    items = (row.get("needs_you") or {}).get("items") or ()
    return any(i.get("level") == "deep" for i in items)


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
        "deep": sum(has_deep(r) for r in rows),
        "in_flight": sum(
            r["zone"] == "in_flight" and r["ball"] != "done" and not r["stale"]
            for r in rows
        ),
        "done": sum(r["ball"] == "done" for r in rows),
        "stale": sum(r["stale"] and r["ball"] != "done" for r in rows),
        "parked": sum(r["zone"] == "parked" for r in rows),
        # Parked rows with open items: since R4c a park hides every item but
        # a Now one, which takes the row out of Parked, so this stays 0 (kept
        # for older clients).
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
        # worktree path → agent id → (its status, since when): by path, so a
        # row reads its own agents, not every agent's (O(rows) per pass).
        self._since: dict[str, dict[str, tuple[str, float]]] = {}
        # (path, agent) → [its turn, running now]: a turn starts when the
        # agent runs after it ended the last one (see :meth:`turn`).
        self._turns: dict[tuple[str, str], list[Any]] = {}
        # worktree path → when the engine last saw one of its agents change
        # status, start or go (not the first sighting when it starts).
        self._saw: dict[str, float] = {}
        self._primed = False
        self._commit_times: dict[str, float] = {}
        # A detached head's commit time, by sha (read once each).
        self._head_times: dict[str, float] = {}
        # (key, head) → the branch that head is already in, or None.
        self._merged: dict[tuple[str, str], str | None] = {}
        # The review requests' rows no worktree holds (key → Facts), from
        # the last pass: the review_request detector's places.
        self.remote: dict[str, Facts] = {}
        self._remote_keys: set[str] = set()
        # The feeders' items linked to a review request's row (last pass).
        self._request_links: set[str] = set()
        self._request_feeders: dict[str, dict[str, Any]] = {}
        self._facts_by_key: dict[str, Facts] = {}
        # Timeline: what each row looked like at the last pass, and who made
        # the next change (a gesture).
        self._tracked: dict[str, dict[str, Any]] = {}
        self._actors: dict[str, str] = {}
        self._last_rows: dict[str, dict[str, Any]] = {}
        self._handle: asyncio.TimerHandle | None = None
        # The clock at the last pass (flush), and the overlay's and the work
        # items' versions after it: status_list reuses its rows while they
        # hold and no recompute is pending, for COALESCE seconds.
        self._passed_at: float | None = None
        self._passed_versions: tuple[int, int] = (-1, -1)
        self._unsubscribe: Callable[[], None] | None = None
        self._ticks = 0
        self._started = False
        # key → the last pass's Derived (status_list {fields}).
        self._derived: dict[str, Derived] = {}
        # The clients that read the opt-in fields, and each row's fields at
        # the last pass while there is one (watch_fields).
        self._field_watchers: set[str] = set()
        self._fields: dict[str, dict[str, Any]] = {}

    @property
    def catalog(self) -> Any:
        """The effective rules catalog: the engine's rule files over the
        built-ins."""
        hub = getattr(self._engine, "rules", None)
        return hub.catalog if hub is not None else _catalog()

    def _rule_items(self, catalog: Any, base: list[Facts], now: float,
                    contexts: list[RowContext]) -> list[Any]:
        """The item rules' inbox items: per row, the item rule that is the
        first match below the locked prefix (catalog.first_item). Each row's
        context goes in ``contexts``, in ``base``'s order: the row's own when
        nothing opens on it."""
        from lazyagent.engine.actionables import RULE, Item

        if not catalog.item_rules:
            return []
        hours = self._engine.config.status.stalled_hours
        out = []
        for facts in base:
            seen = self._seen.get(facts.key)
            last_change = seen.changed_at if seen is not None else self._seed(facts, now)
            # The queue on, with nothing open yet: what the row would be.
            ctx = RowContext(evolve(facts, actionables=()), now, last_change, hours,
                             catalog.not_human)
            contexts.append(ctx)
            rule = catalog.first_item(ctx)
            if rule is None:
                continue
            d = catalog.outputs(rule, ctx)
            pr = facts.pr
            out.append(Item(
                f"Rule:{rule.id}:{facts.key}",
                RULE.name,
                d.state,
                label=f"#{pr.number} {pr.title}" if pr is not None else facts.branch or facts.key,
                detail=rule.says,
                work_item=facts.key,
                worktree_path=facts.worktree_path,
                pr={"number": pr.number, "title": pr.title, "url": pr.url}
                if pr is not None else None,
                level=d.level or "round",
                blocks_agent=d.blocks_agent,
                rule_id=rule.id,
                verb=rule.verb,
                title=rule.verb or "Rule",
                delegable=rule.can_delegate,
                passable=rule.can_pass,
            ))
        return out

    def explain(self, key: str) -> dict[str, Any] | None:
        """``rules_explain``: why ``key``'s row is what the last pass made it."""
        facts = self._facts_by_key.get(key)
        if facts is None:
            self.flush()
            facts = self._facts_by_key.get(key)
            if facts is None:
                return None
        now = self.clock()
        seen = self._seen.get(key)
        last_change = seen.changed_at if seen is not None else now
        catalog = self.catalog
        ctx = RowContext(facts, now, last_change, self._engine.config.status.stalled_hours,
                         catalog.not_human)
        out = catalog.explain(ctx)
        out["key"] = key
        if facts.ci_tests is not None and facts.ci_tests.red:
            # Mechanism A per failing check: what decided it, what was missing.
            out["ci"] = facts.ci_tests.to_dict()
        attestations = self._engine.attestations.of_key(key)
        if attestations:
            # Mechanism B: the attestations on the item (Why? lists the
            # standing ones with Revoke).
            out["attestations"] = attestations
        approval = self._engine.owner_approvals.of_key(key, facts.pr)
        if approval is not None:
            # The user's approval outside GitHub (R3d): Why? shows it with
            # Revoke.
            out["owner_approval"] = approval
        out["items"] = [
            {k: i.get(k) for k in ("id", "kind", "level", "state", "rule_id")}
            for i in facts.actionables or ()
        ]
        from lazyagent.engine.rules.doc import answer

        # One sentence for people, above the rules' detail (the Why? view).
        out["answer"] = answer(out)
        tests = facts.ci_tests
        if tests is not None and tests.required_source != "none":
            # R5a: which checks gate the merge, and where that came from.
            out["ci_required"] = tests.required_dict()
            if tests.red:
                out["answer"] = f"{out['answer']} {required_sentence(tests)}"
        skipped = self._engine.autodelegate.skipped.get(key)
        if skipped:
            # The skip list (engine/autodelegate.py): why no agent got it.
            out["auto_delegate"] = skipped
            out["answer"] = f"{out['answer']} {skipped[0].upper()}{skipped[1:]}."
        return out

    def _delegates(self, key: str, facts: Any, items: Any) -> list[str]:
        """The items Delegate (d) sends to the branch's first live agent."""
        if not items or facts is None:
            return []
        live = self._engine.agent_ids(facts.worktree_path) if facts.worktree_path else []
        return self._engine.delegations.delegates(
            key, [i["id"] for i in items], live[0] if live else None)

    def fields_of(self, key: str, catalog: Any = None) -> dict[str, Any]:
        """The opt-in row fields (Q-R20): rule_id, verb, action (a row the
        queue decides takes its top item's), and ci_note: ``CI red ·
        unrelated (base also red: 2 tests)`` while every failing check is
        explained (R4, Q-R23); delegable (its top item can go to the branch's
        agent), delegates (the items Delegate sends, I4i) and passable (the
        orchestrator can act on something there: one of its items, else its
        rule), the catalog's (I4h); approvable (R3d): see :meth:`approvable`;
        review_commits (I4j), on an approvable line whose rule reads the
        commits after ready or after the approval: what to review
        (engine/review_commits.py)."""
        catalog = catalog or self.catalog
        derived = self._derived.get(key)
        rule = catalog.by_id.get(derived.rule_id) if derived is not None else None
        facts = self._facts_by_key.get(key)
        tests = facts.ci_tests if facts is not None else None
        items = (facts.actionables or ()) if facts is not None else ()
        out: dict[str, Any] = {"rule_id": rule.id if rule is not None else None,
                               "verb": None, "action": None,
                               "ci_note": tests.note if tests is not None else None,
                               "delegable": bool(items) and items[0].get("delegable", False),
                               "delegates": self._delegates(key, facts, items),
                               "passable": any(i.get("passable", True) for i in items)
                               if items else rule is None or rule.can_pass}
        out["approvable"] = self.approvable(facts, rule, catalog) if facts is not None else None
        out["review_commits"] = (
            self._engine.review_commits.of(facts.pr, facts.owner_approval)
            if out["approvable"] is not None and self._reads(facts, rule, catalog) & APPROVAL_FACTS
            else None)
        if rule is None:
            return out
        out["verb"], out["action"] = rule.verb, rule.action
        if rule.id == "queue" and facts is not None and facts.actionables:
            top = facts.actionables[0]
            kind = catalog.kind.get(top["kind"])
            if top.get("rule_id"):
                item_rule = catalog.by_id.get(top["rule_id"])
                out["verb"] = top.get("verb")
                out["action"] = item_rule.action if item_rule is not None else None
            elif kind is not None:
                out["verb"], out["action"] = kind.verb, kind.action
        return out

    def approvable(self, facts: Facts, rule: Any, catalog: Any) -> dict[str, Any] | None:
        """``{sha, pr, approved}`` when Home offers Approve on the row (R3d):
        the user's own open PR in a worktree, a draft or a line whose rule
        (the top item's, under the queue) reads the commits after ready or
        after the owner's approval. ``sha`` is the head shown, ``approved``
        whether the user's approval is of it."""
        from lazyagent.engine.rules.facts import _mine

        pr = facts.pr
        if pr is None or pr.state != "OPEN" or not pr.head_sha or facts.worktree_path is None \
                or _mine(pr, facts.me) is not True:
            return None
        if not pr.is_draft and not self._reads(facts, rule, catalog) & APPROVAL_FACTS:
            return None
        approval = facts.owner_approval
        return {"sha": pr.head_sha, "pr": pr.number,
                "approved": approval is not None and approval.state == "head"}

    @staticmethod
    def _reads(facts: Facts, rule: Any, catalog: Any) -> frozenset[str]:
        """The facts the line's rule reads (the top item's, under the queue)."""
        if rule is not None and rule.id == "queue" and facts.actionables:
            top_rule = facts.actionables[0].get("rule_id")
            rule = catalog.by_id.get(top_rule) if top_rule else None
        return rule.facts if rule is not None else frozenset()

    def watch_fields(self, client: str, on: bool) -> None:
        """``client`` reads the opt-in fields (a v1 connection that asked
        status_list for them): a pass also emits ``status.updated`` for a
        row whose fields alone changed, and the connection adds them."""
        if on:
            self._field_watchers.add(client)
        else:
            self._field_watchers.discard(client)
            if not self._field_watchers:
                self._fields.clear()

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
        self._poll_review_requests()
        hub = getattr(self._engine, "rules", None)
        if hub is not None:
            # A rule file edited by hand (or allowed) applies on this pass.
            hub.check()
        self.flush()

    def _on_event(self, event: EngineEvent) -> None:
        if event.event == "agent.status":
            data = event.data if isinstance(event.data, dict) else {}
            path, agent_id = data.get("worktree_path"), data.get("agent_id")
            if isinstance(path, str) and isinstance(agent_id, str):
                # As it happens: a short run between two passes still counts.
                self.saw_status(path, agent_id, data.get("status", ""))
        if event.event == "worktrees.changed":
            self._worktrees_changed()
        elif event.event == "pr_info.updated":
            # A new PR head may already be in develop (cached per head).
            self._spawn(self.refresh_merged())
            # The commits since the user's approval (cached per head).
            self._spawn(self._engine.owner_approvals.refresh())
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

    def saw_status(self, path: str, agent_id: str, status: str) -> None:
        turn = self._turns.setdefault((path, agent_id), [0, False])
        if status in _RUNNING:
            if not turn[1]:
                turn[0], turn[1] = turn[0] + 1, True
        elif status in TURN_OVER:
            turn[1] = False

    def turn(self, path: str, agent_id: str) -> int:
        """Which turn ``agent_id`` is on (or last ended): it counts the
        runs that followed an ended turn, from the agent.status events and
        each pass. A report or an auto-hold made in one turn knows a new
        run by it. In memory only: it starts again with the engine."""
        state = self._engine.agent_states(path).get(agent_id)
        if state is not None:
            self.saw_status(path, agent_id, state.status.value)
        return self._turns.get((path, agent_id), [0])[0]

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
        self._poll_review_requests()

    def _poll_review_requests(self) -> None:
        """Search the review requests when due, at the PR poller's cadence,
        once it has resolved the repo; while GitHub fails, at its backoff
        (github_health.py)."""
        hub = getattr(self._engine, "review_requests", None)
        service = self._engine.worktree_service
        if hub is None or not service.github.due():
            return
        repo = service.github_repo
        if repo is None:
            return
        hub.health = service.github

        async def poll() -> None:
            if await hub.poll(repo.owner, repo.name):
                self.mark_dirty()

        self._spawn(poll())

    def _poll_actions(self) -> None:
        """Poll GitHub Actions when due (a new branch at once): the
        members' branches but the merge targets, one poll at a time, once
        GitHub answers (while it fails, at its backoff: github_health.py)."""
        hub = getattr(self._engine, "actions", None)
        if hub is None or not hub.enabled:
            return
        health = self._engine.worktree_service.github
        if health.installed_flag is None or not health.due():
            # The PR poller finds out whether gh is installed (its first tick).
            return
        hub.health = health
        targets = set(self.merge_targets()) | {"main", "master"}
        members = [wt for wt in self._members() if wt.branch and wt.branch not in targets]
        branches = [wt.branch for wt in members]
        # Open PRs whose checks are red: their heads are read on their own
        # (a busy repo's latest runs may not hold them).
        red_heads = {}
        for wt in members:
            pr = self._engine.get_pr_info(wt.path)
            if pr is not None and pr.state.upper() == "OPEN" and pr.head_sha and ci_state(pr) == "red":
                red_heads[wt.branch] = pr.head_sha
        hub.tests.config = config = self.catalog.ci
        hub.tests.attestations = self._engine.attestations
        bases: set[str] = set()
        if config.configured or config.gating_from_protection:
            hub.tests.targets = {wt.branch: self._target(wt) for wt in members}
            # The red PRs' bases: their required checks (R5a).
            bases = {t.base for b, t in hub.tests.targets.items() if b in red_heads}
        hub.reruns.on_rerun = self._infra_rerun

        async def poll() -> None:
            if await hub.poll(branches, red_heads=red_heads, bases=bases):
                self.mark_dirty()

        self._spawn(poll())

    def _infra_rerun(self, branch: str, run: Any, job: Any, reason: str, attempt: int) -> None:
        """The engine re-ran a job its runner lost (engine/infra_rerun.py):
        on the row's timeline."""
        wt = next((w for w in self._members() if w.branch == branch), None)
        if wt is None:
            return
        self._engine.overlay.add(work_item_key(repo_name(self._engine), wt), "ci", "engine",
                                 f"re-ran {job.name} ({reason}), attempt {attempt}",
                                 ref=job.url or run.html_url)
        self.mark_dirty()

    def _target(self, wt: Any) -> Any:
        """What mechanism A compares a branch with: its open PR's base
        (else the default branch) and its I6 project."""
        from lazyagent.engine.ci_tests import Target

        pr = self._engine.get_pr_info(wt.path)
        is_open = pr is not None and pr.state.upper() == "OPEN"
        base = ((pr.base_ref if is_open and pr.base_ref else None)
                or self._engine.config.default_branch or "main")
        feeder = self._feeder(wt.branch) or {}
        project = feeder.get("project")
        if isinstance(project, dict):
            project = project.get("id")
        return Target(wt.branch, base, pr.number if is_open else None,
                      project if isinstance(project, str) else None)

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
            # A detached worktree has no branch: its head commit's time.
            heads = {wt.head for wt in self._members() if not wt.branch and getattr(wt, "head", None)}
            for head in heads - self._head_times.keys():
                at = await asyncio.to_thread(commit_time, repo_root, head)
                if at is not None:
                    self._head_times[head] = at
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
        self._passed_at = self.clock()
        self._passed_versions = self._versions()
        events = self._engine.events
        watched = bool(self._field_watchers)
        catalog = self.catalog if watched else None
        for key, row in rows.items():
            changed = self._rows.get(key) != row
            if watched:
                extra = self.fields_of(key, catalog)
                changed |= self._fields.get(key) != extra
                self._fields[key] = extra
            if changed:
                events.emit(STATUS_UPDATED, {"row": row})
        for key in self._rows:
            if key not in rows:
                events.emit(STATUS_REMOVED, {"key": key})
                self._fields.pop(key, None)
        self._rows = rows
        return rows

    def _versions(self) -> tuple[int, int]:
        engine = self._engine
        return (getattr(engine.overlay, "version", -1), getattr(engine.work_items, "version", -1))

    def fresh(self) -> dict[str, dict[str, Any]]:
        """The rows, recomputed unless the last pass still stands: the
        projection runs (a change marks it dirty, see :meth:`mark_dirty`),
        no recompute is pending, the overlay and the work items did not
        change since, and the pass is under :data:`COALESCE` seconds old,
        the delay a change already waits for its pass."""
        passed = self._passed_at
        if (
            self._started
            and self._handle is None
            and passed is not None
            and 0 <= self.clock() - passed < COALESCE
            and self._versions() == self._passed_versions
        ):
            return self._rows
        return self.flush()

    def list(self, *, repo: str | None = None, include_done: bool = False) -> dict[str, Any]:
        """``status_list``: fresh rows (see :meth:`fresh`), sorted, with counts."""
        rows = self.fresh().values()
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
        result["github"] = self._engine.worktree_service.github.to_dict()
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
        self._engine.attestations.check(now)
        overlay.expire_reports(now)
        work_items.expire(now)
        rows: dict[str, dict[str, Any]] = {}
        facts_by_key: dict[str, Facts] = {}
        live_agents: set[tuple[str, str]] = set()
        catalog = self.catalog
        notes_ids = self._restorable_notes()
        # First the facts the queue does not decide, and the item rules'
        # items from them (the built-in Merge, a user rule with ball you).
        base: list[Facts] = []
        for facts in self._facts(now):
            gesture = overlay.gesture(facts, now)
            if gesture is None or gesture["kind"] == "label":
                # The feeder's Waiting lane parks it; the user's park or
                # close wins, and their label comes back once it ends.
                record = work_items.records.get((facts.feeder or {}).get("url", ""))
                gesture = work_items.waiting_gesture(record, facts, now) or gesture
            facts = evolve(
                facts,
                gesture=gesture,
                report=overlay.reports.get(facts.key),
                released=overlay.released_note(facts.key, now),
                spawned_by=overlay.spawned_by(facts.key),
            )
            base.append(evolve(facts, activity_at=self._activity(facts, notes_ids, now)))
        contexts: list[RowContext] = []
        self._engine.actionables.rule_items = self._rule_items(catalog, base, now, contexts)
        # Rows whose PR info the engine reads for the first time (its start,
        # the poller's first answer): their PR items were found, not seen
        # happen, and date from the last sign of work, never from now.
        self._engine.actionables.found_at = {
            f.key: f.activity_at
            for f in base
            if f.activity_at is not None and f.pr is not None
            and not getattr(self._seen.get(f.key), "pr", False)
        }
        queue: dict[str, list[dict[str, Any]]] = {}
        visible_items = self._engine.actionables.flush(now)
        claims.auto_hold(visible_items, now)
        delegations.check(now)
        for item in visible_items:
            if item["work_item"] is not None:
                queue.setdefault(item["work_item"], []).append(item)
        held = self._engine.first_responder.held_keys()
        for i, facts in enumerate(base):
            key = facts.key
            items = delegations.filter(key, claims.filter(key, queue.get(key, ())))
            claim, hold, delegation = claims.get(key), claims.get_hold(key), delegations.get(key)
            holder = "orchestrator" if key in held else None
            ctx = contexts[i] if contexts else None
            if items or ctx is None or holder is not None or not (
                claim is None and hold is None and delegation is None
            ):
                facts = evolve(
                    facts,
                    actionables=tuple(items),
                    claim=claim,
                    hold=hold,
                    delegation=delegation,
                    holder=holder,
                )
                ctx = None
            else:
                # Nothing open, claimed or held: the facts (and so the
                # values) _rule_items read, with the same last change.
                facts = ctx.facts
            facts_by_key[facts.key] = facts
            live_agents.update((facts.worktree_path or "", a.agent_id) for a in facts.agents)
            seen = self._seen.get(facts.key)
            last_change = seen.changed_at if seen is not None else self._seed(facts, now)
            if ctx is None:
                ctx = RowContext(facts, now, last_change, config.stalled_hours,
                                 catalog.not_human)
            derived = catalog.evaluate(ctx)
            fp = fingerprint(facts, derived, user_facts(catalog, ctx, derived.rule_id))
            if seen is None:
                seen = self._seen[facts.key] = _Seen(fp, last_change)
            elif seen.fingerprint != fp:
                # PR info read for the first time is the engine learning, not
                # the item changing: it dates from the last sign of work.
                learned = not seen.pr and facts.pr is not None and facts.activity_at is not None
                seen.fingerprint = fp
                seen.changed_at = min(facts.activity_at, now) if learned else now
            seen.pr = facts.pr is not None
            changed_at = seen.changed_at
            if derived.ball == "stalled" and facts.activity_at is not None:
                # A stalled row's age is how long nothing happened, not when
                # the engine last noticed a fact (its start, PR info coming).
                changed_at = min(facts.activity_at, now)
            if derived.rule == PARK_RULE and isinstance((facts.gesture or {}).get("at"), (int, float)):
                # Parked: since the user parked it.
                changed_at = min(facts.gesture["at"], now)
            stale = (
                not facts.agents
                and facts.pr is None
                and facts.pr_known
                and not facts.restorable
                and now - changed_at > config.stale_days * 86400
            )
            rows[facts.key] = build_row(facts, derived, changed_at, fp, stale=stale)
            if stale:
                # Stale rows fold at the end of In flight: what would be lost.
                statuses = getattr(self._engine, "git_statuses", None) or {}
                git_status = statuses.get(facts.worktree_path or "")
                if git_status is not None:
                    rows[facts.key]["dirty"] = git_status.dirty_count
            self._derived[facts.key] = derived
        self._projects(rows, now)
        self._track(rows, facts_by_key, now)
        self._close(rows, now)
        for key, row in rows.items():
            row["seen_at"] = overlay.seen_at(key)
            row["last_event_at"] = overlay.last_event_at(key)
            row["last_step"] = last_step(overlay.timeline.get(key, ()))
        for key in [k for k in self._seen if k not in rows]:
            del self._seen[key]
        for key in [k for k in self._derived if k not in rows]:
            del self._derived[key]
        for path, agents in list(self._since.items()):
            for agent_id in [a for a in agents if (path, a) not in live_agents]:
                del agents[agent_id]
            if not agents:
                del self._since[path]
        for slot in [s for s in self._turns if s not in live_agents]:
            del self._turns[slot]
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
        heads = {w.path: w.head for w in reversed(self._engine.worktrees)}
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
            ref = heads.get(facts.worktree_path)
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
        # A review request that went (reviewed, withdrawn, the PR closed)
        # leaves no done row: what was kept for it goes.
        for key in self._remote_keys - set(rows):
            overlay.forget(key)
            decisions.expire_item(key, "review_request_done")
        self._remote_keys = {k for k, r in rows.items() if r["worktree_path"] is None}
        known = set(overlay.gestures) | set(overlay.reports) | set(overlay.timeline)
        known |= {d["key"] for d in overlay.decisions.values() if d.get("state") == "open"}
        known |= set(self._last_rows)
        if self._requests_pending():
            # Until the first search answers, a review request's row kept
            # in the overlay (a label, its timeline) is not a removed one.
            known = set(self._last_rows)
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

    def _requests_pending(self) -> bool:
        """The review requests hub is on and has not answered nor failed
        yet (while gh may work)."""
        hub = getattr(self._engine, "review_requests", None)
        if hub is None or not hub.enabled or hub.known or hub.error is not None:
            return False
        return self._engine.worktree_service.github.available

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
            if work_items.waiting_active(record) or record["url"] in self._request_links:
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
        linked = self._request_feeders.get(work_item)
        if linked is not None:
            return linked["priority"]
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
        """When a row first seen last changed: its branch's last commit, its
        agents' last status change or its last sign of work, whichever is
        later; else now."""
        times = [a.since for a in facts.agents]
        if facts.commit_time is not None:
            times.append(facts.commit_time)
        if facts.review_request is not None:
            times.append(facts.review_request["requested_at"])
        if facts.activity_at is not None:
            times.append(facts.activity_at)
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
        if facts.review_request is not None:
            times.append(facts.review_request["requested_at"])
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
        hub = getattr(engine, "review_requests", None)
        me = hub.viewer if hub is not None else None
        known = bool(hub is not None and hub.enabled and hub.known)
        requests = list(hub.requests) if known else []
        out = []
        hub = getattr(engine, "actions", None)
        tests = hub.tests if hub is not None and hub.tests.active else None
        if tests is not None:
            tests.attestations = engine.attestations
        for wt in self._members():
            agents = self._agents(wt.path, now)
            branch = wt.branch
            key = work_item_key(repo, wt)
            pr = engine.get_pr_info(wt.path)
            head = getattr(wt, "head", None)
            merged = None
            if pr is not None and pr.state == "OPEN":
                merged = self._merged.get((key, pr.head_sha or head or ""))
            request = request_of(requests, pr, branch)
            if pr is None and request is not None:
                # The search knows the PR before the PR poller's first answer.
                pr = request_pr(request)
            feeder = self._feeder(branch)
            if request is not None and feeder is None:
                feeder = self._request_feeder(request)
            out.append(
                Facts(
                    key=key,
                    repo=repo,
                    branch=branch,
                    worktree_path=wt.path,
                    agents=agents,
                    pr=pr,
                    pr_known=pr is not None or engine.pr_known(wt.path),
                    commit_time=self._commit_times.get(branch) if branch
                    else self._head_times.get(head or ""),
                    restorable=restorable.get(wt.path, 0),
                    note=self._note(wt.path, agents),
                    tracker=engine.trackers.fact(branch),
                    feeder=feeder,
                    head=head,
                    merged_into=merged,
                    me=me,
                    review_request=request.to_dict() if request is not None else None,
                    requests_known=known,
                    ci_tests=self._ci_tests(tests, wt, pr) if tests is not None else None,
                    owner_approval=engine.owner_approvals.fact(key, pr),
                )
            )
        out += self._request_facts(repo, requests, out, me)
        self.remote = {f.key: f for f in out if f.worktree_path is None}
        # A feeder's item on a review request's row is started, branch or not.
        self._request_feeders = {f.key: f.feeder for f in out
                                 if f.review_request is not None and f.feeder is not None}
        self._request_links = {f["url"] for f in self._request_feeders.values()}
        return out

    def _request_feeder(self, request: Any) -> dict[str, Any] | None:
        record = self._engine.work_items.link_request(request.url, request.head, request.title)
        return self._engine.work_items.public(record) if record is not None else None

    def _request_facts(self, repo: str, requests: list[Any], local: list[Facts],
                       me: str | None) -> list[Facts]:
        """A row per review request no worktree holds: keyed by its head
        branch (``<repo>:#<number>`` for a fork's, or when a worktree has
        that branch for another PR), with the PR's title, author and URL,
        and the feeder's work item it links to (its URL, else the ticket
        key in its branch or title)."""
        taken = {f.review_request["number"] for f in local if f.review_request is not None}
        keys = {f.key for f in local}
        out = []
        for request in requests:
            if request.number in taken:
                continue
            key = f"{repo}:{request.head}"
            if request.cross_repo or key in keys:
                key = f"{repo}:#{request.number}"
            keys.add(key)
            out.append(Facts(
                key=key,
                repo=repo,
                branch=request.head,
                worktree_path=None,
                pr=request_pr(request),
                tracker=self._engine.trackers.fact(request.head),
                feeder=self._request_feeder(request),
                me=me,
                review_request=request.to_dict(),
                requests_known=True,
            ))
        return out

    def _ci_tests(self, tests: Any, wt: Any, pr: PrInfo | None) -> Any:
        from lazyagent.engine.actions import ci_verdict

        return tests.analysis(wt.branch, ci_verdict(self._engine, wt.branch, pr), pr,
                              tests.targets.get(wt.branch) or (self._target(wt) if wt.branch else None))

    def _feeder(self, branch: str | None) -> dict[str, Any] | None:
        record = self._engine.work_items.link(branch)
        return self._engine.work_items.public(record) if record is not None else None

    def _agents(self, path: str, now: float) -> tuple[AgentFact, ...]:
        engine = self._engine
        states = engine.agent_states(path)
        facts = []
        ids = list(states)
        ids += [a for a in engine.agent_ids(path) if a not in states]
        since = self._since.setdefault(path, {})
        for agent_id in ids:
            state = states.get(agent_id)
            status = state.status.value if state is not None else STARTING
            if status == AgentStatus.NO_AGENT.value:
                status = STARTING
            self.saw_status(path, agent_id, status)
            known = since.get(agent_id)
            if known is None or known[0] != status:
                if known is not None or self._primed:
                    # Seen happen, not found running when the engine started.
                    self._saw[path] = now
                known = since[agent_id] = (status, now)
            facts.append(
                AgentFact(agent_id, status, state.detail if state else "", known[1])
            )
        if any(a not in ids for a in since):
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


def request_of(requests: list[Any], pr: PrInfo | None, branch: str | None) -> Any:
    """The review request a worktree holds: its open PR's, else (PR info
    not read yet) the same-repo request on its branch."""
    if pr is not None:
        if pr.state != "OPEN":
            return None
        return next((r for r in requests if r.number == pr.number), None)
    if not branch:
        return None
    return next((r for r in requests if not r.cross_repo and r.head == branch), None)


def request_pr(request: Any) -> PrInfo:
    """What a review request's row knows of its PR: open, not a draft,
    its number, title, URL and author (no checks, review or mergeability:
    those facts read unknown or none)."""
    return PrInfo(
        number=request.number,
        title=request.title,
        state="OPEN",
        checks=[],
        url=request.url,
        review_decision=request.review,
        is_draft=False,
        author=request.author,
    )


def project_wake_text(project: str, record: dict[str, Any], allowed: bool) -> str:
    """The orchestrator's wake line for a startable project task."""
    head = (f"Project {project}: task {record['title']} ({record['url']}) can start: "
            "its dependencies are merged or in review (continue_while_review).")
    if allowed:
        return head + " The user is away: start it within the spawn caps, or say why not."
    return head + " The user is present: propose it to them; do not start it alone."


def commit_time(repo_root: str, sha: str) -> float | None:
    """``sha``'s committer time; None when git cannot tell."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "show", "-s", "--format=%ct", sha],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float(out.stdout.strip()) if out.returncode == 0 else None
    except ValueError:
        return None


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
    # A detached head reads as its worktree's directory, not "@6bf0c804d69b".
    path = row.get("worktree_path")
    name = row["branch"] or (os.path.basename(path.rstrip("/")) if path else "") \
        or row["key"].partition(":")[2]
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
    if row.get("ci_note"):
        state += f" · {row['ci_note']}"
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
