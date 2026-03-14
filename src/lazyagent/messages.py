from __future__ import annotations

from textual.message import Message

from lazyagent.models import AgentStatus, LifecycleConfidence


class AgentStatusChanged(Message):
    """Agent status transition (RUNNING, WAITING, POSSIBLY_HANGED)."""

    def __init__(
        self,
        worktree_path: str,
        status: AgentStatus,
        confidence: LifecycleConfidence = LifecycleConfidence.LOW,
        detail: str = "",
    ) -> None:
        super().__init__()
        self.worktree_path = worktree_path
        self.status = status
        self.confidence = confidence
        self.detail = detail


class AgentExited(Message):
    """Claude process disconnected from the pty."""

    def __init__(self, worktree_path: str) -> None:
        super().__init__()
        self.worktree_path = worktree_path
