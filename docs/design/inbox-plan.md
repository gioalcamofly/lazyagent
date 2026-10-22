# Inbox and status: implementation plan

- **Status:** Proposed (I0, 2026-10-22). Supersedes the phasing of [actionables-plan.md](actionables-plan.md). Each phase gets an "As landed" note in §8 when it merges.
- **Proposal:** [0013-inbox-and-status.md](../proposals/0013-inbox-and-status.md). This plan does not repeat its rationale. It covers only what to build, where, and how to prove it.
- **Base:** `develop` @ `5776c62`.
- **MVP** = I1-I3: the status projection with a CLI/MCP table (and a 1-week test that gates the rest), the engine inbox queue with fixed levels, and the desktop Home. I4-I8 come after (I4-I5: decisions that need context; I8: optional issue-tracker adapters). Auto-triage is an opt-in phase after that.
- **Hard rule:** no agent is spawned and no LLM is called at runtime to keep the list current. Rows come from deterministic reads (engine state, `gh`, git, notes) and from MCP reports by agents that are already running. Any summariser/classifier model is a future opt-in idea, not part of any phase here.

## TL;DR

| # | Branch | Scope | One-line acceptance |
|---|---|---|---|
| I0 | `docs-inbox-status-board` | Proposal 0013 + this plan; 0012 and its plan marked superseded | Merged |
| I1 | `inbox-i1-status-projection` | `engine/status.py`: work items as a projection of worktrees + agents + PR + jobs; ball/state/level derivation (§3); engine-owned PR interest; `status_list` method + `status.updated`/`status.removed` events + `status` feature; MCP `status_list`; `lazyagent status` CLI | With no UI open, `lazyagent status` lists every worktree's branch (agent or not) with ball, fact state and age; the orchestrator answers a status question with one `status_list` call |
| — | (gate) | One week of real use (Q-B1) | Status questions to the orchestrator drop ≥ 50 % and the table needs fewer hand corrections than before. If not, stop at I1 and keep the CLI |
| I2 | `inbox-i2-engine-queue` | 0012 A1, trimmed: `engine/actionables.py` store, today's 10 attention kinds derived in the engine, the fixed kind → level table, marks (dismiss, park) in the engine, `actionables_*` methods + events + feature. No score, no criteria file | An agent waiting on approval yields a Now item with no UI open; dismissing it in one client hides it in the other; `status_list` reports `needs_you` from the queue |
| I3 | `inbox-i3-desktop-home` | Desktop Home: inbox (Now, Round) grouped by work item + in-flight list; rounds; "changed since last look"; one-key actions; noise cut (path, "engine connected", age "0s", Stop next to Open); fallback to the client derivation for old engines | Opening the desktop shows Home, not a terminal; an item is shown once; a new item never reorders the list under the cursor |
| I4 | `inbox-i4-gestures-briefs` | Store for what cannot be derived: label (ball + text, expires on fingerprint change), park until event, close, 24 h tombstones; MCP `status_report` with its expiry rules. **Quick/Deep classes** (fixed rule per kind) and the **To sit down with** zone with `deep_max`; MCP `request_decision` (brief template + validation, §5b); **since you looked** timeline per work item and per-item seen marks | A row labelled "others: QA" keeps its label through a conflict and loses it on merge; a brief with options and evidence lands in To sit down with, one without evidence becomes a Quick item; the timeline lists a commit and a CI run made while the user was away |
| I5 | `inbox-i5-decision-view` | Desktop decision view (brief \| evidence \| timeline), `decision_answer` delivered through the review-reply queue, **Explain this** (`decision_explain_request` / `decision_explain`), decision log in the agent's notes (Decisions) and on the work item | Answering B sends the agent one line when it is idle and appends it to its notes; a restored agent sees it after compaction; Explain is disabled with no live agent |
| I6 | `inbox-i6-feeders` | MCP `work_items_upsert` / `work_items_close` (dedupe by URL, cap per source, `expires_at`); "N not started" counter; link a URL to a branch by its ticket id; the planner change (owner's file, owner's go) | A tracker card with no branch shows in the counter and, once a branch with its ticket id exists, joins that row |
| I7 | `inbox-i7-gha` | 0012 A2: Actions runs per repo (REST + ETag), `PrInfo.head_sha`, "fresh red on head", CI items for branches with no PR; "Delegate to the owner agent" action | A red run on a branch with no PR becomes a Round item; one key sends its failed-log summary to the branch's live agent |
| I8 | `inbox-i8-trackers` | Optional `TrackerAdapter` protocol, GitHub Issues reference adapter, status mapping, edge-triggered inbox rules, polling with cache/ETag/caps, credentials by env/keyring, `trackers.toml` (global + per repo); a generic Jira example adapter (§7b) | With GitHub Issues on, a branch `fix-123-…` whose issue gets the label mapped to "returned" yields a Round item; with no adapter configured, `status_list` output is byte-identical |
| later | — | Rounds as notifications at cut points, Focus mode, backpressure (Round > 7 stops new work), ntfy notifier; **opt-in auto-delegation of red CI to the owner agent** (0012 A3 trimmed, §7) | — |

Each phase is its own branch from up-to-date `develop`, merged with `--no-ff` once `uv run pytest` and (for desktop phases) `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and `cargo test` are green. Every phase updates the docs it changes (engine-split §4d/§4e/§4g, ui-plan §3b, desktop-app D4, this plan §8, proposal status).

**Owner time:** I1 ~1.5 h of review, I2 ~1 h, I3 ~2 h, I4 ~1.5 h, I5 ~1.5 h, I6 ~1.5 h (plus the planner change), I7 ~1 h, I8 ~1 h. Agents implement.

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
| A generic ticket id regex (`ABC-123`) already reads the key from the branch name | models.py `ticket_id` | I6/I8 use it to link tracker URLs and tasks to rows; an adapter may override the pattern |
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
  "external": {"ext:<url>": {"title": "...", "ball": "others", "state": "with QA", "needs_user": false, "branch": null, "source": "planner", "expires_at": 0}},
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
| method | `status_list {repo?, include_done?}` | I1 | `{rows: [Row], counts: {now, round, in_flight, not_started, stale}, generated_at}`, inbox first (level, then FIFO), then in-flight by `changed_at` desc |
| method | `status_seen {key?, at}` | I3, I4 | Stores "last look" per client; with `key` (I4), per work item |
| method | `status_timeline {key, since?}` | I4 | The item's timeline entries since the last look (or `since`) |
| method (agent) | `request_decision {question, options, recommendation, tried, evidence, reversible, impact_if_wrong, class?, blocking?}` / `decision_withdraw {id}` | I4 | Agent token; validation and limits in §5b |
| method | `decision_answer {id, choice, comment?}` | I5 | User; delivered like a review reply, logged in notes Decisions |
| method | `decision_explain_request {id \| key, question?}` | I5 | User; asks the owner agent; disabled with no live agent |
| method (agent) | `decision_explain {id, text}` | I5 | Agent token; the reply shown in the decision view |
| event | `decision.created` / `.answered` / `.resolved`, `status.timeline {key, entry}` | I4, I5 | — |
| event | `status.updated {row}` / `status.removed {key}` | I1 | Coalesced to 1/s |
| feature | `actionables` | I2 | As 0012 A1 |
| method | `actionables_list` / `_get` / `_mark {id, action: dismiss \| park \| unpark, until?}` | I2 | `park` takes an event (`ci_done`, `agent_stopped`, `human_review`) or a time |
| event | `actionable.created` / `.updated` / `.resolved` | I2 | As 0012 |
| method | `status_gesture {key, kind: label \| park \| close \| clear, ball?, text?, until?}` | I4 | User only |
| method (agent) | `status_report {ball?, note, needs_user?}` | I4 | Agent token; scoped to the agent's own work item |
| method (feeder) | `work_items_upsert {url, title, ball?, state?, needs_user?, branch?, expires_at?}` / `work_items_close {url}` | I6 | Source from the token; `max_open_per_source` 50; the engine's facts win over a feeder's `state` |
| MCP | `status_list` (I1), `status_report`, `request_decision`, `decision_withdraw` (I4), `decision_explain` (I5), `work_items_upsert/close` (I6) | — | No arguments → the caller's own scope where it applies |
| CLI | `lazyagent status [--json] [--all]` | I1 | Same rows as `status_list` |
| config | `[status] stale_days, stalled_hours, ticket_pattern`, `[inbox] round_every_minutes, deep_max, max_briefs_per_agent` | I1, I3, I4, I6 | `14, 24, null, 30, 5, 2` |

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

**Answer (I5).** `decision_answer {id, choice, comment}` from the user. The engine:
1. queues the answer for the agent and delivers it like a review reply: `send_agent_input` when the agent is idle, never in the middle of a turn (text: `Decision <id>: <choice> — <label>. <comment>`);
2. appends `- <date> · <question> → <choice> (user): <comment>` to the agent's notes, section Decisions (engine write, no `if_rev` race: append only);
3. keeps a copy on the work item for 30 days.

**Explain this (I5).** `decision_explain_request {id | key, question?}` from the user sends the owner agent a fixed prompt: "Explain <item> for the user in at most 10 lines; reply with `decision_explain {id, text}`." The reply shows in the view. With no live agent the button is disabled. Never automatic.

---

## 6. Files to touch

| Phase | Files |
|---|---|
| I1 | `engine/status.py` (new: membership, derivation, projection, coalesced events), `engine/engine.py` (own it; feed events), `engine/worktrees.py` (`engine:status` interest), `engine/notes.py` (public `section_bullets`), `engine/methods.py`, `engine/server.py` (`FEATURES`), `engine/events.py`, `mcp_server.py` (`status_list`), `cli.py` / engine CLI (`lazyagent status`), `config.py` (`[status]`), `protocol/schema/v1/*`, `tests/engine/test_status.py` (derivation table as golden tests) |
| I2 | `engine/actionables.py` (new, 0012 A1 minus score), `engine/status.py` (`needs_you` from the queue), schemas, `tests/engine/test_actionables.py` (port of attention.rs tests) |
| I3 | desktop: `proto/src/types.rs` (`StatusRow`, `Actionable`, events), `app/src/home_view.rs` (new), `app/src/attention.rs` (fallback only), `app.rs` (default view, keys, noise cut), `persist.rs` (marks import once) |
| I4 | `engine/status.py` (overlay store, gestures, report expiry, timeline ring, per-item seen), `engine/decisions.py` (new: brief validation, limits, lifecycle), `engine/actionables.py` (class per kind, Deep zone), `mcp_server.py` (`status_report`, `request_decision`, `decision_withdraw`), `orchestrator_prompt.py` (when to write a brief), desktop gestures + To sit down with zone |
| I5 | desktop `decision_view.rs` (new: brief \| evidence tabs \| timeline, reusing `diff_view.rs`), `engine/decisions.py` (answer queue through the review-reply path, explain request), `engine/notes.py` (engine append to Decisions), `mcp_server.py` (`decision_explain`) |
| I6 | `mcp_server.py` (`work_items_*`), `engine/status.py` (external rows, ticket link), `models.py` (configurable ticket pattern); the planner skill (owner's file, owner's go) |
| I8 | `engine/trackers/__init__.py` (protocol, registry, entry point `lazyagent.trackers`), `engine/trackers/github_issues.py`, `engine/trackers/jira_example.py`, `engine/status.py` (row enrichment), `engine/actionables.py` (`tracker_status` kind), `config.py` (`trackers.toml` loader), tests with fake HTTP |
| I7 | `worktree_manager.py` (`head_sha`, Actions runs with ETag, jobs), `models.py` (`WorkflowRun`), `engine/actionables.py` (`ci_failed`), desktop "delegate" action |

---

## 7. Later: opt-in auto-delegation of red CI

0012 A3, trimmed. Off by default (`[inbox.autodelegate] enabled = false`).

- Trigger: 0012's "fresh red on head" (I7).
- Action: if the branch has a **live agent of its own** that is idle, the engine sends it the failure brief (the same text as the manual `d` key). It never spawns a separate fixer or a triage agent in this first version. A busy agent gets it when it is idle; no agent → stays a Round item.
- Limits from 0012 stay as code: `max_attempts` per branch + signature (2), `max_per_day` (10), never on default/protected branches, never on a teammate's branch, a dirty worktree with no agent is skipped.
- Verify on the next run; red twice → Round item "CI still red after 2 tries".
- Spawning a fixer for branches with no agent, the Haiku triage agent and revert handling (0012 Q-A1, Q-A7) are reconsidered only if this proves useful.

---

## 7b. Optional tracker adapters (I8)

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
base_url = "https://example.atlassian.net"
token_env = "JIRA_TOKEN"            # or keyring = "lazyagent/jira"
poll_s = 300
max_calls_per_min = 30
[jira.status]
"In Review" = { ball = "others", state = "code review" }
"Reopened"  = { ball = "you", level = "round", state = "returned" }
```

- A fetch error keeps the last good value and marks it stale; it never removes the row or the item.
- Adapters run in the engine's poll loop, not as agents; each call has a timeout.
- The engine's own facts (PR merged, agent waiting) win over a tracker status on ball; the tracker text shows next to them ("in QA · CI green").

## 8. As landed

(empty)

---

## 9. Open questions

| # | Question | Recommendation |
|---|---|---|
| Q-B1 | Gate after I1: run one week of the CLI/MCP table before building the desktop Home? | Yes. Metric: status questions to the orchestrator per day (baseline 5-10) and hand corrections per day. Go on if both drop ≥ 50 % |
| Q-B2 | One list with two zones, or two tabs (Inbox, Status)? | One list. An item is in one zone only. Two tabs bring back "two places to check" |
| Q-B3 | Work item key: `repo:branch`, worktree, or PR? | `repo:branch` with PR and tracker URLs as aliases. It survives worktree cleanup and gains a PR |
| Q-B4 | Ball vocabulary: `agent · others · stalled · done` (+ `you` = inbox), or the planner's six lanes? | Ball. Lanes stay in the planner; a label can carry a lane name |
| Q-B5 | State column: engine facts only, or the agent's note? | Facts only. The agent's note (report or notes Doubts/Pending) shows in the expanded row with its age |
| Q-B6 | Editable? Which gestures? | Label (expires on fingerprint change), Park until event, Close. No drag, no free state. Count gestures per day as a rule-quality metric |
| Q-B7 | Does a label pin the ball or the state? | The ball only. The state stays a fresh fact ("others: QA · conflict with base") |
| Q-B8 | Levels: fixed table per kind, or a score inside a level? | Fixed table + FIFO in the MVP. A score inside Round only if the Round list regularly passes 7 items |
| Q-B9 | Rounds: deliver at cut points in I3, or show items as they come? | Cut points in I3 (close an item, back from a terminal, every 30 min). Now items always at once |
| Q-B10 | Notifications in the MVP? | Only Now items, once per occurrence, by the desktop; the engine notifies with no UI attached (0012 Q-A11). Rounds as one notification later |
| Q-B11 | `status_report` from agents: in the orchestrator prompt and the worker brief? | Yes, in I4, as optional ("report when you need the user or change ball"). Rows never depend on it |
| Q-B12 | Planner integration: who changes the planner skill, and when? | The owner, after I6 lands; until then the planner keeps its board and can already call `status_list` (I1) instead of building tables |
| Q-B13 | Show external items with no branch as rows or only as a counter? | Counter "N not started" in the MVP; rows only when `needs_user` or `ball: others` (e.g. a peer review) |
| Q-B14 | The web UI: port Home? | No (frozen). It keeps its client-side attention |
| Q-B15 | The TUI | Badge with Now/Round counts and "open in desktop" (strategy §4); `lazyagent status` covers SSH |
| Q-B16 | Auto-delegation of red CI (§7): when, and default? | After I7, opt-in, owner agent only. Revisit 0012 A3's separate fixer only with evidence |
| Q-B17 | Tracker adapters: in the MVP or after? | After (I8). The planner can feed tracker states through `work_items_upsert` (I6) meanwhile, so the MVP loses nothing |
| Q-B18 | Which adapters ship in the core? | GitHub Issues (reference, via `gh`). Jira as a generic example. Others as third-party packages through the `lazyagent.trackers` entry point |
| Q-B19 | Inbox items from tracker states: on entering a status, or while in it? | On entering (edge), auto-clear on leaving. Avoids re-creating dismissed items on every poll |
| Q-B20 | Tracker state vs engine facts when they disagree | Engine facts decide the ball; the tracker text is shown beside them |
| Q-B21 | Write back to the tracker (move a card on merge)? | No. Read-only in this plan |
| Q-B22 | Credentials | Env var or OS keyring only, referenced by name in the global config; per-repo files may change mappings, never credentials |
| Q-B23 | Name and shape of the brief tool | `request_decision` with the §5b template (question, 2-4 options, recommendation, tried, evidence, reversibility, impact, class, blocking) |
| Q-B24 | Who sets Quick vs Deep? | A fixed rule per kind; the agent's brief may declare `quick`; never a model. A brief that fails validation is Quick |
| Q-B25 | Limit of "To sit down with" and what happens over it | `deep_max = 5`, shown on the zone; over it a banner. Refusing new work comes with the backpressure phase, not before |
| Q-B26 | Does an agent stop while its brief waits? | Only with `blocking: true` (status `waiting_for_user`); otherwise it keeps working on other parts and gets the answer when idle |
| Q-B27 | Where does the "since you looked" timeline live? | The status overlay, a ring of 50 entries / 7 days per item; it moves to persistent history (0007) when that lands |
| Q-B28 | "Seen" per item or global? | Per item, plus "mark all seen" on Home; the global last look stays for the dot |
| Q-B29 | How does the answer reach the agent? | The review-reply queue: `send_agent_input` when idle, never mid-turn; one line with the choice and the comment |
| Q-B30 | "Explain this": who answers and how much? | The owner agent only, on click, ≤ 10 lines, via `decision_explain`; disabled with no live agent. No fallback model |
| Q-B31 | Where is the decision log kept? | The agent's notes, section Decisions (re-injected after compaction, inherited in handoffs), plus a 30-day copy on the work item |
| Q-B32 | A brief whose evidence is outdated (the branch moved) | Evidence pinned to a commit; "outdated" banner in the view; the user can still answer or press Explain this |
