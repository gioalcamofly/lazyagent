"""Review requests in Home: the open PRs where someone asked the user (or a
team of theirs) for a review, as rows and ``review_request`` items; the
search's cost and caps; a worktree on the branch; the feeders' items they
link; ``pr.mine`` and ``pr.review_requested_from_me``. Every ``gh`` is
:class:`tests.engine.fake_search.FakeSearch`; HOME and the XDG dirs are the
test's (conftest.py)."""

from __future__ import annotations

import asyncio

import pytest
from lazyagent.config import Config, StatusConfig, load_config
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.review_requests import (
    MAX_CALLS_PER_MIN,
    SEARCH,
    ReviewRequestHub,
    parse_search,
)
from lazyagent.engine.rules.facts import RowContext
from lazyagent.engine.rules.lang import UNKNOWN
from lazyagent.engine.status import Facts
from lazyagent.engine.trackers import RateLimited
from lazyagent.models import CiCheck, PrInfo
from lazyagent.protocol import validation
from lazyagent.worktree_manager import GithubRepo

from tests.engine.fake_search import ME, FakeSearch, pr_node, team, user
from tests.engine.fakes import make_engine, worktree

NOW = 1_800_000_000.0
ASKED = NOW - 3 * 3600
KEY = "repo:alice-fix"
ITEM = f"Review_request:#12:{int(ASKED)}"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine(gh: FakeSearch, *worktrees, config: Config | None = None):
    engine = make_engine([worktree("/tmp/repo"), *worktrees], config=config)
    clock = Clock()
    engine.status.clock = clock
    engine.actionables.clock = clock
    engine.review_requests = ReviewRequestHub("/tmp/repo", run=gh, clock=clock)
    engine.worktree_service.github.set_installed(True)
    engine.worktree_service._github_repo = GithubRepo("o", "r")
    return engine, clock


async def _pass(engine):
    """A status pass that polls (when due), then the pass that reads it."""
    engine.status.flush()
    engine.status.tick()
    await engine.wait_idle()
    engine.status.flush()


def _rows(engine, include_done=False):
    result = engine.status.list(include_done=include_done)
    validation.validate_result("status_list", result, strict=True)
    return {r["key"]: r for r in result["rows"]}


def _items(engine, kind="review_request"):
    result = engine.actionables.list()
    validation.validate_result("actionables_list", result, strict=True)
    return [i for i in result["items"] if i["kind"] == kind]


def _asked(number=12, head="alice-fix", **kw):
    kw.setdefault("requested_at", ASKED)
    return pr_node(number, head, **kw)


# --- The search ------------------------------------------------------------------


def test_the_search_asks_for_open_ready_prs_requested_from_me_or_my_teams():
    # review-requested:@me covers a team of the user's; user-review-... would not.
    assert SEARCH == "repo:{owner}/{name} is:pr is:open draft:false review-requested:@me"


@pytest.mark.asyncio
async def test_a_request_becomes_a_round_row_with_no_worktree():
    gh = FakeSearch([_asked(title="Fix the export")])
    engine, _ = _engine(gh)
    await _pass(engine)
    [call] = gh.calls
    assert gh.search_of(call) == SEARCH.format(owner="o", name="r")
    row = _rows(engine)[KEY]
    assert (row["worktree_path"], row["branch"], row["title"]) == (None, "alice-fix", "Fix the export")
    assert (row["zone"], row["level"], row["ball"]) == ("inbox", "round", "you")
    assert row["state"] == "review requested by alice"
    assert row["urls"] == ["https://github.com/o/r/pull/12"]
    assert row["review_request"] == {
        "number": 12, "title": "Fix the export", "url": "https://github.com/o/r/pull/12",
        "author": "alice", "requested_at": ASKED, "team": None}
    [item] = _items(engine)
    assert (item["id"], item["title"], item["level"], item["since"]) == (
        ITEM, "Review requested", "round", ASKED)
    assert item["work_item"] == KEY and item["worktree_path"] is None
    assert item["pr"] == {"number": 12, "title": "Fix the export",
                          "url": "https://github.com/o/r/pull/12"}
    assert item["detail"] == "alice asked you for a review"
    fields = engine.status.fields_of(KEY)
    assert (fields["verb"], fields["action"]) == ("Review", "open_pr")


@pytest.mark.asyncio
async def test_a_review_by_the_user_removes_it_and_a_new_request_brings_it_back():
    gh = FakeSearch([_asked()])
    engine, clock = _engine(gh)
    await _pass(engine)
    assert KEY in _rows(engine) and _items(engine)
    for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
        # A team's request stays on GitHub after the review: the review
        # after the request is what says it is done.
        gh.prs = [_asked(asked=(team("core"),), reviews=((ME, state, ASKED + 60),))]
        clock.now += 61
        await _pass(engine)
        assert KEY not in _rows(engine), state
        assert _items(engine) == []
        assert KEY not in engine.overlay.closed  # no done row: it just goes
        gh.prs = [_asked()]
        clock.now += 61
        await _pass(engine)
        assert KEY in _rows(engine)
    # A pending review is not one; a review older than the request neither.
    gh.prs = [_asked(reviews=((ME, "PENDING", ASKED + 60), (ME, "APPROVED", ASKED - 60)))]
    clock.now += 61
    await _pass(engine)
    assert KEY in _rows(engine)
    # Asked again after the review: a new occurrence, dated then.
    later = ASKED + 7200
    gh.prs = [_asked(requested_at=later, reviews=((ME, "COMMENTED", ASKED + 60),))]
    clock.now += 61
    await _pass(engine)
    [item] = _items(engine)
    assert (item["id"], item["since"]) == (f"Review_request:#12:{int(later)}", later)


@pytest.mark.asyncio
async def test_a_withdrawn_request_or_a_closed_or_merged_pr_goes():
    gh = FakeSearch([_asked()])
    engine, clock = _engine(gh)
    await _pass(engine)
    assert KEY in _rows(engine)
    for gone in ([], [_asked(state="CLOSED")], [_asked(state="MERGED")]):
        gh.prs = gone
        clock.now += 61
        await _pass(engine)
        assert KEY not in _rows(engine) and _items(engine) == []
        gh.prs = [_asked()]
        clock.now += 61
        await _pass(engine)
        assert KEY in _rows(engine)


@pytest.mark.asyncio
async def test_a_team_request_names_the_team():
    gh = FakeSearch([_asked(asked=(team("core"), user("bob")))])
    engine, _ = _engine(gh)
    await _pass(engine)
    assert _rows(engine)[KEY]["review_request"]["team"] == "core"
    [item] = _items(engine)
    assert item["detail"] == "alice asked team core for a review" and item["since"] == ASKED


@pytest.mark.asyncio
async def test_a_draft_never_shows_nor_does_the_users_own_pr():
    gh = FakeSearch([_asked(draft=True), _asked(13, "mine", author=ME)])
    engine, _ = _engine(gh)
    await _pass(engine)
    assert _rows(engine) == {}
    assert engine.review_requests.known and engine.review_requests.requests == ()


@pytest.mark.asyncio
async def test_no_event_dates_it_from_the_prs_update_and_a_forks_pr_is_keyed_by_number():
    gh = FakeSearch([_asked(requested_at=None, updated_at=NOW - 600, cross_repo=True,
                            head="main")])
    engine, _ = _engine(gh)
    await _pass(engine)
    row = _rows(engine)["repo:#12"]
    assert row["review_request"]["requested_at"] == NOW - 600
    assert row["branch"] == "main"


# --- A worktree on the branch -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_worktree_on_the_branch_gets_the_line_instead_of_a_second_row():
    gh = FakeSearch([_asked()])
    wt = worktree("/tmp/repo-alice", "alice-fix", is_main=False)
    engine, clock = _engine(gh, wt)
    # PR info not read yet: the branch says it.
    await _pass(engine)
    rows = _rows(engine)
    assert set(rows) == {KEY}
    assert rows[KEY]["worktree_path"] == "/tmp/repo-alice"
    assert rows[KEY]["state"] == "review requested by alice"
    [item] = _items(engine)
    assert item["worktree_path"] == "/tmp/repo-alice" and item["id"] == ITEM
    # Then its PR info: the same request, by number.
    engine.worktree_service._pr_infos["/tmp/repo-alice"] = PrInfo(
        12, "Fix the export", "OPEN", [CiCheck("tests", "COMPLETED", "SUCCESS")],
        "https://github.com/o/r/pull/12", review_decision="REVIEW_REQUIRED",
        mergeable="MERGEABLE", author="alice", review_requests=1, reviews=0, is_draft=False)
    await _pass(engine)
    assert set(_rows(engine)) == {KEY}
    assert [i["id"] for i in _items(engine)] == [ITEM]
    # Reviewed: the worktree's row stays, a teammate's PR (not "no reviewer").
    gh.prs = []
    clock.now += 61
    await _pass(engine)
    row = _rows(engine)[KEY]
    assert (row["ball"], row["state"]) == ("others", "alice's PR")
    assert "review_request" not in row


@pytest.mark.asyncio
async def test_a_worktree_on_the_branch_for_another_pr_keeps_its_row():
    gh = FakeSearch([_asked()])
    wt = worktree("/tmp/repo-alice", "alice-fix", is_main=False)
    engine, _ = _engine(gh, wt)
    engine.worktree_service._pr_infos["/tmp/repo-alice"] = PrInfo(
        9, "Old one", "MERGED", [], author="alice")
    await _pass(engine)
    rows = _rows(engine, include_done=True)
    assert set(rows) == {KEY, "repo:#12"}
    assert "review_request" not in rows[KEY]
    assert rows["repo:#12"]["state"] == "review requested by alice"


# --- Gestures --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismissed_or_parked_it_reads_set_aside_and_a_label_works():
    gh = FakeSearch([_asked()])
    engine, clock = _engine(gh)
    await _pass(engine)
    methods = EngineMethods(engine)
    [item] = _items(engine)
    await methods.actionables_mark({"id": item["id"], "action": "dismiss",
                                    "occurrence": item["occurrence"]})
    engine.status.flush()
    row = _rows(engine)[KEY]
    assert (row["ball"], row["state"]) == ("stalled", "review requested · set aside")
    await methods.actionables_mark({"id": item["id"], "action": "undismiss"})
    await methods.actionables_mark({"id": item["id"], "action": "park", "until": "tomorrow"})
    engine.status.flush()
    assert _rows(engine)[KEY]["state"] == "review requested · set aside"
    await methods.actionables_mark({"id": item["id"], "action": "unpark"})
    await methods.status_gesture({"key": KEY, "kind": "label", "ball": "others",
                                  "text": "after lunch"})
    engine.status.flush()
    assert _rows(engine)[KEY]["needs_you"] is not None  # the item still leads
    # The request goes: its row and what was kept for it go too.
    gh.prs = []
    clock.now += 61
    await _pass(engine)
    assert KEY not in _rows(engine) and KEY not in engine.overlay.gestures


@pytest.mark.asyncio
async def test_a_review_asked_of_the_user_is_not_passed_to_the_orchestrator():
    """I4h: the review is the user's; the orchestrator cannot act on it."""
    gh = FakeSearch([_asked()])
    engine, _ = _engine(gh)
    await _pass(engine)
    engine.claims.orchestrator_alive = lambda: True
    assert engine.status.fields_of(KEY)["passable"] is False
    with pytest.raises(ValueError, match="Review on .* is yours: the orchestrator cannot act"):
        engine.claims.claim(KEY)
    engine.status.flush()
    row = _rows(engine)[KEY]
    assert row["needs_you"]["top"] == ITEM and row["ball"] == "you" and row["claim"] is None


@pytest.mark.asyncio
async def test_before_the_first_answer_a_kept_review_row_is_not_closed_as_removed():
    gh = FakeSearch([_asked()])
    engine, _ = _engine(gh)
    engine.overlay.add(KEY, "state", "user", "labelled")  # kept from before a restart
    gh.queue.append(asyncio.TimeoutError())  # the first poll hangs, then fails
    engine.status.flush()
    assert KEY not in engine.overlay.closed
    await _pass(engine)  # failed: known nothing, error set
    assert engine.review_requests.error


# --- Feeders ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_feeders_item_joins_the_line_by_ticket_or_url_and_is_started():
    gh = FakeSearch([_asked(title="SHOP-7 Fix the export"), _asked(14, "bob-docs", author="bob")])
    engine, _ = _engine(gh)
    methods = EngineMethods(engine)
    await methods.work_items_upsert({"url": "https://tracker.example.com/browse/SHOP-7",
                                     "title": "Export bug", "source": "planner",
                                     "priority": "high"})
    await methods.work_items_upsert({"url": "https://github.com/o/r/pull/14/",
                                     "title": "Docs review", "source": "planner",
                                     "priority": "low"})
    await methods.work_items_upsert({"url": "https://tracker.example.com/browse/SHOP-9",
                                     "title": "Not started", "source": "planner"})
    await _pass(engine)
    rows = _rows(engine)
    shop = rows[KEY]
    assert shop["feeder"]["url"] == "https://tracker.example.com/browse/SHOP-7"
    assert (shop["priority"], shop["title"]) == ("high", "SHOP-7 Fix the export")
    assert "https://tracker.example.com/browse/SHOP-7" in shop["urls"]
    assert rows["repo:bob-docs"]["priority"] == "low"
    result = engine.status.list()
    assert result["counts"]["not_started"] == 1
    assert [i["url"] for i in result["not_started_items"]] == [
        "https://tracker.example.com/browse/SHOP-9"]
    # Its inbox item takes the priority too.
    items = {i["work_item"]: i for i in _items(engine)}
    assert items[KEY]["priority"] == "high" and items["repo:bob-docs"]["priority"] == "low"


@pytest.mark.asyncio
async def test_a_ticket_in_the_head_branch_links_with_the_configured_pattern():
    gh = FakeSearch([_asked(head="feature/ab_77-export")])
    config = Config(status=StatusConfig(ticket_pattern=r"(?i)\bab_\d+"))
    engine, _ = _engine(gh, config=config)
    await EngineMethods(engine).work_items_upsert({
        "url": "https://tracker.example.com/AB_77", "title": "Export", "source": "board",
        "ticket": "AB_77"})
    await _pass(engine)
    assert _rows(engine)["repo:feature/ab_77-export"]["feeder"]["ticket"] == "AB_77"
    assert engine.status.list()["counts"]["not_started"] == 0


# --- Facts -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_mine_and_review_requested_from_me():
    gh = FakeSearch([_asked()])
    mine = worktree("/tmp/repo-mine", "my-work", is_main=False)
    engine, _ = _engine(gh, mine)
    engine.worktree_service._pr_infos["/tmp/repo-mine"] = PrInfo(
        3, "Mine", "OPEN", [], author="Me", review_decision="REVIEW_REQUIRED")
    # Before the search answers: both unknown.
    engine.status.flush()
    facts = engine.status.facts("repo:my-work")
    ctx = RowContext(facts, NOW, NOW, 24)
    assert ctx.fact("pr.mine") is UNKNOWN
    await _pass(engine)
    ctx = RowContext(engine.status.facts("repo:my-work"), NOW, NOW, 24)
    assert ctx.fact("pr.mine") is True  # logins compare ignoring case
    assert ctx.fact("pr.review_requested_from_me") is False
    ctx = RowContext(engine.status.facts(KEY), NOW, NOW, 24)
    assert (ctx.fact("pr.mine"), ctx.fact("pr.review_requested_from_me"),
            ctx.fact("pr.author")) == (False, True, "alice")
    # A row with no PR: unknown, never false.
    ctx = RowContext(Facts(key="k", repo="repo", branch="b", worktree_path="/x", me=ME,
                           requests_known=True), NOW, NOW, 24)
    assert ctx.fact("pr.review_requested_from_me") is UNKNOWN


@pytest.mark.asyncio
async def test_a_teammates_approved_pr_on_a_local_branch_is_not_yours_to_merge():
    gh = FakeSearch([])
    wt = worktree("/tmp/repo-alice", "alice-fix", is_main=False)
    engine, _ = _engine(gh, wt)
    engine.worktree_service._pr_infos["/tmp/repo-alice"] = PrInfo(
        12, "Fix", "OPEN", [CiCheck("tests", "COMPLETED", "SUCCESS")],
        review_decision="APPROVED", mergeable="MERGEABLE", author="alice")
    await _pass(engine)
    row = _rows(engine)[KEY]
    assert (row["ball"], row["state"]) == ("others", "alice's PR · approved")
    assert not any(i["kind"] == "rule" for i in engine.actionables.list()["items"])


# --- Cost, caps, config ------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_call_per_poll_whatever_the_number_of_prs_and_none_when_not_due():
    gh = FakeSearch([_asked(n, f"b{n}", requested_at=ASKED) for n in range(1, 31)])
    clock = Clock()
    hub = ReviewRequestHub("/tmp/repo", run=gh, clock=clock)
    assert await hub.poll("o", "r") is True
    assert len(gh.calls) == 1 and len(hub.requests) == 30
    assert await hub.poll("o", "r") is False  # not due
    clock.now += 59
    assert await hub.poll("o", "r") is False
    assert len(gh.calls) == 1
    clock.now += 1
    assert await hub.poll("o", "r") is False  # due, unchanged
    assert len(gh.calls) == 2
    # Never a conditional request: GitHub's search has no ETag to send back.
    assert not any("If-None-Match" in " ".join(c) for c in gh.calls)


@pytest.mark.asyncio
async def test_the_per_minute_cap_a_rate_limit_and_a_failure_keep_the_last_answer():
    gh = FakeSearch([_asked()])
    clock = Clock()
    hub = ReviewRequestHub("/tmp/repo", run=gh, clock=clock)
    for _ in range(MAX_CALLS_PER_MIN + 2):
        await hub.poll("o", "r", force=True)
    assert len(gh.calls) == MAX_CALLS_PER_MIN
    clock.now += 60
    gh.queue.append((1, "", "gh: API rate limit exceeded for user"))
    assert await hub.poll("o", "r", force=True) is False
    assert hub.backoff == 60 and hub.requests and hub.known
    clock.now += 30
    await hub.poll("o", "r", force=True)
    assert len(gh.calls) == MAX_CALLS_PER_MIN + 1  # still backing off
    clock.now += 31
    gh.queue.append((0, '{"errors": [{"message": "boom"}]}', ""))
    await hub.poll("o", "r", force=True)
    assert hub.error and hub.requests  # a bad body keeps the last good one
    clock.now += 61
    gh.queue.append(RateLimited("HTTP 403", 120))
    await hub.poll("o", "r", force=True)
    assert hub.backoff == 120
    clock.now += 121
    assert await hub.poll("o", "r", force=True) is False and hub.error is None


@pytest.mark.asyncio
async def test_never_two_polls_at_once():
    gate = asyncio.Event()
    gh = FakeSearch([_asked()])

    async def slow(args):
        await gate.wait()
        return await FakeSearch.__call__(gh, args)

    hub = ReviewRequestHub("/tmp/repo", run=slow, clock=Clock())
    first = asyncio.ensure_future(hub.poll("o", "r"))
    await asyncio.sleep(0)
    assert await hub.poll("o", "r", force=True) is False
    gate.set()
    assert await first is True


@pytest.mark.asyncio
async def test_no_poll_while_github_fails_nor_until_the_repo_is_known():
    gh = FakeSearch([_asked()])
    engine, _ = _engine(gh)
    health = engine.worktree_service.github
    health.failed("could not resolve host")
    await _pass(engine)
    health.succeeded()
    engine.worktree_service._github_repo = None
    await _pass(engine)
    assert gh.calls == [] and KEY not in _rows(engine)


@pytest.mark.asyncio
async def test_turned_off_it_only_learns_the_login_once(tmp_path):
    (tmp_path / ".lazyagent.toml").write_text("[status]\nreview_requests = false\n")
    assert load_config(tmp_path).status.review_requests is False
    assert load_config(tmp_path / "nowhere").status.review_requests is True
    gh = FakeSearch([_asked()])
    engine, _ = _engine(gh)
    engine.review_requests.turn_off()
    await _pass(engine)
    await _pass(engine)
    assert len(gh.calls) == 1 and "search(" not in " ".join(gh.calls[0])
    assert engine.review_requests.viewer == ME
    assert KEY not in _rows(engine)
    # Off, a PR's request is unknown (not "nobody asked"); pr.mine still reads.
    facts = Facts(key="repo:x", repo="repo", branch="x", worktree_path="/x",
                  pr=PrInfo(1, "t", "OPEN", [], author="alice"), me=ME)
    ctx = RowContext(facts, NOW, NOW, 24)
    assert (ctx.fact("pr.review_requested_from_me"), ctx.fact("pr.mine")) == (UNKNOWN, False)


def test_parse_search_rejects_a_body_without_the_viewer_or_the_search():
    for body in ('{"data": null, "errors": [{"message": "x"}]}', '{"data": {"viewer": {}}}',
                 "not json"):
        with pytest.raises(ValueError):
            parse_search(body)
