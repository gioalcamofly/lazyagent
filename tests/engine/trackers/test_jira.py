"""The Jira adapter (experimental) against recorded Jira Cloud responses:
the enhanced JQL search and its pagination, keys from branch names, status
mapping, 400s for unknown keys, 429 with Retry-After, auth errors, and a
"returned" status reaching the inbox through the hub. No network."""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import logging
import threading
from datetime import datetime, timezone

import pytest

from lazyagent.engine.trackers import (
    RateLimited,
    StatusRule,
    TrackerConfig,
    TrackerError,
    TrackerHub,
    adapter_factories,
    branch_key,
    config_dir,
    load_trackers,
)
from lazyagent.engine.trackers import jira as jira_mod
from lazyagent.engine.trackers.jira import Jira
from lazyagent.protocol import validation
from tests.engine.fakes import make_engine, worktree
from tests.engine.trackers.fake_jira import BASE_URL, FakeJira, fixture

NOW = 1_700_000_000.0
BRANCH = "SHOP-12-login-sso"
WT = "/tmp/repo-shop-12"

MAPPING = {
    "In Review": StatusRule("others", "code review"),
    "QA": StatusRule("others", "in QA"),
    "Reopened": StatusRule("you", "returned", "round"),
}


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-api-token")


def config(**options) -> TrackerConfig:
    options = {
        "base_url": BASE_URL, "email_env": "JIRA_EMAIL", "token_env": "JIRA_TOKEN", **options,
    }
    return TrackerConfig("jira", enabled=True, status=dict(MAPPING), options=options)


def adapter(fake: FakeJira, **options) -> Jira:
    return Jira(config(**options), "/tmp/repo", http=fake)


# --- Config ------------------------------------------------------------------


def test_registered_as_a_built_in_marked_experimental():
    assert adapter_factories()["jira"] is Jira
    assert Jira.experimental is True


def test_loaded_from_trackers_toml_and_logged_as_experimental(caplog):
    config_dir().mkdir(parents=True)
    (config_dir() / "trackers.toml").write_text(
        "[jira]\n"
        "enabled = true\n"
        f'base_url = "{BASE_URL}/"\n'
        'email_env = "JIRA_EMAIL"\n'
        'token_env = "JIRA_TOKEN"\n'
        'jql_assigned = "assignee = currentUser() AND project = SHOP"\n'
        "[jira.status]\n"
        '"Reopened" = { ball = "you", level = "round", state = "returned" }\n'
    )
    [cfg] = load_trackers("/tmp/repo")
    with caplog.at_level(logging.WARNING):
        hub = TrackerHub.from_configs([cfg], "/tmp/repo")
    [feed] = hub._feeds
    assert isinstance(feed.adapter, Jira)
    assert feed.adapter.base_url == BASE_URL
    assert feed.adapter.jql_assigned == "assignee = currentUser() AND project = SHOP"
    assert "jira is experimental" in caplog.text


@pytest.mark.parametrize(
    "url",
    [None, "", "http://example.atlassian.net", "ftp://example.atlassian.net", "https://"],
)
def test_base_url_must_be_https(url):
    options = {} if url is None else {"base_url": url}
    with pytest.raises(ValueError, match="base_url"):
        Jira(TrackerConfig("jira", True, options=options), "/tmp/repo")
    # The hub skips it rather than failing the engine.
    assert not TrackerHub.from_configs(
        [TrackerConfig("jira", True, options=options)], "/tmp/repo"
    ).enabled


def test_plain_http_only_on_loopback():
    a = Jira(TrackerConfig("jira", True, options={"base_url": "http://127.0.0.1:8080"}), "")
    assert a.base_url == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    "branch, key, projects",
    [
        ("SHOP-12-login", "SHOP-12", None),
        ("feature/SHOP-12", "SHOP-12", None),
        ("fix-login", None, None),
        ("UTF-8-cleanup", "UTF-8", None),
        ("UTF-8-cleanup", None, ["shop"]),
        ("UTF-8-SHOP-3", "SHOP-3", ["SHOP", "OPS"]),
        ("OPS-4", "OPS-4", ["SHOP", "OPS"]),
    ],
)
def test_the_key_is_the_issue_key_in_the_branch(branch, key, projects):
    options = {"projects": projects} if projects else {}
    assert branch_key(adapter(FakeJira(), **options).key_pattern(), branch) == key


# --- fetch -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_follows_next_page_token():
    fake = FakeJira("search_page1", "search_page2")
    tasks = await adapter(fake).fetch(["SHOP-3", "SHOP-1", "SHOP-2", "SHOP-1"])
    assert [t.key for t in tasks] == ["SHOP-1", "SHOP-2", "SHOP-3"]

    (path1, q1, h1), (path2, q2, _) = fake.requests
    assert path1 == path2 == "/rest/api/3/search/jql"
    assert q1 == {
        "jql": 'key in ("SHOP-1", "SHOP-2", "SHOP-3")',
        "fields": "summary,status,assignee,updated",
        "maxResults": "100",
    }
    assert q2 == {**q1, "nextPageToken": "CAEaAggD"}
    assert h1["Authorization"] == "Basic " + base64.b64encode(
        b"dev@example.com:fake-api-token"
    ).decode()
    assert h1["Accept"] == "application/json"

    first = tasks[0]
    assert first.url == f"{BASE_URL}/browse/SHOP-1"
    assert first.title == "Checkout total rounds wrong"
    assert first.status_raw == "In Progress"
    assert first.assignee == "Alex Doe"
    assert first.updated_at == datetime(2026, 10, 20, 10, tzinfo=timezone.utc).timestamp()
    assert tasks[1].assignee is None
    assert tasks[1].status_raw == "QA"


@pytest.mark.asyncio
async def test_fetch_stops_on_is_last_and_caps_pages(monkeypatch):
    # isLast true: no second request even with a token in the page.
    page = fixture("search_page1")
    page["body"]["isLast"] = True
    fake = FakeJira(page)
    assert len(await adapter(fake).fetch(["SHOP-1"])) == 2
    assert len(fake.requests) == 1

    monkeypatch.setattr(jira_mod, "MAX_PAGES", 2)
    fake = FakeJira("search_page1", "search_page1", "search_page1")
    await adapter(fake).fetch(["SHOP-1"])
    assert len(fake.requests) == 2


@pytest.mark.asyncio
async def test_fetch_batches_and_skips_what_is_not_a_key(monkeypatch):
    monkeypatch.setattr(jira_mod, "BATCH", 2)
    fake = FakeJira("search_page2", "search_page2")
    await adapter(fake).fetch(["SHOP-3", "SHOP-1", "SHOP-2", "shop-4", "123", 'X-1")'])
    assert [fake.jql(0), fake.jql(1)] == [
        'key in ("SHOP-1", "SHOP-2")', 'key in ("SHOP-3")',
    ]
    fake = FakeJira()
    assert await adapter(fake).fetch(["nothing"]) == []
    assert fake.requests == []


@pytest.mark.asyncio
async def test_fetch_only_asks_for_the_configured_projects():
    fake = FakeJira("search_page2")
    await adapter(fake, projects=["SHOP"]).fetch(["SHOP-3", "OPS-1"])
    assert fake.jql() == 'key in ("SHOP-3")'


@pytest.mark.asyncio
async def test_an_unknown_key_is_dropped_and_the_query_sent_again():
    fake = FakeJira("missing_key", "search_page2")
    tasks = await adapter(fake).fetch(["SHOP-3", "SHOP-999"])
    assert [t.key for t in tasks] == ["SHOP-3"]
    assert [fake.jql(0), fake.jql(1)] == ['key in ("SHOP-3", "SHOP-999")', 'key in ("SHOP-3")']

    # Only that key: nothing left to ask.
    fake = FakeJira("missing_key")
    assert await adapter(fake).fetch(["SHOP-999"]) == []
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_a_400_that_names_no_key_fails():
    fake = FakeJira("bad_jql")
    with pytest.raises(TrackerError, match="HTTP 400: Error in the JQL Query"):
        await adapter(fake).fetch(["SHOP-1"])
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_status_falls_back_to_its_category_when_only_that_is_mapped():
    fake = FakeJira("search_page1", "search_page2")
    a = adapter(fake)
    a.config.status = {"In Progress": StatusRule("agent", "in progress"), "QA": MAPPING["QA"]}
    tasks = {t.key: t.status_raw for t in await a.fetch(["SHOP-1", "SHOP-2", "SHOP-3"])}
    # "QA" is mapped by name; "Done" is mapped by neither: its own name.
    assert tasks == {"SHOP-1": "In Progress", "SHOP-2": "QA", "SHOP-3": "Done"}

    # An unmapped name ("Testing") whose category is mapped (ignoring case).
    page = fixture("search_page1")
    page["body"]["issues"][1]["fields"]["status"]["name"] = "Testing"
    page["body"]["isLast"] = True
    a = adapter(FakeJira(page))
    a.config.status = {"in progress": StatusRule("agent")}
    [_, testing] = await a.fetch(["SHOP-1", "SHOP-2"])
    assert testing.status_raw == "In Progress"


# --- Errors ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_raises_rate_limited_with_retry_after():
    with pytest.raises(RateLimited) as caught:
        await adapter(FakeJira("rate_limited")).fetch(["SHOP-1"])
    assert caught.value.retry_after == 120

    # An HTTP date, and a 503 that carries Retry-After.
    limited = fixture("rate_limited")
    limited["status"] = 503
    limited["headers"]["Retry-After"] = "Wed, 21 Oct 2099 07:28:00 GMT"
    with pytest.raises(RateLimited) as caught:
        await adapter(FakeJira(limited)).fetch(["SHOP-1"])
    assert caught.value.retry_after > 3600

    limited["headers"] = {}
    with pytest.raises(TrackerError) as caught:
        await adapter(FakeJira(limited)).fetch(["SHOP-1"])
    assert not isinstance(caught.value, RateLimited)


@pytest.mark.asyncio
async def test_an_auth_error_never_shows_the_credentials():
    with pytest.raises(TrackerError) as caught:
        await adapter(FakeJira("unauthorized")).fetch(["SHOP-1"])
    assert str(caught.value) == "jira: HTTP 401"
    assert "fake-api-token" not in repr(caught.value)


@pytest.mark.asyncio
async def test_no_credentials_no_request(monkeypatch):
    monkeypatch.delenv("JIRA_TOKEN")
    fake = FakeJira()
    with pytest.raises(TrackerError, match="no credentials"):
        await adapter(fake).fetch(["SHOP-1"])
    assert fake.requests == []

    # The email may be written in the config; the token never.
    monkeypatch.setenv("JIRA_TOKEN", "t")
    fake = FakeJira("search_page2")
    await adapter(fake, email="me@example.com", email_env=None).fetch(["SHOP-3"])
    assert fake.requests[0][2]["Authorization"] == "Basic " + base64.b64encode(
        b"me@example.com:t"
    ).decode()


@pytest.mark.asyncio
async def test_a_bad_answer_is_an_error():
    for body in ("<html>", {"nope": 1}, []):
        with pytest.raises(TrackerError, match="bad response"):
            await adapter(
                FakeJira({"status": 200, "headers": {}, "body": body})
            ).fetch(["SHOP-1"])


# --- assigned_to_me ----------------------------------------------------------


@pytest.mark.asyncio
async def test_assigned_to_me_runs_the_configured_jql_over_every_page():
    fake = FakeJira("assigned_page1", "assigned_page2")
    tasks = await adapter(fake).assigned_to_me()
    assert [t.key for t in tasks] == ["SHOP-12", "SHOP-40", "SHOP-41"]
    assert fake.jql(0) == "assignee = currentUser() AND statusCategory != Done"
    assert fake.requests[1][1]["nextPageToken"] == "CAEaAggE"

    fake = FakeJira("assigned_page2")
    await adapter(fake, jql_assigned="project = SHOP AND assignee = currentUser()").assigned_to_me()
    assert fake.jql() == "project = SHOP AND assignee = currentUser()"

    with pytest.raises(TrackerError, match="jql_assigned: HTTP 400"):
        await adapter(FakeJira("bad_jql")).assigned_to_me()


# --- Through the hub and the engine ---------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine(fake: FakeJira, **options):
    engine = make_engine(
        [
            worktree("/tmp/repo"),
            worktree(WT, BRANCH, is_main=False),
            worktree("/tmp/repo-feat", "feat", is_main=False),
        ]
    )
    clock = Clock()
    engine.status.clock = clock
    cfg = config(**options)
    cfg.assigned_to_me = False
    engine.trackers = TrackerHub([(Jira(cfg, "/tmp/repo", http=fake), cfg)], clock=clock)
    return engine, clock


def poll(engine, clock, seconds: float = 0.0):
    clock.now += seconds
    branches = [wt.branch for wt in engine.worktrees if wt.branch]
    return asyncio.run(engine.trackers.poll(branches))


def row(engine) -> dict:
    result = engine.status.list()
    validation.validate_result("status_list", result, strict=True)
    return {r["key"]: r for r in result["rows"]}[f"repo:{BRANCH}"]


def test_entering_a_returned_status_yields_one_round_item_and_leaving_clears_it():
    fake = FakeJira("issue_in_review")
    engine, clock = _engine(fake)
    poll(engine, clock)
    r = row(engine)
    assert (r["ball"], r["zone"], r["state"], r["title"]) == (
        "others", "in_flight", "code review", "Login fails with SSO",
    )
    assert r["tracker"] == {
        "adapter": "jira", "key": "SHOP-12", "url": f"{BASE_URL}/browse/SHOP-12",
        "status": "In Review", "state": "code review", "at": NOW, "stale": False,
    }
    assert fake.jql() == 'key in ("SHOP-12")'

    fake.then("issue_reopened")
    poll(engine, clock, 300)
    r = row(engine)
    assert (r["ball"], r["zone"], r["level"], r["state"]) == ("you", "inbox", "round", "returned")
    [item] = engine.actionables.list()["items"]
    assert (item["id"], item["kind"], item["level"], item["work_item"]) == (
        "tracker:jira:SHOP-12:Reopened", "tracker_status", "round", f"repo:{BRANCH}",
    )
    assert (item["label"], item["detail"]) == (
        "Login fails with SSO", f"{BASE_URL}/browse/SHOP-12",
    )

    # Still reopened on the next poll: the same one item.
    fake.then("issue_reopened")
    poll(engine, clock, 300)
    assert [i["id"] for i in engine.actionables.list()["items"]] == [item["id"]]
    assert engine.status.list()["counts"]["round"] == 1

    fake.then("issue_in_progress")
    poll(engine, clock, 300)
    r = row(engine)
    assert (r["zone"], r["tracker"]["status"]) == ("in_flight", "In Progress")
    assert engine.actionables.list()["items"] == []
    assert not engine.trackers.items


def test_a_429_backs_off_and_keeps_the_last_good_value():
    fake = FakeJira("issue_reopened", "rate_limited")
    engine, clock = _engine(fake)
    poll(engine, clock)
    poll(engine, clock, 300)
    [feed] = engine.trackers._feeds
    assert feed.backoff == 120 and feed.backoff_until == NOW + 300 + 120
    engine.status.clock.now = clock.now
    r = row(engine)
    assert (r["level"], r["tracker"]["status"], r["tracker"]["stale"], r["tracker"]["at"]) == (
        "round", "Reopened", True, NOW,
    )
    assert len(engine.actionables.list()["items"]) == 1

    # Inside the wait nothing is asked, even when a poll is due.
    poll(engine, clock, 60)
    assert len(fake.requests) == 2
    asyncio.run(engine.trackers.poll([BRANCH], force=True))
    assert len(fake.requests) == 2

    # After it, the next answer clears the stale mark.
    fake.then("issue_reopened")
    poll(engine, clock, 300)
    assert len(fake.requests) == 3
    assert row(engine)["tracker"]["stale"] is False


def test_an_auth_error_keeps_the_last_good_value_and_logs_no_secret(caplog):
    fake = FakeJira("issue_in_review", "unauthorized")
    engine, clock = _engine(fake)
    poll(engine, clock)
    with caplog.at_level(logging.DEBUG):
        poll(engine, clock, 300)
    r = row(engine)
    assert (r["tracker"]["status"], r["tracker"]["stale"]) == ("In Review", True)
    assert "jira: HTTP 401" in caplog.text
    assert "fake-api-token" not in caplog.text
    assert base64.b64encode(b"dev@example.com:fake-api-token").decode() not in caplog.text


def test_with_jira_configured_but_off_status_list_is_byte_identical():
    plain = make_engine([worktree("/tmp/repo"), worktree(WT, BRANCH, is_main=False)])
    config_dir().mkdir(parents=True)
    (config_dir() / "trackers.toml").write_text(
        f'[jira]\nenabled = false\nbase_url = "{BASE_URL}"\n'
    )
    off = make_engine([worktree("/tmp/repo"), worktree(WT, BRANCH, is_main=False)])
    off._tracker_loader = load_trackers
    off.load_config()
    assert not off.trackers.enabled

    def dump(engine):
        engine.status.clock = Clock()
        return json.dumps(engine.status.list(include_done=True), sort_keys=True)

    assert dump(plain) == dump(off)
    assert "tracker" not in dump(off)


# --- The default transport ---------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/rest/api/3/search/jql"):
            body = json.dumps(fixture("search_page2")["body"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.mark.asyncio
async def test_the_default_transport_speaks_http_and_never_follows_redirects():
    """Through urllib, against a server on loopback (no network)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        cfg = config(base_url=base)
        tasks = await Jira(cfg, "/tmp/repo").fetch(["SHOP-3"])
        assert [(t.key, t.status_raw) for t in tasks] == [("SHOP-3", "Done")]
        path, auth = _Handler.seen[-1]
        assert path.startswith("/rest/api/3/search/jql?jql=key+in+")
        assert auth and auth.startswith("Basic ")

        moved = Jira(config(base_url=base + "/moved"), "/tmp/repo")
        with pytest.raises(TrackerError, match="HTTP 302"):
            await moved.fetch(["SHOP-3"])
    finally:
        server.shutdown()
        server.server_close()

    # Nothing listening: an error, not a crash.
    with pytest.raises(TrackerError, match="jira:"):
        await Jira(config(base_url=base), "/tmp/repo").fetch(["SHOP-3"])
