from __future__ import annotations

import subprocess
from pathlib import Path

from lazyagent.models import GitStatus, WorktreeInfo


class WorktreeManagerError(Exception):
    """Raised when worktree operations fail."""


class WorktreeManager:
    """Manages git worktrees for a repository."""

    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path).resolve()
        if not (self.repo_path / ".git").exists():
            raise WorktreeManagerError(
                f"Not a git repository: {self.repo_path}"
            )

    def create(self, branch: str, base_branch: str = "master") -> str:
        """Create a new worktree with a new branch.

        Returns the path to the new worktree.
        """
        repo_name = self.repo_path.name
        worktree_path = self.repo_path.parent / f"{repo_name}-{branch}"
        try:
            subprocess.run(
                [
                    "git", "worktree", "add",
                    "-b", branch,
                    str(worktree_path),
                    base_branch,
                ],
                capture_output=True,
                text=True,
                cwd=self.repo_path,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise WorktreeManagerError(
                f"Failed to create worktree: {e.stderr.strip()}"
            ) from e
        return str(worktree_path)

    def remove(self, worktree_path: str | Path, force: bool = False) -> None:
        """Remove a worktree."""
        cmd = ["git", "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(str(worktree_path))
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.repo_path,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise WorktreeManagerError(
                f"Failed to remove worktree: {e.stderr.strip()}"
            ) from e

    def list(self) -> list[WorktreeInfo]:
        """List all worktrees by running `git worktree list --porcelain`."""
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=self.repo_path,
            check=True,
        )
        return self._parse_porcelain(result.stdout)

    @staticmethod
    def _parse_porcelain(raw: str) -> list[WorktreeInfo]:
        """Parse the porcelain output of `git worktree list`.

        The first block is always the main worktree.
        """
        worktrees: list[WorktreeInfo] = []
        if not raw.strip():
            return worktrees

        blocks = raw.strip().split("\n\n")
        for i, block in enumerate(blocks):
            path = ""
            head = ""
            branch: str | None = None
            is_bare = False

            for line in block.strip().splitlines():
                if line.startswith("worktree "):
                    path = line[len("worktree "):]
                elif line.startswith("HEAD "):
                    head = line[len("HEAD "):]
                elif line.startswith("branch "):
                    # Strip refs/heads/ prefix
                    ref = line[len("branch "):]
                    if ref.startswith("refs/heads/"):
                        branch = ref[len("refs/heads/"):]
                    else:
                        branch = ref
                elif line == "bare":
                    is_bare = True
                # "detached" lines are ignored — branch stays None

            if path:
                worktrees.append(
                    WorktreeInfo(
                        path=path,
                        head=head,
                        branch=branch,
                        is_main=(i == 0),
                        is_bare=is_bare,
                    )
                )

        return worktrees

    @staticmethod
    def _parse_git_status(raw: str) -> GitStatus:
        """Parse output of ``git status --porcelain=v1 --branch``."""
        status = GitStatus()
        lines = raw.splitlines()
        if not lines:
            return status

        header = lines[0]
        if header.startswith("## "):
            branch_part = header[3:]
            if "..." in branch_part:
                status.has_upstream = True
                bracket = branch_part.find("[")
                if bracket != -1:
                    info = branch_part[bracket + 1 : branch_part.find("]")]
                    for part in info.split(","):
                        part = part.strip()
                        if part.startswith("ahead "):
                            status.ahead = int(part.split()[1])
                        elif part.startswith("behind "):
                            status.behind = int(part.split()[1])

        for line in lines[1:]:
            if len(line) >= 2:
                status.dirty_count += 1

        return status

    def get_git_status(self, worktree_path: str | Path) -> GitStatus:
        """Get git status for a worktree directory."""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain=v1", "--branch"],
                capture_output=True,
                text=True,
                cwd=str(worktree_path),
                check=True,
            )
            return self._parse_git_status(result.stdout)
        except (subprocess.CalledProcessError, OSError):
            return GitStatus()

    def get_last_commit_subject(self, worktree_path: str | Path) -> str:
        """Get the subject line of the last commit."""
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%s"],
                capture_output=True,
                text=True,
                cwd=str(worktree_path),
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            return ""

    def get_all_git_statuses(
        self, worktrees: list[WorktreeInfo],
    ) -> dict[str, GitStatus]:
        """Fetch git status for all worktrees."""
        statuses: dict[str, GitStatus] = {}
        for wt in worktrees:
            if wt.is_bare:
                statuses[wt.path] = GitStatus()
                continue
            status = self.get_git_status(wt.path)
            status.last_commit_subject = self.get_last_commit_subject(wt.path)
            statuses[wt.path] = status
        return statuses


def find_repo_root(start_path: str | Path | None = None) -> Path:
    """Find the git repository root by walking up from start_path.

    Uses `git rev-parse --show-toplevel`.
    """
    cwd = str(Path(start_path).resolve()) if start_path else None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=True,
        )
        return Path(result.stdout.strip())
    except subprocess.CalledProcessError:
        raise WorktreeManagerError(
            f"Not inside a git repository: {cwd or Path.cwd()}"
        )
