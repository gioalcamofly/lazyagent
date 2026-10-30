"""Re-running jobs that failed for infrastructure reasons (proposal 0014 R5a).

**A deliberate exception.** The engine reads GitHub and never writes to it
on its own: every other CI action (a re-run, a comment, a merge) is the
user's or an agent's. This is the one CI write it makes by itself, because
a job whose runner went away (a spot runner reclaimed, a host lost) says
nothing about the branch, and re-running it is what anyone would do. It is
bounded and switchable: ``[ci] rerun_infra = false`` turns it off, ``[ci]
rerun_infra_max_attempts`` (3) caps it.

**What counts as infrastructure.** A job of a red run that concluded
``failure`` or ``cancelled`` with **no failing step**, and either one of
:data:`MARKERS` in its annotations (``check-runs/<job id>/annotations``,
read only for such a job) or no step that concluded at all. A job that
timed out is not one (a hang may be the branch's). A run with any other
failing job is left alone: its red is real either way, and a re-run would
only hide it for a while (a matrix's ``fail-fast`` cancels the siblings of a
failing job, with "The operation was canceled").

**Never re-run:** a job with a failing step; a nondeterministic check
(``[ci] nondeterministic``: an eval suite) or one whose name says eval; a
check ``[ci.check] policy = true`` marks; a run whose ``run_attempt`` has
reached the cap. A re-run gets a new attempt (and new job ids), so the cap
holds across polls; each (run, attempt, job) is tried once.

**The call.** ``gh run rerun --job <job id>``, through the Actions hub (its
caps, and only while GitHub answers). Then the run reads as queued until
GitHub shows the new attempt (at most :data:`PENDING_FOR`): the row's CI is
running, not red, so no item and no auto-delegation for that red. Each
re-run goes on the row's timeline (``re-ran <job> (runner lost), attempt
2``) and in the log at INFO.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from lazyagent.engine.trackers import TrackerError

if TYPE_CHECKING:
    from lazyagent.engine.actions import ActionsHub
    from lazyagent.engine.ci_tests import CiConfig
    from lazyagent.models import PrInfo, WorkflowRun

log = logging.getLogger(__name__)

# What a runner that went away leaves in a job's annotations (lower case),
# and what the timeline calls it.
MARKERS: tuple[tuple[str, str], ...] = (
    ("the runner has received a shutdown signal", "runner shut down"),
    ("lost communication with the server", "runner lost"),
    ("the operation was canceled", "cancelled, no step failed"),
)
NO_STEP = "no step ran"
DEFAULT_MAX_ATTEMPTS = 3
# A re-run reads as queued this long at most, until the new attempt shows.
PENDING_FOR = 15 * 60.0
# Annotations read per poll.
MAX_ANNOTATION_FETCHES = 3
ANNOTATIONS_PATH = "repos/{owner}/{repo}/check-runs/{id}/annotations?per_page=50"


@dataclass(frozen=True)
class InfraJob:
    """A job of a red run that did not pass, as the infra check reads it."""

    id: int
    name: str
    conclusion: str
    url: str = ""
    # The first step that failed or timed out ("": none).
    failed_step: str = ""
    # Steps with any conclusion.
    concluded: int = 0


def parse_infra(body: str) -> tuple[InfraJob, ...]:
    """``/runs/{id}/attempts/{n}/jobs``: the jobs that failed, timed out or
    were cancelled."""
    try:
        data = json.loads(body)
        out = []
        for j in data["jobs"]:
            conclusion = str(j.get("conclusion") or "")
            if conclusion not in ("failure", "cancelled", "timed_out"):
                continue
            steps = j.get("steps") or []
            failed = next((str(s.get("name") or "") for s in steps
                           if s.get("conclusion") in ("failure", "timed_out")), "")
            out.append(InfraJob(int(j.get("id") or 0), str(j.get("name") or ""), conclusion,
                                str(j.get("html_url") or ""), failed,
                                sum(1 for s in steps if s.get("conclusion"))))
        return tuple(out)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"bad jobs response: {e}") from e


def parse_annotations(body: str) -> list[str]:
    try:
        items = json.loads(body)
        return [str(a.get("message") or "") for a in items if isinstance(a, dict)]
    except (AttributeError, TypeError, ValueError):
        return []


def infra_reason(job: InfraJob, annotations: list[str] | None) -> str | None:
    """Why ``job`` failed for infrastructure reasons, or None: it has a
    failing step, it timed out, or nothing says so."""
    if job.failed_step or job.conclusion not in ("failure", "cancelled"):
        return None
    text = "\n".join(annotations or ()).lower()
    for marker, reason in MARKERS:
        if marker in text:
            return reason
    return NO_STEP if job.concluded == 0 else None


def is_eval(*names: str) -> bool:
    return any("eval" in n.lower() for n in names)


@dataclass
class Pending:
    """A re-run issued: the run reads as queued until ``attempt`` shows."""

    run_id: int
    attempt: int
    job: str
    at: float


class InfraReruns:
    """The hub's re-runs (one repo)."""

    def __init__(self, hub: ActionsHub) -> None:
        self.hub = hub
        # (run id, attempt, job name): tried once each.
        self.tried: set[tuple[int, int, str]] = set()
        # job id → its annotation messages
        self.annotations: dict[int, list[str]] = {}
        # run id → the re-run waiting for its new attempt
        self.pending: dict[int, Pending] = {}
        # Called after each re-run: (branch, run, job, reason, attempt).
        self.on_rerun: Callable[[str, WorkflowRun, InfraJob, str, int], None] = \
            lambda *a: None

    # --- What the rows read ---

    def expire(self, runs: list[WorkflowRun], now: float | None = None) -> None:
        now = self.hub.clock() if now is None else now
        seen = {r.id: r.attempt for r in runs}
        for run_id, p in list(self.pending.items()):
            if seen.get(run_id, 0) >= p.attempt or now - p.at >= PENDING_FOR:
                del self.pending[run_id]

    def queued(self, run: WorkflowRun) -> bool:
        p = self.pending.get(run.id)
        return p is not None and run.attempt < p.attempt \
            and self.hub.clock() - p.at < PENDING_FOR

    def checks(self) -> dict[int, frozenset[str]]:
        """run id → the job names re-run and not yet seen again."""
        now = self.hub.clock()
        return {p.run_id: frozenset({p.job}) for p in self.pending.values()
                if now - p.at < PENDING_FOR}

    # --- The poll's part ---

    async def run(self, red: list[tuple[str, WorkflowRun]], config: CiConfig) -> bool:
        """Re-run the infrastructure failures of ``red`` (branch, run) that
        the config allows; True when one was issued."""
        if not config.rerun_infra:
            return False
        health = self.hub.health
        if health is not None and not health.available:
            return False
        from lazyagent.engine.actions import HttpError
        from lazyagent.engine.ci_tests import check_id

        issued = False
        reads = 0
        for branch, run in red:
            if run.attempt >= config.rerun_infra_max_attempts or run.id in self.pending:
                continue
            jobs = self.hub._infra.get((run.id, run.attempt))
            if not jobs or any(j.failed_step or j.conclusion == "timed_out" for j in jobs):
                # Not read yet, or a real failure in the run.
                continue
            reasons: dict[str, str] = {}
            for job in jobs:
                if job.concluded and job.id and job.id not in self.annotations:
                    if reads >= MAX_ANNOTATION_FETCHES:
                        break
                    reads += 1
                    try:
                        _, _, body = await self.hub._api(
                            ANNOTATIONS_PATH.replace("{id}", str(job.id)))
                    except HttpError:
                        body = "[]"  # none to read: the steps alone decide
                    except (TrackerError, OSError) as e:
                        self.hub._failed(self.hub.clock(), e)
                        return issued
                    self.annotations[job.id] = parse_annotations(body)
                reason = infra_reason(job, self.annotations.get(job.id))
                if reason is not None:
                    reasons[job.name] = reason
            if len(reasons) != len(jobs):
                # A job of the run failed for another reason (or is not read).
                continue
            for job in jobs:
                cid = check_id(run, job.name)
                slot = (run.id, run.attempt, job.name)
                if slot in self.tried or not job.id:
                    continue
                if config.is_nondeterministic(cid, job.name) \
                        or is_eval(cid, job.name, run.workflow) \
                        or config.gate_of(cid, job.name) == "policy":
                    continue
                self.tried.add(slot)
                if await self._rerun(branch, run, job, reasons[job.name]):
                    issued = True
                # One job per run attempt: the re-run starts a new attempt.
                break
        return issued

    async def _rerun(self, branch: str, run: WorkflowRun, job: InfraJob, reason: str) -> bool:
        attempt = run.attempt + 1
        try:
            code, _, err = await self.hub._call(["run", "rerun", "--job", str(job.id)])
        except (TrackerError, OSError) as e:
            log.warning("could not re-run %s (run %s): %s", job.name, run.id, e)
            return False
        if code != 0:
            log.warning("could not re-run %s (run %s): %s", job.name, run.id,
                        " ".join(err.split())[:200] or f"gh exited {code}")
            return False
        log.info("re-ran %s on %s (%s), run %s attempt %d", job.name, branch, reason,
                 run.id, attempt)
        self.pending[run.id] = Pending(run.id, attempt, job.name, self.hub.clock())
        try:
            self.on_rerun(branch, run, job, reason, attempt)
        except Exception:  # a timeline write never stops the poll
            log.exception("infra re-run: on_rerun failed")
        return True


_RUN_ID = re.compile(r"/actions/runs/(\d+)")


def queued_checks(pr: PrInfo, rerun: dict[int, frozenset[str]]) -> PrInfo:
    """``pr`` with the failed checks of the re-run jobs (run id → job
    names) read as queued; ``pr`` itself when none is."""
    checks, hit = [], False
    for c in pr.checks:
        m = _RUN_ID.search(c.details_url)
        if m and c.name in rerun.get(int(m[1]), ()) \
                and c.conclusion.upper() in ("FAILURE", "CANCELLED"):
            c = replace(c, status="QUEUED", conclusion="")
            hit = True
        checks.append(c)
    return replace(pr, checks=checks) if hit else pr
