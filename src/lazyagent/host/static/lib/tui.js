// The hosted TUI's terminal: one xterm.js on the host's PTY. Its bytes never
// go through the view library: WebSocket binary frame → term.write, and
// term.onData → WebSocket.

import { Terminal } from "../vendor/xterm/xterm.mjs";
import { FitAddon } from "../vendor/xterm/addon-fit.mjs";
import { binaryBytes, inputFrame } from "./terminals.js";

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
  // Stream 0 is the TUI's PTY.
  term.onData((data) => conn.send(inputFrame(0, data)));
  term.onBinary((data) => conn.send(inputFrame(0, binaryBytes(data))));
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
    /** @param {Uint8Array} data */
    write(data) {
      term.write(data);
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
