"""MCP stdio server — spawned by Claude Code as a child process.

Exposes lazyagent primitives (worktree management, agent lifecycle) as MCP
tools. Communicates with the main lazyagent app via a Unix domain socket
whose path is read from the ``LAZYAGENT_SOCKET`` environment variable.

Run with: ``python3 -m lazyagent.mcp_server``
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import tempfile
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lazyagent")


class IpcClient:
    """Thin client that talks to the lazyagent IPC server over a Unix socket.

    Opens a fresh connection per call (stateless).
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path

    async def call(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON request and return the result (or raise on error)."""
        reader, writer = await asyncio.open_unix_connection(self._socket_path)
        try:
            request = {
                "id": str(uuid.uuid4()),
                "method": method,
                "params": params or {},
            }
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()

            line = await reader.readline()
            if not line:
                raise ConnectionError("IPC server closed the connection")

            response = json.loads(line)
            if "error" in response:
                err = response["error"]
                raise RuntimeError(f"[{err.get('code', 'ERROR')}] {err.get('message', '')}")
            return response.get("result")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass


_cached_client: IpcClient | None = None


def _discover_socket() -> str | None:
    """Find a running lazyagent IPC socket in the default temp directory."""
    pattern = os.path.join(tempfile.gettempdir(), "lazyagent-*/ipc.sock")
    candidates = glob.glob(pattern)
    # Return the most recently modified socket (likely the active instance)
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _get_client() -> IpcClient:
    global _cached_client
    if _cached_client is None:
        socket_path = os.environ.get("LAZYAGENT_SOCKET") or _discover_socket()
        if not socket_path:
            raise RuntimeError(
                "No running lazyagent instance found. "
                "Start lazyagent first, or set LAZYAGENT_SOCKET."
            )
        _cached_client = IpcClient(socket_path)
    return _cached_client


# ------------------------------------------------------------------
# MCP Tools
# ------------------------------------------------------------------


@mcp.tool()
async def list_worktrees() -> list[dict]:
    """List all git worktrees with their agent status and git status.

    Returns a list of worktree objects with fields: path, branch, head,
    is_main, is_bare, agent_status, and optionally git_status.
    """
    return await _get_client().call("list_worktrees")


@mcp.tool()
async def create_worktree(
    branch: str, base_branch: str = "main", extra: str = ""
) -> dict:
    """Create a new git worktree with a new branch.

    If the repo has a custom create command configured in ``.lazyagent.toml``
    (``[worktree] create = "..."``), that command is run transparently —
    same as when a user creates a worktree from the UI. This lets post-create hooks
    (e.g. ``bun install``, copying ``.env``) run consistently.

    Args:
        branch: Name for the new branch.
        base_branch: Branch to base the new worktree on. Defaults to "main".
        extra: Optional string substituted for the ``{extra}`` placeholder in
            the custom create command template. Ignored when no custom command
            is configured.

    Returns:
        Object with path and branch of the new worktree.
    """
    params: dict[str, Any] = {"branch": branch, "base_branch": base_branch}
    if extra:
        params["extra"] = extra
    return await _get_client().call("create_worktree", params)


@mcp.tool()
async def remove_worktree(worktree_path: str, force: bool = False) -> dict:
    """Remove a git worktree.

    Cannot remove the main worktree or a worktree with a running agent.

    Args:
        worktree_path: Absolute path of the worktree to remove.
        force: If True, force removal even if there are uncommitted changes.

    Returns:
        Object confirming the removal.
    """
    return await _get_client().call(
        "remove_worktree",
        {"worktree_path": worktree_path, "force": force},
    )


@mcp.tool()
async def spawn_agent(
    worktree_path: str,
    instruction: str | None = None,
    skip_permissions: bool = True,
    resume_mode: str = "new",
) -> dict:
    """Spawn a coding agent in a worktree.

    Only one agent can run per worktree at a time.

    Args:
        worktree_path: Absolute path of the worktree.
        instruction: Optional initial instruction passed to the agent CLI.
        skip_permissions: If True (default), the agent runs in "dangerous" mode
            with permission prompts bypassed. Set to False for normal mode where
            the agent asks before sensitive actions.
        resume_mode: Session mode — "new" (default) starts a fresh session,
            "last" resumes the most recent session for this worktree. Interactive
            "pick from list" mode is not supported over MCP.

    Returns:
        Object with worktree_path and status.
    """
    params: dict[str, Any] = {
        "worktree_path": worktree_path,
        "skip_permissions": skip_permissions,
        "resume_mode": resume_mode,
    }
    if instruction is not None:
        params["instruction"] = instruction
    return await _get_client().call("spawn_agent", params)


@mcp.tool()
async def stop_agent(worktree_path: str) -> dict:
    """Stop the running agent in a worktree.

    Args:
        worktree_path: Absolute path of the worktree.

    Returns:
        Object confirming the agent was stopped.
    """
    return await _get_client().call(
        "stop_agent",
        {"worktree_path": worktree_path},
    )


@mcp.tool()
async def get_agent_status(worktree_path: str) -> dict:
    """Get the current status of the agent in a worktree.

    Args:
        worktree_path: Absolute path of the worktree.

    Returns:
        Object with status, confidence, and detail fields.
    """
    return await _get_client().call(
        "get_agent_status",
        {"worktree_path": worktree_path},
    )


@mcp.tool()
async def read_agent_output(worktree_path: str, lines: int = 50) -> dict:
    """Read recent terminal output from the agent in a worktree.

    Includes both scrollback history and the current live screen buffer.

    Args:
        worktree_path: Absolute path of the worktree.
        lines: Number of recent lines to return (default 50).

    Returns:
        Object with lines (list of strings), total_lines count, and worktree_path.
    """
    return await _get_client().call(
        "read_agent_output",
        {"worktree_path": worktree_path, "lines": lines},
    )


@mcp.tool()
async def send_agent_input(worktree_path: str, text: str) -> dict:
    """Send text input to a running agent's terminal.

    Use this to provide follow-up instructions, answer prompts, or approve
    actions without stopping and re-spawning the agent.

    Args:
        worktree_path: Absolute path of the worktree.
        text: The text to send to the agent's stdin (a newline is appended automatically).

    Returns:
        Object confirming the input was sent.
    """
    return await _get_client().call(
        "send_agent_input",
        {"worktree_path": worktree_path, "text": text},
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
