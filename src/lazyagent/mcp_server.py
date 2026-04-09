"""MCP stdio server — spawned by Claude Code as a child process.

Exposes lazyagent primitives (worktree management, agent lifecycle) as MCP
tools. Communicates with the main lazyagent app via a Unix domain socket
whose path is read from the ``LAZYAGENT_SOCKET`` environment variable.

Run with: ``python3 -m lazyagent.mcp_server``
"""

from __future__ import annotations

import asyncio
import json
import os
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


def _get_client() -> IpcClient:
    socket_path = os.environ.get("LAZYAGENT_SOCKET")
    if not socket_path:
        raise RuntimeError(
            "LAZYAGENT_SOCKET environment variable is not set. "
            "This server must be started by lazyagent."
        )
    return IpcClient(socket_path)


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
async def create_worktree(branch: str, base_branch: str = "main") -> dict:
    """Create a new git worktree with a new branch.

    Args:
        branch: Name for the new branch.
        base_branch: Branch to base the new worktree on. Defaults to "main".

    Returns:
        Object with path and branch of the new worktree.
    """
    return await _get_client().call(
        "create_worktree",
        {"branch": branch, "base_branch": base_branch},
    )


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
    worktree_path: str, initial_prompt: str | None = None
) -> dict:
    """Spawn a coding agent in a worktree.

    The agent runs with skip_permissions=True. Only one agent can run
    per worktree at a time.

    Args:
        worktree_path: Absolute path of the worktree.
        initial_prompt: Optional initial prompt to send to the agent after it starts.

    Returns:
        Object with worktree_path and status.
    """
    params: dict[str, Any] = {"worktree_path": worktree_path}
    if initial_prompt is not None:
        params["initial_prompt"] = initial_prompt
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
