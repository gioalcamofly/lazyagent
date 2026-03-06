# Terminal Text Selection — Status & Next Steps

## What we're doing

Enabling mouse-based text selection and copy inside `ScrollableTerminal` panes (agent and terminal tabs). Textual has a built-in selection system, but it requires widgets to opt in by embedding offset metadata in rendered segments and rendering the selection highlight themselves.

## What was implemented

### 1. Offset metadata in `render_line()` (enables position mapping)

`Strip.apply_offsets(x, y)` is called on each rendered strip so Textual's Screen can map mouse coordinates to text positions within the widget. This is what allows Textual to know *where* in the widget's content the user clicked/dragged.

### 2. Selection highlight rendering in `_row_to_strip()`

Textual does **not** automatically render selection highlights — each widget must do it. We check `self.text_selection`, call `selection.get_span(virtual_y)` to find the selected range on each line, and apply the `screen--selection` component style. This follows the same pattern as Textual's built-in `Log` widget.

### 3. `get_selection()` override

Extracts the full text content (scrollback + live screen buffer) from pyte so Textual's `Selection.extract()` can pull the selected substring.

### 4. Ctrl+Shift+C copy handler

Intercepts `ctrl+shift+c` in `on_key()` before the event is forwarded to the PTY. Calls `screen.get_selected_text()` and `app.copy_to_clipboard()`.

## Current state

- Visual selection **works** — clicking and dragging within a terminal pane highlights text correctly.
- Copy **does not work** — `Ctrl+Shift+C` fires, `screen.get_selected_text()` is called, but the copied text doesn't reach the system clipboard.

## What's failing (copy to clipboard)

The likely issue is in the clipboard pipeline. Possible causes:

1. **`app.copy_to_clipboard()` uses OSC 52** — Textual's clipboard support sends an OSC 52 escape sequence to the host terminal. Not all terminal emulators support OSC 52, and some require explicit opt-in (e.g., Alacritty needs `allow_osc52` in config, iTerm2 needs "Allow clipboard access" enabled).

2. **`screen.get_selected_text()` returns `None`** — The selection might be cleared before `ctrl+shift+c` fires (e.g., the key event itself triggers a mouse-up or focus change that clears the selection).

3. **`get_selection()` text extraction mismatch** — Our `get_selection()` builds text from the full scrollback + screen buffer, but `Selection.extract()` uses line/column offsets from the selection. If the offsets don't align with our text structure (e.g., off-by-one in line numbering, trailing whitespace differences), `extract()` may return empty or wrong text.

## Next options to investigate

### Option A: Debug the clipboard pipeline

1. Add logging to the `ctrl+shift+c` handler to see what `screen.get_selected_text()` returns.
2. If it returns text, the issue is OSC 52 support in the host terminal. Try `xclip`/`xsel` as a fallback.
3. If it returns `None`, check `screen.selections` to see if the selection is being tracked.

### Option B: Bypass Textual's clipboard, use system clipboard directly

Instead of `app.copy_to_clipboard()`, use `subprocess` to pipe text to `xclip -selection clipboard` or `xsel --clipboard` on Linux, `pbcopy` on macOS. This bypasses OSC 52 entirely.

```python
import subprocess
text = self.screen.get_selected_text()
if text:
    subprocess.Popen(
        ["xclip", "-selection", "clipboard"],
        stdin=subprocess.PIPE,
    ).communicate(text.encode())
```

### Option C: Debug selection tracking

The selection might be getting cleared on key press. Textual's Screen clears selection on certain events. Investigate whether pressing any key (including Ctrl+Shift+C) clears the selection before our handler runs.

### Option D: Use Textual's built-in copy binding

Textual may already have a built-in copy binding that works with the selection system. Check if there's a default key binding or action for copying selected text that we can leverage instead of implementing our own.
