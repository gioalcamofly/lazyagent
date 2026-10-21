<!-- Logo/banner placeholder: add a dark/light-mode-ready banner here
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/banner-light.svg">
    <img alt="lazyagent" src="docs/assets/banner-light.svg" width="600">
  </picture>
</p>
-->

<p align="center">
  <strong>A lazygit-inspired workspace for orchestrating coding agents across git worktrees: in the terminal, the browser or a native window.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/lazyagent/"><img src="https://img.shields.io/pypi/v/lazyagent?color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/lazyagent/"><img src="https://img.shields.io/pypi/pyversions/lazyagent" alt="Python"></a>
  <a href="https://github.com/gioalcamofly/lazyagent/actions"><img src="https://img.shields.io/github/actions/workflow/status/gioalcamofly/lazyagent/ci.yml?branch=main" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/gioalcamofly/lazyagent?color=green" alt="License"></a>
</p>

<!-- Demo GIF placeholder: record with VHS or asciinema and add here
<p align="center">
  <img src="docs/assets/demo.gif" alt="lazyagent demo" width="800">
</p>
-->

---

## Motivation

You have a big feature to ship.
You crack open three terminals, `cd` into three worktrees, and spawn three coding agents.
Ten minutes later you're alt-tabbing through a mess of tabs, trying to remember which agent finished, which one is stuck waiting for approval, and which one silently errored out five minutes ago.

**lazyagent** does for coding agents what **lazygit** did for git:
one screen, full visibility, zero tab-juggling.

## Highlights

- **Multi-agent orchestration** — spawn, monitor, and stop agents across git worktrees, several per worktree
- **Real-time provider-aware status** — know instantly if an agent is running, waiting for approval, idle, or done
- **Multi-provider support** — Claude, Codex, and Gemini out of the box, configurable per repo
- **Three front-ends, one engine** — a TUI, a web UI, and a native desktop app all drive the same engine
- **Agents that outlive the screen** — with a detached engine, closing the UI leaves the agents running; after a crash or reboot they can be restored
- **Attention queue** (web, desktop) — approvals, questions, failures and red CI in one ranked list, with a reply box
- **Diff review** — working-tree changes in every front-end; in web and desktop, comment on lines and send them to an agent as a review
- **PR / CI at a glance** — pull request state, review status, and CI checks per worktree (via `gh`)
- **MCP server** — let a Claude session (lazyagent's own orchestrator, or yours) drive worktrees and agents

## Installation

> **Prerequisites:** Python 3.10+, git, and at least one agent CLI (`claude`, `codex`, or `gemini`).

```bash
uv tool install lazyagent    # recommended
pip install lazyagent        # or
```

This gives you the TUI, the web UI and the engine. Optional extras:

| For | Install |
|---|---|
| PR and CI status | the [GitHub CLI](https://cli.github.com/) (`gh`), logged in |
| The desktop app (Linux) | a Rust toolchain ([rustup.rs](https://rustup.rs), 1.88+), then from a clone of this repo: `cargo install --locked --path desktop/crates/app` (puts `lazyagent-desktop` in `~/.cargo/bin`) |
| Desktop notifications from the app | `notify-send` |

## Choosing a front-end

| | TUI | Web UI | Desktop app |
|---|---|---|---|
| Command | `lazyagent` | `lazyagent host` | `lazyagent desktop` |
| Runs in | your terminal | a browser tab on 127.0.0.1 | a native window (Rust, egui on wgpu) |
| Status | stable | experimental | experimental |
| Agent terminals | pyte, inside the TUI | xterm.js | `alacritty_terminal`, own GPU renderer |
| Attention queue, review comments | — | ✅ | ✅ |
| Agents survive closing it | with `--remote` | with `--remote` | always |
| Use it when | you live in a terminal, over SSH | you want a GUI with no build step | you want the fastest GUI (~4 ms key → frame) |

All three are clients of the repo's [engine](#the-engine); on a detached engine several can be open on one repo at once.

## Quick Start

### TUI

```bash
cd your-repo
lazyagent              # or: lazyagent --remote, to keep the agents when you quit
```

1. **Create worktrees** — press `c` to branch off parallel workstreams
2. **Spawn agents** — press `s` to launch a coding agent in the selected worktree (again for a second one: each gets a tab)
3. **Monitor** — watch agent output stream in real time; status badges update automatically
4. **Interact** — press `Ctrl+L` to drop into the embedded terminal for hands-on work
5. **Review** — press `Ctrl+D` to inspect diffs before committing
6. **Clean up** — press `d` to remove worktrees when done

By default lazyagent launches `claude`. Set `provider = "codex"` or `provider = "gemini"` in `.lazyagent.toml` to switch.

### Web UI (experimental)

```bash
lazyagent host your-repo --open        # --remote: quitting leaves the agents running
```

It serves 127.0.0.1 only (`--port` to pick one) and prints a URL with an access token; `--open` opens it in your browser. The page shows a live sidebar of worktrees, agents and PR/CI, every agent and shell in a native browser terminal, and the TUI itself as one more tab. From the page you can spawn, create, stop and remove (custom create commands show in a job drawer), work through the attention queue (`g a`), open the PR/CI panel (`Ctrl+.`), and review a diff (`Ctrl+D`): click a line number to comment, then *Submit review* sends the comments to an agent, now or when it next goes idle. Dark, light or system theme; notifications per kind. Design and measurements: [docs/design/ui-plan.md](docs/design/ui-plan.md) (§8a "As landed").

### Desktop app (experimental)

```bash
cargo install --locked --path desktop/crates/app   # once, see Installation
lazyagent desktop your-repo                        # or: lazyagent-desktop your-repo
lazyagent desktop --install                        # add it to the application menu
```

Feature parity with the web UI (sidebar, terminals and shell, actions and job drawer, restore, attention queue with replies, PR/CI panel, notifications, diff viewer with review), and it always runs on a detached engine, starting one if none runs, so agents keep running when it closes. Launched from the menu (or outside a repo) it opens a repo picker with your recent repos; agents started from there get your login shell's environment. `?` in the app lists every key; `lazyagent-desktop --help` lists its options.

**Updates and restarts.** The menu bar's *Help → Check for updates…* compares this install with the latest release on PyPI (and runs `uv tool upgrade` / `pipx upgrade` when one of them made it), or, when lazyagent runs from a git checkout (an editable install, or the app built in `desktop/target/`), fetches the checkout's branch and updates it by fast-forward, then runs `uv sync` when the lock changed and rebuilds the app when `desktop/` changed. Uncommitted changes stay as they are; when git would overwrite some, nothing is updated and the files are listed. It checks on its own 10 s after start and every 6 h (*Help → Check automatically*). *File → Restart…* restarts the app, and optionally the engine too (its running agents are listed first, busy ones flagged), restoring the agents it stopped; the app comes back after the restore. The same from a terminal: `lazyagent update check|apply`, `lazyagent engine restart --restore`.

Measured on a laptop, against the web UI: key → frame p50 ~4 ms (web 8.4), 5 MB of output drawn at 57-102 MB/s (web 15-17), idle 0.2% of a core, 35 MB private memory, launch to sidebar 0.12 s with the engine running. Linux only (X11 tested, Wayland built in); building needs no system development packages. Design and measurements: [docs/design/desktop-app.md](docs/design/desktop-app.md).

## The engine

The engine is the process that owns the worktrees' agents, shells and jobs; every front-end is a client of it.

- **Embedded** (the default for `lazyagent` and `lazyagent host`): the engine lives inside the TUI, and quitting stops the agents.
- **Remote** (`--remote`, or `LAZYAGENT_ENGINE=remote`; always for the desktop app): the engine is a detached process per repo, started on demand. `q` only closes the screen; the next `lazyagent --remote` comes back to the same agents.

```bash
lazyagent engine start [repo]      # start it detached (--foreground: in this terminal)
lazyagent engine status [repo]     # pid, agents, socket, log (--json; exit 3: not running)
lazyagent engine stop [repo]       # stop it and its agents (--if-idle: only with none)
lazyagent engine restart [repo]    # stop and start it (new code); --restore brings back
                                   # exactly the agents it stopped
lazyagent engine logs [repo] -f    # its log (-n LINES, --path)
lazyagent engine restore [repo]    # agents an earlier engine left: list them,
                                   # --all / --id ID to bring them back, --dismiss
```

**Restore.** When an engine ends (stopped, crashed, or the machine rebooted) it leaves a manifest of the agents it ran. The next engine offers them: `lazyagent engine restore`, `Shift+R` in any front-end (or *Restore agents* in the TUI's command palette), or the `restore_agents` MCP tool. Set `[engine] restore_on_start = true` to bring them back automatically. A restored Claude agent resumes its conversation (`--resume`) with the same agent id and session name. Design: [docs/design/engine-split.md](docs/design/engine-split.md).

## Features

### Agent Observability

lazyagent doesn't just scrape terminal output. It taps into each provider's native signals for high-confidence state detection:

| Provider | Signal Source | What It Catches |
|----------|--------------|-----------------|
| **Claude** | Hooks (JSONL logs) | Permission prompts, idle states, task completion |
| **Codex** | App Server Events (JSON-RPC) | Turns, approval requests, failures |
| **Gemini** | Telemetry (file export) + screen detection | Activity, session boundaries |

Statuses are normalized into a clear lifecycle:

| Status | Color | Meaning |
|--------|-------|---------|
| `running` | green | Actively working or calling tools |
| `approving` | yellow | Waiting for user approval |
| `waiting` | yellow | Idle or waiting for input |
| `completed` | cyan | Task finished |
| `failed` | red | Error or turn failure |

<!-- Screenshot placeholder: sidebar showing mixed agent statuses -->

### Worktrees, diffs, PR / CI

- Create, remove, and navigate git worktrees without leaving lazyagent. Each worktree gets its agents, a shell, and a diff view. Custom create/remove commands are supported (see [Configuration](#configuration)).
- The diff covers tracked and untracked files. In the web and desktop apps you can also diff against a base branch and switch between unified and split layouts.
- When `gh` is available, each worktree shows its PR state, review decision, and CI checks (linked, in the apps).

### The orchestrator and messaging between Claude agents

The sidebar's first entry is the repo's orchestrator: a Claude session with lazyagent's MCP tools (wired up as `lazyagent_app`), which can create worktrees and spawn and direct workers for you.

Claude agents that lazyagent spawns get a predictable session name — `<worktree folder>-<agent id>`, e.g. `myrepo-fix-login-a1`, and `<repo>-orchestrator` for the orchestrator. `list_worktrees` and `list_agents` report it as `session_name`. The orchestrator uses Claude Code's built-in `SendMessage` with that name to give workers instructions and `notify_when_idle` to be woken when they finish; workers report back the same way. Messages never land in a permission prompt or a half-typed draft, which typed input (`send_agent_input`, still used for Codex and Gemini) can.

Claude Code holds a message from a session in another permission mode until a human approves it. Spawn the orchestrator and its workers in the same mode, or set `cross_session_inbound = "accept"` (see [Configuration](#configuration)). Accepting means any of your local Claude sessions can instruct the agents, including a bypass-permissions one, so turn it on only if that is what you want.

## Key bindings

### TUI

| Key | Action |
|-----|--------|
| `j` / `k` | Move down / up in sidebar |
| `Ctrl+K` | Focus sidebar |
| `Ctrl+J` | Focus agent pane |
| `Ctrl+D` | Focus diff pane |
| `Ctrl+L` | Focus terminal pane |
| `Alt+]` / `Alt+N` | Next agent tab |
| `Alt+[` / `Alt+P` | Previous agent tab |
| `s` | Spawn an agent in the selected worktree (adds a tab) |
| `x` | Stop the active agent tab |
| `R` | Restore the agents an earlier engine ran |
| `c` | Create new worktree |
| `d` | Remove selected worktree |
| `r` | Refresh worktree list (and every git status) |
| `Alt+G` | Refresh git status of the selected worktree |
| `PageUp` / `PageDown`, mouse wheel | Scroll terminal history |
| `?` | Show help |
| `q` | Quit |

### Web UI and desktop app

They share the TUI's keys (`s`, `x`, `c`, `d`, `r`, `Shift+R`, `?`, `j`/`k` outside a terminal; `Ctrl+K`/`J`/`L`, `Alt+N`/`P`, `Alt+G` anywhere), plus:

| Key | Action |
|-----|--------|
| `g a` / `g w` | The attention queue / the worktree view |
| `Ctrl+.` | The worktree's PR and CI panel |
| `Ctrl+D` | The worktree's diff. **Desktop:** `Ctrl+D` is EOF inside a terminal; there, use `Ctrl+Shift+D` |
| `Ctrl+Enter` | Send: the reply box, a dialog's instruction, a diff comment |
| `Esc` | Close a dialog |
| `Ctrl+=` / `Ctrl+−` / `Ctrl+0` | Desktop: terminal font size larger / smaller / default |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Desktop: copy / paste in a terminal (`Ctrl+C` copies a selection, else sends ^C) |
| `Shift+drag`, `Shift+wheel` | Desktop: select and scroll even when the program uses the mouse |

`?` in either shows the full list.

## Configuration

Create a `.lazyagent.toml` in your repository root:

```toml
# Branch to base new worktrees on (default: "master"). When no worktree has it
# checked out, the main worktree's branch is used instead.
default_branch = "main"

[agent]
# Agent CLI to launch: "claude" (default), "codex", or "gemini"
provider = "claude"
# Claude only: what spawned agents do with SendMessage from a session in another
# permission mode — "accept", "hold" or "refuse". Unset keeps Claude Code's
# default, which holds such messages until a human approves them.
# cross_session_inbound = "accept"

[worktree]
# Custom command templates for worktree management. They run in the repo root,
# and their output shows in a log under the worktree view while they run.
# Available placeholders: {branch}, {name}, {base}, {path}, {repo}, {force}, {extra}
create = "git worktree add -b {branch} ../{name} {base}"
remove = "git worktree remove ../{name}"

[engine]
# Bring back, when the engine starts, the agents the previous one ran when it
# ended (stopped, crashed, or the machine rebooted). Default: only offer them
# (lazyagent engine restore).
# restore_on_start = true
```

### Environment variables

| Variable | Effect |
|---|---|
| `LAZYAGENT_ENGINE=remote` | Same as `--remote` |
| `LAZYAGENT_ENGINE_SOCKET` | Use this engine socket (no discovery, no auto-start) |
| `LAZYAGENT_ENGINE_AUTOSTART=0` | Never start an engine |
| `LAZYAGENT_DESKTOP_BIN` | The desktop binary `lazyagent desktop` runs (else `lazyagent-desktop` on PATH, then `desktop/target/release/` in a source checkout) |
| `LAZYAGENT_DESKTOP_PRESENT` | Desktop present mode: `fifo` (vsync), `immediate`, `mailbox`, `novsync` (default), `auto` |
| `LAZYAGENT_DESKTOP_ALL_GPUS=1` | Desktop: keep an idle offload NVIDIA GPU's Vulkan driver loaded (by default it is left out, saving ~1% of a core) |
| `LAZYAGENT_DESKTOP_NO_NOTIFY` | Desktop: no desktop notifications |
| `LAZYAGENT_DESKTOP_FONT`, `LAZYAGENT_DESKTOP_FONT_SIZE` | Desktop terminal font (default DejaVu Sans Mono, 13) |

Logging variables are under [Diagnostic logging](#diagnostic-logging).

## MCP integration

lazyagent ships an MCP server (`lazyagent mcp`) that exposes its worktree and agent tools:

| Tool | What it does |
|---|---|
| `list_worktrees` | Worktrees with their agents, statuses and session names |
| `create_worktree` | New worktree and branch; without `base_branch` it uses `default_branch` when a worktree has it checked out, else the main worktree's branch |
| `remove_worktree` | Remove a worktree (`force` for a dirty one) |
| `spawn_agent` | Start a new agent (instruction, permissions, resume mode, label) |
| `list_agents` / `get_agent_status` | A worktree's agents / one agent's status |
| `read_agent_output` / `send_agent_input` | Read an agent's screen / type into it |
| `stop_agent` | Stop an agent |
| `list_events` | Status changes since a cursor (to wait on agents) |
| `list_restorable` / `restore_agents` / `dismiss_restorable` | The agents an earlier engine left: list, bring back, stop offering |
| `review_reply` | An agent's answer to a review comment or question from the desktop Diff view (the `comment_id` its prompt gave); shown under the code. Only the agent the comment was sent to may answer it |

Agents that lazyagent itself spawns (worktree agents and the orchestrator) get the server wired up automatically, under the name `lazyagent_app` and running the same lazyagent as the app, so a `lazyagent` server you register yourself can't shadow it. To drive lazyagent from a `claude` session you start by hand, register it once at user scope:

```bash
claude mcp add --scope user lazyagent -- lazyagent mcp
cd /tmp && claude mcp list | grep lazyagent      # verify
```

`python3 -m lazyagent.mcp_server` works too, with the interpreter that installed lazyagent (if `claude mcp add` swallows `-m`, separate args with `--`). The tools talk to a running lazyagent over a Unix socket under `/tmp/lazyagent-<pid>/` and return `No running lazyagent instance found` if none is running.

In an agent lazyagent spawned, both servers are there, and both know which agent is calling: the engine puts the agent's token and socket (`LAZYAGENT_AGENT_TOKEN`, `LAZYAGENT_SOCKET`) in the agent's own environment, and Claude starts every MCP server with it. So `notes_read()` with no arguments reads the caller's own notes on either one (as long as your `lazyagent` server runs a lazyagent recent enough to read the token).

## Development

```bash
git clone https://github.com/gioalcamofly/lazyagent.git
cd lazyagent
uv sync --group dev
uv run pytest
```

| Tests | Run | Notes |
|---|---|---|
| Unit, integration, contract | `uv run pytest` | What CI runs |
| Browser E2E (`e2e` marker) | `uv sync --group dev --group e2e`, then `uv run pytest -m e2e` | Playwright; local only, skipped without Playwright or a Chromium (Playwright's own, else system Chrome) |
| Performance (`perf` marker) | `uv run pytest -m perf` | Deselected by default; checks the [ui-plan](docs/design/ui-plan.md) §7d budgets; local only |
| Desktop app | `cd desktop && cargo test` | Real-engine tests use the repo's `.venv` (or `LAZYAGENT_PYTHON`) and are skipped without it; `cargo test -- --ignored` runs the benches. CI also runs `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` |

Docs: [docs/](docs/README.md) holds the design docs (how things are built) and proposals (what could come next).

### Diagnostic logging

Off by default. To capture a trace of a freeze or of general lag:

```bash
LAZYAGENT_LOG=1 lazyagent              # lifecycle, timers, workers,
                                       # subprocesses, spawn path, watchdog
LAZYAGENT_LOG_LEVEL=debug lazyagent    # ...plus per-chunk terminal traces
```

The log is JSON lines — one object per event, each with a millisecond
timestamp, level, logger, thread, and a `duration_ms` where something was
timed. It goes to `$XDG_STATE_HOME/lazyagent/lazyagent.log`
(`~/.local/state/lazyagent/` by default), rotated at 5 MB with 3 backups.
A detached engine has its own log: `lazyagent engine logs`.

| Variable | Effect |
|---|---|
| `LAZYAGENT_LOG` | `1` to enable, or a level name (`debug`, `info`, …) |
| `LAZYAGENT_LOG_LEVEL` | Level, overriding the above |
| `LAZYAGENT_LOG_FILE` | Write somewhere else (also enables logging) |
| `LAZYAGENT_LOG_WATCHDOG_MS` | Event-loop stall threshold, default `100` |

The watchdog is the useful part when the UI freezes: a thread outside the event
loop notices when the loop stops responding and records `loop.blocked` with a
stack snapshot of what the main thread was doing *while it was still stuck*.

Aggregating afterwards, e.g. the slowest subprocesses:

```bash
jq -r 'select(.event=="subprocess") | [.duration_ms, .cmd] | @tsv' \
  ~/.local/state/lazyagent/lazyagent.log | sort -rn | head
```

## Acknowledgements

- [lazygit](https://github.com/jesseduffield/lazygit) — the inspiration for the UX and workflow
- [Textual](https://github.com/Textualize/textual) — the TUI framework powering the interface
- [pyte](https://github.com/selectel/pyte) — terminal emulation for agent output capture
- [xterm.js](https://xtermjs.org/), [Preact](https://preactjs.com/) — the web UI's terminals and components
- [alacritty_terminal](https://github.com/alacritty/alacritty), [egui](https://github.com/emilk/egui), [wgpu](https://wgpu.rs/) — the desktop app's terminals, UI and rendering

## License

[AGPL-3.0](LICENSE)
