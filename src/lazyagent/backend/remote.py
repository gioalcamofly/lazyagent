"""An engine in another process, behind the :class:`Backend` interface.

:class:`RemoteBackend` talks protocol v1 over the engine's Unix socket
(:class:`~lazyagent.engine.client.Client`). It keeps a client-side store of
what the TUI reads synchronously, built from ``get_sidebar_state`` and then
kept current by the engine's events (``subscribe {since: cursor}``, so
nothing falls between the two): the worktrees, git statuses, PR info, the
agents the engine runs and their states (an
:class:`~lazyagent.engine.registry.AgentRegistry` used as a mirror), the
config the modals need.

The connection is supervised: when the engine cannot be reached, or the
socket closes, subscribers get :data:`~lazyagent.backend.BACKEND_DISCONNECTED`
and the backend retries with backoff (re-discovering the socket each time);
once it is back they get the store's state again, then
:data:`~lazyagent.backend.BACKEND_CONNECTED`. Stopping the backend only
closes the connection: the engine and its agents keep running.

With ``autostart`` (D4) a backend that finds no engine for the repo starts
one, detached (:mod:`lazyagent.engine.launcher`), once per outage: at
start, and after an engine that died without saying so. An engine that
announced ``engine.shutdown`` (``lazyagent engine stop``) was stopped on
purpose and is not restarted; the backend keeps looking for one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any

from lazyagent import __version__
from lazyagent.agent_providers import ResumeMode
from lazyagent.backend import (
    BACKEND_CONNECTED,
    BACKEND_DISCONNECTED,
    Backend,
    EngineUnavailable,
    EventCallback,
)
from lazyagent.backend.remote_session import RemoteSession
from lazyagent.engine import launcher
from lazyagent.engine.client import Client, ConnectionClosed, RequestError, discover
from lazyagent.engine.launcher import resolve_repo_root
from lazyagent.engine.events import (
    AGENT_EXITED,
    AGENT_SPAWNED,
    AGENT_UPDATED,
    ENGINE_NOTICE,
    ENGINE_SHUTDOWN,
    GIT_STATUS_UPDATED,
    PR_INFO_UPDATED,
    WORKTREES_CHANGED,
    EngineEvent,
)
from lazyagent.engine.registry import ORCHESTRATOR_KEY, AgentRegistry
from lazyagent.engine.types import (
    AGENT_KIND,
    ORCHESTRATOR_KIND,
    SHELL_KIND,
    AgentInfo,
    ConfigView,
    CreatedWorktree,
)
from lazyagent.models import (
    AgentState,
    AgentStatus,
    CiCheck,
    GitStatus,
    LifecycleConfidence,
    PrInfo,
    WorktreeInfo,
)
from lazyagent.worktree_manager import WorktreeManagerError

log = logging.getLogger(__name__)

_STREAM_EVENTS = ("stream.resized", "stream.reset", "stream.closed")
# What the engine's v1 lists say of an agent with no status yet.
STARTING_STATUS = "starting"
_ACTIVE_STATUSES = (AgentStatus.RUNNING, AgentStatus.WAITING)

RETRY_INITIAL = 0.25
RETRY_MAX = 5.0
# How long start() waits for the first connection before the TUI opens
# without one (it keeps trying in the background).
FIRST_CONNECT_TIMEOUT = 3.0
_SYNC_TIMEOUT = 5.0


def _git_status(data: Mapping[str, Any]) -> GitStatus:
    fields = GitStatus.__dataclass_fields__
    return GitStatus(**{k: v for k, v in data.items() if k in fields})


def _pr_info(data: Mapping[str, Any] | None) -> PrInfo | None:
    if data is None:
        return None
    fields = PrInfo.__dataclass_fields__
    values = {k: v for k, v in data.items() if k in fields}
    check_fields = CiCheck.__dataclass_fields__
    values["checks"] = [
        CiCheck(**{k: v for k, v in check.items() if k in check_fields})
        for check in data.get("checks") or []
    ]
    return PrInfo(**values)


def _status(value: Any, default: AgentStatus = AgentStatus.NO_AGENT) -> AgentStatus:
    """An agent status from the wire; values this client does not know (an
    open set) keep ``default``."""
    try:
        return AgentStatus(value)
    except ValueError:
        return default


def _confidence(value: Any) -> LifecycleConfidence:
    try:
        return LifecycleConfidence(value)
    except ValueError:
        return LifecycleConfidence.LOW


class RemoteBackend(Backend):
    """The TUI's side of a v1 connection to an engine."""

    remote = True

    def __init__(
        self,
        repo_path: str | None = None,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        retry_initial: float = RETRY_INITIAL,
        retry_max: float = RETRY_MAX,
        first_connect_timeout: float = FIRST_CONNECT_TIMEOUT,
        autostart: bool = False,
        launch: launcher.Launch | None = None,
        start_timeout: float = launcher.START_TIMEOUT,
    ) -> None:
        self.repo_path = repo_path
        self._socket_option = os.fspath(socket_path) if socket_path else None
        self._retry_initial = retry_initial
        self._retry_max = retry_max
        self._first_connect_timeout = first_connect_timeout
        self._autostart = autostart and not self._socket_option
        # Whether this outage may still start an engine.
        self._autostart_armed = self._autostart
        # An engine already being started (by the CLI, before the TUI loaded).
        self._launch = launch
        self._start_timeout = start_timeout
        # The engine.shutdown reason of the engine this client is on.
        self._shutdown_reason: str | None = None

        self.client: Client | None = None
        self._socket_path: str | None = None
        self._engine_id: str | None = None
        self._repo_root = ""
        self._subscribers: list[EventCallback] = []
        self._supervisor: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._connected_once = asyncio.Event()
        self._outage_reported = False
        # The last engine-wide seq applied, and who waits for one (sync()).
        self._seq = 0
        self._seq_changed = asyncio.Event()

        self._worktrees: list[WorktreeInfo] = []
        self._git_statuses: dict[str, GitStatus] = {}
        self._pr: dict[str, PrInfo | None] = {}
        self._registry = AgentRegistry()
        self._agents: dict[str, dict[str, AgentInfo]] = {}
        self._sessions: dict[str, RemoteSession] = {}
        self._config = ConfigView(
            agent_provider="claude",
            default_branch="main",
            has_custom_create=False,
            has_custom_remove=False,
        )
        self._interest: list[str] = []
        self._ui_attached = 0
        self._view: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Lifecycle and events
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._supervisor is not None:
            return
        self._stopping = False
        if not self._socket_option:
            try:
                self._repo_root = resolve_repo_root(self.repo_path)
            except WorktreeManagerError as e:
                self._notice("error", str(e))
        self._supervisor = asyncio.get_running_loop().create_task(self._supervise())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._connected_once.wait(), self._first_connect_timeout
            )

    async def stop(self) -> None:
        """Close the connection. The engine and its agents keep running."""
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        for task in list(self._tasks):
            task.cancel()
        for session in self._sessions.values():
            if session._task is not None:
                session._task.cancel()
        client, self.client = self.client, None
        if client is not None:
            await client.close()

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    async def sync(self) -> None:
        """Wait until every event the engine has emitted so far is applied."""
        client = self.client
        if client is None:
            return
        try:
            cursor = (await self.request("list_events"))["cursor"]
        except (ValueError, EngineUnavailable):
            return

        async def caught_up() -> None:
            while self._seq < cursor and self.client is client:
                self._seq_changed.clear()
                await self._seq_changed.wait()

        await asyncio.wait_for(caught_up(), _SYNC_TIMEOUT)

    @property
    def connected(self) -> bool:
        return self.client is not None

    @property
    def v1_socket_path(self) -> str | None:
        return self._socket_path

    @property
    def engine_id(self) -> str | None:
        return self._engine_id

    def _emit(self, name: str, data: Any, seq: int | None = None) -> None:
        event = EngineEvent(self._seq if seq is None else seq, name, data)
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:
                log.exception("backend subscriber failed on %s", name)

    def _notice(self, severity: str, message: str) -> None:
        self._emit(ENGINE_NOTICE, {"severity": severity, "message": message})

    # ------------------------------------------------------------------
    # The connection
    # ------------------------------------------------------------------

    def _target(self) -> str:
        if self._socket_option:
            return self._socket_option
        if not self._repo_root:
            self._repo_root = resolve_repo_root(self.repo_path)
        path = discover(self._repo_root)
        if path is None:
            raise EngineUnavailable(
                f"no engine serves {self._repo_root} "
                "(start one with: lazyagent engine start)"
            )
        return str(path)

    async def _maybe_autostart(self) -> bool:
        """Start an engine for the repo, once per outage; True when one
        now serves it (this backend's, or one another client started)."""
        if not self._autostart_armed:
            return False
        self._autostart_armed = False
        try:
            if not self._repo_root:
                self._repo_root = resolve_repo_root(self.repo_path)
            if self._launch is None:
                self._launch = await asyncio.to_thread(
                    launcher.prestart, self._repo_root
                )
            if self._launch is not None:
                await asyncio.to_thread(self._launch.wait, self._start_timeout)
        except (launcher.EngineStartError, OSError, WorktreeManagerError) as e:
            self._launch = None
            self._report_outage(str(e))
            return False
        return True

    def _announce_launch(self) -> None:
        """Tell the user when the engine just connected to is the one this
        backend (or the CLI before it) started."""
        launch, self._launch = self._launch, None
        if launch is None:
            return
        launch.close()
        info = launcher.info(self._repo_root) if self._repo_root else None
        if info is not None and info.get("pid") == launch.pid:
            self._notice(
                "information",
                f"Started an engine for this repo (pid {launch.pid}): agents "
                "keep running when you quit. Stop it with: lazyagent engine stop",
            )

    async def _supervise(self) -> None:
        delay = self._retry_initial
        while not self._stopping:
            try:
                path = self._target()
                client = await Client.connect(
                    path, client="tui", client_version=__version__
                )
            except (
                OSError,
                EngineUnavailable,
                RequestError,
                ConnectionError,
                WorktreeManagerError,
                asyncio.TimeoutError,
            ) as e:
                if await self._maybe_autostart():
                    continue
                self._report_outage(str(e) or type(e).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max)
                continue
            try:
                await self._on_connected(client, path)
            except (RequestError, ConnectionClosed, ConnectionError, OSError) as e:
                await client.close()
                self._report_outage(str(e) or type(e).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max)
                continue
            delay = self._retry_initial
            await self._pump(client)
            if self._stopping:
                return
            self._on_lost()

    def _report_outage(self, reason: str) -> None:
        log.info("engine unavailable: %s", reason)
        if not self._outage_reported:
            self._outage_reported = True
            self._emit(BACKEND_DISCONNECTED, {"reason": reason})

    def _on_lost(self) -> None:
        self.client = None
        for session in self._sessions.values():
            session.connection_lost()
        reason = self._shutdown_reason
        if reason is None:
            # It died without a word (K3): its agents are gone, a new engine
            # may as well be started.
            self._autostart_armed = self._autostart
            self._report_outage("the engine closed the connection")
        else:
            how = " with lazyagent engine stop" if reason == "shutdown" else ""
            self._report_outage(
                f"it was stopped{how}; start it again with: lazyagent engine start"
            )

    async def _on_connected(self, client: Client, path: str) -> None:
        hello = client.hello
        assert hello is not None
        first = self._engine_id is None
        engine_changed = not first and hello.engine_id != self._engine_id
        state = await client.request("get_sidebar_state")
        config = await client.request("get_config")
        if self._ui_attached:
            await client.request("attach_ui")
        if self._interest:
            await client.request("set_interest", worktree_paths=list(self._interest))
        await client.request("subscribe", since=state["cursor"])

        self._engine_id = hello.engine_id
        self._repo_root = hello.repo_root
        self._socket_path = path
        self._config = ConfigView(
            agent_provider=config["agent_provider"],
            default_branch=config["default_branch"],
            has_custom_create=config["has_custom_create"],
            has_custom_remove=config["has_custom_remove"],
        )
        if engine_changed:
            for session in self._sessions.values():
                session.gone()
            self._sessions.clear()
        self._seed(state)
        self._seq = state["cursor"]
        self.client = client
        self._outage_reported = False
        self._shutdown_reason = None
        self._autostart_armed = False
        self._connected_once.set()

        self._emit(WORKTREES_CHANGED, state["worktrees"])
        for path_, info in self._pr.items():
            self._emit(PR_INFO_UPDATED, {"path": path_, "pr_info": info})
        for bucket in list(self._agents.values()):
            for info in list(bucket.values()):
                self._emit(AGENT_SPAWNED, info.to_dict())
        for session_id, session in list(self._sessions.items()):
            if session.kind == SHELL_KIND or self._has_session_id(session_id):
                session.connection_restored()
            else:
                del self._sessions[session_id]
                session.gone()
        self._emit(
            BACKEND_CONNECTED, {"first": first, "engine_changed": engine_changed}
        )
        self._announce_launch()
        if self._view is not None:
            self.fire("publish_view", view=self._view)
        # Re-list and sweep git status, as an in-process engine does at start.
        self.fire("refresh_worktrees")

    def _has_session_id(self, session_id: str) -> bool:
        return any(
            info.session_id == session_id
            for bucket in self._agents.values()
            for info in bucket.values()
        )

    async def _pump(self, client: Client) -> None:
        async for event in client.events():
            if event.event in _STREAM_EVENTS:
                continue
            try:
                self._apply(event.event, event.data)
            except Exception:
                log.exception("could not apply %s", event.event)
            self._seq = max(self._seq, event.seq)
            self._seq_changed.set()
            self._emit(event.event, event.data, event.seq)

    # ------------------------------------------------------------------
    # The store
    # ------------------------------------------------------------------

    def _seed(self, state: Mapping[str, Any]) -> None:
        # Cleared in place: the widgets hold on to the registry.
        self._registry.clear()
        self._agents = {}
        self._set_worktrees(state["worktrees"])
        self._set_agents(ORCHESTRATOR_KEY, state["orchestrator"]["agents"])
        self._pr = {path: _pr_info(info) for path, info in state["pr_info"].items()}
        for path, status in state.get("git_statuses", {}).items():
            self._git_statuses[path] = _git_status(status)

    def _set_worktrees(self, payload: list[dict[str, Any]]) -> None:
        self._worktrees = [
            WorktreeInfo(
                path=wt["path"],
                head=wt.get("head") or "",
                branch=wt.get("branch"),
                is_main=bool(wt.get("is_main")),
                is_bare=bool(wt.get("is_bare")),
            )
            for wt in payload
        ]
        paths = {wt.path for wt in self._worktrees}
        for wt in payload:
            self._set_agents(wt["path"], wt.get("agents") or [])
            partial = wt.get("git_status")
            if partial and wt["path"] not in self._git_statuses:
                self._git_statuses[wt["path"]] = _git_status(partial)
        for path in list(self._git_statuses):
            if path not in paths:
                del self._git_statuses[path]
        for path in list(self._pr):
            if path not in paths:
                del self._pr[path]
        for key in list(self._agents):
            if key != ORCHESTRATOR_KEY and key not in paths:
                self._forget_key(key)

    def _set_agents(self, key: str, agents: list[dict[str, Any]]) -> None:
        """Replace ``key``'s agent states with a payload's (a ``starting``
        agent, which has reported no status yet, gets no state)."""
        states: dict[str, AgentState] = {}
        for entry in agents:
            agent_id = entry["agent_id"]
            label = entry.get("label") or ""
            session_name = entry.get("session_name") or ""
            self._registry.register(
                key, agent_id, label=label, session_name=session_name
            )
            session_id = entry.get("session_id")
            if session_id:
                kind = entry.get("kind") or (
                    ORCHESTRATOR_KIND if key == ORCHESTRATOR_KEY else AGENT_KIND
                )
                self._agents.setdefault(key, {})[agent_id] = AgentInfo(
                    key, agent_id, session_id, label, session_name, kind
                )
            if entry.get("status") == STARTING_STATUS:
                continue  # running, but no status to show yet
            states[agent_id] = AgentState(
                status=_status(entry.get("status")),
                confidence=_confidence(entry.get("confidence")),
                detail=entry.get("detail") or "",
                agent_id=agent_id,
                label=label,
                session_name=session_name,
            )
        if states:
            self._registry.states[key] = states
        else:
            self._registry.states.pop(key, None)

    def _forget_key(self, key: str) -> None:
        for agent_id in list(self._agents.get(key, {})):
            self._forget_agent(key, agent_id)
        self._registry.states.pop(key, None)

    def _forget_agent(self, key: str, agent_id: str) -> None:
        bucket = self._agents.get(key, {})
        info = bucket.pop(agent_id, None)
        if not bucket:
            self._agents.pop(key, None)
        self._registry.drop(key, agent_id)
        self._registry.unregister(key, agent_id)
        if info is not None:
            session = self._sessions.pop(info.session_id, None)
            if session is not None:
                session.gone()

    def _apply(self, name: str, data: Any) -> None:
        if name == ENGINE_SHUTDOWN:
            self._shutdown_reason = str(data.get("reason") or "stopped")
        elif name == WORKTREES_CHANGED:
            self._set_worktrees(data)
        elif name == GIT_STATUS_UPDATED:
            for path, status in data.items():
                self._git_statuses[path] = _git_status(status)
        elif name == PR_INFO_UPDATED:
            self._pr[data["path"]] = _pr_info(data.get("pr_info"))
        elif name == AGENT_SPAWNED:
            key, agent_id = data["worktree_path"], data["agent_id"]
            self._registry.register(
                key,
                agent_id,
                label=data.get("label") or "",
                session_name=data.get("session_name") or "",
            )
            self._agents.setdefault(key, {})[agent_id] = AgentInfo(
                key,
                agent_id,
                data["session_id"],
                data.get("label") or "",
                data.get("session_name") or "",
                data.get("kind") or AGENT_KIND,
            )
        elif name == AGENT_UPDATED:
            key, agent_id = data["worktree_path"], data["agent_id"]
            previous = self._registry.agents(key).get(agent_id)
            self._registry.update_status(
                key,
                agent_id,
                _status(
                    data.get("status"),
                    previous.status if previous else AgentStatus.NO_AGENT,
                ),
                _confidence(data.get("confidence")),
                data.get("detail") or "",
            )
        elif name == AGENT_EXITED:
            self._forget_agent(data["worktree_path"], data["agent_id"])

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def request(self, method: str, **params: Any) -> Any:
        """A request to the engine; errors come back as the exceptions the
        in-process engine raises."""
        return await self._result(self._send(method, params))

    def _send(self, method: str, params: dict[str, Any]) -> asyncio.Future:
        client = self.client
        if client is None:
            raise EngineUnavailable("not connected to the engine")
        try:
            return client.send(method, **params)
        except (ConnectionClosed, ConnectionError, OSError) as e:
            raise EngineUnavailable(f"lost the engine: {e}") from None

    @staticmethod
    async def _result(future: asyncio.Future) -> Any:
        try:
            response = await future
        except (ConnectionClosed, ConnectionError, OSError) as e:
            raise EngineUnavailable(f"lost the engine: {e}") from None
        error = response.error
        if error is None:
            return response.result
        if error.code == "WORKTREE_ERROR":
            raise WorktreeManagerError(error.message)
        raise ValueError(error.message)

    def fire(
        self,
        method: str,
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        **params: Any,
    ) -> None:
        """Send a request now without waiting for its answer: requests fired
        (or awaited) one after the other reach the engine in that order."""
        try:
            future = self._send(method, params)
        except EngineUnavailable as e:
            log.info("%s not sent: %s", method, e)
            if on_error is not None:
                on_error(e)
            return

        async def run() -> None:
            try:
                result = await self._result(future)
            except (ValueError, WorktreeManagerError, EngineUnavailable) as e:
                log.info("%s failed: %s", method, e)
                if on_error is not None:
                    on_error(e)
                return
            if on_done is not None:
                on_done(result)

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def repo_root(self) -> str:
        return self._repo_root

    @property
    def worktrees(self) -> list[WorktreeInfo]:
        return self._worktrees

    @property
    def git_statuses(self) -> dict[str, GitStatus]:
        return self._git_statuses

    @git_statuses.setter
    def git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        self._git_statuses = statuses

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    def agent_ids(self, key: str) -> list[str]:
        return list(self._agents.get(key, {}))

    def has_agent(self, key: str) -> bool:
        return bool(self._agents.get(key))

    def has_active_agents(self, path: str) -> bool:
        return any(
            s.status in _ACTIVE_STATUSES for s in self._registry.agents(path).values()
        )

    def get_config(self) -> ConfigView:
        return self._config

    def get_pr_info(self, path: str) -> PrInfo | None:
        return self._pr.get(path)

    def has_pr_info(self, path: str) -> bool:
        return path in self._pr

    # ------------------------------------------------------------------
    # Worktrees, git, PRs
    # ------------------------------------------------------------------

    def refresh_worktrees(self) -> None:
        self.fire("refresh_worktrees")

    def sweep_git_status(self) -> None:
        self.fire("refresh_git_status")

    async def refresh_git_status(self, paths: Collection[str]) -> None:
        try:
            result = await self.request("refresh_git_status", paths=list(paths))
        except EngineUnavailable as e:
            log.info("git status not refreshed: %s", e)
            return
        for path, status in result["statuses"].items():
            self._git_statuses[path] = _git_status(status)

    async def get_diff(self, path: str) -> str:
        return (await self.request("get_diff", worktree_path=path))["diff"]

    async def refresh_pr_info(self, paths: Collection[str]) -> None:
        for path in paths:
            try:
                result = await self.request(
                    "get_pr_info", worktree_path=path, refresh=True
                )
            except EngineUnavailable as e:
                log.info("PR info not refreshed: %s", e)
                return
            if result["fetched"]:
                self._pr[path] = _pr_info(result["pr_info"])

    def set_interest(self, paths: Iterable[str]) -> None:
        self._interest = list(paths)
        self.fire("set_interest", worktree_paths=list(self._interest))

    async def create_worktree(
        self,
        branch: str,
        base_branch: str = "main",
        extra: str = "",
        *,
        env: Mapping[str, str] | None = None,
    ) -> CreatedWorktree:
        params: dict[str, Any] = {"branch": branch, "base_branch": base_branch}
        if extra:
            params["extra"] = extra
        if env is not None:
            params["env"] = dict(env)
        result = await self.request("create_worktree", **params)
        return CreatedWorktree(result["path"], result["branch"], result.get("job_id"))

    async def remove_worktree(
        self, path: str, force: bool = False, *, env: Mapping[str, str] | None = None
    ) -> str | None:
        params: dict[str, Any] = {"worktree_path": path, "force": force, "custom": True}
        if env is not None:
            params["env"] = dict(env)
        result = await self.request("remove_worktree", **params)
        return result.get("job_id")

    # ------------------------------------------------------------------
    # Agents and terminals
    # ------------------------------------------------------------------

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
        params: dict[str, Any] = {
            "worktree_path": worktree_path,
            "skip_permissions": skip_permissions,
            "resume_mode": resume_mode.value,
            "kind": kind,
        }
        if instruction:
            params["instruction"] = instruction
        if env is not None:
            params["env"] = dict(env)
        result = await self.request("spawn_agent", **params)
        key = ORCHESTRATOR_KEY if kind == ORCHESTRATOR_KIND else result["worktree_path"]
        known = self._agents.get(key, {}).get(result["agent_id"])
        if known is not None:
            return known
        return AgentInfo(
            key, result["agent_id"], result.get("session_id", ""), "", "", kind
        )

    def stop_agent(self, key: str, agent_id: str = "") -> None:
        def failed(error: Exception) -> None:
            self._notice("warning", f"Could not stop the agent: {error}")

        self.fire("stop_agent", on_error=failed, worktree_path=key, agent_id=agent_id)

    async def session(self, key: str, agent_id: str = "") -> RemoteSession | None:
        info = self._agents.get(key, {}).get(agent_id)
        if info is None:
            return None
        session = self._sessions.get(info.session_id)
        if session is not None:
            return session
        rows, cols = 24, 80
        with contextlib.suppress(ValueError, EngineUnavailable, WorktreeManagerError):
            listed = await self.request("list_agents", worktree_path=key)
            for entry in listed["agents"]:
                if entry.get("session_id") == info.session_id and "size" in entry:
                    rows, cols = entry["size"]["rows"], entry["size"]["cols"]
        current = self._agents.get(key, {}).get(agent_id)
        if current is None or current.session_id != info.session_id:
            return None  # gone while asking
        session = self._sessions.get(info.session_id)
        if session is None:
            session = self._sessions[info.session_id] = RemoteSession(
                self,
                info.session_id,
                kind=info.kind,
                worktree_path=key,
                agent_id=agent_id,
                rows=rows,
                cols=cols,
            )
        return session

    async def open_shell(
        self, worktree_path: str, *, env: Mapping[str, str] | None = None
    ) -> RemoteSession:
        params: dict[str, Any] = {"worktree_path": worktree_path}
        if env is not None:
            params["env"] = dict(env)
        result = await self.request("open_shell", **params)
        session = self._sessions.get(result["session_id"])
        if session is None or not session.running:
            session = self._sessions[result["session_id"]] = RemoteSession(
                self,
                result["session_id"],
                kind=SHELL_KIND,
                worktree_path=worktree_path,
                rows=result["size"]["rows"],
                cols=result["size"]["cols"],
            )
        return session

    # ------------------------------------------------------------------
    # Hosted UI
    # ------------------------------------------------------------------

    def attach_ui(self) -> Callable[[], None]:
        self._ui_attached += 1
        if self._ui_attached == 1:
            self.fire("attach_ui")
        attached = True

        def detach() -> None:
            nonlocal attached
            if not attached:
                return
            attached = False
            self._ui_attached -= 1
            if self._ui_attached == 0:
                self.fire("detach_ui")

        return detach

    def publish_view(self, view: dict[str, Any]) -> None:
        self._view = dict(view)
        self.fire("publish_view", view=self._view)
