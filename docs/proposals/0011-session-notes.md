# 0011. Session notes per agent

- **Status:** Accepted (open questions P1-P8 answered 2026-10-19); plan in [session-notes-plan.md](../design/session-notes-plan.md); N1 (store and lifecycle, engine only) and N2 (protocol, per-spawn token, MCP tools) landed 2026-10-19
- **Design refs:** [engine-split.md](../design/engine-split.md) §4d, §4e, §4g, §6e (manifest); [ui-plan.md](../design/ui-plan.md) §3a, §3b; [desktop-app.md](../design/desktop-app.md) §5

Each agent has one Markdown notes file, outside the worktree, that the user, the agent and the orchestrator read and edit as a shared cheat sheet. The engine owns it, re-injects it after a compaction, a `/clear` or a resume, and deletes it when the agent is gone for good.

## Problem

- What was agreed with an agent (goal, what not to touch, decisions) lives only in its terminal's scrollback. After a compaction the agent loses it or summarises it badly, and the user has to re-read the terminal to remember it.
- The orchestrator has no cheap way to know what a worker is doing or what it is unsure about: today it reads the worker's screen with `read_agent_output`.

## Today's workaround

- `CLAUDE.md`, engram or a `.specs/*-plan.md` in the worktree: they outlive the session, can be committed by mistake, and belong to no agent in particular.
- The manifest keeps `instruction`, but `send_agent_input` overwrites it on every send (`Engine.send_agent_input`, engine/engine.py:1155-1163): it is the last instruction, not the original one.
- Repeating the context by hand after every compaction or restore.

## Proposal

The engine owns the file: it creates it, keys it to an agent slot, serves it to MCP and to the clients, and announces changes. **Markdown** (decided): fewer tokens to re-inject, agents and users edit it well, egui and Textual show it, diffs are clean. HTML only as a generated view or export.

| Aspect | Design | Where in the code |
|---|---|---|
| Storage | `$XDG_STATE_HOME/lazyagent/notes/<repo hash>/<notes_id>.md` + `<notes_id>.json` (meta: worktree, agent id, conversation id, rev, by, updated_at, ended_at, pinned instruction). Never in the worktree; files 0600, dir 0700 | `engine/runtime.py:42` `state_dir()`, next to `engines/<hash>.manifest.json` |
| Key | One file **per agent slot** (worktree + agent id; the orchestrator's id is `""`). `notes_id` is stored on the manifest `Entry`, so a restore that gives the agent a new id still finds its notes. The provider's conversation id is recorded in the meta, not used as the key | `Entry` (engine/manifest.py:55), `Engine._remember` (engine.py:1271), `Engine._note_conversation` (engine.py:1302) |
| Agent access | MCP `notes_read`, `notes_append {section, text}`, `notes_replace_section {section, text, if_rev}`; never a whole-file rewrite | `mcp_server.py` + `engine/methods.py` (`EngineMethods`, both sockets) |
| Caller identity | A **per-spawn token** in the agent's MCP env (`LAZYAGENT_AGENT_TOKEN`); the engine maps it to the slot, so `notes_*` without a target means "my notes" | `_build_claude_runtime_context` mcp-config (agent_providers.py:455), `Engine._agent_command` (engine.py:974) |
| User access | Editable side panel in the desktop app (MVP), then the web page and `n` in the TUI (free today: app.py:138-159) | `desktop/crates/app/src/notes_view.rs` (new), `host/static/`, `app.py` |
| Sections | Template on creation with five editable sections: **Goal · Constraints (don't touch) · Decisions · Doubts · Pending**, checklists `- [ ]`. Free text is allowed; sections are matched by heading | `engine/notes.py` (new) |
| Pinned instruction | The first `UserPromptSubmit` prompt (also when typed in the terminal, not only via MCP) is kept in the meta and shown as a read-only header above the notes | `ClaudeHooksObserver._map_event` (agent_observers.py:162) |
| Re-injection | A second hook script on `SessionStart` (`compact`, `resume`, `clear`) prints the notes as `additionalContext`. It reads the file from disk (path in the agent's env), no socket round trip | Today the hook only appends JSONL and has no `SessionStart` (agent_providers.py:421-436, `_claude_hook_script` :484) |
| Change notice | When someone else edited since the agent last saw the notes, the same script on `UserPromptSubmit` adds "notes updated" + a short diff | Same script; see Feasibility V3 |
| Concurrency | Every write carries `rev`; the engine serialises writes and rejects a stale `if_rev` (`CONFLICT`). The engine is the only writer of the `.md`; the hook writes only its own "seen" sidecar | Atomic write as the manifest (manifest.py:135: temp + `fsync` + `os.replace`) |

### Lifecycle (P1, P3, P4, P7)

Notes live as long as the agent's conversation can come back, and **only the latest notes of a worktree's ended agents are kept**: at most one file per live agent slot, plus at most one for the worktree's newest ended agent (restorable or orphan).

| Engine event | Notes | Where |
|---|---|---|
| `spawn_agent`, new conversation (`resume_mode=new`) | New file from the template. Every ended agent's notes in that worktree are deleted (the new agent is the most recent) | `Engine.spawn_agent` → `_remember` |
| `spawn_agent` with `--continue` (`resume_mode=last`) | New file; an ended entry it consumes (Q31) keeps its notes as an orphan the panel offers to import (V5: no automatic adoption without an exact conversation id) | `_reconcile_restorable` |
| Conversation id arrives / changes (`/clear`) | Recorded in the meta; the notes follow the agent (same slot, same file) | `_note_conversation` |
| Compaction, `/clear` | Kept and re-injected | `SessionStart` hook (`compact`, `clear`) |
| `engine stop`, crash, reboot | Kept (the entry becomes restorable) | manifest.py `load` |
| Several restorable entries for one worktree | Only the newest entry's notes are kept; the others are deleted | notes GC after `Manifest.load` / `prune` |
| `restore_agents` | Follow the entry (its `notes_id`), re-injected | `SessionStart` hook (`resume`) |
| `stop_agent` | Handoff offered when non-empty (UIs; the result carries the final `notes` text for API/MCP callers), then deleted | `Engine._stop_agent` (engine.py:1078) |
| The agent exits on its own or crashes | **Orphan**, kept 7 days (`[notes] orphan_days`), recoverable from the panel, still subject to "only the latest" | `Engine._forget_agent(stopped=False)` (engine.py:1086) |
| `dismiss_restorable`, worktree removed | Deleted | `Manifest.take`, `prune` |
| Notes with no entry and no orphan right | Pruned at engine start and hourly | `notes.gc` |

### Write modes (P2, P8)

| Mode | Who writes | When |
|---|---|---|
| `manual` | The agent only when the user asks (advisory: the tool description says so), the user, the orchestrator | Effective default until the propose UI lands |
| `propose` (**default**) | The agent's writes land as pending lines (`<!-- proposed -->`) that the panel shows with Accept / Discard; the agent does not wait | Notes that help without noise |
| `free` | The agent writes directly | Unattended agents and **orchestrator workers** (`spawn_agent {notes_mode: "free"}`) |

- Writers (P8): the user (panels), the agent itself (its token), and the orchestrator (the orchestrator's token, or an MCP caller without a token, i.e. an outside Claude session such as the orchestrator skill). Another agent's token writing to a peer is refused. Orchestrator and user writes are always direct, whatever the mode.

### Protocol additions

| Kind | Name | Shape / notes |
|---|---|---|
| method | `notes_get {worktree_path, agent_id}` | `{notes_id, text, rev, size, max_bytes, by, updated_at, conversation_id, instruction, mode}` |
| method | `notes_put {worktree_path, agent_id, text, if_rev}` | User (panel); `CONFLICT` with the current `{text, rev}` when `rev` moved |
| method | `notes_append {section, text, …target, caller?}` / `notes_replace_section {section, text, if_rev, …target, caller?}` | Agent via MCP (`caller` = its token), orchestrator on another agent; `propose` mode marks the line pending |
| method | `notes_list {worktree_path?}` | Live, restorable and orphan notes (`{notes_id, state, worktree_path, agent_id, size, updated_at, expires_at?}`) for the panel's "recover" list |
| method | `notes_adopt {notes_id, worktree_path, agent_id}` / `notes_delete {notes_id}` | Recover an orphan into a live agent (replaces its notes, `if_rev`) / drop one |
| method (change) | `spawn_agent {notes_mode?}`; `stop_agent {notes?: "delete" \| "keep"}` → result `+ notes?` | `keep` makes them an orphan (UIs without a handoff yet); the result carries the final text |
| event | `notes.updated` | `{worktree_path, agent_id, notes_id, rev, size, by: user\|agent\|orchestrator, doubts?}` (`doubts` from phase N8, parsed by the engine: V7) |
| event | `notes.removed` | `{notes_id, worktree_path, agent_id, reason}` (deleted, orphaned, adopted) |
| MCP | `notes_read`, `notes_append`, `notes_replace_section` | No `worktree_path` / `agent_id` = the calling agent's own notes (token, V1) |
| config | `[notes] enabled, mode, max_bytes, orphan_days, sections` | `true`; `propose`; `8192`; `7`; the five headings |

## Feasibility in the code

Everything is feasible; three findings change the design and are already reflected above.

| # | Finding | Effect |
|---|---|---|
| V1 | The agent's MCP server only knows `LAZYAGENT_SOCKET` (mcp_server.py:163) and calls the legacy socket, which cannot tell agents apart. The agent id **is** known before the runtime context is built (`spawn_agent` allocates it, engine.py:899-901, then `_agent_command` :974), but is not passed to it | **Changes:** the engine mints a token per spawn, passes it to `build_runtime_context` (agent_providers.py:161), which writes it into the mcp-config `env`; the engine maps token → slot in memory (agents never outlive their engine, so it is not persisted). `notes_*` without a target = "my notes" |
| V2 | The Claude hook is only a JSONL logger: it prints nothing and `SessionStart` is not registered (agent_providers.py:421-436) | **Changes:** a second, stdlib-only script (`lazyagent/notes_hook.py`, copied into the hook temp dir) on `SessionStart` (`compact`, `resume`, `clear`) and `UserPromptSubmit` reads the file and returns `additionalContext`. The notes path reaches it through `env_overrides` (`LAZYAGENT_NOTES_PATH`), which Claude's hooks inherit |
| V3 | `SendMessage` is between Claude Code sessions; the engine is not one | **Changes:** "notes updated" goes through `UserPromptSubmit` (`additionalContext`) on the agent's next turn. It does not wake an idle agent; `send_agent_input` already does that |
| V4 | The conversation id arrives with the first hook event, not at spawn; `/clear` gives a new one | Keying by slot, not conversation, makes both a meta update in `_note_conversation`. Registering `SessionStart` also brings the id earlier. To verify with a test: whether `claude --resume <id>` keeps the id or forks it (same path either way) |
| V5 | A restore without a conversation id falls back to `--continue`, which two agents in a worktree share (Q27) | Notes follow `notes_id` on the entry, so a restore is exact. A plain `--continue` spawn does not adopt an ended entry's notes; the panel offers them |
| V6 | `_forget_agent` (engine.py:1086) runs both on stop and when the agent exits on its own (`stopped` false), and both call `manifest.remove` | An unexpected exit would lose the notes: they become orphans for 7 days (P3) |
| V7 | The attention queue is derived in each client: `desktop/crates/app/src/attention.rs` and `host/static/lib/attention.js` | The engine parses "Doubts" and sends them in `notes.updated`; clients only show them (no two parsers) |
| V8 | The desktop app has no Markdown widget (only `eframe`, desktop/crates/app/Cargo.toml); Textual ships `Markdown` and `TextArea` | MVP panel = a `TextEdit` over the raw Markdown; a rendered view later (`egui_commonmark`) |
| V9 | Codex has MCP (agent_providers.py:358-399) but its events come from the app-server, not hooks; Gemini has no hooks in lazyagent | Codex: tools yes, re-injection no. Gemini: to verify. Claude first; the UI says "no re-injection" for the others |
| V10 | Agent ids are never reused in a worktree (`Registry.new_agent_id`, registry.py:158, monotonic per worktree) | A fresh spawn always gets a new slot: "only the latest" must be enforced per worktree, not by overwriting a slot |

## Extra uses

| Use | How | Phase (plan) |
|---|---|---|
| The orchestrator reads its workers | `notes_read {worktree_path, agent_id}` instead of `read_agent_output`: status and doubts without reading screens | N2 (free with MCP) |
| Original instruction pinned on top | First `UserPromptSubmit` prompt in a read-only header | N3 |
| "Doubts" in the attention queue | Each `- [ ]` under Doubts is a rank-2 item (Question); the answer from the queue is written under it and checked `- [x]` | N8 |
| Handoff on stop | Before deleting: copy to the PR description, a design doc or the decisions log | N5 (desktop, copy/keep/delete), N7 (TUI, web), N8 (PR description, log) |
| Templates per task type | bug: cause / repro / test; feature: scope / decisions / phases; `spawn_agent {notes_template}` | N9 |
| HTML view / export | Generated from the Markdown, never the source | N9 |

## Fits after

- **PR 9** (manifest, §6e): the conversation id and the `agents` / `restorable` lifecycle the notes hang from; Q31 (live entries consumed). Landed.
- **D2/D3 of the desktop app** (`panes.rs`, `dialogs.rs`) for the panel and the stop handoff; **H1/U2** of the web for its panel. Landed. No new phases of those plans.

## Value / Effort

| Value | Effort |
|---|---|
| ★★ (★★★ for the orchestrator) | M (MVP: N1-N5); L with N6-N9 |

## Risks

| Risk | Mitigation |
|---|---|
| Unbounded growth eats context on re-injection | `max_bytes` (8 KiB ≈ 2k tokens): a warning in the panel and in `notes_append` at 80 %; above it the append is refused with "condense the notes" |
| User and agent edit at once | `rev` per file; the agent only appends or replaces a section with `if_rev`; the panel saves with `if_rev` and shows the conflict, never overwrites |
| Several agents in one worktree | One file per slot; "only the latest" applies only to **ended** agents, live ones each keep theirs |
| A fresh spawn deletes an ended agent's notes the user still wanted | The panel lists the worktree's orphan/restorable notes before a spawn would drop them (spawn dialog note); see open doubts in the plan |
| Other providers | Codex: tools without re-injection; Gemini to verify (V9). Shown in the UI |
| Privacy | Notes may carry secrets: 0600 under the state dir, never in the worktree or a PR unless an explicit handoff |
| Engine down | Agents do not outlive their engine (their PTY closes), so the hook never needs it; the file is on disk (atomic writes) and the next engine finds it through the manifest |
| The agent clings to stale notes | Header with the last edit's time and author; "Pending" checklists the agent ticks |
| Instruction injection through notes someone else edits | Notes are user context; only the user, the agent itself and the orchestrator write (P8); a peer agent's token is refused |

## Open questions

Answered 2026-10-19 (the sections above reflect them):

| # | Question | Decision |
|---|---|---|
| P1 | Keep notes when the same conversation is restored / resumed? | **Yes, only the latest.** At most one file per agent slot; a new agent, or several restorable entries for one worktree, keeps only the most recent entry's notes and deletes the older ones. On `/clear` they follow the agent |
| P2 | Default write mode | `propose` (`manual` until the propose UI lands); `free` for orchestrator workers |
| P3 | Agent exits on its own or crashes (V6) | Orphan notes kept 7 days, recoverable from the panel, still under P1's "only the latest" |
| P4 | `/clear` (new conversation, same agent) | Notes follow the agent |
| P5 | Size cap | 8 KiB, configurable |
| P6 | Fixed sections? | Template with the five sections on creation, editable |
| P7 | Handoff on stop | Offered whenever the notes are not empty |
| P8 | Who writes? | The user, the agent itself and the orchestrator |

Remaining doubts are listed at the end of the plan.
