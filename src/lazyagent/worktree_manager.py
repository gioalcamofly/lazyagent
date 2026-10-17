from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from lazyagent import diagnostics as diag
from lazyagent.models import CiCheck, GitStatus, PrInfo, WorktreeInfo

log = logging.getLogger(__name__)


def _run(cmd, **kwargs):
    """``subprocess.run`` with a diagnostic trace of what ran and for how long.

    Every git/gh call in this module goes through here, so the log answers
    "which subprocess, in which worktree, how long" without any call site
    having to care.
    """
    if not diag.ENABLED:
        return subprocess.run(cmd, **kwargs)
    start = time.perf_counter()
    outcome = "ok"
    try:
        result = subprocess.run(cmd, **kwargs)
        return result
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        diag.event(
            log,
            "subprocess",
            cmd=" ".join(str(c) for c in cmd),
            cwd=str(kwargs.get("cwd") or ""),
            outcome=outcome,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )


# Caps on what get_diff() will produce. The Diff tab re-wraps its whole
# content to measure height on every layout pass — measured at roughly 1 ms
# per KB of real diff — so an unbounded diff is a UI freeze rather than merely
# a large string. 64 KB is ~800 lines of diff, more than anyone reads in a
# tab, and keeps that measurement near 50 ms.
_MAX_DIFF_BYTES = 64 * 1024
_MAX_UNTRACKED_FILE_BYTES = 32 * 1024
_DIFF_TRUNCATED = "… diff truncated (too large to display)"


# Branches per ``gh api graphql`` request. Each one asks for up to
# _PR_NODES_PER_BRANCH pull requests with their checks, and GitHub caps the
# nodes a single query may touch, so a large interest set is split up.
_PR_BATCH_SIZE = 25
# What ``gh pr view`` itself fetches for a branch before choosing one PR. A
# smaller page could miss the open PR behind newer closed ones, or one from
# this repo behind PRs from forks that use the same branch name.
_PR_NODES_PER_BRANCH = 30

_PR_FIELDS = """
fragment pr on PullRequest {
  number title state url reviewDecision mergeable isCrossRepository
  commits(last: 1) {
    nodes {
      commit {
        statusCheckRollup {
          contexts(first: 100) {
            nodes {
              __typename
              ... on CheckRun { name status conclusion detailsUrl }
              ... on StatusContext { context state targetUrl }
            }
          }
        }
      }
    }
  }
}
"""

_REMOTE_OWNER_RE = re.compile(r"[:/]([^/:]+)/[^/]+?/?$")


@dataclass(frozen=True)
class GithubRepo:
    """The repository ``gh`` resolves pull requests against."""

    owner: str
    name: str


def _remote_owner(url: str) -> str | None:
    """Owner part of a GitHub remote URL (ssh, scp-like or https)."""
    match = _REMOTE_OWNER_RE.search(url.strip())
    return match.group(1) if match else None


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
            _run(
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
            _run(
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
        result = _run(
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
            result = _run(
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
            result = _run(
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
            result = _run(
                ["git", "diff"],
                capture_output=True,
                cwd=cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                add(result.stdout.decode("utf-8", errors="replace").strip())

            # Staged binary files — git diff skips these, show a marker
            result = _run(
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
            result = _run(
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
        return WorktreeManager._pr_info_from_view(data)

    @staticmethod
    def _pr_info_from_view(data: dict) -> PrInfo:
        """Build a :class:`PrInfo` from ``gh pr view --json`` shaped data."""
        checks: list[CiCheck] = []
        for item in data.get("statusCheckRollup", []) or []:
            name = item.get("name") or item.get("context", "")
            status = item.get("status", "")
            conclusion = item.get("conclusion") or item.get("state", "") or ""
            details_url = item.get("detailsUrl") or item.get("targetUrl") or ""
            checks.append(
                CiCheck(
                    name=name, status=status, conclusion=conclusion, details_url=details_url
                )
            )

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
    def _graphql_pr_to_view(node: dict) -> dict:
        """Reshape a GraphQL ``PullRequest`` node the way ``gh pr view --json``
        exports it, so both paths share one parser."""
        commits = (node.get("commits") or {}).get("nodes") or []
        rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") if commits else None
        contexts = ((rollup or {}).get("contexts") or {}).get("nodes") or []
        checks = []
        for ctx in contexts:
            if ctx.get("__typename") == "CheckRun":
                checks.append({
                    "name": ctx.get("name") or "",
                    "status": ctx.get("status") or "",
                    "conclusion": ctx.get("conclusion") or "",
                    "detailsUrl": ctx.get("detailsUrl") or "",
                })
            else:
                checks.append({
                    "context": ctx.get("context") or "",
                    "state": ctx.get("state") or "",
                    "targetUrl": ctx.get("targetUrl") or "",
                })
        return {
            "number": node.get("number", 0),
            "title": node.get("title", ""),
            "state": node.get("state", ""),
            "statusCheckRollup": checks,
            "url": node.get("url", ""),
            "reviewDecision": node.get("reviewDecision") or "",
            "mergeable": node.get("mergeable") or "",
        }

    @staticmethod
    def _select_pr(nodes: list[dict], default_branch: str, head: str) -> dict | None:
        """Pick the PR ``gh pr view`` would show for a same-repo branch.

        Open PRs win over closed and merged ones, newest first otherwise;
        PRs from forks that happen to use the same branch name never match;
        and on the default branch only an open PR counts.
        """
        ordered = sorted(nodes, key=lambda n: n.get("state") != "OPEN")
        for node in ordered:
            if node.get("isCrossRepository"):
                continue
            if head == default_branch and node.get("state") != "OPEN":
                continue
            return node
        return None

    @staticmethod
    def _parse_pr_batch(raw: str, heads: list[tuple[str, str]]) -> dict[str, PrInfo | None]:
        """Parse a batched ``gh api graphql`` response.

        ``heads`` lists ``(worktree_path, head_ref)`` in alias order: alias
        ``pr<i>`` answers ``heads[i]``. Raises ``TypeError`` when the
        response does not cover every alias.
        """
        data = json.loads(raw)
        repo = (data.get("data") or {}).get("repository")
        if not isinstance(repo, dict):
            raise TypeError(f"no repository in response: {data.get('errors')}")
        default_branch = (repo.get("defaultBranchRef") or {}).get("name") or ""
        result: dict[str, PrInfo | None] = {}
        for i, (path, head) in enumerate(heads):
            conn = repo.get(f"pr{i}")
            if not isinstance(conn, dict):
                raise TypeError(f"missing pr{i} in response")
            node = WorktreeManager._select_pr(conn.get("nodes") or [], default_branch, head)
            result[path] = (
                None if node is None
                else WorktreeManager._pr_info_from_view(WorktreeManager._graphql_pr_to_view(node))
            )
        return result

    @staticmethod
    def _pr_batch_query(count: int) -> str:
        variables = ", ".join(f"$h{i}: String!" for i in range(count))
        aliases = "\n".join(
            f"    pr{i}: pullRequests(headRefName: $h{i}, "
            f"states: [OPEN, CLOSED, MERGED], first: {_PR_NODES_PER_BRANCH}, "
            "orderBy: {field: CREATED_AT, direction: DESC}) { nodes { ...pr } }"
            for i in range(count)
        )
        return (
            f"query($owner: String!, $name: String!, {variables}) {{\n"
            "  repository(owner: $owner, name: $name) {\n"
            "    defaultBranchRef { name }\n"
            f"{aliases}\n"
            "  }\n"
            "}\n"
            f"{_PR_FIELDS}"
        )

    def get_github_repo(self) -> GithubRepo | None:
        """The repository ``gh pr view`` resolves against, via ``gh repo view``."""
        try:
            result = _run(
                ["gh", "repo", "view", "--json", "owner,name"],
                capture_output=True,
                text=True,
                cwd=self.repo_path,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            return GithubRepo(owner=data["owner"]["login"], name=data["name"])
        except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError):
            return None

    def pr_head_refs(
        self, worktrees: list[WorktreeInfo], repo: GithubRepo,
    ) -> dict[str, str]:
        """Head ref to look up for each worktree a batched query can answer.

        ``gh pr view`` maps the current branch to a PR head through the
        branch's tracking config. Only the plain case is reproduced here: a
        branch that tracks nothing, or a same-named branch on a remote owned
        by ``repo``'s owner, with no push remote configured. Worktrees left
        out (detached, forks, renamed upstreams, ``refs/pull/*`` merges) are
        for the caller to look up one by one with :meth:`get_pr_info`.
        """
        try:
            result = _run(
                ["git", "config", "--get-regexp", r"^(branch|remote)\."],
                capture_output=True,
                text=True,
                cwd=self.repo_path,
            )
        except OSError:
            return {}
        # Exit 1 just means no key matched.
        if result.returncode not in (0, 1):
            return {}
        branch_cfg: dict[str, dict[str, str]] = {}
        remote_urls: dict[str, str] = {}
        push_default = False
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            section, _, rest = key.partition(".")
            sub, _, var = rest.rpartition(".")
            if section == "remote" and not sub and var == "pushdefault":
                push_default = True
            elif section == "remote" and var == "url":
                remote_urls.setdefault(sub, value)
            elif section == "branch" and sub:
                branch_cfg.setdefault(sub, {})[var] = value
        if push_default:
            return {}

        heads: dict[str, str] = {}
        for wt in worktrees:
            if not wt.branch or wt.is_bare:
                continue
            cfg = branch_cfg.get(wt.branch, {})
            if "pushremote" in cfg:
                continue
            merge = cfg.get("merge")
            if merge is not None and merge != f"refs/heads/{wt.branch}":
                continue
            remote = cfg.get("remote")
            if remote is not None:
                owner = _remote_owner(remote_urls.get(remote, remote))
                if owner is None or owner.lower() != repo.owner.lower():
                    continue
            heads[wt.path] = wt.branch
        return heads

    def get_pr_infos(
        self, worktrees: list[WorktreeInfo], repo: GithubRepo,
    ) -> dict[str, PrInfo | None]:
        """PR info for many worktrees in one ``gh api graphql`` call per
        :data:`_PR_BATCH_SIZE` branches.

        Gives the PR ``gh pr view`` would show in each worktree, whoever
        opened it. Worktrees missing from the result need :meth:`get_pr_info`
        (see :meth:`pr_head_refs`). Raises :class:`WorktreeManagerError` if a
        request fails.
        """
        heads = list(self.pr_head_refs(worktrees, repo).items())
        result: dict[str, PrInfo | None] = {}
        for start in range(0, len(heads), _PR_BATCH_SIZE):
            chunk = heads[start:start + _PR_BATCH_SIZE]
            cmd = [
                "gh", "api", "graphql",
                "-f", f"query={self._pr_batch_query(len(chunk))}",
                "-f", f"owner={repo.owner}",
                "-f", f"name={repo.name}",
            ]
            for i, (_, head) in enumerate(chunk):
                cmd += ["-f", f"h{i}={head}"]
            try:
                proc = _run(
                    cmd, capture_output=True, text=True, cwd=self.repo_path, timeout=20,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                raise WorktreeManagerError(f"PR batch query failed: {e}") from e
            if proc.returncode != 0:
                raise WorktreeManagerError(
                    f"PR batch query failed: {proc.stderr.strip() or proc.returncode}"
                )
            try:
                result.update(self._parse_pr_batch(proc.stdout, chunk))
            except (ValueError, AttributeError, TypeError) as e:
                raise WorktreeManagerError(f"PR batch query: bad response: {e}") from e
        return result

    @staticmethod
    def get_pr_info(worktree_path: str | Path) -> PrInfo | None:
        """Get PR info for a worktree via ``gh pr view``."""
        try:
            result = _run(
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
            result = _run(
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
        result = _run(
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
