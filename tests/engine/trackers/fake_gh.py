"""A stand-in for ``gh`` as the GitHub Issues adapter calls it: issues in a
dict, answers shaped like the real ``gh api`` output. No network."""

from __future__ import annotations

import json
import re
from typing import Any


def issue(
    number: int, title: str = "", *, state: str = "OPEN", labels: tuple[str, ...] = (),
    assignees: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Issue {number}",
        "state": state,
        "labels": list(labels),
        "assignees": list(assignees),
        "url": f"https://github.com/acme/widgets/issues/{number}",
        "updatedAt": "2026-10-20T10:00:00Z",
    }


class FakeGh:
    def __init__(self, *issues: dict[str, Any], login: str = "octo") -> None:
        self.issues = {i["number"]: i for i in issues}
        # Numbers that are pull requests, not issues.
        self.pulls: set[int] = set()
        self.login = login
        self.calls: list[list[str]] = []
        # Set to (status, headers) to fail the next REST call; for graphql,
        # set graphql_error to the stderr text.
        self.rest_error: tuple[int, dict[str, str]] | None = None
        self.graphql_error: str | None = None

    def set(self, number: int, **changes: Any) -> None:
        self.issues[number] = {**self.issues[number], **changes}

    async def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            return self._graphql(args)
        return self._rest(args)

    def calls_to(self, what: str) -> int:
        return sum(what in " ".join(c) for c in self.calls)

    def _graphql(self, args: list[str]) -> tuple[int, str, str]:
        if self.graphql_error is not None:
            return 1, "", self.graphql_error
        query = next(a for a in args if a.startswith("query="))
        repo: dict[str, Any] = {}
        errors = []
        for n in map(int, re.findall(r"issueOrPullRequest\(number: (\d+)\)", query)):
            if n in self.pulls:
                repo[f"i{n}"] = {}
            elif n in self.issues:
                i = self.issues[n]
                repo[f"i{n}"] = {
                    "number": n, "url": i["url"], "title": i["title"], "state": i["state"],
                    "updatedAt": i["updatedAt"],
                    "labels": {"nodes": [{"name": x} for x in i["labels"]]},
                    "assignees": {"nodes": [{"login": x} for x in i["assignees"]]},
                }
            else:
                repo[f"i{n}"] = None
                errors.append({"type": "NOT_FOUND", "path": ["repository", f"i{n}"]})
        body: dict[str, Any] = {"data": {"repository": repo}}
        if errors:
            body["errors"] = errors
        # gh exits 1 when the answer carries errors, data or not.
        return (1 if errors else 0), json.dumps(body), ("gh: Could not resolve" if errors else "")

    def _etag(self) -> str:
        mine = sorted(
            (n, i["state"], tuple(i["labels"]))
            for n, i in self.issues.items()
            if self.login in i["assignees"]
        )
        return f'W/"{abs(hash(tuple(mine)))}"'

    def _rest(self, args: list[str]) -> tuple[int, str, str]:
        headers: dict[str, str] = {}
        rest = list(args[1:])
        while rest and rest[0] == "-H":
            name, _, value = rest[1].partition(": ")
            headers[name.lower()] = value
            rest = rest[2:]
        assert rest[0] == "-i", args
        path = rest[1]
        if self.rest_error is not None:
            status, extra = self.rest_error
            self.rest_error = None
            return 1, _response(status, extra, '{"message": "error"}'), f"gh: HTTP {status}"
        if path == "user":
            return 0, _response(200, {}, json.dumps({"login": self.login})), ""
        assert path.startswith("repos/{owner}/{repo}/issues?"), path
        assert f"assignee={self.login}" in path
        etag = self._etag()
        if headers.get("if-none-match") == etag:
            return 1, _response(304, {"ETag": etag}, ""), "gh: HTTP 304"
        items = []
        for n, i in sorted(self.issues.items()):
            if self.login not in i["assignees"] or i["state"] != "OPEN":
                continue
            item = {
                "number": n, "html_url": i["url"], "title": i["title"], "state": "open",
                "updated_at": i["updatedAt"],
                "labels": [{"name": x} for x in i["labels"]],
                "assignees": [{"login": x} for x in i["assignees"]],
            }
            if n in self.pulls:
                item["pull_request"] = {}
            items.append(item)
        return 0, _response(200, {"ETag": etag}, json.dumps(items)), ""


def _response(status: int, headers: dict[str, str], body: str) -> str:
    reason = {200: "OK", 304: "Not Modified", 403: "Forbidden", 429: "Too Many Requests"}
    lines = [f"HTTP/2.0 {status} {reason.get(status, '')}".rstrip()]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    return "\r\n".join(lines) + "\r\n\r\n" + body
