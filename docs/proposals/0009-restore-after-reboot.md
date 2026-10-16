# 0009. Restore everything after a reboot

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §6 (PRs 2, 8, 9), K3, K8

## Problem

- A reboot (or engine crash, K3) kills every agent. The weekday unattended boot (tmux → `lazyagent` → `/gio-planner unattended`) then has to work out from scratch what was running and restart it.

## Today's workaround

- The unattended boot redoes the work: the planner re-reads ClickUp and the board and re-spawns agents.

## Proposal

- **Mostly PR 9** (restart manifest): persist registry metadata (worktree, provider, session name, kind, label) so a fresh engine can offer `resume_mode=last` re-spawns.
- Add to it:
  - a `restore` step at engine start (opt-in via config, or on request from the boot script) that re-spawns every agent alive at shutdown, each with its provider's resume flag (e.g. `claude --resume <session>`);
  - an `engine.restored` event listing what came back and what failed, which the planner reads instead of rebuilding state.

| Kind | Name | Shape / notes |
|---|---|---|
| method | `list_restorable` | From the manifest: agents alive at last shutdown |
| method | `restore {session_ids? \| all}` | Re-spawn with resume; returns per-agent result |
| event | `engine.restored` | `{restored: [...], failed: [{session, reason}]}` |
| config | `[engine] restore_on_start` | `false` by default |

## Fits after

- **PR 9** (restart manifest), which needs **PR 2** (persisted ids, D8) and **PR 8** (detached engine, auto-start; K8 cold-start test).
- Under hosted mode first (D11), PRs 7-9 come later, so this waits for them.

## Value / Effort

| Value | Effort |
|---|---|
| ★ (overlaps PR 9) | S on top of PR 9 |

## Risks

- Resuming mid-turn: an agent that was editing when killed resumes with a stale view. It should restart with a short "you were interrupted; check git status" prompt.
- Restoring and the planner both spawning the same agent. The boot script must call `restore` first and the planner must read `engine.restored`.

## Open questions

- Restore automatically on every engine start, or only when the boot script asks?
