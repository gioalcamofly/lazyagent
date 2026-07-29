"""IPC server — Unix domain socket server running inside the Textual app.

Receives JSON requests from the MCP server process, dispatches to handlers
that operate on the ``LazyAgent`` app instance.

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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent.agent_providers import ResumeMode
from lazyagent.models import AgentState, AgentStatus, rollup_status
from lazyagent.worktree_manager import WorktreeManager, WorktreeManagerError

if TYPE_CHECKING:
    from lazyagent.app import LazyAgent

log = logging.getLogger(__name__)

# How long we wait for Textual-thread operations (spawn/stop agent)
_TEXTUAL_OP_TIMEOUT = 30.0


def _ok(request_id: str, result: Any) -> dict:
    return {"id": request_id, "result": result}


def _err(request_id: str, code: str, message: str) -> dict:
    return {"id": request_id, "error": {"code": code, "message": message}}


class IpcServer:
    """Asyncio Unix domain socket server bound to the Textual app."""

    def __init__(self, app: LazyAgent, socket_path: str) -> None:
        self._app = app
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
        result = []
        for wt in self._app.worktrees:
            bucket = self._agent_bucket(wt.path)
            git_st = self._app._git_statuses.get(wt.path)
            rolled = rollup_status(s.status for s in bucket.values())
            entry: dict[str, Any] = {
                "path": wt.path,
                "branch": wt.branch,
                "head": wt.head,
                "is_main": wt.is_main,
                "is_bare": wt.is_bare,
                # Scalar roll-up kept for back-compat; `agents` has the detail.
                "agent_status": rolled.value,
                "agents": self._agents_payload(bucket),
            }
            if git_st:
                entry["git_status"] = {
                    "dirty_count": git_st.dirty_count,
                    "ahead": git_st.ahead,
                    "behind": git_st.behind,
                }
            result.append(entry)
        return _ok(request_id, result)

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
        - **No custom create command**: runs ``git worktree add``
          synchronously and returns the actual path. ``extra`` is ignored
          (with a warning in the response so callers notice).
        - **Custom create command** (``[worktree] create = "..."`` in
          ``.lazyagent.toml``): the command is dispatched into the user's
          currently-selected worktree terminal and runs asynchronously.
          ``path`` in the response is *predicted* from the modal's naming
          convention (``<parent>/<repo>-<branch>``); the caller MUST poll
          ``list_worktrees`` to confirm the worktree actually appeared
          before calling ``spawn_agent`` on it. ``custom_command: true``
          flags this contract. If no worktree is selected in the UI the
          call fails with VALIDATION_ERROR rather than silently dropping
          the command.
        """
        branch = params.get("branch", "").strip()
        if not branch:
            raise ValueError("branch is required and must not be empty")
        base_branch = params.get("base_branch", "main")
        extra = params.get("extra", "")

        if self._app._config.has_custom_create:
            from lazyagent.config import format_command

            repo_root = self._app._repo_root
            repo_name = os.path.basename(repo_root) if repo_root else ""
            wt_name = f"{repo_name}-{branch}" if repo_name else branch
            wt_path = str(
                os.path.join(os.path.dirname(repo_root), wt_name)
                if repo_root
                else wt_name
            )
            cmd = format_command(
                self._app._config.worktree.create,  # type: ignore[arg-type]
                branch=branch,
                name=wt_name,
                base=base_branch,
                path=wt_path,
                repo=repo_root,
                extra=extra,
            )
            # Run the custom command directly via subprocess so MCP callers
            # don't need a worktree selected in the UI (which was previously
            # required because we dispatched the command into a worktree's
            # terminal pane). cwd defaults to repo_root; the template can
            # still `cd` if it wants. Output is captured and surfaced on
            # failure so the caller knows what went wrong.
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=repo_root or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await proc.communicate()
            if proc.returncode != 0:
                stderr = stderr_b.decode(errors="replace").strip()
                stdout = stdout_b.decode(errors="replace").strip()
                detail = stderr or stdout or f"exit code {proc.returncode}"
                raise WorktreeManagerError(
                    f"Custom create command failed: {detail}"
                )

            self._app.call_later(self._app._load_worktrees)
            return _ok(request_id, {
                "path": wt_path,
                "branch": branch,
                "custom_command": True,
                "warning": (
                    "Path is predicted from the naming convention "
                    "(<parent>/<repo>-<branch>), not parsed from the create "
                    "command output. If the custom script writes elsewhere, "
                    "poll list_worktrees to find the real path before calling "
                    "spawn_agent on it."
                ),
            })

        # Plain ``git worktree add`` path. Surface a warning if extra was
        # passed but cannot be honoured — easier for an LLM caller to
        # notice than a silent drop.
        result: dict[str, Any] = {}
        if extra:
            result["warning"] = (
                "extra= was provided but this repo has no custom create "
                "command configured; the value was ignored."
            )

        manager = WorktreeManager(self._app._repo_root)
        new_path = await asyncio.to_thread(manager.create, branch, base_branch)

        # Refresh the worktree list on the Textual thread
        self._app.call_later(self._app._load_worktrees)

        result["path"] = new_path
        result["branch"] = branch
        return _ok(request_id, result)

    async def handle_remove_worktree(
        self, request_id: str, params: dict
    ) -> dict:
        """Remove a worktree. Params: worktree_path, force (optional)."""
        worktree_path = params.get("worktree_path", "")
        force = params.get("force", False)

        if not worktree_path:
            raise ValueError("worktree_path is required")

        # Reject main worktree
        wt_info = self._find_worktree(worktree_path)
        if wt_info is None:
            raise ValueError(f"Unknown worktree: {worktree_path}")
        if wt_info.is_main:
            raise ValueError("Cannot remove the main worktree")

        # Reject if any agent is running
        bucket = self._agent_bucket(worktree_path)
        if any(
            s.status in (AgentStatus.RUNNING, AgentStatus.WAITING)
            for s in bucket.values()
        ):
            raise ValueError("Agent is running in this worktree — stop it first")

        manager = WorktreeManager(self._app._repo_root)
        await asyncio.to_thread(manager.remove, worktree_path, force)

        self._app._agent_states.pop(worktree_path, None)
        self._app.call_later(self._app._load_worktrees)

        return _ok(request_id, {"removed": worktree_path})

    async def handle_spawn_agent(
        self, request_id: str, params: dict
    ) -> dict:
        """Spawn a NEW agent in a worktree. Params: worktree_path, instruction
        (optional), skip_permissions (optional), resume_mode (optional).

        Each call adds another agent; the new agent id is returned. Multiple
        agents per worktree are supported.
        """
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        wt_info = self._find_worktree(worktree_path)
        if wt_info is None:
            raise ValueError(f"Unknown worktree: {worktree_path}")

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
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

        async def _do_spawn() -> None:
            try:
                from lazyagent.widgets.center_panel import CenterPanel

                center = self._app.query_one(CenterPanel)
                panel = await center.ensure_panel(worktree_path)
                agent_id = await panel.spawn_agent(
                    skip_permissions=skip_permissions,
                    agent_provider=self._app._config.agent.provider,
                    resume_mode=resume_mode,
                    socket_path=self._socket_path,
                    instruction=instruction,
                    label=label,
                )
                state = self._app._get_agent_state(worktree_path, agent_id)
                state.label = panel.agent_label(agent_id)
                self._app._refresh_sidebar_agents(worktree_path)
                future.set_result(agent_id)
            except Exception as e:
                future.set_exception(e)

        self._app.call_later(_do_spawn)

        agent_id = await asyncio.wait_for(future, timeout=_TEXTUAL_OP_TIMEOUT)
        return _ok(
            request_id,
            {"worktree_path": worktree_path, "agent_id": agent_id, "status": "spawned"},
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

        from lazyagent.widgets.center_panel import CenterPanel

        center = self._app.query_one(CenterPanel)
        panel = center.get_panel(worktree_path)
        if panel is None or not panel.agent_ids:
            raise ValueError("No running agent in this worktree")

        agent_id = self._resolve_agent_id(panel.agent_ids, params.get("agent_id"))

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def _do_stop() -> None:
            try:
                self._app._drop_agent_state(worktree_path, agent_id)
                self._app._refresh_sidebar_agents(worktree_path)
                await panel.cleanup_agent(agent_id)
                future.set_result(None)
            except Exception as e:
                future.set_exception(e)

        self._app.call_later(_do_stop)

        await asyncio.wait_for(future, timeout=_TEXTUAL_OP_TIMEOUT)
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

        bucket = self._agent_bucket(worktree_path)
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

        from lazyagent.widgets.center_panel import CenterPanel

        center = self._app.query_one(CenterPanel)
        panel = center.get_panel(worktree_path)
        if panel is None or not panel.agent_ids:
            raise ValueError("No agent terminal in this worktree")

        agent_id = self._resolve_agent_id(panel.agent_ids, params.get("agent_id"))
        terminal = panel.get_agent(agent_id)
        if terminal is None:
            raise ValueError("No agent terminal in this worktree")
        screen = terminal._screen
        total_lines = len(screen.scrollback) + screen.lines

        def _row_text(row_data: dict) -> str:
            return "".join(
                row_data.get(x, screen.default_char).data
                for x in range(screen.columns)
            ).rstrip()

        # Collect only the last num_lines by iterating from the end:
        # first the live screen buffer (bottom), then scrollback (top).
        collected: list[str] = []
        remaining = min(num_lines, total_lines)

        # Live screen lines (bottom-up)
        for row in range(screen.lines - 1, -1, -1):
            if remaining <= 0:
                break
            collected.append(_row_text(screen.buffer[row]))
            remaining -= 1

        # Scrollback lines (bottom-up)
        if remaining > 0:
            for row_data in reversed(screen.scrollback):
                if remaining <= 0:
                    break
                collected.append(_row_text(row_data))
                remaining -= 1

        collected.reverse()

        return _ok(request_id, {
            "worktree_path": worktree_path,
            "agent_id": agent_id,
            "lines": collected,
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

        from lazyagent.widgets.center_panel import CenterPanel

        center = self._app.query_one(CenterPanel)
        panel = center.get_panel(worktree_path)
        if panel is None or not panel.agent_ids:
            raise ValueError("No running agent in this worktree")

        agent_id = self._resolve_agent_id(panel.agent_ids, params.get("agent_id"))
        terminal = panel.get_agent(agent_id)
        if (
            terminal is None
            or terminal.emulator is None
            or terminal.send_queue is None
        ):
            raise ValueError("Agent terminal is not ready")

        # send_input, not a raw queue put: the text and the Enter that
        # submits it have to reach the agent CLI as separate reads, or the
        # CLI parses the trailing CR as a character inside the message and
        # the prompt just sits there unsent.
        await terminal.send_input(text, submit=True)
        return _ok(
            request_id,
            {"worktree_path": worktree_path, "agent_id": agent_id, "status": "sent"},
        )

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

        bucket = self._agent_bucket(worktree_path)
        return _ok(request_id, {
            "worktree_path": worktree_path,
            "agents": self._agents_payload(bucket),
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_worktree(self, path: str):
        """Find a WorktreeInfo by path."""
        for wt in self._app.worktrees:
            if wt.path == path:
                return wt
        return None

    def _agent_bucket(self, worktree_path: str) -> dict[str, AgentState]:
        """Agent-state map for a worktree (may be empty)."""
        return self._app._agent_states.get(worktree_path, {})

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

    @staticmethod
    def _agents_payload(bucket: dict[str, AgentState]) -> list[dict]:
        """Serialise a worktree's agent states for IPC responses."""
        return [
            {
                "agent_id": aid,
                "label": st.label,
                "status": st.status.value,
                "confidence": st.confidence.value,
                "detail": st.detail,
            }
            for aid, st in bucket.items()
        ]


async def start_ipc_server(app: LazyAgent) -> tuple[IpcServer, str]:
    """Factory: create and start an IPC server for the given app.

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

    server = IpcServer(app, socket_path)
    await server.start()
    # start_unix_server doesn't accept a mode arg; chmod after bind.
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass
    return server, socket_path
