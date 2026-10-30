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
import secrets
import shlex
import uuid
from collections.abc import Callable, Collection, Coroutine, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent import diagnostics as diag
from lazyagent.agent_providers import (
    PERMISSION_BYPASS,
    PERMISSION_CLI,
    AgentProvider,
    ResumeMode,
    agent_session_name,
    env_exports,
    get_agent_provider,
    resolve_permission_mode,
)
from lazyagent.config import Config, format_command, load_config
from lazyagent.engine.events import (
    AGENT_EXITED,
    AGENT_SPAWNED,
    AGENT_UPDATED,
    ENGINE_NOTICE,
    ENGINE_RESTORED,
    ENGINE_SHUTDOWN,
    GITHUB_UPDATED,
    JOB_FINISHED,
    JOB_OUTPUT,
    JOB_STARTED,
    NOTES_REMOVED,
    NOTES_UPDATED,
    RESTORABLE_CHANGED,
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
from lazyagent.engine.actionables import ActionableQueue, actionables_path, tracker_items
from lazyagent.engine.attest import Attestations
from lazyagent.engine.owner_approvals import OwnerApprovals
from lazyagent.engine.review_commits import ReviewCommits
from lazyagent.engine.autodelegate import AutoDelegate
from lazyagent.engine.claims import Claims
from lazyagent.engine.delegation import Delegations
from lazyagent.engine.explain import Explainer
from lazyagent.engine.decisions import Decisions
from lazyagent.engine.orchestrator import FirstResponder
from lazyagent.engine.overlay import Overlay, overlay_path
from lazyagent.engine.methods import SpawnCaps
from lazyagent.engine.notes import (
    ADOPTED,
    DELETED,
    MODES as NOTES_MODES,
    ORPHAN,
    Meta,
    Notes,
    NotesStore,
    is_empty,
    notes_dir,
    prune_engine_dirs,
)
from lazyagent.engine.reviews import ReviewStore, reviews_path
from lazyagent.engine.scheduler import Scheduler
from lazyagent.engine.rules.hub import RulesHub
from lazyagent.engine.status import TICK as STATUS_TICK
from lazyagent.engine.status import StatusProjection
from lazyagent.engine.actions import ActionsHub
from lazyagent.engine.review_requests import ReviewRequestHub
from lazyagent.engine.trackers import TrackerConfig, TrackerHub, load_trackers
from lazyagent.engine.work_items import WorkItems, work_items_path
from lazyagent.engine.session import (
    AgentSession,
    SessionEvent,
    SessionExited,
    StatusChanged,
    TerminalSession,
    env_hang_seconds,
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
from lazyagent.worker_prompt import worker_system_prompt
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
# Orphan notes expire in days; an hourly sweep is plenty.
NOTES_GC_INTERVAL = 3600.0

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
        tracker_loader: Callable[[str], list[TrackerConfig]] | None = None,
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
        self._tracker_loader = tracker_loader or (lambda root: load_trackers(root))
        self._provider = provider_lookup or (lambda name: get_agent_provider(name))
        self.config = Config()
        # Optional issue trackers (proposal 0013): none until the config loads.
        self.trackers = TrackerHub()
        # GitHub Actions runs on the worktrees' branches (I7): off until the
        # config loads.
        self.actions = ActionsHub()
        # The open PRs that ask the user for a review: off until the config
        # loads.
        self.review_requests = ReviewRequestHub()

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
        # The live agents spawned resuming their worktree's last conversation
        # (--continue): until their provider reports which one, they may be
        # running a restorable entry (see _live_match).
        self._resumed_last: set[tuple[str, str]] = set()
        # Restorable ids consumed because their agent is live here, and
        # where it runs: why restore_agents refuses them.
        self._consumed: dict[str, str] = {}
        # The restorable ids clients were last told about (at load, by
        # list_restorable; then by restorable.changed).
        self._offered: list[str] = []
        # While restore_agents runs: its engine.restored is the change notice.
        self._restoring = False
        # Session notes (proposal 0011): the store, opened with the manifest
        # (or, without one, on the first spawn), and each live agent's notes
        # id by slot.
        self.notes: NotesStore | None = None
        self._notes_ids: dict[tuple[str, str], str] = {}
        # Notes whose agent's first prompt is still to pin: fresh
        # conversations only (a resumed one's first prompt is not the
        # original instruction, Q-N24).
        self._unpinned: set[str] = set()
        # Each live agent's token (in its MCP server's env) → its slot: who
        # calls the notes methods. In memory only: agents die with us.
        self._tokens: dict[str, tuple[str, str]] = {}
        # Review comments sent to agents and their replies (review_reply);
        # opened on first use.
        self.reviews: ReviewStore | None = None
        # Random per engine process: the v1 engine_id, and the manifest's.
        self.instance_id = uuid.uuid4().hex
        self.worktree_service = WorktreeService(
            self._manager,
            find_root=find_root or (lambda: find_repo_root()),
        )
        self.worktree_service.subscribe(self._on_worktree_event)
        self.worktree_service.github.on_change = self._github_changed
        # Work items (proposal 0013): kept current from the events above,
        # with the inbox queue they read.
        self.actionables = ActionableQueue(self)
        self.actionables.add_detector(lambda snap: tracker_items(self.trackers, snap))
        # What rows cannot derive: gestures, reports, tombstones, timeline,
        # seen marks and briefs.
        self.overlay = Overlay(self)
        self.actionables.add_detector(self.overlay.items)
        # Work items from external feeders (work_items_upsert).
        self.work_items = WorkItems(self)
        self.status = StatusProjection(self)
        # The rule files (proposal 0014 R2): the catalog the rows use.
        self.rules = RulesHub(lambda: self.repo_root, self.events.emit)
        self.worktree_service.wants_review = lambda: self.rules.catalog.reads_review
        self.worktree_service.base_merges.paths_of = lambda: self.rules.catalog.generated_paths
        # Inbox items take their work item's priority.
        self.actionables.filters.append(self.status.prioritize)
        # Pass to orchestrator (claims on work items): rows and queue read it.
        self.claims = Claims(self)
        self.decisions = Decisions(self)
        # Inbox items the user delegated to the branch's agent.
        self.delegations = Delegations(self)
        # Attestations for nondeterministic CI checks (engine/attest.py).
        self.attestations = Attestations(self)
        # The user's approvals of PR heads, outside GitHub (R3d).
        self.owner_approvals = OwnerApprovals(self)
        self.review_commits = ReviewCommits(self)
        # The orchestrator as first responder: routing, presence, mandate,
        # caps, revert (engine/orchestrator.py); opt-in auto-delegation.
        self.first_responder = FirstResponder(self)
        # Explain this and Request a new brief (engine/explain.py, I5).
        self.explainer = Explainer(self)
        self.autodelegate = AutoDelegate(self)
        # The caps on spawns asked for by agents (engine/methods.py).
        self.spawn_caps = SpawnCaps()

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
        self._scheduler.every(
            NOTES_GC_INTERVAL, diag.wrap_timer("timer.notes_gc", self._notes_gc)
        )
        self._scheduler.every(
            STATUS_TICK, diag.wrap_timer("timer.status", self.status.tick)
        )
        self._scheduler.start()
        # Git status and PR info are polled by the service, once a minute and
        # only for the clients' interest sets (PR info also for every work
        # item: the status projection's own interest).
        self.worktree_service.start()
        # Marks are kept by the engine that owns the repo (as the manifest).
        if self.manifest is not None and self.repo_root:
            self.actionables.open(actionables_path(self.repo_root))
            self.overlay.open(overlay_path(self.repo_root))
            self.work_items.open(work_items_path(self.repo_root))
        self.actionables.start()
        self.status.start()
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
        self.status.stop()
        self.actionables.stop()
        self.overlay.save()
        self.work_items.save()
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
        self._tokens.clear()
        for shell in list(self._shells.values()):
            shell.stop()
        self._shells.clear()
        # The agents just stopped stay in it: the next engine offers them.
        if self.manifest is not None:
            await self.manifest.close()
        if self.notes is not None:
            await self.notes.flush()

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
        self.trackers = TrackerHub.from_configs(
            self._tracker_loader(root) if root else [], root
        )
        poll_s = self.config.status.actions_poll_s
        if self.actions.repo_root != (root or ""):
            self.actions = ActionsHub(root or "", poll_s=poll_s)
        else:
            self.actions.poll_s = poll_s
        wanted = self.config.status.review_requests
        if self.review_requests.repo_root != (root or ""):
            self.review_requests = ReviewRequestHub(root or "", enabled=wanted)
        elif not wanted:
            self.review_requests.turn_off()
        else:
            self.review_requests.enabled_config = True

    def get_config(self) -> ConfigView:
        config = self.config
        return ConfigView(
            agent_provider=config.agent.provider,
            default_branch=config.default_branch,
            has_custom_create=config.has_custom_create,
            has_custom_remove=config.has_custom_remove,
            permission_mode=config.agent.permission_mode or PERMISSION_CLI,
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

    def ui_connected(self) -> bool:
        """Whether a UI is connected: a hosted one, or a protocol v1 client
        (the desktop). Presence counts the user away without one."""
        v1 = self._v1_server
        return self.ui_attached or (v1 is not None and v1.connections > 0)

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

    async def get_pr_diff(
        self,
        path: str,
        *,
        base: str | None = None,
        max_bytes: int | None = None,
        fetch: bool = False,
    ) -> dict[str, Any]:
        """``get_diff`` with ``pr``: see ``WorktreeService.get_pr_diff``."""
        return await self.worktree_service.get_pr_diff(
            path,
            base=base,
            default_branch=self.config.default_branch,
            max_bytes=max_bytes,
            fetch=fetch,
        )

    def get_pr_info(self, path: str) -> PrInfo | None:
        """The cached PR info of a worktree (``None`` also when not fetched
        yet; see :meth:`has_pr_info`)."""
        return self.worktree_service.pr_info(path)

    def _github_changed(self) -> None:
        """GitHub stopped answering or answers again: clients hear it, and
        the rows read pr.exists again (unknown during an outage)."""
        self.events.emit(GITHUB_UPDATED, self.worktree_service.github.to_dict())
        self.status.mark_dirty()

    def has_pr_info(self, path: str) -> bool:
        return self.worktree_service.has_pr_info(path)

    def pr_known(self, path: str) -> bool:
        """See :meth:`WorktreeService.pr_known`."""
        return self.worktree_service.pr_known(path)

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
        # Its ended agents' notes go with it.
        if self.notes is not None:
            for meta in self.notes.list(path):
                if meta.notes_id not in self._notes_ids.values():
                    self.notes.delete(meta.notes_id, DELETED)

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
            info = self.get_agent(key, aid)
            if info is not None:
                entry["permission_mode"] = info.permission_mode
            if v1 and info is not None:
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
                            "permission_mode": info.permission_mode,
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
        skip_permissions: bool | None = None,
        permission_mode: str | None = None,
        default_permission_mode: str = PERMISSION_CLI,
        resume_mode: ResumeMode = ResumeMode.NEW,
        label: str | None = None,
        kind: str = AGENT_KIND,
        env: Mapping[str, str] | None = None,
        spawn_id: str = "",
        provider_name: str | None = None,
        resume_conversation: str | None = None,
        agent_id: str | None = None,
        notes_id: str | None = None,
        notes_mode: str | None = None,
        review_comment_ids: list[str] | None = None,
    ) -> AgentInfo:
        """Start a new agent in a worktree (or the orchestrator, for
        ``kind="orchestrator"``, which runs in the repo root and ignores
        ``worktree_path`` and ``label``).

        ``env`` is the environment the agent starts from; ``None`` means the
        engine's own. Emits ``agent.spawned``. ``spawn_id`` is diagnostics
        only. A restore also passes the provider the agent ran with, the
        conversation to resume, the agent id to take again (a new one
        when it is in use) and the session notes to carry over (``notes_id``;
        new notes from the template when missing). A fresh conversation
        (``resume_mode=new``, no ``notes_id``) is its worktree's most recent
        agent: the notes of the agents that ended there are deleted.
        ``review_comment_ids``: review comments in ``instruction`` the agent
        may answer with review_reply, registered before it starts.

        The permission mode: ``permission_mode``, else ``skip_permissions``,
        else ``[agents] permission_mode``, else ``default_permission_mode``
        (agent_providers.resolve_permission_mode).
        """
        if notes_mode is not None and notes_mode not in NOTES_MODES:
            raise ValueError(f"Unknown notes mode: {notes_mode!r}")
        mode = resolve_permission_mode(
            permission_mode,
            skip_permissions,
            self.config.agent.permission_mode,
            default_permission_mode,
        )
        provider_name = provider_name or self.config.agent.provider
        resume_last = resume_mode == ResumeMode.RESUME_LAST and not resume_conversation
        if kind == ORCHESTRATOR_KIND:
            if resume_last and notes_id is None:
                entry = self._last_orchestrator(provider_name)
                if entry is not None:
                    resume_conversation = entry.conversation_id
                    notes_id = entry.notes_id
            info = self._spawn_orchestrator(
                instruction,
                permission_mode=mode,
                resume_mode=resume_mode,
                env=env,
                provider_name=provider_name,
                resume_conversation=resume_conversation,
                notes_id=notes_id,
                notes_mode=notes_mode,
            )
            self._remember(
                info,
                provider_name,
                instruction,
                resume_mode=resume_mode,
                resume_conversation=resume_conversation,
            )
            return info
        if kind != AGENT_KIND:
            raise ValueError(f"Unknown agent kind: {kind!r}")
        if self.worktree(worktree_path) is None:
            raise ValueError(f"Unknown worktree: {worktree_path}")
        if resume_last:
            self._check_continue_in_root(worktree_path, provider_name)

        key = worktree_path
        if not agent_id or agent_id in self._agents.get(key, {}):
            agent_id = self.registry.new_agent_id(key)
        label = label or f"Agent {agent_id[1:]}"
        provider = self._provider(provider_name)
        session_name = (
            agent_session_name(key, agent_id) if provider.session_name_flag else ""
        )
        self.registry.register(key, agent_id, label=label, session_name=session_name)
        token = secrets.token_urlsafe(24)
        # Before the command: Claude's notes hook gets the file's path.
        self._open_notes(
            key,
            agent_id,
            AGENT_KIND,
            notes_id=notes_id,
            resume_mode=resume_mode,
            mode=notes_mode,
            instruction=instruction,
            provider=provider,
        )
        try:
            command, observer = self._agent_command(
                provider,
                worktree_path,
                permission_mode=mode,
                resume_mode=resume_mode,
                instruction=instruction,
                system_prompt=worker_system_prompt(self.config, provider, instruction),
                session_name=session_name,
                env=env,
                spawn_id=spawn_id,
                resume_conversation=resume_conversation,
                agent_token=token,
                notes_path=self._notes_path(key, agent_id),
            )
        except Exception:
            self._end_notes(self._notes_ids.pop((key, agent_id), None), keep=True)
            raise
        session = AgentSession(
            command,
            worktree_path=key,
            agent_id=agent_id,
            observer=observer,
            spawn_id=spawn_id,
        )
        self._tokens[token] = (key, agent_id)
        if review_comment_ids:
            self.review_store().register(key, agent_id, review_comment_ids)
        info = self._start_agent(session, label, session_name, AGENT_KIND, mode)
        self._remember(
            info,
            provider_name,
            instruction,
            resume_mode=resume_mode,
            resume_conversation=resume_conversation,
        )
        return info

    def recorded_permission_mode(self, path: str) -> str | None:
        """The permission mode of the worktree's most recent agent the
        manifest records (running or restorable); None with none."""
        if self.manifest is None:
            return None
        entries = [
            e for e in (*self.manifest.agents.values(), *self.manifest.restorable.values())
            if e.worktree_path == path and e.kind == AGENT_KIND
        ]
        if not entries:
            return None
        return self.entry_permission_mode(max(entries, key=lambda e: e.started_at))

    def entry_permission_mode(self, entry: Entry) -> str:
        """The mode a manifest entry's agent comes back in: the one it
        recorded; an older engine's entry recorded only bypass or not (not
        bypass: the configured mode, unless that is bypass)."""
        return resolve_permission_mode(
            entry.permission_mode,
            entry.skip_permissions,
            self.config.agent.permission_mode,
        )

    def _last_orchestrator(self, provider_name: str) -> Entry | None:
        """The restorable orchestrator a "resume last" of the orchestrator
        brings back: its conversation and its notes, as a restore would.
        Refused while that conversation runs here as an agent."""
        if self.manifest is None:
            return None
        for entry in self.manifest.restorable.values():
            if entry.kind != ORCHESTRATOR_KIND or entry.provider != provider_name:
                continue
            match = self._live_match(entry)
            if match is None:
                return entry
            if match[1].kind != ORCHESTRATOR_KIND:
                raise ValueError(
                    "The orchestrator's conversation runs in this engine as "
                    f"{self._live_where(match[1])}: stop that agent, then "
                    "restore the orchestrator"
                )
        return None

    def _check_continue_in_root(self, worktree_path: str, provider_name: str) -> None:
        """Refuse an agent's "resume last" in the repo root while there is
        an orchestrator of the same provider (running, or restorable): it
        runs in the repo root too, so the provider's last conversation
        there is the orchestrator's, and the agent would take it over under
        an agent's name, prompt and notes."""
        root = self.repo_root
        if not root or os.path.realpath(worktree_path) != os.path.realpath(root):
            return
        live = self.manifest.get(ORCHESTRATOR_KEY, "") if self.manifest else None
        if (live is not None and live.provider == provider_name) or (
            self.manifest is None and self.has_agent(ORCHESTRATOR_KEY)
        ):
            raise ValueError(
                "Resuming the last conversation in the repo root would resume "
                "the running orchestrator's (it runs there too). Start this "
                "agent with resume_mode=new, or pick its conversation"
            )
        if self.manifest is None:
            return
        for entry in self.manifest.restorable.values():
            if entry.kind == ORCHESTRATOR_KIND and entry.provider == provider_name:
                raise ValueError(
                    "Resuming the last conversation in the repo root would "
                    "take the orchestrator's (it runs there too) as a plain "
                    "agent. Restore the orchestrator instead (restore_agents "
                    f"ids=[{entry.id!r}], or spawn kind=orchestrator with "
                    "resume_mode=last), or start this agent with "
                    "resume_mode=new, or pick its conversation"
                )

    def _spawn_orchestrator(
        self,
        instruction: str | None,
        *,
        permission_mode: str,
        resume_mode: ResumeMode,
        env: Mapping[str, str] | None,
        provider_name: str,
        resume_conversation: str | None = None,
        notes_id: str | None = None,
        notes_mode: str | None = None,
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
        token = secrets.token_urlsafe(24)
        self._open_notes(
            ORCHESTRATOR_KEY,
            "",
            ORCHESTRATOR_KIND,
            notes_id=notes_id,
            resume_mode=resume_mode,
            mode=notes_mode,
            instruction=instruction,
            provider=provider,
        )
        try:
            command, observer = self._agent_command(
                provider,
                repo_root,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                instruction=instruction,
                system_prompt=compose_orchestrator_prompt(self.config, repo_root),
                session_name=session_name,
                env=env,
                resume_conversation=resume_conversation,
                agent_token=token,
                notes_path=self._notes_path(ORCHESTRATOR_KEY, ""),
            )
        except Exception:
            slot = (ORCHESTRATOR_KEY, "")
            self._end_notes(self._notes_ids.pop(slot, None), keep=True)
            raise
        session = AgentSession(
            command, worktree_path=ORCHESTRATOR_KEY, observer=observer
        )
        self._tokens[token] = (ORCHESTRATOR_KEY, "")
        return self._start_agent(
            session, "", session_name, ORCHESTRATOR_KIND, permission_mode
        )

    def _agent_command(
        self,
        provider: AgentProvider,
        cwd: str,
        *,
        permission_mode: str,
        resume_mode: ResumeMode,
        instruction: str | None,
        session_name: str,
        env: Mapping[str, str] | None,
        system_prompt: str | None = None,
        spawn_id: str = "",
        resume_conversation: str | None = None,
        agent_token: str | None = None,
        notes_path: str | None = None,
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
                agent_token=agent_token,
                notes_path=notes_path,
            )
        command = provider.build_command(
            cwd,
            permission_mode=permission_mode,
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
        self,
        session: AgentSession,
        label: str,
        session_name: str,
        kind: str,
        permission_mode: str = PERMISSION_CLI,
    ) -> AgentInfo:
        key, agent_id = session.worktree_path, session.agent_id
        session.session_id = self._new_session_id()
        info = AgentInfo(
            key, agent_id, session.session_id, label, session_name, kind, permission_mode
        )
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
            # Kept: a failed restore leaves its entry, and so its notes.
            self._stop_agent(key, agent_id, notes="keep")
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
                question=event.question,
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

    def stop_agent(
        self, key: str, agent_id: str = "", *, notes: str = "delete"
    ) -> AgentInfo:
        """Kill an agent (``key`` is a worktree path or ORCHESTRATOR_KEY).

        Emits ``agent.status`` (exited) and ``agent.exited`` with
        ``stopped: true``. Its session notes are deleted, or with
        ``notes="keep"`` kept as an orphan (see :meth:`stop_agent_with_notes`).
        """
        return self.stop_agent_with_notes(key, agent_id, notes=notes)[0]

    def stop_agent_with_notes(
        self, key: str, agent_id: str = "", *, notes: str = "delete"
    ) -> tuple[AgentInfo, str | None]:
        """:meth:`stop_agent`, and the agent's final notes text (None when
        it had none, or only the template): the handoff of a caller with
        no UI to offer one."""
        if notes not in ("delete", "keep"):
            raise ValueError(f"notes must be 'delete' or 'keep', not {notes!r}")
        agent = self._agents.get(key, {}).get(agent_id)
        if agent is None:
            raise ValueError(f"No agent {agent_id!r} in this worktree")
        text = self._stop_agent(key, agent_id, notes=notes)
        return agent.info, text

    def _stop_agent(
        self, key: str, agent_id: str, *, notes: str = "delete"
    ) -> str | None:
        agent = self._agents.get(key, {}).get(agent_id)
        if agent is None:
            return None
        agent.remove_listener()
        agent.session.stop()
        return self._forget_agent(
            key, agent_id, agent.session.exit_code, stopped=True, notes=notes
        )

    def _forget_agent(
        self,
        key: str,
        agent_id: str,
        exit_code: int | None,
        *,
        stopped: bool,
        notes: str = "delete",
    ) -> str | None:
        """Drop a dead agent; its notes are deleted on a stop (unless
        ``notes="keep"``) and kept as an orphan when it exited on its own.
        The final notes text, when not empty."""
        bucket = self._agents.get(key, {})
        agent = bucket.pop(agent_id, None)
        if not bucket:
            self._agents.pop(key, None)
        if agent is None:
            return None
        agent.remove_listener()
        self._resumed_last.discard((key, agent_id))
        for token, slot in list(self._tokens.items()):
            if slot == (key, agent_id):
                del self._tokens[token]
        if self.manifest is not None:
            self.manifest.remove(key, agent_id)
            # An entry hidden while this agent might have been running it is
            # offered again.
            self._reconcile_restorable()
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
        return self._end_notes(
            self._notes_ids.pop((key, agent_id), None),
            keep=not stopped or notes == "keep",
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
        self.status.input_sent(key, agent_id)
        if self.manifest is not None:
            self.manifest.update(key, agent_id, instruction=clip_instruction(text))

    async def head_sha(self, path: str) -> str | None:
        """The worktree's HEAD commit now (``git rev-parse``), else the one
        the last worktree scan saw."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", path, "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 5.0)
            except asyncio.TimeoutError:
                proc.kill()
                raise
            sha = out.decode().strip()
            if proc.returncode == 0 and sha:
                return sha
        except (OSError, asyncio.TimeoutError):
            pass
        wt = self.worktree(path)
        return wt.head if wt is not None and wt.head else None

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
        if self.manifest is not None or self.notes is not None:
            for bucket in self._agents.values():
                for agent in bucket.values():
                    self._note_conversation(
                        agent.info.worktree_path, agent.info.agent_id
                    )

    def check_hangs(self) -> None:
        """Flag the working agents that have gone quiet for too long."""
        seconds = self.hang_seconds
        for session in self._agent_sessions():
            if session.status == AgentStatus.RUNNING:
                session.check_hang(seconds)

    @property
    def hang_seconds(self) -> float:
        """``[inbox] hang_seconds`` of the repo's .lazyagent.toml, else of
        the global config.toml, else the environment's
        ``LAZYAGENT_HANG_SECONDS`` (every repo), else 600."""
        own = self.config.inbox.hang_seconds
        if own is not None:
            return float(own)
        return env_hang_seconds()

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
            self._offered = [e.id for e in self._offerable()]
            count = len(self.manifest.restorable)
            if count:
                log.info("%d agent(s) of an earlier engine can be restored", count)
            # The engine that keeps the manifest owns the repo's notes.
            if self.notes is None and self.config.notes.enabled:
                root = notes_dir(self.repo_root)
                prune_engine_dirs(root, self.config.notes.orphan_days)
                self.notes = self._new_notes_store(root)
                self.notes.load()
            self._notes_gc()
        return self.manifest

    def _restorable_exists(self, entry: Entry) -> bool:
        if entry.kind == ORCHESTRATOR_KIND:
            return bool(self.repo_root) and os.path.isdir(self.repo_root)
        return self.worktree(entry.worktree_path) is not None

    # ------------------------------------------------------------------
    # Session notes (proposal 0011)
    # ------------------------------------------------------------------

    def _new_notes_store(self, root: Path) -> NotesStore:
        config = self.config.notes
        return NotesStore(
            root,
            max_bytes=config.max_bytes,
            sections=config.sections,
            orphan_days=config.orphan_days,
            listener=self._on_notes_event,
        )

    def _notes_store(self) -> NotesStore | None:
        """The store; an engine without a manifest (another one holds the
        repo, or a throwaway engine) keeps its own, in a dir of its own
        that the holding engine expires."""
        if self.notes is None and self.config.notes.enabled and self.repo_root:
            root = notes_dir(self.repo_root) / f"engine-{self.instance_id[:12]}"
            self.notes = self._new_notes_store(root)
        return self.notes

    def _on_notes_event(self, kind: str, data: dict[str, Any]) -> None:
        self.events.emit(NOTES_UPDATED if kind == "updated" else NOTES_REMOVED, data)

    def notes_id(self, key: str, agent_id: str = "") -> str | None:
        """The live agent's notes id."""
        return self._notes_ids.get((key, agent_id))

    def adopt_notes(
        self,
        notes_id: str,
        key: str,
        agent_id: str = "",
        *,
        if_rev: int | None = None,
        by: str = "user",
    ) -> Notes:
        """Recover ended notes (an orphan, or a restorable entry's) into a
        live agent: its notes take their text (its notes id, so its path,
        stays), then the ended ones go (``notes.removed`` reason
        ``adopted``). ``if_rev`` guards the live agent's current text."""
        store = self.notes
        target = self._notes_ids.get((key, agent_id))
        if store is None or target is None:
            raise ValueError(f"No notes for agent {agent_id!r} in {key}")
        if notes_id == target or notes_id in self._notes_ids.values():
            raise ValueError(f"Notes {notes_id!r} belong to a live agent")
        source = store.get(notes_id)
        current = store.get(target).meta.rev
        notes = store.put(
            target, source.text, current if if_rev is None else if_rev, by
        )
        store.delete(notes_id, ADOPTED)
        self._notes_gc()
        return notes

    def delete_notes(self, notes_id: str) -> None:
        """Drop ended notes (an orphan, or a restorable entry's); a live
        agent's go with it (stop_agent)."""
        store = self.notes
        if store is None or notes_id not in store:
            raise ValueError(f"No notes {notes_id!r}")
        if notes_id in self._notes_ids.values():
            raise ValueError(
                f"Notes {notes_id!r} belong to a live agent: stop it to drop them"
            )
        store.delete(notes_id, DELETED)
        self._notes_gc()

    def token_slot(self, token: str) -> tuple[str, str] | None:
        """The live agent (``(key, agent_id)``) an agent token was minted
        for; None once it is gone, or for a token this engine never gave."""
        return self._tokens.get(token)

    def review_store(self) -> ReviewStore:
        """The review replies; kept on disk by the engine that keeps the
        manifest (it owns the repo), in memory by any other."""
        if self.reviews is None:
            path = None
            if self.manifest is not None and self.repo_root:
                path = reviews_path(self.repo_root)
            self.reviews = ReviewStore(path)
            self.reviews.load()
        return self.reviews

    def _open_notes(
        self,
        key: str,
        agent_id: str,
        kind: str,
        *,
        notes_id: str | None,
        resume_mode: ResumeMode,
        mode: str | None,
        instruction: str | None = None,
        provider: AgentProvider | None = None,
    ) -> None:
        store = self._notes_store()
        if store is None:
            return
        live = set(self._notes_ids.values())
        if notes_id is not None and notes_id in store and notes_id not in live:
            store.revive(notes_id, key, agent_id)
        else:
            if notes_id is None and resume_mode == ResumeMode.NEW:
                store.delete_ended(key, live)
            notes_id = store.create(
                key, agent_id, kind, mode=mode or self.config.notes.mode
            ).notes_id
        self._notes_ids[(key, agent_id)] = notes_id
        if resume_mode != ResumeMode.NEW:
            return
        if provider is not None and provider.name != "claude":
            # Only Claude's hooks report the first prompt (Q-N8): pin the
            # spawn's instruction for the others.
            store.set_instruction(notes_id, clip_instruction(instruction))
        else:
            self._unpinned.add(notes_id)

    def _notes_path(self, key: str, agent_id: str) -> str | None:
        notes_id = self._notes_ids.get((key, agent_id))
        if notes_id is None or self.notes is None:
            return None
        return str(self.notes.path(notes_id))

    def _end_notes(self, notes_id: str | None, *, keep: bool) -> str | None:
        """An agent ended: its notes become an orphan (``keep``) or go.
        Their text when not empty."""
        store = self.notes
        self._unpinned.discard(notes_id)
        if store is None or notes_id is None or notes_id not in store:
            return None
        text = store.get(notes_id).text
        if keep:
            store.mark(notes_id, ORPHAN)
        else:
            store.delete(notes_id, DELETED)
        self._notes_gc()
        return None if is_empty(text) else text

    def _orphan_notes(self, entry: Entry) -> None:
        """A consumed entry's notes: not adopted by the agent that runs its
        conversation (V5), kept as an orphan."""
        store = self.notes
        if (
            store is not None
            and entry.notes_id in store
            and entry.notes_id not in self._notes_ids.values()
        ):
            store.mark(entry.notes_id, ORPHAN)

    def _notes_exist(self, meta: Meta) -> bool:
        if meta.kind == ORCHESTRATOR_KIND:
            return bool(self.repo_root) and os.path.isdir(self.repo_root)
        return self.worktree(meta.worktree_path) is not None

    def _notes_gc(self) -> None:
        """Only the latest notes per worktree, orphans expire (plan §2).
        Not while a restore runs: the entries it took are not live yet."""
        store = self.notes
        if store is None or self._restoring:
            return
        restorable = None
        if self.manifest is not None:
            restorable = {
                e.notes_id: e.ended_at
                for e in self.manifest.restorable.values()
                if e.notes_id
            }
        store.gc(set(self._notes_ids.values()), restorable, self._notes_exist)

    def _remember(
        self,
        info: AgentInfo,
        provider_name: str,
        instruction: str | None,
        *,
        resume_mode: ResumeMode = ResumeMode.NEW,
        resume_conversation: str | None = None,
    ) -> None:
        if self.manifest is None:
            self._notes_gc()
            return
        slot = (info.worktree_path, info.agent_id)
        if resume_mode == ResumeMode.RESUME_LAST and not resume_conversation:
            self._resumed_last.add(slot)
        self.manifest.add(
            Entry(
                id=Entry.new_id(),
                worktree_path=info.worktree_path,
                agent_id=info.agent_id,
                kind=info.kind,
                provider=provider_name,
                label=info.label,
                session_name=info.session_name,
                skip_permissions=info.permission_mode == PERMISSION_BYPASS,
                permission_mode=info.permission_mode,
                conversation_id=resume_conversation,
                instruction=clip_instruction(instruction),
                notes_id=self._notes_ids.get(slot),
            )
        )
        self._reconcile_restorable()
        self._notes_gc()

    def _note_conversation(self, key: str, agent_id: str) -> None:
        session = self.get_session(key, agent_id)
        conversation = session.conversation_id if session is not None else None
        notes_id = self._notes_ids.get((key, agent_id))
        if notes_id and self.notes is not None:
            if conversation:
                # A /clear starts a new conversation; the notes follow the agent.
                self.notes.set_conversation(notes_id, conversation)
            if notes_id in self._unpinned and session and session.first_prompt:
                self._unpinned.discard(notes_id)
                self.notes.set_instruction(notes_id, session.first_prompt)
        if self.manifest is None:
            return
        if conversation and self.manifest.update(
            key, agent_id, conversation_id=conversation
        ):
            # Now known: an entry hidden until then is consumed or shown.
            self._reconcile_restorable()

    def _live_match(self, entry: Entry) -> tuple[bool, Entry] | None:
        """The live agent that is (or may be) running ``entry``'s
        conversation: ``(True, live)`` when it is, so restoring it would
        start a second process on one conversation; ``(False, live)`` when
        it may be, until ``live``'s provider reports its conversation id,
        or while it runs that conversation as the other kind; None when
        ``entry`` is not live here.

        - The orchestrator has one slot: any live orchestrator is it (the
          UIs start it with ``--continue`` in the repo root).
        - Known conversation ids are compared. One kind's conversation run
          as the other (an agent ``--continue`` in the repo root took the
          orchestrator's) only hides the entry: restoring it would put a
          second process on the conversation, and once that agent stops the
          entry comes back as what it was.
        - An agent spawned with ``--continue`` (``resume_mode=last``) in
          the entry's worktree resumes that directory's last conversation:
          the one a restore of an entry with no known id would resume too.
          An entry with a known id waits for the agent's.
        """
        assert self.manifest is not None
        maybe: Entry | None = None
        for slot, live in self.manifest.agents.items():
            if entry.kind != live.kind:
                if (
                    entry.conversation_id
                    and entry.conversation_id == live.conversation_id
                ):
                    maybe = maybe or live
                continue
            if entry.kind == ORCHESTRATOR_KIND:
                return True, live
            if entry.conversation_id and live.conversation_id:
                if entry.conversation_id == live.conversation_id:
                    return True, live
                continue
            if (
                live.worktree_path == entry.worktree_path
                and slot in self._resumed_last
                and not live.conversation_id
            ):
                if not entry.conversation_id:
                    return True, live
                maybe = maybe or live
        return (False, maybe) if maybe is not None else None

    @staticmethod
    def _live_where(live: Entry) -> str:
        if live.kind == ORCHESTRATOR_KIND:
            return "the orchestrator"
        return f"agent {live.agent_id} in {live.worktree_path}"

    def _offerable(self) -> list[Entry]:
        """Restorable entries not hidden by a live agent, oldest first."""
        assert self.manifest is not None
        return sorted(
            (e for e in self.manifest.restorable.values() if not self._live_match(e)),
            key=lambda e: e.started_at,
        )

    def _reconcile_restorable(self) -> None:
        """Consume the restorable entries whose agent is live here (they
        must not be offered again: it already runs), and tell clients
        (``restorable.changed``) when what is offered changed."""
        manifest = self.manifest
        if manifest is None:
            return
        live = [
            (entry.id, match[1])
            for entry in manifest.restorable.values()
            if (match := self._live_match(entry)) is not None and match[0]
        ]
        for entry_id, by in live:
            self._orphan_notes(manifest.restorable[entry_id])
            self._consumed[entry_id] = self._live_where(by)
            log.info(
                "restorable %s is live here (%s); no longer offered",
                entry_id,
                self._live_where(by),
            )
        manifest.take(entry_id for entry_id, _ in live)
        self._announce_restorable()

    def _announce_restorable(self, *, quiet: bool = False) -> None:
        if self.manifest is None or (self._restoring and not quiet):
            return
        offered = self._offerable()
        ids = [e.id for e in offered]
        if ids != self._offered:
            self._offered = ids
            if quiet:
                return
            self.events.emit(
                RESTORABLE_CHANGED, {"agents": [e.to_dict() for e in offered]}
            )

    def _require_manifest(self) -> Manifest:
        if self.manifest is None:
            raise ValueError("This engine keeps no restart manifest")
        return self.manifest

    def list_restorable(self) -> list[Entry]:
        """Agents earlier engines ran when they ended and that do not run
        here now, oldest first."""
        if self._require_manifest().prune(self._restorable_exists):
            self._notes_gc()
        self._reconcile_restorable()
        return self._offerable()

    def dismiss_restorable(self, ids: Iterable[str] | None = None) -> list[str]:
        """Stop offering these agents (every one when ``ids`` is None)."""
        manifest = self._require_manifest()
        wanted = list(manifest.restorable) if ids is None else list(ids)
        taken = manifest.take(wanted)
        for entry in taken:
            if self.notes is not None and entry.notes_id in self.notes:
                self.notes.delete(entry.notes_id, DELETED)
        self._notes_gc()
        self._announce_restorable()
        return [entry.id for entry in taken]

    async def restore_agents(
        self,
        ids: Iterable[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        permission_mode: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Spawn restorable agents again (every one when ``ids`` is None),
        each resuming its conversation: the one its provider reported, else
        its provider's "last" (``resume_mode=last``). Same worktree, kind,
        provider, label, permission mode (``permission_mode`` instead, for
        every one) and, when free, agent id. Emits ``engine.restored``; a
        failed entry stays restorable.

        An entry whose conversation already runs here is refused, never
        spawned twice: consumed when it surely runs, kept when it may (its
        live agent has not reported its conversation yet)."""
        manifest = self._require_manifest()
        self._restoring = True
        try:
            result = await self._restore_agents(manifest, ids, env, permission_mode)
        finally:
            self._restoring = False
        self._notes_gc()
        # Clients fetch the list again on engine.restored.
        self._announce_restorable(quiet=True)
        self.events.emit(ENGINE_RESTORED, result)
        return result

    async def _restore_agents(
        self,
        manifest: Manifest,
        ids: Iterable[str] | None,
        env: Mapping[str, str] | None,
        permission_mode: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        manifest.prune(self._restorable_exists)
        self._reconcile_restorable()
        wanted = (
            [e.id for e in self._offerable()]
            if ids is None
            else list(dict.fromkeys(ids))
        )
        restored: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for entry_id in wanted:
            if entry_id in manifest.restorable:
                continue
            reason = "not restorable (unknown id, or its worktree is gone)"
            if entry_id in self._consumed:
                reason = f"already running in this engine ({self._consumed[entry_id]})"
            failed.append({"id": entry_id, "reason": reason})
        for entry in manifest.take(wanted):
            # Checked for each: an agent restored a moment ago may be this
            # entry's conversation too.
            match = self._live_match(entry)
            if match is not None:
                sure, live = match
                where = self._live_where(live)
                if sure:
                    self._orphan_notes(entry)
                    self._consumed[entry.id] = where
                    reason = f"already running in this engine ({where})"
                elif live.kind != entry.kind:
                    manifest.restorable[entry.id] = entry
                    reason = (
                        f"its conversation runs in this engine as {where}; "
                        "stop that one, then restore this"
                    )
                else:
                    manifest.restorable[entry.id] = entry
                    reason = (
                        f"may be running in this engine ({where} resumed the "
                        "worktree's last conversation; its id is not known yet)"
                    )
                manifest.mark_dirty()
                failed.append(
                    {
                        "id": entry.id,
                        "worktree_path": entry.worktree_path,
                        "agent_id": entry.agent_id,
                        "reason": reason,
                    }
                )
                continue
            try:
                info = await self.spawn_agent(
                    entry.worktree_path,
                    permission_mode=permission_mode or self.entry_permission_mode(entry),
                    resume_mode=ResumeMode.RESUME_LAST,
                    label=entry.label or None,
                    kind=entry.kind,
                    env=env,
                    provider_name=entry.provider,
                    resume_conversation=entry.conversation_id,
                    agent_id=entry.agent_id or None,
                    notes_id=entry.notes_id,
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
        return {"restored": restored, "failed": failed}


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
