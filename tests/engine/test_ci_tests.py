"""CI red but unrelated to the branch, mechanism A (proposal 0014 R4,
engine/ci_tests.py): check identity, failing-test ids from JUnit XML and the
built-in log parsers with their count check, user RE2 parsers, the base
point, ``[ci_unrelated] compare_to``, the facts, Fix CI on related failures
only, Sync with base, the ``ci_note`` row field and ``--why``. Every ``gh``
is :class:`tests.engine.fake_gh.FakeGh` and every git a dict."""

from __future__ import annotations

import json

import pytest

from lazyagent.config import Config
from lazyagent.engine import ci_tests
from lazyagent.engine.ci_tests import (
    CheckConfig,
    Found,
    bare_job,
    check_id,
    compile_re2,
    matrix_values,
    merge_ref_base,
    parse_junit_xml,
    parse_log,
    parse_tables,
    pick_artifacts,
)
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.rules import builtin
from lazyagent.engine.rules.export import format_explain
from lazyagent.engine.rules.facts import FACTS
from lazyagent.engine.rules.lang import UNKNOWN, compile_expr
from lazyagent.engine.rules.user import compose, parse_file
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo, WorkflowRun
from tests.engine.fake_gh import FakeGh, failed_log, jobs_json, run_json
from tests.engine.fakes import make_engine, worktree
from tests.engine.rules.test_review_facts import readme_recipe
from tests.protocol.golden_cases import GLOBAL_RULES, REPO_RULES, _rules_files

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
KEY = "repo:feat"
HEAD = "a" * 40
BASE_P = "c" * 40
NEWER = "d" * 40
MAIN_M = "e" * 40
WF = ".github/workflows/ci.yml"
JOB = "unit-tests (3.12)"
CHECK = f"{WF}/{JOB}"
CI_ID = f"Ci:{FEAT}:7"

async def _upsert(engine):
    """A feeder says QA tested it (no tracker adapter)."""
    await EngineMethods(engine).work_items_upsert({
        "url": "https://planner.example.com/T-1", "title": "Feat", "source": "planner",
        "branch": "feat", "facts": {"tracker.qa": "tested"}})

CONFIG = """version = 1

[ci.check."unit-tests"]
tests_from = ["junit:junit-*", "log:pytest"]
"""


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


# ----------------------------------------------------------------------
# Fixtures: logs and XML as the runners print them
# ----------------------------------------------------------------------

PYTEST_LOG = """\
============================= test session starts ==============================
collected 122 items

tests/test_a.py ..F.                                                     [  3%]
[gw3] [ 50%] FAILED tests/test_a.py::test_retry[exponential]
=================================== FAILURES ===================================
___________________________ test_retry[exponential] ____________________________
E   AssertionError: assert 3 == 2
=========================== short test summary info ============================
FAILED tests/test_a.py::test_retry[exponential] - AssertionError: assert 3 == 2
FAILED tests/test_b.py::TestExport::test_csv - KeyError: 'id'
================== 2 failed, 120 passed, 3 skipped in 41.20s ===================
"""

JEST_LOG = """\
FAIL src/cart.test.js
  Cart
    ✓ adds (3 ms)
    ✕ totals (5 ms)

  ● Cart › totals

    expect(received).toBe(expected)

PASS src/user.test.js
FAIL src/api.test.js
  ● api › retries on 503

Tests:       2 failed, 40 passed, 42 total
"""

VITEST_LOG = """\
 FAIL  src/cart.test.ts > Cart > totals
AssertionError: expected 3 to be 2
 FAIL  src/api.test.ts > api > retries
 Test Files  2 failed | 5 passed (7)
      Tests  2 failed | 30 passed (32)
"""

GO_LOG = """\
=== RUN   TestParse
=== RUN   TestParse/empty
    parse_test.go:12: got 1
    --- FAIL: TestParse/empty (0.00s)
--- FAIL: TestParse (0.00s)
FAIL
FAIL\texample.com/shop/parse\t0.012s
ok  \texample.com/shop/cart\t0.020s
--- FAIL: TestCharge (0.10s)
FAIL\texample.com/shop/pay\t0.300s
"""

CARGO_LOG = """\
     Running unittests src/lib.rs (target/debug/deps/shop-1a2b3c4d5e6f7a8b)

running 3 tests
test cart::tests::adds ... ok
test cart::tests::totals ... FAILED
test pay::tests::charge ... FAILED

failures:

failures:
    cart::tests::totals
    pay::tests::charge

test result: FAILED. 1 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out
"""

MAVEN_LOG = """\
[ERROR] Tests run: 4, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE! - in com.shop.CartTest
[ERROR] totals(com.shop.CartTest)  Time elapsed: 0.01 s  <<< FAILURE!
[ERROR] com.shop.PayTest.charge -- Time elapsed: 0.02 s <<< ERROR!
[ERROR] Tests run: 9, Failures: 1, Errors: 1, Skipped: 0
"""

GRADLE_LOG = """\
CartTest > totals FAILED
    java.lang.AssertionError at CartTest.java:12
12 tests completed, 1 failed
"""

JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="2" tests="4">
    <testcase classname="tests.test_a" name="test_retry[exponential]" time="0.1">
      <failure message="assert 3 == 2">...</failure>
    </testcase>
    <testcase classname="tests.test_a" name="test_ok" time="0.1"/>
    <testcase classname="tests.test_b.TestExport" name="test_csv" time="0.2">
      <error message="KeyError">...</error>
    </testcase>
    <testcase classname="tests.test_b" name="test_skip"><skipped/></testcase>
  </testsuite>
</testsuites>
"""


def lines(text: str) -> list[str]:
    return text.splitlines()


# ----------------------------------------------------------------------
# Identity and parsers
# ----------------------------------------------------------------------


def test_a_check_is_its_workflow_file_and_job_matrix_values_included():
    run = WorkflowRun(1, "Tests", HEAD, "feat", "completed", "failure", path=WF)
    assert check_id(run, JOB) == ".github/workflows/ci.yml/unit-tests (3.12)"
    assert bare_job(JOB) == "unit-tests" and matrix_values("test (ubuntu, 3.11)") == (
        "ubuntu", "3.11")
    # The workflow's name stands in only when the run has no path.
    assert check_id(WorkflowRun(1, "Tests", HEAD, "feat", "completed", "failure"), "lint") == \
        "Tests/lint"
    cfg = CheckConfig("unit-tests", ("log:pytest",))
    assert cfg.matches(CHECK, JOB) and not cfg.matches(f"{WF}/lint", "lint")


def test_pytest_ids_with_parameters_and_the_count_check():
    found = parse_log("pytest", lines(PYTEST_LOG))
    assert found.ids == {"tests/test_a.py::test_retry[exponential]",
                         "tests/test_b.py::TestExport::test_csv"}
    assert found.why == "pytest: 2 failing tests"
    # The worker's progress line is not counted twice, and an error counts.
    with_error = PYTEST_LOG.replace("2 failed, 120", "2 failed, 1 error, 120") + \
        "ERROR tests/test_c.py::test_db - OperationalError\n"
    assert len(parse_log("pytest", lines(with_error)).ids) == 3


def test_a_truncated_log_is_unknown_and_says_what_was_missing():
    cut = "\n".join(PYTEST_LOG.splitlines()[:-2])
    assert parse_log("pytest", lines(cut)) == Found(
        None, "pytest", "pytest: no totals line (the log may be cut)")
    one_missing = PYTEST_LOG.replace("2 failed", "3 failed")
    assert parse_log("pytest", lines(one_missing)).why == "pytest parser found 2 of 3 failures"
    # A step that failed with no test failure (a build, a lint).
    assert parse_log("pytest", ["error: could not compile"]).ids is None


def test_jest_and_vitest():
    jest = parse_log("jest", lines(JEST_LOG))
    assert jest.ids == {"src/cart.test.js::Cart › totals", "src/api.test.js::api › retries on 503"}
    assert parse_log("jest", lines(JEST_LOG.replace("2 failed", "3 failed"))).ids is None
    broken = JEST_LOG + "FAIL src/x.test.js\n  ● Test suite failed to run\n"
    assert "failed to run" in parse_log("jest", lines(broken)).why
    vitest = parse_log("vitest", lines(VITEST_LOG))
    assert vitest.ids == {"src/cart.test.ts::Cart > totals", "src/api.test.ts::api > retries"}


def test_go_keeps_subtests_and_needs_every_failed_package_explained():
    found = parse_log("go", lines(GO_LOG))
    assert found.ids == {"example.com/shop/parse::TestParse/empty",
                         "example.com/shop/parse::TestParse", "example.com/shop/pay::TestCharge"}
    assert found.why == "go: 3 failing tests in 2 packages"
    assert parse_log("go", ["FAIL\texample.com/shop/x [build failed]"]).ids is None
    assert parse_log("go", ["--- FAIL: TestX (0.00s)"]).why.endswith("(the log may be cut)")
    assert parse_log("go", ["panic: boom", "FAIL\texample.com/a\t0.1s"]).ids is None
    done = GO_LOG + "DONE 120 tests, 3 failures in 1.2s\n"
    assert parse_log("go", lines(done)).known
    assert not parse_log("go", lines(done.replace("3 failures", "4 failures"))).known


def test_cargo_names_the_test_binary():
    found = parse_log("cargo", lines(CARGO_LOG))
    assert found.ids == {"shop::cart::tests::totals", "shop::pay::tests::charge"}
    assert not parse_log("cargo", lines(CARGO_LOG.replace("2 failed", "1 failed"))).known


def test_junit_console_maven_and_gradle():
    assert parse_log("junit-console", lines(MAVEN_LOG)).ids == {
        "com.shop.CartTest::totals", "com.shop.PayTest::charge"}
    assert parse_log("junit-console", lines(GRADLE_LOG)).ids == {"CartTest::totals"}


def test_junit_xml_failures_and_errors_and_no_dtd():
    assert sorted(parse_junit_xml(JUNIT_XML)) == [
        "tests.test_a::test_retry[exponential]", "tests.test_b.TestExport::test_csv"]
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite/>'
    with pytest.raises(ValueError, match="DTD"):
        parse_junit_xml(bomb)
    with pytest.raises(ValueError, match="not valid XML"):
        parse_junit_xml("<testsuite>")


def test_a_matrix_job_takes_the_artifact_that_names_its_values():
    arts = [{"id": 1, "name": "junit-3.11"}, {"id": 2, "name": "junit-3.12"},
            {"id": 3, "name": "coverage"}]
    assert pick_artifacts(arts, "junit-*", JOB)[0] == [arts[1]]
    assert pick_artifacts(arts, "junit-*", "unit-tests (3.13)")[1] == \
        "2 artifacts match junit-* and none names 3.13"
    assert pick_artifacts([{"name": "junit-x", "expired": True}], "junit-*", "t")[1] == \
        "the JUnit artifact junit-* has expired"
    assert pick_artifacts([], "junit-*", "t")[1] == "no JUnit artifact junit-*"


def test_user_regexes_are_re2_only_and_have_their_count_check():
    check = CheckConfig("e2e", ("log:regex",), r"^✗ (?P<test>\S+)", r"(?P<total>\d+) failing")
    found = parse_log("regex", ["✗ checkout/pay", "✗ cart/add", "2 failing"], check)
    assert found.ids == {"checkout/pay", "cart/add"}
    assert not parse_log("regex", ["✗ checkout/pay", "2 failing"], check).known
    for bad in (r"(?=x)(?P<test>a)", r"(?P<test>a)\1", r"(?P<test>(?>a))"):
        with pytest.raises(ValueError, match="RE2"):
            compile_re2(bad, "test")
    with pytest.raises(ValueError, match="named group"):
        compile_re2(r"(\S+)", "test")


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


def test_config_tables_their_errors_and_the_later_keys():
    part, errors, warnings = parse_tables(
        {"nondeterministic": ["evals"], "bogus": 1,
         "check": {"unit-tests": {"tests_from": ["junit:junit-*", "log:pytest"]},
                   "e2e": {"tests_from": ["log:mocha"]},
                   "evals": {"confirm": "user"}}},
        {"compare_to": "origin/main", "base_fetches_per_poll": 99,
         "project": {"checkout": {"compare_to": "origin/main"}, "bad": {"compare_to": "a..b"}}})
    assert [c.name for c in part.checks] == ["unit-tests"]
    assert part.compare_to == "origin/main" and part.projects == {"checkout": "origin/main"}
    assert any("log:mocha" in e for e in errors)
    assert "unknown key 'bogus' in [ci]" in errors
    assert any("base_fetches_per_poll" in e for e in errors)
    assert any("not a ref name" in e for e in errors)
    # R5's keys are read now (engine/attest.py).
    assert not warnings
    assert part.attest == {"nondeterministic": ("evals",)}
    assert part.attest_checks == {"evals": {"confirm": "user"}}


def test_the_readmes_ci_example_loads_as_written():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    section = readme.split("### Your rules (rules.toml)")[1].split("\n### ")[0]
    import re

    block = [b for b in re.findall(r"```toml\n(.*?)```", section, re.S) if "[ci.check." in b][0]
    f = parse_file("version = 1\n" + block, "global", GLOBAL_RULES)
    assert f.errors == [] and f.warnings == []
    ci = compose(builtin(), [f]).catalog.ci
    assert [c.name for c in ci.checks] == ["unit-tests", "e2e", "summary"]
    assert ci.compare_for("checkout-redesign") == ("origin/main", "project checkout-redesign")
    e2e = ci.checks[1]
    assert parse_log("regex", ["✗ cart/add", "1 failing"], e2e).ids == {"cart/add"}


def test_the_repo_file_wins_and_the_rule_file_carries_the_config():
    glob = parse_file(CONFIG + '\n[ci_unrelated]\ncompare_to = "origin/develop"\n'
                      '[ci_unrelated.project."checkout"]\ncompare_to = "origin/main"\n',
                      "global", GLOBAL_RULES)
    repo = parse_file('version = 1\n[ci_unrelated]\ncompare_to = "origin/main"\n'
                      '[ci.check."unit-tests"]\ntests_from = ["log:pytest"]\n',
                      "repo", REPO_RULES)
    assert not glob.errors and not glob.warnings
    ci = compose(builtin(), [glob, repo]).catalog.ci
    assert ci.compare_to == "origin/main" and ci.compare_from == "repo"
    assert [c.tests_from for c in ci.checks] == [("log:pytest",)]
    assert ci.compare_for("checkout") == ("origin/main", "project checkout")
    assert ci.compare_for(None) == ("origin/main", "repo rules.toml")
    assert compose(builtin(), []).catalog.ci is ci_tests.NO_CONFIG


# ----------------------------------------------------------------------
# End to end: the hub, the queue, the rows
# ----------------------------------------------------------------------


def _pr(sha=HEAD, base="main", checks=None, **kw):
    checks = checks if checks is not None else [
        CiCheck(JOB, "COMPLETED", "FAILURE", "https://github.com/o/r/actions/runs/11/job/1")]
    return PrInfo(7, "Feat", "OPEN", checks, head_sha=sha, base_ref=base, **kw)


def _log(tests: list[str], job: str = JOB, total: int | None = None) -> str:
    body = [f"FAILED {t} - AssertionError" for t in tests]
    n = len(tests) if total is None else total
    body.append(f"======== {n} failed, 100 passed in 9.10s ========")
    return failed_log(job, "Run pytest", ["collected 101 items", *body])


TWO = ["tests/test_a.py::test_retry[exponential]", "tests/test_b.py::test_csv"]


def _gh(branch_tests=TWO, base_tests=TWO, *, base_run: bool = True) -> FakeGh:
    runs = [run_json(11, "feat", HEAD, path=WF, event="pull_request",
                     created="2026-10-24T10:00:00Z")]
    if base_run:
        runs.append(run_json(21, "main", BASE_P, path=WF, event="push",
                             created="2026-10-24T09:00:00Z"))
    gh = FakeGh(runs)
    gh.jobs[11] = jobs_json((JOB, "Run pytest"))
    gh.jobs[21] = jobs_json((JOB, "Run pytest"))
    gh.logs[11] = _log(branch_tests)
    gh.logs[21] = _log(base_tests)
    gh.merge_refs[7] = [BASE_P, HEAD]
    return gh


def _engine(gh: FakeGh, pr: PrInfo | None = None, git: dict | None = None,
            feeder_project: str | None = None):
    from lazyagent.engine.actions import ActionsHub

    engine = make_engine([worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)],
                         config=Config())
    clock = Clock()
    engine.status.clock = clock
    hub = ActionsHub("/tmp/repo", run=gh, stream=gh.stream, read_heads=lambda: {"feat": HEAD},
                     clock=clock)
    hub.tests.state_path = None
    git = git or {}
    hub.tests.git = lambda args: (0, git[tuple(args)]) if tuple(args) in git else (128, "")
    engine.actions = hub
    engine.worktree_service._gh_available = True
    engine.worktree_service._pr_infos[FEAT] = pr if pr is not None else _pr()
    if feeder_project is not None:
        from dataclasses import replace

        target = engine.status._target
        engine.status._target = lambda wt: replace(target(wt), project=feeder_project)  # type: ignore
    return engine, hub, clock


async def _poll(engine, clock=None):
    if clock is not None:
        clock.now += 61
    engine.status._poll_actions()
    await engine.wait_idle()
    engine.status.flush()


def _items(engine, kind=None):
    engine.status.flush()
    return [i for i in engine.actionables.list()["items"] if kind is None or i["kind"] == kind]


async def _rows(engine, fields=("ci_note", "rule_id")):
    methods = EngineMethods(engine)
    result = await methods.status_list({"fields": list(fields)})
    return {r["key"]: r for r in result["rows"]}


@pytest.mark.asyncio
async def test_the_same_two_failing_tests_on_the_base_clear_fix_ci_and_show_the_note():
    gh = _gh()
    engine, _, clock = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
        row = (await _rows(engine))[KEY]
        assert row["ci_note"] == "CI red · unrelated (base also red: 2 tests)"
        facts = engine.status.facts(KEY)
        t = facts.ci_tests
        assert t.unrelated is True and t.unrelated_checks == {CHECK}
        [c] = t.checks
        assert c.point == BASE_P and c.compared == "main at ccccccc"
        assert c.detail == "both failing tests also fail on main at ccccccc"
        # The CLI table appends the note to the state.
        from lazyagent.engine.status import state_text

        assert state_text(row, NOW).endswith("· CI red · unrelated (base also red: 2 tests)")
        # Stable: more passes with nothing new read change nothing, and no
        # call is made again for what is decided.
        calls = len(gh.calls)
        for _ in range(3):
            await _poll(engine, clock)
            assert _items(engine, "ci") == []
        # The poll's own (the window and the red head's runs, 304s) alone.
        assert all("actions/runs?" in c[-1] for c in gh.calls[calls:])
        assert len(gh.calls) - calls == 6


@pytest.mark.asyncio
async def test_one_extra_failing_test_on_the_branch_brings_fix_ci_back_naming_it():
    gh = _gh(branch_tests=[*TWO, "tests/test_c.py::test_new"])
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
        assert item["state"] == "CI red: unit-tests (1 new)"
        assert item["detail"] == (f"{JOB}: 1 new failing test: tests/test_c.py::test_new")
        assert (await _rows(engine))[KEY]["ci_note"] is None
        t = engine.status.facts(KEY).ci_tests
        assert t.unrelated is False
        [c] = t.checks
        assert c.new == {f"{CHECK}::tests/test_c.py::test_new"}
        why = format_explain(engine.status.explain(KEY))
        assert "unit-tests (3.12): related: 1 failing test not failing on main at ccccccc " \
               "(new: tests/test_c.py::test_new)" in why


@pytest.mark.asyncio
async def test_different_failing_tests_on_both_sides_are_related():
    gh = _gh(branch_tests=["tests/test_x.py::test_mine"], base_tests=TWO)
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert len(_items(engine, "ci")) == 1
        assert engine.status.facts(KEY).ci_tests.unrelated is False


@pytest.mark.asyncio
async def test_a_branch_that_fixed_some_of_the_bases_failures_is_unrelated():
    gh = _gh(branch_tests=TWO[:1], base_tests=TWO)
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
        assert (await _rows(engine))[KEY]["ci_note"] == "CI red · unrelated (base also red: 1 test)"


@pytest.mark.asyncio
async def test_no_junit_and_no_parser_keeps_fix_ci_and_why_says_what_was_missing():
    gh = _gh()
    gh.logs[11] = failed_log(JOB, "Run pytest", ["Traceback (most recent call last):", "boom"])
    gh.jobs[11] = jobs_json((JOB, "Run pytest"), ("lint", "ruff"), passed=())
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
        # As before A: the first failing run's title, cut to five words.
        assert item["state"] == "CI red: Tests / unit-tests…"
        explain = engine.status.explain(KEY)
        assert explain["ci"]["unrelated"] is False or explain["ci"]["unrelated"] is None
        details = {c["job"]: c["detail"] for c in explain["ci"]["checks"]}
        assert details[JOB] == ("junit: no JUnit artifact junit-*; "
                                "pytest: no totals line (the log may be cut)")
        assert details["lint"] == "no tests_from for this check (no test data)"
        why = format_explain(explain)
        assert "  CI     related (some test data unknown)" in why
        assert "lint: related: no tests_from for this check (no test data)" in why


@pytest.mark.asyncio
async def test_junit_artifacts_are_read_and_preferred_over_the_log():
    gh = _gh()
    for run_id in (11, 21):
        gh.artifacts[run_id] = [{"id": run_id * 10, "name": "junit-3.12", "size_in_bytes": 900},
                                {"id": run_id * 10 + 1, "name": "junit-3.11", "size_in_bytes": 900}]
    gh.artifact_files["junit-3.12"] = {"report/junit.xml": JUNIT_XML}
    gh.logs.clear()
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        t = engine.status.facts(KEY).ci_tests
        assert t.unrelated is True
        assert t.checks[0].tests == {f"{CHECK}::tests.test_a::test_retry[exponential]",
                                     f"{CHECK}::tests.test_b.TestExport::test_csv"}
        assert not any(c[:2] == ["run", "view"] for c in gh.calls)
        assert [c[-1] for c in gh.calls if c[-1].endswith("/zip")] == [
            "repos/{owner}/{repo}/actions/artifacts/110/zip",
            "repos/{owner}/{repo}/actions/artifacts/210/zip"]


@pytest.mark.asyncio
async def test_no_base_run_at_exactly_p_is_related_and_is_looked_up_once():
    gh = _gh(base_run=False)
    engine, _, clock = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert len(_items(engine, "ci")) == 1
        [c] = engine.status.facts(KEY).ci_tests.checks
        assert c.detail == "no Tests run on main at ccccccc"
        def point_reads():
            return [c for c in gh.head_calls() if BASE_P in c[-1]]

        assert len(point_reads()) == 1
        await _poll(engine, clock)
        assert len(point_reads()) == 1


@pytest.mark.asyncio
async def test_the_merge_ref_moved_on_is_unknown_and_recorded_once():
    gh = _gh()
    gh.merge_refs[7] = [NEWER, "f" * 40]
    engine, _, clock = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        await _poll(engine, clock)
        [c] = engine.status.facts(KEY).ci_tests.checks
        assert c.detail == "base point unknown: the PR's merge ref has moved on since the run"
        assert sum("/commits/pull/7/merge" in c[-1] for c in gh.calls) == 1
        assert len(_items(engine, "ci")) == 1


def test_merge_ref_base():
    body = json.dumps({"parents": [{"sha": BASE_P}, {"sha": HEAD}]})
    assert merge_ref_base(body, HEAD) == (BASE_P, "")
    assert merge_ref_base(body, NEWER)[0] is None
    assert merge_ref_base("nope", HEAD) == (None, "a bad merge ref response")


@pytest.mark.asyncio
async def test_a_push_run_compares_with_the_merge_base_and_several_are_unknown():
    gh = _gh()
    gh.runs[0]["event"] = "push"
    git = {("merge-base", "--all", HEAD, "origin/main"): BASE_P + "\n"}
    engine, _, _ = _engine(gh, git=git)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert engine.status.facts(KEY).ci_tests.unrelated is True
        assert not any("/commits/pull/" in c[-1] for c in gh.calls)
    gh2 = _gh()
    gh2.runs[0]["event"] = "push"
    engine2, _, _ = _engine(gh2, git={("merge-base", "--all", HEAD, "origin/main"):
                                      f"{BASE_P}\n{NEWER}\n"})
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine2.rules.reload()
        await _poll(engine2)
        [c] = engine2.status.facts(KEY).ci_tests.checks
        assert c.detail == "base point unknown: aaaaaaa and origin/main have 2 merge-bases"


def _stacked_gh(main_run: bool) -> FakeGh:
    gh = _gh(base_run=False)
    if main_run:
        gh.runs.append(run_json(31, "main", MAIN_M, path=WF, event="push",
                                created="2026-10-24T08:00:00Z"))
        gh.jobs[31] = jobs_json((JOB, "Run pytest"))
        gh.logs[31] = _log(TWO)
    return gh


STACKED = CONFIG + '\n[ci_unrelated.project."checkout"]\ncompare_to = "origin/main"\n'


@pytest.mark.asyncio
async def test_a_stacked_branch_is_compared_with_mains_run_at_the_ancestor_commit():
    gh = _stacked_gh(main_run=True)
    git = {("merge-base", "--all", BASE_P, "origin/main"): MAIN_M + "\n"}
    engine, _, _ = _engine(gh, pr=_pr(base="checkout-base"), git=git, feeder_project="checkout")
    with _rules_files({GLOBAL_RULES: STACKED}):
        engine.rules.reload()
        await _poll(engine)
        [c] = engine.status.facts(KEY).ci_tests.checks
        assert c.unrelated and c.point == MAIN_M
        assert c.compared == "origin/main at eeeeeee (project checkout)"
        assert _items(engine, "ci") == []


@pytest.mark.asyncio
async def test_a_stacked_branch_with_no_main_run_at_that_commit_keeps_fix_ci():
    gh = _stacked_gh(main_run=False)
    git = {("merge-base", "--all", BASE_P, "origin/main"): MAIN_M + "\n"}
    engine, _, _ = _engine(gh, pr=_pr(base="checkout-base"), git=git, feeder_project="checkout")
    with _rules_files({GLOBAL_RULES: STACKED}):
        engine.rules.reload()
        await _poll(engine)
        assert len(_items(engine, "ci")) == 1
        [c] = engine.status.facts(KEY).ci_tests.checks
        assert c.detail == "no Tests run on main at eeeeeee"


@pytest.mark.asyncio
async def test_the_base_fixed_gives_sync_with_no_call_beyond_the_polls_own():
    gh = _gh()
    engine, _, clock = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "sync_base") == []
        # main gets a newer green run of the workflow.
        gh.set_runs(gh.runs + [run_json(41, "main", NEWER, path=WF, event="push",
                                        conclusion="success", created="2026-10-24T11:00:00Z")],
                    '"e2"')
        before = len(gh.calls)
        await _poll(engine, clock)
        # The poll's own calls (the window, the red head's runs) alone.
        assert all("actions/runs?" in c[-1] for c in gh.calls[before:])
        assert len(gh.calls) - before == 2
        [item] = _items(engine, "sync_base")
        assert item["state"] == "Sync with base: unit-tests"
        assert item["detail"] == f"{JOB}: passes on main at ddddddd (run 41)"
        assert _items(engine, "ci") == []
        assert (await _rows(engine))[KEY]["ci_note"] == \
            "CI red · unrelated (base also red: 2 tests)"


@pytest.mark.asyncio
async def test_a_newer_red_base_run_is_read_lazily_for_sync():
    gh = _gh()
    engine, _, clock = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        gh.set_runs(gh.runs + [run_json(41, "main", NEWER, path=WF, event="push",
                                        created="2026-10-24T11:00:00Z")], '"e2"')
        gh.jobs[41] = jobs_json((JOB, "Run pytest"))
        gh.logs[41] = _log(["tests/test_z.py::test_other"])
        await _poll(engine, clock)
        [item] = _items(engine, "sync_base")
        assert "its failing tests pass on main at ddddddd (run 41)" in item["detail"]


@pytest.mark.asyncio
async def test_nothing_configured_makes_no_call_and_the_facts_read_as_before():
    gh = _gh()
    engine, _, _ = _engine(gh)
    with _rules_files({}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
        # As before A: the first failing run's title, cut to five words.
        assert item["state"] == "CI red: Tests / unit-tests…"
        assert not any("/artifacts" in c[-1] or "/commits/" in c[-1] or c[:2] == ["run", "view"]
                       for c in gh.calls)
        facts = engine.status.facts(KEY)
        assert facts.ci_tests is None
        row = (await _rows(engine))[KEY]
        assert row["ci_note"] is None
        assert "ci" not in engine.status.explain(KEY)


@pytest.mark.asyncio
async def test_a_restart_keeps_the_decided_verdict(tmp_path):
    gh = _gh()
    engine, hub, _ = _engine(gh)
    hub.tests.state_path = tmp_path / "ci.json"
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
    # A new engine, the same state file: no Fix CI while nothing is read
    # again, and no call for the job data or the merge ref.
    gh2 = _gh()
    engine2, hub2, _ = _engine(gh2)
    hub2.tests.state_path = tmp_path / "ci.json"
    hub2._jobs.clear()
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine2.rules.reload()
        await _poll(engine2)
        assert _items(engine2, "ci") == []
        assert not any("/commits/" in c[-1] or c[:2] == ["run", "view"] for c in gh2.calls)


@pytest.mark.asyncio
async def test_the_readme_owner_gate_merges_on_an_unrelated_red():
    gh = _gh()
    pr = _pr(review_decision="APPROVED", mergeable="MERGEABLE", is_draft=False, labels=[],
             approved_by=["peer"], commits_after_ready=0, author="me")
    engine, _, _ = _engine(gh, pr=pr)
    engine.review_requests.viewer = "me"  # the recipe asks pr.mine
    await _upsert(engine)
    recipe = readme_recipe()
    assert '(ci.state in ["green", "none"] or ci.unrelated or ci.only_nongating)' in recipe
    with _rules_files({GLOBAL_RULES: recipe + "\n" + CONFIG.replace("version = 1\n", "")}):
        engine.rules.reload()
        assert not engine.rules.errors
        await _poll(engine)
        [item] = _items(engine, "rule")
        assert item["verb"] == "Merge"
        assert _items(engine, "ci") == []
    # One new failing test: Fix CI, no Merge.
    gh2 = _gh(branch_tests=[*TWO, "tests/test_c.py::test_new"])
    engine2, _, _ = _engine(gh2, pr=pr)
    engine2.review_requests.viewer = "me"
    await _upsert(engine2)
    with _rules_files({GLOBAL_RULES: recipe + "\n" + CONFIG.replace("version = 1\n", "")}):
        engine2.rules.reload()
        await _poll(engine2)
        assert _items(engine2, "rule") == []
        assert len(_items(engine2, "ci")) == 1


@pytest.mark.asyncio
async def test_a_delegated_fix_ci_ends_as_unrelated_not_still_red():
    gh = _gh()
    engine, _, clock = _engine(gh)
    with _rules_files({}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
    record = {"agent": "a1", "item": item["id"], "occurrence": item["occurrence"],
              "fact": "CI red", "at": NOW, "by": "user", "phase": "on_it", "ran": True,
              "sent_at": NOW}
    engine.overlay.delegations[KEY] = record
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine, clock)
        assert _items(engine, "ci") == []
    # The agent is gone and its item cleared: not "still CI red".
    assert KEY not in engine.overlay.delegations
    texts = [e["text"] for e in engine.overlay.timeline.get(KEY, []) if e["kind"] == "delegate"]
    assert texts[-1] == "agent a1 gone: CI failures unrelated (base also red: 2 tests)"


def test_the_facts_are_registered_with_their_costs():
    for name, cost in (("ci.failing_tests", "extra"), ("ci.base_point", "extra"),
                       ("ci.base_failing_tests", "extra"), ("ci.unrelated_checks", "extra"),
                       ("ci.related_failures", "poll"), ("ci.unrelated", "extra"),
                       ("ci.unrelated_summary", "free"), ("ci.base_fixed", "extra")):
        assert FACTS[name].cost == cost
        assert FACTS[name].row is not None and FACTS[name].item is not None


@pytest.mark.asyncio
async def test_the_language_reads_the_sets_and_failing_tests_of():
    gh = _gh(branch_tests=[*TWO, "tests/test_c.py::test_new"])
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        from lazyagent.engine.rules.facts import ROW_ENV, RowContext

        facts = engine.status.facts(KEY)
        ctx = RowContext(facts, NOW, NOW, 24)
        check = CHECK.lower()

        def ev(src):
            return compile_expr(src, ROW_ENV).fn(ctx)

        assert ev(f'"{check}" in ci.related_failures') is True
        assert ev("empty(ci.unrelated_checks)") is True
        assert ev("ci.unrelated") is False
        assert ev('ci.base_point == "' + BASE_P + '"') is True
        assert ev(f'"{check}::tests/test_c.py::test_new" in ci.failing_tests_of("unit-tests")') \
            is True
        assert ev('ci.failing_tests_of("e2e") subset ci.base_failing_tests') is UNKNOWN \
            or ev('known(ci.failing_tests_of("e2e"))') is False


@pytest.mark.asyncio
async def test_an_agent_at_work_still_holds_fix_ci_back():
    gh = _gh(branch_tests=[*TWO, "tests/test_c.py::test_new"])
    engine, _, _ = _engine(gh)
    engine.registry.states.setdefault(FEAT, {})["a1"] = AgentState(
        status=AgentStatus("running"), agent_id="a1")
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []


@pytest.mark.asyncio
async def test_a_log_that_cannot_be_read_is_tried_again_then_stays_related():
    gh = _gh()
    del gh.logs[11]
    engine, _, clock = _engine(gh)
    with _rules_files({GLOBAL_RULES: 'version = 1\n[ci.check."unit-tests"]\n'
                                      'tests_from = ["log:pytest"]\n'}):
        engine.rules.reload()
        await _poll(engine)
        [c] = engine.status.facts(KEY).ci_tests.checks
        assert c.pending and len(_items(engine, "ci")) == 1
        gh.logs[11] = _log(TWO)
        await _poll(engine, clock)
        assert engine.status.facts(KEY).ci_tests.unrelated is True
    gh2 = _gh()
    del gh2.logs[11]
    engine2, _, clock2 = _engine(gh2)
    with _rules_files({GLOBAL_RULES: 'version = 1\n[ci.check."unit-tests"]\n'
                                      'tests_from = ["log:pytest"]\n'}):
        engine2.rules.reload()
        for _ in range(4):
            await _poll(engine2, clock2)
        [c] = engine2.status.facts(KEY).ci_tests.checks
        assert c.detail.startswith("could not be read: the failed log of run 11")
        reads = sum(c[:2] == ["run", "view"] for c in gh2.calls)
        await _poll(engine2, clock2)
        assert sum(c[:2] == ["run", "view"] for c in gh2.calls) == reads == 3
