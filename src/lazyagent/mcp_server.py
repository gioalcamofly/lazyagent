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


# The longest reply line read (asyncio's default is 64 KiB; rules_catalog
# is larger).
REPLY_LIMIT = 16 * 1024 * 1024


class IpcClient:
    """Thin client that talks to the lazyagent IPC server over a Unix socket.

    Opens a fresh connection per call (stateless).
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path

    async def call(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON request and return the result (or raise on error)."""
        reader, writer = await asyncio.open_unix_connection(
            self._socket_path, limit=REPLY_LIMIT
        )
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
        _add_caller({"worktree_path": worktree_path, "force": force}),
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

    The engine caps the spawns an agent (you included) asks for: at most
    ``max_concurrent_agents`` running and ``max_spawns_per_day`` in 24 h
    (``[orchestrator]`` in .lazyagent.toml; 8 and 30 by default). Over a cap
    the call fails saying which one: stop an agent, or ask the user.

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
    _add_caller(params)
    return await _get_client().call("spawn_agent", params)


def _add_caller(params: dict[str, Any]) -> dict[str, Any]:
    """Who calls: this agent's token, when lazyagent spawned it."""
    caller = os.environ.get("LAZYAGENT_AGENT_TOKEN")
    if caller:
        params["caller"] = caller
    return params


@mcp.tool()
async def status_list(include_done: bool = False) -> dict:
    """The status of every work item (a branch with its worktree, agents,
    PR and CI), as the engine derives it: who has the ball (you, agent,
    others, stalled, done), a short fact as the state, and its age. Items
    that need the user come first (level now, then round).

    When the user asks for status ("status table", "where is everything",
    "what needs me"), call this once and show ``table`` as it is, then add
    only what it does not say. Do not build your own table from
    list_worktrees, list_agents or read_agent_output.

    Args:
        include_done: Also list merged / closed branches and stale ones
            (nothing but an old branch).

    Returns:
        Object with ``table`` (plain text, ready to show), ``rows`` (key,
        branch, worktree_path, title, urls, zone, level, ball, state,
        agents, pr, note, changed_at, ...), ``counts`` (now, round,
        in_flight, done, stale) and ``generated_at``.
    """
    from lazyagent.engine.status import format_table

    params = _add_caller({"include_done": include_done})
    try:
        # The "CI red · unrelated" note beside the state (R4).
        result = await _get_client().call("status_list", {**params, "fields": ["ci_note"]})
    except RuntimeError as e:
        if "fields must be" not in str(e):
            raise
        # An engine before R4.
        result = await _get_client().call("status_list", params)
    result["table"] = format_table(result, result.get("generated_at"))
    return result


@mcp.tool()
async def rules_explain(key: str) -> dict:
    """Why a work item's row is in its state, from the engine's rules: the
    rule that matched (built-in, or the user's global or repo rules.toml,
    with its file and line), the facts it read (value, source), and for
    each rule above it the first condition that was not true. An unknown
    fact never matches.

    When the user asks "why is X in Round?", "why no Merge on X?" or "why
    is this stalled?", call this and answer from it; do not guess from the
    PR or the logs.

    Args:
        key: The row's key from status_list (``repo:branch``), or the
            branch name.

    Returns:
        Object with ``text`` (plain text, ready to show), ``rule``,
        ``then`` (ball, level, state, verb), ``facts``, ``above`` and
        ``items`` (the row's open inbox items; ``item_rule`` when the top
        one is a rule's, like Merge).
    """
    from lazyagent.engine.rules.export import format_explain

    result = await _get_client().call("rules_explain", {"key": key})
    result["text"] = format_explain(result)
    return result


@mcp.tool()
async def rules_catalog() -> dict:
    """The engine's rules catalog: every fact a rule may read (type,
    values, what it is, source, cost), every row rule in order with where
    it comes from (built-in, the user's global or repo rules.toml) and
    what it replaces, the inbox kinds, the actions with their keys, the
    verbs, the rule files and their errors.

    Use it to explain how lazyagent decides a row, or before helping the
    user write a rule in rules.toml (you never write the file: the user
    does, or accepts a proposal).

    Returns:
        Object with ``facts``, ``functions``, ``rules``, ``kinds``,
        ``actions``, ``verbs``, ``files``, ``errors`` and ``warnings``.
    """
    return await _get_client().call("rules_catalog", {})


@mcp.tool()
async def actionables_list(include_hidden: bool = False) -> dict:
    """The user's inbox: what needs the user now, as the engine derives it
    (an agent waiting for approval or an answer, a failed / hung / exited
    agent, an agent done with its turn, a red or conflicting PR with no
    agent at work, changes requested, a failed job). Level ``now`` (an
    agent is blocked) comes first, then ``round``. Items the user dismissed
    or parked are left out.

    Use it to see what is waiting on the user; ``status_list`` gives the
    same per work item (``needs_you``), with everything else in flight.

    Args:
        include_hidden: Also the parked items (with ``park`` set).

    Returns:
        Object with ``table`` (plain text, ready to show), ``items`` (id,
        kind, level, title, label, detail, state, work_item, worktree_path,
        agent_id, pr, since, ...), ``counts`` (now, round, parked) and
        ``generated_at``.
    """
    from lazyagent.engine.actionables import format_items

    result = await _get_client().call(
        "actionables_list", {"include_hidden": include_hidden}
    )
    result["table"] = format_items(result, result.get("generated_at"))
    return result


@mcp.tool()
async def orchestrator_release(key: str, outcome: str, note: str = "") -> dict:
    """End your claim on a work item the user passed to you ("Pass to
    orchestrator"; its status_list row has ``claim`` set and reads
    "orchestrator · looking"), or your own hold (``orchestrator_hold``, or
    the engine's hold on a worker you spawned; the row has ``hold`` set).
    Orchestrator only.

    Call it once you have dealt with the item (``outcome="done"``), or to
    hand it back to the user (``outcome="returned"`` with a short ``note``
    saying what they need to decide; it shows in their Round). Either way
    the note shows on the row ("orchestrator: <note>") and on its timeline,
    so always say in a few words what you did. A claim or hold you never
    release lapses at its ``until`` and goes back to the user. Tool
    approvals of the item stay with the user.

    Args:
        key: The work item's key (``status_list`` rows, e.g. ``repo:branch``).
        outcome: ``done`` or ``returned``.
        note: What you did, or what the user needs to decide (≤ 200 chars).

    Returns:
        Object with key, outcome and the ended ``claim`` (or ``hold``);
        outcome ``nothing_to_release`` when nothing holds it any more
        (released, lapsed, its worktree removed): not an error.
    """
    params = _add_caller({"key": key, "outcome": outcome, "note": note})
    return await _get_client().call("orchestrator_release", params)


@mcp.tool()
async def orchestrator_hold(key: str, note: str, ttl_s: int = 7200) -> dict:
    """Hold a work item yourself while you verify it or wait on something
    (a merge the user will do, a CI run): its row reads "orchestrator ·
    <note>" in flight and never reaches the user's Round. Orchestrator
    only.

    Use it when you take an item on without the user passing it to you,
    e.g. verifying a worker's finished branch before telling the user it is
    ready, or waiting for the user's merge before starting the next task.
    It widens nothing: questions and briefs still follow your mandate, and
    tool approvals stay with the user. End it with ``orchestrator_release``
    (done, or returned with a note); past ``ttl_s`` it goes to the user as
    "orchestrator did not respond". Holding again renews it.

    Args:
        key: The work item's key (``status_list`` rows).
        note: What you are doing, in a few words (≤ 60 chars), e.g.
            "verifying tests", "waiting on the merge".
        ttl_s: How long (seconds, 60-28800; default 7200).

    Returns:
        Object with key and the ``hold`` {key, holder, at, until, by, note, auto}.
    """
    params = _add_caller({"key": key, "note": note, "ttl_s": ttl_s})
    return await _get_client().call("orchestrator_hold", params)


@mcp.tool()
async def decision_get(decision_id: str) -> dict:
    """Read one decision brief (or routed question): its question, options,
    recommendation, what was tried, evidence, reversibility, who holds it
    and its answer.

    When to use: you were woken with "Item <key>: decision <id>" (or
    "question <id>") as the first responder, or you want a brief's details.

    Args:
        decision_id: e.g. "d3".

    Returns:
        Object with ``decision``.
    """
    return await _get_client().call("decision_get", {"id": decision_id})


@mcp.tool()
async def decision_answer(decision_id: str, choice: str, reason: str, comment: str = "") -> dict:
    """Answer a brief you hold as the first responder (orchestrator only).

    Only within your mandate: a reversible quick one always; reversible or
    partly reversible ones while the user is away; never ``reversible: no``.
    The engine refuses anything else, and past your daily caps: then call
    decision_escalate. The worker gets "Decision <id>: <choice> — <label>.
    <comment>" when it is idle; the row and the worker's notes record your
    reason, and the user can revert it.

    Args:
        decision_id: The brief's id, e.g. "d3".
        choice: An option's key; for a plain question (no options), the answer.
        reason: Why (≤ 300 chars), shown to the user.
        comment: Extra words for the worker (optional).

    Returns:
        Object with the answered ``decision``.
    """
    params = {"id": decision_id, "choice": choice, "reason": reason, "comment": comment}
    return await _get_client().call("decision_answer", _add_caller(params))


@mcp.tool()
async def decision_escalate(decision_id: str, note: str, suggestion: str | None = None) -> dict:
    """Hand a brief you hold to the user (orchestrator only): it goes to
    their inbox by class (Now, Round or To sit down with), with your
    suggestion preselected ("orchestrator suggests B").

    When to use: it is outside your mandate (the user is present and it is
    not a small reversible thing; ``reversible: no``), you are unsure, or
    the engine refused your answer.

    Args:
        decision_id: The brief's id.
        note: What the user should know (≤ 200 chars).
        suggestion: The option key you would pick (optional).

    Returns:
        Object with the escalated ``decision``.
    """
    params: dict[str, Any] = {"id": decision_id, "note": note}
    if suggestion is not None:
        params["suggestion"] = suggestion
    return await _get_client().call("decision_escalate", _add_caller(params))


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

    When to use: when the goal or a constraint is set, a decision is
    taken, a doubt comes up or is settled, or a step is added or done:
    your notes are what you (or a successor) get back after a compaction.

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


# ------------------------------------------------------------------
# Review replies
# ------------------------------------------------------------------


@mcp.tool()
async def review_reply(comment_id: str, text: str) -> dict:
    """Answer one review comment or question from the user's diff view.

    When a prompt gives review comments or a question with a
    ``comment_id``, call this once per comment with your answer to it, so
    the user sees it next to the code; also say it in the conversation as
    usual. Answer only the ids your prompt gave you.

    Args:
        comment_id: The id from the prompt, e.g. "c3f9a1".
        text: Your answer to that comment: what you did or why not, or the
            answer to the question. Plain text or Markdown, a few lines.

    Returns:
        Object with reply_id and comment_id.
    """
    params: dict[str, Any] = {"comment_id": comment_id, "text": text}
    caller = os.environ.get("LAZYAGENT_AGENT_TOKEN")
    if caller:
        params["caller"] = caller
    return await _get_client().call("review_reply", params)


# ------------------------------------------------------------------
# Reports and decision briefs (proposal 0013 I4)
# ------------------------------------------------------------------


@mcp.tool()
async def status_report(
    note: str = "",
    ball: str | None = None,
    needs_user: bool = False,
    outward: dict | None = None,
    until: str | float | None = None,
) -> dict:
    """Tell the user's inbox where your work item stands, in one line.

    When to use: when the ball moves and the engine cannot see it: you now
    wait on someone else (``ball="others"``, e.g. "waiting for QA"), you
    are blocked (``ball="stalled"``), or the user must act
    (``needs_user=True``: a Round item for the user until you run again
    after your turn, or are sent input). Also, best effort, after an action
    outside the repo (an issue-tracker comment, a PR, a PR comment):
    ``outward={"kind": "tracker" | "github_comment" | "pr_create", "ref":
    "<url or id>", "text": "<what>"}`` adds it to the item's timeline.
    If you end your turn while work continues in the background, call
    status_report with ball='agent', a note and until. Not for progress
    chatter: the engine already sees commits, CI and your status. For a choice that needs reading, use request_decision instead.

    Only for your own work item (lazyagent's agent token); the note shows
    on the row with its age. The orchestrator: call it with only a ``note``
    saying what you are doing now (e.g. "verifying feat-x's fix"); the user
    sees it on Home. ``ball``, ``needs_user`` and ``outward`` are refused
    for you.

    Args:
        note: One line (≤ 200 chars), e.g. "PR up, waiting for QA".
        ball: "agent", "others" or "stalled"; omit to leave it to the engine.
        needs_user: True only when the user must do something.
        outward: Optional {kind, ref, text} of an action outside the repo.
        until: When the ball lapses: a Unix time or a duration ("45m",
            "2h"; at most 8 h; default 2 h for ball "agent", none for the
            others). Past it, or once you run again and end a turn without
            a new report, the engine reads your status again (your
            finished turn reaches the orchestrator or the user).

    Returns:
        Object with key (your work item; null for the orchestrator), report
        and outward (the timeline entry, if any).
    """
    params: dict[str, Any] = {"note": note, "needs_user": needs_user}
    if ball is not None:
        params["ball"] = ball
    if outward is not None:
        params["outward"] = outward
    if until is not None:
        params["until"] = until
    return await _get_client().call("status_report", _add_caller(params))


@mcp.tool()
async def ci_attest(
    check: str,
    reason: str,
    sha: str,
    base_runs: list[dict],
    own_runs: list[dict],
    method: str = "range",
    key: str | None = None,
) -> dict:
    """Attest that a nondeterministic check's red on your head is not your
    branch's fault (an eval suite whose pass rate varies run to run), with
    the evidence. The user's config decides what it counts for; by default
    the user confirms it, and Merge may then appear. Merging stays theirs.

    When to use: only for a check lazyagent calls nondeterministic (the
    user's [ci] nondeterministic; the engine refuses any other: mechanism A
    compares failing tests for those), after you have
      1. read the suite's history on the base branch: at least 5 recent
         runs of that check, with their pass rates (gh run list / view);
      2. run the suite on your head yourself, at least 3 times, recording
         each run's pass rate. Running it can take long on a shared stack:
         if your instructions define a slot or lock for running this suite,
         request it before each run and release it after;
      3. compared: your lowest pass rate must be at most max_drop (0.05,
         5 points) below the base's lowest.
    If your runs are clearly worse, it is a regression: fix it instead.
    Never edit the test to make it pass.

    The engine checks what it can: the check fails on the head and is
    listed; sha is the head; enough runs; each run_id is a run of that
    check (base runs on the base branch, your runs on the head); it
    recomputes the range and refuses numbers that contradict it. Local runs
    are recorded as your claim. A new commit, a moved base point or 72 h
    expires the attestation; the user may reject it (you get their
    comment) or revoke it.

    Args:
        check: The failing check, e.g. "eval-suite".
        reason: One or two sentences, e.g. "pass rate within the base's
            range over 5 runs; the failing cases differ every run".
        sha: Your head commit (at least 7 characters).
        base_runs: [{"run_id": 123456, "passed": 71, "total": 100}, ...]
            (≥ 5, each with its Actions run id).
        own_runs: [{"passed": 74, "total": 100, "where": "local",
            "at": 1792830000}, {"run_id": 123499, "passed": 76, "total":
            100}] (≥ 3, on the head).
        method: "range" (default; the engine checks it), or "mean_ci" /
            "other" explained in reason (always confirmed by the user).
        key: The orchestrator's only: the work item (within its mandate,
            action attest_ci). A worker attests for its own work item.

    Returns:
        The attestation: id, state (pending: the user is asked; accepted;
        recorded: shown, no effect for this check), the engine's range and
        a message saying what happens next.
    """
    params: dict[str, Any] = {"check": check, "verdict": "unrelated", "reason": reason,
                              "sha": sha, "base_runs": base_runs, "own_runs": own_runs,
                              "method": method}
    if key is not None:
        params["key"] = key
    return await _get_client().call("ci_attest", _add_caller(params))


@mcp.tool()
async def owner_approve(note: str = "") -> dict:
    """Record that the user approved your PR as it is now (its head on
    GitHub), when they review outside GitHub and say so in this
    conversation ("approved", "LGTM, approve").

    When to use: right after the user says they approve, before you move
    the PR on (e.g. out of draft). Only on their word: never on a bot's
    approval or your own judgment. lazyagent counts the commits pushed after
    this point (pr.commits_after_owner_approval in their rules), so a later
    real commit asks for their review again; a clean merge of the base does
    not. Nothing is written to GitHub.

    For your own work item only; the orchestrator cannot approve.

    Args:
        note: Optional, one line (e.g. "approved after the retry fix").

    Returns:
        The approval: key, pr, sha (the head approved), at, source "agent";
        a warning when your worktree's head is not what is pushed.
    """
    return await _get_client().call("owner_approve", _add_caller({"note": note}))


@mcp.tool()
async def request_decision(
    question: str,
    options: list[dict],
    recommendation: str,
    tried: str,
    evidence: list[dict],
    reversible: str,
    impact_if_wrong: str,
    decision_class: str = "deep",
    blocking: bool = False,
    replaces: str | None = None,
) -> dict:
    """Ask the user for a choice that needs reading: a decision brief. It
    waits in the user's "To sit down with" zone, with your evidence beside
    it, instead of a long question in your terminal.

    When to use: two designs, a trade-off, a risky or hard-to-undo step,
    what to do after your tries failed: anything the user must read before
    answering, or any time you need the user to choose (this branch or a
    separate task, an approach): send the brief, then end your turn; never
    leave the choice only as prose. Only a quick yes/no goes in the
    conversation. At most 2 open briefs per agent.

    Template (fill every field; a brief with fewer than 2 options, a
    recommendation that is not one of them, or no evidence is shown only as
    a quick "agent <id> asks: <question>"):

        question: "Retries: fixed or exponential backoff?"      (≤ 200 chars)
        options: [{"key": "A", "label": "Exponential, cap 30 s",
                   "consequence": "slower failure on outages"},
                  {"key": "B", "label": "Fixed 2 s, 5 tries",
                   "consequence": "3/30 runs time out"}]          (2-4)
        recommendation: "A"
        tried: "fixed 2 s: 3 of 30 runs hit the 10 s limit"      (≤ 600 chars)
        evidence: [{"kind": "diff", "path": "src/retry.py", "hunk": "@@ -40,7 +40,12 @@"},
                   {"kind": "log", "ref": "job:tests", "lines": "120-134"},
                   {"kind": "file", "path": "docs/retry.md", "lines": "10-24"},
                   {"kind": "notes", "section": "Doubts"},
                   {"kind": "url", "url": "https://..."}]          (1-8)
        reversible: "yes" | "partly" | "no"
        impact_if_wrong: "slower failures under load"

    Paths are relative to your worktree (outside it they are dropped); a
    diff or file is pinned to your branch head unless you give ``sha``.

    Args:
        question: The one thing to decide.
        options: 2-4 objects {key, label, consequence}.
        recommendation: The key of the option you recommend.
        tried: What you tried and what happened.
        evidence: 1-8 pointers (see the template).
        reversible: "yes", "partly" or "no".
        impact_if_wrong: What goes wrong if the choice is wrong.
        decision_class: "deep" (default: needs reading) or "quick" (small).
        blocking: True only if you cannot go on without the answer; then end
            your turn. Otherwise keep working on what does not depend on it.
        replaces: The id of one of your open briefs this one replaces; also
            the answer to "The user asks for a new brief instead of ..."
            (the orchestrator may write that one when it was asked).

    Returns:
        Object with ``decision`` (its id, state, class, valid, ...) and
        ``message`` (where it went, or why it is not a full brief). The
        answer comes later as a line "Decision <id>: <choice> ...".
    """
    params: dict[str, Any] = {
        "question": question,
        "options": options,
        "recommendation": recommendation,
        "tried": tried,
        "evidence": evidence,
        "reversible": reversible,
        "impact_if_wrong": impact_if_wrong,
        "class": decision_class,
        "blocking": blocking,
    }
    if replaces is not None:
        params["replaces"] = replaces
    return await _get_client().call("request_decision", _add_caller(params))


@mcp.tool()
async def decision_withdraw(decision_id: str) -> dict:
    """Withdraw one of your open decision briefs (request_decision).

    When to use: the question no longer matters (you found the answer, the
    task changed), so it leaves the user's inbox. To change a brief, send a
    new one with ``replaces`` instead.

    Args:
        decision_id: The brief's id, e.g. "d3".

    Returns:
        Object with the withdrawn ``decision``.
    """
    return await _get_client().call(
        "decision_withdraw", _add_caller({"id": decision_id})
    )

# ------------------------------------------------------------------
# External feeders (proposal 0013 I6)
# ------------------------------------------------------------------


@mcp.tool()
async def work_items_upsert(
    url: str,
    title: str,
    source: str,
    priority: str = "medium",
    ticket: str | None = None,
    branch: str | None = None,
    project: dict | None = None,
    depends_on: list[str] | None = None,
    waiting: dict | None = None,
    expires_at: float | None = None,
    facts: dict | None = None,
) -> dict:
    """Tell lazyagent about a task you track elsewhere (a planner's board, an
    issue tracker), so the user's Home shows it. For feeders: a planner or a
    script, not a worker reporting on its own branch (use status_report).

    One work item per URL: calling again with the same URL replaces it, and
    an optional argument left out goes back to its default (so leaving out
    ``waiting`` takes the item out of Parked). Send the whole item each
    time. While no local branch links to it, it counts in Home's "N not
    started"; once a branch carries its ticket key (e.g. ``ABC-123-fix``),
    it joins that branch's row with its priority and project. lazyagent
    never starts a task on its own.

    Args:
        url: The task's http(s) URL (the dedupe key).
        title: Its title, as the user knows it.
        source: Who feeds it, e.g. "planner" (at most
            [status] max_open_per_source open items each, 50 by default).
        priority: "high", "medium" or "low": orders it (and its inbox items)
            within its level.
        ticket: Its ticket key (e.g. "ABC-123"); default: read from the URL.
        branch: The branch it is worked on, when it has no ticket key.
        project: {"id", "title", "policy": {"continue_while_review": bool}}:
            rows group by project; with continue_while_review the
            orchestrator is woken when a task's dependencies are all merged
            or in review.
        depends_on: URLs of the items it waits on ("waits on <title>").
        waiting: {"reason"?, "until"?}: the item sits in Parked as "waiting
            (<source>)" until ``until`` (a Unix time, "tomorrow" or a park
            event) or your next call without it.
        expires_at: Unix time after which lazyagent forgets it.
        facts: Tracker facts for a tracker lazyagent has no adapter for,
            read by the user's rules: {"tracker.qa": "tested" |
            "not_required" | "pending" | "failed", "tracker.status": "<raw
            status>"}. Other names are refused; an adapter's value wins.

    Returns:
        Object with ``item``, ``created`` and ``key`` (its row, or null while
        not started).
    """
    params: dict[str, Any] = {
        "url": url, "title": title, "source": source, "priority": priority,
    }
    for name, value in (("ticket", ticket), ("branch", branch), ("project", project),
                        ("depends_on", depends_on), ("waiting", waiting),
                        ("expires_at", expires_at), ("facts", facts)):
        if value is not None:
            params[name] = value
    return await _get_client().call("work_items_upsert", _add_caller(params))


@mcp.tool()
async def work_items_close(url: str) -> dict:
    """Forget a task you fed with work_items_upsert: it was finished or
    dropped where you track it. Closing an unknown URL is not an error.

    Args:
        url: The task's URL, as upserted.

    Returns:
        Object with url, ``closed`` (false: there was none) and the ``item``.
    """
    return await _get_client().call("work_items_close", _add_caller({"url": url}))



@mcp.tool()
async def decision_explain(
    text: str, decision_id: str | None = None, key: str | None = None
) -> dict:
    """Answer the user's "Explain this": a short explanation of a brief or
    of a work item, shown in their decision view with who wrote it.

    When to use: only when you got the line "Explain brief <id> ..." or
    "Explain item <key> ..." (the user clicked Explain this). The engine
    refuses an explanation nobody asked for. Plain text, at most 10 lines
    (the rest is cut): what the choice is about, what each option does,
    and why you recommend yours. Do not answer the brief itself here.

    Args:
        text: The explanation, at most 10 lines.
        decision_id: The brief's id, e.g. "d3" (when the line named one).
        key: The work item key (when the line named an item instead).

    Returns:
        Object with ``explain`` (state answered, ``by``) and, when cut, a
        ``message``.
    """
    params: dict[str, Any] = {"text": text}
    if decision_id is not None:
        params["id"] = decision_id
    if key is not None:
        params["key"] = key
    return await _get_client().call("decision_explain", _add_caller(params))


if __name__ == "__main__":
    mcp.run(transport="stdio")
