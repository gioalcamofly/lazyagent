from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from lazyagent.models import GitStatus, WorktreeInfo
from lazyagent.worktree_manager import GithubRepo, WorktreeManager, WorktreeManagerError


SAMPLE_PORCELAIN = """\
worktree /home/user/repo
HEAD 6e23555e0c62e491fa146ee27457d6999c868b7d
branch refs/heads/fix-prompt-fixture-reference-date-ordering

worktree /home/user/repo-PROJ-10761
HEAD 53f2d34b6d9155e0e05c1ba88457a92f6d96b134
branch refs/heads/PROJ-13026-fix-nested-validators

worktree /home/user/repo-PROJ-11067
HEAD 94959417dca5a9877ea56b3f86aedfc1157da295
branch refs/heads/PROJ-11067-Remove-Feature-Flags

worktree /home/user/repo-PROJ-12413
HEAD 93d4eed576e4841287e60cd88c2d99540a45d48d
branch refs/heads/PROJ-12413-AIR-Instructions-for-New-Customers-updated-event-is-missing

worktree /home/user/repo-PROJ-12756
HEAD a6b80f3538b6f4756197e56ce053dd53a1271824
branch refs/heads/PROJ-12756-Add-automatic-LLM-fallback-for-call-evaluation

worktree /home/user/repo-PROJ-12802
HEAD 93018f3d91c94f7a40e6fb0a4417fd4aa714631d
branch refs/heads/PROJ-12802-Instrumentation-Event-Tracking

worktree /home/user/repo-fix-service-choice
HEAD 064d8c544fdf95ae5d8aefd4d36ba27d17b6b409
branch refs/heads/fix-service-choice-playbook-switch
"""


class TestParsePorcelain:
    def test_parses_correct_count(self):
        worktrees = WorktreeManager._parse_porcelain(SAMPLE_PORCELAIN)
        assert len(worktrees) == 7

    def test_first_worktree_is_main(self):
        worktrees = WorktreeManager._parse_porcelain(SAMPLE_PORCELAIN)
        assert worktrees[0].is_main is True
        assert all(wt.is_main is False for wt in worktrees[1:])

    def test_paths_parsed(self):
        worktrees = WorktreeManager._parse_porcelain(SAMPLE_PORCELAIN)
        assert worktrees[0].path == "/home/user/repo"
        assert worktrees[1].path == "/home/user/repo-PROJ-10761"

    def test_heads_parsed(self):
        worktrees = WorktreeManager._parse_porcelain(SAMPLE_PORCELAIN)
        assert worktrees[0].head == "6e23555e0c62e491fa146ee27457d6999c868b7d"

    def test_branch_strips_refs_heads(self):
        worktrees = WorktreeManager._parse_porcelain(SAMPLE_PORCELAIN)
        assert worktrees[0].branch == "fix-prompt-fixture-reference-date-ordering"
        assert worktrees[2].branch == "PROJ-11067-Remove-Feature-Flags"

    def test_no_worktrees_is_bare(self):
        worktrees = WorktreeManager._parse_porcelain(SAMPLE_PORCELAIN)
        assert all(wt.is_bare is False for wt in worktrees)

    def test_empty_input(self):
        assert WorktreeManager._parse_porcelain("") == []
        assert WorktreeManager._parse_porcelain("   \n  ") == []

    def test_bare_worktree(self):
        raw = """\
worktree /home/user/repo.git
HEAD abc123def456
bare
"""
        worktrees = WorktreeManager._parse_porcelain(raw)
        assert len(worktrees) == 1
        assert worktrees[0].is_bare is True
        assert worktrees[0].branch is None

    def test_detached_head(self):
        raw = """\
worktree /home/user/repo
HEAD abc123def456
detached
"""
        worktrees = WorktreeManager._parse_porcelain(raw)
        assert len(worktrees) == 1
        assert worktrees[0].branch is None
        assert worktrees[0].is_bare is False


class TestCreate:
    def test_create_calls_git_with_correct_args(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            result = manager.create("feat-x", "main")

        expected_path = str(tmp_path / "myrepo-feat-x")
        assert result == expected_path
        mock_run.assert_called_once_with(
            ["git", "worktree", "add", "-b", "feat-x", expected_path, "main"],
            capture_output=True,
            text=True,
            cwd=repo,
            check=True,
        )

    def test_create_default_base_branch(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            manager.create("feat-y")

        args = mock_run.call_args[0][0]
        assert args[-1] == "master"

    def test_create_derives_path_from_repo_name(self, tmp_path):
        repo = tmp_path / "smith"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch("lazyagent.worktree_manager.subprocess.run"):
            result = manager.create("PROJ-123-fix")

        assert result == str(tmp_path / "smith-PROJ-123-fix")

    def test_create_raises_on_failure(self, tmp_path):
        import subprocess as sp

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=sp.CalledProcessError(1, "git", stderr="branch already exists"),
        ):
            with pytest.raises(WorktreeManagerError, match="branch already exists"):
                manager.create("feat-x")


class TestRemove:
    def test_remove_calls_git_with_correct_args(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            manager.remove("/path/to/worktree")

        mock_run.assert_called_once_with(
            ["git", "worktree", "remove", "/path/to/worktree"],
            capture_output=True,
            text=True,
            cwd=repo,
            check=True,
        )

    def test_remove_with_force_flag(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            manager.remove("/path/to/worktree", force=True)

        args = mock_run.call_args[0][0]
        assert args == ["git", "worktree", "remove", "--force", "/path/to/worktree"]

    def test_remove_raises_on_failure(self, tmp_path):
        import subprocess as sp

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=sp.CalledProcessError(1, "git", stderr="worktree is dirty"),
        ):
            with pytest.raises(WorktreeManagerError, match="worktree is dirty"):
                manager.remove("/path/to/worktree")


class TestParseGitStatus:
    def test_clean_with_upstream(self):
        raw = "## main...origin/main\n"
        gs = WorktreeManager._parse_git_status(raw)
        assert gs.dirty_count == 0
        assert gs.has_upstream is True
        assert gs.ahead == 0
        assert gs.behind == 0

    def test_ahead_and_behind(self):
        raw = "## main...origin/main [ahead 2, behind 1]\n"
        gs = WorktreeManager._parse_git_status(raw)
        assert gs.ahead == 2
        assert gs.behind == 1
        assert gs.has_upstream is True

    def test_ahead_only(self):
        raw = "## main...origin/main [ahead 5]\n"
        gs = WorktreeManager._parse_git_status(raw)
        assert gs.ahead == 5
        assert gs.behind == 0

    def test_behind_only(self):
        raw = "## main...origin/main [behind 3]\n"
        gs = WorktreeManager._parse_git_status(raw)
        assert gs.ahead == 0
        assert gs.behind == 3

    def test_no_upstream(self):
        raw = "## feature-branch\n"
        gs = WorktreeManager._parse_git_status(raw)
        assert gs.has_upstream is False
        assert gs.ahead == 0
        assert gs.behind == 0

    def test_dirty_files(self):
        raw = (
            "## main...origin/main\n"
            " M src/foo.py\n"
            "?? new_file.txt\n"
            "A  staged.py\n"
        )
        gs = WorktreeManager._parse_git_status(raw)
        assert gs.dirty_count == 3

    def test_empty_output(self):
        gs = WorktreeManager._parse_git_status("")
        assert gs.dirty_count == 0
        assert gs.has_upstream is False

    def test_detached_head(self):
        raw = "## HEAD (no branch)\n"
        gs = WorktreeManager._parse_git_status(raw)
        assert gs.has_upstream is False
        assert gs.dirty_count == 0


class TestGetGitStatus:
    def test_correct_subprocess_args(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "## main...origin/main\n"
            manager.get_git_status("/path/to/worktree")

        mock_run.assert_called_once_with(
            ["git", "status", "--porcelain=v1", "--branch"],
            capture_output=True,
            text=True,
            cwd="/path/to/worktree",
            check=True,
        )

    def test_returns_default_on_failure(self, tmp_path):
        import subprocess as sp

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=sp.CalledProcessError(1, "git"),
        ):
            gs = manager.get_git_status("/bad/path")

        assert gs == GitStatus()


class TestGetLastCommitSubject:
    def test_correct_subprocess_args(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Fix the bug\n"
            result = manager.get_last_commit_subject("/path/to/worktree")

        assert result == "Fix the bug"
        mock_run.assert_called_once_with(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True,
            text=True,
            cwd="/path/to/worktree",
            check=True,
        )

    def test_returns_empty_on_failure(self, tmp_path):
        import subprocess as sp

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        manager = WorktreeManager(repo)

        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=sp.CalledProcessError(1, "git"),
        ):
            result = manager.get_last_commit_subject("/bad/path")

        assert result == ""


class _FakeStat:
    """Minimal os.stat_result stand-in: get_diff only reads st_size."""

    def __init__(self, st_size: int) -> None:
        self.st_size = st_size


class TestGetDiff:
    def test_subprocess_args(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.stdout = b""
            mock_run.return_value.returncode = 0
            WorktreeManager.get_diff("/path/to/worktree")

        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["git", "diff"]
        assert calls[0][1]["cwd"] == "/path/to/worktree"
        assert calls[1][0][0] == ["git", "diff", "--cached", "--numstat", "-z"]
        assert calls[1][1]["cwd"] == "/path/to/worktree"
        assert calls[2][0][0] == ["git", "ls-files", "--others", "--exclude-standard", "-z"]
        assert calls[2][1]["cwd"] == "/path/to/worktree"

    def test_tracked_changes(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            diff_result = type("R", (), {"returncode": 0, "stdout": b"diff --git a/f\n"})()
            cached_result = type("R", (), {"returncode": 0, "stdout": b""})()
            ls_result = type("R", (), {"returncode": 0, "stdout": b""})()
            mock_run.side_effect = [diff_result, cached_result, ls_result]
            result = WorktreeManager.get_diff("/path/to/worktree")

        assert result == "diff --git a/f"

    def test_untracked_files_shows_diff(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0]
            r = type("R", (), {"returncode": 0, "stdout": b""})()
            if cmd == ["git", "diff"]:
                r.stdout = b""
            elif cmd == ["git", "diff", "--cached", "--numstat", "-z"]:
                r.stdout = b""
            elif cmd == ["git", "ls-files", "--others", "--exclude-standard", "-z"]:
                r.stdout = b"new.txt\0"
            return r

        with (
            patch("lazyagent.worktree_manager.subprocess.run", side_effect=_side_effect),
            patch("pathlib.Path.stat", return_value=_FakeStat(11)),
            patch("pathlib.Path.read_bytes", return_value=b"new content"),
            patch("pathlib.Path.read_text", return_value="new content"),
        ):
            result = WorktreeManager.get_diff("/path/to/worktree")

        assert "new.txt" in result
        assert "new content" in result

    def test_tracked_and_untracked_combined(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0]
            r = type("R", (), {"returncode": 0, "stdout": b""})()
            if cmd == ["git", "diff"]:
                r.stdout = b"tracked diff\n"
            elif cmd == ["git", "diff", "--cached", "--numstat", "-z"]:
                r.stdout = b""
            elif cmd == ["git", "ls-files", "--others", "--exclude-standard", "-z"]:
                r.stdout = b"new.txt\0"
            return r

        with (
            patch("lazyagent.worktree_manager.subprocess.run", side_effect=_side_effect),
            patch("pathlib.Path.stat", return_value=_FakeStat(17)),
            patch("pathlib.Path.read_bytes", return_value=b"untracked content"),
            patch("pathlib.Path.read_text", return_value="untracked content"),
        ):
            result = WorktreeManager.get_diff("/path/to/worktree")

        assert "tracked diff" in result
        assert "new.txt" in result

    def test_empty_on_failure(self):
        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=OSError("not a repo"),
        ):
            result = WorktreeManager.get_diff("/bad/path")

        assert result == ""

    def test_empty_when_clean(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.stdout = b""
            mock_run.return_value.returncode = 0
            result = WorktreeManager.get_diff("/clean/repo")

        assert result == ""


class TestParsePrInfo:
    def test_valid_json_with_checks(self):
        raw = json.dumps({
            "number": 42,
            "title": "Fix bug",
            "state": "OPEN",
            "statusCheckRollup": [
                {"name": "build", "status": "COMPLETED", "conclusion": "success"},
                {"name": "lint", "status": "COMPLETED", "conclusion": "failure"},
            ],
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.number == 42
        assert pr.title == "Fix bug"
        assert pr.state == "OPEN"
        assert len(pr.checks) == 2
        assert pr.checks[0].name == "build"
        assert pr.checks[0].conclusion == "success"
        assert pr.checks[1].conclusion == "failure"

    def test_without_checks(self):
        raw = json.dumps({
            "number": 10,
            "title": "Add feature",
            "state": "OPEN",
            "statusCheckRollup": [],
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.checks == []
        assert pr.overall_status == "none"

    def test_merged_pr(self):
        raw = json.dumps({
            "number": 5,
            "title": "Merged PR",
            "state": "MERGED",
            "statusCheckRollup": [
                {"name": "build", "status": "COMPLETED", "conclusion": "success"},
            ],
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.state == "MERGED"

    def test_invalid_json(self):
        assert WorktreeManager._parse_pr_info("not json") is None

    def test_empty_string(self):
        assert WorktreeManager._parse_pr_info("") is None

    def test_context_field_fallback(self):
        raw = json.dumps({
            "number": 7,
            "title": "PR with context",
            "state": "OPEN",
            "statusCheckRollup": [
                {"context": "ci/build", "status": "COMPLETED", "state": "success"},
            ],
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.checks[0].name == "ci/build"
        assert pr.checks[0].conclusion == "success"

    def test_null_status_check_rollup(self):
        raw = json.dumps({
            "number": 8,
            "title": "No rollup",
            "state": "OPEN",
            "statusCheckRollup": None,
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.checks == []

    def test_parses_url_and_review_and_mergeable(self):
        raw = json.dumps({
            "number": 50,
            "title": "Full PR",
            "state": "OPEN",
            "statusCheckRollup": [],
            "url": "https://github.com/org/repo/pull/50",
            "reviewDecision": "APPROVED",
            "mergeable": "CONFLICTING",
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.url == "https://github.com/org/repo/pull/50"
        assert pr.review_decision == "APPROVED"
        assert pr.mergeable == "CONFLICTING"

    def test_missing_new_fields_default_empty(self):
        raw = json.dumps({
            "number": 51,
            "title": "Minimal",
            "state": "OPEN",
            "statusCheckRollup": [],
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.url == ""
        assert pr.review_decision == ""
        assert pr.mergeable == ""

    def test_null_review_decision(self):
        raw = json.dumps({
            "number": 52,
            "title": "Null review",
            "state": "OPEN",
            "statusCheckRollup": [],
            "reviewDecision": None,
            "mergeable": None,
        })
        pr = WorktreeManager._parse_pr_info(raw)
        assert pr is not None
        assert pr.review_decision == ""
        assert pr.mergeable == ""


class TestGetPrInfo:
    def test_subprocess_args(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps({
                "number": 1, "title": "t", "state": "OPEN",
                "statusCheckRollup": [],
            })
            WorktreeManager.get_pr_info("/path/to/worktree")

        mock_run.assert_called_once_with(
            ["gh", "pr", "view", "--json", "number,title,state,statusCheckRollup,url,reviewDecision,mergeable"],
            capture_output=True,
            text=True,
            cwd="/path/to/worktree",
            timeout=10,
        )

    def test_none_on_failure(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = WorktreeManager.get_pr_info("/bad/path")

        assert result is None

    def test_none_on_timeout(self):
        import subprocess as sp

        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=sp.TimeoutExpired("gh", 10),
        ):
            result = WorktreeManager.get_pr_info("/path")

        assert result is None


class TestIsGhAvailable:
    def test_true_when_authenticated(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert WorktreeManager.is_gh_available() is True

    def test_false_on_os_error(self):
        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=OSError("not found"),
        ):
            assert WorktreeManager.is_gh_available() is False

    def test_false_on_auth_failure(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert WorktreeManager.is_gh_available() is False


class TestGetDiffCaps:
    """get_diff must stay bounded.

    A worktree holding untracked eval dumps produced 287 MB of "diff" on a
    real repo. The Diff tab re-wraps whatever it is given to measure its
    height on every layout pass, so an unbounded diff is a UI freeze.
    """

    @staticmethod
    def _side_effect(untracked=b"", tracked=b""):
        def _run(*args, **kwargs):
            cmd = args[0]
            r = type("R", (), {"returncode": 0, "stdout": b""})()
            if cmd == ["git", "diff"]:
                r.stdout = tracked
            elif cmd == ["git", "ls-files", "--others", "--exclude-standard", "-z"]:
                r.stdout = untracked
            return r
        return _run

    def test_large_untracked_file_is_described_not_inlined(self):
        from lazyagent.worktree_manager import _MAX_UNTRACKED_FILE_BYTES

        with (
            patch(
                "lazyagent.worktree_manager.subprocess.run",
                side_effect=self._side_effect(untracked=b"huge.json\0"),
            ),
            patch(
                "pathlib.Path.stat",
                return_value=_FakeStat(30 * 1024 * 1024),
            ),
            patch("pathlib.Path.read_bytes") as read_bytes,
        ):
            result = WorktreeManager.get_diff("/wt")

        assert "huge.json" in result
        assert "30.0 MB" in result
        assert "too large to show" in result
        # The whole point: the file is never read.
        read_bytes.assert_not_called()
        assert len(result) < _MAX_UNTRACKED_FILE_BYTES

    def test_small_untracked_file_is_still_inlined(self):
        with (
            patch(
                "lazyagent.worktree_manager.subprocess.run",
                side_effect=self._side_effect(untracked=b"small.txt\0"),
            ),
            patch("pathlib.Path.stat", return_value=_FakeStat(11)),
            patch("pathlib.Path.read_bytes", return_value=b"new content"),
        ):
            result = WorktreeManager.get_diff("/wt")

        assert "small.txt" in result
        assert "new content" in result

    def test_total_output_is_capped(self):
        from lazyagent.worktree_manager import _MAX_DIFF_BYTES

        huge_tracked = b"+" + b"x" * (_MAX_DIFF_BYTES * 3)
        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=self._side_effect(tracked=huge_tracked),
        ):
            result = WorktreeManager.get_diff("/wt")

        assert len(result) <= _MAX_DIFF_BYTES + 200  # cap plus the notice
        assert "truncated" in result

    def test_many_small_files_stop_at_the_budget(self):
        from lazyagent.worktree_manager import _MAX_DIFF_BYTES

        names = b"\0".join(f"f{i}.txt".encode() for i in range(5000)) + b"\0"
        with (
            patch(
                "lazyagent.worktree_manager.subprocess.run",
                side_effect=self._side_effect(untracked=names),
            ),
            patch("pathlib.Path.stat", return_value=_FakeStat(1024)),
            patch("pathlib.Path.read_bytes", return_value=b"y" * 1024),
        ):
            result = WorktreeManager.get_diff("/wt")

        assert len(result) <= _MAX_DIFF_BYTES + 200

    def test_binary_untracked_file_still_flagged(self):
        with (
            patch(
                "lazyagent.worktree_manager.subprocess.run",
                side_effect=self._side_effect(untracked=b"blob.bin\0"),
            ),
            patch("pathlib.Path.stat", return_value=_FakeStat(64)),
            patch("pathlib.Path.read_bytes", return_value=b"ab\x00cd"),
        ):
            result = WorktreeManager.get_diff("/wt")

        assert "blob.bin" in result
        assert "Binary file" in result


# --- Batched PR lookup (gh api graphql) ---


def _check_run(name, status="COMPLETED", conclusion="SUCCESS", url=None):
    run = {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}
    if url is not None:
        run["detailsUrl"] = url
    return run


def _status_context(context, state="SUCCESS", url=None):
    ctx = {"__typename": "StatusContext", "context": context, "state": state}
    if url is not None:
        ctx["targetUrl"] = url
    return ctx


def _pr_node(number, state="OPEN", *, contexts=None, cross=False, rollup=True, **extra):
    commit = {"statusCheckRollup": {"contexts": {"nodes": contexts or []}} if rollup else None}
    node = {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "url": f"https://github.com/o/r/pull/{number}",
        "reviewDecision": None,
        "mergeable": "MERGEABLE",
        "isCrossRepository": cross,
        "commits": {"nodes": [{"commit": commit}]},
    }
    node.update(extra)
    return node


def _batch_response(*connections, default_branch="main"):
    repo = {"defaultBranchRef": {"name": default_branch}}
    for i, nodes in enumerate(connections):
        repo[f"pr{i}"] = {"nodes": nodes}
    return json.dumps({"data": {"repository": repo}})


class TestParsePrBatch:
    def test_open_pr_with_checks(self):
        raw = _batch_response([
            _pr_node(7, contexts=[
                _check_run("build"),
                _check_run("lint", status="IN_PROGRESS", conclusion=None),
                _status_context("ci/legacy", state="FAILURE"),
            ], reviewDecision="APPROVED"),
        ])
        result = WorktreeManager._parse_pr_batch(raw, [("/wt", "feat")])
        pr = result["/wt"]
        assert pr is not None
        assert (pr.number, pr.state, pr.review_decision, pr.mergeable) == (
            7, "OPEN", "APPROVED", "MERGEABLE",
        )
        assert pr.url == "https://github.com/o/r/pull/7"
        assert [(c.name, c.status, c.conclusion) for c in pr.checks] == [
            ("build", "COMPLETED", "SUCCESS"),
            ("lint", "IN_PROGRESS", ""),
            ("ci/legacy", "", "FAILURE"),
        ]

    def test_merged_and_closed_prs(self):
        raw = _batch_response([_pr_node(5, "MERGED")], [_pr_node(6, "CLOSED")])
        result = WorktreeManager._parse_pr_batch(raw, [("/a", "a"), ("/b", "b")])
        assert result["/a"].state == "MERGED"
        assert result["/b"].state == "CLOSED"

    def test_no_pr_for_branch(self):
        raw = _batch_response([])
        assert WorktreeManager._parse_pr_batch(raw, [("/wt", "feat")]) == {"/wt": None}

    def test_missing_status_check_rollup(self):
        raw = _batch_response(
            [_pr_node(8, rollup=False)],
            [_pr_node(9, commits={"nodes": []})],
        )
        result = WorktreeManager._parse_pr_batch(raw, [("/a", "a"), ("/b", "b")])
        assert result["/a"].checks == []
        assert result["/b"].checks == []

    def test_open_pr_wins_over_newer_closed_one(self):
        # Nodes come newest first; gh pr view still prefers the open one.
        raw = _batch_response([_pr_node(12, "CLOSED"), _pr_node(10, "OPEN")])
        result = WorktreeManager._parse_pr_batch(raw, [("/wt", "feat")])
        assert result["/wt"].number == 10

    def test_newest_wins_among_closed(self):
        raw = _batch_response([_pr_node(12, "MERGED"), _pr_node(10, "CLOSED")])
        result = WorktreeManager._parse_pr_batch(raw, [("/wt", "feat")])
        assert result["/wt"].number == 12

    def test_fork_prs_with_the_same_branch_name_are_ignored(self):
        raw = _batch_response([_pr_node(30, cross=True), _pr_node(20, "MERGED")])
        result = WorktreeManager._parse_pr_batch(raw, [("/wt", "fix-typo")])
        assert result["/wt"].number == 20

    def test_default_branch_only_matches_an_open_pr(self):
        raw = _batch_response([_pr_node(3, "MERGED")], default_branch="main")
        assert WorktreeManager._parse_pr_batch(raw, [("/wt", "main")]) == {"/wt": None}

    def test_response_without_repository_raises(self):
        raw = json.dumps({"data": {"repository": None}, "errors": [{"message": "nope"}]})
        with pytest.raises(TypeError):
            WorktreeManager._parse_pr_batch(raw, [("/wt", "feat")])

    def test_response_missing_an_alias_raises(self):
        raw = _batch_response([])
        with pytest.raises(TypeError):
            WorktreeManager._parse_pr_batch(raw, [("/a", "a"), ("/b", "b")])

    def test_matches_gh_pr_view_parse_for_the_same_data(self):
        """Batched and single-branch lookups must yield identical PrInfo."""
        node = _pr_node(
            42,
            contexts=[
                _check_run("build", url="https://x"),
                _check_run("e2e", status="QUEUED", conclusion=None, url="https://y"),
                _status_context("ci/jenkins", state="PENDING", url="https://z"),
            ],
            reviewDecision="CHANGES_REQUESTED",
            mergeable="CONFLICTING",
        )
        # What `gh pr view --json ...` exports for the same pull request.
        view = json.dumps({
            "number": 42,
            "title": "PR 42",
            "state": "OPEN",
            "url": "https://github.com/o/r/pull/42",
            "reviewDecision": "CHANGES_REQUESTED",
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "build", "workflowName": "CI",
                 "status": "COMPLETED", "conclusion": "SUCCESS",
                 "startedAt": "2026-10-01T00:00:00Z", "completedAt": "2026-10-01T00:01:00Z",
                 "detailsUrl": "https://x"},
                {"__typename": "CheckRun", "name": "e2e", "workflowName": "CI",
                 "status": "QUEUED", "conclusion": "",
                 "startedAt": "0001-01-01T00:00:00Z", "completedAt": "0001-01-01T00:00:00Z",
                 "detailsUrl": "https://y"},
                {"__typename": "StatusContext", "context": "ci/jenkins", "state": "PENDING",
                 "targetUrl": "https://z", "startedAt": "2026-10-01T00:00:00Z"},
            ],
        })
        batched = WorktreeManager._parse_pr_batch(_batch_response([node]), [("/wt", "feat")])
        assert batched["/wt"] == WorktreeManager._parse_pr_info(view)

    def test_query_has_one_alias_per_branch_and_no_inlined_names(self):
        query = WorktreeManager._pr_batch_query(3)
        assert [f"pr{i}: pullRequests(headRefName: $h{i}," in query for i in range(3)] == [True] * 3
        assert "pr3:" not in query
        assert "states: [OPEN, CLOSED, MERGED]" in query


def _wt(path, branch, *, is_bare=False):
    return WorktreeInfo(path=path, head="0" * 40, branch=branch, is_main=False, is_bare=is_bare)


def _manager(tmp_path) -> WorktreeManager:
    (tmp_path / ".git").mkdir()
    return WorktreeManager(tmp_path)


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


REPO = GithubRepo(owner="acme", name="widgets")


class TestPrHeadRefs:
    def _heads(self, tmp_path, config, worktrees):
        manager = _manager(tmp_path)
        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            return_value=_completed(config, returncode=0 if config else 1),
        ):
            return manager.pr_head_refs(worktrees, REPO)

    def test_untracked_branch_uses_its_own_name(self, tmp_path):
        assert self._heads(tmp_path, "", [_wt("/a", "feat")]) == {"/a": "feat"}

    def test_same_named_upstream_on_the_repo_owner(self, tmp_path):
        config = (
            "remote.origin.url git@github.com:Acme/widgets.git\n"
            "branch.feat.remote origin\n"
            "branch.feat.merge refs/heads/feat\n"
        )
        assert self._heads(tmp_path, config, [_wt("/a", "feat")]) == {"/a": "feat"}

    def test_branch_names_with_dots(self, tmp_path):
        config = (
            "remote.origin.url https://github.com/acme/widgets\n"
            "branch.release.1.2.remote origin\n"
            "branch.release.1.2.merge refs/heads/release.1.2\n"
        )
        assert self._heads(tmp_path, config, [_wt("/a", "release.1.2")]) == {"/a": "release.1.2"}

    @pytest.mark.parametrize("config", [
        # tracks a differently named upstream branch
        (
            "remote.origin.url git@github.com:acme/widgets.git\n"
            "branch.feat.remote origin\nbranch.feat.merge refs/heads/other\n"
        ),
        # pushed to a fork
        (
            "remote.fork.url git@github.com:someone/widgets.git\n"
            "branch.feat.remote fork\nbranch.feat.merge refs/heads/feat\n"
        ),
        # checked out from a PR ref
        "branch.feat.remote origin\nbranch.feat.merge refs/pull/12/head\n",
        # explicit push remote
        "branch.feat.pushremote fork\n",
    ])
    def test_unusual_tracking_is_left_to_gh_pr_view(self, tmp_path, config):
        assert self._heads(tmp_path, config, [_wt("/a", "feat")]) == {}

    def test_push_default_remote_disables_batching(self, tmp_path):
        assert self._heads(tmp_path, "remote.pushdefault fork\n", [_wt("/a", "feat")]) == {}

    def test_detached_and_bare_worktrees_are_left_out(self, tmp_path):
        worktrees = [_wt("/a", None), _wt("/b", None, is_bare=True), _wt("/c", "feat")]
        assert self._heads(tmp_path, "", worktrees) == {"/c": "feat"}


class TestGetPrInfos:
    def test_one_request_for_all_branches(self, tmp_path):
        manager = _manager(tmp_path)
        response = _batch_response([_pr_node(1)], [], [_pr_node(3, "MERGED")])
        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=[_completed(returncode=1), _completed(response)],
        ) as run:
            result = manager.get_pr_infos(
                [_wt("/a", "a"), _wt("/b", "b"), _wt("/c", "c")], REPO,
            )
        assert {p: (i.number if i else None) for p, i in result.items()} == {
            "/a": 1, "/b": None, "/c": 3,
        }
        graphql = run.call_args_list[1].args[0]
        assert graphql[:3] == ["gh", "api", "graphql"]
        assert "owner=acme" in graphql and "name=widgets" in graphql
        assert ["h0=a", "h1=b", "h2=c"] == [a for a in graphql if a.startswith("h")]

    def test_failed_request_raises(self, tmp_path):
        manager = _manager(tmp_path)
        with (
            patch(
                "lazyagent.worktree_manager.subprocess.run",
                side_effect=[_completed(returncode=1), _completed(returncode=1, stderr="HTTP 502")],
            ),
            pytest.raises(WorktreeManagerError, match="HTTP 502"),
        ):
            manager.get_pr_infos([_wt("/a", "a")], REPO)

    def test_large_sets_are_split(self, tmp_path, monkeypatch):
        monkeypatch.setattr("lazyagent.worktree_manager._PR_BATCH_SIZE", 2)
        manager = _manager(tmp_path)
        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=[
                _completed(returncode=1),
                _completed(_batch_response([], [])),
                _completed(_batch_response([])),
            ],
        ) as run:
            result = manager.get_pr_infos([_wt(f"/{c}", c) for c in "abc"], REPO)
        assert result == {"/a": None, "/b": None, "/c": None}
        assert run.call_count == 3


class TestGetGithubRepo:
    def test_parses_owner_and_name(self, tmp_path):
        manager = _manager(tmp_path)
        out = json.dumps({"owner": {"id": "x", "login": "acme"}, "name": "widgets"})
        with patch("lazyagent.worktree_manager.subprocess.run", return_value=_completed(out)):
            assert manager.get_github_repo() == REPO

    def test_none_on_failure(self, tmp_path):
        manager = _manager(tmp_path)
        with patch("lazyagent.worktree_manager.subprocess.run", return_value=_completed(returncode=1)):
            assert manager.get_github_repo() is None
