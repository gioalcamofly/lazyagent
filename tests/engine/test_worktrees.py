"""WorktreeService: caches, interest-scoped pollers and change events."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from lazyagent.engine.worktrees import (
    GIT_STATUS_UPDATED,
    PR_INFO_UPDATED,
    WORKTREES_CHANGED,
    WorktreeEvent,
    WorktreeService,
)
from lazyagent.models import GitStatus, PrInfo, WorktreeInfo
from lazyagent.worktree_manager import GithubRepo, WorktreeManagerError

pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")

MAIN = WorktreeInfo(path="/repo", head="a" * 40, branch="main", is_main=True, is_bare=False)
FEAT = WorktreeInfo(path="/repo-feat", head="b" * 40, branch="feat", is_main=False, is_bare=False)
FIX = WorktreeInfo(path="/repo-fix", head="c" * 40, branch="fix", is_main=False, is_bare=False)


def _pr(number: int, state: str = "OPEN") -> PrInfo:
    return PrInfo(number=number, title=f"PR {number}", state=state, checks=[])


class World:
    """What the fake git/gh would answer, and a log of what was asked."""

    def __init__(self) -> None:
        self.worktrees = [MAIN, FEAT, FIX]
        self.statuses: dict[str, GitStatus] = {}
        self.prs: dict[str, PrInfo | None] = {}
        self.diffs: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.batch_fails = False
        self.unbatchable: set[str] = set()
        self.github_repo: GithubRepo | None = GithubRepo("acme", "repo")
        self.gh_available = True
        self.pr_delay = 0.0


class FakeManager:
    """Stands in for WorktreeManager; never runs git or gh."""

    def __init__(self, world: World, repo_path: str) -> None:
        self.world = world
        self.repo_path = Path(repo_path)

    def list(self) -> list[WorktreeInfo]:
        self.world.calls.append(("list",))
        return list(self.world.worktrees)

    def get_all_git_statuses(self, worktrees):
        self.world.calls.append(("status_all", tuple(wt.path for wt in worktrees)))
        return {wt.path: self.world.statuses.get(wt.path, GitStatus()) for wt in worktrees}

    def get_git_status(self, path) -> GitStatus:
        self.world.calls.append(("status", path))
        status = self.world.statuses.get(path, GitStatus())
        return GitStatus(**vars(status))

    def get_last_commit_subject(self, path) -> str:
        return self.world.statuses.get(path, GitStatus()).last_commit_subject

    def get_diff(self, path) -> str:
        self.world.calls.append(("diff", path))
        return self.world.diffs.get(path, "")

    def is_gh_available(self) -> bool:
        self.world.calls.append(("gh_auth",))
        return self.world.gh_available

    def get_github_repo(self):
        self.world.calls.append(("repo_view",))
        return self.world.github_repo

    def get_pr_infos(self, worktrees, repo):
        if self.world.pr_delay:
            time.sleep(self.world.pr_delay)
        self.world.calls.append(("pr_batch", tuple(wt.path for wt in worktrees)))
        if self.world.batch_fails:
            raise WorktreeManagerError("HTTP 502")
        return {
            wt.path: self.world.prs.get(wt.path)
            for wt in worktrees if wt.path not in self.world.unbatchable
        }

    def get_pr_info(self, path):
        self.world.calls.append(("pr_view", path))
        return self.world.prs.get(path)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def service(world: World) -> WorktreeService:
    svc = WorktreeService(lambda root: FakeManager(world, root), find_root=lambda: "/repo")
    svc.rescan()
    world.calls.clear()
    return svc


@pytest.fixture
def events(service: WorktreeService) -> list[WorktreeEvent]:
    seen: list[WorktreeEvent] = []
    service.subscribe(seen.append)
    return seen


def _calls(world: World, kind: str) -> list[tuple]:
    return [c for c in world.calls if c[0] == kind]


# --- Worktree list ---


def test_rescan_emits_worktrees_changed(world):
    svc = WorktreeService(lambda root: FakeManager(world, root), find_root=lambda: "/repo")
    seen: list[WorktreeEvent] = []
    svc.subscribe(seen.append)

    assert svc.rescan() == [MAIN, FEAT, FIX]

    assert svc.repo_root == "/repo"
    assert seen == [WorktreeEvent(WORKTREES_CHANGED, [MAIN, FEAT, FIX])]


def test_rescan_with_a_repo_path_resolves_it_through_the_manager(world):
    svc = WorktreeService(lambda root: FakeManager(world, root), find_root=lambda: pytest.fail())
    svc.rescan("/repo/sub/..")
    assert svc.repo_root == "/repo/sub/.."


def test_failed_rescan_keeps_the_list(service, world, events, monkeypatch):
    def boom(self):
        raise WorktreeManagerError("not a repo")

    monkeypatch.setattr(FakeManager, "list", boom)
    with pytest.raises(WorktreeManagerError):
        service.rescan()
    assert service.worktrees == [MAIN, FEAT, FIX]
    assert events == []


def test_unsubscribe(service, world):
    seen: list[WorktreeEvent] = []
    unsubscribe = service.subscribe(seen.append)
    unsubscribe()
    service.rescan()
    assert seen == []


def test_a_failing_subscriber_does_not_starve_the_others(service, world):
    seen: list[WorktreeEvent] = []

    def bad(_event):
        raise RuntimeError("boom")

    service.subscribe(bad)
    service.subscribe(seen.append)
    service.rescan()
    assert [e.event for e in seen] == [WORKTREES_CHANGED]


@pytest.mark.asyncio
async def test_removing_a_worktree_drops_its_caches(service, world, events):
    world.statuses[FIX.path] = GitStatus(dirty_count=1)
    world.prs[FIX.path] = _pr(9)
    world.diffs[FIX.path] = "diff"
    await service.refresh_git_status()
    await service.refresh_pr_info([FIX.path])
    await service.get_diff(FIX.path)
    assert FIX.path in service.git_statuses
    assert service.has_pr_info(FIX.path)
    assert service.diff(FIX.path) == "diff"

    world.worktrees = [MAIN, FEAT]
    service.rescan()

    assert FIX.path not in service.git_statuses
    assert not service.has_pr_info(FIX.path)
    assert service.diff(FIX.path) is None


@pytest.mark.asyncio
async def test_a_result_landing_after_removal_is_dropped(service, world, events):
    world.statuses[FIX.path] = GitStatus(dirty_count=1)
    world.worktrees = [MAIN, FEAT]
    refresh = asyncio.ensure_future(service.refresh_git_status([FIX.path]))
    service.rescan()
    await refresh
    assert FIX.path not in service.git_statuses
    assert [e.event for e in events] == [WORKTREES_CHANGED]


# --- Git status ---


@pytest.mark.asyncio
async def test_full_sweep_covers_every_worktree(service, world, events):
    world.statuses = {wt.path: GitStatus(dirty_count=i + 1) for i, wt in enumerate(world.worktrees)}

    await service.refresh_git_status()

    assert _calls(world, "status_all") == [("status_all", (MAIN.path, FEAT.path, FIX.path))]
    assert set(service.git_statuses) == {MAIN.path, FEAT.path, FIX.path}
    assert events == [WorktreeEvent(GIT_STATUS_UPDATED, dict(service.git_statuses))]


@pytest.mark.asyncio
async def test_git_poller_polls_only_the_interest_set(service, world):
    service.set_interest([FEAT.path])
    await service.poll_git_status()
    assert _calls(world, "status") == [("status", FEAT.path)]


@pytest.mark.asyncio
async def test_empty_interest_means_no_git_polling(service, world):
    await service.poll_git_status()
    service.set_interest([FEAT.path])
    service.set_interest([])
    await service.poll_git_status()
    assert world.calls == []


@pytest.mark.asyncio
async def test_interest_is_the_union_over_clients(service, world):
    service.set_interest([FEAT.path], client="tui")
    service.set_interest([FIX.path, "/not-a-worktree"], client="web")
    assert service.interest == {FEAT.path, FIX.path, "/not-a-worktree"}

    await service.poll_git_status()

    assert sorted(_calls(world, "status")) == [("status", FEAT.path), ("status", FIX.path)]


@pytest.mark.asyncio
async def test_bare_worktrees_are_not_polled_for_git_status(service, world):
    bare = WorktreeInfo(path="/bare", head="", branch=None, is_main=False, is_bare=True)
    world.worktrees = [MAIN, bare]
    service.rescan()
    world.calls.clear()
    service.set_interest([bare.path])
    await service.poll_git_status()
    assert _calls(world, "status") == []


@pytest.mark.asyncio
async def test_unchanged_git_status_emits_nothing_and_a_change_emits_once(service, world, events):
    service.set_interest([FEAT.path])
    world.statuses[FEAT.path] = GitStatus(dirty_count=2, last_commit_subject="wip")

    await service.poll_git_status()
    await service.poll_git_status()
    assert events == [
        WorktreeEvent(GIT_STATUS_UPDATED, {FEAT.path: GitStatus(dirty_count=2, last_commit_subject="wip")})
    ]

    world.statuses[FEAT.path] = GitStatus(dirty_count=3, last_commit_subject="wip")
    await service.poll_git_status()
    assert len(events) == 2
    assert events[1].data == {FEAT.path: GitStatus(dirty_count=3, last_commit_subject="wip")}


@pytest.mark.asyncio
async def test_partial_refresh_keeps_other_statuses(service, world, events):
    service.git_statuses = {
        MAIN.path: GitStatus(last_commit_subject="old main"),
        FEAT.path: GitStatus(last_commit_subject="old feat"),
    }
    world.statuses[FEAT.path] = GitStatus(last_commit_subject="new feat")

    await service.refresh_git_status([FEAT.path])

    assert service.git_statuses[MAIN.path].last_commit_subject == "old main"
    assert service.git_statuses[FEAT.path].last_commit_subject == "new feat"
    assert events[-1].data == {FEAT.path: GitStatus(last_commit_subject="new feat")}


# --- Diff ---


@pytest.mark.asyncio
async def test_get_diff_returns_and_caches(service, world):
    world.diffs[FEAT.path] = "+line"
    assert await service.get_diff(FEAT.path) == "+line"
    assert service.diff(FEAT.path) == "+line"


# --- PR info ---


@pytest.mark.asyncio
async def test_pr_polling_batches_all_interested_branches_into_one_call(service, world, events):
    world.prs = {FEAT.path: _pr(1), FIX.path: _pr(2, "MERGED")}
    service.set_interest([FEAT.path, FIX.path, MAIN.path])

    await service.poll_pr_info()

    assert _calls(world, "pr_batch") == [("pr_batch", (MAIN.path, FEAT.path, FIX.path))]
    assert _calls(world, "pr_view") == []
    assert service.pr_info(FEAT.path) == _pr(1)
    assert service.pr_info(FIX.path) == _pr(2, "MERGED")
    assert service.has_pr_info(MAIN.path) and service.pr_info(MAIN.path) is None
    assert sorted((e.data["path"], e.data["pr_info"]) for e in events if e.event == PR_INFO_UPDATED) == [
        (MAIN.path, None), (FEAT.path, _pr(1)), (FIX.path, _pr(2, "MERGED")),
    ]


@pytest.mark.asyncio
async def test_github_repo_is_looked_up_once_per_scan(service, world):
    service.set_interest([FEAT.path])
    await service.poll_pr_info()
    await service.poll_pr_info()
    assert len(_calls(world, "repo_view")) == 1
    service.rescan()
    await service.poll_pr_info()
    assert len(_calls(world, "repo_view")) == 2


@pytest.mark.asyncio
async def test_gh_auth_is_checked_once(service, world):
    service.set_interest([FEAT.path])
    await service.poll_pr_info()
    await service.poll_pr_info()
    assert len(_calls(world, "gh_auth")) == 1


@pytest.mark.asyncio
async def test_no_pr_polling_without_gh(service, world, events):
    world.gh_available = False
    service.set_interest([FEAT.path])
    await service.poll_pr_info()
    assert _calls(world, "pr_batch") == [] and _calls(world, "pr_view") == []
    assert events == []


@pytest.mark.asyncio
async def test_failed_batch_falls_back_to_one_lookup_per_worktree(service, world, events):
    world.batch_fails = True
    world.prs = {FEAT.path: _pr(1)}
    service.set_interest([FEAT.path, FIX.path])

    await service.poll_pr_info()

    assert len(_calls(world, "pr_batch")) == 1
    assert sorted(_calls(world, "pr_view")) == [("pr_view", FEAT.path), ("pr_view", FIX.path)]
    assert service.pr_info(FEAT.path) == _pr(1)
    assert service.has_pr_info(FIX.path) and service.pr_info(FIX.path) is None


@pytest.mark.asyncio
async def test_worktrees_the_batch_cannot_answer_are_looked_up_alone(service, world):
    world.unbatchable = {FIX.path}
    world.prs = {FEAT.path: _pr(1), FIX.path: _pr(2)}
    service.set_interest([FEAT.path, FIX.path])

    await service.poll_pr_info()

    assert _calls(world, "pr_view") == [("pr_view", FIX.path)]
    assert service.pr_info(FIX.path) == _pr(2)


@pytest.mark.asyncio
async def test_no_github_repo_means_per_worktree_lookups(service, world):
    world.github_repo = None
    service.set_interest([FEAT.path])
    await service.poll_pr_info()
    assert _calls(world, "pr_batch") == []
    assert _calls(world, "pr_view") == [("pr_view", FEAT.path)]


@pytest.mark.asyncio
async def test_empty_interest_means_no_pr_polling(service, world):
    await service.poll_pr_info()
    assert world.calls == []


@pytest.mark.asyncio
async def test_unchanged_pr_info_emits_nothing_and_a_change_emits_once(service, world, events):
    service.set_interest([FEAT.path])
    world.prs[FEAT.path] = _pr(1)
    await service.poll_pr_info()
    await service.poll_pr_info()
    assert len(events) == 1

    world.prs[FEAT.path] = _pr(1, "MERGED")
    await service.poll_pr_info()
    assert len(events) == 2
    assert events[1] == WorktreeEvent(PR_INFO_UPDATED, {"path": FEAT.path, "pr_info": _pr(1, "MERGED")})


@pytest.mark.asyncio
async def test_concurrent_requests_for_one_worktree_share_a_fetch(service, world):
    world.pr_delay = 0.05
    service.set_interest([FEAT.path])
    await asyncio.gather(service.poll_pr_info(), service.refresh_pr_info([FEAT.path]))
    assert len(_calls(world, "pr_batch")) == 1


# --- Pollers ---


@pytest.mark.asyncio
async def test_interest_changes_take_effect_on_the_next_tick(world):
    svc = WorktreeService(
        lambda root: FakeManager(world, root),
        find_root=lambda: "/repo",
        git_status_interval=0.02,
        pr_info_interval=3600,
    )
    svc.rescan()
    svc.set_interest([FEAT.path])
    svc.start()
    try:
        await asyncio.sleep(0.05)
        await svc.wait_idle()
        assert ("status", FEAT.path) in world.calls

        svc.set_interest([FIX.path])
        world.calls.clear()
        await asyncio.sleep(0.05)
        await svc.wait_idle()
        assert {c for c in world.calls if c[0] == "status"} == {("status", FIX.path)}

        svc.set_interest([])
        await asyncio.sleep(0.01)
        await svc.wait_idle()
        world.calls.clear()
        await asyncio.sleep(0.05)
        assert world.calls == []
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_pollers_wait_a_full_interval_before_the_first_tick(world):
    svc = WorktreeService(lambda root: FakeManager(world, root), find_root=lambda: "/repo")
    svc.rescan()
    world.calls.clear()
    svc.set_interest([FEAT.path])
    svc.start()
    try:
        await asyncio.sleep(0.05)
        assert world.calls == []
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_stop_cancels_the_pollers(world):
    svc = WorktreeService(
        lambda root: FakeManager(world, root),
        find_root=lambda: "/repo",
        git_status_interval=0.01,
        pr_info_interval=0.01,
    )
    svc.rescan()
    svc.set_interest([FEAT.path])
    svc.start()
    svc.stop()
    world.calls.clear()
    await asyncio.sleep(0.05)
    assert world.calls == []
