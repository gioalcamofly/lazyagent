"""Review requests: the open PRs where someone asked the user for a review
(docs/design/inbox-plan.md §8, "Review requests as landed").

lazyagent follows the PRs of the user's own branches through the PR poller;
a teammate asking for a review reaches no worktree. This hub asks GitHub
for them, one GraphQL search per poll per repo::

    repo:<owner>/<name> is:pr is:open draft:false review-requested:@me

``review-requested:@me`` matches a request to the user and to any team the
user is in (``user-review-requested:@me`` would miss the team ones). The
query also brings the viewer's login (``pr.mine``), each PR's requested
reviewers, the latest review of each reviewer and the last review-request
events, so a request's age is the time it was made (else the PR's update).

**Done.** A request is done when the viewer's latest review (approve,
request changes or comment; a pending review is not submitted) is newer
than the latest request event that names the viewer or a requested team:
GitHub drops a user's own request on any submitted review, but a team's
request stays after a member reviewed. A request removed, or a PR closed,
merged or turned into a draft leaves the search. Re-requested later, a new
event makes it a request again.

**Cost.** One ``gh api graphql`` call per poll (2 points of the GraphQL
rate limit, measured, for up to :data:`MAX_REQUESTS` PRs), at most every
:data:`DEFAULT_POLL_S` like the PR poller, and only once the PR poller has
found ``gh`` working. GitHub's search answers no conditional requests (no
``ETag``, ``Cache-Control: no-cache``) and GraphQL is a POST, so there is
no 304 to ask for: the last good answer is kept (on an error too), every
call counts against :data:`MAX_CALLS_PER_MIN`, and a rate limit backs off
as the Actions hub does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lazyagent.engine import actions
from lazyagent.engine.actions import Run
from lazyagent.engine.trackers import (
    BACKOFF_MAX,
    BACKOFF_MIN,
    RateLimited,
    TrackerError,
)
from lazyagent.engine.trackers.github_issues import _rate_limited

log = logging.getLogger(__name__)

DEFAULT_POLL_S = 60
# Every gh call of the hub in a rolling minute (one a poll, so a margin).
MAX_CALLS_PER_MIN = 4
CALL_TIMEOUT = 30.0
# PRs one search reads; past it the oldest are left out (and logged).
MAX_REQUESTS = 50

SEARCH = "repo:{owner}/{name} is:pr is:open draft:false review-requested:@me"

QUERY = """
query($q: String!) {
  viewer { login }
  search(query: $q, type: ISSUE, first: %d) {
    issueCount
    nodes {
      ... on PullRequest {
        number title url isDraft state headRefName isCrossRepository updatedAt reviewDecision
        author { login }
        reviewRequests(first: 20) {
          nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } } }
        }
        latestReviews(first: 50) { nodes { author { login } state submittedAt } }
        timelineItems(last: 20, itemTypes: [REVIEW_REQUESTED_EVENT]) {
          nodes {
            ... on ReviewRequestedEvent {
              createdAt
              requestedReviewer { __typename ... on User { login } ... on Team { slug } }
            }
          }
        }
      }
    }
  }
}
""" % MAX_REQUESTS

VIEWER_QUERY = "query { viewer { login } }"


@dataclass(frozen=True)
class ReviewRequest:
    """An open PR, not a draft, that asks the viewer for a review."""

    number: int
    title: str
    url: str
    author: str
    # The PR's head branch (headRefName) and whether it lives in a fork.
    head: str
    cross_repo: bool
    # When the request was made: the latest review-request event naming the
    # viewer or a requested team, else the PR's last update.
    requested_at: float
    # The team it came through when the viewer was not asked directly.
    team: str = ""
    # The PR's review decision (APPROVED, CHANGES_REQUESTED, ...; "" none).
    review: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "head": self.head,
            "requested_at": self.requested_at,
            "team": self.team or None,
            "review": self.review,
        }


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _reviewer(node: Any) -> tuple[str, str] | None:
    """``("user", login)`` or ``("team", slug)``; None when not visible."""
    if not isinstance(node, dict):
        return None
    if node.get("__typename") == "Team" and node.get("slug"):
        return "team", node["slug"].lower()
    if node.get("login"):
        return "user", node["login"].lower()
    return None


def _nodes(conn: Any) -> list[dict[str, Any]]:
    nodes = conn.get("nodes") if isinstance(conn, dict) else None
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def parse_request(node: dict[str, Any], viewer: str) -> ReviewRequest | None:
    """One search hit as a request; None when it is not one (a draft, not
    open, the viewer's own PR, or reviewed since it was asked)."""
    if node.get("state") != "OPEN" or node.get("isDraft") is not False:
        return None
    number = node.get("number")
    if not isinstance(number, int) or not node.get("headRefName"):
        return None
    me = viewer.lower()
    author = ((node.get("author") or {}).get("login")) or ""
    if author.lower() == me:
        return None
    asked = [r for r in (_reviewer(n.get("requestedReviewer"))
                         for n in _nodes(node.get("reviewRequests"))) if r is not None]
    teams = {name for kind, name in asked if kind == "team"}
    direct = ("user", me) in asked
    requested_at = None
    for event in _nodes(node.get("timelineItems")):
        who = _reviewer(event.get("requestedReviewer"))
        if who == ("user", me) or (who is not None and who[0] == "team" and who[1] in teams) \
                or (who is None and not direct):
            at = _epoch(event.get("createdAt"))
            if at is not None and (requested_at is None or at > requested_at):
                requested_at = at
    if requested_at is None:
        requested_at = _epoch(node.get("updatedAt")) or 0.0
    for review in _nodes(node.get("latestReviews")):
        login = ((review.get("author") or {}).get("login")) or ""
        submitted = _epoch(review.get("submittedAt"))
        if login.lower() == me and review.get("state") != "PENDING" and submitted is not None \
                and submitted >= requested_at:
            return None
    return ReviewRequest(
        number=number,
        title=node.get("title") or "",
        url=node.get("url") or "",
        author=author,
        head=node["headRefName"],
        cross_repo=bool(node.get("isCrossRepository")),
        requested_at=requested_at,
        team="" if direct else (sorted(teams)[0] if teams else ""),
        review=node.get("reviewDecision") or "",
    )


def parse_search(body: str) -> tuple[str, list[ReviewRequest], int]:
    """A search response: (the viewer's login, the requests, the hit count).
    Raises ValueError on a body that is not one."""
    data = json.loads(body)
    root = data.get("data") if isinstance(data, dict) else None
    if not isinstance(root, dict):
        raise ValueError(f"no data in response: {data.get('errors') if isinstance(data, dict) else data}")
    viewer = (root.get("viewer") or {}).get("login")
    search = root.get("search")
    if not isinstance(viewer, str) or not viewer or not isinstance(search, dict):
        raise ValueError("no viewer or search in response")
    found = []
    for node in _nodes(search):
        request = parse_request(node, viewer)
        if request is not None:
            found.append(request)
    found.sort(key=lambda r: (r.requested_at, r.number))
    count = search.get("issueCount")
    return viewer, found, count if isinstance(count, int) else len(found)


class ReviewRequestHub:
    """The engine's review requests (one repo); see the module docstring."""

    def __init__(
        self,
        repo_root: str = "",
        *,
        enabled: bool = True,
        poll_s: int = DEFAULT_POLL_S,
        run: Run | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repo_root = repo_root
        self.enabled_config = enabled
        self.poll_s = poll_s
        # Read at call time: the tests' conftest replaces the runner there.
        self.run = run or actions._gh_runner(repo_root)
        self.clock = clock
        self.requests: tuple[ReviewRequest, ...] = ()
        # The authenticated user's login; None until a call told it.
        self.viewer: str | None = None
        # A search answered: an absent request is then known to be absent.
        self.known = False
        self.last_poll = float("-inf")
        self.error: str | None = None
        self.error_at = 0.0
        self.backoff = 0.0
        self.backoff_until = 0.0
        self.calls: deque[float] = deque()
        # The shared GitHub state (github_health.py) each call reports to;
        # the status projection sets it.
        self.health: Any = None
        self._polling = False
        self._viewer_tried = False

    @property
    def enabled(self) -> bool:
        return bool(self.repo_root) and self.enabled_config

    def may_call(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        return now >= self.backoff_until and len(self.calls) < MAX_CALLS_PER_MIN

    def due(self, now: float) -> bool:
        return now - self.last_poll >= self.poll_s

    async def _graphql(self, query: str, **variables: str) -> str:
        now = self.clock()
        if not self.may_call(now):
            raise TrackerError(
                "waiting out GitHub's rate limit" if now < self.backoff_until
                else "too many gh calls this minute"
            )
        self.calls.append(now)
        args = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            args += ["-f", f"{name}={value}"]
        try:
            code, out, err = await asyncio.wait_for(self.run(args), CALL_TIMEOUT)
        except asyncio.TimeoutError as e:
            if self.health is not None:
                self.health.failed("gh api graphql timed out", now)
            raise TrackerError("gh api graphql timed out") from e
        if code != 0:
            if _rate_limited(err) or _rate_limited(out):
                raise RateLimited((err or out).strip()[:200])
            if self.health is not None:
                self.health.failed(err.strip() or f"gh api graphql: {code}", now)
            raise TrackerError(f"gh api graphql failed: {err.strip() or code}")
        if self.health is not None:
            self.health.succeeded()
        return out

    def _failed(self, now: float, e: Exception) -> None:
        message = str(e) or type(e).__name__
        if isinstance(e, RateLimited):
            self.backoff = (
                e.retry_after if e.retry_after
                else min(max(self.backoff * 2, BACKOFF_MIN), BACKOFF_MAX)
            )
            self.backoff_until = now + self.backoff
        if message != self.error:
            # An outage is logged once, by the shared state.
            outage = self.health is not None and not self.health.available
            log.log(logging.DEBUG if outage else logging.WARNING, "review requests: %s", message)
        self.error, self.error_at = message, now

    async def poll(self, owner: str, name: str, *, force: bool = False) -> bool:
        """One search when due (``force``: now); True when what the rows
        read changed. Never two at once. Off, it only learns the viewer's
        login, once (``pr.mine``)."""
        now = self.clock()
        if self._polling or not self.repo_root or not owner or not name:
            return False
        if not self.enabled:
            return await self._learn_viewer()
        if not force and not self.due(now):
            return False
        if not self.may_call(now):
            return False
        self._polling = True
        self.last_poll = now
        try:
            body = await self._graphql(QUERY, q=SEARCH.format(owner=owner, name=name))
            viewer, requests, count = parse_search(body)
        except (TrackerError, ValueError, OSError, AttributeError, TypeError) as e:
            self._failed(self.clock(), e)
            return False
        finally:
            self._polling = False
        if count > MAX_REQUESTS:
            log.info("review requests: %d PRs ask for a review, read the first %d",
                     count, MAX_REQUESTS)
        self.backoff = 0.0
        self.error = None
        changed = (viewer, tuple(requests), True) != (self.viewer, self.requests, self.known)
        self.viewer, self.requests, self.known = viewer, tuple(requests), True
        return changed

    async def _learn_viewer(self) -> bool:
        if self.viewer is not None or self._viewer_tried:
            return False
        self._viewer_tried = True
        self._polling = True
        try:
            body = await self._graphql(VIEWER_QUERY)
            login = ((json.loads(body).get("data") or {}).get("viewer") or {}).get("login")
        except (TrackerError, ValueError, OSError, AttributeError, TypeError) as e:
            self._failed(self.clock(), e)
            return False
        finally:
            self._polling = False
        if not isinstance(login, str) or not login:
            return False
        self.viewer = login
        return True

    def turn_off(self) -> None:
        """``[status] review_requests = false``: the requests go (the login
        stays, for ``pr.mine``)."""
        self.enabled_config = False
        self.requests = ()
        self.known = False
