# Inbox and status: implementation plan

- **Status:** Accepted (2026-10-23; I0 proposed 2026-10-22, doubts answered by the owner 2026-10-23). Supersedes the phasing of [actionables-plan.md](actionables-plan.md). Each phase gets an "As landed" note in §8 when it merges.
- **Proposal:** [0013-inbox-and-status.md](../proposals/0013-inbox-and-status.md). This plan does not repeat its rationale. It covers only what to build, where, and how to prove it.
- **Base:** `develop` @ `5776c62`.
- **MVP** = I1 → I2 ∥ I8a → I3 ∥ I8b: the status projection with a CLI/MCP table and the orchestrator prerequisites; then the engine inbox queue alongside the GitHub Issues adapter; then the desktop Home (with Pass to orchestrator) alongside the Jira adapter (experimental). Phase numbers are names, not order: I8a/I8b keep the tracker number but run inside the MVP (Q-B17). There is no gate after I1 (Q-B1). I4-I7 come after (I4-I5: decisions that need context). Auto-triage is an opt-in phase after that.
- **Hard rule (state path):** no agent is spawned and no LLM is called at runtime to keep the list current. Rows come from deterministic reads (engine state, `gh`, git, notes) and from MCP reports by agents that are already running. Any summariser/classifier model is a future opt-in idea, not part of any phase here.
- **Action path:** an LLM (an owner agent or the orchestrator) acts only as a delegate, on one item, inside limits the engine enforces (allowlist per mandate, caps, claims), and every action is a row/timeline entry (§5c).

## TL;DR

| # | Branch | Scope | One-line acceptance |
|---|---|---|---|
| I0 | `docs-inbox-status-board` | Proposal 0013 + this plan; 0012 and its plan marked superseded | Merged |
| I1 | `inbox-i1-status-projection` | `engine/status.py`: work items as a projection of worktrees + agents + PR + jobs; ball/state/level derivation (§3); engine-owned PR interest; `status_list` method + `status.updated`/`status.removed` events + `status` feature; MCP `status_list`; `lazyagent status` CLI. **Orchestrator prerequisites** (§5c): authenticated orchestrator role (no token ≠ orchestrator), spawn/concurrency caps in the engine, orchestrator prompt reads `status_list` | With no UI open, `lazyagent status` lists every worktree's branch (agent or not) with ball, fact state and age; the orchestrator answers a status question with one `status_list` call; a spawn over `max_concurrent_agents` is refused by the engine; a token-less notes write is no longer attributed to the orchestrator |
| — | (metric, no gate) | Status questions to the orchestrator and hand corrections per day, counted from I1 on (Q-B1) | Non-blocking: reported in §8, never stops a phase |
| I2 | `inbox-i2-engine-queue` | 0012 A1, trimmed: `engine/actionables.py` store, today's 10 attention kinds derived in the engine, the fixed kind → level table, marks (dismiss, park) in the engine, `actionables_*` methods + events + feature. No score, no criteria file | An agent waiting on approval yields a Now item with no UI open; dismissing it in one client hides it in the other; `status_list` reports `needs_you` from the queue |
| I3 | `inbox-i3-desktop-home` | Desktop Home: inbox (Now, Round) grouped by work item + in-flight list; rounds; "changed since last look"; one-key actions; noise cut (path, "engine connected", age "0s", Stop next to Open); **Pass to orchestrator** gesture (claim with TTL, §5c); fallback to the client derivation for old engines | Opening the desktop shows Home, not a terminal; an item is shown once; a new item never reorders the list under the cursor |
| I4 | `inbox-i4-gestures-briefs` | Store for what cannot be derived: label (ball + text, expires on fingerprint change), park until event, close, 24 h tombstones; MCP `status_report` with its expiry rules. **Quick/Deep classes** (fixed rule per kind) and the **To sit down with** zone with `deep_max`; MCP `request_decision` (brief template + validation, §5b); **since you looked** timeline per work item and per-item seen marks. **Tool discovery** (Q-B34 a+b+d): tool descriptions with when-to-use and the brief template, worker preamble at spawn (`[agents] worker_preamble`). **Orchestrator as first responder** (§5c): routing, escalation after N min, presence and mandate allowlist, caps, "decided by orchestrator" in timeline and notes, Revert from the row | A row labelled "others: QA" keeps its label through a conflict and loses it on merge; a freshly spawned worker calls `request_decision` with no hand-written instruction; with the user away the orchestrator answers a reversible brief and the row shows it with Revert, while a `reversible: no` brief reaches the user; an unanswered routed brief reaches the user after 10 min; a brief with options and evidence lands in To sit down with, one without evidence becomes a Quick item; the timeline lists a commit and a CI run made while the user was away |
| I5 | `inbox-i5-decision-view` | Desktop decision view (brief \| evidence \| timeline), `decision_answer` delivered through the review-reply queue, **Explain this** (`decision_explain_request` / `decision_explain`), decision log in the agent's notes (Decisions) and on the work item; **Revert** of an orchestrator decision from the view | Answering B sends the agent one line when it is idle and appends it to its notes; a restored agent sees it after compaction; Explain falls back owner → explicit restore → orchestrator, and is disabled only when none applies |
| I6 | `inbox-i6-feeders` | MCP `work_items_upsert` / `work_items_close` (dedupe by URL, cap per source, `expires_at`); "N not started" counter; link a URL to a branch by its ticket id; `priority` on work items, inherited by their inbox items; **projects**: `project` + `depends_on` on work items, grouped in In flight, per-project `continue_while_review` run by the orchestrator (§5c); the planner change (owner's file, owner's go) | A tracker card with no branch shows in the counter and, once a branch with its ticket id exists, joins that row |
| I7 | `inbox-i7-gha` | 0012 A2: Actions runs per repo (REST + ETag), `PrInfo.head_sha`, "fresh red on head", CI items for branches with no PR; "Delegate to the owner agent" action | A red run on a branch with no PR becomes a Round item; one key sends its failed-log summary to the branch's live agent |
| I8a | `inbox-i8a-github-issues` | **MVP, alongside I2.** `TrackerAdapter` protocol and registry (entry point `lazyagent.trackers`), status mapping, polling with cache/ETag/caps, credentials by env/keyring, `trackers.toml` (global + per repo); the **GitHub Issues** adapter (via `gh`) with `fetch` and `assigned_to_me`; row enrichment and the "N not started" counter from I1's rows; edge-triggered inbox rules wired into I2's queue once it lands (§7b) | With GitHub Issues on, a branch `fix-123-…` whose issue gets the label mapped to "returned" yields a Round item; an issue assigned to the user with no branch adds to "N not started"; with no adapter configured, `status_list` output is byte-identical |
| I8b | `inbox-i8b-jira` | **MVP, alongside I3.** The **Jira** adapter, complete and marked experimental: Jira Cloud REST API v3, JQL search with pagination, issue status and transitions (read-only), `assigned_to_me` by JQL, `Retry-After` on 429; tests with recorded fixtures (transitions, JQL search, pagination, 429) | Against the fixtures, a status change into a mapped "returned" status yields one Round item and leaving it clears it; a 429 backs off and keeps the last good value; the adapter is labelled experimental until checked against a real instance |
| later | — | Rounds as notifications at cut points, Focus mode, backpressure (Round > 7 stops new work), ntfy notifier; **opt-in auto-delegation of stalled items to the owner agent** (0012 A3 trimmed, §7; may come as early as I4, Q-B33); hook nudge for briefs (Q-B34 c); opt-in compiled-in wake rules for the orchestrator (§5c). Never in the core: an always-on autonomous manager loop | — |

Each phase is its own branch from up-to-date `develop`, merged with `--no-ff` once `uv run pytest` and (for desktop phases) `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and `cargo test` are green. Every phase updates the docs it changes (engine-split §4d/§4e/§4g, ui-plan §3b, desktop-app D4, this plan §8, proposal status).

**Owner time:** I1 ~2 h of review (with the orchestrator prerequisites), I2 ~1 h, I3 ~2 h, I4 ~2.5 h (with first-responder routing), I5 ~1.5 h, I6 ~1.5 h (plus the planner change), I7 ~1 h, I8a ~1.5 h, I8b ~1.5 h. Agents implement.

---

## 1. Ground truth (what the code does today)

| Fact | Where | Consequence |
|---|---|---|
| Attention is derived per client: 10 kinds, fixed rank, auto-clear by condition, `occurrence = id@since` | desktop/crates/app/src/attention.rs:12-230; host/static/lib/attention.js | I2 ports it to the engine with the same ids and tests. The web is frozen: it keeps its own derivation and is not ported |
| The sidebar shows, per worktree, name, git dirty/ahead/behind, PR `#N` + state, CI summary, one line per agent | desktop/crates/app/src/app.rs:1167-1260 | I1's row is the same facts, computed once in the engine |
| `AgentStatus`: no_agent, running, waiting, waiting_for_user, waiting_for_approval, completed, failed, interrupted, possibly_hanged | models.py:74-83 | §3 derivation input |
| `PrInfo{number, title, state, checks, url, review_decision, mergeable, base_ref}` | models.py:155-165 | No head sha and no review authors: I7 adds them |
| PR info is polled only for the union of client interests | engine/worktrees.py:134-145 | I1 adds the client `engine:status` with every active work item |
| Notes per agent slot with Goal · Constraints · Decisions · Doubts · Pending; whole-text read only (`_section_bounds` is private) | engine/notes.py:40-46, :251, :283 | I1 adds a public `section_bullets(id, name)` for the expanded row |
| A generic ticket id regex (`ABC-123`) already reads the key from the branch name | models.py `ticket_id` | I6/I8a/I8b use it to link tracker URLs and tasks to rows; an adapter may override the pattern |
| Events on one bus: `agent.status`, `pr_info.updated`, `notes.updated`, `worktrees.changed` | engine/events.py:25-46 | Recompute triggers for the projection, coalesced to 1/s |

---

## 2. Data model

**Row (projection, not stored):**

```jsonc
{
  "key": "<repo>:<branch>" | "ext:<url>",
  "repo": "...", "branch": "...", "worktree_path": "..." | null,
  "title": "PR title | external title | branch",
  "urls": ["<pr url>", "<tracker url>"],
  "zone": "inbox" | "in_flight",
  "level": "now" | "round" | null,            // inbox only
  "ball": "you" | "agent" | "others" | "stalled" | "done",
  "priority": "high" | "medium" | "low",       // inherited from the linked external record; default medium (I6)
  "state": "CI green · bot approved",          // ≤ 5 words, a fact
  "needs_you": {"count": 2, "top": "<actionable id>"},   // links, not text
  "agents": [{"id": "a2", "status": "completed", "since": 1761120000.0}],
  "pr": {"number": 12, "state": "OPEN", "review": "APPROVED", "ci": "green", "mergeable": "MERGEABLE"},
  "note": {"text": "fix holds 30/30", "by": "agent:a2" | "notes" | "feeder:planner", "at": 1761120000.0},
  "gesture": {"kind": "label" | "park", "ball": "others", "text": "QA", "at": ..., "until": "ci_done" | 1761200000.0} | null,
  "changed_at": 1761120300.0,
  "fingerprint": "<hash of the derived facts>"
}
```

**Overlay (stored):** `state_dir()/status/<repo_digest>.json`, 0600, atomic write (as manifest.py), coalesced to ≤ 1 write/s.

```jsonc
{
  "version": 1,
  "gestures": {"<key>": {"kind": "label", "ball": "others", "text": "QA", "fingerprint": "...", "at": 0}},
  "reports":  {"<key>": {"ball": null, "note": "...", "needs_user": true, "by": "a2", "at": 0}},
  "external": {"ext:<url>": {"title": "...", "priority": "medium", "ball": "others", "state": "with QA", "needs_user": false, "branch": null, "source": "planner", "expires_at": 0}},
  "closed":   {"<key>": {"at": 0, "how": "merged" | "closed" | "dropped" | "worktree_removed"}},  // 24 h tombstones
  "seen":     {"<client id>": {"*": 1761120000.0, "<key>": 1761121000.0}},   // last look, global and per item (I4)
  "timeline": {"<key>": [{"at": 0, "kind": "commit" | "ci" | "ball" | "state" | "notes" | "decision" | "review", "text": "…", "ref": "…"}]},  // ring: 50 entries, 7 days (I4)
  "decisions": {"<id>": {"key": "…", "agent": "a3", "brief": {…}, "state": "open" | "answered" | "resolved" | "withdrawn", "answer": {"choice": "B", "comment": "…", "at": 0}}}  // §5b; answered kept 30 days
}
```

**Membership** (completeness): a key is listed when it has a worktree, a live or restorable agent, an open PR authored by the user in a known repo, or an external record. A row with nothing but a branch older than `stale_days` (14) collapses into a "stale (N)" line.

---

## 3. Derivation (first match wins)

| # | Condition | Ball | Zone / level |
|---|---|---|---|
| 1 | Closed: PR merged/closed, worktree removed, or the Close gesture | done | in flight until the next round, then gone (tombstone) |
| 2 | Open actionable (I2) or, before I2, an agent in `waiting_for_approval` / `waiting_for_user` / `possibly_hanged` / `failed` | you | inbox, level from the kind table (0013) |
| 3 | `report.needs_user` or `external.needs_user` | you | inbox, Round |
| 4 | PR approved by a human, CI green, mergeable | you | inbox, Round ("Merge") |
| 5 | Human `CHANGES_REQUESTED`, or conflict with no live agent, or CI red with no live agent | you | inbox, Round |
| 6 | Agent `completed` and no PR | you | inbox, Round ("Review") |
| 7 | Park gesture active | as before the park | in flight, dimmed, "parked until …" |
| 8 | Label gesture with the same fingerprint | the label's ball | in flight |
| 9 | An agent `running`, or own CI running, or CI red being fixed by a live agent | agent | in flight |
| 10 | PR waiting on a human review, or an external `ball: others` | others | in flight |
| 11 | No live agent, nothing running, last change > `stalled_hours` (24) or a known blocker | stalled | in flight |
| 12 | Otherwise | agent if an agent is alive, else stalled | in flight |

**State text** is chosen from the same match: "tests 2/5" (from the agent's last tool line when available, else "working"), "CI green · approved", "CI red: <workflow>", "review requested", "conflict with base", the external `state`, or the label text.

**Expiry rules:** a label expires when the fingerprint of the derived facts changes in a way that crosses a rule boundary (merge, close, new red CI); a conflict fixed by an agent does not clear an `others` label (hysteresis: the case from the evidence). `report.needs_user` clears when the agent's status leaves `waiting*` or it receives input. A `needs_you` from an actionable clears with its condition (0012's auto-clear).

---

## 4. Protocol, events, capabilities, MCP

| Kind | Name | Phase | Shape / notes |
|---|---|---|---|
| feature | `status` | I1 | In `FEATURES` (server.py:101) |
| method | `status_list {repo?, include_done?}` | I1 | `{rows: [Row], counts: {now, round, in_flight, not_started, stale}, generated_at}`, inbox first (level, then blocks a live agent, priority, age), then in-flight by ball, priority, `changed_at` desc |
| method | `status_seen {key?, at}` | I3, I4 | Stores "last look" per client; with `key` (I4), per work item |
| method | `status_timeline {key, since?}` | I4 | The item's timeline entries since the last look (or `since`) |
| method (agent) | `request_decision {question, options, recommendation, tried, evidence, reversible, impact_if_wrong, class?, blocking?}` / `decision_withdraw {id}` | I4 | Agent token; validation and limits in §5b |
| method | `decision_answer {id, choice, comment?}` | I5 | User; delivered like a review reply, logged in notes Decisions |
| method | `decision_explain_request {id \| key, question?, restore?}` | I5 | User; owner agent → restore (explicit) → orchestrator → disabled |
| method | `decision_rebrief_request {id, note?}` | I5 | User; same chain as Explain; the agent answers with `request_decision {…, replaces}` |
| method (agent) | `decision_explain {id, text}` | I5 | Owner's or orchestrator's token; the reply shown in the decision view with `by` |
| event | `decision.created` / `.answered` / `.resolved`, `status.timeline {key, entry}` | I4, I5 | — |
| event | `status.updated {row}` / `status.removed {key}` | I1 | Coalesced to 1/s |
| feature | `actionables` | I2 | As 0012 A1 |
| method | `actionables_list` / `_get` / `_mark {id, action: dismiss \| park \| unpark, until?}` | I2 | `park` takes an event (`ci_done`, `agent_stopped`, `human_review`) or a time |
| event | `actionable.created` / `.updated` / `.resolved` | I2 | As 0012 |
| method | `status_gesture {key, kind: label \| park \| close \| clear, ball?, text?, until?}` | I4 | User only |
| method (agent) | `status_report {ball?, note, needs_user?, outward?}` | I4 | Agent token; scoped to the agent's own work item; `outward` (best effort) adds a "done by" timeline entry |
| method | `status_gesture {key, kind: "delegate", to: "orchestrator", ttl_s?}` / `"keep_for_me"` | I3, I4 | User; claim (§5c) / mandate none for the item |
| method (orchestrator) | `orchestrator_release {key, outcome, note}`, `decision_escalate {id, note, suggestion?}`; `decision_answer` with `reason` | I3, I4 | Orchestrator token; each action checked against the mandate allowlist and caps |
| method | `presence_set {mode: auto \| present \| away}`, `presence_report {input_at}` | I4 | User (Home toggle); the desktop reports the user's last input (as landed, I4b) |
| method | `decision_revert {id \| entry, choice?, comment?}` | I4, I5 | User; override to the worker + recorded inverse |
| event | `status.timeline` entries carry `actor`; `orchestrator.claim` / `.released` / `.lapsed` | I3, I4 | — |
| method (feeder) | `work_items_upsert {url, title, ball?, state?, needs_user?, branch?, priority?: high \| medium \| low, expires_at?}` / `work_items_close {url}` | I6 | Source from the token; `max_open_per_source` 50; the engine's facts win over a feeder's `state` |
| MCP | `status_list` (I1), `status_report`, `request_decision`, `decision_withdraw` (I4), `decision_explain` (I5), `work_items_upsert/close` (I6) | — | No arguments → the caller's own scope where it applies |
| CLI | `lazyagent status [--json] [--all]` | I1 | Same rows as `status_list` |
| config | `[status] stale_days, stalled_hours, ticket_pattern`, `[inbox] round_every_minutes, deep_max, max_briefs_per_agent` | I1, I3, I4, I6 | `14, 24, null, 30, 5, 2` |
| config | `[orchestrator] max_spawns_per_day, max_concurrent_agents` (I1), `escalate_after_min, away_after_min, max_decisions_per_item_day, max_decisions_day, claim_ttl_s, away_allow, allow_outward` (I3-I4) | I1, I3, I4 | `10, 4, 10, 15, 3, 20, 7200`, all away rows, `["tracker", "github_comment", "pr_create"]` |

v1 schemas in `protocol/schema/v1/methods.json` and `events.json`, plus golden fixtures.

---

## 5. Desktop Home (I3)

- Home replaces the Attention tab and is the default view. Terminals, diff and notes are drill-downs (Enter / `o`).
- **Inbox:** Now items first, then the current Round, both grouped by work item (one line per work item with its top item and "+N"). New Round items wait in a "next round" counter until a cut point: the user closes an item, comes back from a terminal, or `round_every_minutes` passes. Now items appear at once.
- **In flight:** one line per row (`item · ball · state · age`), dot for "changed since last look", foldable (`s`), ages refreshed per minute, no red, no "working" animation.
- **Keys:** `Enter` primary action / expand, `o` open terminal, `n` notes, `p` PR, `d` delegate (send to the owner agent), `k` park until…, `l` label, `x` close, `u` clear gesture.
- **Noise cut** (strategy §4): no absolute path in the top bar, engine state only when it fails, no toasts without an action, no "0s" ages, no Stop next to Open, no shell opened by default.
- **Fallback:** an engine without `status`/`actionables` → the client derives rows from what the sidebar already has.
- **From I4:** a third inbox zone, **To sit down with** (Deep items, `n of deep_max`), below Round and outside the rounds; key `v` opens the decision view (I5). The dot becomes a per-item seen mark; Enter on a row shows its "since you looked" timeline.

---

## 5b. Decisions that need context (I4-I5)

**Brief (I4).** MCP `request_decision`, with the agent's token, for its own work item:

```jsonc
{
  "question": "Retries: fixed or exponential backoff?",        // ≤ 200 chars
  "options": [                                                  // 2-4
    {"key": "A", "label": "Exponential, cap 30 s", "consequence": "slower failure on outages"},
    {"key": "B", "label": "Fixed 2 s, 5 tries", "consequence": "3/30 runs time out"}
  ],
  "recommendation": "A",
  "tried": "fixed 2 s: 3 of 30 runs hit the 10 s limit",       // ≤ 600 chars
  "evidence": [                                                 // ≥ 1, ≤ 8
    {"kind": "diff", "path": "src/billing/retry.py", "hunk": "@@ -40,7 +40,12 @@", "sha": "<head>"},
    {"kind": "log", "ref": "job:tests", "lines": "120-134"},
    {"kind": "file", "path": "docs/retry.md", "lines": "10-24", "sha": "<head>"},
    {"kind": "notes", "section": "Doubts"},
    {"kind": "url", "url": "https://…"}
  ],
  "reversible": "yes" | "partly" | "no",
  "impact_if_wrong": "slower failures under load",
  "class": "deep" | "quick",          // optional; default deep
  "blocking": true                    // the agent waits for the answer (status waiting_for_user)
}
```

- **Validation:** fewer than 2 options, an unknown `recommendation`, or no evidence → stored as a **Quick** item "agent a3 asks: <question>" with "open terminal". Text fields are capped and shown as plain text. Evidence paths must be inside the worktree; `sha` defaults to the branch head at request time.
- **Limits:** ≤ 2 open briefs per agent (a third replaces nothing and is refused with a message to the agent); `deep_max` (5) is shown on the zone, never enforced by refusing briefs.
- **Lifecycle:** open → answered (delivered) → resolved. A brief is withdrawn by the agent (`decision_withdraw {id}`), or expires when the work item closes.

**Since you looked (I4).** The overlay keeps a per-work-item ring of timeline entries (last 50, 7 days; moves to 0007's history when it lands): `commit` (from the worktree scan), `ci` (run finished), `ball`/`state` (projection changes), `notes` (section names edited), `decision` (asked, answered), `review` (PR review state). `status_seen {key?, at}` marks one item or all. No text is generated: each entry is a fact with its time.

**Decision view (I5).** Desktop: brief on the left (question, options as radio buttons with the recommendation preselected, tried, reversibility, impact, comment box), evidence tabs on the right (diff hunk through the existing diff view, log lines, file at lines, notes section), and the item's timeline under it. "Outdated" banner when the branch head moved since the brief.

**Request a new brief (I5, Q-B32).** `decision_rebrief_request {id, note?}` from the user goes through the same chain as Explain this (owner agent → explicit restore → orchestrator). The answer is a new `request_decision` with `replaces: <id>`; the old brief is closed as `superseded`.

**Answer (I5).** `decision_answer {id, choice, comment}` from the user. The engine:
1. queues the answer for the agent and delivers it like a review reply: `send_agent_input` when the agent is idle, never in the middle of a turn (text: `Decision <id>: <choice> — <label>. <comment>`);
2. appends `- <date> · <question> → <choice> (user): <comment>` to the agent's notes, section Decisions (engine write, no `if_rev` race: append only);
3. keeps a copy on the work item for 30 days.

**Explain this (I5).** `decision_explain_request {id | key, question?, restore?}` from the user picks who answers, first match:
1. **Owner agent alive** → the engine sends it a fixed prompt: "Explain <item> for the user in at most 10 lines; reply with `decision_explain {id, text}`."
2. **Owner not alive, session restorable** → the view offers **Restore agent and explain** (the request carries `restore: true`). The engine restores the agent with its notes (0011 re-injection), then sends the same prompt. Never automatic: it starts a process and costs more.
3. **Not restorable, orchestrator running** → the same prompt goes to the orchestrator with pointers to the item's notes, diff, CI and timeline. The reply is shown as "explained by orchestrator".
4. **None** → the button is disabled, with **Open terminal** and **Pass to orchestrator** (§5c).

Always only on the user's click, capped at 10 lines; no fallback to a bare model call. `decision_explain` accepts the owner's token or the orchestrator's; the reply records `by`.

---

## 5c. The orchestrator role (I1, I3-I6, later)

The engine owns state. The orchestrator is a delegate for actions, and the engine enforces its limits (0013 "The orchestrator"). With no orchestrator running, every rule below falls back to "the item goes to the user".

**Identity (I1).** The orchestrator is the slot `__orchestrator__` with its own agent token. A caller with no token stops counting as the orchestrator for notes (`methods.py`, the `caller_slot is None` branch) and gets the plain user/client role instead. Every `actor` on the timeline is one of `user`, `agent:<id>`, `orchestrator` or `engine`.

**Caps in the engine (I1).** `[orchestrator] max_spawns_per_day` (10), `max_concurrent_agents` (per repo, 4), checked in `spawn_agent` for every caller with an agent token, the orchestrator included. Over a cap the call is refused with a message, and nothing is queued silently. Its prompt reads `status_list` instead of building tables. An external planner subscribes to `status.*` / `actionable.*` events (`list_events` cursor, or `notify_when_idle`) instead of ticking on a fixed interval.

**Wake.** The engine wakes the orchestrator through the same queue as review replies: `send_agent_input` when it is idle, never mid-turn, with one fixed line (`Item <key>: <kind> <id>. Read it with status_list / decision_get.`). Wake sources: a routed question or brief (I4), a claim (I3), the user, and opt-in rules after I7. Nothing wakes it on a timer.

**Routing (I4).** When a worker emits `request_decision`, or a `waiting_for_user` question, the engine checks whether an orchestrator is alive, the item's mandate is not `none`, and the item is not a tool-permission approval. If all three hold, the item is created with `holder: orchestrator`. The row reads "orchestrator · looking" and the orchestrator is woken. It resolves the item with one of:
- `decision_answer {id, choice, comment, reason}` with its token, checked against its mandate;
- `decision_escalate {id, note, suggestion?}`, after which the item goes to the user's zone (Now / Round / To sit down with by the usual class) with the suggestion preselected and labelled "orchestrator suggests B".

If it has done neither after `escalate_after_min` (10), the engine escalates on its own and adds "orchestrator did not answer".

**Mandate.** `presence` is `present` or `away`. It is `present` while a desktop client reports user input in the last `away_after_min` (15). The Home toggle overrides it until changed. No UI connected means `away`. The engine checks an allowlist per mandate:

| Action | present | away |
|---|---|---|
| Answer a brief / question with `reversible: yes` and class Quick | ✓ | ✓ |
| Answer with `reversible: yes` (Deep) or `partly` | propose (escalate with a suggestion) | ✓ |
| Answer with `reversible: no` | escalate | escalate |
| Relay to an agent (`send_agent_input`), restore the owner agent, CI re-run | ✓ | ✓ |
| Rebase / resolve a conflict through the owner agent, spawn a worker (within caps), start the next project task | propose | ✓ |
| **Outward but routine**, when the skill/instructions say so: issue-tracker comments and fields, moving cards, creating PRs, PR comments (`[orchestrator] allow_outward`, default `["tracker", "github_comment", "pr_create"]`) | ✓ | ✓ |
| **Never alone:** merge to a default/protected branch, publish (tag, release, package), force-push a shared branch, delete a branch/worktree with unpushed work, close/reopen a PR | ✗ | ✗ |

The never-alone list is fixed in code. Config can only shorten the other rows (`[orchestrator] away_allow = [...]`, `allow_outward = [...]`), never add to them.

**What is enforced, honestly.** The engine enforces only its own methods: spawn, worktree removal, decision answers, claims, relays. Direct calls to an external MCP server (an issue tracker) or to `gh` bypass the engine. For those, the guarantee is the skill or instructions, and Claude Code's permission settings are the hard block (for example, deny an issue-tracker MCP server's tools to an agent). For the row's timeline, agents may report outward actions on a best-effort basis: `status_report {outward: {kind: "tracker" | "github_comment" | "pr_create" | …, ref, text}}` adds an entry "done by <actor>". Nothing depends on it.

**Log and revert (I4, I5).** Every orchestrator decision or action writes a timeline entry `{actor: "orchestrator", mandate, action, reason, inverse?}` and appends to the worker's notes Decisions: `- <date> · <question> → <choice> (orchestrator, away): <reason>`. **Revert** (row, I4; decision view, I5) sends the worker `Decision <id> overridden by the user: <choice>. <comment>`. It also runs the recorded inverse when there is one: stop the spawned agent, or `git reset` to the pre-action commit while the branch is unpushed. Revert counts as a user gesture. The orchestrator's actions are counted separately (`auto_actions`, `auto_reverted` per day on Home).

**Caps on autonomy (I4).** `[orchestrator] max_decisions_per_item_day` (3) and `max_decisions_day` (20). Past either cap, the engine routes that item to the user (To sit down with) for the rest of the day.

**Claims (I3).** The user gesture `status_gesture {key, kind: "delegate", to: "orchestrator", ttl_s?}` creates `claim {holder, until, allowed}`. `allowed` is the current mandate's allowlist, and the default `ttl_s` is 7200. The orchestrator ends a claim with `orchestrator_release {key, outcome, note}`. If the claim expires, or the orchestrator slot exits, the item returns to Round marked "delegation lapsed". `auto_delegate_target = "orchestrator"` (§7) creates the same claim without the gesture.

**Projects (I6).** `work_items_upsert` gains `project {id, title, policy?}` and `depends_on [url]`. In flight groups items by project, and an item whose dependency is open shows "waits on <title>". `policy.continue_while_review` (default false) lets the orchestrator start the next task whose dependencies are merged or in review, within its mandate (away: ✓; present: propose) and the spawn caps. The DAG, next-task choice and stacked-PR retargeting stay in the project-orchestration skill. The engine only stores and shows them.

**Later (after I7, opt-in).** Compiled-in wake rules, such as "CI red / conflict with no owner agent for > 1 h → claim for the orchestrator". They are off by default and count against the same caps.

---

## 6. Files to touch

| Phase | Files |
|---|---|
| I1 (orchestrator) | `engine/methods.py` (token-less caller ≠ orchestrator; spawn/concurrency caps), `config.py` (`[orchestrator]`), `orchestrator_prompt.py` (read `status_list`, never build tables), `tests/engine/test_orchestrator_role.py` |
| I1 | `engine/status.py` (new: membership, derivation, projection, coalesced events), `engine/engine.py` (own it; feed events), `engine/worktrees.py` (`engine:status` interest), `engine/notes.py` (public `section_bullets`), `engine/methods.py`, `engine/server.py` (`FEATURES`), `engine/events.py`, `mcp_server.py` (`status_list`), `cli.py` / engine CLI (`lazyagent status`), `config.py` (`[status]`), `protocol/schema/v1/*`, `tests/engine/test_status.py` (derivation table as golden tests) |
| I2 | `engine/actionables.py` (new, 0012 A1 minus score), `engine/status.py` (`needs_you` from the queue), schemas, `tests/engine/test_actionables.py` (port of attention.rs tests) |
| I3 | desktop: `proto/src/types.rs` (`StatusRow`, `Actionable`, events), `app/src/home_view.rs` (new), `app/src/attention.rs` (fallback only), `app.rs` (default view, keys, noise cut), `persist.rs` (marks import once) |
| I4 | `engine/status.py` (overlay store, gestures, report expiry, timeline ring, per-item seen), `engine/decisions.py` (new: brief validation, limits, lifecycle), `engine/actionables.py` (class per kind, Deep zone), `mcp_server.py` (`status_report`, `request_decision`, `decision_withdraw`; descriptions with when-to-use and the brief template), `orchestrator_prompt.py` (when to write a brief), `worker_prompt.py` (new: default worker preamble), `agent_providers.py` (inject it: Claude `--append-system-prompt`, Codex prepended), `config.py` (`[agents] worker_preamble`, `[orchestrator]`), `engine/orchestrator.py` (new: routing, presence, mandate allowlist, caps, claims, escalation timer), desktop gestures + To sit down with zone |
| I5 | desktop `decision_view.rs` (new: brief \| evidence tabs \| timeline, reusing `diff_view.rs`), `engine/decisions.py` (answer queue through the review-reply path, explain request), `engine/notes.py` (engine append to Decisions), `mcp_server.py` (`decision_explain`) |
| I6 | `mcp_server.py` (`work_items_*`), `engine/status.py` (external rows, ticket link, `project`/`depends_on`), `engine/orchestrator.py` (`continue_while_review`), `models.py` (configurable ticket pattern); the planner skill (owner's file, owner's go) |
| I8a | `engine/trackers/__init__.py` (protocol, registry, entry point `lazyagent.trackers`), `engine/trackers/github_issues.py`, `engine/status.py` (row enrichment, "not started" from `assigned_to_me`), `engine/actionables.py` (`tracker_status` kind), `config.py` (`trackers.toml` loader), tests with fake `gh` output |
| I8b | `engine/trackers/jira.py` (REST v3 client: JQL search + pagination, status/transitions, 429 back-off), `tests/engine/trackers/fixtures/jira/*.json` (recorded transitions, search pages, 429), `tests/engine/trackers/test_jira.py` |
| I7 | `worktree_manager.py` (`head_sha`, Actions runs with ETag, jobs), `models.py` (`WorkflowRun`), `engine/actionables.py` (`ci_failed`), desktop "delegate" action |

---

## 7. Opt-in auto-delegation of stalled items (I4 or later)

0012 A3, trimmed and widened from red CI to every stalled kind (Q-B33). Off by default:

```toml
# .lazyagent.toml
[inbox]
auto_delegate = []            # e.g. ["conflict_with_base", "ci_red", "agent_dead"]
```

- Also configurable from the desktop's settings menu (per repo): the engine method `inbox_config_set {repo, auto_delegate, auto_delegate_target}` writes the same `[inbox]` keys in `.lazyagent.toml`; the file stays the source of truth (Q-B33).
- Target: `auto_delegate_target = "owner"` (default) or `"orchestrator"` (a claim, §5c: the orchestrator chooses to restore the owner, spawn one, or escalate).
- Trigger: a stalled item of a listed kind enters Round (conflict with no live agent, dead agent, CI red with no agent; `ci_red` needs I7's "fresh red on head").
- Action: exactly what the manual **Delegate** key does, to the branch's **owner agent**: a live idle one gets the brief (busy → when idle); a dead one is restored with its notes; only with no owner at all is one spawned with the brief. Never a separate fixer or a triage agent. No model decides.
- Limits as code: the engine's concurrency limits, ≤ 2 attempts per item per fingerprint, `max_per_day` (10), never on default/protected branches, never on a teammate's branch.
- After 2 failed attempts the item goes to **To sit down with** with an engine-written brief (what was tried, the last log lines); verify on the next run.
- Spawning a fixer for branches with no agent, the Haiku triage agent and revert handling (0012 Q-A1, Q-A7) are reconsidered only if this proves useful.

---

## 7b. Optional tracker adapters (I8a GitHub Issues, I8b Jira; in the MVP)

```python
class TrackerAdapter(Protocol):
    name: str                      # "github_issues", "jira", ...
    def key_pattern(self) -> str | None: ...           # None = the generic ABC-123 pattern
    async def fetch(self, keys: list[str]) -> list[TrackerTask]: ...
    async def assigned_to_me(self) -> list[TrackerTask]: ...   # optional; may raise NotImplementedError
```

```toml
# ~/.config/lazyagent/trackers.toml  (a repo's .lazyagent/trackers.toml may override [*.status] only)
[github_issues]
enabled = true                      # uses `gh`, no token here
status_from = "labels"              # or "state"
[github_issues.status]
"needs-review" = { ball = "others", state = "code review" }
"qa"           = { ball = "others", state = "in QA" }
"qa-returned"  = { ball = "you", level = "round", state = "returned by QA" }

[jira]
enabled = false
base_url = "https://example.atlassian.net"  # Jira Cloud, REST API v3 (experimental until validated)
email_env = "JIRA_EMAIL"
token_env = "JIRA_TOKEN"            # or keyring = "lazyagent/jira"
jql_assigned = "assignee = currentUser() AND statusCategory != Done"
poll_s = 300
max_calls_per_min = 30
[jira.status]
"In Review" = { ball = "others", state = "code review" }
"Reopened"  = { ball = "you", level = "round", state = "returned" }
```

- Both adapters are complete in the core (Q-B18). Jira: REST API v3 `search/jql` with `nextPageToken` pagination, issue status (as landed, no changelog/transitions read: the hub's first-seen edge detects entries into a mapped status, §8 I8b), `Retry-After` honoured on 429; tested only against recorded fixtures until checked on a real instance, so it ships flagged experimental. Other trackers come as third-party packages.
- A fetch error keeps the last good value and marks it stale; it never removes the row or the item.
- Adapters run in the engine's poll loop, not as agents; each call has a timeout.
- The engine's own facts (PR merged, agent waiting) win over a tracker status on ball; the tracker text shows next to them ("in QA · CI green").

## 7a. How agents learn the tools (I4; c after)

Q-B34. Workers get no system prompt today; only the orchestrator does (`orchestrator_prompt.py` `compose_orchestrator_prompt`, via `--append-system-prompt` in `agent_providers.py`).

- **a. Tool descriptions.** `status_report`, `request_decision`, `decision_withdraw` and the notes tools say when to use them; `request_decision` carries the §5b template. Reaches every MCP provider.
- **b. Worker preamble.** ~10 lines at spawn, default in `worker_prompt.py`, overridable or empty (`""` disables it) in `.lazyagent.toml` `[agents] worker_preamble`. Claude: `--append-system-prompt`; Codex: prepended to the instruction. Fixed text, no model.
- **c. Hook nudge (after I4).** A Claude `Stop` hook marks a turn that ended with a question longer than 2 lines and no open brief; the next `UserPromptSubmit` adds one line suggesting `request_decision` (same path as the "notes updated" notice in `notes_hook.py`).
- **d. Engine backstop.** §5b validation: no options or no evidence → Quick item.
- A plugin/skill for Claude sessions not spawned by lazyagent: later, only if needed.
- The owner's own agent skills will also be updated to use these tools; that work is outside this repo.

---

## 8. As landed

**I1** (`inbox-i1-status-projection`):

- `engine/status.py`: `StatusProjection`, owned by the engine (started and stopped with it). Rows recomputed on `agent.*`, `worktrees.changed`, `pr_info.updated`, `notes.*`, `restorable.changed` (coalesced to 1/s) and on a 60 s tick; `status.updated {row}` / `status.removed {key}` only for what changed. `derive()` is §3 as a pure function (rules 3, 7, 8 and actionables wait for I2/I4); the row also carries `rule`, `blocks_agent` and `stale`.
- Membership: every non-bare worktree; the main checkout only while an agent runs there. Detached: key `<repo>:@<head>`. Open PRs or restorable agents with no worktree are not rows yet.
- Age: `changed_at` moves only when the fingerprint (ball, level, PR state/review/mergeable/CI, agent statuses; not the tool detail) changes. A row first seen starts at its branch's last commit (one `git for-each-ref`, re-read every 5 min), so ages are meaningful after an engine restart.
- Rule 5 reads "no live agent" as "no running agent": an idle agent on a red or conflicting PR puts it in Round. Idle (`waiting`, `completed`, `interrupted`) counts as finished for rule 6. No-checks CI counts as green for rule 4. `waiting_for_user` is always Now.
- Done and stale rows are hidden unless `include_done` (`--all`) and counted in `counts`.
- PR interest: a PR-only set (`WorktreeService.set_pr_interest`, client `engine:status`) read by the PR poller; no git-status sweep and no extra `gh` call of its own (first PR info within one poll, ≤ 60 s).
- Engine jobs are not row inputs (they are not tied to a worktree).
- `status_list {repo?, include_done?, caller?}` on both sockets; feature `status`; schemas `status_row`, `status_list.*`, `status.updated`, `status.removed`; golden fixtures. MCP `status_list` adds a ready `table`. CLI `lazyagent status [repo] [--json] [--all]` (exit 3 with no engine; it does not start one).
- Orchestrator role: a call with no token is the user (notes `by: user`); only the orchestrator's token writes as the orchestrator; an unknown token is refused. Caps in `spawn_agent` for any agent token (`[orchestrator] max_concurrent_agents` 8, live workers, the orchestrator not counted; `max_spawns_per_day` 30, rolling 24 h, in memory: an engine restart resets it; defaults raised from §5c's 4 / 10, which real use with 5+ parallel workers exceeds); 0 turns a cap off; refusal is a VALIDATION_ERROR naming the cap; a spawn reserves its place before it starts. MCP `spawn_agent` now sends the token. The orchestrator prompt answers status questions with one `status_list` call.
- Metric (Q-B1, non-blocking): each `status_list` call logs the diagnostics event `status.list {by}` (user, agent, orchestrator).
- Desktop: only `proto/tests/golden.rs` (the `status_list` fixture and `status.*` events are skipped until I3).

**I2** (`inbox-i2-engine-queue`):

- `engine/actionables.py`: `ActionableQueue`, owned by the engine. The desktop's 10 attention kinds (`approval`, `question`, `hanged`, `failed`, `exited`, `ci`, `review`, `conflict`, `done`, `job`) are derived in the engine with the same ids (`Approval:<worktree>/<agent>`, `Ci:<worktree>:<pr>`, …) and `occurrence = id@since`. attention.rs's tests are ported in `tests/engine/test_actionables.py`. Kinds sit in a registry (`register_kind`) and items come from detectors (`add_detector`, a function from a snapshot to items), so I8a's `tracker_status` plugs in without touching the table.
- Fixed levels: `approval` and `question` are Now and block the agent; the rest are Round. `question` is always Now: the agent's detail is a tool line, not the question, so "short or long" waits for I4's briefs. There is no score. Inside a level the order is: blocks a live agent, priority (always `medium` until I6), oldest first, then the old attention rank, then the id. The desktop sorted newest first.
- The conditions follow I1's rules. `ci` and `conflict` are raised only when no agent is running on the worktree; `review` is raised either way. `done` ("your turn") is raised only with no PR and nothing else running there, never for the orchestrator (its prompt is where it waits), and it counts `interrupted` as done. A red PR with a working agent stays ambient ("fixing CI") until I7's Delegate.
- Rows: rule 2 is now the work item's open items (`needs_you {count, top}`, level and state from the top item, `blocks_agent` from any item). Rules 5 and 6 became kinds, so dismissing or parking an item takes the row out of the inbox. Rule 4 (merge) is not a kind: it stays derived and has nothing to dismiss until I4's gestures. An agent still on a dialog whose item is hidden reads `waits: approve` (rule 12). Items with no work item (the orchestrator's dialogs, jobs) appear only in `actionables_list`; `status_list` counts are unchanged.
- Marks are shared by every client: `dismiss` (one occurrence), `park` until a time or `ci_done` (CI ran and finished) / `agent_stopped` (an agent ran and none runs now, or one there at park time is gone) / `human_review` (the PR's review decision changed), and `unpark`. They can come from the user (no token) or the orchestrator's token; a worker's token is refused. The engine that keeps the manifest also keeps `state_dir()/actionables/<repo digest>.json` (0600, atomic, written only on change): first-seen times, dismissals (7 days) and parks. After a restart, first-seen times are carried for 5 min, so an item that comes back (agents restored, PR info polled) keeps its occurrence and its dismissal. An event park goes away with its item, and a time park lasts until its time.
- Agent exits and failed jobs live only in this engine's memory, as they did in the desktop (cap 50). An exit clears when an agent runs in its slot again or when it is dismissed.
- The queue is re-derived on every status projection pass (same triggers, 1/s coalescing, 60 s tick, so time parks end within a minute). Events: `actionable.created {item}` when an item becomes visible (new or unparked), `actionable.updated {item}`, `actionable.resolved {id, occurrence, reason: cleared | dismissed | parked}`.
- `actionables_list {repo?, include_hidden?}` → `{items, counts {now, round, parked}, generated_at}`, `actionables_get {id}`, `actionables_mark {id, action, until?, caller?}`; all three are also on the legacy socket. Feature `actionables`. Schemas: `actionable`, `actionable_until`, `actionable.*`; golden fixtures. MCP `actionables_list` is read-only and adds a ready `table`; MCP has no mark. The orchestrator prompt names it.
- Not in I2: the engine notifying with no UI attached (Q-B10), and importing the desktop's marks (I3). Desktop: only `proto/tests/golden.rs` (the `actionables_*` fixtures and `actionable.*` events are skipped until I3).

**I8a** (`inbox-i8a-github-issues`):

- `engine/trackers/__init__.py`: the `TrackerAdapter` protocol (`key_pattern`, `fetch(keys)`, optional `assigned_to_me`), `TrackerTask`, and the registry: built-ins plus the `lazyagent.trackers` entry point (a package cannot replace a built-in; one that fails to load is skipped). Config: `$XDG_CONFIG_HOME/lazyagent/trackers.toml` holds one table per adapter, and only `enabled = true` ones load. A repo's `.lazyagent/trackers.toml` merges into `[<adapter>.status]` and nothing else: it can neither enable an adapter nor name a credential. Shared keys: `poll_s` (300), `max_calls_per_min` (30), `assigned_to_me` (true) and `key_pattern`; the rest go to the adapter. Credentials come through `credential()`: `<name>_env`, or the `keyring` service (user `<name>`) when the `keyring` package is installed.
- Status mapping is `{ball?, state (or state_text)?, level?}`. A `level` implies ball `you`, and ball `you` with no level means `round`. A bad entry is skipped. Lookup is exact first, then ignoring case. An unmapped status shows its raw text and leaves the ball alone.
- `TrackerHub` is the engine's, rebuilt with the config. The status projection polls it on its 60 s tick and on `worktrees.changed`, one pass at a time.
  - Only keys linked from a work item's branch are fetched, batched. A new key is fetched at once, the rest every `poll_s`, and `assigned_to_me` runs every `poll_s`.
  - Each adapter has a cap on calls per rolling minute. A 429 makes it wait `Retry-After`, or else twice the last wait starting at 60 s and capped at 1 h.
  - Each call times out after 30 s. On an error the last good value stays, with `stale: true` and its `at`. The table shows it as "in QA (10m ago)".
  - A key no live row links to any more is forgotten.
- GitHub Issues (`engine/trackers/github_issues.py`) goes through `gh`, so it needs no credential of its own. The repo is whatever `gh` resolves in the repo root (`{owner}`/`{repo}`).
  - The key is the issue number in the branch name: `fix-123-…`, `123-x`, `feature/#45`, `issue_7`. `v2-x` and `release2026` don't match.
  - `fetch` uses `gh api graphql`, 50 `issueOrPullRequest` lookups per call. A pull request number or a missing issue is left out.
  - `assigned_to_me` reads `gh api user` once for the login. It then uses `gh api -i repos/{owner}/{repo}/issues?assignee=<login>&state=open&per_page=100` with `If-None-Match`, so a 304 returns the cached list. Pull requests are dropped, and only the first 100 issues are read.
  - `status_from = "labels"` (default) picks a mapped label, preferring one mapped with a `level`, then the mapping's order. With no mapped label the status is `closed` for a closed issue, else empty, so a plain open issue adds no text. `status_from = "state"` gives `open` / `closed`.
- Rows: `row.tracker {adapter, key, url, status, state, at, stale}` appears only when a task is linked. Its URL is appended to `urls`, and its title is used when there is no PR title.
  - Q-B20: the tracker decides the ball only after rule 9 and before rule 10. A mapped `others` gives rule 10; `agent`, `stalled` or `done` gives rule 12. Engine facts (merged, a dialog, merge-ready, red or conflicting with no agent, agent running, CI running) win.
  - The table shows the tracker text beside the engine state ("Bash · in QA").
  - The fingerprint includes the tracker status, so a status change moves the row's age.
  - `counts.not_started` counts assigned tasks that no local branch links to: worktree branches plus every `for-each-ref` branch. The table header shows `NOT STARTED N` only when N > 0.
- Inbox (`tracker_status` kind, Round by default): the hub keeps one open item per linked task while the task is in a status mapped with a `level`. The id is `tracker:<adapter>:<key>:<status>`. Leaving the status, or the branch going away, resolves it.
  - A detector turns these items into queue items on the work item: label is the task title, detail is its URL, and the level comes from the mapping (`Item.level` overrides the kind's).
  - The queue's first-seen time makes this an edge. The id holds while the task stays, so a dismissal holds, and entering again is a new occurrence. A task seen already in such a status when the engine starts counts as entering, and the queue's 5 min carry keeps an earlier dismissal.
  - Rule 2 applies, so an open tracker item puts the row in the inbox even while an agent works. That keeps one item on one row. Dismissed, the row keeps the tracker text and falls through to the engine's rules.
  - The tracker's own rule 3 (its level with no queue) remains only for `derive` without a queue.
- Read-only: no adapter writes to its tracker.
- Tests use fake `gh` output and one `gh` shim on `PATH`, with no network. `XDG_CONFIG_HOME` is isolated for every test. With no adapter configured, `status_list` is byte-identical (tested). Schema: `status_row.tracker`, the `tracker_status` kind. Desktop: untouched.

**I8b** (`inbox-i8b-jira`):

- `engine/trackers/jira.py`: the built-in `jira` adapter, Jira Cloud REST API v3, read-only, with stdlib `urllib` only (no new dependency). It is **experimental**: the class carries `experimental = True` and the hub logs it when it loads; the README's tracker table says so. It stays experimental until checked against a real instance.
- Search: `GET /rest/api/3/search/jql` (the enhanced search), following `nextPageToken` until `isLast`, 100 per page, at most 10 pages. The old `/rest/api/3/search` with `startAt` is deprecated on Jira Cloud, so it is not used. Only `summary,status,assignee,updated` are asked for.
  - `fetch(keys)`: `key in ("SHOP-1", …)`, 50 keys per query. Jira refuses the whole query with a 400 when one key does not exist or is not visible; the keys its error names are dropped and the query is sent once more. A 400 that names no key is an error.
  - `assigned_to_me`: `jql_assigned` (default `assignee = currentUser() AND statusCategory != Done`). Jira search has no ETag, so it is a plain call every `poll_s`.
- Key: the generic `ABC-123` pattern; `projects = ["SHOP"]` narrows both the pattern and the keys asked for (so `UTF-8-x` is not a key).
- Status: `fields.status.name`. When that name is not mapped but its category's name (`To Do`, `In Progress`, `Done`) is, the category's name is used.
- **No changelog or transitions read.** I8a's first-seen edge already makes a status change seen between two polls an entry (one Round item, cleared on leaving). The cost is that leaving and coming back within one `poll_s` counts as one stay. Reading `expand=changelog` on every poll would cost a heavier call per batch for that case only.
- Auth: basic, `email` (or `email_env`) plus `token_env` / `keyring` through `credential()`, read on every call. No credential: an error, no request. `base_url` must be `https://` (plain `http` only on loopback); a bad one skips the adapter with a warning. Redirects are not followed, so the token never leaves `base_url`. Error texts carry the HTTP status and Jira's `errorMessages`, never the request or the token.
- Rate limits: 429, and 503 with `Retry-After`, raise `RateLimited` with `Retry-After` (seconds or an HTTP date); the hub waits it out and keeps the last good value, marked stale. Timeouts: 20 s per request in the transport, under the hub's 30 s per call.
- Tests (`tests/engine/trackers/test_jira.py`, no network): recorded responses in Atlassian's documented shapes under `fixtures/jira/` (two search pages with `nextPageToken`, an issue going In Review → Reopened → In Progress, assigned pages, 429 with `Retry-After`, 401, 400 for an unknown key and for bad JQL), replayed by `fake_jira.py`; the default transport against a loopback HTTP server (redirect refused). With `[jira]` present but off, `status_list` is byte-identical. Desktop and `status.py`/`actionables.py`: untouched.

**I3** (`inbox-i3-desktop-home`):

- Engine, `engine/claims.py` (Pass to orchestrator): `status_gesture {key, kind: "delegate", to: "orchestrator", ttl_s?}` (user only, no token) creates `claim {key, holder, at, until, by, allowed}`; refused with no orchestrator running, for an unknown or done item, or a `ttl_s` outside 60 s-24 h. Default TTL `[orchestrator] claim_ttl_s` (7200). A second pass renews it. `kind: "clear"` is the user taking it back. The other §4 gesture kinds (label, park, close) wait for I4 and are refused.
- While a claim holds, the row is in flight with ball `agent`, state `with orchestrator`, rule 13 (not a §3 row) and `claim` set; its inbox items leave the user's inbox, except `approval` items, which stay in Now. The queue itself keeps them (`actionables_list` still lists them).
- The orchestrator is woken once, when its turn is over (never mid-turn), with one line: `Item <key>: passed to you by the user until HH:MM. Read it with status_list; …call orchestrator_release.`
- `orchestrator_release {key, outcome: done | returned, note}`: orchestrator token only. `returned` puts the item back in the user's Round with the note.
- **Lapse:** a claim that expires, or whose orchestrator exits, becomes a `delegation` actionable (Round, title "Back from the orchestrator", state `delegation lapsed`). It is checked on every status pass (events plus the 60 s tick), and dismissing it clears it. A claim whose worktree is removed is dropped silently.
- Claims live in memory only: an engine restart ends them, and the rows fall back to their own derivation.
- `allowed` records the away mandate's rows of §5c. In I3 the engine enforces only who may claim, release and take back; the per-method mandate checks come with I4.
- Events `orchestrator.claim {claim}`, `orchestrator.released {key, outcome, note, claim}` (outcome `done` · `returned` · `taken_back`), `orchestrator.lapsed {key, reason: expired | orchestrator_gone, claim}`. Feature `claims`. Row field `claim` (null otherwise). Schemas and golden fixtures. MCP `orchestrator_release`; the orchestrator prompt says how to handle a passed item.
- `ActionableQueue.on_dismiss` hooks let a detector's owner forget what was dismissed.
- Desktop, `home.rs` (model) and `home_view.rs` (view): the app opens on **Home** (`View::Home`). Ctrl+K / Ctrl+J / Ctrl+L, a sidebar click and `g w` go to the worktrees; `g h` (and the old `g a`) come back.
- **Engine mode** (features `status` and `actionables`): rows come from `status_list` and items from `actionables_list`, then from `status.*` / `actionable.*` events. A work item shows **once**: its top item and `+N`. Items with no work item (the orchestrator's dialogs, jobs) get one line per place.
- **Fallback** (older engines): lines come from the client's attention queue plus the sidebar facts: ball `agent` / `others` / `stalled`, and the age since the client saw the line change. attention.rs is used only for this fallback.
- **Rounds (Q-B9):** Now lines show at once. A line entering Round waits for a cut point and shows a "next round: N ▸" counter (clicking it is a cut point too). Until then the line stays where it was, in flight, marked `you · next round`. Cut points: a dismiss, a park, a pass or a take back; coming back from the worktrees; every 30 min (a constant in the desktop: `[inbox] round_every_minutes` is not read yet). The first complete load is a cut point. Round lines that leave on their own are counted as "N resolved on their own".
- **Order:** between cut points nothing moves. Lines keep their places, new lines go to the end of their zone, and only a cut point sorts again (§4 order, ported). The selection follows the key, not the index.
- **Since last look:** a dot on in-flight lines changed since the last look, which is the time Home was last left or the app last closed (`home_seen` in the desktop's state file). "mark seen" sets it to now. The engine's `status_seen` is left to I4, with the per-item seen marks.
- **Keys:** `Enter` answers a Now line (opens the agent's terminal) and expands any other line (agents, PR, the agent's note with its age, the claim). Also `o` terminal, `n` notes, `p` PR, `x` dismiss, `z` park until (1 h / 4 h / 24 h / CI ends / the agent stops / a human reviews), `Shift+O` pass to orchestrator, `u` take back, `f` fold.
- Key deviations from §5: `z` instead of `k` (j/k move), `f` instead of `s` (s spawns), `Shift+O` for the pass. No close / label / delegate keys yet (I4 / I7). A CI line keeps "Send the failures to its agent".
- Pass to orchestrator is disabled, with the reason shown, when the engine lacks `claims` or no orchestrator runs.
- Trackers (I8a/I8b) on Home: `tracker_status` items are lines like any other (verb "Tracker", `p` opens the task). An expanded row shows `<adapter> <key>: <state>`, with its age when stale. The In flight header shows "· N not started" from `status_list` counts, fetched again on each return to Home.
- The desktop's own marks are imported once (`marks_imported`). A dismissal becomes `dismiss` when the engine has seen the item since before the dismissal. A snooze becomes a park until its time.
- **Noise cut:** the engine pill shows only when the link is not connected, and the engine version is gone from the top bar. No shell until Ctrl+L or "Shell ▸". Ages are minute-level (`<1m`, never `0s`). There is no Stop next to Open: the attention view is gone, and Stop stays in the worktree header. No info toasts that repeat the screen (worktree created / removed, job done, agents restored).
- **Badges:** "Home · ● N now · M round" in the top bar, and `(N now · M)` in the window title.
- **Notifications** cover only Now items, once per occurrence (Q-B10). The settings menu lists only those kinds.
- Not in I3: the TUI badge (Q-B15), the engine notifying with no UI, and the `round_every_minutes` config.
- Screenshots: [dark](desktop-screenshots/i3-home-dark.png) and [light](desktop-screenshots/i3-home-light.png), neutral demo data, rendered by the `home_screenshots` test.
- Tests: `tests/engine/test_claims.py` (10), MCP release in `tests/test_mcp_status.py`, golden fixtures `status_gesture/*` and `orchestrator_release/*`. Desktop: `home.rs` units (order, rounds, one line per item, ages), persist (marks import), headless Home tests (opens on Home, frozen order, rounds, resolved count, pass gating), and a real-engine pass / take back.

**I4a** (`inbox-i4a-briefs-gestures`; I4 lands in three branches: I4a the engine side of briefs and gestures, I4b the orchestrator as first responder, I4c the desktop):

- `engine/overlay.py`: the store of §2's overlay, `state_dir()/status/<repo digest>.json` (0600, atomic, at most one write a second), kept by the engine that owns the repo, as the actionables marks. It holds gestures, agents' reports, 24 h tombstones, the timeline, per-client seen marks and the briefs (plus their id counter: `d1`, `d2`, …).
- **Gestures** (`status_gesture`, user only; `delegate` as in I3):
  - `label {ball: agent | others | stalled, text ≤ 40}` is rule 8: it sets the ball only, the state stays the fact underneath (Q-B7); the CLI/MCP table shows `QA · <state>`.
  - It holds while its basis holds: the PR (which one, its state, its review decision) and "CI red". A conflict, a CI run or a red that clears do not end it; a merge, a close, a new PR, a review change or a new red do. Rules 1-6 still come first, so a conflicting labelled row sits in the inbox and comes back labelled once fixed (the acceptance case).
  - `park {until}` is rule 7 (time or `ci_done` · `agent_stopped` · `human_review`, the inbox park's rules): the row is in flight with the ball of rules 9-12 and `parked until …`. It does not park the item's inbox items (they have their own park).
  - `close {how: done | dropped}` is rule 1 (`closed by you` / `dropped by you`). It ends like a label, or when a new agent starts running there (work resumed). Closing a passed item takes the pass back.
  - `clear` drops the gesture and takes back a pass. The row's `gesture` is `{kind, at, by, ball?, text?, until?, how?}`.
- **Reports** (`status_report {note, ball?, needs_user?, outward?}`, a worker's token, its own work item; the orchestrator and token-less calls are refused):
  - `needs_user` is a `report` inbox item (Round, Quick). `needs_user` and `ball` last until the agent has ended a turn (`waiting*`, `completed`, `interrupted`) and runs again, is sent input (`send_agent_input`), or is gone.
  - The note (≤ 200) stays as the row's `note` (`by: agent:<id>`) while it is newer than the notes' bullet.
  - A reported ball is placed like a tracker's (Q-B20): after rule 9, before 10.
  - `outward {kind, ref?, text?}` only adds a timeline entry by `agent:<id>`.
- **Tombstones:** a row that becomes done (merged, PR closed, closed by the user) or loses its worktree is recorded in `closed` once, and its open briefs expire. A removed worktree's row stays a done row (`worktree removed`, hidden unless `include_done`) for 24 h, then `status.removed` and its gestures, report, timeline and seen marks go. This changes I1: a removed worktree now emits `status.updated` (done) first, `status.removed` a day later. A merged row whose worktree stays keeps its tombstone.
- **Timeline** (ring per item: 50 entries, 7 days; `{at, kind, actor, text, ref?}`, no generated text):
  - `commit`: a newer branch commit time from the `for-each-ref` scan, at the commit's own time, ref the worktree head.
  - `ci`: CI turned green or red.
  - `review`: a new review decision.
  - `ball` / `state`: the row's fingerprint changed (not a tool line); `state` only when no `ci` / `review` entry says it.
  - `notes`: `notes.updated` now carries `sections`, the headings whose body changed.
  - `decision`: asked, withdrawn, superseded, expired.
  - `outward`.
  - Actors: `engine` for what it saw, `user` for a gesture, `agent:<id>`, `orchestrator`. `status.timeline {key, entry}` per entry.
- **Seen:** `status_seen {key?, at?, client?}` (default client `user`; no key = mark all seen, which drops the per-item marks; never later than now). `status_timeline {key, since?, client?}` gives the entries since the client's last look at the item. Rows carry `seen_at` and `last_event_at`: a dot is `last_event_at > seen_at`.
- **Quick / Deep:** a third level, `deep` ("To sit down with"), after Round; `class` (`quick` · `deep`) on items and inbox rows.
  - Fixed per kind (0013 "Class per kind"): `review` (changes requested) and `done` (a finished branch to review) are now Deep; every other kind stays Quick. The no-queue derivation (rules 5 and 6) says the same.
  - New kinds: `report` (Round) and `decision` (the brief's own level).
  - Counts gain `deep` and `deep_max` (`[inbox] deep_max`, 5), in `actionables_list` and `status_list`; nothing is refused over it.
  - The I3 desktop shows `deep` lines in its Round until I4c.
- `engine/decisions.py` (§5b): `request_decision` (a worker's token, its own work item), `decision_withdraw {id}`, `decision_get {id}`, `decisions_list {key?, state?}`.
  - Validation: fewer than 2 options, a recommendation that is not an option key (case-insensitive), or no usable evidence makes the brief invalid. It is still kept, as a Quick item `agent <id> asks: <question>` (detail "open terminal"); the agent is told why.
  - Caps: question 200, option label 120, consequence 200, tried 600 (keeps line breaks), impact 200; more than 4 options or 8 pieces of evidence keep the first ones. Everything else is one line of plain text. No question at all is refused.
  - Evidence: `diff` / `file` paths must resolve inside the worktree (kept relative) or are dropped. `sha` defaults to the head at request time (`git rev-parse HEAD`, else the worktree scan's). `url` is http(s) only. `reversible` outside `yes` · `partly` · `no` is kept as null.
  - Level: deep, or for a quick one (declared `class: "quick"`, or invalid) Round, Now when `blocking`. `blocking` also sorts it first. While an agent has an open brief, its `question` item is left out.
  - Limit: `[inbox] max_briefs_per_agent` (2) open briefs per agent; a third is refused with a message naming them. `replaces: <id>` (one of its own open briefs) supersedes it instead.
  - Lifecycle: `open` → `answered` → `resolved` (the last two are I5's); `withdrawn`, `superseded`, `expired` (work item closed). Closed briefs are kept 30 days.
  - Every brief carries `holder` (`user`; I4b routes to the orchestrator) and `replaces`. Events: `decision.created`, `decision.resolved` (any close; `state` says which).
- **Tool discovery** (Q-B34 a, b, d):
  - MCP `status_report`, `request_decision` (with the §5b template in its description; `class` is `decision_class` there, a Python keyword) and `decision_withdraw` each say when to use them; so does `notes_append`.
  - `worker_prompt.py`: a fixed preamble of about ten lines. Claude gets it with `--append-system-prompt`; a provider with no system-prompt flag (Codex, Gemini) gets it prepended to the instruction, and none when it starts with no instruction (it would become its first prompt). The manifest keeps the user's instruction, not the preamble.
  - `.lazyagent.toml` `[agents] worker_preamble` replaces it, `""` turns it off; `[agent] worker_preamble` is read too, `[agents]` wins.
  - The orchestrator prompt only gains a short "Briefs" note.
- Features `gestures` and `decisions`. Schemas: `gesture`, `timeline_entry`, `decision*`, the new methods, `status.timeline`, `decision.*`, `status_row.{class, seen_at, last_event_at}`, `actionable.{class, decision_id}`, the counts, `notes.updated.sections`; golden fixtures for every new method.
- Desktop: only `proto` (`types.rs` carries the new fields, all optional for older engines; `golden.rs` skips the new methods and events). No UI.
- Not in I4a: the orchestrator's routing, presence, mandate, caps on autonomy, escalation and Revert (I4b); the desktop's gestures, To sit down with zone and per-item dots (I4c); answers and the decision view (I5).
- Tests: `tests/engine/test_overlay.py`, `test_decisions.py`, `test_worker_preamble.py`, MCP in `tests/test_mcp_status.py`.

**I4b** (`inbox-i4b-first-responder`; the engine side of the orchestrator as first responder, no UI):

- `engine/orchestrator.py`: `FirstResponder`, owned by the engine and run on every status pass (events and the 60 s tick). Nothing in it calls a model or wakes the orchestrator on a timer.
- **Routing.** A new brief goes to the orchestrator first (`holder: "orchestrator"`, `routed {at, mandate}`) when one is alive, the item is not kept for the user, the brief is not `reversible: no`, and no cap is reached. A held brief has no inbox item; the row reads `orchestrator · looking` (rule 14, ball `agent`, in flight) unless the user has other items there (rule 2 still wins; claims are rule 13 and come first). A tool approval is never routed.
  - **Deviation:** `reversible: no` is not routed at all. §5c's table says escalate in both columns, so waking the orchestrator would only delay the user by up to `escalate_after_min`.
  - **Plain questions** (an agent in `waiting_for_user` with no open brief) become a record of their own, `source: "question"`, quick and blocking, answered with free text. It resolves when the agent leaves `waiting_for_user` (answered in the terminal). Without an orchestrator, or with the item kept, the old `question` item stays as it was.
  - **Wake:** one line per item, `Item <key>: decision <id>. Read it with status_list / decision_get.` (`question <id>` for a question), sent when the orchestrator's turn is over (never mid-turn), through a mailbox shared with answer delivery. A line whose item was resolved meanwhile is dropped.
- **Resolution.**
  - `decision_answer {id, choice, comment?, reason}` with the orchestrator's token, only on a brief it holds; `reason` is required. The answer goes to the worker as `Decision <id>: <choice> — <label>. <comment>` when it is idle (`waiting_for_user` counts as idle for a worker). The worker's notes get `- <date> · <question> → <choice> (orchestrator, <presence>): <reason>` under Decisions (an append, by `orchestrator`; nothing when the agent has no notes). The brief becomes `answered` with `answer {choice, label?, comment?, reason?, presence?, at, delivered_at?}`, `answered_by`, `revertible`, and `decision.answered` is emitted.
  - The user's `decision_answer` (no token) shares the code path for any open brief: same delivery and notes line `(user): <comment>`, `revertible: false`. I5 only adds the decision view on top.
  - `decision_escalate {id, note, suggestion?}`: back to the user (`holder: "user"`, `escalation {by, note, suggestion, at}`); the item's detail reads `orchestrator suggests B: <note>`. The engine escalates on its own after `escalate_after_min` (10, `orchestrator did not answer`), at once when the orchestrator exits (`orchestrator gone`) or the user keeps the item (`kept for you`).
- **Presence:** `presence_report {input_at}` and `presence_set {mode: auto | present | away}` (the user's, no token). `status_list` carries `presence {mode, effective, since}`. Auto means present while the last reported input is under `away_after_min` (15) old. No UI connected (no protocol v1 connection and no hosted UI) is always away, even with the override set to present. Presence lives in memory: an engine restart starts at auto.
- **Mandate.** The item's mandate is `none` (the new `status_gesture {kind: "keep_for_me"}`, persisted in the overlay and cleared by `clear` or a pass), `claim` (the claim's `allowed`), or the presence.
  - The table is code: `AWAY_ACTIONS` = `answer_quick` (reversible yes and quick), `answer_reversible` (yes, deep), `answer_partly` (partly or not given), `relay`, `restore_owner`, `ci_rerun`, `rebase_via_owner`, `spawn_worker`, `next_project_task`. `PRESENT_ACTIONS` = `answer_quick`, `relay`, `restore_owner`, `ci_rerun`; the rest are "propose" while present. A refusal names `decision_escalate`.
  - `NEVER_ALONE` is fixed. `[orchestrator] away_allow` and `allow_outward` only drop names (unknown names are ignored), and an action dropped from `away_allow` is not done while present either.
  - Claims now carry the away list as shortened by `away_allow`, plus `mark`. I3's mandate checks are now applied to claimed items.
  - **What the engine enforces** is its own methods only. Answers are checked. `spawn_agent` with the orchestrator's token on a work item it holds (a routed brief or a claim) is checked as `spawn_worker`, logged as an action, and its inverse (stop that agent) is attached to the latest revertible answer there. A spawn on an item it does not hold is the user's word in chat: caps only (I1). `remove_worktree` with the orchestrator's token is refused when the worktree has uncommitted changes or commits on no remote (never alone: delete unpushed work). MCP `remove_worktree` now sends the token. The engine has no merge, publish, force-push or PR close method, so those never-alone rows stay with the instructions and Claude Code's permissions.
- **Caps:** `max_decisions_per_item_day` (3) and `max_decisions_day` (20), counted by local calendar day in the overlay (`orchestrator {day, decisions, items, auto_actions, auto_reverted}`), so a restart keeps them. Past either cap, a new brief stays the user's with class `deep` and `capped` set, an answer is refused, and questions are not routed. `status_list` counts gain `auto_actions` (answers and actions) and `auto_reverted`.
- **Log and Revert.**
  - Every orchestrator answer, escalation or action adds a timeline entry with `actor: orchestrator`, `mandate`, `action`, `reason` and `inverse` (new timeline kinds `action`, `delegate`).
  - An answer records `inverse: [{kind: git_reset, sha: <head at answer>, branch}]`.
  - `decision_revert {id, choice?, comment?}` (the user's) sends `Decision <id> overridden by the user: <choice> — <label>. <comment>`. It stops the agents the orchestrator spawned for the item, then resets the branch with `git reset --keep <sha>`, but only when: the worktree is on the same branch, no agent is running there, the sha is under HEAD, none of the commits since are on a remote, and `--keep` keeps local changes. Otherwise it records why it skipped. The old head stays in the result (`reset to abc1234 (was def5678)`) and in the reflog.
  - Revert counts as a user gesture (the row's next change is the user's) and adds to `auto_reverted`.
- **Auto-delegation** (`engine/autodelegate.py`, §7):
  - `[inbox] auto_delegate` takes `conflict_with_base` (the `conflict` item), `agent_dead` (`exited`) and `ci_red` (accepted, inactive until I7). `auto_delegate_target` is `owner` or `orchestrator` (a claim by `engine`), and `auto_delegate_max_per_day` is 10.
  - Owner: a live agent gets a fixed brief through the mailbox (when idle); a dead one with a restorable entry is restored, then gets it; otherwise one is spawned with it, within the spawn caps.
  - Limits: 2 attempts while the condition lasts. An attempt fails when the item comes back after it left (the agent worked); the count resets once the condition clears with no agent running. Never on the main worktree, `default_branch`, `main` or `master`; never on a teammate's branch (its last commit's author email is not `git config user.email`).
  - After 2 failures: an engine brief, `source: "auto_delegate"`, deep, the user's, with the attempts and the agent's last 8 lines as `tried`.
  - `inbox_config_get {repo?}` → `{repo, auto_delegate, auto_delegate_target}`, read from the file. `inbox_config_set {repo?, auto_delegate, auto_delegate_target}` (the user's) rewrites only those two keys' lines in `[inbox]` (multi-line arrays included), adds what is missing, creates the table or the file when needed, parses the result before an atomic replace, and reloads the config. An inline or dotted `inbox` table is refused (edit by hand).
- Orchestrator prompt: a "Routed items" note (decision_get, answer within mandate, escalate otherwise, release claims). MCP `decision_get`, `decision_answer`, `decision_escalate`.
- Feature `first_responder`; event `decision.answered`; schemas for the seven methods, `presence`, the decision's new fields, `status_row.holder`, the counts, the timeline entry's fields, `status_gesture` `keep_for_me` / `kept`; golden fixtures for every new method. Desktop: `proto` only (`StatusRow.holder`, `StatusCounts.auto_actions` / `auto_reverted`, `StatusList.presence`, `StatusGestureResult.kept`; `golden.rs` skips the new methods and ignores `decision.answered`).
- Tests: `tests/engine/test_orchestrator_first_responder.py`, `tests/engine/test_autodelegate.py` (git cases in a temporary repo, `HOME` isolated).

---

## 9. Open questions (answered 2026-10-23)

The owner answered all 43. The recommendation stands unless the row starts with **Decided:** (Q-B1, Q-B17, Q-B18, Q-B32) or carries an owner's note (Q-B33, Q-B34).

| # | Question | Recommendation |
|---|---|---|
| Q-B1 | Gate after I1: run one week of the CLI/MCP table before building the desktop Home? | Yes. Metric: status questions to the orchestrator per day (baseline 5-10) and hand corrections per day. Go on if both drop ≥ 50 % — **Decided:** no gate (B); I2 and I3 follow I1 directly. Status questions and hand corrections per day stay counted as a non-blocking success metric |
| Q-B2 | One list with two zones, or two tabs (Inbox, Status)? | One list. An item is in one zone only. Two tabs bring back "two places to check" |
| Q-B3 | Work item key: `repo:branch`, worktree, or PR? | `repo:branch` with PR and tracker URLs as aliases. It survives worktree cleanup and gains a PR |
| Q-B4 | Ball vocabulary: `agent · others · stalled · done` (+ `you` = inbox), or the planner's six lanes? | Ball. Lanes stay in the planner; a label can carry a lane name |
| Q-B5 | State column: engine facts only, or the agent's note? | Facts only. The agent's note (report or notes Doubts/Pending) shows in the expanded row with its age |
| Q-B6 | Editable? Which gestures? | Label (expires on fingerprint change), Park until event, Close. No drag, no free state. Count gestures per day as a rule-quality metric |
| Q-B7 | Does a label pin the ball or the state? | The ball only. The state stays a fresh fact ("others: QA · conflict with base") |
| Q-B8 | Levels: fixed table per kind, or a score inside a level? | No visible score. Fixed table per kind; inside a level, fixed sort keys: (1) blocks a live agent, (2) priority inherited from the item's work item (high/medium/low, default medium), (3) age, oldest first. Every item belongs to a work item, linked by the ticket ID in the branch name (`models.py` `ticket_id`) or the `branch` of `work_items_upsert`, which gains `priority` (I6). In flight sorts by ball, then priority. A full score only if Round regularly passes 7 items. Supersedes 0012's deterministic score + agent tie-break. Alternatives: pure FIFO; a score now |
| Q-B9 | Rounds: deliver at cut points in I3, or show items as they come? | Cut points in I3 (close an item, back from a terminal, every 30 min). Now items always at once |
| Q-B10 | Notifications in the MVP? | Only Now items, once per occurrence, by the desktop; the engine notifies with no UI attached (0012 Q-A11). Rounds as one notification later |
| Q-B11 | `status_report` from agents: in the orchestrator prompt and the worker brief? | Yes, in I4, as optional ("report when you need the user or change ball"). Rows never depend on it |
| Q-B12 | Planner integration: who changes the planner skill, and when? | The owner, after I6 lands; until then the planner keeps its board and can already call `status_list` (I1) instead of building tables |
| Q-B13 | Show external items with no branch as rows or only as a counter? | Counter "N not started" in the MVP; rows only when `needs_user` or `ball: others` (e.g. a peer review) |
| Q-B14 | The web UI: port Home? | No (frozen). It keeps its client-side attention |
| Q-B15 | The TUI | Badge with Now/Round counts and "open in desktop" (strategy §4); `lazyagent status` covers SSH |
| Q-B16 | Auto-delegation of red CI (§7): when, and default? | After I7, opt-in, owner agent only. Revisit 0012 A3's separate fixer only with evidence. Widened by Q-B33 |
| Q-B17 | Tracker adapters: in the MVP or after? | **Decided:** in the MVP. I8a (GitHub Issues) alongside I2, I8b (Jira) alongside I3. `work_items_upsert` (I6) stays for other feeders |
| Q-B18 | Which adapters ship in the core? | **Decided:** GitHub Issues and Jira, both complete (not examples). Jira against the Jira Cloud REST API v3, tested with recorded fixtures (transitions, JQL search, pagination, 429), experimental until validated on a real instance. No other tracker adapter in the core; others as third-party packages through the `lazyagent.trackers` entry point |
| Q-B19 | Inbox items from tracker states: on entering a status, or while in it? | On entering (edge), auto-clear on leaving. Avoids re-creating dismissed items on every poll |
| Q-B20 | Tracker state vs engine facts when they disagree | Engine facts decide the ball; the tracker text is shown beside them |
| Q-B21 | Write back to the tracker (move a card on merge)? | No. Read-only in this plan. Scope: it applies to the engine and its tracker adapters only (no automatic writes by lazyagent itself, e.g. moving a card on merge). It does not restrict agents: workers and the orchestrator keep writing to the tracker (comments, status fields, moving cards) and to GitHub as their own skills and instructions say; lazyagent neither mediates nor blocks those calls |
| Q-B22 | Credentials | Env var or OS keyring only, referenced by name in the global config; per-repo files may change mappings, never credentials |
| Q-B23 | Name and shape of the brief tool | `request_decision` with the §5b template (question, 2-4 options, recommendation, tried, evidence, reversibility, impact, class, blocking) |
| Q-B24 | Who sets Quick vs Deep? | A fixed rule per kind; the agent's brief may declare `quick`; never a model. A brief that fails validation is Quick |
| Q-B25 | Limit of "To sit down with" and what happens over it | `deep_max = 5`, shown on the zone; over it a banner. Refusing new work comes with the backpressure phase, not before |
| Q-B26 | Does an agent stop while its brief waits? | Only with `blocking: true` (status `waiting_for_user`); otherwise it keeps working on other parts and gets the answer when idle |
| Q-B27 | Where does the "since you looked" timeline live? | The status overlay, a ring of 50 entries / 7 days per item; it moves to persistent history (0007) when that lands |
| Q-B28 | "Seen" per item or global? | Per item, plus "mark all seen" on Home; the global last look stays for the dot |
| Q-B29 | How does the answer reach the agent? | The review-reply queue: `send_agent_input` when idle, never mid-turn; one line with the choice and the comment |
| Q-B30 | "Explain this": who answers and how much? | Always only on the user's click, ≤ 10 lines, via `decision_explain`. Chain: (1) owner agent alive → it answers; (2) owner not alive but restorable → explicit "Restore agent and explain" (not automatic: it starts a process and costs more), restored with its notes, then answers; (3) not restorable, orchestrator running → the orchestrator answers from notes, diff, CI and timeline, marked "explained by orchestrator"; (4) none → disabled, with "Open terminal" and "Pass to orchestrator". No fallback to a bare model call. Alternatives: owner only, disabled otherwise (the previous recommendation); automatic restore |
| Q-B31 | Where is the decision log kept? | The agent's notes, section Decisions (re-injected after compaction, inherited in handoffs), plus a 30-day copy on the work item |
| Q-B32 | A brief whose evidence is outdated (the branch moved) | Evidence pinned to a commit; "outdated" banner in the view; the user can still answer or press Explain this — **Decided:** as recommended, plus a "Request a new brief" action in the decision view (`decision_rebrief_request`, same chain as Explain this) |
| Q-B33 | Opt-in auto-delegation of stalled items (conflict with no agent, dead agent, CI red with no agent)? | Yes, opt-in per kind (`[inbox] auto_delegate = [...]`, default empty), fixed rule, owner agent only (restore with notes; spawn with the brief only if none), concurrency limits, ≤ 2 attempts per item per fingerprint, then To sit down with with a brief. I4 or later (`ci_red` after I7). Alternatives: never automatic; automatic by default — Owner's note: also configurable from the desktop UI (settings menu), not only `.lazyagent.toml` |
| Q-B34 | How do agents learn `status_report` / `request_decision` / notes? | Four layers, no plugin (§7a): (a) tool descriptions, (b) worker preamble at spawn, (c) Claude hook nudge, (d) engine backstop. a+b+d in I4, c after. Alternatives: only tool descriptions; a plugin/skill — Owner's note: the owner's own skills will also be improved (outside this repo) |
| Q-B35 | Principle: split state from action? | Yes. State (rows, ball, level, class, zone) is the engine's, deterministic, no LLM. Action may be taken by an LLM only as a delegate, under engine-enforced limits, every action visible on the row. Alternatives: no LLM anywhere (the orchestrator stays outside the core, unsupported); allow LLM summaries in the state path |
| Q-B36 | Orchestrator as first responder for worker questions and briefs, and the escalation timeout | Yes, when one is running and the item's mandate allows; it answers within mandate or escalates; the engine escalates after N = 10 min. Tool-permission approvals are not routed. Alternatives: user first always (orchestrator only on Pass); no timeout |
| Q-B37 | Mandate by presence, and how presence is detected | Present → propose, decide only small reversible things; away → decide everything reversible. Presence = desktop input in the last 15 min, overridden by a Home toggle; no UI → away. Alternatives: one mandate always (propose only); a mandate set per item |
| Q-B38 | What the orchestrator never does alone, and what it may do outward | Two groups. **Never alone** (irreversible / high impact): merge to default/protected, publish (tag/release/package), force-push a shared branch, delete a branch/worktree with unpushed work, close/reopen a PR; fixed in code for actions that go through engine methods. **Outward but routine** (what the skills prescribe): issue-tracker comments/fields, moving cards, creating PRs, PR comments; allowed when its skill says so, `[orchestrator] allow_outward = ["tracker", "github_comment", "pr_create"]` by default, the owner can narrow it; logged on the row as "done by orchestrator" when reported. The engine enforces only its own methods; direct tracker MCP or `gh` calls bypass it, so the guarantee there is the skill plus Claude Code permission settings (deny a tracker MCP's tools as a hard block); agents may report outward actions best-effort via `status_report`. Alternatives: keep tracker/GitHub writes in never-alone; a fully configurable list |
| Q-B39 | Caps on autonomous decisions and Revert | 3 per item per day, 20 per day; past them → To sit down with. Revert on the row (I4) and in the decision view (I5); autonomous actions and reverts counted apart from gestures. Alternatives: no caps (trust the log); only a daily cap |
| Q-B40 | "Pass to orchestrator" gesture and claim timeout | Yes, at I3; claim TTL 2 h with the mandate's allowed actions; on expiry or orchestrator death → Round with "delegation lapsed". Alternatives: 30 min TTL; no gesture (rules only) |
| Q-B41 | Projects as work items with `depends_on`, and the per-project continue policy | Yes, at I6: `project` + `depends_on` in `work_items_upsert`; the skill keeps the DAG and stacked PRs; `continue_while_review` (default off) run by the orchestrator within mandate and caps. Alternatives: projects only in the skill; the engine runs the DAG |
| Q-B42 | I1 prerequisites and the external planner's tick | Yes, in I1: spawn/concurrency caps in the engine, authenticated orchestrator role (no token ≠ orchestrator), orchestrator reads `status_list`. An external planner moves from a fixed-interval tick to engine events once they exist (its change stays the owner's). Alternatives: prerequisites with I4; keep the tick |
| Q-B43 | Discovering new tasks | The engine never invents tasks. Sources: local state (I1); GitHub, deterministic (review requests to the user, PRs/CI of their branches; I1/I7); an external planner as feeder (I6); optional read-only tracker adapters (I8a/I8b, MVP). Discovered tasks show as "N not started", rows only when they need the user. The user starts them, or the orchestrator under a project's continue policy and its mandate; never the engine. Gap once the planner is event-driven (Q-B42): (b) the tracker adapters poll the tracker (`assigned_to_me`); with Q-B17 they are in the MVP (I8a/I8b); (a) the planner's light deterministic discovery poll (API calls, no LLM) remains only as its interim. Alternatives: only (a); only (b) and accept the gap until I8 |
