# Native desktop app: plan

- **Status:** plan (D0), 2026-10-18. Implementation follows phase by phase (§9); each phase gets an "As landed" note in §10.
- **Base:** `develop` @ `487f587` (U1-U5 and E1 merged).
- **Builds on:** [engine-split.md](engine-split.md) (protocol v1 §4, discovery §4f, auto-start §6d) and [ui-plan.md](ui-plan.md) (the web MVP U1-U5 and its budgets, §7d).
- **Decision being implemented:** Giovanni chose **Rust + `alacritty_terminal`, now**, over Tauri / Electron / GTK+VTE: a native app that talks to the engine directly over its Unix socket. No webview, no `lazyagent host` in between.
- **Measurements** come from throwaway spikes on this laptop (i7-12700H, 20 threads, Intel Alder Lake iGPU + NVIDIA dGPU, X11, Mesa 23 / lavapipe, Linux 6.8, Rust 1.99.1). Real-display runs are kept short and batched; everything that can run headless does (lavapipe).

## TL;DR

- **Stack:** `eframe`/`egui` 0.37 on `wgpu` + `winit` (X11, everything dlopen'ed at runtime), `alacritty_terminal` 0.26 for emulation, and **our own terminal renderer**: a glyph atlas rasterised with `ab_glyph` (pure Rust, no FreeType/fontconfig) drawn as one textured mesh per terminal through egui. It builds here with **no system -dev package**.
- **Why egui and not iced or raw wgpu:** the framework is not the cost. A full 200×50 terminal redraw costs **0.2-0.4 ms** of CPU with our mesh (egui's text layout: 1.7 ms); idle is **0.0-0.8 %** of a core; parsing runs at **~88 MB/s** (pyte 1.4 MB/s, xterm.js 42-52 MB/s). egui gives forms, lists, scroll areas and a headless test harness (`egui_kittest` + lavapipe) for free; raw wgpu would mean writing all of that.
- **Layout:** a cargo workspace in `desktop/`: `lazyagent-proto` (framing, envelope, typed messages, a blocking thread-based client, discovery, auto-start), `lazyagent-term` (alacritty wrapper: feed, render snapshot, keys → bytes, mouse, selection) and `lazyagent-desktop` (the app).
- **Engine:** the app is one more v1 client (`client: "desktop"`, an open set). It finds the engine like the remote TUI does (`engine.json` in the runtime dir) and auto-starts it through `python -m lazyagent engine start`, so the lock/log/race rules stay in one place. **No engine change is needed for the MVP.**
- **Launch:** `lazyagent desktop [repo]` (Python subcommand that finds and execs the binary with the right interpreter), or `lazyagent-desktop [repo]` directly; a `.desktop` file comes with the packaging phase.
- **Tests:** Rust unit tests; the Python golden fixtures and v1 schemas read from Rust tests (both clients stay in sync); integration tests against a real Python engine with the fake agent; headless render tests on lavapipe; a bench mode with the web budgets as the bar. CI gains a `desktop` job.
- **Phases:** D1 protocol client → D2 terminals + sidebar (+ manual pass) → D3 actions → D4 attention + PR/CI → D5 diff review → D6 packaging, polish, final manual pass.

---

## 1. Goals and non-goals

### 1a. Goals

| # | Goal | Measured by |
|---|---|---|
| G1 | **Parity with the web MVP (U1-U5)** for daily use | §2 parity table all ✅ by D5 |
| G2 | **Best possible performance**: beat every web budget (ui-plan §7d-1) the GUI side owns | §7 targets, measured per phase |
| G3 | **The engine stays the only runtime.** The app is a client; closing it leaves agents running | Integration test: quit app → agent alive → relaunch → screen restored |
| G4 | **Python keeps working:** `lazyagent`, `lazyagent host`, full pytest green; any engine/protocol change is additive and covered by Python tests | CI per merge |
| G5 | **Builds on a stock Ubuntu** without root (no -dev packages) | `cargo build` on this machine and on a bare CI runner |

### 1b. Non-goals (MVP)

- macOS / Windows (D9 of engine-split: Linux only).
- Grid view (U6), orchestrator board, phone, usage/history (U8-U11): later, same protocol.
- Replacing the TUI: it stays for SSH, tmux and the unattended boot.
- Font ligatures, colour emoji, images (sixel/kitty): not in the MVP (§8, R4).

---

## 2. Scope: parity with the web MVP

| Web piece (ui-plan §8a-landed) | Desktop | Phase |
|---|---|---|
| U1 sidebar: orchestrator row, worktrees (branch, dirty/ahead/behind, PR + CI badge), agents (status dot, label, detail) | Native egui side panel from `get_sidebar_state` + events | D2 |
| U1 top bar: repo, engine state (connected / reconnecting / gone), toasts, help sheet, theme | Top bar + toasts + `?` sheet from one keymap table; dark/light | D2 (bar, toasts) / D3 (help) |
| U2 native terminals per agent, the worktree shell as a bottom pane, tabs, size ownership (claim on focus/type), detach after hidden, reattach with tail | Terminal widget per session over `attach_stream` (snapshot / tail), input frames, `claim_size`, selection + copy, paste, mouse reports, scrollback | D2 |
| U2 reconnect with backoff, every visible terminal resuming from its last byte | Same (client supervisor thread) | D2 |
| U3 forms: Spawn (instruction, permissions, resume, label), New worktree (+ launch), Remove (guards, force), Stop (confirm), job drawer, refresh keys | Native dialogs with the same accelerators and guards; job drawer from `job.*` | D3 |
| Restore (TUI `R`, PR 9) | Banner "N agents can be brought back" → `restore_agents` | D3 |
| U4 attention queue (ranks 1-7), reply box, snooze/dismiss, PR/CI panel with linked checks, desktop notifications, title badge | Same, derived client-side; notifications via the freedesktop D-Bus API (`notify-send` fallback) | D4 |
| U5 diff viewer: base, unified/split, virtualised, per-line comments, submit review (now or when idle) | Same; comments kept in the app's state file per worktree | D5 |

---

## 3. Architecture

### 3a. Crates (`desktop/`, a cargo workspace)

```
desktop/
  Cargo.toml                 workspace, shared profile (release: lto = "thin", codegen-units = 1)
  Cargo.lock                 committed (an app, not a library)
  crates/
    proto/   lazyagent-proto     framing.rs envelope.rs types.rs (serde) client.rs discovery.rs launch.rs
    term/    lazyagent-term      terminal.rs (alacritty Term + vte Processor) input.rs (keys→bytes, mouse SGR) render.rs (cell snapshot)
    app/     lazyagent-desktop   main.rs app.rs state.rs (store) views/ (sidebar, terminal, dialogs, attention, diff) renderer/ (atlas, mesh) bench.rs
```

| Crate | Depends on | Why separate |
|---|---|---|
| `lazyagent-proto` | `serde`, `serde_json`, `sha1_smol` | No GUI: unit- and integration-testable in isolation; reusable by a future CLI or a Rust engine's contract tests |
| `lazyagent-term` | `alacritty_terminal` | Emulation and input encoding without a window: tested by feeding bytes and reading cells |
| `lazyagent-desktop` | both + `eframe`, `ab_glyph`, `fontdb` | The only crate that links a GPU stack |

### 3b. Threads and data flow

```
            ┌────────── engine.sock (u32 len · u8 kind · payload) ──────────┐
            │                                                               │
  reader thread  ── control JSON ──► channel ──► UI thread (egui) ── input frames ──► writer (Mutex<UnixStream>)
     │  output frames                               │  reads Arc<Mutex<Terminal>> per session
     └──► Terminal (alacritty) per stream ──────────┘  builds one mesh per visible terminal
            parse here, then ctx.request_repaint()
```

- **Parsing happens on the reader thread**, as alacritty does: the UI thread never parses bytes, it only reads the grid under a short lock and builds a mesh. A 5 MB burst cannot stall input handling.
- **Keystrokes are written synchronously** from the UI thread to the socket (one `write_all` of a ≤ 64 KiB input frame under a mutex): no extra hop between a key and the engine.
- **Repaint on demand only:** the reader asks for a repaint when a visible terminal changed or an event changed the store; an idle app renders nothing (egui's reactive mode).
- **No async runtime.** Two threads per connection (reader, request timeouts are per-call condvars) are simpler and cheaper than tokio for one socket.
- **Store:** plain Rust structs mirroring `get_sidebar_state` (worktrees, orchestrator, pr_info, git_statuses), updated by events in order. `seq`/cursor kept for `subscribe {since}` on reconnect.

### 3c. Terminal rendering

- **Emulator:** `alacritty_terminal::Term` + `vte::ansi::Processor`, one per attached session, scrollback 5,000 lines (the web uses 5,000; snapshots carry 2,000).
- **Renderer:** a glyph atlas (single-channel coverage stored in an egui texture, partial uploads for new glyphs) rasterised with `ab_glyph` at the exact pixel size; cells snapped to whole pixels. Per visible terminal, per frame: one background mesh (runs of equal colour merged) and one glyph mesh, ≈ 4 vertices per non-blank cell. Measured in the spike: **0.2-0.4 ms** for a full 200×50 screen of coloured text (egui's own text layout per row: 1.7-2.8 ms).
- **Fonts:** found with `fontdb` (pure Rust, scans the font dirs; no fontconfig): the configured family (default `DejaVu Sans Mono`), then a fallback chain for symbols agents print (`⏺ ✻ ⎿ ▐▛`: DejaVu Sans, Noto Sans Symbols 2, Noto Sans Mono), then egui's bundled Hack. Bold uses the bold face when found, else the regular one. Box-drawing and block characters fall through the same chain.
- **Damage:** a terminal's mesh is rebuilt only when its content generation changed (bumped by the reader thread) or the view scrolled; otherwise last frame's mesh `Arc` is reused.
- **Cursor, selection, wide chars, underline/strikeout/inverse/dim/hidden** are drawn from the cell flags; the cursor shape follows DECSCUSR.

### 3d. Engine discovery and auto-start

Same rules as the remote TUI (engine-split §4f, §6d):

1. **Repo root:** the `repo` argument resolved (canonical path), else `git rev-parse --show-toplevel` from the cwd: exactly what `resolve_repo_root` computes, because the runtime dir is named after that string.
2. **Socket:** `LAZYAGENT_ENGINE_SOCKET` when set; else `$XDG_RUNTIME_DIR/lazyagent/<sha1(root)[:12]>/engine.json` (fallback `$TMPDIR|/tmp/lazyagent-<uid>/…`) naming this repo, with a socket that accepts a connection.
3. **Auto-start** (unless `LAZYAGENT_ENGINE_AUTOSTART=0` or a socket was given): run `<python> -m lazyagent engine start <root>` in a background thread **while the window initialises**, then connect. `<python>` is `LAZYAGENT_PYTHON` (set by `lazyagent desktop`), else the `lazyagent` executable on PATH. Delegating keeps the lock, log rotation, ready pipe and race handling in one place (Python); the cost is one Python start (≈ 0.2 s) overlapped with the GPU init.
4. **Supervisor:** reconnect with backoff 0.25 s → 5 s; same engine (`engine_id`) → re-seed and resume every visible stream from its last byte (tail); a different engine → drop the old sessions. An engine that announced `engine.shutdown` is not restarted (overlay with a Start button), one that vanished is (as §6d).

### 3e. Launch

| Way | What it does |
|---|---|
| `lazyagent desktop [repo]` | New Python subcommand (no Textual import): resolves the repo root, finds the binary (`LAZYAGENT_DESKTOP_BIN`, then `lazyagent-desktop` on PATH, then `desktop/target/release/lazyagent-desktop` next to a source checkout) and `exec`s it with `LAZYAGENT_PYTHON=sys.executable`. Without a binary it prints how to build one |
| `lazyagent-desktop [repo]` | The binary itself (`cargo install --path desktop/crates/app`, or the release build) |
| `.desktop` file | `lazyagent desktop --install-desktop-entry` writes `~/.local/share/applications/lazyagent-desktop.desktop` (D6) |

- The binary is **not** shipped in the PyPI wheel for now (§8, Q1): building it needs a Rust toolchain, and a manylinux wheel with a GUI binary is its own project.

---

## 4. GUI framework choice

### 4a. Options

| Option | Build here w/o -dev pkgs | Terminal render cost | Widgets we would write | Test story | Verdict |
|---|---|---|---|---|---|
| **egui / eframe 0.37 (wgpu) + own mesh** | ✅ verified (3 min clean release build) | 0.2-0.4 ms per full 200×50 frame | Terminal only | `egui_kittest`: AccessKit queries + headless wgpu render (verified on lavapipe, no DISPLAY) | **Chosen** |
| egui with its own text layout per row | ✅ | 1.7-2.8 ms per frame | none | same | Rejected: 5× slower than the mesh |
| iced 0.14 (wgpu + cosmic-text) | expected ✅ (same winit/wgpu stack; not spiked) | same mesh/shader approach needed, through its `shader` widget | Terminal; virtualised lists | No headless renderer harness of the same maturity | Not chosen: Elm-style message plumbing for every form, and no gain where the time goes |
| raw winit + wgpu + glyphon | ✅ | lowest ceiling (≈ the mesh above minus egui's tessellation) | **Everything**: text inputs, scroll, lists, dialogs, focus | Hand-made | Rejected: weeks of widget work for < 0.2 ms per frame |
| GTK4 + VTE | ❌ needs libgtk-4-dev / libvte-dev | VTE | — | — | Excluded by the user's choice and by the machine |

- **iced was not spiked:** egui's measured costs leave nothing for another framework to win on the hot path (the terminal), and egui is the one with a headless test harness that already works here. If egui's immediate mode ever costs too much on big lists, the fix is virtualisation (`ScrollArea::show_rows`), which we need anyway.

### 4b. Spike numbers (egui, throwaway, 1700×900 window, 200×50 terminal, real GPU)

| | Vulkan (Intel) | GL (EGL, iris) |
|---|---|---|
| Idle, 5 s: CPU | 0.7-0.8 % | 0.0 % |
| Flood, 64 KiB of coloured agent-like output per frame (3.3 MB/s, parse + mesh + present at 60 fps): CPU | 15.8-22.9 % | 16.8 % |
| Frame interval during flood, p50 / p95 | 16.7 / 17.9-18.9 ms | 16.7 / 17.5 ms |
| Mesh build per frame, p50 / p95 | 0.20-0.36 / 0.43-0.54 ms | 0.23 / 0.44 ms |
| Memory idle: RSS / PSS / private dirty | 215 / 164 / 36 MB | 119 / 59 / 30 MB |
| Time to first `ui()` call (warm shader cache) | 0.29-0.30 s (2.2 s cold, first run ever) | similar |

- **`alacritty_terminal` throughput:** 50 MB of agent-like output (SGR colours, bold, 256-colour backgrounds, `⏺ ✻ ⎿`, clears every 40 lines, 10,000-line scrollback) parsed in 0.56-0.58 s: **87-89 MB/s** on one core. pyte does 1.4 MB/s on the same kind of input; xterm.js 42-52 MB/s.
- **Memory is mostly GPU drivers:** the Vulkan loader maps every installed ICD (NVIDIA, Intel, lavapipe, radeon) and LLVM; PSS counts them. The app's own private memory is ~30-36 MB idle. The web page's renderer was 388-396 MB RSS (ui-plan B9).
- **Present mode:** `Mailbox` is refused by this surface (validation error), so the latency plan uses `Fifo` with a frame latency of 1 (§7). The backend default (Vulkan vs GL) is decided in D2 by the latency bench; `WGPU_BACKEND` overrides it.

---

## 5. Protocol use

All of it exists in v1 today; the desktop app needs **no engine change for the MVP**.

| Need | Method / event |
|---|---|
| Connect | `hello {protocol: 1, client: "desktop", client_version}` |
| Sidebar | `get_sidebar_state`, `subscribe {since: cursor}`, `worktrees.changed`, `agent.*`, `git_status.updated`, `pr_info.updated`, `engine.notice`, `engine.shutdown` |
| Polling scope | `set_interest {worktree_paths}`: every worktree (as the host does, Q12), so PR badges cover all |
| Terminals | `attach_stream` / `detach_stream`, output + input frames, `claim_size`, `resize`, `stream.resized` / `stream.reset` / `stream.closed`, `open_shell` |
| Actions | `get_config`, `spawn_agent` (with the app's env, D5), `stop_agent`, `create_worktree`, `remove_worktree {custom: true}`, `refresh_worktrees`, `refresh_git_status`, `job.*` |
| Restore | `list_restorable`, `restore_agents`, `dismiss_restorable`, `engine.restored`, `restorable.changed` |
| Attention | `send_agent_input`, `read_agent_output`, `get_pr_info` |
| Diff | `get_diff {worktree_path, base, max_bytes}` |

- Unknown events and fields are ignored; unknown status values are shown raw (§4c of engine-split).
- **Env (D5):** the app passes its own environment to `spawn_agent` / `open_shell` / `create_worktree` / `remove_worktree` / `restore_agents`, as the TUI does. A `.desktop` launch gets the session's environment, which can lack the shell profile's PATH additions: the app logs the PATH it sends, and `lazyagent desktop` from a shell is the recommended launch until D6 adds a login-shell env probe (Q4).

---

## 6. Testing strategy

| Layer | What | Runs |
|---|---|---|
| Unit (proto) | Framing round trip + the Python framing test vectors; decoder errors (unknown kind, oversize, short stream header); envelope parse | `cargo test` |
| **Golden fixtures** | Every `tests/protocol/fixtures/ipc/*.json` response deserialises into the Rust types (no field silently lost for the types we model); every v1 method and event the client uses exists in `src/lazyagent/protocol/schema/v1/*.json`; the event names we handle are the schema's | `cargo test` (reads the Python repo's files by relative path) |
| Unit (term) | Feed bytes → cells/cursor/modes; the engine's ANSI snapshot (from a fixture captured from `ansi_snapshot`) → same screen as the engine's `read_agent_output`; keys → bytes per mode (app cursor, bracketed paste); mouse SGR | `cargo test` |
| **Integration (real engine)** | `python -m lazyagent.engine <scratch repo>` with a `claude` shim running `tests/engine/fake_agent.py` (as tests/test_engine_cli.py): hello, sidebar, spawn, attach + snapshot, type → echo, `size` after `claim_size`, `stream.closed` on stop, reconnect + tail, create/remove worktree, diff, auto-start through `lazyagent engine start`. Every process under a timeout and killed in `Drop` | `cargo test` when `LAZYAGENT_PYTHON` (default `../.venv/bin/python`) can import lazyagent; skipped with a message otherwise; **required in CI** |
| UI (headless) | `egui_kittest` with a fake store: sidebar rows, dialogs and their guards, attention ranks, diff parse + view, keyboard map. Render tests through wgpu on **lavapipe** (no display): terminal glyphs land in the right cells, cursor, selection, light/dark | `cargo test` |
| Perf | `lazyagent-desktop --bench <scenario>` against a real engine: key→frame latency, frames during a 5 MB burst, end-to-end throughput, idle CPU, attach, memory, cold start; JSON output. Headless parts (parse MB/s, mesh build) as `#[ignore]` tests | Local (real GPU), numbers in each "As landed" |
| Manual | Real app on a scratch repo with a real engine: screenshots (`--screenshot` flag), rough edges fixed | After D2 and at the end |

- **No windows on Giovanni's display from tests.** Everything automated is headless; real-display runs (benches, manual pass) are short, titled "lazyagent desktop bench (auto-closes)", batched, and run under `timeout -k`.

---

## 7. Performance targets (the web budgets are the bar)

| # | Metric | Web budget | Web as landed | Desktop target |
|---|---|---|---|---|
| B1 | Key → frame that paints the echo, idle (p50 / p95) | 8 / 16 ms | 8.4 / 8.9 ms | **≤ 5 / 8 ms** to the rendered frame |
| B2 | Same, with 8 chatty hidden agents (p95 / p99) | 25 / 50 | not re-measured | ≤ 12 / 25 ms |
| B3 | Engine echo over the socket from the client (p95) | 3 ms | 1.09 | ≤ 1.5 ms (no host hop) |
| B4 | Frames while the focused agent prints 5 MB: p95; UI-thread work per frame | 17 ms; long tasks ≤ 100 ms / 5 MB | 16.7-16.8; 63-64 ms | p95 ≤ 17 ms; UI thread ≤ 4 ms per frame, never > 16 |
| B5 | 5 MB end to end | ≥ 10 MB/s | 15-17 MB/s | ≥ 20 MB/s (the engine is then the limit) |
| B6 | Idle CPU, app + engine, 10 agents, 1 visible | ≤ 2 % | 0.3-0.5 % | ≤ 0.3 % |
| B8 | Attach + snapshot, p95 | 50 ms | 7.7-8.6 ms | ≤ 10 ms |
| B9 | Memory, 10 agents | ≤ 450 MB renderer | 388-396 MB | ≤ 60 MB private, ≤ 150 MB RSS (drivers included) |
| B10 | Cold start → interactive, engine running / not | ≤ 1.5 s | 0.4 s TUI warm | ≤ 0.5 s / ≤ 1.0 s |
| B12 | Sidebar with 100 worktrees, every row changed | 4 ms | 2.8 / 3.7-7.1 ms | ≤ 1 ms frame CPU |
| U5 | 5,000-line diff: first screen; scroll frames p95 | 100 ms; 17 ms | 29-63 ms; 16.85 | ≤ 20 ms; ≤ 17 ms with ≤ 2 ms CPU |

- B1 is measured inside the app: the key event timestamp → the end of the frame whose terminal contains the echoed byte (the web measured to the rAF that paints it, the same point). Scan-out adds up to one refresh in both.

---

## 8. Risks and open questions (each with a decision)

| # | Risk / question | Decision |
|---|---|---|
| R1 | Vulkan maps every ICD (NVIDIA, radeon, lavapipe…): 160+ MB PSS before we draw | Measure both backends in D2; default to the one with the better latency, document `WGPU_BACKEND=gl` / `vulkan`. Memory budget counts private memory separately from drivers |
| R2 | `Mailbox` is not offered here; `Fifo` adds up to a frame of latency | `Fifo` + `desired_maximum_frame_latency = 1`; render immediately on echo (no frame pacing on our side); measure B1 |
| R3 | No fontconfig: font lookup by family name through `fontdb` might miss user aliases (`monospace`) | Map `monospace` to a known list (DejaVu Sans Mono, Noto Sans Mono, Liberation Mono, FreeMono); a config key `[desktop] font = "…"` or a path; bundled Hack as the last resort |
| R4 | Colour emoji and complex scripts | Monochrome glyphs from the fallback chain (emoji as outline fonts when present, else tofu). Colour emoji later via `swash` (not needed by agents' UIs) |
| R5 | Wayland | The target session is X11. Build the `wayland` feature too if it links without dev packages (winit dlopens libwayland); otherwise XWayland. Decided in D2 |
| R6 | IME / dead keys | egui forwards IME commits; the terminal sends the committed text. Pre-edit drawn in D6 if time allows |
| R7 | Agents' env when launched from a `.desktop` file (no shell profile) | §5 Env: recommend `lazyagent desktop` from a shell; D6 adds an opt-in login-shell env probe (`$SHELL -lc env`) |
| R8 | Two clients with different sizes on one session (D3 "latest") | Same rule as the web: a terminal claims on focus/typing/resize while owning; renders at the PTY size and crops/pads otherwise |
| R9 | `alacritty_terminal` vs the engine's pyte snapshot (§3b of engine-split): fidelity limited to what pyte models | Accept; the snapshot is ANSI, alacritty parses it like any output. A golden snapshot test pins it |
| R10 | Engine version skew (the app newer than a detached engine) | `hello.capabilities` gates features (e.g. `get_diff` `base`); a missing method is a toast, not a crash |
| Q1 | Ship the binary on PyPI? | **No** for now: `cargo install` / release build + `lazyagent desktop` finding it. Revisit with a manylinux wheel or GitHub release artifacts |
| Q2 | A Rust job in CI? | **Yes** (D1): `cargo fmt --check`, `clippy -D warnings`, `cargo test` incl. the integration tests against the Python engine (uv) and lavapipe render tests (`mesa-vulkan-drivers`), with `Swatinem/rust-cache` |
| Q3 | Where do review comments and snoozes live (the web keeps them in localStorage)? | A per-repo state file `$XDG_STATE_HOME/lazyagent/desktop/<hash>.json` (app-only, like localStorage); engine-stored threads wait for U8/0008 |
| Q4 | Engine env for `.desktop` launches | R7 |
| Q5 | Theme | Dark (default) and light, the web's tokens; the terminal palette follows the theme |
| Q6 | Key map, and Ctrl+D (the web takes it globally, which hides EOF from the terminal; the UI agent's doubt 3) | The web's map (ui-plan §7a), one table that drives both the handling and the `?` sheet; inside a terminal only the global set is intercepted. **Ctrl+D is not global here:** in a focused terminal it is EOF; the diff is Ctrl+D outside a terminal and **Ctrl+Shift+D** everywhere |
| Q7 | Notifications | freedesktop D-Bus via `notify-rust` (pure Rust `zbus`), falling back to `notify-send`; only when the window is not focused |
| Q8 | Clipboard | egui-winit's clipboard (`arboard`, X11 via x11rb, pure Rust); primary selection on select (X11 habit) as a D6 nicety |

---

## 9. Phased plan

Each phase is its own branch from up-to-date `develop`, merged with `--no-ff` once `cargo test` and the full pytest are green.

| # | Branch | Scope | Acceptance | Performance |
|---|---|---|---|---|
| D0 | `desktop-plan` | This document | Merged | Spike numbers (§4b) |
| D1 | `desktop-d1-proto` | Workspace; `lazyagent-proto`: framing, envelope, typed messages, blocking client (requests with timeouts, events, stream frames, input), discovery, auto-start via `lazyagent engine start`; `.gitignore` `target/`; CI `desktop` job | Golden fixtures + schema names tests; integration tests vs a real engine: hello, sidebar, spawn, attach (snapshot), input → echo, claim_size, stop → `stream.closed`, auto-start | B3 echo from Rust p50/p95/p99 |
| D2 | `desktop-d2-terminals` | `lazyagent-term`; the app: window, top bar, sidebar, agent tabs + shell pane with native terminals (attach/snapshot/tail, input, claim/resize, scroll, selection, copy/paste, mouse), reconnect, toasts, theme; `lazyagent desktop`; `--bench`, `--screenshot`. **Manual pass** after it | Agent usable without any other UI; quit → relaunch restores the screen; headless render tests | B1, B4, B5, B6, B8, B9, B10 |
| D3 | `desktop-d3-actions` | Dialogs: Spawn, New worktree (+ launch), Remove (guards, force), Stop; job drawer; refresh keys; restore banner; `?` sheet | §4a flow end to end against the engine (integration) and in kittest | Dialog open ≤ 1 frame; no change to B1 |
| D4 | `desktop-d4-attention` | Attention queue (ranks 1-7, reply, snooze/dismiss, CI items), PR/CI panel, notifications, title badge | A fake agent going `waiting_for_user` is queued; the reply reaches it and clears it | Queue re-derive for 200 agents ≤ 2 ms |
| D5 | `desktop-d5-diff` | Diff viewer (base, unified/split, virtualised rows, comments, submit now / when idle) | Comments + review reach the fake agent | U5 row of §7 |
| D6 | `desktop-d6-polish` | `.desktop` entry, `cargo install` docs, env probe, font config, rough edges; **final manual pass**; README section | Launchable from the app menu; every §2 row ✅ | Full §7 table re-measured |

---

## 10. As landed

(One note per phase.)

### D1: protocol client (2026-10-18)

- **Workspace** `desktop/` (`Cargo.lock` committed, `rustfmt.toml` at 120 columns, release profile with thin LTO). Crates: `lazyagent-proto` and `lazyagent-testkit` (test support, not published). `target/` is ignored.
- **`lazyagent-proto`:**
  - `framing`: the Python framing byte for byte (test vectors taken from `lazyagent.protocol.framing`); the decoder fails for good on a bad header and skips a bad payload, as the Python one; consumed bytes are dropped only when they dominate the buffer (no memmove per frame).
  - `envelope`: req/res/evt; unknown fields ignored; error codes as strings (open set).
  - `types`: every method result and event the app uses; open sets (statuses, confidence, delivery, kinds) stay strings. `EngineEvent::parse` gives the known events a shape; an unknown event, or a known one with an unreadable payload, is `Other` and ignored.
  - `client`: a reader thread per connection; writes from the caller's thread under one mutex (a key is one `write_all`, no hop). Requests block (`request`/`call`, with a timeout) or call back on the reader thread (`request_async`); a request that cannot be sent still completes its callback. `attach_async` registers the sink under the stream id when the reader sees the response, before any output frame of that stream; `stream.resized` / `stream.reset` / `stream.closed` reach the sink on the reader thread in wire order, so a terminal can apply a resize exactly where it falls in the bytes. Pastes over 64 KiB go out as several input frames.
  - `discovery`: repo root, runtime dir (`sha1(root)[:12]`, `XDG_RUNTIME_DIR` else `TMPDIR`/`/tmp` + `lazyagent-<uid>`), `engine.json` naming this repo + a socket that accepts a connection, `LAZYAGENT_ENGINE_SOCKET`, `LAZYAGENT_ENGINE_AUTOSTART=0`. Auto-start runs `$LAZYAGENT_PYTHON -m lazyagent engine start <root>` (else `lazyagent` on PATH) without the client-only variables. Everything takes an explicit `Env` (process env plus overrides), so tests run isolated engines side by side.
- **Sync with Python** (`tests/golden.rs`): the 19 golden fixtures of the methods the app uses decode and re-encode **without losing a field** (a new field in a Python response fails the test until the Rust type has it); error fixtures decode; every method the app calls has params and result schemas; **every event in `events.json` is either handled or listed as deliberately ignored** (`ui.command`, `ui.view_changed`), so a new event forces a decision; the required fields of `attach_stream`, `claim_size`, `open_shell` and `get_config` results equal the schema's.
- **Integration** (`tests/engine.rs`, a real `python -m lazyagent.engine` per test in its own process group, the fake agent behind a `claude` shim, killed in `Drop`): hello + capabilities + sidebar + config; error responses; spawn → `agent.spawned` → attach (snapshot) → type → echo → `claim_size` → `stream.resized` → the agent sees the size → `resize` → stop → `stream.closed` + `agent.exited {stopped}` with no offset gap; detach + re-attach from the last byte is a `tail`; shell + create (`worktrees.changed`) + diff + remove; a dead engine fails pending requests; auto-start through `lazyagent engine start` (idempotent) then `shutdown`.
- **Found on the way:** typing before the fake agent prints its prompt loses the input to the PTY's line discipline (it is not in raw mode yet); the tests wait for the prompt, and the app will too only in the sense that real agents do the same (nothing to fix in the engine).
- **CI:** a `desktop` job: `uv sync`, stable Rust with clippy/rustfmt, `Swatinem/rust-cache`, `cargo fmt --check`, `clippy -D warnings`, `cargo test --locked` with `LAZYAGENT_REQUIRE_ENGINE=1` so the engine tests cannot silently skip.
- **Measured** (release, this laptop, 300 samples after 30 warm-up, 3 runs):

  | | p50 | p95 | p99 |
  |---|---|---|---|
  | B3 echo, Rust client → engine process → fake agent → back | 1.14-1.38 ms | 1.73-1.95 ms | 2.08-2.38 ms |
  | Same with the Python reference client (separate engine process, same session) | 1.08-1.17 | 1.74-1.93 | 2.02-2.35 |
  | `tests/perf/test_echo.py` (engine inside the test's process: no process hop) | 1.03 | 1.39-1.40 | 1.49-1.77 |

  The client adds nothing measurable: the round trip is the engine's (budget B3 p95 ≤ 3 ms; target ≤ 1.5 ms is met at p50 and missed by the engine hop at p95, which no client can remove). Cold engine start through `lazyagent engine start`: **0.20-0.23 s**.

### D2: native terminals and the sidebar (2026-10-18)

![dark](desktop-screenshots/d2-dark.png)

- **`lazyagent-term`:** `Terminal` (alacritty `Term` + vte `Processor`): feed, resize, reset, a scrolled view, selection (simple / word / line), the program's requests (DSR and DA replies, title, OSC 52, bell) and a `Frame` of resolved cells (colour, bold, italic, underline kinds, strike, wide, selected) that the renderer reads without knowing alacritty. `palette`: the web page's colours (Tango ANSI as xterm.js, theme fg/bg, selection blended over the background), bold brightens colours 0-7 as xterm.js does. `input`: keys (DECCKM, xterm modifier parameters, Shift+Enter = ESC CR for an agent's newline), pastes (bracketed, end marker stripped), mouse reports (X10 and SGR, press/release/drag/motion/wheel).
- **The app** (`lazyagent-desktop`):
  - `conn`: a supervisor thread finds the engine or starts it (`lazyagent engine start`), says hello, seeds from `get_sidebar_state` + `get_config`, then `subscribe {since: cursor}`; on loss it retries 0.25 → 5 s; a new `engine_id` drops the dead panes; an `engine.shutdown` (deliberate stop) is not restarted until the user clicks **Start the engine** in the banner.
  - `panes`: one per session; the stream's sink runs on the reader thread (parse there, UI only draws). The attach callback applies the snapshot before any frame of the stream (since E1 the frames carry the engine's unparsed backlog: the first version of the snapshot test failed exactly there). A gap or `stream.reset` re-attaches from the last byte. Shown panes attach, the three most recently hidden stay attached, the rest detach (P7). The PTY size is claimed on focus and on a box change while focused; typing with another size claims first and the keys wait for the answer. Only the focused pane answers the program's queries.
  - `render`: glyph atlas (1024², rasterised on demand at the exact pixel size, uploaded in pieces), a background mesh (equal-colour runs merged, decorations, cursor) and a glyph mesh per terminal, rebuilt only when the terminal, the atlas, the origin or the focus changed. **Box drawing and block elements are drawn as rectangles** (a table generated from the Unicode names: light, heavy, double; rounded corners square) so lines join and Claude Code's `▐▛███▜▌` fills its cells; `⏺` is drawn as a disc in the cell's colour (only colour-emoji fonts have it, as a fixed orange square). **Colour emoji** come from Noto Color Emoji's PNG strikes, scaled into the cell(s).
  - `fonts`: `fontdb` (pure Rust, reads fonts.conf) → DejaVu Sans Mono, then monospace and symbol fallbacks, then the bundled Hack and Noto Emoji. Font files are **memory-mapped** (file pages, not private memory). egui's own text gets the same fallbacks, and every symbol the UI draws is in the bundled fonts (tested), so no system font is required.
  - Layout: top bar (repo name, engine pill, theme), sidebar (orchestrator, worktrees with branch, dirty/ahead/behind, PR and CI badge, agents with status), worktree header (branch, last commit), agent tabs, the shell as a resizable bottom split, toasts, a banner while the engine is unreachable. Keys: Ctrl+K sidebar (j/k/↑/↓/Enter there), Ctrl+J agent and Ctrl+L shell (each passes through when its terminal already has the keyboard: newline in Claude Code, clear in a shell), Alt+]/[ and Alt+N/P tabs, Ctrl+=/−/0 font size; in a terminal Ctrl+C copies when there is a selection (else ^C), Ctrl+Shift+C/V, Shift+Insert, middle-click paste, an image-only clipboard paste sends ^V (Claude Code then reads the image).
  - **`lazyagent desktop [repo]`** (Python, no Textual import): runs the binary with `LAZYAGENT_PYTHON` set to its own interpreter; without a binary it prints how to build one.
- **Measured choices** (each from a bench below):
  - **Present without vsync** (`AutoNoVsync`: Immediate, else Mailbox, else Fifo) + a **repaint pacer** (output-driven repaints at once when the last one is a refresh old, else at the next slot): key → frame 16.7 ms → **4 ms** p50; floods still draw at ≤ 60 fps. GL offers only Fifo here, so Vulkan stays the default.
  - The window title was re-sent every frame, and a viewport command asks for a repaint: the app never idled (60 fps, 13 % CPU). Sent on change only.
  - **An offload NVIDIA GPU's Vulkan driver is kept out of the process** (`VK_ICD_FILENAMES` without it, only when every connected output is on another DRM driver, never when the user set it, and not passed on to agents): its threads polled every 10-100 ms, **1.2-1.4 % of a core idle**, and 20-30 MB.
  - Raw X11 device events are off (`DeviceEvents::Never`): they wake the loop on any mouse move on screen.
  - Release binary 24 MB (debug info stripped).
- **Tests** (cargo 59 = proto 26, term 16, app 17, plus 1 ignored bench; pytest +4): term unit tests (colours, wide chars, scrollback, modes, replies, selection, split sequences) and the snapshot round trip against a real engine; renderer and box-drawing geometry; the GPU heuristic on fake `/sys/class/drm` trees; **headless kittest UI tests**: sidebar from seed and events, keys → bytes through egui (Ctrl+C, Alt+b, arrows), copy with a selection, B12, no shell retry storm, a **lavapipe render** of terminal cells (CI requires a Vulkan adapter), and **the app against a real engine**: connect, click "Spawn an agent", type, read the fake agent's answer off the terminal, the shell opens. CI installs `mesa-vulkan-drivers`.
- **Manual pass** (real display, scratch repo with three worktrees, fake agents, `xdotool`): spawn and switch agents from the sidebar, type, `size` after a focus claim (37×166, the view) and after a window resize (23×89), Ctrl+L shell, wheel scrollback with its indicator, Alt+] tabs, SGR attributes, Unicode, light theme, drag-select + Ctrl+Shift+C (clipboard checked with xclip), app restart with the agents still running (screens restored by snapshot), `lazyagent engine stop` with the app open, then Start from the banner. Found and fixed: colour-only emoji (`⏺`, 🙂) blank; box-drawing seams; a failed `open_shell` retried every frame (toast storm); stale view after a stop (banner); long repo path pushing the engine pill off the bar; toasts covering the header buttons; wheel 6 lines per event (3 now).
- **Measured** (release, this laptop, defaults; ranges over 2-3 runs; web numbers from ui-plan §8a-landed):

  | | desktop | web | budget |
  |---|---|---|---|
  | B1 key → frame with the echo built, p50 / p95 | **3.9-4.4 / 6.6-9.9 ms** (next frame 6.2-6.5 / 8.2-11.6) | 8.4 / 8.9 (rAF) | 8 / 16 |
  | B1 from an idle loop (no keypress frame before) | 3.4 / 5.7 | — | |
  | B2 same with 8 hidden agents at ~1,000 lines/s, p95 / p99 | **6.95-7.2 / 8.6-9.1 ms** | not re-measured | 25 / 50 |
  | B3 key → bytes back (in the app) | 0.8-0.9 / 1.2-1.3 ms | 2.4 / 3.4 | 3 (p95) |
  | B4 frames while 5 MB arrive | 0 frames > 50 ms, interval p95 8.4-10.8 ms | p95 16.7-16.8 | 17 |
  | B5 5 MB of output until its last screen is drawn | **55-90 ms (57-102 MB/s)**, 1-2 engine stream resets (by design: a client 1 MiB behind gets a fresh snapshot) | 15-17 MB/s | ≥ 10 |
  | B6 idle, app + engine, one agent and a shell on screen | **0.2 % + 0.1-0.2 %**, 4 frames in 10 s | 0.3-0.5 % | ≤ 2 % |
  | B8 attach with snapshot (2,000 lines of history) p50 / p95 | 3.4-5.7 / 6.8-7.5 ms | 7.7-8.6 | 50 |
  | B9 memory | **35 MB private**, 138 MB RSS (drivers' pages) | 388-396 MB renderer RSS | ≤ 60 private |
  | B10 launch → sidebar / → agent terminal drawn, engine running | **121-128 / 155-162 ms** | — | |
  | B10 launch → sidebar, engine started by the app | 387-414 ms | TUI cold 0.43 s | ≤ 1 s |
  | B12 whole frame, 100 worktrees all changing | 2.1-2.2 ms | 2.8 / 3.7-7.1 ms (sidebar) | 4 (my target 1 ms: missed, egui lays out off-screen rows; virtualise when needed) |

  How: `lazyagent-desktop --bench echo|flood|idle|attach|startup` against a scratch repo whose `claude` is the fake agent (`LAZYAGENT_BENCH_BUSY=8` for B2, `LAZYAGENT_BENCH_OFF_FRAME=1` for the idle-loop row); B12 is `cargo test --release sidebar_with_100`. The echo's p95 is the event loop waking for the echo while it is still presenting the keypress's own frame; the frame itself costs 0.4 ms to build and ~1 ms of CPU in all.
- **Not done here** (later phases): dialogs (spawn options, create, remove, stop), the job drawer, restore, the `?` sheet (D3); attention, PR/CI panel, notifications (D4); diff (D5); `.desktop` entry, login-shell env, IME pre-edit, rounded box corners as arcs (D6).

### D3: actions (2026-10-18)

![spawn](desktop-screenshots/d3-spawn.png)

- **Dialogs** (`dialogs.rs`, egui modals; the web's dialogs.js): **Spawn** (instruction, label, skip permissions, session new / resume pick / resume last, the provider shown), **New worktree** (branch, base prefilled as on the web: the configured default branch only when some worktree has it checked out, else the main worktree's branch; extra options when a custom create command is configured; "and launch an agent in it" with its instruction and permissions), **Remove** (the web's stricter guard: the main worktree, or any agent not completed / failed / stopped; force), **Stop** (confirm), and the **`?` sheet** generated from one key table. Accelerators as the TUI's modals when no text field has the keyboard (`n` `d` `r` `l`, `y` `n` `f`), Enter submits, Ctrl+Enter from the instruction, Esc or a click outside closes. An engine error stays in the dialog (a failing custom create shows `WORKTREE_ERROR …` there).
- **Keys outside a terminal** (sidebar focused, or nothing): `s` `x` `c` `d` `r` `Shift+R` `?`, and Alt+G anywhere. "Outside" is decided with egui's `text_edit_focused()`, not `egui_wants_keyboard_input()`, which is true whenever any widget (the sidebar) has focus: the manual pass found `s` dead after Ctrl+K.
- **Create + launch** (§4a): `create_worktree` with the app's env; a custom command runs as a job (the drawer opens); the new worktree is selected once listed, then `spawn_agent` in it; a failed create launches nothing; a failed spawn after a good create is a toast and the worktree stays.
- **Job drawer:** `job.started` / `job.output` / `job.finished` in a bottom panel (the command, its output, the result; the last 192-256 KiB kept per job), a toast when each ends.
- **Restore** (PR 9): on every connection the app asks `list_restorable`; a banner names what an earlier engine ran ("Implementer (repo-feat-login), …") with **Restore all (Shift+R)**, Dismiss, Later; `engine.restored` toasts what came back and what failed. `restorable.changed` (engine-split Q31) replaces the list: an offered agent started again in this engine (the orchestrator with `--continue`) leaves the banner at once. The orchestrator is named "Orchestrator (the orchestrator)", not "Agent (the orchestrator)".
- Header buttons: + Agent, Remove, Stop (when there is an agent), Shell; sidebar: + New, ↻, ?.
- **Tests** (cargo 67 = proto 26, term 16, app 25): default base, remove guard, every key opens its dialog (also with the sidebar focused), the help sheet lists every key; against a real engine: **create + launch → the guard refuses the remove while the agent runs → stop (confirm with `y`) → remove → the selection falls back to main**; **a failing custom create** (`echo boom-{branch}; exit 3` in `.lazyagent.toml`) keeps the error in the dialog, its output in the job drawer and launches nothing; **the instruction and "skip permissions" reach the agent's command line** (read from `/proc`); **agents of a stopped engine are offered by the next one and come back** with Restore all.
- **Found on the way:** two edits of mine had silently not applied (the anchor text had been reformatted by rustfmt): the restore fetch on connect was missing until a test caught it; the edit helper now fails loudly. A test stopping the engine with SIGTERM to its whole process group killed the agents first, so they were "exited on their own" and rightly not offered: the testkit gained `terminate_engines` (the engine alone, as `engine stop` or a crash). A click one frame after the launch checkbox hit the modal's backdrop (the dialog grows a frame later); only a test can click that fast.
- **Measured:** a dialog opens in the frame of its key (the key is handled before the frame is laid out); nothing on the stream path changed (B1 as in D2).

### D4: attention queue, PR/CI panel, notifications (2026-10-18)

![attention](desktop-screenshots/d4-attention.png)
![pr panel](desktop-screenshots/d4-pr-panel.png)

- **Queue** (`attention.rs`, pure, the web's attention.js item for item): approval 1, question 2, hanged 3, failed / exited on its own 4, CI failing / changes requested / merge conflict on an open PR 5, "your turn" (`completed`, and `waiting`, how Claude reports a finished turn) 6, a failed job 7; newest first within a rank. An item lives while its condition holds; first-seen times are dropped once it clears, so a dismissed occurrence stays hidden but the next one shows. Re-derived only when its inputs change (store version, exits, finished jobs, marks) or every 30 s (snoozes running out).
- **Attention view** (`g a`, or Attention (N) in the top bar; `g w` back): the list (j / k / ↑ / ↓, Enter to the terminal) and the picked item: an agent's **live terminal** (the same pane as in the worktree view) with a **reply box** (`send_agent_input`, Ctrl+Enter; the toast says how it was delivered), Open (in the worktree view), Stop, Snooze 1h, Dismiss; a PR item shows the PR, its checks with links and **Send the failures to <agent>** (the web's prompt, check names and links); an unexpected exit offers **Spawn again (resume the last session)**; a failed job shows its output.
- **PR/CI panel** (Ctrl+. or PR in the worktree header): title (linked), state, review decision, mergeable, a check count and every check with its link; "No PR for <branch>", or "GitHub CLI not available" when `get_pr_info` could not fetch; ↻ fetches again.
- **Notifications:** while the window is in the background, a new item notifies through `notify-send` (libnotify; **decision changed from the plan's `notify-rust`**: no dependency, already on the desktop; urgent for approvals and questions), per kind on/off in **Notifications ▾**; a PR going red is a toast and a notification even with the window in front. The count is in the window title, `(3) lazyagent — repo` (taskbars show it).
- **State file** (the plan's Q3): `$XDG_STATE_HOME/lazyagent/desktop/<hash>.json` (0600, temp file + rename, at most once a second, off the UI thread, flushed on exit): dismissed occurrences and snoozes (pruned after a week / when expired), notification settings, the theme (used when `--theme` is not given). Tests point it at a temp dir.
- **Tests** (cargo 78 = proto 26, term 16, app 36): queue ranks, clearing, occurrences, snooze, PR kinds, exits, jobs, the CI prompt, mark pruning, the state file (round trip, debounce, corrupt file); the view: ranks, `(3)` title badge, `g a` / `g w` (after Ctrl+K: the agent's terminal otherwise has the keyboard), Dismiss and Snooze persisted, a new occurrence after the condition cleared; a PR going red: toast, CI item, the panel's checks, "Send the failures"; settings and theme back after a restart; a reply reaching a real fake agent with its delivery toast; a lavapipe render of both screens (above).
- **Measured:** re-deriving the queue for 200 agents (160 items): p50 0.10 / p95 0.18 ms release (budget 2 ms; asserted in release only, per the rule that perf asserts stay out of debug and CI paths); the attention view adds no work to the stream path.
- **Found on the way:** toasts at the top right covered the header buttons and the PR panel: they sit at the bottom right now, above the reply box. Not exercised on the real desktop: the notification popup itself (one `notify-send` call; `LAZYAGENT_DESKTOP_NO_NOTIFY=1` turns it off).

### D5: diff viewer and review (2026-10-18)

![diff](desktop-screenshots/d5-diff.png)
![split](desktop-screenshots/d5-diff-split.png)

- **Parser** (`diff.rs`, pure, the web's lib/diff.js): `git diff` plus the engine's untracked-file entries (raw contents), binary markers and the truncation notice → files (status, +/−, binary) with typed rows and line numbers; split pairs a run of removals with the run of additions after it; a comment anchors to (file, side, line); the review prompt is the web's, character for character (tested).
- **View** (Diff in the worktree header, **Ctrl+D outside a terminal, Ctrl+Shift+D anywhere** — Ctrl+D inside a terminal stays EOF, the plan's Q6; Back or the same key returns): the base (prefilled as New worktree would, empty for the main worktree = working tree vs index; Enter or ↻ diffs again), Unified / Split (kept in the state file), the counts, "Diff truncated at 4 MiB", a file list that jumps, and **one virtualised list of fixed-height rows** (`ScrollArea::show_rows`, 18 px): only the visible rows are laid out. `get_diff {base, max_bytes: 4 MiB}`; the text is parsed on a thread, never in the frame.
- **Comments:** click a line's numbers (either side in split) → write → Ctrl+Enter saves; shown under their line in both layouts (a context line has one anchor, so one comment row in split); click one to edit or delete. Kept per worktree in the app's state file (the web keeps them in localStorage; engine-stored threads are U8 / proposal 0008).
- **Submit review:** pick the agent (one that is idle first), an optional summary, a preview of exactly what it receives, "clear the comments once sent"; **Send** to an idle agent, or to a busy one **Send when idle** (queued in the app, sent when its status becomes waiting / your turn) or Send now. A review that could not be sent keeps the comments.
- **Tests** (cargo 87 = proto 26, term 16, app 45): the parser on the web's cases (modified, renamed, binary, untracked raw contents with an empty line, no-newline note, truncation), split pairs, anchors and the exact prompt; rows carrying comments in both layouts; the view: counts, a comment under its line, Split persisted, Submit → Send when idle → sent when the agent becomes idle (and comments kept when the send fails); **a real change in a real engine's worktree: Ctrl+Shift+D from the agent's terminal, the new file listed, a comment, Send now, the review text arriving in the fake agent, the comments cleared**; a lavapipe render of both layouts (above).
- **Measured** (5,201-line diff, 25 files, 1400×860, release; asserted in release only):

  | | desktop | web | budget |
  |---|---|---|---|
  | parse | 0.9-1.7 ms (on a thread) | 2-5 ms | — |
  | first screen | **6.1-6.3 ms** | 29-63 ms | 100 |
  | a scroll step (60 px), CPU per frame p50 / p95 | **0.43-0.46 / 0.52-0.55 ms** | rows 3.0-3.4 / 4.6-6.0 ms | 17 ms frame, 2 ms CPU (mine) |

- **Not done:** "open in the editor"; long lines scroll sideways, not wrapped (see the diff columns fix below); engine-stored review threads (U8).

### D6: packaging, polish, final manual pass (2026-10-18)

- **Application menu:** `lazyagent desktop --install` (or `lazyagent-desktop --install`) writes `$XDG_DATA_HOME/applications/lazyagent-desktop.desktop` (`desktop-file-validate` clean; `Exec` sets `LAZYAGENT_DESKTOP_LOGIN_ENV=1` and the `LAZYAGENT_PYTHON` it was installed with) and a generated 128 px icon under `icons/hicolor`; the window carries the same icon. Launched from the menu the cwd is `$HOME`, so a **repo picker** opens (recent repos, kept in the state dir, and a path field that has the keyboard); the choice re-execs the app on that repo.
- **Login-shell environment** (R7, Q4): with `LAZYAGENT_DESKTOP_LOGIN_ENV=1` (the menu entry sets it) the app probes `$SHELL -lic 'env -0'` (3 s at most, behind a marker so rc-file noise is ignored) while the window comes up, and agents, shells and jobs get that environment instead of the graphical session's (the profile's PATH, `SSH_AUTH_SOCK`, …). Variables the app sets for itself never reach them.
- **Wayland** (R5): built with winit's Wayland support (dlopen'ed, no -dev package); exercised on X11 only, the session this machine runs.
- README section; `lazyagent desktop --help` mentions `--install`.
- **Final manual pass** (real display, release build): the menu entry through `gtk-launch` → the picker → the app on the chosen repo, its engine auto-started and the shell carrying the login profile; in an isolated scratch world: spawn from the dialog (Ctrl+Enter and Enter), a second agent with a label, Claude hook events turning statuses into **approval / question items**, a **reply from the queue** reaching the fake agent, a real **diff from `main`**, a **comment by clicking a line number**, **Submit review → Send when idle → delivered the moment the agent finished its turn**. Found and fixed: text fields invisible on the main background (now `panel`-filled); Enter in the Spawn dialog's label field did not submit; the picker showed "Not inside a git repository: $HOME" as an error and its field did not have the keyboard; the review preview was not monospace (`small()` resets the font) and said "1 comments". Earlier phases' passes are in their notes.
- Left on this machine afterwards: nothing (the installed entry, icon, recent list and the scratch engine's log were removed; no process left).
- **Tests:** cargo 91 = proto 26, term 16, app 49 (+4: icon, `--install` output, recent repos, the login-shell probe with an rc file that prints noise).

### Fix: sidebar arrows stopped after one row (2026-10-19)

- Reported on a 27-worktree repo, fine on a 5-worktree one. egui moves the keyboard focus itself on unmodified arrow keys (and Tab) to the nearest focusable widget in that direction; with a long list a row sits below the middle of the sidebar's focus area, so the first ↓ moved the selection *and* the focus, and `j`/`k`/↑/↓ then went nowhere (they act only while the sidebar has the focus). The sidebar now sets a focus-lock filter for the arrows, as the terminal view already did; Tab still moves the focus. There are no Home/End/PageUp/PageDown keys in the sidebar.
- **Test:** `sidebar_keys_walk_a_long_list` (kittest, 27 worktrees with long branch names and detached ones): ↓×5, j×3, ↑, k, then ←/→ do not take the keyboard. Failed at the second ↓ before the fix.

### Fix: the diff's columns overlapped (2026-10-19)

- Seen on a 51-file branch in Split: long file names and their +/− counts ran over the line numbers (a truncated label in a horizontal row takes the remaining width, so the counts after it overflowed, and a vertical-only `ScrollArea` grows to its content); split panes clipped long lines with no way to read them; the worktree header's branch and "last: …" subject ran under its buttons (the labels took the row before the right-to-left buttons were laid out).
- **File list:** a resizable `Panel::left` inside the diff (default 260 px, 120-640); each entry paints its counts right-aligned and its name elided (`TextWrapping::truncate_at_width`) into what is left, full path on hover and as its accessible label.
- **Long lines scroll sideways**, one shared offset for both layouts (so split's two panes stay aligned): shift+wheel or a sideways swipe over the code, or the bar under it; line numbers and +/− marks stay put, the code clips past them and to its own pane. The limit is the longest line (tabs as four) against the narrowest code column.
- **Header:** buttons first, then the names left to right in what remains; the branch takes at most 60 % of it and the subject shows only when 60 px are left.
- **Test:** `the_diff_view_keeps_long_names_and_lines_in_their_columns` (kittest, 1000 px window): the header's branch and subject end before Stop/Remove (failed before the fix), every file entry ends left of the rows, shift+wheel scrolls split and unified sideways and clamps at both ends; the render test gained a long name and line.

## 11. Final state (2026-10-18)

| Web piece | Desktop | Notes |
|---|---|---|
| Sidebar, top bar, toasts, help, theme (U1) | ✅ D2/D3 | |
| Native terminals, shell, reconnect, size ownership (U2) | ✅ D2 | alacritty, own renderer, 4 ms key → frame |
| Actions, job drawer, restore (U3, PR 9) | ✅ D3 | |
| Attention queue, PR/CI panel, notifications (U4) | ✅ D4 | notify-send |
| Diff viewer, comments, review (U5) | ✅ D5 | comments in the app's state file |
| Launch from a terminal / the menu | ✅ D2/D6 | `lazyagent desktop`, `--install` |

- **How to run it:** `cargo install --locked --path desktop/crates/app` (or `cargo build --release --manifest-path desktop/Cargo.toml`), then `lazyagent desktop [repo]`; `lazyagent desktop --install` for the menu. Keys: `?`.
- **Measured against the web** (details per phase): key → frame p50 ~4 ms (web 8.4), with 8 chatty hidden agents p95 ~7 ms (budget 25), 5 MB of output drawn at 57-102 MB/s (web 15-17), idle 0.2 % of a core, 35 MB private memory (web renderer ~390 MB RSS), attach 3-6 ms, launch to sidebar 0.12 s (engine running) / 0.4 s (cold), a 5,200-line diff's first screen 6 ms.
- **Not done (later):** grid and orchestrator board (U6), engine-stored review threads (U8), phone (U9), usage/history (U10); IME pre-edit drawing, rounded box corners as arcs, "open in editor", a 1 ms frame with 100 worktrees (2.1 ms now: off-screen rows are laid out), a PyPI/manylinux build of the binary (Q1).
