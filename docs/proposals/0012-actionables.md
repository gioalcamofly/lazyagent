# 0012. Actionables: the engine's queue of what needs doing

- **Status:** Proposed (2026-10-20); plan in [actionables-plan.md](../design/actionables-plan.md); open doubts Q-A1-Q-A22 at the end of the plan
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4d, §4e; [ui-plan.md](../design/ui-plan.md) §3b (attention queue); [desktop-app.md](../design/desktop-app.md) D4; proposals [0002](0002-github-ci-triggers.md) (GitHub events), [0003](0003-phone-notifications-approvals.md) (notifications), [0011](0011-session-notes.md) (notes, agent token)

An **actionable** is something that needs doing on one of the user's worktrees: a red CI run, an agent waiting on approval, a reviewer's change request. The engine owns them, so they exist with no UI open. They come in two layers. **Layer 1 (auto-triage):** fixed, deterministic rules (MVP: failed GitHub Actions runs) hand the problem to a fix agent within hard limits. **Layer 2 (human):** a single queue ordered by a deterministic score, with criteria in an editable Markdown file. A cheap agent only breaks ties. **Layer 3** is sketched here and not designed: a local board that replaces gio-planner's Obsidian board and takes external sources (peer review requests, ClickUp cards returned by QA/CR).

## Problem

- A user who runs several worktrees at once has no common place for "what needs me now". The Attention tab shows agent states and open-PR problems, but it is derived **in each client**, separately (`desktop/crates/app/src/attention.rs`, `host/static/lib/attention.js`). It exists only while a window is open, and dismiss/snooze are per client.
- Red CI on a worktree's branch is noticed late. The PR poller only covers the worktrees a client has in its interest set (`WorktreeService.set_interest`, engine/worktrees.py:134). With no UI open, nothing polls. A branch without a PR has no CI data at all.
- Fixing a red run is usually mechanical (read the failed log, fix, push), yet it waits for the user or for the next gio-planner tick.
- The order of the queue is fixed by kind (`Kind::rank`, attention.rs:28), and it says nothing about which PR is closest to merge or which project matters most. gio-planner knows that (Phase 4 score), but only inside its own LLM turn every 30 minutes.

## Today's workaround

- `/gio-planner tick` every 30 min (`/loop 30m`). T1 polls `gh` and ClickUp. T6c sends a red check on an agent's PR to that agent. T2 classifies events into board lanes. Each tick is one LLM turn, even when nothing changed. It runs only while a Claude session with the loop is alive, and its rules are prose that a model interprets.
- The user watches the Attention tab, or GitHub's emails, and sends "CI failed, please fix" by hand (`ci_failure_prompt`, attention.rs:240).
- Agent caps ("at most 2 planner-started agents") are prompt text, not code.

## Proposal

### Concept

| Aspect | Design |
|---|---|
| Owner | The engine. `engine/actionables.py` derives, stores and scores them, and the UIs are views (decision 1). Clients that know the `actionables` capability stop deriving their own queue |
| Identity | `id` = a stable **dedupe key** per condition (`gha:<repo>:<branch>:<signature>`, `agent:<worktree>/<agent_id>:approval`, `pr:<repo>#<n>:changes_requested`, `ext:<source>:<key>`). An **occurrence** (`id@first_seen`) is what a dismissal hides, as today |
| Layers | `auto` (layer 1: the engine acts within limits) and `human` (layer 2: the user decides). An auto actionable that exhausts its limits **escalates** to a human one, with the same id and its history |
| Lifecycle | `open → (auto: queued → fixing → verifying) → resolved \| escalated \| dismissed \| expired`. A condition-backed actionable resolves itself when its condition clears, as the attention queue does today |
| Storage | `state_dir()/actionables/<repo digest>.json` (atomic write, like the manifest): open items, marks (dismiss/snooze), and the **attempt ledger** for layer 1. It survives engine restarts, while the event log does not (0007) |
| Sources | MVP: agent statuses and exits, jobs (what attention shows today), PR info, **GitHub Actions runs**. Later: MCP `actionables_create` (agents, orchestrator, gio-planner). Then layer 3: review requests, ClickUp |

### Layer 1: auto-triage (fixed rules, MVP = GitHub Actions failures)

| Rule part | Design (decision 2) |
|---|---|
| Trigger | A workflow run on a **lazyagent worktree's branch** ends with `failure` or `timed_out` (`cancelled`, `skipped` and `action_required` don't count). It must run on the branch's **current head**, with no later successful run of the same workflow (gio-planner's "fresh red" rule) |
| Signature | `workflow name + failed job names`, normalised. Attempts are counted per **branch + signature** |
| (a) Activity | The worktree had activity in the last `active_days` (default 7): last commit on the branch, an agent event, or a notes write. Older worktrees get a human actionable, not a fixer |
| (b) Ignored / paused | The user marks a worktree **ignored** (never auto-triaged; human items still show) or **paused until** a date, from any UI. Stored in the engine state, not in toml |
| (c) Concurrency | At most `max_fixers` (default 2) fix agents running at once, engine-wide. Extra incidents wait in `queued`, oldest first |
| (d) Attempts | At most `max_attempts` (default 2) per branch + signature. When they run out, or the fixer gives up, the item escalates to a human actionable ("CI still red after 2 attempts") with the fixer's notes |
| Guards | Never on the default branch or on `[actionables] protected` branches. A worktree with uncommitted changes and no lazyagent agent is skipped and flagged (the changes may be the user's). A live agent in the worktree gets the failure when it is idle (`send_agent_input`), and no second agent is started (see Q-A3) |
| Dispatch | A **triage agent** on a cheap model (`triage_model`, default `haiku`), kind `triage`, in the repo root. The engine wakes it only when there is a new incident, so it costs zero tokens while idle. It reads the failed log (`gh run view --log-failed`, trimmed), writes the fixer's brief, and calls `actionables_dispatch`. The engine checks (a)-(d) again before it spawns, so the model cannot break a limit |
| Fixer | A normal agent in the worktree (`fixer_model`, default: the provider's default). Unattended (`skip_permissions`), notes in `free` mode with the incident in Goal. It **may push** to the worktree's branch (decision 2), and reverts its own commits with a revert commit if its fix makes things worse. It never force-pushes. It ends with `actionables_report {outcome}` |
| Verify | After a push, the engine waits for the next run on the new head. Green means `resolved` and the attempts reset. The same signature still red means `attempt + 1`. A job that was green and is now red means the fixer is told to revert, and the item escalates |
| Human change requests | A human (not a bot) `CHANGES_REQUESTED` review, or new unresolved human threads, gives a **human** actionable "Review the requested changes". No fixer starts on it. A button (later an opt-in rule) starts a read-only analysis agent that writes its findings into notes. The user decides |

### Layer 2: human actionables ordered by priority

| Aspect | Design (decision 3) |
|---|---|
| Score | Deterministic `score = Σ weight·dimension` (0-110, like gio-planner's `final_score`) computed by the engine on every change. A **breakdown** goes with each item so the UIs can show why it ranks where it does |
| Dimensions (MVP) | Taken from gio-planner Phase 4 and its radar events, mapped to what the engine knows: **D1 urgency of the kind** (agent blocked on you > escalated CI > changes requested > conflict > your turn > job), weight 3. **D2 project/worktree priority** (user pin, later the orchestrator's weight), weight 2. **D3 proximity to completion** (approved + green = "just merge" 10, in review 9, in progress 5), weight 4. **D4 effort / quick win** (Merge ~15m, Decide ~10m, Review ~25m), weight 2. **Age** adds up to +10, so slipping is visible (gio-planner's frozen `📅`) |
| Criteria file | `~/.config/lazyagent/actionables.md`, a prompt-style Markdown file the user edits. Its fenced ```` ```toml ```` block holds the weights and the per-kind tables, which the engine parses. The prose around it is the tie-break agent's prompt. A repo `.lazyagent/actionables.md` overrides it. The engine ships an example generated from gio-planner's rules |
| Tie-break | Only when two items in the top `tiebreak_top` (5) are within `tiebreak_epsilon` (3 points). A cheap agent (`haiku`) reads the criteria prose and the two items and returns an order. The result is cached by the items' fingerprints, at most one call per `tiebreak_min_interval`. Phase A5, not the MVP |
| Notifications | System notifications only for items with `score ≥ notify_min_score` (default 70), once per occurrence. The engine emits `actionable.notify`. A UI that is open shows it. With **no UI attached**, the engine runs `notify-send` itself (Linux; Q-A11) |
| Existing kinds | Every kind the attention queue has (approval, question, hanged, failed, exited, ci, review, conflict, done, job) becomes an engine actionable with the same condition and auto-clear. The behaviour does not change; only the place it lives and its order do |

### Extensibility (decision 4: the hook, not the detail)

| Hook | Shape |
|---|---|
| Deterministic creation over MCP | `actionables_create {kind: "custom", title, subject: {worktree_path?, pr?, url?}, dedupe_key, base_score?, expires_at?, body?}` and `actionables_resolve {dedupe_key}`. The caller's token gives the `source` (agent, orchestrator, outside MCP client = "external"). Each source has a cap on open items and a cap on `base_score` |
| Orchestrator weight | A `boost` per item or per worktree (`actionables_boost {target, delta, reason, expires_at}`), bounded to ±20. Only the orchestrator's token or the user may set it. It shows in the breakdown |
| New layer-1 rules | `engine/triage.py` has a `Rule` protocol (`detect(snapshot) → incidents`, `limits`, `dispatch_template`). Only rules compiled in are allowed, never rules from config or MCP. Future rules: conflict → `/base-merge` (gio-planner state D), lint-only failures |
| Sources | A `Source` protocol (`poll() → [ExternalItem]`) for layer 3 (GitHub review requests, ClickUp). Disabled until the board exists (decision 5) |

### Layer 3, sketched: an integrated local board

Not designed. Recorded here so that layers 1-2 leave room for it.

- **Board = lanes over actionables grouped by work item.** A work item is identified by its URL, the board's identity key (ClickUp card or PR), and holds the actionables of its branch/PR. Lanes are the user's work states from `~/.claude/CLAUDE.md`: **Todo · Doing · Delegated · Waiting · Blocked · Done**. The column is the source of truth, and a card carries exactly one verb (`Dev · Review · Test · Merge · Decide · Assign · Chase · Check · Land · Triage`).
- **Cards change state by themselves**, following gio-planner's T2 table, now as engine rules: a red CI with no agent goes to Todo `Dev`, approved goes to `Merge`, a review requested from a coworker goes to Delegated `Chase`, an agent finished goes to Doing `Review`. **Lane stickiness** (`set_by: hand` pins a card until its fingerprint changes) and the **frozen `📅`** carry over as they are.
- **External sources** plug in as `Source`s: review requests from teammates (`gh pr list --search review-requested:@me`), and ClickUp cards returned from QA/CR. ClickUp stays the system of record. The engine reads it (the token in its env) and never writes to it.
- **Budget:** Doing + Todo ≤ ~4.5 h, with gio-planner's estimates. Overflow goes to Waiting, never out of Doing.
- **Transition:** an export to `~/Documentos/smith/scrum/board.md` in the Kanban plugin format (a read-only view) until the desktop board replaces Obsidian. The Done sweep logs to the monthly file as today.

### Engine vs "gio-planner does it all" (decision 6)

| | Actionables in the engine | gio-planner does it all (tick every 30 min) |
|---|---|---|
| Latency | The PR/run poller every 60 s (5-10 min at rest), event-driven | Up to 30 min per hop |
| Cost when nothing changes | 0 tokens (one conditional REST call per repo, which a `304` answers) | One LLM turn per tick |
| Without a Claude session | Works (the engine is enough) | Nothing happens if the `/loop` session is dead |
| Visible in desktop / web / TUI | Yes, one queue for every UI | No (terminal text plus the Obsidian board) |
| Limits (agents, attempts) | Code, with tests | Prompt text the model must follow |
| Determinism and tests | Rules and score as pure functions, golden tests | Prose rules interpreted each turn |
| ClickUp, scrum, budget, morning plan | Not its job (layer 3 is only a sketch) | Already done and tuned |
| Judgement (complexity, "blocked per its latest comment") | No, apart from the tie-break | Yes, LLM |
| Cost to build | New engine code (A1-A4, ~M-L) | Already exists |
| Risk | Duplicating gio-planner logic in two places | A single brain that is slow, costly and invisible to the UIs |

**How they coexist** (**decided** 2026-10-20): the engine **detects, scores and auto-fixes**, driven by events and fixed rules. gio-planner stays the **daily-planning and ClickUp/scrum layer** (morning plan, ClickUp fetch, monthly log). There is **one priority logic, the engine's**, with its criteria in the editable Markdown file. It is not duplicated in gio-planner's prompt.
1. **Consumes:** on each tick, gio-planner reads the engine's actionables over MCP (`actionables_list`, with score and breakdown). When the engine advertises `actionables`, T6c drops its "red check on an agent's PR" row and its own `gh` polling of agent PRs, and it respects the engine's claims, so the two never fix the same branch. T2 places cards by the engine's score and layer.
2. **Feeds:** gio-planner pushes the ClickUp-derived items (QA return, @mention, urgent card, unassigned urgent bug) with `actionables_create` (source `gio-planner`, dedupe by card URL). It passes the ClickUp facts as **inputs** (priority, status, project position, its effort estimate). The engine scores them with the same criteria file: Phase 4's weights move there and are no longer in the skill's prompt. Those items then show in the desktop queue and use its notification threshold.
3. **Later (layer 3):** once the board exists in the engine, gio-planner becomes **one more source** that feeds it (ClickUp adapter plus morning plan). It writes to the engine board instead of `board.md`, and its tick could run less often, or stop.

## Feasibility in the code

| # | Finding | Effect |
|---|---|---|
| F1 | PR info is polled only for the union of the clients' interest sets (engine/worktrees.py:134-145, `poll_pr_info` :495). With no client, nothing is polled | **Changes:** the engine adds its own interest set, `engine:actionables` = active, non-ignored worktrees (rule a/b), through the same `set_interest` |
| F2 | `PrInfo` has no head sha, and `CiCheck` has no run id or head (models.py:155). The "fresh red on head" rule needs both | **Changes:** add `head_sha` (`headRefOid`) to the batched GraphQL query and to `PrInfo`. The run id comes from the Actions REST data (F3) |
| F3 | A branch without a PR has no CI data today | **Changes:** one REST `GET /repos/{o}/{r}/actions/runs?created=>=<since>&per_page=100` per repo, filtered by `head_branch` in the engine, with `If-None-Match` (ETag, `304` costs no rate limit). It gives `head_sha`, `conclusion`, `workflow`, and the jobs on demand (`/runs/{id}/jobs`) |
| F4 | `spawn_agent` has no model parameter. `AgentConfig` only has `provider` (config.py:39; engine.py:907) | **Changes:** `spawn_agent {model?}`, passed by each provider (`claude --model`, `codex -m`). The triage agent and the fixer need it |
| F5 | The queue and its marks live in each client (attention.rs, persist.rs: marks and notify settings; attention.js: localStorage) | Clients turn into views of the engine's queue. Each keeps its derivation as the **fallback** for engines without the capability (Q-A13). The desktop's marks are imported once |
| F6 | Notifications: the desktop runs `notify-send` only while it is open (notify.rs, app.rs:409-440). The web has per-kind toggles | `actionable.notify` with the threshold. When no UI is attached, the engine notifies itself (`ui_attached`, engine.py:482, already tells it) |
| F7 | The event log is in memory (0007 not landed) | The attempt ledger and queued incidents are persisted in the actionables store |
| F8 | Agents don't outlive their engine | An incident whose fixer died with the engine becomes `interrupted`. At start, the engine requeues it if attempts remain (it counts as an attempt), or escalates it |
| F9 | Every agent has an identity token (0011 V1, 20-10 fix B) | `actionables_report` / `_dispatch` / `_boost` know their caller. A fixer can report only its own incident |
| F10 | There is no "last activity" per worktree | Derive it as max(last commit date on the branch, last agent event or manifest `ended_at`, notes `updated_at`), cached on the worktree scan |
| F11 | The engine knows only its own agents. A Claude session the user opened by hand in a worktree is invisible to it | The "dirty worktree, no lazyagent agent" guard covers most of it. A `lazyagent-owned` lock file could cover the rest (Q-A4) |

## Fits after

- **PR 3** (`WorktreeService` pollers and batched PR info), **PR 6** (v1 server, capabilities, `subscribe`), **0011 N2** (agent tokens, MCP identity). All landed.
- **0002** (typed GitHub events): A2 delivers part of it (`gha.run_failed`, plus the PR diff). 0002's generic rules stay out: layer 1 rules are compiled in.
- **0003** (phone): later, via `actionable.notify`.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (several worktrees at once) | L (MVP A1-A4); XL with layer 3 |

## Risks

| Risk | Mitigation |
|---|---|
| An auto-fix pushes something wrong to a shared branch | Only worktree branches, never the default or `protected` branches. Revert commits, no force-push. Attempt cap. The engine verifies the next run and escalates. A teammate-authored PR on that branch skips auto-triage (Q-A8) |
| Runaway cost (agents in a loop on a flaky test) | `max_fixers`, `max_attempts`, `max_fixes_per_day` (Q-A18). An item that comes back with the same signature after being resolved uses the same ledger within 24 h |
| Fighting gio-planner (both start a fixer) | Engine claims are visible over MCP. gio-planner skips branches the engine claims (§ coexistence). One shared cap (Q-A16) |
| Touching the user's own work | Dirty worktree guard; an idle agent gets the failure (no second agent); ignore/pause per worktree |
| Notification fatigue | Threshold, once per occurrence, snooze in the engine |
| An opaque order | A score breakdown on every item. Weights in a file the user edits. The tie-break is optional and cached |
| Logs with secrets in a prompt | The failed log is trimmed (last N lines per job) and stays in the fixer's own notes and prompt. It is never sent outside the agent |
| Two queues during the migration | The capability gate. Clients' own derivation only when the engine lacks it |
| Prompt injection from CI logs or review text | The fixer's brief quotes them as data. The fixer's limits are enforced by the engine, not by its prompt |

## Open questions

In the plan, §9 (Q-A1-Q-A22).
