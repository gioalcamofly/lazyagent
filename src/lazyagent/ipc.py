"""IPC server — the legacy Unix domain socket server of the engine.

Receives JSON requests from the MCP server process and dispatches them to
handlers that call the :class:`~lazyagent.engine.engine.Engine`'s public API
only. It runs on the engine's loop, so the handlers call it directly.

Protocol: newline-delimited JSON.

    Request:  {"id": "uuid", "method": "list_worktrees", "params": {}}
    Success:  {"id": "uuid", "result": [...]}
    Error:    {"id": "uuid", "error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any

from lazyagent.agent_events import STATUS_GROUPS, make_filter, resolve_statuses
from lazyagent.agent_providers import ResumeMode
from lazyagent.models import AgentStatus
from lazyagent.worktree_manager import WorktreeManagerError

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

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


def _ok(request_id: str, result: Any) -> dict:
    return {"id": request_id, "result": result}


def _err(request_id: str, code: str, message: str) -> dict:
    return {"id": request_id, "error": {"code": code, "message": message}}


class IpcServer:
    """Asyncio Unix domain socket server in front of an :class:`Engine`."""

    def __init__(self, engine: Engine, socket_path: str) -> None:
        self._engine = engine
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._handlers = {
            "list_worktrees": self.handle_list_worktrees,
            "create_worktree": self.handle_create_worktree,
            "remove_worktree": self.handle_remove_worktree,
            "spawn_agent": self.handle_spawn_agent,
            "stop_agent": self.handle_stop_agent,
            "get_agent_status": self.handle_get_agent_status,
            "read_agent_output": self.handle_read_agent_output,
            "send_agent_input": self.handle_send_agent_input,
            "list_agents": self.handle_list_agents,
            "list_events": self.handle_list_events,
        }

    @property
    def socket_path(self) -> str:
        return self._socket_path

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
        )
        log.info("IPC server listening on %s", self._socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        # Remove the parent directory if empty
        parent = os.path.dirname(self._socket_path)
        try:
            os.rmdir(parent)
        except OSError:
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection (one request per line)."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue

                request_id = request.get("id", "")
                method = request.get("method", "")
                params = request.get("params", {})

                response = await self._dispatch(request_id, method, params)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _dispatch(
        self, request_id: str, method: str, params: dict
    ) -> dict:
        handler = self._handlers.get(method)

        if handler is None:
            return _err(request_id, "UNKNOWN_METHOD", f"Unknown method: {method}")

        try:
            result = await handler(request_id, params)
            return result
        except WorktreeManagerError as e:
            return _err(request_id, "WORKTREE_ERROR", str(e))
        except ValueError as e:
            return _err(request_id, "VALIDATION_ERROR", str(e))
        except TimeoutError:
            return _err(request_id, "TIMEOUT", "Operation timed out")
        except Exception as e:
            log.exception("IPC handler error for method=%s", method)
            return _err(request_id, "INTERNAL_ERROR", str(e))

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def handle_list_worktrees(
        self, request_id: str, params: dict
    ) -> dict:
        """List all worktrees with agent status and git status."""
        return _ok(request_id, self._engine.worktrees_payload())

    async def handle_create_worktree(
        self, request_id: str, params: dict
    ) -> dict:
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

        created = await self._engine.create_worktree(branch, base_branch, extra)
        if created.job_id is not None:
            return _ok(request_id, {
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
            })

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
        return _ok(request_id, result)

    async def handle_remove_worktree(
        self, request_id: str, params: dict
    ) -> dict:
        """Remove a worktree. Params: worktree_path, force (optional).

        Always ``git worktree remove``: a configured custom remove command is
        what the TUI runs, not this method.
        """
        worktree_path = params.get("worktree_path", "")
        force = params.get("force", False)

        if not worktree_path:
            raise ValueError("worktree_path is required")

        await self._engine.remove_worktree(worktree_path, force, custom=False)
        return _ok(request_id, {"removed": worktree_path})

    async def handle_spawn_agent(
        self, request_id: str, params: dict
    ) -> dict:
        """Spawn a NEW agent in a worktree. Params: worktree_path, instruction
        (optional), skip_permissions (optional), resume_mode (optional),
        label (optional), env (optional: the environment the agent starts
        from instead of the engine's).

        Each call adds another agent; the new agent id is returned. Multiple
        agents per worktree are supported.
        """
        worktree_path = params.get("worktree_path", "")
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
        env = params.get("env")
        if env is not None and not (
            isinstance(env, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        ):
            raise ValueError("env must be an object of string values")

        # Before the spawn, so every event of the new agent is after it.
        cursor = self._engine.events.cursor
        info = await asyncio.wait_for(
            self._engine.spawn_agent(
                worktree_path,
                instruction,
                skip_permissions=skip_permissions,
                resume_mode=resume_mode,
                label=label,
                env=env,
            ),
            timeout=_OP_TIMEOUT,
        )
        return _ok(
            request_id,
            {
                "worktree_path": worktree_path,
                "agent_id": info.agent_id,
                "status": "spawned",
                "cursor": cursor,
            },
        )

    async def handle_stop_agent(
        self, request_id: str, params: dict
    ) -> dict:
        """Stop an agent in a worktree. Params: worktree_path, agent_id (optional).

        ``agent_id`` may be omitted when the worktree has exactly one agent.
        """
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        agent_ids = self._engine.agent_ids(worktree_path)
        if not agent_ids:
            raise ValueError("No running agent in this worktree")

        agent_id = self._resolve_agent_id(agent_ids, params.get("agent_id"))
        self._engine.stop_agent(worktree_path, agent_id)
        return _ok(
            request_id,
            {"worktree_path": worktree_path, "agent_id": agent_id, "status": "stopped"},
        )

    async def handle_get_agent_status(
        self, request_id: str, params: dict
    ) -> dict:
        """Get agent status for a worktree. Params: worktree_path, agent_id
        (optional). ``agent_id`` may be omitted when there is one agent."""
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        bucket = self._engine.agent_states(worktree_path)
        if not bucket:
            return _ok(request_id, {
                "worktree_path": worktree_path,
                "agent_id": "",
                "status": AgentStatus.NO_AGENT.value,
                "detail": "",
            })

        agent_id = self._resolve_agent_id(list(bucket.keys()), params.get("agent_id"))
        state = bucket[agent_id]
        return _ok(request_id, {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "status": state.status.value,
            "confidence": state.confidence.value,
            "detail": state.detail,
        })

    async def handle_read_agent_output(
        self, request_id: str, params: dict
    ) -> dict:
        """Read recent terminal output from an agent. Params: worktree_path,
        agent_id (optional), lines (optional)."""
        worktree_path = params.get("worktree_path", "")
        num_lines = params.get("lines", 50)
        if not worktree_path:
            raise ValueError("worktree_path is required")

        agent_ids = self._engine.agent_ids(worktree_path)
        if not agent_ids:
            raise ValueError("No agent terminal in this worktree")

        agent_id = self._resolve_agent_id(agent_ids, params.get("agent_id"))
        lines, total_lines = self._engine.read_agent_output(
            worktree_path, agent_id, num_lines
        )

        return _ok(request_id, {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "lines": lines,
            "total_lines": total_lines,
        })

    async def handle_send_agent_input(
        self, request_id: str, params: dict
    ) -> dict:
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

        agent_id = self._resolve_agent_id(agent_ids, params.get("agent_id"))
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
        return _ok(request_id, result)

    async def handle_list_events(
        self, request_id: str, params: dict
    ) -> dict:
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
        return _ok(request_id, {
            "events": [e.to_dict() for e in events],
            "cursor": bus.cursor,
            "truncated": truncated,
        })

    async def handle_list_agents(
        self, request_id: str, params: dict
    ) -> dict:
        """List the agents in a worktree. Params: worktree_path.

        Returns ``{worktree_path, agents: [{agent_id, label, status,
        confidence, detail}]}``.
        """
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        return _ok(request_id, {
            "worktree_path": worktree_path,
            "agents": self._engine.agents_payload(worktree_path),
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_agent_id(
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
                    f"No {what} {agent_id!r} in this worktree. "
                    f"Available: {available}"
                )
            return agent_id
        if len(available) == 1:
            return available[0]
        if not available:
            raise ValueError(f"No running {what} in this worktree")
        raise ValueError(
            f"Multiple agents in this worktree; specify agent_id. "
            f"Available: {available}"
        )


async def start_ipc_server(engine: Engine) -> tuple[IpcServer, str]:
    """Factory: create and start an IPC server for the given engine.

    Returns ``(server, socket_path)``.
    Socket is created at ``/tmp/lazyagent-{pid}/ipc.sock`` with 0o700 dir
    and 0o600 socket perms so other users on the host cannot connect to it
    (the IPC handlers can spawn agents and remove worktrees, so an
    unauthenticated cross-user connection is privilege escalation).
    """
    socket_dir = os.path.join(tempfile.gettempdir(), f"lazyagent-{os.getpid()}")
    os.makedirs(socket_dir, exist_ok=True)
    # Re-apply mode in case the directory already existed with looser perms.
    os.chmod(socket_dir, 0o700)
    socket_path = os.path.join(socket_dir, "ipc.sock")

    # Remove stale socket if present
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    server = IpcServer(engine, socket_path)
    await server.start()
    # start_unix_server doesn't accept a mode arg; chmod after bind.
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    return server, socket_path
