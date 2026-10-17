"""The plain values the engine hands its clients (standard library only).

They live apart from :mod:`lazyagent.engine.engine` so a client that talks
to a remote engine can build and use them without importing the engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

__all__ = [
    "AGENT_KIND",
    "ORCHESTRATOR_KIND",
    "SHELL_KIND",
    "AgentInfo",
    "ConfigView",
    "CreatedWorktree",
]

AGENT_KIND = "agent"
ORCHESTRATOR_KIND = "orchestrator"
SHELL_KIND = "shell"


@dataclass(frozen=True)
class AgentInfo:
    """Who an agent is; the ``agent.spawned`` payload."""

    worktree_path: str
    agent_id: str
    session_id: str
    label: str
    session_name: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ConfigView:
    """The read-only part of the config that clients need (modals)."""

    agent_provider: str
    default_branch: str
    has_custom_create: bool
    has_custom_remove: bool


@dataclass(frozen=True)
class CreatedWorktree:
    """A new worktree. With ``job_id`` set a custom command created it, and
    ``path`` is predicted from the naming convention."""

    path: str
    branch: str
    job_id: str | None = None
