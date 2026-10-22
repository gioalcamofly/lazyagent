# 0006. Cost and tokens per agent and task

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4d (`list_agents`), §4e, §6 (PR 2; H2 / web for display)

## Problem

- No visibility into what each agent or tracker task costs in tokens. Hard to tell a runaway agent from a productive one, or to compare providers.

## Today's workaround

- None inside lazyagent. Totals only, outside, per account.

## Proposal

- **Claude:** every hook payload carries `transcript_path` (the session JSONL). Assistant entries there include `message.usage` (input, output, cache read/write tokens) and the model. The engine tails it like the hook log (`JsonlTailObserver`, agent_observers.py:89) and sums per session.
- **Codex / Gemini:** use their existing logs (app-server log, telemetry) where they report usage; otherwise mark as unknown.
- **Cost:** tokens × a price table in config (per model). Shown as an estimate.
- **Per task:** aggregate by worktree; the worktree/branch name already carries the task id.

| Kind | Name | Shape / notes |
|---|---|---|
| event | `agent.usage` | `{session_id, model, input, output, cache_read, cache_write, cost_est}`, cumulative, throttled |
| method | `list_agents` | + `usage` summary per agent |
| method | `get_usage {worktree_path?, since?}` | Aggregates, including exited agents (from the event log) |
| MCP | `get_usage` | So planners can report spend per task |

## Fits after

- **PR 2** (registry + event log): usage is just another typed event.
- Displayed by the TUI status line, then by **H2** or the **web phase**. Per-task history across restarts needs [0007](0007-persistent-history.md).

## Value / Effort

| Value | Effort |
|---|---|
| ★★ | S (Claude) / M (all providers + aggregation) |

## Risks

- The transcript format is internal to Claude Code and can change. Parse defensively; drop unknown entries.
- Subagent transcripts live in separate files; totals undercount unless they are followed too.
- Prices drift; the estimate must say so.

## Open questions

- Budgets: warn or pause an agent past N tokens? Fits with [0001](0001-shared-resource-slots.md)-style rules.
