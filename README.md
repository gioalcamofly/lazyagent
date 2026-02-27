# lazyagent

A lazygit-inspired TUI for managing coding agents across git worktrees.

## Features

- **Multi-worktree management** — create, remove, and navigate git worktrees from one screen
- **Real-time agent output** — watch coding agents stream output as they work
- **Sentinel-based status detection** — automatically detects when agents finish or need input
- **Scrollback buffer** — scroll through agent output history with PageUp/PageDown or mouse wheel
- **Diff tab** — view working tree changes (tracked + untracked) without leaving the TUI
- **PR/CI status** — see pull request state, review status, and CI check results per worktree (requires `gh` CLI)
- **Embedded terminal pane** — interact with worktrees directly without leaving the TUI
- **Configurable agent provider** — run either `claude` or `codex` per repository
- **Configurable worktree commands** — override create/remove commands via `.lazyagent.toml`

## Installation

From GitHub:

```bash
uv tool install git+https://github.com/gioalcamofly/lazyagent.git
```

Or with pip:

```bash
pip install git+https://github.com/gioalcamofly/lazyagent.git
```

## Quick Start

```bash
cd your-repo
lazyagent
```

lazyagent discovers existing worktrees and lets you spawn coding agents in each one.
By default it launches `claude`; set `provider = "codex"` in config to use Codex.

## Usage

### Keybindings

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

### Workflow

1. Open lazyagent in a git repository
2. Press `c` to create worktrees for parallel tasks
3. Press `s` to spawn a coding agent in a worktree
4. Watch agent output in real time — status updates automatically when the agent finishes or needs input
5. Use `Ctrl+L` to drop into the terminal pane for manual interaction
6. Press `d` to clean up worktrees when done

## Configuration

Create a `.lazyagent.toml` in your repository root:

```toml
# Branch to base new worktrees on (default: "master")
default_branch = "main"

[agent]
# Agent CLI to launch in each worktree: "claude" (default) or "codex"
provider = "codex"

[worktree]
# Custom command template for creating worktrees
# Available placeholders: {branch}, {name}, {base}, {path}, {repo}
create = "git worktree add -b {branch} ../{name} {base}"

# Custom command template for removing worktrees
remove = "git worktree remove ../{name}"
```

## Development

```bash
git clone https://github.com/gioalcamofly/lazyagent.git
cd lazyagent
uv sync --group dev
uv run pytest
```

## License

[MIT](LICENSE)
