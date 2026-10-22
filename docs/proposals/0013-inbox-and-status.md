# 0013. Inbox and status: one home screen for the team lead

- **Status:** Proposed (2026-10-22). **Supersedes [0012](0012-actionables.md)** in scope and phasing; 0012 stays as the reference for the actionable model, the GitHub Actions detection and the (now optional) auto-triage. Plan in [inbox-plan.md](../design/inbox-plan.md); open doubts Q-B1-Q-B43 at the end of the plan
- **Design refs:** [0012-actionables.md](0012-actionables.md), [actionables-plan.md](../design/actionables-plan.md); [engine-split.md](../design/engine-split.md) §4d, §4e, §4g; [ui-plan.md](../design/ui-plan.md) §3b; [desktop-app.md](../design/desktop-app.md) D4; [0011](0011-session-notes.md) (notes, agent token)

The desktop opens on **Home**: one list of the user's **work items** (a branch, with its PR, agents and CI). The ones that need the user come first, as an **inbox** in two levels, **Now** and **Round**. Everything else is the **in-flight** list below, one row per item with who has the ball, a fact-based state and its age. That list is the third level, **Ambient**, made readable. Each work item shows **once**. The engine owns the list, derives every state, and the UIs only display it. Nothing on Home is a kanban to maintain.

## Problem

- **"What needs me?" and "where is everything?" are two questions, and nothing answers either in one place.** The Attention tab answers the first per agent and per client (attention.rs, attention.js), only while a window is open. The sidebar half-answers the second (per worktree: PR, CI, one line per agent), but it mixes system noise with state and has no notion of "who has the ball".
- **Evidence from the author's own use.** Over 3.5 weeks (~19 long sessions) the author asked an orchestrator LLM session for a "status table" about **150-180 times**: 11-17 a day at the peak, sometimes every 6-15 minutes, and 5-10 a day once a 30-minute planner tick and event wake-ups started pushing updates. Every answer was a hand-built table (columns varied: `Lane | Card | State | Agent | Next`, `Card | PR/CI | Next step | Agent`, `… | Needs you?`), 40+ lines with bullets, and almost every session compacted.
- **The friction was about data, not looks:** incomplete tables ("you only showed 4, there are more"; items with no agent missing), stale "needs you" ("isn't it already approved by me?", "I already answered both agents"), wrong lane ("it must stay Delegated; fixing conflicts doesn't move it back"), and the "I'm back, what happened?" summary.
- **0012 is too big and too personal for the strategy.** The strategy plan (2026-10-22) makes the inbox the product and asks for a small MVP. 0012's MVP (A1-A4) includes auto-fixing CI with a triage agent, a visible 0-120 score with a criteria file, and a tie-break LLM, and its layer 3 carries one user's lanes, verbs and budget.

## Today's workaround

- Ask the orchestrator session "show me status table" and read a long, hand-assembled answer. Correct it in chat ("X already merged, update and show me").
- The planner's markdown board (lanes Todo · Doing · Delegated · Waiting · Blocked · Done, one verb per card), reconciled by an LLM every 30 minutes. Status tables are effectively that board plus the live agents, rebuilt in prose each time.
- The Attention tab for agent prompts; GitHub e-mails for CI and reviews.

## Proposal

### Concept

| Aspect | Design |
|---|---|
| Unit | **Work item** = `repo:branch`. It outlives its worktree and gains a PR on the way. A PR URL and an issue-tracker URL hang from it as aliases. An external item with no branch yet (a tracker card fed by the planner, a peer's review request) has the key `ext:<url>` until a branch claims it |
| One list, two zones | **Inbox** (top): items with something for the user, grouped by work item, in two levels. **In flight** (below): every other work item, one row each. An item is in exactly one zone. Moving between zones happens only between rounds, never under the cursor |
| Levels | **Now:** an agent is blocked on the user and it takes < 1 min (approval, short question). **Round:** needs the user's judgement but nobody stops if it waits (review a finished branch, merge, changes requested by a human, conflict with no agent, CI escalated). **Ambient:** the in-flight list itself. No visible score: a fixed level per kind, and fixed sort keys inside a level (see "Order inside a level") |
| Ball | Each in-flight row says **who has the ball**: `agent` (working, queued, own CI running), `others` (human review, QA, a teammate, an external state), `stalled` (nobody can move it: conflict with no agent, dead agent, a dependency, idle > N days), `done` (transient, leaves after the round). `you` is never a row state: those items are in the inbox |
| Row state | A short **fact** from the engine (≤ 5 words): "tests 2/5", "CI green · bot approved", "with QA", "conflict with base". Never free prose. The agent's own note shows only in the expanded row, with its age |
| Derived, not edited | The engine derives ball, state and level from agents, PR, CI, open actionables and feeders. The user has three **gestures**, not lanes: **Label** (put a ball/label on a row, e.g. "others: QA"; it expires when the row's fingerprint changes), **Park until** an event (CI ends, agent stops, a human reviews, tomorrow) and **Close** (done / dropped, for what the engine cannot see). Each gesture is shown on the row and counted: gestures per day is the metric of bad rules |
| Completeness | The in-flight list shows **every** work item with a worktree, a live agent, an open PR by the user, or an external record, plus a counter "N not started". Never only the rows with an agent |
| Since last look | Rows that changed since the user last looked carry a dot (I3). From I4, each work item has a deterministic timeline "since you looked" with a per-item seen mark. There is no prose digest |
| Quick and Deep | Items decided from the row in under a minute are **Quick** (Now, Round). Items that need reading are **Deep** and wait in **To sit down with**, with a visible limit. See "Decisions that need context" |
| Owner | The engine. `engine/status.py` (projection) and `engine/actionables.py` (inbox queue, 0012 A1). Clients without the `status` / `actionables` features keep their own derivation |
| No LLM in the state path | **State** (rows, ball, level, class, zone) is the engine's. Nothing is spawned and no model is called to keep Home up to date. It changes only through (1) deterministic reads of engine state and APIs (agent status, PR, CI, git, notes, jobs) and (2) the agents that are already working, which report over MCP (`status_report`: ball, short note, needs the user). A summariser or classifier model is at most a future opt-in idea. **Action** (judgement on one item) may be taken by an LLM only as a delegate: an owner agent or the orchestrator, under limits the engine enforces, with every action shown on the item's row (see "The orchestrator") |
| What is stored | Only what cannot be derived: user gestures, the agents' last `status_report`, external items, and a 24 h tombstone for closed items. Rows themselves are a projection, recomputed on events |

### Wireframe: Home (desktop)

```
┌─ lazyagent ─────────────────────────────────────────── Lead ▾ · ● 1 now ─┐
│ NOW                                                                       │
│ ▸ Approve   login-ttl · wants to run `npm test`                  [Enter]  │
│ ROUND · 11:30                                       2 resolved on their own│
│   Review    fix-typo #9 · finished, CI green                     [r]      │
│   Merge     docs-index #14 · approved, green                     [m]      │
│   Decide    billing-hook #15 · CI red after agent's 2 tries  +1  [d]      │
│───────────────────────────────────────────────────────────────────────────│
│ IN FLIGHT (5) · 3 not started                                [s] fold    │
│   export-csv        agent    implementing · tests 2/5              4m    │
│ • payments-retry #12 others  with QA · label by you                1h    │
│   cache-bust        agent    queued #2                            20m    │
│   search-index #16  stalled  conflict with base, no agent          2h    │
│   docs-api #11      others   review requested from a teammate      1d    │
└───────────────────────────────────────────────────────────────────────────┘
 • changed since your last look · no red outside NOW · rows never reorder while shown
```

Expanded row (Enter on an in-flight row):

```
│ payments-retry #12 · others (label by you, 1h) · CI green · approved by bot │
│   agent a2  stopped 40m ago       notes: "fix holds 30/30 vs 14/30" (40m)   │
│   [o] open terminal  [n] notes  [p] PR  [k] park until…  [x] close  [u] unlabel │
```

### Wireframe: the 1-week test (CLI / MCP, before any UI)

```
$ lazyagent status
NOW    1   ROUND  3   IN FLIGHT  5   not started 3          (11:32, 2 repos)
LEVEL  ITEM                 BALL     STATE                          AGE
now    login-ttl            you      approve: npm test              2m
round  fix-typo #9          you      review: finished, CI green     15m
round  docs-index #14       you      merge: approved, green         1h
round  billing-hook #15     you      decide: CI red, 2 tries        30m
-      export-csv           agent    tests 2/5                      4m
-      payments-retry #12   others   with QA (label)                1h
...
```

The orchestrator calls the MCP tool `status_list` and prints this table instead of building one by hand.

### Decisions that need context

Some items take a minute from the row (approve a command, merge a green PR). Others need reading and thinking (pick between two designs, judge a reviewer's change request, decide what to do with a branch red after two tries). Mixing them in one Round makes the user either rush the deep ones or postpone the quick ones. Five pieces, all deterministic, none of them spawns anything:

| Piece | Design |
|---|---|
| **Two classes** | **Quick:** decided from the row in under a minute (today's items). **Deep:** needs reading. Deep items leave the rounds and go to their own zone, **To sit down with** ("Para sentarse"), meant for focus blocks. The zone shows a visible limit (`deep_max`, default 5): past it a banner says so, and the later backpressure phase stops starting new work. The class comes from a fixed rule per kind, or from the agent's own brief; never from a model |
| **Decision brief** | The agent that is already working writes it over MCP (`request_decision`): one concrete question, 2-4 options with a recommendation, what it has tried, linked evidence (diff hunks, log lines, files and line ranges, notes sections, URLs), reversibility, and the impact if the choice is wrong. Fixed template, validated by the engine: a brief with no options or no evidence is downgraded to a Quick item "the agent asks something" with a link to its terminal |
| **Since you looked** | A deterministic timeline per work item since the user's last look at **that item**: commits, CI runs, ball and state changes, notes edits (section names), answers given, reviews. A per-item "seen" mark; "mark all seen" on Home |
| **Decision view** | Opening a Deep item shows the brief on one side and the evidence on the other (diff hunk, notes, log excerpt, file at lines). The answer (option + comment) goes to the agent through the same queue as review replies and `send_agent_input`: delivered when the agent is idle, never in the middle of a turn |
| **Explain this** | A button, on demand only: it asks the **owner agent** for a short explanation, or to answer one question the user types. The agent replies over MCP and the reply shows in the view. Disabled when the work item has no live agent. Nothing is asked automatically |
| **Decision log** | Every answer is appended by the engine to the agent's notes, section **Decisions** (`- 2026-10-22 · <question> → B (user): <comment>`), and kept on the work item for 30 days. The agent sees it after a compaction (0011 re-injection) and a successor inherits it in a handoff, so the same thing is not decided twice |

Class per kind (fixed):

| Kind | Class |
|---|---|
| Approval, short question (≤ 2 lines or A/B), merge ready, conflict with no agent (delegate), CI red (delegate) | Quick |
| Agent's question longer than 2 lines with no brief | Quick, with "open terminal" (and a hint to ask for a brief) |
| `request_decision` that passes validation | Deep (the agent may declare `class: "quick"` for a small one) |
| Human change request, CI red after the agent's tries, finished branch with no PR to review | Deep |
| External item with `needs_user` | Quick (open the URL) |

### Wireframe: "To sit down with" and the decision view

```
│ ROUND · 11:30                                                     │
│   Merge     docs-index #14 · approved, green                 [m]  │
│ TO SIT DOWN WITH (2 of 5)                       for a focus block │
│   Decide    billing-hook #15 · retries: fixed or exponential?     │
│             brief by agent a3 · 3 options · reversible · 12m      │
│   Review    api-pagination #21 · changes requested by a reviewer  │
│───────────────────────────────────────────────────────────────────│
│ IN FLIGHT (5) …                                                   │
```

```
┌ billing-hook #15 · Decide ─────────────────────┬ Evidence ───────────────────────────┐
│ Retries: fixed or exponential backoff?         │ [diff] [log] [notes] [file]          │
│ ● A  Exponential, cap 30 s   (recommended)     │ src/billing/retry.py  @@ -40,7 +40,12 │
│ ○ B  Fixed 2 s, 5 tries                        │ -    time.sleep(2)                   │
│ ○ C  Leave as is, add a TODO                   │ +    delay = min(2 ** n, 30)         │
│ Tried: fixed 2 s → 3/30 runs time out          │ log: 3 of 30 runs hit the 10 s limit │
│ Reversible: yes · If wrong: slower failures    │                                      │
│ Comment [________________________]             │ Since you looked (40m)               │
│ [Send B…]  [Explain this]  [Open terminal]     │ 11:02 commit "try fixed backoff"     │
│                                                │ 11:10 CI red · 11:14 brief written   │
└────────────────────────────────────────────────┴──────────────────────────────────────┘
```

### How agents learn the tools

0013 adds MCP tools (`status_report`, `request_decision`) and leans on notes, but worker agents get no system prompt today: only the orchestrator gets one (`compose_orchestrator_prompt`, passed with `--append-system-prompt`). Four layers, no plugin:

| Layer | What | Reach |
|---|---|---|
| **a. Tool descriptions** | Each MCP tool's description says when to use it and carries the brief template | Every MCP-capable provider |
| **b. Worker preamble** | ~10 lines injected at spawn: report when you need the user, use `request_decision` for a choice that needs reading, keep notes current. Claude: `--append-system-prompt`; Codex: prepended to the instruction. Editable or disabled in `.lazyagent.toml` `[agents] worker_preamble` | Agents lazyagent spawns |
| **c. Hook nudge** | If a Claude turn ends with a question longer than 2 lines and no brief, the next `UserPromptSubmit` reminds the agent to use `request_decision` (same mechanism as the "notes updated" notice) | Claude agents lazyagent spawns |
| **d. Engine backstop** | A brief with no options or no evidence becomes a Quick item (see above) | Every caller |

A plugin or skill for Claude sessions that lazyagent did not spawn is a later option. Phasing: a, b and d in I4; c after (Q-B34).

### Opt-in auto-delegation of stalled items

Today a stalled row (conflict with no live agent, dead agent, CI red with no agent) only offers a one-key **Delegate** in Round. An opt-in rule per kind can do that key press for the user: `[inbox] auto_delegate = ["conflict_with_base", "ci_red"]` in `.lazyagent.toml`, empty by default. It is a fixed rule; no model decides. It always delegates to the branch's **owner agent** (restore it with its notes; spawn one with the brief only if there is none), respects the concurrency limits, and makes at most 2 attempts per item per fingerprint. After 2 failures the item goes to **To sit down with** with a brief. I4 or later; `ci_red` needs I7's detection (Q-B33, which widens Q-B16). The target can also be the orchestrator (`auto_delegate_target = "owner" | "orchestrator"`), which then chooses: restore the owner agent, spawn one, or escalate.

### The orchestrator: first responder, not owner of state

Today the orchestrator is an ordinary agent session in a special slot. Its limits are prompt text. A fixed-interval planner tick wakes it, which accounts for most of its turns, and many of those find nothing new. It compacts about once a day, and its copy of "who has the ball" goes stale. Its work on the action path is still real value: progress while the user is away, a red CI nobody was watching, a stuck agent caught, plans distilled into one-line decisions. 0013 keeps that value and takes state away from it. **The engine owns state. The orchestrator acts as a delegate, inside limits the engine enforces, and every action shows on the item's row.** The core works the same with no orchestrator running: everything then goes to the user, as in the rest of this proposal.

| Piece | Design |
|---|---|
| **Present, event-woken** | It may run all day, but it stays idle (zero tokens) until something wakes it: a worker question or `request_decision` routed to it, an item handed to it, an opt-in engine rule (after I7), or the user. There is no fixed-interval polling. On each wake it reads `status_list` and the item, and it keeps no state of its own |
| **First responder** | A worker question or `request_decision` goes to the orchestrator first, when one is running and the item's mandate allows it. The row shows "orchestrator · looking". The orchestrator answers within its mandate, or escalates (`decision_escalate`, with its suggestion attached) to the user's inbox: Now, or To sit down with. If it has done neither within **N minutes** (10), the engine escalates anyway. Tool-permission approvals are never routed to it: they stay in Now for the user |
| **Mandate by presence** | **Present** (desktop activity, or the explicit toggle on Home): it proposes, and decides only small things: a Quick, fully reversible answer, a relay, a CI retry, restoring the owner agent. **Away:** it decides anything reversible: technical approach, CI retry, rebase, continuing with the next task. A "keep for me" gesture on an item sets that item's mandate to none |
| **Never alone** | It never takes an irreversible or outward action on its own: merging to a default or protected branch, closing or reopening a PR, any GitHub comment, review or label, writing to an issue tracker, publishing (tags, releases, packages), force-pushing a shared branch, deleting a branch or worktree with unpushed work. The engine enforces this. Every orchestrator action goes through an engine method that checks an allowlist for the current mandate and the brief's `reversible` field. A refusal tells the orchestrator to escalate |
| **Visible and revertible** | Every orchestrator decision becomes an entry "decided by orchestrator (away)", with its reason, in the item's **since you looked** timeline and in the Decisions section of the worker's notes. The row and the decision view offer **Revert**: the user picks another option and the engine sends it to the worker as an override. If the engine recorded an inverse for an action (stopping the agent it spawned, resetting the branch to the pre-action commit while it is unpushed), Revert runs that inverse |
| **Caps** | At most 3 autonomous decisions per item per day, and 20 per day in total. Past either cap, the item goes to **To sit down with**. Home counts autonomous actions, and how many were reverted, separately from the user's own gestures |
| **Stalled items and handoff** | `auto_delegate` can target the orchestrator (see above). **Pass to orchestrator** is a gesture on any row: a claim with a timeout (2 h) that allows the current mandate's actions. If the orchestrator dies or the claim expires, the row returns to Round marked "delegation lapsed" |
| **Projects** | Project tasks enter the engine as work items with dependencies (`work_items_upsert` with `project` and `depends_on`). The project-orchestration skill keeps the DAG, next-task and stacked-PR logic. "Continue with the next task while another awaits review" becomes a per-project policy that the orchestrator runs, within its mandate and the spawn caps. Projects show in In flight, and a task waiting on another shows "waits on <task>" |
| **Engine prerequisites (I1)** | The orchestrator reads `status_list` instead of building tables. Spawn and concurrency caps are enforced in the engine, not in the prompt. The orchestrator becomes an authenticated role (today any caller without an agent token counts as the orchestrator for notes). An external planner that ticks on a fixed interval can subscribe to engine events instead |
| **Not in the core** | An always-on autonomous manager loop, with its own ticks, its own board and its own priorities |

```
 NOW 1 · ROUND 3 · IN FLIGHT 6         auto 4 today · 1 reverted        [Present ▾]
 ...
 ● api  fix-retries         agent · tests running                          12m
   └ decided by orchestrator (away) 10:42 · retries? → A exponential
     "matches notes Constraints; reversible"                    [Revert] [Seen]
 ● web  login-form          orchestrator · looking (3m) · escalates at 10m
```

Phasing: I1 the prerequisites; I3 the **Pass to orchestrator** gesture; I4 first-responder routing, the mandate and the decision log (together with `request_decision`); I5 Revert from the decision view; I6 projects with `depends_on`; after I7, opt-in event wake rules (Q-B35-Q-B42).

### Kinds → level (fixed table, no score)

| Source | Level | Primary action |
|---|---|---|
| Agent `waiting_for_approval` | Now | Approve / deny |
| Agent `waiting_for_user` (question) | Now if short (≤ 2 lines, or A/B options), else Round | Answer |
| Agent `possibly_hanged`, `failed`, exited with error | Round | Relaunch / open |
| Agent `completed`, no PR yet ("your turn") | Round | Review |
| PR approved by a human + CI green + mergeable | Round | Merge |
| Human `CHANGES_REQUESTED` | Round | Review the changes / send to the agent |
| Conflict with no live agent | Round | Send to an agent / open |
| CI red with a live agent on the branch | Ambient (row "agent · fixing CI") once the user delegates it; Round until then | **Delegate to the owner agent** (one key: sends the failed-log summary to that agent) |
| CI red with no agent | Round | Delegate (spawns an agent with the brief) / open |
| Job failed | Ambient (row state), Round only if it blocks something | — |
| External item with `needs_user` | Round | Open the URL |
| Engine errors | Ambient log; an item only when there is a useful button | — |

### Order inside a level (no score)

Inside Now and Round, items sort by three fixed keys, in order:

1. **Blocks a live agent** first (an agent waiting on the answer, `blocking: true` briefs).
2. **Priority** inherited from the item's work item: `high` · `medium` · `low`, default `medium`.
3. **Age**, oldest first.

Every inbox item belongs to a work item (a branch) and inherits its priority. The link is the ticket ID found in the branch name (the label pattern in `models.py` `ticket_id`) or the `branch` field of a `work_items_upsert`, which also carries `priority` (I6). Until a feeder sets one, every item is `medium`, so the MVP order is "blocks an agent, then oldest". The in-flight list sorts by ball, then priority, then last change. There is no visible score; a full score comes back only if Round regularly holds more than 7 items (Q-B8). This **supersedes 0012's decision** of a deterministic score with an agent tie-break.

### Feeding the list, and what it costs

| Feed | What | Cost |
|---|---|---|
| Facts already in the engine | `AgentStatus`, `PrInfo` (state, checks, review decision, mergeable), worktree scan, jobs | Pure projection, no new polling |
| Engine-owned interest | PR info polled for every active work item, not only those a client watches (0012 F1) | One `set_interest` client, `engine:status` |
| Agents via MCP | `status_report {ball?, note, needs_user?}` with the agent's token. Stored per work item; `needs_user` clears itself when the agent runs again or receives input | Optional for agents. Rows never depend on it |
| Notes (0011) | When there is no report, the expanded row shows the first bullet of Doubts, else Pending | Read-only use of the notes store |
| The planner and other feeders | `work_items_upsert {url, title, ball?, state?, needs_user?, branch?, priority?, expires_at}` / `work_items_close {url}` over MCP; dedupe by URL; cap per source | Brings items with no branch (the "N not started" counter, peer reviews). The engine never writes back to the tracker |
| GitHub Actions on branches with no PR | 0012 A2 detection (REST + ETag) | After the MVP |
| Issue-tracker adapters (optional) | The task's state at its source ("in QA", "code review", "returned by QA") on the row, and fixed-rule inbox items. See below | After the MVP (I8) |

### Optional issue-tracker sources

The user may switch on adapters for an issue tracker. With none configured, nothing changes.

| Aspect | Design |
|---|---|
| Shape | A `TrackerAdapter` protocol in the core: `fetch(keys) -> [TrackerTask]` and optional `assigned_to_me() -> [TrackerTask]`. `TrackerTask = {key, url, title, status_raw, assignee?, updated_at, etag?}`. Deterministic API calls only, never a model |
| Link to a row | The ticket key comes from the branch name (the generic `ABC-123` pattern in `models.py` `ticket_id`, overridable per adapter) or from a `work_items_upsert` URL. A task with no branch shows only in the "N not started" counter |
| Status mapping | Configurable per adapter: tracker status → `{ball, state_text, level?}`, e.g. `"QA" = {ball = "others", state = "in QA"}`, `"Returned" = {ball = "you", level = "round", state = "returned by QA"}`. An unmapped status shows raw text with ball unchanged |
| Inbox rules | Fixed and compiled in: a mapping with `level` creates an inbox item when a task **enters** that status (edge, not level), dedupe key `tracker:<adapter>:<key>:<status>`, auto-clears when the task leaves it. No rules from free text |
| Polling | Only keys linked to live rows (plus `assigned_to_me` if on), batched per adapter, every `poll_s` (300) at rest, ETag/`If-Modified-Since` where the API has it, a per-adapter rate cap, back-off on 429, last good value kept on errors (the row shows its age) |
| Credentials | From the environment or the OS keyring (`keyring` service `lazyagent/<adapter>`), named in config by reference (`token_env = "JIRA_TOKEN"`). Never in the repo, never in the state dir, never sent anywhere but the tracker's own API |
| Config | Global `~/.config/lazyagent/trackers.toml`, overridable per repo by `<repo>/.lazyagent/trackers.toml` (mapping only; credentials only global) |
| Read-only | Adapters never write to the tracker in this proposal |
| In the public core | **GitHub Issues** as the reference adapter (uses `gh`, no new credential). Jira and other trackers as example adapters (generic, with placeholder mappings) or third-party packages via an entry point `lazyagent.trackers`. No employer-specific mapping ships in the repo |

### Where new tasks come from

The engine never invents tasks. It discovers them only from deterministic sources:

| Source | What it finds | Phase |
|---|---|---|
| Local state | Worktrees, branches, agents | I1 |
| GitHub, deterministic | Review requests to the user, PRs and CI of the user's branches | I1 (PRs), I7 (Actions on branches with no PR) |
| An external planner as a feeder | Tracker cards assigned to the user, project tasks, via `work_items_upsert` | I6 |
| Optional tracker adapters | Tasks assigned to the user, read-only (`assigned_to_me`) | I8 |

Discovered tasks with no branch show as the **"N not started"** counter; they become rows only when they need the user (a peer review, `needs_user`). Starting one is the user's call, or the orchestrator's under a project's continue policy and its mandate; never the engine's.

Gap: once the external planner moves from a fixed-interval tick to engine events (Q-B42), a new card in the tracker arrives only on the planner's next run. Two ways to close it: **(a)** the planner keeps a light deterministic discovery poll (API calls, no LLM); **(b)** a tracker adapter (I8) polls the tracker itself: generic, one adapter per tracker, its config private to the user. Recommended: (b), with (a) in the meantime (Q-B43).

### Where the board went

- **The board is not built.** The in-flight list *is* the derived board, reduced to one list with a "ball" instead of six lanes. Lanes and verbs are one user's configuration: a label gesture can carry a lane name, and the planner can feed `ball/state` from its own lanes, but the core knows only `agent · others · stalled · done`.
- **No drag and drop, no Todo lane, no budget, no frozen due dates, no Done lane in the core.** The planner keeps its markdown board and its done-sweep. Once Home is trusted, it can feed the engine and stop rebuilding status tables.

### What survives from 0012

| 0012 piece | In 0013 |
|---|---|
| A1 engine queue (store, today's 10 kinds, marks in the engine, events, capability) | **Kept** as phase I2, with a fixed level instead of a score |
| A2 GitHub Actions detection, `head_sha`, engine interest | Interest set in I1; Actions detection in I7 (after the MVP) |
| A3 auto-triage (triage agent, fixer, verify, ledger) | **Out of the MVP.** Later, **opt-in**, and redesigned: delegate to the **owner agent** of the branch (or spawn one only on the user's key). No separate triage agent in the first version |
| A4 score, criteria file, tie-break, notification threshold | **Cut.** Fixed level per kind; inside a level: blocks an agent, inherited priority, age (no score, no tie-break). Notifications only for Now (and later per round) |
| A5 `actionables_create/resolve` | Replaced by `work_items_upsert/close` (I6). `boost` cut |
| A6 planner changes | I6, on the owner's go; the planner's skill keeps working until then |
| A7 TUI list, analysis agent | Cut. The TUI gets a badge and "open in desktop" only |
| Q-A1-Q-A22 (accepted 2026-10-20) | Kept where the piece survives (Q-A3, Q-A4, Q-A8, Q-A11-Q-A13, Q-A15, Q-A20). Q-A1, Q-A2, Q-A5-Q-A7, Q-A9, Q-A16, Q-A18, Q-A21 move with auto-triage to the opt-in phase. Q-A10, Q-A19 lapse with the score. Q-A14, Q-A17, Q-A22: see the plan |

## Fits after

- PR 3 (pollers, batched PR info), PR 6 (v1 server, capabilities), 0011 N2 (agent tokens). All landed. I1 needs nothing new.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (the most measured need of the only user) | MVP I1-I3: M (~20 h of agents, ~5 h of the owner). Full plan I1-I8: L |

## Risks

| Risk | Mitigation |
|---|---|
| A stale row ("already approved") makes the user stop trusting Home and go back to terminals | Facts only in the state column; auto-clear on condition; `needs_user` reports expire on the next agent turn; a visible age on every row |
| The status view becomes a board through the back door | No lanes, no drag, no due dates in the core; three gestures only, each visible and counted |
| Two places to check (inbox and status) | One list; an item is in exactly one zone; the inbox groups by work item |
| A calm screen turns into a wall of green dots | Ambient rows have no red, no animation, minute-level ages; the list folds |
| The measured need is n = 1 and personal | The 1-week CLI/MCP test (I1) decides whether a screen is worth it; tracker lanes stay in the planner |
| The planner and the engine disagree on state | The engine's facts win over a feeder's `state`; a feeder's `ball` is shown as a label |
| Auto-delegation loops or spends agents behind the user's back | Opt-in per kind, off by default; owner agent only; concurrency limits; 2 attempts per fingerprint, then a Deep item |
| The orchestrator decides wrongly while the user is away | Only reversible actions; caps (3 per item, 20 per day), then To sit down with; every decision on the row with its reason and a Revert |
| The orchestrator becomes a second source of truth again | It keeps no state: it reads `status_list` on each wake; ball, level and zone are the engine's |
| A silent orchestrator holds the user's items | Escalation after N minutes (10); claims expire (2 h) and on its death; no orchestrator running → everything goes to the user |
| Uncapped spawns from an orchestrator or an external planner | Spawn and concurrency caps and an authenticated orchestrator role in the engine, at I1 |
| Agents never call the new tools | Tool descriptions + worker preamble + engine backstop (Q-B34); rows never depend on reports |
| Agents flood "To sit down with" with briefs | At most 2 open briefs per agent; `deep_max` visible; a brief without options or evidence becomes a Quick item |
| A brief goes stale while it waits (the branch moved) | Evidence is pinned to a commit; the view marks it "outdated" when the branch head changed and offers "Explain this" |
| An answer is lost if the agent dies before reading it | It is in the agent's notes (Decisions) and on the work item; a restored or successor agent gets it |
| "Explain this" costs tokens | Only on the user's click, only the owner agent, capped at a short answer |

## Open questions

In the plan, §9 (Q-B1-Q-B43).
