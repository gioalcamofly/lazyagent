# 0002. GitHub/CI event triggers

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4d (`get_pr_info`, `set_interest`, `subscribe`), §4e (`pr_info.updated`), §6 (PRs 2, 3, 6)

## Problem

- Things that should wake an agent happen on GitHub: a base PR merges (stacked PRs need retargeting), CI goes red, a reviewer leaves a comment.
- Nobody notices until someone polls.

### Where the cost really is

| Who polls | Cost per check | Today |
|---|---|---|
| An agent or skill (`/planner` tick, the orchestrator) | One LLM turn: tokens, context, seconds to minutes | Every 15-30 min, even when nothing changed |
| lazyagent TUI | One `gh pr view` subprocess (a network round trip) | Every 60 s, **selected worktree only** (app.py:156-158 timer, app.py:384-405 `_refresh_pr_status`, worktree_manager.py:366-383 `get_pr_info`) |
| The engine, done right | One request per repo, often answered "nothing changed" | — |

- The expensive part is waking an LLM to ask "anything new?", not HTTP.
- Per-worktree `gh pr view` only becomes a cost once the engine watches every worktree in the interest set or with a rule; batching (below) keeps that at one call per repo.

## Today's workaround

- `/planner` tick mode polls every 15-30 min; `lazyagent-orchestrator` retargets stacked PRs when it next looks.
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

1. **One call per repo, not per worktree:** one `gh api graphql` query with an aliased `pullRequests(headRefName: $h<i>, states: [OPEN, CLOSED, MERGED], first: 30, orderBy: {field: CREATED_AT, direction: DESC})` per watched branch, plus `defaultBranchRef`. The owner and name come from `gh repo view --json owner,name`, cached until the next rescan. 10 watched worktrees: 10 subprocesses per minute become 1. **Shipped in PR 3** (`WorktreeManager.get_pr_infos`, used by `WorktreeService`).
   - **Why not `gh pr list --author @me`** (the earlier idea): `gh pr view`, which the TUI ran per worktree, shows the PR whose head is that branch **by any author**, so `--author @me` would drop teammates' PRs on shared branches; it also lists every PR of the user and paginates on a busy repo, while the per-branch query returns only the branches asked for. Querying by `headRefName` gives **exact parity with `gh pr view`**, which is what keeps the PR badge and the IPC output unchanged.
   - **Parity rules**, taken from `gh pr view`'s own branch lookup: it asks for up to 30 PRs per branch (so `first: 30`, not 1), prefers an open PR, otherwise the newest; skips cross-repository PRs (a fork's `fix-typo` is not ours); and on the default branch only an open PR counts. Branches whose tracking config `gh` resolves differently (an upstream with another name, a fork remote, a `refs/pull/*` merge ref, `pushRemote` / `remote.pushDefault`) and detached worktrees are not batched: they keep one `gh pr view` each. If the batched call fails, every branch falls back to `gh pr view`.
   - Checks come from `commits(last: 1) { … statusCheckRollup { contexts(first: 100) } }`, reshaped into what `gh pr view --json statusCheckRollup` exports, so one parser serves both paths.
   - Large interest sets are split into requests of 25 branches to stay under GitHub's per-query node limit.
   - Measured on a repo with 26 worktrees, 21 of them batchable: 3.8 s for one batched call against 27 s for 21 sequential `gh pr view`, with identical `PrInfo` for every branch.
   - Merged/closed PRs stay in the result (`states` covers them), so `pr.merged` / `pr.closed` can be emitted from the diff of two snapshots with no extra call.
   - Hand-written GraphQL beyond this only when needed (several repos in one query, review comments).
   - With interest = the selected worktree, as the TUI sets it today, this is still one call per poll; the batching pays off once rules or clients widen the interest set.
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

### Issue tracker

- Same poller pattern: list tasks with `date_updated_gt=<last poll>` so only changed tasks come back; emit `card.status_changed` / `card.comment`.

## Fits after

- **PR 2** (event log), **PR 3** (PR caches and pollers in `WorktreeService`, plus per-repo batching), **PR 6** (v1 server, so skills can `subscribe`, run `events wait` and add rules).
- Retargeting stacked PRs also needs to know the stack; today that lives in `lazyagent-orchestrator`'s DAG. Start with `notify` + `send_input`; add `retarget` once the stack is visible to the engine.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (replaces tick-mode polling) | M (L with `/notifications` and the issue tracker) |

## Risks

- Still polling under the hood (`gh` rate limits); mitigated by batching, `304`s and adaptive intervals.
- Waking an agent mid-turn: `send_input` must wait for `WAITING` status or queue.
- Rule loops (an agent's push triggers CI, which triggers the rule again): rate-limit per rule and PR.
- **Alternative, webhooks:** GitHub/tracker webhooks need a public URL; a local engine would need a tunnel (smee, `gh webhook forward`). An optional accelerator later; well-done polling is the baseline.

## Open questions

- Poll interval for PRs not in any client's interest set but with a rule attached.
- Where rules are authored: config file, MCP, or both.
- `--until` vocabulary for `events wait` (`checks-finished`, `merged`, `reviewed`, …): fixed list or a `where` filter like rules?
- Split the issue tracker into its own proposal (0010)?
