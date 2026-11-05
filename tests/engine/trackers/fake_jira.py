"""A stand-in for Jira Cloud's REST API as the Jira adapter calls it: it
replays recorded responses (``fixtures/jira/*.json``, each ``{status,
headers, body}`` in Atlassian's documented shapes) in order and records the
requests. No network."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "jira"
BASE_URL = "https://example.atlassian.net"
# Atlassian's gateway, for a token with scopes (``cloud_id``).
GATEWAY = "https://api.atlassian.com"
CLOUD_ID = "11111111-2222-4333-8444-555555555555"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeJira:
    def __init__(self, *responses: str | dict[str, Any]) -> None:
        self.queue: list[str | dict[str, Any]] = list(responses)
        # (path, query, headers) per request.
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.roots: list[str] = []

    def then(self, *responses: str | dict[str, Any]) -> FakeJira:
        self.queue += responses
        return self

    async def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        assert root in (BASE_URL, GATEWAY), url
        self.roots.append(root)
        self.requests.append(
            (parts.path, dict(urllib.parse.parse_qsl(parts.query)), dict(headers))
        )
        assert self.queue, f"no recorded response left for {url}"
        response = self.queue.pop(0)
        if isinstance(response, str):
            response = fixture(response)
        body = response["body"]
        return (
            response["status"],
            {k.lower(): v for k, v in response["headers"].items()},
            body if isinstance(body, str) else json.dumps(body),
        )

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.requests]

    def jql(self, n: int = -1) -> str:
        return self.requests[n][1]["jql"]
