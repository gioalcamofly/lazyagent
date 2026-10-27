"""What the owner must review on "new commits since your approval"
(docs/design/inbox-plan.md I4j): the ``review_commits`` row field.

A line whose rule reads ``pr.commits_after_owner_approval`` or
``pr.commits_after_ready`` (an ``approvable`` row, R3d) asks the user to
review the commits after a point:

- **approval**: the user's approval (owner_approvals.py), when it is newer
  than the last ready event and its commit is still in the head's history;
- **ready**: the head when the PR was last marked ready (``PrInfo.ready_sha``,
  the last commit before the event in the timeline);
- **opened**: neither (a PR opened ready, or a ready event with no commit
  before it in the window): the PR's own commits, from its base.

The field: ``{since, from, head, count, base_merges, commits, more,
compare_url}``. ``commits``, newest first and at most :data:`CAP`, are
``{sha, subject, author, date, base_merge}``; ``base_merge`` names a merge
of the base that is not counted (R3c, R4c's generated files), and ``count``
leaves those out as the facts do. ``more`` is how many were cut.
``compare_url`` is GitHub's ``<repo>/compare/<from>...<head>`` (``from`` the
base branch for **opened**).

The list is local git (:meth:`BaseMerges.commit_list`), off the loop, once
per (from, head, base): the field is None until it is there, and the pass
that follows carries it (a ``status.updated``). A list that could not be
read (a commit not fetched yet, a start no longer in the head's history) is
read again after :data:`RETRY_AFTER`, the old answer kept meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from lazyagent.engine.owner_approvals import ready_time

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

CAP = 15
RETRY_AFTER = 600.0          # seconds before a failed list is read again
_CACHE_MAX = 512


def start_of(pr: Any, approval: Any) -> tuple[str, str | None]:
    """``(since, from sha)``: where the review starts (see the module)."""
    since = ready_time(pr)
    newer = approval is not None and approval.state != "lost" and \
        (since is None or pr.commits_after_ready is None or since <= approval.at)
    if newer:
        return "approval", approval.sha
    if pr.ready_sha:
        return "ready", pr.ready_sha
    return "opened", None


def compare_url(pr_url: str, start: str, head: str) -> str | None:
    """GitHub's compare page of ``start...head`` from the PR's own URL."""
    repo, sep, _ = pr_url.partition("/pull/")
    return f"{repo}/compare/{start}...{head}" if sep and repo else None


def payload(pr: Any, since: str, start: str | None, commits: list[dict[str, Any]],
            cap: int = CAP) -> dict[str, Any]:
    head = pr.head_sha
    shown = commits[:cap]
    return {"since": since, "from": start or pr.base_ref, "head": head,
            "count": sum(c["base_merge"] is None for c in commits),
            "base_merges": sum(c["base_merge"] is not None for c in commits),
            "commits": shown, "more": len(commits) - len(shown),
            "compare_url": compare_url(pr.url, start or pr.base_ref, head)}


class ReviewCommits:
    """The engine's ``review_commits`` cache."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # (since, from, head, base) → the commits, or None (from lost).
        self._lists: dict[tuple[str, str | None, str, str], list[dict[str, Any]] | None] = {}
        self._running: set[tuple[str, str | None, str, str]] = set()
        # slot → when its list failed (None), to read it again later.
        self._failed: dict[tuple[str, str | None, str, str], float] = {}
        self.clock: Callable[[], float] = time.monotonic

    def of(self, pr: Any, approval: Any) -> dict[str, Any] | None:
        """The field for an open PR; None until its list is read."""
        since, start = start_of(pr, approval)
        slot = (since, start, pr.head_sha, pr.base_ref)
        if self._stale(slot):
            self._schedule(slot)
            if slot not in self._lists:
                return None
        commits = self._lists[slot]
        if commits is None and since != "opened":
            # The point is no longer in the head's history (a force push):
            # the PR's own commits.
            fallback = ("opened", None, pr.head_sha, pr.base_ref)
            if self._stale(fallback):
                self._schedule(fallback)
                if fallback not in self._lists:
                    return None
            since, start, commits = "opened", None, self._lists[fallback]
        if commits is None:
            return None
        return payload(pr, since, start, commits)

    def _stale(self, slot: tuple[str, str | None, str, str]) -> bool:
        """Not read yet, or failed :data:`RETRY_AFTER` ago."""
        if slot not in self._lists:
            return True
        failed = self._failed.get(slot)
        return failed is not None and self.clock() - failed >= RETRY_AFTER

    def _schedule(self, slot: tuple[str, str | None, str, str]) -> None:
        if slot in self._running:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._running.add(slot)
        self._engine._spawn(self._read(slot))

    async def _read(self, slot: tuple[str, str | None, str, str]) -> None:
        engine = self._engine
        try:
            merges = engine.worktree_service.base_merges
            _, start, head, base = slot
            found = await asyncio.to_thread(
                merges.commit_list, engine.repo_root, start, head, base)
            if len(self._lists) >= _CACHE_MAX:
                self._lists.clear()
                self._failed.clear()
            self._lists[slot] = found
            if found is None:
                self._failed[slot] = self.clock()
            else:
                self._failed.pop(slot, None)
        finally:
            self._running.discard(slot)
        engine.status.mark_dirty()
