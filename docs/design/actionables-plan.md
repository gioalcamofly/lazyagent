# Actionables: implementation plan

- **Status:** **Superseded by [inbox-plan.md](inbox-plan.md)** (2026-10-22). Not implemented. A1 survives trimmed as I2, A2 as I6, A3 as an opt-in later phase; A4's score, A5's tie-break and boost, and A7 are cut. Kept as the reference for the data model and the layer-1 rules.
- **Proposal:** [0012-actionables.md](../proposals/0012-actionables.md). This plan does not repeat its rationale. It covers only what to build, where, and how to prove it.
- **Base:** `develop` @ `12f336f` (0011 MVP, agent tokens, review replies, desktop D1-D6 landed).
- **MVP** = A1-A4: engine queue (today's attention kinds), GitHub Actions detection, auto-triage with the 4 limits, deterministic layer 2 score with criteria file and notification threshold, desktop + web as views. A5-A7 come after. Layer 3 (A8+) is a sketch only.

## TL;DR

| # | Branch | Scope | One-line acceptance |
|---|---|---|---|
| A0 | `docs-actionables-proposal` | Proposal 0012 + this plan | Merged |
| A1 | `actionables-a1-engine-queue` | `engine/actionables.py`: store, today's 10 kinds derived in the engine, marks (dismiss/snooze) in the engine, `actionables_*` methods + events + capability, v1 schemas and golden fixtures | With no UI attached, an agent waiting on approval yields an `actionable.created`. Dismissing it from one client hides it in another |
| A2 | `actionables-a2-gha-detect` | Engine-owned interest (active, non-ignored worktrees), `PrInfo.head_sha`, Actions runs poller (REST + ETag, one call per repo), "fresh red" rule, signature, ignore/pause, `[actionables]` config | A failing run on a worktree branch with no PR and no client open becomes an `auto` actionable. A later green run on the same workflow resolves it |
| A3 | `actionables-a3-triage` | `engine/triage.py`: incident state machine, the 4 limits, triage agent (Haiku), `actionables_dispatch` / `_report`, fixer spawn (`spawn_agent {model}`), verify on next run, escalation, attempt ledger, restart recovery | Fake runs: red, then fixer, then push, then green resolves. Red twice escalates to human. A third incident with `max_fixers=2` waits `queued` |
| A4 | `actionables-a4-score-ui` | Criteria file (md + toml block), deterministic score + breakdown, `notify_min_score` + engine-side `notify-send` with no UI, "review requested changes" items, desktop and web consume the engine queue (fallback kept), ignore/pause toggles, auto-triage panel | The desktop order equals the engine's score order. Editing the weights in the md file re-orders live. Only items above the threshold notify |
| A5 | `actionables-a5-mcp-tiebreak` | MCP `actionables_list/create/resolve/boost`, per-source caps, orchestrator boost, Haiku tie-break with cache | An orchestrator `actionables_create` shows in the desktop queue with its source. A peer agent's boost is refused |
| A6 | `actionables-a6-planner` | The planner skill changes (tick consumes the engine queue, feeds tracker items, drops its own score and its red-CI row); the engine accepts tracker inputs | One tick creates the QA-returned card as an actionable and orders it by the engine score. Nothing in the skill computes a score |
| A7 | `actionables-a7-tui-analysis` | TUI badge + list, "prepare analysis" agent for change requests (read-only, writes notes) | From a "Review changes" item, Prepare starts an agent that fills Notes → Decisions without pushing |
| A8+ | (sketch) | Layer 3 board, external sources, Obsidian export | §7 |

Each phase is its own branch from up-to-date `develop`, merged with `--no-ff` once `uv run pytest` and (for desktop phases) `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and `cargo test` are green. Every phase updates the docs it changes (engine-split §4d/§4e rows, ui-plan §3b, desktop-app D4, this plan §8, proposal status).

---

## 1. Ground truth (what the code does today)

| Fact | Where | Consequence |
|---|---|---|
| The attention queue is derived per client: 10 kinds with a fixed rank, auto-clear by condition, `occurrence = id@since`, marks pruned after 7 days | desktop/crates/app/src/attention.rs:12-230; host/static/lib/attention.js | A1 ports the derivation to Python with the same ids and the same tests, so both clients can switch with no visible change |
| Marks and per-kind notification toggles are persisted by the desktop (`persist.rs`) and by the web (localStorage) | desktop/crates/app/src/persist.rs:2-40 | A1 owns marks in the engine. A4 imports the desktop's once and replaces the per-kind toggles with the threshold (kinds stay as a filter) |
| Notifications: `notify-send` from the desktop, only while it runs, once per occurrence | notify.rs, app.rs:409-440 | A4: the engine emits `actionable.notify`, and notifies by itself when `ui_attached` is false (engine.py:482) |
| PR info: batched GraphQL per repo, polled every 60 s for the union of client interests; `pr_info.updated` on change | engine/worktrees.py:46-54, :134-145, :376-499 | A2 adds the client `engine:actionables` to the interest set |
| `PrInfo{number, title, state, checks, url, review_decision, mergeable, base_ref}`; `CiCheck` has a name, status, conclusion and details_url | models.py:155 | A2 adds `head_sha`. Review authors are not fetched (needed for "human, not bot": A4 adds `latestReviews{author{login}, state}`) |
| Event names live in one bus with one `seq`; capabilities = methods + `FEATURES` | engine/events.py:32-46; server.py:100, :716 | New events `actionable.*`; feature `actionables`; the methods appear on their own |
| `spawn_agent(worktree_path, instruction, *, skip_permissions, resume_mode, label, kind, env, provider_name, …)`, with no model | engine.py:907 | A3 adds `model`. `kind` gains `triage` (repo root, like the orchestrator) and `fixer` (tag only; it is a normal agent) |
| Per-spawn agent token, identity env, notes per slot (`free` mode for orchestrator workers) | 0011 N2, 20-10 fix B | The fixer's brief goes into its notes Goal. `actionables_report` authenticates with the token |
| The event log and the jobs are in memory, and agents die with the engine | events.py; engine.py:279 | Ledger + queue persisted (§2). Restart recovery in A3 |
| The MCP server exposes worktrees, agents, events, restorable, notes | mcp_server.py:178-660 | A5 adds `actionables_*` tools |
| `Config` dataclasses: `[worktree] [agent] [orchestrator] [engine] [notes]` | config.py:29-83 | `ActionablesConfig` → `[actionables]` (+ `[actionables.triage]`) |

---

## 2. Data model

**Store:** `state_dir()/actionables/<repo_digest>.json`, 0600, atomic write (temp + fsync + replace, as manifest.py). Writes are coalesced (at most 1/s) via `asyncio.to_thread`.

```jsonc
{
  "version": 1,
  "items": {                         // open, escalated, recently resolved (24 h, for re-open dedupe)
    "<id>": {
      "id": "gha:<repo>:<branch>:<sig>",  // stable dedupe key
      "kind": "ci_failed",           // §2a
      "layer": "auto" | "human",
      "state": "open" | "queued" | "fixing" | "verifying" | "resolved" | "escalated" | "expired",
      "source": "engine" | "agent:<wt>/<id>" | "orchestrator" | "planner" | "external",
      "subject": {"worktree_path": "...", "branch": "...", "agent_id": null, "pr": 123, "url": "...", "run_id": 987, "head_sha": "..."},
      "title": "CI failing: tests", "detail": "jobs: tests, lint",
      "inputs": {"tracker_priority": "high", "tracker_status": "qa", "project_rank": 2, "effort_min": 25},  // optional, from feeders (A6)
      "score": 84, "breakdown": {"kind": 30, "project": 12, "completion": 28, "effort": 8, "age": 4, "boost": 2},
      "boost": {"delta": 2, "by": "orchestrator", "reason": "...", "expires_at": null},
      "first_seen": 1760950000.0, "updated_at": 1760950300.0, "expires_at": null,
      "occurrence": "<id>@1760950000.0",
      "incident": {"attempt": 1, "fixer": {"worktree_path": "...", "agent_id": "a3"}, "pushed_sha": null}  // auto only
    }
  },
  "marks": {"dismissed": {"<occurrence>": 1760950000.0}, "snoozed": {"<id>": 1760990000.0}},
  "ledger": {"<branch>|<sig>": {"attempts": 2, "last_at": 1760950000.0, "last_outcome": "gave_up"}},
  "worktrees": {"<path>": {"triage": "on" | "ignored" | "paused", "paused_until": null, "pin": 0}},
  "fixes_today": {"date": "2026-10-20", "count": 3}
}
```

### 2a. Kinds (MVP)

| Kind | Layer | Condition (auto-clears when false) | Replaces |
|---|---|---|---|
| `approval`, `question`, `hanged`, `failed`, `exited`, `done`, `job` | human | As today (agent status, exit, job) | attention kinds of the same name |
| `ci_failed` | auto → human | Fresh red run on the head of a worktree branch (§3) | `ci` (an open PR's red checks): kept as the human view when auto-triage does not apply |
| `ci_escalated` | human | `ci_failed` past its limits, or the fixer gave up / reverted | — |
| `changes_requested` | human | A human `CHANGES_REQUESTED` review (or unresolved human threads, A7) on an open PR | `review` |
| `conflict` | human | PR `CONFLICTING` | `conflict` |
| `merge_ready` | human | PR open, approved by a human, checks green, not draft | — (the planner's `Merge`) |
| `triage_blocked` | human | Auto-triage skipped: dirty worktree, live busy agent for > `busy_wait`, teammate's PR | — |
| `custom` | any | Created / resolved through MCP (A5) | — |

---

## 3. Layer 1 rules (A2 detection, A3 action)

**Detection (A2):** every 60 s while some auto item is `fixing`/`verifying`, or while a run is in progress on an interest branch; otherwise every 5 min. One REST call per repo: `gh api -H 'If-None-Match: <etag>' repos/{o}/{r}/actions/runs?created=>=<now-active_days>&per_page=100`. Runs are grouped by `(head_branch, workflow_id)`, keeping the newest. A `ci_failed` is created when:

1. The branch belongs to a lazyagent worktree in the engine interest (active within `active_days`, `triage != ignored`, not paused). The branch is not the default branch and not in `protected` (`["main","master","develop"]` by default).
2. The newest run of that workflow on the branch has `conclusion ∈ {failure, timed_out}`, and its `head_sha` equals the branch's remote head (`PrInfo.head_sha`, or `git ls-remote` for a branch with no PR).
3. The signature = `workflow` + the sorted names of the failed jobs (`/runs/{id}/jobs`, fetched once per run).

An item **resolves** when a later run of the same workflow on the branch succeeds. It **expires** when the worktree is removed or the branch is merged.

**Action (A3), in order; the first that applies wins:**

| # | Check | Result |
|---|---|---|
| 1 | Worktree ignored / paused / inactive | Human `ci_failed` (shown, no fixer) |
| 2 | `ledger[branch\|sig].attempts ≥ max_attempts`, or `fixes_today ≥ max_fixes_per_day` | `ci_escalated` |
| 3 | The PR's author or the branch's last commit is not the user (`gh api user` login, cached) | `triage_blocked` "teammate's branch" |
| 4 | Uncommitted changes and no live lazyagent agent in the worktree | `triage_blocked` "dirty worktree — yours?" |
| 5 | A live agent in the worktree, idle (`waiting`/`completed`) | `send_agent_input` with the failure brief. The state is `fixing` with `fixer = that agent`, and it counts as an attempt but not as a fixer slot (Q-A3) |
| 6 | A live agent that is busy | Wait for its idle. After `busy_wait` (30 min), `triage_blocked` |
| 7 | Running fixers ≥ `max_fixers` | `queued` (FIFO) |
| 8 | Otherwise | Wake the triage agent with the incident, which leads to `actionables_dispatch`, then the engine re-checks 1-7 and spawns the fixer. The state is `fixing`, `attempt + 1` |

**Triage agent:** one per engine, `kind="triage"`, `model=triage_model`, in the repo root, spawned lazily on the first incident. It stops after `triage_idle_minutes` (15) with an empty queue. Its system prompt is built in (`engine/triage_prompt.py`): for each incident it reads `gh run view <id> --log-failed | tail -n 200`, writes a brief (what failed, the excerpt, the files probably involved, the push and revert rules), and calls `actionables_dispatch {id, brief}`. It never edits code. Its tools are the lazyagent MCP plus `gh` and git read-only. With `dispatcher = "engine"` (Q-A1), the engine fills a fixed template instead.

**Fixer:** `spawn_agent(worktree, instruction=brief, skip_permissions=true, kind="fixer", model=fixer_model, notes_mode="free")`, with a notes Goal pre-filled. Its contract (in the brief): fix, run the failing job locally if it can, commit, `git push` (no force), then `actionables_report {id, outcome: "pushed" | "gave_up" | "reverted", sha?, summary}`. The engine moves the item to `verifying` with `pushed_sha`, and the next run on that sha decides:

| Next run on `pushed_sha` | Result |
|---|---|
| Same workflow green | `resolved`, ledger reset, the fixer is stopped (notes handed off to the item) |
| Same signature red | `attempt + 1`. Under `max_attempts`: the same fixer gets the new log (no new spawn). Otherwise: `ci_escalated` |
| New jobs red that were green before | The fixer is told "revert your commits with `git revert`, push, report". Then `ci_escalated` |
| No report after `fixer_timeout` (60 min), or the fixer exits | `ci_escalated` with the fixer's notes |

**Restart:** at engine start, `fixing`/`verifying` items whose fixer is gone become `queued` (the attempt has already been counted), or `ci_escalated` when attempts are exhausted.

---

## 4. Layer 2 score (A4)

**Criteria file**, resolved in this order: `<repo>/.lazyagent/actionables.md`, `~/.config/lazyagent/actionables.md`, then the built-in default (an example ships as `docs/actionables-criteria.example.md`). It is reloaded on change (mtime checked every poll).

````markdown
# How I prioritise

Prose for the tie-break agent: "a teammate waiting on me beats my own work; ...".

```toml
weights = { kind = 3, project = 2, completion = 4, effort = 2 }
age = { per_hour = 0.5, max = 10 }
notify_min_score = 70

[kind]            # D1, 0-10 (the planner radar events)
approval = 10
question = 10
ci_escalated = 9
changes_requested = 8
hanged = 7
conflict = 7
merge_ready = 6
done = 6
exited = 6
failed = 6
triage_blocked = 5
ci_failed = 4
job = 3
custom = 5

[completion]      # D3, 0-10 (the planner Phase 4 dimension 3)
merge_ready = 10
pr_in_review = 9
pr_open = 7
in_progress = 5
no_pr = 3
tracker_status = { "qa complete" = 10, "code review" = 9, "ready for qa" = 7, "qa" = 7, "in progress" = 5, "to do" = 3, "backlog" = 1 }

[effort]          # D4, 0-10, minutes → score (quick wins first)
"<=15" = 10
"<=30" = 8
"<=60" = 6
"<=180" = 4
"*" = 2
defaults_min = { merge_ready = 15, question = 10, approval = 5, changes_requested = 60, ci_escalated = 60, done = 25 }

[project]         # D2, 0-10: worktree pin (-2..+2 → 2..10), the tracker project rank from feeders, else 5
tracker_priority = { urgent = 10, high = 8, normal = 5, low = 3 }
```
````

`score = Σ weightᵢ·dimᵢ + age + boost`, clamped to 0-120. With all-10 dimensions and the default weights, the base is 110, as in the planner. The score is a pure function `score(item, criteria, now) -> (int, breakdown)`, with golden tests over fixtures that mirror the planner's examples. Order: score desc, then `first_seen` desc (newest first, as today), then the tie-break (A5).

**Notifications:** `actionable.notify` when an occurrence first reaches `score ≥ notify_min_score`, or when it rises over the threshold through age. It fires once per occurrence and not at all while snoozed. The engine runs `notify-send -a lazyagent` itself only when no UI is attached. The desktop and the web keep a per-kind "mute" filter on top.

---

## 5. Protocol, events, capabilities, MCP

| Kind | Name | Shape / notes |
|---|---|---|
| feature | `actionables` | In `FEATURES` (server.py:100). Clients without it keep deriving their own queue |
| method | `actionables_list {layer?, state?, worktree_path?, include_resolved?}` | `{items: [Actionable], criteria: {path, rev, notify_min_score}, triage: {running_fixers, max_fixers, queued, fixes_today}}`, sorted |
| method | `actionables_get {id}` | Item + ledger entry + the fixer's notes ref |
| method | `actionables_mark {id, action: "dismiss" \| "snooze" \| "unsnooze", until?}` | Dismiss hides the **occurrence** |
| method | `actionables_triage_set {worktree_path, triage: "on" \| "ignored" \| "paused", until?, pin?}` | Limits (b) and D2 pin |
| method | `actionables_retry {id}` / `actionables_escalate {id}` | User: one more attempt past the cap (+1) / stop auto-triage now |
| method (agent) | `actionables_dispatch {id, brief}` | Triage agent's token only; the engine re-checks the limits; `{spawned: {worktree_path, agent_id}} \| {queued} \| {refused: reason}` |
| method (agent) | `actionables_report {id, outcome, sha?, summary}` | The fixer's token, for its own incident only |
| method (A5) | `actionables_create {kind:"custom", title, subject, dedupe_key, base_score?, inputs?, expires_at?, body?}` / `actionables_resolve {dedupe_key}` / `actionables_boost {target, delta, reason, expires_at?}` | Source from the token; caps per source (`max_open_per_source` 20, `base_score ≤ 80`, boost ±20, orchestrator/user only) |
| method (change) | `spawn_agent {model?}` | Provider-specific flag |
| event | `actionable.created` / `actionable.updated` / `actionable.resolved` | `{item}` / `{item, changed: [fields]}` / `{id, reason: cleared \| fixed \| dismissed \| expired}` |
| event | `actionable.notify` | `{id, occurrence, title, detail, score}` |
| event | `triage.changed` | `{running_fixers, queued, fixes_today}` (badge) |
| MCP | `actionables_list`, `actionables_get`, `actionables_dispatch`, `actionables_report` (A3); `actionables_create`, `actionables_resolve`, `actionables_boost` (A5) | No arguments → the caller's own scope where it applies |
| config | `[actionables] enabled, active_days, protected, criteria_file, notify_min_score`, `[actionables.triage] enabled, dispatcher ("agent" \| "engine"), triage_model, fixer_model, max_fixers, max_attempts, max_fixes_per_day, busy_wait_minutes, fixer_timeout_minutes, triage_idle_minutes, poll_active_s, poll_rest_s` | `true, 7, ["main","master","develop"], null, 70`; `true, "agent", "haiku", null, 2, 2, 10, 30, 60, 15, 60, 300`. `triage.enabled` ships `false` until A3's acceptance has been watched on real runs (Q-A21) |

v1 schemas in `protocol/schema/v1/methods.json` and `events.json`, plus golden fixtures, as for notes.

---

## 6. Files to touch

| Phase | Files |
|---|---|
| A1 | `engine/actionables.py` (new: store, derivation of the 10 kinds, marks), `engine/engine.py` (feed status/exit/job events; own the store), `engine/methods.py`, `engine/server.py` (`FEATURES`), `engine/events.py` (names), `protocol/schema/v1/{methods,events}.json`, `tests/engine/test_actionables.py` (port of the attention.rs tests), fixtures |
| A2 | `engine/worktrees.py` (engine interest, activity timestamp), `worktree_manager.py` (`head_sha` in GraphQL, `get_workflow_runs` with ETag, `get_run_jobs`), `models.py` (`PrInfo.head_sha`, `WorkflowRun`), `config.py` (`ActionablesConfig`), `engine/actionables.py` (`ci_failed` detection) |
| A3 | `engine/triage.py` (new: rules, state machine, limits, ledger, restart), `engine/triage_prompt.py` (new), `engine/engine.py` (`spawn_agent {model}`, kinds `triage`/`fixer`), `agent_providers.py` (model flag per provider), `mcp_server.py` (dispatch/report/list/get), `orchestrator_prompt.py` (tell the orchestrator about the triage agent and claims), tests with a fake `gh` |
| A4 | `engine/criteria.py` (new: md + toml parse, `score`), `docs/actionables-criteria.example.md` (new), `engine/notifier.py` (new: `notify-send` when headless), `worktree_manager.py` (review authors), desktop: `proto/src/types.rs` (`Actionable`, events), `app/src/attention.rs` (fallback only), `attention_view.rs` (score + breakdown, layer badge, triage panel, ignore/pause), `app.rs` (notify on `actionable.notify`), `persist.rs` (marks import); web: `lib/attention.js`, `components/attention.js` |
| A5 | `mcp_server.py`, `engine/actionables.py` (sources, caps, boost), `engine/criteria.py` (tie-break call through a one-shot `claude -p --model haiku` job) |
| A6 | `~/.claude/skills/planner/SKILL.md` (**the user's file, edited only with their go**): Phase 4 weights → the criteria file; T1/T6c read `actionables_list`; T2 feeds with `actionables_create` |
| A7 | `app.py`, `widgets/` (TUI), analysis agent template |
| Docs | engine-split §4d/§4e, ui-plan §3b, desktop-app D4, proposals README, this plan §8 |

---

## 7. Layer 3: sketched

- **Model:** `WorkItem {key: url, title (card name verbatim), verb, lane, set_by: hand|planner, fingerprint, due (frozen 📅), estimate_min, actionables: [id]}`. Lanes: Todo · Doing · Delegated · Waiting · Blocked · Done. An actionable attaches to the work item of its branch/PR (the PR URL, or the tracker card through `TASK-123` in the branch name).
- **State machine** = the planner's T2 table as rules over actionable events (red CI with no agent → Todo `Dev`; approved → `Merge`; peer review requested → Todo `Review`; my PR waiting on a coworker → Delegated `Chase`; agent finished → Doing `Review`; card `blocked` → Blocked). Stickiness: a `hand` card moves only when its fingerprint changes.
- **Sources** (`Source.poll()`): `github.review_requests` (`gh pr list --search review-requested:@me`), `tracker.returned` (QA/CR returned, read-only API token), `planner` (pushes). Disabled until the board lands.
- **Budget:** Doing + Todo ≤ 270 min, overflow → Waiting.
- **Views:** desktop "Board" tab (lanes, drag = `set_by: hand`), web, and a read-only export to `board.md` in the Kanban format while Obsidian is still used. The Done sweep and the monthly log stay in the planner (standup layer).

---

## 8. As landed

(empty)

---

## 9. Open questions

| # | Question | Recommendation |
|---|---|---|
| Q-A1 | Should the Haiku triage agent dispatch (decided), or should the engine fill a fixed template with no LLM? The agent's value is reading the log and writing a better brief; its cost is one cheap turn per incident | Keep the agent (decision 2). Ship `dispatcher = "engine"` as a fallback, and use it in tests |
| Q-A2 | Fixers run with `skip_permissions=true` (needed to push unattended) | Yes, limited to worktree branches; the guards are in the engine |
| Q-A3 | A live agent already in the worktree: send it the failure, spawn a second agent, or skip? | Idle → send it (no second agent; counts as an attempt, not as a fixer slot). Busy → wait `busy_wait`, then `triage_blocked` |
| Q-A4 | The user's own Claude session in a worktree (invisible to the engine, F11) | Dirty-worktree guard. Optionally, a `lazyagent-hold` lock file the user creates |
| Q-A5 | Defaults: `active_days=7`, `max_fixers=2`, `max_attempts=2`, `max_fixes_per_day=10` | As listed |
| Q-A6 | Signature granularity: workflow, workflow + jobs, or + failing test | Workflow + failed jobs |
| Q-A7 | Revert: who reverts, and does the engine revert by itself if the fixer is gone? | The fixer, with `git revert` + push. The engine never pushes; a missing fixer escalates |
| Q-A8 | Branches with a teammate's commits / PR | Skip auto-triage (`triage_blocked`); human item |
| Q-A9 | Flaky / infra failures: a deterministic re-run first? | `rerun_first = false` in the MVP. Later, a fixed list of infra signatures leads to one `gh run rerun --failed` |
| Q-A10 | Criteria file: location, format, per-repo override | `~/.config/lazyagent/actionables.md` + `<repo>/.lazyagent/actionables.md`; toml in a fenced block; prose = tie-break prompt |
| Q-A11 | With no UI open, the engine runs `notify-send` itself. OK on this machine (engine started by systemd/tmux, `DISPLAY`/`DBUS_SESSION_BUS_ADDRESS` present?) | Yes; skip silently without a session bus |
| Q-A12 | Marks move to the engine (one dismiss for every UI); the desktop's marks are imported once | Yes |
| Q-A13 | Keep each client's own derivation as a fallback for older engines, for how long? | One release, then remove |
| Q-A14 | "Review requested changes": start the analysis agent automatically or with a button? | Button (A7); opt-in auto later |
| Q-A15 | Ignore bot reviews (the `claude` reviewer) for `changes_requested` | Yes; a `bots` list in config |
| Q-A16 | One agent cap shared with the planner (its 2-agent cap) and the orchestrator? | Fixers have their own `max_fixers`. The planner skips claimed branches; a global cap is left to layer 3 |
| Q-A17 | Layer 3 storage: the engine store with an Obsidian export, or `board.md` stays the source? | Engine store + export; `board.md` read-only after the switch |
| Q-A18 | Cost cap beyond attempts (0006 not landed) | `max_fixes_per_day`; a token budget once 0006 lands |
| Q-A19 | Tie-break: when exactly, and its cost | Only within ε=3 in the top 5; cached per fingerprint; ≤ 1 call / 10 min |
| Q-A20 | The `ci` kind for a PR whose run is outside Actions (an external status check) | Human `ci_failed` without auto-triage |
| Q-A21 | Ship `triage.enabled = false` by default until real runs have been watched? | Yes; turn it on after one day of `dry_run` (the engine logs what it would dispatch) |
| Q-A22 | The planner A6 changes edit the user's skill: who does them, and when? | The user's go, after A4; the skill keeps working until then (the engine queue is additive) |
