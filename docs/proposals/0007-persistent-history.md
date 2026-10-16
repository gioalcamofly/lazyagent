# 0007. Persistent history: per-agent timeline

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4e (event log), §6 (PRs 2, 8)

## Problem

- "What happened overnight?" has no answer inside lazyagent. Status changes, spawns, exits and PR updates exist only in memory (`AgentEventBus`, agent_events.py:123) and are lost when the process exits.

## Today's workaround

- The morning summary is rebuilt by hand (or by `/gio-planner`) from ClickUp, GitHub and agent screens.

## Proposal

- Persist the engine event log (§4e, one engine-wide `seq`) to an append-only JSONL file in the state dir, rotated by size/day.
- A **timeline** query per agent, per worktree, or for a time window, with simple rollups: time running vs waiting vs blocked, approvals asked, PR events, exits.
- A **digest** method that produces the "since X" summary as structured data; the planner skill renders it.

| Kind | Name | Shape / notes |
|---|---|---|
| method | `subscribe {since}` | Unchanged; `since` can now reach before the engine started |
| method | `get_timeline {session_id? \| worktree_path?, since?, until?}` | Ordered events + rollups |
| method | `get_digest {since}` | Per worktree: status changes, PR events, waiting time, exits, usage ([0006](0006-cost-tokens-per-agent.md)) |
| MCP | `get_digest` | For `/gio-planner` and the orchestrator |

## Fits after

- **PR 2** (typed event log) for the data; **PR 8** (detached engine) so it records while no screen is open.
- Under hosted mode first (D11), history only covers the time the TUI was running until PR 8 lands.

## Value / Effort

| Value | Effort |
|---|---|
| ★★ | S (persist log) / M (timeline + digest) |

## Risks

- Disk growth with chatty events: persist status/lifecycle/PR events, not `job.output` or stream data.
- `seq` must stay monotonic across restarts (same approach as D8's persisted agent ids), or `since` cursors break.

## Open questions

- Retention: days or size?
- Should the digest include an excerpt of each agent's last screen (`read_agent_output`) at the time it went idle?
