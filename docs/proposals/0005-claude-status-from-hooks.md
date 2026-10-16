# 0005. Hook-driven status: skip the screen scan where it isn't needed

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §3a, K1, §6 (PR 4)

## Problem

- The roadmap item was "Claude status from hooks / stream-json instead of screen scraping". **Half of it already exists:** Claude status comes from hook events (`ClaudeHooksObserver`, agent_observers.py:135; hooks installed by agent_providers.py:387-410). Codex uses its app-server log the same way. Only Gemini also scrapes the screen (`GeminiPromptObserver`).
- What's left is cost: `MonitoredTerminal._do_scan_screen` (widgets/monitored_terminal.py:137) still renders the full screen text up to ~2 Hz per agent and hands it to `on_screen_update`, which for Claude and Codex is the base no-op. In the engine that becomes a headless pyte feed for **every** agent (K1).

## Today's workaround

- Visibility gating and coalescing in the TUI (scrollable_terminal.py:54, :528) hide the cost for agents not on screen.

## Proposal

- Observers declare what they need: `needs_screen: bool` (default `False`; `True` for `GeminiPromptObserver`). A `CompositeObserver` needs it if any child does.
- The engine skips the scan entirely when no observer needs the screen. Hang detection keeps using output timing (`_last_output_time`), which needs no screen.
- The headless emulator becomes **lazy** for those agents: feed the ring buffer always; parse into pyte only when someone calls `read_agent_output`, takes a snapshot, or attaches (replay from the ring, §3b).
- `stream-json`: only relevant for non-interactive (`claude -p`) background agents, which lazyagent doesn't run today. Out of scope until it does.
- **No protocol change.** `agent.status` stays the same; the difference is where it comes from and what it costs.

## Fits after

- **PR 4** (`AgentSession` holds the observer, scan and ring buffer). The flag can land earlier in the TUI as a pure optimisation.

## Value / Effort

| Value | Effort |
|---|---|
| ★★ (cuts K1; more agents per machine) | S (skip scan) / M (lazy headless emulator) |

## Risks

- Lazy parsing makes the first `read_agent_output` after a long idle slow (replay of up to the ring size, e.g. 4 MiB). Cap or keep a checkpoint snapshot.
- If the ring wrapped, a lazy screen is incomplete. Keep a periodic low-rate snapshot (e.g. every 30 s) as a floor.
- Hooks depend on Claude Code's hook contract; a breaking change silently freezes status. `POSSIBLY_HANGED` is the safety net.

## Open questions

- Does the orchestrator's `read_agent_output` frequency make lazy parsing a wash in practice? Measure with `diag` timings first.
