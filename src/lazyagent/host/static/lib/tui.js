// The hosted TUI's terminal: one xterm.js on the host's PTY. Its bytes never
// go through the view library: WebSocket binary frame → term.write, and
// term.onData → WebSocket.

import { Terminal } from "../vendor/xterm/xterm.mjs";
import { FitAddon } from "../vendor/xterm/addon-fit.mjs";

// The TUI paints its own (dark) palette over the whole screen, so its
// terminal stays dark in both page themes: a light background would only
// show as a frame around it.
const TUI_THEME = {
  background: "#121212",
  foreground: "#e0e0e0",
  cursor: "#e0e0e0",
  selectionBackground: "#0178d466",
};

/** @param {import("./conn.js").Connection} conn */
export function createTui(conn) {
  const term = new Terminal({
    fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono"),
    fontSize: 13,
    cursorBlink: false,
    scrollback: 1000,
    theme: TUI_THEME,
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  const encoder = new TextEncoder();

  term.onData((data) => conn.send(encoder.encode(data)));
  term.onBinary((data) => {
    const bytes = new Uint8Array(data.length);
    for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 0xff;
    conn.send(bytes);
  });
  term.onResize(({ rows, cols }) => conn.sendJson({ type: "resize", rows, cols }));

  return {
    term,
    /** @param {HTMLElement} element @param {HTMLElement} pane */
    mount(element, pane) {
      term.open(element);
      new ResizeObserver(() => this.refit()).observe(pane);
    },
    refit() {
      try {
        fit.fit();
      } catch {
        // Not laid out yet.
      }
    },
    /** @param {ArrayBuffer} data */
    write(data) {
      term.write(new Uint8Array(data));
    },
    /** Tell the host our size and ask the TUI for a full repaint. */
    announce() {
      this.refit();
      conn.sendJson({ type: "resize", rows: term.rows, cols: term.cols, redraw: true });
    },
    focus() {
      term.focus();
    },
  };
}
