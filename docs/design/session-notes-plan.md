# Session notes: implementation plan

- **Status:** **MVP complete:** N1, N2, N3, N4 and N5 landed 2026-10-19 (N3's real-Claude check pending, §5) (N0 plan merged). N6-N9 are next. Each phase gets an "As landed" note in §5 when it merges.
- **Proposal:** [0011-session-notes.md](../proposals/0011-session-notes.md) (accepted; P1-P8 answered). This plan does not repeat its rationale, only what to build, where, and how to prove it.
- **Base:** `develop` @ `c0c3ac0` (PR 9 manifest + Q31 landed; desktop D1-D6 landed).
- **MVP** = N1-N5: the notes file and its lifecycle, protocol + MCP tools, re-injection after compaction / `/clear` / resume, and the desktop panel. TUI and web panels, propose mode, the attention queue and templates come after.

## TL;DR

**The MVP (N1-N5) is complete** (2026-10-19): store and lifecycle, protocol and MCP tools, Claude hooks, the desktop panel, and the desktop's recover list, stop handoff and spawn warning. Still open from it: the real-Claude manual check of N3 (Q-N27).

| # | Branch | Scope | One-line acceptance |
|---|---|---|---|
| N0 | `plan-session-notes` | Proposal 0011 + this plan | Merged |
| N1 | `notes-n1-store` | `engine/notes.py` store, `Entry.notes_id`, lifecycle hooks in `Engine`, GC ("only the latest", orphans 7 days), `[notes]` config, `notes.updated` / `notes.removed` events | Lifecycle table of 0011 passes as Engine tests with real PTYs |
| N2 | `notes-n2-mcp` | `notes_*` methods (both sockets, v1 schemas, golden fixtures), per-spawn token, MCP tools, `spawn_agent {notes_mode}`, `stop_agent {notes}` | An agent's MCP `notes_append` with no target writes its own notes; a peer's token is refused |
| N3 | `notes-n3-hooks` | `lazyagent/notes_hook.py` on `SessionStart` (compact / clear / resume) and `UserPromptSubmit` (change notice), `LAZYAGENT_NOTES_PATH`, pinned instruction | A fake `SessionStart {source: compact}` payload prints the notes as `additionalContext`; real-Claude manual check |
| N4 | `notes-n4-desktop-panel` | Desktop side panel: raw `TextEdit`, save with `if_rev`, conflict banner, live refresh, size meter, `n` key | Edit in the panel → the agent's next turn gets the notice; a conflict never overwrites |
| N5 | `notes-n5-desktop-recover` | Desktop: orphan / restorable notes list + adopt, stop dialog handoff (copy / keep / delete), spawn-dialog warning | Crash → orphan shown and adopted; stop with notes offers the handoff |
| N6 | `notes-n6-propose` | `propose` mode: pending lines, accept / discard (engine + desktop); `propose` becomes the effective default | An agent's append in propose mode shows as pending; Accept makes it plain |
| N7 | `notes-n7-tui-web` | TUI `n` (Textual `TextArea` / `Markdown`), web panel, their stop handoff (drop `notes: "keep"`) | Three UIs edit one file without losing changes |
| N8 | `notes-n8-attention` | Doubts parsed by the engine into `notes.updated.doubts`; rank-2 attention items in desktop and web; handoff targets (PR description, decisions log) | A doubt appears as a Question and is answered from the queue |
| N9 | `notes-n9-extras` | Templates per task type, HTML export, Codex tools, Gemini check | `spawn_agent {notes_template: "bug"}` creates its sections |

Each phase is its own branch from up-to-date `develop`, merged with `--no-ff` once the full `uv run pytest` and (for desktop phases) `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test` are green. Every phase updates the docs it changes (engine-split §4d/§4e/§4g rows, desktop-app §5, this plan's §5, proposal status).

---

## 1. Ground truth (what the code does today)

| Fact | Where | Consequence for the plan |
|---|---|---|
| The manifest keeps one `Entry` per live agent (`agents`, keyed by `(worktree, agent_id)`) and the restorable set; `load` keeps the newest entry per slot | engine/manifest.py:55 (`Entry`), :187 (`load`), :206 (`prune`), :252 (`take`) | `Entry` gains `notes_id`; GC compares note metas against `agents` + `restorable` |
| `_remember` adds the live entry on every spawn (also restores) with a fresh `Entry.id` | engine/engine.py:1271 | Where a notes file is created (new) or carried (restore) |
| `_note_conversation` records the provider's id on status changes and polls | engine.py:1302 | Where the meta's `conversation_id` follows `/clear` |
| `_stop_agent` → `_forget_agent(stopped=True)`; an exit on its own → `_forget_agent(stopped=False)`; both `manifest.remove` | engine.py:1078, :1086 | Stop = delete (after the handoff), exit = orphan |
| `restore_agents` spawns with `resume_conversation` and the same agent id when free, then `manifest.update(…)` | engine.py:1420-1531 | Pass the taken entry's `notes_id` through `spawn_agent` (new private param, like `agent_id`) |
| `_reconcile_restorable` consumes entries a live agent already runs (Q31) | engine.py:1365 | A consumed entry's notes become an orphan (V5), not adopted |
| `spawn_agent` allocates `agent_id` before `_agent_command` builds the runtime context; ids are monotonic per worktree | engine.py:899-901, :974; registry.py:158 | The token and notes path can go into the runtime context at spawn; "only the latest" is per worktree |
| `build_runtime_context(worktree, socket_path, cross_session_inbound)`; Claude's writes `log_hook.py`, `hooks-settings.json`, `mcp-config.json` (env: `LAZYAGENT_SOCKET`) and sets `LAZYAGENT_CLAUDE_HOOK_LOG` | agent_providers.py:161, :405-481, :484 | New kwargs `agent_token`, `notes_path`; a second hook command; `SessionStart` registered |
| `ClaudeHooksObserver._map_event` takes `session_id` from any main-agent event, ignores unknown event names | agent_observers.py:162 | `SessionStart` lines are harmless to it; the first `UserPromptSubmit` `prompt` gives the pinned instruction |
| `EngineMethods.handlers` serve both the legacy NDJSON socket (MCP) and v1 | engine/methods.py:81, ipc.py:48, engine/server.py:568 | `notes_*` go in `EngineMethods`; v1 schemas in `protocol/schema/v1/methods.json` / `events.json` |
| The MCP server talks NDJSON to `LAZYAGENT_SOCKET` and has no idea who calls | mcp_server.py:152-176 | Reads `LAZYAGENT_AGENT_TOKEN` and sends it as `caller` |
| `Config` has `[worktree]`, `[agent]`, `[orchestrator]`, `[engine]` dataclasses from `.lazyagent.toml` | config.py:29-80 | `NotesConfig` → `[notes]`; `get_config` (`ConfigView`) exposes it |
| Desktop: `State` + `App` in app.rs; `View::{Worktrees, Attention}`; keys outside a terminal `s x c d r R g ?` (no `n`); dialogs in dialogs.rs; typed events in proto/src/types.rs `EngineEvent` | desktop/crates/app/src/app.rs:43, :810-918; proto/src/types.rs:390 | A `notes_view.rs` side panel, `n` key, `EngineEvent::NotesUpdated` |
| TUI: `n` unbound (app.py:138-159) | src/lazyagent/app.py | N7 binds it |

---

## 2. Data model (decided here, used by every phase)

- **Directory:** `state_dir() / "notes" / <repo_digest(repo_root)>` (engine/runtime.py:29, :42), 0700.
- **Files per notes id** (`uuid4().hex[:12]`, like `Entry.new_id`):
  - `<id>.md`: the editable text, 0600. Only the engine writes it (temp + `fsync` + `os.replace`, as `manifest.write`).
  - `<id>.json`: meta `{version, notes_id, worktree_path, agent_id, kind, conversation_id, rev, by, updated_at, created_at, ended_at, state: live|restorable|orphan, mode, instruction}`. Written after the `.md`, same atomic write. `rev` is an int, +1 per write.
  - `<id>.seen.json`: written **only by the hook** (`{rev, text}` the agent last saw), so the hook can say "updated" and diff. Never read by the engine except to delete it.
- **Writes off the loop:** both files are ≤ 8 KiB + meta; written via `asyncio.to_thread` under a per-id `asyncio.Lock`. `rev` is checked and bumped on the loop before the thread write (the engine is single-threaded on the loop, so it serialises writers).
- **Sections:** a heading `## <name>` (case-insensitive match against `[notes] sections`). `replace_section` replaces the body up to the next `## ` heading; `append` adds a line at the end of the section (creating the heading at the end of the file when missing). Lines starting with `- ` are kept as is; other text becomes `- <text>`.
- **Template** (P6):

  ```markdown
  ## Goal

  ## Constraints (don't touch)

  ## Decisions

  ## Doubts

  ## Pending
  ```

  A file equal to the template counts as **empty** (no re-injection, no handoff offer, GC treats it as nothing to keep).
- **Size:** `max_bytes` (default 8192) on the UTF-8 text. `notes_put` / `append` / `replace_section` over it → `VALIDATION_ERROR "notes would be N bytes (max M): condense them"`. Results carry `size` and `warning` at ≥ 80 %.
- **GC rule** ("only the latest", P1/P3), run after `open_manifest`, after `prune`, on every spawn / exit / stop / restore / dismiss / worktree removal, and hourly:
  1. Notes referenced by a live entry (`manifest.agents[*].notes_id`) are kept.
  2. Among the others **per worktree** (restorable entries' notes + orphans), keep only the newest by `ended_at` (fallback `updated_at`); delete the rest.
  3. An orphan older than `orphan_days` (7) is deleted.
  4. Notes whose worktree is gone are deleted (as `manifest.prune`).
  5. A fresh spawn (`resume_mode=new`) in worktree W deletes every non-live notes of W before creating its own.
  - Without a manifest (a second engine in the repo, tests with `Engine()`), notes work in memory-backed mode: files are still written, GC only applies rules 3-4 to files this engine created. (Simplest correct thing; see doubt Q-N4.)

---

## 3. Phases

### N1: the store and its lifecycle (engine only)

**Files**

| File | Change |
|---|---|
| `src/lazyagent/engine/notes.py` (new) | `NotesStore(root, *, max_bytes, sections, orphan_days, clock)`: `create(worktree, agent_id, kind) -> Meta`, `get(id)`, `put(id, text, if_rev, by)`, `append(id, section, text, by, if_rev=None)`, `replace_section(id, section, text, if_rev, by)`, `set_conversation(id, conv)`, `set_instruction(id, text)` (first write wins), `mark(id, state, ended_at)`, `delete(id, reason)`, `gc(live_ids, restorable: {id: (worktree, ended_at)}, exists) -> removed`, `list(worktree=None)`, `is_empty(text)`; `notes_dir(repo_root, environ)`. Stdlib only, sync file I/O helpers + async wrappers. `NotesConflict(rev, text)`, `NotesTooLarge(size, max)` |
| `src/lazyagent/engine/manifest.py` | `Entry.notes_id: str \| None = None` (old files load: unknown/missing fields already tolerated by `from_dict`) |
| `src/lazyagent/engine/engine.py` | `self.notes: NotesStore \| None` opened in `open_manifest` (and lazily without one); `spawn_agent(..., notes_id=None, notes_mode=None)` private param from restore; `_remember` stores `notes_id` (create, or carry on restore); fresh spawn → GC rule 5; `_note_conversation` → `set_conversation`; `_forget_agent(stopped=False)` → `mark(orphan)`; `_stop_agent` → `delete` unless `keep_notes`; `stop_agent(key, agent_id, *, notes="delete")` returns the final text; `restore_agents` passes `entry.notes_id`; `dismiss_restorable`, `prune` paths → GC; `_reconcile_restorable` consumed entries → `mark(orphan)`; hourly GC task next to the pollers; events `notes.updated` / `notes.removed` |
| `src/lazyagent/engine/events.py` | `NOTES_UPDATED = "notes.updated"`, `NOTES_REMOVED = "notes.removed"` constants (with the others) |
| `src/lazyagent/config.py` | `NotesConfig(enabled=True, mode="propose", max_bytes=8192, orphan_days=7, sections=[…5])`, `Config.notes`, parsing + validation (mode in `manual\|propose\|free`, `max_bytes` 1024-65536) |
| `src/lazyagent/protocol/schema/v1/events.json` | `notes.updated`, `notes.removed` definitions |

**Tests**

- `tests/engine/test_notes.py` (unit, tmp dir, fake clock): template on create; `is_empty`; put with stale `if_rev` → conflict carrying current text; append into an existing / missing section; replace_section bounds (next `##`, end of file); size cap and 80 % warning; atomic write leaves no temp file and 0600/0700 modes; corrupt meta is skipped and logged; GC rules 1-5 each with its own test (newest-per-worktree, orphan TTL, worktree gone, fresh spawn drops ended notes, live always kept).
- `tests/engine/test_notes_lifecycle.py` (real PTYs, `fake_agent.py`, the `world` fixture of test_restore.py): spawn creates notes and `Entry.notes_id`; conversation id change (set `_observer.conversation_id`, as test_restore.py:215 `_conversation`) updates the meta, same file (`/clear`, P4); engine restart + `restore_agents` keeps the same `notes_id` and text, also when the old agent id is taken (new id, same notes); `stop_agent` deletes and returns the text; `stop_agent(notes="keep")` leaves an orphan; an agent that exits on its own leaves an orphan; two restorable entries in one worktree → only the newest's notes survive the restart; a fresh spawn in that worktree deletes them; a `--continue` spawn that consumes an entry (Q31) leaves its notes as orphan; dismiss deletes; a removed worktree deletes; events emitted with `by`.
- `tests/test_config.py`: `[notes]` parsing, defaults, invalid values.

**Acceptance**

- Every row of 0011's lifecycle table is a passing test.
- No notes I/O on the loop thread (asserted as test_manifest.py does for writes).
- Full pytest green; engine-split §6e gains a "notes" bullet; §4e gains the two events.

### N2: protocol, token and MCP tools

**Files**

| File | Change |
|---|---|
| `src/lazyagent/engine/methods.py` | Handlers `notes_get`, `notes_put`, `notes_append`, `notes_replace_section`, `notes_list`, `notes_adopt`, `notes_delete`; target resolution: explicit `worktree_path` + `agent_id` (via `resolve_agent_id`, methods.py:493), else `caller` token → slot, else `VALIDATION_ERROR "no target: pass worktree_path and agent_id"`; author `by`: `agent` (token of the target), `orchestrator` (orchestrator token, or no token), refused with `VALIDATION_ERROR` for a peer agent's token. `spawn_agent` accepts `notes_mode`; `stop_agent` accepts `notes` and returns `notes` (text) when non-empty. Errors: `NotesConflict` → `CONFLICT`, a new `ErrorCode` (protocol/envelope.py:20) mapped in `classify` (methods.py:59); `classify` gains an optional `data` so the v1 error carries `{rev, text}` (the envelope already has `error.data`, envelope.py:7); the legacy socket keeps code + message ("notes changed (rev N): read them again"), enough for MCP |
| `src/lazyagent/engine/engine.py` | `_tokens: dict[str, tuple[str, str]]` minted in `spawn_agent` / `_spawn_orchestrator` (`secrets.token_urlsafe(24)`), dropped in `_forget_agent`; passed to `_agent_command` |
| `src/lazyagent/agent_providers.py` | `build_runtime_context(..., agent_token=None)`; Claude, Codex and Gemini put `LAZYAGENT_AGENT_TOKEN` into their MCP server env (mcp-config `env`, Codex `config.toml` env table, Gemini settings) |
| `src/lazyagent/mcp_server.py` | Tools `notes_read(worktree_path=None, agent_id=None)`, `notes_append(section, text, worktree_path=None, agent_id=None)`, `notes_replace_section(section, text, if_rev, worktree_path=None, agent_id=None)`, each sending `caller=os.environ.get("LAZYAGENT_AGENT_TOKEN")`. Docstrings carry the mode rule ("in manual mode write only when the user asks") and the cap |
| `src/lazyagent/engine/server.py` | Pass `classify`'s `data` into the error response; `capabilities` lists the new methods automatically (server.py:716) |
| `src/lazyagent/protocol/envelope.py`, `schema/v1/envelope.json` | `ErrorCode.CONFLICT`; the code list in the schema description |
| `src/lazyagent/protocol/schema/v1/methods.json` | Params/results of the seven methods; `spawn_agent.params.notes_mode`; `stop_agent.params.notes`, `stop_agent.result.notes` |
| `src/lazyagent/orchestrator_prompt.py` | One paragraph: read workers with `notes_read`, spawn workers with `notes_mode: "free"` (P2) |

**Tests**

- `tests/protocol/golden_cases.py` + `tests/protocol/fixtures/ipc/notes_get__template.json`, `notes_put__conflict.json`, `notes_append__by_token.json`, `notes_append__too_large.json`, `notes_append__peer_refused.json`, `notes_list__orphan.json`, `stop_agent__with_notes.json` (regenerate with the golden tool, strict schema validation).
- `tests/contract/test_notes.py` (v1 harness): get/put round trip, `notes.updated` after a put, `CONFLICT` shape, `notes_list` after a crash (`harness.restart()`), `notes_adopt` replaces and emits `notes.removed {reason: "adopted"}`.
- `tests/test_mcp_server.py`: the three tools forward `caller` from the env and the optional target; no env token → no `caller`.
- `tests/test_agent_providers.py`: the token lands in Claude's mcp-config env, Codex's config.toml and Gemini's settings; absent when not given.
- `tests/engine/test_notes_lifecycle.py`: a token resolves to its slot, dies with the agent, is new after a restore.

**Acceptance**

- An agent calling MCP `notes_append {section: "Decisions", text: "use sqlite"}` with no target writes its own notes; `by: agent`.
- The orchestrator (its token, or an outside session with none) writes another agent's notes; a peer agent's token is refused.
- `stop_agent` over MCP returns the final notes text (the orchestrator's handoff).
- engine-split §4d / §4g rows added; golden fixtures and schemas strict-validated.

### N3: hooks: re-injection, change notice, pinned instruction

**Files**

| File | Change |
|---|---|
| `src/lazyagent/notes_hook.py` (new) | Stdlib-only script (no lazyagent imports; copied, not imported, so a pipx install works like `log_hook.py`). Reads the payload; `LAZYAGENT_NOTES_PATH` → `<id>.md` + `<id>.json`. **SessionStart** with `source` in `compact`, `clear`, `resume` (and `startup` when non-empty: a restore with `--resume` may report `startup`, see Q-N1): prints `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<header + notes>"}}`; header = "Session notes (lazyagent; last edit <time> by <by>; edit with notes_* tools)" + pinned instruction. **UserPromptSubmit**: if `meta.rev` > `seen.rev` and the last writer is not `agent`, prints "Your session notes were updated by <by>:" + a unified diff capped at ~1.5 KiB (`difflib`), then writes `<id>.seen.json`. Always exits 0, prints nothing on any error (a broken notes file must never block a prompt) |
| `src/lazyagent/agent_providers.py` | `build_runtime_context(..., notes_path=None)`; Claude: copy `notes_hook.py` into the temp dir, register it on `SessionStart` (all sources; the script filters) and add it as a second command on `UserPromptSubmit`; also log `SessionStart` with the existing logger (earlier conversation id, V4); `env_overrides["LAZYAGENT_NOTES_PATH"]` |
| `src/lazyagent/engine/engine.py` | Pass `notes_path` to `_agent_command`; on the first `UserPromptSubmit` prompt (from the observer) call `notes.set_instruction` |
| `src/lazyagent/agent_observers.py` | `ClaudeHooksObserver.first_prompt: str \| None` from the first main-agent `UserPromptSubmit` `prompt` (clipped to `INSTRUCTION_LIMIT`); `AgentObserver.first_prompt = None` default; `CompositeObserver` forwards it |
| `src/lazyagent/engine/session.py` | `AgentSession.first_prompt` property next to `conversation_id` (session.py:838) |

**Tests**

- `tests/test_notes_hook.py`: run the script as a subprocess with fake payloads: `SessionStart` compact / clear / resume print valid JSON with the notes; startup with template-only notes prints nothing; missing env / missing file / corrupt meta → exit 0, no output; `UserPromptSubmit` after a user edit prints the diff once (second prompt silent), after the agent's own edit prints nothing; diff cap; runs in < 150 ms (cold Python).
- `tests/test_agent_providers.py`: `SessionStart` registered, two commands on `UserPromptSubmit`, script copied and executable, `LAZYAGENT_NOTES_PATH` in `env_overrides`.
- `tests/test_agent_observers.py`: `first_prompt` from the first main `UserPromptSubmit`, not from a subagent's, not overwritten by later prompts; `SessionStart` sets `conversation_id`.
- `tests/engine/test_notes_lifecycle.py`: the pinned instruction reaches the meta from a fake hook line written to `LAZYAGENT_CLAUDE_HOOK_LOG`.

**Acceptance**

- Automated: the four payload cases above.
- **Manual, real Claude** (documented in §5 with the transcript): a scratch repo, spawn an agent, it `notes_append`s a decision, `/compact` → ask "what is in your session notes?" → it answers from the injected context; `/clear` → same; `lazyagent engine stop` + `lazyagent engine restore --all` → same; the user edits the file through `notes_put` → the next prompt shows the notice. Also check V4: does `--resume <id>` keep the id (record it in Q27's row)?

### N4: desktop panel

**Files**

| File | Change |
|---|---|
| `desktop/crates/proto/src/types.rs` | `Notes {notes_id, text, rev, size, max_bytes, by, updated_at, conversation_id, instruction, mode}`, `NotesPutResult`, `NotesUpdated`, `NotesRemoved`; `EngineEvent::NotesUpdated` / `NotesRemoved` in `parse` |
| `desktop/crates/proto/src/envelope.rs` | Surface `CONFLICT` error `data` (`{rev, text}`) to callers if not already carried |
| `desktop/crates/app/src/notes_view.rs` (new) | Right `SidePanel` for the active agent: pinned instruction (read-only, collapsible), a monospace multiline `TextEdit` over the raw Markdown (V8), size meter (amber ≥ 80 %, red at cap), "last edit <time> by <by>", Save (Ctrl+S), Revert. State per agent: `base_rev`, `base_text`, `draft`, `dirty`. On `notes.updated` with a newer rev: not dirty → reload silently; dirty → a banner "Changed by <by> — Reload (lose mine) / Keep editing (save will conflict)". Save → `notes_put {if_rev: base_rev}`; `CONFLICT` → keep the draft, show the engine's text below it and "Overwrite with mine" (re-put with the new rev) / "Take theirs" |
| `desktop/crates/app/src/app.rs` | `State.notes_open`, per-agent notes cache; `n` (outside a terminal) and a header button "Notes" toggle the panel; fetch on open / agent switch (`notes_get`), events routed in `on_event`; the panel's `TextEdit` focus counts as `text_edit_focused` so the single-letter keys stay off; `?` sheet row from the key table |
| `desktop/crates/app/src/dialogs.rs` | Key table entry for `n` |
| `docs/design/desktop-app.md` | §5 table: notes row; §10: "As landed" note |

**Tests**

- `desktop/crates/proto/tests/golden.rs`: every `notes_*` fixture deserialises; `notes.updated` / `notes.removed` exist in `events.json` (schema-names test).
- `desktop/crates/proto/tests/engine.rs` (real engine): spawn → `notes_get` returns the template; `notes_put` → `notes.updated` received; stale put → `CONFLICT` with `data.rev`.
- `desktop/crates/app/src/tests.rs` (kittest, fake store): `n` opens/closes the panel and is ignored inside a terminal; typing in the panel does not trigger `s`/`x`; dirty + incoming update shows the banner; conflict keeps the draft and shows both; size meter colours at 80 % / 100 %. Against a real engine: open notes, type, Ctrl+S, read the file on disk.

**Acceptance**

- Edit in the panel → the agent's next turn shows "notes updated" (N3); a conflict never overwrites text silently; panel open adds no measurable cost to B1 (re-run `--bench echo`, numbers in §5).

### N5: desktop recover and stop handoff

**Files**

| File | Change |
|---|---|
| `desktop/crates/app/src/notes_view.rs` | "Earlier notes in this worktree" list from `notes_list {worktree_path}` (orphan / restorable, size, age, expiry): View (read-only), **Adopt into this agent** (`notes_adopt`, confirms when the current notes are not empty), Delete |
| `desktop/crates/app/src/dialogs.rs` | **Stop** dialog: when the agent's notes are not empty, three choices: **Copy notes and stop** (clipboard, then `stop_agent {notes: "delete"}`), **Stop, keep notes 7 days** (`notes: "keep"`), **Stop and delete notes**. **Spawn** dialog: a one-line warning when the worktree has ended notes that a new conversation would delete (rule 5), with a link to the panel list |
| `desktop/crates/app/src/app.rs` | Banner/toast on `notes.removed {reason: "orphaned"}` for the visible worktree ("Agent a2 exited; its notes are kept 7 days") |
| `desktop/crates/proto/src/types.rs` | `NotesList`, `NotesListItem`, `StopResult.notes` |

**Tests**

- kittest: the stop dialog shows the three choices only with non-empty notes; "Copy" puts the text on the (fake) clipboard; the spawn warning appears only with ended notes.
- Real engine (`desktop/crates/app/src/tests.rs`): kill a fake agent's process (exit on its own) → the list shows the orphan → Adopt into a new agent → the text is there and the orphan is gone; stop with "keep" leaves an orphan.

**Acceptance**

- The MVP demo end to end in the desktop app: write notes, compact, restart engine, restore, see notes; stop with handoff. This closes the MVP; proposal 0011 status → "MVP landed".

### N6: propose mode

**Files**

| File | Change |
|---|---|
| `src/lazyagent/engine/notes.py` | In `propose` mode an agent's append/replace writes `- [ ] <text> <!-- proposed:<pid> -->` (replace: the whole new section body as one proposed block, see Q-N3); `accept(id, pid)` strips the marker; `discard(id, pid)` removes it; proposals count toward the cap; re-injection and the hook show them marked "(proposed, not accepted)" |
| `src/lazyagent/engine/methods.py`, schema | `notes_accept {…target, proposal_id}`, `notes_discard {…}`; `notes.updated.proposals: [{id, section, text}]` |
| `src/lazyagent/notes_hook.py` | Render pending lines as proposed |
| `desktop/crates/app/src/notes_view.rs` | Pending proposals above the editor with Accept / Discard; the raw editor shows the markers read-only (see Q-N3) |
| `src/lazyagent/config.py` | Default `mode = "propose"` now effective (before N6, `propose` behaves as `manual`; documented in N1) |

**Tests:** unit (accept/discard, cap, markers survive a user `put` that does not touch them), contract (`notes_accept` → `notes.updated`), hook rendering, kittest Accept/Discard.

**Acceptance:** an agent in propose mode appends → the panel shows a pending line → Accept → plain line; workers spawned by the orchestrator in `free` write directly.

### N7: TUI and web panels

**Files**

| File | Change |
|---|---|
| `src/lazyagent/widgets/notes_panel.py` (new) | Textual modal/side panel: `TextArea` (markdown language), `Markdown` preview toggle, save with `if_rev`, conflict view, pending proposals; the same states as the desktop |
| `src/lazyagent/app.py` | `Binding("n", "notes", "Notes")`; the stop confirm (`widgets/confirm_modal.py`) gets the handoff choices (copy via OSC 52 / write to a file under the state dir); remote mode through `backend/` (the `Backend` methods mirror the new engine methods) |
| `src/lazyagent/backend/` | `notes_get` / `notes_put` / … on the local and remote backends |
| `src/lazyagent/host/static/components/` (new `notes.js`), `lib/store.js`, `lib/keymap.js`, `app.js` | Web panel (textarea, save, conflict, proposals), `n` key, stop dialog handoff |

**Tests:** Textual Pilot (`tests/test_notes_panel.py`: open with `n`, edit, save, conflict, stop handoff); `tests/host/` WebSocket test for the methods; browser test (`tests/browser.py` harness) for the panel; once all three UIs offer the handoff, UIs stop sending `notes: "keep"` by default.

**Acceptance:** desktop, TUI and web edit the same file concurrently without losing changes (a scripted test: two clients put with the same `if_rev`, one gets `CONFLICT`).

### N8: doubts in the attention queue, handoff targets

**Files**

| File | Change |
|---|---|
| `src/lazyagent/engine/notes.py` | `doubts(text) -> [{index, text, done}]` from the Doubts section; `notes.updated.doubts`; `answer_doubt(id, index, answer, by)` writes `  - answer: …` under it and ticks `- [x]` |
| `src/lazyagent/engine/methods.py`, schema, MCP | `notes_answer_doubt {…target, index, text}`; optional `send_to_agent: true` also types the answer into the agent (`send_agent_input`) |
| `desktop/crates/app/src/attention.rs`, `attention_view.rs` | Open doubts → rank-2 Question items; the reply box calls `notes_answer_doubt` |
| `src/lazyagent/host/static/lib/attention.js` | Same |
| Handoff targets (desktop + web stop dialog) | "Append to the PR description" (`gh pr edit --body-file` through a new engine job, like custom create) and "Append to the decisions log" (path from `[notes] handoff_log`, optional) |

**Tests:** parser unit tests; `cargo test` attention ranks with doubts; JS attention test; contract test for the answer; the PR handoff job with a fake `gh`.

**Acceptance:** a `- [ ]` under Doubts appears as a Question, is answered from the queue, and the agent sees the answer on its next turn.

### N9: templates, export, other providers

- `[notes.templates.<name>]` sections + `spawn_agent {notes_template}` (desktop/TUI/web spawn dialogs get a picker); `notes_export {format: "html"}` (stdlib Markdown subset → HTML, never stored as source); Codex: tools already work (N2), document "no re-injection"; Gemini: check whether its CLI has a usable hook/context mechanism and record the result.
- Tests: template creation, export escaping (no raw HTML from notes), provider env tests.

---

## 4. Cross-cutting rules

- **Never in the worktree:** no phase writes notes under a worktree; a test in N1 asserts the store root is outside `repo_root`.
- **Additive protocol:** every method/field/event is new; old clients ignore them (§4c). `notes_*` are in `capabilities`, so the desktop hides the panel against an older engine (R10 of desktop-app).
- **The hook never blocks:** any exception → exit 0 with no output; a test feeds it garbage.
- **No notes I/O on the event loop thread**, as the manifest.
- **Docs per phase:** engine-split §4d/§4e/§4g, desktop-app §5/§10, ui-plan where the web panel lands, this plan's §5, proposal 0011's status line.

---

## 5. As landed

### N1: the store and its lifecycle (`notes-n1-store`, 2026-10-19)

- **Store** (`engine/notes.py`, stdlib only): `NotesStore` keeps every meta and text in memory; a change bumps `rev` on the loop and schedules that id's write in a worker thread (`.md` then `.json`, temp + `fsync` + `os.replace`, dir 0700, files 0600), one write in flight per id, a burst coalesced into one write; without a running loop it writes at once (unit tests). `flush()` waits for pending writes; `Engine.stop` calls it. The API is the planned one plus `revive(id, worktree, agent_id)` (carry on restore), `delete_ended(worktree, live)` (rule 5), `path(id)` (for N3's `LAZYAGENT_NOTES_PATH`), `expires_at(meta)`, and `prune_engine_dirs` (Q-N10). Results are `Notes(meta, text, max_bytes)` with `size`, `warning` (≥ 80 %), `empty`. Errors: `NotesConflict(rev, text)`, `NotesTooLarge(size, max)`, both `NotesError(ValueError)`; message "notes would be N bytes (max M): condense them". Loading at engine start is synchronous, like `Manifest.load` (Q-N14); corrupt or half-written metas are skipped and logged.
- **Engine:** the lock holder opens the store in `open_manifest` (load, then GC); an engine without a manifest gets its own on the first spawn (Q-N10). `self._notes_ids` maps each live slot to its notes id, set before `session.start()`, so every exit path finds it. `spawn_agent(..., notes_id=None, notes_mode=None)`: a restore passes `entry.notes_id` and the notes are revived under the (possibly new) agent id; a fresh conversation (`resume_mode=new`, no `notes_id`) first deletes the worktree's ended notes (rule 5); else new notes from the template. `Entry.notes_id` is recorded in `_remember`. `_note_conversation` (now run with or without a manifest) records the conversation id in the meta. `_forget_agent` orphans the notes when the agent exited on its own, deletes them on a stop; `stop_agent(key, agent_id, *, notes="delete"|"keep")` keeps returning `AgentInfo`, `stop_agent_with_notes` returns `(info, final text or None)` (Q-N11). Consumed entries (Q31, in `_reconcile_restorable` and inside a restore) orphan their notes; `dismiss_restorable` and `remove_worktree` delete them. GC runs after `open_manifest`, every spawn, exit, stop, dismiss, restore (once, at its end: not while it runs, its taken entries are not live yet), `list_restorable` when it pruned, and hourly (`NOTES_GC_INTERVAL`).
- **GC as built:** live ids kept (state `live`); a gone worktree's notes deleted; notes of restorable entries marked `restorable` with the entry's `ended_at`; other non-orphans deleted (no right); orphans past `orphan_days` deleted; ended notes that are only the template deleted (Q-N12); then the newest per worktree is kept, ties broken by the newest agent (Q-N13). Without a manifest only the TTL, gone worktrees and empties apply.
- **Events:** `notes.updated {worktree_path, agent_id, notes_id, rev, size, by}` on every text write (not on create: `agent.spawned` is the notice, the template is `rev 0`, `by null`); `notes.removed {notes_id, worktree_path, agent_id, reason}` with `deleted`, `orphaned` (live → orphan) and `pruned` (GC, Q-N16). In `events.json`, `protocol.EVENTS`, and the desktop's `IGNORED_EVENTS` until N4. `restorable_agent` gained an optional `notes_id` (schema, golden fixture, desktop `RestorableAgent`).
- **Config:** `[notes] enabled, mode, max_bytes (1024-65536), orphan_days (≥ 0), sections`; an invalid value keeps its default. `enabled = false` creates no notes at all. `propose` is the default and behaves as `manual` until N6 (nothing writes as an agent before N2 anyway). Not yet in `get_config`: no client needs it before N4.
- **Tests:** tests/engine/test_notes.py (27: template and sections, `is_empty`, put/conflict, append into existing/last/missing sections, indented lines, replace_section bounds, cap and warning, 0600/0700 and no temp file, delete with the hook's sidecar, load round trip with corrupt metas logged, writes off the loop thread and coalesced, GC rules 1-5 one test each plus updated_at fallback, unreferenced/empty, no-manifest mode, orphan event once, `prune_engine_dirs`, `notes_dir`); tests/engine/test_notes_lifecycle.py (22, real PTYs: every row of 0011's lifecycle table except the hook rows (N3), plus the orchestrator (Q-N9), a failed restore, no manifest, `enabled = false`, writes off the loop thread); tests/test_config.py (+4). Full `uv run pytest` green; desktop `cargo fmt --check`, `clippy -D warnings`, `cargo test` green.

### N2: protocol, token and MCP tools (`notes-n2-mcp`, 2026-10-19)

- **Methods** (`EngineMethods`, so both sockets; also in `protocol.LEGACY_METHODS`): `notes_get`, `notes_put`, `notes_append`, `notes_replace_section`, `notes_list`, `notes_adopt`, `notes_delete`, as planned, plus `notes_get {notes_id}` to read ended notes (Q-N18). Target resolution: `worktree_path` (+ `agent_id` via `resolve_agent_id`; the orchestrator is `worktree_path: "__orchestrator__"`), else the `caller` token's slot, else `VALIDATION_ERROR "no target: pass worktree_path and agent_id"`; `agent_id` alone is refused. Writer: the target's own token → `agent`; the orchestrator agent's token or no token → `orchestrator` (Q-N5); another agent's token → `VALIDATION_ERROR "An agent may only write its own notes…"`, but it may read (Q-N17); a token the engine does not know (its agent is gone) → `VALIDATION_ERROR` (Q-N20). `notes_put` and `notes_adopt` without a token write as `user` (Q-N19). Write results: `{worktree_path, agent_id, notes_id, rev, size, max_bytes, by, updated_at, warning?}`; `notes_get` adds `text, state, kind, mode, conversation_id, instruction, created_at, ended_at, empty`; `notes_list` gives `{notes: [{notes_id, state, worktree_path, agent_id, kind, size, empty, by, updated_at, ended_at, expires_at}]}` newest first. `notes_adopt` puts the ended text into the live agent's own notes (its id and path stay, for N3) and deletes the ended ones (`notes.removed {reason: "adopted"}`, `ADOPTED` in notes.py) through `Engine.adopt_notes`; `notes_delete` → `Engine.delete_notes`; both refuse a live agent's notes. `[notes] enabled = false` → `VALIDATION_ERROR` on every notes method.
- **Errors:** `ErrorCode.CONFLICT` (Python, schema description, desktop `codes::CONFLICT`). `classify` returns `(code, message, data)`: `NotesConflict` → `CONFLICT` with `{rev, text}`; the v1 server sends it as `error.data`, the legacy socket drops it (the MCP client shows `[CONFLICT] notes changed (rev N): read them again`).
- **spawn / stop:** `spawn_agent {notes_mode}` (checked against `MODES`); `stop_agent {notes: "delete"|"keep"}` through `stop_agent_with_notes`; the result has `notes` only when the final text was not empty. MCP `spawn_agent(notes_mode=)` and `stop_agent(notes=)` too.
- **Token:** `Engine._tokens` (token → slot), `secrets.token_urlsafe(24)` minted per spawn (agents and the orchestrator; a restore is a spawn, so a new one), registered once the command is built, dropped in `_forget_agent` (stop and own exit) and on `Engine.stop` (which does not go through it). `Engine.token_slot(token)`. `build_runtime_context(..., agent_token=None)`: `_mcp_env` puts `LAZYAGENT_AGENT_TOKEN` (`AGENT_TOKEN_ENV`) in Claude's mcp-config, Codex's `config.toml` env table and Gemini's settings; never in the agent's own env.
- **MCP:** `notes_read`, `notes_append`, `notes_replace_section` send `caller` from `LAZYAGENT_AGENT_TOKEN` when set; docstrings carry the manual-mode rule, the cap and the exact headings (Q-N22). The orchestrator prompt gained a "Session notes" paragraph (workers with `notes_mode="free"`, `notes_read` instead of the screen, the `stop_agent` handoff).
- **Desktop:** only what the shared fixtures force: `StopResult.notes: Option<String>`, `codes::CONFLICT`, and the golden test skips `notes_*` fixtures until N4 types them.
- **Tests:** golden fixtures (`notes_get__template`, `notes_put__written`, `notes_put__conflict`, `notes_append__by_token`, `notes_append__too_large`, `notes_append__peer_refused`, `notes_replace_section__written`, `notes_list__orphan`, `notes_adopt__adopted`, `notes_delete__orphan`, `stop_agent__with_notes`; `CONFLICT` joins the legacy codes); the generator now sets its own `XDG_STATE_HOME` (run outside pytest, its manifest engines had been writing notes of the fake `/tmp/repo` into the real state dir; removed). tests/contract/test_notes.py (9: round trip and `notes.updated`, `CONFLICT` data, the cap, stop handoff, own exit → orphan → `notes_list` → `notes_get {notes_id}` → adopt → `notes.removed adopted`, restart → restorable → delete, live notes refused, `notes_mode`, no target / forged token); tests/test_mcp_notes.py (4, the acceptance end to end: an agent's MCP server with the token from its real mcp-config appends to its own notes, `by: agent`, `CONFLICT` on a stale `if_rev`; a peer reads but cannot write; the orchestrator writes a worker's by token and with none, `stop_agent` returns the notes; a dead agent's token is refused); tests/test_mcp_server.py (+4), tests/test_agent_providers.py (+7), tests/engine/test_notes_lifecycle.py (+2: a token resolves to its slot, dies with a stop or an own exit; a restore gets a new one). Full `uv run pytest` 1546 passed; desktop fmt, clippy `-D warnings`, `cargo test` green.

### N3: hooks: re-injection, change notice, pinned instruction (`notes-n3-hooks`, 2026-10-19)

- **Hook** (`src/lazyagent/notes_hook.py`, stdlib only, no lazyagent imports; copied as is into each Claude agent's hooks dir next to `log_hook.py`, shebang `python3`, executable). `LAZYAGENT_NOTES_PATH` = `<id>.md`; it reads `<id>.json` first (the engine writes the `.md` before it) and writes only `<id>.seen.json` (`{rev, text}`, 0600, atomic; the store already deleted it with the notes). Subagent payloads (`agent_id`) are ignored.
  - `SessionStart` with `source` in `startup`, `resume`, `clear`, `compact`, when the text is not the template (the same `is_empty` as the store, so a fresh spawn prints nothing and a restore reporting `startup` does, Q-N6): `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": …}}` = "Session notes (lazyagent; last edit <local time> by <by>; edit with the notes_* tools, <rule>):" + "Original instruction (pinned):" + the notes. `<rule>`: "keep them current" in `free` mode, else "write to them only when the user asks". It also records the rev as seen, so the next prompt does not repeat it.
  - `UserPromptSubmit`: `meta.rev` > seen rev → seen is updated; a notice only when the last writer is not `agent` (nor `null`) and the text differs from the seen one: "Your session notes were updated by <by> (<time>):" + a `difflib` unified diff (1 line of context) capped at `DIFF_LIMIT` 1536 bytes ("… (diff clipped: read them with notes_read)"). No seen file = rev 0 and an empty text (a user's edit before the first prompt shows all of it).
  - Any error → exit 0, nothing printed (garbage, missing env/file, corrupt meta all tested). Cold run ≈ 50-70 ms here (system `python3`); the test asserts < 150 ms.
- **Providers:** `build_runtime_context(..., notes_path=None)`; Claude only (`NOTES_PATH_ENV` in `env_overrides`, metadata `notes_hook_path`). `SessionStart` is now always registered and logged (V4: the conversation id arrives before the first prompt and right after a `/clear`); the notes hook is the second command on `SessionStart` (no matcher, the script filters) and `UserPromptSubmit`. Codex and Gemini get no path (no re-injection, V9).
- **Engine:** `_open_notes` now runs before `_agent_command`, so the notes path is known when the hooks are written; a command that cannot be built ends those notes as an orphan (a template is pruned at once, Q-N15). A revive or an adopt keeps the notes id, so the path stays. The pinned instruction: `ClaudeHooksObserver.first_prompt` (the first main-agent `UserPromptSubmit` `prompt`, non-blank, `clip_instruction`), `AgentObserver.first_prompt = None`, `CompositeObserver` forwards it, `AgentSession.first_prompt`; `_note_conversation` pins it (`set_instruction`, first wins) only for notes a fresh conversation created (`_unpinned`, Q-N24). Providers other than Claude pin the spawn instruction (Q-N8). The GC keeps restorable notes whose only content is a pinned instruction (Q-N23).
- **Tests:** tests/test_notes_hook.py (26, the script as a subprocess: the four `SessionStart` sources, `free` mode, template-only (also with an instruction) silent, unknown source / event / subagent, garbage payloads, missing env / files, corrupt metas, a user edit noticed once, the first prompt after an edit, the agent's own edits silent and moving seen, nothing new, a `.md` newer than its `.json`, the diff cap, 0600 and no temp file, < 150 ms and no lazyagent imports); tests/test_agent_providers.py (+6: registration, copy and executable, env, none without notes, not for Codex/Gemini); tests/test_agent_observers.py (+7: first prompt, not a subagent's, not overwritten, blank skipped, clipped, `SessionStart` sets the conversation id, composite); tests/engine/test_notes_lifecycle.py (+7, real PTYs: pinned from a fake hook line in `LAZYAGENT_CLAUDE_HOOK_LOG` and kept across a restart and restore, a `--continue` spawn pins nothing, an instruction alone survives a restart, the spawn instruction without hooks, **the agent's own copy of the hook prints the notes the engine wrote for `SessionStart {source: compact}` and notices a user's append once**, the same path after a restore, a command that cannot be built); tests/engine/test_notes.py (+1, Q-N23). Full `uv run pytest`: 1593 passed. No protocol or schema change (`instruction` was already in `notes_get`), so no desktop run.
- **Real-Claude check: not done.** Driven through an isolated engine (`XDG_STATE_HOME` / `XDG_RUNTIME_DIR` in a temp dir, a scratch repo), Claude Code 2.1.303 stopped at its workspace-trust dialog for the new folder (also inside an already trusted worktree). Accepting it writes the user's `~/.claude.json`, which this phase must not do, so the steps of the N3 acceptance (append, `/compact`, `/clear`, engine stop + restore, a `notes_put` notice; V4 `--resume <id>`) stay for a run in a folder the user already trusts. Nothing was written to `~/.claude*` or `~/.local/state`.

### N4: desktop panel (`notes-n4-desktop-panel`, 2026-10-19)

- **Types** (`desktop/crates/proto/src/types.rs`): `Notes` (`notes_get`), `NotesWritten` (the shared result of `notes_put` / `append` / `replace_section` / `adopt`, the schema's `notes_written`; the plan's `NotesPutResult`), `NotesUpdated`, `NotesRemoved`, `EngineEvent::NotesUpdated` / `NotesRemoved`; also `NotesList`, `NotesListItem`, `NotesDeleted` already, so every `notes_*` fixture decodes (Q-N30). `NotesConflict::of(&ClientError)` reads a `CONFLICT`'s `{rev, text}`: `RpcError.data` already carried it, so `envelope.rs` is unchanged.
- **Panel** (`desktop/crates/app/src/notes_view.rs`): a right `Panel` for the active tab of the selection (the orchestrator too), shown in the Worktrees view. Pinned instruction (collapsible, read-only, selectable), a monospace multiline `TextEdit` over the raw Markdown, size meter on the draft (`meter`: amber ≥ 80 %, red at 100 %, Save disabled only above the cap, Q-N32), "last edit <age> ago by <by> · rev N · mode M", Save (Ctrl+S), Revert. `NotesDoc` per (worktree, agent): `meta` (last `notes_get`), `base_rev`, `base_text`, `draft`, `latest` (the newest rev an event announced), `incoming`, `conflict`, `loading` / `saving` / `stale` / `error`.
  - `notes.updated` only raises `latest`; the shown agent's notes are fetched when `latest` passes the fetched rev (lazily: an agent not on screen is fetched when shown, Q-N31). A fetch into a clean draft (or one equal to the engine's text, e.g. the echo of our own save) replaces it; into a dirty one it becomes `incoming`: the banner "Changed by <by> while you edit (rev N)" with **Reload (lose mine)** / **Keep editing (save will conflict)**.
  - Save = `notes_put {if_rev: base_rev}`. `CONFLICT` keeps the draft, shows the engine's text below it ("Theirs (rev N)") with **Overwrite with mine** (re-put with their rev) / **Take theirs**; any other error (the cap) is shown, the draft kept.
  - `notes.removed` for the shown notes drops the doc; a dirty draft then goes with a warning toast (Q-N28). A reconnect marks every doc stale; a new engine process drops the clean ones.
- **App** (`app.rs`): `State.{capabilities, notes_open, notes_focus, notes}`; `hello.capabilities` without `notes_get` hides the header button and makes `n` a toast (R10). `n` outside a terminal (Worktrees view) and the header's **Notes** toggle the panel; `n` focuses the editor (Q-N29). The editor is a text field, so `ctx.text_edit_focused()` already keeps `s`/`x`/… off; Ctrl+S is consumed by the panel. `dialogs.rs`: `N` and `Ctrl+S` rows in `KEYMAP` (the `?` sheet).
- **Acceptance.** "Edit in the panel → the agent's next turn gets the notice": `notes_edited_in_the_panel_reach_the_file_and_the_agents_next_turn` (real engine) types into the panel, Ctrl+S, waits for the engine's `.md` to equal the draft, then runs N3's `notes_hook.py` on that file with a `UserPromptSubmit` payload, as Claude would: it prints "Your session notes were updated by user" and the typed line. Not with a real Claude (Q-N27 still applies). "A conflict never overwrites": the same test then writes as another client, the panel's save gets `CONFLICT`, the draft and the file both stay. B1 with the panel open (`LAZYAGENT_BENCH_NOTES=1 --bench echo`, ~6 KiB of notes, release build, three runs each on a busy machine): key → frame built p50 / p95 6.1-9.4 / 11.6-16.9 ms closed, 7.3-8.0 / 13.9-15.2 ms open; frame build 0.8-2.4 ms either way: no difference above the noise. One early run with the panel open hung until its 120 s timeout and did not happen again in three more.
- **Tests:** proto: `types.rs` (+1, the conflict data), `golden.rs` (every `notes_*` fixture round-trips; `notes.updated` / `notes.removed` now `KNOWN_EVENTS`, `notes_get` / `notes_put` in `METHODS_USED`), `engine.rs` (+1 real engine: capabilities, template, put → `notes.updated {by: user}`, stale put → `CONFLICT` with `{rev, text}`, the file untouched). App: `notes_view.rs` (3 unit: clean/dirty reloads and our own echo, save/conflict/take/cap error, meter levels); `tests.rs` (6 kittest: `n` goes to a focused terminal and opens the panel outside one, focused editor, typing `sxn` opens no dialog, Esc + `n` closes, header button, Revert; hidden against an engine without notes; the dirty-draft banner from a real `notes.updated` event, Keep editing / Reload; the conflict view with both texts, Overwrite (fails without an engine, draft kept) / Take theirs; the meter at 79 / 80 / 100 / 101 of 100 bytes; plus the real-engine test above). `cargo fmt --check`, `clippy --all-targets -D warnings`, `cargo test` (with `LAZYAGENT_REQUIRE_ENGINE=1`) green; full `uv run pytest` 1593 passed (no Python change).

### N5: desktop recover and stop handoff (`notes-n5-recover-handoff`, 2026-10-19)

- **Desktop only**: no engine, protocol or schema change. `NotesList`, `NotesListItem`, `StopResult.notes` were already typed (N2, Q-N30).
- **Earlier notes** (`notes_view.rs`): `State.earlier` per worktree (or the orchestrator key) = `notes_list {worktree_path}` without the live ones, newest first. The panel shows "Earlier notes in this worktree (N)" under the editor (a fixed share of its height, the list scrolls): per item "Agent a2", "orphan, 2.0 KiB, ended 3h ago, deleted in 7d", **View** / Hide (`notes_get {notes_id}`, read-only, selectable), **Adopt into this agent** (`notes_adopt {if_rev: base_rev}`; asks "Replace this agent's notes with them?" unless they are only the template and the draft is clean; a confirmed adopt drops the draft and loads the adopted text), **Delete** (`notes_delete`, always asks, Q-N38). A worktree with no live agent still lists them (Adopt disabled: "Spawn an agent here first"). Refetched when shown and stale: any `notes.removed` of the worktree, `agent.spawned` there (a restore revives notes without a `notes.removed`), `restorable.changed`, a reconnect; one request at a time, a change meanwhile refetches after it, a reconnect drops older replies (`seq`).
- **Orphan notice** (`app.rs` → `on_notes_removed`): `notes.removed {reason: "orphaned"}` for the visible worktree (selected, Worktrees view) refetches the list and toasts "Agent a2 exited; its notes are kept 7 days (Notes, n: Earlier notes)." with the days from the orphan's `expires_at` ("stopped" after a stop with keep; nothing for a template-only orphan, pruned at once, Q-N12).
- **Stop dialog** (`dialogs.rs`): opening it re-reads the agent's notes (`notes_get`). `handoff()`: `Unsupported` (an engine without notes: as before), `Checking`, `Empty` (only the template and no unsaved draft: the plain Stop, `notes: "delete"`), `Offer` (also when the notes could not be read): three full-width buttons replace Stop: **Copy notes and stop (c)** (`notes: "delete"`, then the result's `notes`, the engine's final text, goes to the clipboard through `ctx.copy_text` at the next frame, Q-N35), **Stop, keep notes (k)** (`notes: "keep"`; hover: kept `[notes] orphan_days`, 7 by default, Q-N34), **Stop and delete notes (d)**. `y` / Enter = keep, `n` / Esc cancel; unsaved panel edits are called out. A plain stop before the notes were read sends `keep` (Q-N36). `C K D` row in the `?` sheet.
- **Spawn dialog**: opening it fetches the target's earlier notes. With **New** session selected and ended notes there: "A new conversation deletes the earlier notes of Agent a2 (orphan, 2.0 KiB, ended 3h ago, deleted in 7d)[ and N more]." + **Carry them into the new agent** (off by default) + a "See them in the notes panel" link (closes the dialog, opens the panel). Resume (pick / last) deletes nothing (no rule 5), so no warning; their adopt is the panel's. The carry (Q-N21's "spawn, then adopt", Q-N33): `notes_get {notes_id}` before the spawn (rule 5 deletes them during it), then `spawn_agent`, then `notes_put {if_rev: 0}` into the new agent's fresh notes (`by: user`); if that put fails the text waits in the panel as an unsaved draft (`NotesDoc.carried`) and the panel opens.
- **Acceptance.** "Crash → orphan shown and adopted; stop with notes offers the handoff": `a_crashed_agents_notes_are_offered_adopted_and_handed_off` (real engine, fake agent): the agent's notes are written, it exits on its own (`exit 3`) → the toast and the panel's list (no agent there) → a `--continue` spawn adopts them from the panel (text there, orphan gone) → stop with **keep** leaves an orphan ("stopped; … kept 7 days") → the spawn dialog warns, **Carry** → the new agent has the text and the orphan is pruned → **Copy notes and stop** puts the text on the clipboard and the notes are gone. The MVP demo: `the_mvp_demo_notes_survive_compaction_and_restore_then_hand_off` types notes into the panel and saves, runs N3's hook with `SessionStart {source: compact}` on the engine's file (the notes come back as `additionalContext`), restarts the engine (the notes listed as `restorable`, the banner's entry carries the `notes_id`), **Restore all** → the restored agent's panel shows the same `notes_id` and text, the `resume` hook prints them, then `c` in the stop dialog copies them and deletes the file. With the fake agent, not a real Claude (Q-N27 still open).
- **Tests:** app unit (`notes_view.rs` +1: list fetch state, summary, days, labels); kittest (`tests.rs` +4: the stop dialog's three choices only with non-empty notes, unsaved edits called out, `c` submits, template-only → plain Stop, `Checking` / `Unsupported`; the clipboard drain; the spawn warning only with ended notes and a New session, Carry, the panel link; the panel's list: summary rows, View/Hide, Adopt asks first, Delete asks, the list without a live agent with Adopt disabled); real engine (+2, above). `cargo fmt --check`, `clippy --all-targets -D warnings`, `cargo test` (`LAZYAGENT_REQUIRE_ENGINE=1`) green, the two real-engine tests 4/4 in a loop; full `uv run pytest` 1614 passed (no Python change).

---

## 6. Open doubts

Decisions taken in this plan that the user may want to revisit (recommended choice first):

| # | Doubt | Plan's choice | Alternative |
|---|---|---|---|
| Q-N1 | "Only the latest" scope. P1 says one file per agent slot, and that a new agent or several restorable entries in a worktree keep only the most recent. Agent ids are never reused (V10), so this plan applies "only the latest" **per worktree, to ended agents only**; live agents always keep theirs | Per worktree, ended agents only; a fresh spawn deletes the worktree's ended notes (rule 5), with the spawn-dialog warning (N5) | Per slot only (a fresh spawn never deletes another slot's notes; ended notes expire by TTL) |
| Q-N2 | Default of `stop_agent` over MCP / CLI (no UI to offer the handoff) | `delete`, and the result returns the final text (that is the handoff for the orchestrator); UIs without a handoff yet send `keep` | `keep` (orphan 7 days) by default everywhere |
| Q-N3 | `replace_section` in propose mode: a whole-section rewrite as one pending block, or refused (append only)? | One pending block, accepted or discarded as a whole | Refuse `replace_section` in propose mode |
| Q-N4 | An engine without a manifest (second lazyagent in the repo; it holds no `engine.lock`) | Notes still work for its agents; GC only TTL + gone worktrees, on its own files | No notes without the lock (the methods answer `VALIDATION_ERROR`) |
| Q-N5 | Who counts as "the orchestrator" for writes (P8): the orchestrator agent's token, and **any MCP caller without a token** (an outside Claude session, e.g. the orchestrator skill) | Both | Only the orchestrator agent's token; outside sessions read only |
| Q-N6 | Re-inject on `SessionStart source=startup` | Only when the notes are not empty (covers a restore that reports `startup`; a fresh spawn has template-only notes) | Never on startup |
| Q-N7 | The `--continue` spawn that Q31 consumes an entry for (V5): adopt the entry's notes automatically when the match is "sure" without an exact id? | No: orphan, offered in the panel | Adopt on a "sure" match |
| Q-N8 | Pinned instruction source: the first `UserPromptSubmit` (P-proposal) vs the spawn `instruction` argument | First `UserPromptSubmit`, falling back to the spawn instruction for providers without hooks | Spawn instruction only |
| Q-N9 | Orphan / restorable notes of the **orchestrator** (single slot, repo root) | Same rules; its "worktree" is the orchestrator key | Never orphan the orchestrator's notes (always carried to the next orchestrator) |

Raised while building N1 (choice taken, implemented):

| # | Doubt | Choice (N1) | Alternative |
|---|---|---|---|
| Q-N10 | Where an engine without a manifest (Q-N4) keeps its notes: in the shared dir the lock holder's GC would delete them (no entry references them) | Its own subdir `notes/<hash>/engine-<instance id>/`, never loaded by others; the lock holder removes such dirs untouched for `orphan_days` | Shared dir, with the engine id in the meta and the holder sparing other engines' live notes |
| Q-N11 | `stop_agent` "returns the final text" vs callers that use its `AgentInfo` | `stop_agent` unchanged (+ `notes=`), `stop_agent_with_notes` returns `(info, text)`; N2's method handler uses the latter | Change `stop_agent`'s return type |
| Q-N12 | Ended notes that are only the template | Never kept: an empty orphan or restorable notes is pruned at once (a restore then starts from a new template) | Keep them like any other until TTL / "only the latest" |
| Q-N13 | Ties in "only the latest": the agents of one engine all end at its take-over time | The newest agent (notes `created_at`) wins | The most recently written notes |
| Q-N14 | Loading the notes at engine start reads files on the loop | Synchronous, as `Manifest.load` (once, ≤ a few dozen 8 KiB files); every write and delete is off the loop | Load in a worker thread before serving |
| Q-N15 | A spawn whose process fails to start | Its notes are kept as an orphan (a failed restore keeps its entry's notes; a fresh spawn's template is pruned as empty) | Delete them |
| Q-N16 | `notes.removed` reasons: 0011 names `deleted`, `orphaned`, `adopted` | Plus `pruned` for GC removals (only the latest, TTL, worktree gone, a fresh agent) | Report GC removals as `deleted` |

Raised while building N2 (choice taken, implemented):

| # | Doubt | Choice (N2) | Alternative |
|---|---|---|---|
| Q-N17 | P8 refuses a peer agent's token **writing**; may it read a peer's notes? | Yes: reads are open (it can `read_agent_output` the same agent anyway); only writes are refused | Refuse reads too: an agent sees only its own notes |
| Q-N18 | N5's "View" of ended notes needs their text; `notes_list` has only metadata | `notes_get {notes_id}` reads any notes by id (now, to avoid a later protocol change) | A separate `notes_get_ended` in N5 |
| Q-N19 | `by` of a call without a token: P8 makes it the orchestrator, but the panel's `notes_put` is the user's | `notes_put` / `notes_adopt` without a token → `user`; `notes_append` / `notes_replace_section` without one → `orchestrator` | An explicit `by` param the client sets |
| Q-N20 | A `caller` token the engine does not know (its agent ended, another engine's) | Refused (`VALIDATION_ERROR "Unknown caller token"`) | Treat it as no token (writes as the orchestrator) |
| Q-N21 | Adopting after a crash: a **fresh** spawn in that worktree prunes its ended notes first (rule 5), so the orphan is gone before a new agent could adopt it | Kept: adopt targets an agent that was already live, or one spawned with `resume_mode=last` / a restore; N5's spawn dialog must offer "adopt" (spawn, then adopt) before rule 5 runs | Let a spawn carry `adopt_notes_id` so the new agent starts from them |
| Q-N22 | Section names are matched whole (case-insensitive): `notes_append("Constraints", …)` adds a new `## Constraints` instead of using `## Constraints (don't touch)` | Whole names; the MCP docstring lists the exact headings | Match a heading by prefix |

Raised while building N3 (choice taken, implemented):

| # | Doubt | Choice (N3) | Alternative |
|---|---|---|---|
| Q-N23 | A restorable agent's notes with a pinned instruction but template-only text: Q-N12 prunes them, so the restored agent loses its instruction | Kept while restorable (its restore resumes that conversation); an orphan like that is still pruned | Count the instruction as content everywhere (orphans too), or always prune (Q-N12 as is) |
| Q-N24 | Which "first prompt" to pin after a restore or a `--continue` spawn that starts new notes: its first prompt is "carry on", not the original instruction | Only notes created for a fresh conversation (`resume_mode=new`) pin the observed first prompt; revived notes keep theirs, resumed ones pin nothing | Pin the first prompt seen whatever the spawn |
| Q-N25 | Change notice when the user edits and the agent then writes before its next prompt: the last writer is `agent`, so the hook says nothing and the agent never sees the user's change as a diff | Silent (seen moves to the agent's text); the agent's write went through the section tools, the user's lines are in the file it re-reads | Keep the writers since the seen rev in the meta and notice unless all were the agent |
| Q-N26 | Re-injection with template-only notes but a pinned instruction (after `/compact` the instruction alone could help) | Nothing injected: §2's "a template counts as empty" applies to the header too | Inject the header and the instruction alone |
| Q-N27 | Real-Claude manual check needs a folder Claude already trusts; a fresh scratch repo makes it ask, and accepting writes `~/.claude.json` | Not done by the agent; left for the user (§5 N3) | Accept the trust prompt in a temp folder |

Raised while building N4 (choice taken, implemented):

| # | Doubt | Choice (N4) | Alternative |
|---|---|---|---|
| Q-N28 | The panel has unsaved edits when the agent's notes go (`notes.removed`: stopped, exited, pruned) | The draft is dropped with a warning toast; the engine's last saved text survives as an orphan when the agent exited (N5 lists it) | Keep the draft in the app (a recovery entry, or the clipboard) until dismissed |
| Q-N29 | `n` opens the panel: also give the editor the keyboard? | Yes: opening is for typing; Esc leaves the editor, `n` again closes | Open without focus; a click (or Tab) to edit |
| Q-N30 | N5's `NotesList` / `NotesListItem` and N4's "every `notes_*` fixture deserialises" | Typed now (with `NotesDeleted`), unused until N5 | Keep skipping `notes_list` / `notes_delete` fixtures until N5 |
| Q-N31 | Keeping the notes of agents not on screen current | Lazily: an event only marks a newer rev; the text is fetched when that agent's notes are shown (one `notes_get` per write while shown) | Fetch on every `notes.updated` for every cached agent |
| Q-N32 | The meter at exactly `max_bytes`: the engine accepts it (it refuses above) | Red from 100 %, Save disabled only above the cap ("over the cap: condense them") | Disable Save at 100 % too |

Raised while building N5 (choice taken, implemented):

| # | Doubt | Choice (N5) | Alternative |
|---|---|---|---|
| Q-N33 | Q-N21's "spawn, then adopt" for a **new** conversation: rule 5 deletes the ended notes inside the spawn, before any `notes_adopt` could run | The spawn dialog reads them first (`notes_get {notes_id}`), spawns, then `notes_put {if_rev: 0}` into the new agent's fresh notes (`by: user`); a failed put leaves them in the panel as an unsaved draft. Opt-in checkbox, only for a New session (resume modes delete nothing) | `spawn_agent {adopt_notes_id}`: the engine starts the new agent from them (a protocol change) |
| Q-N34 | The client does not know `[notes] orphan_days` (not in `get_config`) for "Stop, keep notes 7 days" | The button says "Stop, keep notes" (hover: `orphan_days`, 7 by default); the toast after it gives the real days from the orphan's `expires_at` | Expose `orphan_days` in `get_config` or `notes_get` |
| Q-N35 | What "Copy notes and stop" copies | The stop result's `notes`: the engine's final text, after the agent's last write; unsaved panel edits are not in it (the dialog says so) | The panel's draft when dirty, else the cached text, copied before the stop |
| Q-N36 | Defaults of the stop dialog with notes | `y` / Enter = keep (nothing lost); a plain stop before the notes were read sends `keep` (an empty orphan is pruned at once, Q-N12) | `delete`, the engine's default |
| Q-N37 | Which orphans are announced | Only the visible worktree's (selected, Worktrees view), as planned; "exited" or "stopped" from the app's own exits list. Any worktree's earlier notes are in the panel when it is selected | A toast for any worktree |
| Q-N38 | Confirmations in the earlier list | Adopt asks unless the agent's notes are only the template and the draft is clean; Delete always asks (it cannot be undone) | Delete without asking, like Revert |
