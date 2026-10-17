"""How the TUI reaches the engine: one :class:`Backend` interface, two
implementations.

- :class:`~lazyagent.backend.inprocess.InProcessBackend` wraps an
  :class:`~lazyagent.engine.engine.Engine` running on the TUI's own loop
  (the default).
- :class:`~lazyagent.backend.remote.RemoteBackend` talks to an engine in
  another process over its protocol v1 socket (``LAZYAGENT_ENGINE=remote``).

The app and its widgets only ever see a :class:`Backend`. State reads are
synchronous (the remote backend answers them from a store it builds from
``get_sidebar_state`` and the engine's events); commands that need an
answer are coroutines; fire-and-forget commands are plain calls. A
subscriber gets :class:`~lazyagent.engine.events.EngineEvent` objects after
the backend's state already reflects them, exactly as with the engine
itself, plus the backend's own :data:`BACKEND_CONNECTED` /
:data:`BACKEND_DISCONNECTED` events.

Terminals are handed out as objects the views render and type into: the
engine's :class:`~lazyagent.engine.session.TerminalSession` in-process, a
:class:`~lazyagent.backend.remote_session.RemoteSession` (its own display
screen, fed by the stream) otherwise. Both offer what
:class:`~lazyagent.widgets.scrollable_terminal.ScrollableTerminal` uses:
``screen``, ``rows`` / ``cols``, ``running``, the input modes, ``attach``,
``flush``, ``write``, ``resize``, ``frame_input`` and ``send_input``.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from lazyagent.agent_providers import ResumeMode
from lazyagent.engine.types import AGENT_KIND

if TYPE_CHECKING:
    from lazyagent.engine.events import EngineEvent
    from lazyagent.engine.registry import AgentRegistry
    from lazyagent.engine.types import AgentInfo, ConfigView, CreatedWorktree
    from lazyagent.models import AgentState, GitStatus, PrInfo, WorktreeInfo

__all__ = [
    "BACKEND_CONNECTED",
    "BACKEND_DISCONNECTED",
    "AUTOSTART_ENV",
    "ENGINE_ENV",
    "ENGINE_SOCKET_ENV",
    "Backend",
    "EngineUnavailable",
    "make_backend",
]

#: ``remote`` makes the TUI talk to an engine over its socket.
ENGINE_ENV = "LAZYAGENT_ENGINE"
#: The socket of that engine; found from the repo root when unset.
ENGINE_SOCKET_ENV = "LAZYAGENT_ENGINE_SOCKET"
#: ``0`` keeps a remote TUI from starting an engine when it finds none.
AUTOSTART_ENV = "LAZYAGENT_ENGINE_AUTOSTART"

#: The backend reached the engine (again): ``{"first", "engine_changed"}``.
BACKEND_CONNECTED = "backend.connected"
#: The backend lost the engine and is trying again: ``{"reason"}``.
BACKEND_DISCONNECTED = "backend.disconnected"

EventCallback = Callable[["EngineEvent"], None]


class EngineUnavailable(ConnectionError):
    """The engine cannot be reached right now."""


class Backend(ABC):
    """Everything the TUI asks of the engine."""

    #: Whether the engine lives in another process (agents outlive the TUI).
    remote = False

    # ------------------------------------------------------------------
    # Lifecycle and events
    # ------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Start (in-process) or connect to (remote) the engine."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the engine and everything it runs (in-process), or only
        disconnect from it (remote: the agents keep running)."""

    @abstractmethod
    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        """Call ``callback`` with every event; returns the unsubscriber."""

    async def sync(self) -> None:
        """Return once every event the engine has emitted so far has reached
        the subscribers (immediately in-process)."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether commands can reach the engine now."""

    @property
    @abstractmethod
    def v1_socket_path(self) -> str | None:
        """The engine's protocol v1 socket, if it has one."""

    @property
    @abstractmethod
    def engine_id(self) -> str | None: ...

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def repo_root(self) -> str: ...

    @property
    @abstractmethod
    def worktrees(self) -> list[WorktreeInfo]: ...

    @property
    @abstractmethod
    def git_statuses(self) -> dict[str, GitStatus]: ...

    @git_statuses.setter
    @abstractmethod
    def git_statuses(self, statuses: dict[str, GitStatus]) -> None: ...

    @property
    @abstractmethod
    def registry(self) -> AgentRegistry:
        """Agent states, labels and session names."""

    def worktree(self, path: str) -> WorktreeInfo | None:
        return next((wt for wt in self.worktrees if wt.path == path), None)

    def agent_states(self, key: str) -> dict[str, AgentState]:
        return self.registry.agents(key)

    @abstractmethod
    def agent_ids(self, key: str) -> list[str]:
        """The agents the engine runs for ``key``, in spawn order."""

    @abstractmethod
    def has_agent(self, key: str) -> bool:
        """Whether ``key`` (a worktree path or the orchestrator) has a live
        agent."""

    def has_agent_session(self, key: str, agent_id: str = "") -> bool:
        return agent_id in self.agent_ids(key)

    @abstractmethod
    def has_active_agents(self, path: str) -> bool:
        """Whether an agent of the worktree is running or waiting."""

    @abstractmethod
    def get_config(self) -> ConfigView: ...

    @abstractmethod
    def get_pr_info(self, path: str) -> PrInfo | None: ...

    @abstractmethod
    def has_pr_info(self, path: str) -> bool: ...

    # ------------------------------------------------------------------
    # Worktrees, git, PRs
    # ------------------------------------------------------------------

    @abstractmethod
    def refresh_worktrees(self) -> None:
        """Re-list the worktrees and sweep their git status (in the
        background; the result arrives as events)."""

    @abstractmethod
    def sweep_git_status(self) -> None:
        """Refresh every worktree's git status in the background."""

    @abstractmethod
    async def refresh_git_status(self, paths: Collection[str]) -> None: ...

    @abstractmethod
    async def get_diff(self, path: str) -> str: ...

    @abstractmethod
    async def refresh_pr_info(self, paths: Collection[str]) -> None: ...

    @abstractmethod
    def set_interest(self, paths: Iterable[str]) -> None:
        """The worktrees this TUI shows, which the engine polls."""

    @abstractmethod
    async def create_worktree(
        self,
        branch: str,
        base_branch: str = "main",
        extra: str = "",
        *,
        env: Mapping[str, str] | None = None,
    ) -> CreatedWorktree: ...

    @abstractmethod
    async def remove_worktree(
        self, path: str, force: bool = False, *, env: Mapping[str, str] | None = None
    ) -> str | None:
        """Remove a worktree the way the TUI does (the custom remove
        command when one is configured); returns its job id, if any."""

    # ------------------------------------------------------------------
    # Agents and terminals
    # ------------------------------------------------------------------

    @abstractmethod
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
    ) -> AgentInfo: ...

    @abstractmethod
    def stop_agent(self, key: str, agent_id: str = "") -> None:
        """Stop an agent; its ``agent.exited`` event follows."""

    @abstractmethod
    async def session(self, key: str, agent_id: str = "") -> Any:
        """The terminal of an agent (or the orchestrator), or None."""

    @abstractmethod
    async def open_shell(
        self, worktree_path: str, *, env: Mapping[str, str] | None = None
    ) -> Any:
        """The terminal of the worktree's shell, started on first use."""

    # ------------------------------------------------------------------
    # Hosted UI
    # ------------------------------------------------------------------

    @abstractmethod
    def attach_ui(self) -> Callable[[], None]:
        """Carry out ``ui.command`` events from now on; returns the
        function that stops it."""

    @abstractmethod
    def publish_view(self, view: dict[str, Any]) -> None:
        """Announce what the TUI shows (``ui.view_changed``)."""


def wants_remote(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether ``LAZYAGENT_ENGINE`` asks for a remote engine."""
    mode = environ.get(ENGINE_ENV, "").strip().lower()
    if mode == "remote":
        return True
    if mode not in ("", "inprocess", "in-process", "embedded"):
        raise ValueError(f"{ENGINE_ENV} must be remote or inprocess, not {mode!r}")
    return False


def autostart_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether a remote TUI starts the engine it does not find: unless
    ``LAZYAGENT_ENGINE_SOCKET`` names one or ``LAZYAGENT_ENGINE_AUTOSTART=0``."""
    return not environ.get(ENGINE_SOCKET_ENV) and (
        environ.get(AUTOSTART_ENV, "1").strip() != "0"
    )


def make_backend(
    repo_path: str | None = None,
    environ: Mapping[str, str] = os.environ,
    *,
    launch: Any = None,
) -> Backend:
    """The backend ``LAZYAGENT_ENGINE`` asks for: ``remote`` (with
    ``LAZYAGENT_ENGINE_SOCKET`` or the repo's discovered socket, which it
    starts when there is none, see :func:`autostart_enabled`), else an
    in-process engine. ``launch``: an engine the caller is already starting
    (a :class:`~lazyagent.engine.launcher.Launch`)."""
    if wants_remote(environ):
        from lazyagent.backend.remote import RemoteBackend

        return RemoteBackend(
            repo_path,
            socket_path=environ.get(ENGINE_SOCKET_ENV) or None,
            autostart=autostart_enabled(environ),
            launch=launch,
        )
    from lazyagent.backend.inprocess import InProcessBackend

    return InProcessBackend.for_repo(repo_path)
