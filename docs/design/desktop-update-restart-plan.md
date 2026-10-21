# Desktop: update and restart from the UI — plan

- **Status:** Proposed (U0, 2026-10-21). Implementation waits for the go. Each step gets an "As landed" note in §10 when it merges.
- **Base:** `develop` @ `99409b9` (review replies, orchestrator-restore fix `dd242aa`).
- **Branch:** `feat-desktop-update-restart` (one branch, U1-U4 as commits).
- **Ask:** a typical menu bar in the desktop app; "Check for updates" and "Restart" (the app; optionally the engine too); after an update, offer the restart; an option to restore every agent once the engine restarted. Today the user does it by hand: `uv run lazyagent engine stop` / `start`, `engine restore --id …`, `cargo install --locked --path desktop/crates/app`, `uv run lazyagent desktop`.

## TL;DR

| # | Scope | Acceptance |
|---|---|---|
| U0 (prior phase, own branch) | Package the desktop binary **inside the one `lazyagent` package** (user decision): platform wheels built by a hatchling build hook, plus the pure wheel and sdist as fallback; `publish.yml` builds them; `desktop.py` finds the wheel's binary; CI builds the wheels as artifacts only (Q-U16, §3c) | `uv tool install lazyagent` of the CI-built wheel on a clean Linux box runs `lazyagent desktop` with no Rust toolchain; nothing published, no tag |
| U1 | Python: `lazyagent update check/apply` with two modes: **published** (default: newer release on PyPI, applied with `uv tool upgrade` / `pipx upgrade`) and **development** (launched from a git checkout: fetch, ff-only pull, `uv sync`, app build; refuses when there are uncommitted changes); `lazyagent engine restart [--restore]` | pytest: a dirty checkout is refused and left byte-identical; a restart brings back exactly the agents it stopped, the orchestrator as the orchestrator |
| U2 | Engine: `source_commit` in the info file and `hello` (additive) | The app can tell "the engine runs older code than the checkout" |
| U3 | Desktop: menu bar (File / View / Agents / Help), Restart dialog (app; engine; restore), Check-for-updates dialog, progress, app re-exec, post-restart report toast | cargo test: the restart sequence against the testkit engine; the old app never talks to the new engine; the new app starts after the restore |
| U4 | Docs (README, desktop-app.md, engine-split §4 rows), screenshots | Screenshots in docs/design/desktop-screenshots/ |

---

## 1. Ground truth

| Fact | Where | Consequence |
|---|---|---|
| The user's install is **editable**: `.venv` → `file:///home/giovanni/smith/lazyagent` (`direct_url.json`, `editable: true`); the desktop binary is `~/.cargo/bin/lazyagent-desktop` (found on PATH by `desktop.py`) | `src/lazyagent/desktop.py` | A `git pull` changes the Python code of every **new** process at once; the binary only after `cargo install` |
| Version is `0.8.0` on both `develop` and the last tag (`v0.8.0`); `engine_version` = package version | `pyproject.toml`, `hello` | The version cannot tell an update from a source checkout: git commits must |
| `shutdown {stop_agents}` stops a **detached** engine; an engine hosted by a TUI refuses it; `hello`/`get_engine_info` say `detached` | `engine/server.py:1142`, `:650` | "Restart the engine" is offered only for a detached engine |
| A stopped engine keeps its agents in the manifest's `agents`; the next engine offers `agents + restorable` as restorable; entry ids are stable; a restore keeps the agent id when free and resumes the exact conversation (`--resume <id>`), else "resume last" | `engine/manifest.py`, `engine.py:1864` | A restart = stop, start, restore the ids that were live. No new engine method needed |
| `restore_agents` without ids restores **everything** restorable, including entries left by older engines (up to 50) | `engine/cli.py:230` | "Restore all" after a restart must restore only what this restart stopped (Q-U6) |
| The 20-10 bug: `lazyagent desktop .` spawned a plain agent in the repo root with "resume last"; `claude --continue` there took the orchestrator's conversation as `lazyagent-a1`. Fixed in the engine (`dd242aa`): an agent's "resume last" in the root is refused while an orchestrator of the same provider runs or is restorable; the orchestrator's "resume last" takes its entry; a conversation running as the other kind hides the entry | `engine.py` `_check_continue_in_root`, `_last_orchestrator`, `_live_match`; tests/engine/test_restore.py (+4) | Checked: the guard is in the engine, so every client gets it. The new flow never spawns, it only calls `restore_agents`; and the new app starts **after** the restore (§4) |
| The desktop never spawns on its own at start. The only "resume last" buttons: the spawn dialog, and the attention item "Spawn again (resume the last session)" on an exit | `attention_view.rs:222`, `dialogs.rs:232` | After a manual engine stop with the app open, every killed agent becomes an "exited" item with that button; in the root the engine now refuses it, elsewhere it resumes the worktree's last conversation, not necessarily the agent's. U3 drops exits of a previous engine (Q-U11) |
| The connection supervisor does not restart an engine that announced `engine.shutdown` (Stopped), but reconnects as soon as one is located; `Conn::shutdown()` ends the supervisor | `conn.rs` | The old app quits its supervisor before the engine stops, so it never meets the new engine |
| `cargo install` of the app (release, `lto = "thin"`, `codegen-units = 1`) takes minutes; replacing the binary does not disturb the running process | `desktop/Cargo.toml` | The build runs in the background, in the jobs drawer; the restart is offered when it ends |
| The main checkout `/home/giovanni/smith/lazyagent` is on `develop` tracking `origin/develop`, with the user's uncommitted changes (`M .claude/skills/release/SKILL.md`, `?? orchestrator-epic-plan.md`, `?? scripts/`) | `git status` (read only) | Those are never touched: no stash, reset, checkout, clean or rebase, ever |

## 2. What "an update" is

**User decision (21-10, via the orchestrator):** by default an update is a **published release**. When the app was launched in **development mode**, it behaves dynamically: it checks the source checkout (fetch, new commits on its branch) and updates with `pull --ff-only` + `uv sync` + an app build, never touching uncommitted changes (if there are any: warn, do not update).

**Mode detection (Q-U1)**, in `lazyagent update check`, first match wins:

| Signal | Mode |
|---|---|
| The `lazyagent` distribution is editable (`direct_url.json` → `dir_info.editable`) and its source root (`Path(lazyagent.__file__).parents[2]`, as `desktop.py`'s `SOURCE_ROOT`) holds `.git` and a `pyproject.toml` named `lazyagent` | development (the user's case: `.venv` → `/home/giovanni/smith/lazyagent`) |
| The desktop binary that runs (`LAZYAGENT_DESKTOP_BIN`, else the one `desktop.py` found; passed by the app as `--app-binary`) is under `<checkout>/desktop/target/` | development, that checkout |
| Otherwise | published |

The app shows the mode in Help → About and in the update dialog ("development checkout `/home/…/lazyagent`, branch `develop`" / "published release 0.8.0").

| Mode | Detect | Apply |
|---|---|---|
| **Published** (default) | PyPI JSON `https://pypi.org/pypi/lazyagent/json`: latest non-yanked, non-pre-release > `__version__`; release notes = the GitHub release body for `v<latest>` (best effort) | §3b |
| **Development** | `git fetch --quiet <remote> <branch>` of the current branch's upstream (for the user `develop` → `origin/develop`); `behind = rev-list --count HEAD..@{u}`; commits `log --oneline HEAD..@{u}` (max 30 shown) | §3 |
| Unknown (offline, no upstream…) | — | "Cannot check: …" with the reason |

Also reported, without a fetch (so "Check" is useful even when nothing is new upstream — the case "I pulled by hand"):

- **engine behind:** the engine's `source_commit` (U2, `git rev-parse HEAD` of its source at start) ≠ checkout `HEAD` → "the engine runs older code: restart it";
- **app behind:** the binary's build commit (`build.rs` → `LAZYAGENT_BUILD_COMMIT`, plus `+dirty` when `desktop/` had changes) ≠ last commit touching `desktop/` in `HEAD` → "rebuild the app".

`lazyagent update check --json` (one JSON object; `--no-fetch` for tests/offline): `{mode: "development"|"published"|"unknown", version, latest, installer, root, branch, upstream, behind, ahead, commits:[{sha, subject}], dirty:[path], blocked_by:[path], can_apply, reason, changes:{python, lock, desktop}, engine_commit, head}`. `changes` comes from `git diff --name-only HEAD..@{u}`: `python` = `src/` or `pyproject.toml`; `lock` = `uv.lock` or `pyproject.toml`; `desktop` = `desktop/`.

**When to check (Q-U3):** on demand (Help → Check for updates…), plus once 10 s after start and every 6 h, quietly; a found update shows a small "Update available" chip at the right of the menu bar. Off switch in the Help menu, kept in the app's `persist.rs` file. A `git fetch` only writes `.git/` refs, never the working tree.

## 3. Applying it safely (development mode)

`lazyagent update apply --json-lines` prints one line per step (`{step, state, detail}`) so the app can show progress. One update at a time (an `fcntl` lock in `$XDG_STATE_HOME/lazyagent/update.lock`).

Refused, with the reason and nothing changed (all checked **before** any write):

1. detached HEAD, no upstream, or a merge/rebase/cherry-pick in progress (`MERGE_HEAD`, `rebase-merge/`, …);
2. not a fast-forward: `ahead > 0` (local commits or diverged) → "your `develop` has N commits not on `origin/develop`; update it by hand" (Q-U4);
3. **uncommitted changes** (user decision: warn, do not update): any modified, staged, deleted or conflicted tracked file (`git status --porcelain=v1 -z --untracked-files=no` not empty) → "the checkout has uncommitted changes: <list>. Commit or put them aside yourself, then check again". Untracked files do not block, unless the update adds a file at that path (then refused with the list too) (Q-U5). The dialog still lists the new commits, so the user knows what they are missing. Today the user's main checkout (`M .claude/skills/release/SKILL.md`) would get this warning; that is intended.

Steps:

1. `git merge --ff-only @{u}` (after the fetch of `check`; git itself refuses again if a file would be overwritten: a second safety net). Never `stash`, `reset`, `checkout`, `clean`, `rebase`, `pull --rebase`.
2. `uv sync` in the checkout only when `changes.lock` (else the env is already right; `uv run` would sync it anyway). Missing `uv` → the step is skipped with a note when `lock` is false, an error when true.
3. The app build, only when `changes.desktop` or "app behind" (Q-U8), matching how the running binary was installed: binary under `<checkout>/desktop/target/release/` → `cargo build --release --manifest-path desktop/Cargo.toml`; otherwise (`~/.cargo/bin`, the user's case) → `cargo install --locked --path desktop/crates/app`. Missing `cargo` → skipped with the command to run. Output tail kept in the jobs drawer; the app keeps working while it builds.
4. Done → the restart dialog opens preset: **app** always; **engine** checked when `changes.python` or "engine behind"; **restore** checked (Q-U7).

### 3b. Published mode

The installer is told from `sys.prefix` / the distribution's `INSTALLER` file: `uv tool` (prefix under `uv tool dir`) → `uv tool upgrade lazyagent`; `pipx` → `pipx upgrade lazyagent`; anything else (a project venv, system pip) → not applied, the dialog shows the command to copy (Q-U2). The updater in published mode looks at PyPI only. The desktop binary comes with it once U0 lands (it is inside the platform wheel, so one upgrade moves Python and app together). Before U0 lands there is no published binary: the dialog updates Python only and says the app must be rebuilt by hand. Then the same restart dialog as step 4.

### 3c. U0: the app binary inside the published wheel (prior phase)

Today PyPI only gets the pure Python wheel (`publish.yml`: `uv build`, hatchling, `packages = ["src/lazyagent"]`); `lazyagent-desktop` is published nowhere. **User decision (21-10):** platform wheels with the binary inside, one package on PyPI, one version for Python and app.

Chosen approach (Q-U16): **keep hatchling, add a custom build hook** (`hatch_build.py`, `[tool.hatch.build.targets.wheel.hooks.custom]`):

- With `LAZYAGENT_BUILD_DESKTOP=1` the hook runs `cargo build --release --locked --manifest-path desktop/Cargo.toml`, adds the binary as `lazyagent/bin/lazyagent-desktop` (`force_include`, mode 755), sets `pure_python = False` and `tag = "py3-none-manylinux_2_28_<arch>"` (the binary does not depend on the Python version, so one wheel per platform, not per interpreter). Without the variable it does nothing: `uv build`, editable installs and `uv sync` stay pure Python and need no Rust.
- **Fallback:** the `py3-none-any` pure wheel and the sdist are still built and uploaded. On a platform with no binary wheel pip picks the pure one; `lazyagent desktop` then prints today's `BUILD_HELP` (how to `cargo install` it). The sdist builds pure unless the variable is set.
- **`desktop.py`:** lookup becomes `LAZYAGENT_DESKTOP_BIN` → the package's own `lazyagent/bin/lazyagent-desktop` → `lazyagent-desktop` on PATH → `desktop/target/release/` of a source checkout. (A dev checkout has no `lazyagent/bin/`, so its order is unchanged.)
- **Platforms (Q-U18):** Linux x86_64 and aarch64, built in `quay.io/pypa/manylinux_2_28_<arch>` containers with rustup, then `auditwheel show` to check the tag (winit/wgpu `dlopen` X11, Wayland, Vulkan and GL at run time, so the binary links only glibc-era libs). macOS later.
- **`publish.yml`:** on `v*` tags: a matrix job per platform builds the binary wheel, a job builds the pure wheel + sdist, the publish job downloads all artifacts and uploads them together (unchanged trusted publishing). **Not run in this work:** no tag, no release, nothing sent to PyPI.
- **Tested without publishing (Q-U19):** a `wheels` job in `ci.yml` (on PRs touching `desktop/`, `pyproject.toml`, `hatch_build.py` or the workflows) runs the same build as artifacts, installs the x86_64 wheel in a clean venv, checks `lazyagent-desktop --help` and that `desktop.find_binary()` returns the wheel's binary; it uploads the wheels with `actions/upload-artifact`.

Alternatives considered:

| Option | For | Against |
|---|---|---|
| **Hatch build hook** (chosen) | Keeps the build backend and every current workflow; pure fallback for free; one switch | We set the platform tag ourselves (and check it with `auditwheel`) |
| maturin `bindings = "bin"` with `python-source` | Tags, manylinux checks and cross builds done by maturin | Changes the build backend of the whole package: editable installs and `uv sync` would compile Rust, or need special casing |
| cibuildwheel around the hook | Standard runner, macOS/Windows later | Built per interpreter by default (3× the same binary); for a `py3-none` wheel a plain manylinux container job is simpler |
| GitHub Release assets / `cargo install` | No packaging work | A second channel and version, or a Rust toolchain per user: against the decision |

## 4. Restart: the safe order

Restart dialog (File → Restart…, also from the update dialog):

```
Restart lazyagent
[x] Restart the engine too        (disabled: "this engine runs inside a lazyagent TUI")
    [x] Restore its agents afterwards
  The engine stops 4 agents and 2 shells:
    ⚠ lazyagent-a2  feat-x        running          its current turn is cut
      lazyagent-a1  feat-y        waiting for you
      orchestrator                waiting          comes back as the orchestrator
      lazyagent-a3  docs          completed        resumes "last" (no conversation id yet)
                                             [Cancel]  [Restart]   (→ "Restart anyway" when ⚠)
```

Busy = `running`, `waiting_for_approval`, `possibly_hanged` (mid-turn; a restore resumes the conversation, not the turn). Any busy agent turns the button into "Restart anyway" (Q-U9). Entries with no conversation id are flagged ("resumes last").

Sequence (app + engine):

1. Flush the app's saved state (`Saver::flush`); warn first if the notes panel has unsaved edits (Q-U10).
2. `Conn::shutdown()`: the supervisor ends, panes detach. From here the old app sends nothing to any engine (no autostart, no reconnect to the new engine, no stale "resume last" button).
3. Run `LAZYAGENT_PYTHON -m lazyagent engine restart --restore --json <root>` (with the app's agent env, as for `engine start`), in a thread; a modal shows its steps:
   1. `list_restorable` → `before` (ids already offered);
   2. `shutdown {stop_agents: true}`; wait for the pid to exit (15 s);
   3. start (as `engine start`; if another client started one in the gap, use it);
   4. `list_restorable` → `after`; `restore_agents {ids: after − before}` (exactly the agents this restart stopped; the orchestrator entry goes through `_spawn_orchestrator` with `--resume <id>`, so it comes back as the orchestrator);
   5. print `{stopped, restored:[…], failed:[…], left:[…]}`.
4. Write the result to `$XDG_RUNTIME_DIR/lazyagent/desktop-restart-<pid>.json`.
5. **Re-exec the app, after the restore:** `exec LAZYAGENT_PYTHON -m lazyagent desktop <root> <original options>` (same lookup as a launch, so a freshly installed binary is the one that runs; falls back to `lazyagent` on PATH, then to the current exe path without the `" (deleted)"` suffix), with the environment the app was **started** with (captured before `gpu::hide_offload_nvidia` and the login-env merge) plus `LAZYAGENT_DESKTOP_RESTART_REPORT=<file>`.
6. The new app connects, reads and deletes the report, toasts "Engine restarted · 4 agents restored · 1 failed: …"; failures stay offered in the restore banner.

App only: steps 1, 2, 5. The agents keep running; the new app re-attaches to every stream.

If step 3 fails before the stop, nothing happened: the modal says so and the app reconnects (`Conn` is recreated). If it fails after the stop, the app still re-execs (the new app shows the engine state and the restore banner).

Engine restart without "restore": agents stay restorable (banner in the new app, as today).

Why the engine part is a Python CLI and not Rust: the same command serves the TUI, the web and the terminal (it replaces today's 3 manual commands), it runs the updated code, and it is tested with pytest against a real engine.

## 5. Protocol / engine

- **No new method is needed.** `shutdown`, `list_restorable`, `restore_agents {ids}` cover it.
- **Additive (U2):** `source_commit` (`git rev-parse HEAD` of the engine's source tree at start, `null` for a wheel) in the engine info file and in `hello` / `get_engine_info`; golden fixture and `types.rs` (`Option<String>`). Old apps ignore it.
- Not now: engine methods `update_check` / `update_apply` (would let the web page do it; §7).

## 6. Menu bar

A 24 px row above today's top bar (`egui::MenuBar`), the top bar unchanged (Q-U12):

| Menu | Items |
|---|---|
| **File** | New agent… (N) · New worktree… · Open another repo… (the picker) · — · Restart… · Quit (Ctrl+Q) |
| **View** | Worktrees (g w) · Attention (g a) · Notes panel · PR panel · Diff · — · Light / dark theme |
| **Agents** | Restore agents… (when any are offered) · Stop the selected agent · Engine: start / stop |
| **Help** | Check for updates… · Check automatically [x] · Keyboard shortcuts (?) · About lazyagent (app build, engine version and commit, source root) |

Items reuse existing actions; nothing new besides Restart, Check for updates and About. Right side of the menu row: the "Update available" chip.

## 7. TUI and web (note only)

- **TUI:** the same CLI; a command-palette entry "Restart the engine" can run `lazyagent engine restart --restore` when its engine is detached; a TUI that hosts its engine restarts by quitting and relaunching. Not in this branch.
- **Web:** the page is a client of the engine; an update/restart there needs engine methods (§5 "not now") because the browser cannot run the CLI. Later, if wanted.

## 8. Tests

| Where | What |
|---|---|
| `tests/test_update.py` (new) | Mode detection (editable dist, binary under `desktop/target/`, wheel → published). Development, temp bare remote + clone: up to date; behind 2 → commits listed; ahead → refused; diverged → refused; any modified/staged tracked file → refused and byte-identical; untracked file elsewhere → applied and untouched; untracked file that the update adds → refused; merge in progress → refused; `changes` flags; `uv`/`cargo` shims on PATH that record their args (never a real build), `cargo build` vs `cargo install` by binary location. Published: stubbed PyPI fetch (newer, same, pre-release ignored, offline); installer detection → the right command, unknown → not applied. Lock held → refused |
| U0 (its branch) | pytest: the hook is a no-op without the variable (pure wheel, `py3-none-any`); with a fake `cargo` shim it force-includes the binary and sets the tag; `desktop.find_binary()` order. CI `wheels` job: builds the wheels as artifacts, installs one in a clean venv, `lazyagent-desktop --help`. No publish |
| `tests/engine/test_engine_restart.py` (new, isolated engine via the existing fixtures: own `HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`) | 2 agents + orchestrator → `engine restart --restore`: same agent ids, conversations resumed by id, orchestrator `kind: orchestrator` under `lazyagent-orchestrator`; an older restorable entry is **not** restored and is still offered; without `--restore` everything stays restorable; TUI-hosted engine refused; **no `--continue` spawn in the root** (the fake agent records its argv) |
| Engine info / hello | `source_commit` present; golden fixture updated |
| `cargo test` (app) | Menu items enabled/disabled by state; busy detection and the "Restart anyway" switch; the re-exec command line and environment (pure function); report read/delete; exits of a previous engine dropped; with the testkit engine: the restart sequence calls `Conn::shutdown` before the CLI and the CLI before the exec (exec replaced by a recorder in tests) |
| Checks | `uv run pytest`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`; benches only in release, unchanged |

Hard rule for every test and manual try: an engine of its own in a temp `HOME`/`XDG_STATE_HOME`/`XDG_RUNTIME_DIR`; never the user's running engine (pid 9983 today) or app, never the main checkout (the update tests use temp repos; the CLI takes `--source PATH`).

## 9. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | The update pulls into the user's main checkout with uncommitted work | Any uncommitted tracked change blocks the update (warn only), ff-only, no destructive git command anywhere; git's own refusal as a second net; tests assert byte-identical files |
| R2 | Editable install: the running engine sees new code on disk after the pull | Restart offered at once with the engine box preset; short window |
| R3 | A busy agent loses its turn | Confirmation list with ⚠; the conversation itself resumes |
| R4 | Agents without a conversation id come back with "resume last", which may pick another conversation in that worktree | Flagged in the list; the root case is refused by the engine guard (`dd242aa`) |
| R5 | `restore all` resurrecting old entries | Only `after − before` is restored |
| R6 | Re-exec loses the window position and size | Accepted in v1 (Q-U13) |
| R7 | Another client (TUI with autostart) starts an engine between stop and start | The CLI uses whatever engine serves the repo; the restore still targets the stopped ids |
| R8 | The app's restart leaves the user with no window if the exec fails | The exec error is printed and the app stays open, reconnecting (only possible before step 5 replaces the process) |
| R9 | `cargo install` builds the working tree, including uncommitted changes under `desktop/` | Shown in the update dialog when `desktop/` is dirty; it is what the manual command does anyway |
| R10 | SendMessage addresses change if an agent id is taken | The restore keeps agent ids when free; the restart restores into an empty engine, so they are free |

## 10. As landed

(empty)

## 11. Open questions (decided as recommended unless the user says otherwise)

| # | Doubt | Choice | Alternative |
|---|---|---|---|
| Q-U1 | What "update" compares, and how dev mode is told | **User decision:** published release by default; development mode (editable dist from a git checkout, or the binary under `desktop/target/`) compares the checkout's branch with its upstream, by commits | Always the checkout; always PyPI |
| Q-U2 | Published installs: who runs the upgrade | `uv tool upgrade` / `pipx upgrade` when that is the installer; otherwise show the command | Always only show the command; or run pip in whatever env |
| Q-U3 | Automatic checks | At start (+10 s) and every 6 h, quiet chip; switch in Help | Only on demand |
| Q-U4 | Local commits / diverged | Refuse, explain, update by hand | Rebase or merge for the user |
| Q-U5 | Uncommitted changes (dev mode) | **User decision:** warn and do not update. Any modified/staged tracked file blocks; untracked files block only when the update adds that path. Never stash/reset | Untracked files block too; or allow dirty files the update does not touch |
| Q-U6 | What "restore all" restores | Only the agents this restart stopped (`after − before`) | Every restorable entry (today's `restore --all`) |
| Q-U7 | Defaults of the restart dialog | After an update: engine checked iff Python changed or engine behind; restore checked. From the menu: engine unchecked, restore checked; both remembered | Always unchecked |
| Q-U8 | When to rebuild the app | Only when `desktop/` changed or the binary is behind | Always |
| Q-U9 | Busy agents | Confirmation list, "Restart anyway"; no "wait until idle" in v1 | Wait for idle automatically; or refuse |
| Q-U10 | Unsaved notes edits on app restart | Ask (save / discard / cancel) | Save silently |
| Q-U11 | Exits left by a previous engine ("Spawn again (resume last)") | Dropped when the engine changes; the restore banner offers them instead | Keep them |
| Q-U12 | Menu bar placement | Own 24 px row above the current top bar | Menus at the left of the current top bar (no extra height) |
| Q-U13 | Window geometry across the re-exec | Not kept in v1 | Pass it as `--geometry` to the new app |
| Q-U14 | Where the restart logic lives | Python CLI `lazyagent engine restart`, run by the app; the app only re-execs | All in Rust over the protocol; or a detached helper that also relaunches the app |
| Q-U15 | `source_commit` / build commit | Add both (additive) to say "engine/app behind" | Only compare with upstream |
| Q-U16 | How the app binary gets into the one `lazyagent` wheel (user decision: platform wheels, one package) | hatchling custom build hook behind `LAZYAGENT_BUILD_DESKTOP=1`, binary at `lazyagent/bin/`, tag `py3-none-manylinux_2_28_<arch>`; pure wheel + sdist as fallback; prior phase U0 on its own branch | maturin `bindings = "bin"` (changes the backend); cibuildwheel around the hook |
| Q-U18 | Platforms of the first binary wheels | Linux x86_64 + aarch64 (manylinux_2_28); macOS later | Also macOS now |
| Q-U19 | Proving the workflow without publishing | CI `wheels` job builds them as artifacts and smoke-tests one; `publish.yml` changed but only runs on tags, none created | A dry-run to TestPyPI |
| Q-U17 | Published mode before U0 lands | Update Python only and say the app must be rebuilt by hand | Block published updates until U0 |
