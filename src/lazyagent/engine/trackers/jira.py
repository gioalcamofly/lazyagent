"""Jira Cloud, read through its REST API v3. **Experimental**: built and
tested against Atlassian's documented response shapes only, until checked
against a real instance.

The key is the issue key in the branch name (``SHOP-123``, the generic
pattern); ``projects = ["SHOP"]`` limits it to some projects. Both calls go
to ``GET /rest/api/3/search/jql`` (the enhanced search: ``nextPageToken``
pagination, ``isLast``; the old ``/rest/api/3/search`` with ``startAt`` is
deprecated on Jira Cloud), asking only for the fields shown:

- :meth:`Jira.fetch`: ``key in (...)``, :data:`BATCH` keys per query. Jira
  refuses the whole query (400) when one key names an issue that does not
  exist or that the user cannot see; those keys are dropped and the query
  is sent once more.
- :meth:`Jira.assigned_to_me`: the ``jql_assigned`` query (default
  ``assignee = currentUser() AND statusCategory != Done``), at most
  :data:`MAX_PAGES` pages.

The status is ``fields.status.name``; when that name is not mapped but its
category's name (``To Do``, ``In Progress``, ``Done``) is, the category's.
QA (``tracker.qa``): the ``[jira.qa]`` mapping names statuses (the name,
else its category's name); with ``qa_field = "customfield_10042"`` the QA
value comes from that field instead (asked for in both calls), its value
(text, a select option's ``value``, or a multi-select's options: the most
conservative one mapped) mapped by ``qa_field_values``. The category is
read only when the status name is unmapped, so one value maps at most.

The changelog is not read: the hub's first-seen edge already turns a status
change between two polls into an entry, and a leave-and-return inside one
``poll_s`` counts as one stay.

Credentials: basic auth with the account's email (``email``, or
``email_env``) and an API token (``token_env``, or the ``keyring`` service),
read on every call and sent only to ``base_url``, which must be https.
Redirects are not followed, so the token never goes to another host.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import datetime
from email.utils import parsedate_to_datetime

from lazyagent.engine.trackers import (
    RateLimited,
    TrackerConfig,
    TrackerError,
    TrackerTask,
    conservative_qa,
    credential,
)

BATCH = 50
PAGE_SIZE = 100
MAX_PAGES = 10
TIMEOUT = 20

SEARCH_PATH = "/rest/api/3/search/jql"
FIELDS = "summary,status,assignee,updated"
DEFAULT_JQL_ASSIGNED = "assignee = currentUser() AND statusCategory != Done"

_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
# A key Jira refused in a 400: "The value 'SHOP-9' does not exist for the
# field 'key'.", "An issue with key 'SHOP-9' does not exist for field 'key'."
_BAD_KEY = re.compile(r"'([A-Z][A-Z0-9_]*-\d+)'")

# (url, headers) → (HTTP status, headers by lower-case name, body)
Http = Callable[[str, dict[str, str]], Awaitable[tuple[int, dict[str, str], str]]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _urllib_http() -> Http:
    opener = urllib.request.build_opener(_NoRedirect)

    def get(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(request, timeout=TIMEOUT) as response:
                return (
                    response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read().decode("utf-8", "replace"),
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp is not None else ""
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise TrackerError(f"jira: {reason}") from e

    async def http(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        return await asyncio.to_thread(get, url, headers)

    return http


def _timestamp(text: str | None) -> float:
    """Jira's ``2026-10-20T10:00:00.000+0000``."""
    if not text:
        return 0.0
    for form in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, form).timestamp()
        except ValueError:
            continue
    return 0.0


def _retry_after(headers: dict[str, str]) -> float | None:
    value = headers.get("retry-after", "").strip()
    if not value:
        return None
    if value.isdigit():
        return max(float(value), 1.0)
    try:
        return max(parsedate_to_datetime(value).timestamp() - time.time(), 1.0)
    except (TypeError, ValueError):
        return None


def _messages(body: str) -> str:
    """Jira's ``errorMessages`` / ``errors``, else nothing (never the body:
    it may echo the request)."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    found = [m for m in data.get("errorMessages") or [] if isinstance(m, str)]
    errors = data.get("errors")
    if isinstance(errors, dict):
        found += [str(v) for v in errors.values()]
    return "; ".join(found)


def base_url(config: TrackerConfig) -> str:
    """``base_url`` without a trailing slash; https only (http for a
    loopback host, as a local proxy)."""
    url = config.options.get("base_url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("jira: base_url is not set")
    url = url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if not parts.hostname or not (
        parts.scheme == "https" or (parts.scheme == "http" and loopback)
    ):
        raise ValueError(f"jira: base_url must be https://…, not {url!r}")
    return url


class Jira:
    name = "jira"
    experimental = True

    def __init__(
        self, config: TrackerConfig, repo_root: str, *, http: Http | None = None
    ) -> None:
        self.config = config
        self.base_url = base_url(config)
        self.http = http or _urllib_http()
        jql = config.options.get("jql_assigned")
        self.jql_assigned = (
            jql.strip() if isinstance(jql, str) and jql.strip() else DEFAULT_JQL_ASSIGNED
        )
        projects = config.options.get("projects")
        self.fields = FIELDS + (f",{config.qa_field}" if config.qa_field else "")
        self.projects = (
            {p.upper() for p in projects if isinstance(p, str)}
            if isinstance(projects, list)
            else None
        )

    def key_pattern(self) -> str | None:
        if not self.projects:
            return None
        names = "|".join(sorted(map(re.escape, self.projects)))
        return rf"\b((?:{names})-\d+)"

    # --- HTTP ---

    def _auth(self) -> str:
        email = self.config.options.get("email")
        if not isinstance(email, str) or not email:
            email = credential(self.config, "email")
        token = credential(self.config, "token")
        if not email or not token:
            raise TrackerError("jira: no credentials (email_env / token_env / keyring)")
        return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    async def _search(self, jql: str, token: str | None, size: int) -> dict:
        query = {"jql": jql, "fields": self.fields, "maxResults": str(size)}
        if token:
            query["nextPageToken"] = token
        url = f"{self.base_url}{SEARCH_PATH}?{urllib.parse.urlencode(query)}"
        status, headers, body = await self.http(
            url, {"Authorization": self._auth(), "Accept": "application/json"}
        )
        if status == 429 or (status == 503 and "retry-after" in headers):
            raise RateLimited(f"jira: HTTP {status}", _retry_after(headers))
        if status == 400:
            raise _BadQuery(_messages(body))
        if status != 200:
            detail = _messages(body)
            raise TrackerError(f"jira: HTTP {status}" + (f": {detail}" if detail else ""))
        try:
            data = json.loads(body)
        except ValueError as e:
            raise TrackerError(f"jira: bad response: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
            raise TrackerError("jira: bad response: no issues")
        return data

    async def _search_all(self, jql: str, size: int, pages: int) -> list[TrackerTask]:
        tasks: list[TrackerTask] = []
        token: str | None = None
        for _ in range(pages):
            data = await self._search(jql, token, size)
            tasks += [t for t in map(self._task, data["issues"]) if t is not None]
            token = data.get("nextPageToken")
            if data.get("isLast") is not False or not isinstance(token, str) or not token:
                break
        return tasks

    # --- The task ---

    def status_of(self, status: object) -> str:
        if not isinstance(status, dict):
            return ""
        name = status.get("name") if isinstance(status.get("name"), str) else ""
        category = status.get("statusCategory")
        category = category.get("name") if isinstance(category, dict) else None
        if isinstance(category, str) and category and (
            not name or (self.config.rule(name) is None and self.config.rule(category) is not None)
        ):
            return category
        return name

    def qa_of(self, fields: dict) -> str | None:
        config = self.config
        if config.qa_field:
            value = fields.get(config.qa_field)
            found = []
            for option in value if isinstance(value, list) else [value]:
                if isinstance(option, dict):
                    option = option.get("value", option.get("name"))
                if isinstance(option, str):
                    found.append(config.qa_of(option, config.qa_field_values))
            return conservative_qa(found)
        status = fields.get("status")
        if not config.qa or not isinstance(status, dict):
            return None
        name = status.get("name")
        qa = config.qa_of(name) if isinstance(name, str) else None
        category = status.get("statusCategory")
        category = category.get("name") if isinstance(category, dict) else None
        if qa is None and isinstance(category, str):
            qa = config.qa_of(category)
        return qa

    def _task(self, issue: object) -> TrackerTask | None:
        if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
            return None
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        assignee = fields.get("assignee")
        if isinstance(assignee, dict):
            assignee = assignee.get("displayName") or assignee.get("accountId")
        return TrackerTask(
            key=issue["key"],
            url=f"{self.base_url}/browse/{issue['key']}",
            title=fields.get("summary") or "",
            status_raw=self.status_of(fields.get("status")),
            assignee=assignee if isinstance(assignee, str) else None,
            updated_at=_timestamp(fields.get("updated")),
            qa=self.qa_of(fields),
        )

    # --- fetch ---

    async def fetch(self, keys: list[str]) -> list[TrackerTask]:
        wanted = sorted({k for k in keys if _KEY.match(k)})
        if self.projects is not None:
            wanted = [k for k in wanted if k.rsplit("-", 1)[0] in self.projects]
        tasks: list[TrackerTask] = []
        for start in range(0, len(wanted), BATCH):
            tasks += await self._fetch_batch(wanted[start:start + BATCH])
        return tasks

    async def _fetch_batch(self, keys: list[str]) -> list[TrackerTask]:
        for retry in (True, False):
            if not keys:
                return []
            jql = "key in ({})".format(", ".join(f'"{k}"' for k in keys))
            try:
                return await self._search_all(jql, PAGE_SIZE, MAX_PAGES)
            except _BadQuery as e:
                bad = set(_BAD_KEY.findall(e.detail)) & set(keys)
                if not retry or not bad:
                    raise TrackerError(f"jira: HTTP 400: {e.detail}") from e
                keys = [k for k in keys if k not in bad]
        return []

    # --- assigned_to_me ---

    async def assigned_to_me(self) -> list[TrackerTask]:
        try:
            return await self._search_all(self.jql_assigned, PAGE_SIZE, MAX_PAGES)
        except _BadQuery as e:
            raise TrackerError(f"jira: jql_assigned: HTTP 400: {e.detail}") from e


class _BadQuery(TrackerError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
