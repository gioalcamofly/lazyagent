# 0002. GitHub/CI event triggers

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4d (`get_pr_info`, `set_interest`), §4e (`pr_info.updated`), §6 (PRs 2, 3, 6)

## Problem

- Things that should wake an agent happen on GitHub: a base PR merges (stacked PRs need retargeting), CI goes red, a reviewer leaves a comment.
- Nobody notices until someone polls.

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

- Example rule: `on: pr.checks, where: {state: failure}, action: send_input "CI failed on {failed}; investigate and fix"`.
- Ignore events authored by bots (e.g. the `claude` reviewer) unless the rule opts in.

## Fits after

- **PR 2** (event log), **PR 3** (PR caches and pollers in `WorktreeService`), **PR 6** (v1 server, so skills can `subscribe` and add rules).
- Retargeting stacked PRs also needs to know the stack; today that lives in `lazyagent-orchestrator`'s DAG. Start with `notify` + `send_input`; add `retarget` once the stack is visible to the engine.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (replaces tick-mode polling) | M |

## Risks

- Still polling under the hood (`gh` rate limits). Webhooks need a public endpoint; not for a local engine.
- Waking an agent mid-turn: `send_input` must wait for `WAITING` status or queue.
- Rule loops (an agent's push triggers CI, which triggers the rule again): rate-limit per rule and PR.

## Open questions

- Poll interval for PRs not in any client's interest set but with a rule attached.
- Where rules are authored: config file, MCP, or both.
