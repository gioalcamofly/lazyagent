"""Attestations for nondeterministic CI checks, mechanism B (proposal 0014
R5, engine/attest.py): the proposal's worked example end to end (a
delegated Fix CI, mechanism A clears unit-tests, the agent attests
eval-suite, a Confirm item, Accept → the chip and Merge; Reject → Fix CI
back and the comment delivered), validation, expiry (a new commit, the base
point, the TTL), revoke, a check accept_attestation_for does not list,
``confirm = "none"``, the orchestrator's mandate, auto-delegation's brief
for a nondeterministic check and the skip list. Every ``gh`` is
:class:`tests.engine.fake_gh.FakeGh`."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.orchestrator import AWAY_ACTIONS, PRESENT_ACTIONS
from lazyagent.engine.rules.facts import FACTS
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo

from tests.engine.fake_gh import FakeGh, jobs_json, run_json
from tests.engine.rules.test_review_facts import readme_recipe
from tests.engine.test_ci_tests import (
    BASE_P,
    CHECK,
    FEAT,
    HEAD,
    JOB,
    KEY,
    NOW,
    TWO,
    WF,
    _engine,
    _items,
    _log,
    _poll,
    _rows,
    _upsert,
)
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

EVAL = "eval-suite"
EVAL_CHECK = f"{WF}/{EVAL}"
NEW_HEAD = "f" * 40
TOKEN = "tok-a1"

CONFIG = """version = 1

[ci]
nondeterministic = ["eval-suite"]
accept_attestation_for = ["eval-suite"]

[ci.check."unit-tests"]
tests_from = ["junit:junit-*", "log:pytest"]
"""

# The suite on main: five recent runs (71-78 %), older than the run at P.
BASE_RATES = {31: 71, 32: 75, 33: 78, 34: 73, 35: 76}


def _gh() -> FakeGh:
    runs = [run_json(11, "feat", HEAD, path=WF, event="pull_request",
                     created="2026-10-24T10:00:00Z"),
            run_json(21, "main", BASE_P, path=WF, event="push", created="2026-10-24T09:00:00Z")]
    for i, run_id in enumerate(BASE_RATES):
        runs.append(run_json(run_id, "main", f"{run_id:x}" * 20, path=WF, event="push",
                             conclusion="success", created=f"2026-10-2{i}T08:00:00Z"))
    gh = FakeGh(runs)
    gh.jobs[11] = jobs_json((JOB, "Run pytest"), (EVAL, "Run evals"))
    gh.jobs[21] = jobs_json((JOB, "Run pytest"), passed=(EVAL,))
    for run_id in BASE_RATES:
        gh.jobs[run_id] = jobs_json(passed=(JOB, EVAL))
    # The agent's own run of the suite on the head (a workflow_dispatch,
    # not in the window).
    gh.run_details[12] = run_json(12, "feat", HEAD, path=WF, event="workflow_dispatch",
                                  created="2026-10-24T11:00:00Z")
    gh.jobs[12] = jobs_json((EVAL, "Run evals"), passed=(JOB,))
    gh.logs[11] = _log(TWO)
    gh.logs[21] = _log(TWO)
    gh.merge_refs[7] = [BASE_P, HEAD]
    return gh


def _pr(sha=HEAD, **kw) -> PrInfo:
    checks = [CiCheck(JOB, "COMPLETED", "FAILURE", "https://github.com/o/r/actions/runs/11/job/1"),
              CiCheck(EVAL, "COMPLETED", "FAILURE", "https://github.com/o/r/actions/runs/11/job/2")]
    return PrInfo(7, "Feat", "OPEN", checks, head_sha=sha, base_ref="main", **kw)


GATE_PR = dict(review_decision="APPROVED", mergeable="MERGEABLE", is_draft=False, labels=[],
               approved_by=["peer"], commits_after_ready=0, author="me")


def _setup(gh=None, pr=None):
    engine, hub, clock = _engine(gh or _gh(), pr=pr or _pr())
    engine._tokens[TOKEN] = (FEAT, "a1")
    engine._agents.setdefault(FEAT, {})
    from lazyagent.engine.engine import AgentInfo, _Agent

    engine._agents[FEAT]["a1"] = _Agent(None, AgentInfo(FEAT, "a1", "s", "", "", "agent"),
                                        lambda: None)
    engine.send_agent_input = AsyncMock()  # type: ignore[method-assign]
    _agent(engine, "waiting")
    return engine, hub, clock


def _agent(engine, status: str) -> None:
    engine.registry.states.setdefault(FEAT, {})["a1"] = AgentState(
        status=AgentStatus(status), agent_id="a1")


def _params(**changes):
    params = {
        "caller": TOKEN, "check": EVAL, "verdict": "unrelated",
        "reason": "pass rate within the base's range over 5 base runs; failing cases differ "
                  "every run",
        "sha": HEAD[:7],
        "base_runs": [{"run_id": r, "passed": p, "total": 100} for r, p in BASE_RATES.items()],
        "own_runs": [{"passed": 74, "total": 100, "where": "local", "at": NOW - 600},
                     {"passed": 75, "total": 100, "where": "local", "at": NOW - 300},
                     {"run_id": 12, "passed": 76, "total": 100}],
        "method": "range",
    }
    params.update(changes)
    return params


async def _attest(engine, **changes):
    return await EngineMethods(engine).ci_attest(_params(**changes))


def _timeline(engine, kind):
    return [e["text"] for e in engine.overlay.timeline.get(KEY, []) if e["kind"] == kind]


# ----------------------------------------------------------------------
# The worked example
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worked_example_accept_gives_the_chip_and_merge():
    engine, _, clock = _setup(pr=_pr(**GATE_PR))
    engine.review_requests.viewer = "me"
    await _upsert(engine)
    recipe = readme_recipe()
    with _rules_files({GLOBAL_RULES: recipe + "\n" + CONFIG.replace("version = 1\n", "")}):
        engine.rules.reload()
        assert not engine.rules.errors
        await _poll(engine)
        # 1. CI red: A has explained unit-tests; eval-suite is related.
        [fix] = _items(engine, "ci")
        assert fix["state"] == "CI red: eval-suite"
        t = engine.status.facts(KEY).ci_tests
        assert t.unrelated_checks == {CHECK}
        assert _items(engine, "rule") == []
        # 2. The user delegates it; the agent runs.
        engine.overlay.delegations[KEY] = {
            "agent": "a1", "item": fix["id"], "occurrence": fix["occurrence"],
            "fact": "CI red: eval-suite", "at": NOW, "by": "user", "phase": "on_it",
            "ran": True, "sent_at": NOW}
        _agent(engine, "running")
        await _poll(engine, clock)
        # 3. The agent read 5 base runs, ran the suite 3 times, attests.
        out = await _attest(engine)
        assert out["state"] == "pending" and out["id"] == "att1"
        assert out["checks"] == [EVAL_CHECK] and out["sha"] == HEAD
        assert out["text"] == "base 5 runs 71-78 %, branch 3 runs (2 local) 74-76 %"
        rng = out["evidence"]["range"]
        assert (rng["base_min"], rng["own_min"], rng["drop"], rng["within"]) == (0.71, 0.74, 0.0,
                                                                              True)
        assert out["evidence"]["base_runs"][0]["sha"] == "1f" * 20
        assert _timeline(engine, "attest") == [
            "eval-suite attested by agent a1 at aaaaaaa: base 5 runs 71-78 %, branch 3 runs "
            "(2 local) 74-76 % (pending)"]
        # 4. A Confirm item stands for it; the agent's turn ends: the
        # delegation is over, the failures unrelated, not "still CI red".
        _agent(engine, "waiting")
        engine.status.flush()
        [confirm] = _items(engine, "attestation_confirm")
        assert confirm["state"] == "Confirm unrelated: eval-suite"
        assert confirm["level"] == "round" and confirm["class"] == "quick"
        assert confirm["attestation"]["id"] == "att1"
        assert confirm["detail"].startswith("agent a1 says eval-suite's failure is unrelated: "
                                            "base 5 runs 71-78 %")
        assert "drop 0 points vs max 5" in confirm["detail"]
        assert _items(engine, "ci") == []
        assert KEY not in engine.overlay.delegations
        assert _timeline(engine, "delegate")[-1] == (
            "agent a1 finished: CI failures unrelated (base also red: 2 tests; eval-suite "
            "attested)")
        # Pending counts as related: no Merge yet, no chip.
        assert _items(engine, "rule") == []
        assert (await _rows(engine))[KEY]["ci_note"] is None
        # 5. The user accepts: every failing check explained, the chip, Merge.
        methods = EngineMethods(engine)
        accepted = await methods.ci_attestation_confirm({"id": "att1", "accept": True})
        assert accepted["state"] == "accepted" and accepted["confirmed_by"] == "user"
        engine.status.flush()
        assert _items(engine, "attestation_confirm") == []
        assert _items(engine, "ci") == []
        row = (await _rows(engine))[KEY]
        assert row["ci_note"] == ("CI red · unrelated (base also red: 2 tests; eval-suite "
                                  "attested by agent a1)")
        [merge] = _items(engine, "rule")
        assert merge["verb"] == "Merge"
        facts = engine.status.facts(KEY)
        assert facts.ci_tests.attested == {EVAL_CHECK}
        # Why? names it and lists it with its state.
        why = await methods.rules_explain({"key": KEY})
        [b] = [c for c in why["ci"]["checks"] if c["mechanism"] == "B"]
        assert b["detail"] == ("attested by agent a1 at aaaaaaa: base 5 runs 71-78 %, branch "
                               "3 runs (2 local) 74-76 %; confirmed by you")
        assert [a["id"] for a in why["attestations"]] == ["att1"]
        assert why["attestations"][0]["state"] == "accepted"


@pytest.mark.asyncio
async def test_the_worked_example_reject_brings_fix_ci_back_and_tells_the_agent():
    engine, _, clock = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        [first] = _items(engine, "ci")
        _agent(engine, "running")
        await _attest(engine)
        _agent(engine, "waiting")
        engine.status.flush()
        assert _items(engine, "ci") == []
        [confirm] = _items(engine, "attestation_confirm")
        clock.now += 60
        out = await EngineMethods(engine).ci_attestation_confirm(
            {"id": "att1", "accept": False, "comment": "the drop on case 14 is new: look again"})
        assert out["state"] == "rejected"
        await engine.wait_idle()
        engine.send_agent_input.assert_awaited_once_with(
            FEAT, "a1", "Attestation att1 for eval-suite rejected by the user: the drop on "
                        "case 14 is new: look again")
        engine.status.flush()
        assert _items(engine, "attestation_confirm") == []
        [back] = _items(engine, "ci")
        assert back["state"] == "CI red: eval-suite (attestation rejected)"
        # A new occurrence, ready for d again.
        assert back["occurrence"] != first["occurrence"]
        assert _timeline(engine, "attest")[-1] == (
            "attestation att1 for eval-suite rejected: rejected by the user")
        # Rejected is final: no second answer.
        with pytest.raises(ValueError, match="rejected: nothing to confirm"):
            await EngineMethods(engine).ci_attestation_confirm({"id": "att1", "accept": True})


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_refuses_what_actions_or_the_numbers_contradict():
    engine, _, clock = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        cases = [
            # A check not listed as nondeterministic: mechanism A's.
            (dict(check="unit-tests"), "not listed in \\[ci\\] nondeterministic"),
            (dict(sha="1234567"), "sha must be the head, aaaaaaa"),
            (dict(base_runs=_params()["base_runs"][:4]), "base_runs: 4 given, 5 needed"),
            (dict(own_runs=_params()["own_runs"][:2]), "own_runs: 2 given, 3 needed"),
            (dict(base_runs=[*_params()["base_runs"][:4], {"run_id": 99, "passed": 1,
                                                         "total": 2}]),
             "Run 99 does not exist"),
            # The run at P is a run of main, not of the head; the head's
            # own red run is not a base run.
            (dict(own_runs=[*_params()["own_runs"][:2], {"run_id": 21, "passed": 1,
                                                       "total": 2}]),
             "own run 21 ran at ccccccc, not on the head"),
            (dict(base_runs=[*_params()["base_runs"][:4], {"run_id": 11, "passed": 70,
                                                         "total": 100}]),
             "base run 11 ran on feat, not on the base branch main"),
            (dict(base_runs=[*_params()["base_runs"][:4], {"run_id": 31, "passed": 101,
                                                         "total": 100}]),
             "passed must be from 0 to total"),
            (dict(base_runs=[*_params()["base_runs"][:4], {"passed": 70, "total": 100}]),
             "a base run needs its run_id"),
            # The engine recomputes the range: 62 % is 9 points below 71 %.
            (dict(own_runs=[{"passed": 62, "total": 100}, *_params()["own_runs"][1:]]),
             "62 % is 9 points below the base's lowest 71 %"),
            (dict(verdict="related"), 'verdict must be "unrelated"'),
            (dict(reason=" "), "reason is required"),
            (dict(base_runs=[*_params()["base_runs"][:4], {"run_id": 31, "passed": 70,
                                                         "total": 100}]),
             "cited twice"),
        ]
        for changes, message in cases:
            # Each run is checked once (cached), inside the hub's calls a
            # minute.
            clock.now += 61
            with pytest.raises(ValueError, match=message):
                await _attest(engine, **changes)
        assert engine.overlay.attestations == {}
        # A job of another name in a base run is not that check.
        gh: FakeGh = engine.actions.run  # type: ignore[assignment]
        gh.jobs[35] = jobs_json(passed=(JOB,))
        engine.attestations._runs.clear()
        clock.now += 61
        with pytest.raises(ValueError, match="Run 35 has no eval-suite job"):
            await _attest(engine)
        # Only a worker's token, or the orchestrator's: never the user.
        with pytest.raises(ValueError, match="needs the agent's token"):
            await _attest(engine, caller=None)


@pytest.mark.asyncio
async def test_another_method_is_recorded_with_the_range_and_always_asks_the_user():
    engine, _, _ = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG.replace(
            '[ci.check."unit-tests"]', '[ci.check."eval-suite"]\nconfirm = "none"\n\n'
                                       '[ci.check."unit-tests"]')}):
        engine.rules.reload()
        await _poll(engine)
        out = await _attest(engine, method="other",
                            own_runs=[{"passed": 62, "total": 100}, *_params()["own_runs"][1:]])
        assert out["state"] == "pending" and out["confirm"] == "user"
        assert out["range_text"] == "drop 9 points vs max 5: outside the base's range"


# ----------------------------------------------------------------------
# Expiry and revoke
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_commit_expires_it():
    engine, _, clock = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        await _attest(engine)
        await EngineMethods(engine).ci_attestation_confirm({"id": "att1", "accept": True})
        engine.status.flush()
        assert _items(engine, "ci") == []
        # A new commit; its run is red in the same way.
        gh: FakeGh = engine.actions.run  # type: ignore[assignment]
        gh.runs.append(run_json(13, "feat", NEW_HEAD, path=WF, event="pull_request",
                                created="2026-10-24T12:00:00Z"))
        gh.etag = '"e2"'
        gh.jobs[13] = gh.jobs[11]
        gh.logs[13] = _log(TWO)
        gh.merge_refs[7] = [BASE_P, NEW_HEAD]
        engine.worktree_service._pr_infos[FEAT] = _pr(sha=NEW_HEAD)
        engine.actions.read_heads = lambda: {"feat": NEW_HEAD}
        await _poll(engine, clock)
        await _poll(engine, clock)
        assert engine.overlay.attestations["att1"]["state"] == "expired"
        assert _timeline(engine, "attest")[-1] == (
            "attestation att1 for eval-suite expired: head moved")
        [item] = _items(engine, "ci")
        assert item["state"] == "CI red: eval-suite (attestation expired)"
        [c] = [c for c in engine.status.facts(KEY).ci_tests.checks if c.attest_note]
        assert c.detail == "attestation att1 expired: head moved"


@pytest.mark.asyncio
async def test_the_ttl_and_a_moved_base_point_expire_it():
    engine, _, clock = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG.replace(
            'accept_attestation_for = ["eval-suite"]',
            'accept_attestation_for = ["eval-suite"]\nattestation_ttl = "2h"')}):
        engine.rules.reload()
        await _poll(engine)
        out = await _attest(engine)
        assert out["expires_at"] == out["at"] + 7200
        assert out["point"] == BASE_P and out["compared"] == "main at ccccccc"
        clock.now += 7200
        engine.status.flush()
        assert engine.overlay.attestations["att1"]["state"] == "expired"
        assert engine.overlay.attestations["att1"]["ended"] == "older than 2 h"
        assert _items(engine, "attestation_confirm") == []
        # The base point: the comparison point read for the check moves.
        out = await _attest(engine)
        assert out["state"] == "pending"
        hub = engine.actions
        hub.tests._merge_ref[11] = ("9" * 40, "")
        engine.status.flush()
        assert engine.overlay.attestations[out["id"]]["state"] == "expired"
        assert engine.overlay.attestations[out["id"]]["ended"] == "base point moved"


@pytest.mark.asyncio
async def test_revoke_from_why_makes_the_check_related_again():
    engine, _, _ = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG.replace(
            '[ci.check."unit-tests"]', '[ci.check."eval-suite"]\nconfirm = "none"\n\n'
                                       '[ci.check."unit-tests"]')}):
        engine.rules.reload()
        await _poll(engine)
        out = await _attest(engine)
        # confirm = "none": accepted at once, no Confirm item.
        assert out["state"] == "accepted" and out["confirmed_by"] == "rule"
        engine.status.flush()
        assert _items(engine, "attestation_confirm") == []
        assert _items(engine, "ci") == []
        row = (await _rows(engine))[KEY]
        assert row["ci_note"].endswith("eval-suite attested by agent a1)")
        methods = EngineMethods(engine)
        with pytest.raises(ValueError, match="the user's"):
            await methods.ci_attestation_revoke({"id": "att1", "caller": TOKEN})
        revoked = await methods.ci_attestation_revoke({"id": "att1"})
        assert revoked["state"] == "revoked"
        engine.status.flush()
        [item] = _items(engine, "ci")
        assert item["state"] == "CI red: eval-suite (attestation revoked)"
        with pytest.raises(ValueError, match="revoked: nothing to revoke"):
            await methods.ci_attestation_revoke({"id": "att1"})
        with pytest.raises(ValueError, match="No attestation 'att9'"):
            await methods.ci_attestation_revoke({"id": "att9"})


@pytest.mark.asyncio
async def test_a_check_not_in_accept_attestation_for_is_recorded_and_changes_nothing():
    engine, _, _ = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG.replace(
            'accept_attestation_for = ["eval-suite"]\n', "")}):
        engine.rules.reload()
        await _poll(engine)
        out = await _attest(engine)
        assert out["state"] == "recorded" and out["accepted_for"] is False
        engine.status.flush()
        assert _items(engine, "attestation_confirm") == []
        [item] = _items(engine, "ci")
        assert item["state"] == "CI red: eval-suite"
        [b] = [c for c in engine.status.facts(KEY).ci_tests.checks if c.attestation]
        assert not b.unrelated and "does not list it" in b.detail


@pytest.mark.asyncio
async def test_a_new_attestation_replaces_the_standing_one():
    engine, _, _ = _setup()
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        await _attest(engine)
        second = await _attest(engine)
        assert second["id"] == "att2"
        assert engine.overlay.attestations["att1"]["state"] == "expired"
        assert engine.overlay.attestations["att1"]["ended"] == "replaced by att2"
        engine.status.flush()
        [confirm] = _items(engine, "attestation_confirm")
        assert confirm["attestation"]["id"] == "att2"


# ----------------------------------------------------------------------
# The orchestrator, the facts, the store
# ----------------------------------------------------------------------


def test_attest_ci_is_in_the_away_list_only():
    assert "attest_ci" in AWAY_ACTIONS and "attest_ci" not in PRESENT_ACTIONS


@pytest.mark.asyncio
async def test_the_orchestrator_attests_within_its_mandate_only():
    from lazyagent.engine.registry import ORCHESTRATOR_KEY

    engine, _, _ = _setup()
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "o1")
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        engine.first_responder.presence.connected = lambda: True
        engine.first_responder.presence.set("present")
        with pytest.raises(ValueError, match="Not within your mandate"):
            await _attest(engine, caller="tok-orch", key=KEY)
        engine.first_responder.presence.set("away")
        with pytest.raises(ValueError, match="key is required"):
            await _attest(engine, caller="tok-orch")
        out = await _attest(engine, caller="tok-orch", key="feat")
        assert out["actor"] == "orchestrator" and out["actor_text"] == "the orchestrator"
        actions = [e for e in engine.overlay.timeline[KEY] if e["kind"] == "action"]
        assert actions[-1]["action"] == "attest_ci" and actions[-1]["mandate"] == "away"


def test_the_facts_are_registered():
    for name in ("ci.attested", "ci.attestation_pending", "ci.first_attestation_pending"):
        assert FACTS[name].cost == "free"
        assert FACTS[name].row is not None and FACTS[name].item is not None


@pytest.mark.asyncio
async def test_attestations_survive_a_restart(tmp_path):
    engine, _, _ = _setup()
    path = tmp_path / "overlay.json"
    engine.overlay.open(path)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        await _attest(engine)
        engine.overlay.save()
        engine2, _, _ = _setup()
        engine2.overlay.open(path)
        assert engine2.overlay.attestations["att1"]["state"] == "pending"
        assert engine2.overlay.next_attestation == 2
        engine2.rules.reload()
        await _poll(engine2)
        [confirm] = _items(engine2, "attestation_confirm")
        assert confirm["attestation"]["id"] == "att1"


def test_nothing_attested_keeps_the_overlay_file_as_before():
    from lazyagent.engine.overlay import Overlay

    assert "attestations" not in Overlay()._serialize()


# ----------------------------------------------------------------------
# Auto-delegation of a nondeterministic check (with the skip list)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_delegation_asks_to_investigate_a_nondeterministic_check_not_to_fix_it():
    from tests.engine.test_actions import _agent as _actions_agent
    from tests.engine.test_actions import _pass, _skip_engine, _two_jobs_gh

    engine, hub, _ = _skip_engine(_two_jobs_gh(), [])
    with _rules_files({GLOBAL_RULES: 'version = 1\n[ci]\nnondeterministic = ["eval-suite"]\n'}):
        engine.rules.reload()
        await hub.poll(["feat"])
        session = _actions_agent(engine, "a1", "waiting")
        await _pass(engine)
    [text] = [c.args[0] for c in session.send_input.await_args_list]
    assert ("eval-suite is nondeterministic (the pass rate varies run to run; the user's [ci] "
            "nondeterministic): do not edit the test to make it pass.") in text
    assert ("if your instructions define a slot or lock for running this suite, request it "
            "before each run and release it after") in text
    assert "otherwise file ci_attest with that evidence." in text
    assert "skip list" not in text


@pytest.mark.asyncio
async def test_the_skip_list_wins_over_nondeterministic():
    from tests.engine.test_actions import _agent as _actions_agent
    from tests.engine.test_actions import _pass, _skip_engine, _two_jobs_gh

    engine, hub, _ = _skip_engine(_two_jobs_gh(), ["eval-*"])
    with _rules_files({GLOBAL_RULES: 'version = 1\n[ci]\nnondeterministic = ["eval-suite"]\n'}):
        engine.rules.reload()
        await hub.poll(["feat"])
        session = _actions_agent(engine, "a1", "waiting")
        await _pass(engine)
    [text] = [c.args[0] for c in session.send_input.await_args_list]
    # Delegated for unit; eval-suite is left alone, not investigated.
    assert "Leave the failing eval-suite alone" in text
    assert "nondeterministic" not in text


def test_the_readmes_attestation_example_loads_as_written():
    import re
    from pathlib import Path

    from lazyagent.engine.rules import builtin
    from lazyagent.engine.rules.user import compose, parse_file

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    section = readme.split("### Your rules (rules.toml)")[1].split("\n### ")[0]
    [block] = [b for b in re.findall(r"```toml\n(.*?)```", section, re.S)
               if "nondeterministic = " in b]
    f = parse_file("version = 1\n" + block, "global", GLOBAL_RULES)
    assert f.errors == [] and f.warnings == []
    ci = compose(builtin(), [f]).catalog.ci
    assert ci.nondeterministic == ("eval-suite",) and ci.attestation_ttl == 72 * 3600
    assert ci.is_nondeterministic(EVAL_CHECK, EVAL) and ci.accepts(EVAL_CHECK, EVAL)
    assert not ci.is_nondeterministic(CHECK, JOB)
    rules = ci.attest_rules(EVAL_CHECK, EVAL)
    assert (rules.confirm, rules.min_base_runs, rules.min_repetitions, rules.max_drop) == (
        "user", 8, 3, 0.05)
    assert ci.attest_rules(CHECK, JOB).min_base_runs == 5


def test_bad_attestation_keys_are_errors_and_left_out():
    from lazyagent.engine.ci_tests import parse_tables

    part, errors, _ = parse_tables(
        {"nondeterministic": "eval-suite", "attestation_ttl": "soon", "max_drop": 2,
         "check": {"eval-suite": {"confirm": "maybe"}, "evals": {"min_repetitions": 0}}}, None)
    assert part.attest == {} and part.attest_checks == {}
    assert len(errors) == 5
    assert any("nondeterministic must be a list" in e for e in errors)
    assert any('confirm must be "user"' in e for e in errors)
