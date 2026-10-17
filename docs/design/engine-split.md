# Engine / screen split: design

- **Status:** draft for review. Design only, no code changes.
- **Base:** `develop` @ `4ca663d` (release 0.8.0). PRs 1-9 and H1/H2 have landed since; the "As landed" notes record what changed. Baseline: `uv run pytest -q` gives **523 passed** (56 s).
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
| `repo_path`, `_repo_root` | app.py:99, :112, :180-186 | **Engine** | Defines the engine's identity: one engine per repo root. Since PR 5 `Engine.repo_path` / `Engine.repo_root`; the app's `_repo_root` is a property over it |
| `worktrees: list[WorktreeInfo]` | app.py:100, :188 | **Engine** (cache) + client mirror | IPC `list_worktrees` reads it (ipc.py:172); clients render it from `worktrees.changed`. Since PR 3 it lives in `WorktreeService` (rescan included) and `app.worktrees` is a property over it |
| `_agent_states` (2-level registry) | app.py:104, :218-248 | **Engine** (`AgentRegistry`) + client mirror | This is the source of truth for MCP, the sidebar and events. Since PR 5 the `Engine` owns the registry and updates it from its sessions' events; the app only reads it |
| `agent_events: AgentEventBus` | app.py:107; agent_events.py:123 | **Engine** | The cursor log behind `list_events` and `send_agent_input` confirmation (ipc.py:596). Since PR 5 `Engine.events` is an `EventBus` (engine/events.py): every §4e event shares its `seq`, `list_events` still returns `agent.status` only |
| `_git_statuses` cache | app.py:108, :332-340 | **Engine** | Shared by every client and by MCP `list_worktrees` (ipc.py:174). Since PR 3: `WorktreeService.git_statuses`, merged there; `app._git_statuses` is a property over it |
| `_selected_worktree`, `_orchestrator_selected` | app.py:109-110, :417-450 | **Client** | Pure view state. The engine only learns an *interest set* (see `set_interest`, §4) that it uses to scope polling. Since PR 3 the TUI sets its interest to the selected worktree (empty for the orchestrator or no selection) |
| `_selection_debounce` timer | app.py:116, :284-310 | **Client** | UX throttling. Becomes a debounced `set_interest` / `refresh_git_status` call. PR 3 keeps it in the app: interest is set at once, the debounced refresh calls the service |
| `_config: Config` | app.py:111, :174-178 | **Engine**, with a read-only copy for clients | Spawn/create/remove need it (ipc.py:227, :373). Modals need `default_branch` and `has_custom_create` (app.py:759-760) through `get_config`. Done in PR 5: `Engine.config`, `Engine.get_config()` → `ConfigView` |
| `_gh_available` | app.py:113, :390-394 | **Engine** | Belongs to the PR poller. Moved to `WorktreeService` in PR 3 |
| `_ipc_server`, `_ipc_socket_path` | app.py:114-115, :167-172, :733-735 | **Engine** | It is the engine's server. `LAZYAGENT_SOCKET` is injected into agents (agent_providers.py:438-441). Done in PR 5: `Engine.start()` starts it with the engine (`Engine.socket_path`) and `Engine.stop()` stops it |
| Timer `check_hangs` 60 s | app.py:139, :512-525 | **Engine** | Agent liveness. Done in PR 5: `engine/scheduler.py`, run by `Engine.start/stop` |
| Timer `poll_agents` 1 s | app.py:143, :501-510 | **Engine** | Hook-log tailing, which feeds status. Done in PR 5, same scheduler |
| Timer `git_status` 60 s (selected only) | app.py:149-152 | **Engine**, scoped to the union of clients' interest sets | Done in PR 3: a `WorktreeService` asyncio poller over the interest set |
| Timer `diff` 30 s (selected only) | app.py:153-155 | **Engine** on request; the client polls while the diff tab is visible | Diff is large and only wanted when it is shown. Since PR 3 the timer stays in the app and calls `WorktreeService.get_diff` |
| Timer `pr_status` 60 s (selected only) | app.py:156-158 | **Engine**, scoped by interest | `gh` network calls should be deduplicated across clients. Done in PR 3: a `WorktreeService` poller; a worktree whose fetch is in flight is not fetched again, and the interest set is fetched in one batched `gh api graphql` call ([proposal 0002](../proposals/0002-github-ci-triggers.md#polling-strategy)) |
| Worker `_refresh_git_statuses` (all) | app.py:263-282 | **Engine** (`asyncio.to_thread`) | Triggered by rescan/create/remove. PR 3: `WorktreeService.refresh_git_status()`, which the app runs as an exclusive async worker. Since PR 5 `Engine.refresh_worktrees` starts it as an engine task (a new sweep cancels the previous one) |
| Worker `_refresh_selected_git_status` | app.py:312-330 | **Engine** | Same as above, for one path. PR 3: `refresh_git_status([path])` |
| Worker `_refresh_selected_diff` | app.py:355-370 | **Engine** method `get_diff` | Request/response. PR 3: `WorktreeService.get_diff`, which also keeps the last diff per worktree |
| Worker `_refresh_pr_status` | app.py:383-405 | **Engine**, pushed as `pr_info.updated` events | Was not exclusive, so concurrent `gh` calls were possible. PR 3: `refresh_pr_info(paths)` deduplicates per path and emits `pr_info.updated` only on change |
| `on_agent_status_changed` (registry update, label/session pull from panel, event publish) | app.py:454-482 | **Engine** | Pulling label and session from the panel (:461-468) goes away, because the engine sets them at spawn. Done in PR 5: the engine listens to each session; the app only refreshes the sidebar on `agent.status` / `agent.updated` |
| `on_agent_exited` | app.py:484-497 | **Engine** (drop + event); **Client** (toast, tab cleanup) | Split by concern. Done in PR 5: `agent.exited` (`stopped: false`) → the app removes the tab and shows the toast |
| `action_spawn_agent` / `_spawn_orchestrator_agent` | app.py:529-602 | **Engine** `spawn_agent` (+ `kind:"orchestrator"`); **Client** keeps the `SpawnModal` | Spawn logic is duplicated today in the IPC handler (ipc.py:365-390). Done in PR 5: both call `Engine.spawn_agent`; the TUI passes its environment (D5) |
| `compose_orchestrator_prompt` | app.py:586; orchestrator_prompt.py:178 | **Engine** | Needs config and repo root only. Done in PR 5 |
| `action_stop_agent` / orchestrator stop | app.py:604-637 | **Engine** `stop_agent`; **Client** chooses the target | The active tab is client state. Done in PR 5 |
| `action_create_worktree` / `_do_create_worktree` | app.py:751-792 | **Engine** `create_worktree`; **Client** modal | The custom-create path differs between UI (types into shell, :782) and IPC (subprocess, ipc.py:253). Both paths become an engine job (subprocess) with streamed output (decision D6). Done in PR 5: `Engine.run_job` in the repo root, with the client's env when the TUI starts it (IPC: the engine's); the TUI shows `job.*` events in a `JobLog` under the worktree view, and selects the new worktree once the job is done |
| `action_remove_worktree` / `_do_remove_worktree` | app.py:794-851 | **Engine**; **Client** confirm modal | The "agent running" guard is duplicated (app.py:803 vs ipc.py:320). Done in PR 5: `Engine.remove_worktree` holds the guards, runs the custom remove as a job (`custom=False` for IPC, which keeps `git worktree remove`), then stops the worktree's agents and shell |
| `_send_to_terminal` | app.py:853-868 | **Engine** `write_input` to the worktree shell session | Writes straight into a widget queue today. Gone in PR 5: its only users were the custom commands, now jobs; `Engine.write_input` remains for clients |
| `action_refresh` / `_load_worktrees` | app.py:738, :180-199 | **Engine** `refresh_worktrees` → `worktrees.changed` | IPC calls it through `call_later` (ipc.py:268, :296, :331). Since PR 3 it calls `WorktreeService.rescan`, which still lists inline (create selects the new worktree right after) and emits `worktrees.changed`. Since PR 5 IPC calls `Engine.refresh_worktrees` directly |
| Focus/tab actions, keybindings, CSS, `compose` | app.py:51-95, :639-728 | **Client** | Pure UI |
| `diag` watchdog/log | app.py:127-138; diagnostics.py | **Both** (separate log files) | Each process needs its own loop watchdog |
| `main()` argparse | app.py:877-899 | **CLI** module, with subcommands | See §5 |

### 1b. State living in widgets (the real runtime)

| State / logic | Where | Goes to | Why |
|---|---|---|---|
| `WorktreePanel._agents / _labels / _session_names / _agent_counter` | center_panel.py:181-185, :320-322 | **Engine** `AgentRegistry` | Agent identity and ids. Ids `a1, a2…` were per-panel counters; since PR 2 the registry allocates them, monotonic per worktree and persisted across restarts (D8), and holds each agent's label and session name. `_agents` (the views) is still per panel; since PR 4 each view wraps an `AgentSession` (`panel.get_session(id)`). Since PR 5 the engine creates the session and the panel only adds its tab (`show_agent`) when `agent.spawned` arrives |
| Provider command + runtime context build | center_panel.py:394-420; orchestrator_panel.py:96-123 | **Engine** `spawn` | Hook temp dirs, MCP config and the session name are all engine concerns. Done in PR 5 |
| Per-worktree shell `bash -l` | center_panel.py:209-230 | **Engine** session `kind:"shell"` (D1) | Custom create/remove commands are typed into it (app.py:782, :838). It should survive the UI closing like the agents do. Since PR 4 it is a `TerminalSession` (`kind = "shell"`) rendered by a plain `ScrollableTerminal`. Since PR 5 `Engine.open_shell` creates it (get-or-create, client env), and `remove_worktree` stops it |
| `OrchestratorPanel._agent_terminal`, `_session_name` | orchestrator_panel.py:35-132 | **Engine** (key `__orchestrator__`) | Same as for agents. Done in PR 5: `spawn_agent(kind="orchestrator")`; the panel shows the session and reads the session name from the registry |
| `MonitoredTerminal` status/detail/`_last_output_time`/observer/scan timer | monitored_terminal.py:42-49, :88-205 | **Engine** `AgentSession` | Status detection. Done in PR 4: `engine/session.py`; `MonitoredTerminal` only re-posts the session's events as Textual messages. Since PR 5 it posts nothing (status and exit reach the app as engine events) and `messages.py` is gone |
| `ScrollableTerminal.emulator`, queues, `recv` loop | scrollable_terminal.py:327-333, :411-447, :485-604 | **Engine** (PTY I/O) | The process lifetime must not depend on a widget. Done in PR 4: `TerminalSession` owns the emulator, the recv loop, a 4 MiB offset-addressed ring of raw output and the exit code |
| Terminal modes `mouse_tracking`, `bracketed_paste` | scrollable_terminal.py:320-324, :606-631 | **Engine** (authoritative); clients derive their own | `send_input` framing for MCP needs them (:1108-1124). Since PR 4 they live on the session; the TUI view reads them from there |
| `send_input` + child-ack + submit | scrollable_terminal.py:1126-1161 | **Engine** method (`send_agent_input`) | It is protocol semantics, not UI. Done in PR 4: `TerminalSession.send_input` |
| `ScrollbackScreen` pyte screen + scrollback | scrollable_terminal.py:194-263, :345-346 | **Both**: engine headless (detection, `read_agent_output`, snapshot); client display emulator | See §3. Since PR 4 the class is in `engine/screen.py` and the session owns the only instance; the in-process TUI renders that same object (K2) |
| Hidden-feed coalescing | scrollable_terminal.py:54, :449-475, :582-599 | **Client** concern (subscribe only to visible streams); the engine feeds the headless screen on its own throttle | Today detection *depends on widget visibility* (:528). Since PR 4 the session batches the feed every 0.25 s unless a viewer it was attached to reports it is watching (`attach(on_output, watching=…)`); the TUI view watches while its height is non-zero |
| Rendering, style cache, dirty rows, selection, clipboard, key table, mouse → PTY | scrollable_terminal.py:348-406, :681-1101, :1163-1426 | **Client** | Pure display. The mouse SGR encoding moves out of pty_emulator.py:189-202, so clients send raw bytes. Done in PR 4: the view encodes SGR and calls `session.write(...)` |
| `PtyEmulator` | pty_emulator.py (all) | **Engine** | Since PR 4 it queues raw bytes only (the session decodes incrementally for pyte and the observer), keeps the child's exit code, and no longer encodes mouse events. Since PR 5 the child is started with `posix_spawnp` + util-linux `setsid --ctty` instead of `pty.fork` (no Python runs in a child forked from a threaded process) |
| `WorktreeList` agent/git roll-ups | worktree_list.py:160-238 | **Client** | It renders from events |
| `PrStatusBar`, `GitInfoBar` | pr_status_bar.py:29; center_panel.py:72-115 | **Client** | They render from events |

---

## 2. Coupling hotspots

### 2a. Engine logic calling Textual

| # | Where | Coupling | Decoupling move |
|---|---|---|---|
| C1 | monitored_terminal.py:77-86, :155; messages.py | Status/exit is sent through `post_message(AgentStatusChanged/AgentExited)`, so it only reaches `App` through the DOM bubble | `AgentSession` emits to a plain listener (`Callable[[AgentEvent], None]`); the TUI adapter re-posts it as a Textual message. Fixed in PR 4: `StatusChanged` / `SessionExited` (with `exit_code`) go to `add_listener` callables; `MonitoredTerminal` is the adapter. PR 5: the engine is the only listener |
| C2 | app.py:139-158 | `set_interval` drives the engine timers | Add an `engine/scheduler.py` with plain asyncio tasks, started and stopped by `Engine.start/stop`. Git status and PR timers: fixed in PR 3 (asyncio tasks in `WorktreeService.start/stop`). `check_hangs` and `poll_agents`: fixed in PR 5 (`engine/scheduler.py`). Only the client's diff timer stays in the app |
| C3 | app.py:263, :312, :355, :383 | `@work(thread=True)` + `call_from_thread` | Use `asyncio.to_thread` inside the engine and publish the results as events; the client applies them on its own loop. Fixed in PR 3 for all four |
| C4 | ipc.py:268, :296, :331, :390, :434 | `self._app.call_later(...)` to hop to the Textual thread | Unnecessary: the IPC server already runs on the same loop. Call `Engine` methods directly. Fixed in PR 5 |
| C5 | ipc.py:365-383, :414-429, :480-490, :535-543 | Handlers do `query_one(CenterPanel)` and `panel.spawn_agent / get_agent / cleanup_agent` | Handlers take an `Engine` and never import widgets. Fixed in PR 5: `IpcServer(engine, path)`; tests/test_ipc.py and the golden cases run against an `Engine` with no app |
| C6 | ipc.py:172-174, :227, :330 | Reads private `_app.worktrees`, `_git_statuses`, `_config`, `_drop_worktree_agents` | Use the engine's public API. Since PR 3 `worktrees`, `_git_statuses` and `_repo_root` are app properties over `WorktreeService`. Fixed in PR 5: the handlers use the engine's public API only |
| C7 | center_panel.py:365-444; orchestrator_panel.py:79-132 | Process spawn is interleaved with `TabbedContent.add_pane`/`mount` | Split into `Engine.spawn_agent(...) → AgentInfo` and a TUI reaction to `agent.spawned` that mounts the tab. Fixed in PR 5 (the reactions run in event order through `call_later`) |
| C8 | app.py:461-472 | The registry pulls `label`, `session_name` and `last_output_time` back from widgets | `label` and `session_name`: fixed in PR 2, the panel registers them with the registry at spawn. `last_output_time`: fixed in PR 4, it is an `AgentSession` field carried on each `StatusChanged` event into `registry.update_status` |
| C9 | app.py:493, :172, :190 | Engine paths call `self.notify` | Publish an `engine.notice` event that clients show as a toast. Fixed in PR 5 for the rescan failure and the IPC start failure; the "agent exited" toast is the client's reaction to `agent.exited` |
| C10 | scrollable_terminal.py:528 (`if self.size.height > 0`) | The pyte feed and screen scan run only while the widget is visible; hidden agents rely on the 0.25 s coalesced flush | Feed the headless screen in the engine regardless of clients. Fixed in PR 4: the session feeds and scans with or without a view (per chunk while one watches, ≤4 Hz otherwise) |
| C11 | scrollable_terminal.py:1242-1273 | **PTY size = widget size.** A hidden widget never resizes | Resize ownership policy, §3c. PR 4 keeps the policy (the view calls `session.resize`, with the same no-SIGWINCH-on-show guard). PR 6: `latest` among v1 clients; the TUI view counts as a client that claims by resizing and by typing (it resizes the PTY back to itself on a key or paste). PR 7: with the remote backend the view renders a display screen at the PTY's size and crops or pads it; it claims the size on the same events (real resize, key, paste), never while hidden |

### 2b. Widgets / IPC reaching into engine state

| # | Where | Coupling | Move |
|---|---|---|---|
| R1 | ipc.py:491-512 | `read_agent_output` reads `terminal._screen` (pyte buffer + scrollback) | Engine headless screen: `AgentSession.tail_lines(n)`. Fixed in PR 4; since PR 5 through `Engine.read_agent_output` |
| R2 | monitored_terminal.py:105-112 | Detection joins `self._screen.buffer`, the *display* screen | Engine headless screen. Fixed in PR 4 |
| R3 | ipc.py:544-575 | `send_agent_input` checks `terminal.emulator/send_queue` and calls widget `send_input` | Use `AgentSession.send_input`. Fixed in PR 4 |
| R4 | app.py:866 | `terminal.send_queue.put_nowait(["stdin", …])` on the shell widget | Use `Engine.write_input(shell_session, bytes)`. Since PR 4 it calls `session.write(...)` on the shell session, still found through the widget. PR 5: the custom commands it served are jobs now; `Engine.write_input` is the API |
| R5 | app.py:194, :252, :339 | The app pushes `_agent_states` and `_git_statuses` dicts by reference into `WorktreeList` | The client store is built from events. Git statuses: since PR 3 the sidebar is updated from `git_status.updated` / `worktrees.changed`. PR 7: the remote backend's store is seeded by `get_sidebar_state` and kept by the events; the app reads it through the `Backend` |
| R6 | center_panel.py:318; orchestrator_panel.py:61-65 | `has_agent` is derived from `terminal.emulator is not None` | Comes from the registry. Since PR 4 it reads `session.running`. Since PR 5 the app asks `Engine.has_agent` |
| R7 | agent_providers.py:254-267 (`env_exports`) | Agents inherit the env **of the process that spawns them** | The spawning client sends its env with `spawn_agent`; the engine uses that instead of its own `os.environ` (D5). Done in PR 5: `spawn_agent(env=…)` / `open_shell(env=…)`; the TUI passes its env, IPC passes `params.env` when given (the MCP server sends none yet) |
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
- **As landed (PR 6):**
  - `attach_stream` answers `mode: "tail"` when `from_offset` is still in the ring **and** at most 512 KiB behind (half the per-stream queue); otherwise `mode: "snapshot"` with the ANSI in the JSON result (`snapshot`, a string). `offset` in the result is where the output frames that follow start; the client renders the snapshot into an empty terminal of `size` and then feeds the frames.
  - The snapshot (`engine/screen.py` `ansi_snapshot`) is: RIS, the last 2,000 scrollback lines with SGR (scrolled into the receiver's history), DECSET 1049 when the child is on the alternate screen, every row at its absolute position, DECSTBM margins, every private mode pyte recorded (2004, 1000/1002/1006, 1 = application cursor keys, …) plus IRM/LNM, `?7l` / `?25l` when autowrap is off / the cursor is hidden, the cursor position (relative under DECOM) and the current pen. Unit tests rebuild each case in a clean pyte and compare every cell, the scrollback, the cursor, the modes and the margins.
  - The snapshot offset excludes bytes the UTF-8 decoder still holds (a character split across chunks); those bytes are sent as the first output frame. Known limit: a chunk that ends inside an escape sequence leaves pyte's parser mid-sequence, which the snapshot cannot express, so the receiver may draw the tail of that sequence as text. It is rare (the child writes whole sequences per write in practice) and fixes itself on the next redraw.
  - Output frames are at most 64 KiB; offsets are contiguous per stream (the reference client records gaps in `Stream.gaps`).
  - New per-connection event **`stream.closed {stream_id, session_id, offset, exit_code?}`**: the PTY is gone (exited or stopped) and the stream is detached. Shells otherwise have no exit event (Q4).

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
- **As landed (PR 6):**
  - The server keeps one owner per session: the v1 connection that last sent `claim_size` (optionally with `rows`/`cols`, claim + resize in one step) or **any** input frame on one of its streams. `resize` from anyone else is `NOT_SIZE_OWNER`, including when nobody owns it yet: a client claims first.
  - The in-process TUI is not a v1 client and keeps resizing the session directly. A direct resize counts as the TUI claiming the size: the v1 owner is cleared and has to claim again. The TUI claims when its view's size really changes (today's guard, so a worktree switch never SIGWINCHes) and when the user types or pastes into a view whose size differs from the PTY's (it resizes the PTY back first). Until PR 7 sizes the TUI's display at the PTY size, a TUI view shows a smaller PTY padded and a bigger one cropped, which is what the manual test saw.
  - An owner that disconnects leaves the size as it is, with no owner.
  - `stream.resized` and `stream.reset` carry `session_id` too and go to every stream attached to the session, on its own connection.
- **As landed (PR 7), remote TUI:**
  - Each terminal is a `RemoteSession` (`backend/remote_session.py`) with its own `ScrollbackScreen`, fed by the stream. Its size is the PTY's: the attach reply and each `stream.resized` resize it where it falls in the byte stream (output frames and events share one ordered queue per connection). The widget renders it exactly as it renders an engine session, so a wider PTY is cropped and a narrower one padded.
  - The widget asks for a size with `claim_size {rows, cols}` (claim + resize) when its own size really changes (the PR 4 guard, so a worktree switch never claims) and before a key or paste when the PTY's size differs; the screen follows only when the engine reports the change. A hidden view has no stream and never claims.
  - The stream is attached while a view is: the widget attaches on Show and detaches on Hide (in-process too, where it changes nothing). The first attach renders the snapshot; a re-attach asks `from_offset` = the last byte fed (tail when close enough, else a new snapshot). A frame past that offset (a gap) or `stream.reset` re-attaches the same way; `stream.closed` ends the session. Frames that arrive with the attach reply, before it is applied, are buffered.
  - Keys, pastes and SGR mouse reports are encoded in the client as before and sent as input frames; typed before the stream is (re)attached they wait (up to 64 KiB).

---

## 4. Protocol draft (v1)

### 4a. Transport and framing

| Transport | Used by | Framing |
|---|---|---|
| Unix socket `engine.sock` (0600, dir 0700 as today, ipc.py:735-753). **Done in PR 6:** `engine/server.py`, in-process next to the legacy socket | TUI, MCP (later), CLI, the H2 host | `u32 length` + `u8 kind` + payload. `kind=1` is JSON control, `kind=2` is a binary stream frame |
| WebSocket `ws://127.0.0.1:<port>/v1` | Web UI, Tauri | Text frames = JSON control; binary frames = stream frames |
| **Legacy** `ipc.sock`, NDJSON (ipc.py:6-10) | Existing `mcp_server.py` and running agents | Unchanged until the MCP moves to v1; kept for at least one release afterwards |

Binary stream frame payload: `u8 type` (1 = output, 2 = input) · `u32 stream_id` · `u64 offset` (output only) · bytes.

- All integers are big-endian. The `u32 length` counts the payload only, not the 5-byte header. Frames over 16 MiB are rejected.
- `offset` is the position of the frame's first data byte in the session's output since the session started.
- A bad header (unknown `kind`, oversized length) is unrecoverable and closes the connection. A bad payload inside a well-delimited frame (invalid JSON, short stream header) is reported and skipped.
  - As landed: a bad payload, a message that is not a request, an output frame from a client or input for an unknown stream is answered with an error response whose `id` is `""` (`PROTOCOL_ERROR`); the connection carries on. The reference client keeps those in `Client.errors`.

### 4b. Envelope

- **Request:** `{"type":"req","id":"…","method":"…","params":{…}}`. This is the IPC shape plus `type`.
- **Response:** `{"type":"res","id":"…","result":…}` or `{"type":"res","id":"…","error":{"code","message","data?"}}`. Codes are the existing ones (ipc.py:148-161): `UNKNOWN_METHOD`, `WORKTREE_ERROR`, `VALIDATION_ERROR`, `TIMEOUT`, `INTERNAL_ERROR`. New: `UNAUTHORIZED`, `UNSUPPORTED_VERSION`, `NOT_SIZE_OWNER`, `NO_HOSTED_UI` (H1: a `ui.*` command with no hosted TUI attached), and `PROTOCOL_ERROR` (malformed envelope). Codes are an open set: a peer must handle one it does not know.
- **Event:** `{"type":"evt","seq":123,"event":"agent.status","data":{…}}`.
- **As landed (PR 6):** requests on one connection run concurrently (a `send_agent_input` waiting for its confirmation does not hold back the next request or the input frames); responses are matched by `id`. Only `hello` is handled in order, before anything else.

### 4c. Handshake and versioning

- The first frame is an ordinary request with method `hello` and params `{protocol: 1, client: "tui"|"web"|"mcp"|"cli", client_version, token?}`. The result is `{protocol: 1, engine_version, capabilities: [...], repo_root, engine_id}`.
- **Major version** is an integer; a mismatch returns an `UNSUPPORTED_VERSION` error response with `data: {supported: [1], requested}`. The client checks the reply's `protocol` the same way.
- **As landed (PR 6):** any request before `hello` is a `PROTOCOL_ERROR` (input frames too); a failed `hello` leaves the connection open so the client can retry. `engine_id` is random per engine instance, `engine_version` is the package version, and `capabilities` lists every method the server answers plus the features `events.replay`, `stream.snapshot`, `stream.tail`, `stream.input`, `stream.closed`, `size.latest`.
- **Additive changes** (new methods, fields or events) need no bump and are advertised in `capabilities`. Both sides **ignore unknown fields and events**.
- **JSON Schemas** are kept in `protocol/schema/v1/`: `envelope.json`, `handshake.json`, `methods.json` (`<method>.params` / `<method>.result`) and `events.json` (one definition per event). Contract tests validate both directions against them (§6). Python validates them with a small stdlib validator for the keyword subset they use (`protocol/validation.py`), so the package has no dependencies; a non-Python engine can use any JSON Schema validator.
- Schemas are lenient on the wire (unknown fields allowed). Tests validate in a strict mode that rejects undeclared fields, which is how the golden fixtures prove that every field the legacy server sends is described.
- **Unknown enum values must be tolerated by clients.** Value sets that grow over time (agent `status` / `status_before` / `previous_status`, `confidence`, `delivery`) are open: the schema declares them as `{"type": "string", "x-known-values": [...]}` instead of `enum`. On the wire any string is valid, and a client that meets a value it does not know treats it as opaque (shows it raw, does not crash, does not reject the message). Strict mode enforces `x-known-values`, so tests still catch a value the schema has not been told about. Other JSON Schema validators ignore the `x-` keyword as an annotation. Closed sets that only the caller chooses (`resume_mode`, `kind`, `severity`) stay `enum`.

### 4d. Methods

Done in PR 6: every row except `shutdown` (PR 8). The ten legacy methods are answered by `engine/methods.py` (`EngineMethods`), which both the legacy NDJSON server and the v1 server call, so their semantics cannot drift; the v1 server only adds fields. Params and results of every method are in `protocol/schema/v1/methods.json`.

| Method | Origin | Notes |
|---|---|---|
| `list_worktrees` | ipc.py:167 | unchanged |
| `create_worktree` | ipc.py:195 | Custom create always runs as an engine job; its output is streamed via `job.output` / `job.finished` events (D6). `remove_worktree` custom commands likewise |
| `remove_worktree` | ipc.py:302 | unchanged (keeps `git worktree remove`, Q1) |
| `spawn_agent` | ipc.py:335 | + `kind: "agent"\|"orchestrator"` (`worktree_path` is then optional and ignored), `system_prompt` composed by the engine, `env` from the calling client (D5; MCP forwards the requesting agent's env); returns `session_id` + `cursor` |
| `stop_agent` | ipc.py:403 | unchanged |
| `get_agent_status` | ipc.py:442 | unchanged |
| `read_agent_output` | ipc.py:470 | Reads the engine headless screen |
| `send_agent_input` | ipc.py:523 | unchanged semantics (framing, ack, delivery) |
| `list_agents` | ipc.py:651 | + `session_id`, `kind`, `size`, `attached_clients` (connections with a stream on it) |
| `list_events` | ipc.py:624 | unchanged (agent status events only, for MCP compatibility) |
| `subscribe {since?, events?}` / `unsubscribe` | new | Push events. `since` replays from the event log (the last 1,000 events of every kind, not only `agent.status`) right after the response; the result is `{cursor, replayed, truncated}`. `events` filters by name; a new `subscribe` replaces the previous one |
| `attach_stream {session_id, from_offset?}` / `detach_stream {stream_id}` | new | §3b. Returns `stream_id`, `session_id`, `size`, `mode`, `offset`, `snapshot?`. Works for agents, the orchestrator and shells |
| `resize {session_id, rows, cols}` / `claim_size {session_id, rows?, cols?}` | new | §3c. Both return `{session_id, rows, cols, owner}` |
| `open_shell {worktree_path, env?}` | new | Gets or creates the worktree `bash -l` session; returns its `session_id` and `size` |
| `refresh_worktrees` | new | Replaces `call_later(_load_worktrees)`; returns the new list (`WORKTREE_ERROR` when listing fails) |
| `refresh_git_status {paths?}` | new | Alt+G (app.py:742); returns `{statuses: {path: git_status}}` and also emits `git_status.updated` |
| `get_diff {worktree_path}` | new | app.py:355; capped as worktree_manager.py:48-50 |
| `get_pr_info {worktree_path, refresh?}` | new | Cached, deduplicated; fetches when nothing is cached yet or `refresh`. `fetched: false` means no answer (no `gh`) |
| `set_interest {worktree_paths}` | new | Scopes the git/PR pollers to what some client shows. Per connection, dropped on disconnect; the pollers use the union of every connection's set and the TUI's. Returns the client's set and the union |
| `get_config` | new | Read-only subset for modals (`ConfigView`) |
| `get_sidebar_state` | H1 | Everything a host sidebar needs in one call: `{worktrees, orchestrator: {agents}, pr_info: {path: pr_info\|null}, git_statuses, view, cursor}`. `worktrees` is the v1 `list_worktrees`; `pr_info` holds the worktrees fetched so far; `git_statuses` (PR 7) the full status of each worktree computed so far (the list only has the counts); `subscribe {since: cursor}` continues from it without a gap |
| `ui.get_view` | H1 | `{view}`: the last `ui.view_changed` payload, null until a UI announced one. Always offered |
| `ui.select_worktree {worktree_path}` / `ui.select_orchestrator` | H1 | Hosted UI shows that worktree (or the orchestrator) and focuses its active agent, else its shell. Unknown worktree → `VALIDATION_ERROR` |
| `ui.focus_agent {worktree_path?, orchestrator?, agent_id?}` | H1 | Selects the target (default: what the UI shows) and focuses the agent's tab; no `agent_id` = the active tab (Ctrl+J). No agent there, or an unknown id → `VALIDATION_ERROR` |
| `ui.open_shell {worktree_path?}` / `ui.show_diff {worktree_path?}` | H1 | Focus the worktree shell / the Diff tab (diff refreshed). The orchestrator has neither → `VALIDATION_ERROR` |
| `ui.open_modal {modal, worktree_path?, orchestrator?}` | H1 | `spawn` / `create` / `remove`, as the `s` / `c` / `d` keys. `remove` of the main worktree or one with an active agent, or any modal while one is open → `VALIDATION_ERROR` |
| `attach_ui` / `detach_ui` | PR 7 | The connection becomes (stops being) a hosted UI: what `Engine.attach_ui` does for an in-process TUI, for a hosted TUI on a remote engine. Dropped on disconnect. Not a `ui.*` command, so always offered |
| `publish_view {view}` | PR 7 | A UI announces what it shows (`ui.view_changed` unless unchanged; `{published}`): every TUI publishes its view, a remote one through this |
| `shutdown {stop_agents?}` | PR 8 | `lazyagent engine stop`. Stops a detached engine once the reply is sent: `{stopping: true, agents}` (agents stopped with it, orchestrator included), then `engine.shutdown {reason: "shutdown"}`. `stop_agents` defaults to true; false only stops an engine that runs no agent (`VALIDATION_ERROR` otherwise), because an agent cannot outlive its engine (its PTY closes). An engine inside a TUI answers `VALIDATION_ERROR` (quit that TUI instead) |
| `list_restorable` / `restore_agents {ids?, env?}` / `dismiss_restorable {ids?}` | PR 9 | The restart manifest (§6e): agents earlier engines ran when they ended; restore spawns them again, each resuming its conversation; `ids` default to all. `VALIDATION_ERROR` for an engine without a manifest. Since the MCP follow-up they are legacy methods too (`EngineMethods`, both sockets, golden fixtures), and the legacy `list_events` gains `restores` |

Input bytes are sent as **binary frames** (`type=2`), not as a JSON method, which avoids base64 for keystrokes.

- **As landed (H1):** the `ui.*` commands (all but `ui.get_view`) are validated by the server, then published as a `ui.command` event that the hosted TUI carries out in order; the result is `{seq, command, worktree_path, agent_id?, modal?}`, `seq` being that event's. They are fire-and-forget: the host learns the outcome from `ui.view_changed`. They are in `capabilities` only while a hosted UI is attached (`Engine.attach_ui`, which the TUI calls before starting the servers), and answer `NO_HOSTED_UI` otherwise. `worktree_path: null` in a result or event means the orchestrator (`create` always answers null).
- **As landed (H1, Q9):** the v1 `list_worktrees`, `refresh_worktrees` and `worktrees.changed` give each agent `session_id` and `kind`. The legacy NDJSON `list_worktrees` is unchanged (golden fixtures).
- **As landed (PR 7):** `create_worktree` takes `env` (the client's, for a custom command) and `remove_worktree` takes `custom` (run the configured remove command as a job, as the TUI does; default false, so MCP keeps `git worktree remove`, Q1) and `env`; its result carries `job_id` when a job ran. Both are additive, legacy socket included. The reference client gained `Client.send(method, **params)`, which puts a request on the wire at once and returns its future, so fire-and-forget requests keep their order.

### 4e. Events

| Event | Data | Replaces |
|---|---|---|
| `agent.status` | `AgentEvent.to_dict()` (agent_events.py:85-102) | `AgentStatusChanged` message |
| `agent.updated` | `{worktree_path, agent_id, status, confidence, detail}` | Sidebar refresh on every status/detail change (`agent.status` only has the transitions); added in PR 5 |
| `agent.spawned` | `{worktree_path, agent_id, session_id, label, session_name, kind}` | Tab mount in `spawn_agent` |
| `agent.exited` | `{worktree_path, agent_id, session_id, exit_code?, stopped?}` | `AgentExited` message. `stopped` (PR 5) tells a client stop from an exit, so only the latter gets the "exited" toast |
| `worktrees.changed` | full list (same shape as `list_worktrees`) | `_load_worktrees` → `set_worktrees` |
| `git_status.updated` | `{path: GitStatus}` | `_apply_git_statuses` |
| `pr_info.updated` | `{path, pr_info\|null}` | `_apply_pr_info` |
| `stream.resized` / `stream.reset` | `{stream_id, session_id, rows, cols, offset}` | — (reset = slow client dropped, so re-attach from `offset`) |
| `stream.closed` | `{stream_id, session_id, offset, exit_code?}` | — (PR 6: the session's PTY is gone) |
| `engine.notice` | `{severity, message}` | `self.notify` from engine paths |
| `job.started` / `job.output` / `job.finished` | `{job_id, title, command}` / `{job_id, text}` (UTF-8 decoded with replacement) / `{job_id, exit_code\|null}` | Custom create/remove typed into the shell (app.py:782, :838) (D6). `job.started` added in PR 5 so a client can label the output |
| `engine.shutdown` | `{reason}` | — |
| `engine.restored` | `{restored: [{id, worktree_path, agent_id, session_id, label, session_name, kind, resumed}], failed: [{id, reason, …}]}` | — (PR 9: what a `restore_agents` brought back) |
| `ui.command` | `{command, worktree_path\|null, agent_id?, modal?}` | H1: what a host asked the hosted UI to show (`command` is an open set; a UI ignores one it does not know) |
| `ui.view_changed` | `{worktree_path\|null, orchestrator, agent_id\|null, focus, modal\|null}` | H1: published by the TUI in every mode, on each change of selection, active tab, focus or modal. `agent_id` is the agent whose tab is active (`""` = the orchestrator's); `focus` is `agent` / `shell` / `diff` / `sidebar` / `modal` / `none` (open set); `modal` names it (`spawn`, `create`, `remove`, `confirm`, `help`). A view is announced once it has stayed put for 50 ms, and a host command's steps (select, mount, focus) are announced as one view at its end, so one host action is normally one event; an unchanged view is not re-sent |

- **Event log:** one engine-wide `seq`. `AgentEventBus` (agent_events.py:123) generalises to typed events; `list_events` keeps filtering to `agent.status` so the MCP cursors stay valid. Done in PR 5: `engine/events.py` `EventBus` (`emit`, `subscribe(callback, events)`, `history(cursor)`); a gap in `seq` between two `list_events` entries is another event, not truncation.
- **Backpressure:** each client has a bounded output queue (e.g. 1 MiB per stream). On overflow the engine drops that client's stream and sends `stream.reset`; the engine itself never blocks on a slow client.
- **As landed (PR 6):**
  - `stream.*` events are per connection, not in the event log: their `seq` is the engine's latest `seq` (not a new one) and `since` never replays them.
  - Per stream, the server counts the output bytes queued or not yet drained by the socket. Over 1 MiB the stream is detached, its unsent frames are discarded, and `stream.reset` says the offset of the first byte the client did not get, so it can re-attach with `from_offset` (tail if still close enough, snapshot otherwise). Control messages are never dropped; a connection with more than 16 MiB of them queued is closed.
  - v1 viewers do not count as watching: an attached v1 stream does not make the headless screen parse per chunk (K1); a snapshot flushes it first.
  - `Engine.stop` emits `engine.shutdown {reason: "stopped"}` on the bus, and the server flushes every connection (up to 0.5 s) before closing it.

### 4f. Auth and discovery

- **Runtime dir:** `$XDG_RUNTIME_DIR/lazyagent/<sha1(repo_root)[:12]>/` (falls back to `/tmp/lazyagent-<uid>/…`). It holds `engine.sock`, `ipc.sock`, `token` (0600, 32 random bytes), `engine.json` (pid, port, version) and `engine.lock` (flock = single instance per repo).
- **Unix socket:** filesystem permissions as today, plus an optional `SO_PEERCRED` uid check. A token is not needed.
- **As landed (PR 6):**
  - `$XDG_RUNTIME_DIR/lazyagent/<sha1(repo_root)[:12]>/` (else `$TMPDIR` or `/tmp` + `lazyagent-<uid>/<hash>/`). Both dirs are created 0700 and must be owned by us. It holds `engine.sock` (0600) and `engine.json` (`pid`, `version`, `protocol`, `socket`, `engine_id`, `repo_root`), written with temp file + `os.replace`. On stop the socket and `engine.json` are removed only if they are still this process's (socket inode, `pid`), then the repo dir and the per-user dir if empty. The legacy `ipc.sock` stays at `/tmp/lazyagent-<pid>/` (agents' MCP configs point there) and `token` waits for the web phase.
  - The `SO_PEERCRED` check is always on: a peer with another uid (or none) is closed before it can send anything.
  - Two lazyagent instances in one repo: the second finds a live `engine.sock` (a connect succeeds), leaves it and `engine.json` alone, runs without a v1 server and shows an `engine.notice` warning naming the other pid. A socket file nobody listens on is replaced. The real single-instance lock is PR 8.
- **As landed (PR 8):**
  - **Lock:** the engine that serves a repo (detached or inside a TUI) holds an `flock` on `engine.lock` (0600, holds its pid) in the runtime dir for as long as it runs; the descriptor is close-on-exec, so agents never inherit it. A second engine fails the lock, touches nothing, runs without v1 (a TUI) or exits 1 (headless), and its notice names the holder's pid. The holder unlinks the file before it drops the lock, and a new holder re-checks that the file it locked is still the one at the path, so a release racing an acquire never leaves two holders. The live-socket probe stays behind the lock, for an older lazyagent that holds none.
  - **Stale files:** a killed engine leaves `engine.sock`, `engine.json` and `engine.lock`; the lock died with it, so the next engine takes all three over. Discovery (`launcher.find`) counts a socket nobody listens on as no engine. On stop the dirs are removed once the lock file is gone.
  - **`engine.json`** gains `detached` (false: inside a TUI, stops with it) and `started_at` (Unix time).
  - **Log:** a detached engine's stdout/stderr go to `$XDG_STATE_HOME/lazyagent/engines/<hash>.log` (state dir, not runtime dir: it must survive a logout to explain a crash).
  - `LAZYAGENT_V1_SERVER=0` turns the server off (`Engine(v1_server=False)` in code); the legacy socket is unaffected.
- **Discovery (H1):** a host finds the socket either (a) from the OSC 7717 announcement the hosted TUI writes (§6b), or (b) from `engine.json` in the runtime dir, which is computable from `repo_root` (the exact `git rev-parse --show-toplevel` string): `lazyagent.engine.client.discover(repo_root)` returns the socket path, or None when there is no `engine.json`, it names another repo, or its socket is gone. The runtime-dir helpers live in `engine/runtime.py` (stdlib only).
- **Reference client:** `lazyagent.engine.client.Client` (stdlib asyncio; it imports only `lazyagent.protocol` and `engine/runtime.py`): `connect(path)` with the hello, `request(method, **params)` (raises `RequestError`), `call` (raw `Response`), `events()` / `next_event()`, `attach(session_id, on_output, from_offset=)` → `Stream` (registered before its first frame is read), `detach`, `send_input`. §5a placed it at `protocol/client.py`; it lives next to the server because the protocol package stays free of transports (and of the asyncio socket code a non-Python engine would not share).
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
| `list_events` :421 | `list_events` (+ `restores`: each `engine.restored` after `since`, with its `seq`, engine-wide; the key is absent when there was none, so older answers are unchanged) |
| `list_restorable` | `list_restorable` (MCP follow-up to PR 9) |
| `restore_agents` | `restore_agents` (no `env`: the engine's, as for MCP spawns, K4) |
| `dismiss_restorable` | `dismiss_restorable` |

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
    schema/v1/*.json     # contract schemas
  engine/                # NO Textual imports (enforced by test)
    engine.py            # Engine facade: start/stop, public API
    registry.py          # AgentRegistry, AgentIdStore, ORCHESTRATOR_KEY (from app.py:104, :218-248)
    session.py           # AgentSession/ShellSession: PtyEmulator + headless screen + observer + ring
    screen.py            # ScrollbackScreen (moved from widgets/scrollable_terminal.py:194-263) + ANSI snapshot
    worktrees.py         # WorktreeService: list/git/pr/diff caches + interest-scoped pollers (from app.py:139-158, :263-413)
    events.py            # generalised AgentEventBus
    scheduler.py
    methods.py           # the legacy method set, shared by both servers (PR 6)
    server.py            # v1 server (unix since PR 6; ws later)
    client.py            # reference asyncio client (unix since PR 6; ws later)
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
| `lazyagent [repo]` | TUI. Connects to the repo's engine, **auto-starting** it detached when none is running (tmux-like), so the unattended tmux boot keeps working unchanged (D4). `q` only detaches the screen; agents keep running. `--embedded` = in-process engine (old behaviour; migration fallback only). **As landed (PR 8):** still embedded by default; `--remote` (or `LAZYAGENT_ENGINE=remote`) gives the remote TUI with auto-start, `--embedded` forces the in-process one |
| `lazyagent engine {start,stop,status,logs} [repo]` | **Landed in PR 8.** `start [--foreground] [--timeout S]`: a detached engine unless one serves the repo (idempotent, prints pid, socket and log); `--foreground` runs it in the terminal. `stop [--if-idle] [--timeout S]`: v1 `shutdown`, then waits for the process to exit. `status [--json]`: pid, detached or inside a TUI, version, uptime, socket, log, worktrees and agents; exit 3 when none runs. `logs [-n N] [-f] [--path]`. None of them loads Textual; only `start --foreground` loads the engine |
| `lazyagent web [repo] [--port N] [--open]` | Ensures the engine with WS is up, prints `http://127.0.0.1:N/#token=…` |
| `lazyagent mcp` | Alias of `python -m lazyagent.mcp_server` (landed in PR 8) |
| `python -m lazyagent.engine [repo]` | **Landed in PR 7** (minimal, for testing): an engine with no TUI in the foreground until SIGTERM/SIGINT/SIGHUP, which stop it and its agents; exit 1 when it cannot serve the repo on a v1 socket (another engine does). `lazyagent.engine.headless.serve()` is the part `lazyagent engine` (PR 8) reuses |
| `lazyagent host [repo] [--port N] [--open]` | **Landed in H2** (experimental, §6b): runs the TUI in hosted mode on a PTY and serves it in a browser page next to a live sidebar |

- **Parsing:** argparse subparsers with backward-compatible positional parsing. If `argv[1]` is not a known subcommand it is treated as `repo`, so a directory literally named `engine` must be passed as `./engine`.
  - As landed (PR 8): the entry point is `lazyagent.cli:main` (`lazyagent.app:main` delegates to it). It dispatches `engine`, `host` and `mcp` before importing Textual; the subcommand comes first (`lazyagent engine stop [repo]`), the repo after it.

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
| 5 | **engine: Engine facade, IPC rewired** | `Engine.spawn/stop/create/remove/open_shell` absorb center_panel.py:365-444, orchestrator_panel.py:79-132 and the app actions. `ipc.py` handlers call `Engine` only (removes C4-C6). The TUI reacts to `agent.spawned/exited`. As landed: `engine/engine.py` (`Engine`), `engine/events.py` (one-`seq` `EventBus`), `engine/scheduler.py`; custom create/remove are engine jobs (D6) shown in a TUI `JobLog`; agents and shells take the client's env (D5) | Existing test_ipc and the golden cases against `Engine` without the Textual app (only the custom-create fixture changed: it gains `job_id`); tests/engine/test_engine.py with the fake agent (spawn/exit/stop, remove cleanup, jobs incl. failure, client env, one `seq`); **boundary test** (the whole engine and `lazyagent.ipc` import no Textual or widget) | 1 |
| 6 | **engine: v1 server (unix)** ✅ | `engine/server.py` speaking §4: hello, req/res, subscribe, attach_stream with snapshot, input frames, resize/claim. Runs in-process alongside the legacy socket. As landed: the legacy handlers moved to `engine/methods.py`, shared by both servers; `engine/screen.py` `ansi_snapshot`; `engine/client.py` reference client; the per-repo runtime dir with `engine.json`; the TUI view claims the PTY size when typed into; `LAZYAGENT_V1_SERVER=0` | **Contract suite** `tests/contract/` (black-box over the socket, parametrised by engine implementation × transport through a `Harness`): every method validated strictly against the schemas, event ordering and `since`, unknown events, snapshot + live has no gap (rendered in a clean pyte = `read_agent_output`), offsets, raw-tail and snapshot reattach, binary input, version mismatch, malformed frames, slow-client reset, size-owner rules, peer uid, live/dead socket, `engine.json`. Plus snapshot round-trip unit tests | 0/1 |
| 7 | **tui: RemoteBackend** ✅ | The TUI talks to the engine only through a `Backend` interface (InProcess / Remote). The terminal widget gets a display pyte fed by stream frames, sized to the PTY. Behind `LAZYAGENT_ENGINE=remote`. As landed: see §6c | TUI pilot tests run against both backends (parametrised fixture) | 1 |
| 8 | **cli: `lazyagent engine` + auto-start** ✅ | Subcommands, runtime dir, lock, detach (setsid), logs, discovery; legacy `ipc.sock` path kept. Default stays `--embedded` until soaked, then flips in a follow-up PR. As landed: see §6d | Subprocess tests: start, attach, quit TUI and the agent survives, reattach and the snapshot matches, `engine stop` | 1 |
| 9 | **engine: restart manifest** ✅ | Persist registry metadata (session names, worktree, provider) so that after an engine restart it can offer `resume_mode=last` re-spawns. The per-worktree id counters were already persisted in PR 2 (D8). As landed: see §6e | Unit tests | 1 |
| 10+ | **web** | WS transport + token + Origin; static serving; `web/` scaffold with sidebar, xterm.js terminal and status; `lazyagent web`; CI web and wheel jobs | Contract suite also over WS; Playwright smoke | 2 |
| … | Diff review with comments (`get_diff` structured + comment threads → `send_agent_input`), notifications (events → Notification API), prompt editor | per feature | 3 |
| … | Tauri wrapper | — | 4 |
| … | Rust engine | Must pass the **same contract suite** over the socket; that is what the suite is for | 5 |

- **Ordering:** PRs 2-5 are pure refactors (in-process) and can each ship in a release. PR 6 adds capability without a user-visible change. PR 7 is the first switchable behaviour, and PR 8 makes "agents survive the UI" real.

### 6c. PR 7 as built: the TUI behind a `Backend`

- **Interface** (`lazyagent/backend/__init__.py`): everything the app and its widgets use: state reads (worktrees, git statuses, PR info, the agent registry, config, `has_agent`…) are synchronous; commands that answer are coroutines (`spawn_agent`, `create/remove_worktree`, `get_diff`, `refresh_git_status`, `refresh_pr_info`, `session`, `open_shell`); fire-and-forget ones are plain calls (`stop_agent`, `set_interest`, `refresh_worktrees`, `publish_view`, `attach_ui`). Subscribers get `EngineEvent`s after the backend's state reflects them, plus `backend.connected {first, engine_changed}` / `backend.disconnected {reason}`. `sync()` returns once every event emitted so far has been applied (immediately in-process). Terminals are objects the views render and type into (`screen`, `rows`/`cols`, modes, `attach`, `write`, `resize`, …).
- **`InProcessBackend`** wraps the `Engine` (the default; terminals are the engine's sessions, one emulator). **`RemoteBackend`** (`LAZYAGENT_ENGINE=remote`; `LAZYAGENT_ENGINE_SOCKET`, else `discover(repo_root)`, the root computed as the engine does) is a v1 client: hello, `get_sidebar_state` + `get_config`, `subscribe {since: cursor}`, then a store the events keep current (an `AgentRegistry` as mirror, cleared in place because the widgets hold it). Errors come back as the in-process exceptions (`WORKTREE_ERROR` → `WorktreeManagerError`, anything else → `ValueError`; no connection → `EngineUnavailable`). `make_backend` picks; `LazyAgent(backend=…)` takes one; the app imports no `Engine` (a test runs the remote TUI against `python -m lazyagent.engine` and checks `lazyagent.engine.engine` is never loaded).
- **Connection loss:** the backend reports `backend.disconnected` once per outage (the app shows "Lost the engine …; reconnecting…" and sets the subtitle) and retries with backoff (0.25 s doubling to 5 s, re-discovering the socket). Back on the same engine it re-seeds, re-sends its interest, hosted-UI attach and view, and every visible stream resumes from its last byte (tail). A different `engine_id` (the engine came back as a new process, so its agents died with the old one, K3) drops the old sessions: the app removes agent tabs the engine no longer runs and reopens the worktree shells. Commands while disconnected fail with a toast; background refreshes are skipped.
- **Quitting:** with the remote backend `q` (and Ctrl+Q) only disconnect: the engine, its agents and shells keep running, and the next TUI gets an `agent.spawned` for each running agent when it connects, so it shows their tabs and attaches with a snapshot.
- **Engine without a TUI:** `python -m lazyagent.engine [repo]` (§5d). Auto-start, the lock and detach came in PR 8 (§6d).
- **Shell views:** opening a worktree's shell is a round trip, so the panel mounts its view when the answer comes; a focus asked for earlier (Ctrl+L, a host's select) lands then, and a host command waits for it before announcing its view.
- **Fixes found on the way:** two agents spawned in quick succession could end with the first agent's tab active and no focus: `Widget.focus()` is deferred in Textual, and the first agent's focus, queued behind the second tab, switched back to its own tab (the flaky hosted-TUI test); the deferred focus now applies only if that agent's tab is still shown. Also: `Engine.wait_idle` (and the worktree service's) spun forever when it caught a finished task before its done callback ran (Python 3.12 completes a gather of finished tasks without yielding); stopping the v1 server waited the full flush timeout for clients already gone; a new agent's tab took the keyboard even in a panel not on screen (an agent spawned over MCP in another worktree, in-process too).
- **Tests:** `backend_kind` runs 58 pilot tests against both backends (tests/tui_backends.py: the remote app is a real `RemoteBackend` over the socket to an engine on the same loop, started and stopped with the app, reachable as `app.backend.engine`): test_app_integration, test_multi_agent_panel, test_orchestrator_panel, test_hosted and the hosted-TUI contract tests. In-process only, on purpose: the widget unit tests of test_scrollable_terminal / test_terminal_input / test_monitored_terminal (they drive the view with an engine session; the `RemoteSession` side is in test_remote_backend) and the app tests that mock the `Backend` calls (backend-agnostic by construction). Remote only: tests/test_remote_backend.py (19: snapshot then live, re-attach on a gap, a reset and after falling behind the tail, no stream without a view, screen at the PTY size, exit, the store, a late client learning running agents, error mapping, no engine, the view cropping a wider PTY and typing taking the size back, a hidden terminal never claiming, losing the connection, an engine that comes back, agents outliving `q` and reappearing with a coherent snapshot, the shell taking the keyboard once its view is mounted, a client learning a "starting" agent) and tests/test_headless_engine.py (2, real processes).
- **Performance** (fake agents, 1 visible + 5 hidden each printing 200 kB/s, load generated inside the engine's process in both modes, 160x50 headless TUI, this laptop). "Before" is the engine feeding each hidden session's batch to pyte in one go every 0.25 s; "after" is the sliced feeder (below):

  | | in-process before | in-process after | remote before | remote after |
  |---|---|---|---|---|
  | Echo of a key in the visible agent, p50 / p95 / max | 5.4 / 6.3 / 7.4 ms (the probe misses the stalls) | 2.7 / 7.1 / 11.6 ms | 6.0 / 1,274 / 1,288 ms | 2.9 / 6.8 / 8.5 ms |
  | TUI loop stall, p95 / max | 11 ms / 1.14 s | 4.2 / 9.4 ms | 1.7 / 27 ms | 1.1 / 2.4 ms |
  | CPU, visible agent quiet (TUI + engine) | 84 % | 55 % | 42 % + 84 % | 4 % + 52 % |
  | CPU, the visible agent printing 200 kB/s too | 91 % | 62 % | 7 % + 83 % | 8 % + 53 % |

  - Before, the hidden agents' output (K1) held the engine loop for about a second per batch: in-process the whole TUI froze, remote about one keystroke in three echoed a second late.
  - **The feeder** (`engine/session.py`, `_Feeder`, one per loop): an unwatched session's batch (still coalesced for 0.25 s) is parsed in pyte calls of 1,024 characters, at most 4 ms per turn, sessions somebody is attached to first, then round robin; after a full 4 ms turn it rests 4 ms, so hidden output gets at most about half of the loop and keys, frames and requests wait at most one turn. A session it falls more than 1 MiB behind on keeps only the newest 256 KiB of its backlog, cut at a line: the status scan wants the latest screen and `read_agent_output` the last lines; the dropped part never reaches the scrollback (counted in `feed_dropped`; a full-screen program's next redraw repairs what a cut leaves). `flush()` (snapshot, `read_agent_output`, resize, a viewer starting to watch) still feeds everything at once. In this benchmark nothing was dropped: the feeder kept up at half duty (8 MB parsed, 0 dropped), and the old one-shot parse was the cost, not the parsing itself.
  - Double emulation is cheap at this rate: the TUI's display screen of a visible agent printing 200 kB/s costs about 4-8 % CPU; the engine's screen of that agent is fed through the feeder like a hidden one (a v1 viewer does not count as watching).
  - The remote TUI's CPU with a quiet visible agent was mostly the benchmark's own 0.5 ms echo-polling loop.

### 6d. PR 8 as built: `lazyagent engine` and auto-start

- **CLI** (§5d): `lazyagent.cli` is the entry point. `lazyagent engine {start,stop,status,logs} [repo]` (`engine/cli.py`) and `lazyagent mcp` never load Textual; `lazyagent [repo] --remote | --embedded` picks the TUI's backend, embedded staying the default until soaked.
- **Detach** (`engine/launcher.py`, stdlib only so a TUI can use it without loading the engine): `spawn` runs `python -m lazyagent.engine --detached --ready-fd N <repo>` with `start_new_session` (setsid: no controlling terminal, its own process group, so closing the tmux pane or terminal that started it does not reach it), `cwd /`, stdin `/dev/null`, stdout/stderr appended to the repo's log, and the starting client's env minus `LAZYAGENT_ENGINE*` / `LAZYAGENT_HOST`. A daemon thread reaps it if it exits while the starter lives (no zombie); no double fork, because the starter (a CLI that exits, or a TUI) never needs to outlive it.
- **Ready and discovery:** the engine writes `ready` on the inherited pipe once its v1 socket and `engine.json` are in place. `Launch.wait(timeout)` (default 10 s) reads it with `select`; an engine that exits first (it lost the lock to a concurrent start, or failed) closes the pipe, and the waiter then waits for whichever process holds `engine.lock` to serve, or raises `EngineStartError` with the exit code, the log's last lines and its path. `launcher.find(repo_root)` = `engine.json` for this repo + a socket that accepts a connection.
- **Log and crash log (K3):** the detached engine logs to the file its stderr points at through a `RotatingFileHandler` (2 MiB, one old file kept) that, on rotation, `dup2`s the new file onto fds 1 and 2: tracebacks, `faulthandler` dumps (enabled), uncaught exceptions (`sys.excepthook`) and event-loop errors all land inside the cap. The launcher also rotates a full log before a start. Each run logs its start (version, pid, python), what it serves, a requested shutdown, and its exit status and uptime.
- **Stop:** v1 `shutdown` (§4d) sets the headless runner's stop event after the reply is queued; `Engine.stop(reason)` then emits `engine.shutdown {reason: "shutdown"}` and flushes clients as before. `lazyagent engine stop` waits for the pid to go (15 s).
- **Auto-start (D4, Q16):** `make_backend` gives the `RemoteBackend` `autostart` unless `LAZYAGENT_ENGINE_SOCKET` is set or `LAZYAGENT_ENGINE_AUTOSTART=0`. The CLI calls `launcher.prestart` before it imports the app, so the engine boots while Textual loads, and hands the `Launch` to the backend. When the supervisor cannot connect it starts (or waits for) an engine once per outage and retries at once; on success it shows "Started an engine for this repo (pid N) …" when the engine it reached is the one it started. A failure keeps the PR 7 notice, with the reason and the log path.
- **Deliberate stop vs crash:** an engine that announced `engine.shutdown` (`engine stop`, SIGTERM) is not restarted by the TUIs on it: they say "it was stopped with lazyagent engine stop; start it again with: lazyagent engine start" and keep looking for one. An engine that vanished without a word (K3) re-arms auto-start: its agents are gone anyway, and the TUI comes back on a fresh engine (the PR 7 `engine_changed` path drops the old tabs).
- **`q` (Q15):** unchanged: only disconnects.
- **MCP / `ipc.sock`:** owned by the engine process, wherever it runs. A detached engine serves `/tmp/lazyagent-<engine pid>/ipc.sock`; its agents' `mcp-config.json` carry that path in `LAZYAGENT_SOCKET`, and their MCP servers' ancestor walk (mcp_server.py) finds the engine pid too, since agents are the engine's children. A remote TUI serves no `ipc.sock` (it has no engine). An MCP server outside lazyagent falls back to the newest live socket, as before. Tested: the cold-start test spawns over that socket, checks the agent's MCP config points at it, and calls it again after the TUI quit.
- **Performance** (this laptop, fake world, median of 8, from launching `lazyagent` on a PTY until the sidebar shows the worktree from the engine):

  | | engine ready | first frame with the worktree |
  |---|---|---|
  | cold, `--remote` (engine auto-started) | 0.21 s | **0.43 s** (0.41-0.45) |
  | same, without the CLI's prestart | 0.53 s | 0.60 s |
  | warm, `--remote` (engine running) | — | 0.40 s |
  | `--embedded` | 0.28 s | 0.34 s |

  `lazyagent engine start` takes 0.21 s and `stop` 0.16 s end to end. Starting the engine before importing Textual saves about 170 ms: the engine's boot (Python, the engine imports, `git worktree list`, both sockets) overlaps the TUI's imports.
- **Tests:** tests/test_engine_cli.py (real processes, a `claude` shim on PATH running the fake agent, each test's runtime dir as the marker of its processes; the fixture kills what is left and fails the test if anything was): start/status/stop/logs (own session, stdin `/dev/null`, stderr to the log, files and dir gone after stop), three concurrent starts → one engine, a SIGKILLed engine's stale socket/json/lock replaced, `stop --if-idle` refused with an agent and `stop` taking it down, an embedded TUI's engine refusing `stop` and blocking a second start, the K8 cold start + MCP, quit → agent keeps working → a second TUI reattaches with a snapshot equal to the engine's screen at the size the first TUI set, a failing auto-start keeping the notice, no restart after a deliberate stop. tests/engine/test_launcher.py (log cap with stderr following the rotation, start failure message, `find`), the lock and stale-file tests in tests/contract/test_discovery.py, and the `shutdown` contract tests in tests/contract/test_methods.py (the harness gained `detach()`).
- **Found on the way:** pyte's `resize` to fewer rows drops lines from the top, so a remote view that claims a smaller PTY loses what a program that does not repaint on SIGWINCH printed (real agents repaint). Left as is; the reattach test takes it into account.

### 6e. PR 9 as built: the restart manifest

- **What is kept** (`engine/manifest.py`): one JSON file per repo in the state dir, `$XDG_STATE_HOME/lazyagent/engines/<hash>.manifest.json`, next to the engine log (the runtime dir is wiped at logout, and a reboot is the case this is for). `agents` = what this engine runs; `restorable` = what earlier engines ran when they ended and nobody restored or dismissed yet. An entry: opaque `id`, `worktree_path`, `agent_id`, `kind` (agent / orchestrator), `provider`, `label`, `session_name`, `skip_permissions`, `conversation_id`, `instruction` (the last one sent, first 4,096 characters), `started_at`, `engine_id`, `ended_at`. Not the env: it can hold secrets, and a restore takes the caller's (D5).
- **When it changes:** on every spawn, exit, stop (an agent stopped or exited on its own is dropped: it is not offered back), `send_agent_input` (the instruction) and when an agent's conversation id shows up. Only the engine that holds `engine.lock` keeps a manifest, so a second lazyagent in the repo never overwrites it. `Engine.stop` stops the sessions without dropping them: whatever ran when the engine went away, crash or `engine stop` or `q` in an embedded TUI, is offered by the next engine.
- **Never on the loop:** a change marks the manifest dirty (about 0.6 µs). A debounced task (0.2 s) takes a snapshot on the loop (0.2 ms for 10 agents + 50 restorable, 0.5 ms for 100 + 50) and writes it from a worker thread: temp file, `fsync`, `os.replace` (3-5 ms, 51-128 KiB). At most one write is in flight, and a burst of changes becomes one write (the test spawns 200 and checks one write, off the loop thread, and under 50 ms of loop time for the burst). `Engine.stop` flushes. A corrupt file is moved to `….corrupt` and the engine starts without it.
- **Taking over:** at start, the old `agents` and `restorable` together become restorable (capped at the newest 50). Entries whose worktree is gone are pruned at start, when a worktree is removed, and before every list or restore.
- **Conversation id:** Claude's hook payloads carry its `session_id`. `ClaudeHooksObserver` keeps the main agent's (subagent events are ignored), the session exposes it, and the engine notes it on status changes and on each 1 s poll. A restore then resumes exactly that conversation (`--resume <id>`, the provider's pick flag plus the id: `AgentProvider.build_command(resume_conversation=…)`). Without one (another provider, or no hook seen yet) it falls back to `resume_mode=last` (`--continue` for Claude).
- **Restore:** `Engine.restore_agents(ids?, env)` spawns each entry again with its worktree, kind, provider, label and permissions, and the same agent id when it is free, so the session name (`--name <worktree>-<id>`), the MCP `agent_id` and the tab label stay the same. It emits `engine.restored {restored: [{id, …agent.spawned fields, resumed: "conversation" | "last"}], failed: [{id, reason}]}`; a failed entry stays restorable (for example an orchestrator that is already running). `dismiss_restorable` forgets entries.
- **v1** (§4d): `list_restorable`, `restore_agents {ids?, env?}`, `dismiss_restorable {ids?}`, with schemas and contract tests. The contract harness gained `restart()`, a new engine on the same manifest. An engine without a manifest answers `VALIDATION_ERROR`.
- **CLI:** `lazyagent engine restore [repo]` lists; `--all` or `--id ID` (repeatable) restores, from the caller's env; `--dismiss` forgets; `--json`. `status` shows how many agents are restorable.
- **Config:** `[engine] restore_on_start = true` restores everything at engine start (default false: offered only). The unattended boot (K8) can do either: set the option, or run `lazyagent engine start && lazyagent engine restore --all` before the planner, which then reads `engine.restored` or `list_restorable` instead of rebuilding state.
- **Tests:** tests/engine/test_manifest.py (11: file format, atomic 0600 write, corrupt file, unknown fields, take-over and pruning, cap, debounce off the loop thread, close flushes, take/prune, a failed write logged), tests/engine/test_restore.py (10, real PTYs: restart offers and restores with the same id, label and session name and `--resume <id>` / `--continue`, a crash leaves the last write, exited and stopped agents not offered, removed worktree pruned, failed restore kept, dismiss, id in use, `restore_on_start`, no manifest, only the lock holder keeps it), tests/contract/test_restore.py (5, across a restart), and in tests/test_engine_cli.py a real-process cycle: an agent on a detached engine, a hook event naming its conversation, SIGKILL of the engine, `engine start`, `status` showing 1 restorable, `restore --all` bringing back the fake agent with `--resume <id>` and the same `--name`, then a deliberate stop keeping it restorable and `--dismiss` forgetting it.

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

- **Host detection:** the app launches `lazyagent` with `LAZYAGENT_HOST=<host id>` (same idea as `TERM_PROGRAM`). Without it, the TUI is unchanged. With it, the TUI enters **hosted mode**: it hides what the host paints (sidebar, PR badges, status bar) and keeps the agent terminals.
- **Side channel:**

  | Channel | How | Pro | Con |
  |---|---|---|---|
  | **Socket (primary)** | The host connects to the v1 server (PR 6): `list_worktrees`, `subscribe` events, PR status | Already designed; rich data | Doesn't cross SSH without socket forwarding |
  | **OSC escapes (secondary)** | lazyagent emits private OSC sequences in its output; the host strips and handles them (like OSC 9 / OSC 133) | Works over SSH and inside tmux (needs `allow-passthrough on`) | Only for small payloads: notifications, focus hints |

- **H1 as landed:**
  - **Flag:** `LAZYAGENT_HOST` set and non-empty. Its value is opaque (it is echoed in the OSC payload as `host`), not a socket: the host gets the socket from the OSC or `discover`. Without it nothing changes, except that `ui.view_changed` is published in every mode.
  - **Compact layout:** hidden: sidebar (worktree list + PR bar), the per-worktree git info bar, header and footer. Kept: agent tabs, Diff tab, worktree shell, job log, toasts, and every modal (spawn, create, remove, confirm, help). The footer goes too: the host has its own chrome, the panel needs every row, and `?` still lists the keys. The worktree list stays in the DOM (`display: none`, not focusable) because selection still runs through it.
  - **Keys:** everything works as before; Ctrl+K has no sidebar to go to, so it leaves the terminal (no focus) and `q`, `s`, `x`, `c`, `d` reach the app (the help says so). Ctrl+Q quits from anywhere.
  - **Host → TUI:** the `ui.*` methods (§4d) → `ui.command` events → carried out by the TUI one at a time. A select focuses the active agent, else the shell. A command that arrives while a modal is open is dropped with a toast.
  - **TUI → host:** `ui.view_changed` (§4e) and `ui.get_view`.
  - **OSC:** at start and after each terminal resize the hosted TUI writes `ESC ] 7717 ; lazyagent ; <json> BEL`, `<json>` = `{"v":1, "socket", "engine_id", "repo_root", "pid", "host"}` (ASCII JSON, so no ESC/BEL inside; `socket` and `engine_id` null without a v1 server). It goes through Textual's driver writer, between frames. Inside tmux (`$TMUX`) it is written again wrapped in `ESC P tmux; … ESC \` (needs `allow-passthrough on`). 7717 is unused by the terminals we know (xterm, VTE, kitty, iTerm2, WezTerm and Windows Terminal use 0-52, 99, 104-119, 133, 633, 777, 1337, 5113, 5522, 9001). `lazyagent.protocol.hosted` has `encode_osc` / `parse_osc` / `tmux_passthrough` for hosts written in Python.
  - **Sidebar data:** `get_sidebar_state` (§4d), then `subscribe {since: cursor}` for `worktrees.changed`, `agent.*`, `git_status.updated`, `pr_info.updated`, `ui.view_changed`. A host that wants PR info for every worktree adds them to its `set_interest`.
  - **Tests:** tests/test_hosted.py (pilot: layout both ways, Ctrl+K, OSC exact bytes and tmux wrap, view), tests/contract/test_ui.py (protocol level, stand-in UI), tests/contract/test_hosted_tui.py (the real TUI under `run_test` driven over its own v1 socket), tests/protocol/test_hosted.py (OSC format).
- **H2 as built** (experimental): `lazyagent host [repo] [--port N] [--open]`.
  - **Shape:** one Python process (`lazyagent/host/`, no Textual or pyte; a test checks it). It runs `python -m lazyagent [repo]` in the current dir on a PTY of its own, under `setsid --ctty` (so a PTY resize reaches the TUI as SIGWINCH and the host's Ctrl+C does not), with `LAZYAGENT_HOST=lazyagent-host-<pid>`, `TERM=xterm-256color` and `$TMUX` removed. It serves `127.0.0.1:<port>` (0 = a free port) and prints `http://127.0.0.1:<port>/#token=…`.

    ```
    browser ──HTTP: page, xterm.js──┐
            ──WS /ws ───────────────┤ lazyagent host ──PTY──► lazyagent (hosted TUI)
               binary  = PTY bytes  │      │                        │
               JSON    = v1 subset  │      └── engine.sock (one v1 connection per page) ◄┘
    ```
  - **Discovery:** the OSC 7717 announcement in the PTY output (scanned on the way, split chunks included), else `discover(repo_root)` every 0.5 s, for up to 30 s. Without a socket the page is a terminal only.
  - **Terminal:** PTY output goes to every page as binary frames; binary frames from a page are keystrokes. A page sends its xterm.js size (`resize`); the PTY takes the latest (D3). A page that connects late gets the DECSET/DECRST modes the TUI set (alternate screen, mouse, bracketed paste, cursor; never 2026) and a full repaint, forced by a one-column resize and back when its size equals the PTY's. A page more than 8 MiB behind is dropped (close 1013) and can reload.
  - **Sidebar:** each page opens its own v1 connection (`client: "web"`), so `subscribe` and its events are per page: `get_sidebar_state`, then `subscribe {since: cursor}` for `worktrees.changed`, `git_status.updated`, `pr_info.updated`, `agent.*`, `ui.view_changed`, `engine.notice`, `engine.shutdown`. It shows the orchestrator and each worktree (name, branch, dirty/ahead/behind, PR state and CI roll-up, agents with a coloured status) and highlights what `ui.view_changed` says the TUI shows. The host's own connection sets its interest to every worktree, so PR info covers all of them, and prints `engine.notice` on stderr.
  - **Interaction:** a worktree click is `ui.select_worktree`, an agent click `ui.focus_agent`, the orchestrator `ui.select_orchestrator`; the Spawn / New / Remove buttons are `ui.open_modal`, Shell and Diff `ui.open_shell` / `ui.show_diff` (they act on what the TUI shows), and the keyboard goes back to the terminal so the TUI's modal takes it. `agent.status` transitions to `waiting*` / `completed` / `failed` / `possibly_hanged`, an `agent.exited` nobody asked for, and `engine.notice` are toasts, and browser notifications once allowed while the page is not focused.
  - **Lifecycle:** `q` in the TUI or a dead PTY: every page gets `{type: "exit", code}` and a close 1001, and the host exits with the TUI's code (128+n for a signal). Ctrl+C, SIGTERM or SIGHUP to the host: Ctrl+Q to the TUI (its own quit, which stops its agents and shells), then SIGTERM, then SIGKILL after 5 s and 3 s. No relaunch: run `lazyagent host` again.
  - **Security (K6):** loopback bind only; every HTTP request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding), and the upgrade also `Origin: http://127.0.0.1:<port>` or `http://localhost:<port>` (cross-site WebSocket hijacking), else 403. The token is 32 random bytes (`secrets.token_urlsafe(32)`), read from the URL fragment (never sent to the server in a URL; the page keeps it in `sessionStorage` and clears the address bar) and sent as the first WebSocket message `{type: "auth", token}`, compared in constant time; anything else, or nothing within 10 s, closes with 1008 before any PTY byte flows. No CORS headers. Every response has `Content-Security-Policy: default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, COOP/CORP `same-origin` and `Cache-Control: no-store`. `style-src` allows inline styles because xterm.js writes its theme and cell metrics into `<style>` elements (it has no nonce option); scripts stay self-only. Static files are a fixed map built at start (no path from the request reaches the filesystem). A page never talks to `engine.sock`: the host forwards only `get_sidebar_state`, `list_worktrees`, `get_pr_info`, `get_diff`, `subscribe`, `unsubscribe`, `ui.get_view` and the `ui.*` commands, and answers anything else with `FORBIDDEN` (spawn, input, create/remove go through the TUI's modals). Messages over 1 MiB close the socket (1009).
  - **Stack (D7):** no new dependency. The WebSocket is a stdlib server side of RFC 6455 (`host/websocket.py`, about 350 lines with docstrings: handshake, fragmentation, ping/pong, closing handshake, size cap, the RFC's protocol errors), tested frame by frame. HTTP is the request head only, enough for a few static files. The page is plain HTML/JS/CSS with no build step (`host/static/`), and xterm.js 6.0.0 with addon-fit 0.11.0 is vendored as ES modules with its MIT licenses (`host/static/vendor/xterm/VENDOR.md` has the tarball hashes). Hatch ships the static files in the wheel and sdist because they are tracked files of the package; `lazyagent host` fails with a clear message if they are missing.
  - **Tests:** tests/host/test_websocket.py (handshake, masking, 7/16/64-bit lengths, fragments with interleaved ping, close codes, size caps, every protocol error), test_tui.py (a scripted child: own session, OSC and modes split across chunks, resize/SIGWINCH, redraw, the stop ladder), test_server.py (static files and headers, Host/Origin/token refusals, path traversal, the whitelist, resize validation, slow pages, exit), test_host_integration.py (the real TUI in a temporary repo, driven like app.js: discovery, `get_sidebar_state`, `ui.select_worktree` → `ui.view_changed`, keystrokes reaching the shell, resize reaching the PTY, the spawn modal, `q`, stop, a killed TUI, no process left with the host's `LAZYAGENT_HOST`), and test_browser.py (Playwright + headless Chromium: the same flow plus a fake agent's sidebar entry, toast and notification; skipped without Playwright, run with `uv run --with playwright pytest tests/host/test_browser.py`).
  - **Limits:** Linux only (D9); checked in Chrome (headless and the manual test), not in Firefox. One TUI per host; closing the page leaves the TUI running until the host stops. Phone/remote access would need a reverse proxy with its own auth, which is out of scope.
- **Sequence:** PRs 1-6 as in §6 → **H1** hosted-mode flag and compact layout in the TUI ✅ → **H2** minimal host app (xterm.js panel + widgets fed by `subscribe`) ✅ → replace TUI parts with native widgets as needed → resume PRs 7-9 when "agents survive the UI" is needed.

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
| K1 | The engine feeds pyte for **every** agent continuously; today hidden agents are coalesced to 0.25 s and the scan is gated on visibility (scrollable_terminal.py:54, :528) | Coalesce headless feeds at ≤4 Hz when unattached; detection already tolerates staleness. A Rust or native emulator is the Phase 5 lever. Since PR 4: `UNWATCHED_FEED_INTERVAL` = 0.25 s, per chunk only while a viewer watches; the cost matches the pre-PR 4 hidden-widget path. Since PR 7: the batches are parsed by a sliced feeder (4 ms turns, at most half the loop, backlog capped at 1 MiB → newest 256 KiB), so they never stall the loop (§6c) |
| K2 | Double emulation (engine + TUI display) once the engine is out of process | The TUI subscribes only to visible sessions. The in-process backend shares the screen object (PR 4). PR 7: a remote terminal streams only while its view is shown; measured cost in §6c |
| K3 | The engine crashing kills **all** agents (the PTY master closes, SIGHUP) | Keep the engine small and well tested; restart manifest (PR 9); crash log in the state dir. Since PR 8: a detached engine's log (`lazyagent engine logs`) gets uncaught exceptions, event-loop errors and `faulthandler` dumps, capped at 2 × 2 MiB; a remote TUI that loses an engine which said nothing starts a new one. Since PR 9: the agents it ran are restorable from the restart manifest, each into its own conversation (§6e) |
| K4 | Agents would inherit the **engine's** env (`env_exports`, agent_providers.py:254), and an auto-started engine would freeze `SSH_AUTH_SOCK`, `DISPLAY` and `PATH` from the first TUI | Resolved by D5: `spawn_agent` carries the client's env (PR 5: `Engine.spawn_agent(env=…)`, IPC `params.env`). Open detail: which env MCP-initiated spawns use (proposal: the MCP server's own env, which is the requesting agent's); until the MCP server sends one they get the engine's |
| K5 | Running agents hold `mcp-config.json` files that point at `sys.executable -m lazyagent.mcp_server` and a socket path (agent_providers.py:427-441) | Never move that module path; keep the legacy socket; an engine upgrade means restarting agents |
| K6 | Web exposure: a localhost WS that can spawn shells is reachable from any browser tab | Token plus strict Origin/Host checks (§4f), 127.0.0.1 only, no CORS |
| K7 | Snapshot fidelity is limited to pyte's model (no OSC 8 links, images, some modes) | Prefer the raw tail when it is available (§3b option C); snapshot only as a fallback |
| K8 | Unattended boot (tmux → `lazyagent` → `/gio-planner`) changes once the TUI auto-starts a detached engine | D4: the TUI auto-starts the engine, so the boot script needs no change. PR 8 must include a test of the cold-start path. **Done (PR 8):** `test_cold_start_auto_starts_the_engine_and_agents_outlive_the_tui` runs the real `lazyagent --remote` on a PTY with no engine, spawns an agent over the legacy socket as the orchestrator's MCP does, quits and checks agent and MCP survive. The boot script needs `--remote` (or `LAZYAGENT_ENGINE=remote`) until the default flips |
| K9 | Textual-coupled tests (test_app_integration, test_center_panel, test_multi_agent_panel) need fixtures for the backend | Parametrised backend fixture in PR 7: `backend_kind`, 58 pilot tests on both backends (§6c) |

### Open questions (PR 5)

Decided for PR 5 as below; open for review.

| # | Topic | PR 5 behaviour | Alternative |
|---|---|---|---|
| Q1 | Custom remove over IPC/MCP | `remove_worktree` from IPC keeps `git worktree remove` (`custom=False`), as before PR 5; only the TUI runs `[worktree] remove` | Run the custom command for every client (D6 read literally) |
| Q2 | Which jobs the TUI shows | The `JobLog` shows every job, MCP/orchestrator-started ones included (they were invisible before) | Only jobs this client started |
| Q3 | `JobLog` lifetime | Hidden 5 s after success, 30 s after a failure, or on click | Stays until dismissed with a key |
| Q4 | Shells in the event stream | No `agent.spawned` / `agent.exited` for `kind:"shell"` sessions (the schema allows it) | Announce shells too, for clients that list every session |

### Open questions (PR 6)

Decided for PR 6 as below; open for review.

| # | Topic | PR 6 behaviour | Alternative |
|---|---|---|---|
| Q5 | Who may `resize` when nobody owns the size | Nobody: `claim_size` first (or type) | The first `resize` claims implicitly |
| Q6 | The TUI as a size client | It claims by resizing (its real size changes) and by typing/pasting; it does not claim on focus or on becoming visible, so switching worktrees never SIGWINCHes a session a v1 client sized | Also claim on focus, which is closer to §3c's "visible and focused" but resizes on every tab switch while a GUI shares the session |
| Q7 | Snapshot transport | A JSON string in the `attach_stream` result | A binary frame type 3 (`snapshot`), which avoids JSON escaping of ESC (`\u001b`, 6 bytes each) for big screens |
| Q8 | Raw tail size | Up to 512 KiB behind; further back gets a snapshot even if the ring (4 MiB) still has it | Stream the whole ring tail through the queue in pieces |
| Q9 | `list_worktrees` agents | **Resolved in H1:** the v1 `list_worktrees` / `refresh_worktrees` / `worktrees.changed` agents gain `session_id` and `kind` (legacy NDJSON unchanged), and `get_sidebar_state` gives the H2 sidebar worktrees, agents, orchestrator, PR info and view in one call | Only `list_agents` with the v1 fields (the PR 6 behaviour) |
| Q10 | Second instance in one repo | **Resolved in PR 8:** `engine.lock` decides. A second TUI (embedded) runs without v1 and warns, naming the holder's pid; a second headless engine exits 1; `lazyagent engine start` reports the running one (exit 0); a remote TUI connects to it | Fail the start, or take over the socket |

### Open questions (H2)

Decided for H2 as below; open for review.

| # | Topic | H2 behaviour | Alternative |
|---|---|---|---|
| Q11 | Inline styles in the host page's CSP | `style-src 'self' 'unsafe-inline'`: xterm.js writes its theme into `<style>` elements and has no nonce option. Scripts stay self-only | xterm.js's canvas/WebGL renderer, or a nonce patch, to drop `'unsafe-inline'` |
| Q12 | PR polling scope | The host sets its interest to every worktree, so the sidebar has PR/CI for all of them; the batched GraphQL fetch (PR 3) keeps that to one `gh` call per poll | Only what the TUI shows, as without a host |
| Q13 | TUI exit | The host stops with it; no relaunch | A relaunch button on the page |

### Open questions (PR 7)

Decided for PR 7 as below; open for review.

| # | Topic | PR 7 behaviour | Alternative |
|---|---|---|---|
| Q14 | Agents already running when a TUI connects | The backend announces each as `agent.spawned`, so the TUI adds their tabs (without the focus) and a tab attaches when shown | A separate "known agents" list the app reconciles; or show none until selected |
| Q15 | `q` with the remote backend | Only disconnects; the agents keep running (D4). Ctrl+Q too; nothing stops the engine from the TUI | A "quit and stop the engine" key |
| Q16 | No engine for the repo | **Resolved in PR 8:** the remote TUI starts one, detached (the CLI starts it before loading Textual), and says so in a toast. When that fails it opens empty with "Lost the engine (could not start an engine … (log: …))" and keeps looking (no second start in the same outage). `LAZYAGENT_ENGINE_AUTOSTART=0`, or `LAZYAGENT_ENGINE_SOCKET`, turn auto-start off | Fail at start (the PR 7 notice + retry is what auto-start off gives) |
| Q17 | Agents with no status yet | **Resolved:** the v1 lists (`list_worktrees`, `list_agents`, `get_sidebar_state`, `worktrees.changed`) carry every agent the engine runs; one that has reported no status yet is `status: "starting"` (a known value of the open set). The legacy NDJSON answers are unchanged | — |
| Q18 | Snapshot after a long hidden spell | A re-attach further than 512 KiB behind gets a new snapshot (scrollback capped at 2,000 lines) and the display drops the older history | Page the history in with `fetch_scrollback` |

### Open questions (PR 8)

Decided for PR 8 as below; open for review.

| # | Topic | PR 8 behaviour | Alternative |
|---|---|---|---|
| Q19 | Who owns `ipc.sock` in remote mode | The engine process (`/tmp/lazyagent-<engine pid>/ipc.sock`), as it owns the agents; the remote TUI serves none | The TUI keeps one too and proxies to the engine (two sockets per repo, nothing gained) |
| Q20 | `engine stop --keep-agents` | Not possible: an agent's PTY belongs to the engine and closes with it. `shutdown {stop_agents: false}` / `--if-idle` stops only an engine with no agent | Hand the PTY masters to a surviving helper (that helper is an engine again) |
| Q21 | A TUI whose engine went away | Restart it only when it vanished without `engine.shutdown` (crash); a deliberate stop is respected and the TUI waits for a manual `engine start` | Exit the TUI with the engine (tmux's `kill-server`); or always restart |
| Q22 | Env of an auto-started engine | The first remote TUI's env (minus `LAZYAGENT_ENGINE*`, `LAZYAGENT_HOST`); agents spawned from a TUI still get that TUI's env (D5), MCP spawns the engine's (K4) | A minimal env (PATH, HOME, locale) for the engine |
| Q23 | Log cap | 2 MiB + one old file per repo; one log for every run (each run marks its start) | A file per run with a count cap; or journald via `systemd-run --user` |
| Q24 | Pane/terminal close | `setsid` is enough (no ctty, own group); no double fork, the starter reaps | Double fork so the engine is never the starter's child |

### Open questions (PR 9)

Decided for PR 9 as below; open for review.

| # | Topic | PR 9 behaviour | Alternative |
|---|---|---|---|
| Q25 | Restore automatically? | Offered only; `[engine] restore_on_start = true` or `lazyagent engine restore --all` brings them back | Always restore at start (the proposal's open question) |
| Q26 | Agents stopped by `engine stop` | Offered back like a crash's (an upgrade restart wants them) | Only offer what a crash left; `engine stop` dismisses |
| Q27 | Which conversation | The exact one from Claude's hooks (`--resume <id>`), else the provider's last in that directory (`--continue`), which two agents in one worktree would share | Always `--continue`, as the plan said |
| Q28 | Interrupted turn | Resumed as is; no "you were interrupted" prompt is typed (proposal 0009's risk) | Send a short note after the resume |
| Q29 | `engine.restored` for MCP | **Resolved (MCP follow-up):** MCP tools `list_restorable`, `restore_agents`, `dismiss_restorable` over the legacy socket, and the legacy `list_events` carries `restores` (each `engine.restored` after `since`, with its `seq`; absent when none) next to the `agent.status` events, so /gio-planner can restore and see the result over MCP | Leave restores to the CLI and v1 |
| Q30 | The TUI | No restore UI: the CLI, v1 and the config cover the boot | A toast with "restore" when a TUI connects to an engine with restorable agents |

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
