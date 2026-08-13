# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Performance
- Terminal panes render roughly 7x cheaper per frame: rows are run-length
  encoded into styled segments instead of being built a character at a time,
  and resolved styles are cached per pane
- Scrollback now costs ~2.8 MB per terminal instead of ~91 MB at the 5000-line
  default, and no longer holds GC-tracked objects. Full garbage collections
  with six worktrees' worth of history drop from ~800 ms — a visible freeze of
  the whole UI — to under 20 ms, and no longer grow with the number of
  worktrees that have scrollback, idle or not
- Terminal output parsing is ~3x faster for plain ASCII, which is most of it.
  Each additional streaming agent now costs about a fifth of what it did, so
  several busy worktrees no longer saturate a core

### Fixed
- A styled cell in the rightmost column of a terminal is no longer painted with
  its left neighbour's style (visible as a box border or right-aligned badge
  losing its colour)

## [0.6.0] - 2026-07-14

### Added
- Multiple agents per worktree: press `s` to spawn additional agents in the
  selected worktree; each gets its own tab to the left of the Diff tab. `x`
  stops the active agent tab, and the sidebar shows a roll-up status (e.g.
  `waiting (+1)`) when a worktree runs more than one agent
- New `list_agents` MCP tool returning per-agent id/label/status; `spawn_agent`
  now returns the new `agent_id` and no longer rejects a worktree that already
  has a running agent
- `agent_id` is now an optional parameter on `stop_agent`, `get_agent_status`,
  `read_agent_output`, and `send_agent_input` — omit it when a worktree has a
  single agent; supply it (else an error lists the choices) when there are
  several. `list_worktrees` gains an `agents` array alongside the scalar
  `agent_status` roll-up
- Cycle between agent tabs with `Alt+]` (next) and `Alt+[` (previous); `Alt+n`
  and `Alt+p` do the same on terminals that drop the Alt modifier from bracket
  keys

## [0.5.3] - 2026-06-17

### Fixed
- Newly-mounted `WorktreePanel` is now hidden until activated, fixing a phantom pane that stacked under the visible panel when an MCP `spawn_agent` targeted a non-focused worktree

### Performance
- Cut idle CPU and worktree-switch spikes on the navigation hot path: skip no-op terminal resizes, coalesce hidden-terminal stdout, throttle screen scans, and move the selected-diff git subprocess off the message pump

## [0.5.2] - 2026-05-12

### Fixed
- `send_agent_input` now sends a carriage return so the TUI actually submits the input
- Custom `[worktree] create` command runs in the repo root via subprocess and no longer requires a worktree selected in the UI, unblocking MCP-driven orchestration

## [0.5.1] - 2026-05-07

### Fixed
- Add missing MCP tools and wire missing parameters
- Auto-discover IPC socket and add `.mcp.json`
- Filter DCS/CSI escape sequences that pyte 0.8.2 mishandles
- Harden MCP/IPC layer, pyte filter, and PTY capture

## [0.5.0] - 2026-04-24

### Added
- MCP server and IPC layer for orchestrator communication
- Orchestrator UI panel with dedicated sidebar entry
- Orchestrator system prompt wired to MCP
- Pass initial instruction to spawned agents via CLI argument

### Fixed
- Critical bugs and efficiency issues in MCP/IPC layer
- Unified instruction type and fixed brittle test assertions
- Thread socket_path to all providers for IPC spawn

### Changed
- Eliminated orchestrator/worktree code duplication
- New dependency: `mcp>=1.0.0`

## [0.4.0] - 2026-04-04

### Added
- Mouse text selection in terminal panes (double-click word, triple-click line)
- System clipboard copy support for terminal selection
- Session resume support in spawn modal and agent providers

### Fixed
- Terminal cursor visibility by applying cursor style after character styles
- Line ending normalization in terminal
- Improved spawn modal UX

### Changed
- Improved terminal cursor styling and cached resolved default colors for better performance

## [0.3.2] - 2026-03-31

### Fixed
- Terminal cursor visibility by applying cursor style after character styles

### Changed
- Improved terminal cursor styling and cached resolved default colors for better performance

## [0.3.1] - 2026-03-19

### Fixed
- Stale `SENTINEL_SYSTEM_PROMPT` import in test_center_panel causing CI collection failure
- Updated sentinel-based test assertions to match new hooks-based observer approach

## [0.3.0] - 2026-03-19

### Added
- Provider observability: real-time agent lifecycle detection via JSONL log tailing for Claude, Codex, and Gemini
- Claude observer with tool-use hook detection and `--settings` flag support
- Codex and Gemini observers with structured event parsing
- `AgentObserver` base interface and `CompositeObserver` for combining observation strategies
- Rich agent status model with lifecycle confidence levels
- Remove worktree modal with force-removal toggle (`f` key)

### Changed
- Worktree removal now supports `--force` flag for dirty worktrees
- Pending observer events are flushed on PTY disconnect

## [0.2.2] - 2026-03-16

### Fixed
- UnicodeDecodeError when viewing diffs with binary or non-UTF-8 files (e.g. PDFs)

## [0.2.0] - 2026-03-05

### Added
- Agent provider interface: pluggable support for multiple agent CLIs
- Gemini provider support (`--approval-mode=yolo` for dangerous mode)
- Integration tests for app startup and provider configuration

## [0.1.0] - 2026-03-03

### Added
- Textual-based TUI for managing coding agents across git worktrees
- Multi-worktree management (create, remove, navigate)
- Real-time agent output streaming
- Sentinel-based status detection for agent completion and input prompts
- Scrollback buffer with PageUp/PageDown and mouse wheel support
- Diff tab showing working tree changes (tracked + untracked)
- PR/CI status widget per worktree (requires `gh` CLI)
- Embedded terminal pane for direct worktree interaction
- Configurable agent provider (`claude` or `codex`) via `.lazyagent.toml`
- Configurable worktree create/remove commands with placeholders
- `py.typed` marker for type checking support

### Fixed
- Agent session lifecycle: crash on re-spawn, incomplete cleanup, and stale terminal on exit
- Paste handling and visual glitches
- Local environment variables now exported to spawned agents
