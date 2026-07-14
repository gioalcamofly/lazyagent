from __future__ import annotations

from textual.message import Message

from lazyagent.models import AgentStatus, LifecycleConfidence


class AgentStatusChanged(Message):
    """Agent status transition (RUNNING, WAITING, POSSIBLY_HANGED).

    ``agent_id`` identifies which agent within the worktree changed; ``""``
    means the single/legacy agent (e.g. the orchestrator).
    """

    def __init__(
        self,
        worktree_path: str,
        status: AgentStatus,
        confidence: LifecycleConfidence = LifecycleConfidence.LOW,
        detail: str = "",
        agent_id: str = "",
    ) -> None:
        super().__init__()
        self.worktree_path = worktree_path
        self.status = status
        self.confidence = confidence
        self.detail = detail
        self.agent_id = agent_id


class AgentExited(Message):
    """Claude process disconnected from the pty."""

    def __init__(self, worktree_path: str, agent_id: str = "") -> None:
        super().__init__()
        self.worktree_path = worktree_path
        self.agent_id = agent_id
