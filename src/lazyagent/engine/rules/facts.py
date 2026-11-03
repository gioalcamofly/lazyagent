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

The facts that later phases bring (``ci.attested`` …) join this list with
their phases (docs/design/
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
    open_set,
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
# Any name: lower case, as GitHub matches labels.
LABELS = open_set("label")
# Check ids (``<workflow file>/<job>``) and test ids, lower case.
CHECKS = open_set("check")
TESTS = open_set("test")
REQUIRED_SOURCE = enum("required_source", ("protection", "default_branch", "config", "none"))
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

    __slots__ = ("facts", "now", "last_change", "stalled_hours", "not_human", "_memo", "first")
    getters: dict[str, Getter] = {}

    def __init__(self, facts: Facts, now: float, last_change: float, stalled_hours: float,
                 not_human: frozenset[str] = frozenset()):
        self.facts = facts
        self.now = now
        self.last_change = last_change
        self.stalled_hours = stalled_hours
        # The catalog's [review] not_human (lower case).
        self.not_human = not_human
        self._memo: dict[str, Any] = {}
        # Catalog.first_item's scan on this context, for a match after it.
        self.first: tuple[int, bool] | None = None

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
        return "none" if c.facts.pr_known else UNKNOWN
    return _PR_STATES.get(pr.state, UNKNOWN)


def _pr_attr(attr: str) -> Getter:
    def get(c: RowContext) -> Any:
        pr = c.facts.pr
        if pr is None:
            return UNKNOWN
        value = getattr(pr, attr)
        return UNKNOWN if value is None else value

    return get


def _after_owner_approval(c: RowContext) -> Any:
    from lazyagent.engine.owner_approvals import after_owner_approval

    pr = c.facts.pr
    if pr is None:
        return UNKNOWN
    after = after_owner_approval(pr, c.facts.owner_approval)
    return UNKNOWN if after is None else after


def _owner_approved(c: RowContext) -> Any:
    approval = c.facts.owner_approval
    if c.facts.pr is None:
        return UNKNOWN
    if approval is None:
        return False
    return {"head": True, "counted": True, "lost": False}.get(approval.state, UNKNOWN)


def _owner_approved_sha(c: RowContext) -> Any:
    if c.facts.pr is None:
        return UNKNOWN
    approval = c.facts.owner_approval
    return approval.sha if approval is not None else ""


def _exists(pr: Any, known: bool) -> Any:
    """Unknown while GitHub fails and the poller never answered here."""
    return True if pr is not None else (False if known else UNKNOWN)


def _mine(pr: Any, me: str | None) -> Any:
    if pr is None or not pr.author or not me:
        return UNKNOWN
    return pr.author.casefold() == me.casefold()


def _requested(pr: Any, request: Any, known: bool) -> Any:
    if pr is None or not known:
        return UNKNOWN
    return request is not None


def _author(pr: Any) -> Any:
    return UNKNOWN if pr is None or not pr.author else pr.author


def _labels(c: RowContext) -> Any:
    pr = c.facts.pr
    if pr is None or pr.labels is None:
        return UNKNOWN
    return frozenset(name.lower() for name in pr.labels)


def _human_approvals(c: RowContext) -> Any:
    pr = c.facts.pr
    if pr is None or pr.approved_by is None:
        return UNKNOWN
    return sum(1 for login in pr.approved_by if login.lower() not in c.not_human)


def _item_human_approvals(c: ItemContext) -> Any:
    pr = c.place.pr
    if pr is None or pr.approved_by is None:
        return UNKNOWN
    return sum(1 for login in pr.approved_by if login.lower() not in c.place.not_human)


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


def _report_until(c: RowContext) -> str:
    """``14:05``: when the report's ball lapses (empty: none, or it never
    does)."""
    import time

    from lazyagent.engine.overlay import default_until

    r = c.facts.report
    if r is None or not r.get("ball"):
        return ""
    until = r.get("until")
    if not isinstance(until, (int, float)):
        until = default_until(r.get("ball"), r["at"])
    return time.strftime("%H:%M", time.localtime(until)) if until is not None else ""


def _held_what(c: RowContext) -> str:
    """What an agent that holds the ball folds (I4i): ``conflict``, ``CI
    red``, both joined with " + "; empty: nothing."""
    pr = c.facts.pr
    out = []
    if pr is not None and pr.state.upper() == "OPEN" and pr.mergeable.upper() == "CONFLICTING":
        out.append("conflict")
    if _row_ci_state(c) == "red":
        out.append("CI red")
    return " + ".join(out)


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
        return "none" if c.place.pr_known else UNKNOWN
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


# --- Mechanism A (proposal 0014 R4, engine/ci_tests.py) ---
# A context's CiTests: None when no check has tests_from, and then the
# facts read as before A: every failing check is related.

_RED_CONCLUSIONS = ("FAILURE", "ERROR", "TIMED_OUT")


def _pr_failing(pr: Any) -> set[str]:
    if pr is None or pr.state.upper() != "OPEN":
        return set()
    return {c.name.lower() for c in pr.checks if c.conclusion.upper() in _RED_CONCLUSIONS}


def _tests_of(c: Any) -> Any:
    return c.facts.ci_tests if isinstance(c, RowContext) else c.place.tests


def _red(c: Any) -> bool:
    """CI is red where the context looks: the PR's checks on a row, the
    PR's (made precise by Actions) or Actions' on an inbox item."""
    if isinstance(c, RowContext):
        return _row_ci_state(c) == "red"
    place = c.place
    if place.pr is not None and place.pr.state.upper() == "OPEN":
        from lazyagent.engine.actionables import pr_red

        if pr_red(place.pr, place.ci):
            return True
    return place.ci is not None and place.ci.state == "red"


def _related(c: Any) -> Any:
    t = _tests_of(c)
    if t is not None:
        return frozenset(x.lower() for x in t.related)
    if not _red(c):
        return frozenset()
    names = _pr_failing(c.facts.pr if isinstance(c, RowContext) else c.place.pr)
    if not isinstance(c, RowContext) and c.place.ci is not None and c.place.ci.state == "red":
        from lazyagent.engine.ci_tests import check_id

        for run in c.place.ci.red:
            names |= {check_id(run, j.name).lower() for j in run.failed_jobs or ()} or {
                (run.path or run.workflow).lower()}
    return frozenset(names or {"ci"})


def _gating(c: Any) -> Any:
    """R4c: the related checks that gate the merge; without a [ci.check]
    table every related check does."""
    t = _tests_of(c)
    if t is None:
        return _related(c)
    return frozenset(x.lower() for x in t.gating_failures)


def _policy(c: Any) -> Any:
    t = _tests_of(c)
    return frozenset() if t is None else frozenset(x.lower() for x in t.policy_failures)


def _only_nongating(c: Any) -> Any:
    t = _tests_of(c)
    return False if t is None else t.only_nongating


def _first_policy(c: Any) -> Any:
    t = _tests_of(c)
    first = t.first_policy() if t is not None else None
    return UNKNOWN if first is None else first


def _required_source(c: Any) -> Any:
    """R5a: where the gating comes from (branch protection, the default
    branch's, [ci.check] alone, none)."""
    t = _tests_of(c)
    return "none" if t is None else t.required_source


def _required_checks(c: Any) -> Any:
    t = _tests_of(c)
    required = t.required if t is not None else None
    return frozenset() if required is None else frozenset(n.lower() for n in required.names)


def _unrelated(c: Any) -> Any:
    t = _tests_of(c)
    if t is None:
        return UNKNOWN if _red(c) else False
    value = t.unrelated
    return UNKNOWN if value is None else value


def _unrelated_checks(c: Any) -> Any:
    t = _tests_of(c)
    return frozenset() if t is None else frozenset(x.lower() for x in t.unrelated_checks)


def _failing_tests(c: Any) -> Any:
    t = _tests_of(c)
    if t is None:
        return UNKNOWN if _red(c) else frozenset()
    if t.others or any(r.tests is None for r in t.checks):
        return UNKNOWN
    return frozenset(x.lower() for r in t.checks for x in r.tests or ())


def _base_failing_tests(c: Any) -> Any:
    t = _tests_of(c)
    if t is None:
        return UNKNOWN if _red(c) else frozenset()
    if t.others or any(r.base_tests is None for r in t.checks):
        return UNKNOWN
    return frozenset(x.lower() for r in t.checks for x in r.base_tests or ())


def _base_point(c: Any) -> Any:
    t = _tests_of(c)
    points = {r.point for r in t.checks} if t is not None else set()
    return points.pop() if len(points) == 1 and None not in points else UNKNOWN


def _unrelated_summary(c: Any) -> str:
    t = _tests_of(c)
    return "" if t is None else t.summary


def _base_fixed(c: Any) -> Any:
    t = _tests_of(c)
    if t is None:
        return frozenset()
    fixed = t.base_fixed
    return UNKNOWN if fixed is None else frozenset(x.lower() for x in fixed)


def _attested(c: Any) -> Any:
    t = _tests_of(c)
    return frozenset() if t is None else frozenset(x.lower() for x in t.attested)


def _attestation_pending(c: Any) -> Any:
    t = _tests_of(c)
    return frozenset() if t is None else frozenset(x.lower() for x in t.attestation_pending)


def _first_attestation_pending(c: Any) -> Any:
    t = _tests_of(c)
    first = t.first_pending() if t is not None else None
    return UNKNOWN if first is None else first


def _first_related(c: Any) -> Any:
    t = _tests_of(c)
    if t is not None and (t.unrelated_checks or t.gates
                          or any(r.new or r.attest_note for r in t.checks)):
        first = t.first_related()
        if first is not None:
            return first
    return c.fact("ci.first_failing")


def _first_base_fixed(c: Any) -> Any:
    t = _tests_of(c)
    first = t.first_fixed() if t is not None else None
    return UNKNOWN if first is None else first


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
_PRR = "PR poller (review query)"
_RQ = "review requests search"
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
_fact("agent.question", TEXT, "the question the agent's finished turn ends with", _REG,
      "on agent.* events", "free", item=_agent("question"))
_fact("place.is_orchestrator", BOOL, "the orchestrator's own place", _REG, "static",
      "free", item=_is_orchestrator)
_fact("place.agent_holds", BOOL,
      "an agent there has the ball (at work, or its report holds it)", _OV, "per pass", "free",
      item=lambda c: c.place.agent_holds)
_fact("agent.reported", BOOL, "the agent's report says where the ball is (it works on)",
      _OV, "per pass", "free",
      item=lambda c: c.agent is not None and c.place.reported == c.agent.agent_id)

_fact("pr.exists", BOOL, "there is a PR", _PRP, "≤ 60 s", "poll",
      row=lambda c: _exists(c.facts.pr, c.facts.pr_known),
      item=lambda c: _exists(c.place.pr, c.place.pr_known))
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
_fact("pr.draft", BOOL, "the PR is a draft", _PRP, "≤ 60 s", "poll",
      row=_pr_attr("is_draft"))
_fact("pr.author", TEXT, "the PR's author (login)", _PRP, "≤ 60 s", "poll",
      row=lambda c: _author(c.facts.pr), item=lambda c: _author(c.place.pr))
_fact("pr.mine", BOOL, "the PR is yours (its author is the user gh is logged in as)",
      "PR poller, gh's login", "≤ 60 s", "poll",
      row=lambda c: _mine(c.facts.pr, c.facts.me),
      item=lambda c: _mine(c.place.pr, c.place.me))
_fact("pr.review_requested_from_me", BOOL,
      "the PR asks you for a review (you, or a team of yours)", _RQ, "≤ 60 s", "poll",
      row=lambda c: _requested(c.facts.pr, c.facts.review_request, c.facts.requests_known),
      item=lambda c: _requested(c.place.pr, c.place.review_request, c.place.requests_known))
_fact("pr.labels", LABELS, "the PR's labels (lower case)", _PRR, "≤ 60 s", "extra",
      row=_labels)
_fact("pr.human_approvals", NUM,
      "the PR's approvals by people (latest review each; no apps)", _PRR, "≤ 60 s", "extra",
      row=_human_approvals, item=_item_human_approvals)
_fact("pr.commits_after_ready", NUM,
      "the pushes since the PR was marked ready (0: a draft; clean merges of the base left out)",
      _PRR, "≤ 60 s", "extra",
      row=_pr_attr("commits_after_ready"))
_fact("pr.base_merges_after_ready", NUM,
      "the clean merges of the base since the PR was marked ready (not counted in "
      "pr.commits_after_ready)", _PRR, "≤ 60 s", "extra",
      row=_pr_attr("base_merges_after_ready"))
_OA = "your approval (owner_approve, Approve, lazyagent pr approve) and git"
_fact("pr.commits_after_owner_approval", NUM,
      "the pushes since the newer of the PR's ready event and your approval of its head "
      "(0: a draft; clean merges of the base left out)", _OA + ", PR poller (review query)",
      "≤ 60 s", "extra", row=_after_owner_approval)
_fact("pr.owner_approved", BOOL,
      "you approved a commit still in the PR's history (lazyagent's record, not GitHub's)",
      _OA, "per head", "free", row=_owner_approved)
_fact("pr.owner_approved_sha", TEXT, "the commit you approved (empty: none)", _OA,
      "on approval", "free", row=_owner_approved_sha)
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

_CT = "JUnit artifacts or log parsers ([ci.check] tests_from)"
_fact("ci.failing_tests", TESTS, "the failing tests on the head (lower case)", _CT,
      "once per run attempt", "extra", row=_failing_tests, item=_failing_tests)
_fact("ci.base_point", TEXT, "the base commit the head is compared with", "git and Actions "
      "run metadata ([ci_unrelated] compare_to)", "per head", "extra", row=_base_point,
      item=_base_point)
_fact("ci.base_failing_tests", TESTS, "the tests failing on the base at that commit", _CT,
      "once per base sha", "extra", row=_base_failing_tests, item=_base_failing_tests)
_fact("ci.unrelated_checks", CHECKS, "the failing checks whose tests all fail on the base too",
      "mechanism A", "per pass", "extra", row=_unrelated_checks, item=_unrelated_checks)
_fact("ci.related_failures", CHECKS, "the failing checks not explained by the base",
      "failing checks minus ci.unrelated_checks", "per pass", "poll",
      row=_related, item=_related)
_GT = "[ci.check] gating and policy (R4c)"
_fact("ci.gating_failures", CHECKS, "the failing checks that gate the merge",
      "ci.related_failures minus non-gating and policy checks", "per pass", "poll",
      row=_gating, item=_gating)
_fact("ci.policy_failures", CHECKS, "the failing policy checks (a label or process check)",
      _GT, "per pass", "free", row=_policy, item=_policy)
_fact("ci.only_nongating", BOOL,
      "CI is red, but no failing check gates the merge (each is non-gating, policy or unrelated)",
      _GT, "per pass", "free", row=_only_nongating, item=_only_nongating)
_fact("ci.first_policy", TEXT, "the first failing policy check", _GT, "per pass", "free",
      row=_first_policy, item=_first_policy)
_RQ5 = "branch protection and rulesets of the PR's base (R5a)"
_fact("ci.required_source", REQUIRED_SOURCE,
      "where the checks that gate the merge come from: the base's protection, the default "
      "branch's (the base requires none), [ci.check] alone, or none",
      _RQ5, "≤ 6 h", "extra", row=_required_source, item=_required_source)
_fact("ci.required_checks", CHECKS, "the checks the PR's base requires (as the PR names them)",
      _RQ5, "≤ 6 h", "extra", row=_required_checks, item=_required_checks)
_fact("ci.unrelated", BOOL, "CI is red only for reasons unrelated to the branch",
      "mechanism A", "per pass", "extra", row=_unrelated, item=_unrelated)
_fact("ci.unrelated_summary", TEXT, "what explains the red (empty: nothing)", "mechanism A",
      "per pass", "free", row=_unrelated_summary, item=_unrelated_summary)
_fact("ci.base_fixed", CHECKS, "the unrelated checks the base has fixed since",
      "the base's newest run in the Actions runs", "≤ actions_poll_s", "extra",
      row=_base_fixed, item=_base_fixed)
_fact("ci.first_related", TEXT, "the first failing check not explained by the base",
      "mechanism A, else the first failing check", "per pass", "poll",
      row=_first_related, item=_first_related)
_fact("ci.first_base_fixed", TEXT, "the first check the base has fixed", "mechanism A",
      "≤ actions_poll_s", "extra", row=_first_base_fixed, item=_first_base_fixed)
_AT = "attestations (ci_attest, [ci] accept_attestation_for)"
_fact("ci.attested", CHECKS, "the failing checks an accepted attestation explains",
      _AT, "per pass", "free", row=_attested, item=_attested)
_fact("ci.attestation_pending", CHECKS,
      "the failing checks whose attestation waits for your confirmation", _AT, "per pass",
      "free", row=_attestation_pending, item=_attestation_pending)
_fact("ci.first_attestation_pending", TEXT,
      "the first check whose attestation waits for your confirmation", _AT, "per pass",
      "free", row=_first_attestation_pending, item=_first_attestation_pending)

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
_fact("delegation.held", TEXT, "why its letter waits (empty: sent)", "delegation",
      "per pass", "free", row=_delegation("held"))

_fact("report.needs_user", BOOL, "the agent reported it needs you", _OV, "per pass",
      "free", row=lambda c: bool(c.facts.report and c.facts.report.get("needs_user")))
_fact("report.ball", TEXT, "the agent's reported ball (empty: none)", _OV, "per pass",
      "free", row=_report("ball"))
_fact("report.note", TEXT, "the agent's report note (empty: none)", _OV, "per pass",
      "free", row=_report("note"))
_fact("report.agent", TEXT, "the agent that reported (empty: none)", _OV, "per pass",
      "free", row=_report("agent"))
_fact("report.until", TEXT, "when the reported ball lapses, HH:MM (empty: none)", _OV,
      "per pass", "free", row=_report_until)
_fact("held.what", TEXT,
      "what an agent holding the ball folds: conflict, CI red (empty: nothing)", _PRP,
      "≤ 60 s", "free", row=_held_what)


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


def _failing_tests_of(ctx: Any, name: Any) -> Any:
    """The failing tests of the checks ``name`` matches (a check id, a job
    name, or a job name without its matrix values)."""
    import fnmatch

    from lazyagent.engine.ci_tests import bare_job

    t = _tests_of(ctx)
    if t is None or name is UNKNOWN:
        return UNKNOWN
    name = str(name).lower()
    hits = [r for r in t.checks
            if any(fnmatch.fnmatchcase(x.lower(), name) for x in (r.check, r.job, bare_job(r.job)))]
    if not hits or any(r.tests is None for r in hits):
        return UNKNOWN
    return frozenset(x.lower() for r in hits for x in r.tests or ())


_FAILING_TESTS_OF = Function(
    "ci.failing_tests_of", ("literal:check",), TESTS, _failing_tests_of,
    doc='ci.failing_tests_of("<check>"): the failing tests of that check (a check id, a job '
        "name, or a job name without its matrix values; unknown with no test data)",
)

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
FUNCTIONS["ci.failing_tests_of"] = _FAILING_TESTS_OF
ROW_FUNCTIONS = {
    **FUNCTIONS,
    "agents.detail_of": Function(
        "agents.detail_of", ("literal:agent_status",), TEXT, _detail_of, AGENT_STATUS,
        doc='agents.detail_of("<status>"): the detail line of the first agent in that status',
    ),
}

# What the PR poll's second query fetches, only while a rule reads it.
REVIEW_FACTS = frozenset({"pr.labels", "pr.human_approvals", "pr.commits_after_ready",
                          "pr.base_merges_after_ready", "pr.commits_after_owner_approval"})

RowContext.getters = {n: f.row for n, f in FACTS.items() if f.row is not None}
ItemContext.getters = {n: f.item for n, f in FACTS.items() if f.item is not None}

ROW_ENV = Env({n: FACTS[n].type for n in RowContext.getters}, ROW_FUNCTIONS)
ITEM_ENV = Env({n: FACTS[n].type for n in ItemContext.getters}, FUNCTIONS)
SCOPES = {"row": ROW_ENV, "item": ITEM_ENV}
