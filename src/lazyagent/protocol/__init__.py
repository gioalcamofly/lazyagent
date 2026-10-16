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

EVENTS: tuple[str, ...] = (
    "agent.status",
    "agent.spawned",
    "agent.exited",
    "worktrees.changed",
    "git_status.updated",
    "pr_info.updated",
    "stream.resized",
    "stream.reset",
    "engine.notice",
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
    "ErrorCode",
    "ErrorInfo",
    "Event",
    "Message",
    "ProtocolError",
    "Request",
    "Response",
    "parse_message",
]
