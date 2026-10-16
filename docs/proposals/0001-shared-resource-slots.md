# 0001. Shared resource slots

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0e, §4d, §4e, §4g, §6 (PRs 5-6)

## Problem

- Several agents compete for things only one (or a few) can use at once: the shared dev DB / Docker Postgres used by VART evals and PoL evidence runs, and the machine itself (too many agents at once).
- The rules exist only in skills and in Giovanni's head, so nothing enforces them.

## Today's workaround

- The `eval-orchestrator` skill watches worktrees and hands an "eval slot" around by polling.
- The smith orchestrator enforces a manual 2-agent cap.
- PoL evidence runs on the shared dev stack are serialized by convention ("coordinate a window first").

## Proposal

- The engine owns named **slots**, each with a capacity: `dev-db` (1), `evals` (1), `agents` (N). Config in `.lazyagent.toml`.
- Agents (via MCP) and skills (via the protocol) **acquire** a slot before a guarded step and **release** it after. Waiters queue FIFO and get an event when granted.
- The `agents` slot is enforced by `spawn_agent` itself: past the cap, the spawn is queued (or refused with `slot_busy`, caller's choice).
- Leases carry a TTL and the holder's `session_id`. A holder that exits (`agent.exited`) or lets the TTL lapse loses the slot, so a crashed agent can't block the queue.

| Kind | Name | Shape / notes |
|---|---|---|
| method | `acquire_slot {name, holder?, ttl_s?, wait?}` | Returns `{granted, position?, lease_id}` |
| method | `release_slot {lease_id}` / `list_slots` | `list_slots` shows holders and queue |
| method | `spawn_agent` | + `queue_if_capped: bool` |
| event | `slot.granted` / `slot.released` / `slot.queued` | `{name, lease_id, holder, position?}` |
| MCP | `acquire_slot`, `release_slot`, `list_slots` | Thin wrappers, so agents and skills can call them (§4g) |

## Fits after

- **PR 5** (Engine facade): spawn goes through `Engine`, so the cap can live there. Slots need the registry (PR 2) to tie leases to agents.
- **PR 6** (v1 server) exposes it to skills and scripts. Usable in hosted mode (D11) without PRs 7-9.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (retires `eval-orchestrator` and the manual cap) | M |

## Risks

- Agents forget to release: TTL + release on exit covers it, but a TTL that is too short evicts a live eval mid-run. Allow `renew`.
- Slots only guard work that asks for them; a bare `make vart.eval` in a shell bypasses them. Skills must be updated to call `acquire_slot`.

## Open questions

- Should the engine also **run** the guarded command (as a job, D6) instead of just granting a lease? Stronger guarantee, more scope.
- One engine per repo (D2): the dev DB is shared across repos. Cross-repo slots need a host-level lock file; out of scope until D2 changes.
