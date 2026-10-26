"""Clean merges of the base branch (docs/design/rules-plan.md R3c): left out
of ``pr.commits_after_ready``; a conflict resolved by hand, an extra edit,
a merge of another branch or a normal commit still count. Real temporary
git repos (an ``origin`` bare repo, a clone, a second clone that pushes
what GitHub's "Update branch" would), both merge paths (``merge-tree
--write-tree`` on git ≥ 2.38, the temporary index below it), the fetch of a
merge that is not local, the head check and the timeline through a fake
``gh``. No network.

HOME and every XDG dir are under the test's tmp_path (tests/conftest.py)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lazyagent.engine.base_merges import BaseMerges, git_version
from lazyagent.engine.worktrees import WorktreeService
from lazyagent.models import PrInfo, WorktreeInfo
from lazyagent.worktree_manager import GithubRepo, WorktreeManager

GIT = git_version()
HAS_MERGE_TREE = GIT is not None and GIT >= (2, 38)
OLD = (2, 34)                 # forces the temporary index
NEW = (2, 38)
READY = "2026-10-01T10:00:00Z"


def _env(tmp: Path) -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x",
            "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x",
            "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}


class Repo:
    """``origin`` (bare) and a clone of it at ``root``, branch ``main``."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.env = _env(tmp)
        self.origin = tmp / "origin.git"
        self.root = tmp / "clone"
        self.git_in(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git_in(tmp, "clone", "-q", str(self.origin), str(self.root))
        self.git("checkout", "-q", "-b", "main")
        self.write("f.txt", "".join(f"line {n}\n" for n in range(1, 11)))
        self.write("other.txt", "other\n")
        self.commit("base")
        self.git("push", "-q", "origin", "main")

    def git_in(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def git(self, *args: str) -> str:
        return self.git_in(self.root, *args)

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text)

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.root / name
        path.write_text(path.read_text().replace(old, new))

    def commit(self, msg: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def feature(self) -> str:
        """A ``feat`` branch with one commit, and ``main`` moved on (pushed)."""
        self.git("checkout", "-q", "-b", "feat")
        self.edit("f.txt", "line 1\n", "line 1 feat\n")
        head = self.commit("feat")
        self.git("checkout", "-q", "main")
        self.edit("f.txt", "line 10\n", "line 10 main\n")
        self.write("new.txt", "new on main\n")
        self.commit("main moves")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "feat")
        return head

    def merge(self, ref: str = "origin/main") -> str:
        self.git("fetch", "-q", "origin")
        self.git("merge", "-q", "--no-edit", ref)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path)


def _both(fn):
    """Run a test on the temporary-index path, and on merge-tree when git has it."""
    params = [pytest.param(OLD, id="index"),
              pytest.param(NEW, id="merge-tree", marks=pytest.mark.skipif(
                  not HAS_MERGE_TREE, reason="git < 2.38: no merge-tree --write-tree"))]
    return pytest.mark.parametrize("version", params)(fn)


def _classify(repo: Repo, sha: str, version, **kw) -> tuple[bool, str]:
    return BaseMerges(version=version, fetch=False, **kw).verdict(str(repo.root), sha, "main")


# --- what counts --------------------------------------------------------------


@_both
def test_a_clean_merge_of_the_base_does_not_count(repo, version):
    repo.feature()
    merge = repo.merge()
    assert _classify(repo, merge, version) == (True, "clean base merge")


@_both
def test_a_conflict_resolved_by_hand_counts(repo, version):
    repo.feature()
    repo.git("checkout", "-q", "main")
    repo.edit("f.txt", "line 1\n", "line 1 main\n")
    repo.commit("main edits line 1")
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "feat")
    repo.git("fetch", "-q", "origin")
    assert subprocess.run(["git", "merge", "-q", "--no-edit", "origin/main"], cwd=repo.root,
                          env=repo.env, capture_output=True).returncode != 0
    repo.write("f.txt", (repo.root / "f.txt").read_text().split("<<<<<<<")[0]
               + "line 1 both\n" + "".join(f"line {n}\n" for n in range(2, 10))
               + "line 10 main\n")
    merge = repo.commit("resolved")
    clean, why = _classify(repo, merge, version)
    assert not clean and why in ("conflicts", "f.txt: conflicts")


@_both
def test_a_merge_with_an_extra_edit_counts(repo, version):
    repo.feature()
    repo.git("fetch", "-q", "origin")
    repo.git("merge", "-q", "--no-commit", "--no-ff", "origin/main")
    repo.edit("other.txt", "other\n", "other, edited in the merge\n")
    merge = repo.commit("merge with an edit")
    assert _classify(repo, merge, version) == (False, "the tree differs from the automatic merge")


@_both
def test_a_merge_of_another_branch_counts(repo, version):
    start = repo.feature()
    repo.git("checkout", "-q", "-b", "side", "origin/main~1")
    repo.write("side.txt", "side\n")
    repo.commit("side")
    repo.git("checkout", "-q", "feat")
    assert repo.git("rev-parse", "HEAD") == start
    merge = repo.merge("side")
    assert _classify(repo, merge, version) == (False, "neither parent is on main")


@_both
def test_a_normal_commit_counts(repo, version):
    repo.feature()
    repo.edit("other.txt", "other\n", "changed\n")
    sha = repo.commit("normal")
    assert _classify(repo, sha, version) == (False, "not a merge")


def test_the_local_base_branch_counts_as_the_base_too(repo):
    repo.feature()
    repo.git("checkout", "-q", "main")
    repo.write("local.txt", "only local\n")
    repo.commit("not pushed")
    repo.git("checkout", "-q", "feat")
    merge = repo.merge("main")
    assert _classify(repo, merge, OLD)[0] is True


# --- the temporary index: what it decides and what it leaves undecided --------


def test_a_mode_change_on_one_side_merges_cleanly(repo):
    repo.feature()
    repo.git("checkout", "-q", "main")
    repo.edit("f.txt", "line 5\n", "line 5 main\n")
    repo.commit("edit")
    (repo.root / "f.txt").chmod(0o755)
    repo.commit("chmod")
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "feat")
    merge = repo.merge()
    assert repo.git("ls-tree", merge, "f.txt").startswith("100755")
    assert _classify(repo, merge, OLD)[0] is True


def test_a_binary_conflict_is_undecided_and_counts(repo):
    (repo.root / "b.bin").write_bytes(b"\x00\x01base")
    repo.commit("binary")
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "-b", "feat")
    (repo.root / "b.bin").write_bytes(b"\x00\x01feat")
    repo.commit("feat binary")
    repo.git("checkout", "-q", "main")
    (repo.root / "b.bin").write_bytes(b"\x00\x01main")
    repo.commit("main binary")
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "feat")
    repo.git("fetch", "-q", "origin")
    subprocess.run(["git", "merge", "-q", "--no-edit", "origin/main"], cwd=repo.root,
                   env=repo.env, capture_output=True)
    repo.git("checkout", "--theirs", "b.bin")
    merge = repo.commit("took main's binary")
    assert _classify(repo, merge, OLD) == (False, "b.bin: cannot merge")


def test_several_merge_bases_are_undecided_and_count(repo):
    # A criss-cross: feat and main each merged the other's earlier tip.
    repo.git("checkout", "-q", "-b", "feat")
    repo.edit("f.txt", "line 1\n", "line 1 feat\n")
    f1 = repo.commit("f1")
    repo.git("checkout", "-q", "main")
    repo.edit("f.txt", "line 10\n", "line 10 main\n")
    m1 = repo.commit("m1")
    repo.git("merge", "-q", "--no-edit", f1)
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "feat")
    repo.git("merge", "-q", "--no-edit", m1)
    repo.edit("other.txt", "other\n", "feat again\n")
    repo.commit("f2")
    merge = repo.merge()
    assert _classify(repo, merge, OLD) == (False, "2 merge bases")


def test_the_repository_is_never_written_to(repo):
    repo.feature()
    merge = repo.merge()
    objects = repo.root / ".git" / "objects"
    before = sorted(p.name for p in objects.rglob("*") if p.is_file())
    index = (repo.root / ".git" / "index").read_bytes()
    assert BaseMerges(version=OLD, fetch=False).is_clean_base_merge(str(repo.root), merge, "main")
    assert sorted(p.name for p in objects.rglob("*") if p.is_file()) == before
    assert (repo.root / ".git" / "index").read_bytes() == index
    assert repo.git("status", "--porcelain") == ""


# --- missing commits, old git, the cache ----------------------------------------


def _update_branch_elsewhere(repo: Repo) -> str:
    """What GitHub's "Update branch" does: a merge pushed from somewhere else."""
    repo.feature()
    repo.git("push", "-q", "origin", "feat")
    other = repo.tmp / "elsewhere"
    repo.git_in(repo.tmp, "clone", "-q", str(repo.origin), str(other))
    repo.git_in(other, "checkout", "-q", "feat")
    repo.git_in(other, "merge", "-q", "--no-edit", "origin/main")
    repo.git_in(other, "push", "-q", "origin", "feat")
    return repo.git_in(other, "rev-parse", "HEAD")


def test_a_merge_not_here_is_fetched_once_then_classified(repo):
    merge = _update_branch_elsewhere(repo)
    root = str(repo.root)
    assert BaseMerges(version=OLD, fetch=False).verdict(root, merge, "main") == (
        False, "the commit is not here")
    merges = BaseMerges(version=OLD)
    with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": repo.env["GIT_CONFIG_GLOBAL"]}):
        assert merges.verdict(root, merge, "main") == (True, "clean base merge")
    # No ref moved but origin/main (as any fetch of it does); no FETCH_HEAD.
    assert repo.git("rev-parse", "HEAD") != merge
    assert not (repo.root / ".git" / "FETCH_HEAD").exists()
    calls = merges.git_calls
    assert merges.is_clean_base_merge(root, merge, "main")
    assert merges.git_calls == calls           # cached


def test_a_failed_fetch_counts_and_is_not_retried_at_once(repo):
    merge = "0" * 40
    now = [0.0]
    merges = BaseMerges(version=OLD, clock=lambda: now[0])
    fetches: list[list[str]] = []
    real = merges._git

    def spy(root, args, *a, **kw):
        if args[0] == "fetch":
            fetches.append(args)
        return real(root, args, *a, **kw)

    merges._git = spy
    root = str(repo.root)
    assert merges.verdict(root, merge, "main") == (False, "the commit is not here")
    assert merges.verdict(root, merge, "main")[0] is False
    assert len(fetches) == 1 and fetches[0][-2:] == [merge, "refs/heads/main"]
    now[0] = 601.0
    merges.verdict(root, merge, "main")
    assert len(fetches) == 2                 # again after the back-off


def test_git_older_than_2_29_never_fetches(repo):
    merge = _update_branch_elsewhere(repo)
    merges = BaseMerges(version=(2, 28))
    assert merges.verdict(str(repo.root), merge, "main") == (False, "the commit is not here")


def test_the_git_version_is_read_once_and_picks_the_path(repo, monkeypatch):
    repo.feature()
    merge = repo.merge()
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr("lazyagent.worktree_manager.subprocess.run", spy)
    assert BaseMerges(version=OLD, fetch=False).is_clean_base_merge(str(repo.root), merge,
                                                                    "main")
    assert any(c[1:3] == ["read-tree", "-i"] for c in seen)
    assert not any(c[1] == "merge-tree" for c in seen)
    if HAS_MERGE_TREE:
        seen.clear()
        assert BaseMerges(fetch=False).is_clean_base_merge(str(repo.root), merge, "main")
        assert any(c[1] == "merge-tree" for c in seen)


def test_git_version_parses_and_fails_soft():
    out = subprocess.CompletedProcess([], 0, "git version 2.34.1\n", "")
    assert git_version(lambda *a, **k: out) == (2, 34)
    out = subprocess.CompletedProcess([], 0, "git version 2.39.5 (Apple Git-154)\n", "")
    assert git_version(lambda *a, **k: out) == (2, 39)

    def boom(*a, **k):
        raise OSError("no git")

    assert git_version(boom) is None


def test_bad_input_counts_without_running_git():
    merges = BaseMerges(version=OLD, fetch=False)
    assert merges.verdict("", "a" * 40, "main")[0] is False
    assert merges.verdict("/x", "--upload-pack=x", "main")[0] is False
    assert merges.verdict("/x", "a" * 40, "--evil")[0] is False
    assert merges.git_calls == 0


# --- force pushes and the head check --------------------------------------------


def test_a_force_push_that_only_redoes_the_base_merge_does_not_count(repo):
    repo.feature()
    first = repo.merge()
    repo.git("checkout", "-q", "main")
    repo.write("later.txt", "later\n")
    repo.commit("main again")
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "feat")
    repo.git("reset", "-q", "--hard", "HEAD^1")
    second = repo.merge()
    merges = BaseMerges(version=OLD, fetch=False)
    root = str(repo.root)
    assert merges.moved_only_by_base_merges(root, first, second, "main") == 1
    # A real change in the force push: counted.
    repo.edit("other.txt", "other\n", "amended\n")
    repo.git("commit", "-q", "-a", "--amend", "--no-edit")
    amended = repo.git("rev-parse", "HEAD")
    assert merges.moved_only_by_base_merges(root, first, amended, "main") is None
    # A rebase: counted.
    repo.git("reset", "-q", "--hard", "HEAD^1")
    repo.git("rebase", "-q", "origin/main")
    assert merges.moved_only_by_base_merges(root, first, repo.git("rev-parse", "HEAD"),
                                            "main") is None


def _pr(head: str, after: int = 0, **kw) -> PrInfo:
    return PrInfo(7, "t", "OPEN", [], head_sha=head, base_ref="main", is_draft=False,
                  commits_after_ready=after, ready_at=READY, base_merges_after_ready=0, **kw)


def test_the_head_check_applies_the_same_rule(repo):
    service = WorktreeService(lambda root: None)
    service.base_merges = BaseMerges(version=OLD, fetch=False)
    root, wt = str(repo.root), "/w"
    h0 = repo.feature()
    assert service._ready_head(wt, _pr(h0), root).commits_after_ready == 0
    merge = repo.merge()
    moved = service._ready_head(wt, _pr(merge), root)
    assert (moved.commits_after_ready, moved.base_merges_after_ready) == (0, 1)
    repo.edit("other.txt", "other\n", "after the merge\n")
    normal = repo.commit("normal")
    assert service._ready_head(wt, _pr(normal), root).commits_after_ready == 1
    # Not here: counted, as before R3c.
    assert service._ready_head(wt, _pr("f" * 40), root).commits_after_ready == 1


# --- the timeline through a fake gh, end to end -----------------------------------


def _completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


def _timeline_commit(sha: str, parents: int, when: str) -> dict:
    return {"__typename": "PullRequestCommit",
            "commit": {"committedDate": when, "oid": sha, "parents": {"totalCount": parents}}}


def test_the_poll_leaves_clean_base_merges_out_end_to_end(repo, tmp_path):
    start = repo.feature()
    clean = repo.merge()
    repo.edit("other.txt", "other\n", "a real change\n")
    normal = repo.commit("after ready")
    repo.git("checkout", "-q", "main")
    repo.write("more.txt", "more\n")
    repo.commit("main again")
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "feat")
    second = repo.merge()
    node = {"number": 7, "title": "PR 7", "state": "OPEN", "url": "u", "isCrossRepository": False,
            "headRefOid": second, "baseRefName": "main", "isDraft": False,
            "author": {"login": "owner"}, "reviews": {"totalCount": 0}, "commits": {"nodes": []}}
    items = [_timeline_commit(start, 1, "2026-10-01T09:00:00Z"),
             {"__typename": "ReadyForReviewEvent", "createdAt": READY},
             _timeline_commit(clean, 2, "2026-10-01T11:00:00Z"),
             _timeline_commit(normal, 1, "2026-10-01T12:00:00Z"),
             _timeline_commit(second, 2, "2026-10-01T13:00:00Z")]
    extra = {"r0": {"createdAt": "2026-10-01T08:00:00Z",
                    "labels": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                    "reviews": {"totalCount": 0, "pageInfo": {"hasPreviousPage": False},
                                "nodes": []},
                    "timelineItems": {"pageInfo": {"hasPreviousPage": False}, "nodes": items}}}
    main = {"data": {"repository": {"defaultBranchRef": {"name": "main"},
                                    "pr0": {"nodes": [node]}}}}
    (tmp_path / "mgr" / ".git").mkdir(parents=True)
    manager = WorktreeManager(tmp_path / "mgr")
    wts = [WorktreeInfo("/w0", "abc", "feat", False, False)]
    with patch("lazyagent.worktree_manager.subprocess.run",
               side_effect=[_completed(returncode=1), _completed(json.dumps(main)),
                            _completed(json.dumps({"data": {"repository": extra}}))]):
        infos = manager.get_pr_infos(wts, GithubRepo("acme", "widgets"), review=True)
    pr = infos["/w0"]
    assert (pr.commits_after_ready, pr.after_ready_merges) == (3, (clean, second))
    service = WorktreeService(lambda root: None)
    service.base_merges = BaseMerges(version=OLD, fetch=False)
    got = service._after_ready(str(repo.root), infos)["/w0"]
    assert (got.commits_after_ready, got.base_merges_after_ready) == (1, 2)
    # Unknown stays unknown.
    unknown = PrInfo(8, "t", "OPEN", [], base_ref="main")
    assert service._after_ready(str(repo.root), {"/w1": unknown})["/w1"] is unknown


def test_the_cost_is_local_git_only_and_cached(repo):
    repo.feature()
    merge = repo.merge()
    pr = _pr(merge, after=1, after_ready_merges=(merge,))
    merges = BaseMerges(version=OLD, fetch=False)
    root = str(repo.root)
    assert merges.adjust(root, pr).commits_after_ready == 0
    first = merges.git_calls
    assert first <= 24              # 18 on the temporary index, one file merged
    for _ in range(5):
        merges.adjust(root, pr)
    assert merges.git_calls == first
