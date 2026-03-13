# Terminal Rendering — Tech Debt & Follow-ups

## Context

After fixing the agent view rendering bugs (scrollback margin guard, incremental UTF-8 decoder, visibility guards, debounced scanning), several areas remain hacky or could be improved.

## High Priority

### 1. `_safe_emulator_run` is a full copy-paste of upstream code

**File:** `src/lazyagent/widgets/scrollable_terminal.py` (lines ~108-152)

We duplicated ~45 lines of `TerminalEmulator._run()` from textual-terminal just to change 1 line (`decoder.decode(raw)` vs `.decode()`). If textual-terminal updates its message protocol, our copy silently diverges.

**Options:**
- **Best:** Submit a PR upstream to textual-terminal adding incremental UTF-8 decoding
- **Alternative:** Fork textual-terminal as a project dependency we control
- Either way, once fixed upstream, delete `_safe_emulator_run` and revert `start()` to use `emulator.start()` directly

### 2. Two separate hidden-widget guards doing the same thing

**Files:** `src/lazyagent/widgets/scrollable_terminal.py`

- `recv()` checks `self.size.height > 0` before updating layout/scroll
- `on_resize()` checks `ncol == 0 or nrow == 0` before sending to PTY

Both guard against the same problem: `ContentSwitcher` setting size to (0,0) on hidden widgets. Could be unified into a single `_is_active` property that both check, or handled more idiomatically via Textual's `display` property / `Show`/`Hide` lifecycle.

## Low Priority

### 3. Extra imports only needed by duplicated code

`codecs`, `fcntl`, `struct`, `termios` are imported at module level in `scrollable_terminal.py` but only used by `_safe_emulator_run`. If/when the upstream fix lands and the function is removed, these imports should be cleaned up too.

## What's Clean (no action needed)

- `ScrollbackScreen.index()` margin guard — minimal, correct
- `_follow_output` flag — reliable across visibility changes
- `_on_stdout` / `_after_stdout_processed` hook pattern — clean extension points
- Debounced sentinel scanning with `asyncio.TimerHandle`
