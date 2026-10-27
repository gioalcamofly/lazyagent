"""What to review on "new commits since your approval" (docs/design/inbox-plan.md
I4j, engine/review_commits.py): the commit list with real temporary git repos
(after the approval, after ready, the PR's own; clean base merges listed but
not counted; a force push), where the review starts, the cap, the compare
link, ``PrInfo.ready_sha`` from the timeline, and the row field
``review_commits`` end to end. Also the opt-in fields of a pass: a row whose
fields alone changed is announced while a client reads them. No network;
HOME and the XDG dirs are under tmp_path (tests/conftest.py)."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.engine.base_merges import CLEAN, BaseMerges
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.owner_approvals import ApprovalFact
from lazyagent.engine.review_commits import RETRY_AFTER, compare_url, payload, start_of
from lazyagent.worktree_manager import _commits_after_ready

from tests.engine.rules.test_review_facts import readme_recipe, ready_at, timeline
from tests.engine.test_base_merges import OLD, Repo
from tests.engine.test_owner_approvals import (
    HEAD,
    KEY,
    NEXT,
    NOW,
    READY_T,
    _engine,
    _pr,
    _rule_row,
    _set_pr,
)
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path)


# --- the list, with real git ---------------------------------------------------------


def test_the_commits_after_a_point_newest_first_base_merges_listed_not_counted(repo):
    merges = BaseMerges(version=OLD, fetch=False)
    root = str(repo.root)
    approved = repo.feature()
    assert merges.commit_list(root, approved, approved, "main") == []
    merged = repo.merge()
    repo.edit("other.txt", "other\n", "a fix\n")
    fixed = repo.commit("a fix")
    listed = merges.commit_list(root, approved, fixed, "main")
    assert [(c["sha"], c["subject"], c["base_merge"]) for c in listed] == [
        (fixed, "a fix", None), (merged, "Merge remote-tracking branch 'origin/main' into feat",
                                 CLEAN)]
    assert listed[0]["author"] == "a" and listed[0]["date"].startswith("20")
    # The base's own commits the merge brought are not listed.
    assert "main moves" not in {c["subject"] for c in listed}


def test_the_prs_own_commits_from_its_base(repo):
    merges = BaseMerges(version=OLD, fetch=False)
    first = repo.feature()
    repo.merge()
    listed = merges.commit_list(str(repo.root), None, repo.git("rev-parse", "HEAD"), "main")
    assert [c["base_merge"] for c in listed] == [CLEAN, None]
    assert listed[-1]["sha"] == first


def test_a_point_a_force_push_dropped_is_none(repo):
    merges = BaseMerges(version=OLD, fetch=False)
    approved = repo.feature()
    repo.edit("other.txt", "other\n", "amended\n")
    repo.git("commit", "-q", "-a", "--amend", "--no-edit")
    head = repo.git("rev-parse", "HEAD")
    assert merges.commit_list(str(repo.root), approved, head, "main") is None
    assert merges.commit_list(str(repo.root), "not a sha", head, "main") is None


# --- where it starts, the cap, the link ----------------------------------------------


def _fact(state: str, at: float = NOW) -> ApprovalFact:
    return ApprovalFact(HEAD, at, "home", state)


def test_the_review_starts_at_the_newer_of_the_approval_and_the_ready_event():
    ready = _pr(head=NEXT, after=2, ready_sha="r" * 40)
    assert start_of(ready, _fact("counted")) == ("approval", HEAD)
    assert start_of(ready, _fact("counted", at=READY_T - 60)) == ("ready", "r" * 40)
    assert start_of(ready, None) == ("ready", "r" * 40)
    # A lost approval: from the ready event.
    assert start_of(ready, _fact("lost")) == ("ready", "r" * 40)
    # Opened ready (no event): the PR's own commits.
    assert start_of(_pr(head=NEXT, after=2), None) == ("opened", None)


def _commits(n: int, merges: int = 0) -> list[dict]:
    return [{"sha": f"{i:040x}", "subject": f"c{i}", "author": "a", "date": "",
             "base_merge": CLEAN if i < merges else None} for i in range(n)]


def test_the_list_is_capped_and_counts_only_real_commits():
    pr = _pr(head=NEXT, url=URL)
    out = payload(pr, "approval", HEAD, _commits(20, merges=2))
    assert (out["count"], out["base_merges"], len(out["commits"]), out["more"]) == (18, 2, 15, 5)
    assert out["compare_url"] == f"https://github.com/o/r/compare/{HEAD}...{NEXT}"
    opened = payload(pr, "opened", None, _commits(3))
    assert (opened["from"], opened["more"]) == ("main", 0)
    assert opened["compare_url"] == f"https://github.com/o/r/compare/main...{NEXT}"


def test_no_pr_url_no_link():
    assert compare_url("", "a", "b") is None
    assert compare_url("https://github.com/o/r", "a", "b") is None


# --- the ready head, from the timeline -----------------------------------------------


def _commit(t: str, oid: str) -> dict:
    return {"__typename": "PullRequestCommit", "commit": {"committedDate": t, "oid": oid}}


def test_the_ready_head_is_the_last_commit_or_push_before_the_last_event():
    node = timeline(_commit("2026-10-01T09:00:00Z", "a" * 40),
                    {"__typename": "HeadRefForcePushedEvent", "createdAt": "2026-10-01T09:30:00Z",
                     "beforeCommit": {"oid": "a" * 40}, "afterCommit": {"oid": "b" * 40}},
                    ready_at("2026-10-01T10:00:00Z"), _commit("2026-10-01T11:00:00Z", "c" * 40))
    assert _commits_after_ready(node).ready_sha == "b" * 40
    # Opened ready: no event, no ready head.
    assert _commits_after_ready(timeline(_commit("2026-10-01T11:00:00Z", "c" * 40))).ready_sha == ""


# --- the row field, end to end -------------------------------------------------------


class _Lists:
    """BaseMerges with canned commit lists and counts."""

    def __init__(self, lists):
        self.lists = lists
        self.calls = []

    def commits_since(self, root, since, head, base):
        return None

    def commit_list(self, root, since, head, base):
        self.calls.append((since, head))
        return self.lists.get((since, head))


async def _settled(engine, key: str = KEY) -> dict:
    """The row's fields once the background list is read."""
    for _ in range(100):
        engine.status.fields_of(key)
        await asyncio.sleep(0.01)
        if not engine.review_commits._running:
            break
    return engine.status.fields_of(key)


@pytest.mark.asyncio
async def test_review_commits_on_a_review_line_since_the_ready_event():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=NEXT, after=1, ready_sha=HEAD, url=URL))
        engine.rules.reload()
        lists = _Lists({(HEAD, NEXT): _commits(1)})
        engine.worktree_service.base_merges = lists
        await _rule_row(engine)
        fields = await _settled(engine)
        assert fields["approvable"]["sha"] == NEXT
        rc = fields["review_commits"]
        assert (rc["since"], rc["from"], rc["head"], rc["count"]) == ("ready", HEAD, NEXT, 1)
        assert rc["compare_url"].endswith(f"/compare/{HEAD}...{NEXT}")
        # Read once per (from, head).
        await _settled(engine)
        assert lists.calls == [(HEAD, NEXT)]


@pytest.mark.asyncio
async def test_review_commits_since_the_pr_was_opened():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=NEXT, after=3, url=URL))
        engine.rules.reload()
        engine.worktree_service.base_merges = _Lists({(None, NEXT): _commits(3)})
        await _rule_row(engine)
        rc = (await _settled(engine))["review_commits"]
        assert (rc["since"], rc["from"], rc["count"]) == ("opened", "main", 3)


@pytest.mark.asyncio
async def test_review_commits_after_the_approval_and_after_a_force_push():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=HEAD, after=0, ready_sha="r" * 40, url=URL))
        engine.rules.reload()
        await EngineMethods(engine).owner_approve({"key": KEY})
        lists = _Lists({(HEAD, NEXT): _commits(2, merges=1), (None, NEXT): _commits(4)})
        engine.worktree_service.base_merges = lists
        _set_pr(engine, _pr(head=NEXT, after=2, ready_sha="r" * 40, url=URL))
        engine.owner_approvals._counts[(KEY, HEAD, NEXT, "main")] = (1, 1, 0)
        await _rule_row(engine)
        rc = (await _settled(engine))["review_commits"]
        assert (rc["since"], rc["from"], rc["count"], rc["base_merges"]) == \
            ("approval", HEAD, 1, 1)
        # A force push dropped the approved commit (and the ready head):
        # the PR's own commits.
        third = "c" * 40
        lists.lists[(None, third)] = _commits(4)
        _set_pr(engine, _pr(head=third, after=2, url=URL))
        engine.owner_approvals._counts[(KEY, HEAD, third, "main")] = None
        await _rule_row(engine)
        rc = (await _settled(engine))["review_commits"]
        assert (rc["since"], rc["count"]) == ("opened", 4)


@pytest.mark.asyncio
async def test_not_on_a_draft_nor_a_teammates_pr():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=NEXT, after=0, is_draft=True, url=URL))
        engine.rules.reload()
        engine.worktree_service.base_merges = _Lists({})
        await _rule_row(engine)
        fields = await _settled(engine)
        assert fields["approvable"] is not None and fields["review_commits"] is None
        _set_pr(engine, _pr(head=NEXT, after=1, ready_sha=HEAD, url=URL))
        await _rule_row(engine, me="someone-else")
        assert (await _settled(engine))["review_commits"] is None


@pytest.mark.asyncio
async def test_a_list_that_failed_is_read_again_after_a_while():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=NEXT, after=1, ready_sha=HEAD, url=URL))
        engine.rules.reload()
        lists = _Lists({})                   # not fetched yet: None
        engine.worktree_service.base_merges = lists
        now = [1000.0]
        engine.review_commits.clock = lambda: now[0]
        await _rule_row(engine)
        await _settled(engine)
        # The fallback (the PR's own commits) failed too: nothing shown.
        assert (await _settled(engine))["review_commits"] is None
        assert lists.calls == [(HEAD, NEXT), (None, NEXT)]
        # Within RETRY_AFTER: not read again.
        now[0] += RETRY_AFTER - 1
        await _settled(engine)
        assert len(lists.calls) == 2
        # Past it, with the commit fetched since: read again, shown.
        lists.lists[(HEAD, NEXT)] = _commits(1)
        now[0] += 2
        rc = (await _settled(engine))["review_commits"]
        assert (rc["since"], rc["count"]) == ("ready", 1)
        assert lists.calls[2] == (HEAD, NEXT)


# --- a pass announces a change of the fields alone -----------------------------------


@pytest.mark.asyncio
async def test_a_row_whose_fields_alone_changed_is_updated_while_watched():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=NEXT, after=1, ready_sha=HEAD, url=URL))
        engine.rules.reload()
        engine.worktree_service.base_merges = _Lists({(HEAD, NEXT): _commits(1)})
        await _rule_row(engine)
        seen = []
        engine.events.subscribe(lambda e: seen.append(e.event) if e.event == "status.updated"
                                else None)
        # Nobody reads the fields: the list arriving is no row change.
        await _settled(engine)
        engine.status.flush()
        assert seen == []
        engine.status.watch_fields("v1:1", True)
        engine.status.flush()                 # the first look
        seen.clear()
        engine.status.flush()
        assert seen == []
        # A list read again (a new commit list for the same head): only the
        # field changes.
        engine.review_commits._lists[("ready", HEAD, NEXT, "main")] = _commits(2)
        engine.status.flush()
        assert seen == ["status.updated"]
        engine.status.watch_fields("v1:1", False)
        engine.review_commits._lists[("ready", HEAD, NEXT, "main")] = _commits(3)
        engine.status.flush()
        assert seen == ["status.updated"]
