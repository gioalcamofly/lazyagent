"""The review facts (proposal 0014 R3b): ``pr.draft``, ``pr.labels``,
``pr.human_approvals``, ``pr.commits_after_ready``; the PR poll's second
query that fetches the last three only while a rule reads them; ``[review]
not_human``; and the README's "owner review → human peer review → QA →
merge" recipe, stage by stage. No network.

HOME and every XDG dir are under the test's tmp_path (conftest.py)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.rules import builtin
from lazyagent.engine.rules.facts import FACTS, REVIEW_FACTS, RowContext
from lazyagent.engine.rules.lang import UNKNOWN, RuleError, compile_expr
from lazyagent.engine.rules.user import compose, parse_file
from lazyagent.engine.status import AgentFact, Facts
from lazyagent.engine.trackers import TrackerFact
from lazyagent.engine.worktrees import WorktreeService
from lazyagent.models import CiCheck, PrInfo
from lazyagent.protocol import validation
from lazyagent.worktree_manager import (
    GithubRepo,
    WorktreeManager,
    _approved_by,
    _commits_after_ready,
    _labels,
)
from tests.engine.fakes import make_engine, worktree
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

NOW = 1_800_000_000.0
BRANCH = "SHOP-7-export"
WT = "/tmp/repo-shop-7"
KEY = f"repo:{BRANCH}"
GREEN = [CiCheck("tests", "COMPLETED", "SUCCESS")]
RED = [CiCheck("tests", "COMPLETED", "FAILURE")]
ME = "owner"


def readme_recipe() -> str:
    readme = (Path(__file__).resolve().parents[3] / "README.md").read_text()
    section = readme.split("### Your rules (rules.toml)")[1].split("\n### ")[0]
    blocks = re.findall(r"```toml\n(.*?)```", section, re.S)
    (recipe,) = [b for b in blocks if "owner review, peer review, QA, merge" in b]
    return recipe


def review(state: str, login: str | None, typename: str = "User") -> dict:
    return {"state": state, "author": None if login is None else
            {"__typename": typename, "login": login}}


def reviews(*nodes: dict, cut: bool = False) -> dict:
    return {"totalCount": len(nodes), "pageInfo": {"hasPreviousPage": cut}, "nodes": list(nodes)}


def ready_at(t: str) -> dict:
    return {"__typename": "ReadyForReviewEvent", "createdAt": t}


def commit(t: str) -> dict:
    return {"__typename": "PullRequestCommit", "commit": {"committedDate": t}}


def force(t: str) -> dict:
    return {"__typename": "HeadRefForcePushedEvent", "createdAt": t}


def timeline(*items: dict, draft: bool = False, created: str = "2026-10-01T09:00:00Z",
             cut: bool = False) -> dict:
    return {"isDraft": draft, "createdAt": created,
            "timelineItems": {"pageInfo": {"hasPreviousPage": cut}, "nodes": list(items)}}


# --- human approvals ---------------------------------------------------------


def test_a_later_changes_requested_or_dismissal_cancels_an_approval_a_comment_does_not():
    conn = reviews(
        review("APPROVED", "ana"), review("CHANGES_REQUESTED", "ana"),
        review("CHANGES_REQUESTED", "bo"), review("APPROVED", "bo"), review("COMMENTED", "bo"),
        review("DISMISSED", "cy"),  # an approval GitHub dismissed (a push, or by hand)
        review("APPROVED", "dee"), review("PENDING", "dee"),
    )
    assert _approved_by(conn, ME) == ["bo", "dee"]


def test_apps_deleted_accounts_and_the_author_never_count():
    conn = reviews(
        review("APPROVED", "review-app", "Bot"), review("APPROVED", "ci-helper[bot]"),
        review("APPROVED", None), review("APPROVED", "Owner"), review("APPROVED", "ana"),
    )
    assert _approved_by(conn, ME) == ["ana"]


def test_reviews_not_fetched_or_cut_short_are_unknown_not_zero():
    assert _approved_by({"totalCount": 3}, ME) is None  # the main query's count only
    assert _approved_by(None, ME) is None
    assert _approved_by(reviews(review("APPROVED", "ana"), cut=True), ME) is None
    assert _approved_by(reviews(), ME) == []


# --- commits after ready -----------------------------------------------------


def test_commits_and_force_pushes_after_the_last_ready_event_count():
    node = timeline(commit("2026-10-01T09:00:00Z"), ready_at("2026-10-01T10:00:00Z"),
                    commit("2026-10-01T11:00:00Z"), force("2026-10-01T12:00:00Z"))
    assert _commits_after_ready(node) == (2, "2026-10-01T10:00:00Z")
    # Back to draft, fixes, ready again: only what follows the last one.
    node = timeline(ready_at("2026-10-01T10:00:00Z"), commit("2026-10-01T11:00:00Z"),
                    ready_at("2026-10-01T13:00:00Z"))
    assert _commits_after_ready(node) == (0, "2026-10-01T13:00:00Z")


def test_a_rebase_after_ready_counts_whatever_dates_its_commits_keep():
    # Rebased with the old committer dates: the commits sort before the
    # event, the force push's own event does not.
    node = timeline(commit("2026-10-01T09:00:00Z"), ready_at("2026-10-01T10:00:00Z"),
                    force("2026-10-02T08:00:00Z"))
    assert _commits_after_ready(node) == (1, "2026-10-01T10:00:00Z")


def test_a_draft_is_zero_even_unfetched():
    assert _commits_after_ready(timeline(commit("2026-10-01T11:00:00Z"), draft=True)) == (0, "")
    assert _commits_after_ready({"isDraft": True}) == (0, "")


def test_a_pr_opened_ready_counts_from_its_opening():
    node = timeline(commit("2026-10-01T08:00:00Z"), commit("2026-10-01T10:00:00Z"),
                    force("2026-10-01T11:00:00Z"), created="2026-10-01T09:00:00Z")
    assert _commits_after_ready(node) == (2, "2026-10-01T09:00:00Z")


def test_an_unfetched_or_cut_timeline_without_an_event_is_unknown():
    assert _commits_after_ready({"isDraft": False}) == (None, "")
    assert _commits_after_ready(timeline(commit("2026-10-01T10:00:00Z"), cut=True)) == (None, "")
    # Cut short, but the event is in the window: what follows it is all there.
    node = timeline(ready_at("2026-10-01T10:00:00Z"), commit("2026-10-01T11:00:00Z"), cut=True)
    assert _commits_after_ready(node) == (1, "2026-10-01T10:00:00Z")


def test_labels_from_both_shapes_and_unknown_when_cut():
    assert _labels({"pageInfo": {"hasNextPage": False}, "nodes": [{"name": "QA"}]}) == ["QA"]
    assert _labels([{"id": "x", "name": "bug"}]) == ["bug"]  # gh pr view --json labels
    assert _labels({"pageInfo": {"hasNextPage": True}, "nodes": [{"name": "QA"}]}) is None
    assert _labels(None) is None


# --- the poll -----------------------------------------------------------------


def _node(number: int, state: str = "OPEN", **kw) -> dict:
    return {"number": number, "title": f"PR {number}", "state": state, "url": "u",
            "isCrossRepository": False, "headRefOid": "h1", "isDraft": False,
            "author": {"login": ME}, "reviews": {"totalCount": 1},
            "commits": {"nodes": []}, **kw}


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    import subprocess

    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _main(*nodes: list[dict]) -> str:
    repo = {"defaultBranchRef": {"name": "main"}}
    for i, conn in enumerate(nodes):
        repo[f"pr{i}"] = {"nodes": conn}
    return json.dumps({"data": {"repository": repo}})


REPO = GithubRepo("acme", "widgets")


def _infos(tmp_path, responses, review: bool, n: int = 2):
    from lazyagent.models import WorktreeInfo

    (tmp_path / ".git").mkdir(exist_ok=True)
    manager = WorktreeManager(tmp_path)
    wts = [WorktreeInfo(f"/w{i}", "abc", f"b{i}", False, False) for i in range(n)]
    with patch("lazyagent.worktree_manager.subprocess.run",
               side_effect=[_completed(returncode=1), *responses]) as run:
        result = manager.get_pr_infos(wts, REPO, review=review)
    return result, [c.args[0] for c in run.call_args_list[1:]]


def test_the_review_query_asks_only_for_open_prs_by_number(tmp_path):
    extra = {"r0": {"createdAt": "2026-10-01T09:00:00Z",
                    "labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": "QA"}]},
                    "reviews": reviews(review("APPROVED", "ana")),
                    "timelineItems": {"pageInfo": {"hasPreviousPage": False},
                                      "nodes": [ready_at("2026-10-01T10:00:00Z")]}}}
    result, calls = _infos(tmp_path, [
        _completed(_main([_node(7)], [_node(8, "MERGED")])),
        _completed(json.dumps({"data": {"repository": extra}})),
    ], review=True)
    assert len(calls) == 2
    second = calls[1]
    assert "r0: pullRequest(number: $n0)" in second[4] and "r1:" not in second[4]
    assert second[second.index("-F") + 1] == "n0=7"
    pr = result["/w0"]
    assert (pr.is_draft, pr.labels, pr.approved_by, pr.commits_after_ready, pr.ready_at) == (
        False, ["QA"], ["ana"], 0, "2026-10-01T10:00:00Z")
    merged = result["/w1"]
    assert (merged.labels, merged.approved_by, merged.commits_after_ready) == (None, None, None)


def test_without_a_rule_reading_them_the_poll_is_one_call_as_before(tmp_path):
    result, calls = _infos(tmp_path, [_completed(_main([_node(7)], []))], review=False)
    assert len(calls) == 1
    pr = result["/w0"]
    assert (pr.is_draft, pr.author) == (False, ME)
    assert (pr.labels, pr.approved_by, pr.commits_after_ready) == (None, None, None)
    # No open PR: no second call either.
    _, calls = _infos(tmp_path, [_completed(_main([_node(7, "CLOSED")], []))], review=True)
    assert len(calls) == 1


def test_a_failed_review_query_leaves_the_facts_unknown_not_zero(tmp_path):
    result, calls = _infos(tmp_path, [
        _completed(_main([_node(7)], [])), _completed(returncode=1, stderr="HTTP 502"),
    ], review=True)
    assert len(calls) == 2
    pr = result["/w0"]
    assert pr.number == 7 and pr.is_draft is False
    facts = Facts(key=KEY, repo="repo", branch=BRANCH, worktree_path=WT, pr=pr)
    ctx = RowContext(facts, NOW, NOW, 24)
    for name in REVIEW_FACTS:
        assert ctx.fact(name) is UNKNOWN, name
    rule = compile_expr("pr.human_approvals == 0 or pr.commits_after_ready == 0",
                        _row_env())
    assert rule.fn(ctx) is UNKNOWN


def test_the_head_check_catches_a_commit_dated_before_ready_and_pushed_after():
    service = WorktreeService(lambda root: None)
    pr = PrInfo(7, "t", "OPEN", [], head_sha="h1", is_draft=False, commits_after_ready=0,
                ready_at="2026-10-01T10:00:00Z")
    assert service._ready_head(WT, pr) is pr
    moved = service._ready_head(WT, PrInfo(**{**pr.__dict__, "head_sha": "h2"}))
    assert moved.commits_after_ready == 1
    # Marked ready again: a new baseline.
    again = PrInfo(**{**pr.__dict__, "head_sha": "h2", "ready_at": "2026-10-01T12:00:00Z"})
    assert service._ready_head(WT, again).commits_after_ready == 0
    # Back to draft: forgotten.
    draft = PrInfo(**{**pr.__dict__, "is_draft": True, "ready_at": ""})
    assert service._ready_head(WT, draft) is draft and WT not in service._ready_heads


def test_the_engine_asks_for_the_review_facts_only_while_a_rule_reads_them():
    engine = make_engine([worktree("/tmp/repo")])
    with _rules_files({}):
        engine.rules.reload()
        assert engine.worktree_service.wants_review() is False
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine.rules.reload()
        assert engine.worktree_service.wants_review() is True
    assert builtin().reads_review is False


# --- the facts and the language -----------------------------------------------


def _row_env():
    from lazyagent.engine.rules.facts import ROW_ENV

    return ROW_ENV


def _ctx(pr: PrInfo | None, not_human: frozenset[str] = frozenset(), **kw) -> RowContext:
    facts = Facts(key=KEY, repo="repo", branch=BRANCH, worktree_path=WT, pr=pr, **kw)
    return RowContext(facts, NOW, NOW, 24, not_human)


def test_the_facts_read_the_pr_and_are_unknown_without_one():
    pr = PrInfo(7, "t", "OPEN", [], is_draft=True, labels=["QA", "needs-docs"],
                approved_by=["ana", "Review-Bot"], commits_after_ready=0)
    ctx = _ctx(pr, frozenset({"review-bot"}))
    assert ctx.fact("pr.draft") is True
    assert ctx.fact("pr.labels") == frozenset({"qa", "needs-docs"})
    assert ctx.fact("pr.human_approvals") == 1
    assert ctx.fact("pr.commits_after_ready") == 0
    for name in ("pr.draft", *REVIEW_FACTS):
        assert _ctx(None).fact(name) is UNKNOWN
        assert _ctx(PrInfo(7, "t", "OPEN", [])).fact(name) is UNKNOWN
        assert FACTS[name].cost == ("poll" if name == "pr.draft" else "extra")


def test_labels_are_matched_in_lower_case_and_an_upper_case_literal_is_refused():
    ctx = _ctx(PrInfo(7, "t", "OPEN", [], labels=["QA:Skip"]))
    assert compile_expr('"qa:skip" in pr.labels', _row_env()).fn(ctx) is True
    assert compile_expr('"blocked" not in pr.labels', _row_env()).fn(ctx) is True
    assert compile_expr('["qa:skip"] subset pr.labels', _row_env()).fn(ctx) is True
    with pytest.raises(RuleError, match="lower case: write 'qa:skip'"):
        compile_expr('"QA:Skip" in pr.labels', _row_env())


def test_review_not_human_from_both_files_and_its_errors():
    g = parse_file('version = 1\n[review]\nnot_human = ["Review-App"]\n', "global", "/g")
    r = parse_file('version = 1\n[review]\nnot_human = ["ci-user"]\n', "repo", "/r")
    composed = compose(builtin(), [g, r])
    assert composed.errors == []
    assert composed.catalog.not_human == frozenset({"review-app", "ci-user"})
    bad = parse_file('version = 1\n[review]\nnot_human = "x"\nbots = []\n', "global", "/g")
    assert [(e["line"], e["message"]) for e in bad.errors] == [
        (2, "unknown key 'bots' in [review]"), (2, "[review] not_human must be a list of logins")]
    assert bad.not_human == []


# --- the README recipe, stage by stage -------------------------------------------


def _recipe():
    composed = compose(builtin(), [parse_file(readme_recipe(), "global", GLOBAL_RULES)])
    assert composed.errors == [] and composed.warnings == []
    return composed.catalog


def _pr(*, draft=False, approvals=("ana",), after=0, checks=GREEN, mergeable="MERGEABLE",
        review="APPROVED") -> PrInfo:
    return PrInfo(7, "Export to CSV", "OPEN", list(checks), "https://example.com/pull/7",
                  review_decision=review, mergeable=mergeable, is_draft=draft, author=ME,
                  approved_by=list(approvals) if approvals is not None else None,
                  commits_after_ready=after, labels=[])


def _tracker(qa: str | None) -> TrackerFact:
    return TrackerFact("jira", "SHOP-7", "", "", "x", None, "x", None, NOW, False, qa=qa)


def _stage(catalog, pr: PrInfo, qa: str | None = None, *, running: bool = False,
           queue: bool = True, me: str | None = ME):
    """(rule id, ball, verb, state): the item rule a pass opens (ball you),
    else what the row reads. ``me``: the login gh is logged in as."""
    agents = (AgentFact("a1", "running" if running else "waiting"),)
    facts = Facts(key=KEY, repo="repo", branch=BRANCH, worktree_path=WT, pr=pr, agents=agents,
                  tracker=None if qa is None else _tracker(qa),
                  actionables=() if queue else None, me=me)
    ctx = RowContext(facts, NOW, NOW, 24, catalog.not_human)
    if queue:
        rule = catalog.first_item(ctx)
        if rule is not None:
            d = catalog.outputs(rule, ctx)
            return rule.id, d.ball, rule.verb, d.state
    d = catalog.evaluate(RowContext(facts, NOW, NOW, 24, catalog.not_human))
    return d.rule_id, d.ball, catalog.by_id[d.rule_id].verb, d.state


@pytest.mark.parametrize("queue", [True, False])
def test_every_stage_of_the_readme_recipe(queue):
    cat = _recipe()
    assert cat.not_human == frozenset({"my-review-app"})
    order = [r.id for r in cat.rules]
    assert (order.index("label") < order.index("owner_review") < order.index("owner_review_again")
            < order.index("waiting_for_peer") < order.index("waiting_for_qa")
            < order.index("agent_running"))
    stage = lambda pr, qa=None, **kw: _stage(cat, pr, qa, queue=queue, **kw)  # noqa: E731

    # Dev: the agent at work on its draft.
    assert stage(_pr(draft=True, approvals=()), running=True)[:2] == ("agent_running", "agent")
    # Your review, looping with the agent.
    assert stage(_pr(draft=True, approvals=())) == (
        "owner_review", "you", "Review", "draft · your review")
    # You approved (ready for review): a person has to review now.
    assert stage(_pr(approvals=())) == (
        "waiting_for_peer", "others", None, "waiting for peer review")
    # An app's approval is not a person's.
    assert stage(_pr(approvals=("my-review-app",))) == (
        "waiting_for_peer", "others", None, "waiting for peer review")
    # Fixes for the peer's comments pushed after your approval: yours again.
    assert stage(_pr(approvals=("ana",), after=2)) == (
        "owner_review_again", "you", "Review", "new commits since your approval")
    assert stage(_pr(approvals=(), after=1))[0] == "owner_review_again"
    # A person approved; QA has it.
    assert stage(_pr(), "pending") == ("waiting_for_qa", "others", None, "waiting for QA")
    # QA done, or not needed: Merge.
    for qa in ("tested", "not_required"):
        assert stage(_pr(), qa) == ("merge", "you", "Merge", "merge: peer approved, QA ok")
    # Merge needs all of it.
    for pr, qa in (
        (_pr(after=1), "tested"), (_pr(approvals=()), "tested"), (_pr(draft=True), "tested"),
        (_pr(checks=RED), "tested"), (_pr(mergeable="CONFLICTING"), "tested"),
        (_pr(), "failed"), (_pr(), None),
    ):
        assert stage(pr, qa)[0] != "merge", (pr, qa)


@pytest.mark.parametrize("queue", [True, False])
def test_the_recipe_leaves_a_teammates_pr_on_a_local_branch_alone(queue):
    # You checked out a teammate's branch: you are the peer, so the PR is
    # not "waiting for peer review", not your review, not yours to merge.
    cat = _recipe()
    theirs = lambda **kw: replace(_pr(**kw), author="ana")  # noqa: E731
    recipe = {"merge", "owner_review", "owner_review_again", "waiting_for_peer",
              "waiting_for_qa"}
    for pr, qa in ((theirs(approvals=()), None), (theirs(draft=True, approvals=()), None),
                   (theirs(approvals=(), after=1), None), (theirs(), "pending"),
                   (theirs(), "tested")):
        rule_id, ball, _, state = _stage(cat, pr, qa, queue=queue)
        assert rule_id not in recipe and state != "waiting for peer review", (pr, qa)
    # A teammate's ready PR, nobody approved: the built-ins read it as theirs.
    assert _stage(cat, theirs(approvals=(), review="REVIEW_REQUIRED"), queue=queue) == (
        "teammate_pr", "others", None, "ana's PR")
    # Your login not read yet: none of the recipe's rules guesses.
    for qa in ("tested", "pending", None):
        assert _stage(cat, _pr(approvals=()), qa, queue=queue, me=None)[0] not in recipe


def test_a_failed_fetch_never_reads_as_no_approvals_or_not_a_draft():
    cat = _recipe()
    recipe = {"merge", "owner_review", "owner_review_again", "waiting_for_peer",
              "waiting_for_qa"}
    unknown = [
        _pr(approvals=None), _pr(after=None),
        PrInfo(7, "t", "OPEN", GREEN, review_decision="APPROVED", mergeable="MERGEABLE"),
    ]
    for pr in unknown:
        for qa in ("tested", "pending", None):
            for queue in (True, False):
                assert _stage(cat, pr, qa, queue=queue)[0] not in recipe, (pr, qa, queue)


def test_the_recipe_end_to_end_with_why():
    engine = make_engine([worktree("/tmp/repo"), worktree(WT, BRANCH, is_main=False)])
    engine.worktree_service._pr_infos[WT] = _pr(draft=True, approvals=())
    engine.review_requests.viewer = ME
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        result = engine.status.list()
        validation.validate_result("status_list", result, strict=True)
        row = {r["key"]: r for r in result["rows"]}[KEY]
        item = f"Rule:owner_review:{KEY}"
        assert (row["zone"], row["state"], row["needs_you"]["top"]) == (
            "inbox", "draft · your review", item)
        assert engine.actionables.get(item)["verb"] == "Review"
        why = asyncio.run(EngineMethods(engine).rules_explain({"key": KEY}))
        facts = {f["name"]: f["value"] for f in why["item_rule"]["facts"]}
        assert facts["pr.draft"] is True and why["item_rule"]["id"] == "owner_review"
        catalog = asyncio.run(EngineMethods(engine).rules_catalog({}))
        assert catalog["review"] == {"not_human": ["my-review-app"]}
        assert {"pr.draft", *REVIEW_FACTS} <= {f["name"] for f in catalog["facts"]}

        # You approve: the agent marks it ready. Nobody has reviewed yet.
        engine.worktree_service._pr_infos[WT] = _pr(approvals=())
        engine.status.flush()
        row = {r["key"]: r for r in engine.status.list()["rows"]}[KEY]
        assert (row["ball"], row["state"]) == ("others", "waiting for peer review")
