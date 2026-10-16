# Graphical UI: plan

- **Status:** draft for review. Plan and mockups only, no code changes.
- **Base:** `develop` @ `145752e` (H2 merged). All `file:line` refs are under `src/lazyagent/` at that commit.
- **Builds on:** [engine-split.md](engine-split.md) (§0 why, §4 protocol v1, §6/§6b migration, D1-D11) and [proposals 0001-0009](../proposals/README.md).
- **Mockups:** [`ui-mockups/`](ui-mockups/), shown in §3a-§3d. Static, self-contained HTML/CSS (no scripts, no network) with sample data, in H2's dark tokens; the PNGs are headless Chrome screenshots (Playwright, `channel="chrome"`, 1440×900; the phone one at 390×844).

## TL;DR

- **The GUI grows out of the H2 host page, piece by piece.** H2 already serves a page with a native sidebar and the whole TUI in one xterm.js. Each U-PR moves one TUI piece (agent terminals, modals, diff, shell) to a native widget fed by protocol v1, until the hosted TUI pane is optional (U7) and, after PR 8, unnecessary.
- **The single biggest step is U2: native agent terminals.** The host forwards `attach_stream` and binary stream frames, so the page opens one xterm.js per engine session instead of drawing the agent inside the TUI. Everything the engine needs for it already exists (§3b/§3c of engine-split, PR 6).
- **The new screens that justify a GUI** (§0a of engine-split): an **attention queue** (who needs me, with the live terminal next to it), a **multi-agent grid**, a **diff review** with comments that go back to the agent (0008), and a **PR/CI panel** per worktree (0002).
- **The TUI stays** for SSH, tmux and the unattended weekday boot. It is never deprecated; it just stops being the only screen.
- **Stack (proposal, D7 stays Giovanni's call):** keep H2's no-build-step approach and vendor **Preact + htm + signals** as ES modules (~15 KB), JSDoc types, Chrome `--app` window as the shell. Tauri, a build step and TypeScript are deferred until something forces them (§6).
- **Protocol:** most screens run on existing v1 methods; the host's method whitelist widens step by step. New engine methods are listed in §5 and are all additive.

---

## 1. Goal and users

### 1a. What the GUI solves

From engine-split §0a: the axis is terminal app vs GUI app. The GUI is for the things a character grid does badly.

| Problem today (TUI) | GUI answer | Screen |
|---|---|---|
| One agent visible at a time; the rest is a coloured word in the sidebar | Grid/split of live terminals | Multi-agent (§3c) |
| "Who needs me?" means scanning the sidebar for yellow | A queue sorted by urgency, with the terminal and a reply box next to it | Attention queue (§3b) |
| Reviewing an agent's diff happens in GitHub or the editor, feedback is copy-pasted | Side-by-side diff with inline comments, sent back as one prompt | Diff review (§3d) |
| PR/CI is a 5-line box for the selected worktree only (pr_status_bar.py:29) | Per-worktree badges plus a panel with every check | Main layout right panel (§3a) |
| Notifications are toasts inside a terminal you are not looking at | Desktop notifications, later phone (0003) | §4e |
| Modals are key-driven only (`n`/`d`/`r`/`l` in the spawn modal, prompt_modal.py:70-77); no instruction field | Forms with an instruction box, provider, base branch | Create + launch (§4a) |
| Hover, links, drag, images: none | Native | Everywhere |

### 1b. Users

| User | Where | Uses |
|---|---|---|
| Giovanni at the desk | Linux, Chrome, big screen | GUI (main), TUI in a terminal when it is quicker |
| Giovanni away | Phone | Attention queue + approve/answer (0003), read-only status |
| Unattended boot | tmux → `lazyagent` → `/gio-planner unattended` | TUI only (K8) |
| Over SSH | Remote box | TUI only |
| Orchestrator / skills | MCP + protocol | No UI; they share the engine with whatever screen is open |

### 1c. What stays TUI-only

| Stays in the TUI | Why |
|---|---|
| SSH, tmux, unattended boot | No display. The GUI never becomes a dependency of the boot path |
| Hosted mode (H1) | Kept as the migration crutch until U7; then optional |
| Quick one-off use (`lazyagent` in a terminal) | Zero setup |

---

## 2. Inventory: TUI today → GUI

### 2a. Screens and widgets

| TUI piece | Where | GUI target | Step |
|---|---|---|---|
| Worktree list + orchestrator row, agent roll-ups | worktree_list.py:70-255 | **Native** sidebar (already in H2) | done (H2) |
| PR status bar (selected worktree) | pr_status_bar.py:8-89 | **Native**: badge per worktree (H2) + PR/CI panel with every check | U4 |
| Git info bar (branch, dirty, ahead/behind, last commit) | center_panel.py:72-115 | **Native** header of the worktree view | U3 |
| Agent tabs (`TabbedContent`, one `MonitoredTerminal` per agent) | center_panel.py:118-400 | **Terminal (xterm.js)** per session via `attach_stream`; tabs are native | U2 |
| Agent placeholder ("press s to spawn") | center_panel.py:203 | **Native** empty state with a Spawn button | U2 |
| Diff tab (`Static` with coloured text) | center_panel.py:204-210 | **Native** diff viewer (files, hunks, split/unified) | U5 |
| Worktree shell pane (`bash -l`) | center_panel.py:211-230 | **Terminal (xterm.js)** via `open_shell` + `attach_stream`; collapsible bottom pane | U2 |
| Orchestrator panel | orchestrator_panel.py:15-100 | **Terminal** + native board of worktrees (§3e) | U2 / U6 |
| Job log (custom create/remove output) | job_log.py:15-75 | **Native** log drawer fed by `job.*` events | U3 |
| Toasts (`notify`) | app.py | **Native** toasts (H2) + desktop notifications | done (H2) / U4 |
| Header / Footer | app.py:118-130 | **Disappears**: top bar + command palette + `?` sheet | U1 |

### 2b. Modals

| Modal | Keys today | GUI target | Step |
|---|---|---|---|
| Spawn (`SpawnModal`) | `n` normal, `d` skip permissions, `r`/`l` resume, Backspace new, Esc | **Native** form: permissions toggle, resume mode, **instruction** box (engine already takes `instruction`), label | U3 |
| Create worktree | branch, base (default from config), extra (custom create only) | **Native** form, plus "and launch an agent with: …" (§4a) | U3 |
| Remove worktree (`RemoveWorktreeModal`) | `y`/`n`, `f` force | **Native** confirm with force checkbox, guard text (agent running / main) | U3 |
| Confirm (stop agent) | `y`/`n` | **Native** confirm | U3 |
| Help | `?` | **Native** shortcut sheet, generated from the keymap table (§7a) | U1 |

### 2c. Keyboard

Full table in §7a. Every TUI binding (app.py:132-154, worktree_list.py:197-198, modal bindings) keeps the same key in the GUI, plus a command palette.

### 2d. Flows that exist today

| Flow | TUI | GUI |
|---|---|---|
| Select worktree → see its agent and shell | `j`/`k` | Click or `j`/`k` in the sidebar; native view (U2) |
| Spawn agent | `s` → modal | Button / `s` → form with instruction (U3) |
| Stop agent | `x` → confirm | Tab close button / `x` (U3) |
| Create / remove worktree | `c` / `d` | Forms (U3) |
| Refresh | `r`, `Alt+G` | Same keys; buttons in the worktree header |
| Switch agent tab | `Alt+]`/`Alt+[`, `Alt+n`/`Alt+p` | Same |
| Scrollback | PageUp/PageDown, wheel | xterm.js native scrollback |
| Open PR | click URL in PR bar | Link in PR panel |

### 2e. Migration path: H2 → native, piece by piece

```
H2 today        U2                    U3                 U5            U7 (after PR 8)
┌────┬──────┐   ┌────┬───────┬────┐   ┌────┬───────┐     ┌────┬──────┐  ┌────┬──────────┐
│side│ TUI  │ → │side│agent  │TUI │ → │side│agents │  →  │side│agents│→ │side│ agents   │
│bar │(all) │   │bar │xterm  │pane│   │bar │+forms │     │bar │+diff │  │bar │ shell    │
│    │      │   │    │(native│(rest│  │    │ TUI   │     │    │+PR/CI│  │    │ diff …   │
│    │      │   │    │ tabs) │)   │   │    │ pane ▸│     │    │TUI ▸ │  │    │ no TUI   │
└────┴──────┘   └────┴───────┴────┘   └────┴───────┘     └────┴──────┘  └────┴──────────┘
```

| Step | Moves to native | Criterion to start | Criterion to call it done |
|---|---|---|---|
| U2 | Agent + shell terminals | U1 store in place | A spawned agent is usable (type, scroll, resize, reattach) without the TUI pane in view |
| U3 | Spawn/create/remove/stop forms, job log | U2 | Every TUI modal has a form; the TUI pane is no longer needed for any mutation |
| U4 | Attention queue, PR/CI panel, notifications | U2 | Waiting agents can be answered from the queue |
| U5 | Diff viewer | U1 | Diff tab of the TUI no longer needed |
| U7 | Hosted TUI pane hidden by default, then dropped | U2-U5 done **and** PR 8 (engine outlives the TUI) | `lazyagent ui` runs with no TUI process; parity table §2a all green |

- **Rule for each step:** the hosted TUI pane stays available (a toggle) until U7, so a missing feature never blocks work.
- **Who owns the engine before PR 8:** the hosted TUI process (it is the engine's process in hosted mode). The GUI can hide its pane but not stop it. After PR 8 the host connects to the detached engine instead (§8b).

---

## 3. Information architecture and screens

### 3a. Main layout

![Main layout](ui-mockups/main.png)

([main.html](ui-mockups/main.html))

| Region | Content | Source |
|---|---|---|
| Top bar | repo, engine pill (connected / reconnecting / gone), attention count, usage today (0006), search/palette, settings | `hello`, `engine.*`, derived |
| Left sidebar (resizable, collapsible) | Orchestrator row; worktrees (name, branch, dirty/ahead/behind, PR + CI badge) with their agents (status dot, label, detail) | `get_sidebar_state` + events (H2) |
| Centre | Tabs per agent of the selected worktree (xterm.js), shell as a bottom split, Diff tab | `attach_stream`, `open_shell`, `get_diff` |
| Right panel (toggle `Ctrl+.`) | PR (title, state, review, mergeable), CI checks list, recent events of this worktree | `pr_info.updated`, event log |
| Bottom drawer | Job log (custom create/remove) | `job.*` |
| Toasts | Bottom right (H2) | events |

- **View modes** (top-bar switcher, also `g w` / `g a` / `g g` / `g o` sequences): **Worktree** (default), **Attention**, **Grid**, **Orchestrator**.
- The selection is per page (client state), as in the TUI; the hosted TUI follows it through `ui.select_worktree` only while its pane is shown.

### 3b. Attention queue

![Attention queue](ui-mockups/attention-queue.png)

([attention-queue.html](ui-mockups/attention-queue.html), phone: ![phone](ui-mockups/attention-queue-phone.png))

- **Left:** a list of items, newest-urgent first. **Right:** the selected item's live terminal (attached), its last screen lines and the actions.
- **Item kinds and urgency:**

| Rank | Kind | From | Actions |
|---|---|---|---|
| 1 | Needs approval | `agent.status` → `waiting_for_approval` | Open terminal (focus, type `1`/`2`/…); later Approve / Deny buttons (`answer_prompt`, new) |
| 2 | Question | `waiting_for_user` | Reply box → `send_agent_input` |
| 3 | Hanged? | `possibly_hanged` | Open, Stop |
| 4 | Failed / exited unexpectedly | `failed`, `agent.exited {stopped:false}` | Open shell, respawn (resume last) |
| 5 | CI red / changes requested / conflicts | `pr_info.updated` (diff of two states, client side until `pr.checks` (0002)) | Open PR panel, "send CI failure to agent" |
| 6 | Completed | `completed` | Review diff, dismiss |
| 7 | Job failed | `job.finished {exit_code≠0}` | Open job log |

- **Lifecycle:** an item clears itself when its condition clears (agent back to `running`, CI green). Manual dismiss/snooze is client-local (localStorage) until a shared `attention.*` exists (§5).
- **Badge:** the count shows in the top bar, the tab title (`(3) lazyagent`) and the favicon.

### 3c. Multi-agent view (grid)

![Multi-agent grid](ui-mockups/multi-agent.png)

([multi-agent.html](ui-mockups/multi-agent.html))

- **Layouts:** auto grid (1, 2, 2×2, 3×2), or a pinned split (one large + a column of small). Tiles are chosen by filter: "all running", "this worktree", "pinned", "needs attention".
- **Tile:** header (worktree, agent, status, elapsed), the live terminal, a footer with the last detail and quick actions (focus, stop, diff).
- **Size policy (D3, §3c of engine-split):** only the **focused** tile claims the PTY size. Other tiles render at the PTY size and scale down with CSS `transform: scale()`, so a grid never SIGWINCHes every agent. Pressing Enter on a tile opens it full size (claims).
- **Limits:** above ~6 live tiles (or with the page hidden), tiles fall back to a text preview from `read_agent_output` every 2-5 s and detach their stream (§7d).

### 3d. Diff review

![Diff review](ui-mockups/diff-review.png)

([diff-review.html](ui-mockups/diff-review.html))

- File tree (with +/− counts) → file diff (unified or split) → inline comment threads → **Submit review to Agent N**.
- Stage 1 (U5) parses today's `get_diff` text on the client; comments are page-local. Stage 2 (U8) is proposal 0008 with engine-stored threads (§5).

### 3e. Orchestrator view

- **Left:** the orchestrator's terminal (it is an agent with key `__orchestrator__`).
- **Right:** a board of every worktree as a card, in columns by roll-up status (Needs you / Running / Idle / Done), with PR/CI badges. Cards link to the worktree view.
- **Bottom:** an event feed (all `agent.*`, `pr_info.updated`, `job.*`, `engine.notice`), filterable.
- "Spawned by the orchestrator" grouping needs a `spawned_by` field that the engine does not have (§5, new).

### 3f. Settings

| Section | Holds | Stored |
|---|---|---|
| Appearance | Theme (dark / light / system), font, font size, density | Page (localStorage) |
| Notifications | Desktop on/off per kind, sound, quiet hours; phone (later) | Page; phone config in engine (0003) |
| Keyboard | The keymap (read-only first, rebinding later) | Page |
| Engine | `get_config` (provider, default branch, custom create/remove) read-only; engine version, `engine_id`, socket | Engine (read-only) |
| About | Versions, protocol capabilities from `hello` | — |

- Editing `.lazyagent.toml` from the GUI: not planned (Q4). "Open config in editor" is enough.

### 3g. Empty, error and disconnected states

| State | Shows |
|---|---|
| No worktrees but main | Sidebar: main only + "New worktree" call to action |
| Worktree with no agent | Centre: "No agent here" + Spawn button + the shell |
| No PR | PR panel: "No PR for `branch`" + a `gh pr create` hint (no action) |
| No `gh` | PR panel: "GitHub CLI not available" (`fetched: false`) |
| Diff empty / capped | "No changes" / "Diff truncated at N KB, open in editor" |
| Engine method error | Inline error on the form that caused it; `engine.notice` as a toast |
| WebSocket dropped | Banner "Reconnecting…", everything greyed but readable; on reconnect: `get_sidebar_state` → `subscribe {since: cursor}` → reattach each visible stream with `from_offset` (tail if still in the ring, else snapshot). No reload needed |
| Engine gone (`engine.shutdown`, or the hosted TUI exited) | Full overlay. Before PR 8: "lazyagent exited (code N). Run `lazyagent host` again" (Q13 of engine-split). After PR 8: "Engine stopped" + Start button (runs the auto-start) |
| Stream reset (slow page) | Silent reattach from the reset offset |
| Token missing/invalid | "Open the link printed by `lazyagent host`" |

---

## 4. Key flows

Each step names the protocol call; **(new)** marks what the engine or host lacks today (§5).

### 4a. Create a worktree and launch an agent with an instruction

1. `c` or "New" → form: branch, base (prefilled from `get_config.default_branch`), extra (shown only if `has_custom_create`), **instruction** (multiline), permissions (normal / skip), provider (**new**: `spawn_agent` has no provider param; it comes from config), label.
2. Submit → `create_worktree {branch, base_branch, extra}`.
   - Plain create: the result has `path`; the sidebar updates from `worktrees.changed`.
   - Custom create: the result has `job_id`; the job drawer opens and streams `job.output` until `job.finished`.
3. On success → `spawn_agent {worktree_path, instruction, skip_permissions, label, env}`. The host sends **its own** env (D5): it was started from Giovanni's shell, like the TUI.
4. `agent.spawned` → a tab opens and attaches (`attach_stream`), focus goes to it.
5. Failure at 2 → form stays with the error; at 3 → worktree exists, a toast offers "Spawn again".

- The chain is client-side; no combined engine method is needed.

### 4b. Approve a permission / answer a blocked agent

1. Agent hook → `agent.status` `waiting_for_approval` (detail `needs approval`, agent_observers.py:159) → attention item, toast, desktop notification if the page is not focused.
2. Click the item → right pane attaches the agent's stream: Giovanni sees the real prompt (the command, the file).
3. **Today:** focus the terminal and press the key the provider shows (`1`, `2`, Enter). Typing claims the size (D3).
4. **Later (0003):** Approve / Deny buttons → `answer_prompt {session_id, choice}` (**new**), which maps to provider keys and refuses if the agent is no longer waiting. A question gets a reply box → `send_agent_input {worktree_path, agent_id, text}` (exists; waits for the child's ack).
5. `agent.status` back to `running` → the item clears.

### 4c. Review an agent's diff and send comments back (0008)

1. From the attention item "Completed", the worktree's Diff tab, or `Ctrl+D` → `get_diff {worktree_path}`.
2. Stage 1: the page parses the unified diff (files → hunks → lines). Comments are kept in the page (localStorage per worktree).
3. "Submit review" → a preview of the composed prompt (`path:line`, quoted hunk lines, comment) → `send_agent_input` to the chosen agent. If the agent is not `waiting`, the button says so and offers "Send when idle" (queued in the page).
4. Stage 2: `get_diff` structured + `base`, `add_review_comment` / `list_review_comments` / `resolve_review_comment`, `submit_review`, `review.updated` (all **new**, 0008). Comments survive reloads and show in every client; the next diff marks comments whose lines changed as "addressed?".

### 4d. Follow a branch's PR and CI (0002)

1. The page widens interest: H2's host already sets it to every worktree (Q12), so `pr_info` covers all of them.
2. Sidebar badge (state + CI roll-up); right panel: title, state, review decision, mergeable, **every check** (`checks[]`: name, status, conclusion).
3. A check link needs `details_url` on `ci_check` (**new**, additive field).
4. Transitions (green→red, review → changes requested, conflicts) are computed client-side from consecutive `pr_info.updated` until the engine emits `pr.checks` / `pr.review` / `pr.merged` (**new**, 0002).
5. "Wake the agent when CI fails" toggle on the panel → `add_rule {on: pr.checks, where: {state: failure}, action: send_input …}` (**new**). Until then the button "Send failure to agent" composes the failed check names into `send_agent_input` by hand.

### 4e. Notifications (0003), including the phone

| Stage | What | Needs |
|---|---|---|
| Desktop, page open | Toast + Web Notification when the page is not focused (H2 already does it); click focuses the agent | — (exists) |
| Desktop, page closed | `notify-send` from the engine | `[notify]` config + notifier (**new**, 0003); after PR 8 so it runs with no screen |
| Phone, push | ntfy topic with the agent, worktree and kind only (no screen excerpt on a public server), link to the phone page | `[notify] ntfy`, `agent.attention` (**new**) |
| Phone, answer | Mobile layout of the attention queue (mockup above) over HTTPS | Reach beyond 127.0.0.1, see below |

- **The 127.0.0.1 clash.** H2 binds loopback only and checks `Host` and `Origin` against `127.0.0.1:<port>` / `localhost:<port>` (K6). A phone cannot reach that.

| Option | How | Verdict |
|---|---|---|
| A. Bind 0.0.0.0 / LAN | Open the port | **No.** Any device on the network gets shells |
| **B. Tailscale Serve (recommended)** | `tailscale serve` proxies `https://<machine>.<tailnet>.ts.net` → `127.0.0.1:<port>`; tailnet-only, TLS by Tailscale, identity headers available | The host keeps its loopback bind; it adds the tailnet origin to the allowed `Host`/`Origin` list (`--allow-origin`, **new**) and keeps the token |
| C. Reverse SSH tunnel / cloudflared | Similar, more moving parts; cloudflared is public unless Access is set up | Fallback only |
| D. ntfy action buttons calling back | Push with "Approve" that POSTs to the engine | Same reachability problem as A-C, and approving blind (0003 risk) |

- **Phone method whitelist:** a narrower set than the desktop page: `get_sidebar_state`, `subscribe`, `read_agent_output`, `send_agent_input`, `answer_prompt`, `attach_stream` (read-only: input frames refused). No spawn, create, remove, shell. Chosen per origin (Q6).
- **Secure context:** Web Notifications and service workers need HTTPS or loopback. Loopback is fine on the desktop; the phone gets HTTPS from Tailscale.

### 4f. Cost / tokens (0006)

- Top bar: "today: 1.2 M tok · ~$4.10 (est.)". Agent tab and sidebar: a small usage chip. Worktree header: total per worktree (≈ per task).
- All **new**: `agent.usage` event, `list_agents.usage`, `get_usage {worktree_path?, since?}`. Shown as estimates (the price table drifts).

### 4g. History (0007)

- "Since you left" card on first load after ≥ 1 h away: per worktree, status changes, approvals asked, PR events, exits, usage. Source: `get_digest {since}` (**new**).
- Per-agent timeline strip (running / waiting / blocked bands) in the right panel: `get_timeline` (**new**).
- Before 0007, the page can build a short timeline from `subscribe {since}` replay (the last 1,000 events, in-memory only).

### 4h. Restore after a reboot (0009, PR 9)

1. Page connects; `hello.capabilities` includes `restore` (**new**). `list_restorable` (**new**) returns agents alive at the last shutdown.
2. Banner: "4 agents were running before the restart. Restore all · Choose · Dismiss".
3. `restore {session_ids | all}` (**new**) → `engine.restored {restored, failed}` → each restored agent gets a tab; failures appear in the attention queue.
4. If the boot script already restored (`restore_on_start`), the banner only reports the result from `engine.restored`.

---

## 5. Protocol needs

Status: **exists** = in v1 today (`protocol/schema/v1/`). **host** = exists in the engine but the H2 host does not forward it (whitelist, server.py:51-62) or does not multiplex its frames. **new** = not in the engine; an additive method/event/field, advertised in `capabilities`.

### 5a. By screen / flow

| Screen / flow | Method / event | Status | U-PR |
|---|---|---|---|
| Sidebar, top bar | `get_sidebar_state`, `subscribe`, `worktrees.changed`, `agent.*`, `git_status.updated`, `pr_info.updated`, `engine.notice`, `engine.shutdown` | exists, forwarded | H2 |
| Native terminals | `attach_stream`, `detach_stream`, `claim_size`, `resize`, binary output/input frames with `stream_id`, `stream.resized` / `stream.reset` / `stream.closed` | host | U2 |
| Worktree shell | `open_shell` (+ attach) | host | U2 |
| Agent list detail (size, attached clients) | `list_agents` | host | U2 |
| Git refresh (`r`, `Alt+G`) | `refresh_worktrees`, `refresh_git_status` | host | U3 |
| Forms | `get_config`, `create_worktree`, `remove_worktree`, `spawn_agent`, `stop_agent`, `job.started` / `job.output` / `job.finished` | host | U3 |
| Spawn: choose provider | `spawn_agent.provider` | **new** (field) | U3 or later |
| Attention: reply | `send_agent_input` | host | U4 |
| Attention: preview text | `read_agent_output` | host | U4 |
| Attention: approve/deny buttons | `answer_prompt {session_id, choice\|text}` | **new** (0003) | U9 |
| Attention: shared dismiss/snooze | `attention.ack` / `agent.attention` | **new** (0003) | U9 |
| PR/CI panel | `get_pr_info`, `pr_info.updated` | exists | U4 |
| Check links | `ci_check.details_url` | **new** (field) | U4 |
| PR transitions | `pr.checks`, `pr.review`, `pr.merged`, `pr.closed` | **new** (0002) | later |
| CI rules | `add_rule`, `list_rules`, `remove_rule` | **new** (0002) | later |
| Diff stage 1 | `get_diff` (text, capped) | exists, forwarded | U5 |
| Diff stage 2 | `get_diff {base?, structured}`, `get_file_diff {path}` (lazy per file), `add/list/resolve_review_comment`, `submit_review`, `review.updated` | **new** (0008) | U8 |
| Orchestrator board | `get_sidebar_state` | exists | U6 |
| Orchestrator grouping | `agent.spawned.spawned_by` | **new** (field) | later |
| Event feed | `subscribe` (all events) | exists | U6 |
| Usage | `agent.usage`, `list_agents.usage`, `get_usage` | **new** (0006) | U10 |
| History | `get_timeline`, `get_digest`, persistent `since` | **new** (0007) | U10 |
| Restore | `list_restorable`, `restore`, `engine.restored` | **new** (0009, PR 9) | U11 |
| Slots (0001) shown in top bar | `list_slots`, `slot.*` | **new** (0001) | when 0001 lands |
| CPU per agent (0004) | `agent.resources` | **new** (0004) | when 0004 lands |
| Phone reach | `--allow-origin`, per-origin whitelist | **new** (host only) | U9 |
| Engine stop / start | `shutdown` | **new** (PR 8) | U7 |

### 5b. Host changes (not engine)

| Change | Why | U-PR |
|---|---|---|
| Multiplex engine stream frames on the page WebSocket | Today binary page frames are TUI keystrokes. Proposal: page binary frame = `u8 type · u32 stream_id · u64 offset? · bytes`, i.e. **the v1 stream frame unchanged**, with `stream_id 0` reserved for the hosted TUI PTY | U2 |
| Widen `ALLOWED_METHODS` per U-PR | Keep the whitelist; each PR adds what its screen needs (table above) | U2-U5 |
| Pass the host env to `spawn_agent` / `open_shell` | D5: the host is the client | U3 |
| `--allow-origin` and per-origin method sets | Phone (§4e) | U9 |
| Serve without a hosted TUI | Connect to a detached engine (`discover(repo_root)`), no PTY of its own | U7 (needs PR 8) |

- **Why the whitelist can widen safely:** the page can already type anything into the hosted TUI, which has shells and spawns agents. Forwarding `spawn_agent` or input frames adds no capability an authenticated page lacks; it only makes it structured. The token, `Host`/`Origin` checks and CSP (K6) remain the boundary. Still, the H2 rule "mutations go through the TUI" is a decision to revisit explicitly (Q2).

---

## 6. Stack

### 6a. Front-end approach

| Option | Build step | Size (min+gz) | CSP `script-src 'self'` | Fit |
|---|---|---|---|---|
| Plain JS/DOM (H2 today, app.js 485 lines) | No | 0 | ✅ | Fine for one sidebar; hand-written DOM diffing gets painful at ~10 screens, forms, a diff viewer |
| **Preact + htm + @preact/signals** (vendored ES modules) | No | ~4 + ~1 + ~2 KB | ✅ (htm parses tagged templates, no `eval`) | Components and state without tooling; React-like, easy to hire into; signals fit an event-fed store |
| Lit | No | ~6 KB | ✅ | Web components; good for isolated widgets; state across screens is DIY |
| Solid (`solid-js/html`) | Optional | ~7 KB | ✅ | Fast, but the no-build path is second-class |
| Svelte | **Yes** (compiler) | small output | ✅ | Needs Node in CI and a built-asset story (engine-split §5c) |
| React + JSX | **Yes** | ~45 KB | ✅ | Heaviest; ecosystem not needed here |

- **TypeScript:** JSDoc annotations + `// @ts-check`, checked by `tsc --noEmit` in an optional CI job. No transpile step, so the wheel ships the same files the tests run.
- **Build step trigger:** introduce Vite (engine-split §5c) only if one of these happens: the code passes ~8-10k lines, a dependency ships only as CommonJS, or bundle size matters on the phone. Until then, vendored ES modules with hashes in `VENDOR.md`, exactly like xterm.js in H2.
- **Diff rendering:** own component over a small unified-diff parser (~150 lines) first; `diff2html` is an option but brings its own CSS and HTML injection surface.
- **Terminal:** xterm.js 6 (vendored, as H2), `addon-fit`, `addon-webgl` for the focused terminal only (WebGL contexts are capped at ~16 per page in Chrome), DOM renderer for grid tiles, `addon-search`, `addon-web-links` later.

### 6b. Shell

| Option | Linux engine | Pros | Cons | Verdict |
|---|---|---|---|---|
| Browser tab | Chrome (Blink) | Zero install, best xterm.js perf | Ctrl+W/T/N/L conflicts; mixed with other tabs | Works (H2 today) |
| **Chrome `--app=<url>` window** | Blink | Own window and icon, no address bar, most shortcuts reach the page | Needs Chrome/Chromium installed (it is); not a real "app" | **Recommended default** for `--open` |
| PWA (installed) | Blink | Dock icon, standalone window, badging API for the attention count | Needs a **stable origin**: today the port is random (0), and the token lives in `sessionStorage` per window | Later, if a stable port per repo is added (Q8) |
| Tauri | **WebKitGTK** | Small, native menus/tray, system notifications | WebKitGTK is weaker for WebGL/xterm.js scrolling and fonts (engine-split §0b); a Rust toolchain in CI; a second packaging path | Phase 4, only if tray/global shortcuts are wanted |
| Electron | Blink | Full control | ~100 MB, its own update story | No |

### 6c. Security (keeps H2's model)

| Control | H2 today | GUI plan |
|---|---|---|
| Bind | 127.0.0.1 only | Same. Phone via Tailscale Serve proxying to it (§4e) |
| Token | 32 bytes, URL fragment → `sessionStorage`, first WS message, constant-time compare | Same. PWA (later) would need a persisted token: `localStorage` + rotation on engine restart |
| Host / Origin | Must be `127.0.0.1:<port>` / `localhost:<port>` | Plus explicitly allowed origins (`--allow-origin`) |
| CSP | `script-src 'self'`, `style-src 'self' 'unsafe-inline'` (Q11) | Same. No CDN, no inline scripts, no `eval` (rules out some template libs; htm is fine). Diff and terminal text are inserted as text nodes, never `innerHTML` |
| Method whitelist | Read-only + `ui.*` | Widened per U-PR (§5a); a narrower set per origin for the phone |
| Message size | 1 MiB cap | Same; stream frames ≤ 64 KiB (engine side) |
| Mutations | Through the TUI modals only | Direct engine calls from U3 (Q2) |

### 6d. Distribution in the wheel

- The page stays under `src/lazyagent/host/static/` as **tracked files**, so hatch ships them in the wheel and the sdist with no build hook (as H2). Vendored libraries under `static/vendor/<lib>/` with license and `VENDOR.md` hash.
- No Node at install time, ever. If a build step comes later, follow engine-split §5c (CI builds into the package, `artifacts = [...]`).
- Command name: keep `lazyagent host` until U7, then add `lazyagent ui` (alias) for the no-TUI mode (Q3).

---

## 7. Accessibility, keyboard, theme, performance

### 7a. Keyboard-first, TUI parity

- **Scopes:** keys typed into a focused terminal go to the agent, except the **global** set below, intercepted with xterm.js `attachCustomKeyEventHandler` (same as the TUI's `priority=True` bindings). Single letters work only when focus is outside a terminal (sidebar, queue, grid), as in the TUI.

| Key | TUI action | GUI | Scope |
|---|---|---|---|
| `Ctrl+K` | Focus sidebar | Same | global |
| `Ctrl+J` | Focus active agent | Same | global |
| `Ctrl+D` | Diff | Same (Diff tab) | global |
| `Ctrl+L` | Focus terminal (shell) | Same | global |
| `Alt+]` / `Alt+n` | Next agent tab | Same | global |
| `Alt+[` / `Alt+p` | Previous agent tab | Same | global |
| `Alt+G` | Refresh git status | Same | global |
| `j` / `k` | Sidebar down/up | Same (also arrows) | sidebar |
| `s` / `x` | Spawn / stop | Same, opens form / confirm | outside terminal |
| `c` / `d` | Create / remove worktree | Same | outside terminal |
| `r` | Refresh list | Same | outside terminal |
| `?` | Help | Shortcut sheet | outside terminal |
| `q` | Quit | **Changed:** closes nothing; before PR 8 quitting means stopping the TUI and all agents, so it is a menu item with a confirm, not a key | — |
| Modal keys (`n`/`d`/`r`/`l`, `y`/`n`/`f`, Esc, Enter) | Spawn / confirm / remove | Same accelerators on the native forms | form |
| — | — | `Ctrl+Shift+P` command palette (every action by name) | global |
| — | — | `g w` / `g a` / `g g` / `g o`: Worktree / Attention / Grid / Orchestrator view | outside terminal |
| — | — | `Ctrl+.` right panel, `Ctrl+B` sidebar | global |
| — | — | `Ctrl+Enter` send (reply box, review) | form |

- **Browser conflicts:** `Ctrl+L`, `Ctrl+D`, `Ctrl+K` are browser shortcuts in a tab; the page `preventDefault`s them, which works in a tab and better in `--app` windows. `Ctrl+W/T/N` cannot be caught in a tab: that is a reason for `--app` (§6b).
- **Shell keys stolen:** `Ctrl+L` (clear), `Ctrl+D` (EOF), `Ctrl+K` (kill line) are taken globally, exactly as in the TUI. A per-key "pass through to terminal" setting is an option (Q7).

### 7b. Accessibility

- Sidebar is an ARIA `tree` (worktree → agents) with roving tabindex; the queue is a `listbox`; toasts are an `aria-live="polite"` region (H2 already).
- Status is never colour-only: every dot has a text label (`running`, `approval`, …) as the H2 sidebar does.
- Visible focus ring on every control; focus returns to the opener when a form closes.
- xterm.js `screenReaderMode` toggle in settings.
- `prefers-reduced-motion`: no animated spinners/transitions.
- Contrast: tokens checked at WCAG AA for text on `--bg` / `--surface` in both themes.

### 7c. Theme

- Dark is the default and reuses H2's tokens (`--bg #121212`, `--surface #1e1e1e`, `--panel #262626`, `--accent #ffa62b`, `--primary #0178d4`, status colours), app.css:1-16.
- Light theme: the same token names redefined under `[data-theme=light]` and `prefers-color-scheme: light`; xterm.js gets a matching theme object (agents' own colours stay ANSI).
- Status colours are shared with the TUI's meaning (worktree_list.py:33-46): running green, waiting/approval yellow, completed cyan, failed/hanged red.

### 7d. Performance

| Concern | Rule |
|---|---|
| Many agents (10-20) | Attach streams only for **visible** terminals; hidden tabs detach and reattach on show (tail when close, snapshot otherwise, engine-split §3b). The engine's headless screen keeps detection going (K1) |
| Hidden page | On `visibilitychange` to hidden, detach all streams after 30 s; keep the event subscription (cheap) |
| Grid | ≤ 6 live tiles; beyond that text previews from `read_agent_output`. WebGL only on the focused terminal |
| Event bursts | Store updates are batched per animation frame (signals) |
| Big diffs | Per-file collapse; files over ~2,000 lines rendered on demand; stage 2 loads files lazily (`get_file_diff`, new) |
| Scrollback | xterm.js `scrollback: 5000` per attached terminal; snapshots carry 2,000 lines (engine cap) |
| Backpressure | Engine drops a slow stream at 1 MiB and sends `stream.reset` (engine-split §4e); the host drops a page 8 MiB behind (H2). The page reattaches |

---

## 8. Phased plan

### 8a. U-PRs

Each PR is small, keeps `lazyagent` and `lazyagent host` working, and extends `tests/host/test_browser.py`-style Playwright tests (headless Chromium, falling back to `channel="chrome"`; fake agent from the host fixtures).

| # | PR | Scope | Done when | Tests |
|---|---|---|---|---|
| U0 | **docs: UI plan** (this) | Plan + mockups | Reviewed; Q1-Q10 answered | — |
| U1 | **ui: component foundation** | Vendor Preact + htm + signals; `store.js` built from `get_sidebar_state` + events; port the H2 sidebar, toasts and top bar to components; theme tokens incl. light; help sheet from a keymap table. **No behaviour change** | The existing browser test passes unchanged; light theme renders | Existing test_browser; new: theme toggle, `?` sheet lists every TUI binding (asserted against `LazyAgent.BINDINGS`) |
| U2 | **host+ui: native terminals** | Host multiplexes v1 stream frames (`stream_id 0` = TUI PTY) and forwards `attach_stream`, `detach_stream`, `claim_size`, `resize`, `open_shell`, `list_agents`. Page: agent tabs + shell split as xterm.js per session; TUI pane becomes a toggle | A fake agent spawned through the TUI shows in a native tab; typing reaches it; resize claims; detach/reattach (hide/show) keeps the screen; a reconnect resumes with `tail` | test_server: frame mux, whitelist; test_browser: agent tab, input, reattach after reload, `stream.closed` closes the tab |
| U3 | **ui: native actions** | Forms for spawn (with instruction), create (+ launch), remove, stop; job drawer; refresh keys; host forwards the mutating methods and passes its env | Every TUI modal has a native equivalent; §4a works end to end | test_browser: create + launch with a fake agent and a fake custom create (job output); remove guard; stop confirm. test_server: env forwarding |
| U4 | **ui: attention queue + PR/CI panel** | Attention view derived client-side (§3b); reply box (`send_agent_input`), preview (`read_agent_output`); PR/CI right panel with every check; title/favicon badge; notification settings | A fake agent going `waiting_for_user` appears in the queue, a reply reaches it and clears the item | test_browser: queue ordering, reply, auto-clear, notification body; PR panel from a fake `pr_info` |
| U5 | **ui: diff viewer** | Client-side unified-diff parser, file tree, unified/split, page-local comments, "submit review" via `send_agent_input` | Diff tab of the TUI not needed for daily review | Parser unit tests (run in the browser page via Playwright `evaluate`, no Node), test_browser: comment + submit reaches the fake agent |
| U6 | **ui: grid + orchestrator views** | Grid with size policy (§3c), text-preview fallback; orchestrator board + event feed | 4 agents watched at once without SIGWINCH storms | test_browser: tiles don't claim size (PTY size unchanged), focus claims |
| U7 | **host: no-TUI mode** (after **PR 8**) | `lazyagent ui` connects to the detached engine via `discover`; no hosted TUI; engine stop/start from the page | Closing the page and the host leaves agents running; reopening shows them | test_host_integration with a detached engine (PR 8 fixtures) |
| U8 | **engine+ui: review comments (0008)** | Structured diff, stored threads, `submit_review` | Comments survive reload and show in two pages | Contract tests for the new methods; test_browser two pages |
| U9 | **engine+host+ui: phone (0003)** | `answer_prompt`, `agent.attention`, ntfy notifier, `--allow-origin`, phone whitelist, mobile layout | Approve a fake agent's prompt from a phone-size viewport through an allowed origin | Contract tests; test_server origin sets; test_browser at 390×844 |
| U10 | **engine+ui: usage + history (0006, 0007)** | Usage chips and totals; "since you left" digest; timeline strip | Numbers match a scripted transcript | Contract + browser |
| U11 | **ui: restore banner (0009)** | Banner and flow on top of PR 9 | Restore after an engine restart from the page | Needs PR 9 fixtures |

- **Order rationale:** U1 pays for itself before any new screen (H2's app.js is already at the size where hand DOM updates get brittle). U2 is the hinge: once terminals are native, every other piece is "just a form". U4 before U5 because waiting agents cost more time than reviewing diffs. U8-U11 wait for their engine proposals.

### 8b. Fit with PRs 7-9 and §6's web phase

| Engine-split item | Relation |
|---|---|
| **PR 7** (TUI RemoteBackend, in progress elsewhere) | Independent. Both are v1 clients; U2 exercises the same `attach_stream`/size rules from the browser, which is a useful second client for PR 7's contract tests |
| **PR 8** (`lazyagent engine`, auto-start, agents survive the UI) | **Gates U7.** Until then the hosted TUI process owns the engine; the GUI can hide but not drop it. U1-U6 do not depend on it |
| **PR 9** (restart manifest) | **Gates U11** (restore) |
| **§6 PR 10+ "web"** (WS in the engine, `lazyagent web`, Vite) | **Proposal: fold it into the host.** The host is already the web server and a v1 client; keeping WS, auth and the whitelist in the host keeps the engine a unix-socket-only process with one security boundary. `lazyagent web` becomes the U7 no-TUI mode of the host. Engine-side WS stays possible (contract suite over WS) but is not needed (Q1) |
| Phase 3 (diff review, notifications) | = U5/U8 and U4/U9 |
| Phase 4 (Tauri) | Optional after U7 (§6b) |

---

## 9. Open questions

| # | Question | Recommendation |
|---|---|---|
| Q1 | Where does the web server live long term: the host (a v1 client proxying with a whitelist) or the engine (WS transport, engine-split PR 10+)? | **The host.** One security boundary, engine stays unix-only and smaller; the contract suite can still run over WS later if a Rust engine wants it |
| Q2 | Drop H2's "mutations only through the TUI modals" rule and forward `spawn_agent` / `create_worktree` / `remove_worktree` / `stop_agent` / `send_agent_input` / input frames from the page? | **Yes, from U2/U3.** The page can already type into the TUI, so it adds no capability; the token/Origin/CSP boundary is unchanged |
| Q3 | Command name for the GUI: keep `lazyagent host`, or add `lazyagent ui` / `lazyagent web`? | Keep `host` until U7; then **`lazyagent ui`** for the no-TUI mode, `host` as the hosted variant. Drop the separate `web` name from engine-split §5d |
| Q4 | Edit config from the GUI? | **No.** Read-only `get_config` + "open in editor"; avoids a `set_config` with validation and file-format round trips |
| Q5 | Front-end stack (D7) | **Preact + htm + signals, vendored, no build step, JSDoc types**; Chrome `--app` window; Tauri only for tray/global shortcuts later. Revisit a build step at ~8-10k lines |
| Q6 | Phone reach | **Tailscale Serve** to the loopback host + `--allow-origin` + a narrower per-origin method set; ntfy for push with no screen excerpt. Never bind beyond loopback |
| Q7 | `Ctrl+L` / `Ctrl+D` / `Ctrl+K` are stolen from the shell (as in the TUI). Keep parity? | **Keep parity** by default, plus a setting to pass a key through when the shell has focus |
| Q8 | Stable port per repo (needed for an installable PWA and bookmarks)? | **Not yet.** Random port + `--app` window is enough; revisit with U9 (phone) where Tailscale gives a stable URL anyway |
| Q9 | Attention dismiss/snooze: per page or shared in the engine? | **Per page** in U4; shared (`agent.attention` + ack) with U9, when the phone makes it a multi-client problem |
| Q10 | Grid tiles: scale the PTY-size render down, or let tiles claim their own size? | **Scale down, never claim** unless focused (D3); a tile claiming would SIGWINCH every agent on each layout change |
| Q11 | Should `q` exist in the GUI? | **No key.** Before PR 8 it kills every agent; a menu item with a confirm. After PR 8 "close" only detaches, so the window close button is enough |
| Q12 | Diff stage 1 comments are page-local. Acceptable until U8? | **Yes**; the value is the send-back flow, not storage. Warn in the UI that comments live in this browser |
