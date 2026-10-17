// The hosted TUI's terminal: one xterm.js on the host's PTY. Its bytes never
// go through the view library: WebSocket binary frame → term.write, and
// term.onData → WebSocket.

import { Terminal } from "../vendor/xterm/xterm.mjs";
import { FitAddon } from "../vendor/xterm/addon-fit.mjs";
import { effect } from "./html.js";
import { resolvedTheme, terminalTheme } from "./theme.js";

/** @param {import("./conn.js").Connection} conn */
export function createTui(conn) {
  const term = new Terminal({
    fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono"),
    fontSize: 13,
    cursorBlink: false,
    scrollback: 1000,
    theme: terminalTheme(),
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
  effect(() => {
    resolvedTheme.value;
    term.options.theme = terminalTheme();
  });

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
