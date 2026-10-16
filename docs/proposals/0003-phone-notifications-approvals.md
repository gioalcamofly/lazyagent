# 0003. Notifications and approvals from the phone

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4e (`agent.status`), §4f (auth), §6 (Phase 3 notifications), §6b (H2), K6

## Problem

- Agents stall on permission prompts and questions while Giovanni is away from the desk. The work stops until he looks at the TUI.

## Today's workaround

- Watching the TUI (or the tmux session over SSH).

## Proposal

- The engine already knows when an agent needs a human: `agent.status` with `WAITING_FOR_APPROVAL` or `WAITING_FOR_USER` (Claude: `Notification` hooks `permission_prompt` / `elicitation_dialog`, agent_observers.py:143-175).
- **Notify:** a notifier subscribes to those events and pushes a message with the agent, worktree and the last screen lines (`read_agent_output`).
- **Answer:** the push links to a minimal page (served by the web phase or the H2 app) showing the prompt and buttons. Approve / deny / free text become `send_agent_input` with the right keys for that provider.

| Kind | Name | Shape / notes |
|---|---|---|
| event | `agent.attention` | `{session_id, kind: "approval"\|"question", excerpt}`; derived from `agent.status`, deduplicated |
| method | `answer_prompt {session_id, choice\|text}` | Engine maps to provider keystrokes; refuses if the agent is no longer waiting |
| config | `[notify]` | Backend: ntfy topic, desktop `notify-send`, or both |

## Fits after

- **H2** (host app fed by `subscribe`) or the **web phase (PR 10+)** for the answer page; Phase 3 "notifications" in §6.
- Desktop-only notifications (`notify-send`) can ship earlier, after PR 6, with no page.
- Phone reach needs more than the 127.0.0.1-only bind (K6). That is an open decision, not a new phase.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (agents stop waiting hours for a click) | M (notify) + M (answer page) |

## Risks

- **Security:** anything reachable from the phone can approve shell commands. Needs a tunnel with auth (Tailscale or similar), never a public port.
- Approving blind: the push must show enough context (command, file) to decide safely.
- Prompt-to-keystroke mapping differs per provider and changes with versions.

## Open questions

- Transport for the phone: Tailscale to the web UI, or a push service (ntfy) with action buttons calling back into the engine.
- Should some approvals be auto-answerable by rules (see [0001](0001-shared-resource-slots.md), [0002](0002-github-ci-triggers.md))?
