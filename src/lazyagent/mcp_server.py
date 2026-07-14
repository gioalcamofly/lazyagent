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
import socket as _socket
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


def _socket_alive(path: str) -> bool:
    """Return True if a Unix socket at ``path`` accepts a connection.

    A bare ``os.path.exists`` is not enough: a lazyagent crash leaves the
    socket file in place but with no listener, and a stale entry would
    poison ``_discover_socket`` until manual cleanup.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(path)
            return True
    except OSError:
        return False


def _walk_ancestors():
    """Yield this process's ancestor pids, parent first.

    Reads ``/proc/<pid>/status`` for the ``PPid:`` line — Linux only.
    Other platforms get an empty walk and fall back to the mtime heuristic.
    """
    try:
        pid = os.getppid()
    except OSError:
        return
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        yield pid
        try:
            with open(f"/proc/{pid}/status") as f:
                next_pid = 0
                for line in f:
                    if line.startswith("PPid:"):
                        try:
                            next_pid = int(line.split()[1])
                        except (IndexError, ValueError):
                            next_pid = 0
                        break
                if next_pid <= 0:
                    return
                pid = next_pid
        except OSError:
            return


def _discover_socket() -> str | None:
    """Find a running lazyagent IPC socket.

    Two-phase: first try to scope to the lazyagent that owns this MCP
    process by walking ancestor pids and checking ``/tmp/lazyagent-<pid>``.
    This prevents an MCP spawned by lazyagent A from accidentally driving
    lazyagent B (different repo) just because B's socket has a more recent
    mtime. Only if no ancestor matches do we fall back to "most recently
    modified live socket" — and we *connect* to verify it's alive, not
    just that the file exists.
    """
    tmp = tempfile.gettempdir()

    # Phase 1: ancestor-scoped lookup.
    for ancestor_pid in _walk_ancestors():
        candidate = os.path.join(tmp, f"lazyagent-{ancestor_pid}", "ipc.sock")
        if _socket_alive(candidate):
            return candidate

    # Phase 2: fall back to scanning the temp dir.
    pattern = os.path.join(tmp, "lazyagent-*/ipc.sock")
    candidates = glob.glob(pattern)

    def _safe_mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    candidates.sort(key=_safe_mtime, reverse=True)
    for path in candidates:
        if _socket_alive(path):
            return path
    return None


def _get_client() -> IpcClient:
    """Return a (possibly cached) client to the lazyagent IPC server.

    Invalidates the cache if the cached socket is no longer alive — this
    matters when the user restarts lazyagent while a long-running MCP
    process keeps running.
    """
    global _cached_client
    if _cached_client is not None and not _socket_alive(_cached_client._socket_path):
        _cached_client = None
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

    Two modes depending on the repo's ``.lazyagent.toml``:

    1. **No custom create command**: runs ``git worktree add`` synchronously.
       The returned ``path`` exists on disk when this call returns, and
       ``spawn_agent`` can be called on it immediately. ``extra`` is ignored
       (a ``warning`` field is included in the response so this is visible).

    2. **Custom create command** (``[worktree] create = "..."``): the
       configured command is executed as a subprocess in the repo root and
       awaited to completion before this call returns. No UI worktree
       selection is required. The response includes ``custom_command: true``
       and a ``warning`` field. The returned ``path`` is *predicted* from
       a naming convention (``<parent>/<repo>-<branch>``); if the custom
       script writes the worktree elsewhere, poll ``list_worktrees`` to find
       the real path before calling ``spawn_agent`` on it. If the custom
       command exits non-zero, the call fails with stderr/stdout in the
       error message.

    Args:
        branch: Name for the new branch.
        base_branch: Branch to base the new worktree on. Defaults to "main".
        extra: Optional string substituted for the ``{extra}`` placeholder
            in the custom create command template. Ignored (with a warning)
            when no custom command is configured. Note: this value is
            interpolated into a shell command — avoid characters that would
            break a shell template (``;``, ``$()``, backticks, etc.).

    Returns:
        Object with ``path``, ``branch``, optional ``custom_command``, and
        optional ``warning`` describing any caveats.
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
    label: str | None = None,
) -> dict:
    """Spawn a NEW coding agent in a worktree.

    A worktree can host multiple agents, each in its own tab. Every call to
    this tool adds another agent and returns its ``agent_id``; use that id with
    the other agent tools to address this specific agent. Omitting ``agent_id``
    on those tools works only when the worktree has exactly one agent.

    Args:
        worktree_path: Absolute path of the worktree.
        instruction: Optional initial instruction passed to the agent CLI.
        skip_permissions: If True (default), the agent runs in "dangerous" mode
            with permission prompts bypassed. Set to False for normal mode where
            the agent asks before sensitive actions.
        resume_mode: Session mode — "new" (default) starts a fresh session,
            "last" resumes the most recent session for this worktree. Interactive
            "pick from list" mode is not supported over MCP. **Combining
            ``resume_mode="last"`` with ``instruction``** resumes the prior
            session AND passes the instruction as a new turn — it does not
            replace the prior task. For a clean restart with new instructions,
            use the default ``resume_mode="new"``.
        label: Optional human-facing tab label (defaults to "Agent N").

    Returns:
        Object with worktree_path, agent_id, and status.
    """
    params: dict[str, Any] = {
        "worktree_path": worktree_path,
        "skip_permissions": skip_permissions,
        "resume_mode": resume_mode,
    }
    if instruction is not None:
        params["instruction"] = instruction
    if label is not None:
        params["label"] = label
    return await _get_client().call("spawn_agent", params)


@mcp.tool()
async def list_agents(worktree_path: str) -> dict:
    """List the agents running in a worktree.

    Args:
        worktree_path: Absolute path of the worktree.

    Returns:
        Object with worktree_path and an ``agents`` list; each entry has
        agent_id, label, status, confidence, and detail.
    """
    return await _get_client().call(
        "list_agents",
        {"worktree_path": worktree_path},
    )


@mcp.tool()
async def stop_agent(worktree_path: str, agent_id: str | None = None) -> dict:
    """Stop a running agent in a worktree.

    Args:
        worktree_path: Absolute path of the worktree.
        agent_id: Which agent to stop. May be omitted when the worktree has
            exactly one agent; required (else an error lists the choices) when
            there are several.

    Returns:
        Object confirming the agent was stopped (includes agent_id).
    """
    params: dict[str, Any] = {"worktree_path": worktree_path}
    if agent_id is not None:
        params["agent_id"] = agent_id
    return await _get_client().call("stop_agent", params)


@mcp.tool()
async def get_agent_status(worktree_path: str, agent_id: str | None = None) -> dict:
    """Get the current status of an agent in a worktree.

    Args:
        worktree_path: Absolute path of the worktree.
        agent_id: Which agent to query. May be omitted when the worktree has
            exactly one agent; required when there are several.

    Returns:
        Object with agent_id, status, confidence, and detail fields.
    """
    params: dict[str, Any] = {"worktree_path": worktree_path}
    if agent_id is not None:
        params["agent_id"] = agent_id
    return await _get_client().call("get_agent_status", params)


@mcp.tool()
async def read_agent_output(
    worktree_path: str, lines: int = 50, agent_id: str | None = None
) -> dict:
    """Read recent terminal output from an agent in a worktree.

    Includes both scrollback history and the current live screen buffer.

    Args:
        worktree_path: Absolute path of the worktree.
        lines: Number of recent lines to return (default 50).
        agent_id: Which agent to read. May be omitted when the worktree has
            exactly one agent; required when there are several.

    Returns:
        Object with lines (list of strings), total_lines count, worktree_path,
        and agent_id.
    """
    params: dict[str, Any] = {"worktree_path": worktree_path, "lines": lines}
    if agent_id is not None:
        params["agent_id"] = agent_id
    return await _get_client().call("read_agent_output", params)


@mcp.tool()
async def send_agent_input(
    worktree_path: str, text: str, agent_id: str | None = None
) -> dict:
    """Send text input to a running agent's terminal.

    Use this to provide follow-up instructions, answer prompts, or approve
    actions without stopping and re-spawning the agent.

    Args:
        worktree_path: Absolute path of the worktree.
        text: The text to send to the agent's stdin (a newline is appended automatically).
        agent_id: Which agent to send to. May be omitted when the worktree has
            exactly one agent; required when there are several.

    Returns:
        Object confirming the input was sent (includes agent_id).
    """
    params: dict[str, Any] = {"worktree_path": worktree_path, "text": text}
    if agent_id is not None:
        params["agent_id"] = agent_id
    return await _get_client().call("send_agent_input", params)


if __name__ == "__main__":
    mcp.run(transport="stdio")
