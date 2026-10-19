"""Worktree service: the worktree list, git status, diff and PR caches.

The service owns what lazyagent knows about the repo's worktrees and the
pollers that keep it fresh. Clients declare an *interest set* (the worktrees
they show) with :meth:`WorktreeService.set_interest`; the git status and PR
pollers only ever touch the union of those sets, so a worktree nobody looks
at costs nothing between full rescans.

Changes are pushed to subscribers as :class:`WorktreeEvent`, named after the
protocol events they become:

- ``worktrees.changed``: ``data`` is the new ``list[WorktreeInfo]``. Every
  rescan emits it, since a rescan is an explicit request for the list.
- ``git_status.updated``: ``data`` maps path to :class:`GitStatus`, only for
  the worktrees whose status actually changed.
- ``pr_info.updated``: ``data`` is ``{"path": str, "pr_info": PrInfo | None}``,
  only when a worktree's PR info changed (or was fetched for the first time).

All git/gh subprocesses run in ``asyncio.to_thread``; the caches and the
subscriber calls stay on the loop that drives the service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Collection, Coroutine, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any

from lazyagent import diagnostics as diag
from lazyagent.models import GitStatus, PrInfo, WorktreeInfo
from lazyagent.worktree_manager import (
    GithubRepo,
    WorktreeManager,
    WorktreeManagerError,
    find_repo_root,
)

log = logging.getLogger(__name__)

WORKTREES_CHANGED = "worktrees.changed"
GIT_STATUS_UPDATED = "git_status.updated"
PR_INFO_UPDATED = "pr_info.updated"

# Only the worktrees in the interest set are polled, and only once a minute:
# `git status` walks the whole working tree, so sweeping every worktree on a
# timer costs seconds on a repo with many of them. The other worktrees
# refresh on the full rescan (`r`, create, remove, MCP), and a client can ask
# for one on demand (Alt+G in the TUI).
GIT_STATUS_INTERVAL = 60.0
PR_INFO_INTERVAL = 60.0

# How long a diff waits for a PR lookup already running before it falls
# back to the default base.
_PR_WAIT = 15.0

# Concurrency gauge for PR fetches (diagnostics only).
_PR_STATUS_GAUGE = diag.Gauge()

_UNRESOLVED = object()

ManagerFactory = Callable[[str], WorktreeManager]


@dataclass(frozen=True)
class WorktreeEvent:
    """One change notification; see the module docstring for ``data``."""

    event: str
    data: Any


Subscriber = Callable[[WorktreeEvent], None]


class WorktreeService:
    """Worktree list, git status, diff and PR caches with interest-scoped pollers."""

    def __init__(
        self,
        manager_factory: ManagerFactory = WorktreeManager,
        *,
        find_root: Callable[[], Any] = find_repo_root,
        git_status_interval: float = GIT_STATUS_INTERVAL,
        pr_info_interval: float = PR_INFO_INTERVAL,
    ) -> None:
        self._manager = manager_factory
        self._find_root = find_root
        self._git_status_interval = git_status_interval
        self._pr_info_interval = pr_info_interval

        self.repo_root: str = ""
        self.worktrees: list[WorktreeInfo] = []
        self.git_statuses: dict[str, GitStatus] = {}
        self._pr_infos: dict[str, PrInfo | None] = {}
        self._diffs: dict[str, str] = {}

        self._interests: dict[str, frozenset[str]] = {}
        self._subscribers: list[Subscriber] = []
        self._pollers: list[asyncio.Task] = []
        self._ticks: set[asyncio.Task] = set()
        self._pr_in_flight: set[str] = set()
        # Set when the fetch a path is in flight with ends.
        self._pr_done: dict[str, asyncio.Event] = {}
        self._gh_available: bool | None = None
        self._github_repo: Any = _UNRESOLVED

    # --- Subscriptions ---

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Call ``callback`` with every :class:`WorktreeEvent`; returns an
        unsubscribe function."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, event: str, data: Any) -> None:
        notice = WorktreeEvent(event, data)
        for callback in list(self._subscribers):
            try:
                callback(notice)
            except Exception:
                log.exception("worktree subscriber failed on %s", event)

    # --- Interest ---

    def set_interest(self, paths: Iterable[str], client: str = "default") -> None:
        """Replace ``client``'s interest set. Pollers read it on their next tick."""
        interest = frozenset(paths)
        if interest:
            self._interests[client] = interest
        else:
            self._interests.pop(client, None)

    @property
    def interest(self) -> frozenset[str]:
        """Union of every client's interest set."""
        return frozenset().union(*self._interests.values())

    # --- Worktree list ---

    def rescan(self, repo_path: str | None = None) -> list[WorktreeInfo]:
        """Re-list the worktrees and emit ``worktrees.changed``.

        Runs ``git worktree list`` inline: callers rely on the new list being
        in place when this returns (a create selects the new worktree right
        away). Caches of worktrees that are gone are dropped. Raises
        :class:`WorktreeManagerError` and leaves the list as it was if the
        repo cannot be listed.
        """
        if repo_path:
            root = self._manager(repo_path).repo_path
        else:
            root = self._find_root()
        self.repo_root = str(root)
        worktrees = self._manager(self.repo_root).list()
        self.worktrees = list(worktrees)
        # The GitHub repo is looked up again after a rescan, so a failed
        # lookup (offline at startup) does not disable batching for good.
        self._github_repo = _UNRESOLVED
        self._prune()
        self._emit(WORKTREES_CHANGED, list(self.worktrees))
        return self.worktrees

    def worktree(self, path: str) -> WorktreeInfo | None:
        for wt in self.worktrees:
            if wt.path == path:
                return wt
        return None

    def _known(self, paths: Collection[str]) -> list[WorktreeInfo]:
        return [wt for wt in self.worktrees if wt.path in paths]

    def _prune(self) -> None:
        live = {wt.path for wt in self.worktrees}
        for cache in (self.git_statuses, self._pr_infos, self._diffs):
            for path in [p for p in cache if p not in live]:
                del cache[path]

    # --- Git status ---

    async def refresh_git_status(self, paths: Collection[str] | None = None) -> None:
        """Fetch git status for ``paths``, or for every worktree when ``None``.

        ``git status`` walks the whole working tree; on a repo with a couple
        of dozen worktrees the full sweep takes seconds, which is why only
        the rescan paths ask for it.
        """
        repo_root = self.repo_root
        if not repo_root:
            return
        if paths is None:
            worktrees = list(self.worktrees)
            if not worktrees:
                return
            with diag.timed(log, "worker.git_status_all", worktrees=len(worktrees)):
                try:
                    statuses = await asyncio.to_thread(
                        lambda: self._manager(repo_root).get_all_git_statuses(worktrees)
                    )
                except WorktreeManagerError:
                    return
        else:
            statuses = {}
            for wt in self._known(paths):
                if wt.is_bare:
                    continue
                with diag.timed(log, "worker.git_status_selected", worktree=wt.path):
                    try:
                        statuses[wt.path] = await asyncio.to_thread(
                            self._fetch_git_status, repo_root, wt.path
                        )
                    except WorktreeManagerError:
                        continue
        self._store_git_statuses(statuses)

    def _fetch_git_status(self, repo_root: str, path: str) -> GitStatus:
        manager = self._manager(repo_root)
        status = manager.get_git_status(path)
        status.last_commit_subject = manager.get_last_commit_subject(path)
        return status

    def _store_git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        """Merge into the cache (a partial refresh must not drop the others)
        and emit what changed."""
        live = {wt.path for wt in self.worktrees}
        changed = {
            path: status for path, status in statuses.items()
            if path in live and self.git_statuses.get(path) != status
        }
        if not changed:
            return
        self.git_statuses.update(changed)
        self._emit(GIT_STATUS_UPDATED, changed)

    # --- Diff ---

    async def get_diff(
        self, path: str, *, base: str | None = None, max_bytes: int | None = None
    ) -> str:
        """Fetch a worktree's diff (capped, see ``WorktreeManager.get_diff``).
        Only the default one (no ``base``, default cap) is kept as
        :meth:`diff`."""
        manager = self._manager_cls()
        with diag.timed(log, "worker.diff", worktree=path) as span:
            if base is None and max_bytes is None:
                text = await asyncio.to_thread(manager.get_diff, path)
            else:
                text = await asyncio.to_thread(
                    partial(manager.get_diff, path, base=base, max_bytes=max_bytes)
                )
            span["bytes"] = len(text)
        if base is None and max_bytes is None and self.worktree(path) is not None:
            self._diffs[path] = text
        return text

    async def get_pr_diff(
        self,
        path: str,
        *,
        base: str | None = None,
        default_branch: str | None = None,
        max_bytes: int | None = None,
        fetch: bool = False,
    ) -> dict[str, Any]:
        """The diff the branch's pull request shows, and its uncommitted
        changes apart (``WorktreeManager.get_pr_diff``), as ``get_diff``
        returns it with ``pr``.

        Without ``base`` it is resolved: the open PR's base branch (from the
        PR cache; fetched once when not cached yet), else ``default_branch``,
        else the main worktree's branch, else ``main``; each as
        ``origin/<name>`` when that exists, else the local branch (but never
        the worktree's own branch). ``base`` is used as given. ``fetch`` runs ``git fetch origin <branch>`` in the
        worktree first (refreshing the PR info too). Raises ``ValueError``
        only for a ``base`` with no merge-base.
        """
        manager = self._manager_cls()
        fetch_error: str | None = None
        info = None
        candidates: list[tuple[str, str]] = []
        if base is not None:
            ref, branch, source = base, None, "override"
            if fetch and base.startswith("origin/"):
                fetch_error = await asyncio.to_thread(manager.fetch_branch, path, base[len("origin/"):])
        else:
            info = await self.ensure_pr_info(path, refresh=fetch)
            if info is not None and info.state == "OPEN" and info.base_ref:
                candidates.append((info.base_ref, "pr"))
            main = next((wt.branch for wt in self.worktrees if wt.is_main and wt.branch), None)
            for name in (default_branch, main, "main"):
                if name and all(name != c for c, _ in candidates):
                    candidates.append((name, "default"))
            if fetch:
                fetch_error = await asyncio.to_thread(manager.fetch_branch, path, candidates[0][0])
            ref = branch = source = None
            wt = self.worktree(path)
            own = wt.branch if wt is not None else None
            for name, kind in candidates:
                found = await asyncio.to_thread(manager.resolve_base_ref, path, name)
                # The worktree's own local branch: always an empty diff.
                if found is not None and found != own:
                    ref, branch, source = found, name, kind
                    break
        base_error: str | None = None
        with diag.timed(log, "worker.pr_diff", worktree=path) as span:
            try:
                result = await asyncio.to_thread(
                    partial(manager.get_pr_diff, path, ref, max_bytes=max_bytes)
                )
            except ValueError as e:
                if source == "override":
                    raise
                base_error = str(e)
                ref = None
                result = await asyncio.to_thread(
                    partial(manager.get_pr_diff, path, None, max_bytes=max_bytes)
                )
            span["bytes"] = len(result.committed) + len(result.uncommitted)
        reply: dict[str, Any] = {
            "worktree_path": path,
            "diff": result.committed,
            "uncommitted": result.uncommitted,
            "base": None if ref is None else {
                "ref": ref,
                "branch": branch,
                "source": source,
                "merge_base": result.merge_base,
                "pr_number": info.number if source == "pr" and info is not None else None,
            },
        }
        if ref is None:
            reply["base_error"] = base_error or "No base branch found (none of: " + ", ".join(
                c for c, _ in candidates
            ) + ")"
        if fetch_error:
            reply["fetch_error"] = fetch_error
        return reply

    def diff(self, path: str) -> str | None:
        """The last diff fetched for ``path``, if any."""
        return self._diffs.get(path)

    def _manager_cls(self) -> Any:
        """Something to call ``WorktreeManager``'s static helpers on."""
        if isinstance(self._manager, type):
            return self._manager
        return self._manager(self.repo_root)

    # --- PR info ---

    def has_pr_info(self, path: str) -> bool:
        return path in self._pr_infos

    def pr_info(self, path: str) -> PrInfo | None:
        return self._pr_infos.get(path)

    async def ensure_pr_info(self, path: str, *, refresh: bool = False) -> PrInfo | None:
        """``path``'s PR info, fetched when not cached yet (or ``refresh``).
        A fetch already in flight is waited for, not doubled."""
        done = self._pr_done.get(path)
        if done is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), _PR_WAIT)
        elif refresh or not self.has_pr_info(path):
            await self.refresh_pr_info([path])
        return self.pr_info(path)

    async def refresh_pr_info(self, paths: Collection[str]) -> None:
        """Fetch PR info for ``paths``, batched into as few ``gh`` calls as possible.

        A worktree whose fetch is already in flight is skipped; its answer is
        on the way.
        """
        repo_root = self.repo_root
        targets = [wt for wt in self._known(paths) if wt.path not in self._pr_in_flight]
        if not repo_root or not targets:
            return

        # Claimed before the first await, so a concurrent caller sees them.
        claimed = {wt.path for wt in targets}
        self._pr_in_flight |= claimed
        done = asyncio.Event()
        for path in claimed:
            self._pr_done[path] = done
        try:
            if self._gh_available is None:
                with diag.timed(log, "worker.gh_auth_status"):
                    self._gh_available = await asyncio.to_thread(
                        self._manager_cls().is_gh_available
                    )
            if not self._gh_available:
                return

            fields: dict[str, Any] = {"worktrees": len(targets)}
            if len(targets) == 1:
                fields["worktree"] = targets[0].path
            with diag.timed(log, "worker.pr_status", **fields) as span:
                with _PR_STATUS_GAUGE as in_flight:
                    span["in_flight"] = in_flight
                    span["peak_in_flight"] = _PR_STATUS_GAUGE.peak
                    infos, batched = await asyncio.to_thread(
                        self._fetch_pr_infos, repo_root, targets
                    )
                span["batched"] = batched
                span["found"] = sum(info is not None for info in infos.values())
        finally:
            self._pr_in_flight -= claimed
            # Waiters run at the next await, after the cache is updated below.
            done.set()
            for path in claimed:
                if self._pr_done.get(path) is done:
                    del self._pr_done[path]

        live = {wt.path for wt in self.worktrees}
        for path, info in infos.items():
            if path not in live:
                continue
            if path in self._pr_infos and self._pr_infos[path] == info:
                continue
            self._pr_infos[path] = info
            self._emit(PR_INFO_UPDATED, {"path": path, "pr_info": info})

    def _fetch_pr_infos(
        self, repo_root: str, targets: list[WorktreeInfo],
    ) -> tuple[dict[str, PrInfo | None], int]:
        """Batched lookup first, then ``gh pr view`` for whatever it did not
        answer. Returns the infos and how many came from the batch."""
        manager = self._manager(repo_root)
        results: dict[str, PrInfo | None] = {}
        repo = self._resolve_github_repo(manager)
        if repo is not None:
            try:
                results = manager.get_pr_infos(targets, repo)
            except WorktreeManagerError as e:
                diag.event(log, "worker.pr_batch_failed", error=str(e))
                results = {}
        batched = len(results)
        for wt in targets:
            if wt.path not in results:
                results[wt.path] = manager.get_pr_info(wt.path)
        return results, batched

    def _resolve_github_repo(self, manager: WorktreeManager) -> GithubRepo | None:
        if self._github_repo is _UNRESOLVED:
            self._github_repo = manager.get_github_repo()
        return self._github_repo

    # --- Pollers ---

    def start(self) -> None:
        """Start the git status and PR pollers on the running loop."""
        if self._pollers:
            return
        loop = asyncio.get_running_loop()
        self._pollers = [
            loop.create_task(self._every(
                self._git_status_interval,
                diag.wrap_timer("timer.git_status", lambda: self._spawn(self.poll_git_status())),
            )),
            loop.create_task(self._every(
                self._pr_info_interval,
                diag.wrap_timer("timer.pr_status", lambda: self._spawn(self.poll_pr_info())),
            )),
        ]

    def stop(self) -> None:
        """Cancel the pollers and any tick still running."""
        for task in (*self._pollers, *self._ticks):
            task.cancel()
        self._pollers = []
        self._ticks.clear()

    async def wait_idle(self) -> None:
        """Wait for every tick spawned so far to finish."""
        # Only the unfinished ones: a finished task leaves the set in a
        # done callback, and gathering finished tasks completes without
        # yielding to the loop that would run it.
        while pending := [t for t in self._ticks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def poll_git_status(self) -> None:
        """One git status tick: the current interest set, nothing else."""
        interest = self.interest
        if interest:
            await self.refresh_git_status(interest)

    async def poll_pr_info(self) -> None:
        """One PR info tick: the current interest set, nothing else."""
        interest = self.interest
        if interest:
            await self.refresh_pr_info(interest)

    @staticmethod
    async def _every(seconds: float, callback: Callable[[], None]) -> None:
        while True:
            await asyncio.sleep(seconds)
            callback()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        # Ticks run as their own tasks, like timer callbacks that start a
        # worker: a slow `gh` call does not push the next tick back.
        task = asyncio.get_running_loop().create_task(coro)
        self._ticks.add(task)
        task.add_done_callback(self._tick_done)

    def _tick_done(self, task: asyncio.Task) -> None:
        self._ticks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("worktree poller tick failed", exc_info=task.exception())
