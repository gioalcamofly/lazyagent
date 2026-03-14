# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
