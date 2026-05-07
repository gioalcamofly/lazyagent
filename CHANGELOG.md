# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
