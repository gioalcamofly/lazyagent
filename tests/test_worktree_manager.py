from __future__ import annotations

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
