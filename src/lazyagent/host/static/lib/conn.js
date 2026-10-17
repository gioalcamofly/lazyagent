// The page's one WebSocket to the host (lazyagent/host/server.py has the
// message format): the token first, then v1 requests matched by id, pushed
// control messages, and binary frames.

/** The token from the URL fragment (then dropped from the address bar),
 * else the one this tab kept from an earlier load. */
export function takeToken() {
  const match = /(?:^#|&)token=([^&]+)/.exec(location.hash);
  let token = match ? decodeURIComponent(match[1]) : null;
  try {
    if (token) sessionStorage.setItem("lazyagent-token", token);
    else token = sessionStorage.getItem("lazyagent-token");
  } catch {
    // Storage blocked: the token only lives in this page load.
  }
  if (match) history.replaceState(null, "", location.pathname);
  return token;
}

export class RequestError extends Error {
  constructor(error) {
    super(error.message);
    this.code = error.code;
    this.data = error.data;
  }
}

export class Connection {
  /**
   * @param {{onOpen?: () => void, onControl: (message: any) => void,
   *   onBinary: (data: ArrayBuffer) => void, onClose: (event: CloseEvent) => void}} handlers
   */
  constructor(handlers) {
    this.handlers = handlers;
    this.socket = null;
    this.nextId = 1;
    this.pending = new Map();
  }

  open(token) {
    const socket = new WebSocket(`ws://${location.host}/ws`);
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ type: "auth", token }));
      this.handlers.onOpen?.();
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data !== "string") {
        this.handlers.onBinary(event.data);
        return;
      }
      const message = JSON.parse(event.data);
      if (message.type === "res" && this.pending.has(message.id)) {
        const waiter = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) waiter.reject(new RequestError(message.error));
        else waiter.resolve(message.result);
        return;
      }
      this.handlers.onControl(message);
    });
    socket.addEventListener("close", (event) => {
      for (const waiter of this.pending.values()) {
        waiter.reject(new RequestError({ code: "DISCONNECTED", message: "disconnected" }));
      }
      this.pending.clear();
      this.handlers.onClose(event);
    });
  }

  get isOpen() {
    return this.socket !== null && this.socket.readyState === WebSocket.OPEN;
  }

  /** Send a text or binary message as is; dropped while not connected. */
  send(message) {
    if (this.isOpen) this.socket.send(message);
  }

  sendJson(message) {
    this.send(JSON.stringify(message));
  }

  /** A v1 request through the host; resolves with its result. */
  request(method, params = {}) {
    const id = String(this.nextId++);
    return new Promise((resolve, reject) => {
      if (!this.isOpen) {
        reject(new RequestError({ code: "DISCONNECTED", message: "not connected" }));
        return;
      }
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ type: "req", id, method, params }));
    });
  }
}
