"""The GitHub Issues adapter against fake ``gh`` output: keys from branch
names, batched fetch, status from labels or state, assigned issues with
ETags, rate limits."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from lazyagent.engine.trackers import (
    RateLimited,
    StatusRule,
    TrackerConfig,
    TrackerError,
    branch_key,
)
from lazyagent.engine.trackers import github_issues as gi
from lazyagent.engine.trackers.github_issues import GithubIssues, split_response
from tests.engine.trackers.fake_gh import FakeGh, issue

MAPPING = {
    "needs-review": StatusRule("others", "code review"),
    "qa": StatusRule("others", "in QA"),
    "qa-returned": StatusRule("you", "returned by QA", "round"),
}


def adapter(gh: FakeGh, **options) -> GithubIssues:
    config = TrackerConfig("github_issues", enabled=True, status=dict(MAPPING), options=options)
    return GithubIssues(config, "/tmp/repo", run=gh)


@pytest.mark.parametrize(
    "branch, key",
    [
        ("fix-123-login", "123"),
        ("123-login", "123"),
        ("feature/123", "123"),
        ("feature/#45-x", "45"),
        ("issue_7", "7"),
        ("fix-12.3", "12"),
        ("fix-login", None),
        ("v2-cleanup", None),
        ("release2026", None),
    ],
)
def test_the_key_is_the_issue_number_in_the_branch(branch, key):
    assert branch_key(adapter(FakeGh()).key_pattern(), branch) == key


@pytest.mark.asyncio
async def test_fetch_reads_the_status_from_the_first_mapped_label():
    gh = FakeGh(
        issue(1, labels=("bug", "qa")),
        issue(2, labels=("qa", "qa-returned")),
        issue(7, labels=("qa", "needs-review")),
        issue(3, labels=("bug",)),
        issue(4, state="CLOSED"),
        issue(5, labels=("QA",), assignees=("octo",)),
    )
    gh.pulls.add(6)
    tasks = {t.key: t for t in await adapter(gh).fetch(["1", "2", "3", "4", "5", "6", "7", "99"])}
    # A level first, then mapping order (not label order); no PR and no
    # missing issue.
    assert {k: t.status_raw for k, t in tasks.items()} == {
        "1": "qa", "2": "qa-returned", "3": "", "4": "closed", "5": "qa",
        "7": "needs-review",
    }
    assert tasks["5"].assignee == "octo"
    assert tasks["1"].url == "https://github.com/acme/widgets/issues/1"
    assert tasks["1"].title == "Issue 1"
    assert tasks["1"].updated_at > 0
    assert gh.calls_to("graphql") == 1
    # The repository is the one gh resolves in the repo root.
    assert ["-F", "owner={owner}"] == gh.calls[0][4:6]


@pytest.mark.asyncio
async def test_fetch_batches_and_ignores_non_numbers(monkeypatch):
    monkeypatch.setattr(gi, "BATCH", 2)
    gh = FakeGh(*(issue(n) for n in range(1, 6)))
    tasks = await adapter(gh).fetch(["5", "4", "3", "2", "1", "ABC-1"])
    assert sorted(t.key for t in tasks) == ["1", "2", "3", "4", "5"]
    assert gh.calls_to("graphql") == 3


@pytest.mark.asyncio
async def test_status_from_state():
    gh = FakeGh(issue(1, labels=("qa",)), issue(2, state="CLOSED"))
    tasks = await adapter(gh, status_from="state").fetch(["1", "2"])
    assert [t.status_raw for t in tasks] == ["open", "closed"]


@pytest.mark.asyncio
async def test_fetch_failures():
    gh = FakeGh(issue(1))
    gh.graphql_error = "gh: HTTP 401: Bad credentials"
    with pytest.raises(TrackerError, match="Bad credentials"):
        await adapter(gh).fetch(["1"])
    gh.graphql_error = "gh: HTTP 403: You have exceeded a secondary rate limit"
    with pytest.raises(RateLimited):
        await adapter(gh).fetch(["1"])


@pytest.mark.asyncio
async def test_assigned_to_me_uses_the_etag():
    gh = FakeGh(
        issue(1, assignees=("octo",)),
        issue(2, assignees=("someone",)),
        issue(3, assignees=("octo",), state="CLOSED"),
        issue(4, assignees=("octo", "someone"), labels=("qa",)),
    )
    gh.pulls.add(4)
    a = adapter(gh)
    assert [t.key for t in await a.assigned_to_me()] == ["1"]
    assert gh.calls_to("-i user") == 1

    # Unchanged: a 304, the same answer, the login not asked again.
    assert [t.key for t in await a.assigned_to_me()] == ["1"]
    assert gh.calls[-1][:2] == ["api", "-H"]
    assert gh.calls_to("-i user") == 1

    gh.set(2, assignees=["octo"])
    assert [t.key for t in await a.assigned_to_me()] == ["1", "2"]


@pytest.mark.asyncio
async def test_assigned_to_me_rate_limits():
    gh = FakeGh(issue(1, assignees=("octo",)))
    a = adapter(gh)
    await a.assigned_to_me()
    gh.rest_error = (429, {"Retry-After": "120"})
    with pytest.raises(RateLimited) as caught:
        await a.assigned_to_me()
    assert caught.value.retry_after == 120
    gh.rest_error = (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"})
    with pytest.raises(RateLimited):
        await a.assigned_to_me()
    gh.rest_error = (500, {})
    with pytest.raises(TrackerError, match="HTTP 500"):
        await a.assigned_to_me()


def test_split_response():
    assert split_response("HTTP/2.0 304 Not Modified\nEtag: x\n\n") == (304, {"etag": "x"}, "")
    assert split_response("garbage")[0] == 0


@pytest.mark.asyncio
async def test_the_default_runner_calls_gh_on_path(tmp_path, monkeypatch):
    """Through a real subprocess: a ``gh`` shim that prints a recorded answer."""
    shim = tmp_path / "bin" / "gh"
    shim.parent.mkdir()
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "with open(sys.argv[0] + '.args', 'w') as f: json.dump(sys.argv[1:], f)\n"
        "print(json.dumps({'data': {'repository': {'i7': {'number': 7, 'url': 'u',"
        " 'title': 't', 'state': 'OPEN', 'updatedAt': None,"
        " 'labels': {'nodes': [{'name': 'qa'}]}, 'assignees': {'nodes': []}}}}}))\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ['PATH']}")
    config = TrackerConfig("github_issues", enabled=True, status=dict(MAPPING))
    tasks = await GithubIssues(config, str(tmp_path)).fetch(["7"])
    assert [(t.key, t.status_raw) for t in tasks] == [("7", "qa")]
