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
from lazyagent.models import AgentStatus
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
            state = self._app._agent_states.get(wt.path)
            git_st = self._app._git_statuses.get(wt.path)
            entry: dict[str, Any] = {
                "path": wt.path,
                "branch": wt.branch,
                "head": wt.head,
                "is_main": wt.is_main,
                "is_bare": wt.is_bare,
                "agent_status": state.status.value if state else AgentStatus.NO_AGENT.value,
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

            # The custom command needs a terminal to run in. Without a
            # selected worktree, _send_to_terminal silently no-ops via a
            # warning notify and the caller would get "success" for an
            # operation that never happened. Reject explicitly.
            if self._app._get_selected_worktree() is None:
                raise ValueError(
                    "Custom create command requires a selected worktree to "
                    "dispatch into; none is currently selected in the UI"
                )

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
            self._app.call_later(self._app._send_to_terminal, cmd)
            self._app.call_later(self._app._load_worktrees)
            return _ok(request_id, {
                "path": wt_path,
                "branch": branch,
                "custom_command": True,
                "warning": (
                    "Path is predicted, not verified. The custom create "
                    "command runs asynchronously in the selected worktree's "
                    "terminal. Poll list_worktrees to confirm the worktree "
                    "exists before calling spawn_agent on it."
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

        # Reject if agent is running
        state = self._app._agent_states.get(worktree_path)
        if state and state.status in (AgentStatus.RUNNING, AgentStatus.WAITING):
            raise ValueError("Agent is running in this worktree — stop it first")

        manager = WorktreeManager(self._app._repo_root)
        await asyncio.to_thread(manager.remove, worktree_path, force)

        self._app._agent_states.pop(worktree_path, None)
        self._app.call_later(self._app._load_worktrees)

        return _ok(request_id, {"removed": worktree_path})

    async def handle_spawn_agent(
        self, request_id: str, params: dict
    ) -> dict:
        """Spawn an agent in a worktree. Params: worktree_path, instruction (optional),
        skip_permissions (optional), resume_mode (optional)."""
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        wt_info = self._find_worktree(worktree_path)
        if wt_info is None:
            raise ValueError(f"Unknown worktree: {worktree_path}")

        # Reject if agent already running
        state = self._app._agent_states.get(worktree_path)
        if state and state.status in (AgentStatus.RUNNING, AgentStatus.WAITING):
            raise ValueError("Agent is already running in this worktree")

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        instruction = params.get("instruction") or params.get("initial_prompt")
        skip_permissions = params.get("skip_permissions", True)
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
                panel = center.ensure_panel(worktree_path)
                await panel.spawn_agent(
                    skip_permissions=skip_permissions,
                    agent_provider=self._app._config.agent.provider,
                    resume_mode=resume_mode,
                    socket_path=self._socket_path,
                    instruction=instruction,
                )
                future.set_result(None)
            except Exception as e:
                future.set_exception(e)

        self._app.call_later(_do_spawn)

        await asyncio.wait_for(future, timeout=_TEXTUAL_OP_TIMEOUT)
        return _ok(request_id, {"worktree_path": worktree_path, "status": "spawned"})

    async def handle_stop_agent(
        self, request_id: str, params: dict
    ) -> dict:
        """Stop the agent in a worktree. Params: worktree_path."""
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        from lazyagent.widgets.center_panel import CenterPanel

        center = self._app.query_one(CenterPanel)
        panel = center.get_panel(worktree_path)
        if panel is None or not panel.has_agent:
            raise ValueError("No running agent in this worktree")

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def _do_stop() -> None:
            try:
                from lazyagent.widgets.worktree_list import WorktreeList

                state = self._app._get_agent_state(worktree_path)
                state.status = AgentStatus.NO_AGENT
                state.last_output_time = None
                self._app.query_one(WorktreeList).update_agent_state(
                    worktree_path, state
                )
                await panel.cleanup_agent()
                future.set_result(None)
            except Exception as e:
                future.set_exception(e)

        self._app.call_later(_do_stop)

        await asyncio.wait_for(future, timeout=_TEXTUAL_OP_TIMEOUT)
        return _ok(request_id, {"worktree_path": worktree_path, "status": "stopped"})

    async def handle_get_agent_status(
        self, request_id: str, params: dict
    ) -> dict:
        """Get agent status for a worktree. Params: worktree_path."""
        worktree_path = params.get("worktree_path", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")

        state = self._app._agent_states.get(worktree_path)
        if state is None:
            return _ok(request_id, {
                "worktree_path": worktree_path,
                "status": AgentStatus.NO_AGENT.value,
                "detail": "",
            })

        return _ok(request_id, {
            "worktree_path": worktree_path,
            "status": state.status.value,
            "confidence": state.confidence.value,
            "detail": state.detail,
        })

    async def handle_read_agent_output(
        self, request_id: str, params: dict
    ) -> dict:
        """Read recent terminal output from an agent. Params: worktree_path, lines (optional)."""
        worktree_path = params.get("worktree_path", "")
        num_lines = params.get("lines", 50)
        if not worktree_path:
            raise ValueError("worktree_path is required")

        from lazyagent.widgets.center_panel import CenterPanel

        center = self._app.query_one(CenterPanel)
        panel = center.get_panel(worktree_path)
        if panel is None or panel.agent_terminal is None:
            raise ValueError("No agent terminal in this worktree")

        terminal = panel.agent_terminal
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
            "lines": collected,
            "total_lines": total_lines,
        })

    async def handle_send_agent_input(
        self, request_id: str, params: dict
    ) -> dict:
        """Send text input to a running agent's terminal. Params: worktree_path, text."""
        worktree_path = params.get("worktree_path", "")
        text = params.get("text", "")
        if not worktree_path:
            raise ValueError("worktree_path is required")
        if not text:
            raise ValueError("text is required and must not be empty")

        from lazyagent.widgets.center_panel import CenterPanel

        center = self._app.query_one(CenterPanel)
        panel = center.get_panel(worktree_path)
        if panel is None or not panel.has_agent:
            raise ValueError("No running agent in this worktree")

        terminal = panel.agent_terminal
        if terminal is None or terminal.send_queue is None:
            raise ValueError("Agent terminal is not ready")

        # Append CR (\r) so the agent's TUI treats this as Enter/submit.
        # Textual delivers Enter as event.character == "\r" in the UI key
        # handler; LF would be interpreted as Shift+Enter (newline within
        # the prompt) by Claude Code and most line-editing TUIs.
        await terminal.send_queue.put(["stdin", text + "\r"])
        return _ok(request_id, {"worktree_path": worktree_path, "status": "sent"})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_worktree(self, path: str):
        """Find a WorktreeInfo by path."""
        for wt in self._app.worktrees:
            if wt.path == path:
                return wt
        return None


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
