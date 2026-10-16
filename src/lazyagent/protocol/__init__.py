"""lazyagent wire protocol, v1.

Shared by the engine, the TUI, the MCP server and any future non-Python
engine, so this package imports only the standard library: never Textual,
pyte or another ``lazyagent`` module.

- :mod:`.envelope`: req/res/evt messages and error codes.
- :mod:`.framing`: length-prefixed control (JSON) and binary stream frames.
- :mod:`.handshake`: ``hello`` and protocol version checks.
- :mod:`.validation`: payload validation against ``schema/v1/*.json``.
"""

from .envelope import (
    ERROR_CODES,
    ErrorCode,
    ErrorInfo,
    Event,
    Message,
    ProtocolError,
    Request,
    Response,
    parse_message,
)
from .handshake import PROTOCOL_VERSION, SUPPORTED_VERSIONS

# The methods inherited from the legacy NDJSON IPC server (ipc.py).
LEGACY_METHODS: tuple[str, ...] = (
    "list_worktrees",
    "create_worktree",
    "remove_worktree",
    "spawn_agent",
    "stop_agent",
    "get_agent_status",
    "read_agent_output",
    "send_agent_input",
    "list_agents",
    "list_events",
)

# The methods protocol v1 adds.
V1_METHODS: tuple[str, ...] = (
    "subscribe",
    "unsubscribe",
    "attach_stream",
    "detach_stream",
    "resize",
    "claim_size",
    "open_shell",
    "refresh_worktrees",
    "refresh_git_status",
    "get_diff",
    "get_pr_info",
    "set_interest",
    "get_config",
)

EVENTS: tuple[str, ...] = (
    "agent.status",
    "agent.updated",
    "agent.spawned",
    "agent.exited",
    "worktrees.changed",
    "git_status.updated",
    "pr_info.updated",
    "stream.resized",
    "stream.reset",
    "stream.closed",
    "engine.notice",
    "job.started",
    "job.output",
    "job.finished",
    "engine.shutdown",
)

__all__ = [
    "ERROR_CODES",
    "EVENTS",
    "LEGACY_METHODS",
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "V1_METHODS",
    "ErrorCode",
    "ErrorInfo",
    "Event",
    "Message",
    "ProtocolError",
    "Request",
    "Response",
    "parse_message",
]
