"""The owner's approval of a PR head outside GitHub (docs/design/rules-plan.md
R3d, engine/owner_approvals.py): ``pr.commits_after_owner_approval`` (ready
only, approval only, both, a clean merge or a real commit after it, a force
push), the commits counted with real temporary git repos, ``owner_approve``
and ``owner_approval_revoke`` (tokens, refusals, undo), the row field
``approvable``, Why?, the timeline and persistence. No network; HOME and
the XDG dirs are under tmp_path (tests/conftest.py)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from lazyagent.engine.base_merges import BaseMerges
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.overlay import Overlay
from lazyagent.engine.owner_approvals import ApprovalFact, after_owner_approval, why_text
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus, PrInfo

from tests.engine.fakes import add_session, make_engine, worktree
from tests.engine.rules.test_review_facts import readme_recipe
from tests.engine.test_base_merges import OLD, Repo
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

NOW = 1_800_000_000.0
FEAT = "/tmp/repo-feat"
KEY = "repo:feat"
HEAD = "a" * 40
NEXT = "b" * 40
READY = "2027-01-15T08:00:00Z"            # NOW - 3200 s
READY_T = 1_799_996_800.0
TOKEN = "tok-a1"


def _pr(head: str = HEAD, after: int | None = 0, **kw) -> PrInfo:
    fields = dict(head_sha=head, base_ref="main", is_draft=False, commits_after_ready=after,
                  ready_at=READY, author="me", labels=[], approved_by=["peer"],
                  base_merges_after_ready=0, mergeable="MERGEABLE")
    fields.update(kw)
    return PrInfo(7, "Feat", "OPEN", [], **fields)


def _approval(state: str, at: float = NOW, after: int | None = None) -> ApprovalFact:
    return ApprovalFact(HEAD, at, "home", state, after, 0 if after is not None else None)


# --- the fact, as a pure function --------------------------------------------------


def test_ready_only_is_commits_after_ready():
    assert after_owner_approval(_pr(after=2), None) == 2
    assert after_owner_approval(_pr(after=None), None) is None


def test_a_draft_is_zero_as_commits_after_ready():
    assert after_owner_approval(_pr(after=0, is_draft=True), _approval("counted", after=3)) == 0


def test_an_approval_at_the_head_is_zero():
    assert after_owner_approval(_pr(after=4), _approval("head")) == 0


def test_an_approval_newer_than_ready_counts_from_itself():
    assert after_owner_approval(_pr(after=4), _approval("counted", after=1)) == 1


def test_a_ready_event_newer_than_the_approval_wins():
    older = _approval("counted", at=READY_T - 60, after=5)
    assert after_owner_approval(_pr(after=1), older) == 1
    assert "marked ready after it" in why_text(_pr(after=1), older)


def test_an_approval_on_a_pr_opened_ready_stops_counting_from_its_opening():
    # No ready event: ready_at is the PR's creation; the approval is newer.
    assert after_owner_approval(_pr(after=9), _approval("counted", after=0)) == 0


def test_a_lost_approval_counts_from_ready_or_is_unknown():
    lost = _approval("lost")
    assert after_owner_approval(_pr(after=3), lost) == 3
    assert "no longer in the PR's history" in why_text(_pr(after=3), lost)
    assert after_owner_approval(_pr(after=None, ready_at=""), lost) is None
    assert why_text(_pr(after=None, ready_at=""), lost).endswith("unknown")


def test_not_counted_yet_is_unknown_never_fewer():
    assert after_owner_approval(_pr(after=3), _approval("pending")) is None


# --- counting with real git ------------------------------------------------------


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path)


def test_commits_since_the_approved_commit(repo):
    merges = BaseMerges(version=OLD, fetch=False)
    root = str(repo.root)
    approved = repo.feature()
    assert merges.commits_since(root, approved, approved, "main") == (0, 0)
    # A clean merge of the base after the approval: not counted.
    merged = repo.merge()
    assert merges.commits_since(root, approved, merged, "main") == (0, 1)
    # A real commit after it: counted (the base's own commits never are).
    repo.edit("other.txt", "other\n", "a fix\n")
    fixed = repo.commit("fix")
    assert merges.commits_since(root, approved, fixed, "main") == (1, 1)


def test_a_force_push_that_drops_the_approved_commit_is_lost(repo):
    merges = BaseMerges(version=OLD, fetch=False)
    approved = repo.feature()
    repo.edit("other.txt", "other\n", "amended\n")
    repo.git("commit", "-q", "-a", "--amend", "--no-edit")
    assert merges.commits_since(str(repo.root), approved, repo.git("rev-parse", "HEAD"),
                                "main") is None
    assert merges.commits_since(str(repo.root), "f" * 40, approved, "main") is None
    assert merges.commits_since(str(repo.root), "not a sha", approved, "main") is None


# --- the engine --------------------------------------------------------------------


def _engine(pr: PrInfo | None = None):
    engine = make_engine([worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)])
    engine.status.clock = lambda: NOW
    engine.worktree_service._gh_available = False
    engine.worktree_service._pr_infos[FEAT] = pr if pr is not None else _pr()
    engine._tokens[TOKEN] = (FEAT, "a1")
    engine.registry.states[FEAT] = {"a1": AgentState(status=AgentStatus.WAITING,
                                                     agent_id="a1")}
    return engine


def _set_pr(engine, pr: PrInfo) -> None:
    engine.worktree_service._pr_infos[FEAT] = pr


def _rule_id(why) -> str:
    """The rule that decided the row (under the queue: its top item's)."""
    items = why.get("items") or []
    return items[0]["rule_id"] if why["rule"]["id"] == "queue" and items else why["rule"]["id"]


async def _rule_row(engine, me: str = "me"):
    # No agent there: its finished turn would lead the row (the queue).
    engine.registry.states.pop(FEAT, None)
    engine.review_requests.viewer = me
    await engine.owner_approvals.refresh()
    engine.status.flush()
    return engine.status.explain(KEY)


@pytest.mark.asyncio
async def test_the_agent_records_the_pushed_head():
    engine = _engine()
    out = await EngineMethods(engine).owner_approve({"caller": TOKEN, "note": "after the fix"})
    assert (out["key"], out["sha"], out["source"], out["agent_id"]) == (KEY, HEAD, "agent", "a1")
    assert out["state"] == "head" and out["note"] == "after the fix"
    entry = engine.overlay.entries(KEY)[-1]
    assert (entry["kind"], entry["actor"]) == ("owner_approval", "agent:a1")
    assert entry["text"] == "you approved aaaaaaa (recorded by agent a1)"


@pytest.mark.asyncio
async def test_the_agent_is_warned_when_its_head_is_not_pushed():
    engine = _engine(_pr(head=NEXT))
    out = await EngineMethods(engine).owner_approve({"caller": TOKEN})
    assert out["sha"] == NEXT
    assert "your worktree is at abc123" in out["warning"]


@pytest.mark.asyncio
async def test_the_orchestrator_and_unknown_tokens_are_refused():
    engine = _engine()
    add_session(engine, type("S", (), {"worktree_path": ORCHESTRATOR_KEY, "agent_id": "",
                                        "running": True})(), kind="orchestrator")
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="the orchestrator cannot approve"):
        await methods.owner_approve({"caller": "tok-orch", "key": KEY})
    with pytest.raises(ValueError, match="Unknown caller token"):
        await methods.owner_approve({"caller": "tok-gone"})
    assert engine.overlay.owner_approvals == {}


@pytest.mark.asyncio
async def test_the_user_approves_the_head_shown_and_is_refused_once_it_moved():
    engine = _engine()
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="moved since you looked"):
        await methods.owner_approve({"key": "feat", "sha": NEXT[:7]})
    with pytest.raises(ValueError, match="source must be home or cli"):
        await methods.owner_approve({"key": "feat", "source": "agent"})
    out = await methods.owner_approve({"key": "#7", "sha": HEAD[:9], "source": "cli"})
    assert (out["key"], out["source"]) == (KEY, "cli")
    with pytest.raises(ValueError, match="No work item"):
        await methods.owner_approve({"key": "#99"})


@pytest.mark.asyncio
async def test_no_open_pr_no_approval():
    engine = _engine(replace(_pr(), state="MERGED"))
    with pytest.raises(ValueError, match="no open PR"):
        await EngineMethods(engine).owner_approve({"caller": TOKEN})


@pytest.mark.asyncio
async def test_revoke_and_undo_bring_back_the_approval_it_replaced():
    engine = _engine()
    methods = EngineMethods(engine)
    await methods.owner_approve({"key": KEY})
    _set_pr(engine, _pr(head=NEXT))
    second = await methods.owner_approve({"key": KEY, "sha": NEXT})
    assert second["has_previous"] is True
    with pytest.raises(ValueError, match="the user's"):
        await methods.owner_approval_revoke({"key": KEY, "caller": TOKEN})
    with pytest.raises(ValueError, match="not aaaaaaa"):
        await methods.owner_approval_revoke({"key": KEY, "sha": HEAD[:7]})
    undone = await methods.owner_approval_revoke({"key": KEY, "undo": True})
    assert undone["standing"]["sha"] == HEAD
    revoked = await methods.owner_approval_revoke({"key": KEY})
    assert revoked["standing"] is None and engine.overlay.owner_approvals == {}
    with pytest.raises(ValueError, match="no approval"):
        await methods.owner_approval_revoke({"key": KEY})
    texts = [e["text"] for e in engine.overlay.entries(KEY) if e["kind"] == "owner_approval"]
    assert texts[-2:] == ["undone: your approval of bbbbbbb (aaaaaaa stands)",
                          "you took back your approval of aaaaaaa"]


def test_an_approval_persists_and_goes_with_the_item(tmp_path):
    path = tmp_path / "status.json"
    overlay = Overlay()
    overlay.open(path)
    overlay.owner_approvals[KEY] = {"key": KEY, "sha": HEAD, "at": NOW, "source": "home",
                                    "pr": 7}
    overlay.owner_approvals["bad"] = {"sha": 3}
    overlay.save()
    again = Overlay()
    again.open(path)
    assert list(again.owner_approvals) == [KEY]
    again.forget(KEY)
    assert again.owner_approvals == {}


@pytest.mark.asyncio
async def test_an_approval_of_another_pr_on_the_branch_is_not_this_ones():
    engine = _engine()
    await EngineMethods(engine).owner_approve({"key": KEY})
    assert engine.owner_approvals.fact(KEY, replace(_pr(), number=8)) is None


# --- the README recipe, end to end ---------------------------------------------------


class _Counts:
    """BaseMerges.commits_since with canned answers."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def commits_since(self, root, since, head, base):
        self.calls.append((since, head))
        return self.answers.get((since, head))


@pytest.mark.asyncio
async def test_case_one_a_real_commit_after_ready_then_the_owner_approves_again():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=NEXT, after=1))
        engine.rules.reload()
        engine.worktree_service.base_merges = _Counts({})
        why = await _rule_row(engine)
        assert _rule_id(why) == "owner_review_again"
        await EngineMethods(engine).owner_approve({"key": KEY, "sha": NEXT[:7]})
        why = await _rule_row(engine)
        assert _rule_id(why) != "owner_review_again"
        assert why["owner_approval"]["text"].endswith("the PR's head, no commits since")


@pytest.mark.asyncio
async def test_a_commit_after_the_approval_brings_the_review_back_a_clean_merge_does_not():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=HEAD, after=3))
        engine.rules.reload()
        await EngineMethods(engine).owner_approve({"key": KEY})
        counts = _Counts({(HEAD, NEXT): (0, 1), (HEAD, "c" * 40): (1, 1)})
        engine.worktree_service.base_merges = counts
        _set_pr(engine, _pr(head=NEXT, after=4))
        why = await _rule_row(engine)
        assert _rule_id(why) != "owner_review_again"
        assert "0 commits since (1 clean merge of the base not counted)" in \
            why["owner_approval"]["text"]
        _set_pr(engine, _pr(head="c" * 40, after=5))
        why = await _rule_row(engine)
        assert _rule_id(why) == "owner_review_again"
        # Counted once per (approved commit, head).
        await engine.owner_approvals.refresh()
        assert counts.calls == [(HEAD, NEXT), (HEAD, "c" * 40)]


@pytest.mark.asyncio
async def test_a_force_push_after_the_approval_counts_from_ready_again():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=HEAD, after=0))
        engine.rules.reload()
        await EngineMethods(engine).owner_approve({"key": KEY})
        engine.worktree_service.base_merges = _Counts({})       # lost: None
        _set_pr(engine, _pr(head=NEXT, after=2))
        why = await _rule_row(engine)
        assert _rule_id(why) == "owner_review_again"
        assert "counting from the ready event" in why["owner_approval"]["text"]
        row = engine.status.explain(KEY)
        assert row["owner_approval"]["state"] == "lost"


@pytest.mark.asyncio
async def test_approvable_on_the_users_own_review_lines_only():
    with _rules_files({GLOBAL_RULES: readme_recipe()}):
        engine = _engine(_pr(head=NEXT, after=1))
        engine.rules.reload()
        engine.worktree_service.base_merges = _Counts({})
        await _rule_row(engine)
        fields = engine.status.fields_of(KEY)
        assert fields["approvable"] == {"sha": NEXT, "pr": 7, "approved": False}
        await EngineMethods(engine).owner_approve({"key": KEY})
        await _rule_row(engine)
        approvable = engine.status.fields_of(KEY)["approvable"]
        # The line is no longer a review line once approved, unless it is.
        assert approvable is None or approvable["approved"] is True
        # A teammate's PR: never.
        await _rule_row(engine, me="someone-else")
        assert engine.status.fields_of(KEY)["approvable"] is None
        # A draft of yours: always.
        _set_pr(engine, _pr(head=NEXT, after=0, is_draft=True))
        await _rule_row(engine)
        assert engine.status.fields_of(KEY)["approvable"]["sha"] == NEXT
