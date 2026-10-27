"""Opt-in auto-delegation of stalled items (proposal 0013, inbox-plan.md §7,
Q-B33).

Off by default. ``[inbox] auto_delegate`` in ``.lazyagent.toml`` lists the
kinds it acts on; ``inbox_config_set`` writes the same keys (the file stays
the source of truth). When a stalled item of a listed kind is in the inbox,
the engine does what the manual Delegate key does, with no model deciding:

- target ``owner`` (default): the branch's owner agent gets a fixed brief.
  A live one when idle (a busy one once its turn is over); a dead one is
  restored with its notes first; only with no owner at all is one spawned
  with the brief, within the spawn caps.
- target ``orchestrator``: a claim (Pass to orchestrator, claims.py), as the
  gesture makes, while an orchestrator runs.

Limits, as code: at most :data:`MAX_ATTEMPTS` attempts per item while its
condition lasts (the "fingerprint": the item open, gone while an agent
works on it, back when it stops; it resets once the condition clears),
``[inbox] auto_delegate_max_per_day`` (10) a day, never on the default or a
protected branch, never on a teammate's branch (the branch's last commit is
by someone else). After two failed attempts the item becomes an engine
brief in To sit down with (what was tried, the agent's last lines).

Kinds: ``conflict_with_base`` (the inbox's ``conflict``), ``agent_dead``
(``exited``) and ``ci_red`` (``ci`` and ``ci_failed``: a fresh red run on
the branch's head, engine/actions.py, with no agent at work). A CI brief
carries the failed-log summary, as the Delegate key's line does.

**The skip list.** ``[inbox] auto_delegate_skip_checks`` names failing
checks no agent can fix (a nondeterministic eval, a label gate): check,
workflow or job names, fnmatch globs, compared without case. A red whose
failing checks are all on it is not delegated: the item stays the user's,
and the row says ``auto-delegation skipped: <check> is on the skip list``
(``auto_delegate`` on the row, and in Why?). With other failing checks too,
it is delegated, and the brief names the ones to leave alone. A red Actions
run is one check per failed job (its job or its workflow name may match),
or its workflow while its jobs are unknown; a failing PR check that is not
an Actions run counts by its name, and so does every failing PR check when
Actions has no red run on the head.

**Nondeterministic checks** (``[ci] nondeterministic`` in rules.toml,
proposal 0014 R5). A red check listed there and *not* on the skip list is
delegated like any other, but its brief asks the agent to investigate, not
to "fix the test": read the suite's recent runs on the base, run it on the
head itself (through the user's slot or lock for that suite, when its
instructions define one), then fix a real regression or file ``ci_attest``
with the evidence. A check on both lists is skipped: the skip list wins,
the item stays the user's. To let agents attest a suite, take it off the
skip list.

**A failed attempt says why.** Each attempt's line in the final brief says
what the agent did: whether it committed (the worktree's head before and
after), its last ``status_report`` line, and the last bullets of its notes'
Pending and Doubts.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from lazyagent.agent_providers import resolve_permission_mode
from lazyagent.engine import actions
from lazyagent.engine.delegation import CI_KINDS, SUMMARY_WAIT, with_summary
from lazyagent.engine.orchestrator import _git

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

# Config name → the inbox kinds it acts on.
KINDS: dict[str, tuple[str, ...]] = {
    "conflict_with_base": ("conflict",),
    "agent_dead": ("exited",),
    "ci_red": ("ci", "ci_failed"),
}
TARGETS = ("owner", "orchestrator")
MAX_ATTEMPTS = 2
PROTECTED = ("main", "master")
_RUNNING = ("running", "starting")
_WHAT = {
    "conflict": "a merge conflict with its base",
    "exited": "an agent that exited",
    "ci": "red CI on its head",
    "ci_failed": "red CI on its head",
}


# Notes sections whose last bullets say where a failed attempt stopped.
NOTES_SECTIONS = ("Pending", "Doubts")
NOTES_BULLETS = 2


def brief_text(branch: str, kind: str, state: str, attempt: int,
               leave: list[str] | None = None,
               nondeterministic: list[str] | None = None) -> str:
    text = (
        f"lazyagent auto-delegation (attempt {attempt} of {MAX_ATTEMPTS}): branch "
        f"{branch} has {_WHAT.get(kind, kind)} ({state}). Fix it on this branch, then "
        "end your turn. Do not merge it, and ask with request_decision if it needs "
        "a choice."
    )
    if leave:
        text += (
            f" Leave the failing {_names(leave)} alone: "
            f"{'it is' if len(leave) == 1 else 'they are'} on the user's skip list "
            "([inbox] auto_delegate_skip_checks), not yours to fix."
        )
    if nondeterministic:
        one = len(nondeterministic) == 1
        text += (
            f" {_names(nondeterministic)} {'is' if one else 'are'} nondeterministic (the pass "
            "rate varies run to run; the user's [ci] nondeterministic): do not edit the test "
            "to make it pass. Investigate: read its recent runs on the base branch (at least "
            "5, with their pass rates) and run it on this head yourself (at least 3 times; if "
            "your instructions define a slot or lock for running this suite, request it "
            "before each run and release it after). If your runs are clearly worse than the "
            "base's, fix the regression; otherwise file ci_attest with that evidence."
        )
    return text


def nondeterministic_checks(engine: Any, key: str, names: list[str]) -> list[str]:
    """Of ``names`` (failing checks as :func:`failing_checks` shows them),
    those ``[ci] nondeterministic`` lists, by their job's name."""
    from lazyagent.engine.ci_tests import bare_job, check_id

    config = engine.status.catalog.ci
    if not config.nondeterministic:
        return []
    try:
        _, runs = actions.red_runs(engine, key)
    except ValueError:
        return []
    out: list[str] = []
    for run in runs:
        for job in run.failed_jobs or ():
            if job.name in names and config.is_nondeterministic(check_id(run, job.name), job.name) \
                    and bare_job(job.name) not in out:
                out.append(bare_job(job.name))
    return out


def _names(names: list[str]) -> str:
    return ", ".join(names)


def skipped_text(names: list[str]) -> str:
    """``auto-delegation skipped: eval-suite is on the skip list``."""
    verb = "is" if len(names) == 1 else "are"
    return f"auto-delegation skipped: {_names(names)} {verb} on the skip list"


def failing_checks(engine: Any, key: str) -> list[tuple[str, tuple[str, ...]]]:
    """A CI item's failing checks: ``(name shown, names it matches by)``;
    see "The skip list" in the module docstring."""
    try:
        _, runs = actions.red_runs(engine, key)
    except ValueError:
        return []
    out: list[tuple[str, tuple[str, ...]]] = []
    for run in runs:
        jobs = run.failed_jobs or ()
        if jobs:
            out += [(j.name, (j.name, run.workflow)) for j in jobs]
        else:
            out.append((run.workflow, (run.workflow,)))
    path = engine.status.path_of(key)
    pr = engine.get_pr_info(path) if path is not None else None
    if pr is not None and pr.state.upper() == "OPEN":
        if runs:
            names = [c["name"] for c in actions.other_checks(pr)]
        else:
            names = [c.name for c in pr.checks
                     if c.conclusion.upper() in ("FAILURE", "ERROR", "TIMED_OUT")]
        out += [(n, (n,)) for n in names]
    return list({shown: (shown, names) for shown, names in out}.values())


def split_skipped(
    checks: list[tuple[str, tuple[str, ...]]], patterns: list[str]
) -> tuple[list[str], list[str]]:
    """The failing checks on the skip list (by the name that matched) and
    the rest."""
    skipped: list[str] = []
    rest: list[str] = []
    folded = [p.casefold() for p in patterns]
    for shown, names in checks:
        hit = next((n for n in names
                    if any(fnmatch.fnmatchcase(n.casefold(), p) for p in folded)), None)
        if hit is None:
            rest.append(shown)
        elif hit not in skipped:
            skipped.append(hit)
    return skipped, rest


class AutoDelegate:
    """The engine's auto-delegation; see the module docstring."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # (key, kind) → {count, in_flight, agent, tries: [text], briefed}
        self.attempts: dict[tuple[str, str], dict[str, Any]] = {}
        self._day = ""
        self.today = 0
        self._busy: set[tuple[str, str]] = set()
        # key → "auto-delegation skipped: …" (the skip list), last pass.
        self.skipped: dict[str, str] = {}
        # Whether a branch is a teammate's (tests replace it).
        self.teammate: Callable[[str, str], Awaitable[bool]] = _teammate

    def check(self, rows: dict[str, dict[str, Any]], items: list[dict[str, Any]], now: float) -> None:
        config = self._engine.config.inbox
        if not config.auto_delegate:
            self.attempts.clear()
            self.skipped = {}
            return
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self._day:
            self._day, self.today = day, 0
        wanted = {kind for name in config.auto_delegate for kind in KINDS.get(name, ())}
        open_items = {
            (i["work_item"], i["kind"]): i
            for i in items
            if i["kind"] in wanted and i["work_item"] is not None
            # The catalog may say an agent cannot resolve it (I4h).
            and i.get("delegable", True)
        }
        for slot, attempt in list(self.attempts.items()):
            if slot in open_items:
                if attempt["in_flight"] and attempt.get("left") and slot not in self._busy:
                    # Back after the agent's go: that attempt failed.
                    attempt["in_flight"] = attempt["left"] = False
                continue
            if attempt["in_flight"]:
                # The agent is at it (or the condition cleared).
                attempt["left"] = True
            row = rows.get(slot[0])
            running = row is not None and any(a["status"] in _RUNNING for a in row["agents"])
            if row is None or not running:
                # The condition cleared (or the item went away): reset.
                if slot not in self._busy:
                    del self.attempts[slot]
        skipped: dict[str, str] = {}
        for slot, item in open_items.items():
            attempt = self.attempts.setdefault(
                slot, {"count": 0, "in_flight": False, "agent": None, "tries": [], "briefed": False}
            )
            row = rows.get(slot[0])
            if slot[1] in CI_KINDS:
                checks = failing_checks(self._engine, slot[0])
                leave, rest = split_skipped(checks, config.auto_delegate_skip_checks or [])
                if leave and not rest:
                    skipped[slot[0]] = skipped_text(leave)
                    if row is not None:
                        row["auto_delegate"] = skipped[slot[0]]
                    continue
                attempt["leave"] = leave
                # The skip list wins over [ci] nondeterministic.
                attempt["nondeterministic"] = nondeterministic_checks(self._engine, slot[0], rest)
            if attempt["in_flight"] or slot in self._busy or attempt["briefed"] or attempt.get("skip"):
                continue
            if row is None or row["worktree_path"] is None:
                continue
            if attempt["count"] >= MAX_ATTEMPTS:
                attempt["briefed"] = True
                self._busy.add(slot)
                self._spawn(self._brief(slot, row, item, attempt, now))
                continue
            if config.auto_delegate_max_per_day and self.today >= config.auto_delegate_max_per_day:
                continue
            if self._protected(row):
                continue
            self._busy.add(slot)
            self._spawn(self._attempt(slot, row, item, attempt))
        self.skipped = skipped

    def _protected(self, row: dict[str, Any]) -> bool:
        wt = self._engine.worktree(row["worktree_path"])
        branch = row["branch"]
        if wt is None or wt.is_main or not branch:
            return True
        return branch in (self._engine.config.default_branch, *PROTECTED)

    def _spawn(self, coro: Any) -> None:
        try:
            self._engine._spawn(coro)
        except RuntimeError:  # no loop: never in the engine
            coro.close()

    async def _attempt(
        self, slot: tuple[str, str], row: dict[str, Any], item: dict[str, Any],
        attempt: dict[str, Any],
    ) -> None:
        engine = self._engine
        key, kind = slot
        path = row["worktree_path"]
        try:
            if await self.teammate(path, row["branch"]):
                attempt["skip"] = True  # never: leave it to the user
                return
            await self._outcome(key, path, attempt)
            n = attempt["count"] + 1
            text = brief_text(row["branch"], kind, item["state"], n, attempt.get("leave"),
                              attempt.get("nondeterministic"))
            if kind in CI_KINDS:
                text = await self._ci_summary(key, text)
            target = engine.config.inbox.auto_delegate_target
            if target == "orchestrator":
                if engine.claims.get(key) is not None:
                    return
                engine.claims.claim(key, by="engine")
                done = "passed to the orchestrator"
            else:
                done = await self._to_owner(path, text, attempt)
            attempt["count"] = n
            attempt["in_flight"] = True
            attempt["tries"].append(f"{time.strftime('%H:%M')} {done}")
            attempt["sent"] = {"head": await engine.head_sha(path), "at": engine.status.clock()}
            self.today += 1
            engine.overlay.add(key, "delegate", "engine",
                               f"auto-delegated {kind} (attempt {n}): {done}")
            engine.status.mark_dirty()
        except (ValueError, OSError) as e:
            log.warning("auto-delegation of %s on %s failed: %s", kind, key, e)
            attempt["count"] += 1
            attempt["tries"].append(f"{time.strftime('%H:%M')} could not delegate: {e}")
        finally:
            self._busy.discard(slot)

    async def _ci_summary(self, key: str, text: str) -> str:
        engine = self._engine
        try:
            runs = actions.red_runs(engine, key)[1]
        except ValueError:
            return text
        if not runs:
            return text
        try:
            summary = await asyncio.wait_for(engine.actions.summary(runs), SUMMARY_WAIT)
        except (asyncio.TimeoutError, OSError, ValueError):
            summary = None
        return with_summary(text, summary, runs)

    async def _to_owner(self, path: str, text: str, attempt: dict[str, Any]) -> str:
        engine = self._engine
        live = engine.agent_ids(path)
        if live:
            agent_id = live[0]
            engine.first_responder.mailbox.post((path, agent_id), text)
            engine.first_responder.mailbox.flush()
            attempt["agent"] = agent_id
            return f"sent to agent {agent_id}"
        manifest = engine.manifest
        entries = [e for e in (manifest.restorable.values() if manifest is not None else ())
                   if e.worktree_path == path]
        if entries:
            result = await engine.restore_agents([entries[-1].id])
            restored = result.get("restored") or []
            if restored:
                agent_id = restored[0].get("agent_id", "")
                engine.first_responder.mailbox.post((path, agent_id), text)
                attempt["agent"] = agent_id
                return f"restored agent {agent_id} with the brief"
        caps = engine.spawn_caps
        reserved = caps.reserve(engine)
        try:
            info = await engine.spawn_agent(path, text, permission_mode=self.permissions(path))
        except BaseException:
            caps.release(reserved)
            raise
        caps.done()
        attempt["agent"] = info.agent_id
        return f"spawned agent {info.agent_id} with the brief"

    async def _outcome(self, key: str, path: str, attempt: dict[str, Any]) -> None:
        """What the agent did on the last attempt, after its line in
        ``tries``: a commit or none, its report, its notes."""
        sent = attempt.pop("sent", None)
        if sent is None or not attempt["tries"]:
            return
        engine = self._engine
        head = await engine.head_sha(path)
        said: list[str] = []
        if sent["head"] and head:
            said.append("no commit was made" if head == sent["head"]
                        else f"it committed (head now {head[:7]})")
        agent_id = attempt.get("agent") or ""
        report = engine.overlay.reports.get(key)
        if report is not None and report.get("agent") == agent_id \
                and report.get("at", 0) >= sent["at"] and report.get("note"):
            said.append(f'its last report: "{report["note"]}"')
        notes = self._notes(path, agent_id)
        if notes:
            said.append("its notes: " + "; ".join(notes))
        if said:
            attempt["tries"][-1] += " · " + " · ".join(said)

    def _notes(self, path: str, agent_id: str) -> list[str]:
        """The last bullets of the agent's notes' Pending and Doubts."""
        engine = self._engine
        store = engine.notes
        if store is None or not agent_id:
            return []
        notes_id = engine.notes_id(path, agent_id)
        if notes_id is None or notes_id not in store:
            return []
        out: list[str] = []
        for name in NOTES_SECTIONS:
            bullets = store.section_bullets(notes_id, name)[-NOTES_BULLETS:]
            out += [f"{name}: {b}" for b in bullets]
        return out

    def permissions(self, path: str) -> str:
        """The permission mode of an agent spawned in ``path``: the mode of
        the worktree's most recent agent on record (running or restorable),
        else bypass with ``[inbox] auto_delegate_skip_permissions``, else
        ``[agents] permission_mode`` (cli when not set, or bypass)."""
        engine = self._engine
        recorded = engine.recorded_permission_mode(path)
        if recorded is not None:
            return recorded
        return resolve_permission_mode(
            None,
            engine.config.inbox.auto_delegate_skip_permissions,
            engine.config.agent.permission_mode,
        )

    async def _brief(self, slot: tuple[str, str], row: dict[str, Any], item: dict[str, Any],
                     attempt: dict[str, Any], now: float) -> None:
        try:
            await self._outcome(slot[0], row["worktree_path"], attempt)
            self._write_brief(row, item, attempt, now)
        finally:
            self._busy.discard(slot)

    def _write_brief(self, row: dict[str, Any], item: dict[str, Any], attempt: dict[str, Any],
                     now: float) -> None:
        engine = self._engine
        lines: list[str] = []
        agent_id = attempt.get("agent") or ""
        if agent_id and agent_id in engine.agent_ids(row["worktree_path"]):
            try:
                lines, _ = engine.read_agent_output(row["worktree_path"], agent_id, 8)
            except ValueError:
                lines = []
        tried = "\n".join(attempt["tries"])
        if lines:
            tried += "\nlast lines:\n" + "\n".join(lines)
        engine.decisions.engine_brief(
            key=row["key"],
            worktree_path=row["worktree_path"],
            agent_id=agent_id,
            question=f"Auto-delegation failed twice: {item['state']} on {row['branch']}",
            tried=tried,
            now=now,
        )


async def _teammate(path: str, branch: str) -> bool:
    """Whether the branch's last commit is by someone other than the user
    (``git config user.email``); unknown counts as the user's."""
    mine = await _git(path, "config", "user.email")
    theirs = await _git(path, "log", "-1", "--format=%ae", branch)
    return bool(mine and theirs and mine.casefold() != theirs.casefold())
