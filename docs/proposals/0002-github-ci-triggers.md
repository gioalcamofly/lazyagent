# 0002. GitHub/CI event triggers

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4d (`get_pr_info`, `set_interest`, `subscribe`), §4e (`pr_info.updated`), §6 (PRs 2, 3, 6)

## Problem

- Things that should wake an agent happen on GitHub: a base PR merges (stacked PRs need retargeting), CI goes red, a reviewer leaves a comment.
- Nobody notices until someone polls.

### Where the cost really is

| Who polls | Cost per check | Today |
|---|---|---|
| An agent or skill (`/gio-planner` tick, the orchestrator) | One LLM turn: tokens, context, seconds to minutes | Every 15-30 min, even when nothing changed |
| lazyagent TUI | One `gh pr view` subprocess (a network round trip) | Every 60 s, **selected worktree only** (app.py:156-158 timer, app.py:384-405 `_refresh_pr_status`, worktree_manager.py:366-383 `get_pr_info`) |
| The engine, done right | One request per repo, often answered "nothing changed" | — |

- The expensive part is waking an LLM to ask "anything new?", not HTTP.
- Per-worktree `gh pr view` only becomes a cost once the engine watches every worktree in the interest set or with a rule; batching (below) keeps that at one call per repo.

## Today's workaround

- `/gio-planner` tick mode polls every 15-30 min; `lazyagent-orchestrator` retargets stacked PRs when it next looks.
- Latency of up to half an hour per hop, and every poll costs an LLM turn.

## Proposal

- The engine already polls PR info for worktrees in the interest set (PR 3, `WorktreeService`). Extend that poller to diff successive states and emit **typed GitHub events** into the event log.
- **Rules** map an event to an action. Built-in actions: `send_input` (wake the worktree's agent with a templated prompt), `notify`, `retarget` (run `gh pr edit --base`).

| Kind | Name | Shape / notes |
|---|---|---|
| event | `pr.merged` / `pr.closed` | `{path, number, base, head}` |
| event | `pr.checks` | `{path, number, state: "pending"\|"failure"\|"success", failed: [names]}` |
| event | `pr.review` / `pr.comment` | `{path, number, author, state?, body_excerpt}` |
| method | `add_rule {on, where?, action}` / `list_rules` / `remove_rule` | Rules persist in the runtime dir |
| MCP | `add_rule`, `list_rules` | So the orchestrator can set them up |
| CLI | `lazyagent events wait` | `--worktree`, `--pr`, `--type`, `--until`, `--timeout`. Blocks on `subscribe`; exit 0 matched, 2 timeout, 3 engine unavailable |

- Example rule: `on: pr.checks, where: {state: failure}, action: send_input "CI failed on {failed}; investigate and fix"`.
- Ignore events authored by bots (e.g. the `claude` reviewer) unless the rule opts in.

### Polling strategy

1. **One call per repo, not per worktree:** `gh pr list --author @me --state open --json number,headRefName,baseRefName,state,statusCheckRollup,reviewDecision,mergeable` (GraphQL under the hood). Map PRs to worktrees by `headRefName`. 10 watched worktrees: 10 subprocesses per minute become 1.
   - Merged/closed PRs drop out of `--state open`: when a tracked PR disappears, confirm with one `gh pr view <n>` for that PR only, then emit `pr.merged` / `pr.closed`.
   - Hand-written `gh api graphql` only when needed (several repos in one query, review comments).
   - Belongs in **PR 3** (`WorktreeService`), where polling widens beyond the selected worktree. As a standalone TUI change it buys little today, since only the selected worktree is polled.
2. **Conditional "anything new?":** REST `/notifications` with `If-Modified-Since` / `Last-Modified`. A `304` doesn't count against the rate limit; honour `X-Poll-Interval`. One call covers review requests, comments and mentions across repos. GraphQL has no 304 equivalent: batching vs conditional is the trade-off.
3. **Adaptive interval:** 30-60 s while an agent waits on something specific (CI running, review requested); 5-10 min at rest; nothing for PRs no client is interested in and no rule covers.
4. **Diffs only:** keep the last snapshot per PR, emit events only on change. No change, no event, nobody wakes.
5. **Budget:** ~60 GraphQL queries/hour against the 5,000 points/hour limit: negligible.

### Delivery: waking agents without burning tokens

| Mechanism | How | Cost while waiting |
|---|---|---|
| **Background wait (preferred)** | The agent runs `lazyagent events wait --pr 123 --until checks-finished` in the background; Claude Code re-invokes the agent when the command exits | Zero tokens |
| **Engine nudges an idle agent** | Rule action `send_input`, only when the agent's status is `WAITING`; otherwise queue | Zero until the nudge |
| **To the orchestrator** | Event → the orchestrator decides whom to wake | One turn, only on a real change |

### ClickUp

- Same poller pattern: list tasks with `date_updated_gt=<last poll>` so only changed tasks come back; emit `card.status_changed` / `card.comment`.

## Fits after

- **PR 2** (event log), **PR 3** (PR caches and pollers in `WorktreeService`, plus per-repo batching), **PR 6** (v1 server, so skills can `subscribe`, run `events wait` and add rules).
- Retargeting stacked PRs also needs to know the stack; today that lives in `lazyagent-orchestrator`'s DAG. Start with `notify` + `send_input`; add `retarget` once the stack is visible to the engine.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (replaces tick-mode polling) | M (L with `/notifications` and ClickUp) |

## Risks

- Still polling under the hood (`gh` rate limits); mitigated by batching, `304`s and adaptive intervals.
- Waking an agent mid-turn: `send_input` must wait for `WAITING` status or queue.
- Rule loops (an agent's push triggers CI, which triggers the rule again): rate-limit per rule and PR.
- **Alternative, webhooks:** GitHub/ClickUp webhooks need a public URL; a local engine would need a tunnel (smee, `gh webhook forward`). An optional accelerator later; well-done polling is the baseline.

## Open questions

- Poll interval for PRs not in any client's interest set but with a rule attached.
- Where rules are authored: config file, MCP, or both.
- `--until` vocabulary for `events wait` (`checks-finished`, `merged`, `reviewed`, …): fixed list or a `where` filter like rules?
- Split ClickUp into its own proposal (0010)?
