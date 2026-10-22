"""The Diff like GitHub's pull request view: ``WorktreeManager.get_pr_diff``
and the base ``WorktreeService.get_pr_diff`` resolves for it. Real git, in
temporary repos with a bare ``origin``; no ``gh`` (PR info is injected)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lazyagent.engine.worktrees import WorktreeService
from lazyagent.models import PrInfo
from lazyagent.worktree_manager import WorktreeManager


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(cwd: Path, name: str, text: str, message: str) -> None:
    (cwd / name).write_text(text)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-qm", message)


@pytest.fixture
def origin(tmp_path) -> Path:
    """A bare origin: main (base.txt), feature-a off main (a.txt), and main
    moved on after that (later.txt)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "switch", "-qc", "main")
    _commit(seed, "base.txt", "base\n", "base")
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "switch", "-qc", "feature-a")
    _commit(seed, "a.txt", "from a\n", "feature a")
    _git(seed, "push", "-q", "origin", "feature-a")
    _git(seed, "switch", "-q", "main")
    _commit(seed, "later.txt", "later on main\n", "main moves on")
    _git(seed, "push", "-q", "origin", "main")
    return origin


@pytest.fixture
def repo(tmp_path, origin) -> Path:
    """A clone whose local main is stale (from before main moved on), with
    feature-b stacked on feature-a and main's newer commit merged in: the
    shape of a stacked feature branch."""
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "reset", "-q", "--hard", "origin/main~1")  # stale local main
    _git(repo, "switch", "-qc", "feature-b", "origin/feature-a")
    _commit(repo, "b.txt", "from b\n", "feature b")
    _git(repo, "merge", "-q", "--no-edit", "origin/main")
    return repo


def _files(diff: str) -> list[str]:
    return [line.split(" b/")[-1] for line in diff.splitlines() if line.startswith("diff --git ")]


class TestGetPrDiff:
    def test_stacked_on_another_feature_branch(self, repo):
        stacked = WorktreeManager.get_pr_diff(repo, "origin/feature-a")
        # Only what feature-b brings: not feature-a's commit. main's merged
        # commit is in it, as on GitHub for a stacked PR with main merged in.
        assert _files(stacked.committed) == ["b.txt", "later.txt"]
        assert stacked.merge_base == _git(repo, "rev-parse", "origin/feature-a")
        against_main = WorktreeManager.get_pr_diff(repo, "origin/main")
        assert _files(against_main.committed) == ["a.txt", "b.txt"]

    def test_stale_local_base_versus_origin(self, repo):
        stale = WorktreeManager.get_pr_diff(repo, "main")
        assert "later.txt" in _files(stale.committed)  # main's commit looks foreign
        fresh = WorktreeManager.get_pr_diff(repo, "origin/main")
        assert "later.txt" not in _files(fresh.committed)

    def test_uncommitted_changes_are_apart(self, repo):
        _commit(repo, "local.txt", "not pushed\n", "local only")
        (repo / "b.txt").write_text("from b\nunstaged\n")
        (repo / "staged.txt").write_text("staged\n")
        _git(repo, "add", "staged.txt")
        (repo / "untracked.txt").write_text("untracked\n")

        diff = WorktreeManager.get_pr_diff(repo, "origin/feature-a")
        # Unpushed commits are part of HEAD, so of the PR's diff.
        assert _files(diff.committed) == ["b.txt", "later.txt", "local.txt"]
        assert "+unstaged" not in diff.committed
        assert _files(diff.uncommitted) == ["b.txt", "staged.txt", "untracked.txt"]
        assert "+unstaged" in diff.uncommitted and "+staged" in diff.uncommitted
        assert "diff --git a/untracked.txt b/untracked.txt\nnew file\nuntracked" in diff.uncommitted

    def test_without_a_base_only_the_uncommitted(self, repo):
        (repo / "b.txt").write_text("edited\n")
        diff = WorktreeManager.get_pr_diff(repo, None)
        assert diff.committed == "" and diff.merge_base == ""
        assert _files(diff.uncommitted) == ["b.txt"]

    def test_no_merge_base(self, repo):
        with pytest.raises(ValueError, match="merge-base"):
            WorktreeManager.get_pr_diff(repo, "no-such-branch")

    def test_the_two_share_the_cap(self, repo):
        _commit(repo, "big.txt", "c\n" * 5000, "big")
        (repo / "small.txt").write_text("small\n")
        diff = WorktreeManager.get_pr_diff(repo, "origin/feature-a", max_bytes=4000)
        # The big committed part is cut; the small uncommitted one is whole.
        assert diff.committed.endswith("… diff truncated (too large to display)")
        assert len(diff.committed) < 4000
        assert diff.uncommitted == "diff --git a/small.txt b/small.txt\nnew file\nsmall\n"


class TestBaseRefs:
    def test_origin_wins_over_the_local_branch(self, repo):
        assert WorktreeManager.resolve_base_ref(repo, "main") == "origin/main"
        assert WorktreeManager.resolve_base_ref(repo, "feature-a") == "origin/feature-a"
        _git(repo, "branch", "local-only")
        assert WorktreeManager.resolve_base_ref(repo, "local-only") == "local-only"
        assert WorktreeManager.resolve_base_ref(repo, "nowhere") is None

    def test_fetch_moves_the_remote_tracking_branch(self, tmp_path, origin, repo):
        other = tmp_path / "other"
        _git(tmp_path, "clone", "-q", str(origin), str(other))
        _commit(other, "news.txt", "news\n", "news")
        _git(other, "push", "-q", "origin", "main")
        before = _git(repo, "rev-parse", "origin/main")
        assert WorktreeManager.fetch_branch(repo, "main") is None
        assert _git(repo, "rev-parse", "origin/main") == _git(other, "rev-parse", "HEAD") != before
        assert WorktreeManager.fetch_branch(repo, "no-such-branch")


def _service(repo: Path) -> WorktreeService:
    service = WorktreeService(find_root=lambda: repo)
    service.rescan()
    service._gh_available = False  # never the real gh
    return service


def _pr(base_ref: str, state: str = "OPEN") -> PrInfo:
    return PrInfo(number=4242, title="b", state=state, checks=[], base_ref=base_ref)


@pytest.mark.asyncio
class TestResolvedBase:
    async def test_the_open_prs_base_branch(self, repo):
        service = _service(repo)
        service._pr_infos[str(repo)] = _pr("feature-a")
        reply = await service.get_pr_diff(str(repo), default_branch="main")
        assert reply["base"] == {
            "ref": "origin/feature-a",
            "branch": "feature-a",
            "source": "pr",
            "merge_base": _git(repo, "rev-parse", "origin/feature-a"),
            "pr_number": 4242,
        }
        assert _files(reply["diff"]) == ["b.txt", "later.txt"]
        assert reply["uncommitted"] == ""

    async def test_a_closed_pr_or_none_falls_back_to_origin_default(self, repo):
        service = _service(repo)
        service._pr_infos[str(repo)] = _pr("feature-a", state="MERGED")
        reply = await service.get_pr_diff(str(repo), default_branch="main")
        assert reply["base"]["ref"] == "origin/main"  # not the stale local main
        assert reply["base"]["source"] == "default"
        assert reply["base"]["pr_number"] is None
        assert _files(reply["diff"]) == ["a.txt", "b.txt"]
        service._pr_infos[str(repo)] = None
        reply = await service.get_pr_diff(str(repo), default_branch="nonexistent")
        assert reply["base"]["ref"] == "origin/main"  # the main worktree's branch

    async def test_an_override_is_used_as_given(self, repo):
        service = _service(repo)
        service._pr_infos[str(repo)] = _pr("feature-a")
        reply = await service.get_pr_diff(str(repo), base="main", default_branch="main")
        assert reply["base"]["ref"] == "main" and reply["base"]["source"] == "override"
        assert "later.txt" in _files(reply["diff"])
        with pytest.raises(ValueError):
            await service.get_pr_diff(str(repo), base="no-such-branch")

    async def test_no_base_at_all_still_shows_the_uncommitted(self, tmp_path):
        lone = tmp_path / "lone"
        lone.mkdir()
        _git(lone, "init", "-q", "-b", "trunk")
        _commit(lone, "x.txt", "x\n", "x")
        (lone / "x.txt").write_text("y\n")
        service = _service(lone)
        service._pr_infos[str(lone)] = None
        _git(lone, "switch", "-q", "--detach")  # no branch to fall back to
        service.rescan()
        reply = await service.get_pr_diff(str(lone), default_branch="develop")
        assert reply["base"] is None and "develop" in reply["base_error"]
        assert _files(reply["uncommitted"]) == ["x.txt"]

    async def test_fetch_updates_the_base_first(self, tmp_path, origin, repo):
        other = tmp_path / "other"
        _git(tmp_path, "clone", "-q", str(origin), str(other))
        _git(other, "switch", "-q", "feature-a")
        _commit(other, "a2.txt", "more a\n", "more a")
        _git(other, "push", "-q", "origin", "feature-a")
        service = _service(repo)
        service._pr_infos[str(repo)] = _pr("feature-a")
        stale = await service.get_pr_diff(str(repo))
        fresh = await service.get_pr_diff(str(repo), fetch=True)
        assert "fetch_error" not in fresh
        assert fresh["base"]["ref"] == "origin/feature-a"
        assert _git(repo, "rev-parse", "origin/feature-a") == _git(other, "rev-parse", "HEAD")
        # Same merge-base (feature-b has not merged the new commit): the
        # base moved, the PR's diff did not.
        assert fresh["base"]["merge_base"] == stale["base"]["merge_base"]
        assert fresh["diff"] == stale["diff"]

    async def test_a_failed_fetch_is_reported_not_raised(self, repo):
        service = _service(repo)
        service._pr_infos[str(repo)] = _pr("gone-upstream")
        reply = await service.get_pr_diff(str(repo), default_branch="main", fetch=True)
        assert reply["fetch_error"]
        assert reply["base"]["ref"] == "origin/main"

    async def test_waits_for_a_pr_lookup_in_flight(self, repo, monkeypatch):
        service = _service(repo)
        service._gh_available = True
        calls = []

        def fetch(root, targets):
            calls.append([wt.path for wt in targets])
            return {str(repo): _pr("feature-a")}, 0

        monkeypatch.setattr(service, "_fetch_pr_infos", fetch)
        import asyncio

        poll = asyncio.ensure_future(service.refresh_pr_info([str(repo)]))
        await asyncio.sleep(0)  # the poll has claimed the path
        reply = await service.get_pr_diff(str(repo), default_branch="main")
        await poll
        assert calls == [[str(repo)]]
        assert reply["base"]["source"] == "pr"
