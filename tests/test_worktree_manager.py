from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from lazyagent.models import GitStatus
from lazyagent.worktree_manager import WorktreeManager, WorktreeManagerError


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


class TestGetDiff:
    def test_subprocess_args(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            mock_run.return_value.returncode = 0
            WorktreeManager.get_diff("/path/to/worktree")

        assert mock_run.call_count == 2
        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["git", "diff", "HEAD"]
        assert calls[0][1]["cwd"] == "/path/to/worktree"
        assert calls[1][0][0] == ["git", "ls-files", "--others", "--exclude-standard"]
        assert calls[1][1]["cwd"] == "/path/to/worktree"

    def test_tracked_changes(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            diff_result = type("R", (), {"returncode": 0, "stdout": "diff --git a/f\n"})()
            ls_result = type("R", (), {"returncode": 0, "stdout": ""})()
            mock_run.side_effect = [diff_result, ls_result]
            result = WorktreeManager.get_diff("/path/to/worktree")

        assert result == "diff --git a/f"

    def test_untracked_files_shows_diff(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0]
            r = type("R", (), {"returncode": 0, "stdout": ""})()
            if cmd == ["git", "diff", "HEAD"]:
                r.stdout = ""
            elif cmd == ["git", "ls-files", "--others", "--exclude-standard"]:
                r.stdout = "new.txt\n"
            elif cmd[0:3] == ["git", "diff", "--no-index"]:
                r.returncode = 1
                r.stdout = "diff --git a/dev/null b/new.txt\n+new content\n"
            return r

        with patch("lazyagent.worktree_manager.subprocess.run", side_effect=_side_effect):
            result = WorktreeManager.get_diff("/path/to/worktree")

        assert "new.txt" in result
        assert "+new content" in result

    def test_tracked_and_untracked_combined(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0]
            r = type("R", (), {"returncode": 0, "stdout": ""})()
            if cmd == ["git", "diff", "HEAD"]:
                r.stdout = "tracked diff\n"
            elif cmd == ["git", "ls-files", "--others", "--exclude-standard"]:
                r.stdout = "new.txt\n"
            elif cmd[0:3] == ["git", "diff", "--no-index"]:
                r.returncode = 1
                r.stdout = "untracked diff\n"
            return r

        with patch("lazyagent.worktree_manager.subprocess.run", side_effect=_side_effect):
            result = WorktreeManager.get_diff("/path/to/worktree")

        assert "tracked diff" in result
        assert "untracked diff" in result

    def test_empty_on_failure(self):
        with patch(
            "lazyagent.worktree_manager.subprocess.run",
            side_effect=OSError("not a repo"),
        ):
            result = WorktreeManager.get_diff("/bad/path")

        assert result == ""

    def test_empty_when_clean(self):
        with patch("lazyagent.worktree_manager.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
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
