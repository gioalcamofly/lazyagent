"""lazyagent wire protocol, v1.

Shared by the engine, the TUI, the MCP server and any future non-Python
engine, so this package imports only the standard library: never Textual,
pyte or another ``lazyagent`` module.

- :mod:`.envelope`: req/res/evt messages and error codes.
- :mod:`.framing`: length-prefixed control (JSON) and binary stream frames.
- :mod:`.handshake`: ``hello`` and protocol version checks.
- :mod:`.validation`: payload validation against ``schema/v1/*.json``.
- :mod:`.hosted`: hosted mode (``LAZYAGENT_HOST``) and its OSC announcement.
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
    # The restart manifest, on the legacy socket too so MCP can restore.
    "list_restorable",
    "restore_agents",
    "dismiss_restorable",
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
    "get_sidebar_state",
    "attach_ui",
    "detach_ui",
    "publish_view",
    "shutdown",
    "ui.get_view",
    "ui.select_worktree",
    "ui.select_orchestrator",
    "ui.focus_agent",
    "ui.open_shell",
    "ui.show_diff",
    "ui.open_modal",
    "ui.blank",
)

# The v1 methods a hosted UI carries out: an engine advertises them only
# while one is attached and answers NO_HOSTED_UI otherwise.
UI_COMMAND_METHODS: tuple[str, ...] = tuple(
    m for m in V1_METHODS if m.startswith("ui.") and m != "ui.get_view"
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
    "engine.restored",
    "restorable.changed",
    "ui.command",
    "ui.view_changed",
)

__all__ = [
    "ERROR_CODES",
    "EVENTS",
    "LEGACY_METHODS",
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "UI_COMMAND_METHODS",
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
