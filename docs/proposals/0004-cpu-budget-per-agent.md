# 0004. Per-agent CPU budget

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §3a, §4d (`spawn_agent`), §6 (PRs 4-5)

## Problem

- One agent's test run can starve everything else. Seen 2026-10-15: a vitest run with 10 workers saturated the machine, slowing every other agent and the TUI.

## Today's workaround

- None. Giovanni notices the machine is slow and finds the culprit by hand.

## Proposal

- The engine spawns every agent (`pty.fork`, pty_emulator.py:107), so it can place each agent's process group under limits at spawn time:
  - **cgroup v2** via `systemd-run --user --scope -p CPUQuota=… -p MemoryHigh=…` (no root needed on systemd hosts);
  - fallback: `nice` / `ionice` on the process group.
- **Test-worker caps:** export env hints at spawn (`VITEST_MAX_WORKERS`, `PYTEST_XDIST_AUTO_NUM_WORKERS`, `MAKEFLAGS=-j…`) so runners default to a sane number. Goes through the client env path (D5).
- **Visibility:** sample per-agent CPU/memory from the cgroup or `/proc` and expose it.

| Kind | Name | Shape / notes |
|---|---|---|
| method | `spawn_agent` | + `limits: {cpu_quota?, memory_high?, nice?}`; defaults from config |
| method | `set_limits {session_id, …}` | Adjust a running agent |
| event | `agent.resources` | `{session_id, cpu_pct, rss_mb}` at ~0.2 Hz, only while subscribed |
| config | `[limits]` | Per provider / per worktree defaults |

## Fits after

- **PR 4** (`AgentSession` owns the PTY process) and **PR 5** (spawn moves into `Engine`). Before that, spawn lives in widgets (center_panel.py) and would need the change twice.
- Display of `agent.resources` follows the clients: TUI status bar, then H2 / web.

## Value / Effort

| Value | Effort |
|---|---|
| ★★ | S (nice + env caps) / M (cgroups + metrics) |

## Risks

- `systemd-run --scope` wraps the process: the PTY still has to be the child's controlling terminal. Needs a spike.
- Env caps only help runners that read them; others ignore them.
- Hosts without a user systemd (some containers) only get `nice`.

## Open questions

- Fixed quota per agent, or a shared pool where the active/visible agent gets priority?
- Should the [0001](0001-shared-resource-slots.md) `agents` cap and CPU limits share one config block?
