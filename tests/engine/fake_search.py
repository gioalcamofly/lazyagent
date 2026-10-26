"""A fake ``gh api graphql`` for the review requests hub
(engine/review_requests.py): the search's answer built from PR nodes as
GitHub returns them. Never the real GitHub."""

from __future__ import annotations

import json
from typing import Any

ME = "me"


def iso(t: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(login: str) -> dict[str, Any]:
    return {"__typename": "User", "login": login}


def team(slug: str) -> dict[str, Any]:
    return {"__typename": "Team", "slug": slug}


def pr_node(
    number: int, head: str, *, author: str = "alice", title: str = "Fix the export",
    asked: tuple[dict[str, Any], ...] = (user(ME),), requested_at: float | None = None,
    reviews: tuple[tuple[str, str, float], ...] = (), draft: bool = False,
    state: str = "OPEN", cross_repo: bool = False, updated_at: float = 0.0,
    review: str = "REVIEW_REQUIRED",
) -> dict[str, Any]:
    """A search hit. ``asked``: the requested reviewers (one event each at
    ``requested_at``); ``reviews``: (login, state, submitted at)."""
    events = [] if requested_at is None else [
        {"createdAt": iso(requested_at), "requestedReviewer": who} for who in asked]
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/o/r/pull/{number}",
        "isDraft": draft,
        "state": state,
        "headRefName": head,
        "isCrossRepository": cross_repo,
        "updatedAt": iso(updated_at) if updated_at else "2026-10-20T09:00:00Z",
        "reviewDecision": review,
        "author": {"login": author},
        "reviewRequests": {"nodes": [{"requestedReviewer": who} for who in asked]},
        "latestReviews": {"nodes": [
            {"author": {"login": login}, "state": st, "submittedAt": iso(at)}
            for login, st, at in reviews]},
        "timelineItems": {"nodes": events},
    }


class FakeSearch:
    """``prs`` is what the search finds now; ``queue`` overrides the next
    calls with an answer or an exception."""

    def __init__(self, prs: list[dict[str, Any]] | None = None, viewer: str = ME) -> None:
        self.prs = list(prs or [])
        self.viewer = viewer
        self.calls: list[list[str]] = []
        self.queue: list[tuple[int, str, str] | Exception] = []

    async def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if self.queue:
            answer = self.queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        assert args[:2] == ["api", "graphql"], args
        query = next(a for a in args if a.startswith("query="))
        if "search(" not in query:
            return 0, json.dumps({"data": {"viewer": {"login": self.viewer}}}), ""
        return 0, json.dumps({"data": {
            "viewer": {"login": self.viewer},
            "search": {"issueCount": len(self.prs), "nodes": self.prs},
        }}), ""

    def search_of(self, call: list[str]) -> str:
        return next(a for a in call if a.startswith("q=")).removeprefix("q=")
