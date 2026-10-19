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
    branch: str, base_branch: str | None = None, extra: str = ""
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
        base_branch: Branch to base the new worktree on. Default: the repo's
            configured default branch when a worktree has it checked out,
            else the branch of the main worktree.
        extra: Optional string substituted for the ``{extra}`` placeholder
            in the custom create command template. Ignored (with a warning)
            when no custom command is configured. Note: this value is
            interpolated into a shell command — avoid characters that would
            break a shell template (``;``, ``$()``, backticks, etc.).

    Returns:
        Object with ``path``, ``branch``, optional ``custom_command`` and
        ``job_id`` (the engine job that ran the custom command), and
        optional ``warning`` describing any caveats.
    """
    params: dict[str, Any] = {"branch": branch}
    if base_branch:
        params["base_branch"] = base_branch
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
    notes_mode: str | None = None,
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
        notes_mode: How the agent may write its session notes: "manual"
            (only when the user asks), "propose" or "free" (directly).
            Default: the repo's ``[notes] mode``. Spawn workers you
            orchestrate with "free", so their notes tell you where they are.

    Returns:
        Object with worktree_path, agent_id, status, and ``cursor`` (event
        cursor taken before the spawn; pass it as ``since`` to list_events
        to see every status change of the new agent).
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
    if notes_mode is not None:
        params["notes_mode"] = notes_mode
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
async def stop_agent(
    worktree_path: str, agent_id: str | None = None, notes: str = "delete"
) -> dict:
    """Stop a running agent in a worktree.

    Args:
        worktree_path: Absolute path of the worktree.
        agent_id: Which agent to stop. May be omitted when the worktree has
            exactly one agent; required (else an error lists the choices) when
            there are several.
        notes: What happens to the agent's session notes: "delete" (default)
            or "keep" (kept a few days as an orphan the user can recover).

    Returns:
        Object confirming the agent was stopped (includes agent_id), and
        ``notes``: the agent's final session notes when they were not empty.
        Keep what matters from them (a PR description, a decisions log):
        with "delete" they are gone.
    """
    params: dict[str, Any] = {"worktree_path": worktree_path}
    if agent_id is not None:
        params["agent_id"] = agent_id
    if notes != "delete":
        params["notes"] = notes
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
    worktree_path: str,
    text: str,
    agent_id: str | None = None,
    confirm: bool = True,
) -> dict:
    """Send text to a running agent's terminal and submit it.

    Use this to give follow-up instructions, answer prompts, or approve
    actions without stopping and re-spawning the agent.

    By default the call confirms delivery: it waits (a few seconds at most)
    for the agent to start working on the text. ``delivery`` in the result is
    "confirmed" (the agent started a turn), "unconfirmed" (it did not react —
    the text may be sitting unsent in its prompt; check read_agent_output),
    "busy" (the agent was already working, so its reaction cannot be observed
    until the current turn ends) or "unchecked" (``confirm=False``).
    ``status_before`` says what the agent was doing; if it was blocked on a
    permission prompt or a question, a warning notes that the text answered
    that dialog rather than starting a new prompt.

    Args:
        worktree_path: Absolute path of the worktree.
        text: The text to send (it is submitted automatically).
        agent_id: Which agent to send to. May be omitted when the worktree has
            exactly one agent; required when there are several.
        confirm: Wait for the agent to start working on the text (default True).

    Returns:
        Object with agent_id, delivery, status_before, ``cursor`` (event
        cursor taken just before sending — pass it as ``since`` to
        list_events to see what the agent did with the text), and optional
        ``warnings``.
    """
    params: dict[str, Any] = {
        "worktree_path": worktree_path,
        "text": text,
        "confirm": confirm,
    }
    if agent_id is not None:
        params["agent_id"] = agent_id
    return await _get_client().call("send_agent_input", params)


@mcp.tool()
async def list_events(
    since: int | None = None,
    worktree_path: str | None = None,
    agent_id: str | None = None,
    statuses: str | list[str] | None = None,
) -> dict:
    """Read the agent event log: every status change of every agent.

    Never blocks. Each event has seq, time, worktree_path, agent_id, label,
    status, previous_status and detail. Keep the returned ``cursor`` and pass
    it back as ``since`` to get only newer events.

    Omitting ``since`` means "from now": it returns no events, just the
    current cursor to start from. The log keeps the last 1000 events;
    ``truncated`` is True if events after ``since`` were already dropped.

    Args:
        since: Event cursor to read after. Omit to start from now.
        worktree_path: Only events for this worktree.
        agent_id: Only events for this agent id.
        statuses: Only events whose new status is one of these (statuses or
            groups).

    Statuses: running, waiting, waiting_for_user, waiting_for_approval,
    completed, failed, interrupted, possibly_hanged, no_agent, and exited
    (agent gone). Groups: "idle" (turn finished, at its prompt), "blocked"
    (on a permission prompt or a question), "finished" (completed, failed,
    interrupted or exited), "not_running" (anything but running).

    Returns:
        Object with events (list, oldest first), cursor, and truncated. When
        agents were restored after ``since`` (restore_agents, or the engine's
        ``restore_on_start``), also ``restores``: one entry per restore with
        its seq, ``restored`` (id, worktree_path, agent_id, label, session_name,
        kind, resumed) and ``failed`` (id, reason). The filters do not apply
        to it.
    """
    params: dict[str, Any] = {}
    if since is not None:
        params["since"] = since
    if worktree_path is not None:
        params["worktree_path"] = worktree_path
    if agent_id is not None:
        params["agent_id"] = agent_id
    if statuses is not None:
        params["statuses"] = statuses
    return await _get_client().call("list_events", params)


@mcp.tool()
async def list_restorable() -> dict:
    """List the agents an earlier lazyagent engine ran when it ended (it was
    stopped, crashed, or the machine rebooted), which can be brought back.

    Read this after a restart before spawning agents again: restoring one
    resumes its own conversation, with the same worktree, agent id and
    session name, where a new spawn would start from scratch. Agents that
    already run in this engine (the orchestrator started again with
    --continue, an agent resuming the same conversation) are not listed.

    Returns:
        Object with ``agents``: each has ``id`` (what restore_agents takes),
        worktree_path, agent_id, kind ("agent" or "orchestrator"), provider,
        label, session_name, skip_permissions, conversation_id (null when the
        provider never reported one: the restore then resumes the provider's
        last conversation in that worktree), instruction (the last one sent
        to it, when known), started_at and ended_at.
    """
    return await _get_client().call("list_restorable", {})


@mcp.tool()
async def restore_agents(ids: list[str] | None = None) -> dict:
    """Bring back agents from list_restorable (all of them when ``ids`` is
    omitted). Each is spawned again in its worktree with its provider, label
    and permissions, the same agent id when it is free, and resumes its
    conversation.

    Args:
        ids: The ``id`` values from list_restorable to restore. Omit for all.

    Returns:
        Object with ``restored`` (id, worktree_path, agent_id, session_id,
        label, session_name, kind, and ``resumed``: "conversation" for the
        exact one, "last" for the provider's last in that worktree) and
        ``failed`` (id, reason; a failed agent stays restorable, except one
        refused because its conversation already runs here). The same
        object shows up in list_events as a ``restores`` entry.
    """
    params: dict[str, Any] = {}
    if ids is not None:
        params["ids"] = ids
    return await _get_client().call("restore_agents", params)


@mcp.tool()
async def dismiss_restorable(ids: list[str] | None = None) -> dict:
    """Stop offering agents from list_restorable (all of them when ``ids``
    is omitted), e.g. once their work was finished or redone another way.

    Returns:
        Object with ``dismissed``: the ids no longer offered.
    """
    params: dict[str, Any] = {}
    if ids is not None:
        params["ids"] = ids
    return await _get_client().call("dismiss_restorable", params)


# ------------------------------------------------------------------
# Session notes
# ------------------------------------------------------------------


def _notes_params(
    worktree_path: str | None, agent_id: str | None, **params: Any
) -> dict[str, Any]:
    """A notes call's params: the optional target, and who calls (this
    agent's token, when lazyagent spawned it), so no target = "my notes"."""
    if worktree_path is not None:
        params["worktree_path"] = worktree_path
    if agent_id is not None:
        params["agent_id"] = agent_id
    caller = os.environ.get("LAZYAGENT_AGENT_TOKEN")
    if caller:
        params["caller"] = caller
    return params


@mcp.tool()
async def notes_read(
    worktree_path: str | None = None, agent_id: str | None = None
) -> dict:
    """Read session notes: a short Markdown cheat sheet per agent (Goal,
    Constraints (don't touch), Decisions, Doubts, Pending) that the user,
    the agent and the orchestrator share. lazyagent keeps it outside the
    worktree and gives it back to the agent after a compaction, a /clear
    or a restore.

    Without arguments: your own notes (when lazyagent spawned you). With
    worktree_path (+ agent_id when it has several agents): that agent's,
    e.g. an orchestrator checking on a worker without reading its screen.

    Returns:
        Object with text, rev (pass it as if_rev to notes_replace_section),
        size, max_bytes, by (who wrote last: user, agent, orchestrator),
        updated_at, mode, instruction (the pinned first prompt), and the
        target's worktree_path and agent_id.
    """
    return await _get_client().call(
        "notes_get", _notes_params(worktree_path, agent_id)
    )


@mcp.tool()
async def notes_append(
    section: str,
    text: str,
    worktree_path: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """Add lines to a section of session notes (see notes_read). Each line
    becomes a list item; a missing section is created at the end.

    Write in your own notes only what the user agreed or asked to keep
    (goal, what not to touch, decisions, open doubts, pending steps): in
    "manual" mode (notes_read's ``mode``) write only when the user asks.
    Keep them short: they are re-read after every compaction. Above
    max_bytes (8 KiB by default) a write is refused: condense first.

    Without worktree_path / agent_id: your own notes. An orchestrator may
    write a worker's; an agent may not write another agent's.

    Args:
        section: The heading as in notes_read (case does not matter), e.g.
            "Decisions", "Doubts", "Pending", "Constraints (don't touch)".
        text: One or more lines ("- [ ] ..." for a checklist item).
        worktree_path: Another agent's worktree (orchestrator only).
        agent_id: Which agent there, when it has several.

    Returns:
        Object with rev, size, max_bytes, by, and ``warning`` from 80 % full.
    """
    return await _get_client().call(
        "notes_append",
        _notes_params(worktree_path, agent_id, section=section, text=text),
    )


@mcp.tool()
async def notes_replace_section(
    section: str,
    text: str,
    if_rev: int,
    worktree_path: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """Replace the body of one section of session notes (see notes_read),
    e.g. to tick "- [x]" items under Pending or condense Decisions. The
    whole file is never rewritten this way: other sections stay as they are.

    Same rules as notes_append (manual mode: only when the user asks; the
    size cap). ``if_rev`` is the rev from your last notes_read: if someone
    edited since, the call fails with CONFLICT ("notes changed"): read them
    again and redo the change on the new text.

    Args:
        section: The heading whose body is replaced.
        text: The new body (Markdown lines; may be empty).
        if_rev: The rev the change is based on.
        worktree_path: Another agent's worktree (orchestrator only).
        agent_id: Which agent there, when it has several.

    Returns:
        Object with the new rev, size, max_bytes, by, and ``warning``.
    """
    return await _get_client().call(
        "notes_replace_section",
        _notes_params(
            worktree_path, agent_id, section=section, text=text, if_rev=if_rev
        ),
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
