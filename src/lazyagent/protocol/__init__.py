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
    # Session notes (proposal 0011), on the legacy socket for the MCP tools.
    "notes_get",
    "notes_put",
    "notes_append",
    "notes_replace_section",
    "notes_list",
    "notes_adopt",
    "notes_delete",
    # Review replies, on the legacy socket for the review_reply MCP tool.
    "review_register",
    "review_reply",
    "review_replies",
    # Work items and the inbox (proposal 0013), on the legacy socket for
    # the MCP tools.
    "status_list",
    "actionables_list",
    "actionables_get",
    "actionables_mark",
    # Pass to orchestrator (claims, proposal 0013 I3).
    "status_gesture",
    "orchestrator_release",
    # The orchestrator's own hold on a work item (proposal 0013 I4d).
    "orchestrator_hold",
    # Agents' reports, the timeline and seen marks, decision briefs
    # (proposal 0013 I4).
    "status_report",
    "status_seen",
    "status_timeline",
    "request_decision",
    "decision_withdraw",
    "decision_get",
    "decisions_list",
    # The orchestrator as first responder, presence, auto-delegation
    # settings (proposal 0013 I4b).
    "decision_answer",
    "decision_escalate",
    "decision_revert",
    "presence_report",
    "presence_set",
    "inbox_config_get",
    "inbox_config_set",
    # Explain this, Request a new brief, the evidence tabs (proposal 0013
    # I5).
    "decision_explain_request",
    "decision_rebrief_request",
    "decision_explain",
    "decision_evidence",
    # Work items from external feeders (proposal 0013 I6).
    "work_items_upsert",
    "work_items_close",
    # The failed-log summary of a red CI run (proposal 0013 I7).
    "ci_log_summary",
    # Answers from Home and the orchestrator's log (proposal 0013 I4e).
    "approval_answer",
    "question_answer",
    "orchestrator_actions",
    # The rules catalog, why a row is where it is, the user's rule files
    # (proposal 0014 R2).
    "rules_catalog",
    "rules_explain",
    "rules_check",
    "rules_allow",
    # The user's agent prompt files (lazyagent prompts).
    "prompts_status",
    "prompts_plan",
    "prompts_apply",
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
    "notes.updated",
    "notes.removed",
    "review.replied",
    "status.updated",
    "status.removed",
    "actionable.created",
    "actionable.updated",
    "actionable.resolved",
    "orchestrator.claim",
    "orchestrator.released",
    "orchestrator.lapsed",
    "orchestrator.hold",
    "status.timeline",
    "decision.created",
    "decision.answered",
    "decision.explain",
    "decision.resolved",
    "rules.changed",
    "rules.errors",
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
