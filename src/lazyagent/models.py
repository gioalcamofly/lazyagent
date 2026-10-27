from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

# The default ticket key in a branch name or a URL: ``ABC-123``.
# ``[status] ticket_pattern`` (config.py) may replace it.
TICKET_PATTERN = r"\b[A-Z][A-Z0-9]*-\d+"


def ticket_id(text: str | None, pattern: str | None = None) -> str | None:
    """The ticket key in ``text`` (a branch name, a URL): the pattern's first
    group when it has one, else the whole match; ``pattern`` None is
    :data:`TICKET_PATTERN`."""
    if not text:
        return None
    match = re.search(pattern or TICKET_PATTERN, text)
    if match is None:
        return None
    return match.group(1) if match.re.groups else match.group(0)


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
        """Extract a ticket ID like TASK-123 from the branch name, if present."""
        return ticket_id(self.branch)

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
    session_name: str = ""  # SendMessage address (Claude agents only)
    # The question its finished turn ends with, while idle (engine/questions.py).
    question: str = ""


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
    details_url: str = ""  # the check's page (CheckRun detailsUrl / StatusContext targetUrl)


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
    base_ref: str = ""          # the branch it merges into (baseRefName)
    head_sha: str = ""          # the PR's head commit (headRefOid)
    # Requested reviewers and reviews given; None when not fetched.
    review_requests: int | None = None
    reviews: int | None = None
    # isDraft and the label names; None when not fetched.
    is_draft: bool | None = None
    labels: list[str] | None = None
    author: str = ""            # the PR's author's login
    # The reviewers (no apps, not the author) whose latest review that is
    # not a comment approves, and the commits and force pushes since the
    # last ready-for-review event (ready_at; the PR's creation when it was
    # opened ready). None when not fetched: only the batched query has them.
    approved_by: list[str] | None = None
    commits_after_ready: int | None = None
    ready_at: str = ""
    # The merge commits and the force pushes (before, after) counted in
    # commits_after_ready, and how many of them were clean merges of the base
    # (engine/base_merges.py: taken out of commits_after_ready; None when
    # that is unknown).
    after_ready_merges: tuple[str, ...] = ()
    after_ready_pushes: tuple[tuple[str, str], ...] = ()
    base_merges_after_ready: int | None = None
    # Of those, the ones whose tree differs from the automatic merge only in
    # [review] generated_paths.
    generated_merges_after_ready: int = 0

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


@dataclass(frozen=True)
class WorkflowJob:
    """A job of a workflow run that did not succeed (``failure``,
    ``timed_out``), with the step that failed."""

    name: str
    step: str = ""
    url: str = ""
    conclusion: str = ""
    # When it started and ended (Unix time, 0: unknown) and its steps
    # other than GitHub's own ("Set up job", "Complete job", "Post …"):
    # what tells an aggregate job (one that waits on the others) apart.
    started_at: float = 0.0
    completed_at: float = 0.0
    own_steps: int = 0


@dataclass(frozen=True)
class WorkflowRun:
    """A GitHub Actions run (REST ``/actions/runs``)."""

    id: int
    workflow: str
    head_sha: str
    head_branch: str
    status: str            # "queued", "in_progress", "completed", ...
    conclusion: str        # "success", "failure", "timed_out", ...; "" while running
    html_url: str = ""
    created_at: float = 0.0
    attempt: int = 1
    workflow_id: int = 0
    # Fetched on demand (``/runs/{id}/jobs``); None until then.
    failed_jobs: tuple[WorkflowJob, ...] | None = None
    # The workflow file (``.github/workflows/ci.yml``) and what started the
    # run (``push``, ``pull_request``, ...): a check's identity and its
    # base point (engine/ci_tests.py).
    path: str = ""
    event: str = ""

    @property
    def red(self) -> bool:
        return self.status == "completed" and self.conclusion in ("failure", "timed_out")


def default_base(
    default_branch: str | None, worktrees: Iterable[WorktreeInfo]
) -> str:
    """The branch a new worktree starts from when none is given: the
    configured default branch when a worktree has it checked out, else the
    main worktree's branch (the built-in default, ``master``, is often not a
    branch at all), else the configured one, else ``main``. The host page's
    ``defaultBase`` (host/static/components/dialogs.js) follows the same
    rule."""
    worktrees = list(worktrees)
    if default_branch and any(wt.branch == default_branch for wt in worktrees):
        return default_branch
    main = next((wt.branch for wt in worktrees if wt.is_main and wt.branch), None)
    return main or default_branch or "main"
