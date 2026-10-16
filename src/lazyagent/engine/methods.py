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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from lazyagent.agent_events import STATUS_GROUPS, make_filter, resolve_statuses
from lazyagent.agent_providers import ResumeMode
from lazyagent.engine.types import AGENT_KIND, ORCHESTRATOR_KIND
from lazyagent.models import AgentStatus
from lazyagent.worktree_manager import WorktreeManagerError

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[Any]]

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


def classify(error: Exception) -> tuple[str, str]:
    """``(code, message)`` for an exception a method raised."""
    if isinstance(error, WorktreeManagerError):
        return "WORKTREE_ERROR", str(error)
    if isinstance(error, ValueError):
        return "VALIDATION_ERROR", str(error)
    if isinstance(error, TimeoutError):
        return "TIMEOUT", "Operation timed out"
    return "INTERNAL_ERROR", str(error)


def check_env(env: Any) -> dict[str, str] | None:
    """A client's ``env`` param, checked: ``None`` or a str → str map."""
    if env is not None and not (
        isinstance(env, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
    ):
        raise ValueError("env must be an object of string values")
    return env


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
        }

    async def list_worktrees(self, params: dict) -> Any:
        """List all worktrees with agent status and git status."""
        return self._engine.worktrees_payload()

    async def create_worktree(self, params: dict) -> Any:
        """Create a new worktree.

        Params:
            branch: required, non-empty branch name.
            base_branch: optional, defaults to "main".
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
        base_branch = params.get("base_branch", "main")
        extra = params.get("extra", "")
        env = check_env(params.get("env"))

        created = await self._engine.create_worktree(branch, base_branch, extra, env=env)
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
        starts the repo's orchestrator, ignoring worktree_path and label).

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

        # Before the spawn, so every event of the new agent is after it.
        cursor = self._engine.events.cursor
        info = await asyncio.wait_for(
            self._engine.spawn_agent(
                worktree_path,
                instruction,
                skip_permissions=skip_permissions,
                resume_mode=resume_mode,
                label=label,
                kind=kind,
                env=env,
            ),
            timeout=_OP_TIMEOUT,
        )
        return {
            "worktree_path": worktree_path
            if kind == AGENT_KIND
            else info.worktree_path,
            "agent_id": info.agent_id,
            "status": "spawned",
            "cursor": cursor,
        }

    async def stop_agent(self, params: dict) -> Any:
        """Stop an agent in a worktree. Params: worktree_path, agent_id (optional).

        ``agent_id`` may be omitted when the worktree has exactly one agent.
        """
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        agent_ids = self._engine.agent_ids(worktree_path)
        if not agent_ids:
            raise ValueError("No running agent in this worktree")

        agent_id = resolve_agent_id(agent_ids, params.get("agent_id"))
        self._engine.stop_agent(worktree_path, agent_id)
        return {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "status": "stopped",
        }

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
        return {
            "events": [e.to_dict() for e in events],
            "cursor": bus.cursor,
            "truncated": truncated,
        }

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
