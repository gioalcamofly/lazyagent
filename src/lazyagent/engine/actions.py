"""GitHub Actions runs on the worktrees' branches (proposal 0013 I7, from
0012 A2; docs/design/inbox-plan.md §8).

One poll per repo, at most every ``[status] actions_poll_s`` (60 s): ``gh
api -i repos/{owner}/{repo}/actions/runs?per_page=100`` with
``If-None-Match``, so an unchanged list costs a 304, which GitHub does not
count against the rate limit. Only the repo's latest 100 runs are read, and
only while a worktree branch other than the merge targets is tracked.

**Fresh red on head.** A branch's head is its local head (``git
for-each-ref``, re-read on each poll), or the PR's head when it has an open
PR. Its runs count only while the newest run on the branch is on that head:
a newer run on another sha means the head moved (a push from elsewhere), a
head with no run yet means it was not pushed or CI has not started. On the
head, the latest run of each workflow decides: ``failure`` or
``timed_out`` is red, not completed is running, the rest is green. A re-run
(same id, a new attempt) is running again, and green once it passes.

**A head of its own.** On a busy repo the latest 100 runs hold the newer
runs of other branches, and a head's own runs get many of them (a label
workflow per label, a long evals run beside a quick check), so its red runs
can fall out of the window. Each open PR whose checks are red gets its own
read, ``actions/runs?head_sha=<sha>`` (with an ETag; at most
:data:`HEAD_PAGES` pages of 100), at most :data:`MAX_HEAD_FETCHES` a poll;
its runs join the window's for the verdict (the latest run of each
workflow on the head still decides). A red check that is not an Actions
run (a commit status, another provider's check run) has no log to read:
``ci_log_summary`` says so and names the check (:func:`other_checks`).

The failed jobs of a red run (``/runs/{id}/jobs``, the failing step) are
fetched once per run attempt, and its failed log (``gh run view <id>
--log-failed``) only on demand: a delegation, Home's expanded row, a brief's
``job:`` evidence. Both are cached. Every ``gh`` call counts against
:data:`MAX_CALLS_PER_MIN` and is cut off after a timeout; a 429 (or a 403
with no rate limit left) waits its ``Retry-After``, else twice the last wait
from a minute up to an hour, as the tracker adapters do. On an error the
last good runs stay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from lazyagent.engine.trackers import (
    BACKOFF_MAX,
    BACKOFF_MIN,
    RateLimited,
    TrackerError,
)
from lazyagent.engine.trackers.github_issues import (
    _rate_limited,
    _retry_after,
    split_response,
)
from lazyagent.models import PrInfo, WorkflowJob, WorkflowRun

log = logging.getLogger(__name__)

RUNS_PATH = "repos/{owner}/{repo}/actions/runs?per_page=100"
HEAD_PATH = "repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=100"
# A red PR head's own runs: pages of 100 read, heads read per poll.
HEAD_PAGES = 3
MAX_HEAD_FETCHES = 5
RED = ("failure", "timed_out")
DEFAULT_POLL_S = 60
# Every gh call of the hub (runs, jobs, logs) in a rolling minute.
MAX_CALLS_PER_MIN = 20
# Red runs whose jobs one poll fetches; the rest wait for the next poll.
MAX_JOB_FETCHES = 5
CALL_TIMEOUT = 30.0
# `gh run view --log-failed` downloads the run's logs.
LOG_TIMEOUT = 60.0
# The failed log is streamed, never held whole: per job, the first
# LOG_HEAD_BYTES and the last LOG_TAIL_BYTES of its (cleaned) lines are
# kept, the middle dropped; a run's jobs keep at most LOG_KEEP_BYTES in all
# (a later job's head and tail shrink to what is left), and reading stops
# after LOG_READ_BYTES.
LOG_HEAD_BYTES = 1024 * 1024
LOG_TAIL_BYTES = 4 * 1024 * 1024
LOG_KEEP_BYTES = 16 * 1024 * 1024
LOG_READ_BYTES = 256 * 1024 * 1024
STREAM_CHUNK = 64 * 1024
# The failed-log summary: the last lines of each failed job, a few jobs,
# each line cut, the whole text capped.
LOG_LINES = 30
LOG_JOBS = 3
LOG_RUNS = 2
LINE_CHARS = 300
SUMMARY_CHARS = 6000
# Run attempts whose jobs and logs are kept.
CACHE = 32

# argv → (exit code, stdout, stderr)
Run = Callable[[list[str]], Awaitable[tuple[int, str, str]]]
# (argv, a sink for stdout's chunks that returns False to stop reading,
# timeout) → (exit code, stderr); the call is cut off when the sink stops.
Stream = Callable[[list[str], Callable[[bytes], bool], float], Awaitable[tuple[int, str]]]

# Colour and terminal codes, with the ESC byte or as the two characters
# "^[" that gh writes for it into a pipe: CSI (``^[[31m``), OSC
# (``^[]8;;url^G``, ended by BEL or ST) and charset selection (``^[(B``).
_ESC = r"(?:\x1b|\^\[)"
_ANSI = re.compile(
    _ESC + r"(?:\[[0-9;?<=>!]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*?(?:\x07|\^G|\x1b\\|\^\[\\)"
    r"|[()][0-9A-Za-z])"
)
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")


def _gh_runner(cwd: str) -> Run:
    from lazyagent.worktree_manager import _run

    def run_sync(args: list[str]) -> tuple[int, str, str]:
        timeout = LOG_TIMEOUT if args[:1] == ["run"] else CALL_TIMEOUT
        try:
            proc = _run(["gh", *args], capture_output=True, text=True, cwd=cwd or None,
                        timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise TrackerError(f"gh timed out: {e}") from e
        except OSError as e:
            raise TrackerError(f"gh: {e}") from e
        return proc.returncode, proc.stdout, proc.stderr

    async def run(args: list[str]) -> tuple[int, str, str]:
        return await asyncio.to_thread(run_sync, args)

    return run


def _gh_streamer(cwd: str) -> Stream:
    """``gh`` with its stdout fed to a sink in chunks (never held whole);
    killed when the sink has had enough or the timeout passes."""

    def stream_sync(args: list[str], sink: Callable[[bytes], bool],
                    timeout: float) -> tuple[int, str]:
        import tempfile
        import threading

        with tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(["gh", *args], stdout=subprocess.PIPE, stderr=err,
                                        cwd=cwd or None)
            except OSError as e:
                raise TrackerError(f"gh: {e}") from e
            timed_out = threading.Event()

            def cut_off() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(timeout, cut_off)
            timer.start()
            stopped = False
            try:
                assert proc.stdout is not None
                while chunk := proc.stdout.read(STREAM_CHUNK):
                    if not sink(chunk):
                        stopped = True
                        proc.kill()
                        break
                code = proc.wait()
            finally:
                timer.cancel()
                if proc.stdout is not None:
                    proc.stdout.close()
            if timed_out.is_set():
                raise TrackerError(f"gh {args[0]} timed out")
            err.seek(0)
            return (0 if stopped else code), err.read(4096).decode(errors="replace")

    async def stream(args: list[str], sink: Callable[[bytes], bool],
                     timeout: float) -> tuple[int, str]:
        return await asyncio.to_thread(stream_sync, args, sink, timeout)

    return stream


def _stream_of(run: Run) -> Stream:
    """A :data:`Stream` over a plain runner (a fake): its stdout, fed at
    once."""

    async def stream(args: list[str], sink: Callable[[bytes], bool],
                     timeout: float) -> tuple[int, str]:
        code, out, err = await run(args)
        data = out.encode()
        for i in range(0, len(data), STREAM_CHUNK):
            if not sink(data[i:i + STREAM_CHUNK]):
                return 0, err
        return code, err

    return stream


def _heads_reader(repo_root: str) -> Callable[[], dict[str, str]]:
    from lazyagent.worktree_manager import WorktreeManager

    return lambda: WorktreeManager(repo_root).branch_heads()


def _timestamp(text: Any) -> float:
    if not isinstance(text, str) or not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def parse_runs(body: str) -> list[WorkflowRun]:
    """``/actions/runs``'s ``workflow_runs``; raises ValueError on a bad body."""
    try:
        data = json.loads(body)
        items = data["workflow_runs"]
        if not isinstance(items, list):
            raise TypeError("workflow_runs is not a list")
        return [
            WorkflowRun(
                id=int(r["id"]),
                workflow=str(r.get("name") or r.get("display_title") or ""),
                head_sha=str(r.get("head_sha") or ""),
                head_branch=str(r.get("head_branch") or ""),
                status=str(r.get("status") or ""),
                conclusion=str(r.get("conclusion") or ""),
                html_url=str(r.get("html_url") or ""),
                created_at=_timestamp(r.get("created_at")),
                attempt=int(r.get("run_attempt") or 1),
                workflow_id=int(r.get("workflow_id") or 0),
                path=str(r.get("path") or ""),
                event=str(r.get("event") or ""),
            )
            for r in items
            if isinstance(r, dict)
        ]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"bad actions/runs response: {e}") from e


def runs_total(body: str) -> int:
    """``/actions/runs``'s ``total_count`` (0 when absent)."""
    try:
        return int(json.loads(body).get("total_count") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


# Check hosts that are not GitHub Actions, as people name them.
PROVIDERS = {
    "circleci.com": "CircleCI",
    "buildkite.com": "Buildkite",
    "travis-ci.com": "Travis CI",
    "travis-ci.org": "Travis CI",
    "dev.azure.com": "Azure Pipelines",
    "gitlab.com": "GitLab CI",
    "jenkins": "Jenkins",
    "vercel.com": "Vercel",
    "netlify.com": "Netlify",
    "codecov.io": "Codecov",
    "sonarcloud.io": "SonarCloud",
}


def provider_of(url: str) -> str:
    """Who runs a check that is not an Actions run, from its URL: a known
    CI by its host, a GitHub App's check run, else the host itself."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "a commit status with no link"
    for known, name in PROVIDERS.items():
        if host == known or host.endswith("." + known) or known in host.split("."):
            return name
    if host == "github.com":
        return "a GitHub App (not Actions)"
    return host


def other_checks(pr: PrInfo | None) -> list[dict[str, str]]:
    """The PR's failing checks that are not Actions runs: no log to read."""
    if pr is None:
        return []
    return [
        {"name": c.name, "provider": provider_of(c.details_url), "url": c.details_url}
        for c in pr.checks
        if c.conclusion.upper() in ("FAILURE", "ERROR", "TIMED_OUT")
        and "/actions/runs/" not in c.details_url
    ]


def no_log_text(checks: list[dict[str, str]]) -> str:
    """``no Actions log: check e2e-suite from CircleCI``."""
    return "no Actions log: " + "; ".join(
        f"check {c['name']} from {c['provider']}" for c in checks
    )


def _github_step(name: str) -> bool:
    """A step GitHub adds to every job, not one of the workflow's."""
    return name in ("Set up job", "Complete job") or name.startswith("Post ")


def parse_jobs(body: str) -> tuple[WorkflowJob, ...]:
    """``/runs/{id}/jobs``: the jobs that failed or timed out, each with its
    first failing step."""
    try:
        data = json.loads(body)
        jobs = data["jobs"]
        out = []
        for j in jobs:
            if j.get("conclusion") not in RED:
                continue
            step = next(
                (s.get("name", "") for s in j.get("steps") or [] if s.get("conclusion") in RED),
                "",
            )
            own = [x for x in j.get("steps") or [] if not _github_step(str(x.get("name") or ""))]
            out.append(WorkflowJob(str(j.get("name") or ""), str(step), str(j.get("html_url") or ""),
                                   str(j.get("conclusion") or ""),
                                   _timestamp(j.get("started_at")), _timestamp(j.get("completed_at")),
                                   len(own)))
        return tuple(out)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"bad jobs response: {e}") from e


def _clean(line: str) -> str:
    line = _STAMP.sub("", _ANSI.sub("", line)).rstrip()
    if len(line) > LINE_CHARS:
        line = line[:LINE_CHARS - 1] + "…"
    return line


# A log line longer than this (raw) is cut where it is read.
RAW_LINE_BYTES = 64 * 1024


class FailedLogReader:
    """``gh run view --log-failed`` (``<job>\\t<step>\\t<time> <line>`` per
    line) read as it streams, in the log's order, timestamps and colour
    codes stripped. Per job, the first lines up to a head cap and the last up
    to a tail cap are kept and the middle is dropped (``cut``: the lines
    dropped), so memory stays bounded however long the log is; the summary
    and the runners' totals lines are at the end. ``feed`` returns False once
    :data:`LOG_READ_BYTES` are read."""

    # What a job keeps once the run's keep cap is spent: enough for the
    # summary and a runner's final lines.
    min_head = 16 * 1024
    min_tail = 64 * 1024

    def __init__(self, head: int = LOG_HEAD_BYTES, tail: int = LOG_TAIL_BYTES,
                 keep: int = LOG_KEEP_BYTES, limit: int = LOG_READ_BYTES) -> None:
        self.head, self.tail, self.left, self.limit = head, tail, keep, limit
        self.jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.read = 0
        self._carry = b""
        self._skipping = False

    def feed(self, chunk: bytes) -> bool:
        self.read += len(chunk)
        start = 0
        if self._skipping:
            # The rest of an over-long line, up to its newline.
            start = chunk.find(b"\n")
            if start < 0:
                return self.read < self.limit
            self._skipping = False
        *lines, self._carry = (self._carry + chunk[start:]).split(b"\n")
        for raw in lines:
            self._line(raw)
        if len(self._carry) > RAW_LINE_BYTES:
            self._carry, self._skipping = self._carry[:RAW_LINE_BYTES], True
        return self.read < self.limit

    def _job(self, name: str) -> dict[str, Any]:
        job = self.jobs.get(name)
        if job is None:
            head = min(self.head, max(self.left // 5, self.min_head))
            tail = min(self.tail, max(self.left - head, self.min_tail))
            self.left = max(self.left - head - tail, 0)
            job = self.jobs[name] = {"name": name, "step": "", "head": [], "head_left": head,
                                     "tail": deque(), "tail_size": 0, "tail_cap": tail,
                                     "cut": 0, "seen": 0}
        return job

    def _line(self, raw: bytes) -> None:
        name, sep, rest = raw.decode("utf-8", "replace").partition("\t")
        if not sep:
            return
        step, sep, line = rest.partition("\t")
        if not sep:
            line, step = step, ""
        job = self._job(name)
        if step and step != "UNKNOWN STEP":
            job["step"] = step
        line = _clean(line)
        size = len(line) + 1
        job["seen"] += 1
        if not job["tail"] and size <= job["head_left"]:
            job["head"].append(line)
            job["head_left"] -= size
            return
        tail = job["tail"]
        tail.append(line)
        job["tail_size"] += size
        while job["tail_size"] > job["tail_cap"] and len(tail) > 1:
            job["tail_size"] -= len(tail.popleft()) + 1
            job["cut"] += 1

    def close(self) -> list[dict[str, Any]]:
        """``[{name, step, lines, total, cut?, cut_at?}]``: ``lines`` the
        kept head and tail (trailing blank lines dropped), ``total`` every
        line read, ``cut`` the lines dropped between them, when any, at
        ``lines[cut_at]``."""
        if self._carry:
            self._line(self._carry)
            self._carry = b""
        out = []
        for job in self.jobs.values():
            lines = job["head"] + list(job["tail"])
            total = job["seen"]
            while lines and not lines[-1].strip():
                lines.pop()
                total -= 1
            entry = {"name": job["name"], "step": job["step"], "lines": lines, "total": total}
            if job["cut"]:
                entry["cut"] = job["cut"]
                entry["cut_at"] = min(len(job["head"]), len(lines))
            out.append(entry)
        return out


def parse_failed_log(text: str) -> list[dict[str, Any]]:
    """``gh run view --log-failed`` output, whole: see
    :class:`FailedLogReader`. :func:`trim` keeps the tail."""
    reader = FailedLogReader()
    reader.feed(text.encode())
    return reader.close()


def trim(jobs: list[dict[str, Any]], lines: int = LOG_LINES) -> list[dict[str, Any]]:
    """The last ``lines`` lines of each job (blank ones dropped), at most
    :data:`LOG_JOBS` jobs."""
    out = []
    for job in jobs[:LOG_JOBS]:
        kept = [x for x in job["lines"] if x.strip()][-lines:]
        out.append({**job, "lines": kept})
    return out


# ----------------------------------------------------------------------
# Fresh red on head
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CiVerdict:
    """What Actions says about one branch at one head."""

    branch: str
    head: str
    # red · running · green · moved (the newest run is on another sha)
    state: str
    # The latest run of each workflow on the head, newest first.
    runs: tuple[WorkflowRun, ...] = ()

    @property
    def red(self) -> tuple[WorkflowRun, ...]:
        return tuple(r for r in self.runs if r.red)


def _newest_first(run: WorkflowRun) -> tuple[float, int]:
    return (-run.created_at, -run.id)


def verdict(runs: Iterable[WorkflowRun], branch: str, head: str | None) -> CiVerdict | None:
    """Fresh red on head (module docstring); None with no run on the branch.
    With no head known, the newest run's sha stands for it."""
    mine = sorted((r for r in runs if r.head_branch == branch), key=_newest_first)
    if not mine:
        return None
    newest = mine[0].head_sha
    head = head or newest
    if newest != head:
        return CiVerdict(branch, head, "moved")
    latest: dict[Any, WorkflowRun] = {}
    for r in mine:
        if r.head_sha == head:
            latest.setdefault(r.workflow_id or r.workflow, r)
    on_head = tuple(sorted(latest.values(), key=_newest_first))
    if any(r.red for r in on_head):
        state = "red"
    elif any(r.status != "completed" for r in on_head):
        state = "running"
    else:
        state = "green"
    return CiVerdict(branch, head, state, on_head)


def run_title(run: WorkflowRun) -> str:
    """``<workflow> / <first failed job>``, or the workflow alone."""
    jobs = run.failed_jobs or ()
    return f"{run.workflow} / {jobs[0].name}" if jobs else run.workflow


def run_info(run: WorkflowRun) -> dict[str, Any]:
    """An inbox item's ``run``."""
    return {
        "id": run.id,
        "attempt": run.attempt,
        "workflow": run.workflow,
        "head_sha": run.head_sha,
        "branch": run.head_branch,
        "url": run.html_url,
        "conclusion": run.conclusion,
        "failed_jobs": None
        if run.failed_jobs is None
        else [{"name": j.name, "step": j.step, "url": j.url} for j in run.failed_jobs],
    }


def run_detail(run: WorkflowRun) -> str:
    jobs = run.failed_jobs or ()
    if not jobs:
        return f"{run.workflow}: {run.conclusion}"
    return f"{run.workflow}: " + ", ".join(
        f"{j.name} ({j.step})" if j.step else j.name for j in jobs
    )


def summary_text(runs: list[WorkflowRun], jobs: list[dict[str, Any]]) -> str:
    """The failed-log summary as the agent reads it."""
    out = []
    for run in runs:
        out.append(
            f"CI failed on {run.head_branch} at {run.head_sha[:7]}: {run.workflow} "
            f"({run.conclusion}, run {run.id}"
            + (f", attempt {run.attempt}" if run.attempt > 1 else "")
            + f") {run.html_url}".rstrip()
        )
    for job in jobs:
        step = f', step "{job["step"]}"' if job.get("step") else ""
        shown = len(job["lines"])
        tail = f" (last {shown} of {job['total']} lines)" if job["total"] > shown else ""
        out.append(f'Job "{job["name"]}"{step}{tail}:')
        out.extend(f"    {line}" for line in job["lines"])
    return "\n".join(out)


def ci_verdict(engine: Any, branch: str | None, pr: PrInfo | None) -> CiVerdict | None:
    """Actions on a worktree's head: the open PR's head, else the branch's
    local head. None for a closed or merged PR, or an untracked branch."""
    hub = getattr(engine, "actions", None)
    if hub is None or not branch:
        return None
    if pr is None:
        return hub.verdict(branch)
    if pr.state.upper() == "OPEN" and pr.head_sha:
        return hub.verdict(branch, pr.head_sha)
    return None


def red_runs(engine: Any, key: str) -> tuple[str, list[WorkflowRun]]:
    """The work item's branch and its fresh red runs on the head (none
    when it is not red); raises ValueError for an unknown work item."""
    path = engine.status.path_of(key)
    wt = engine.worktree(path) if path is not None else None
    if wt is None:
        raise ValueError(f"No work item {key!r}")
    v = ci_verdict(engine, wt.branch, engine.get_pr_info(path))
    return wt.branch or key, list(v.red) if v is not None and v.state == "red" else []


def ci_tests_of(engine: Any, key: str) -> Any:
    """The work item's mechanism-A verdict now (engine/ci_tests.py); None
    when nothing is configured or the work item is gone."""
    hub = getattr(engine, "actions", None)
    path = engine.status.path_of(key)
    wt = engine.worktree(path) if path is not None else None
    if hub is None or wt is None or not hub.tests.config.configured:
        return None
    pr = engine.get_pr_info(path)
    hub.tests.attestations = getattr(engine, "attestations", None)
    return hub.tests.analysis(wt.branch, ci_verdict(engine, wt.branch, pr), pr)


def _other_checks_of(engine: Any, key: str) -> list[dict[str, str]]:
    path = engine.status.path_of(key)
    pr = engine.get_pr_info(path) if path is not None else None
    return other_checks(pr) if pr is not None and pr.state.upper() == "OPEN" else []


async def log_summary(engine: Any, key: Any = None, run_id: Any = None) -> dict[str, Any]:
    """``ci_log_summary``: the failed-log summary of a work item's red runs
    on its head, or of one run."""
    hub: ActionsHub = engine.actions
    if key is not None and not isinstance(key, str):
        raise ValueError("key must be a string")
    if run_id is not None and type(run_id) is not int:
        raise ValueError("run_id must be an integer")
    if key is None and run_id is None:
        raise ValueError("key or run_id is required")
    if not hub.enabled:
        return {"key": key, "runs": [], "jobs": [], "text": "",
                "error": "CI tracking is off ([status] actions_poll_s = 0) or there is no repo"}
    if run_id is not None:
        run = hub.find_run(run_id)
        if run is None:
            return {"key": key, "runs": [], "jobs": [], "text": "",
                    "error": f"run {run_id} is not among the repo's latest runs"}
        runs = [run]
    else:
        branch, runs = red_runs(engine, key)
        checks = _other_checks_of(engine, key)
        if not runs:
            # A red that is not Actions': say whose it is, and link it.
            error = no_log_text(checks) if checks else f"no failed CI run on the head of {branch}"
            return {"key": key, "runs": [], "jobs": [], "text": "", "checks": checks,
                    "error": error}
        return {"key": key, **await hub.summary(runs), "checks": checks}
    return {"key": key, **await hub.summary(runs)}


# ----------------------------------------------------------------------
# The hub
# ----------------------------------------------------------------------


class ActionsHub:
    """The engine's Actions runs (one repo); see the module docstring."""

    def __init__(
        self,
        repo_root: str = "",
        *,
        poll_s: int = DEFAULT_POLL_S,
        run: Run | None = None,
        stream: Stream | None = None,
        read_heads: Callable[[], dict[str, str]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repo_root = repo_root
        self.poll_s = poll_s
        self.run = run or _gh_runner(repo_root)
        # A fake runner with no stream of its own is streamed from its
        # stdout, never the real gh.
        self.stream = stream or (_gh_streamer(repo_root) if run is None else _stream_of(run))
        self.read_heads = read_heads or _heads_reader(repo_root)
        self.clock = clock
        self.runs: list[WorkflowRun] = []
        # A red PR head's own runs (sha → runs, its page 1 ETag): they join
        # the window's for the verdict.
        self.head_runs: OrderedDict[str, list[WorkflowRun]] = OrderedDict()
        self.head_etags: dict[str, str] = {}
        # branch → the head of its open PR whose checks are red.
        self.red_heads: dict[str, str] = {}
        self.heads: dict[str, str] = {}
        self.tracked: frozenset[str] = frozenset()
        self.etag: str | None = None
        self.last_poll = float("-inf")
        self.error: str | None = None
        self.error_at = 0.0
        self.backoff = 0.0
        self.backoff_until = 0.0
        self.calls: deque[float] = deque()
        # The shared GitHub state (github_health.py) each call reports to;
        # the status projection sets it.
        self.health: Any = None
        self._polling = False
        # (run id, attempt) → failed jobs / the parsed failed log
        self._jobs: OrderedDict[tuple[int, int], tuple[WorkflowJob, ...]] = OrderedDict()
        self._logs: OrderedDict[tuple[int, int], list[dict[str, Any]]] = OrderedDict()
        # Mechanism A's test data (proposal 0014 R4): nothing until a rule
        # file gives a check tests_from.
        from lazyagent.engine.ci_tests import CiTestsHub, state_file

        self.tests = CiTestsHub(self, state_path=state_file(repo_root) if repo_root else None)

    @property
    def enabled(self) -> bool:
        return bool(self.repo_root) and self.poll_s > 0

    # --- Calls ---

    def may_call(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        return now >= self.backoff_until and len(self.calls) < MAX_CALLS_PER_MIN

    async def _call(self, args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
        """One gh call, counted and cut off; raises TrackerError."""
        timeout = timeout or CALL_TIMEOUT
        now = self.clock()
        if not self.may_call(now):
            raise TrackerError(
                "waiting out GitHub's rate limit" if now < self.backoff_until
                else "too many gh calls this minute"
            )
        self.calls.append(now)
        try:
            return await asyncio.wait_for(self.run(args), timeout)
        except asyncio.TimeoutError as e:
            if self.health is not None:
                self.health.failed(f"gh {args[0]} timed out", now)
            raise TrackerError(f"gh {args[0]} timed out") from e

    async def _stream(self, args: list[str], sink: Callable[[bytes], bool],
                      timeout: float) -> tuple[int, str]:
        """One streamed gh call (:data:`Stream`), counted like :meth:`_call`
        and cut off a little after its own timeout."""
        now = self.clock()
        if not self.may_call(now):
            raise TrackerError(
                "waiting out GitHub's rate limit" if now < self.backoff_until
                else "too many gh calls this minute"
            )
        self.calls.append(now)
        try:
            return await asyncio.wait_for(self.stream(args, sink, timeout), timeout + 5)
        except asyncio.TimeoutError as e:
            raise TrackerError(f"gh {args[0]} timed out") from e

    async def _api(self, path: str, etag: str | None = None) -> tuple[int, dict[str, str], str]:
        args = ["api", "-i", path]
        if etag:
            args[1:1] = ["-H", f"If-None-Match: {etag}"]
        started = self.clock()
        code, out, err = await self._call(args)
        # A 304 exits 1 ("gh: HTTP 304") with the status line on stdout.
        status, headers, body = split_response(out)
        if status == 429 or (status == 403 and headers.get("x-ratelimit-remaining") == "0"):
            raise RateLimited(f"HTTP {status}", _retry_after(headers))
        if status == 0:
            if _rate_limited(err):
                raise RateLimited(err.strip())
            # No HTTP answer at all: gh could not reach GitHub (or log in).
            if self.health is not None:
                self.health.failed(err.strip() or f"gh api: {code}", started)
            raise TrackerError(f"gh api {path} failed: {err.strip() or code}")
        if self.health is not None:
            self.health.succeeded()
        if status >= 400:
            raise TrackerError(f"gh api {path}: HTTP {status}")
        return status, headers, body

    def _failed(self, now: float, e: Exception) -> None:
        message = str(e) or type(e).__name__
        if isinstance(e, RateLimited):
            self.backoff = (
                e.retry_after if e.retry_after
                else min(max(self.backoff * 2, BACKOFF_MIN), BACKOFF_MAX)
            )
            self.backoff_until = now + self.backoff
        if message != self.error:
            # An outage is logged once, by the shared state.
            outage = self.health is not None and not self.health.available
            log.log(logging.DEBUG if outage else logging.WARNING, "actions: %s", message)
        self.error, self.error_at = message, now

    # --- The poll ---

    def due(self, now: float) -> bool:
        return now - self.last_poll >= self.poll_s

    async def poll(self, branches: Iterable[str], *, force: bool = False,
                   red_heads: dict[str, str] | None = None) -> bool:
        """One poll when due (``force``: now); True when what the rows read
        changed. Never two at once. ``red_heads``: branch → the head of its
        open PR whose checks are red, read on its own (module docstring)."""
        now = self.clock()
        if red_heads is not None:
            self.red_heads = {b: s for b, s in red_heads.items() if b and s}
        tracked = frozenset(b for b in branches if b)
        if tracked != self.tracked:
            # A new branch is looked at in this poll, not a minute later.
            force = force or bool(tracked - self.tracked)
            self.tracked = tracked
        if not self.enabled or self._polling or not tracked:
            return False
        if not force and not self.due(now):
            return False
        if not self.may_call(now):
            return False
        self._polling = True
        self.last_poll = now
        changed = False
        try:
            try:
                heads = await asyncio.to_thread(self.read_heads)
            except OSError:
                heads = self.heads
            if heads != self.heads:
                self.heads = dict(heads)
                changed = True
            try:
                status, headers, body = await self._api(RUNS_PATH, self.etag)
                if status != 304:
                    runs = parse_runs(body)
                    self.etag = headers.get("etag")
                    if runs != self.runs:
                        self.runs = runs
                        changed = True
                self.backoff = 0.0
                self.error = None
            except (TrackerError, ValueError, OSError) as e:
                self._failed(self.clock(), e)
            changed |= await self._fetch_heads()
            changed |= await self._fetch_jobs()
            if self.tests.config.configured:
                verdicts = {}
                for branch in sorted(self.tracked):
                    v = self.verdict(branch, self.red_heads.get(branch))
                    if v is not None:
                        verdicts[branch] = v
                changed |= await self.tests.fetch(verdicts)
        finally:
            self._polling = False
        return changed

    async def _fetch_heads(self) -> bool:
        """The runs of each red PR head (its own read, paginated)."""
        wanted = [s for b, s in sorted(self.red_heads.items()) if b in self.tracked]
        changed = False
        for sha in [s for s in list(self.head_runs) if s not in wanted]:
            del self.head_runs[sha]
            self.head_etags.pop(sha, None)
            changed = True
        for sha in wanted[:MAX_HEAD_FETCHES]:
            path = HEAD_PATH.replace("{sha}", sha)
            try:
                status, headers, body = await self._api(path, self.head_etags.get(sha))
                if status == 304:
                    continue
                runs = parse_runs(body)
                total = runs_total(body)
                page = 1
                while len(runs) < total and page < HEAD_PAGES:
                    page += 1
                    _, _, more = await self._api(f"{path}&page={page}")
                    batch = parse_runs(more)
                    if not batch:
                        break
                    runs += batch
            except (TrackerError, ValueError, OSError) as e:
                self._failed(self.clock(), e)
                continue
            if headers.get("etag"):
                self.head_etags[sha] = headers["etag"]
            if self.head_runs.get(sha) != runs:
                _put(self.head_runs, sha, runs)
                changed = True
        return changed

    def all_runs(self) -> list[WorkflowRun]:
        """The window's runs and the red heads' own, one per run id (a
        head's read is the newer)."""
        if not self.head_runs:
            return self.runs
        by_id = {r.id: r for r in self.runs}
        for runs in self.head_runs.values():
            by_id.update((r.id, r) for r in runs)
        return list(by_id.values())

    async def _fetch_jobs(self) -> bool:
        """The failed jobs of each fresh red run that has none yet."""
        wanted = []
        runs = self.all_runs()
        for branch in sorted(self.tracked):
            v = verdict(runs, branch, self.red_heads.get(branch) or self.heads.get(branch))
            if v is not None:
                wanted += [r for r in v.red if (r.id, r.attempt) not in self._jobs]
        changed = False
        for run in wanted[:MAX_JOB_FETCHES]:
            if await self.jobs(run) is not None:
                changed = True
        return changed

    async def jobs(self, run: WorkflowRun) -> tuple[WorkflowJob, ...] | None:
        """``run``'s failed jobs (cached per attempt); None when they could
        not be read."""
        slot = (run.id, run.attempt)
        if slot in self._jobs:
            return self._jobs[slot]
        try:
            _, _, body = await self._api(
                f"repos/{{owner}}/{{repo}}/actions/runs/{run.id}/attempts/{run.attempt}/jobs"
                "?per_page=100"
            )
            jobs = parse_jobs(body)
        except (TrackerError, ValueError, OSError) as e:
            self._failed(self.clock(), e)
            return None
        _put(self._jobs, slot, jobs)
        return jobs

    # --- What the rows read ---

    def _with_jobs(self, run: WorkflowRun) -> WorkflowRun:
        jobs = self._jobs.get((run.id, run.attempt))
        return run if jobs is None else replace(run, failed_jobs=jobs)

    def head(self, branch: str) -> str | None:
        return self.heads.get(branch)

    def verdict(self, branch: str | None, head: str | None = None) -> CiVerdict | None:
        """Fresh red on ``branch``'s head (default: its local head), with
        the failed jobs known so far."""
        if not branch or not self.enabled or branch not in self.tracked:
            return None
        v = verdict(self.all_runs(), branch, head or self.heads.get(branch))
        if v is None:
            return None
        return replace(v, runs=tuple(self._with_jobs(r) for r in v.runs))

    def find_run(self, run_id: int) -> WorkflowRun | None:
        run = next((r for r in self.all_runs() if r.id == run_id), None)
        return self._with_jobs(run) if run is not None else None

    # --- The failed-log summary ---

    def cached_summary(self, runs: list[WorkflowRun]) -> dict[str, Any] | None:
        """:meth:`summary` when every run's log is cached already."""
        if not runs or any((r.id, r.attempt) not in self._logs for r in runs[:LOG_RUNS]):
            return None
        return self._summary(runs[:LOG_RUNS])

    async def summary(self, runs: list[WorkflowRun]) -> dict[str, Any]:
        """``{runs, jobs, text, error?}``: the failed jobs of ``runs``, the
        failing step and the tail of each failed log, trimmed to
        :data:`SUMMARY_CHARS`."""
        runs = runs[:LOG_RUNS]
        errors = []
        for i, run in enumerate(runs):
            if run.failed_jobs is None:
                jobs = await self.jobs(run)
                if jobs is not None:
                    runs[i] = run = replace(run, failed_jobs=jobs)
            slot = (run.id, run.attempt)
            if slot in self._logs:
                continue
            reader = FailedLogReader()
            try:
                code, err = await self._stream(
                    ["run", "view", str(run.id), "--log-failed"], reader.feed, LOG_TIMEOUT
                )
                logged = reader.close()
                if code != 0 and not logged:
                    raise TrackerError(f"gh run view {run.id}: {err.strip() or code}")
            except (TrackerError, OSError) as e:
                self._failed(self.clock(), e)
                errors.append(str(e) or type(e).__name__)
                continue
            _put(self._logs, slot, logged)
        result = self._summary(runs)
        if errors:
            result["error"] = "; ".join(errors)
        return result

    def _summary(self, runs: list[WorkflowRun]) -> dict[str, Any]:
        jobs: list[dict[str, Any]] = []
        for run in runs:
            logged = self._logs.get((run.id, run.attempt))
            failed = list(run.failed_jobs or ())
            by_name = {j.name: j for j in failed}
            if logged is None:
                # No log (yet): the failed jobs and their steps alone.
                jobs += [{"name": j.name, "step": j.step, "url": j.url, "lines": [], "total": 0}
                         for j in failed]
                continue
            for job in logged:
                known = by_name.get(job["name"])
                jobs.append({
                    "name": job["name"],
                    "step": (known.step if known is not None and known.step else job["step"]),
                    "url": known.url if known is not None else run.html_url,
                    "lines": job["lines"],
                    "total": job["total"],
                })
        lines = LOG_LINES
        while True:
            shown = trim(jobs, lines)
            text = summary_text(runs, shown)
            if len(text) <= SUMMARY_CHARS or lines <= 5:
                break
            lines //= 2
        if len(text) > SUMMARY_CHARS:
            text = text[:SUMMARY_CHARS - 1] + "…"
        return {"runs": [run_info(r) for r in runs], "jobs": shown, "text": text}


def _put(cache: OrderedDict, key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > CACHE:
        cache.popitem(last=False)
