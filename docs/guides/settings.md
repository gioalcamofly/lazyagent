# Settings

*File → Settings…* in the desktop app edits lazyagent's settings that live
in config files. It is a list of sections; each option has a label, one line
on what it does, its scope and the file that holds it.

| Section | Option | Key | Scopes |
|---|---|---|---|
| Agents | Default permission mode for agents the engine starts | `[agents] permission_mode` | global, this repo |
| Inbox | Seconds without output before an agent counts as hung | `[inbox] hang_seconds` | global, this repo |

## Scopes and files

- **Global**: `$XDG_CONFIG_HOME/lazyagent/config.toml` (`~/.config` by
  default), for every repo. Created, with its folder, on the first save.
- **This repo**: the repo's `.lazyagent.toml`.

The repo's value wins. The page shows the value in force and where it comes
from ("from this repo's …", "from your global …", or "the default"), and,
with Global picked while the repo sets its own, says that the repo's wins.
**Remove from this file** drops the key from the picked file, so the other
file's value, else the default, applies.

Saving goes through the engine (`settings_set`); the page never writes a file
itself. The engine replaces only that key's line (an inline comment after it
is kept, as are every other line and comment), parses the result back before
it replaces the file, and re-reads its config at once: the next spawn uses
the new value, no restart. An unknown key, scope or value is refused, and the
file is left as it was. After a save the page reads the settings back, and
the app reads the engine's config again, so the spawn dialogs start from the
new value.

## Default permission mode for agents the engine starts

The mode used whenever a spawn names none: Home's quick spawn, Delegate →
Spawn, auto-delegation spawns (when no agent was recorded in the worktree)
and restores of agents with no recorded mode. Values:

| Value | Meaning |
|---|---|
| `cli` | "CLI default (your agent CLI's own setting)": nothing is passed, e.g. Claude's `permissions.defaultMode` applies |
| `bypass` | The provider's dangerous flag (skip permissions) |
| `acceptEdits`, `plan`, `dontAsk`, `auto`, `default` | Claude's `--permission-mode` values; Codex and Gemini start in their own default |

The spawn and create dialogs have a **Skip permissions** box: it starts
checked only when this mode is `bypass`. Unchecked, the agent starts in this
mode, unless it is `bypass`, then in `cli`. See "Permission modes" in the
README for the full order.

## Seconds without output before an agent counts as hung

How long a working agent may go with no output before Home shows it as
"agent may hang". On Linux an agent whose process tree used CPU since the
last check (a long build or eval suite in the foreground) does not count.
A whole number of seconds, at least 60, typed in a box; a value below that
or not a whole number shows a hint and cannot be saved, and the engine
refuses one too.

Order: the repo's `.lazyagent.toml` `[inbox] hang_seconds`, else the global
`config.toml`'s `[inbox] hang_seconds`, else the engine's
`LAZYAGENT_HANG_SECONDS` environment variable, else 600. The page's "the
default" is the last two: what applies when neither file sets it. An invalid
value in the global file is ignored with a warning in the engine's log (in
the repo's file it is ignored too); the page shows it as not set there.

## For other clients

`settings_get {repo?}` returns `{repo, files: {global, repo}, settings: [{key,
type, value, source, global, repo, default, choices, min?}]}`. `type` is
`choice` (a string, one of `choices`) or `int` (a JSON number, at least `min`;
`choices` empty). `settings_set {key, scope, value}` (value `null` removes it
from that file) writes and returns the same.
`settings_set` is the user's (a call with an agent's token is refused); the
engine announces both with the feature `settings`.
