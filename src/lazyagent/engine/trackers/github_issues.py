"""GitHub Issues, read through ``gh`` (its login: no credential of ours).

The key is the issue number in the branch name (``fix-123-login``,
``feature/123``, ``issue-45``). :meth:`GithubIssues.fetch` reads up to
:data:`BATCH` issues per ``gh api graphql`` call; :meth:`assigned_to_me`
lists the user's open issues with ``gh api`` and ``If-None-Match``, so an
unchanged list costs a 304 (which GitHub does not count against the rate
limit). The repository is the one ``gh`` resolves in the repo root.

``status_from = "labels"`` (default): the status is a label of the issue
that the ``[github_issues.status]`` mapping names (one mapped with a
``level`` first, then in the mapping's order), else ``closed`` for a closed
issue, else none. ``"state"``: ``open``
or ``closed``.

QA (``tracker.qa``): the labels the ``[github_issues.qa]`` mapping names
(whatever ``status_from`` says); with several, the most conservative
(failed, pending, tested, not_required).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from lazyagent.engine.trackers import (
    RateLimited,
    TrackerConfig,
    TrackerError,
    TrackerTask,
    conservative_qa,
)

BATCH = 50
TIMEOUT = 20

# argv → (exit code, stdout, stderr)
Run = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

_FIELDS = """
fragment F on Issue {
  number url title state updatedAt
  labels(first: 50) { nodes { name } }
  assignees(first: 5) { nodes { login } }
}
"""


def _gh_runner(cwd: str) -> Run:
    from lazyagent.worktree_manager import _run

    def run_sync(args: list[str]) -> tuple[int, str, str]:
        try:
            proc = _run(
                ["gh", *args], capture_output=True, text=True, cwd=cwd or None, timeout=TIMEOUT
            )
        except subprocess.TimeoutExpired as e:
            raise TrackerError(f"gh timed out: {e}") from e
        except OSError as e:
            raise TrackerError(f"gh: {e}") from e
        return proc.returncode, proc.stdout, proc.stderr

    async def run(args: list[str]) -> tuple[int, str, str]:
        return await asyncio.to_thread(run_sync, args)

    return run


def _timestamp(text: str | None) -> float:
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _rate_limited(stderr: str) -> bool:
    text = stderr.lower()
    return "429" in text or "rate limit" in text


def split_response(stdout: str) -> tuple[int, dict[str, str], str]:
    """``gh api -i`` output: (HTTP status, headers by lower-case name, body)."""
    head, _, body = stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return 0, {}, stdout
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers, body


class GithubIssues:
    name = "github_issues"

    def __init__(
        self, config: TrackerConfig, repo_root: str, *, run: Run | None = None
    ) -> None:
        self.config = config
        self.run = run or _gh_runner(repo_root)
        self.status_from = (
            "state" if config.options.get("status_from") == "state" else "labels"
        )
        self._login: str | None = None
        self._etag: str | None = None
        self._assigned: list[TrackerTask] = []

    def key_pattern(self) -> str:
        return r"(?:^|[/_-])#?(\d+)(?=$|[/_.-])"

    # --- The status ---

    def status_of(self, state: str, labels: list[str]) -> str:
        if self.status_from == "state":
            return state.lower()
        names = {label.casefold() for label in labels}
        found = [m for m in self.config.status if m.casefold() in names]
        # A status that asks for the user beats the others.
        found.sort(key=lambda m: self.config.status[m].level is None)
        if found:
            return found[0]
        return "closed" if state.lower() == "closed" else ""

    def qa_of(self, labels: list[str]) -> str | None:
        return conservative_qa(self.config.qa_of(label) for label in labels)

    def _task(
        self, number: int, url: str, title: str, state: str, updated: str | None,
        labels: list[str], assignees: list[str],
    ) -> TrackerTask:
        return TrackerTask(
            key=str(number),
            url=url,
            title=title,
            status_raw=self.status_of(state, labels),
            assignee=assignees[0] if assignees else None,
            updated_at=_timestamp(updated),
            qa=self.qa_of(labels),
        )

    # --- fetch ---

    async def fetch(self, keys: list[str]) -> list[TrackerTask]:
        numbers = sorted({int(k) for k in keys if k.isdigit()})
        tasks: list[TrackerTask] = []
        for start in range(0, len(numbers), BATCH):
            tasks += await self._fetch_batch(numbers[start:start + BATCH])
        return tasks

    async def _fetch_batch(self, numbers: list[int]) -> list[TrackerTask]:
        aliases = "\n".join(
            f"    i{n}: issueOrPullRequest(number: {n}) {{ ...F }}" for n in numbers
        )
        query = (
            "query($owner: String!, $name: String!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            f"{aliases}\n"
            "  }\n"
            "}\n"
            f"{_FIELDS}"
        )
        code, out, err = await self.run(
            ["api", "graphql", "-f", f"query={query}", "-F", "owner={owner}", "-F", "name={repo}"]
        )
        try:
            data = json.loads(out) if out.strip() else None
        except ValueError:
            data = None
        repo = (data or {}).get("data", {}) if isinstance(data, dict) else {}
        repo = repo.get("repository") if isinstance(repo, dict) else None
        if not isinstance(repo, dict):
            # A missing issue still answers with data; no data is a failure.
            if _rate_limited(err) or _rate_limited(out):
                raise RateLimited(err.strip() or "rate limited")
            raise TrackerError(f"gh api graphql failed: {err.strip() or code}")
        tasks = []
        for n in numbers:
            node = repo.get(f"i{n}")
            # null: no such issue; no number: a pull request.
            if not isinstance(node, dict) or "number" not in node:
                continue
            tasks.append(
                self._task(
                    node["number"],
                    node.get("url", ""),
                    node.get("title", ""),
                    node.get("state", ""),
                    node.get("updatedAt"),
                    [x["name"] for x in (node.get("labels") or {}).get("nodes", [])],
                    [x["login"] for x in (node.get("assignees") or {}).get("nodes", [])],
                )
            )
        return tasks

    # --- assigned_to_me ---

    async def _api(self, path: str, etag: str | None = None) -> tuple[int, dict[str, str], str]:
        args = ["api", "-i", path]
        if etag:
            args[1:1] = ["-H", f"If-None-Match: {etag}"]
        code, out, err = await self.run(args)
        status, headers, body = split_response(out)
        if status == 429 or (
            status == 403 and headers.get("x-ratelimit-remaining") == "0"
        ):
            raise RateLimited(f"HTTP {status}", _retry_after(headers))
        if status == 0:
            if _rate_limited(err):
                raise RateLimited(err.strip())
            raise TrackerError(f"gh api {path} failed: {err.strip() or code}")
        if status >= 400:
            raise TrackerError(f"gh api {path}: HTTP {status}")
        return status, headers, body

    async def assigned_to_me(self) -> list[TrackerTask]:
        if self._login is None:
            _, _, body = await self._api("user")
            try:
                self._login = json.loads(body)["login"]
            except (ValueError, KeyError, TypeError) as e:
                raise TrackerError(f"gh api user: bad response: {e}") from e
        path = (
            "repos/{owner}/{repo}/issues"
            f"?assignee={self._login}&state=open&per_page=100"
        )
        status, headers, body = await self._api(path, self._etag)
        if status == 304:
            return list(self._assigned)
        try:
            items = json.loads(body)
            tasks = [
                self._task(
                    item["number"],
                    item.get("html_url", ""),
                    item.get("title", ""),
                    item.get("state", ""),
                    item.get("updated_at"),
                    [x["name"] for x in item.get("labels", [])],
                    [x["login"] for x in item.get("assignees", [])],
                )
                for item in items
                if "pull_request" not in item
            ]
        except (ValueError, KeyError, TypeError) as e:
            raise TrackerError(f"gh api issues: bad response: {e}") from e
        self._etag = headers.get("etag")
        self._assigned = tasks
        return list(tasks)


def _retry_after(headers: dict[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value and value.isdigit():
        return float(value)
    reset = headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(float(reset) - time.time(), 1.0)
    return None
