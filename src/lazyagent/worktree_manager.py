from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

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
# What a caller may ask for instead (the web viewer is virtualised).
MAX_DIFF_BYTES_LIMIT = 4 * 1024 * 1024
_MAX_UNTRACKED_FILE_BYTES = 32 * 1024
_DIFF_TRUNCATED = "… diff truncated (too large to display)"
# `git fetch origin <base>` from the Diff view's refresh.
_FETCH_TIMEOUT = 60


def _budget(max_bytes: int | None) -> int:
    if max_bytes is None:
        return _MAX_DIFF_BYTES
    return max(1, min(max_bytes, MAX_DIFF_BYTES_LIMIT))


class _Capped:
    """Diff entries joined by blank lines, cut off once a byte budget is spent."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.parts: list[str] = []

    @property
    def full(self) -> bool:
        return self.budget <= 0

    def add(self, entry: str) -> bool:
        """Append an entry within budget. Returns False once full."""
        if self.budget <= 0:
            return False
        cost = len(entry) + 2  # entries are joined with "\n\n"
        if cost > self.budget:
            self.parts.append(entry[: max(self.budget - 2, 0)])
            self.parts.append(_DIFF_TRUNCATED)
            self.budget = 0
            return False
        self.parts.append(entry)
        self.budget -= cost
        return True

    def text(self) -> str:
        return "\n\n".join(self.parts)


def _clip(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` bytes, with the truncation notice."""
    if len(text) <= limit:
        return text
    out = _Capped(limit)
    out.add(text)
    return out.text()


@dataclass(frozen=True)
class PrDiff:
    """What :meth:`WorktreeManager.get_pr_diff` returns."""

    committed: str
    uncommitted: str
    merge_base: str


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
  number title state url reviewDecision mergeable isCrossRepository baseRefName
  headRefOid reviewRequests { totalCount } reviews { totalCount } isDraft author { login }
  commits(last: 1) {
    nodes {
      commit {
        statusCheckRollup {
          contexts(first: 100) {
            nodes {
              __typename
              ... on CheckRun {
                name status conclusion detailsUrl
                checkSuite { workflowRun { workflow { name } } }
              }
              ... on StatusContext { context state targetUrl }
            }
          }
        }
      }
    }
  }
}
"""

# The review facts (pr.labels, pr.human_approvals, pr.commits_after_ready):
# a second query on the chosen open PRs, by number, made only while a rule
# reads them. In the query above every connection is paid for the
# _PR_NODES_PER_BRANCH candidates of each branch: these three there took a
# 25-branch batch from 15 to 38 rate-limit points; here they cost 1.
_REVIEW_FIELDS = """
fragment review on PullRequest {
  createdAt
  labels(first: 50) { pageInfo { hasNextPage } nodes { name } }
  reviews(last: 100) {
    totalCount pageInfo { hasPreviousPage }
    nodes { state author { __typename login } }
  }
  timelineItems(last: 100, itemTypes: [READY_FOR_REVIEW_EVENT, PULL_REQUEST_COMMIT,
                                       HEAD_REF_FORCE_PUSHED_EVENT]) {
    pageInfo { hasPreviousPage }
    nodes {
      __typename
      ... on ReadyForReviewEvent { createdAt }
      ... on HeadRefForcePushedEvent { createdAt beforeCommit { oid } afterCommit { oid } }
      ... on PullRequestCommit { commit { committedDate oid parents { totalCount } } }
    }
  }
}
"""

def _count(value: object) -> int | None:
    """A GraphQL ``{totalCount}`` or a ``gh --json`` list, as a count; None
    when the field was not fetched."""
    if isinstance(value, dict) and isinstance(value.get("totalCount"), int):
        return value["totalCount"]
    if isinstance(value, list):
        return len(value)
    return None


_RUN_ID = re.compile(r"/actions/runs/(\d+)")


def _latest_runs(items: list[dict]) -> list[dict]:
    """A head's checks with one run per workflow and job: the run created
    last (the highest run id). The rollup lists every run's check runs, so
    a re-run of an old run (which replays its old event, its old labels)
    would otherwise keep a red beside a newer green run. A check with no
    workflow name or run id (a commit status, another app) is kept."""
    best: dict[tuple[str, str], int] = {}
    for item in items:
        m = _RUN_ID.search(item.get("detailsUrl") or "")
        if m and item.get("workflowName") and item.get("name"):
            key = (item["workflowName"], item["name"])
            best[key] = max(best.get(key, 0), int(m[1]))
    out = []
    for item in items:
        m = _RUN_ID.search(item.get("detailsUrl") or "")
        key = (item.get("workflowName") or "", item.get("name") or "")
        if m and key in best and int(m[1]) != best[key]:
            continue
        out.append(item)
    return out


def _labels(conn: object) -> list[str] | None:
    """A GraphQL ``labels`` connection or a ``gh --json labels`` list, as
    names; None when not fetched or cut short."""
    if isinstance(conn, list):
        return [n["name"] for n in conn if isinstance(n, dict) and isinstance(n.get("name"), str)]
    if not isinstance(conn, dict) or (conn.get("pageInfo") or {}).get("hasNextPage"):
        return None
    nodes = conn.get("nodes")
    return _labels(nodes) if isinstance(nodes, list) else None


def is_bot_login(author: object) -> bool:
    """A GraphQL actor that is an app: type Bot, or a ``[bot]`` login (REST's
    spelling)."""
    if not isinstance(author, dict):
        return False
    login = author.get("login") or ""
    return author.get("__typename") == "Bot" or login.lower().endswith("[bot]")


def _approved_by(conn: object, pr_author: str) -> list[str] | None:
    """The logins whose latest review that is not a comment is an approval:
    a later CHANGES_REQUESTED or DISMISSED cancels it, COMMENTED and PENDING
    do not count. Apps, deleted accounts and the PR's author are left out.
    None when the reviews were not fetched or the window is cut short (an
    approval older than it would be missed)."""
    if not isinstance(conn, dict) or not isinstance(conn.get("nodes"), list):
        return None
    if (conn.get("pageInfo") or {}).get("hasPreviousPage"):
        return None
    latest: dict[str, str] = {}
    for review in conn["nodes"]:
        if not isinstance(review, dict):
            continue
        state = review.get("state")
        author = review.get("author")
        if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        if not isinstance(author, dict) or not author.get("login") or is_bot_login(author):
            continue
        latest[author["login"]] = state
    me = pr_author.lower()
    return sorted(login for login, state in latest.items()
                  if state == "APPROVED" and login.lower() != me)


class _AfterReady(NamedTuple):
    """What ``_commits_after_ready`` read from the timeline."""

    count: int | None
    ready_at: str = ""
    merges: tuple[str, ...] = ()                   # merge commits counted
    pushes: tuple[tuple[str, str], ...] = ()       # force pushes counted: (before, after)


def _commits_after_ready(node: dict) -> _AfterReady:
    """Commits and force pushes after the PR's last ready-for-review event,
    with that event's time, and the merge commits and force pushes among
    them (the engine takes the clean merges of the base out:
    ``engine/base_merges.py``).

    GitHub orders a timeline's commits by their committer date, not by when
    they were pushed, so a commit made before the event and pushed after it
    sits before it; a force push's event carries the push's own time. The
    engine's head check covers the first case (``WorktreeService.
    _ready_head``). A draft is 0. A PR opened ready, with no event, counts
    from its creation (commits committed after it, force pushes after it).
    None when the timeline was not fetched, or is cut short with no event
    in it.
    """
    conn = node.get("timelineItems")
    if node.get("isDraft"):
        return _AfterReady(0)
    if not isinstance(conn, dict) or not isinstance(conn.get("nodes"), list):
        return _AfterReady(None)
    items = [i for i in conn["nodes"] if isinstance(i, dict)]
    ready = [n for n, i in enumerate(items) if i.get("__typename") == "ReadyForReviewEvent"]
    if ready:
        since = items[ready[-1]].get("createdAt") or ""
        after = items[ready[-1] + 1:]
    elif (conn.get("pageInfo") or {}).get("hasPreviousPage"):
        return _AfterReady(None)
    else:
        since = node.get("createdAt") or ""
        if not since:
            return _AfterReady(None)
        after = [i for i in items if _when(i) > since]
    counted = [i for i in after
               if i.get("__typename") in ("PullRequestCommit", "HeadRefForcePushedEvent")]
    merges, pushes = [], []
    for i in counted:
        if i.get("__typename") == "PullRequestCommit":
            commit = i.get("commit") or {}
            if ((commit.get("parents") or {}).get("totalCount") or 0) >= 2 and commit.get("oid"):
                merges.append(commit["oid"])
        else:
            before = (i.get("beforeCommit") or {}).get("oid")
            now = (i.get("afterCommit") or {}).get("oid")
            if before and now:
                pushes.append((before, now))
    return _AfterReady(len(counted), since, tuple(merges), tuple(pushes))


def _when(item: dict) -> str:
    """A timeline commit's committer date, a force push's time."""
    if item.get("__typename") == "PullRequestCommit":
        return (item.get("commit") or {}).get("committedDate") or ""
    if item.get("__typename") == "HeadRefForcePushedEvent":
        return item.get("createdAt") or ""
    return ""


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

    def branch_commit_times(self) -> dict[str, float]:
        """Each local branch's last commit time (Unix), in one git call."""
        try:
            result = _run(
                [
                    "git",
                    "for-each-ref",
                    "--format=%(refname:short)%09%(committerdate:unix)",
                    "refs/heads",
                ],
                capture_output=True,
                text=True,
                cwd=str(self.repo_path),
                check=True,
            )
        except (subprocess.CalledProcessError, OSError):
            return {}
        times: dict[str, float] = {}
        for line in result.stdout.splitlines():
            name, _, stamp = line.rpartition("\t")
            if name and stamp.isdigit():
                times[name] = float(stamp)
        return times

    def branch_heads(self) -> dict[str, str]:
        """Each local branch's head commit (full sha), in one git call."""
        try:
            result = _run(
                ["git", "for-each-ref", "--format=%(refname:short)%09%(objectname)", "refs/heads"],
                capture_output=True,
                text=True,
                cwd=str(self.repo_path),
                check=True,
            )
        except (subprocess.CalledProcessError, OSError):
            return {}
        heads: dict[str, str] = {}
        for line in result.stdout.splitlines():
            name, _, sha = line.rpartition("\t")
            if name and sha:
                heads[name] = sha
        return heads

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
    def get_diff(
        worktree_path: str | Path,
        *,
        base: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        """Get diff showing all working tree changes including untracked files.

        Uses ``git diff`` for tracked changes. Untracked files are inlined,
        but only up to :data:`_MAX_UNTRACKED_FILE_BYTES` each and
        :data:`_MAX_DIFF_BYTES` overall (``max_bytes`` when given, up to
        :data:`MAX_DIFF_BYTES_LIMIT`).

        With ``base`` (a branch or commit), the tracked part is the diff from
        the merge-base of ``base`` and ``HEAD`` to the working tree: what
        the branch committed plus what is staged and unstaged. Raises
        :class:`ValueError` when the merge-base cannot be found.

        The caps are not cosmetic. A worktree holding a few 30 MB eval dumps
        produced 287 MB of "diff" here, and the Diff tab re-measures whatever
        it is handed on every layout pass — which is what made opening an
        agent take over a second. Nobody reads a megabyte of diff in a tab
        either, so the cap costs nothing real.
        """
        cwd = str(worktree_path)
        out = _Capped(_budget(max_bytes))
        since: list[str] = []
        if base is not None:
            since = [WorktreeManager._merge_base(cwd, base)]

        try:
            # Tracked changes (staged + unstaged)
            result = _run(
                ["git", "diff", *since],
                capture_output=True,
                cwd=cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                out.add(result.stdout.decode("utf-8", errors="replace").strip())

            # Staged binary files — git diff skips these, show a marker
            # (a diff from a merge-base already lists them).
            result = _run(
                ["git", "diff", "--cached", "--numstat", "-z"],
                capture_output=True,
                cwd=cwd,
            ) if not since else None
            if result is not None and result.returncode == 0 and result.stdout.strip():
                for entry in result.stdout.decode("utf-8", errors="replace").split("\0"):
                    if entry.startswith("-\t-\t"):
                        f = entry[len("-\t-\t"):]
                        out.add(f"diff --git a/{f} b/{f}\nstaged\nBinary file")

            WorktreeManager._add_untracked(cwd, out)
            return out.text()
        except OSError:
            return ""

    @staticmethod
    def get_pr_diff(
        worktree_path: str | Path,
        base: str | None,
        *,
        max_bytes: int | None = None,
    ) -> PrDiff:
        """The diff a pull request from ``HEAD`` into ``base`` would show,
        and apart from it what is not committed yet.

        ``committed`` is ``git diff $(git merge-base base HEAD) HEAD``,
        GitHub's three-dot "Files changed" (commits not pushed yet included).
        ``uncommitted`` is ``HEAD`` against the working tree (staged and
        unstaged together) plus the untracked files, as in :meth:`get_diff`.
        Without ``base`` only ``uncommitted`` is filled in. Raises
        :class:`ValueError` when there is no merge-base.

        The two share one byte budget (see :meth:`get_diff`): each gets at
        least half of it, and what one does not use the other may.
        """
        cwd = str(worktree_path)
        budget = _budget(max_bytes)
        merge_base = WorktreeManager._merge_base(cwd, base) if base is not None else ""
        committed = ""
        try:
            if merge_base:
                result = _run(["git", "diff", merge_base, "HEAD"], capture_output=True, cwd=cwd)
                if result.returncode == 0:
                    committed = result.stdout.decode("utf-8", errors="replace").strip()
            out = _Capped(budget)
            result = _run(["git", "diff", "HEAD"], capture_output=True, cwd=cwd)
            if result.returncode == 0 and result.stdout.strip():
                out.add(result.stdout.decode("utf-8", errors="replace").strip())
            WorktreeManager._add_untracked(cwd, out)
            uncommitted = out.text()
        except OSError:
            return PrDiff("", "", merge_base)
        half = budget // 2
        return PrDiff(
            committed=_clip(committed, max(half, budget - len(uncommitted))),
            uncommitted=_clip(uncommitted, max(half, budget - len(committed))),
            merge_base=merge_base,
        )

    @staticmethod
    def resolve_base_ref(worktree_path: str | Path, branch: str) -> str | None:
        """``origin/<branch>`` when that remote-tracking branch exists, else
        ``branch`` when it is a local branch, else ``None``.

        The remote-tracking one wins because the local branch is often far
        behind (nobody pulls the base in a feature worktree), and GitHub
        diffs against what is on the remote.
        """
        candidates = (
            (f"origin/{branch}", f"refs/remotes/origin/{branch}"),
            (branch, f"refs/heads/{branch}"),
        )
        for ref, full in candidates:
            try:
                found = _run(
                    ["git", "rev-parse", "--verify", "--quiet", full],
                    capture_output=True,
                    cwd=str(worktree_path),
                )
            except OSError:
                return None
            if found.returncode == 0:
                return ref
        return None

    @staticmethod
    def fetch_branch(worktree_path: str | Path, branch: str) -> str | None:
        """``git fetch origin <branch>``, which also moves ``origin/<branch>``.
        Returns ``None`` on success, else why it failed."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            result = _run(
                ["git", "fetch", "--quiet", "origin", branch],
                capture_output=True,
                text=True,
                cwd=str(worktree_path),
                env=env,
                timeout=_FETCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"git fetch origin {branch} timed out"
        except OSError as e:
            return str(e)
        if result.returncode != 0:
            return result.stderr.strip() or f"git fetch origin {branch} failed"
        return None

    @staticmethod
    def _merge_base(cwd: str, base: str) -> str:
        found = _run(["git", "merge-base", base, "HEAD"], capture_output=True, cwd=cwd)
        if found.returncode != 0 or not found.stdout.strip():
            raise ValueError(f"No merge-base between {base!r} and HEAD")
        return found.stdout.decode().strip()

    @staticmethod
    def _add_untracked(cwd: str, out: _Capped) -> None:
        """Untracked files — their contents, or a binary / too-large marker."""
        result = _run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            cwd=cwd,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        files = [f for f in result.stdout.decode("utf-8", errors="replace").split("\0") if f]
        for f in files:
            if out.full:
                break
            filepath = Path(cwd) / f
            header = f"diff --git a/{f} b/{f}\nnew file"
            try:
                # Size first: the old code sniffed 8 KB for binary and
                # then read the *whole* file, so a 30 MB dump was
                # inlined in full.
                size = filepath.stat().st_size
                if size > _MAX_UNTRACKED_FILE_BYTES:
                    out.add(f"{header}\n{_format_size(size)} — too large to show")
                    continue
                chunk = filepath.read_bytes()
            except OSError:
                continue
            if b"\x00" in chunk[:8000] or chunk.startswith(b"%PDF"):
                out.add(f"{header}\nBinary file")
            else:
                out.add(f"{header}\n{chunk.decode('utf-8', errors='replace')}")

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
        for item in _latest_runs(data.get("statusCheckRollup", []) or []):
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
            base_ref=data.get("baseRefName") or "",
            head_sha=data.get("headRefOid") or "",
            review_requests=_count(data.get("reviewRequests")),
            reviews=_count(data.get("latestReviews", data.get("reviews"))),
            is_draft=data["isDraft"] if isinstance(data.get("isDraft"), bool) else None,
            labels=_labels(data.get("labels")),
            author=(data.get("author") or {}).get("login") or "",
            approved_by=data.get("approvedBy"),
            commits_after_ready=data.get("commitsAfterReady"),
            ready_at=data.get("readyAt") or "",
            after_ready_merges=tuple(data.get("afterReadyMerges") or ()),
            after_ready_pushes=tuple(data.get("afterReadyPushes") or ()),
            base_merges_after_ready=0 if data.get("commitsAfterReady") is not None else None,
        )

    @staticmethod
    def _graphql_pr_to_view(node: dict) -> dict:
        """Reshape a GraphQL ``PullRequest`` node the way ``gh pr view --json``
        exports it, so both paths share one parser."""
        author = node.get("author") if isinstance(node.get("author"), dict) else {}
        after = _commits_after_ready(node)
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
                    "workflowName": (((ctx.get("checkSuite") or {}).get("workflowRun") or {})
                                     .get("workflow") or {}).get("name") or "",
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
            "baseRefName": node.get("baseRefName") or "",
            "headRefOid": node.get("headRefOid") or "",
            "reviewRequests": node.get("reviewRequests"),
            "reviews": node.get("reviews"),
            "isDraft": node.get("isDraft"),
            "labels": node.get("labels"),
            "author": author,
            # Only the batched query has them (gh pr view's --json has no
            # timeline, nor the reviewers' account types): unknown otherwise.
            "approvedBy": _approved_by(node.get("reviews"), author.get("login") or ""),
            "commitsAfterReady": after.count,
            "readyAt": after.ready_at,
            "afterReadyMerges": after.merges,
            "afterReadyPushes": after.pushes,
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
    def _select_batch(raw: str, heads: list[tuple[str, str]]) -> dict[str, dict | None]:
        """The chosen PR node per worktree in a batched response.

        ``heads`` lists ``(worktree_path, head_ref)`` in alias order: alias
        ``pr<i>`` answers ``heads[i]``. Raises ``TypeError`` when the
        response does not cover every alias.
        """
        data = json.loads(raw)
        repo = (data.get("data") or {}).get("repository")
        if not isinstance(repo, dict):
            raise TypeError(f"no repository in response: {data.get('errors')}")
        default_branch = (repo.get("defaultBranchRef") or {}).get("name") or ""
        result: dict[str, dict | None] = {}
        for i, (path, head) in enumerate(heads):
            conn = repo.get(f"pr{i}")
            if not isinstance(conn, dict):
                raise TypeError(f"missing pr{i} in response")
            result[path] = WorktreeManager._select_pr(conn.get("nodes") or [], default_branch, head)
        return result

    @staticmethod
    def _infos(nodes: dict[str, dict | None]) -> dict[str, PrInfo | None]:
        return {
            path: None if node is None
            else WorktreeManager._pr_info_from_view(WorktreeManager._graphql_pr_to_view(node))
            for path, node in nodes.items()
        }

    @staticmethod
    def _parse_pr_batch(raw: str, heads: list[tuple[str, str]]) -> dict[str, PrInfo | None]:
        """Parse a batched ``gh api graphql`` response (see :meth:`_select_batch`)."""
        return WorktreeManager._infos(WorktreeManager._select_batch(raw, heads))

    @staticmethod
    def _review_query(count: int) -> str:
        variables = ", ".join(f"$n{i}: Int!" for i in range(count))
        aliases = "\n".join(f"    r{i}: pullRequest(number: $n{i}) {{ ...review }}"
                            for i in range(count))
        return (
            f"query($owner: String!, $name: String!, {variables}) {{\n"
            "  repository(owner: $owner, name: $name) {\n"
            f"{aliases}\n"
            "  }\n"
            "}\n"
            f"{_REVIEW_FIELDS}"
        )

    def _add_review(self, nodes: dict[str, dict | None], repo: GithubRepo) -> None:
        """The review fields merged into ``nodes``' open PRs, in one call. A
        failure leaves them out: those facts read unknown."""
        open_nodes = [n for n in nodes.values() if n is not None and n.get("state") == "OPEN"]
        if not open_nodes:
            return
        cmd = [
            "gh", "api", "graphql",
            "-f", f"query={self._review_query(len(open_nodes))}",
            "-f", f"owner={repo.owner}",
            "-f", f"name={repo.name}",
        ]
        for i, node in enumerate(open_nodes):
            cmd += ["-F", f"n{i}={int(node.get('number') or 0)}"]
        try:
            proc = _run(cmd, capture_output=True, text=True, cwd=self.repo_path, timeout=20)
            if proc.returncode != 0:
                raise WorktreeManagerError(proc.stderr.strip() or str(proc.returncode))
            data = (json.loads(proc.stdout).get("data") or {}).get("repository") or {}
        except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError,
                WorktreeManagerError) as e:
            diag.event(log, "worker.pr_review_failed", error=str(e))
            return
        for i, node in enumerate(open_nodes):
            extra = data.get(f"r{i}")
            if isinstance(extra, dict):
                node.update(extra)

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
        self, worktrees: list[WorktreeInfo], repo: GithubRepo, *, review: bool = False,
    ) -> dict[str, PrInfo | None]:
        """PR info for many worktrees in one ``gh api graphql`` call per
        :data:`_PR_BATCH_SIZE` branches; with ``review``, one more per batch
        that has open PRs, for the review facts (:data:`_REVIEW_FIELDS`).

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
                nodes = self._select_batch(proc.stdout, chunk)
                if review:
                    self._add_review(nodes, repo)
                result.update(self._infos(nodes))
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
                    "--json",
                    "number,title,state,statusCheckRollup,url,reviewDecision,mergeable,baseRefName,"
                    "headRefOid,reviewRequests,latestReviews,isDraft,labels,author",
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
