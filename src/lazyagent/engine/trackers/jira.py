"""Jira Cloud, read through its REST API v3. **Experimental**: built and
tested against Atlassian's documented response shapes only, until checked
against a real instance (``lazyagent trackers check jira`` does the first
check: :func:`check`).

The key is the issue key in the branch name: the generic upper-case pattern
(``SHOP-123``, ``feat/SHOP-123-x``) by default; with ``projects = ["SHOP"]``
only those projects', in any case (``shop-123-fix`` is ``SHOP-123``) and
after any separator (``feat_shop-123``). Both calls go to ``GET
/rest/api/3/search/jql`` (the enhanced search: ``nextPageToken``
pagination, the token absent on the last page; the old
``/rest/api/3/search`` with ``startAt`` is gone from Jira Cloud), asking
only for the fields shown:

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
read on every call and sent only to the API root, which must be https:
``base_url`` (the site, ``https://example.atlassian.net``) for a classic
token; for a token *with scopes*, which Atlassian accepts only through its
gateway, ``cloud_id`` makes the root ``https://api.atlassian.com/ex/jira/
<cloud_id>``. Issue links (``<base_url>/browse/KEY``) always use the site.
Redirects are not followed, so the token never goes to another host.

Rate limits: a 429 (or a 503 with ``Retry-After``) backs the hub off for
``Retry-After`` seconds, else until ``X-RateLimit-Reset`` (ISO 8601), else
the hub's doubling back-off; ``RateLimit-Reason`` goes in the message.
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
MYSELF_PATH = "/rest/api/3/myself"
STATUS_PATH = "/rest/api/3/status"
FIELD_PATH = "/rest/api/3/field"
# The site's public tenant info: its cloudId, with no credential.
TENANT_INFO_PATH = "/_edge/tenant_info"
GATEWAY = "https://api.atlassian.com/ex/jira/"
FIELDS = "summary,status,assignee,updated"
DEFAULT_JQL_ASSIGNED = "assignee = currentUser() AND statusCategory != Done"

_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
# Without ``projects``: an upper-case key after a non-alphanumeric (so
# ``feat_SHOP-12`` and ``feat/SHOP-12`` both read SHOP-12).
GENERIC_BRANCH_KEY = r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]*-\d+)(?!\d)"
_PROJECT = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CLOUD_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
# ``/browse/SHOP-12`` and a board's ``?selectedIssue=SHOP-12``.
_BROWSE = re.compile(r"/browse/([A-Za-z][A-Za-z0-9_]*-\d+)(?:[/?#]|$)")
_SELECTED = re.compile(r"[?&]selectedIssue=([A-Za-z][A-Za-z0-9_]*-\d+)(?:&|#|$)")
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
    """``Retry-After`` (seconds, or an HTTP date), else ``X-RateLimit-Reset``
    (an ISO 8601 time, on a 429 only); None: neither."""
    value = headers.get("retry-after", "").strip()
    if value:
        if value.isdigit():
            return max(float(value), 1.0)
        try:
            return max(parsedate_to_datetime(value).timestamp() - time.time(), 1.0)
        except (TypeError, ValueError):
            pass
    reset = headers.get("x-ratelimit-reset", "").strip()
    if reset:
        try:
            at = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        except ValueError:
            return None
        if at.tzinfo is not None:
            return max(at.timestamp() - time.time(), 1.0)
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


def api_root(config: TrackerConfig) -> str:
    """Where the REST calls go: the site, or Atlassian's gateway for the
    site's ``cloud_id`` (a token with scopes)."""
    cloud_id = config.options.get("cloud_id")
    if cloud_id is None or cloud_id == "":
        return base_url(config)
    if not isinstance(cloud_id, str) or not _CLOUD_ID.fullmatch(cloud_id.strip()):
        raise ValueError(f"jira: bad cloud_id {cloud_id!r}")
    return GATEWAY + cloud_id.strip()


def projects_of(config: TrackerConfig) -> set[str] | None:
    """``projects`` upper-cased; None: every project. A bad entry raises."""
    projects = config.options.get("projects")
    if projects is None:
        return None
    if not isinstance(projects, list) or not all(
        isinstance(p, str) and _PROJECT.fullmatch(p) for p in projects
    ):
        raise ValueError(f"jira: projects must be a list of project keys, not {projects!r}")
    return {p.upper() for p in projects} or None


class Jira:
    name = "jira"
    experimental = True

    def __init__(
        self, config: TrackerConfig, repo_root: str, *, http: Http | None = None
    ) -> None:
        self.config = config
        self.base_url = base_url(config)
        self.api_root = api_root(config)
        self.http = http or _urllib_http()
        jql = config.options.get("jql_assigned")
        self.jql_assigned = (
            jql.strip() if isinstance(jql, str) and jql.strip() else DEFAULT_JQL_ASSIGNED
        )
        self.fields = FIELDS + (f",{config.qa_field}" if config.qa_field else "")
        self.projects = projects_of(config)

    def key_pattern(self) -> str:
        if not self.projects:
            return GENERIC_BRANCH_KEY
        names = "|".join(sorted(map(re.escape, self.projects)))
        return rf"(?i)(?<![A-Za-z0-9])((?:{names})-\d+)(?!\d)"

    def normalize_key(self, key: str) -> str:
        """Jira's keys are upper case (``shop-12`` → ``SHOP-12``)."""
        return key.upper()

    def key_of_url(self, url: str) -> str | None:
        """The key a link to an issue on this site names (``/browse/KEY``,
        a board's ``selectedIssue=KEY``); None: another host, or no key."""
        if not isinstance(url, str):
            return None
        parts = urllib.parse.urlsplit(url.strip())
        site = urllib.parse.urlsplit(self.base_url)
        if (parts.hostname or "").lower() != (site.hostname or "").lower():
            return None
        match = _BROWSE.search(parts.path) or _SELECTED.search("?" + parts.query)
        if match is None:
            return None
        key = match.group(1).upper()
        if self.projects is not None and key.rsplit("-", 1)[0] not in self.projects:
            return None
        return key

    def url_for(self, key: str) -> str | None:
        """The issue's link, for a key this adapter reads; None otherwise."""
        key = key.strip().upper() if isinstance(key, str) else ""
        if not _KEY.match(key):
            return None
        if self.projects is not None and key.rsplit("-", 1)[0] not in self.projects:
            return None
        return f"{self.base_url}/browse/{key}"

    # --- HTTP ---

    def _auth(self) -> str:
        email = self.config.options.get("email")
        if not isinstance(email, str) or not email:
            email = credential(self.config, "email")
        token = credential(self.config, "token")
        if not email or not token:
            raise TrackerError("jira: no credentials (email_env / token_env / keyring)")
        return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    async def get(self, path: str, query: dict[str, str] | None = None) -> object:
        """``GET <api root><path>`` with the credentials: the decoded JSON.
        Raises :class:`RateLimited`, :class:`_BadQuery` (a 400) or
        :class:`TrackerError`."""
        url = f"{self.api_root}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        status, headers, body = await self.http(
            url, {"Authorization": self._auth(), "Accept": "application/json"}
        )
        if status == 429 or (status == 503 and "retry-after" in headers):
            reason = headers.get("ratelimit-reason", "").strip()
            raise RateLimited(
                f"jira: HTTP {status}" + (f" ({reason})" if reason else ""),
                _retry_after(headers),
            )
        if status == 400:
            raise _BadQuery(_messages(body))
        if status != 200:
            detail = _messages(body) or _HINTS.get(status, "")
            raise TrackerError(f"jira: HTTP {status}" + (f": {detail}" if detail else ""))
        try:
            return json.loads(body)
        except ValueError as e:
            raise TrackerError(f"jira: bad response: {e}") from e

    async def _search(self, jql: str, token: str | None, size: int) -> dict:
        query = {"jql": jql, "fields": self.fields, "maxResults": str(size)}
        if token:
            query["nextPageToken"] = token
        data = await self.get(SEARCH_PATH, query)
        if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
            raise TrackerError("jira: bad response: no issues")
        return data

    async def _search_all(self, jql: str, size: int, pages: int) -> list[TrackerTask]:
        tasks: list[TrackerTask] = []
        token: str | None = None
        for _ in range(pages):
            data = await self._search(jql, token, size)
            tasks += [t for t in map(self._task, data["issues"]) if t is not None]
            # The last page has no token (``isLast`` may be left out).
            token = data.get("nextPageToken")
            if data.get("isLast") is True or not isinstance(token, str) or not token:
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
        wanted = sorted({k.upper() for k in keys if isinstance(k, str) and _KEY.match(k.upper())})
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


# What a status with no Jira message most likely means.
_HINTS = {
    401: "the email or API token is wrong or expired (a token with scopes "
         "also needs cloud_id)",
    403: "the token lacks a scope or the account lacks a permission "
         "(read:jira-work, read:jira-user)",
    404: "not found (check base_url, cloud_id and the project keys)",
}


class _BadQuery(TrackerError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# ----------------------------------------------------------------------
# First-run check (``lazyagent trackers check jira``)
# ----------------------------------------------------------------------

# The keys of ``[jira]`` the adapter reads (besides the shared ones).
OPTIONS = frozenset({
    "base_url", "cloud_id", "email", "email_env", "token_env", "keyring", "projects",
    "jql_assigned",
})
CHECK_SAMPLE = 5


def _credential_source(config: TrackerConfig, name: str) -> str | None:
    """Where :func:`credential` finds a secret (never the secret)."""
    import os

    env = config.options.get(f"{name}_env")
    if isinstance(env, str) and os.environ.get(env):
        return f"${env}"
    service = config.options.get("keyring")
    if isinstance(service, str) and service and credential(config, name):
        return f"keyring {service!r} (user {name!r})"
    return None


def _status_names(data: object) -> list[tuple[str, str]]:
    """``(name, category)`` from ``/status`` (a list of statuses) or
    ``/project/KEY/statuses`` (issue types, each with its statuses)."""
    out: dict[str, str] = {}
    items = data if isinstance(data, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        group = item.get("statuses") if isinstance(item.get("statuses"), list) else [item]
        for status in group:
            if not isinstance(status, dict) or not isinstance(status.get("name"), str):
                continue
            category = status.get("statusCategory")
            category = category.get("name") if isinstance(category, dict) else ""
            out.setdefault(status["name"], category if isinstance(category, str) else "")
    return sorted(out.items(), key=lambda nc: (nc[1], nc[0].casefold()))


async def check(
    config: TrackerConfig, *, http: Http | None = None, say: Callable[[str, str], None]
) -> bool:
    """Check ``[jira]`` against the instance, read-only: the config, the
    credentials (never shown), ``/myself``, one ``jql_assigned`` search,
    the status names (to fill ``[jira.status]`` / ``[jira.qa]``) and
    ``qa_field``. ``say(level, text)``, level ``ok`` · ``warn`` · ``fail``
    · ``info``. True: nothing failed."""
    ok = True

    def fail(text: str) -> None:
        nonlocal ok
        ok = False
        say("fail", text)

    unknown = sorted(set(config.options) - OPTIONS)
    if "token" in config.options:
        fail("[jira] token = … is never read (and should not be in a file): "
             "use token_env or keyring")
        unknown.remove("token")
    for key in unknown:
        say("warn", f"[jira] {key} is not a key lazyagent reads (typo?)")
    try:
        jira = Jira(config, "", http=http)
    except ValueError as e:
        fail(str(e))
        return False
    say("ok", f"site {jira.base_url}; API {jira.api_root}")
    say("info", "projects: " + (", ".join(sorted(jira.projects)) if jira.projects
                                else "any (upper-case keys only in branch names)"))
    email = config.options.get("email")
    email_from = "email" if isinstance(email, str) and email else _credential_source(config, "email")
    token_from = _credential_source(config, "token")
    if email_from is None:
        fail("no email: set email, or email_env to a variable that is set")
    if token_from is None:
        fail("no API token: set token_env to a variable that is set, or keyring")
    if email_from is None or token_from is None:
        return False
    say("ok", f"credentials: email from {email_from}, token from {token_from} (not shown)")

    # The site's cloudId, for a token with scopes (no credential sent).
    tenant: object = None
    try:
        status, _, body = await jira.http(
            f"{jira.base_url}{TENANT_INFO_PATH}", {"Accept": "application/json"}
        )
        tenant = json.loads(body) if status == 200 else None
    except (TrackerError, ValueError):
        tenant = None
    cloud_id = tenant.get("cloudId") if isinstance(tenant, dict) else None
    configured = config.options.get("cloud_id")
    if isinstance(cloud_id, str) and cloud_id:
        if configured and configured.strip() != cloud_id:
            fail(f"cloud_id {configured!r} is not this site's ({cloud_id})")
        elif not configured:
            say("info", f"site cloudId {cloud_id} (set cloud_id = \"{cloud_id}\" "
                        "if your API token has scopes)")

    try:
        me = await jira.get(MYSELF_PATH)
    except TrackerError as e:
        fail(f"/myself: {e}")
        return False
    if isinstance(me, dict):
        say("ok", f"signed in as {me.get('displayName') or '?'} "
                  f"(accountId {me.get('accountId') or '?'})")

    try:
        page = await jira.get(SEARCH_PATH, {
            "jql": jira.jql_assigned, "fields": jira.fields, "maxResults": str(CHECK_SAMPLE),
        })
    except _BadQuery as e:
        fail(f"jql_assigned {jira.jql_assigned!r}: HTTP 400: {e.detail}")
        page = None
    except TrackerError as e:
        fail(f"search: {e}")
        page = None
    if isinstance(page, dict) and isinstance(page.get("issues"), list):
        tasks = [t for t in map(jira._task, page["issues"]) if t is not None]
        more = " and more" if page.get("nextPageToken") and page.get("isLast") is not True else ""
        say("ok", f"jql_assigned: {len(tasks)} issue(s){more}: "
                  + (", ".join(f"{t.key} [{t.status_raw}]" for t in tasks) or "none"))

    seen: list[tuple[str, str]] = []
    sources = (
        [f"/rest/api/3/project/{urllib.parse.quote(p)}/statuses" for p in sorted(jira.projects)]
        if jira.projects else [STATUS_PATH]
    )
    for path in sources:
        try:
            seen += _status_names(await jira.get(path))
        except TrackerError as e:
            fail(f"{path}: {e}")
    names = dict(seen)
    if names:
        say("info", "statuses (name · category; mapped ones marked):")
        for name, category in sorted(names.items(), key=lambda nc: (nc[1], nc[0].casefold())):
            marks = []
            if config.rule(name) is not None:
                marks.append("[jira.status]")
            if config.qa_of(name) is not None:
                marks.append("[jira.qa]")
            say("info", f"  {name} · {category}" + (f"   ← {', '.join(marks)}" if marks else ""))
        known = {n.casefold() for n in names} | {c.casefold() for c in names.values() if c}
        for table, mapped in (("[jira.status]", config.status), ("[jira.qa]", config.qa)):
            for name in mapped:
                if name.casefold() not in known:
                    say("warn", f"{table} {name!r} is not a status (or category) of "
                                + ("these projects" if jira.projects else "this site"))

    if config.qa_field:
        try:
            fields = await jira.get(FIELD_PATH)
        except TrackerError as e:
            fail(f"{FIELD_PATH}: {e}")
            fields = None
        if isinstance(fields, list):
            found = next((f for f in fields if isinstance(f, dict)
                          and config.qa_field in (f.get("id"), f.get("key"))), None)
            if found is None:
                fail(f"qa_field {config.qa_field!r} is not a field of this site")
            else:
                say("ok", f"qa_field {config.qa_field}: {found.get('name') or '?'}")
    return ok
