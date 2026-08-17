from __future__ import annotations

import json
import subprocess
from pathlib import Path

from lazyagent.models import CiCheck, GitStatus, PrInfo, WorktreeInfo


# Caps on what get_diff() will produce. The Diff tab re-wraps its whole
# content to measure height on every layout pass — measured at roughly 1 ms
# per KB of real diff — so an unbounded diff is a UI freeze rather than merely
# a large string. 64 KB is ~800 lines of diff, more than anyone reads in a
# tab, and keeps that measurement near 50 ms.
_MAX_DIFF_BYTES = 64 * 1024
_MAX_UNTRACKED_FILE_BYTES = 32 * 1024
_DIFF_TRUNCATED = "… diff truncated (too large to display)"


def _format_size(num_bytes: int) -> str:
    """Human-readable byte count for files we decline to inline."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


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

    @staticmethod
    def get_diff(worktree_path: str | Path) -> str:
        """Get diff showing all working tree changes including untracked files.

        Uses ``git diff`` for tracked changes. Untracked files are inlined,
        but only up to :data:`_MAX_UNTRACKED_FILE_BYTES` each and
        :data:`_MAX_DIFF_BYTES` overall.

        The caps are not cosmetic. A worktree holding a few 30 MB eval dumps
        produced 287 MB of "diff" here, and the Diff tab re-measures whatever
        it is handed on every layout pass — which is what made opening an
        agent take over a second. Nobody reads a megabyte of diff in a tab
        either, so the cap costs nothing real.
        """
        cwd = str(worktree_path)
        parts: list[str] = []
        budget = _MAX_DIFF_BYTES

        def add(entry: str) -> bool:
            """Append an entry within budget. Returns False once full."""
            nonlocal budget
            if budget <= 0:
                return False
            cost = len(entry) + 2  # entries are joined with "\n\n"
            if cost > budget:
                parts.append(entry[: max(budget - 2, 0)])
                parts.append(_DIFF_TRUNCATED)
                budget = 0
                return False
            parts.append(entry)
            budget -= cost
            return True

        try:
            # Tracked changes (staged + unstaged)
            result = subprocess.run(
                ["git", "diff"],
                capture_output=True,
                cwd=cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                add(result.stdout.decode("utf-8", errors="replace").strip())

            # Staged binary files — git diff skips these, show a marker
            result = subprocess.run(
                ["git", "diff", "--cached", "--numstat", "-z"],
                capture_output=True,
                cwd=cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                for entry in result.stdout.decode("utf-8", errors="replace").split("\0"):
                    if entry.startswith("-\t-\t"):
                        f = entry[len("-\t-\t"):]
                        add(f"diff --git a/{f} b/{f}\nstaged\nBinary file")

            # Untracked files — show contents or binary marker
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                capture_output=True,
                cwd=cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                files = [f for f in result.stdout.decode("utf-8", errors="replace").split("\0") if f]
                for f in files:
                    if budget <= 0:
                        break
                    filepath = Path(cwd) / f
                    header = f"diff --git a/{f} b/{f}\nnew file"
                    try:
                        # Size first: the old code sniffed 8 KB for binary and
                        # then read the *whole* file, so a 30 MB dump was
                        # inlined in full.
                        size = filepath.stat().st_size
                        if size > _MAX_UNTRACKED_FILE_BYTES:
                            add(f"{header}\n{_format_size(size)} — too large to show")
                            continue
                        chunk = filepath.read_bytes()
                    except OSError:
                        continue
                    if b"\x00" in chunk[:8000] or chunk.startswith(b"%PDF"):
                        add(f"{header}\nBinary file")
                    else:
                        add(f"{header}\n{chunk.decode('utf-8', errors='replace')}")

            return "\n\n".join(parts)
        except OSError:
            return ""

    @staticmethod
    def _parse_pr_info(raw: str) -> PrInfo | None:
        """Parse JSON output from ``gh pr view``."""
        if not raw or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

        checks: list[CiCheck] = []
        for item in data.get("statusCheckRollup", []) or []:
            name = item.get("name") or item.get("context", "")
            status = item.get("status", "")
            conclusion = item.get("conclusion") or item.get("state", "") or ""
            checks.append(CiCheck(name=name, status=status, conclusion=conclusion))

        return PrInfo(
            number=data.get("number", 0),
            title=data.get("title", ""),
            state=data.get("state", ""),
            checks=checks,
            url=data.get("url", ""),
            review_decision=data.get("reviewDecision") or "",
            mergeable=data.get("mergeable") or "",
        )

    @staticmethod
    def get_pr_info(worktree_path: str | Path) -> PrInfo | None:
        """Get PR info for a worktree via ``gh pr view``."""
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "view",
                    "--json", "number,title,state,statusCheckRollup,url,reviewDecision,mergeable",
                ],
                capture_output=True,
                text=True,
                cwd=str(worktree_path),
                timeout=10,
            )
            if result.returncode != 0:
                return None
            return WorktreeManager._parse_pr_info(result.stdout)
        except (subprocess.TimeoutExpired, OSError):
            return None

    @staticmethod
    def is_gh_available() -> bool:
        """Check if ``gh`` CLI is installed and authenticated."""
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


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
