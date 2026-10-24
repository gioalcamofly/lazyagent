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
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

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


def brief_text(branch: str, kind: str, state: str, attempt: int) -> str:
    return (
        f"lazyagent auto-delegation (attempt {attempt} of {MAX_ATTEMPTS}): branch "
        f"{branch} has {_WHAT.get(kind, kind)} ({state}). Fix it on this branch, then "
        "end your turn. Do not merge it, and ask with request_decision if it needs "
        "a choice."
    )


class AutoDelegate:
    """The engine's auto-delegation; see the module docstring."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # (key, kind) → {count, in_flight, agent, tries: [text], briefed}
        self.attempts: dict[tuple[str, str], dict[str, Any]] = {}
        self._day = ""
        self.today = 0
        self._busy: set[tuple[str, str]] = set()
        # Whether a branch is a teammate's (tests replace it).
        self.teammate: Callable[[str, str], Awaitable[bool]] = _teammate

    def check(self, rows: dict[str, dict[str, Any]], items: list[dict[str, Any]], now: float) -> None:
        config = self._engine.config.inbox
        if not config.auto_delegate:
            self.attempts.clear()
            return
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self._day:
            self._day, self.today = day, 0
        wanted = {kind for name in config.auto_delegate for kind in KINDS.get(name, ())}
        open_items = {
            (i["work_item"], i["kind"]): i
            for i in items
            if i["kind"] in wanted and i["work_item"] is not None
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
        for slot, item in open_items.items():
            attempt = self.attempts.setdefault(
                slot, {"count": 0, "in_flight": False, "agent": None, "tries": [], "briefed": False}
            )
            if attempt["in_flight"] or slot in self._busy or attempt["briefed"] or attempt.get("skip"):
                continue
            row = rows.get(slot[0])
            if row is None or row["worktree_path"] is None:
                continue
            if attempt["count"] >= MAX_ATTEMPTS:
                self._brief(row, item, attempt, now)
                continue
            if config.auto_delegate_max_per_day and self.today >= config.auto_delegate_max_per_day:
                continue
            if self._protected(row):
                continue
            self._busy.add(slot)
            self._spawn(self._attempt(slot, row, item, attempt))

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
            n = attempt["count"] + 1
            text = brief_text(row["branch"], kind, item["state"], n)
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
            info = await engine.spawn_agent(path, text)
        except BaseException:
            caps.release(reserved)
            raise
        caps.done()
        attempt["agent"] = info.agent_id
        return f"spawned agent {info.agent_id} with the brief"

    def _brief(self, row: dict[str, Any], item: dict[str, Any], attempt: dict[str, Any],
               now: float) -> None:
        engine = self._engine
        attempt["briefed"] = True
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
