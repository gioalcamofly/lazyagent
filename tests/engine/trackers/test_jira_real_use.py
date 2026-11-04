"""The Jira adapter for real use, against recorded Jira Cloud responses and a
fake transport (no network): a token with scopes through Atlassian's gateway,
pagination without ``isLast``, the newer rate-limit headers, branch keys in
any case and after any prefix, a branch carrying both a GitHub issue number
and a Jira key, feeders sending Jira links or bare keys, the issue link on
``lazyagent status --links``, and ``lazyagent trackers check``."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

from lazyagent.engine import cli as engine_cli
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.status import format_table, row_links
from lazyagent.engine.trackers import (
    RateLimited,
    StatusRule,
    TrackerConfig,
    TrackerError,
    TrackerHub,
    TrackerTask,
    branch_key,
    config_dir,
)
from lazyagent.engine.trackers import jira as jira_mod
from lazyagent.engine.trackers.github_issues import GithubIssues
from lazyagent.engine.trackers.jira import Jira, check
from tests.engine.fakes import make_engine, worktree
from tests.engine.trackers.fake_gh import FakeGh
from tests.engine.trackers.fake_jira import (
    BASE_URL,
    CLOUD_ID,
    GATEWAY,
    FakeJira,
    fixture,
)

NOW = 1_700_000_000.0
TOKEN = "fake-api-token-do-not-print"


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_TOKEN", TOKEN)


def config(status=None, qa=None, **options) -> TrackerConfig:
    options = {
        "base_url": BASE_URL, "email_env": "JIRA_EMAIL", "token_env": "JIRA_TOKEN", **options,
    }
    options = {k: v for k, v in options.items() if v is not None}
    return TrackerConfig(
        "jira", enabled=True, status=dict(status or {}), qa=dict(qa or {}), options=options
    )


def adapter(fake, **options) -> Jira:
    return Jira(config(**options), "/tmp/repo", http=fake)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


# --- A token with scopes: Atlassian's gateway ---------------------------------


@pytest.mark.asyncio
async def test_cloud_id_sends_calls_through_the_gateway_and_links_to_the_site():
    fake = FakeJira("search_page2")
    a = adapter(fake, cloud_id=CLOUD_ID)
    [task] = await a.fetch(["SHOP-3"])
    assert fake.roots == [GATEWAY]
    assert fake.paths() == [f"/ex/jira/{CLOUD_ID}/rest/api/3/search/jql"]
    assert task.url == f"{BASE_URL}/browse/SHOP-3"


@pytest.mark.parametrize("cloud_id", ["../evil", "a b", 42, "x" * 65])
def test_a_bad_cloud_id_is_refused(cloud_id):
    with pytest.raises(ValueError, match="cloud_id"):
        adapter(FakeJira(), cloud_id=cloud_id)


# --- Pagination ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_without_is_last_still_pages_and_no_token_stops():
    page = fixture("search_page1")
    del page["body"]["isLast"]
    last = fixture("search_page2")
    del last["body"]["isLast"]
    fake = FakeJira(page, last)
    tasks = await adapter(fake).fetch(["SHOP-1", "SHOP-2", "SHOP-3"])
    assert [t.key for t in tasks] == ["SHOP-1", "SHOP-2", "SHOP-3"]
    assert len(fake.requests) == 2


# --- Rate limits and errors ---------------------------------------------------


@pytest.mark.asyncio
async def test_429_with_only_x_ratelimit_reset_waits_until_then(monkeypatch):
    reset = datetime.fromisoformat("2026-10-22T16:02:00+00:00").timestamp()
    monkeypatch.setattr(time, "time", lambda: reset - 42)
    with pytest.raises(RateLimited) as caught:
        await adapter(FakeJira("rate_limited_reset")).fetch(["SHOP-1"])
    assert caught.value.retry_after == pytest.approx(42)
    assert str(caught.value) == "jira: HTTP 429 (jira-burst-based)"

    # Retry-After wins over the reset time.
    with pytest.raises(RateLimited) as caught:
        await adapter(FakeJira("rate_limited")).fetch(["SHOP-1"])
    assert caught.value.retry_after == 120
    assert "jira-quota-tenant-based" in str(caught.value)


@pytest.mark.asyncio
async def test_error_bodies_and_hints():
    with pytest.raises(TrackerError) as caught:
        await adapter(FakeJira("forbidden")).fetch(["SHOP-1"])
    assert str(caught.value) == "jira: HTTP 403: Unauthorized; scope does not match"
    no_body = {"status": 403, "headers": {}, "body": ""}
    with pytest.raises(TrackerError, match="read:jira-work"):
        await adapter(FakeJira(no_body)).fetch(["SHOP-1"])
    with pytest.raises(TrackerError, match="No project could be found"):
        await adapter(FakeJira("not_found")).fetch(["SHOP-1"])


# --- Branch → key -------------------------------------------------------------


@pytest.mark.parametrize(
    "branch, key, projects",
    [
        ("ABC-123", "ABC-123", None),
        ("feat/ABC-123-x", "ABC-123", None),
        ("feat_ABC-123-x", "ABC-123", None),
        ("abc-123-fix", None, None),           # lower case: only with projects
        ("fix-123", None, None),
        ("abc-123-fix", "ABC-123", ["ABC"]),
        ("feat/abc-123-x", "ABC-123", ["abc"]),
        ("feat_Abc-123", "ABC-123", ["ABC"]),
        ("xabc-123", None, ["ABC"]),            # not a word of its own
        ("abc-1234", "ABC-1234", ["ABC"]),
        ("MY_PROJ-7-x", "MY_PROJ-7", ["MY_PROJ"]),
        ("fix-123", None, ["ABC"]),
    ],
)
def test_branch_keys_in_any_case_after_any_prefix(branch, key, projects):
    a = adapter(FakeJira(), projects=projects)
    found = branch_key(a.key_pattern(), branch)
    assert (a.normalize_key(found) if found else None) == key


@pytest.mark.parametrize("projects", ["SHOP", ["SHOP", 3], ["SH OP"]])
def test_bad_projects_are_refused(projects):
    with pytest.raises(ValueError, match="projects"):
        adapter(FakeJira(), projects=projects)


def _hub(*adapters) -> TrackerHub:
    return TrackerHub([(a, a.config) for a in adapters], clock=Clock())


def test_a_lower_case_branch_fetches_the_upper_case_key_and_shows_its_link():
    fake = FakeJira("issue_reopened")
    engine = make_engine([worktree("/tmp/repo"),
                          worktree("/tmp/repo-x", "feat/shop-12-sso", is_main=False)])
    engine.status.clock = Clock()
    cfg = config(status={"Reopened": StatusRule("you", "returned", "round")},
                 projects=["SHOP"])
    cfg.assigned_to_me = False
    engine.trackers = TrackerHub([(Jira(cfg, "/tmp/repo", http=fake), cfg)], clock=Clock())
    asyncio.run(engine.trackers.poll(["feat/shop-12-sso"]))
    assert fake.jql() == 'key in ("SHOP-12")'
    row = {r["key"]: r for r in engine.status.list()["rows"]}["repo:feat/shop-12-sso"]
    assert row["tracker"]["key"] == "SHOP-12"
    assert row["tracker"]["url"] == f"{BASE_URL}/browse/SHOP-12"
    assert f"{BASE_URL}/browse/SHOP-12" in row["urls"]


def test_a_branch_with_a_github_issue_number_and_a_jira_key_links_both():
    gh = GithubIssues(TrackerConfig("github_issues", enabled=True), "/tmp/repo", run=FakeGh())
    jira = adapter(FakeJira())
    branch = "SHOP-12-fix-123"
    assert branch_key(gh.key_pattern(), branch) == "123"
    assert branch_key(jira.key_pattern(), branch) == "SHOP-12"
    assert branch_key(gh.key_pattern(), "feat/SHOP-12") is None
    hub = _hub(gh, jira)
    assert [(f.name, k) for f, k in hub.links(branch)] == [
        ("github_issues", "123"), ("jira", "SHOP-12"),
    ]


def test_key_of_url_and_url_for():
    a = adapter(FakeJira(), projects=["SHOP"])
    assert a.key_of_url(f"{BASE_URL}/browse/SHOP-12") == "SHOP-12"
    assert a.key_of_url(f"{BASE_URL}/browse/shop-12?focusedCommentId=1") == "SHOP-12"
    assert a.key_of_url(
        f"{BASE_URL}/jira/software/projects/SHOP/boards/1?selectedIssue=SHOP-7"
    ) == "SHOP-7"
    assert a.key_of_url(f"{BASE_URL}/browse/OPS-1") is None          # not a project of ours
    assert a.key_of_url("https://other.example.com/browse/SHOP-12") is None
    assert a.url_for("shop-12") == f"{BASE_URL}/browse/SHOP-12"
    assert a.url_for("OPS-1") is None
    assert a.url_for("nope") is None


# --- Feeders: Jira links and bare keys ----------------------------------------


async def _feeder_engine(branch: str, **options):
    engine = make_engine([worktree("/tmp/repo"), worktree("/tmp/repo-x", branch, is_main=False)])
    engine.status.clock = Clock()
    cfg = config(projects=["SHOP"], **options)
    cfg.assigned_to_me = False
    engine.trackers = TrackerHub(
        [(Jira(cfg, "/tmp/repo", http=FakeJira("issue_in_review")), cfg)], clock=Clock()
    )
    await engine.trackers.poll([branch])
    return engine


def _row(engine, branch):
    return {r["key"]: r for r in engine.status.list()["rows"]}[f"repo:{branch}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [
    {"url": f"{BASE_URL}/browse/SHOP-12"},
    {"url": f"{BASE_URL}/browse/shop-12"},
    {"url": "SHOP-12"},
    {"url": "shop-12"},
    {"url": "https://planner.example.com/card/77", "ticket": "SHOP-12"},
    {"url": "https://planner.example.com/card/77", "ticket": "shop-12"},
])
async def test_a_jira_link_or_key_from_a_feeder_joins_the_adapter_row(given):
    branch = "feat/shop-12-sso"
    engine = await _feeder_engine(branch)
    result = await EngineMethods(engine).work_items_upsert(
        {"title": "SSO login", "source": "planner", "priority": "high", **given}
    )
    if given["url"].lower() == "shop-12":
        assert result["item"]["url"] == f"{BASE_URL}/browse/SHOP-12"
    row = _row(engine, branch)
    assert row["priority"] == "high"
    assert row["tracker"]["key"] == "SHOP-12"
    assert engine.status.not_started_items() == []
    assert engine.status.list()["counts"].get("not_started", 0) == 0


@pytest.mark.asyncio
async def test_a_bare_key_needs_an_adapter_that_reads_it():
    engine = make_engine([worktree("/tmp/repo")])
    with pytest.raises(Exception, match="http"):
        await EngineMethods(engine).work_items_upsert(
            {"url": "SHOP-12", "title": "x", "source": "planner"}
        )
    engine = await _feeder_engine("main-ish")
    with pytest.raises(Exception, match="http"):
        await EngineMethods(engine).work_items_upsert(
            {"url": "OPS-1", "title": "x", "source": "planner"}
        )
    # Closing by the bare key closes the link it stood for.
    await EngineMethods(engine).work_items_upsert(
        {"url": "SHOP-12", "title": "x", "source": "planner"}
    )
    closed = await EngineMethods(engine).work_items_close({"url": "shop-12"})
    assert closed["closed"] is True


@pytest.mark.asyncio
async def test_a_fed_task_and_the_same_assigned_issue_count_once():
    engine = make_engine([worktree("/tmp/repo")])
    engine.status.clock = Clock()
    cfg = config(projects=["SHOP"])
    jira = Jira(cfg, "/tmp/repo", http=FakeJira())
    hub = TrackerHub([(jira, cfg)], clock=Clock())
    hub._feeds[0].assigned = [
        TrackerTask("SHOP-12", f"{BASE_URL}/browse/SHOP-12", "SSO", "In Review"),
        TrackerTask("SHOP-41", f"{BASE_URL}/browse/SHOP-41", "Shipping", "To Do"),
    ]
    engine.trackers = hub
    await EngineMethods(engine).work_items_upsert(
        {"url": "https://planner.example.com/card/77", "ticket": "SHOP-12",
         "title": "SSO login", "source": "planner"}
    )
    items = engine.status.not_started_items()
    assert sorted(i["url"] for i in items) == [
        f"{BASE_URL}/browse/SHOP-41", "https://planner.example.com/card/77",
    ]


# --- lazyagent status --links ---------------------------------------------------


def test_status_links_name_the_pr_and_the_task():
    row = {
        "urls": ["https://github.com/o/r/pull/9", f"{BASE_URL}/browse/SHOP-12"],
        "pr": {"number": 9}, "tracker": {"key": "SHOP-12", "url": f"{BASE_URL}/browse/SHOP-12"},
    }
    assert row_links(row) == [
        "PR #9 https://github.com/o/r/pull/9", f"SHOP-12 {BASE_URL}/browse/SHOP-12",
    ]
    engine = asyncio.run(_feeder_engine("SHOP-12-sso"))
    result = engine.status.list()
    text = format_table(result, links=True)
    assert f"    SHOP-12 {BASE_URL}/browse/SHOP-12" in text.splitlines()
    assert "/browse/" not in format_table(result)


# --- lazyagent trackers check ---------------------------------------------------

STATUS = {
    "In Review": StatusRule("others", "code review"),
    "Reopened": StatusRule("you", "returned", "round"),
    "Typo Status": StatusRule("others", "x"),
}


def _check(fake, **options) -> tuple[bool, list[tuple[str, str]]]:
    said: list[tuple[str, str]] = []
    cfg = config(status=STATUS, qa={"QA passed": "tested"}, **options)
    if "qa_field" in options:
        cfg.qa_field = options.pop("qa_field")
        cfg.options.pop("qa_field")
    good = asyncio.run(check(cfg, http=fake, say=lambda level, text: said.append((level, text))))
    return good, said


def _text(said) -> str:
    return "\n".join(f"{level} {text}" for level, text in said)


def test_check_walks_myself_one_search_and_the_status_names():
    fake = FakeJira("tenant_info", "myself", "assigned_page1", "project_statuses")
    good, said = _check(fake, projects=["SHOP"])
    text = _text(said)
    assert good, text
    assert fake.paths() == [
        "/_edge/tenant_info", "/rest/api/3/myself", "/rest/api/3/search/jql",
        "/rest/api/3/project/SHOP/statuses",
    ]
    # tenant_info is public: no credential sent there.
    assert "Authorization" not in fake.requests[0][2]
    assert fake.requests[2][1]["maxResults"] == "5"
    assert "signed in as Alex Doe" in text
    assert f'cloud_id = "{CLOUD_ID}"' in text
    assert "jql_assigned: 2 issue(s) and more: SHOP-12 [In Review], SHOP-40 [To Do]" in text
    assert "  In Review · In Progress   ← [jira.status]" in text
    assert "  QA passed · In Progress   ← [jira.qa]" in text
    assert "  Reopened · To Do   ← [jira.status]" in text
    assert "warn [jira.status] 'Typo Status' is not a status" in text
    assert "token from $JIRA_TOKEN (not shown)" in text
    assert TOKEN not in text


def test_check_without_projects_lists_every_status_and_checks_the_qa_field():
    fake = FakeJira("tenant_info", "myself", "search_page2", "statuses_all", "fields")
    good, said = _check(fake, qa_field="customfield_10042")
    assert good, _text(said)
    assert fake.paths()[-2:] == ["/rest/api/3/status", "/rest/api/3/field"]
    assert "ok qa_field customfield_10042: QA state" in _text(said)

    fake = FakeJira("tenant_info", "myself", "search_page2", "statuses_all", "fields")
    good, said = _check(fake, qa_field="customfield_99999")
    assert not good
    assert "qa_field 'customfield_99999' is not a field" in _text(said)


def test_check_with_a_scoped_token_uses_the_gateway_and_checks_the_cloud_id():
    fake = FakeJira("tenant_info", "myself", "search_page2", "statuses_all")
    good, said = _check(fake, cloud_id=CLOUD_ID)
    assert good, _text(said)
    assert fake.roots == [BASE_URL, GATEWAY, GATEWAY, GATEWAY]

    fake = FakeJira("tenant_info", "myself", "search_page2", "statuses_all")
    good, said = _check(fake, cloud_id="99999999-2222-4333-8444-555555555555")
    assert not good
    assert "is not this site's" in _text(said)


def test_check_stops_on_a_401_with_a_hint_and_no_secret():
    fake = FakeJira("tenant_info", "unauthorized")
    good, said = _check(fake)
    text = _text(said)
    assert not good
    assert "fail /myself: jira: HTTP 401: the email or API token is wrong" in text
    assert len(fake.requests) == 2
    assert TOKEN not in text


def test_check_reports_a_bad_jql_and_carries_on():
    fake = FakeJira("tenant_info", "myself", "bad_jql", "statuses_all")
    good, said = _check(fake, jql_assigned="assignee = currentUser() AND summary ~ %")
    assert not good
    assert "reserved JQL character" in _text(said)
    assert fake.paths()[-1] == "/rest/api/3/status"


def test_check_without_credentials_or_with_a_token_in_the_file(monkeypatch):
    monkeypatch.delenv("JIRA_TOKEN")
    fake = FakeJira()
    good, said = _check(fake)
    assert not good and fake.requests == []
    assert "no API token" in _text(said)

    monkeypatch.setenv("JIRA_TOKEN", TOKEN)
    good, said = _check(FakeJira("tenant_info", "myself", "search_page2", "statuses_all"),
                        token="pasted-secret", tokn_env="X")
    text = _text(said)
    assert not good
    assert "token = … is never read" in text
    assert "warn [jira] tokn_env is not a key lazyagent reads" in text
    assert "pasted-secret" not in text


def test_trackers_check_command(monkeypatch, capsys, tmp_path):
    fake = FakeJira("tenant_info", "myself", "assigned_page1", "project_statuses")
    monkeypatch.setattr(jira_mod, "_urllib_http", lambda: fake)
    monkeypatch.chdir(tmp_path)
    assert engine_cli.trackers_main(["check"]) == 1
    assert "no " in capsys.readouterr().out

    config_dir().mkdir(parents=True)
    path = config_dir() / "trackers.toml"
    path.write_text("[jira\nenabled = true\n")
    assert engine_cli.trackers_main(["check", "jira"]) == 1
    assert "FAIL" in capsys.readouterr().out

    path.write_text(
        "[jira]\n"
        "enabled = true\n"
        f'base_url = "{BASE_URL}"\n'
        'email_env = "JIRA_EMAIL"\n'
        'token_env = "JIRA_TOKEN"\n'
        'projects = ["SHOP"]\n'
        "[jira.status]\n"
        '"In Review" = { ball = "others", state = "code review" }\n'
    )
    assert engine_cli.trackers_main(["check", "jira"]) == 0
    out = capsys.readouterr().out
    assert "ok   " in out and "signed in as Alex Doe" in out
    assert "jira is experimental" in out
    assert "lazyagent engine restart" in out
    assert TOKEN not in out
    assert engine_cli.trackers_main(["check", "github"]) == 1
    assert "no [github] table" in capsys.readouterr().out
    assert engine_cli.trackers_main([]) == 2


def test_trackers_is_a_top_level_subcommand(monkeypatch):
    from lazyagent import cli

    seen = []
    monkeypatch.setattr(engine_cli, "trackers_main", lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as done:
        cli.main(["trackers", "check", "jira"])
    assert done.value.code == 0 and seen == [["check", "jira"]]
