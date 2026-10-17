"""The engine facade: the one owner of lazyagent's state and operations.

:class:`Engine` composes everything a client needs and nothing a client
shows: the repo root and its :class:`~lazyagent.config.Config`, the
:class:`~lazyagent.engine.registry.AgentRegistry`, the
:class:`~lazyagent.engine.worktrees.WorktreeService`, every terminal session
(agents, the orchestrator under :data:`ORCHESTRATOR_KEY`, one ``bash -l``
shell per worktree), the timers that keep agent status fresh, the jobs that
run custom create/remove commands, the legacy IPC server and the protocol
v1 server (:mod:`lazyagent.engine.server`).

Clients (the Textual app, the IPC and v1 servers) call its methods and react to its
:class:`~lazyagent.engine.events.EventBus`; they never start a process or
hold engine state of their own. Everything runs on the loop that called
:meth:`Engine.start`.
"""

from __future__ import annotations

import asyncio
import codecs
import itertools
import logging
import os
import shlex
import uuid
from collections.abc import Callable, Collection, Coroutine, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent import diagnostics as diag
from lazyagent.agent_providers import (
    AgentProvider,
    ResumeMode,
    agent_session_name,
    env_exports,
    get_agent_provider,
)
from lazyagent.config import Config, format_command, load_config
from lazyagent.engine.events import (
    AGENT_EXITED,
    AGENT_SPAWNED,
    AGENT_UPDATED,
    ENGINE_NOTICE,
    ENGINE_RESTORED,
    ENGINE_SHUTDOWN,
    JOB_FINISHED,
    JOB_OUTPUT,
    JOB_STARTED,
    UI_COMMAND,
    UI_VIEW_CHANGED,
    EngineEvent,
    EventBus,
)
from lazyagent.engine.registry import (
    ORCHESTRATOR_KEY,
    AgentIdStore,
    AgentRegistry,
    default_id_store_path,
)
from lazyagent.engine.manifest import (
    Entry,
    Manifest,
    clip_instruction,
    manifest_path,
)
from lazyagent.engine.scheduler import Scheduler
from lazyagent.engine.session import (
    AgentSession,
    SessionEvent,
    SessionExited,
    StatusChanged,
    TerminalSession,
)
from lazyagent.engine.types import (
    AGENT_KIND,
    ORCHESTRATOR_KIND,
    SHELL_KIND,
    AgentInfo,
    ConfigView,
    CreatedWorktree,
)
from lazyagent.engine.worktrees import (
    GIT_STATUS_UPDATED,
    PR_INFO_UPDATED,
    WORKTREES_CHANGED,
    WorktreeEvent,
    WorktreeService,
)
from lazyagent.models import (
    AgentState,
    AgentStatus,
    GitStatus,
    LifecycleConfidence,
    PrInfo,
    WorktreeInfo,
    default_base,
    rollup_status,
)
from lazyagent.orchestrator_prompt import compose_orchestrator_prompt
from lazyagent.worktree_manager import (
    WorktreeManager,
    WorktreeManagerError,
    find_repo_root,
)

if TYPE_CHECKING:
    from lazyagent.engine.runtime import EngineLock
    from lazyagent.engine.server import V1Server
    from lazyagent.ipc import IpcServer

__all__ = [
    "AGENT_KIND",
    "ORCHESTRATOR_KEY",
    "ORCHESTRATOR_KIND",
    "SHELL_KIND",
    "AgentInfo",
    "ConfigView",
    "CreatedWorktree",
    "Engine",
    "JobResult",
]

log = logging.getLogger(__name__)

# Hook logs are otherwise only read when an agent prints something; a quiet
# agent's status (and the events that follow from it) would lag.
POLL_AGENTS_INTERVAL = 1.0
CHECK_HANGS_INTERVAL = 60.0

NOTICE_SEVERITIES = ("information", "warning", "error")

# The v1 status of an agent the engine runs that has reported none yet.
STARTING_STATUS = "starting"

# An agent in one of these is working or about to: its worktree must not be
# pulled out from under it.
_ACTIVE_STATUSES = (AgentStatus.RUNNING, AgentStatus.WAITING)

_JOB_READ_SIZE = 4096


@dataclass(frozen=True)
class JobResult:
    """How a job ended. ``exit_code`` is negative for a signal."""

    job_id: str
    exit_code: int | None
    stdout: str
    stderr: str

    @property
    def detail(self) -> str:
        """What to tell a caller when the job failed."""
        return (
            self.stderr.strip() or self.stdout.strip() or f"exit code {self.exit_code}"
        )


@dataclass
class _Agent:
    session: AgentSession
    info: AgentInfo
    remove_listener: Callable[[], None]


class Engine:
    """Owns agents, shells, worktrees, config, jobs and the event bus."""

    def __init__(
        self,
        repo_path: str | None = None,
        *,
        manager_factory: Callable[[str], WorktreeManager] | None = None,
        find_root: Callable[[], Any] | None = None,
        config_loader: Callable[[str], Config] | None = None,
        provider_lookup: Callable[[str | None], AgentProvider] | None = None,
        id_store: AgentIdStore | None = None,
        persist_ids: bool = True,
        poll_interval: float = POLL_AGENTS_INTERVAL,
        hang_interval: float = CHECK_HANGS_INTERVAL,
        v1_server: bool | None = None,
        persist_manifest: bool | None = None,
    ) -> None:
        self.repo_path = repo_path
        # Looked up on each call, not bound here, so tests can swap the
        # module attributes.
        self._manager = manager_factory or (lambda root: WorktreeManager(root))
        self._config_loader = config_loader or (lambda root: load_config(root))
        self._provider = provider_lookup or (lambda name: get_agent_provider(name))
        self.config = Config()

        self.events = EventBus()
        # persist_ids=False keeps agent ids in memory (tests, throwaway engines).
        if id_store is None and persist_ids:
            id_store = AgentIdStore(default_id_store_path())
        self.registry = AgentRegistry(id_store=id_store, events=self.events)
        # The restart manifest (PR 9); None: this engine keeps none (another
        # engine serves the repo, or a throwaway engine). Follows persist_ids
        # unless told otherwise.
        self._persist_manifest = (
            persist_ids if persist_manifest is None else persist_manifest
        )
        self.manifest: Manifest | None = None
        # Random per engine process: the v1 engine_id, and the manifest's.
        self.instance_id = uuid.uuid4().hex
        self.worktree_service = WorktreeService(
            self._manager,
            find_root=find_root or (lambda: find_repo_root()),
        )
        self.worktree_service.subscribe(self._on_worktree_event)

        self.socket_path: str | None = None
        self._ipc_server: IpcServer | None = None
        # None: on unless LAZYAGENT_V1_SERVER=0.
        self._v1_enabled = v1_server
        self.v1_socket_path: str | None = None
        self._v1_server: V1Server | None = None
        self._lock: EngineLock | None = None
        # The pid of the lazyagent that already serves the repo when this
        # engine could not (0: unknown), else None.
        self.other_instance: int | None = None
        # Set by whoever runs a detached engine: called (after the reply is
        # on its way) when a client asks it to shut down. None: the engine
        # lives inside a TUI and only that TUI stops it.
        self.shutdown_handler: Callable[[], None] | None = None

        # Hosted UIs that carry out ui.command, and the last view a UI
        # announced (ui.view_changed).
        self._ui_hosts = 0
        self.ui_view: dict[str, Any] | None = None

        self._agents: dict[str, dict[str, _Agent]] = {}
        self._shells: dict[str, TerminalSession] = {}
        self._session_ids = itertools.count(1)
        self._job_ids = itertools.count(1)
        self._jobs: dict[str, asyncio.subprocess.Process] = {}

        self._poll_interval = poll_interval
        self._hang_interval = hang_interval
        self._scheduler = Scheduler()
        self._tasks: set[asyncio.Task] = set()
        self._groups: dict[str, asyncio.Task] = {}
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, ipc: bool = True) -> None:
        """List the worktrees, load the config, start the servers (when
        ``ipc``: the legacy IPC one and, unless disabled, the v1 one) and
        the timers."""
        if self._started:
            return
        self._started = True
        with diag.timed(log, "engine.load_worktrees"):
            self.refresh_worktrees()
        self.load_config()
        if ipc:
            with diag.timed(log, "engine.start_ipc"):
                await self._start_ipc()
            with diag.timed(log, "engine.start_v1"):
                await self._start_v1()
            # Only the engine that holds the repo lock keeps the manifest.
            if self._lock is not None and self._persist_manifest:
                self.open_manifest()
        self._scheduler = Scheduler()
        self._scheduler.every(
            self._hang_interval, diag.wrap_timer("timer.check_hangs", self.check_hangs)
        )
        self._scheduler.every(
            self._poll_interval, diag.wrap_timer("timer.poll_agents", self.poll_agents)
        )
        self._scheduler.start()
        # Git status and PR info are polled by the service, once a minute and
        # only for the clients' interest sets.
        self.worktree_service.start()
        if (
            self.manifest is not None
            and self.manifest.restorable
            and self.config.engine.restore_on_start
        ):
            self._spawn(self.restore_agents())

    async def stop(self, reason: str = "stopped") -> None:
        """Stop the timers, jobs, servers and every session. Idempotent.

        Clients hear ``engine.shutdown {reason}`` first. Sessions are stopped
        quietly: no ``agent.exited`` is emitted for an engine that is going
        away.
        """
        if self._started:
            self.events.emit(ENGINE_SHUTDOWN, {"reason": reason})
        self._started = False
        self._scheduler.stop()
        self.worktree_service.stop()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._groups.clear()
        for proc in list(self._jobs.values()):
            _kill(proc)
        if self._v1_server is not None:
            v1, self._v1_server = self._v1_server, None
            self.v1_socket_path = None
            await v1.stop()
        if self._lock is not None:
            from lazyagent.engine.runtime import remove_empty_dirs

            lock, self._lock = self._lock, None
            lock.release()
            remove_empty_dirs(lock.path.parent)
        if self._ipc_server is not None:
            server, self._ipc_server = self._ipc_server, None
            await server.stop()
        for bucket in list(self._agents.values()):
            for agent in list(bucket.values()):
                agent.remove_listener()
                agent.session.stop()
        self._agents.clear()
        for shell in list(self._shells.values()):
            shell.stop()
        self._shells.clear()
        # The agents just stopped stay in it: the next engine offers them.
        if self.manifest is not None:
            await self.manifest.close()

    async def _start_ipc(self) -> None:
        from lazyagent.ipc import start_ipc_server

        try:
            self._ipc_server, self.socket_path = await start_ipc_server(self)
        except OSError as e:
            self.notice("warning", f"IPC server failed to start: {e}")

    async def _start_v1(self) -> None:
        from lazyagent.engine import server as v1
        from lazyagent.engine.runtime import (
            LOCK_NAME,
            EngineLock,
            lock_holder,
            remove_empty_dirs,
        )

        enabled = self._v1_enabled
        if enabled is None:
            enabled = v1.enabled()
        repo_root = self.repo_root
        if not enabled or not repo_root:
            return
        path = v1.socket_path(repo_root)
        lock = EngineLock(path.with_name(LOCK_NAME))
        server = v1.V1Server(self, path, engine_id=self.instance_id)
        try:
            v1.private_dir(path.parent)
            started = lock.acquire() and await server.start()
        except OSError as e:
            lock.release()
            remove_empty_dirs(path.parent)
            self.notice("warning", f"Protocol v1 server failed to start: {e}")
            return
        if not started:
            # The lock's holder, or (holding no lock: an older lazyagent)
            # whoever engine.json names.
            other = lock_holder(lock.path) if not lock.held else None
            lock.release()
            if other is None:
                other = (v1.read_info(path.with_name(v1.INFO_NAME)) or {}).get("pid")
            self.other_instance = other if isinstance(other, int) else 0
            self.notice(
                "warning",
                "Another lazyagent"
                + (f" (pid {other})" if other else "")
                + f" already serves this repo on {path}; this instance runs "
                "without the protocol v1 server.",
            )
            return
        self._lock = lock
        self._v1_server = server
        self.v1_socket_path = str(path)

    def load_config(self) -> None:
        root = self.repo_root
        self.config = self._config_loader(root) if root else Config()

    def get_config(self) -> ConfigView:
        config = self.config
        return ConfigView(
            agent_provider=config.agent.provider,
            default_branch=config.default_branch,
            has_custom_create=config.has_custom_create,
            has_custom_remove=config.has_custom_remove,
        )

    def notice(self, severity: str, message: str) -> None:
        """Tell every client something it should show the user."""
        if severity not in NOTICE_SEVERITIES:
            raise ValueError(f"Unknown severity: {severity!r}")
        log.info("notice (%s): %s", severity, message)
        self.events.emit(ENGINE_NOTICE, {"severity": severity, "message": message})

    def subscribe(
        self, callback, events: Iterable[str] | None = None
    ) -> Callable[[], None]:
        """Shorthand for ``self.events.subscribe``."""
        return self.events.subscribe(callback, events)

    @property
    def engine_id(self) -> str | None:
        """The v1 server's ``engine_id``; None while it does not run."""
        return self._v1_server.engine_id if self._v1_server is not None else None

    # ------------------------------------------------------------------
    # Hosted UI
    # ------------------------------------------------------------------

    def attach_ui(self) -> Callable[[], None]:
        """Register a UI that carries out ``ui.command`` events (a TUI in
        hosted mode); returns the function that unregisters it."""
        self._ui_hosts += 1
        attached = True

        def detach() -> None:
            nonlocal attached
            if attached:
                attached = False
                self._ui_hosts -= 1

        return detach

    @property
    def ui_attached(self) -> bool:
        return self._ui_hosts > 0

    def ui_command(self, command: str, **args: Any) -> EngineEvent:
        """Ask the hosted UI to change what it shows."""
        return self.events.emit(UI_COMMAND, {"command": command, **args})

    def publish_view(self, view: dict[str, Any]) -> bool:
        """Announce what a UI shows (``ui.view_changed``), unless unchanged."""
        if view == self.ui_view:
            return False
        self.ui_view = dict(view)
        self.events.emit(UI_VIEW_CHANGED, dict(view))
        return True

    async def wait_idle(self) -> None:
        """Wait for the background work started so far (git sweeps)."""
        # Only the unfinished ones: a finished task leaves the set in a
        # done callback, and gathering finished tasks completes without
        # yielding to the loop that would run it.
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.worktree_service.wait_idle()

    def _spawn(
        self, coro: Coroutine[Any, Any, Any], *, group: str | None = None
    ) -> None:
        """Run ``coro`` in the background. A new task in a ``group`` cancels
        the one before it: only the latest answer matters."""
        if group is not None and (previous := self._groups.get(group)) is not None:
            previous.cancel()
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        if group is not None:
            self._groups[group] = task
        task.add_done_callback(lambda t: self._task_done(t, group))

    def _task_done(self, task: asyncio.Task, group: str | None) -> None:
        self._tasks.discard(task)
        if group is not None and self._groups.get(group) is task:
            del self._groups[group]
        if not task.cancelled() and task.exception() is not None:
            log.error("engine task failed", exc_info=task.exception())

    # ------------------------------------------------------------------
    # Worktrees, git, PRs
    # ------------------------------------------------------------------

    @property
    def repo_root(self) -> str:
        return self.worktree_service.repo_root

    @property
    def worktrees(self) -> list[WorktreeInfo]:
        return self.worktree_service.worktrees

    @property
    def git_statuses(self) -> dict[str, GitStatus]:
        return self.worktree_service.git_statuses

    def worktree(self, path: str) -> WorktreeInfo | None:
        return self.worktree_service.worktree(path)

    def refresh_worktrees(self) -> bool:
        """Re-list the worktrees (emits ``worktrees.changed``), then sweep
        every worktree's git status in the background.

        A listing failure is reported as an ``engine.notice`` and the old
        list is kept; returns whether the listing worked.
        """
        try:
            self.worktree_service.rescan(self.repo_path)
        except WorktreeManagerError as e:
            self.notice("error", str(e))
            return False
        self.sweep_git_status()
        return True

    def sweep_git_status(self) -> None:
        """Refresh every worktree's git status in the background; a new
        sweep supersedes one still running."""
        self._spawn(self.worktree_service.refresh_git_status(), group="git_status_all")

    async def refresh_git_status(self, paths: Collection[str] | None = None) -> None:
        await self.worktree_service.refresh_git_status(paths)

    async def get_diff(
        self, path: str, *, base: str | None = None, max_bytes: int | None = None
    ) -> str:
        if base is None and max_bytes is None:
            return await self.worktree_service.get_diff(path)
        return await self.worktree_service.get_diff(path, base=base, max_bytes=max_bytes)

    def get_pr_info(self, path: str) -> PrInfo | None:
        """The cached PR info of a worktree (``None`` also when not fetched
        yet; see :meth:`has_pr_info`)."""
        return self.worktree_service.pr_info(path)

    def has_pr_info(self, path: str) -> bool:
        return self.worktree_service.has_pr_info(path)

    async def refresh_pr_info(self, paths: Collection[str]) -> None:
        await self.worktree_service.refresh_pr_info(paths)

    def set_interest(self, paths: Iterable[str], client: str = "default") -> None:
        self.worktree_service.set_interest(paths, client)

    def predicted_worktree_name(self, branch: str) -> str:
        """``<repo>-<branch>``: the name a custom create command is expected
        to give ``branch``'s worktree."""
        repo_root = self.repo_root
        repo_name = os.path.basename(repo_root) if repo_root else ""
        return f"{repo_name}-{branch}" if repo_name else branch

    def predicted_worktree_path(self, branch: str) -> str:
        """Where a custom create command is expected to put ``branch``: next
        to the repo root, under :meth:`predicted_worktree_name`."""
        repo_root = self.repo_root
        name = self.predicted_worktree_name(branch)
        return str(
            os.path.join(os.path.dirname(repo_root), name) if repo_root else name
        )

    async def create_worktree(
        self,
        branch: str,
        base_branch: str | None = None,
        extra: str = "",
        *,
        env: Mapping[str, str] | None = None,
    ) -> CreatedWorktree:
        """Create a worktree, with ``git worktree add`` or the configured
        custom command (run as a job, waited for), then re-list.

        ``extra`` and ``env`` (the environment the job runs with; ``None``
        means the engine's) only reach a custom command. Without a
        ``base_branch`` the new branch starts from :func:`default_base`.
        Raises :class:`WorktreeManagerError` when the creation fails.
        """
        branch = branch.strip()
        if not branch:
            raise ValueError("branch is required and must not be empty")
        base_branch = base_branch or default_base(
            self.config.default_branch, self.worktrees
        )
        repo_root = self.repo_root
        template = self.config.worktree.create
        if template is not None:
            path = self.predicted_worktree_path(branch)
            command = format_command(
                template,
                branch=branch,
                name=self.predicted_worktree_name(branch),
                base=base_branch,
                path=path,
                repo=repo_root,
                extra=extra,
            )
            job = await self.run_job(
                command, cwd=repo_root, title=f"Create worktree {branch}", env=env
            )
            if job.exit_code != 0:
                raise WorktreeManagerError(
                    f"Custom create command failed: {job.detail}"
                )
            self.refresh_worktrees()
            return CreatedWorktree(path, branch, job.job_id)

        manager = self._manager(repo_root)
        path = await asyncio.to_thread(manager.create, branch, base_branch)
        self.refresh_worktrees()
        return CreatedWorktree(path, branch)

    def has_active_agents(self, path: str) -> bool:
        """Whether an agent of the worktree is running or waiting."""
        return any(
            s.status in _ACTIVE_STATUSES for s in self.registry.agents(path).values()
        )

    async def remove_worktree(
        self,
        path: str,
        force: bool = False,
        *,
        custom: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> str | None:
        """Remove a worktree and everything the engine runs in it.

        Uses the configured custom remove command (as a job) when there is
        one and ``custom`` is true (with ``env`` as for create), else
        ``git worktree remove``. Then stops
        the worktree's agents and shell, drops their state and re-lists.
        Returns the job id when a custom command ran.
        """
        worktree = self.worktree(path)
        if worktree is None:
            raise ValueError(f"Unknown worktree: {path}")
        if worktree.is_main:
            raise ValueError("Cannot remove the main worktree")
        if self.has_active_agents(path):
            raise ValueError("Agent is running in this worktree — stop it first")

        repo_root = self.repo_root
        template = self.config.worktree.remove
        job_id = None
        if custom and template is not None:
            command = format_command(
                template,
                branch=worktree.branch or "",
                name=worktree.name,
                base="",
                path=worktree.path,
                repo=repo_root,
                force="--force" if force else "",
            )
            job = await self.run_job(
                command,
                cwd=repo_root,
                title=f"Remove worktree {worktree.name}",
                env=env,
            )
            if job.exit_code != 0:
                raise WorktreeManagerError(
                    f"Custom remove command failed: {job.detail}"
                )
            job_id = job.job_id
        else:
            manager = self._manager(repo_root)
            await asyncio.to_thread(manager.remove, path, force=force)

        self._drop_worktree(path)
        self.refresh_worktrees()
        return job_id

    def _drop_worktree(self, path: str) -> None:
        for agent_id in list(self._agents.get(path, {})):
            self._stop_agent(path, agent_id)
        self.registry.drop_worktree(path)
        self.close_shell(path)
        if self.manifest is not None:
            self.manifest.prune(self._restorable_exists)

    def worktrees_payload(self, *, v1: bool = False) -> list[dict[str, Any]]:
        """The worktree list as ``list_worktrees`` / ``worktrees.changed``
        carry it. ``v1`` adds each agent's ``session_id`` and ``kind``; the
        legacy IPC answer stays without them."""
        result = []
        for wt in self.worktrees:
            agents = self.registry.agents(wt.path)
            entry: dict[str, Any] = {
                "path": wt.path,
                "branch": wt.branch,
                "head": wt.head,
                "is_main": wt.is_main,
                "is_bare": wt.is_bare,
                # Scalar roll-up kept for back-compat; `agents` has the detail.
                "agent_status": rollup_status(s.status for s in agents.values()).value,
                "agents": self.agents_payload(wt.path, v1=v1),
            }
            git_status = self.git_statuses.get(wt.path)
            if git_status:
                entry["git_status"] = {
                    "dirty_count": git_status.dirty_count,
                    "ahead": git_status.ahead,
                    "behind": git_status.behind,
                }
            result.append(entry)
        return result

    def agents_payload(self, key: str, *, v1: bool = False) -> list[dict[str, Any]]:
        """A worktree's agent states as the IPC responses carry them; ``v1``
        adds ``session_id`` and ``kind`` for the agents the engine runs, and
        lists those with no status yet as ``starting``."""
        result = []
        states = self.registry.agents(key)
        for aid, st in states.items():
            entry: dict[str, Any] = {
                "agent_id": aid,
                "label": st.label,
                "session_name": st.session_name,
                "status": st.status.value,
                "confidence": st.confidence.value,
                "detail": st.detail,
            }
            info = self.get_agent(key, aid) if v1 else None
            if info is not None:
                entry["session_id"] = info.session_id
                entry["kind"] = info.kind
            result.append(entry)
        if v1:
            for info in self.list_agents(key):
                if info.agent_id not in states:
                    result.append(
                        {
                            "agent_id": info.agent_id,
                            "label": info.label,
                            "session_name": info.session_name,
                            "status": STARTING_STATUS,
                            "confidence": LifecycleConfidence.LOW.value,
                            "detail": "",
                            "session_id": info.session_id,
                            "kind": info.kind,
                        }
                    )
        return result

    def _on_worktree_event(self, event: WorktreeEvent) -> None:
        if event.event == WORKTREES_CHANGED:
            data: Any = self.worktrees_payload(v1=True)
        elif event.event == GIT_STATUS_UPDATED:
            data = {path: asdict(status) for path, status in event.data.items()}
        elif event.event == PR_INFO_UPDATED:
            info = event.data["pr_info"]
            data = {
                "path": event.data["path"],
                "pr_info": asdict(info) if info else None,
            }
        else:
            return
        self.events.emit(event.event, data)

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    async def run_job(
        self,
        command: str,
        *,
        cwd: str | None = None,
        title: str = "",
        env: Mapping[str, str] | None = None,
    ) -> JobResult:
        """Run a shell command to completion, streaming its output.

        ``env`` is the environment it runs with (a client's, D5); ``None``
        means the engine's own.

        Emits ``job.started``, a ``job.output`` per chunk of stdout or
        stderr, and ``job.finished`` (always, even when the command could not
        be started; ``exit_code`` is then ``null``).
        """
        job_id = f"j{next(self._job_ids)}"
        self.events.emit(
            JOB_STARTED, {"job_id": job_id, "title": title, "command": command}
        )
        stdout: list[str] = []
        stderr: list[str] = []
        exit_code: int | None = None
        with diag.timed(log, "engine.job", job_id=job_id, title=title) as span:
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=cwd or None,
                    env=dict(env) if env is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._jobs[job_id] = proc
                try:
                    await asyncio.gather(
                        self._pump(job_id, proc.stdout, stdout),
                        self._pump(job_id, proc.stderr, stderr),
                    )
                    exit_code = await proc.wait()
                except asyncio.CancelledError:
                    _kill(proc)
                    raise
                finally:
                    self._jobs.pop(job_id, None)
            finally:
                span["exit_code"] = exit_code
                self.events.emit(
                    JOB_FINISHED,
                    {
                        "job_id": job_id,
                        "exit_code": exit_code
                        if exit_code is None or exit_code >= 0
                        else None,
                    },
                )
        return JobResult(job_id, exit_code, "".join(stdout), "".join(stderr))

    async def _pump(
        self, job_id: str, stream: asyncio.StreamReader | None, sink: list[str]
    ) -> None:
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            chunk = await stream.read(_JOB_READ_SIZE)
            text = decoder.decode(chunk, final=not chunk)
            if text:
                sink.append(text)
                self.events.emit(JOB_OUTPUT, {"job_id": job_id, "text": text})
            if not chunk:
                return

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def spawn_agent(
        self,
        worktree_path: str,
        instruction: str | None = None,
        *,
        skip_permissions: bool = False,
        resume_mode: ResumeMode = ResumeMode.NEW,
        label: str | None = None,
        kind: str = AGENT_KIND,
        env: Mapping[str, str] | None = None,
        spawn_id: str = "",
        provider_name: str | None = None,
        resume_conversation: str | None = None,
        agent_id: str | None = None,
    ) -> AgentInfo:
        """Start a new agent in a worktree (or the orchestrator, for
        ``kind="orchestrator"``, which runs in the repo root and ignores
        ``worktree_path`` and ``label``).

        ``env`` is the environment the agent starts from; ``None`` means the
        engine's own. Emits ``agent.spawned``. ``spawn_id`` is diagnostics
        only. A restore also passes the provider the agent ran with, the
        conversation to resume and the agent id to take again (a new one
        when it is in use).
        """
        provider_name = provider_name or self.config.agent.provider
        if kind == ORCHESTRATOR_KIND:
            info = self._spawn_orchestrator(
                instruction,
                skip_permissions=skip_permissions,
                resume_mode=resume_mode,
                env=env,
                provider_name=provider_name,
                resume_conversation=resume_conversation,
            )
            self._remember(info, provider_name, skip_permissions, instruction)
            return info
        if kind != AGENT_KIND:
            raise ValueError(f"Unknown agent kind: {kind!r}")
        if self.worktree(worktree_path) is None:
            raise ValueError(f"Unknown worktree: {worktree_path}")

        key = worktree_path
        if not agent_id or agent_id in self._agents.get(key, {}):
            agent_id = self.registry.new_agent_id(key)
        label = label or f"Agent {agent_id[1:]}"
        provider = self._provider(provider_name)
        session_name = (
            agent_session_name(key, agent_id) if provider.session_name_flag else ""
        )
        self.registry.register(key, agent_id, label=label, session_name=session_name)
        command, observer = self._agent_command(
            provider,
            worktree_path,
            skip_permissions=skip_permissions,
            resume_mode=resume_mode,
            instruction=instruction,
            session_name=session_name,
            env=env,
            spawn_id=spawn_id,
            resume_conversation=resume_conversation,
        )
        session = AgentSession(
            command,
            worktree_path=key,
            agent_id=agent_id,
            observer=observer,
            spawn_id=spawn_id,
        )
        info = self._start_agent(session, label, session_name, AGENT_KIND)
        self._remember(info, provider_name, skip_permissions, instruction)
        return info

    def _spawn_orchestrator(
        self,
        instruction: str | None,
        *,
        skip_permissions: bool,
        resume_mode: ResumeMode,
        env: Mapping[str, str] | None,
        provider_name: str,
        resume_conversation: str | None = None,
    ) -> AgentInfo:
        if self.has_agent(ORCHESTRATOR_KEY):
            raise ValueError("Orchestrator agent already running")
        self._stop_agent(ORCHESTRATOR_KEY, "")
        repo_root = self.repo_root
        provider = self._provider(provider_name)
        session_name = (
            agent_session_name(repo_root, "orchestrator")
            if provider.session_name_flag
            else ""
        )
        self.registry.register(ORCHESTRATOR_KEY, "", session_name=session_name)
        command, observer = self._agent_command(
            provider,
            repo_root,
            skip_permissions=skip_permissions,
            resume_mode=resume_mode,
            instruction=instruction,
            system_prompt=compose_orchestrator_prompt(self.config, repo_root),
            session_name=session_name,
            env=env,
            resume_conversation=resume_conversation,
        )
        session = AgentSession(
            command, worktree_path=ORCHESTRATOR_KEY, observer=observer
        )
        return self._start_agent(session, "", session_name, ORCHESTRATOR_KIND)

    def _agent_command(
        self,
        provider: AgentProvider,
        cwd: str,
        *,
        skip_permissions: bool,
        resume_mode: ResumeMode,
        instruction: str | None,
        session_name: str,
        env: Mapping[str, str] | None,
        system_prompt: str | None = None,
        spawn_id: str = "",
        resume_conversation: str | None = None,
    ):
        # build_runtime_context touches the filesystem for some providers
        # (hook scripts, settings files), so it is timed separately.
        with diag.timed(
            log,
            "spawn.build_runtime_context",
            spawn_id=spawn_id,
            provider=provider.name,
            worktree=cwd,
        ):
            runtime_context = provider.build_runtime_context(
                cwd,
                socket_path=self.socket_path,
                cross_session_inbound=self.config.agent.cross_session_inbound,
            )
        command = provider.build_command(
            cwd,
            skip_permissions=skip_permissions,
            runtime_context=runtime_context,
            resume_mode=resume_mode,
            instruction=instruction,
            system_prompt=system_prompt,
            session_name=session_name or None,
            env=env,
            resume_conversation=resume_conversation,
        )
        return command, provider.create_observer_from_context(runtime_context)

    def _start_agent(
        self, session: AgentSession, label: str, session_name: str, kind: str
    ) -> AgentInfo:
        key, agent_id = session.worktree_path, session.agent_id
        session.session_id = self._new_session_id()
        info = AgentInfo(key, agent_id, session.session_id, label, session_name, kind)
        remove_listener = session.add_listener(
            lambda event: self._on_agent_event(info, event)
        )
        self._agents.setdefault(key, {})[agent_id] = _Agent(
            session, info, remove_listener
        )
        self.events.emit(AGENT_SPAWNED, info.to_dict())
        try:
            session.start()
        except Exception:
            self._stop_agent(key, agent_id)
            raise
        return info

    def _new_session_id(self) -> str:
        return f"s{next(self._session_ids)}"

    def _on_agent_event(self, info: AgentInfo, event: SessionEvent) -> None:
        if isinstance(event, StatusChanged):
            state = self.registry.update_status(
                info.worktree_path,
                info.agent_id,
                event.status,
                event.confidence,
                event.detail,
                last_output_time=event.last_output_time,
            )
            # Every change, the detail-only ones included; agent.status
            # (the list_events log) only has the transitions.
            self.events.emit(
                AGENT_UPDATED,
                {
                    "worktree_path": info.worktree_path,
                    "agent_id": info.agent_id,
                    "status": state.status.value,
                    "confidence": state.confidence.value,
                    "detail": state.detail,
                },
            )
            self._note_conversation(info.worktree_path, info.agent_id)
        elif isinstance(event, SessionExited):
            self._forget_agent(
                info.worktree_path, info.agent_id, event.exit_code, stopped=False
            )

    def stop_agent(self, key: str, agent_id: str = "") -> AgentInfo:
        """Kill an agent (``key`` is a worktree path or ORCHESTRATOR_KEY).

        Emits ``agent.status`` (exited) and ``agent.exited`` with
        ``stopped: true``.
        """
        agent = self._agents.get(key, {}).get(agent_id)
        if agent is None:
            raise ValueError(f"No agent {agent_id!r} in this worktree")
        self._stop_agent(key, agent_id)
        return agent.info

    def _stop_agent(self, key: str, agent_id: str) -> None:
        agent = self._agents.get(key, {}).get(agent_id)
        if agent is None:
            return
        agent.remove_listener()
        agent.session.stop()
        self._forget_agent(key, agent_id, agent.session.exit_code, stopped=True)

    def _forget_agent(
        self, key: str, agent_id: str, exit_code: int | None, *, stopped: bool
    ) -> None:
        bucket = self._agents.get(key, {})
        agent = bucket.pop(agent_id, None)
        if not bucket:
            self._agents.pop(key, None)
        if agent is None:
            return
        agent.remove_listener()
        if self.manifest is not None:
            self.manifest.remove(key, agent_id)
        self.registry.drop(key, agent_id)
        self.registry.unregister(key, agent_id)
        self.events.emit(
            AGENT_EXITED,
            {
                "worktree_path": key,
                "agent_id": agent_id,
                "session_id": agent.info.session_id,
                "exit_code": exit_code,
                "stopped": stopped,
            },
        )

    def get_session(self, key: str, agent_id: str = "") -> AgentSession | None:
        agent = self._agents.get(key, {}).get(agent_id)
        return agent.session if agent is not None else None

    def find_session(self, session_id: str) -> TerminalSession | None:
        """The agent or shell session with this engine-wide id."""
        for bucket in self._agents.values():
            for agent in bucket.values():
                if agent.info.session_id == session_id:
                    return agent.session
        for shell in self._shells.values():
            if shell.session_id == session_id:
                return shell
        return None

    def get_agent(self, key: str, agent_id: str = "") -> AgentInfo | None:
        agent = self._agents.get(key, {}).get(agent_id)
        return agent.info if agent is not None else None

    def list_agents(self, key: str) -> list[AgentInfo]:
        """The agents the engine runs for ``key``, in spawn order."""
        return [agent.info for agent in self._agents.get(key, {}).values()]

    def agent_ids(self, key: str) -> list[str]:
        return list(self._agents.get(key, {}))

    def agent_states(self, key: str) -> dict[str, AgentState]:
        """The registry's view of ``key``'s agents (may be empty)."""
        return self.registry.agents(key)

    def agent_count(self) -> int:
        """Agents (the orchestrator included) this engine runs."""
        return sum(len(bucket) for bucket in self._agents.values())

    def has_agent(self, key: str) -> bool:
        """Whether any agent of ``key`` has a live process."""
        return any(
            agent.session.running for agent in self._agents.get(key, {}).values()
        )

    async def send_agent_input(
        self, key: str, agent_id: str, text: str, *, submit: bool = True
    ) -> None:
        session = self.get_session(key, agent_id)
        if session is None or not session.running:
            raise ValueError("Agent terminal is not ready")
        await session.send_input(text, submit=submit)
        if self.manifest is not None:
            self.manifest.update(key, agent_id, instruction=clip_instruction(text))

    def read_agent_output(
        self, key: str, agent_id: str, lines: int
    ) -> tuple[list[str], int]:
        """The last ``lines`` lines of an agent's screen and scrollback, and
        how many lines it holds in all."""
        session = self.get_session(key, agent_id)
        if session is None:
            raise ValueError("No agent terminal in this worktree")
        return session.tail_lines(lines), session.total_lines

    def _agent_sessions(self) -> list[AgentSession]:
        return [
            agent.session
            for bucket in self._agents.values()
            for agent in bucket.values()
        ]

    def poll_agents(self) -> None:
        """Pick up every agent's latest hook/telemetry events."""
        for session in self._agent_sessions():
            session.poll()
        if self.manifest is not None:
            for bucket in self._agents.values():
                for agent in bucket.values():
                    self._note_conversation(
                        agent.info.worktree_path, agent.info.agent_id
                    )

    def check_hangs(self) -> None:
        """Flag the working agents that have gone quiet for too long."""
        for session in self._agent_sessions():
            if session.status == AgentStatus.RUNNING:
                session.check_hang()

    # ------------------------------------------------------------------
    # Shells
    # ------------------------------------------------------------------

    def open_shell(
        self, worktree_path: str, *, env: Mapping[str, str] | None = None
    ) -> TerminalSession:
        """The worktree's ``bash -l`` session, started on first use.

        ``env`` is the environment a new shell starts from; ``None`` means
        the engine's own. A shell that has exited is replaced.
        """
        shell = self._shells.get(worktree_path)
        if shell is not None and shell.running:
            return shell
        script = f"{env_exports(base=env)} && cd {shlex.quote(worktree_path)} && exec bash -l"
        shell = TerminalSession(
            f"bash -c {shlex.quote(script)}", worktree_path=worktree_path
        )
        shell.session_id = self._new_session_id()
        shell.add_listener(lambda event: self._on_shell_event(shell, event))
        self._shells[worktree_path] = shell
        shell.start()
        return shell

    def get_shell(self, worktree_path: str) -> TerminalSession | None:
        return self._shells.get(worktree_path)

    def close_shell(self, worktree_path: str) -> None:
        shell = self._shells.pop(worktree_path, None)
        if shell is not None:
            shell.stop()

    def _on_shell_event(self, shell: TerminalSession, event: SessionEvent) -> None:
        if (
            isinstance(event, SessionExited)
            and self._shells.get(shell.worktree_path) is shell
        ):
            del self._shells[shell.worktree_path]

    @staticmethod
    def write_input(session: TerminalSession, data: bytes | str) -> None:
        """Send raw input (keys, a command line) to a session."""
        session.write(data)


    # ------------------------------------------------------------------
    # Restart manifest (PR 9)
    # ------------------------------------------------------------------

    def open_manifest(self, path: os.PathLike[str] | str | None = None) -> Manifest:
        """Keep the restart manifest (at ``path``, else the repo's in the
        state dir) and take over what earlier engines left in it."""
        if self.manifest is None:
            root = self.repo_root
            self.manifest = Manifest(
                Path(path) if path is not None else manifest_path(root),
                repo_root=root,
                engine_id=self.instance_id,
            )
            self.manifest.load(self._restorable_exists)
            count = len(self.manifest.restorable)
            if count:
                log.info("%d agent(s) of an earlier engine can be restored", count)
        return self.manifest

    def _restorable_exists(self, entry: Entry) -> bool:
        if entry.kind == ORCHESTRATOR_KIND:
            return bool(self.repo_root) and os.path.isdir(self.repo_root)
        return self.worktree(entry.worktree_path) is not None

    def _remember(
        self,
        info: AgentInfo,
        provider_name: str,
        skip_permissions: bool,
        instruction: str | None,
    ) -> None:
        if self.manifest is None:
            return
        self.manifest.add(
            Entry(
                id=Entry.new_id(),
                worktree_path=info.worktree_path,
                agent_id=info.agent_id,
                kind=info.kind,
                provider=provider_name,
                label=info.label,
                session_name=info.session_name,
                skip_permissions=skip_permissions,
                instruction=clip_instruction(instruction),
            )
        )

    def _note_conversation(self, key: str, agent_id: str) -> None:
        if self.manifest is None:
            return
        session = self.get_session(key, agent_id)
        conversation = session.conversation_id if session is not None else None
        if conversation:
            self.manifest.update(key, agent_id, conversation_id=conversation)

    def _require_manifest(self) -> Manifest:
        if self.manifest is None:
            raise ValueError("This engine keeps no restart manifest")
        return self.manifest

    def list_restorable(self) -> list[Entry]:
        """Agents earlier engines ran when they ended, oldest first."""
        manifest = self._require_manifest()
        manifest.prune(self._restorable_exists)
        return sorted(manifest.restorable.values(), key=lambda e: e.started_at)

    def dismiss_restorable(self, ids: Iterable[str] | None = None) -> list[str]:
        """Stop offering these agents (every one when ``ids`` is None)."""
        manifest = self._require_manifest()
        wanted = list(manifest.restorable) if ids is None else list(ids)
        return [entry.id for entry in manifest.take(wanted)]

    async def restore_agents(
        self,
        ids: Iterable[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Spawn restorable agents again (every one when ``ids`` is None),
        each resuming its conversation: the one its provider reported, else
        its provider's "last" (``resume_mode=last``). Same worktree, kind,
        provider, label, permissions and, when free, agent id. Emits
        ``engine.restored``; a failed entry stays restorable."""
        manifest = self._require_manifest()
        manifest.prune(self._restorable_exists)
        wanted = list(manifest.restorable if ids is None else dict.fromkeys(ids))
        restored: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = [
            {"id": i, "reason": "not restorable (unknown id, or its worktree is gone)"}
            for i in wanted
            if i not in manifest.restorable
        ]
        for entry in manifest.take(wanted):
            try:
                info = await self.spawn_agent(
                    entry.worktree_path,
                    skip_permissions=entry.skip_permissions,
                    resume_mode=ResumeMode.RESUME_LAST,
                    label=entry.label or None,
                    kind=entry.kind,
                    env=env,
                    provider_name=entry.provider,
                    resume_conversation=entry.conversation_id,
                    agent_id=entry.agent_id or None,
                )
            except (ValueError, KeyError, OSError, WorktreeManagerError) as e:
                manifest.restorable[entry.id] = entry
                manifest.mark_dirty()
                failed.append(
                    {
                        "id": entry.id,
                        "worktree_path": entry.worktree_path,
                        "agent_id": entry.agent_id,
                        "reason": str(e) or type(e).__name__,
                    }
                )
                continue
            manifest.update(
                info.worktree_path,
                info.agent_id,
                instruction=entry.instruction,
                conversation_id=entry.conversation_id,
            )
            restored.append(
                {
                    "id": entry.id,
                    **info.to_dict(),
                    "resumed": "conversation" if entry.conversation_id else "last",
                }
            )
        result = {"restored": restored, "failed": failed}
        self.events.emit(ENGINE_RESTORED, result)
        return result


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
