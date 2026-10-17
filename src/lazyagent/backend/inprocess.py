"""The engine on the TUI's own loop, behind the :class:`Backend` interface.

Everything is a direct call; terminals are the engine's own sessions, so
the views render the engine's headless screen (no second emulator).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any

from lazyagent.agent_providers import ResumeMode
from lazyagent.backend import Backend, EventCallback
from lazyagent.engine.engine import Engine
from lazyagent.engine.registry import AgentRegistry
from lazyagent.engine.session import AgentSession, TerminalSession
from lazyagent.engine.types import AGENT_KIND, AgentInfo, ConfigView, CreatedWorktree
from lazyagent.models import GitStatus, PrInfo, WorktreeInfo

# Interest-set owner name of the TUI in the engine.
INTEREST_CLIENT = "tui"


class InProcessBackend(Backend):
    """Wraps an :class:`Engine` that runs in this process."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def for_repo(cls, repo_path: str | None) -> InProcessBackend:
        return cls(Engine(repo_path))

    # Lifecycle and events

    async def start(self) -> None:
        await self.engine.start()

    async def stop(self) -> None:
        await self.engine.stop()

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        return self.engine.subscribe(callback)

    @property
    def connected(self) -> bool:
        return True

    @property
    def v1_socket_path(self) -> str | None:
        return self.engine.v1_socket_path

    @property
    def engine_id(self) -> str | None:
        return self.engine.engine_id

    # State

    @property
    def repo_root(self) -> str:
        return self.engine.repo_root

    @property
    def worktrees(self) -> list[WorktreeInfo]:
        return self.engine.worktrees

    @property
    def git_statuses(self) -> dict[str, GitStatus]:
        return self.engine.git_statuses

    @git_statuses.setter
    def git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        self.engine.worktree_service.git_statuses = statuses

    @property
    def registry(self) -> AgentRegistry:
        return self.engine.registry

    def worktree(self, path: str) -> WorktreeInfo | None:
        return self.engine.worktree(path)

    def agent_ids(self, key: str) -> list[str]:
        return self.engine.agent_ids(key)

    def has_agent(self, key: str) -> bool:
        return self.engine.has_agent(key)

    def has_agent_session(self, key: str, agent_id: str = "") -> bool:
        return self.engine.get_session(key, agent_id) is not None

    def has_active_agents(self, path: str) -> bool:
        return self.engine.has_active_agents(path)

    def get_config(self) -> ConfigView:
        return self.engine.get_config()

    def get_pr_info(self, path: str) -> PrInfo | None:
        return self.engine.get_pr_info(path)

    def has_pr_info(self, path: str) -> bool:
        return self.engine.has_pr_info(path)

    # Worktrees, git, PRs

    def refresh_worktrees(self) -> None:
        self.engine.refresh_worktrees()

    def sweep_git_status(self) -> None:
        self.engine.sweep_git_status()

    async def refresh_git_status(self, paths: Collection[str]) -> None:
        await self.engine.refresh_git_status(paths)

    async def get_diff(self, path: str) -> str:
        return await self.engine.get_diff(path)

    async def refresh_pr_info(self, paths: Collection[str]) -> None:
        await self.engine.refresh_pr_info(paths)

    def set_interest(self, paths: Iterable[str]) -> None:
        self.engine.set_interest(paths, INTEREST_CLIENT)

    async def create_worktree(
        self,
        branch: str,
        base_branch: str | None = None,
        extra: str = "",
        *,
        env: Mapping[str, str] | None = None,
    ) -> CreatedWorktree:
        return await self.engine.create_worktree(branch, base_branch, extra, env=env)

    async def remove_worktree(
        self, path: str, force: bool = False, *, env: Mapping[str, str] | None = None
    ) -> str | None:
        return await self.engine.remove_worktree(path, force, env=env)

    # Agents and terminals

    async def spawn_agent(
        self,
        worktree_path: str,
        instruction: str | None = None,
        *,
        skip_permissions: bool = False,
        resume_mode: ResumeMode = ResumeMode.NEW,
        kind: str = AGENT_KIND,
        env: Mapping[str, str] | None = None,
        spawn_id: str = "",
    ) -> AgentInfo:
        return await self.engine.spawn_agent(
            worktree_path,
            instruction,
            skip_permissions=skip_permissions,
            resume_mode=resume_mode,
            kind=kind,
            env=env,
            spawn_id=spawn_id,
        )

    def stop_agent(self, key: str, agent_id: str = "") -> None:
        self.engine.stop_agent(key, agent_id)

    async def session(self, key: str, agent_id: str = "") -> AgentSession | None:
        return self.engine.get_session(key, agent_id)

    async def open_shell(
        self, worktree_path: str, *, env: Mapping[str, str] | None = None
    ) -> TerminalSession:
        return self.engine.open_shell(worktree_path, env=env)

    # Hosted UI

    def attach_ui(self) -> Callable[[], None]:
        return self.engine.attach_ui()

    def publish_view(self, view: dict[str, Any]) -> None:
        self.engine.publish_view(view)
