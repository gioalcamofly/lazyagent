from __future__ import annotations

import os
import re
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
    POSSIBLY_HANGED = "possibly_hanged"


@dataclass
class AgentState:
    status: AgentStatus = AgentStatus.NO_AGENT
    last_output_time: float | None = None  # time.monotonic()


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
