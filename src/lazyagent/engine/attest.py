"""Attestations for nondeterministic CI checks, mechanism B (proposal 0014
"Agent judgment for nondeterministic suites"; docs/design/rules-plan.md R5).

An eval suite whose pass rate varies run to run cannot be compared by test
ids (mechanism A, engine/ci_tests.py). An agent that has read the suite's
history on the base and run it on the branch head itself files an
**attestation** with ``ci_attest``: a structured claim, by an actor, with
its evidence, which the user's ``[ci]`` config may accept.

- **Which checks.** Only failing checks on the head that ``[ci]
  nondeterministic`` lists. Anything else is refused: deterministic checks
  have mechanism A.
- **Validation.** ``sha`` is the head; at least ``min_base_runs`` base runs
  and ``min_repetitions`` own runs; each ``run_id`` exists in the repo's
  Actions, is a run of that check's workflow with a job of that name, and
  ran on the base branch (base runs) or on the head (own runs); pass rates
  are counts with ``0 ≤ passed ≤ total``. The engine recomputes the range
  (the base's lowest and highest pass rate, the branch's) and, with
  ``method = "range"``, refuses an attestation whose lowest own rate is
  more than ``max_drop`` below the base's lowest. Local runs cannot be
  checked: they are recorded as the actor's claim (``where: "local"``).
- **Record** in the overlay (``attestations``): ``{id, key, branch, check,
  checks, name, sha, point, actor, at, evidence, state, confirm, ...}``,
  ``state`` one of ``pending · accepted · rejected · expired · revoked ·
  recorded``, and an ``attest`` timeline entry.
- **Acceptance** is the user's rule, per check: ``[ci]
  accept_attestation_for`` lets an accepted attestation put the check in
  ``ci.unrelated_checks``. A check not listed: ``recorded``, shown, no
  effect.
- **Confirmation** (Q-R12): per check ``confirm = "user"`` (default) makes
  it ``pending``: a Confirm item in Round (``attestation_confirm``); Accept
  records it, Reject sends the user's comment to the attesting agent and
  brings Fix CI back. ``confirm = "none"`` accepts at once, unless the
  method is not ``range`` (the engine could not check the comparison).
- **Expiry** (Q-R14): the head moves, the check's base point moves, or
  ``attestation_ttl`` (72 h) passes. The user may revoke one (Why?).
- **The orchestrator** attests only within its mandate: ``attest_ci``, in
  the away list (engine/orchestrator.py).

Untrusted text stays data: an attestation's reason is shown, never parsed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from lazyagent.engine import actions
from lazyagent.engine.ci_tests import CiConfig, CiTests, bare_job, check_id
from lazyagent.engine.overlay import one_line
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.trackers import TrackerError

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine
    from lazyagent.models import WorkflowRun

log = logging.getLogger(__name__)

STATES = ("pending", "accepted", "rejected", "expired", "revoked", "recorded")
# Still standing: shown, and expired when the head or base point moves.
ACTIVE = ("pending", "accepted", "recorded")
METHODS = ("range", "mean_ci", "other")
REASON_CHARS = 500
COMMENT_CHARS = 500
WHERE_CHARS = 40
# Runs one attestation may cite (each is checked against Actions).
MAX_RUNS = 20
# Records kept per work item (the newest; ended ones go first).
KEEP = 20
# Validated runs kept (run id → what Actions said).
RUN_CACHE = 256


def pct(passed: int, total: int) -> int:
    return round(100 * passed / total)


def _span(rates: Iterable[int]) -> str:
    rates = list(rates)
    lo, hi = min(rates), max(rates)
    return f"{lo} %" if lo == hi else f"{lo}-{hi} %"


def actor_text(record: Mapping[str, Any]) -> str:
    if record.get("actor") == "orchestrator":
        return "the orchestrator"
    return f"agent {record.get('agent_id') or '?'}"


def evidence_text(record: Mapping[str, Any]) -> str:
    """``base 5 runs 71-78 %, branch 3 runs 74-76 %`` (with ``1 local`` when
    some own runs are the actor's own word)."""
    ev = record.get("evidence") or {}
    base = [pct(r["passed"], r["total"]) for r in ev.get("base_runs", ())]
    own = [pct(r["passed"], r["total"]) for r in ev.get("own_runs", ())]
    if not base or not own:
        return ""
    local = sum(1 for r in ev.get("own_runs", ()) if r.get("run_id") is None)
    where = f" ({local} local)" if local else ""
    return (f"base {len(base)} runs {_span(base)}, branch {len(own)} runs{where} "
            f"{_span(own)}")


def range_text(record: Mapping[str, Any]) -> str:
    """``drop 0 points (max 5)``: the engine's recomputed comparison."""
    rng = (record.get("evidence") or {}).get("range") or {}
    if not rng:
        return ""
    drop = round(100 * rng["drop"])
    limit = round(100 * rng["max_drop"])
    verdict = "within" if rng.get("within") else "outside"
    return f"drop {drop} points vs max {limit}: {verdict} the base's range"


def public(record: Mapping[str, Any]) -> dict[str, Any]:
    """An attestation as clients see it (rules_explain, the Confirm item,
    ci_attest's answer)."""
    out = {k: record.get(k) for k in (
        "id", "key", "branch", "check", "name", "checks", "sha", "point", "compared", "actor",
        "agent_id", "at", "state", "confirm", "accepted_for", "confirmed_by", "decided_at",
        "comment", "ended", "expires_at")}
    out["evidence"] = dict(record.get("evidence") or {})
    out["actor_text"] = actor_text(record)
    out["text"] = evidence_text(record)
    out["range_text"] = range_text(record)
    return out


def _sha_matches(given: str, sha: str) -> bool:
    given = given.strip().lower()
    return len(given) >= 7 and sha.lower().startswith(given)


def _count(where: str, value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{where} must be a whole number ≥ 0")
    return value


def _rates(name: str, runs: Any, *, need_id: bool) -> list[dict[str, Any]]:
    if not isinstance(runs, list):
        raise ValueError(f"{name} must be a list of runs")
    if len(runs) > MAX_RUNS:
        raise ValueError(f"{name}: at most {MAX_RUNS} runs")
    out = []
    for i, r in enumerate(runs):
        where = f"{name}[{i}]"
        if not isinstance(r, dict):
            raise ValueError(f"{where} must be an object {{run_id, passed, total}}")
        passed = _count(f"{where}.passed", r.get("passed"))
        total = _count(f"{where}.total", r.get("total"))
        if total == 0 or passed > total:
            raise ValueError(f"{where}: passed must be from 0 to total, and total above 0")
        run_id = r.get("run_id")
        if run_id is not None and (type(run_id) is not int or run_id <= 0):
            raise ValueError(f"{where}.run_id must be an Actions run id (a number)")
        if need_id and run_id is None:
            raise ValueError(f"{where}: a base run needs its run_id (checked against Actions)")
        entry: dict[str, Any] = {"passed": passed, "total": total}
        if run_id is not None:
            entry["run_id"] = run_id
        for key in ("sha", "where"):
            if r.get(key) is not None:
                if not isinstance(r[key], str):
                    raise ValueError(f"{where}.{key} must be a string")
                entry[key] = one_line(r[key], WHERE_CHARS)
        if run_id is None:
            entry["where"] = entry.get("where") or "local"
        if r.get("at") is not None:
            if isinstance(r["at"], bool) or not isinstance(r["at"], (int, float)):
                raise ValueError(f"{where}.at must be a Unix time")
            entry["at"] = float(r["at"])
        out.append(entry)
    return out


class Attestations:
    """The engine's attestations (one store, the overlay's table)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # Bumped on every change: CiTestsHub.analysis memoises on it.
        self.version = 0
        # run id → (WorkflowRun, its jobs' names): what Actions said.
        self._runs: dict[int, tuple[WorkflowRun, frozenset[str]]] = {}

    @property
    def store(self) -> dict[str, dict[str, Any]]:
        return self._engine.overlay.attestations

    def changed(self) -> None:
        self.version += 1
        self._engine.overlay.changed()
        self._engine.status.mark_dirty()

    # --- What a pass reads ---

    def covered(self, branch: str) -> frozenset[str]:
        """The check ids active attestations on ``branch`` cover."""
        return frozenset(c for r in self.store.values()
                         if r.get("branch") == branch and r.get("state") in ACTIVE
                         for c in r.get("checks", ()))

    def apply(self, branch: str, head: str, tests: CiTests, config: CiConfig) -> CiTests:
        """``tests`` with each failing check an attestation speaks for:
        explained (accepted, the check accepted by config), waiting for the
        user (pending), or carrying why the latest one ended."""
        records = sorted((r for r in self.store.values() if r.get("branch") == branch),
                         key=lambda r: (r["at"], r["id"]))
        if not records:
            return tests
        now = self._engine.status.clock()
        checks = []
        changed = False
        for c in tests.checks:
            mine = [r for r in records if c.check in r.get("checks", ())]
            if not mine or c.unrelated:
                checks.append(c)
                continue
            latest = mine[-1]
            state = latest["state"]
            if state in ACTIVE and (latest["sha"] != head or now >= latest["expires_at"]):
                # check() expires it on this pass; read it as expired now.
                state = "expired"
            view = public(latest)
            if state == "accepted" and latest.get("accepted_for") \
                    and config.accepts(c.check, c.job):
                confirmed = {"user": "confirmed by you",
                             "rule": "no confirmation asked"}.get(latest.get("confirmed_by"), "")
                detail = (f"attested by {actor_text(latest)} at {latest['sha'][:7]}: "
                          f"{evidence_text(latest)}" + (f"; {confirmed}" if confirmed else ""))
                c = replace(c, unrelated=True, mechanism="B", attestation=view, detail=detail)
            elif state == "pending":
                c = replace(c, attestation=view, attest_pending=True,
                            detail=f"{actor_text(latest)} says it is unrelated: waiting for "
                                   "your confirmation")
            elif state == "recorded":
                c = replace(c, attestation=view,
                            detail=f"attested by {actor_text(latest)}, but [ci] "
                                   "accept_attestation_for does not list it")
            else:
                # The row's state has five words: "CI red: eval-suite
                # (attestation expired)"; the detail says why.
                why = latest.get("ended") or ("head moved" if latest["sha"] != head
                                              else f"older than {_hours(latest)}")
                c = replace(c, attest_note=f"attestation {state}",
                            detail=f"attestation {latest['id']} {state}: {why}")
            changed = True
            checks.append(c)
        return replace(tests, checks=tuple(checks)) if changed else tests

    def of_key(self, key: str) -> list[dict[str, Any]]:
        """Every attestation on ``key``, newest first, as clients see them."""
        return [public(r) for r in sorted(
            (r for r in self.store.values() if r.get("key") == key),
            key=lambda r: (r["at"], r["id"]), reverse=True)]

    # --- Expiry, on each status pass ---

    def check(self, now: float) -> None:
        engine = self._engine
        hub = getattr(engine, "actions", None)
        for r in list(self.store.values()):
            if r.get("state") not in ACTIVE:
                continue
            path = engine.status.path_of(r["key"])
            if path is None:
                continue
            if now >= r["expires_at"]:
                self._end(r, "expired", f"older than {_hours(r)}", now)
                continue
            pr = engine.get_pr_info(path)
            if pr is not None and pr.state.upper() != "OPEN":
                continue
            head = pr.head_sha if pr is not None and pr.head_sha else (
                hub.head(r["branch"]) if hub is not None else None)
            if head and head != r["sha"]:
                self._end(r, "expired", "head moved", now)
                continue
            if hub is None or not r.get("point"):
                continue
            v = actions.ci_verdict(engine, r["branch"], pr)
            run = _run_of(v, r["checks"]) if v is not None else None
            target = hub.tests.targets.get(r["branch"])
            if run is None or target is None:
                continue
            point = hub.tests.point_of(run, target)
            if point is not None and point != r["point"]:
                self._end(r, "expired", "base point moved", now)

    def _end(self, r: dict[str, Any], state: str, why: str, now: float, *,
             actor: str = "engine") -> None:
        r["state"] = state
        r["ended"] = why
        r["decided_at"] = now
        self._engine.overlay.add(r["key"], "attest", actor,
                                 f"attestation {r['id']} for {r['name']} {state}: {why}",
                                 ref=r["id"], at=now)
        self.changed()

    # --- The user's ---

    def _get(self, attestation_id: Any) -> dict[str, Any]:
        if not isinstance(attestation_id, str) or not attestation_id:
            raise ValueError("id is required: an attestation id (e.g. att3)")
        r = self.store.get(attestation_id)
        if r is None:
            raise ValueError(f"No attestation {attestation_id!r}")
        return r

    def confirm(self, attestation_id: Any, accept: Any, comment: Any) -> dict[str, Any]:
        """Accept or reject a pending attestation (the Confirm item)."""
        r = self._get(attestation_id)
        if type(accept) is not bool:
            raise ValueError("accept must be true (Accept) or false (Reject)")
        if comment is not None and not isinstance(comment, str):
            raise ValueError("comment must be a string")
        if r["state"] != "pending":
            raise ValueError(f"Attestation {r['id']} is {r['state']}: nothing to confirm")
        now = self._engine.status.clock()
        comment = one_line(comment or "", COMMENT_CHARS)
        if accept:
            r.update(state="accepted", confirmed_by="user", decided_at=now)
            if comment:
                r["comment"] = comment
            self._engine.overlay.add(r["key"], "attest", "user",
                                     f"attestation {r['id']} for {r['name']} accepted",
                                     ref=r["id"], at=now)
            self.changed()
            return public(r)
        r["comment"] = comment
        self._end(r, "rejected", "rejected by the user", now, actor="user")
        self._tell(r, f"Attestation {r['id']} for {r['name']} rejected by the user: "
                      f"{comment or '(no comment)'}")
        return public(r)

    def revoke(self, attestation_id: Any) -> dict[str, Any]:
        """Take back a standing attestation (Why?): the check is related
        again."""
        r = self._get(attestation_id)
        if r["state"] not in ACTIVE:
            raise ValueError(f"Attestation {r['id']} is {r['state']}: nothing to revoke")
        self._end(r, "revoked", "revoked by the user", self._engine.status.clock(),
                  actor="user")
        return public(r)

    def _tell(self, r: Mapping[str, Any], text: str) -> None:
        engine = self._engine
        path = ORCHESTRATOR_KEY if r["actor"] == "orchestrator" else r.get("path")
        agent_id = r.get("agent_id")
        if not path or not agent_id:
            return
        mailbox = engine.first_responder.mailbox
        mailbox.post((path, agent_id), text, tag=f"attest:{r['id']}")
        mailbox.flush()

    # --- Filing one (ci_attest) ---

    async def attest(self, params: dict, *, key: str, actor: str, path: str,
                     agent_id: str) -> dict[str, Any]:
        engine = self._engine
        hub = getattr(engine, "actions", None)
        if hub is None or not hub.enabled:
            raise ValueError("GitHub Actions is not read for this repo: nothing to attest")
        wt_path = engine.status.path_of(key)
        wt = engine.worktree(wt_path) if wt_path is not None else None
        if wt is None or not wt.branch:
            raise ValueError(f"No work item {key!r}")
        config: CiConfig = engine.status.catalog.ci
        name = params.get("check")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("check is required: the failing check's name (e.g. eval-suite)")
        name = name.strip()
        if params.get("verdict", "unrelated") != "unrelated":
            raise ValueError('verdict must be "unrelated" (the only verdict an attestation has)')
        reason = params.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required: why the failure is not the branch's, in a "
                             "sentence")
        method = params.get("method", "range")
        if method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}")
        pr = engine.get_pr_info(wt.path)
        v = actions.ci_verdict(engine, wt.branch, pr)
        if v is None or v.state != "red":
            raise ValueError(f"CI is not red on {wt.branch}'s head: nothing to attest")
        sha = params.get("sha")
        if not isinstance(sha, str) or not _sha_matches(sha, v.head):
            raise ValueError(f"sha must be the head, {v.head[:7]} (at least 7 characters): an "
                             "attestation is for the commit CI ran on")
        failing = [(run, j.name) for run in v.red for j in run.failed_jobs or ()]
        mine = [(run, job) for run, job in failing
                if name.lower() in (check_id(run, job).lower(), job.lower(),
                                    bare_job(job).lower())]
        listed = config.is_nondeterministic(name, name) or any(
            config.is_nondeterministic(check_id(run, job), job) for run, job in mine)
        if not listed:
            raise ValueError(
                f"{name} is not listed in [ci] nondeterministic: attestations are only for "
                "checks whose pass rate varies run to run (mechanism A compares the others)")
        if not mine:
            unread = [r for r in v.red if r.failed_jobs is None]
            if unread:
                raise ValueError(f"The failed jobs of run {unread[0].id} are not read yet: "
                                 "call ci_attest again in a minute")
            raise ValueError(f"{name} is not failing on {wt.branch}'s head {v.head[:7]}")
        if not all(config.is_nondeterministic(check_id(run, job), job) for run, job in mine):
            raise ValueError(f"{name} names checks [ci] nondeterministic does not list")
        run0, job0 = mine[0]
        rules = config.attest_rules(check_id(run0, job0), job0)
        base_runs = _rates("base_runs", params.get("base_runs"), need_id=True)
        own_runs = _rates("own_runs", params.get("own_runs"), need_id=False)
        if len(base_runs) < rules.min_base_runs:
            raise ValueError(f"base_runs: {len(base_runs)} given, {rules.min_base_runs} needed "
                             "(the suite's recent runs on the base branch)")
        if len(own_runs) < rules.min_repetitions:
            raise ValueError(f"own_runs: {len(own_runs)} given, {rules.min_repetitions} needed "
                             f"(runs of the suite on the head {v.head[:7]})")
        ids = [r["run_id"] for r in base_runs + own_runs if "run_id" in r]
        if len(ids) != len(set(ids)):
            raise ValueError("a run id is cited twice")
        target = hub.tests.targets.get(wt.branch) or engine.status._target(wt)
        for r in base_runs:
            run, jobs = await self._run(hub, r["run_id"])
            self._same_check(r["run_id"], run, jobs, run0, job0)
            if run.head_branch != target.base:
                raise ValueError(f"base run {r['run_id']} ran on {run.head_branch or '?'}, not "
                                 f"on the base branch {target.base}")
            if r.get("sha") and not _sha_matches(r["sha"], run.head_sha):
                raise ValueError(f"base run {r['run_id']} ran at {run.head_sha[:7]}, not "
                                 f"{r['sha']}")
            r["sha"] = run.head_sha
        for r in own_runs:
            if "run_id" not in r:
                continue
            run, jobs = await self._run(hub, r["run_id"])
            self._same_check(r["run_id"], run, jobs, run0, job0)
            if run.head_sha != v.head:
                raise ValueError(f"own run {r['run_id']} ran at {run.head_sha[:7]}, not on the "
                                 f"head {v.head[:7]}")
        rng = _range(base_runs, own_runs, rules.max_drop)
        if method == "range" and not rng["within"]:
            raise ValueError(
                f"The runs contradict the claim: the branch's lowest pass rate "
                f"{round(100 * rng['own_min'])} % is {round(100 * rng['drop'])} points below "
                f"the base's lowest {round(100 * rng['base_min'])} % (max_drop "
                f"{round(100 * rules.max_drop)} points). Fix the regression, or explain a "
                "better comparison with method \"other\"")
        point, compared, _ = await hub.tests.point_for(run0, target)
        now = engine.status.clock()
        overlay = engine.overlay
        att_id = f"att{overlay.next_attestation}"
        overlay.next_attestation += 1
        accepted_for = all(config.accepts(check_id(run, job), job) for run, job in mine)
        confirm = rules.confirm if method == "range" else "user"
        if not accepted_for:
            state = "recorded"
        elif confirm == "none":
            state = "accepted"
        else:
            state = "pending"
        checks = sorted({check_id(run, job) for run, job in mine})
        record: dict[str, Any] = {
            "id": att_id, "key": key, "branch": wt.branch, "path": path, "check": name,
            "name": bare_job(job0), "checks": checks, "sha": v.head, "point": point,
            "compared": compared, "actor": actor, "agent_id": agent_id, "at": now,
            "expires_at": now + config.attestation_ttl, "state": state, "confirm": confirm,
            "accepted_for": accepted_for,
            "evidence": {"verdict": "unrelated", "reason": one_line(reason, REASON_CHARS),
                         "method": method, "base_runs": base_runs, "own_runs": own_runs,
                         "range": rng},
        }
        if state == "accepted":
            record["confirmed_by"] = "rule"
            record["decided_at"] = now
        for old in self.store.values():
            if old["key"] == key and old["state"] in ACTIVE and set(old["checks"]) & set(checks):
                self._end(old, "expired", f"replaced by {att_id}", now)
        self.store[att_id] = record
        self._prune(key)
        overlay.add(key, "attest", actor if actor == "orchestrator" else f"agent:{agent_id}",
                    f"{record['name']} attested by {actor_text(record)} at {v.head[:7]}: "
                    f"{evidence_text(record)} ({state})", ref=att_id, at=now)
        self.changed()
        out = public(record)
        out["message"] = {
            "pending": "Recorded; the user is asked to confirm it (a Confirm item in their "
                       "inbox). Until then the check counts as related.",
            "accepted": "Recorded and accepted ([ci] confirm = \"none\"): the check no longer "
                        "counts against the branch.",
            "recorded": "Recorded and shown, but [ci] accept_attestation_for does not list "
                        "this check: it still counts as related.",
        }[state]
        return out

    def _prune(self, key: str) -> None:
        mine = sorted((r for r in self.store.values() if r["key"] == key),
                      key=lambda r: (r["state"] in ACTIVE, r["at"]))
        for r in mine[:max(0, len(mine) - KEEP)]:
            del self.store[r["id"]]

    async def _run(self, hub: Any, run_id: int) -> tuple[WorkflowRun, frozenset[str]]:
        """What Actions says of ``run_id``: the run and its jobs' names."""
        got = self._runs.get(run_id)
        if got is not None:
            return got
        try:
            run = next((r for r in hub.all_runs() if r.id == run_id), None)
            if run is None:
                _, _, body = await hub._api(f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}")
                data = json.loads(body)
                run = actions.parse_runs(json.dumps({"workflow_runs": [data]}))[0]
            _, _, body = await hub._api(
                f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100")
            jobs = frozenset(str(j.get("name") or "") for j in json.loads(body)["jobs"])
        except TrackerError as e:
            if "HTTP 404" in str(e):
                raise ValueError(f"Run {run_id} does not exist in this repo's Actions") from e
            raise ValueError(f"Run {run_id} could not be checked ({e}): call ci_attest again "
                             "in a minute") from e
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
            raise ValueError(f"Run {run_id}: a bad answer from Actions") from e
        if len(self._runs) >= RUN_CACHE:
            self._runs.pop(next(iter(self._runs)))
        self._runs[run_id] = (run, jobs)
        return run, jobs

    @staticmethod
    def _same_check(run_id: int, run: WorkflowRun, jobs: frozenset[str],
                    failing: WorkflowRun, job: str) -> None:
        from lazyagent.engine.ci_tests import _same_workflow

        if not _same_workflow(run, failing):
            raise ValueError(f"Run {run_id} is a run of {run.path or run.workflow}, not of "
                             f"{failing.path or failing.workflow}")
        if job not in jobs and bare_job(job) not in {bare_job(j) for j in jobs}:
            raise ValueError(f"Run {run_id} has no {bare_job(job)} job")
        if run.status != "completed":
            raise ValueError(f"Run {run_id} has not finished")


def _range(base: list[dict[str, Any]], own: list[dict[str, Any]],
           max_drop: float) -> dict[str, Any]:
    b = [r["passed"] / r["total"] for r in base]
    o = [r["passed"] / r["total"] for r in own]
    drop = max(0.0, min(b) - min(o))
    return {"base_min": min(b), "base_max": max(b), "own_min": min(o), "own_max": max(o),
            "drop": drop, "max_drop": max_drop, "within": drop <= max_drop + 1e-9}


def _hours(r: Mapping[str, Any]) -> str:
    hours = round((r["expires_at"] - r["at"]) / 3600)
    return f"{hours} h"


def _run_of(v: Any, checks: Iterable[str]) -> WorkflowRun | None:
    wanted = set(checks)
    return next((run for run in v.red
                 if any(check_id(run, j.name) in wanted for j in run.failed_jobs or ())), None)


__all__ = ["ACTIVE", "Attestations", "STATES", "actor_text", "evidence_text", "public"]
