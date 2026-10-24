"""The old ``derive()`` (engine/status.py before proposal 0014 R1), kept
verbatim as the oracle for the built-in rules catalog (docs/design/rules-plan.md
§4). Do not edit the two functions: test_oracle.py asserts the catalog gives
the same :class:`Derived` for every case they see.

The constants the functions read are copied too, so this file does not move
when the engine's own change.
"""

from __future__ import annotations

from dataclasses import replace

from lazyagent.engine.status import (
    Derived,
    Facts,
    _failing_check,
    _with,
    ci_state,
    clip_words,
    park_text,
)
from lazyagent.engine.work_items import waiting_text
from lazyagent.models import AgentStatus

STARTING = "starting"
_RUNNING = frozenset({AgentStatus.RUNNING.value, STARTING})
_IDLE = frozenset(
    {
        AgentStatus.WAITING.value,
        AgentStatus.COMPLETED.value,
        AgentStatus.INTERRUPTED.value,
    }
)
_NEEDS_YOU = (
    (AgentStatus.WAITING_FOR_APPROVAL.value, "now", "approve", True),
    (AgentStatus.WAITING_FOR_USER.value, "now", "answer", True),
    (AgentStatus.FAILED.value, "round", "agent failed", False),
    (AgentStatus.POSSIBLY_HANGED.value, "round", "agent may hang", False),
)
CLAIM_RULE = 13
CLAIM_STATE = "orchestrator · looking"
HELD_RULE = 14
HELD_STATE = "orchestrator · looking"
HOLD_RULE = 15
DELEGATED_RULE = 16
PARK_RULE = 7


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
    if open_pr and facts.merged_into:
        # Closing the PR stays the user's (never alone, engine/orchestrator.py).
        return Derived(1, "done", f"already merged into {facts.merged_into}",
                       suggestion=f"close PR #{pr.number}: its head is in {facts.merged_into}")
    if gesture.get("kind") == "close":
        how = "dropped" if gesture.get("how") == "dropped" else "closed"
        return Derived(1, "done", f"{how} by you")
    if gesture.get("kind") == "park":
        # The whole work item: its row and its inbox items wait in Parked.
        before = derive(replace(facts, gesture=None), now, last_change,
                        stalled_hours=stalled_hours)
        if gesture.get("waiting"):
            # A feeder's Waiting lane (work_items.py): parked the same way.
            state = waiting_text(gesture.get("source", "feeder"), gesture.get("text"))
        else:
            state = park_text(gesture.get("until"), gesture.get("text"), now)
        return Derived(PARK_RULE, before.ball, state)

    queue = facts.actionables
    if queue:
        # With a pass or a hold, only what is still the user's is left here
        # (tool approvals, and questions and briefs not routed: claims.py).
        top = queue[0]
        blocks = any(i["blocks_agent"] for i in queue)
        return Derived(2, "you", top["state"], top["level"], blocks)
    if facts.claim is not None:
        return Derived(CLAIM_RULE, "agent", CLAIM_STATE)
    hold = facts.hold
    if hold is not None:
        note = hold.get("note") or "holding"
        return Derived(HOLD_RULE, "agent", clip_words(f"orchestrator · {note}", 8))
    if facts.holder == "orchestrator":
        return Derived(HELD_RULE, "agent", HELD_STATE)
    delegation = facts.delegation
    if delegation is not None and delegation.get("phase") == "on_it":
        fact = delegation.get("fact") or "it"
        return Derived(DELEGATED_RULE, "agent",
                       clip_words(f"agent {delegation['agent']} · fixing {fact}", 8))
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
        # Not the agent's tool line ("Bash done"): that is noise on a row.
        state = "starting" if running[0].status == STARTING else "working"
        return Derived(9, "agent", state)
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
        if pr.review_requests == 0 and pr.reviews == 0:
            # Nobody was asked: that is the user's to do, not others' to wait on.
            return Derived(10, "you", "PR has no reviewer", "round")
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
    last = facts.activity_at if facts.activity_at is not None else last_change
    if now - last > stalled_hours * 3600:
        return Derived(11, "stalled", "no activity")
    if open_pr and pr.review_decision == "APPROVED":
        return Derived(12, "stalled", "approved, not mergeable")
    return Derived(12, "stalled", "no agent")
