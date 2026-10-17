// Native terminals: one xterm.js per engine session (agent, orchestrator,
// worktree shell), fed by the session's v1 stream through the host.
//
// The bytes never touch the view library: a binary WebSocket message is
// split into (stream id, offset, Uint8Array view) and handed to term.write
// with no copy and no text decoding. Writes are immediate when none is in
// flight, else coalesced and written once per animation frame.
//
// Size (engine-split §3c, D3): a terminal renders at the PTY's size. It
// claims the size (claim_size with its fitted rows/cols) when it is shown
// focused, when it is typed into while the sizes differ, and when its box
// changes while it owns the size. A terminal out of sight never claims.
//
// Streams (ui-plan §7d-6 P7): attached while shown; a hidden terminal stays
// attached for HIDDEN_GRACE_MS (and only the KEEP_HIDDEN most recent ones),
// then detaches and is disposed. The page going hidden detaches everything
// after the same grace.

import { Terminal } from "../vendor/xterm/xterm.mjs";
import { FitAddon } from "../vendor/xterm/addon-fit.mjs";
import { WebglAddon } from "../vendor/xterm/addon-webgl.mjs";
import { terminalTheme } from "./theme.js";

export const OUTPUT = 1;
export const INPUT = 2;
export const HIDDEN_GRACE_MS = 30000;
export const KEEP_HIDDEN = 3;
export const RECLAIM_INTERVAL_MS = 1000;
const SCROLLBACK = 5000;

/** (type, stream id, offset, data view) of a binary message. */
export function parseFrame(buffer) {
  const view = new DataView(buffer);
  const type = view.getUint8(0);
  const streamId = view.getUint32(1);
  if (type !== OUTPUT) return { type, streamId, offset: 0, data: new Uint8Array(buffer, 5) };
  const offset = view.getUint32(5) * 2 ** 32 + view.getUint32(9);
  return { type, streamId, offset, data: new Uint8Array(buffer, 13) };
}

const encoder = new TextEncoder();

/** An input frame for ``streamId`` carrying ``text`` (UTF-8) or bytes. */
export function inputFrame(streamId, text) {
  const body = typeof text === "string" ? encoder.encode(text) : text;
  const frame = new Uint8Array(5 + body.length);
  frame[0] = INPUT;
  new DataView(frame.buffer).setUint32(1, streamId);
  frame.set(body, 5);
  return frame;
}

/** Bytes of a Latin-1 string (xterm.js onBinary). */
export function binaryBytes(data) {
  const bytes = new Uint8Array(data.length);
  for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 0xff;
  return bytes;
}

/** Whether a WebGL renderer string names a software rasteriser (measured
 * 10× slower than the DOM renderer, ui-plan §7d-4). */
export function isSoftwareRenderer(name) {
  return /swiftshader|llvmpipe|softpipe|software|basic render|mesa offscreen/i.test(name || "");
}

let webglVerdict = null;
/** WebGL for terminals: on a real GPU only. Asked once per page. */
export function webglUsable() {
  if (webglVerdict !== null) return webglVerdict;
  webglVerdict = { ok: false, renderer: "" };
  try {
    const gl = document.createElement("canvas").getContext("webgl2");
    if (gl) {
      const info = gl.getExtension("WEBGL_debug_renderer_info");
      const renderer = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
      webglVerdict = { ok: !isSoftwareRenderer(renderer), renderer: String(renderer) };
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    }
  } catch {
    // No WebGL: the DOM renderer it is.
  }
  return webglVerdict;
}

/** Writes to an xterm.js terminal: immediate when none is pending, else
 * queued and written once per frame (or 50 ms when frames do not run). */
export class Writer {
  constructor(write) {
    this.write = write; // (Uint8Array, done) => void
    this.queue = [];
    this.queued = 0;
    this.inFlight = false;
    this.scheduled = false;
    this.flush = this.flush.bind(this);
    this.done = this.done.bind(this);
  }

  push(chunk) {
    if (!this.inFlight && this.queue.length === 0) {
      this.inFlight = true;
      this.write(chunk, this.done);
      return;
    }
    this.queue.push(chunk);
    this.queued += chunk.length;
    this.schedule();
  }

  schedule() {
    if (this.scheduled) return;
    this.scheduled = true;
    requestAnimationFrame(this.flush);
    this.timer = setTimeout(this.flush, 50);
  }

  flush() {
    if (!this.scheduled) return;
    this.scheduled = false;
    clearTimeout(this.timer);
    if (this.inFlight || this.queue.length === 0) return;
    let chunk;
    if (this.queue.length === 1) chunk = this.queue[0];
    else {
      chunk = new Uint8Array(this.queued);
      let at = 0;
      for (const part of this.queue) {
        chunk.set(part, at);
        at += part.length;
      }
    }
    this.queue = [];
    this.queued = 0;
    this.inFlight = true;
    this.write(chunk, this.done);
  }

  done() {
    this.inFlight = false;
    if (this.queue.length) this.schedule();
  }

  clear() {
    this.queue = [];
    this.queued = 0;
  }
}

/** One engine session's terminal. */
export class SessionTerminal {
  constructor(manager, sessionId) {
    this.manager = manager;
    this.sessionId = sessionId;
    this.streamId = null;
    // Offset after the last byte written, null before the first attach.
    this.received = null;
    this.attaching = null;
    this.closed = null; // {exit_code} once the session is gone
    this.visible = false;
    this.focused = false;
    this.hiddenAt = 0;
    this.pty = { rows: 24, cols: 80 };
    this.owner = false;
    this.reclaimedAt = -Infinity;
    this.renderer = "dom";

    this.element = document.createElement("div");
    this.element.className = "native-term";
    this.element.dataset.session = sessionId;
    this.term = new Terminal({
      fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono"),
      fontSize: 13,
      cursorBlink: false,
      scrollback: SCROLLBACK,
      theme: terminalTheme(),
      allowProposedApi: false,
    });
    this.fit = new FitAddon();
    this.term.loadAddon(this.fit);
    this.writer = new Writer((data, done) => this.term.write(data, done));
    this.term.onData((data) => this.input(encoder.encode(data)));
    this.term.onBinary((data) => this.input(binaryBytes(data)));
    this.opened = false;
    this.resizeObserver = new ResizeObserver(() => this.refit());
  }

  /** Put the terminal in ``container`` and show it. */
  show(container) {
    if (this.element.parentElement !== container) container.append(this.element);
    this.element.hidden = false;
    this.visible = true;
    if (!this.opened) {
      this.term.open(this.element);
      this.opened = true;
      this.term.textarea?.addEventListener("focus", () => this.onFocus(true));
      this.term.textarea?.addEventListener("blur", () => this.onFocus(false));
      this.loadRenderer();
      this.resizeObserver.observe(this.element);
    }
    this.manager.touched(this);
    this.ensureAttached();
    this.refit();
  }

  hide() {
    if (!this.visible) return;
    this.visible = false;
    this.focused = false;
    this.element.hidden = true;
    this.hiddenAt = performance.now();
    this.manager.touched(this);
  }

  loadRenderer() {
    const verdict = webglUsable();
    if (!verdict.ok || this.manager.forceDom) return;
    try {
      const webgl = new WebglAddon();
      webgl.onContextLoss(() => {
        webgl.dispose();
        this.renderer = "dom";
      });
      this.term.loadAddon(webgl);
      this.renderer = "webgl";
    } catch {
      this.renderer = "dom";
    }
  }

  focus() {
    this.term.focus();
  }

  onFocus(focused) {
    this.focused = focused;
    if (focused && this.visible) this.claim();
  }

  /** Rows and columns that fit the box, or null when not laid out. */
  proposed() {
    if (!this.visible || !this.opened) return null;
    try {
      const dims = this.fit.proposeDimensions();
      if (!dims || !dims.rows || !dims.cols || Number.isNaN(dims.rows)) return null;
      return { rows: Math.max(2, dims.rows), cols: Math.max(2, dims.cols) };
    } catch {
      return null;
    }
  }

  refit() {
    if (!this.visible) return;
    if (this.owner || this.focused) this.claim();
  }

  /** Ask for the PTY to match our box (we become the size owner). Returns
   * the pending claim, or null when there is nothing to change. */
  claim() {
    if (this.closed || this.streamId === null) return null;
    const want = this.proposed();
    if (!want) return null;
    this.owner = true;
    if (want.rows === this.pty.rows && want.cols === this.pty.cols) return null;
    const pending = this.claiming;
    if (pending && pending.rows === want.rows && pending.cols === want.cols) return pending.done;
    const claim = { ...want };
    claim.done = this.manager.conn
      .request("claim_size", { session_id: this.sessionId, ...want })
      .catch(() => {})
      .finally(() => {
        if (this.claiming === claim) this.claiming = null;
      });
    this.claiming = claim;
    return claim.done;
  }

  setPtySize(rows, cols) {
    this.pty = { rows, cols };
    if (this.term.rows !== rows || this.term.cols !== cols) this.term.resize(cols, rows);
  }

  input(bytes) {
    if (this.closed || this.streamId === null) return;
    // Keys typed while the PTY has another size wait for our claim to be
    // answered, so the program sees the resize before the key.
    const claimed = this.claim() || this.claiming?.done;
    if (claimed || this.held) {
      if (!this.held) {
        this.held = [];
        const release = () => {
          const held = this.held;
          this.held = null;
          if (this.streamId === null || this.closed) return;
          for (const part of held) this.manager.conn.send(inputFrame(this.streamId, part));
        };
        (claimed || Promise.resolve()).then(release);
      }
      this.held.push(bytes);
      return;
    }
    this.owner = true;
    this.manager.conn.send(inputFrame(this.streamId, bytes));
  }

  // -------------------------------------------------------------- stream

  ensureAttached() {
    if (this.streamId !== null || this.attaching || this.closed) return this.attaching;
    const params = { session_id: this.sessionId };
    if (this.received !== null) params.from_offset = this.received;
    this.attaching = this.manager.conn
      .request("attach_stream", params)
      .then((result) => this.attached(result))
      .catch((error) => {
        if (error.code === "VALIDATION_ERROR") this.markClosed(null);
      })
      .finally(() => {
        this.attaching = null;
      });
    return this.attaching;
  }

  attached(result) {
    if (this.disposed) {
      this.manager.conn.request("detach_stream", { stream_id: result.stream_id }).catch(() => {});
      return;
    }
    this.streamId = result.stream_id;
    this.mode = result.mode;
    this.manager.byStream.set(result.stream_id, this);
    this.writer.clear();
    this.setPtySize(result.size.rows, result.size.cols);
    if (result.mode === "snapshot") {
      this.term.reset();
      if (result.snapshot) this.writer.push(encoder.encode(result.snapshot));
    }
    this.received = result.offset;
    if (this.visible && (this.focused || this.owner)) this.claim();
  }

  output(offset, data) {
    if (offset !== this.received) {
      // A gap: start again from what we have.
      this.reattach();
      return;
    }
    this.received = offset + data.length;
    this.writer.push(data);
  }

  reattach() {
    this.dropStream();
    if (this.visible || this.manager.keepsAttached(this)) this.ensureAttached();
  }

  dropStream() {
    if (this.streamId !== null) this.manager.byStream.delete(this.streamId);
    this.streamId = null;
  }

  detach() {
    if (this.streamId === null) return;
    const streamId = this.streamId;
    this.dropStream();
    this.manager.conn.request("detach_stream", { stream_id: streamId }).catch(() => {});
  }

  markClosed(exitCode) {
    this.dropStream();
    this.closed = { exit_code: exitCode };
    this.manager.onClosed?.(this);
  }

  dispose() {
    this.disposed = true;
    this.detach();
    this.resizeObserver.disconnect();
    this.term.dispose();
    this.element.remove();
  }
}

export class TerminalManager {
  /** @param {import("./conn.js").Connection} conn */
  constructor(conn) {
    this.conn = conn;
    this.sessions = new Map();
    this.byStream = new Map();
    this.forceDom = false;
    this.onClosed = null;
    this.sweeper = setInterval(() => this.sweep(), 5000);
    document.addEventListener("visibilitychange", () => {
      this.pageHiddenAt = document.hidden ? performance.now() : 0;
      if (!document.hidden) {
        for (const terminal of this.sessions.values()) {
          if (terminal.visible) terminal.ensureAttached();
        }
      }
    });
    this.pageHiddenAt = 0;
  }

  get(sessionId) {
    let terminal = this.sessions.get(sessionId);
    if (!terminal) {
      terminal = new SessionTerminal(this, sessionId);
      this.sessions.set(sessionId, terminal);
    }
    return terminal;
  }

  touched() {
    this.recent = [...this.sessions.values()]
      .filter((t) => !t.visible && t.streamId !== null)
      .sort((a, b) => b.hiddenAt - a.hiddenAt);
  }

  /** Hidden terminals that keep their stream: the most recent few. */
  keepsAttached(terminal) {
    return (this.recent || []).slice(0, KEEP_HIDDEN).includes(terminal);
  }

  /** Detach and dispose what has been out of sight too long. */
  sweep(now = performance.now()) {
    const pageGone = this.pageHiddenAt && now - this.pageHiddenAt > HIDDEN_GRACE_MS;
    this.touched();
    for (const terminal of [...this.sessions.values()]) {
      if (terminal.visible && !pageGone) continue;
      const stale = now - terminal.hiddenAt > HIDDEN_GRACE_MS || !this.keepsAttached(terminal);
      if (pageGone && terminal.visible) {
        terminal.detach();
      } else if (!terminal.visible && stale) {
        this.sessions.delete(terminal.sessionId);
        terminal.dispose();
      }
    }
  }

  /** Back from a hidden page or a reconnect: every shown terminal
   * re-attaches from its last byte (a tail when the engine still has it). */
  resume() {
    for (const terminal of this.sessions.values()) {
      terminal.dropStream();
      if (terminal.visible) terminal.ensureAttached();
    }
  }

  /** Dispose the terminals of sessions no longer running. */
  retain(sessionIds) {
    for (const [id, terminal] of [...this.sessions]) {
      if (!sessionIds.has(id)) {
        this.sessions.delete(id);
        terminal.dispose();
      }
    }
  }

  onFrame(buffer) {
    const frame = parseFrame(buffer);
    const terminal = this.byStream.get(frame.streamId);
    if (terminal && frame.type === OUTPUT) terminal.output(frame.offset, frame.data);
  }

  onEvent(name, data) {
    const terminal = this.byStream.get(data.stream_id);
    if (!terminal) return;
    if (name === "stream.resized") {
      terminal.setPtySize(data.rows, data.cols);
      // Someone else took the size while this terminal shows the session
      // and owned it: typically the hosted TUI, whose view claims when
      // Textual lays it out, late under load. Take it back, at most once a
      // second, so two clients that both insist cannot SIGWINCH the agent
      // in a loop; never from a hidden page.
      const now = performance.now();
      const want = terminal.owner && terminal.visible ? terminal.proposed() : null;
      const foreign = want && (want.rows !== data.rows || want.cols !== data.cols);
      if (foreign && !document.hidden && now - terminal.reclaimedAt > RECLAIM_INTERVAL_MS) {
        terminal.reclaimedAt = now;
        terminal.claim();
      }
    }
    else if (name === "stream.reset") {
      terminal.dropStream();
      terminal.received = data.offset;
      if (terminal.visible || this.keepsAttached(terminal)) terminal.ensureAttached();
    } else if (name === "stream.closed") terminal.markClosed(data.exit_code ?? null);
  }

  applyTheme(theme) {
    for (const terminal of this.sessions.values()) terminal.term.options.theme = theme;
  }
}
