# Engine / screen split: design

- **Status:** draft for review. Design only, no code changes.
- **Base:** `develop` @ `4ca663d` (release 0.8.0). Baseline: `uv run pytest -q` gives **523 passed** (56 s).
- **All `file:line` refs** are under `src/lazyagent/` at that commit.

## TL;DR

- Today the **Textual widget tree is the runtime**. Agents live inside widgets (`WorktreePanel._agents`, center_panel.py:181), and status travels as Textual messages. IPC/MCP handlers reach into `query_one(CenterPanel)` and into the pyte buffers. The engine therefore has to be carved out of the widgets, not just out of `app.py`.
- **Proposed split:** the engine owns PTYs, a headless emulator (used for detection and `read_agent_output` only), the agent registry, the event log, the worktree/git/PR caches and pollers, config, and the servers. Clients own display emulation, layout, selection, modals and keys→bytes.
- **Protocol:** the existing `{id, method, params}` IPC envelope plus events, plus binary PTY frames that carry byte offsets. On attach the client gets a snapshot (ANSI) and then the live stream. Resize follows a single-owner policy: the most recently active client owns the PTY size.
- **Migration:** about 9 strangler PRs to `develop`. The engine is first extracted **in-process** (same loop, direct calls), then moved behind the protocol, then out of process. Each step keeps `lazyagent` working, and the legacy `ipc.sock` and the `lazyagent.mcp_server` module path stay valid throughout.
- **Why (§0):** the real axis is terminal app vs GUI app, not Python vs Rust. The split keeps the TUI's SSH/tmux strengths and makes a GUI just another client. The roadmap (§0e) ranks the features that make the engine worth it: shared resources, GitHub/CI triggers, phone approvals.

---

## 0. Motivation & roadmap

The rest of this doc says **how** to split lazyagent. This section says **why**, and what the split is for once it lands.

### 0a. Why change at all

- **Trigger:** comparing lazyagent with Warp (warp.dev); see the Warp comparison in the (local, uncommitted) competitive analysis. Warp looks better and does more because it is a **GUI app that draws its own pixels**, not because it is written in Rust. A Rust TUI (ratatui) hits the same character-grid limits as Textual.
- **So the real axis is terminal app vs GUI app**, not Python vs Rust.

| | Terminal app (Textual, ratatui) | GUI app (Warp, Zed, VS Code) |
|---|---|---|
| Layout | Fixed cell grid, one font size | Free layout, any font and size |
| Images | Only via kitty/sixel | Native |
| Input | Poor hover, no rich input | Hover, rich editors, drag and drop |
| Notifications | OSC escapes / `notify-send` | Native |
| Reach | ✅ SSH, tmux, unattended boot, any terminal | ❌ needs a display |

- **Terminal emulation never goes away.** Agents (claude, codex, gemini) are terminal programs whatever the UI is. The only question is who renders them: pyte in a TUI today, xterm.js in a GUI.
- **Goal:** GUI-level possibilities without losing the terminal-native strengths. Separating the engine from the screens gives both: the TUI stays for SSH/tmux/boot, a GUI is just another client.

### 0b. Options considered

| Option | Verdict |
|---|---|
| Stay pure TUI and polish (themes, notifications, textual-serve) | Do the cheap parts anyway; it doesn't lift the grid ceiling |
| Full Rust GUI rewrite (GPUI/egui/iced + `alacritty_terminal`, like Warp or Zed) | **Rejected.** Best performance, but months of rebuilding UI (diff viewer, editor, markdown) that HTML gives for free |
| **Engine + clients; GUI = web (xterm.js), optionally wrapped in Tauri** | **Chosen.** Stack details stay deferred to Phase 2 (D7) |

Performance notes:

- **xterm.js is the VS Code terminal.** Its WebGL renderer handles tens of MB/s, far above an agent's output rate (human reading speed).
- **Tauri is small** (system webview, a few MB), but on Linux that webview is **WebKitGTK**, weaker than Chromium at WebGL, scrolling and fonts. Linux is the only platform (D9), so this matters. Fallbacks: Electron, or the engine serving the page and opening it with `chrome --app=`.
- **Today's bottleneck is pyte** (pure Python) doing both emulation and display. After the split, display moves to each client and the engine keeps only a headless emulator for status detection (§3a, K1).
- **Rust stays possible later** behind the same protocol and contract suite (Phase 5), with no big-bang rewrite.

### 0c. Engine vs Claude Code

| | Claude Code | Engine |
|---|---|---|
| What it is | **An agent:** an LLM loop with tools | **Not an agent:** no LLM. A supervisor |
| Scope | One session, one directory | Worktrees, N agent PTYs from any provider, status detection, events, MCP |
| Analogy | A worker | tmux plus a git-aware foreman |

- **Risk, stated honestly:** Claude Code keeps absorbing coordination features (subagents, worktree isolation, cross-session SendMessage, cloud sessions). Each release narrows the gap.
- **The engine earns its place** only with what Claude Code doesn't do:
  - multiple providers (claude, codex, gemini) under one roof;
  - visible, interactive terminals, not background workers;
  - a single board across all worktrees;
  - a durable worktree lifecycle (create, PR, retarget, remove);
  - the workflow rules in §0e.

### 0d. Value the engine delivers

| Value | Today | With the engine |
|---|---|---|
| Agents survive the screen | Closing or crashing the TUI kills them | Detach and reattach like tmux (D4) |
| Several screens | One TUI | TUI + web + phone, at the same time |
| One API for everything | The orchestrator, planner skills and scripts need the TUI open | Any tool talks to the engine via the protocol (§4) |
| Provider-neutral status | A different mechanism per provider (Claude hooks, Codex app-server log, Gemini telemetry + screen scan), delivered as Textual messages to the TUI only | Structured, uniform events (§4e) for every client |
| GUI foundation | Rewrite needed | New screens are just clients |
| Rust option | Full rewrite | Swap the engine only, gated by the contract suite |

### 0e. Roadmap: features that make the engine worth it

Ranked by value for Giovanni's workflow. The top three replace skill-based workarounds that **poll**. Each row has its own proposal under [`docs/proposals/`](../proposals/README.md) (problem, today's workaround, protocol additions, risks); "Fits after" maps it onto §6/§6b.

| # | Feature | Value | Fits after |
|---|---|---|---|
| 1 | [Shared resource slots](../proposals/0001-shared-resource-slots.md) (exclusive dev-DB slot, "at most N agents" cap) | ★★★ | PR 5 (engine owns spawn); exposed by PR 6 |
| 2 | [GitHub/CI event triggers](../proposals/0002-github-ci-triggers.md) (merged → retarget; CI red / review → wake agent) | ★★★ | PRs 2, 3, 6 |
| 3 | [Phone notifications and approvals](../proposals/0003-phone-notifications-approvals.md) | ★★★ | H2 or the web phase (PR 10+) |
| 4 | [Per-agent CPU budget](../proposals/0004-cpu-budget-per-agent.md) | ★★ | PRs 4-5 |
| 5 | [Hook-driven status, no screen scan for Claude](../proposals/0005-claude-status-from-hooks.md) | ★★ | PR 4 |
| 6 | [Cost/tokens per agent and task](../proposals/0006-cost-tokens-per-agent.md) | ★★ | PR 2; shown by H2 / web |
| 7 | [Persistent history](../proposals/0007-persistent-history.md) ("what happened overnight") | ★★ | PRs 2 and 8 |
| 8 | [Diff review with comments](../proposals/0008-diff-review-comments.md) | ★★ (needs the GUI) | Phase 3, after H2 or PR 10+ |
| 9 | [Restore after reboot](../proposals/0009-restore-after-reboot.md) | ★ | PR 9 (largely the same item) |

- **Items 1-3 turn the engine from "tmux with statuses" into the place where workflow rules live.** That is the differentiator against Claude Code and Warp.

---

## 1. State inventory

### 1a. `LazyAgent` app (app.py)

| State / logic | Where | Goes to | Why |
|---|---|---|---|
| `repo_path`, `_repo_root` | app.py:99, :112, :180-186 | **Engine** | Defines the engine's identity: one engine per repo root |
| `worktrees: list[WorktreeInfo]` | app.py:100, :188 | **Engine** (cache) + client mirror | IPC `list_worktrees` reads it (ipc.py:172); clients render it from `worktrees.changed`. Since PR 3 it lives in `WorktreeService` (rescan included) and `app.worktrees` is a property over it |
| `_agent_states` (2-level registry) | app.py:104, :218-248 | **Engine** (`AgentRegistry`) + client mirror | This is the source of truth for MCP, the sidebar and events |
| `agent_events: AgentEventBus` | app.py:107; agent_events.py:123 | **Engine** | The cursor log behind `list_events` and `send_agent_input` confirmation (ipc.py:596) |
| `_git_statuses` cache | app.py:108, :332-340 | **Engine** | Shared by every client and by MCP `list_worktrees` (ipc.py:174). Since PR 3: `WorktreeService.git_statuses`, merged there; `app._git_statuses` is a property over it |
| `_selected_worktree`, `_orchestrator_selected` | app.py:109-110, :417-450 | **Client** | Pure view state. The engine only learns an *interest set* (see `set_interest`, §4) that it uses to scope polling. Since PR 3 the TUI sets its interest to the selected worktree (empty for the orchestrator or no selection) |
| `_selection_debounce` timer | app.py:116, :284-310 | **Client** | UX throttling. Becomes a debounced `set_interest` / `refresh_git_status` call. PR 3 keeps it in the app: interest is set at once, the debounced refresh calls the service |
| `_config: Config` | app.py:111, :174-178 | **Engine**, with a read-only copy for clients | Spawn/create/remove need it (ipc.py:227, :373). Modals need `default_branch` and `has_custom_create` (app.py:759-760) through `get_config` |
| `_gh_available` | app.py:113, :390-394 | **Engine** | Belongs to the PR poller. Moved to `WorktreeService` in PR 3 |
| `_ipc_server`, `_ipc_socket_path` | app.py:114-115, :167-172, :733-735 | **Engine** | It is the engine's server. `LAZYAGENT_SOCKET` is injected into agents (agent_providers.py:438-441) |
| Timer `check_hangs` 60 s | app.py:139, :512-525 | **Engine** | Agent liveness |
| Timer `poll_agents` 1 s | app.py:143, :501-510 | **Engine** | Hook-log tailing, which feeds status |
| Timer `git_status` 60 s (selected only) | app.py:149-152 | **Engine**, scoped to the union of clients' interest sets | Done in PR 3: a `WorktreeService` asyncio poller over the interest set |
| Timer `diff` 30 s (selected only) | app.py:153-155 | **Engine** on request; the client polls while the diff tab is visible | Diff is large and only wanted when it is shown. Since PR 3 the timer stays in the app and calls `WorktreeService.get_diff` |
| Timer `pr_status` 60 s (selected only) | app.py:156-158 | **Engine**, scoped by interest | `gh` network calls should be deduplicated across clients. Done in PR 3: a `WorktreeService` poller; a worktree whose fetch is in flight is not fetched again, and the interest set is fetched in one batched `gh api graphql` call ([proposal 0002](../proposals/0002-github-ci-triggers.md#polling-strategy)) |
| Worker `_refresh_git_statuses` (all) | app.py:263-282 | **Engine** (`asyncio.to_thread`) | Triggered by rescan/create/remove. PR 3: `WorktreeService.refresh_git_status()`, which the app runs as an exclusive async worker |
| Worker `_refresh_selected_git_status` | app.py:312-330 | **Engine** | Same as above, for one path. PR 3: `refresh_git_status([path])` |
| Worker `_refresh_selected_diff` | app.py:355-370 | **Engine** method `get_diff` | Request/response. PR 3: `WorktreeService.get_diff`, which also keeps the last diff per worktree |
| Worker `_refresh_pr_status` | app.py:383-405 | **Engine**, pushed as `pr_info.updated` events | Was not exclusive, so concurrent `gh` calls were possible. PR 3: `refresh_pr_info(paths)` deduplicates per path and emits `pr_info.updated` only on change |
| `on_agent_status_changed` (registry update, label/session pull from panel, event publish) | app.py:454-482 | **Engine** | Pulling label and session from the panel (:461-468) goes away, because the engine sets them at spawn |
| `on_agent_exited` | app.py:484-497 | **Engine** (drop + event); **Client** (toast, tab cleanup) | Split by concern |
| `action_spawn_agent` / `_spawn_orchestrator_agent` | app.py:529-602 | **Engine** `spawn_agent` (+ `kind:"orchestrator"`); **Client** keeps the `SpawnModal` | Spawn logic is duplicated today in the IPC handler (ipc.py:365-390) |
| `compose_orchestrator_prompt` | app.py:586; orchestrator_prompt.py:178 | **Engine** | Needs config and repo root only |
| `action_stop_agent` / orchestrator stop | app.py:604-637 | **Engine** `stop_agent`; **Client** chooses the target | The active tab is client state |
| `action_create_worktree` / `_do_create_worktree` | app.py:751-792 | **Engine** `create_worktree`; **Client** modal | The custom-create path differs between UI (types into shell, :782) and IPC (subprocess, ipc.py:253). Both paths become an engine job (subprocess) with streamed output (decision D6) |
| `action_remove_worktree` / `_do_remove_worktree` | app.py:794-851 | **Engine**; **Client** confirm modal | The "agent running" guard is duplicated (app.py:803 vs ipc.py:320) |
| `_send_to_terminal` | app.py:853-868 | **Engine** `write_input` to the worktree shell session | Writes straight into a widget queue today |
| `action_refresh` / `_load_worktrees` | app.py:738, :180-199 | **Engine** `refresh_worktrees` → `worktrees.changed` | IPC calls it through `call_later` (ipc.py:268, :296, :331). Since PR 3 it calls `WorktreeService.rescan`, which still lists inline (create selects the new worktree right after) and emits `worktrees.changed` |
| Focus/tab actions, keybindings, CSS, `compose` | app.py:51-95, :639-728 | **Client** | Pure UI |
| `diag` watchdog/log | app.py:127-138; diagnostics.py | **Both** (separate log files) | Each process needs its own loop watchdog |
| `main()` argparse | app.py:877-899 | **CLI** module, with subcommands | See §5 |

### 1b. State living in widgets (the real runtime)

| State / logic | Where | Goes to | Why |
|---|---|---|---|
| `WorktreePanel._agents / _labels / _session_names / _agent_counter` | center_panel.py:181-185, :320-322 | **Engine** `AgentRegistry` | Agent identity and ids. Ids `a1, a2…` were per-panel counters; since PR 2 the registry allocates them, monotonic per worktree and persisted across restarts (D8), and holds each agent's label and session name. `_agents` (the views) is still per panel; since PR 4 each view wraps an `AgentSession` (`panel.get_session(id)`), which the panel still creates until PR 5 |
| Provider command + runtime context build | center_panel.py:394-420; orchestrator_panel.py:96-123 | **Engine** `spawn` | Hook temp dirs, MCP config and the session name are all engine concerns |
| Per-worktree shell `bash -l` | center_panel.py:209-230 | **Engine** session `kind:"shell"` (D1) | Custom create/remove commands are typed into it (app.py:782, :838). It should survive the UI closing like the agents do. Since PR 4 it is a `TerminalSession` (`kind = "shell"`) rendered by a plain `ScrollableTerminal`; the panel still creates it |
| `OrchestratorPanel._agent_terminal`, `_session_name` | orchestrator_panel.py:35-132 | **Engine** (key `__orchestrator__`) | Same as for agents |
| `MonitoredTerminal` status/detail/`_last_output_time`/observer/scan timer | monitored_terminal.py:42-49, :88-205 | **Engine** `AgentSession` | Status detection. Done in PR 4: `engine/session.py`; `MonitoredTerminal` only re-posts the session's events as Textual messages |
| `ScrollableTerminal.emulator`, queues, `recv` loop | scrollable_terminal.py:327-333, :411-447, :485-604 | **Engine** (PTY I/O) | The process lifetime must not depend on a widget. Done in PR 4: `TerminalSession` owns the emulator, the recv loop, a 4 MiB offset-addressed ring of raw output and the exit code |
| Terminal modes `mouse_tracking`, `bracketed_paste` | scrollable_terminal.py:320-324, :606-631 | **Engine** (authoritative); clients derive their own | `send_input` framing for MCP needs them (:1108-1124). Since PR 4 they live on the session; the TUI view reads them from there |
| `send_input` + child-ack + submit | scrollable_terminal.py:1126-1161 | **Engine** method (`send_agent_input`) | It is protocol semantics, not UI. Done in PR 4: `TerminalSession.send_input` |
| `ScrollbackScreen` pyte screen + scrollback | scrollable_terminal.py:194-263, :345-346 | **Both**: engine headless (detection, `read_agent_output`, snapshot); client display emulator | See §3. Since PR 4 the class is in `engine/screen.py` and the session owns the only instance; the in-process TUI renders that same object (K2) |
| Hidden-feed coalescing | scrollable_terminal.py:54, :449-475, :582-599 | **Client** concern (subscribe only to visible streams); the engine feeds the headless screen on its own throttle | Today detection *depends on widget visibility* (:528). Since PR 4 the session batches the feed every 0.25 s unless a viewer it was attached to reports it is watching (`attach(on_output, watching=…)`); the TUI view watches while its height is non-zero |
| Rendering, style cache, dirty rows, selection, clipboard, key table, mouse → PTY | scrollable_terminal.py:348-406, :681-1101, :1163-1426 | **Client** | Pure display. The mouse SGR encoding moves out of pty_emulator.py:189-202, so clients send raw bytes. Done in PR 4: the view encodes SGR and calls `session.write(...)` |
| `PtyEmulator` | pty_emulator.py (all) | **Engine** | Since PR 4 it queues raw bytes only (the session decodes incrementally for pyte and the observer), keeps the child's exit code, and no longer encodes mouse events |
| `WorktreeList` agent/git roll-ups | worktree_list.py:160-238 | **Client** | It renders from events |
| `PrStatusBar`, `GitInfoBar` | pr_status_bar.py:29; center_panel.py:72-115 | **Client** | They render from events |

---

## 2. Coupling hotspots

### 2a. Engine logic calling Textual

| # | Where | Coupling | Decoupling move |
|---|---|---|---|
| C1 | monitored_terminal.py:77-86, :155; messages.py | Status/exit is sent through `post_message(AgentStatusChanged/AgentExited)`, so it only reaches `App` through the DOM bubble | `AgentSession` emits to a plain listener (`Callable[[AgentEvent], None]`); the TUI adapter re-posts it as a Textual message. Fixed in PR 4: `StatusChanged` / `SessionExited` (with `exit_code`) go to `add_listener` callables; `MonitoredTerminal` is the adapter |
| C2 | app.py:139-158 | `set_interval` drives the engine timers | Add an `engine/scheduler.py` with plain asyncio tasks, started and stopped by `Engine.start/stop`. Git status and PR timers: fixed in PR 3 (asyncio tasks in `WorktreeService.start/stop`). `check_hangs` and `poll_agents` remain |
| C3 | app.py:263, :312, :355, :383 | `@work(thread=True)` + `call_from_thread` | Use `asyncio.to_thread` inside the engine and publish the results as events; the client applies them on its own loop. Fixed in PR 3 for all four |
| C4 | ipc.py:268, :296, :331, :390, :434 | `self._app.call_later(...)` to hop to the Textual thread | Unnecessary: the IPC server already runs on the same loop. Call `Engine` methods directly |
| C5 | ipc.py:365-383, :414-429, :480-490, :535-543 | Handlers do `query_one(CenterPanel)` and `panel.spawn_agent / get_agent / cleanup_agent` | Handlers take an `Engine` and never import widgets |
| C6 | ipc.py:172-174, :227, :330 | Reads private `_app.worktrees`, `_git_statuses`, `_config`, `_drop_worktree_agents` | Use the engine's public API. Since PR 3 `worktrees`, `_git_statuses` and `_repo_root` are app properties over `WorktreeService`; the handlers still go through the app until PR 5 |
| C7 | center_panel.py:365-444; orchestrator_panel.py:79-132 | Process spawn is interleaved with `TabbedContent.add_pane`/`mount` | Split into `Engine.spawn_agent(...) → AgentInfo` and a TUI reaction to `agent.spawned` that mounts the tab |
| C8 | app.py:461-472 | The registry pulls `label`, `session_name` and `last_output_time` back from widgets | `label` and `session_name`: fixed in PR 2, the panel registers them with the registry at spawn. `last_output_time`: fixed in PR 4, it is an `AgentSession` field carried on each `StatusChanged` event into `registry.update_status` |
| C9 | app.py:493, :172, :190 | Engine paths call `self.notify` | Publish an `engine.notice` event that clients show as a toast |
| C10 | scrollable_terminal.py:528 (`if self.size.height > 0`) | The pyte feed and screen scan run only while the widget is visible; hidden agents rely on the 0.25 s coalesced flush | Feed the headless screen in the engine regardless of clients. Fixed in PR 4: the session feeds and scans with or without a view (per chunk while one watches, ≤4 Hz otherwise) |
| C11 | scrollable_terminal.py:1242-1273 | **PTY size = widget size.** A hidden widget never resizes | Resize ownership policy, §3c. PR 4 keeps the policy (the view calls `session.resize`, with the same no-SIGWINCH-on-show guard); `latest` lands in PRs 6/7 |

### 2b. Widgets / IPC reaching into engine state

| # | Where | Coupling | Move |
|---|---|---|---|
| R1 | ipc.py:491-512 | `read_agent_output` reads `terminal._screen` (pyte buffer + scrollback) | Engine headless screen: `AgentSession.tail_lines(n)`. Fixed in PR 4 (the handler still finds the session through the panel, `get_session`, until PR 5) |
| R2 | monitored_terminal.py:105-112 | Detection joins `self._screen.buffer`, the *display* screen | Engine headless screen. Fixed in PR 4 |
| R3 | ipc.py:544-575 | `send_agent_input` checks `terminal.emulator/send_queue` and calls widget `send_input` | Use `AgentSession.send_input`. Fixed in PR 4 |
| R4 | app.py:866 | `terminal.send_queue.put_nowait(["stdin", …])` on the shell widget | Use `Engine.write_input(shell_session, bytes)`. Since PR 4 it calls `session.write(...)` on the shell session, still found through the widget |
| R5 | app.py:194, :252, :339 | The app pushes `_agent_states` and `_git_statuses` dicts by reference into `WorktreeList` | The client store is built from events. Git statuses: since PR 3 the sidebar is updated from `git_status.updated` / `worktrees.changed` |
| R6 | center_panel.py:318; orchestrator_panel.py:61-65 | `has_agent` is derived from `terminal.emulator is not None` | Comes from the registry. Since PR 4 it reads `session.running` |
| R7 | agent_providers.py:254-267 (`env_exports`) | Agents inherit the env **of the process that spawns them** | The spawning client sends its env with `spawn_agent`; the engine uses that instead of its own `os.environ` (D5) |
| R8 | mcp_server.py:85-150 | Socket discovery walks ancestor PIDs to `/tmp/lazyagent-<pid>/ipc.sock` | Still works, because agents are children of the engine. Add a stable per-repo path later (§4f) |

---

## 3. Terminal rendering split

### 3a. Model

```
PTY master ──bytes──► Engine AgentSession
                       ├─ ring buffer (raw bytes, offset-addressed, e.g. 4 MiB)
                       ├─ headless ScrollbackScreen (pyte today, Rust later)
                       │    └─ detection scan (observer.on_screen_update), read_agent_output, snapshots
                       ├─ observer.on_terminal_output / poll (hooks, telemetry)
                       └─ fan-out ──► client A (TUI: own pyte display emulator)
                                  └─► client B (web: xterm.js)
```

- **Engine headless emulator:** fed from every chunk, coalesced at ≤4 Hz while nobody is attached to that session and per chunk while somebody is. It replaces C10. The cost is pyte for *all* agents all the time (K1), which is also where a Rust emulator would pay off later.
- **Raw bytes, not decoded text:** today `PtyEmulator` decodes UTF-8 before queueing (pty_emulator.py:178). The engine should keep the raw bytes for the ring and the clients (xterm.js wants `Uint8Array`) and decode separately for pyte. That avoids lossy `replace` decoding reaching clients. Done in PR 4: the emulator queues raw bytes; the session appends them to the ring, hands them to viewers with their offset, and decodes them with an incremental decoder for pyte and the observer, so a character split across chunks is never replaced.
- **Clients emulate for display:** the TUI keeps `ScrollableTerminal` rendering, fed from stream frames instead of the PTY; the web uses xterm.js. Keys→bytes stays in the client (TUI `ctrl_keys`, scrollable_terminal.py:363-406; xterm.js natively), and the client sends raw `input` frames.
- **Programmatic input stays in the engine:** `send_agent_input` (frame + ack + submit) needs modes and output timing (scrollable_terminal.py:1108-1161).

### 3b. Attach, reconnect and replay

| Option | How | Pros | Cons |
|---|---|---|---|
| A. Raw replay | Send the ring buffer from its oldest offset | Exact bytes, trivial | If the ring has wrapped, it starts mid-escape-sequence or mid-screen-mode, so the state is wrong; replay time grows with the ring size |
| **B. Snapshot + live (recommended)** | The engine serialises the headless screen to ANSI **at offset N** (reset, modes, scrollback lines with SGR, screen rows, cursor pos/visibility, DECSET 1049/2004/1000/25), then streams live from N+1 | Bounded size, correct state, works for any client emulator | Fidelity is limited to what pyte models (pyte_patch.py adds dim; no images or hyperlinks) |
| C. B + raw tail | Snapshot at N, plus raw bytes from N−k to N when they are still in the ring | Lets a client that has seen ≤ offset N resume with no snapshot | More complex. Use it only for *reconnect* (client sends `from_offset`) |

- **Proposed rules:**
  - `attach_stream(session, from_offset?)`. If `from_offset` is still in the ring, the engine sends the raw tail (C). Otherwise it sends `stream.snapshot` (B).
  - The snapshot and the start of the live stream are generated in one loop step, so there is no gap.
  - Every data frame carries `offset`. A client that sees a gap re-attaches.
  - Scrollback in the snapshot is capped (e.g. 2,000 lines). A client-side `fetch_scrollback(session, before_line, n)` can page further back later.

### 3c. Resize ownership when several clients have different sizes

A PTY has one size, and the headless screen must match it.

| Policy | Behaviour | Fit |
|---|---|---|
| tmux `smallest` | PTY = min over attached clients | Penalises the big screen when a phone is attached |
| **`latest` (recommended)** | PTY = size of the client that most recently sent input to, or explicitly focused, that session | Matches intent: whoever is typing gets a correct layout. tmux added this as `window-size latest` |
| Fixed | Engine default (e.g. 200×50) | Breaks full-screen TUIs on small clients |

- **Rules:**
  - `resize(session, rows, cols)` is honoured only from the session's current **size owner**.
  - `claim_size(session)` makes a client the owner. It is sent implicitly on the first `input` frame and when a session becomes visible and focused.
  - Non-owners render at the PTY size and pad or crop. The TUI widget must size its display pyte to the **PTY** size, not the widget size, which is a behaviour change from scrollable_terminal.py:1268-1271.
  - Hidden TUI panels never claim (this keeps today's "don't SIGWINCH on worktree switch" guard, :1259-1264).
  - MCP never claims.
  - With no clients attached, the last size is kept.
  - Every size change emits `stream.resized {rows, cols, offset}`.

---

## 4. Protocol draft (v1)

### 4a. Transport and framing

| Transport | Used by | Framing |
|---|---|---|
| Unix socket `engine.sock` (0600, dir 0700 as today, ipc.py:735-753) | TUI, MCP (later), CLI | `u32 length` + `u8 kind` + payload. `kind=1` is JSON control, `kind=2` is a binary stream frame |
| WebSocket `ws://127.0.0.1:<port>/v1` | Web UI, Tauri | Text frames = JSON control; binary frames = stream frames |
| **Legacy** `ipc.sock`, NDJSON (ipc.py:6-10) | Existing `mcp_server.py` and running agents | Unchanged until the MCP moves to v1; kept for at least one release afterwards |

Binary stream frame payload: `u8 type` (1 = output, 2 = input) · `u32 stream_id` · `u64 offset` (output only) · bytes.

- All integers are big-endian. The `u32 length` counts the payload only, not the 5-byte header. Frames over 16 MiB are rejected.
- `offset` is the position of the frame's first data byte in the session's output since the session started.
- A bad header (unknown `kind`, oversized length) is unrecoverable and closes the connection. A bad payload inside a well-delimited frame (invalid JSON, short stream header) is reported and skipped.

### 4b. Envelope

- **Request:** `{"type":"req","id":"…","method":"…","params":{…}}`. This is the IPC shape plus `type`.
- **Response:** `{"type":"res","id":"…","result":…}` or `{"type":"res","id":"…","error":{"code","message","data?"}}`. Codes are the existing ones (ipc.py:148-161): `UNKNOWN_METHOD`, `WORKTREE_ERROR`, `VALIDATION_ERROR`, `TIMEOUT`, `INTERNAL_ERROR`. New: `UNAUTHORIZED`, `UNSUPPORTED_VERSION`, `NOT_SIZE_OWNER`, and `PROTOCOL_ERROR` (malformed envelope). Codes are an open set: a peer must handle one it does not know.
- **Event:** `{"type":"evt","seq":123,"event":"agent.status","data":{…}}`.

### 4c. Handshake and versioning

- The first frame is an ordinary request with method `hello` and params `{protocol: 1, client: "tui"|"web"|"mcp"|"cli", client_version, token?}`. The result is `{protocol: 1, engine_version, capabilities: [...], repo_root, engine_id}`.
- **Major version** is an integer; a mismatch returns an `UNSUPPORTED_VERSION` error response with `data: {supported: [1], requested}`. The client checks the reply's `protocol` the same way.
- **Additive changes** (new methods, fields or events) need no bump and are advertised in `capabilities`. Both sides **ignore unknown fields and events**.
- **JSON Schemas** are kept in `protocol/schema/v1/`: `envelope.json`, `handshake.json`, `methods.json` (`<method>.params` / `<method>.result`) and `events.json` (one definition per event). Contract tests validate both directions against them (§6). Python validates them with a small stdlib validator for the keyword subset they use (`protocol/validation.py`), so the package has no dependencies; a non-Python engine can use any JSON Schema validator.
- Schemas are lenient on the wire (unknown fields allowed). Tests validate in a strict mode that rejects undeclared fields, which is how the golden fixtures prove that every field the legacy server sends is described.
- **Unknown enum values must be tolerated by clients.** Value sets that grow over time (agent `status` / `status_before` / `previous_status`, `confidence`, `delivery`) are open: the schema declares them as `{"type": "string", "x-known-values": [...]}` instead of `enum`. On the wire any string is valid, and a client that meets a value it does not know treats it as opaque (shows it raw, does not crash, does not reject the message). Strict mode enforces `x-known-values`, so tests still catch a value the schema has not been told about. Other JSON Schema validators ignore the `x-` keyword as an annotation. Closed sets that only the caller chooses (`resume_mode`, `kind`, `severity`) stay `enum`.

### 4d. Methods

| Method | Origin | Notes |
|---|---|---|
| `list_worktrees` | ipc.py:167 | unchanged |
| `create_worktree` | ipc.py:195 | Custom create always runs as an engine job; its output is streamed via `job.output` / `job.finished` events (D6). `remove_worktree` custom commands likewise |
| `remove_worktree` | ipc.py:302 | unchanged |
| `spawn_agent` | ipc.py:335 | + `kind: "agent"\|"orchestrator"`, `system_prompt` composed by the engine, `env` from the calling client (D5; MCP forwards the requesting agent's env); returns `session_id` + `cursor` |
| `stop_agent` | ipc.py:403 | unchanged |
| `get_agent_status` | ipc.py:442 | unchanged |
| `read_agent_output` | ipc.py:470 | Reads the engine headless screen |
| `send_agent_input` | ipc.py:523 | unchanged semantics (framing, ack, delivery) |
| `list_agents` | ipc.py:651 | + `session_id`, `kind`, `size`, `attached_clients` |
| `list_events` | ipc.py:624 | unchanged (agent status events only, for MCP compatibility) |
| `subscribe {since?, events?}` / `unsubscribe` | new | Push events. `since` replays from the event log (as `list_events`) |
| `attach_stream {session_id, from_offset?}` / `detach_stream` | new | §3b. Returns `stream_id`, `size`, `snapshot?` |
| `resize {session_id, rows, cols}` / `claim_size` | new | §3c |
| `open_shell {worktree_path}` | new | Gets or creates the worktree `bash -l` session |
| `refresh_worktrees` | new | Replaces `call_later(_load_worktrees)` |
| `refresh_git_status {paths?}` | new | Alt+G (app.py:742) |
| `get_diff {worktree_path}` | new | app.py:355; capped as worktree_manager.py:48-50 |
| `get_pr_info {worktree_path}` | new | Cached, deduplicated |
| `set_interest {worktree_paths}` | new | Scopes the git/PR pollers to what some client shows |
| `get_config` | new | Read-only subset for modals |
| `shutdown {stop_agents: bool}` | new | `lazyagent engine stop` |

Input bytes are sent as **binary frames** (`type=2`), not as a JSON method, which avoids base64 for keystrokes.

### 4e. Events

| Event | Data | Replaces |
|---|---|---|
| `agent.status` | `AgentEvent.to_dict()` (agent_events.py:85-102) | `AgentStatusChanged` message |
| `agent.spawned` | `{worktree_path, agent_id, session_id, label, session_name, kind}` | Tab mount in `spawn_agent` |
| `agent.exited` | `{worktree_path, agent_id, session_id, exit_code?}` | `AgentExited` message |
| `worktrees.changed` | full list (same shape as `list_worktrees`) | `_load_worktrees` → `set_worktrees` |
| `git_status.updated` | `{path: GitStatus}` | `_apply_git_statuses` |
| `pr_info.updated` | `{path, pr_info\|null}` | `_apply_pr_info` |
| `stream.resized` / `stream.reset` | `{stream_id, rows, cols, offset}` | — (reset = slow client dropped, so re-attach) |
| `engine.notice` | `{severity, message}` | `self.notify` from engine paths |
| `job.output` / `job.finished` | `{job_id, text}` (UTF-8 decoded with replacement) / `{job_id, exit_code\|null}` | Custom create/remove typed into the shell (app.py:782, :838) (D6) |
| `engine.shutdown` | `{reason}` | — |

- **Event log:** one engine-wide `seq`. `AgentEventBus` (agent_events.py:123) generalises to typed events; `list_events` keeps filtering to `agent.status` so the MCP cursors stay valid.
- **Backpressure:** each client has a bounded output queue (e.g. 1 MiB per stream). On overflow the engine drops that client's stream and sends `stream.reset`; the engine itself never blocks on a slow client.

### 4f. Auth and discovery

- **Runtime dir:** `$XDG_RUNTIME_DIR/lazyagent/<sha1(repo_root)[:12]>/` (falls back to `/tmp/lazyagent-<uid>/…`). It holds `engine.sock`, `ipc.sock`, `token` (0600, 32 random bytes), `engine.json` (pid, port, version) and `engine.lock` (flock = single instance per repo).
- **Unix socket:** filesystem permissions as today, plus an optional `SO_PEERCRED` uid check. A token is not needed.
- **WebSocket:** bind `127.0.0.1` only.
  - **Token is required.** The client sends it in `hello`; the browser takes it from a URL fragment (`/#token=…`) that `lazyagent web` prints.
  - **Origin and Host headers are checked** against `http://127.0.0.1:<port>` and `localhost`. This defends against DNS rebinding and against cross-site WebSocket hijacking from any open browser tab. **Not optional:** the API can spawn shells.
- **Legacy compatibility:** keep creating `/tmp/lazyagent-<engine_pid>/ipc.sock` (symlink or second listener), so `_discover_socket` (mcp_server.py:116) and the already-written `LAZYAGENT_SOCKET` in agents' mcp-config files (agent_providers.py:427-441) keep working.

### 4g. MCP mapping

| MCP tool (mcp_server.py) | Engine method |
|---|---|
| `list_worktrees` :179 | `list_worktrees` |
| `create_worktree` :189 | `create_worktree` |
| `remove_worktree` :232 | `remove_worktree` |
| `spawn_agent` :251 | `spawn_agent` |
| `list_agents` :298 | `list_agents` |
| `stop_agent` :315 | `stop_agent` |
| `get_agent_status` :334 | `get_agent_status` |
| `read_agent_output` :352 | `read_agent_output` |
| `send_agent_input` :376 | `send_agent_input` |
| `list_events` :421 | `list_events` |

- **Phase 1:** the MCP server keeps talking NDJSON to the legacy socket, with **zero changes**.
- **Later:** swap `IpcClient` (mcp_server.py:26-64) for `lazyagent.protocol.client` (v1, `client:"mcp"`). The tools stay 1:1, so it becomes just another client.

---

## 5. Repo layout

### 5a. Packages

```
src/lazyagent/
  __init__.py            # no pyte: the patch is imported by the terminal widget (later engine/pty.py)
  cli.py                 # entry point, subcommands
  protocol/              # NO Textual, NO pyte imports
    envelope.py          # req/res/evt dataclasses, error codes
    framing.py           # u32-len frames, stream frame pack/unpack
    handshake.py         # PROTOCOL_VERSION, hello / version checks
    validation.py        # stdlib validator for schema/v1
    client.py            # asyncio client (unix + ws), later
    schema/v1/*.json     # contract schemas
  engine/                # NO Textual imports (enforced by test)
    engine.py            # Engine facade: start/stop, public API
    registry.py          # AgentRegistry, AgentIdStore, ORCHESTRATOR_KEY (from app.py:104, :218-248)
    session.py           # AgentSession/ShellSession: PtyEmulator + headless screen + observer + ring
    screen.py            # ScrollbackScreen (moved from widgets/scrollable_terminal.py:194-263) + ANSI snapshot
    worktrees.py         # WorktreeService: list/git/pr/diff caches + interest-scoped pollers (from app.py:139-158, :263-413)
    events.py            # generalised AgentEventBus
    scheduler.py
    server.py            # v1 server (unix + ws)
    ipc_legacy.py        # today's ipc.py, handlers rewired to Engine
    providers/           # agent_providers.py, agent_observers.py, orchestrator_prompt.py
    pty.py               # pty_emulator.py, pyte_patch.py
  tui/                   # Textual client
    app.py, widgets/..., store.py (event-built mirror), backend.py (InProcess | Remote)
  mcp_server.py          # stays at this path (running agents exec `-m lazyagent.mcp_server`)
  web/static/            # built assets (not in git), served by engine.server
web/                     # frontend source: Vite + TS + xterm.js
```

- **Shims:** leave re-export modules at the old paths (`lazyagent.app`, `lazyagent.ipc`, `lazyagent.widgets.*`) for one release, so tests and imports migrate gradually.
- **Import boundary:** a test (or `ruff` banned-imports / `import-linter`) fails if `lazyagent.engine` or `lazyagent.protocol` imports `textual`. `lazyagent.protocol` is stricter: stdlib only and no other `lazyagent` module (tests/protocol/test_boundary.py). `import lazyagent.protocol` loads no pyte: `pyte_patch` is imported by `engine/screen.py` (since PR 4 the only module that builds pyte screens; `widgets/scrollable_terminal.py` imports it too) rather than by `lazyagent/__init__.py`. `import lazyagent.engine` loads no pyte either; `engine.session` / `engine.screen` do, and tests/engine/test_boundary.py checks that they still load no Textual (tests/conftest.py applies it for tests that use pyte directly).

### 5b. uv workspace vs single package

| | Single package + extras (**recommended now**) | uv workspace (`lazyagent-engine`, `lazyagent-tui`, …) |
|---|---|---|
| Release | One wheel, one tag (current `/release` skill, publish.yml) | N wheels, version pinning between them |
| Install | `pipx install lazyagent` keeps working | Users need a meta-package |
| Boundary enforcement | Import-lint test | Hard, by dependency graph |
| When to switch | Phase 5 (Rust engine, built with maturin) or a Python-free engine | — |

- **Extras:** base = engine + TUI + MCP (keeps `lazyagent` behaviour). `lazyagent[web]` = `aiohttp` (HTTP static + WS in one server) or `websockets`, decided in Phase 2. Textual stays in base; an engine-only install is not a goal yet.

### 5c. Web assets in the wheel

- Source lives in `web/` (pnpm/npm + Vite).
- The CI `build` job runs `npm ci && npm run build` into `src/lazyagent/web/static/`. Then `uv build` picks it up via `[tool.hatch.build.targets.wheel] artifacts = ["src/lazyagent/web/static/**"]`, which ships files that are not in git.
- The sdist builds a wheel without the assets, and `lazyagent web` then errors with a clear message. Alternative: a hatch build hook that runs npm, which needs Node at install time (no).

### 5d. CLI

| Command | Behaviour |
|---|---|
| `lazyagent [repo]` | TUI. Connects to the repo's engine, **auto-starting** it detached when none is running (tmux-like), so the unattended tmux boot keeps working unchanged (D4). `q` only detaches the screen; agents keep running. `--embedded` = in-process engine (old behaviour; migration fallback only) |
| `lazyagent engine [repo] [--foreground] [status\|stop [--keep-agents]]` | Runs or manages the engine |
| `lazyagent web [repo] [--port N] [--open]` | Ensures the engine with WS is up, prints `http://127.0.0.1:N/#token=…` |
| `lazyagent mcp` | Alias of `python -m lazyagent.mcp_server` |

- **Parsing:** argparse subparsers with backward-compatible positional parsing. If `argv[1]` is not a known subcommand it is treated as `repo`, so a directory literally named `engine` must be passed as `./engine`.

### 5e. CI additions

- Trigger on `develop` too. Today ci.yml only runs on `main` push/PR, which is why stacked PRs to develop go untested. **Shipped now as its own small PR, independent of this project (D10).**
- Job `contract`: protocol tests that start a real engine subprocess with a **fake agent provider** (a scripted PTY program that prints known ANSI and reacts to input) and drive it over the socket.
- Job `boundary`: the no-Textual-in-engine import check.
- Job `web` (Phase 2+): `npm ci`, typecheck, unit tests, and a Playwright smoke test against a real engine with the fake agent.
- Job `wheel`: build, then assert `web/static/index.html` is inside the wheel and `lazyagent --help` runs from a clean venv.
- Matrix stays Linux-only: macOS is out of scope (D9), so the PTY code and the `/proc` ancestor walk (mcp_server.py:85) can stay Linux-specific.

---

## 6. Migration plan: strangler PRs to `develop`

Each PR keeps `lazyagent` behaving as today and the 523 tests passing (some move with the code).

- **Prerequisite, outside this plan:** a small PR that enables CI on `develop` (D10), so every PR below runs in CI.

| # | PR | Changes | Tests added | Phase |
|---|---|---|---|---|
| 1 | **protocol: envelope + schemas** | `lazyagent/protocol/` (envelope, framing, handshake, v1 JSON schemas for the 10 existing methods and the §4e events). Stdlib only, relative imports. No runtime use | Golden fixtures of today's IPC responses (taken from test_ipc.py cases), validated against the schemas; framing round-trip and fuzz | 0 |
| 2 | **engine: AgentRegistry + event log** | Move app.py:104-107, :218-248, :454-482 logic into `engine/registry.py`. The app delegates | Unit tests on the registry (status → event publish, rollup, drop → EXITED); monotonic agent ids persisted in the state dir and surviving a registry reload (D8) | 1 |
| 3 | **engine: WorktreeService** | Move list/git/diff/PR caches and pollers (app.py:139-158, :263-413) to asyncio + `to_thread`, with interest scoping. The app subscribes and applies on its loop; its interest is the selected worktree, so polling is unchanged. PR info is fetched in one batched `gh api graphql` call for the whole interest set, with `gh pr view` per worktree as the fallback | Service tests with a fake `WorktreeManager`: the pollers respect the interest set, batching and its fallback, change-only events, caches dropped with their worktree; parse tests of the GraphQL response, and parity with the `gh pr view` parse | 1 |
| 4 | **engine: AgentSession** | `PtyEmulator` + observer + scan/hang logic + modes + `send_input` + ring buffer in `engine/session.py`. `MonitoredTerminal` becomes a *view* that renders from the session's screen (shared in-process, so there is no double parse yet). Fixes C1, C10, R1-R3. As landed: the worktree shell and the orchestrator also run on sessions (`TerminalSession` / `AgentSession`); panels still create them until PR 5 | Session tests with the fake-agent PTY: status transitions, send_input framing/ack, `tail_lines`, ring offsets | 1 |
| 5 | **engine: Engine facade, IPC rewired** | `Engine.spawn/stop/create/remove/open_shell` absorb center_panel.py:365-444, orchestrator_panel.py:79-132 and the app actions. `ipc.py` handlers call `Engine` only (removes C4-C6). The TUI reacts to `agent.spawned/exited` | Existing test_ipc against `Engine` without the Textual app; **boundary test** (engine imports no textual) | 1 |
| 6 | **engine: v1 server (unix)** | `engine/server.py` speaking §4: hello, req/res, subscribe, attach_stream with snapshot, input frames, resize/claim. Runs in-process alongside the legacy socket | **Contract suite** (black-box over the socket): every method, event ordering, snapshot + live has no gap, offsets, version mismatch, slow-client reset, size-owner rules | 0/1 |
| 7 | **tui: RemoteBackend** | The TUI talks to the engine only through a `Backend` interface (InProcess / Remote). The terminal widget gets a display pyte fed by stream frames, sized to the PTY. Behind `LAZYAGENT_ENGINE=remote` | TUI pilot tests run against both backends (parametrised fixture) | 1 |
| 8 | **cli: `lazyagent engine` + auto-start** | Subcommands, runtime dir, lock, detach (setsid), logs, discovery; legacy `ipc.sock` path kept. Default stays `--embedded` until soaked, then flips in a follow-up PR | Subprocess tests: start, attach, quit TUI and the agent survives, reattach and the snapshot matches, `engine stop` | 1 |
| 9 | **engine: restart manifest** | Persist registry metadata (session names, worktree, provider) so that after an engine restart it can offer `resume_mode=last` re-spawns. The per-worktree id counters were already persisted in PR 2 (D8) | Unit tests | 1 |
| 10+ | **web** | WS transport + token + Origin; static serving; `web/` scaffold with sidebar, xterm.js terminal and status; `lazyagent web`; CI web and wheel jobs | Contract suite also over WS; Playwright smoke | 2 |
| … | Diff review with comments (`get_diff` structured + comment threads → `send_agent_input`), notifications (events → Notification API), prompt editor | per feature | 3 |
| … | Tauri wrapper | — | 4 |
| … | Rust engine | Must pass the **same contract suite** over the socket; that is what the suite is for | 5 |

- **Ordering:** PRs 2-5 are pure refactors (in-process) and can each ship in a release. PR 6 adds capability without a user-visible change. PR 7 is the first switchable behaviour, and PR 8 makes "agents survive the UI" real.

### 6b. Alternative ordering: hosted mode first

Same protocol, different order: get a GUI on screen before the engine leaves the TUI process. The GUI **hosts the existing TUI** in a terminal panel (xterm.js) and paints native widgets from structured data that lazyagent sends over a side channel. This is the Warp and VS Code shell-integration pattern.

```
┌─────────────── App (GUI) ──────────────────────────────────┐
│  ┌─ terminal panel (xterm.js) ─┐   ┌─ native widgets ─────┐│
│  │  lazyagent TUI (compact)    │   │ PR badges, agent     ││
│  │                             │   │ states, notifications││
│  └─────────────▲───────────────┘   └──────────▲───────────┘│
└────────────────┼──────────────────────────────┼────────────┘
                 │ PTY bytes                    │ JSON events (side channel)
            lazyagent ──────────────────────────┘
```

- **Host detection:** the app launches `lazyagent` with `LAZYAGENT_HOST=<socket>` (same idea as `TERM_PROGRAM`). Without it, the TUI is unchanged. With it, the TUI enters **hosted mode**: it hides what the host paints (sidebar, PR badges, status bar) and keeps the agent terminals.
- **Side channel:**

  | Channel | How | Pro | Con |
  |---|---|---|---|
  | **Socket (primary)** | The host connects to the v1 server (PR 6): `list_worktrees`, `subscribe` events, PR status | Already designed; rich data | Doesn't cross SSH without socket forwarding |
  | **OSC escapes (secondary)** | lazyagent emits private OSC sequences in its output; the host strips and handles them (like OSC 9 / OSC 133) | Works over SSH and inside tmux (needs `allow-passthrough on`) | Only for small payloads: notifications, focus hints |

- **Sequence:** PRs 1-6 as in §6 → **H1** hosted-mode flag and compact layout in the TUI → **H2** minimal host app (xterm.js panel + widgets fed by `subscribe`) → replace TUI parts with native widgets as needed → resume PRs 7-9 when "agents survive the UI" is needed.

| | Engine first (§6) | Hosted mode first (§6b) |
|---|---|---|
| First visible GUI | After PRs 1-9 (web is PR 10+) | After PR 6 + H1/H2 |
| Agents survive closing the UI | ✅ at PR 8 | ❌ until PRs 7-8 land |
| Risk | Big refactor before any visible result | Low, mostly additive |
| Duplication | None | TUI and host paint similar data until compact mode covers it |

Both orderings share the protocol and the contract suite, so choosing one doesn't discard work from the other. Hosted mode first was chosen (D11).

---

## 7. Risks and open questions

### Risks

| # | Risk | Mitigation |
|---|---|---|
| K1 | The engine feeds pyte for **every** agent continuously; today hidden agents are coalesced to 0.25 s and the scan is gated on visibility (scrollable_terminal.py:54, :528) | Coalesce headless feeds at ≤4 Hz when unattached; detection already tolerates staleness. A Rust or native emulator is the Phase 5 lever. Since PR 4: `UNWATCHED_FEED_INTERVAL` = 0.25 s, per chunk only while a viewer watches; the cost matches the pre-PR 4 hidden-widget path |
| K2 | Double emulation (engine + TUI display) once the engine is out of process | The TUI subscribes only to visible sessions. The in-process backend shares the screen object (PR 4) |
| K3 | The engine crashing kills **all** agents (the PTY master closes, SIGHUP) | Keep the engine small and well tested; restart manifest (PR 9); crash log in the state dir |
| K4 | Agents would inherit the **engine's** env (`env_exports`, agent_providers.py:254), and an auto-started engine would freeze `SSH_AUTH_SOCK`, `DISPLAY` and `PATH` from the first TUI | Resolved by D5: `spawn_agent` carries the client's env. Open detail: which env MCP-initiated spawns use (proposal: the MCP server's own env, which is the requesting agent's) |
| K5 | Running agents hold `mcp-config.json` files that point at `sys.executable -m lazyagent.mcp_server` and a socket path (agent_providers.py:427-441) | Never move that module path; keep the legacy socket; an engine upgrade means restarting agents |
| K6 | Web exposure: a localhost WS that can spawn shells is reachable from any browser tab | Token plus strict Origin/Host checks (§4f), 127.0.0.1 only, no CORS |
| K7 | Snapshot fidelity is limited to pyte's model (no OSC 8 links, images, some modes) | Prefer the raw tail when it is available (§3b option C); snapshot only as a fallback |
| K8 | Unattended boot (tmux → `lazyagent` → `/gio-planner`) changes once the TUI auto-starts a detached engine | D4: the TUI auto-starts the engine, so the boot script needs no change. PR 8 must include a test of the cold-start path |
| K9 | Textual-coupled tests (test_app_integration, test_center_panel, test_multi_agent_panel) need fixtures for the backend | Parametrised backend fixture in PR 7 |

### Decisions (2026-10-15)

Giovanni answered the open questions; the sections above already reflect these.

| # | Topic | Decision |
|---|---|---|
| D1 | Per-worktree shells | Engine sessions (`kind:"shell"`), shared across clients and surviving UI close |
| D2 | Engine scope | One engine per repo for Phase 1 (the protocol still carries `repo_root`) |
| D3 | Resize policy | `latest`: the active client owns the PTY size (§3c) |
| D4 | Quit / start | `q` only detaches the screen and agents keep running. The TUI auto-starts the engine when none is running, so the unattended tmux boot keeps working |
| D5 | Agent env | Agents get their env from the client that spawns them, not from the engine (`spawn_agent.env`) |
| D6 | Custom create/remove | Run as engine jobs with streamed output (`job.output` / `job.finished`) |
| D7 | Web stack, framework, Tauri | Deferred to Phase 2 |
| D8 | Agent ids | Monotonic per worktree and persisted across engine restarts (PR 2). Counters live in `$XDG_STATE_HOME/lazyagent/agent_ids.json` (`~/.local/state/…`), keyed by the worktree's absolute path and shared by every lazyagent instance; writes are `flock` (on `agent_ids.json.lock`) + temp file + `os.replace`. A missing or corrupt file restarts at 1, never below an id live in the registry; a corrupt file is first renamed to `agent_ids.json.corrupt-<UTC timestamp>` (newest 3 kept). The tab label follows the id (`Agent N`, matching the session name and the MCP `agent_id`), so the first agent after a restart may not be "Agent 1". It is the state dir, not the runtime dir: `$XDG_RUNTIME_DIR` is wiped at logout, which would defeat the point |
| D9 | Platforms | Linux only; macOS out of scope |
| D10 | CI on `develop` | Enable now, as a separate small PR outside this migration |
| D11 | Ordering | Hosted mode first (§6b). PRs 1-6, then H1/H2, then PRs 7-9 when agents need to survive the UI |
