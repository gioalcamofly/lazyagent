"""R4c (docs/design/rules-plan.md): non-gating and policy checks, a parked
work item parks its items, the latest-created run wins, hang detection
(``[inbox] hang_seconds``, CPU), mergeable UNKNOWN keeps the last known
value, and base merges that differ only in generated files."""

from __future__ import annotations

import logging
import time

import pytest

from lazyagent.engine.ci_tests import parse_tables
from lazyagent.engine.methods import EngineMethods
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo
from tests.engine.fake_gh import FakeGh
from tests.engine.rules.test_review_facts import readme_recipe
from tests.engine.test_ci_tests import (
    CHECK,
    KEY,
    TWO,
    _engine,
    _gh,
    _items,
    _poll,
    _pr,
    _rows,
    _upsert,
)
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

NONGATING = """version = 1

[ci.check."unit-tests"]
gating = false
"""

NONGATING_TESTS = """version = 1

[ci.check."unit-tests"]
tests_from = ["log:pytest"]
gating = false
"""

POLICY = """version = 1

[ci.check."label-check"]
policy = true
"""

LABEL = CiCheck("label-check", "COMPLETED", "FAILURE", "https://checks.example.com/label/1")
READY = dict(review_decision="APPROVED", mergeable="MERGEABLE", is_draft=False,
             approved_by=["peer"])


# ----------------------------------------------------------------------
# P2: gating = false, policy = true
# ----------------------------------------------------------------------


def test_the_gate_keys_are_read_and_checked():
    part, errors, _ = parse_tables(
        {"check": {"evals": {"gating": False}, "labels": {"policy": True},
                   "both": {"policy": True, "gating": True}, "bad": {"gating": "no"},
                   "plain": {"gating": True}}}, None)
    gates = {c.name: c.gate for c in part.checks}
    # R5a: an explicit gating = true is kept (it wins over branch protection).
    assert gates == {"evals": "nongating", "labels": "policy", "plain": "gating"}
    assert any("policy check is not a merge gate" in e for e in errors)
    assert any("gating must be true or false" in e for e in errors)


@pytest.mark.asyncio
async def test_a_non_gating_red_raises_no_fix_ci_and_shows_on_the_row():
    engine, _, _ = _engine(_gh())
    with _rules_files({GLOBAL_RULES: NONGATING}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
        assert (await _rows(engine))[KEY]["ci_note"] == "unit-tests red · not gating"
        t = engine.status.facts(KEY).ci_tests
        assert t.only_nongating and t.gating_failures == [] and t.nongating_failures == [CHECK]


@pytest.mark.asyncio
async def test_a_non_gating_check_whose_failing_tests_are_the_branchs_gates_again():
    engine, _, _ = _engine(_gh(branch_tests=[*TWO, "tests/test_c.py::test_new"]))
    with _rules_files({GLOBAL_RULES: NONGATING_TESTS}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
        assert item["state"] == "CI red: unit-tests (1 new)"
        assert engine.status.facts(KEY).ci_tests.only_nongating is False


@pytest.mark.asyncio
async def test_a_non_gating_check_with_unknown_test_data_stays_non_gating():
    gh = _gh()
    del gh.logs[21]  # the base's log cannot be read: unknown
    gh.logs[11] = gh.logs[11].replace("2 failed", "9 failed")  # the count check fails
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: NONGATING_TESTS}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
        assert engine.status.facts(KEY).ci_tests.only_nongating is True


@pytest.mark.asyncio
async def test_a_gating_check_beside_a_non_gating_one_is_named_first():
    engine, _, _ = _engine(_gh(), pr=_pr(checks=[
        CiCheck("unit-tests (3.12)", "COMPLETED", "FAILURE",
                "https://github.com/o/r/actions/runs/11/job/1"), LABEL]))
    with _rules_files({GLOBAL_RULES: NONGATING}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
        assert item["state"] == "CI red: label-check"


@pytest.mark.asyncio
async def test_a_policy_check_waits_until_the_merge_is_next():
    pr = _pr(checks=[LABEL])
    engine, _, _ = _engine(FakeGh([]), pr=pr)
    with _rules_files({GLOBAL_RULES: POLICY}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine) == []
        assert (await _rows(engine))[KEY]["ci_note"] == "label-check red · policy"
    # GitHub's "approved" from an app is not a person's approval; nor is an
    # account in [review] not_human, nor approvals not read yet.
    for approved_by in ([], ["review-app"], None):
        bot = _pr(checks=[LABEL], **(READY | {"approved_by": approved_by}))
        engine, _, _ = _engine(FakeGh([]), pr=bot)
        with _rules_files({GLOBAL_RULES: POLICY + '[review]\nnot_human = ["review-app"]\n'}):
            engine.rules.reload()
            await _poll(engine)
            assert _items(engine) == [], approved_by
    ready = _pr(checks=[LABEL], **READY)
    engine, _, _ = _engine(FakeGh([]), pr=ready)
    with _rules_files({GLOBAL_RULES: POLICY}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine)
        assert (item["kind"], item["state"]) == ("ci_policy", "Fix label-check")
        # pr.human_approvals is read (the review query) only with a policy check.
        assert engine.rules.catalog.reads_review
        assert item["detail"] == "policy check red: label-check; the rest is ready to merge"
    # A gating red beside it: Fix CI, not the policy check.
    both = _pr(checks=[LABEL, CiCheck("e2e", "COMPLETED", "FAILURE", "https://ci.example.com/1")],
               **READY)
    engine, _, _ = _engine(FakeGh([]), pr=both)
    with _rules_files({GLOBAL_RULES: POLICY}):
        engine.rules.reload()
        await _poll(engine)
        assert [i["kind"] for i in _items(engine)] == ["ci"]


@pytest.mark.asyncio
async def test_the_readme_recipe_merges_while_only_non_gating_checks_are_red():
    pr = _pr(review_decision="APPROVED", mergeable="MERGEABLE", is_draft=False, labels=[],
             approved_by=["peer"], commits_after_ready=0, author="me")
    engine, _, _ = _engine(_gh(), pr=pr)
    engine.review_requests.viewer = "me"
    await _upsert(engine)
    recipe = readme_recipe()
    assert "ci.only_nongating" in recipe
    with _rules_files({GLOBAL_RULES: recipe + "\n" + NONGATING.replace("version = 1\n", "")}):
        engine.rules.reload()
        assert not engine.rules.errors
        await _poll(engine)
        [item] = _items(engine, "rule")
        assert item["verb"] == "Merge"


# ----------------------------------------------------------------------
# P3: parking a work item parks its items
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_parked_work_item_hides_its_items_but_a_now_one():
    engine, _, _ = _engine(FakeGh([]), pr=_pr(checks=[LABEL]))
    with _rules_files({}):
        engine.rules.reload()
        await _poll(engine)
        assert [i["kind"] for i in _items(engine)] == ["ci"]
        await EngineMethods(engine).status_gesture(
            {"key": KEY, "kind": "park", "until": "indefinite"})
        assert _items(engine) == []
        hidden = engine.actionables.list(include_hidden=True)["items"]
        assert [(i["kind"], i["park"]["until"]) for i in hidden] == [("ci", "indefinite")]
        row = (await _rows(engine))[KEY]
        assert (row["zone"], row["needs_you"]) == ("parked", None)
        assert engine.status.list()["counts"]["parked_needs_you"] == 0
        # An agent blocked on an approval breaks through, and takes the row
        # out of Parked until it is answered.
        from tests.engine.test_ci_tests import FEAT

        engine.registry.states.setdefault(FEAT, {})["a1"] = AgentState(
            status=AgentStatus("waiting_for_approval"), agent_id="a1", detail="Bash: rm")
        assert [i["kind"] for i in _items(engine)] == ["approval"]
        row = (await _rows(engine))[KEY]
        assert (row["zone"], row["level"]) == ("inbox", "now")
        engine.registry.states[FEAT].pop("a1")
        assert _items(engine) == []
        assert (await _rows(engine))[KEY]["zone"] == "parked"
        # Unparked: the item is back.
        await EngineMethods(engine).status_gesture({"key": KEY, "kind": "clear"})
        assert [i["kind"] for i in _items(engine)] == ["ci"]


@pytest.mark.asyncio
async def test_an_engine_brief_of_a_parked_work_item_does_not_surface():
    from tests.engine.test_ci_tests import FEAT

    engine, _, _ = _engine(FakeGh([]), pr=_pr(checks=[]))
    with _rules_files({}):
        engine.rules.reload()
        await EngineMethods(engine).status_gesture(
            {"key": KEY, "kind": "park", "until": "indefinite"})
        engine.decisions.engine_brief(key=KEY, worktree_path=FEAT, agent_id="a1",
                                      question="Fix CI twice failed", tried="two runs",
                                      now=time.time())
        assert _items(engine) == []
        await EngineMethods(engine).status_gesture({"key": KEY, "kind": "clear"})
        assert [i["kind"] for i in _items(engine)] == ["decision"]


# ----------------------------------------------------------------------
# P7: the run created last wins
# ----------------------------------------------------------------------


def test_a_rerun_of_an_old_run_does_not_hide_a_newer_green_run():
    from lazyagent.engine.actions import verdict
    from lazyagent.models import WorkflowRun

    old = WorkflowRun(11, "Labels", "a" * 40, "feat", "completed", "failure",
                      created_at=100.0, attempt=2, workflow_id=5)
    new = WorkflowRun(12, "Labels", "a" * 40, "feat", "completed", "success",
                      created_at=200.0, attempt=1, workflow_id=5)
    for runs in ([old, new], [new, old]):
        v = verdict(runs, "feat", "a" * 40)
        assert (v.state, [r.id for r in v.runs]) == ("green", [12])


def test_the_prs_checks_keep_the_newest_run_of_each_workflow_and_job():
    from lazyagent.worktree_manager import WorktreeManager

    def run(n, conclusion, name="label-check", workflow="Labels"):
        return {"name": name, "status": "COMPLETED", "conclusion": conclusion,
                "detailsUrl": f"https://github.com/o/r/actions/runs/{n}/job/{n}1",
                "workflowName": workflow}

    pr = WorktreeManager._pr_info_from_view({"number": 1, "statusCheckRollup": [
        run(300, "FAILURE"),               # an old run, re-run last: replays old labels
        run(400, "SUCCESS"),               # created after it
        run(250, "FAILURE", "test", "CI"),  # another workflow's job: kept
        run(260, "FAILURE", "test", "Other"),
        {"context": "deploy", "state": "FAILURE", "targetUrl": ""},
    ]})
    assert [(c.name, c.conclusion) for c in pr.checks] == [
        ("label-check", "SUCCESS"), ("test", "FAILURE"), ("test", "FAILURE"),
        ("deploy", "FAILURE")]
    # The GraphQL shape carries the workflow too.
    view = WorktreeManager._graphql_pr_to_view({"commits": {"nodes": [{"commit": {
        "statusCheckRollup": {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "label-check", "status": "COMPLETED",
             "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/r/actions/runs/3/job/1",
             "checkSuite": {"workflowRun": {"workflow": {"name": "Labels"}}}},
            {"__typename": "CheckRun", "name": "label-check", "status": "COMPLETED",
             "conclusion": "SUCCESS", "detailsUrl": "https://github.com/o/r/actions/runs/4/job/2",
             "checkSuite": {"workflowRun": {"workflow": {"name": "Labels"}}}},
        ]}}}}]}})
    pr = WorktreeManager._pr_info_from_view(view)
    assert [c.conclusion for c in pr.checks] == ["SUCCESS"]
    assert pr.overall_status == "pass"


# ----------------------------------------------------------------------
# P8: hang detection
# ----------------------------------------------------------------------


def test_hang_seconds_is_read_from_inbox(tmp_path):
    from lazyagent.config import load_config

    (tmp_path / ".lazyagent.toml").write_text("[inbox]\nhang_seconds = 1800\n")
    assert load_config(tmp_path).inbox.hang_seconds == 1800
    (tmp_path / ".lazyagent.toml").write_text("[inbox]\nhang_seconds = 5\n")
    assert load_config(tmp_path).inbox.hang_seconds is None


def _global_toml(text):
    from lazyagent.config import global_config_path

    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_the_repos_hang_seconds_wins_over_the_global_one(tmp_path):
    from lazyagent.config import load_config

    _global_toml("[inbox]\nhang_seconds = 1200\n")
    (tmp_path / ".lazyagent.toml").write_text("[inbox]\nhang_seconds = 1800\n")
    assert load_config(tmp_path).inbox.hang_seconds == 1800


def test_the_global_hang_seconds_applies_when_the_repo_sets_none(tmp_path):
    from lazyagent.config import load_config

    _global_toml("[inbox]\nhang_seconds = 1200\n")
    assert load_config(tmp_path).inbox.hang_seconds == 1200, "no repo file"
    (tmp_path / ".lazyagent.toml").write_text("[inbox]\ndeep_max = 3\n")
    assert load_config(tmp_path).inbox.hang_seconds == 1200
    (tmp_path / ".lazyagent.toml").write_text("[inbox]\nhang_seconds = 5\n")
    assert load_config(tmp_path).inbox.hang_seconds == 1200, "an invalid repo value"


@pytest.mark.parametrize("bad", ["30", '"900"', "900.0", "true"])
def test_an_invalid_global_hang_seconds_warns_and_is_ignored(tmp_path, bad, caplog, monkeypatch):
    from lazyagent.config import Config, load_config
    from tests.engine.fakes import make_engine, worktree

    _global_toml(f"[inbox]\nhang_seconds = {bad}\n")
    with caplog.at_level(logging.WARNING, logger="lazyagent.config"):
        config = load_config(tmp_path)
    assert config.inbox.hang_seconds is None
    assert "ignoring [inbox] hang_seconds" in caplog.text
    monkeypatch.setenv("LAZYAGENT_HANG_SECONDS", "1500")
    assert make_engine([worktree("/tmp/repo")], config=config).hang_seconds == 1500
    assert Config().inbox.hang_seconds is None


def test_the_engine_takes_the_repos_then_the_environments_hang_seconds(monkeypatch):
    from lazyagent.config import Config
    from tests.engine.fakes import make_engine, worktree

    engine = make_engine([worktree("/tmp/repo")], config=Config())
    monkeypatch.delenv("LAZYAGENT_HANG_SECONDS", raising=False)
    assert engine.hang_seconds == 600
    monkeypatch.setenv("LAZYAGENT_HANG_SECONDS", "1200")
    assert engine.hang_seconds == 1200
    engine.config.inbox.hang_seconds = 3600
    assert engine.hang_seconds == 3600


def _fake_proc(tmp_path, tree):
    """``tree``: pid → (ppid, ticks)."""
    for pid, (ppid, ticks) in tree.items():
        d = tmp_path / str(pid)
        d.mkdir(exist_ok=True)
        # pid (comm) state ppid ... utime stime cutime cstime at fields 14-17
        fields = ["S", str(ppid)] + ["0"] * 9 + [str(ticks), "0", "0", "0"] + ["0"] * 5
        (d / "stat").write_text(f"{pid} (a b) " + " ".join(fields) + "\n")
    return str(tmp_path)


def test_tree_cpu_ticks_sums_the_process_tree(tmp_path):
    from lazyagent.engine.session import tree_cpu_ticks

    proc = _fake_proc(tmp_path, {10: (1, 5), 11: (10, 7), 12: (11, 100), 20: (1, 999)})
    assert tree_cpu_ticks(10, proc) == 112
    assert tree_cpu_ticks(99, proc) is None
    assert tree_cpu_ticks(10, str(tmp_path / "nowhere")) is None


def test_a_quiet_agent_whose_tree_uses_cpu_is_not_hanged(monkeypatch):
    from lazyagent.engine import session as session_mod
    from tests.engine.test_session import _agent

    session, _, _ = _agent(running=False)
    session.status = AgentStatus.RUNNING
    session.last_output_time = time.monotonic() - 700
    ticks = iter([100, 150, 150])
    monkeypatch.setattr(session_mod, "tree_cpu_ticks", lambda pid: next(ticks))
    session.check_hang(600)  # first sample: nothing to compare yet
    assert session.status == AgentStatus.POSSIBLY_HANGED
    session.status = AgentStatus.RUNNING
    session.check_hang(600)  # used CPU since: busy
    assert session.status == AgentStatus.RUNNING
    session.check_hang(600)  # idle since
    assert session.status == AgentStatus.POSSIBLY_HANGED
    session.status = AgentStatus.RUNNING
    session.last_output_time = time.monotonic() - 700
    monkeypatch.setattr(session_mod, "tree_cpu_ticks", lambda pid: None)
    session.check_hang(1800)  # a longer threshold
    assert session.status == AgentStatus.RUNNING


# ----------------------------------------------------------------------
# P9: mergeable UNKNOWN keeps the last known value
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mergeable_unknown_keeps_the_last_known_value_for_ten_minutes():
    from lazyagent.engine.worktrees import MERGEABLE_KEEP
    from tests.engine.test_ci_tests import FEAT

    engine, _, _ = _engine(FakeGh([]))
    service = engine.worktree_service
    now = [1000.0]
    service.clock = lambda: now[0]

    def pr(mergeable, number=7):
        return PrInfo(number, "Feat", "OPEN", [], mergeable=mergeable)

    assert service._keep_mergeable(FEAT, pr("CONFLICTING")).mergeable == "CONFLICTING"
    now[0] += 60
    assert service._keep_mergeable(FEAT, pr("UNKNOWN")).mergeable == "CONFLICTING"
    assert service._keep_mergeable(FEAT, pr("UNKNOWN", 8)).mergeable == "UNKNOWN"
    now[0] += MERGEABLE_KEEP
    assert service._keep_mergeable(FEAT, pr("UNKNOWN")).mergeable == "UNKNOWN"
    assert service._keep_mergeable(FEAT, pr("MERGEABLE")).mergeable == "MERGEABLE"
    assert service._keep_mergeable(FEAT, pr("")).mergeable == "MERGEABLE"
    assert service._keep_mergeable(FEAT, None) is None
    assert service._keep_mergeable(FEAT, pr("UNKNOWN")).mergeable == "UNKNOWN"


def test_the_readmes_r4c_examples_load_as_written():
    import re
    from pathlib import Path

    from lazyagent.engine.rules import builtin
    from lazyagent.engine.rules.user import compose, parse_file

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    section = readme.split("### Your rules (rules.toml)")[1].split("\n### ")[0]
    blocks = re.findall(r"```toml\n(.*?)```", section, re.S)
    [generated] = [b for b in blocks if "generated_paths" in b]
    [gates] = [b for b in blocks if "gating = false" in b]
    files = [parse_file("version = 1\n" + b, "global", GLOBAL_RULES) for b in (generated, gates)]
    assert all(f.errors == [] and f.warnings == [] for f in files)
    cat = compose(builtin(), files).catalog
    assert cat.generated_paths == ("**/generated-map.json", "docs/api/*.lock")
    assert [(c.name, c.gate) for c in cat.ci.checks] == [
        ("eval-suite", "nongating"), ("label-check", "policy")]


def test_the_review_query_is_made_for_ci_policy_only_with_a_policy_check():
    from lazyagent.engine.rules import builtin
    from lazyagent.engine.rules.user import compose, parse_file

    assert not builtin().reads_review
    for text, wanted in ((NONGATING, False), (POLICY, True)):
        f = parse_file(text, "global", GLOBAL_RULES)
        assert compose(builtin(), [f]).catalog.reads_review is wanted
