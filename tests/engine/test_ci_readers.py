"""Mechanism A's readers on a large CI (proposal 0014 R4b): colour codes as gh
writes them into a pipe (``^[`` for ESC), aggregate jobs (a ``summary`` that
waits on the test jobs), a JUnit artifact of thousands of files read from its
zip, and a failed log longer than the caps read by its head and tail. Every
``gh`` is :class:`tests.engine.fake_gh.FakeGh` (or a fake ``gh`` script on
PATH) and every log and zip is synthetic."""

from __future__ import annotations

import io
import os
import stat
import time
import tracemalloc
import zipfile

import pytest

from lazyagent.engine import actions, ci_tests
from lazyagent.engine.actions import FailedLogReader, _clean, parse_failed_log, parse_jobs
from lazyagent.engine.ci_tests import parse_log, parse_tables, read_junit_zip, suite_counts
from lazyagent.engine.rules.export import format_explain
from tests.engine.fake_gh import failed_log
from tests.engine.test_ci_tests import (
    CHECK,
    CONFIG,
    JOB,
    KEY,
    TWO,
    WF,
    _engine,
    _gh,
    _items,
    _log,
    _poll,
    _rows,
)
from tests.protocol.golden_cases import GLOBAL_RULES, _rules_files

# ----------------------------------------------------------------------
# B1: "^[" for ESC
# ----------------------------------------------------------------------

# pytest --color=yes as gh run view --log-failed prints it into a pipe: ESC
# written as the two characters "^[".
PYTEST_CARET = """\
^[[1m============================= test session starts ==============================^[[0m
collected 122 items

tests/test_a.py ^[[32m.^[[0m^[[32m.^[[0m^[[31mF^[[0m^[[32m.^[[0m^[[31m                                                     [  3%]^[[0m
^[[31m^[[1m___________________________ test_retry[exponential] ____________________________^[[0m
^[[1m^[[31mE   AssertionError: assert 3 == 2^[[0m
^[[36m^[[1m=========================== short test summary info ============================^[[0m
^[[31mFAILED^[[0m tests/test_a.py::^[[1mtest_retry[exponential]^[[0m - AssertionError: assert 3 == 2
^[[31mFAILED^[[0m tests/test_b.py::^[[1mTestExport::test_csv^[[0m - KeyError: 'id'
^[[31m================== ^[[31m^[[1m2 failed^[[0m, ^[[32m120 passed^[[0m, ^[[33m3 skipped^[[0m^[[31m in 41.20s^[[0m^[[31m ===================^[[0m
"""

# vitest in CI, the same way.
VITEST_CARET = """\
 ^[[31m❯^[[39m src/cart.test.ts ^[[2m(^[[22m^[[2m3 tests^[[22m^[[2m | ^[[22m^[[31m1 failed^[[39m^[[2m)^[[22m
^[[31m⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯^[[39m
^[[41m^[[1m FAIL ^[[22m^[[49m src/cart.test.ts^[[2m > ^[[22mCart^[[2m > ^[[22mtotals
^[[31m^[[1mAssertionError^[[22m: expected 3 to be 2^[[39m
^[[41m^[[1m FAIL ^[[22m^[[49m src/api.test.ts^[[2m > ^[[22mapi^[[2m > ^[[22mretries
^[[2m Test Files ^[[22m ^[[1m^[[31m2 failed^[[39m^[[22m^[[2m | ^[[22m^[[1m^[[32m5 passed^[[39m^[[22m^[[90m (7)^[[39m
^[[2m      Tests ^[[22m ^[[1m^[[31m2 failed^[[39m^[[22m^[[2m | ^[[22m^[[1m^[[32m30 passed^[[39m^[[22m^[[90m (32)^[[39m
"""


def _as_gh(text: str, job: str = JOB, step: str = "Run tests") -> str:
    return failed_log(job, step, text.splitlines())


def test_caret_escapes_are_stripped_like_the_esc_byte():
    assert _clean("^[[31mFAILED^[[0m x") == "FAILED x"
    assert _clean("\x1b[31mFAILED\x1b[0m x") == "FAILED x"
    # OSC (a hyperlink) ended by BEL or ST, in either form; charset selection.
    assert _clean("^[]8;;file:///a.py^Ga.py^[]8;;^G ok") == "a.py ok"
    assert _clean("\x1b]8;;file:///a.py\x1b\\a.py\x1b]8;;\x1b\\ ok") == "a.py ok"
    assert _clean("^[(B^[[mdone") == "done"
    # A caret that is not an escape stays.
    assert _clean("x ^ y ^[ z") == "x ^ y ^[ z"


def test_a_coloured_pytest_log_in_the_caret_form_is_read():
    [job] = parse_failed_log(_as_gh(PYTEST_CARET))
    found = parse_log("pytest", job["lines"])
    assert found.ids == {"tests/test_a.py::test_retry[exponential]",
                         "tests/test_b.py::TestExport::test_csv"}
    # I7's summary shows it clean too.
    assert all("^[" not in line for line in job["lines"])
    assert job["lines"][-1].startswith("=== 2 failed") or "2 failed, 120 passed" in job["lines"][-1]


def test_a_coloured_vitest_log_in_the_caret_form_is_read():
    [job] = parse_failed_log(_as_gh(VITEST_CARET))
    found = parse_log("vitest", job["lines"])
    assert found.ids == {"src/cart.test.ts::Cart > totals", "src/api.test.ts::api > retries"}


@pytest.mark.asyncio
async def test_a_caret_coloured_log_clears_fix_ci_end_to_end():
    gh = _gh()
    for run_id in (11, 21):
        gh.logs[run_id] = _as_gh(PYTEST_CARET.replace("TestExport::test_csv", "test_csv"),
                                 step="Run pytest")
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
        summary = await engine.actions.summary(list(engine.actions.verdict("feat").red))
        assert "^[" not in summary["text"]


# ----------------------------------------------------------------------
# B2: aggregate jobs
# ----------------------------------------------------------------------

T0 = "2026-10-24T10:00:00Z"


def _job(name: str, *, started: str = T0, completed: str = "2026-10-24T10:05:00Z",
         steps: tuple[str, ...] = ("Run pytest",), conclusion: str = "failure") -> dict:
    return {"name": name, "conclusion": conclusion, "html_url": f"https://github.com/o/r/job/{name}",
            "started_at": started, "completed_at": completed,
            "steps": [{"name": "Set up job", "conclusion": "success"},
                      *({"name": s, "conclusion": "failure" if i == len(steps) - 1 else "success"}
                        for i, s in enumerate(steps)),
                      {"name": "Complete job", "conclusion": "success"}]}


def _summary(steps: tuple[str, ...] = ("Check the needed jobs",)) -> dict:
    return _job("summary", started="2026-10-24T10:05:01Z", completed="2026-10-24T10:05:04Z",
                steps=steps)


def _with_summary(gh, summary: dict | None = None) -> None:
    for run_id in (11, 21):
        gh.jobs[run_id] = {"jobs": [_job(JOB), summary or _summary()]}


def test_the_jobs_data_carries_what_tells_an_aggregate_apart():
    unit, summary = parse_jobs(
        '{"jobs": [%s, %s]}' % tuple(__import__("json").dumps(j) for j in (
            _job(JOB, steps=("Checkout", "Run pytest")),
            _summary(("Check", "Post Check")))))
    assert unit.own_steps == 2 and summary.own_steps == 1
    assert summary.started_at > unit.completed_at > unit.started_at > 0


@pytest.mark.asyncio
async def test_a_configured_aggregate_is_unrelated_when_the_jobs_it_waits_on_are():
    gh = _gh()
    # More steps than auto-detection takes: the config alone decides.
    _with_summary(gh, _summary(("Checkout", "Install", "Check")))
    engine, _, _ = _engine(gh)
    config = CONFIG + '\n[ci.check."summary"]\naggregate = true\n'
    with _rules_files({GLOBAL_RULES: config}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
        t = engine.status.facts(KEY).ci_tests
        assert t.unrelated is True
        unit, summary = t.checks
        assert summary.check == f"{WF}/summary" and summary.aggregate_of == (JOB,)
        assert summary.detail == f"waits on {JOB}: unrelated"
        assert summary.point == unit.point
        assert (await _rows(engine))[KEY]["ci_note"] == "CI red · unrelated (base also red: 2 tests)"
        # Nothing is read for the aggregate itself: no log of its own.
        assert engine.status.explain(KEY)["ci"]["checks"][1]["aggregate_of"] == [JOB]


@pytest.mark.asyncio
async def test_an_aggregate_is_related_naming_the_job_that_is():
    gh = _gh(branch_tests=[*TWO, "tests/test_c.py::test_new"])
    _with_summary(gh)
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        [item] = _items(engine, "ci")
        assert item["state"] == "CI red: unit-tests (1 new)"
        t = engine.status.facts(KEY).ci_tests
        assert t.unrelated is False
        summary = t.checks[1]
        assert not summary.unrelated
        assert summary.detail == (f"waits on {JOB}, related: 1 failing test not failing on "
                                  "main at ccccccc")
        why = format_explain(engine.status.explain(KEY))
        assert f"summary: related: waits on {JOB}, related" in why


@pytest.mark.asyncio
async def test_an_aggregate_is_detected_from_the_runs_jobs_alone():
    gh = _gh()
    _with_summary(gh)
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
        assert engine.status.facts(KEY).ci_tests.checks[1].aggregate_of == (JOB,)


@pytest.mark.parametrize("summary, why", [
    # A job with work of its own (a lint after the tests).
    (_summary(("Checkout", "Install", "Lint")), "no tests_from"),
    # One that ran beside the tests, not after them.
    (_job("summary", started=T0, steps=("Check",)), "no tests_from"),
])
@pytest.mark.asyncio
async def test_a_job_that_does_not_look_like_an_aggregate_stays_related(summary, why):
    gh = _gh()
    _with_summary(gh, summary)
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert len(_items(engine, "ci")) == 1
        assert why in engine.status.facts(KEY).ci_tests.checks[1].detail


@pytest.mark.asyncio
async def test_aggregate_false_turns_the_detection_off_and_a_lone_aggregate_is_related():
    gh = _gh()
    _with_summary(gh)
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG + '\n[ci.check."summary"]\naggregate = false\n'}):
        engine.rules.reload()
        await _poll(engine)
        assert len(_items(engine, "ci")) == 1
    gh = _gh()
    for run_id in (11, 21):
        gh.jobs[run_id] = {"jobs": [_job(JOB, conclusion="cancelled"), _summary()]}
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG + '\n[ci.check."summary"]\naggregate = true\n'}):
        engine.rules.reload()
        await _poll(engine)
        [c] = engine.status.facts(KEY).ci_tests.checks
        assert c.unrelated is False and "no other failing job in run 11" in c.detail


def test_aggregate_config_and_its_errors():
    part, errors, _ = parse_tables({"check": {
        "summary": {"aggregate": True},
        "all-green": {"aggregate": "yes"},
        "both": {"aggregate": True, "tests_from": ["log:pytest"]},
        "off": {"aggregate": False, "tests_from": ["log:pytest"]},
    }}, None)
    assert [(c.name, c.tests_from, c.aggregate) for c in part.checks] == [
        ("summary", (), True), ("off", ("log:pytest",), False)]
    assert any("aggregate must be true or false" in e for e in errors)
    assert any("an aggregate job has no tests of its own" in e for e in errors)
    # An aggregate alone configures nothing: no call is made.
    cfg = ci_tests.merge_parts([("global", parse_tables(
        {"check": {"summary": {"aggregate": True}}}, None)[0])])
    assert not cfg.configured
    assert cfg.to_dict()["checks"] == [{"name": "summary", "tests_from": [], "aggregate": True}]


# ----------------------------------------------------------------------
# B3a: a JUnit artifact of thousands of files
# ----------------------------------------------------------------------

def _suite(i: int, failing: bool) -> str:
    cases = "".join(
        f'<testcase classname="app{i}.tests.test_views.ViewTests" name="test_{k}" time="0.01"/>'
        for k in range(9))
    bad = (f'<testcase classname="app{i}.tests.test_views.ViewTests" name="test_broken">'
           '<failure message="boom">Traceback ...</failure></testcase>') if failing else (
        f'<testcase classname="app{i}.tests.test_views.ViewTests" name="test_9" time="0.01"/>')
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="app{i}" tests="10" '
            f'errors="0" failures="{1 if failing else 0}" skipped="0">{cases}{bad}'
            '<system-out><![CDATA[noise]]></system-out></testsuite>\n')


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


FAILING = (17, 2500, 5999)


def _big_zip(n: int = 6000) -> bytes:
    return _zip({f"reports/TEST-app{i}.xml": _suite(i, i in FAILING) for i in range(n)})


def test_a_6000_file_junit_zip_is_read_within_time_and_memory(tmp_path, capsys):
    path = tmp_path / "a.zip"
    path.write_bytes(_big_zip())
    tracemalloc.start()
    start = time.perf_counter()
    ids, error = read_junit_zip(str(path))
    seconds = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert error is None
    assert sorted(ids) == sorted(f"app{i}.tests.test_views.ViewTests::test_broken"
                                 for i in FAILING)
    with capsys.disabled():
        print(f"\n[R4b] JUnit zip: 6000 files, {path.stat().st_size >> 10} KiB zipped: "
              f"{seconds:.2f} s, peak {peak / 2**20:.1f} MiB (tracemalloc)")
    assert seconds < 10
    assert peak < 8 * 2**20


def test_the_junit_count_check_and_the_caps(tmp_path, monkeypatch):
    def read(files, **kw):
        path = tmp_path / "x.zip"
        path.write_bytes(_zip(files))
        return read_junit_zip(str(path), **kw)

    # A file that says it has a failure the parse does not find.
    lying = _suite(1, False).replace('failures="0"', 'failures="1"')
    assert read({"a.xml": _suite(0, True), "b.xml": lying}) == (
        [], "found 1 of 2 failures and errors its 2 files count (20 tests)")
    # Files without counts: no check, as before.
    bare = '<testsuite><testcase classname="a" name="t"><error/></testcase></testsuite>'
    assert read({"a.xml": bare}) == (["a::t"], None)
    # A <testsuites> root's own counts are taken, not added to its suites'.
    nested = (b'<testsuites tests="3" failures="1" errors="0"><testsuite tests="3" failures="1">'
              b'<testcase classname="a" name="t"><failure/></testcase></testsuite></testsuites>')
    assert suite_counts(nested) == (3, 1)
    assert read({"a.xml": nested}) == (["a::t"], None)
    # A DTD in a failing file is refused; one in a passing file is never parsed.
    dtd = '<!DOCTYPE x [<!ENTITY a "b">]>' + _suite(2, True)
    assert "a DTD or an entity" in read({"a.xml": dtd})[1]
    assert read({"a.xml": "<!DOCTYPE x>" + _suite(2, False), "b.xml": _suite(3, True)})[1] is None
    assert read({"a.txt": "x"}) == ([], "no XML file in it")
    # Byte caps and the time cap, not a file count.
    monkeypatch.setattr(ci_tests, "JUNIT_MAX_FILE_BYTES", 100)
    assert "is larger than" in read({"a.xml": _suite(0, True)})[1]
    monkeypatch.setattr(ci_tests, "JUNIT_MAX_FILE_BYTES", 32 * 2**20)
    monkeypatch.setattr(ci_tests, "JUNIT_MAX_XML_BYTES", 1000)
    assert "its XML is larger than" in read({f"{i}.xml": _suite(i, False) for i in range(5)})[1]
    monkeypatch.setattr(ci_tests, "JUNIT_MAX_XML_BYTES", 512 * 2**20)
    ticks = iter(range(0, 1000, 10))
    assert "took over 30 s" in read({f"{i}.xml": _suite(i, False) for i in range(5)},
                                     clock=lambda: next(ticks))[1]


@pytest.mark.asyncio
async def test_a_large_junit_artifact_is_streamed_and_compared(monkeypatch):
    gh = _gh()
    for run_id in (11, 21):
        gh.artifacts[run_id] = [{"id": run_id * 10, "name": "junit-3.12", "size_in_bytes": 900}]
        gh.zips[run_id * 10] = _big_zip(1500)
    gh.logs.clear()
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        t = engine.status.facts(KEY).ci_tests
        assert t.unrelated is True and len(t.checks[0].tests) == 1
        assert t.checks[0].tests == {f"{CHECK}::app17.tests.test_views.ViewTests::test_broken"}
    # A zip larger than the cap stops the download: unknown, then the log.
    monkeypatch.setattr(ci_tests, "JUNIT_MAX_BYTES", 64 * 1024)
    gh = _gh()
    gh.artifacts[11] = [{"id": 110, "name": "junit-3.12", "size_in_bytes": 900}]
    gh.zips[110] = os.urandom(200 * 1024)
    engine, _, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        c = engine.status.facts(KEY).ci_tests.checks[0]
        assert c.unrelated is True
        found = engine.actions.tests._found[(11, 1, JOB)]
        assert found.source == "log:pytest"


# ----------------------------------------------------------------------
# B3b: a failed log longer than the caps
# ----------------------------------------------------------------------

def _long_log_chunks(mb: int, tests: list[str], job: str = JOB):
    """A pytest job's failed log of about ``mb`` MiB in gh's form, made
    lazily in 64 KiB chunks: noise, then the summary at the end."""
    stamp = "2026-10-24T10:01:02.1234567Z "
    noise = f"{job}\tRun pytest\t{stamp}" + "tests/test_x.py::test_case PASSED " + "." * 60 + "\n"
    target = mb * 2**20
    head = f"{job}\tRun pytest\t{stamp}collected 9000 items\n"
    buf, size = [head], len(head)
    while size < target:
        buf.append(noise)
        size += len(noise)
        if len(buf) >= 700:
            yield "".join(buf).encode()
            buf = []
    tail = [f"FAILED {t} - AssertionError" for t in tests]
    tail.append(f"======== {len(tests)} failed, 8998 passed in 900.10s ========")
    buf += [f"{job}\tRun pytest\t{stamp}{line}\n" for line in tail]
    yield "".join(buf).encode()


def test_a_12_mb_log_keeps_its_head_and_tail_in_bounded_memory(capsys):
    tracemalloc.start()
    start = time.perf_counter()
    reader = FailedLogReader()
    read = 0
    for chunk in _long_log_chunks(12, TWO):
        read += len(chunk)
        assert reader.feed(chunk)
    [job] = reader.close()
    seconds = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    with capsys.disabled():
        print(f"\n[R4b] failed log: {read / 2**20:.1f} MiB streamed: {seconds:.2f} s, "
              f"peak {peak / 2**20:.1f} MiB (tracemalloc), {job['cut']} lines dropped")
    assert job["lines"][0] == "collected 9000 items"
    assert job["cut"] > 0 and job["total"] == len(job["lines"]) + job["cut"]
    assert sum(len(x) + 1 for x in job["lines"]) <= actions.LOG_HEAD_BYTES + actions.LOG_TAIL_BYTES
    found = parse_log("pytest", job["lines"], cut=job["cut"], cut_at=job["cut_at"])
    assert found.ids == set(TWO)
    assert seconds < 10
    # The kept lines (5 MiB of text) and their objects; never the 12 MiB.
    assert peak < 12 * 2**20


def test_a_cut_log_trusts_only_the_parsers_whose_totals_are_one_final_line():
    lines = ["--- FAIL: TestA (0.0s)", "FAIL\texample.com/a\t0.1s"]
    found = parse_log("go", lines, cut=500, cut_at=1)
    assert found.ids is None and found.why == (
        "go: the log's middle (500 lines) was not read, and go's totals cannot be checked "
        "without it")
    assert parse_log("cargo", ["x"], cut=2).ids is None
    # jest: a ● after the cut is not put under a FAIL line before it.
    jest = ["FAIL src/a.test.js", "  ● a › one", "  ● b › two", "Tests: 2 failed, 9 passed"]
    assert parse_log("jest", jest, cut=10, cut_at=2).why == "jest parser found 1 of 2 failures"
    assert parse_log("jest", jest).known


def test_the_reader_shares_its_keep_cap_and_stops_at_the_read_cap():
    reader = FailedLogReader(head=100, tail=400, keep=600, limit=10_000)
    reader.min_head, reader.min_tail = 10, 50
    for job in ("a", "b", "c"):
        for i in range(200):
            assert reader.feed(f"{job}\ts\t{i:08d}\n".encode()) or job == "c"
    a, b, c = reader.close()
    assert a["total"] == b["total"] == 200 and a["lines"][-1] == "00000199"
    # The first job took most of the 600; the later ones what was left.
    assert sum(len(x) + 1 for x in a["lines"]) <= 500
    assert len(b["lines"]) < len(a["lines"]) and c["lines"][-1].startswith("000001")
    assert not reader.feed(b"x" * 20_000)
    # A line with no newline for ever is cut, not held whole.
    reader = FailedLogReader()
    reader.feed(b"a\ts\t" + b"y" * (actions.RAW_LINE_BYTES * 3))
    reader.feed(b"y" * 1000 + b"\na\ts\tnext\n")
    [job] = reader.close()
    assert job["lines"][-1] == "next" and len(job["lines"][0]) == actions.LINE_CHARS


@pytest.mark.asyncio
async def test_a_12_mb_failed_log_clears_fix_ci_and_the_summary_shows_its_end():
    gh = _gh()
    gh.logs[11] = b"".join(_long_log_chunks(12, TWO)).decode()
    engine, hub, _ = _engine(gh)
    with _rules_files({GLOBAL_RULES: CONFIG}):
        engine.rules.reload()
        await _poll(engine)
        assert _items(engine, "ci") == []
    summary = await hub.summary(list(hub.verdict("feat").red))
    [job] = summary["jobs"]
    assert job["lines"][-1] == "======== 2 failed, 8998 passed in 900.10s ========"
    assert job["total"] > 80_000 and job["total"] > len(job["lines"])
    assert gh.logs[21] == _log(TWO)


# ----------------------------------------------------------------------
# The real streamer, on a fake gh
# ----------------------------------------------------------------------


def _fake_gh(tmp_path, monkeypatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\n" + script)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.asyncio
async def test_the_streamer_feeds_chunks_stops_when_told_and_times_out(tmp_path, monkeypatch):
    _fake_gh(tmp_path, monkeypatch,
             'if [ "$1" = slow ]; then exec sleep 5; fi\n'
             'if [ "$1" = fail ]; then echo nope >&2; exit 3; fi\n'
             'yes "a line of the log" | head -c 3000000\n')
    stream = actions._gh_streamer(str(tmp_path))
    got = []
    code, err = await stream(["run", "view"], lambda c: got.append(len(c)) or True, 10)
    assert code == 0 and sum(got) == 3_000_000
    got.clear()
    code, _ = await stream(["run", "view"], lambda c: got.append(len(c)) or sum(got) < 100_000, 10)
    assert code == 0 and sum(got) < 3_000_000
    assert await stream(["fail"], lambda c: True, 10) == (3, "nope\n")
    start = time.monotonic()
    with pytest.raises(actions.TrackerError, match="timed out"):
        await stream(["slow"], lambda c: True, 0.3)
    assert time.monotonic() - start < 4
