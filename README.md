<!-- Logo/banner placeholder: add a dark/light-mode-ready banner here
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/banner-light.svg">
    <img alt="lazyagent" src="docs/assets/banner-light.svg" width="600">
  </picture>
</p>
-->

<p align="center">
  <strong>A lazygit-inspired workspace for orchestrating coding agents across git worktrees: in the terminal, the browser or a native window.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/lazyagent/"><img src="https://img.shields.io/pypi/v/lazyagent?color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/lazyagent/"><img src="https://img.shields.io/pypi/pyversions/lazyagent" alt="Python"></a>
  <a href="https://github.com/gioalcamofly/lazyagent/actions"><img src="https://img.shields.io/github/actions/workflow/status/gioalcamofly/lazyagent/ci.yml?branch=main" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/gioalcamofly/lazyagent?color=green" alt="License"></a>
</p>

<!-- Demo GIF placeholder: record with VHS or asciinema and add here
<p align="center">
  <img src="docs/assets/demo.gif" alt="lazyagent demo" width="800">
</p>
-->

---

## Motivation

You have a big feature to ship.
You crack open three terminals, `cd` into three worktrees, and spawn three coding agents.
Ten minutes later you're alt-tabbing through a mess of tabs, trying to remember which agent finished, which one is stuck waiting for approval, and which one silently errored out five minutes ago.

**lazyagent** does for coding agents what **lazygit** did for git:
one screen, full visibility, zero tab-juggling.

## Highlights

- **Multi-agent orchestration** — spawn, monitor, and stop agents across git worktrees, several per worktree
- **Real-time provider-aware status** — know instantly if an agent is running, waiting for approval, idle, or done
- **Multi-provider support** — Claude, Codex, and Gemini out of the box, configurable per repo
- **Three front-ends, one engine** — a TUI, a web UI, and a native desktop app all drive the same engine
- **Agents that outlive the screen** — with a detached engine, closing the UI leaves the agents running; after a crash or reboot they can be restored
- **Attention queue** (web, desktop) — approvals, questions, failures and red CI in one ranked list, with a reply box
- **Diff review** — working-tree changes in every front-end; in web and desktop, comment on lines and send them to an agent as a review
- **PR / CI at a glance** — pull request state, review status, and CI checks per worktree (via `gh`)
- **MCP server** — let a Claude session (lazyagent's own orchestrator, or yours) drive worktrees and agents

## Installation

> **Prerequisites:** Python 3.10+, git, and at least one agent CLI (`claude`, `codex`, or `gemini`).

```bash
uv tool install lazyagent    # recommended
pip install lazyagent        # or
```

This gives you the TUI, the web UI and the engine. Optional extras:

| For | Install |
|---|---|
| PR and CI status | the [GitHub CLI](https://cli.github.com/) (`gh`), logged in |
| The desktop app (Linux) | nothing on x86_64 and aarch64: the `lazyagent` wheel there carries it. Elsewhere, or from a clone of this repo: a Rust toolchain ([rustup.rs](https://rustup.rs), 1.88+), then `cargo install --locked --path desktop/crates/app` (puts `lazyagent-desktop` in `~/.cargo/bin`); `lazyagent desktop` prints the command for your install |
| Desktop notifications from the app | `notify-send` |

## Choosing a front-end

| | TUI | Web UI | Desktop app |
|---|---|---|---|
| Command | `lazyagent` | `lazyagent host` | `lazyagent desktop` |
| Runs in | your terminal | a browser tab on 127.0.0.1 | a native window (Rust, egui on wgpu) |
| Status | stable | experimental | experimental |
| Agent terminals | pyte, inside the TUI | xterm.js | `alacritty_terminal`, own GPU renderer |
| Attention queue, review comments | — | ✅ | ✅ |
| Agents survive closing it | with `--remote` | with `--remote` | always |
| Use it when | you live in a terminal, over SSH | you want a GUI with no build step | you want the fastest GUI (~4 ms key → frame) |

All three are clients of the repo's [engine](#the-engine); on a detached engine several can be open on one repo at once.

## Quick Start

### TUI

```bash
cd your-repo
lazyagent              # or: lazyagent --remote, to keep the agents when you quit
```

1. **Create worktrees** — press `c` to branch off parallel workstreams
2. **Spawn agents** — press `s` to launch a coding agent in the selected worktree (again for a second one: each gets a tab)
3. **Monitor** — watch agent output stream in real time; status badges update automatically
4. **Interact** — press `Ctrl+L` to drop into the embedded terminal for hands-on work
5. **Review** — press `Ctrl+D` to inspect diffs before committing
6. **Clean up** — press `d` to remove worktrees when done

By default lazyagent launches `claude`. Set `provider = "codex"` or `provider = "gemini"` in `.lazyagent.toml` to switch.

### Web UI (experimental)

```bash
lazyagent host your-repo --open        # --remote: quitting leaves the agents running
```

It serves 127.0.0.1 only (`--port` to pick one) and prints a URL with an access token; `--open` opens it in your browser. The page shows a live sidebar of worktrees, agents and PR/CI, every agent and shell in a native browser terminal, and the TUI itself as one more tab. From the page you can spawn, create, stop and remove (custom create commands show in a job drawer), work through the attention queue (`g a`), open the PR/CI panel (`Ctrl+.`), and review a diff (`Ctrl+D`): click a line number to comment, then *Submit review* sends the comments to an agent, now or when it next goes idle. Dark, light or system theme; notifications per kind. Design and measurements: [docs/design/ui-plan.md](docs/design/ui-plan.md) (§8a "As landed").

### Desktop app (experimental)

```bash
lazyagent desktop your-repo                        # the app (see Installation if it is not built)
lazyagent desktop --install                        # add it to the application menu
```

Feature parity with the web UI (sidebar, terminals and shell, actions and job drawer, restore, attention queue with replies, PR/CI panel, notifications, diff viewer with review), and it always runs on a detached engine, starting one if none runs, so agents keep running when it closes. Launched from the menu (or outside a repo) it opens a repo picker with your recent repos; agents started from there get your login shell's environment. `?` in the app lists every key; `lazyagent-desktop --help` lists its options.

**Updates and restarts.** The menu bar's *Help → Check for updates…* compares this install with the latest release on PyPI (and runs `uv tool upgrade` / `pipx upgrade` when one of them made it), or, when lazyagent runs from a git checkout (an editable install, or the app built in `desktop/target/`), fetches the checkout's branch and updates it by fast-forward, then runs `uv sync` when the lock changed and rebuilds the app when `desktop/` changed or the app was built from older code (*Rebuild* when the checkout was pulled by hand); the restart is offered only after that build succeeded. Uncommitted changes stay as they are; when git would overwrite some, nothing is updated and the files are listed. It checks on its own 10 s after start and every 6 h (*Help → Check automatically*). *File → Restart…* restarts the app, and optionally the engine too (its running agents are listed first, busy ones flagged), restoring the agents it stopped; the app comes back after the restore. The same from a terminal: `lazyagent update check|apply`, `lazyagent engine restart --restore`.

Measured on a laptop, against the web UI: key → frame p50 ~4 ms (web 8.4), 5 MB of output drawn at 57-102 MB/s (web 15-17), idle 0.2% of a core, 35 MB private memory, launch to sidebar 0.12 s with the engine running. Linux only (X11 tested, Wayland built in); building needs no system development packages. Design and measurements: [docs/design/desktop-app.md](docs/design/desktop-app.md).

## The engine

The engine is the process that owns the worktrees' agents, shells and jobs; every front-end is a client of it.

- **Embedded** (the default for `lazyagent` and `lazyagent host`): the engine lives inside the TUI, and quitting stops the agents.
- **Remote** (`--remote`, or `LAZYAGENT_ENGINE=remote`; always for the desktop app): the engine is a detached process per repo, started on demand. `q` only closes the screen; the next `lazyagent --remote` comes back to the same agents.

```bash
lazyagent engine start [repo]      # start it detached (--foreground: in this terminal)
lazyagent engine status [repo]     # pid, agents, socket, log (--json; exit 3: not running)
lazyagent engine stop [repo]       # stop it and its agents (--if-idle: only with none)
lazyagent engine restart [repo]    # stop and start it (new code); --restore brings back
                                   # exactly the agents it stopped
lazyagent engine logs [repo] -f    # its log (-n LINES, --path)
lazyagent engine restore [repo]    # agents an earlier engine left: list them,
                                   # --all / --id ID to bring them back, --dismiss
```

**Restore.** When an engine ends (stopped, crashed, or the machine rebooted) it leaves a manifest of the agents it ran. The next engine offers them: `lazyagent engine restore`, `Shift+R` in any front-end (or *Restore agents* in the TUI's command palette), or the `restore_agents` MCP tool. Set `[engine] restore_on_start = true` to bring them back automatically. A restored Claude agent resumes its conversation (`--resume`) with the same agent id and session name. Design: [docs/design/engine-split.md](docs/design/engine-split.md).

## Features

### Agent Observability

lazyagent doesn't just scrape terminal output. It taps into each provider's native signals for high-confidence state detection:

| Provider | Signal Source | What It Catches |
|----------|--------------|-----------------|
| **Claude** | Hooks (JSONL logs) | Permission prompts, idle states, task completion |
| **Codex** | App Server Events (JSON-RPC) | Turns, approval requests, failures |
| **Gemini** | Telemetry (file export) + screen detection | Activity, session boundaries |

Statuses are normalized into a clear lifecycle:

| Status | Color | Meaning |
|--------|-------|---------|
| `running` | green | Actively working or calling tools |
| `approving` | yellow | Waiting for user approval |
| `waiting` | yellow | Idle or waiting for input |
| `completed` | cyan | Task finished |
| `failed` | red | Error or turn failure |

<!-- Screenshot placeholder: sidebar showing mixed agent statuses -->

### Worktrees, diffs, PR / CI

- Create, remove, and navigate git worktrees without leaving lazyagent. Each worktree gets its agents, a shell, and a diff view. Custom create/remove commands are supported (see [Configuration](#configuration)).
- The diff covers tracked and untracked files. In the web and desktop apps you can also diff against a base branch and switch between unified and split layouts.
- When `gh` is available, each worktree shows its PR state, review decision, and CI checks (linked, in the apps).

### The orchestrator and messaging between Claude agents

The sidebar's first entry is the repo's orchestrator: a Claude session with lazyagent's MCP tools (wired up as `lazyagent_app`), which can create worktrees and spawn and direct workers for you.

Claude agents that lazyagent spawns get a predictable session name — `<worktree folder>-<agent id>`, e.g. `myrepo-fix-login-a1`, and `<repo>-orchestrator` for the orchestrator. `list_worktrees` and `list_agents` report it as `session_name`. The orchestrator uses Claude Code's built-in `SendMessage` with that name to give workers instructions and `notify_when_idle` to be woken when they finish; workers report back the same way. Messages never land in a permission prompt or a half-typed draft, which typed input (`send_agent_input`, still used for Codex and Gemini) can.

Claude Code holds a message from a session in another permission mode until a human approves it. Spawn the orchestrator and its workers in the same mode, or set `cross_session_inbound = "accept"` (see [Configuration](#configuration)). Accepting means any of your local Claude sessions can instruct the agents, including a bypass-permissions one, so turn it on only if that is what you want.

## Key bindings

### TUI

| Key | Action |
|-----|--------|
| `j` / `k` | Move down / up in sidebar |
| `Ctrl+K` | Focus sidebar |
| `Ctrl+J` | Focus agent pane |
| `Ctrl+D` | Focus diff pane |
| `Ctrl+L` | Focus terminal pane |
| `Alt+]` / `Alt+N` | Next agent tab |
| `Alt+[` / `Alt+P` | Previous agent tab |
| `s` | Spawn an agent in the selected worktree (adds a tab) |
| `x` | Stop the active agent tab |
| `R` | Restore the agents an earlier engine ran |
| `c` | Create new worktree |
| `d` | Remove selected worktree |
| `r` | Refresh worktree list (and every git status) |
| `Alt+G` | Refresh git status of the selected worktree |
| `PageUp` / `PageDown`, mouse wheel | Scroll terminal history |
| `?` | Show help |
| `q` | Quit |

### Web UI and desktop app

They share the TUI's keys (`s`, `x`, `c`, `d`, `r`, `Shift+R`, `?`, `j`/`k` outside a terminal; `Ctrl+K`/`J`/`L`, `Alt+N`/`P`, `Alt+G` anywhere), plus:

| Key | Action |
|-----|--------|
| `g a` / `g w` | The attention queue / the worktree view |
| `Ctrl+.` | The worktree's PR and CI panel |
| `Ctrl+D` | The worktree's diff. **Desktop:** `Ctrl+D` is EOF inside a terminal; there, use `Ctrl+Shift+D` |
| `Ctrl+Enter` | Send: the reply box, a dialog's instruction, a diff comment |
| `Esc` | Close a dialog |
| `Ctrl+=` / `Ctrl+−` / `Ctrl+0` | Desktop: terminal font size larger / smaller / default |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Desktop: copy / paste in a terminal (`Ctrl+C` copies a selection, else sends ^C) |
| `Shift+drag`, `Shift+wheel` | Desktop: select and scroll even when the program uses the mouse |

The desktop app opens on **Home** (`g h`): what needs you (Now, Round, To sit down with), what is in flight, and what you parked. Its keys: `Enter` answer or expand, `o`/`n`/`p` terminal / notes / PR, `x` dismiss, `z` park (1 h, tomorrow 08:00, 1 week, until something changes, indefinitely; `w` the whole work item), `Shift+Z` show parked, `d` delegate to the branch's agent (only what an agent can fix: CI red, a conflict, changes requested, a dead agent), `l` label, `Shift+X` close, `Shift+O`/`u` pass to the orchestrator / take back (not on what only you can do: approve, answer, decide, review, merge, confirm), `m`/`Shift+M` seen / all seen, `Shift+U` revert the orchestrator's decision, `w` why (the rule that put the line there, the facts it read, the rules above it), `f` fold, `?` How lazyagent works (`F1` anywhere). **Guide:** [docs/guides/how-lazyagent-works.md](docs/guides/how-lazyagent-works.md) (zones, the ball, every state, verb and key, your rules, the facts; generated from the rules catalog).

`?` in either shows the full list.

## Configuration

Create a `.lazyagent.toml` in your repository root:

```toml
# Branch to base new worktrees on (default: "master"). When no worktree has it
# checked out, the main worktree's branch is used instead.
default_branch = "main"

[agent]
# Agent CLI to launch: "claude" (default), "codex", or "gemini"
provider = "claude"
# Claude only: what spawned agents do with SendMessage from a session in another
# permission mode — "accept", "hold" or "refuse". Unset keeps Claude Code's
# default, which holds such messages until a human approves them.
# cross_session_inbound = "accept"

[agents]
# A few fixed lines every worker lazyagent spawns gets (when to report with
# status_report, when to write a decision brief with request_decision, keep
# its notes): Claude as an appended system prompt, Codex and Gemini before the
# instruction. Replace the text here, or turn it off with "".
# worker_preamble = ""
# The permission mode agents are spawned in when the spawn names none (also
# read from $XDG_CONFIG_HOME/lazyagent/config.toml, ~/.config by default, for
# every repo; this file wins). "cli" passes nothing: the agent CLI's own
# default, e.g. Claude's `permissions.defaultMode` in your settings (which, as
# "dontAsk", silently denies every tool not on your allowlist). "bypass" is the
# provider's dangerous flag. Claude also takes its --permission-mode values:
# "acceptEdits", "plan", "dontAsk", "auto", "default"; Codex and Gemini start
# in their own default for those. Any other value is ignored with a warning.
# Unset: the desktop spawns with "cli"; MCP's spawn_agent (the
# orchestrator) with "bypass", as before. A restore brings an agent back in the
# mode it ran in. See "Permission modes" below.
# permission_mode = "acceptEdits"

[status]
# A branch's ticket key, which links a feeder's work item (work_items_upsert)
# to its branch: a regex, its first group or else the whole match. Default:
# the generic ABC-123 key.
# ticket_pattern = "(?i)\\b[a-z]+-\\d+"
# Open work items per feeder source; a new one past it is refused (0: no cap).
# max_open_per_source = 50
# The open PRs where someone asked you (or a team of yours) for a review, as
# Round lines: one GraphQL search per repo at the PR poll's pace (2 points of
# GitHub's 5000 an hour). false turns it off.
# review_requests = true
# GitHub Actions runs on the worktrees' branches: one conditional REST call
# per repo (`gh api`, answered by a 304 when nothing changed) this often, in
# seconds (at least 15). A red run on a branch's head with no PR is an inbox
# item; a red PR's item names the workflow and the failed job. 0 turns it off.
# actions_poll_s = 60

[inbox]
# "To sit down with" (decision briefs, changes requested, a finished branch to
# review) shows its count against this; nothing is refused over it.
# deep_max = 5
# Open decision briefs per agent; a third one is refused (0: no limit).
# max_briefs_per_agent = 2
# Opt-in auto-delegation of stalled items to the branch's owner agent (a live
# one gets a fixed brief, a dead one is restored, else one is spawned), or to
# the orchestrator as a claim. Kinds: "conflict_with_base", "agent_dead", and
# "ci_red" (a red GitHub Actions run on the branch's head; its brief carries
# the failed log's tail). At most 2 attempts per item, never on the default
# branch or a teammate's; after 2 failures the item becomes a brief in To sit
# down with. The desktop writes these keys too.
# auto_delegate = []
# auto_delegate_target = "owner"   # or "orchestrator"
# auto_delegate_max_per_day = 10
# Failing checks auto-delegation leaves alone: check, workflow or job names,
# fnmatch globs, any case. A red made only of these is not delegated (Home
# and Why? say "auto-delegation skipped: eval-suite is on the skip list"); with
# other failing checks too, the agent is told to leave these alone. The
# desktop's Auto-delegation menu shows the list. A check that fails at random
# can instead be listed in rules.toml's `[ci] nondeterministic`: off this list,
# its brief asks the agent to investigate and attest (see "Nondeterministic
# suites: attestations"); on both, this list wins and it stays yours.
# auto_delegate_skip_checks = ["eval-suite", "label-*"]
# The permission mode of an agent auto-delegation has to spawn (no agent ran
# on the branch): the worktree's last recorded agent's mode when there is one,
# else this (true: permission prompts bypassed, as spawn_agent's default;
# false: [agents] permission_mode, unless that is "bypass", else "cli").
# auto_delegate_skip_permissions = true

[orchestrator]
# Caps on spawns made with an agent token (0 turns one off).
# max_spawns_per_day = 30
# max_concurrent_agents = 8
# Pass to orchestrator: how long a claim lasts (seconds).
# claim_ttl_s = 7200
# The finished work (agent done, changes requested) of agents the orchestrator
# spawned is held for it this long first, then comes to you as "orchestrator
# did not respond" (minutes; 0 turns it off).
# hold_spawned_ttl_min = 60
# First responder: a worker's brief or question goes to a running orchestrator
# first; it answers within its mandate or escalates. Unanswered after this many
# minutes, the item goes to you.
# escalate_after_min = 10
# You count as present while a client reported input this recently; with no UI
# connected you are away. Present: it answers only small reversible things.
# away_after_min = 15
# Its own answers per item and per day; past one, items come to you.
# max_decisions_per_item_day = 3
# max_decisions_day = 20
# These can only shorten its mandate (merging to the default branch,
# publishing, force-pushing, deleting unpushed work and closing PRs are never
# its to do alone). Actions: answer_quick, answer_reversible, answer_partly,
# relay, restore_owner, ci_rerun, rebase_via_owner, spawn_worker,
# next_project_task, attest_ci.
# away_allow = ["answer_quick", "relay"]
# allow_outward = ["tracker", "github_comment", "pr_create"]

[worktree]
# Custom command templates for worktree management. They run in the repo root,
# and their output shows in a log under the worktree view while they run.
# Available placeholders: {branch}, {name}, {base}, {path}, {repo}, {force}, {extra}
create = "git worktree add -b {branch} ../{name} {base}"
remove = "git worktree remove ../{name}"

[engine]
# Bring back, when the engine starts, the agents the previous one ran when it
# ended (stopped, crashed, or the machine rebooted). Default: only offer them
# (lazyagent engine restore).
# restore_on_start = true
```

### Permission modes

Every spawn resolves one mode, in this order:

1. `permission_mode` given to the spawn (MCP and IPC `spawn_agent`, the
   desktop's spawn dialog, which preselects the configured mode);
2. `skip_permissions` given to the spawn: `true` is `"bypass"`; `false` means
   "not bypass": the configured mode unless that is `"bypass"`, else `"cli"`
   (the TUI's spawn dialog sends one or the other);
3. `[agents] permission_mode` (the repo's `.lazyagent.toml`, else the global
   `config.toml`);
4. the spawning path's own default: `"bypass"` for MCP's `spawn_agent` and a
   bare IPC call (unchanged for orchestrators), `"cli"` for the desktop's
   quick spawn and Delegate → Spawn (the IPC parameter
   `default_permission_mode` names it).

The mode is recorded per agent: `list_agents`, `list_worktrees` and
`list_restorable` show it as `permission_mode`, the desktop's agent tab shows
it when it is not `"cli"`, and a restore (or an auto-delegation spawn in the
same worktree) reuses it. `restore_agents` takes a `permission_mode` to bring
them all back in another one. An agent an older engine recorded has only
"bypass or not": not bypass resolves as `skip_permissions = false` above.

### Issue trackers (optional)

The engine can read the status of the issue linked from a branch name and the
issues assigned to you, read-only, and show them in `lazyagent status` (and the
inbox). Nothing changes until an adapter is enabled in
`~/.config/lazyagent/trackers.toml` (`$XDG_CONFIG_HOME`). A repo's
`.lazyagent/trackers.toml` may override only the `[<adapter>.status]` and
`[<adapter>.qa]` tables.

| Adapter | Branch key | Credentials | Status |
|---|---|---|---|
| `github_issues` | the issue number (`fix-123-login`) | `gh`'s own login | stable |
| `jira` (Jira Cloud) | the issue key (`SHOP-123-login`) | email + API token, by reference | **experimental** (tested against recorded responses only) |

```toml
[jira]
enabled = true
base_url = "https://example.atlassian.net"   # https only
email_env = "JIRA_EMAIL"                     # or email = "you@example.com"
token_env = "JIRA_TOKEN"                     # or keyring = "lazyagent/jira" (user "token")
# projects = ["SHOP"]                        # only these keys in branch names
# jql_assigned = "assignee = currentUser() AND statusCategory != Done"
# poll_s = 300
# max_calls_per_min = 30

[jira.status]   # Jira status name (or its category: "To Do", "In Progress", "Done")
"In Review" = { ball = "others", state = "code review" }
"QA"        = { ball = "others", state = "in QA" }
"Reopened"  = { ball = "you", level = "round", state = "returned" }
```

A status mapped with a `level` opens an inbox item when the issue enters it
and clears it when the issue leaves. The token (an
[Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens))
is never written by lazyagent and is sent only to `base_url`.

**QA.** A `[<adapter>.qa]` table gives your rules the `tracker.qa` fact
(`tested` · `not_required` · `pending` · `failed`; anything not mapped is
unknown, and an unknown never matches). Without one, nothing changes.

```toml
[jira.qa]                        # Jira status name (or its category) → QA
"QA passed"     = "tested"
"Ready to ship" = "tested"
"No QA"         = "not_required"
"In QA"         = "pending"
"QA failed"     = "failed"
# or read a field instead of the status:
# qa_field = "customfield_10042"
# qa_field_values = { "Tested" = "tested", "Not required" = "not_required" }

[github_issues.qa]               # issue labels → QA
"qa-failed" = "failed"
"qa-passed" = "tested"
"no-qa"     = "not_required"
"qa"        = "pending"
```

`qa_field` takes the field's text, a select's option, or a multi-select's
options. When an issue has several mapped labels (or options), the most
conservative wins: `failed`, then `pending`, `tested`, `not_required`, so a
stale "qa-passed" never hides "qa-failed".

### Feeding work items (a planner, a script)

Anything that tracks work elsewhere (a planner with its own board, a
script over an issue tracker) can tell the engine about it with the MCP
tools `work_items_upsert` and `work_items_close` (or the engine methods of
the same names). lazyagent never starts a task on its own: it shows them.

- **One item per URL.** Upserting the same URL again replaces it; optional
  fields left out go back to their defaults, so send the whole item each
  time. At most `[status] max_open_per_source` (50) open items per
  `source`; `expires_at` (Unix time) forgets one on its own.
- **Not started, then a row.** While no local branch carries its ticket key
  (given, or read from the URL with `[status] ticket_pattern`), the item
  adds to Home's "N not started" (with the trackers' assigned issues, once
  per URL). Once a branch such as `SHOP-12-checkout` exists, it joins that
  row with its title, priority and project. A review someone asked of you
  needs no branch: an item whose `url` is the PR's, or whose ticket key is
  in the PR's head branch or title, joins the review request's line.
- **Priority** (`high` · `medium` · `low`) orders the row and its inbox
  items inside their level, after "blocks a live agent".
- **Waiting.** `waiting: {reason?, until?}` parks the row in Parked as
  "waiting (<source>)" or "waiting: <reason>", until `until` (a time,
  `tomorrow`, or a park event) or your next upsert without it. The user's own
  park or unpark wins until you send a different `waiting`.
- **Tracker facts.** For a tracker lazyagent has no adapter for,
  `facts: {"tracker.qa": "tested", "tracker.status": "Ready to ship"}`
  gives your rules `tracker.qa` and `tracker.status` on the linked row
  (and makes `tracker.linked` true). No other names are accepted; when an
  adapter also reads the row's task, the adapter's value wins.
- **Projects.** `project: {id, title, policy?}` groups In flight;
  `depends_on` shows "waits on <title>". With
  `policy.continue_while_review = true`, the engine wakes the orchestrator
  once when a task's dependencies are all merged or in review; it starts it
  within its mandate (away) or proposes it (present). The plan and the
  next-task choice stay with you or your skill.

```python
# From a Claude session with lazyagent's MCP server:
work_items_upsert(
    url="https://tracker.example.com/browse/SHOP-12",
    title="Checkout flow",
    source="planner",
    priority="high",
    project={"id": "shop", "title": "Checkout revamp",
             "policy": {"continue_while_review": True}},
    depends_on=["https://tracker.example.com/browse/SHOP-11"],
)
work_items_upsert(url="https://tracker.example.com/browse/SHOP-14",
                  title="Refunds", source="board",
                  waiting={"reason": "design review"})
work_items_upsert(url="https://tracker.example.com/browse/SHOP-15",
                  title="Gift cards", source="planner",
                  facts={"tracker.qa": "not_required"})
work_items_close(url="https://tracker.example.com/browse/SHOP-11")
```

### Your rules (rules.toml)

Every Home row is decided by a catalog of rules over facts the engine
computes (`pr.review`, `ci.state`, `agents.running`, `tracker.linked`, …):
first match wins, and an unknown fact never matches. The built-in catalog
is today's behaviour; your own files can replace, narrow, add, move or turn
off rules. Rules are data: a small expression language with no functions of
your own, no loops, no I/O and caps (2000 characters, depth 32, 256 parts
per expression; 200 rules and 256 KB per file).

| File | Applies |
|---|---|
| `~/.config/lazyagent/rules.toml` (`$XDG_CONFIG_HOME`) | always: it is yours |
| `<repo>/.lazyagent/rules.toml` | only once you allow its content hash; a changed file stays on its last allowed version until you allow it again |

Order: a locked prefix (merged and closed rows, your park, the inbox queue,
the orchestrator's pass, hold and delegation) that no file can replace or
precede, then the repo's new rules, the global new rules, then the
built-ins. A rule with a built-in's `id` replaces it in place; `and_when`
adds a condition to the rule it replaces; `before` / `after` move a rule;
`disabled = true` turns a non-locked one off. A rule that puts the ball with
you (`ball = "you"`) is an inbox item you can dismiss or park; so is the
built-in Merge.

Who else can act on an item: `delegable` (Delegate, `d`, hands it to the
branch's agent) and `passable` (Pass, `Shift+O`, to the orchestrator). They
default by the verb: Fix CI, Sync, Address review, Resolve conflict,
Relaunch, Check agent, Fix job and Update task are delegable; Approve,
Answer, Decide, Review, Merge and Confirm are not passable; a verb of your
own is neither delegable nor kept from the orchestrator. A rule's `then`
may say either (`delegable = true`); the engine refuses a delegate or a pass
where it is false, and Home does not offer it.

An agent whose turn ends with a question to you in prose ("Should I fix it
in this PR, or open a separate task?") gets an **Asks you** item (verb
Answer, Enter opens its terminal): it is in the inbox queue, so it leads the
row above every rule of yours and the built-in PR rules, and it goes when
you send the agent input or it starts a new turn. A question in a code
block, a table or a quote does not count (Claude Code agents: read from the
turn's last message; other CLIs do not say it yet). The agents lazyagent starts are
told to send a brief (`request_decision`) for a choice instead; a brief
stands for the question its turn ends with.

A team whose "ready to merge" means "approved, CI green, and QA tested it or
it needs none" (`tracker.qa` comes from the trackers' `[<adapter>.qa]`
mapping, or a feeder's `facts`):

```toml
# ~/.config/lazyagent/rules.toml
version = 1

[[rule]]
id = "merge"                     # replaces the built-in "merge"
says = "Merge when approved, CI is fine and QA is done."
when = '''
pr.state == "open" and pr.review == "approved" and pr.mergeable == "mergeable"
and (ci.state in ["green", "none"] or ci.unrelated)   # red only for reasons not the branch's
and tracker.qa in ["tested", "not_required"]
'''
[rule.then]
ball = "you"
level = "round"
verb = "Merge"
action = "open_pr"
state = "merge: approved, QA ok"

[[rule]]
id = "waiting_for_qa"            # without it, such a PR would fall through to the rules below
after = "merge"
when = '''
pr.state == "open" and pr.review == "approved"
and tracker.qa not in ["tested", "not_required", "failed"]
'''
[rule.then]
ball = "others"
state = "approved · waiting for QA"

[[rule]]
id = "qa_failed"
after = "waiting_for_qa"
when = 'pr.state == "open" and tracker.qa == "failed"'
[rule.then]
ball = "you"
level = "round"
verb = "Review"
action = "open_tracker"
state = "returned by QA"
```

A branch with no tracker task, or whose status is not mapped, has an unknown
`tracker.qa`: neither `merge` nor `waiting_for_qa` matches it, and the
built-ins below decide (approved and green with no QA known reads
"approved, CI green", stalled). A team where some branches have no task adds
`or not tracker.linked` to the QA condition. Narrowing the built-in for one
repo:

```toml
# <repo>/.lazyagent/rules.toml
version = 1

[[rule]]
id = "merge"
and_when = 'item.branch != "release" and tracker.linked'
```

**Owner review → human peer review → QA → merge.** When your agents push
and open PRs as you, GitHub never shows your own approval: the PR stays a
draft while you review (looping with the agent), and the agent marks it
ready for review when you approve. A peer then reviews (looping too), QA
tests it if needed, and you merge. `pr.review == "approved"` is not enough
for the peer step, since an app can approve; the recipe reads these facts:

| Fact | What it is |
|---|---|
| `pr.mine` | the PR is yours: its author is the account `gh` is logged in as (unknown until the engine has read it) |
| `pr.draft` | the PR is a draft |
| `pr.human_approvals` | reviewers whose latest review (comments aside) approves: a later "changes requested" or a dismissal cancels it; apps (`Bot` accounts, `…[bot]`), the PR's author and the logins in `[review] not_human` do not count |
| `pr.commits_after_ready` | commits and force pushes since the PR was last marked ready for review; 0 for a draft; a PR opened ready counts from its opening. Clean merges of the base branch are left out (see below) |
| `pr.base_merges_after_ready` | the clean merges of the base branch since then, which `pr.commits_after_ready` left out: for a state text such as `state_expr = 'pr.base_merges_after_ready > 0 ? "base merged since, not counted" : "waiting for peer review"'` |
| `pr.labels` | the label names, lower case: `"qa:skip" in pr.labels` |

```toml
# ~/.config/lazyagent/rules.toml: owner review, peer review, QA, merge
version = 1

[review]
not_human = ["my-review-app"]      # accounts that review but are not people

[[rule]]
id = "merge"
says = "Merge when a person approved what you approved, CI is fine and QA is done."
when = '''
pr.state == "open" and pr.mine and not pr.draft and pr.commits_after_ready == 0
and pr.human_approvals >= 1 and (ci.state in ["green", "none"] or ci.unrelated)
and pr.mergeable == "mergeable" and tracker.qa in ["tested", "not_required"]
'''
[rule.then]
ball = "you"
level = "round"
verb = "Merge"
action = "open_pr"
state = "merge: peer approved, QA ok"

[[rule]]
id = "owner_review"
after = "label"                     # your own label still wins
when = 'pr.state == "open" and pr.mine and pr.draft and agents.running == 0'
[rule.then]
ball = "you"
verb = "Review"
action = "open_pr"
state = "draft · your review"

[[rule]]
id = "owner_review_again"
after = "owner_review"
when = '''
pr.state == "open" and pr.mine and not pr.draft and pr.commits_after_ready > 0
and agents.running == 0
'''
[rule.then]
ball = "you"
verb = "Review"
action = "open_pr"
state = "new commits since your approval"

[[rule]]
id = "waiting_for_peer"
after = "owner_review_again"
when = '''
pr.state == "open" and pr.mine and not pr.draft and pr.human_approvals == 0
and agents.running == 0
'''
[rule.then]
ball = "others"
state = "waiting for peer review"

[[rule]]
id = "waiting_for_qa"
after = "waiting_for_peer"
when = '''
pr.state == "open" and pr.mine and not pr.draft and pr.human_approvals >= 1
and pr.commits_after_ready == 0 and tracker.qa == "pending" and agents.running == 0
'''
[rule.then]
ball = "others"
state = "waiting for QA"
```

While an agent works on the branch the built-in "agent at work" line
decides (each rule asks `agents.running == 0`), and the inbox (changes
requested, CI red, conflicts) still comes first. Pushes after your approval
bring the review back to you even if the agent forgets to turn the PR back
into a draft. A **clean merge of the base branch** ("Update branch", or
`git merge origin/main` with no conflicts) does not count: a merge commit
with one parent on the base (`origin/<base>` or `<base>`) whose tree is
exactly the automatic merge of its parents, checked with local git
(`git merge-tree --write-tree` on git 2.38 or later, a temporary index
below it; never your working tree). A conflict resolved by hand, an extra
edit in the merge, a merge of another branch, or any other commit counts;
so does a force push, unless it changed the branch only by such merges. A
merge GitHub made is fetched once (`git fetch origin <sha> <base>`, which
also moves `origin/<base>`); when it cannot be checked, it counts. The timeline
orders commits by their date, so a commit made before the PR was marked
ready and pushed after it is caught only while the engine is running (it
notices the head move). These facts are unknown when the PR could not be
read: none of these rules matches then, and the built-ins below decide.
`pr.labels`, `pr.human_approvals` and `pr.commits_after_ready` cost one
more GitHub query per PR poll (1 point of the rate limit for up to 25
branches), made only while one of your rules reads them.

Every rule asks `pr.mine`: checking out a teammate's branch (to try it,
or for your review) must not read "waiting for peer review" when you are
the peer. While `pr.mine` is unknown (the engine has not read your login
yet) none of them matches.

**Reviewing others' PRs.** A teammate who asks you (or a team you are in)
for a review gets a Round line of its own, "Review" ·
"review requested by <author>", with the PR's title and the time it was
asked; `p` opens the PR. It needs no branch or worktree: the engine
searches the repo's open PRs (`review-requested:@me`, drafts left out) at
the PR poll's pace. The line goes when you submit a review (approve,
request changes or comment), when the request is withdrawn, or when the PR
closes or merges; asked again, it comes back. A worktree on that branch
gets the line instead of a second one, and the built-ins read a teammate's
PR as theirs (`pr.mine` false: no Merge, no "PR has no reviewer"; dismissed
or parked, the request reads "review requested · set aside"). Your rules
can read `pr.review_requested_from_me` and `pr.author`;
`[status] review_requests = false` turns the search off.

**CI red, but not the branch's fault.** A check that is red on the branch
counts as **unrelated** only when its failing *tests* all fail on the base
too, at the commit the branch is compared with: the same test ids, not the
same red check. A branch that fails one more test than the base, or other
tests, is related and keeps **Fix CI**, which names the new tests. A check
with no test data (no `tests_from`, no artifact, a parser that could not
match the runner's own count, a build or lint failure, another CI's check),
a check that exists only on the branch, or a base with no run at exactly
that commit: related. Suites whose results vary run to run (LLM evals,
noisy benchmarks) are never compared this way: they stay related unless an
agent attests them (below).

```toml
# rules.toml (global or repo): where each check's failing-test ids come from
[ci.check."unit-tests"]                    # a job name (matrix values aside), a job name with them, or "<workflow file>/<job>"; * and ? work
tests_from = ["junit:junit-*", "log:pytest"]   # the first that gives a known set

[ci.check."e2e"]
tests_from = ["log:regex"]
test_regex = '^✗ (?P<test>\S+)'            # RE2 syntax only
total_regex = '(?P<total>\d+) failing'     # the runner's own count, checked

[ci.check."summary"]                       # a job that `needs:` the test jobs and fails when they do
aggregate = true                           # unrelated when the run's other failing jobs all are

[ci_unrelated]
compare_to = "build_base"                  # default: the commit the failing run built on
base_fetches_per_poll = 3                  # Sync's lazy reads of a newer red base run

[ci_unrelated.project."checkout-redesign"]   # a feeder's project id (stacked branches)
compare_to = "origin/main"                 # main's run at the newest main commit under the build base
```

| Key | Meaning |
|---|---|
| `[ci.check."<name>"] tests_from` | `junit:<artifact glob>` (the run's artifacts, every `*.xml`; a matrix job takes the artifact naming its values), `log:pytest`, `log:jest`, `log:vitest`, `log:go`, `log:cargo`, `log:junit-console` (Maven, Gradle), `log:regex` (with `test_regex` and `total_regex`) |
| `[ci.check."<name>"] aggregate` | `true`: an aggregate job (a `summary` / `all-green` that waits on the test jobs and has no tests of its own); `false`: never one; unset: not one, unless `detect_aggregates` is on. Takes no `tests_from`. The first `[ci.check]` whose name matches a job is the one read, so put it before a `*` |
| `[ci_unrelated] compare_to` | `build_base` (default): a `pull_request` run's merge-ref base parent, a `push` run's single merge-base with `origin/<base>`; else a ref, compared at its newest commit that is an ancestor of the build base. A project's `compare_to` wins over the repo file's, which wins over the global one |
| `[ci_unrelated] base_fetches_per_poll` | 3 |
| `[ci_unrelated] detect_aggregates` | `false` (default). `true`: a job with no `aggregate` key may be taken for one from the run's jobs (see below). The repo file's wins over the global one |

When every failing check is unrelated, Fix CI clears and the row carries
`CI red · unrelated (base also red: 2 tests)` (`status_list {fields:
["ci_note"]}`; the CLI and MCP tables add it to the state). If the base's
newest run of that workflow then passes (or no longer fails those tests),
**Sync** asks for the base to be merged in. `lazyagent status --why <key>`
lists each failing check with what decided it, or what was missing. Rules
read `ci.unrelated`, `ci.unrelated_checks`, `ci.related_failures`,
`ci.failing_tests`, `ci.base_point`, `ci.base_failing_tests`,
`ci.base_fixed`, `ci.unrelated_summary` and `ci.failing_tests_of("<check>")`.
Nothing is read until a check has `tests_from`; then, only while a head is
red, one artifact list and download (or the failed log) per failing job and
run attempt, and the same once per base commit, inside the Actions caps.

*Aggregate jobs.* A job that fails because the jobs it `needs:` failed is
unrelated when every other failing job of its run (not an aggregate) is
unrelated, else related naming the first that is not (`summary: waits on
unit-tests (3.12), related: 1 failing test not failing on main at 3f2a9c1`);
with no other failing job in its run (they were cancelled or skipped), it is
related. GitHub's jobs list has no `needs`, so the workflow file is not read:
the run's other failing jobs stand in, which is stricter, and the row's
verdict is the same since they are failing checks too. A job is an
aggregate only when its check says `aggregate = true`: a job wrongly taken
for one could be called unrelated and let a real failure merge. With
`[ci_unrelated] detect_aggregates = true`, a job with no `aggregate` key is
also taken for one when it has no `tests_from`, started only after every
other failing job of its run had ended, and runs at most two steps of its
own; set `aggregate = false` on a job that looks like that and is not.

*Large reports.* A JUnit artifact (at most 20 MB zipped) is read from its
zip one file at a time, however many files it has: a file with no
`<failure` and no `<error` is only scanned for its `<testsuite>` counts,
the others are parsed, and the failures and errors found must add up to the
files' own counts (when every file has them). It stays unknown past 512 MB
of XML, a 32 MB file, 64 MB of failing XML, 50 000 failing cases or 30
seconds. A failed log is streamed: each job keeps its first 1 MB and its
last 4 MB (16 MB a run, reading stops at 256 MB), so the summary and the
totals at the end of a long log are read. When the middle was dropped, only
`pytest`, `jest` and `vitest` (one totals line at the end) are trusted; the
parsers that add totals up over the log (`go`, `cargo`, `junit-console`,
`regex`) say the log was cut. Colour codes are stripped in either form,
the ESC byte or the `^[` that `gh` writes into a pipe.

**Nondeterministic suites: attestations.** A suite whose pass rate varies
run to run cannot be compared by test ids. For the checks you list as
nondeterministic, an agent that has read the suite's history on the base and
run it on its own head files an **attestation** (MCP `ci_attest`): its
evidence, which the engine checks as far as it can, and which your config
decides on. Merging stays yours.

```toml
# rules.toml (global or repo)
[ci]
nondeterministic = ["eval-suite"]          # pass rate varies run to run; attestations are refused for any other check
accept_attestation_for = ["eval-suite"]    # an accepted attestation explains the check (else: recorded, no effect)
attestation_ttl = "72h"                    # also expires on a new commit and when the check's base point moves
min_base_runs = 5                          # defaults for every check
min_repetitions = 3
max_drop = 0.05

[ci.check."eval-suite"]
confirm = "user"                           # default: you confirm each one; "none" accepts a valid one at once
min_base_runs = 8                          # a check's own keys win over [ci]'s
```

| Key | Meaning |
|---|---|
| `[ci] nondeterministic` | Check names (job, job without matrix values, or `<workflow file>/<job>`; `*` and `?` work) whose red may be attested |
| `[ci] accept_attestation_for` | The checks an accepted attestation takes out of `ci.related_failures` |
| `[ci] attestation_ttl` | `"72h"` (default), `"3d"`, or seconds |
| `min_base_runs`, `min_repetitions`, `max_drop` | In `[ci]` or per check: base runs cited (5), runs on the head (3), how far below the base's lowest pass rate the branch's lowest may be (0.05) |
| `[ci.check."<name>"] confirm` | `"user"` (default) or `"none"`; an attestation whose method is not `range` always asks you |

What the engine checks: the check is red on the head and listed; `sha` is
the head; enough runs; every `run_id` is a run of that check's workflow with
that job, base runs on the base branch and the agent's own on the head; pass
rates are counts. It recomputes the range and refuses an attestation whose
own numbers contradict it. Runs the agent made locally are recorded as its
claim and shown as such.

A worked example. CI is red on `9be01d4`: `unit-tests` (2 failing tests) and
`eval-suite`. You press `d` on Fix CI. Mechanism A finds both unit tests
failing on main too; the agent reads 5 runs of `eval-suite` on main (71-78
%), runs it 3 times on its head (74-76 %) and calls `ci_attest`. A
**Confirm** item takes Fix CI's place in Round: `agent a2 says eval-suite's
failure is unrelated: base 5 runs 71-78 %, branch 3 runs 74-76 %; drop 0
points vs max 5`, and the delegation ends `CI failures unrelated (base also
red: 2 tests; eval-suite attested)`. **Accept**: every failing check is
explained, the row carries `CI red · unrelated (base also red: 2 tests;
eval-suite attested by agent a2)`, and your Merge rule (with `ci.unrelated`)
shows **Merge**. **Reject** (with a comment): the comment goes to the agent
(`Attestation att1 for eval-suite rejected by the user: …`) and Fix CI comes
back as `CI red: eval-suite (attestation rejected)`. A new commit expires it:
`CI red: eval-suite (attestation expired)`. Why? (`w`) lists the standing
attestations with **Revoke**. Rules read `ci.attested`,
`ci.attestation_pending` and `ci.first_attestation_pending`.

Auto-delegation (`[inbox] auto_delegate = ["ci_red"]`) of a red listed here
asks the agent to investigate, never to edit the test: read the base's runs,
run the suite on the head (if its instructions define a slot or lock for
running this suite, it requests it before each run and releases it after),
then fix a real regression or attest. A check also on `[inbox]
auto_delegate_skip_checks` is skipped instead: the skip list wins. The
orchestrator may attest only within its mandate (`attest_ci`, while you are
away).

```sh
lazyagent rules check     # errors with file and line (exit 1), and what is in force instead
lazyagent rules show      # the effective rules in order, and where each comes from
lazyagent rules allow     # print the repo file and its hash; then: lazyagent rules allow <hash>
lazyagent rules doc       # "How lazyagent works" as Markdown (--effective: with yours; --write: the repo's docs/guides copy)
lazyagent status --why <branch>   # the rule behind a row: file, line, facts, the rules above it
```

The engine re-reads the files when they change. A file that does not parse
keeps its last good version (also across restarts); a bad rule is skipped
alone and what it replaced stays in force. Home shows one line while there
are errors, naming the rule, its file and line. The MCP tools
`rules_explain` and `rules_catalog` let the orchestrator answer "why is X
in Round?" from the engine.

### Agent prompts (lazyagent prompts)

The agents lazyagent starts get a short preamble; your own sessions and
skills do not. `lazyagent prompts install` adds lazyagent's guidance to your
agent CLIs: a **lazyagent skill** (status reports, decision briefs, notes,
working under an orchestrator, how rules and Why? work), rendered for the
installed version, and a short **pointer** to it at the end of your
instructions file (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
`~/.gemini/GEMINI.md`), for the CLIs it finds. `--skills a,b` also adds one
line to skills of yours (an orchestrator or planner skill).

```sh
lazyagent prompts status      # per target: installed, outdated, modified or absent
lazyagent prompts diff        # what install would change
lazyagent prompts install     # each file's diff, then y / n / q (--dry-run, --yes)
lazyagent prompts uninstall   # remove it again, file by file
```

Nothing is written without your approval of each file's diff; a file that
changed since its diff was shown is refused. Blocks sit between
version-stamped markers, so an update replaces only the block and a block you
edited is shown, not overwritten. Every write is atomic and backed up first
under `$XDG_STATE_HOME/lazyagent/prompts/backups`. `--scope repo` writes the
repo's `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` instead (never the default). In
the desktop: Agents → Prompts…. Details: [docs/guides/agent-prompts.md](docs/guides/agent-prompts.md).

### Environment variables

| Variable | Effect |
|---|---|
| `LAZYAGENT_ENGINE=remote` | Same as `--remote` |
| `LAZYAGENT_ENGINE_SOCKET` | Use this engine socket (no discovery, no auto-start) |
| `LAZYAGENT_ENGINE_AUTOSTART=0` | Never start an engine |
| `LAZYAGENT_DESKTOP_BIN` | The desktop binary `lazyagent desktop` runs (else the one inside the installed wheel, then `lazyagent-desktop` on PATH, then `desktop/target/release/` in a source checkout, rebuilt first with `cargo build --release` when a file under `desktop/` is newer than it); `lazyagent desktop --help` shows which one runs |
| `LAZYAGENT_DESKTOP_PRESENT` | Desktop present mode: `fifo` (vsync), `immediate`, `mailbox`, `novsync` (default), `auto` |
| `LAZYAGENT_DESKTOP_ALL_GPUS=1` | Desktop: keep an idle offload NVIDIA GPU's Vulkan driver loaded (by default it is left out, saving ~1% of a core) |
| `LAZYAGENT_DESKTOP_NO_NOTIFY` | Desktop: no desktop notifications |
| `LAZYAGENT_DESKTOP_FONT`, `LAZYAGENT_DESKTOP_FONT_SIZE` | Desktop terminal font (default DejaVu Sans Mono, 13) |

Logging variables are under [Diagnostic logging](#diagnostic-logging).

## MCP integration

lazyagent ships an MCP server (`lazyagent mcp`) that exposes its worktree and agent tools:

| Tool | What it does |
|---|---|
| `list_worktrees` | Worktrees with their agents, statuses and session names |
| `create_worktree` | New worktree and branch; without `base_branch` it uses `default_branch` when a worktree has it checked out, else the main worktree's branch |
| `remove_worktree` | Remove a worktree (`force` for a dirty one) |
| `spawn_agent` | Start a new agent (instruction, permission mode, resume mode, label) |
| `list_agents` / `get_agent_status` | A worktree's agents / one agent's status |
| `read_agent_output` / `send_agent_input` | Read an agent's screen / type into it |
| `stop_agent` | Stop an agent |
| `list_events` | Status changes since a cursor (to wait on agents) |
| `list_restorable` / `restore_agents` / `dismiss_restorable` | The agents an earlier engine left: list, bring back, stop offering |
| `review_reply` | An agent's answer to a review comment or question from the desktop Diff view (the `comment_id` its prompt gave); shown under the code. Only the agent the comment was sent to may answer it |

Agents that lazyagent itself spawns (worktree agents and the orchestrator) get the server wired up automatically, under the name `lazyagent_app` and running the same lazyagent as the app, so a `lazyagent` server you register yourself can't shadow it. To drive lazyagent from a `claude` session you start by hand, register it once at user scope:

```bash
claude mcp add --scope user lazyagent -- lazyagent mcp
cd /tmp && claude mcp list | grep lazyagent      # verify
```

`python3 -m lazyagent.mcp_server` works too, with the interpreter that installed lazyagent (if `claude mcp add` swallows `-m`, separate args with `--`). The tools talk to a running lazyagent over a Unix socket under `/tmp/lazyagent-<pid>/` and return `No running lazyagent instance found` if none is running.

In an agent lazyagent spawned, both servers are there, and both know which agent is calling: the engine puts the agent's token and socket (`LAZYAGENT_AGENT_TOKEN`, `LAZYAGENT_SOCKET`) in the agent's own environment, and Claude starts every MCP server with it. So `notes_read()` with no arguments reads the caller's own notes on either one (as long as your `lazyagent` server runs a lazyagent recent enough to read the token).

A lazyagent server in the repo's own `.mcp.json` (any entry that runs `lazyagent.mcp_server` or `lazyagent mcp`) is turned off in the agents lazyagent spawns, by name (`disabledMcpjsonServers` in the agent's settings): they have `lazyagent_app` already, and the repo's other servers still run. A user-scope one stays, since settings cannot turn it off for one session; `/mcp disable lazyagent` in a project turns it off there.

## Development

```bash
git clone https://github.com/gioalcamofly/lazyagent.git
cd lazyagent
uv sync --group dev   # also what this repo's .mcp.json runs: .venv/bin/python
uv run pytest
```

| Tests | Run | Notes |
|---|---|---|
| Unit, integration, contract | `uv run pytest` | What CI runs |
| Python matrix (3.10, 3.11, 3.12) | `UV_PROJECT_ENVIRONMENT=.venv-3.10 uv run --python 3.10 --group dev pytest`, and the same with 3.11 / 3.12 | What CI's `test` jobs run; a venv per version keeps `.venv` (3.12, from `.python-version`) as it is. `uvx vermin -t=3.10- --violations --eval-annotations src tests` checks nothing needs more than 3.10 |
| Browser E2E (`e2e` marker) | `uv sync --group dev --group e2e`, then `uv run pytest -m e2e` | Playwright; local only, skipped without Playwright or a Chromium (Playwright's own, else system Chrome) |
| Performance (`perf` marker) | `uv run pytest -m perf` | Deselected by default; checks the [ui-plan](docs/design/ui-plan.md) §7d budgets; local only |
| Desktop app | `cd desktop && cargo test` | Real-engine tests use the repo's `.venv` (or `LAZYAGENT_PYTHON`) and are skipped without it; `cargo test -- --ignored` runs the benches. CI also runs `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` |

Docs: [docs/](docs/README.md) holds the design docs (how things are built) and proposals (what could come next).

### Diagnostic logging

Off by default. To capture a trace of a freeze or of general lag:

```bash
LAZYAGENT_LOG=1 lazyagent              # lifecycle, timers, workers,
                                       # subprocesses, spawn path, watchdog
LAZYAGENT_LOG_LEVEL=debug lazyagent    # ...plus per-chunk terminal traces
```

The log is JSON lines — one object per event, each with a millisecond
timestamp, level, logger, thread, and a `duration_ms` where something was
timed. It goes to `$XDG_STATE_HOME/lazyagent/lazyagent.log`
(`~/.local/state/lazyagent/` by default), rotated at 5 MB with 3 backups.
A detached engine has its own log: `lazyagent engine logs`.

| Variable | Effect |
|---|---|
| `LAZYAGENT_LOG` | `1` to enable, or a level name (`debug`, `info`, …) |
| `LAZYAGENT_LOG_LEVEL` | Level, overriding the above |
| `LAZYAGENT_LOG_FILE` | Write somewhere else (also enables logging) |
| `LAZYAGENT_LOG_WATCHDOG_MS` | Event-loop stall threshold, default `100` |

The watchdog is the useful part when the UI freezes: a thread outside the event
loop notices when the loop stops responding and records `loop.blocked` with a
stack snapshot of what the main thread was doing *while it was still stuck*.

Aggregating afterwards, e.g. the slowest subprocesses:

```bash
jq -r 'select(.event=="subprocess") | [.duration_ms, .cmd] | @tsv' \
  ~/.local/state/lazyagent/lazyagent.log | sort -rn | head
```

## Acknowledgements

- [lazygit](https://github.com/jesseduffield/lazygit) — the inspiration for the UX and workflow
- [Textual](https://github.com/Textualize/textual) — the TUI framework powering the interface
- [pyte](https://github.com/selectel/pyte) — terminal emulation for agent output capture
- [xterm.js](https://xtermjs.org/), [Preact](https://preactjs.com/) — the web UI's terminals and components
- [alacritty_terminal](https://github.com/alacritty/alacritty), [egui](https://github.com/emilk/egui), [wgpu](https://wgpu.rs/) — the desktop app's terminals, UI and rendering

## License

[AGPL-3.0](LICENSE)
