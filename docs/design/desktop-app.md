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
| Restore | `list_restorable`, `restore_agents`, `dismiss_restorable`, `engine.restored` |
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
