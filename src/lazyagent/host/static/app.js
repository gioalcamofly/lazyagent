// lazyagent host page: the TUI in xterm.js, a sidebar fed by engine events.
// The host serves these files and proxies a whitelist of v1 methods; see
// lazyagent/host/server.py for the message format.

import { html, render } from "./lib/html.js";
import { Connection, takeToken } from "./lib/conn.js";
import { createStore, SIDEBAR_EVENTS } from "./lib/store.js";
import { basename } from "./lib/format.js";
import { createTui } from "./lib/tui.js";
import { Sidebar } from "./components/sidebar.js";
import { TopBar, Toasts, HelpSheet, TuiPane, Overlay } from "./components/chrome.js";

const store = createStore();

store.notify = (title, body) => {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (document.hasFocus()) return;
  try {
    new Notification(title, { body });
  } catch {
    // Some browsers only allow notifications from a service worker.
  }
};

const conn = new Connection({
  onOpen: () => (store.conn.value = { text: "terminal only", kind: "" }),
  onControl,
  onBinary: (data) => tui.write(data),
  onClose,
});
const tui = createTui(conn);

// ---------------------------------------------------------------- actions

async function command(method, params = {}) {
  try {
    await conn.request(method, params);
  } catch (e) {
    store.toast(e.message, "warning");
  }
  tui.focus();
}

const actions = {
  select: (path) =>
    path === null
      ? command("ui.select_orchestrator")
      : command("ui.select_worktree", { worktree_path: path }),
  focusAgent: (path, agentId) => {
    const target = path === "__orchestrator__" ? { orchestrator: true } : { worktree_path: path };
    command("ui.focus_agent", { ...target, agent_id: agentId });
  },
  toolbar: (action) =>
    ({
      spawn: () => command("ui.open_modal", { modal: "spawn" }),
      create: () => command("ui.open_modal", { modal: "create" }),
      remove: () => command("ui.open_modal", { modal: "remove" }),
      shell: () => command("ui.open_shell"),
      diff: () => command("ui.show_diff"),
    })[action](),
};

// ---------------------------------------------------------------- engine

let refreshTimer = null;
function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(loadSidebar, 100);
}

async function loadSidebar() {
  try {
    const sidebar = await conn.request("get_sidebar_state");
    store.enqueue(() => store.applySidebar(sidebar));
  } catch (e) {
    store.toast(`Sidebar: ${e.message}`, "error");
  }
}

async function onEngineReady() {
  store.engine.value = true;
  store.conn.value = { text: "connected", kind: "ok" };
  try {
    const sidebar = await conn.request("get_sidebar_state");
    store.enqueue(() => store.applySidebar(sidebar));
    await conn.request("subscribe", { since: sidebar.cursor, events: SIDEBAR_EVENTS });
  } catch (e) {
    store.toast(`Engine: ${e.message}`, "error");
  }
}

function onControl(message) {
  switch (message.type) {
    case "hello":
      store.repoRoot.value = message.repo_root;
      document.title = `lazyagent — ${basename(message.repo_root)}`;
      tui.announce();
      tui.focus();
      break;
    case "engine":
      if (message.ready) onEngineReady();
      else {
        store.engine.value = false;
        store.conn.value = { text: "engine gone", kind: "bad" };
      }
      break;
    case "res":
      // An answer nobody waits for: only its error is worth showing.
      if (message.error) store.toast(message.error.message, "error");
      break;
    case "evt":
      store.enqueue(() => {
        if (store.applyEvent(message.event, message.data)) refreshSoon();
      });
      break;
    case "exit":
      store.exited.value = message.code;
      store.conn.value = { text: "exited", kind: "bad" };
      store.overlay.value = `lazyagent exited (code ${message.code}).\nThe host has stopped; run lazyagent host again to restart.`;
      break;
  }
}

function onClose(event) {
  store.engine.value = false;
  if (store.exited.value !== null) return;
  store.conn.value = { text: "disconnected", kind: "bad" };
  store.overlay.value =
    event.code === 1008
      ? "The host refused this page (bad or stale token).\nOpen the URL lazyagent host printed."
      : "Disconnected from lazyagent host.\nReload the page if it is still running.";
}

// ---------------------------------------------------------------- keys

const inTerminal = (target) => target instanceof Element && target.closest(".xterm") !== null;
const inField = (target) =>
  target instanceof Element && target.closest("input, textarea, select, [contenteditable]") !== null;

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && store.help.value) {
    store.help.value = false;
    event.preventDefault();
    return;
  }
  if (inTerminal(event.target) || inField(event.target)) return;
  if (event.key === "?" && !event.ctrlKey && !event.altKey && !event.metaKey) {
    store.help.value = !store.help.value;
    event.preventDefault();
  }
});

// ---------------------------------------------------------------- page

function App() {
  return html`
    <${TopBar} store=${store} />
    <main>
      <${Sidebar} store=${store} actions=${actions} />
      <div class="center">
        <${TuiPane} tui=${tui} />
        <${Overlay} store=${store} />
      </div>
    </main>
    <${Toasts} store=${store} />
    ${store.help.value && html`<${HelpSheet} onClose=${() => (store.help.value = false)} />`}
  `;
}

render(html`<${App} />`, document.body);

const token = takeToken();
if (token) conn.open(token);
else {
  store.overlay.value = "No token: open the URL lazyagent host printed (it ends in #token=…).";
  store.conn.value = { text: "no token", kind: "bad" };
}

// For the browser tests.
window.lazyagent = { store, conn, tui };
