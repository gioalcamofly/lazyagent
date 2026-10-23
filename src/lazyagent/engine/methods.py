"""The request methods every engine server exposes, independent of the wire.

:class:`EngineMethods` implements the ten methods the legacy NDJSON IPC
server has always answered (``list_worktrees`` … ``list_events``) on top of
the :class:`~lazyagent.engine.engine.Engine`'s public API. Both servers call
it: :mod:`lazyagent.ipc` (legacy envelope) and :mod:`lazyagent.engine.server`
(protocol v1), so the two cannot drift apart.

A method returns its result or raises; :func:`classify` maps the exception
to the error code both protocols share.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from lazyagent import diagnostics as diag
from lazyagent.agent_events import STATUS_GROUPS, make_filter, resolve_statuses
from lazyagent.agent_providers import ResumeMode
from lazyagent.engine.events import ENGINE_RESTORED, REVIEW_REPLIED
from lazyagent.engine.notes import MODES as NOTES_MODES
from lazyagent.engine.notes import Notes, NotesConflict, NotesStore
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.reviews import check_comment_id
from lazyagent.engine.types import AGENT_KIND, ORCHESTRATOR_KIND
from lazyagent.models import AgentStatus
from lazyagent.worktree_manager import WorktreeManagerError

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[Any]]

# notes.updated ``by``: who wrote. Also the caller roles (Caller.role).
USER_BY = "user"
AGENT_BY = "agent"
ORCHESTRATOR_BY = "orchestrator"

_DAY = 86400.0

# How long a spawn may take before the caller is told it timed out.
_OP_TIMEOUT = 30.0


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


# How long send_agent_input waits for the agent to start working on the text
# before reporting the delivery as unconfirmed.
_INPUT_CONFIRM_TIMEOUT = _env_seconds("LAZYAGENT_INPUT_CONFIRM_TIMEOUT", 5.0)

# An agent sitting on one of these is showing a dialog: input sent to it
# answers the dialog rather than starting a new turn.
_BLOCKED_STATUSES = STATUS_GROUPS["blocked"]


def classify(error: Exception) -> tuple[str, str, Any]:
    """``(code, message, data)`` for an exception a method raised; ``data``
    is None unless the error carries detail (v1 sends it as ``error.data``,
    the legacy socket only the code and message)."""
    if isinstance(error, NotesConflict):
        return "CONFLICT", str(error), {"rev": error.rev, "text": error.text}
    if isinstance(error, WorktreeManagerError):
        return "WORKTREE_ERROR", str(error), None
    if isinstance(error, ValueError):
        return "VALIDATION_ERROR", str(error), None
    # Distinct from the builtin before Python 3.11.
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "TIMEOUT", "Operation timed out", None
    return "INTERNAL_ERROR", str(error), None


def check_env(env: Any) -> dict[str, str] | None:
    """A client's ``env`` param, checked: ``None`` or a str → str map."""
    if env is not None and not (
        isinstance(env, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
    ):
        raise ValueError("env must be an object of string values")
    return env


def _item_id(params: dict) -> str:
    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise ValueError("id must be an actionable's id")
    return item_id


class SpawnCaps:
    """``[orchestrator] max_spawns_per_day`` / ``max_concurrent_agents``,
    applied to the spawns asked for with an agent token. One per engine:
    every server's methods share it."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._spawns: list[float] = []
        # Spawns checked but not started yet: they count as running.
        self._pending = 0

    def reserve(self, engine: Engine) -> float:
        """Take one spawn from the caps, or raise ``ValueError`` (a
        VALIDATION_ERROR) saying which one is full. Give it back with
        :meth:`release` when the spawn fails, :meth:`done` when it starts."""
        config = engine.config.orchestrator
        now = self.clock()
        self._spawns = [t for t in self._spawns if now - t < _DAY]
        cap = config.max_spawns_per_day
        if cap and len(self._spawns) >= cap:
            raise ValueError(
                f"spawn refused: agents spawned {len(self._spawns)} agents in "
                f"the last 24 h (max_spawns_per_day = {cap} in [orchestrator] "
                "of .lazyagent.toml). Ask the user to spawn it."
            )
        cap = config.max_concurrent_agents
        running = self._pending + sum(
            len(engine.agent_ids(wt.path)) for wt in engine.worktrees
        )
        if cap and running >= cap:
            raise ValueError(
                f"spawn refused: {running} agents are running "
                f"(max_concurrent_agents = {cap} in [orchestrator] of "
                ".lazyagent.toml). Stop one first, or ask the user."
            )
        self._spawns.append(now)
        self._pending += 1
        return now

    def done(self) -> None:
        self._pending -= 1

    def release(self, reserved: float) -> None:
        self._pending -= 1
        if reserved in self._spawns:
            self._spawns.remove(reserved)


class EngineMethods:
    """The legacy method set, as plain coroutines over an :class:`Engine`."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.handlers: dict[str, Handler] = {
            "list_worktrees": self.list_worktrees,
            "create_worktree": self.create_worktree,
            "remove_worktree": self.remove_worktree,
            "spawn_agent": self.spawn_agent,
            "stop_agent": self.stop_agent,
            "get_agent_status": self.get_agent_status,
            "read_agent_output": self.read_agent_output,
            "send_agent_input": self.send_agent_input,
            "list_agents": self.list_agents,
            "list_events": self.list_events,
            "list_restorable": self.list_restorable,
            "restore_agents": self.restore_agents,
            "dismiss_restorable": self.dismiss_restorable,
            "notes_get": self.notes_get,
            "notes_put": self.notes_put,
            "notes_append": self.notes_append,
            "notes_replace_section": self.notes_replace_section,
            "notes_list": self.notes_list,
            "notes_adopt": self.notes_adopt,
            "notes_delete": self.notes_delete,
            "review_register": self.review_register,
            "review_reply": self.review_reply,
            "review_replies": self.review_replies,
            "status_list": self.status_list,
            "actionables_list": self.actionables_list,
            "actionables_get": self.actionables_get,
            "actionables_mark": self.actionables_mark,
        }

    def _caller(self, params: dict) -> tuple[str, tuple[str, str] | None]:
        """``(role, slot)`` of a call: ``orchestrator`` or ``agent`` for a
        live agent's token (``caller``), ``user`` with no token. An unknown
        token is refused rather than taken for the user."""
        caller = params.get("caller")
        if caller is not None and not isinstance(caller, str):
            raise ValueError("caller must be a string")
        if not caller:
            return USER_BY, None
        slot = self._engine.token_slot(caller)
        if slot is None:
            raise ValueError("Unknown caller token: its agent is gone")
        return (ORCHESTRATOR_BY if slot[0] == ORCHESTRATOR_KEY else AGENT_BY), slot

    async def list_worktrees(self, params: dict) -> Any:
        """List all worktrees with agent status and git status."""
        return self._engine.worktrees_payload()

    async def create_worktree(self, params: dict) -> Any:
        """Create a new worktree.

        Params:
            branch: required, non-empty branch name.
            base_branch: optional; default: the configured default branch
                when a worktree has it checked out, else the main
                worktree's branch.
            extra: optional, only used when a custom create command is
                configured.

        Two paths:
        - **No custom create command**: runs ``git worktree add`` and
          returns the actual path. ``extra`` is ignored (with a warning in
          the response so callers notice).
        - **Custom create command** (``[worktree] create = "..."`` in
          ``.lazyagent.toml``): the command runs as an engine job in the repo
          root (its output is streamed as ``job.output`` events) and the call
          waits for it. ``path`` in the response is *predicted* from the
          naming convention (``<parent>/<repo>-<branch>``); the caller MUST
          poll ``list_worktrees`` to confirm the worktree actually appeared
          before calling ``spawn_agent`` on it. ``custom_command: true``
          flags this contract. A failing command is a WORKTREE_ERROR carrying
          its output.
        """
        branch = params.get("branch", "").strip()
        if not branch:
            raise ValueError("branch is required and must not be empty")
        base_branch = params.get("base_branch") or None
        extra = params.get("extra", "")
        env = check_env(params.get("env"))

        created = await self._engine.create_worktree(
            branch, base_branch, extra, env=env
        )
        if created.job_id is not None:
            return {
                "path": created.path,
                "branch": created.branch,
                "custom_command": True,
                "job_id": created.job_id,
                "warning": (
                    "Path is predicted from the naming convention "
                    "(<parent>/<repo>-<branch>), not parsed from the create "
                    "command output. If the custom script writes elsewhere, "
                    "poll list_worktrees to find the real path before calling "
                    "spawn_agent on it."
                ),
            }

        # Surface a warning if extra was passed but cannot be honoured —
        # easier for an LLM caller to notice than a silent drop.
        result: dict[str, Any] = {}
        if extra:
            result["warning"] = (
                "extra= was provided but this repo has no custom create "
                "command configured; the value was ignored."
            )
        result["path"] = created.path
        result["branch"] = created.branch
        return result

    async def remove_worktree(self, params: dict) -> Any:
        """Remove a worktree. Params: worktree_path, force (optional),
        custom (optional), env (optional).

        ``git worktree remove`` unless ``custom`` is true: then a configured
        custom remove command runs as an engine job (with ``env`` when given),
        as when the TUI removes a worktree.
        """
        worktree_path = params.get("worktree_path", "")
        force = params.get("force", False)
        custom = params.get("custom", False)

        if not worktree_path:
            raise ValueError("worktree_path is required")
        if type(custom) is not bool:
            raise ValueError("custom must be a boolean")
        env = check_env(params.get("env"))

        job_id = await self._engine.remove_worktree(
            worktree_path, force, custom=custom, env=env
        )
        result: dict[str, Any] = {"removed": worktree_path}
        if job_id is not None:
            result["job_id"] = job_id
        return result

    async def spawn_agent(self, params: dict) -> Any:
        """Spawn a NEW agent in a worktree. Params: worktree_path, instruction
        (optional), skip_permissions (optional), resume_mode (optional),
        label (optional), env (optional: the environment the agent starts
        from instead of the engine's), kind (optional: ``"orchestrator"``
        starts the repo's orchestrator, ignoring worktree_path and label),
        review_comment_ids (optional: the review comments in the instruction,
        registered before the agent starts, as review_register would).

        Each call adds another agent; the new agent id is returned. Multiple
        agents per worktree are supported.
        """
        kind = params.get("kind", AGENT_KIND)
        if kind not in (AGENT_KIND, ORCHESTRATOR_KIND):
            raise ValueError(f"Unknown agent kind: {kind!r}")
        worktree_path = params.get("worktree_path", "")
        if kind == AGENT_KIND:
            if not worktree_path:
                raise ValueError("worktree_path is required")
            if self._engine.worktree(worktree_path) is None:
                raise ValueError(f"Unknown worktree: {worktree_path}")

        instruction = params.get("instruction") or params.get("initial_prompt")
        skip_permissions = params.get("skip_permissions", True)
        label = params.get("label")
        resume_mode_str = params.get("resume_mode", "new")
        try:
            resume_mode = ResumeMode(resume_mode_str)
        except ValueError:
            raise ValueError(
                f"Invalid resume_mode: {resume_mode_str!r}. "
                f"Must be one of: {', '.join(m.value for m in ResumeMode)}"
            )
        env = check_env(params.get("env"))
        notes_mode = params.get("notes_mode")
        if notes_mode is not None and notes_mode not in NOTES_MODES:
            raise ValueError(
                f"Invalid notes_mode: {notes_mode!r}. "
                f"Must be one of: {', '.join(NOTES_MODES)}"
            )

        review_comment_ids = params.get("review_comment_ids")
        if review_comment_ids is not None:
            if kind != AGENT_KIND or not isinstance(review_comment_ids, list):
                raise ValueError("review_comment_ids must be a list, for an agent")
            for comment_id in review_comment_ids:
                check_comment_id(comment_id)

        # The caps bind agents (the orchestrator included), not the user.
        role, _ = self._caller(params)
        caps = self._engine.spawn_caps if role != USER_BY else None
        reserved = caps.reserve(self._engine) if caps is not None else 0.0

        # Before the spawn, so every event of the new agent is after it.
        cursor = self._engine.events.cursor
        try:
            info = await asyncio.wait_for(
                self._engine.spawn_agent(
                    worktree_path,
                    instruction,
                    skip_permissions=skip_permissions,
                    resume_mode=resume_mode,
                    label=label,
                    kind=kind,
                    env=env,
                    notes_mode=notes_mode,
                    review_comment_ids=review_comment_ids,
                ),
                timeout=_OP_TIMEOUT,
            )
        except BaseException:
            if caps is not None:
                caps.release(reserved)
            raise
        if caps is not None:
            caps.done()
        return {
            "worktree_path": worktree_path
            if kind == AGENT_KIND
            else info.worktree_path,
            "agent_id": info.agent_id,
            "status": "spawned",
            "cursor": cursor,
        }

    async def stop_agent(self, params: dict) -> Any:
        """Stop an agent in a worktree. Params: worktree_path, agent_id
        (optional), notes (optional: ``"delete"``, the default, or ``"keep"``
        to leave its session notes as an orphan).

        ``agent_id`` may be omitted when the worktree has exactly one agent.
        The result carries the agent's final ``notes`` text when they were
        not empty: the handoff of a caller with no UI to offer one.
        """
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")
        notes = params.get("notes", "delete")
        if notes not in ("delete", "keep"):
            raise ValueError(f"notes must be 'delete' or 'keep', not {notes!r}")

        agent_ids = self._engine.agent_ids(worktree_path)
        if not agent_ids:
            raise ValueError("No running agent in this worktree")

        agent_id = resolve_agent_id(agent_ids, params.get("agent_id"))
        _, text = self._engine.stop_agent_with_notes(
            worktree_path, agent_id, notes=notes
        )
        result: dict[str, Any] = {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "status": "stopped",
        }
        if text is not None:
            result["notes"] = text
        return result

    async def get_agent_status(self, params: dict) -> Any:
        """Get agent status for a worktree. Params: worktree_path, agent_id
        (optional). ``agent_id`` may be omitted when there is one agent."""
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        bucket = self._engine.agent_states(worktree_path)
        if not bucket:
            return {
                "worktree_path": worktree_path,
                "agent_id": "",
                "status": AgentStatus.NO_AGENT.value,
                "detail": "",
            }

        agent_id = resolve_agent_id(list(bucket.keys()), params.get("agent_id"))
        state = bucket[agent_id]
        return {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "status": state.status.value,
            "confidence": state.confidence.value,
            "detail": state.detail,
        }

    async def read_agent_output(self, params: dict) -> Any:
        """Read recent terminal output from an agent. Params: worktree_path,
        agent_id (optional), lines (optional)."""
        worktree_path = params.get("worktree_path", "")
        num_lines = params.get("lines", 50)
        if not worktree_path:
            raise ValueError("worktree_path is required")

        agent_ids = self._engine.agent_ids(worktree_path)
        if not agent_ids:
            raise ValueError("No agent terminal in this worktree")

        agent_id = resolve_agent_id(agent_ids, params.get("agent_id"))
        # A backlog after a burst is parsed in slices first, so reading the
        # screen never holds the loop for long; what is left is small.
        session = self._engine.get_session(worktree_path, agent_id)
        if session is not None:
            await session.catch_up()
        lines, total_lines = self._engine.read_agent_output(
            worktree_path, agent_id, num_lines
        )

        return {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "lines": lines,
            "total_lines": total_lines,
        }

    async def send_agent_input(self, params: dict) -> Any:
        """Send text input to a running agent's terminal. Params: worktree_path,
        text, agent_id (optional)."""
        worktree_path = params.get("worktree_path", "")
        text = params.get("text", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")
        if not text:
            raise ValueError("text is required and must not be empty")

        agent_ids = self._engine.agent_ids(worktree_path)
        if not agent_ids:
            raise ValueError("No running agent in this worktree")

        agent_id = resolve_agent_id(agent_ids, params.get("agent_id"))
        session = self._engine.get_session(worktree_path, agent_id)
        if session is None or not session.running:
            raise ValueError("Agent terminal is not ready")

        confirm = params.get("confirm", True)

        bus = self._engine.events
        state = self._engine.agent_states(worktree_path).get(agent_id)
        # No state yet = spawned but no status reported so far; the terminal
        # itself is running (checked above), so it is not "exited".
        status_before = (
            state.status.value if state is not None else AgentStatus.NO_AGENT.value
        )
        # Taken before the write: only transitions after this point can be a
        # reaction to our input.
        cursor = bus.cursor
        warnings: list[str] = []
        if status_before in _BLOCKED_STATUSES:
            warnings.append(
                f"The agent was {status_before} (detail: "
                f"{state.detail if state else ''!r}), so the text answered that "
                "dialog instead of starting a new prompt."
            )

        # send_input, not a raw queue put: the text and the Enter that
        # submits it have to reach the agent CLI as separate reads, or the
        # CLI parses the trailing CR as a character inside the message and
        # the prompt just sits there unsent.
        await self._engine.send_agent_input(worktree_path, agent_id, text, submit=True)

        result: dict[str, Any] = {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "status": "sent",
            "status_before": status_before,
            "cursor": cursor,
        }

        # Delivery is confirmed by the agent starting to work: every turn
        # begins with a RUNNING transition (for Claude, the UserPromptSubmit
        # hook). An agent that was already working queues the text or ignores
        # it, and neither is observable, so that case stays "busy".
        if status_before == AgentStatus.RUNNING.value:
            result["delivery"] = "busy"
            warnings.append(
                "The agent was already working; the text was sent but cannot be "
                "confirmed until its current turn ends."
            )
        elif confirm:
            events, _ = await bus.wait(
                cursor,
                make_filter(
                    worktree_path,
                    agent_id,
                    frozenset({AgentStatus.RUNNING.value}) | STATUS_GROUPS["finished"],
                ),
                _INPUT_CONFIRM_TIMEOUT,
            )
            started = events[0] if events else None
            if started is not None and started.status == AgentStatus.RUNNING.value:
                result["delivery"] = "confirmed"
            else:
                result["delivery"] = "unconfirmed"
                warnings.append(
                    f"The agent did not start working within "
                    f"{_INPUT_CONFIRM_TIMEOUT:g}s. The text may be sitting unsent "
                    "in its prompt — check read_agent_output."
                    if started is None
                    else f"The agent {started.status} instead of starting work."
                )
        else:
            result["delivery"] = "unchecked"

        if warnings:
            result["warnings"] = warnings
        return result

    async def list_events(self, params: dict) -> Any:
        """Return agent events after a cursor; never blocks. Params: since
        (cursor; omitted = from now), worktree_path, agent_id, statuses
        (filters, all optional).
        """
        bus = self._engine.events
        since = params.get("since")
        if since is None:
            since = bus.cursor
        elif not isinstance(since, int) or since < 0:
            raise ValueError("since must be a non-negative integer cursor")
        match = make_filter(
            params.get("worktree_path") or None,
            params.get("agent_id"),
            resolve_statuses(params.get("statuses")),
        )
        events, truncated = bus.since(since, match)
        # The events are filtered, so the cursor has to cover what the filter
        # skipped too — otherwise the next call would re-scan it forever.
        result: dict[str, Any] = {
            "events": [e.to_dict() for e in events],
            "cursor": bus.cursor,
            "truncated": truncated,
        }
        # Restores (engine.restored) are engine-wide, so no filter applies;
        # the key is only there when one happened after the cursor, which
        # keeps the answer unchanged for clients that never restore.
        restores = [
            {"seq": e.seq, **e.data}
            for e in bus.history(since)
            if e.event == ENGINE_RESTORED
        ]
        if restores:
            result["restores"] = restores
        return result

    @staticmethod
    def _ids(params: dict) -> list[str] | None:
        ids = params.get("ids")
        if ids is not None and not (
            isinstance(ids, list) and all(isinstance(i, str) for i in ids)
        ):
            raise ValueError("ids must be a list of strings")
        return ids

    async def list_restorable(self, params: dict) -> Any:
        """Agents earlier engines ran when they ended (restart manifest)."""
        return {"agents": [e.to_dict() for e in self._engine.list_restorable()]}

    async def restore_agents(self, params: dict) -> Any:
        """Spawn restorable agents again (all of them without ``ids``), each
        resuming its conversation. Params: ids, env (both optional)."""
        env = check_env(params.get("env"))
        return await self._engine.restore_agents(self._ids(params), env=env)

    async def dismiss_restorable(self, params: dict) -> Any:
        """Stop offering restorable agents (all of them without ``ids``)."""
        return {"dismissed": self._engine.dismiss_restorable(self._ids(params))}

    # ------------------------------------------------------------------
    # Session notes (proposal 0011)
    # ------------------------------------------------------------------

    def _notes_store(self) -> NotesStore:
        store = self._engine.notes
        if store is None:
            raise ValueError("Session notes are off ([notes] enabled = false)")
        return store

    def _notes_target(
        self, params: dict, *, writing: bool
    ) -> tuple[tuple[str, str], str, str]:
        """``(slot, notes_id, by)`` of a notes call.

        The target is ``worktree_path`` (+ ``agent_id`` unless it has one
        agent) when given, else the calling agent (its ``caller`` token).
        The writer is the agent itself (its own notes), the orchestrator
        (only with the orchestrator agent's token) or the user (a caller
        with no token: a client, or a session lazyagent did not spawn);
        another agent's token may read but not write.
        """
        role, caller_slot = self._caller(params)
        worktree_path = params.get("worktree_path") or ""
        agent_id = params.get("agent_id")
        if worktree_path:
            agent_ids = self._engine.agent_ids(worktree_path)
            if not agent_ids:
                raise ValueError("No running agent in this worktree")
            slot = (worktree_path, resolve_agent_id(agent_ids, agent_id))
        elif agent_id:
            raise ValueError("agent_id needs worktree_path")
        elif caller_slot is not None:
            slot = caller_slot
        else:
            raise ValueError(
                "no target: this call carries no agent token, so it has no "
                "'my notes'. Pass worktree_path (and agent_id when that "
                "worktree runs several agents; list_agents shows them). In an "
                "agent lazyagent spawned, call the notes tools of its "
                "lazyagent_app MCP server: they always carry the agent's "
                "token (a lazyagent server from your own Claude config only "
                "does when it inherits LAZYAGENT_AGENT_TOKEN from the agent)"
            )

        if caller_slot is None:
            by = USER_BY
        elif caller_slot == slot:
            by = AGENT_BY
        elif role == ORCHESTRATOR_BY:
            by = ORCHESTRATOR_BY
        elif writing:
            raise ValueError(
                "An agent may only write its own notes: "
                f"{slot[1] or 'the orchestrator'} in {slot[0]} is another agent"
            )
        else:
            by = AGENT_BY

        self._notes_store()
        notes_id = self._engine.notes_id(*slot)
        if notes_id is None:
            raise ValueError(f"Agent {slot[1]!r} in {slot[0]} has no notes")
        return slot, notes_id, by

    @staticmethod
    def _text(params: dict, name: str = "text") -> str:
        text = params.get(name)
        if not isinstance(text, str):
            raise ValueError(f"{name} is required and must be a string")
        return text

    @staticmethod
    def _rev(params: dict, *, required: bool) -> int | None:
        rev = params.get("if_rev")
        if rev is None and not required:
            return None
        if type(rev) is not int or rev < 0:
            raise ValueError("if_rev must be a non-negative integer (the rev read)")
        return rev

    @staticmethod
    def _written(notes: Notes) -> dict[str, Any]:
        meta = notes.meta
        result: dict[str, Any] = {
            "worktree_path": meta.worktree_path,
            "agent_id": meta.agent_id,
            "notes_id": meta.notes_id,
            "rev": meta.rev,
            "size": notes.size,
            "max_bytes": notes.max_bytes,
            "by": meta.by,
            "updated_at": meta.updated_at,
        }
        if notes.warning:
            result["warning"] = notes.warning
        return result

    async def notes_get(self, params: dict) -> Any:
        """An agent's session notes. Params: worktree_path + agent_id, or
        notes_id (any notes, an ended agent's too), or neither with a
        ``caller`` token (the calling agent's own)."""
        store = self._notes_store()
        notes_id = params.get("notes_id")
        if notes_id is not None:
            if not isinstance(notes_id, str) or notes_id not in store:
                raise ValueError(f"No notes {notes_id!r}")
        else:
            _, notes_id, _ = self._notes_target(params, writing=False)
        notes = store.get(notes_id)
        meta = notes.meta
        result = self._written(notes)
        result.update(
            {
                "text": notes.text,
                "state": meta.state,
                "kind": meta.kind,
                "mode": meta.mode,
                "conversation_id": meta.conversation_id,
                "instruction": meta.instruction,
                "created_at": meta.created_at,
                "ended_at": meta.ended_at,
                "empty": notes.empty,
            }
        )
        return result

    async def notes_put(self, params: dict) -> Any:
        """Replace the whole text (a user's editor). Params: worktree_path,
        agent_id, text, if_rev (the rev the edit started from: a stale one
        is a CONFLICT carrying the current ``{rev, text}``)."""
        text = self._text(params)
        if_rev = self._rev(params, required=True)
        _, notes_id, by = self._notes_target(params, writing=True)
        if not params.get("caller"):
            by = USER_BY
        return self._written(self._notes_store().put(notes_id, text, if_rev, by))

    async def notes_append(self, params: dict) -> Any:
        """Add lines at the end of a section (created when missing).
        Params: section, text, if_rev (optional), the target (see
        notes_get), caller."""
        section = self._text(params, "section")
        text = self._text(params)
        if_rev = self._rev(params, required=False)
        _, notes_id, by = self._notes_target(params, writing=True)
        notes = self._notes_store().append(notes_id, section, text, by, if_rev)
        return self._written(notes)

    async def notes_replace_section(self, params: dict) -> Any:
        """Replace a section's body. Params: section, text, if_rev, the
        target (see notes_get), caller."""
        section = self._text(params, "section")
        text = self._text(params)
        if_rev = self._rev(params, required=True)
        _, notes_id, by = self._notes_target(params, writing=True)
        notes = self._notes_store().replace_section(
            notes_id, section, text, if_rev, by
        )
        return self._written(notes)

    async def notes_list(self, params: dict) -> Any:
        """Every notes this engine knows: live, restorable and orphan (of
        ``worktree_path`` only, when given), newest first."""
        store = self._notes_store()
        worktree_path = params.get("worktree_path") or None
        items = []
        for meta in store.list(worktree_path):
            notes = store.get(meta.notes_id)
            items.append(
                {
                    "notes_id": meta.notes_id,
                    "state": meta.state,
                    "worktree_path": meta.worktree_path,
                    "agent_id": meta.agent_id,
                    "kind": meta.kind,
                    "size": notes.size,
                    "empty": notes.empty,
                    "by": meta.by,
                    "updated_at": meta.updated_at,
                    "ended_at": meta.ended_at,
                    "expires_at": store.expires_at(meta),
                }
            )
        return {"notes": items}

    async def notes_adopt(self, params: dict) -> Any:
        """Recover ended notes into a live agent: they replace its text and
        go (``notes.removed`` reason ``adopted``). Params: notes_id,
        worktree_path, agent_id, if_rev (optional: the live notes' rev)."""
        notes_id = self._text(params, "notes_id")
        if_rev = self._rev(params, required=False)
        slot, _, by = self._notes_target(params, writing=True)
        if not params.get("caller"):
            by = USER_BY
        notes = self._engine.adopt_notes(notes_id, *slot, if_rev=if_rev, by=by)
        return self._written(notes)

    async def notes_delete(self, params: dict) -> Any:
        """Drop ended notes (an orphan, or a restorable agent's). Params:
        notes_id."""
        notes_id = self._text(params, "notes_id")
        self._notes_store()
        self._engine.delete_notes(notes_id)
        return {"deleted": notes_id}

    async def review_register(self, params: dict) -> Any:
        """Lets an agent answer review comments with review_reply. Params:
        worktree_path, agent_id, comment_ids (the ids its prompt gives).
        A client calls it when it sends the comments (after the spawn, for
        an agent started with them)."""
        worktree_path = self._text(params, "worktree_path")
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str):
            raise ValueError("agent_id is required and must be a string")
        if agent_id not in self._engine.agent_ids(worktree_path):
            raise ValueError(f"No agent {agent_id!r} in {worktree_path}")
        comment_ids = params.get("comment_ids")
        if not isinstance(comment_ids, list) or not comment_ids:
            raise ValueError("comment_ids must be a non-empty list")
        store = self._engine.review_store()
        ids = store.register(worktree_path, agent_id, comment_ids)
        return {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "comment_ids": ids,
        }

    async def review_reply(self, params: dict) -> Any:
        """An agent's answer to a review comment. Params: comment_id, text,
        caller (the agent's token: only an agent the comment was registered
        for may answer it). Emits ``review.replied``."""
        caller = params.get("caller")
        if not isinstance(caller, str) or not caller:
            raise ValueError(
                "review_reply needs the agent's token: call it from the "
                "lazyagent_app MCP server of an agent lazyagent spawned"
            )
        slot = self._engine.token_slot(caller)
        if slot is None:
            raise ValueError("Unknown caller token: its agent is gone")
        comment_id = params.get("comment_id")
        text = self._text(params)
        reply = self._engine.review_store().reply(slot, comment_id, text)
        self._engine.events.emit(REVIEW_REPLIED, reply.to_dict())
        return {"reply_id": reply.reply_id, "comment_id": reply.comment_id}

    async def review_replies(self, params: dict) -> Any:
        """The kept replies (of ``worktree_path`` when given), oldest first:
        what a client missed while it was away."""
        worktree_path = params.get("worktree_path") or None
        replies = self._engine.review_store().replies(worktree_path)
        return {"replies": [r.to_dict() for r in replies]}

    async def status_list(self, params: dict) -> Any:
        """The work items (proposal 0013): one row per branch with who has
        the ball, a short fact and its age, inbox first. Params: repo (only
        that repo's rows), include_done (also merged / closed / stale rows).
        """
        repo = params.get("repo")
        if repo is not None and not isinstance(repo, str):
            raise ValueError("repo must be a string")
        include_done = params.get("include_done", False)
        if type(include_done) is not bool:
            raise ValueError("include_done must be a boolean")
        try:
            role, _ = self._caller(params)
        except ValueError:
            role = "unknown"
        # The 0013 gate's metric: how often the table is asked for, by whom.
        diag.event(log, "status.list", by=role)
        return self._engine.status.list(repo=repo or None, include_done=include_done)

    async def actionables_list(self, params: dict) -> Any:
        """The inbox queue (proposal 0013): what needs the user, Now first,
        then Round. Params: repo (only items of that repo's work items),
        include_hidden (also the parked ones)."""
        repo = params.get("repo")
        if repo is not None and not isinstance(repo, str):
            raise ValueError("repo must be a string")
        include_hidden = params.get("include_hidden", False)
        if type(include_hidden) is not bool:
            raise ValueError("include_hidden must be a boolean")
        return self._engine.actionables.list(
            repo=repo or None, include_hidden=include_hidden
        )

    async def actionables_get(self, params: dict) -> Any:
        """One item of the inbox queue, parked or not. Params: id."""
        return {"item": self._engine.actionables.get(_item_id(params))}

    async def actionables_mark(self, params: dict) -> Any:
        """Dismiss an item's occurrence, park it until a time or an event
        (ci_done, agent_stopped, human_review), or unpark it, for every
        client. Params: id, action, until (park only). The user's (no token)
        or the orchestrator's; a worker's token is refused."""
        role, _ = self._caller(params)
        if role == AGENT_BY:
            raise ValueError("Only the user or the orchestrator marks inbox items")
        action = params.get("action")
        if not isinstance(action, str):
            raise ValueError("action must be a string")
        item = self._engine.actionables.mark(
            _item_id(params), action, until=params.get("until"), by=role
        )
        return {"item": item}

    async def list_agents(self, params: dict) -> Any:
        """List the agents in a worktree. Params: worktree_path.

        Returns ``{worktree_path, agents: [{agent_id, label, status,
        confidence, detail}]}``.
        """
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        return {
            "worktree_path": worktree_path,
            "agents": self._engine.agents_payload(worktree_path),
        }


def resolve_agent_id(
    available: list[str], agent_id: str | None, *, what: str = "agent"
) -> str:
    """Resolve an optional agent id against the available ids.

    Back-compatible rule: an explicit id must exist; if omitted and there
    is exactly one agent, address it; otherwise fail with a list so the
    caller disambiguates.
    """
    if agent_id:
        if agent_id not in available:
            raise ValueError(
                f"No {what} {agent_id!r} in this worktree. Available: {available}"
            )
        return agent_id
    if len(available) == 1:
        return available[0]
    if not available:
        raise ValueError(f"No running {what} in this worktree")
    raise ValueError(
        f"Multiple agents in this worktree; specify agent_id. Available: {available}"
    )
