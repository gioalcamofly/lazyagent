"""R5a (docs/design/rules-plan.md): the checks that gate a merge read from
branch protection and rulesets (engine/required_checks.py), and the bounded
re-run of jobs that failed for infrastructure reasons
(engine/infra_rerun.py). Every ``gh`` is :class:`tests.engine.fake_gh.FakeGh`."""

from __future__ import annotations

import logging

import pytest

from lazyagent.engine.actions import ActionsHub
from lazyagent.engine.ci_tests import parse_tables
from lazyagent.engine.github_health import GithubHealth
from lazyagent.engine.infra_rerun import InfraJob, infra_reason, parse_infra
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.required_checks import TTL, Required
from lazyagent.models import CiCheck, PrInfo
from tests.engine.fake_gh import FakeGh, infra_jobs, run_json
from tests.engine.test_ci_tests import (
    HEAD,
    JOB,
    KEY,
    WF,
    _engine,
    _gh,
    _items,
    _poll,
    _rows,
)
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

NOW = 1_700_000_000.0
RUN_URL = "https://github.com/o/r/actions/runs/11/job/1"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _check(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED",
           url: str = "") -> CiCheck:
    return CiCheck(name, status, conclusion, url or f"https://github.com/o/r/actions/runs/11/job/{name}")


def _pr(checks: list[CiCheck], base: str = "main") -> PrInfo:
    return PrInfo(7, "Feat", "OPEN", checks, head_sha=HEAD, base_ref=base)


RED_UNIT = CiCheck(JOB, "COMPLETED", "FAILURE", RUN_URL)


def _hub(gh: FakeGh, clock: Clock | None = None) -> ActionsHub:
    hub = ActionsHub("/tmp/repo", run=gh, read_heads=lambda: {"feat": HEAD},
                     clock=clock or Clock())
    hub.tests.state_path = None
    return hub


# ----------------------------------------------------------------------
# Part 1: the required set
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_required_set_comes_from_protection_contexts_and_checks():
    gh = FakeGh()
    gh.protection["main"] = ["lint", "unit-tests (3.12)"]
    hub = _hub(gh)
    assert await hub.required.fetch(["main"]) is True
    r = hub.required.for_base("main")
    assert r == Required(("lint", "unit-tests (3.12)"), "protection", "main", "main")
    # Two reads: the branch, its rulesets; no default branch needed.
    assert [c[-1] for c in gh.calls] == ["repos/{owner}/{repo}/branches/main",
                                         "repos/{owner}/{repo}/rules/branches/main"]


@pytest.mark.asyncio
async def test_rulesets_add_their_required_checks():
    gh = FakeGh()
    gh.protection["main"] = ["lint"]
    gh.rulesets["main"] = ["e2e", "LINT"]
    hub = _hub(gh)
    await hub.required.fetch(["main"])
    assert hub.required.for_base("main").names == ("e2e", "lint")


@pytest.mark.asyncio
async def test_rulesets_alone_on_an_unprotected_branch():
    gh = FakeGh()
    gh.protection["main"] = []
    gh.rulesets["main"] = ["build"]
    hub = _hub(gh)
    await hub.required.fetch(["main"])
    assert hub.required.for_base("main").source == "protection"


@pytest.mark.asyncio
async def test_a_base_that_requires_nothing_borrows_the_default_branchs_set():
    gh = FakeGh()
    gh.protection["project-x"] = []
    gh.protection["main"] = ["lint"]
    hub = _hub(gh)
    await hub.required.fetch(["project-x"])
    r = hub.required.for_base("project-x")
    assert r == Required(("lint",), "default_branch", "project-x", "main")
    assert "repos/{owner}/{repo}" in [c[-1] for c in gh.calls]


@pytest.mark.asyncio
async def test_the_set_is_cached_for_six_hours_then_read_again():
    gh = FakeGh()
    gh.protection["main"] = ["lint"]
    clock = Clock()
    hub = _hub(gh, clock)
    await hub.required.fetch(["main"])
    calls = len(gh.calls)
    clock.now += TTL - 60
    assert await hub.required.fetch(["main"]) is False
    assert len(gh.calls) == calls
    gh.protection["main"] = ["lint", "build"]
    clock.now += 120
    assert await hub.required.fetch(["main"]) is True
    assert hub.required.for_base("main").names == ("build", "lint")


@pytest.mark.asyncio
async def test_an_unreadable_protection_gives_no_set_and_is_not_read_again_at_once():
    gh = FakeGh()
    gh.protection["main"] = 403
    hub = _hub(gh)
    await hub.required.fetch(["main"])
    assert hub.required.for_base("main") is None and not hub.required.known()
    calls = len(gh.calls)
    await hub.required.fetch(["main"])
    assert len(gh.calls) == calls


@pytest.mark.asyncio
async def test_no_answer_at_all_is_not_cached():
    gh = FakeGh()
    gh.protection["main"] = ["lint"]
    gh.queue.append((1, "", "error connecting to api.github.com"))
    hub = _hub(gh)
    hub.health = GithubHealth(hub.clock)
    await hub.required.fetch(["main"])
    assert "main" not in hub.required.branches
    assert hub.health.failures == 1


def test_names_match_as_the_rollup_names_them():
    r = Required(("Unit-Tests (3.12)", "ci/circleci: e2e"), "protection", "main", "main")
    assert r.covers("unit-tests (3.12)")
    assert r.covers("nope", "CI/CircleCI: e2e")
    assert not r.covers("unit-tests")  # the matrix values are part of the name
    assert Required(("Tests / lint",), "protection", "main", "main").covers("lint", "Tests / lint")


def test_all_passed_needs_every_required_check_reported_green():
    r = Required(("lint", "build"), "protection", "main", "main")
    assert r.all_passed(_pr([_check("lint"), _check("build", "SKIPPED"), RED_UNIT]))
    assert not r.all_passed(_pr([_check("lint"), RED_UNIT]))  # build never reported
    assert not r.all_passed(_pr([_check("lint"), _check("build", "", "IN_PROGRESS")]))
    assert not r.all_passed(None)


def test_the_r5a_keys_are_read_and_checked():
    part, errors, _ = parse_tables({"gating_from_protection": False, "rerun_infra": "yes",
                                    "rerun_infra_max_attempts": 2,
                                    "check": {"plain": {"gating": True}}}, None)
    assert part.options == {"gating_from_protection": False, "rerun_infra_max_attempts": 2}
    assert any("rerun_infra must be true or false" in e for e in errors)
    [plain] = part.checks
    assert plain.gating_set and plain.gate == "gating"
    _, errors, _ = parse_tables({"rerun_infra_max_attempts": 0}, None)
    assert errors == ["[ci] rerun_infra_max_attempts must be a number from 1 to 10"]


# --- On the rows ------------------------------------------------------------


def _protected(gh: FakeGh, *names: str) -> FakeGh:
    gh.protection["main"] = list(names)
    return gh


GREEN = [_check("lint"), _check("build")]


@pytest.mark.asyncio
async def test_a_red_that_is_not_required_while_every_required_check_passed_does_not_gate():
    engine, _, _ = _engine(_protected(_gh(), "lint", "build"), pr=_pr([RED_UNIT, *GREEN]))
    await _poll(engine)
    assert _items(engine, "ci") == []
    t = engine.status.facts(KEY).ci_tests
    assert t.gating_failures == [] and t.only_nongating and t.required_source == "protection"
    assert (await _rows(engine))[KEY]["ci_note"] == "unit-tests red · not gating"
    out = await EngineMethods(engine).rules_explain({"key": KEY})
    assert out["ci_required"] == {"source": "protection", "base": "main", "read_from": "main",
                                  "checks": ["build", "lint"]}
    assert "Required checks (branch protection of main): build, lint; " \
           "red but not gating: unit-tests." in out["answer"]


@pytest.mark.asyncio
async def test_a_required_red_gates():
    engine, _, _ = _engine(_protected(_gh(), "lint", JOB), pr=_pr([RED_UNIT, _check("lint")]))
    await _poll(engine)
    [item] = _items(engine, "ci")
    assert engine.status.facts(KEY).ci_tests.gating_failures == [f"{WF}/{JOB}"]


@pytest.mark.asyncio
async def test_a_red_that_is_not_required_gates_while_a_required_check_runs():
    engine, _, _ = _engine(_protected(_gh(), "lint", "build"),
                           pr=_pr([RED_UNIT, _check("lint"), _check("build", "", "IN_PROGRESS")]))
    await _poll(engine)
    assert engine.status.facts(KEY).ci_tests.gating_failures == [f"{WF}/{JOB}"]


@pytest.mark.asyncio
async def test_unreadable_protection_gates_every_red_as_before():
    gh = _gh()
    gh.protection["main"] = 403
    engine, _, _ = _engine(gh, pr=_pr([RED_UNIT, *GREEN]))
    await _poll(engine)
    assert len(_items(engine, "ci")) == 1
    # Nothing configured and nothing known: no verdict at all, as before R5a.
    assert engine.status.facts(KEY).ci_tests is None


@pytest.mark.asyncio
async def test_the_default_branchs_set_gates_a_project_branch():
    gh = _gh()
    gh.protection["project-x"] = []
    gh.protection["main"] = ["lint", "build"]
    engine, _, _ = _engine(gh, pr=_pr([RED_UNIT, *GREEN], base="project-x"))
    await _poll(engine)
    assert _items(engine, "ci") == []
    t = engine.status.facts(KEY).ci_tests
    assert t.required_source == "default_branch" and t.required.read_from == "main"


@pytest.mark.asyncio
async def test_config_wins_over_the_derived_set():
    # unit-tests is not required, but the rule file says it gates; lint is
    # required, but the rule file says it does not.
    rules = """version = 1

[ci.check."unit-tests"]
gating = true

[ci.check."lint"]
gating = false
"""
    engine, _, _ = _engine(_protected(_gh(), "lint", "build"), pr=_pr([RED_UNIT, *GREEN]))
    with _rules_files({GLOBAL_RULES: rules}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
        t = engine.status.facts(KEY).ci_tests
        assert t.gating_failures == [f"{WF}/{JOB}"]
        assert t.required_source == "protection"


@pytest.mark.asyncio
async def test_config_alone_is_the_source_when_protection_is_off():
    rules = """version = 1

[ci]
gating_from_protection = false

[ci.check."unit-tests"]
gating = false
"""
    gh = _protected(_gh(), "lint", "build")
    engine, _, _ = _engine(gh, pr=_pr([RED_UNIT, *GREEN]))
    with _rules_files({GLOBAL_RULES: rules}):
        engine.rules.reload()
        await _poll(engine)
        assert gh.required_calls() == []
        t = engine.status.facts(KEY).ci_tests
        assert t.required_source == "config" and t.required is None


@pytest.mark.asyncio
async def test_the_facts_read_the_source_and_the_set():
    from lazyagent.engine.rules.facts import FACTS, RowContext

    engine, _, clock = _engine(_protected(_gh(), "lint", "build"), pr=_pr([RED_UNIT, *GREEN]))
    await _poll(engine)
    ctx = RowContext(engine.status.facts(KEY), clock.now, clock.now, 24)
    assert FACTS["ci.required_source"].row(ctx) == "protection"
    assert FACTS["ci.required_checks"].row(ctx) == {"lint", "build"}


@pytest.mark.asyncio
async def test_a_github_outage_makes_no_gh_call():
    gh = _protected(_gh(), "lint")
    engine, hub, clock = _engine(gh, pr=_pr([RED_UNIT, _check("lint")]))
    health = engine.worktree_service.github
    health.failed("error connecting to api.github.com", clock.now)
    await _poll(engine)
    assert gh.calls == []
    # The hub itself does not call either, whoever polls it.
    hub.health = health
    assert await hub.poll(["feat"], force=True, bases=["main"]) is False
    assert gh.calls == []


# ----------------------------------------------------------------------
# Part 2: infrastructure failures
# ----------------------------------------------------------------------


LOST = "The self-hosted runner: spot-7 lost communication with the server."
SHUTDOWN = "The runner has received a shutdown signal. This can happen when ..."


@pytest.mark.parametrize("messages, reason", [
    ([SHUTDOWN], "runner shut down"),
    ([LOST], "runner lost"),
    # What a deliberate cancel leaves: not a runner loss.
    (["The operation was canceled."], None),
    ([], None),
])
def test_the_markers(messages, reason):
    job = InfraJob(5, "unit", "failure", concluded=1)
    assert infra_reason(job, messages) == reason


def test_no_step_concluded_is_infrastructure_and_a_failing_step_or_a_timeout_never_is():
    assert infra_reason(InfraJob(5, "unit", "cancelled", concluded=0), None) == "no step ran"
    assert infra_reason(InfraJob(5, "unit", "failure", failed_step="Run pytest"), [LOST]) is None
    assert infra_reason(InfraJob(5, "unit", "timed_out", concluded=0), [LOST]) is None


def test_parse_infra_reads_ids_steps_and_cancelled_jobs():
    body = infra_jobs((501, "unit", "failure", (("Set up job", "success"), ("Run", None))),
                      (502, "e2e", "cancelled", ()))
    import json

    jobs = parse_infra(json.dumps(body))
    assert [(j.id, j.name, j.conclusion, j.failed_step, j.concluded) for j in jobs] == [
        (501, "unit", "failure", "", 1), (502, "e2e", "cancelled", "", 0)]


def _infra_gh(attempt: int = 1, *, job: str = JOB, steps=(("Set up job", "success"),
              ("Run pytest", None)), workflow: str = "Tests") -> FakeGh:
    gh = FakeGh([run_json(11, "feat", HEAD, path=WF, event="pull_request", attempt=attempt,
                          workflow=workflow)])
    gh.jobs[11] = infra_jobs((500 + attempt, job, "failure", steps))
    gh.annotations[500 + attempt] = [LOST]
    return gh


async def _hub_poll(hub: ActionsHub, clock: Clock) -> bool:
    clock.now += 61
    return await hub.poll(["feat"], force=True)


@pytest.mark.asyncio
async def test_a_lost_runner_is_re_run_once_per_attempt_and_reads_as_running(caplog):
    gh = _infra_gh()
    clock = Clock()
    hub = _hub(gh, clock)
    seen = []
    hub.reruns.on_rerun = lambda *a: seen.append(a)
    with caplog.at_level(logging.INFO, "lazyagent.engine.infra_rerun"):
        assert await _hub_poll(hub, clock) is True
    assert gh.rerun_calls() == [["run", "rerun", "--job", "501"]]
    [(branch, run, job, reason, attempt)] = seen
    assert (branch, job.name, reason, attempt) == ("feat", JOB, "runner lost", 2)
    assert "re-ran unit-tests (3.12) on feat (runner lost), run 11 attempt 2" in caplog.text
    # GitHub has not shown the new attempt yet: queued, no second re-run.
    assert hub.verdict("feat").state == "running"
    await _hub_poll(hub, clock)
    assert len(gh.rerun_calls()) == 1

    # Attempt 2 lost its runner too: re-run again (a new job id).
    gh.set_runs([run_json(11, "feat", HEAD, path=WF, event="pull_request", attempt=2)], '"e2"')
    gh.jobs[11] = infra_jobs((502, JOB, "failure", (("Run pytest", None),)))
    gh.annotations[502] = [LOST]
    await _hub_poll(hub, clock)
    await _hub_poll(hub, clock)
    assert gh.rerun_calls()[-1] == ["run", "rerun", "--job", "502"]

    # Attempt 3: the cap. Red stays red.
    gh.set_runs([run_json(11, "feat", HEAD, path=WF, event="pull_request", attempt=3)], '"e3"')
    gh.jobs[11] = infra_jobs((503, JOB, "failure", (("Run pytest", None),)))
    gh.annotations[503] = [LOST]
    await _hub_poll(hub, clock)
    await _hub_poll(hub, clock)
    assert len(gh.rerun_calls()) == 2
    assert hub.verdict("feat").state == "red"


@pytest.mark.asyncio
async def test_a_failing_test_step_is_never_re_run():
    gh = _infra_gh(steps=(("Run pytest", "failure"),))
    clock = Clock()
    hub = _hub(gh, clock)
    await _hub_poll(hub, clock)
    assert gh.rerun_calls() == []


@pytest.mark.asyncio
async def test_a_run_with_another_real_failure_is_left_alone():
    # fail-fast: the sibling of a failing job is cancelled with no failed step.
    gh = _infra_gh()
    gh.jobs[11] = infra_jobs((501, JOB, "cancelled", ()),
                             (601, "unit-tests (3.11)", "failure", (("Run pytest", "failure"),)))
    clock = Clock()
    hub = _hub(gh, clock)
    await _hub_poll(hub, clock)
    assert gh.rerun_calls() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("job, workflow, rules", [
    ("eval-suite", "Tests", ""),
    (JOB, "Evals", ""),
    ("bench", "Tests", '[ci]\nnondeterministic = ["bench"]\n'),
    ("label-check", "Tests", '[ci.check."label-check"]\npolicy = true\n'),
])
async def test_evals_and_policy_checks_are_never_re_run(job, workflow, rules):
    gh = _infra_gh(job=job, workflow=workflow)
    clock = Clock()
    hub = _hub(gh, clock)
    if rules:
        from lazyagent.engine.ci_tests import merge_parts

        part, errors, _ = parse_tables(*_tables(rules))
        assert errors == []
        hub.tests.config = merge_parts([("global", part)])
    await _hub_poll(hub, clock)
    assert gh.rerun_calls() == []


def _tables(text: str):
    import tomllib

    data = tomllib.loads(text)
    return data.get("ci"), data.get("ci_unrelated")


@pytest.mark.asyncio
async def test_rerun_infra_false_turns_it_off_and_the_cap_is_configurable():
    from lazyagent.engine.ci_tests import merge_parts

    for text, attempt in (("[ci]\nrerun_infra = false\n", 1),
                          ("[ci]\nrerun_infra_max_attempts = 2\n", 2)):
        gh = _infra_gh(attempt)
        clock = Clock()
        hub = _hub(gh, clock)
        hub.tests.config = merge_parts([("global", parse_tables(*_tables(text))[0])])
        await _hub_poll(hub, clock)
        assert gh.rerun_calls() == []


@pytest.mark.asyncio
async def test_a_failed_rerun_call_is_not_retried_for_that_attempt():
    gh = _infra_gh()
    gh.rerun_code = 1
    clock = Clock()
    hub = _hub(gh, clock)
    await _hub_poll(hub, clock)
    await _hub_poll(hub, clock)
    assert len(gh.rerun_calls()) == 1
    assert hub.verdict("feat").state == "red"


@pytest.mark.asyncio
async def test_no_rerun_during_an_outage():
    gh = _infra_gh()
    clock = Clock()
    hub = _hub(gh, clock)
    hub.health = GithubHealth(clock)
    hub.health.failed("error connecting to api.github.com", clock.now)
    # Before the outage's next retry: not a single call.
    assert await hub.poll(["feat"], force=True) is False
    assert gh.calls == []


@pytest.mark.asyncio
async def test_on_the_row_a_re_run_reads_as_running_with_no_ci_item_and_a_timeline_entry():
    gh = _infra_gh()
    pr = PrInfo(7, "Feat", "OPEN", [CiCheck(JOB, "COMPLETED", "FAILURE", RUN_URL)],
                head_sha=HEAD, base_ref="main")
    engine, _, _ = _engine(gh, pr=pr)
    await _poll(engine)
    assert gh.rerun_calls() == [["run", "rerun", "--job", "501"]]
    assert engine.get_pr_info("/tmp/repo-feat").overall_status == "pending"
    assert _items(engine, "ci") == []
    assert (await _rows(engine))[KEY]["pr"]["ci"] == "running"
    texts = [e["text"] for e in engine.overlay.entries(KEY)]
    assert "re-ran unit-tests (3.12) (runner lost), attempt 2" in texts


def test_the_readmes_r5a_example_loads_as_written():
    import re
    import tomllib
    from pathlib import Path

    from lazyagent.engine.ci_tests import merge_parts

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    [block] = [b for b in re.findall(r"```toml\n(.*?)```", readme, re.S)
               if "rerun_infra_max_attempts" in b]
    part, errors, _ = parse_tables(tomllib.loads(block)["ci"], None)
    assert errors == []
    config = merge_parts([("global", part)])
    assert (config.gating_from_protection, config.rerun_infra,
            config.rerun_infra_max_attempts) == (True, True, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["manual", "concurrency"])
async def test_a_cancelled_run_is_never_re_run(event):
    # A person (Cancel, gh run cancel) or concurrency: cancel-in-progress
    # cancelled it: the run concludes cancelled, its job too, with "The
    # operation was canceled." and no failing step.
    gh = FakeGh([run_json(11, "feat", HEAD, path=WF, event="pull_request",
                          conclusion="cancelled")])
    steps = (("Set up job", "success"), ("Run pytest", "cancelled")) if event == "manual" else ()
    gh.jobs[11] = infra_jobs((501, JOB, "cancelled", steps))
    gh.annotations[501] = ["The operation was canceled."]
    clock = Clock()
    hub = _hub(gh, clock)
    await _hub_poll(hub, clock)
    assert hub.verdict("feat").state == "green"  # a cancelled run is not red
    # Even handed to the re-run with its jobs read, the run's conclusion stops it.
    import json

    hub._infra[(11, 1)] = parse_infra(json.dumps(gh.jobs[11]))
    assert await hub.reruns.run([("feat", hub.find_run(11))], hub.tests.config) is False
    assert gh.rerun_calls() == []


@pytest.mark.asyncio
async def test_a_cancelled_job_in_a_failed_run_needs_a_runner_loss_marker():
    gh = _infra_gh()
    gh.jobs[11] = infra_jobs((501, JOB, "cancelled", (("Set up job", "success"),
                                                      ("Run pytest", "cancelled"))))
    gh.annotations[501] = ["The operation was canceled."]
    clock = Clock()
    hub = _hub(gh, clock)
    await _hub_poll(hub, clock)
    assert gh.rerun_calls() == []
    # The same job with a shutdown signal is the runner's.
    gh2 = _infra_gh()
    gh2.jobs[11] = gh.jobs[11]
    gh2.annotations[501] = ["The operation was canceled.", SHUTDOWN]
    hub2 = _hub(gh2, clock)
    await _hub_poll(hub2, clock)
    assert gh2.rerun_calls() == [["run", "rerun", "--job", "501"]]


@pytest.mark.asyncio
async def test_a_runner_lost_run_on_an_older_head_is_ignored():
    old = "f" * 40
    gh = FakeGh([run_json(12, "feat", HEAD, path=WF, event="pull_request", conclusion="success",
                          created="2026-10-24T11:00:00Z"),
                 run_json(11, "feat", old, path=WF, event="pull_request")])
    gh.jobs[11] = infra_jobs((501, JOB, "failure", (("Run pytest", None),)))
    gh.annotations[501] = [LOST]
    clock = Clock()
    hub = _hub(gh, clock)
    await _hub_poll(hub, clock)
    assert gh.rerun_calls() == []
