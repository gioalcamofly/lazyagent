from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


@dataclass
class WorktreeInfo:
    """Information about a single git worktree."""

    path: str
    head: str
    branch: str | None
    is_main: bool
    is_bare: bool

    @property
    def name(self) -> str:
        """Directory name of the worktree."""
        return os.path.basename(self.path)

    @property
    def ticket_id(self) -> str | None:
        """Extract PROJ-XXXX ticket ID from branch name, if present."""
        if self.branch is None:
            return None
        match = re.search(r"PROJ-\d+", self.branch)
        return match.group(0) if match else None

    @property
    def display_label(self) -> str:
        """Short label for the worktree list.

        Uses ticket ID if available, otherwise branch name, falling back to
        directory name. Main worktree gets a (main) suffix.
        """
        if self.is_main:
            return "(main)"
        if self.ticket_id:
            return self.ticket_id
        if self.branch:
            return self.display_branch
        return self.name

    @property
    def display_branch(self) -> str:
        """Branch name, truncated to 40 characters if needed."""
        if self.branch is None:
            return "(detached)"
        if len(self.branch) > 40:
            return self.branch[:37] + "..."
        return self.branch

    @property
    def short_head(self) -> str:
        """First 12 characters of the commit hash."""
        return self.head[:12]


@dataclass
class GitStatus:
    """Git working tree status for a worktree."""

    dirty_count: int = 0
    ahead: int = 0
    behind: int = 0
    has_upstream: bool = False
    last_commit_subject: str = ""


class AgentStatus(Enum):
    NO_AGENT = "no_agent"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    POSSIBLY_HANGED = "possibly_hanged"


class LifecycleConfidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class AgentState:
    status: AgentStatus = AgentStatus.NO_AGENT
    confidence: LifecycleConfidence = LifecycleConfidence.LOW
    detail: str = ""
    last_output_time: float | None = None  # time.monotonic()
    agent_id: str = ""   # "" = legacy/primary (single-agent, e.g. orchestrator)
    label: str = ""      # human-facing tab label (e.g. "Agent 1")


# Priority order for rolling up several per-agent statuses into one summary
# status for a worktree. Earlier = "needs more attention", so it wins the
# roll-up. Used by both the sidebar summary and the IPC scalar agent_status.
_STATUS_PRIORITY: tuple[AgentStatus, ...] = (
    AgentStatus.WAITING_FOR_APPROVAL,
    AgentStatus.WAITING_FOR_USER,
    AgentStatus.WAITING,
    AgentStatus.POSSIBLY_HANGED,
    AgentStatus.FAILED,
    AgentStatus.RUNNING,
    AgentStatus.INTERRUPTED,
    AgentStatus.COMPLETED,
    AgentStatus.NO_AGENT,
)


def _status_rank(status: AgentStatus) -> int:
    try:
        return _STATUS_PRIORITY.index(status)
    except ValueError:
        return len(_STATUS_PRIORITY)


def rollup_status(statuses: Iterable[AgentStatus]) -> AgentStatus:
    """Collapse several agent statuses into a single summary status.

    Returns the highest-priority status (see ``_STATUS_PRIORITY``), or
    ``NO_AGENT`` when there are no agents.
    """
    ranked = sorted(statuses, key=_status_rank)
    return ranked[0] if ranked else AgentStatus.NO_AGENT


def dominant_state(states: Iterable[AgentState]) -> AgentState | None:
    """Return the agent state that wins the roll-up, or None if empty."""
    items = list(states)
    if not items:
        return None
    return min(items, key=lambda s: _status_rank(s.status))


@dataclass
class CiCheck:
    """A single CI status check from a pull request."""

    name: str
    status: str       # "COMPLETED", "IN_PROGRESS", "QUEUED", etc.
    conclusion: str   # "success", "failure", "neutral", etc.


@dataclass
class PrInfo:
    """Pull request metadata and CI check results."""

    number: int
    title: str
    state: str             # "OPEN", "CLOSED", "MERGED"
    checks: list[CiCheck]
    url: str = ""
    review_decision: str = ""   # "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", ""
    mergeable: str = ""         # "MERGEABLE", "CONFLICTING", "UNKNOWN", ""

    @property
    def checks_summary(self) -> str:
        """Human-readable summary like '3/5 passed'."""
        if not self.checks:
            return "no checks"
        passed = sum(
            1 for c in self.checks if c.conclusion.upper() == "SUCCESS"
        )
        total = len(self.checks)
        return f"{passed}/{total} passed"

    @property
    def overall_status(self) -> str:
        """Aggregate status: 'pass', 'fail', 'pending', or 'none'."""
        if not self.checks:
            return "none"
        if any(c.conclusion.upper() == "FAILURE" for c in self.checks):
            return "fail"
        if any(
            c.status.upper() != "COMPLETED" and c.conclusion.upper() != "SUCCESS"
            for c in self.checks
        ):
            return "pending"
        return "pass"
