# Engine / screen split: design

- **Status:** draft for review. Design only, no code changes.
- **Base:** `develop` @ `4ca663d` (release 0.8.0). Baseline: `uv run pytest -q` gives **523 passed** (56 s).
- **All `file:line` refs** are under `src/lazyagent/` at that commit.

## TL;DR

- Today the **Textual widget tree is the runtime**. Agents live inside widgets (`WorktreePanel._agents`, center_panel.py:181), and status travels as Textual messages. IPC/MCP handlers reach into `query_one(CenterPanel)` and into the pyte buffers. The engine therefore has to be carved out of the widgets, not just out of `app.py`.
- **Proposed split:** the engine owns PTYs, a headless emulator (used for detection and `read_agent_output` only), the agent registry, the event log, the worktree/git/PR caches and pollers, config, and the servers. Clients own display emulation, layout, selection, modals and keys→bytes.
- **Protocol:** the existing `{id, method, params}` IPC envelope plus events, plus binary PTY frames that carry byte offsets. On attach the client gets a snapshot (ANSI) and then the live stream. Resize follows a single-owner policy: the most recently active client owns the PTY size.
- **Migration:** about 9 strangler PRs to `develop`. The engine is first extracted **in-process** (same loop, direct calls), then moved behind the protocol, then out of process. Each step keeps `lazyagent` working, and the legacy `ipc.sock` and the `lazyagent.mcp_server` module path stay valid throughout.

---

## 1. State inventory

### 1a. `LazyAgent` app (app.py)

| State / logic | Where | Goes to | Why |
|---|---|---|---|
| `repo_path`, `_repo_root` | app.py:99, :112, :180-186 | **Engine** | Defines the engine's identity: one engine per repo root |
| `worktrees: list[WorktreeInfo]` | app.py:100, :188 | **Engine** (cache) + client mirror | IPC `list_worktrees` reads it (ipc.py:172); clients render it from `worktrees.changed` |
| `_agent_states` (2-level registry) | app.py:104, :218-248 | **Engine** (`AgentRegistry`) + client mirror | This is the source of truth for MCP, the sidebar and events |
| `agent_events: AgentEventBus` | app.py:107; agent_events.py:123 | **Engine** | The cursor log behind `list_events` and `send_agent_input` confirmation (ipc.py:596) |
| `_git_statuses` cache | app.py:108, :332-340 | **Engine** | Shared by every client and by MCP `list_worktrees` (ipc.py:174) |
| `_selected_worktree`, `_orchestrator_selected` | app.py:109-110, :417-450 | **Client** | Pure view state. The engine only learns an *interest set* (see `set_interest`, §4) that it uses to scope polling |
| `_selection_debounce` timer | app.py:116, :284-310 | **Client** | UX throttling. Becomes a debounced `set_interest` / `refresh_git_status` call |
| `_config: Config` | app.py:111, :174-178 | **Engine**, with a read-only copy for clients | Spawn/create/remove need it (ipc.py:227, :373). Modals need `default_branch` and `has_custom_create` (app.py:759-760) through `get_config` |
| `_gh_available` | app.py:113, :390-394 | **Engine** | Belongs to the PR poller |
| `_ipc_server`, `_ipc_socket_path` | app.py:114-115, :167-172, :733-735 | **Engine** | It is the engine's server. `LAZYAGENT_SOCKET` is injected into agents (agent_providers.py:438-441) |
| Timer `check_hangs` 60 s | app.py:139, :512-525 | **Engine** | Agent liveness |
| Timer `poll_agents` 1 s | app.py:143, :501-510 | **Engine** | Hook-log tailing, which feeds status |
| Timer `git_status` 60 s (selected only) | app.py:149-152 | **Engine**, scoped to the union of clients' interest sets | Today it is tied to UI selection |
| Timer `diff` 30 s (selected only) | app.py:153-155 | **Engine** on request; the client polls while the diff tab is visible | Diff is large and only wanted when it is shown |
| Timer `pr_status` 60 s (selected only) | app.py:156-158 | **Engine**, scoped by interest | `gh` network calls should be deduplicated across clients |
| Worker `_refresh_git_statuses` (all) | app.py:263-282 | **Engine** (`asyncio.to_thread`) | Triggered by rescan/create/remove |
| Worker `_refresh_selected_git_status` | app.py:312-330 | **Engine** | Same as above, for one path |
| Worker `_refresh_selected_diff` | app.py:355-370 | **Engine** method `get_diff` | Request/response |
| Worker `_refresh_pr_status` | app.py:383-405 | **Engine**, pushed as `pr_info.updated` events | Not exclusive today, so concurrent `gh` calls are possible; the engine can deduplicate per path |
| `on_agent_status_changed` (registry update, label/session pull from panel, event publish) | app.py:454-482 | **Engine** | Pulling label and session from the panel (:461-468) goes away, because the engine sets them at spawn |
| `on_agent_exited` | app.py:484-497 | **Engine** (drop + event); **Client** (toast, tab cleanup) | Split by concern |
| `action_spawn_agent` / `_spawn_orchestrator_agent` | app.py:529-602 | **Engine** `spawn_agent` (+ `kind:"orchestrator"`); **Client** keeps the `SpawnModal` | Spawn logic is duplicated today in the IPC handler (ipc.py:365-390) |
| `compose_orchestrator_prompt` | app.py:586; orchestrator_prompt.py:178 | **Engine** | Needs config and repo root only |
| `action_stop_agent` / orchestrator stop | app.py:604-637 | **Engine** `stop_agent`; **Client** chooses the target | The active tab is client state |
| `action_create_worktree` / `_do_create_worktree` | app.py:751-792 | **Engine** `create_worktree`; **Client** modal | The custom-create path differs between UI (types into shell, :782) and IPC (subprocess, ipc.py:253). Unify on the engine subprocess and stream its output as a job (open question Q6) |
| `action_remove_worktree` / `_do_remove_worktree` | app.py:794-851 | **Engine**; **Client** confirm modal | The "agent running" guard is duplicated (app.py:803 vs ipc.py:320) |
| `_send_to_terminal` | app.py:853-868 | **Engine** `write_input` to the worktree shell session | Writes straight into a widget queue today |
| `action_refresh` / `_load_worktrees` | app.py:738, :180-199 | **Engine** `refresh_worktrees` → `worktrees.changed` | IPC calls it through `call_later` (ipc.py:268, :296, :331) |
| Focus/tab actions, keybindings, CSS, `compose` | app.py:51-95, :639-728 | **Client** | Pure UI |
| `diag` watchdog/log | app.py:127-138; diagnostics.py | **Both** (separate log files) | Each process needs its own loop watchdog |
| `main()` argparse | app.py:877-899 | **CLI** module, with subcommands | See §5 |

### 1b. State living in widgets (the real runtime)

| State / logic | Where | Goes to | Why |
|---|---|---|---|
| `WorktreePanel._agents / _labels / _session_names / _agent_counter` | center_panel.py:181-185, :320-322 | **Engine** `AgentRegistry` | Agent identity and ids. Ids `a1, a2…` are per-panel counters today (Q8) |
| Provider command + runtime context build | center_panel.py:394-420; orchestrator_panel.py:96-123 | **Engine** `spawn` | Hook temp dirs, MCP config and the session name are all engine concerns |
| Per-worktree shell `bash -l` | center_panel.py:209-230 | **Engine** session `kind:"shell"` (recommended, Q1) | Custom create/remove commands are typed into it (app.py:782, :838). It should survive the UI closing like the agents do |
| `OrchestratorPanel._agent_terminal`, `_session_name` | orchestrator_panel.py:35-132 | **Engine** (key `__orchestrator__`) | Same as for agents |
| `MonitoredTerminal` status/detail/`_last_output_time`/observer/scan timer | monitored_terminal.py:42-49, :88-205 | **Engine** `AgentSession` | Status detection |
| `ScrollableTerminal.emulator`, queues, `recv` loop | scrollable_terminal.py:327-333, :411-447, :485-604 | **Engine** (PTY I/O) | The process lifetime must not depend on a widget |
| Terminal modes `mouse_tracking`, `bracketed_paste` | scrollable_terminal.py:320-324, :606-631 | **Engine** (authoritative); clients derive their own | `send_input` framing for MCP needs them (:1108-1124) |
| `send_input` + child-ack + submit | scrollable_terminal.py:1126-1161 | **Engine** method (`send_agent_input`) | It is protocol semantics, not UI |
| `ScrollbackScreen` pyte screen + scrollback | scrollable_terminal.py:194-263, :345-346 | **Both**: engine headless (detection, `read_agent_output`, snapshot); client display emulator | See §3 |
| Hidden-feed coalescing | scrollable_terminal.py:54, :449-475, :582-599 | **Client** concern (subscribe only to visible streams); the engine feeds the headless screen on its own throttle | Today detection *depends on widget visibility* (:528) |
| Rendering, style cache, dirty rows, selection, clipboard, key table, mouse → PTY | scrollable_terminal.py:348-406, :681-1101, :1163-1426 | **Client** | Pure display. The mouse SGR encoding moves out of pty_emulator.py:189-202, so clients send raw bytes |
| `PtyEmulator` | pty_emulator.py (all) | **Engine** | Unchanged apart from emitting raw bytes as well as decoded text (§3) |
| `WorktreeList` agent/git roll-ups | worktree_list.py:160-238 | **Client** | It renders from events |
| `PrStatusBar`, `GitInfoBar` | pr_status_bar.py:29; center_panel.py:72-115 | **Client** | They render from events |

---

## 2. Coupling hotspots

### 2a. Engine logic calling Textual

| # | Where | Coupling | Decoupling move |
|---|---|---|---|
| C1 | monitored_terminal.py:77-86, :155; messages.py | Status/exit is sent through `post_message(AgentStatusChanged/AgentExited)`, so it only reaches `App` through the DOM bubble | `AgentSession` emits to a plain listener (`Callable[[AgentEvent], None]`); the TUI adapter re-posts it as a Textual message |
| C2 | app.py:139-158 | `set_interval` drives the engine timers | Add an `engine/scheduler.py` with plain asyncio tasks, started and stopped by `Engine.start/stop` |
| C3 | app.py:263, :312, :355, :383 | `@work(thread=True)` + `call_from_thread` | Use `asyncio.to_thread` inside the engine and publish the results as events; the client applies them on its own loop |
| C4 | ipc.py:268, :296, :331, :390, :434 | `self._app.call_later(...)` to hop to the Textual thread | Unnecessary: the IPC server already runs on the same loop. Call `Engine` methods directly |
| C5 | ipc.py:365-383, :414-429, :480-490, :535-543 | Handlers do `query_one(CenterPanel)` and `panel.spawn_agent / get_agent / cleanup_agent` | Handlers take an `Engine` and never import widgets |
| C6 | ipc.py:172-174, :227, :330 | Reads private `_app.worktrees`, `_git_statuses`, `_config`, `_drop_worktree_agents` | Use the engine's public API |
| C7 | center_panel.py:365-444; orchestrator_panel.py:79-132 | Process spawn is interleaved with `TabbedContent.add_pane`/`mount` | Split into `Engine.spawn_agent(...) → AgentInfo` and a TUI reaction to `agent.spawned` that mounts the tab |
| C8 | app.py:461-472 | The registry pulls `label`, `session_name` and `last_output_time` back from widgets | These become `AgentSession` fields |
| C9 | app.py:493, :172, :190 | Engine paths call `self.notify` | Publish an `engine.notice` event that clients show as a toast |
| C10 | scrollable_terminal.py:528 (`if self.size.height > 0`) | The pyte feed and screen scan run only while the widget is visible; hidden agents rely on the 0.25 s coalesced flush | Feed the headless screen in the engine regardless of clients |
| C11 | scrollable_terminal.py:1242-1273 | **PTY size = widget size.** A hidden widget never resizes | Resize ownership policy, §3c |

### 2b. Widgets / IPC reaching into engine state

| # | Where | Coupling | Move |
|---|---|---|---|
| R1 | ipc.py:491-512 | `read_agent_output` reads `terminal._screen` (pyte buffer + scrollback) | Engine headless screen: `AgentSession.tail_lines(n)` |
| R2 | monitored_terminal.py:105-112 | Detection joins `self._screen.buffer`, the *display* screen | Engine headless screen |
| R3 | ipc.py:544-575 | `send_agent_input` checks `terminal.emulator/send_queue` and calls widget `send_input` | Use `AgentSession.send_input` |
| R4 | app.py:866 | `terminal.send_queue.put_nowait(["stdin", …])` on the shell widget | Use `Engine.write_input(shell_session, bytes)` |
| R5 | app.py:194, :252, :339 | The app pushes `_agent_states` and `_git_statuses` dicts by reference into `WorktreeList` | The client store is built from events |
| R6 | center_panel.py:318; orchestrator_panel.py:61-65 | `has_agent` is derived from `terminal.emulator is not None` | Comes from the registry |
| R7 | agent_providers.py:254-267 (`env_exports`) | Agents inherit the env **of the process that spawns them** | The engine's env becomes the agents' env (risk K4) |
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
- **Raw bytes, not decoded text:** today `PtyEmulator` decodes UTF-8 before queueing (pty_emulator.py:178). The engine should keep the raw bytes for the ring and the clients (xterm.js wants `Uint8Array`) and decode separately for pyte. That avoids lossy `replace` decoding reaching clients.
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

### 4b. Envelope

- **Request:** `{"type":"req","id":"…","method":"…","params":{…}}`. This is the IPC shape plus `type`.
- **Response:** `{"type":"res","id":"…","result":…}` or `{"type":"res","id":"…","error":{"code","message","data?"}}`. Codes are the existing ones (ipc.py:148-161): `UNKNOWN_METHOD`, `WORKTREE_ERROR`, `VALIDATION_ERROR`, `TIMEOUT`, `INTERNAL_ERROR`. New: `UNAUTHORIZED`, `UNSUPPORTED_VERSION`, `NOT_SIZE_OWNER`.
- **Event:** `{"type":"evt","seq":123,"event":"agent.status","data":{…}}`.

### 4c. Handshake and versioning

- The first frame is `hello {protocol: 1, client: "tui"|"web"|"mcp"|"cli", client_version, token?}`. The reply is `{protocol: 1, engine_version, capabilities: [...], repo_root, engine_id}`.
- **Major version** is an integer; a mismatch returns `UNSUPPORTED_VERSION`.
- **Additive changes** (new methods, fields or events) need no bump and are advertised in `capabilities`. Both sides **ignore unknown fields and events**.
- **JSON Schemas** are kept in `protocol/schema/v1/`. Contract tests validate both directions against them (§6).

### 4d. Methods

| Method | Origin | Notes |
|---|---|---|
| `list_worktrees` | ipc.py:167 | unchanged |
| `create_worktree` | ipc.py:195 | Custom create always runs as an engine subprocess; output is streamed as a `job` (Q6) |
| `remove_worktree` | ipc.py:302 | unchanged |
| `spawn_agent` | ipc.py:335 | + `kind: "agent"\|"orchestrator"`, `system_prompt` composed by the engine; returns `session_id` + `cursor` |
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
  __init__.py            # keeps pyte_patch import (__init__.py:3)
  cli.py                 # entry point, subcommands
  protocol/              # NO Textual, NO pyte imports
    envelope.py          # req/res/evt dataclasses, error codes
    framing.py           # u32-len frames, stream frame pack/unpack
    client.py            # asyncio client (unix + ws)
    schema/v1/*.json     # contract schemas
  engine/                # NO Textual imports (enforced by test)
    engine.py            # Engine facade: start/stop, public API
    registry.py          # AgentRegistry (from app.py:104, :218-248)
    session.py           # AgentSession/ShellSession: PtyEmulator + headless screen + observer + ring
    screen.py            # ScrollbackScreen (moved from widgets/scrollable_terminal.py:194-263) + ANSI snapshot
    worktrees.py         # WorktreeService: list/git/pr/diff caches + interest-scoped pollers
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
- **Import boundary:** a test (or `ruff` banned-imports / `import-linter`) fails if `lazyagent.engine` or `lazyagent.protocol` imports `textual`.

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
| `lazyagent [repo]` | TUI. Connects to the repo's engine, **auto-starting** it detached when none is running (tmux-like). `--embedded` = in-process engine (old behaviour; the fallback during migration) |
| `lazyagent engine [repo] [--foreground] [status\|stop [--keep-agents]]` | Runs or manages the engine |
| `lazyagent web [repo] [--port N] [--open]` | Ensures the engine with WS is up, prints `http://127.0.0.1:N/#token=…` |
| `lazyagent mcp` | Alias of `python -m lazyagent.mcp_server` |

- **Parsing:** argparse subparsers with backward-compatible positional parsing. If `argv[1]` is not a known subcommand it is treated as `repo`, so a directory literally named `engine` must be passed as `./engine`.

### 5e. CI additions

- Trigger on `develop` too. Today ci.yml only runs on `main` push/PR, which is why stacked PRs to develop go untested.
- Job `contract`: protocol tests that start a real engine subprocess with a **fake agent provider** (a scripted PTY program that prints known ANSI and reacts to input) and drive it over the socket.
- Job `boundary`: the no-Textual-in-engine import check.
- Job `web` (Phase 2+): `npm ci`, typecheck, unit tests, and a Playwright smoke test against a real engine with the fake agent.
- Job `wheel`: build, then assert `web/static/index.html` is inside the wheel and `lazyagent --help` runs from a clean venv.
- Matrix: add `macos-latest` for engine/pty tests. The PTY and `/proc` ancestor walk (mcp_server.py:85) are Linux-only today.

---

## 6. Migration plan: strangler PRs to `develop`

Each PR keeps `lazyagent` behaving as today and the 523 tests passing (some move with the code).

| # | PR | Changes | Tests added | Phase |
|---|---|---|---|---|
| 1 | **protocol: envelope + schemas** | `lazyagent/protocol/` (envelope, framing, v1 JSON schemas for the 10 existing methods). No runtime use | Golden fixtures of today's IPC responses (taken from test_ipc.py cases), validated against the schemas; framing round-trip and fuzz | 0 |
| 2 | **engine: AgentRegistry + event log** | Move app.py:104-107, :218-248, :454-482 logic into `engine/registry.py`. The app delegates | Unit tests on the registry (status → event publish, rollup, drop → EXITED) | 1 |
| 3 | **engine: WorktreeService** | Move list/git/diff/PR caches and pollers (app.py:139-158, :263-413) to asyncio + `to_thread`, with interest scoping. The app subscribes and applies on its loop | Service tests with a fake `WorktreeManager`; the poller respects the interest set | 1 |
| 4 | **engine: AgentSession** | `PtyEmulator` + observer + scan/hang logic + modes + `send_input` + ring buffer in `engine/session.py`. `MonitoredTerminal` becomes a *view* that renders from the session's screen (shared in-process, so there is no double parse yet). Fixes C1, C10, R1-R3 | Session tests with the fake-agent PTY: status transitions, send_input framing/ack, `tail_lines`, ring offsets | 1 |
| 5 | **engine: Engine facade, IPC rewired** | `Engine.spawn/stop/create/remove/open_shell` absorb center_panel.py:365-444, orchestrator_panel.py:79-132 and the app actions. `ipc.py` handlers call `Engine` only (removes C4-C6). The TUI reacts to `agent.spawned/exited` | Existing test_ipc against `Engine` without the Textual app; **boundary test** (engine imports no textual) | 1 |
| 6 | **engine: v1 server (unix)** | `engine/server.py` speaking §4: hello, req/res, subscribe, attach_stream with snapshot, input frames, resize/claim. Runs in-process alongside the legacy socket | **Contract suite** (black-box over the socket): every method, event ordering, snapshot + live has no gap, offsets, version mismatch, slow-client reset, size-owner rules | 0/1 |
| 7 | **tui: RemoteBackend** | The TUI talks to the engine only through a `Backend` interface (InProcess / Remote). The terminal widget gets a display pyte fed by stream frames, sized to the PTY. Behind `LAZYAGENT_ENGINE=remote` | TUI pilot tests run against both backends (parametrised fixture) | 1 |
| 8 | **cli: `lazyagent engine` + auto-start** | Subcommands, runtime dir, lock, detach (setsid), logs, discovery; legacy `ipc.sock` path kept. Default stays `--embedded` until soaked, then flips in a follow-up PR | Subprocess tests: start, attach, quit TUI and the agent survives, reattach and the snapshot matches, `engine stop` | 1 |
| 9 | **engine: restart manifest** (optional) | Persist registry metadata (session names, worktree, provider) so that after an engine restart it can offer `resume_mode=last` re-spawns | Unit tests | 1 |
| 10+ | **web** | WS transport + token + Origin; static serving; `web/` scaffold with sidebar, xterm.js terminal and status; `lazyagent web`; CI web and wheel jobs | Contract suite also over WS; Playwright smoke | 2 |
| … | Diff review with comments (`get_diff` structured + comment threads → `send_agent_input`), notifications (events → Notification API), prompt editor | per feature | 3 |
| … | Tauri wrapper | — | 4 |
| … | Rust engine | Must pass the **same contract suite** over the socket; that is what the suite is for | 5 |

- **Ordering:** PRs 2-5 are pure refactors (in-process) and can each ship in a release. PR 6 adds capability without a user-visible change. PR 7 is the first switchable behaviour, and PR 8 makes "agents survive the UI" real.

---

## 7. Risks and open questions

### Risks

| # | Risk | Mitigation |
|---|---|---|
| K1 | The engine feeds pyte for **every** agent continuously; today hidden agents are coalesced to 0.25 s and the scan is gated on visibility (scrollable_terminal.py:54, :528) | Coalesce headless feeds at ≤4 Hz when unattached; detection already tolerates staleness. A Rust or native emulator is the Phase 5 lever |
| K2 | Double emulation (engine + TUI display) once the engine is out of process | The TUI subscribes only to visible sessions. The in-process backend shares the screen object (PR 4) |
| K3 | The engine crashing kills **all** agents (the PTY master closes, SIGHUP) | Keep the engine small and well tested; restart manifest (PR 9); crash log in the state dir |
| K4 | Agents inherit the **engine's** env (`env_exports`, agent_providers.py:254). An auto-started engine freezes `SSH_AUTH_SOCK`, `DISPLAY` and `PATH` from the first TUI | Document it. Optionally let `spawn_agent` take `env` from the requesting client (Q5) |
| K5 | Running agents hold `mcp-config.json` files that point at `sys.executable -m lazyagent.mcp_server` and a socket path (agent_providers.py:427-441) | Never move that module path; keep the legacy socket; an engine upgrade means restarting agents |
| K6 | Web exposure: a localhost WS that can spawn shells is reachable from any browser tab | Token plus strict Origin/Host checks (§4f), 127.0.0.1 only, no CORS |
| K7 | Snapshot fidelity is limited to pyte's model (no OSC 8 links, images, some modes) | Prefer the raw tail when it is available (§3b option C); snapshot only as a fallback |
| K8 | Unattended boot (tmux → `lazyagent` → `/gio-planner`) changes once the TUI auto-starts a detached engine | Keep `--embedded`, or make the boot script start `lazyagent engine` first; decide before flipping the default (PR 8) |
| K9 | Textual-coupled tests (test_app_integration, test_center_panel, test_multi_agent_panel) need fixtures for the backend | Parametrised backend fixture in PR 7 |

### Open questions for Giovanni

1. **Shells:** should the per-worktree `bash -l` terminals be engine sessions (shared across clients and surviving UI close; recommended) or stay client-local?
2. **Engine scope:** one engine per repo (matches today; recommended for Phase 1) or one global engine for all repos (better for a single web UI)? The protocol carries `repo_root` either way.
3. **Resize policy:** is `latest` (the active client owns the PTY size) acceptable, or do you prefer `smallest`?
4. **Quit semantics:** should `q` in the TUI *detach* (agents keep running), with `lazyagent engine stop` as the way to kill everything? Should the TUI auto-start the engine, or should it be started explicitly or by a systemd user unit?
5. **Agent env:** should agents take their env from the engine (simple, can go stale) or from the client that requests the spawn?
6. **Custom create/remove commands:** today the UI types them into the shell (interactive, visible), while MCP runs a subprocess. Should both run as an engine job with streamed output, or should the UI keep the shell route?
7. **Web stack:** `aiohttp` vs `websockets` (+ static); React/Svelte/vanilla + xterm.js? Is Tauri still the plan, or is a browser tab enough?
8. **Agent ids:** today these are per-panel counters (`a1`, `a2`) that restart at 1. Should they be monotonic per worktree and persisted, so ids are not reused across engine restarts in the event log?
9. **macOS:** is it in scope? It affects the PTY and `/proc` code paths and the CI matrix.
10. **CI on `develop`:** OK to enable it now (independent of this project)?
