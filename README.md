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
  <strong>A lazygit-inspired TUI for orchestrating coding agents across git worktrees.</strong>
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
one TUI, full visibility, zero tab-juggling.

## Highlights

- **Multi-agent orchestration** — spawn, monitor, and stop agents across git worktrees from a single screen
- **Real-time provider-aware status** — know instantly if an agent is running, waiting for approval, idle, or done
- **Multi-provider support** — Claude, Codex, and Gemini out of the box, configurable per repo
- **Embedded terminal** — drop into any worktree's shell without leaving the TUI
- **Inline diff view** — review working-tree changes (tracked + untracked) right next to agent output
- **PR / CI at a glance** — pull request state, review status, and CI checks per worktree (via `gh`)

## Installation

> **Prerequisites:** Python 3.10+, git, and at least one agent CLI (`claude`, `codex`, or `gemini`).

### uv (recommended)

```bash
uv tool install lazyagent
```

### pip

```bash
pip install lazyagent
```

> **Optional:** install the [GitHub CLI](https://cli.github.com/) (`gh`) to enable PR and CI status features.

## Quick Start

```bash
cd your-repo
lazyagent
```

From there:

1. **Create worktrees** — press `c` to branch off parallel workstreams
2. **Spawn agents** — press `s` to launch a coding agent in the selected worktree
3. **Monitor** — watch agent output stream in real time; status badges update automatically
4. **Interact** — press `Ctrl+L` to drop into the embedded terminal for hands-on work
5. **Review** — press `Ctrl+D` to inspect diffs before committing
6. **Clean up** — press `d` to remove worktrees when done

By default lazyagent launches `claude`. Set `provider = "codex"` or `provider = "gemini"` in `.lazyagent.toml` to switch.

## Features

### Worktree Management

Create, remove, and navigate git worktrees without leaving the TUI. Each worktree gets its own agent, terminal, and diff view — perfect for parallelizing tasks across branches.

<!-- Screenshot placeholder: worktree sidebar with multiple active agents -->

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

### Diff Tab

Press `Ctrl+D` to see working-tree changes (both tracked and untracked files) rendered inline. Review what your agents have done before committing.

<!-- Screenshot placeholder: diff view -->

### PR / CI Status

When the `gh` CLI is available, lazyagent shows pull request state, review status, and CI check results per worktree — no browser needed.

### Embedded Terminal

Press `Ctrl+L` to open a full terminal session inside any worktree. Run tests, inspect files, or interact with agents directly — then jump back to the overview.

### Configurable Providers

Switch between `claude`, `codex`, and `gemini` globally or per repository via `.lazyagent.toml`. Custom worktree create/remove commands are also supported.

<details>
<summary><strong>Keybindings</strong></summary>

| Key | Action |
|-----|--------|
| `j` / `k` | Move down / up in sidebar |
| `Ctrl+K` | Focus sidebar |
| `Ctrl+J` | Focus agent pane |
| `Ctrl+D` | Focus diff pane |
| `Ctrl+L` | Focus terminal pane |
| `s` | Spawn agent in selected worktree |
| `x` | Stop agent in selected worktree |
| `c` | Create new worktree |
| `d` | Remove selected worktree |
| `r` | Refresh worktree list |
| `PageUp` / `PageDown` | Scroll terminal history |
| `?` | Show help |
| `q` | Quit |

</details>

## Configuration

Create a `.lazyagent.toml` in your repository root:

```toml
# Branch to base new worktrees on (default: "master")
default_branch = "main"

[agent]
# Agent CLI to launch: "claude" (default), "codex", or "gemini"
provider = "claude"

[worktree]
# Custom command templates for worktree management
# Available placeholders: {branch}, {name}, {base}, {path}, {repo}
create = "git worktree add -b {branch} ../{name} {base}"
remove = "git worktree remove ../{name}"
```

## Development

```bash
git clone https://github.com/gioalcamofly/lazyagent.git
cd lazyagent
uv sync --group dev
uv run pytest
```

## Acknowledgements

- [lazygit](https://github.com/jesseduffield/lazygit) — the inspiration for the UX and workflow
- [Textual](https://github.com/Textualize/textual) — the TUI framework powering the interface
- [pyte](https://github.com/selectel/pyte) — terminal emulation for agent output capture

## License

[AGPL-3.0](LICENSE)
