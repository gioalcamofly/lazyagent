"""The fact registry (proposal 0014, "Facts"): the closed list of what a rule
may read about a work item, with its type, allowed values, an English
phrase, where it comes from, how fresh it is and what it costs.

A fact is read in one of two **scopes**:

- ``row``: a work item's row, from :class:`lazyagent.engine.status.Facts`
  (:class:`RowContext`);
- ``item``: an inbox kind's detector, from an actionables ``Place`` and, for
  the agent kinds, one of its agents (:class:`ItemContext`).

A fact has a getter per scope it exists in; a rule that names a fact its
scope lacks fails validation. Enum values are lower case whatever the
source spells. A getter returns :data:`UNKNOWN` when the fact is not there
(no PR for ``pr.review``, no tracker task for ``tracker.ball``); a known
absence (no gesture, no report) is a value. Facts are memoised per context,
so a pass reads each one once per row.

The facts that later phases bring (``ci.failing_tests``, ``ci.unrelated``
…) join this list with their phases (docs/design/
rules-plan.md); rules cannot define facts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lazyagent.engine.rules.lang import (
    BOOL,
    DURATION,
    NUM,
    RAW,
    TEXT,
    TIME,
    UNKNOWN,
    Env,
    Function,
    Type,
    enum,
    set_of,
)
from lazyagent.engine.trackers import QA_VALUES
from lazyagent.models import AgentStatus

if TYPE_CHECKING:
    from lazyagent.engine.status import Facts

STARTING = "starting"
AGENT_STATUSES = tuple(s.value for s in AgentStatus if s is not AgentStatus.NO_AGENT) + (
    STARTING,
)
_RUNNING = frozenset({AgentStatus.RUNNING.value, STARTING})
_IDLE = frozenset(
    {AgentStatus.WAITING.value, AgentStatus.COMPLETED.value, AgentStatus.INTERRUPTED.value}
)

BALLS = ("you", "agent", "others", "stalled", "done")
LEVEL_VALUES = ("now", "round", "deep")
BALL = enum("ball", BALLS)
LEVEL = enum("level", LEVEL_VALUES)
AGENT_STATUS = enum("agent_status", AGENT_STATUSES)
PR_STATE = enum("pr_state", ("open", "merged", "closed", "none"))
PR_REVIEW = enum("pr_review", ("approved", "changes_requested", "review_required", "none"))
PR_MERGEABLE = enum("pr_mergeable", ("mergeable", "conflicting", "unknown"))
CI_STATE = enum("ci_state", ("green", "red", "running", "none"))
CI_HEAD = enum("ci_head", ("green", "red", "running", "moved", "none"))
TRACKER_QA = enum("tracker_qa", QA_VALUES)
GESTURE_KIND = enum("gesture_kind", ("label", "park", "close", "none"))

_PR_STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
_PR_REVIEWS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "REVIEW_REQUIRED": "review_required",
    "": "none",
}
_PR_MERGEABLE = {"MERGEABLE": "mergeable", "CONFLICTING": "conflicting", "UNKNOWN": "unknown"}

Getter = Callable[[Any], Any]
_MISSING = object()


@dataclass(frozen=True)
class Fact:
    name: str
    type: Type
    # What the fact is, as the generated page names it ("the PR's review").
    phrase: str
    source: str
    freshness: str
    # free (in the engine's memory) · poll (comes with a poll the engine
    # runs) · extra (a new call, made only when an active rule reads it).
    cost: str
    row: Getter | None = None
    item: Getter | None = None

    @property
    def values(self) -> tuple[str, ...]:
        return self.type.values


FACTS: dict[str, Fact] = {}


def _fact(name: str, type_: Type, phrase: str, source: str, freshness: str, cost: str,
          *, row: Getter | None = None, item: Getter | None = None) -> None:
    FACTS[name] = Fact(name, type_, phrase, source, freshness, cost, row, item)


# ----------------------------------------------------------------------
# Contexts
# ----------------------------------------------------------------------


class RowContext:
    """One row's facts for one evaluation (memoised)."""

    __slots__ = ("facts", "now", "last_change", "stalled_hours", "_memo")
    getters: dict[str, Getter] = {}

    def __init__(self, facts: Facts, now: float, last_change: float, stalled_hours: float):
        self.facts = facts
        self.now = now
        self.last_change = last_change
        self.stalled_hours = stalled_hours
        self._memo: dict[str, Any] = {}

    def fact(self, name: str) -> Any:
        memo = self._memo
        value = memo.get(name, _MISSING)
        if value is _MISSING:
            value = memo[name] = self.getters[name](self)
        return value


class ItemContext:
    """A detector's place (and agent) for one evaluation (memoised)."""

    __slots__ = ("place", "agent", "now", "_memo")
    getters: dict[str, Getter] = {}

    def __init__(self, place: Any, agent: Any = None, now: float = 0.0) -> None:
        self.place = place
        self.agent = agent
        self.now = now
        self._memo: dict[str, Any] = {}

    def fact(self, name: str) -> Any:
        memo = self._memo
        value = memo.get(name, _MISSING)
        if value is _MISSING:
            value = memo[name] = self.getters[name](self)
        return value


# ----------------------------------------------------------------------
# Row getters (status.Facts)
# ----------------------------------------------------------------------


def _pr(c: RowContext) -> Any:
    return c.facts.pr


def _pr_enum(attr: str, table: dict[str, str]) -> Getter:
    def get(c: RowContext) -> Any:
        pr = c.facts.pr
        if pr is None:
            return UNKNOWN
        return table.get(getattr(pr, attr), UNKNOWN)

    return get


def _row_pr_state(c: RowContext) -> Any:
    pr = c.facts.pr
    if pr is None:
        return "none"
    return _PR_STATES.get(pr.state, UNKNOWN)


def _pr_attr(attr: str) -> Getter:
    def get(c: RowContext) -> Any:
        pr = c.facts.pr
        if pr is None:
            return UNKNOWN
        value = getattr(pr, attr)
        return UNKNOWN if value is None else value

    return get


def _row_ci_state(c: RowContext) -> str:
    from lazyagent.engine.status import ci_state

    return ci_state(c.facts.pr)


def _row_first_failing(c: RowContext) -> Any:
    from lazyagent.engine.status import _failing_check

    pr = c.facts.pr
    return UNKNOWN if pr is None else _failing_check(pr)


def _count(statuses: frozenset[str]) -> Getter:
    def get(c: RowContext) -> int:
        return sum(1 for a in c.facts.agents if a.status in statuses)

    return get


def _first_running(c: RowContext) -> Any:
    for a in c.facts.agents:
        if a.status in _RUNNING:
            return a.status
    return UNKNOWN


def _queue(c: RowContext) -> Any:
    return c.facts.actionables


def _items_top(key: str) -> Getter:
    def get(c: RowContext) -> Any:
        queue = c.facts.actionables
        return queue[0][key] if queue else UNKNOWN

    return get


def _items_count(c: RowContext) -> Any:
    queue = c.facts.actionables
    return UNKNOWN if queue is None else len(queue)


def _items_blocks(c: RowContext) -> Any:
    queue = c.facts.actionables
    return UNKNOWN if queue is None else any(i["blocks_agent"] for i in queue)


def _gesture(key: str, default: Any = None) -> Getter:
    def get(c: RowContext) -> Any:
        g = c.facts.gesture
        return UNKNOWN if g is None else g.get(key, default)

    return get


def _gesture_kind(c: RowContext) -> str:
    g = c.facts.gesture
    return "none" if not g else g.get("kind", "none")


def _idle_for(c: RowContext) -> float:
    f = c.facts
    last = f.activity_at if f.activity_at is not None else c.last_change
    return c.now - last


def _tracker(attr: str) -> Getter:
    def get(c: RowContext) -> Any:
        t = c.facts.tracker
        if t is None:
            return UNKNOWN
        value = getattr(t, attr)
        return UNKNOWN if value is None else value

    return get


def _feeder_fact(c: RowContext, name: str) -> Any:
    feeder = c.facts.feeder
    facts = feeder.get("facts") if feeder is not None else None
    return facts.get(name, UNKNOWN) if facts else UNKNOWN


def _tracker_linked(c: RowContext) -> bool:
    return c.facts.tracker is not None or bool((c.facts.feeder or {}).get("facts"))


def _tracker_status(c: RowContext) -> Any:
    """The adapter's, else a feeder's (Q-R15: the adapter wins)."""
    t = c.facts.tracker
    if t is not None:
        return t.status
    return _feeder_fact(c, "tracker.status")


def _tracker_qa(c: RowContext) -> Any:
    t = c.facts.tracker
    if t is not None and t.qa is not None:
        return t.qa
    return _feeder_fact(c, "tracker.qa")


def _delegation(key: str) -> Getter:
    def get(c: RowContext) -> Any:
        d = c.facts.delegation
        return UNKNOWN if d is None else d.get(key) or ""

    return get


def _report(key: str) -> Getter:
    def get(c: RowContext) -> Any:
        r = c.facts.report
        return (r.get(key) if r is not None else None) or ""

    return get


# ----------------------------------------------------------------------
# Item getters (actionables Place, AgentSeen)
# ----------------------------------------------------------------------


def _item_pr_enum(attr: str, table: dict[str, str]) -> Getter:
    def get(c: ItemContext) -> Any:
        pr = c.place.pr
        if pr is None:
            return UNKNOWN
        return table.get((getattr(pr, attr) or "").upper(), UNKNOWN)

    return get


def _item_pr_state(c: ItemContext) -> Any:
    pr = c.place.pr
    if pr is None:
        return "none"
    return _PR_STATES.get(pr.state.upper(), UNKNOWN)


def _item_ci_state(c: ItemContext) -> str:
    from lazyagent.engine.status import ci_state

    return ci_state(c.place.pr)


def _item_pr_red(c: ItemContext) -> Any:
    from lazyagent.engine.actionables import pr_red

    pr = c.place.pr
    return UNKNOWN if pr is None else pr_red(pr, c.place.ci)


def _item_first_failing(c: ItemContext) -> Any:
    """The first red Actions run's title on the head, else the PR's first
    failing check."""
    from lazyagent.engine.actions import run_title
    from lazyagent.engine.status import _failing_check

    ci = c.place.ci
    red = ci.red if ci is not None else ()
    if red:
        return run_title(red[0])
    pr = c.place.pr
    return UNKNOWN if pr is None else _failing_check(pr)


def _agent(attr: str) -> Getter:
    def get(c: ItemContext) -> Any:
        return UNKNOWN if c.agent is None else getattr(c.agent, attr)

    return get


def _is_orchestrator(c: ItemContext) -> bool:
    from lazyagent.engine.registry import ORCHESTRATOR_KEY

    return c.place.path == ORCHESTRATOR_KEY


# ----------------------------------------------------------------------
# The list
# ----------------------------------------------------------------------

_W = "worktree scan"
_REG = "agent registry"
_PRP = "PR poller"
_OV = "overlay"

_fact("item.key", TEXT, "the work item", _W, "on worktrees.changed", "free",
      row=lambda c: c.facts.key)
_fact("item.repo", TEXT, "the repo", _W, "on worktrees.changed", "free",
      row=lambda c: c.facts.repo)
_fact("item.branch", TEXT, "the branch", _W, "on worktrees.changed", "free",
      row=lambda c: UNKNOWN if c.facts.branch is None else c.facts.branch)
_fact("item.restorable", NUM, "the agents to restore", "restart manifest",
      "on restorable.changed", "free", row=lambda c: c.facts.restorable)
_fact("item.idle", DURATION, "the time since the last sign of work",
      "projection (activity, else the facts' last change)", "per pass", "free",
      row=_idle_for)
_fact("config.stalled_after", DURATION, "the stalled threshold",
      "config [status] stalled_hours", "on start", "free",
      row=lambda c: c.stalled_hours * 3600)
_fact("now", TIME, "now", "the engine's clock", "per pass", "free",
      row=lambda c: c.now, item=lambda c: c.now)

_fact("agents.count", NUM, "the agents", _REG, "on agent.* events", "free",
      row=lambda c: len(c.facts.agents), item=lambda c: len(c.place.agents))
_fact("agents.running", NUM, "the agents at work (running or starting)", _REG,
      "on agent.* events", "free", row=_count(_RUNNING),
      item=lambda c: sum(1 for a in c.place.agents if a.status in _RUNNING))
_fact("agents.idle", NUM, "the agents whose turn is over", _REG, "on agent.* events",
      "free", row=_count(_IDLE))
_fact("agents.statuses", set_of("agent_status", AGENT_STATUSES), "the agents' statuses",
      _REG, "on agent.* events", "free",
      row=lambda c: frozenset(a.status for a in c.facts.agents),
      item=lambda c: frozenset(a.status for a in c.place.agents))
_fact("agents.first_running", AGENT_STATUS, "the first working agent's status", _REG,
      "on agent.* events", "free", row=_first_running)
_fact("agent.id", TEXT, "the agent", _REG, "on agent.* events", "free",
      item=_agent("agent_id"))
_fact("agent.status", AGENT_STATUS, "the agent's status", _REG, "on agent.* events",
      "free", item=_agent("status"))
_fact("agent.detail", TEXT, "the agent's detail line", _REG, "on agent.* events", "free",
      item=_agent("detail"))
_fact("place.is_orchestrator", BOOL, "the orchestrator's own place", _REG, "static",
      "free", item=_is_orchestrator)
_fact("agent.reported", BOOL, "the agent's report says where the ball is (it works on)",
      _OV, "per pass", "free",
      item=lambda c: c.agent is not None and c.place.reported == c.agent.agent_id)

_fact("pr.exists", BOOL, "there is a PR", _PRP, "≤ 60 s", "poll",
      row=lambda c: c.facts.pr is not None, item=lambda c: c.place.pr is not None)
_fact("pr.state", PR_STATE, "the PR's state", _PRP, "≤ 60 s", "poll",
      row=_row_pr_state, item=_item_pr_state)
_fact("pr.review", PR_REVIEW, "the PR's review decision", _PRP, "≤ 60 s", "poll",
      row=_pr_enum("review_decision", _PR_REVIEWS),
      item=_item_pr_enum("review_decision", _PR_REVIEWS))
_fact("pr.mergeable", PR_MERGEABLE, "the PR's mergeability", _PRP, "≤ 60 s", "poll",
      row=_pr_enum("mergeable", _PR_MERGEABLE),
      item=_item_pr_enum("mergeable", _PR_MERGEABLE))
_fact("pr.review_requests", NUM, "the PR's requested reviewers", _PRP, "≤ 60 s", "poll",
      row=_pr_attr("review_requests"))
_fact("pr.reviews", NUM, "the PR's reviews", _PRP, "≤ 60 s", "poll",
      row=_pr_attr("reviews"))
_fact("pr.number", NUM, "the PR's number", _PRP, "≤ 60 s", "poll",
      row=_pr_attr("number"))
_fact("pr.base", TEXT, "the PR's base branch", _PRP, "≤ 60 s", "poll",
      row=_pr_attr("base_ref"))
_fact("pr.head_sha", TEXT, "the PR's head commit", _PRP, "≤ 60 s", "poll",
      row=_pr_attr("head_sha"))
_fact("pr.merged_into", TEXT, "the branch that already holds the head (empty: none)",
      "git (merged_into)", "per head", "free",
      row=lambda c: c.facts.merged_into or "")

_fact("ci.state", CI_STATE, "CI on the PR", "PR checks", "≤ 60 s", "poll",
      row=_row_ci_state, item=_item_ci_state)
_fact("ci.first_failing", TEXT, "the first failing check (empty: none)",
      "Actions runs on the head, else PR checks", "≤ 60 s", "poll",
      row=_row_first_failing, item=_item_first_failing)
_fact("ci.pr_red", BOOL, "the PR's checks are red on its head",
      "PR checks, made precise by Actions", "≤ actions_poll_s", "poll", item=_item_pr_red)
_fact("ci.head", CI_HEAD, "Actions on the head", "Actions runs (I7)", "≤ actions_poll_s",
      "poll", item=lambda c: c.place.ci.state if c.place.ci is not None else "none")

_fact("queue.enabled", BOOL, "the inbox queue is on", "ActionableQueue", "static", "free",
      row=lambda c: c.facts.actionables is not None)
_fact("items.count", NUM, "the open inbox items on the work item", "ActionableQueue",
      "per pass", "free", row=_items_count)
_fact("items.top.kind", TEXT, "the top inbox item's kind", "ActionableQueue", "per pass",
      "free", row=_items_top("kind"))
_fact("items.top.level", LEVEL, "the top inbox item's level", "ActionableQueue",
      "per pass", "free", row=_items_top("level"))
_fact("items.top.state", TEXT, "the top inbox item's state", "ActionableQueue",
      "per pass", "free", row=_items_top("state"))
_fact("items.blocks_agent", BOOL, "an open inbox item blocks an agent", "ActionableQueue",
      "per pass", "free", row=_items_blocks)

_fact("tracker.linked", BOOL, "a tracker task is linked",
      "tracker hub, else a feeder's facts", "poll_s", "poll", row=_tracker_linked)
_fact("tracker.status", TEXT, "the task's status",
      "tracker adapter, else a feeder's facts", "poll_s", "poll", row=_tracker_status)
_fact("tracker.qa", TRACKER_QA,
      "the task's QA (unknown: no task, or nothing maps it)",
      "tracker adapter [<adapter>.qa] mapping, else a feeder's facts", "poll_s", "poll",
      row=_tracker_qa)
_fact("tracker.state", TEXT, "the task's mapped state", "tracker adapter mapping",
      "poll_s", "poll", row=_tracker("state"))
_fact("tracker.ball", BALL, "the task's mapped ball", "tracker adapter mapping",
      "poll_s", "poll", row=_tracker("ball"))
_fact("tracker.level", LEVEL, "the task's mapped level", "tracker adapter mapping",
      "poll_s", "poll", row=_tracker("level"))
_fact("tracker.stale", BOOL, "the tracker's last call failed", "tracker hub", "poll_s",
      "free", row=_tracker("stale"))

_fact("gesture.kind", GESTURE_KIND, "your gesture", _OV, "per pass", "free",
      row=_gesture_kind)
_fact("gesture.ball", BALL, "your label's ball", _OV, "per pass", "free",
      row=_gesture("ball"))
_fact("gesture.text", TEXT, "your gesture's text", _OV, "per pass", "free",
      row=_gesture("text"))
_fact("gesture.until", RAW, "your park's end (a time or an event)", _OV, "per pass",
      "free", row=_gesture("until"))
_fact("gesture.how", TEXT, "how you closed it", _OV, "per pass", "free",
      row=_gesture("how"))
_fact("gesture.waiting", BOOL, "a feeder's Waiting lane", _OV, "per pass", "free",
      row=lambda c: UNKNOWN if c.facts.gesture is None
      else bool(c.facts.gesture.get("waiting")))
_fact("gesture.source", TEXT, "the feeder that parked it", _OV, "per pass", "free",
      row=_gesture("source", "feeder"))

_fact("claim.active", BOOL, "you passed it to the orchestrator", "claims", "per pass",
      "free", row=lambda c: c.facts.claim is not None)
_fact("hold.active", BOOL, "the orchestrator holds it", "claims", "per pass", "free",
      row=lambda c: c.facts.hold is not None)
_fact("hold.note", TEXT, "the orchestrator's hold note (empty: none)", "claims",
      "per pass", "free",
      row=lambda c: UNKNOWN if c.facts.hold is None else c.facts.hold.get("note") or "")
_fact("holder", TEXT, "who holds a routed item here (empty: nobody)", "first responder",
      "per pass", "free", row=lambda c: c.facts.holder or "")
_fact("delegation.phase", TEXT, "the delegation's phase", "delegation", "per pass",
      "free", row=_delegation("phase"))
_fact("delegation.agent", TEXT, "the agent it was delegated to", "delegation",
      "per pass", "free", row=_delegation("agent"))
_fact("delegation.fact", TEXT, "what the agent is fixing (empty: unsaid)", "delegation",
      "per pass", "free", row=_delegation("fact"))

_fact("report.needs_user", BOOL, "the agent reported it needs you", _OV, "per pass",
      "free", row=lambda c: bool(c.facts.report and c.facts.report.get("needs_user")))
_fact("report.ball", TEXT, "the agent's reported ball (empty: none)", _OV, "per pass",
      "free", row=_report("ball"))
_fact("report.note", TEXT, "the agent's report note (empty: none)", _OV, "per pass",
      "free", row=_report("note"))
_fact("report.agent", TEXT, "the agent that reported (empty: none)", _OV, "per pass",
      "free", row=_report("agent"))


# ----------------------------------------------------------------------
# Functions (the closed list)
# ----------------------------------------------------------------------


def _text(value: Any) -> str:
    return "" if value is UNKNOWN or value is None else str(value)


def _with_fn(ctx: Any, prefix: Any, detail: Any) -> Any:
    from lazyagent.engine.status import _with

    if prefix is UNKNOWN:
        return UNKNOWN
    return _with(prefix, _text(detail))


def _clip_fn(ctx: Any, text: Any, words: Any) -> Any:
    from lazyagent.engine.status import clip_words

    if text is UNKNOWN or words is UNKNOWN:
        return UNKNOWN
    return clip_words(text, words)


def _park_text_fn(ctx: Any, until: Any, reason: Any) -> Any:
    from lazyagent.engine.status import park_text

    if until is UNKNOWN:
        return UNKNOWN
    return park_text(until, None if reason is UNKNOWN else reason, ctx.now)


def _waiting_text_fn(ctx: Any, source: Any, reason: Any) -> Any:
    from lazyagent.engine.work_items import waiting_text

    if source is UNKNOWN:
        return UNKNOWN
    return waiting_text(source, None if reason is UNKNOWN else reason)


def _detail_of(ctx: Any, status: Any) -> Any:
    for a in ctx.facts.agents:
        if a.status == status:
            return a.detail
    return UNKNOWN


FUNCTIONS: dict[str, Function] = {
    f.name: f
    for f in (
        Function("with", ("text", "any"), TEXT, _with_fn,
                 doc='with("CI red", detail): "CI red: <detail>", the prefix alone '
                     "when the detail is empty or unknown; 5 words"),
        Function("clip", ("text", "num"), TEXT, _clip_fn, doc="clip(text, n): n words"),
        Function("park_text", ("raw", "any"), TEXT, _park_text_fn,
                 doc='park_text(until, reason): "parked until 08:00", "parked: <reason>"'),
        Function("waiting_text", ("text", "any"), TEXT, _waiting_text_fn,
                 doc='waiting_text(source, reason): "waiting: <reason>", "waiting (<source>)"'),
    )
}
ROW_FUNCTIONS = {
    **FUNCTIONS,
    "agents.detail_of": Function(
        "agents.detail_of", ("literal:agent_status",), TEXT, _detail_of, AGENT_STATUS,
        doc='agents.detail_of("<status>"): the detail line of the first agent in that status',
    ),
}

RowContext.getters = {n: f.row for n, f in FACTS.items() if f.row is not None}
ItemContext.getters = {n: f.item for n, f in FACTS.items() if f.item is not None}

ROW_ENV = Env({n: FACTS[n].type for n in RowContext.getters}, ROW_FUNCTIONS)
ITEM_ENV = Env({n: FACTS[n].type for n in ItemContext.getters}, FUNCTIONS)
SCOPES = {"row": ROW_ENV, "item": ITEM_ENV}
