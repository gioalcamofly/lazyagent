# Session notes: implementation plan

- **Status:** plan (N0), 2026-10-19. Each phase gets an "As landed" note in §5 when it merges.
- **Proposal:** [0011-session-notes.md](../proposals/0011-session-notes.md) (accepted; P1-P8 answered). This plan does not repeat its rationale, only what to build, where, and how to prove it.
- **Base:** `develop` @ `c0c3ac0` (PR 9 manifest + Q31 landed; desktop D1-D6 landed).
- **MVP** = N1-N5: the notes file and its lifecycle, protocol + MCP tools, re-injection after compaction / `/clear` / resume, and the desktop panel. TUI and web panels, propose mode, the attention queue and templates come after.

## TL;DR

| # | Branch | Scope | One-line acceptance |
|---|---|---|---|
| N0 | `plan-session-notes` | Proposal 0011 + this plan | Merged |
| N1 | `notes-n1-store` | `engine/notes.py` store, `Entry.notes_id`, lifecycle hooks in `Engine`, GC ("only the latest", orphans 7 days), `[notes]` config, `notes.updated` / `notes.removed` events | Lifecycle table of 0011 passes as Engine tests with real PTYs |
| N2 | `notes-n2-protocol` | `notes_*` methods (both sockets, v1 schemas, golden fixtures), per-spawn token, MCP tools, `spawn_agent {notes_mode}`, `stop_agent {notes}` | An agent's MCP `notes_append` with no target writes its own notes; a peer's token is refused |
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

(Empty until N1 merges.)

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
