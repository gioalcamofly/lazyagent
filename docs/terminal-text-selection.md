# Terminal Text Selection — Status & Next Steps

## What we're doing

Enabling mouse-based text selection and copy inside `ScrollableTerminal` panes (agent and terminal tabs).

## Architecture: Widget-local selection

We use a **widget-local** selection system instead of Textual's built-in cross-widget selection. Textual's Screen-level selection spans across all widgets in the drag region, which causes text in adjacent panes (agent terminal, shell terminal) to be highlighted when dragging within a single pane.

### How it works

1. **`ALLOW_SELECT = False`** — Opts out of Textual's Screen-level selection tracking entirely. This prevents the cross-widget spatial-map selection in `Screen._watch__select_end`.

2. **Mouse event handlers** (`on_mouse_down`, `on_mouse_move`, `on_mouse_up`) — Track selection start/end as `Offset` coordinates in virtual space (scrollback + screen). `capture_mouse()` is used during drag to keep events confined to the originating widget.

3. **`_local_selection`** property — Returns a `Selection` object from the widget's own `_sel_start`/`_sel_end`, used by `_row_to_strip()` to render highlights and by `_selected_text()` to extract content.

4. **`Ctrl+Shift+C`** — Extracts text via `_selected_text()` and calls `app.copy_to_clipboard()`.

## Current state

- Visual selection **works** — clicking and dragging within a terminal pane highlights text correctly, confined to that pane only.
- Copy uses `app.copy_to_clipboard()` (OSC 52). Terminal emulators may need OSC 52 enabled (Alacritty: `allow_osc52`, iTerm2: "Allow clipboard access").

## Remaining items

- **System clipboard fallback** — If OSC 52 isn't supported, fall back to `xclip`/`xsel` on Linux, `pbcopy` on macOS.
- **Double-click word selection** — Select the word under the cursor on double-click.
- **Selection auto-scroll** — When dragging past the top/bottom edge, scroll the terminal.
