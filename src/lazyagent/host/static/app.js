// lazyagent host page: native terminals for the agents and shells, a
// sidebar fed by engine events, and the hosted TUI as one more tab.
// The host serves these files and proxies a whitelist of v1 methods; see
// lazyagent/host/server.py for the message format.

import { html, render, effect } from "./lib/html.js";
import { Connection, takeToken } from "./lib/conn.js";
import { createStore, SIDEBAR_EVENTS } from "./lib/store.js";
import { ORCHESTRATOR_KEY, agentLabel, basename } from "./lib/format.js";
import { createTui } from "./lib/tui.js";
import { TerminalManager, parseFrame } from "./lib/terminals.js";
import { resolvedTheme, terminalTheme } from "./lib/theme.js";
import { Sidebar } from "./components/sidebar.js";
import { Center } from "./components/center.js";
import { TopBar, Toasts, HelpSheet, TuiPane, Overlay } from "./components/chrome.js";
import { SpawnDialog, CreateDialog, RemoveDialog, StopDialog } from "./components/dialogs.js";
import { JobDrawer } from "./components/jobs.js";
import { AttentionView, PrPanel } from "./components/attention.js";
import { ciFailurePrompt } from "./lib/attention.js";

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
  onBinary,
  onClose,
});
const tui = createTui(conn);
const terminals = new TerminalManager(conn);
effect(() => {
  resolvedTheme.value;
  terminals.applyTheme(terminalTheme());
});

function onBinary(buffer) {
  // Stream 0 is the hosted TUI; the rest are this page's streams.
  if (new DataView(buffer).getUint32(1) === 0) tui.write(parseFrame(buffer).data);
  else terminals.onFrame(buffer);
}

// ---------------------------------------------------------------- actions

const keyOf = (path) => (path === null ? ORCHESTRATOR_KEY : path);

async function command(method, params = {}) {
  try {
    await conn.request(method, params);
    return true;
  } catch (e) {
    store.toast(e.message, "warning");
    return false;
  }
}

/** Tell the hosted TUI to show what the page selected (TUI tab only). */
function tuiFollow() {
  tuiBlanked = false;
  const path = store.selected.value;
  if (path === undefined) return command("ui.blank");
  return path === null
    ? command("ui.select_orchestrator")
    : command("ui.select_worktree", { worktree_path: path });
}

// Whether the last thing the page asked the TUI to show was nothing.
let tuiBlanked = false;

/** The hosted TUI is out of sight: it renders nothing (ui.blank). */
function tuiBlank() {
  const view = store.view.value;
  const blank = view && !view.orchestrator && view.worktree_path === null && view.focus !== "modal";
  if (!store.engine.value || (blank && tuiBlanked)) return;
  tuiBlanked = true;
  command("ui.blank");
}

// The latest focus request; an older one still retrying gives up.
let focusRequest = 0;

/** Give the keyboard to a session's terminal once it is on screen: tried
 * each frame (a busy page may not have shown it yet), for up to a second,
 * and dropped as soon as the keyboard goes anywhere else meanwhile. */
function focusTerminal(sessionId) {
  const request = ++focusRequest;
  const from = document.activeElement;
  let tries = 0;
  const step = () => {
    if (request !== focusRequest) return;
    const now = document.activeElement;
    const terminal = terminals.sessions.get(sessionId);
    if (now !== from && now !== document.body && !terminal?.element.contains(now)) return;
    if (terminal?.visible && terminal.opened) {
      terminal.focus();
      if (terminal.element.contains(document.activeElement)) return;
    }
    if (++tries < 60) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function focusShown() {
  if (store.centerView.value === "tui") {
    requestAnimationFrame(() => tui.focus());
    return;
  }
  const agent = store.shownAgent();
  if (agent) focusTerminal(agent.session_id);
  else if (store.shellOpen.value) focusShell();
}

function focusShell() {
  const path = store.selected.value;
  const id = path ? store.shells.value[path] : undefined;
  if (id) focusTerminal(id);
}

async function ensureShell(path) {
  if (!path || store.shells.value[path]) return;
  try {
    const shell = await conn.request("open_shell", { worktree_path: path });
    store.shells.value = { ...store.shells.value, [path]: shell.session_id };
  } catch (e) {
    store.toast(`Shell: ${e.message}`, "warning");
  }
}

const actions = {
  select(path, { focus = true } = {}) {
    store.selected.value = path;
    if (store.centerView.value === "attention") store.centerView.value = "native";
    if (store.centerView.value === "tui") tuiFollow();
    if (store.shellOpen.value && path) ensureShell(path);
    if (focus) focusShown();
  },
  focusAgent(path, agentId) {
    store.activeAgent.value = { ...store.activeAgent.value, [path]: agentId };
    const target = path === ORCHESTRATOR_KEY ? null : path;
    if (store.centerView.value === "tui") {
      store.selected.value = target;
      command("ui.focus_agent", target === null ? { orchestrator: true, agent_id: agentId } : { worktree_path: target, agent_id: agentId });
      return;
    }
    actions.select(target);
  },
  showAgent(agentId) {
    const path = store.selected.value;
    if (path === undefined) return;
    store.activeAgent.value = { ...store.activeAgent.value, [keyOf(path)]: agentId };
    if (store.centerView.value === "tui") actions.showNative();
    focusShown();
  },
  cycleAgent(step) {
    const agents = store.selectedAgents();
    if (agents.length === 0) return;
    const shown = store.shownAgent();
    const at = agents.findIndex((a) => a.agent_id === shown?.agent_id);
    const next = agents[(at + step + agents.length) % agents.length];
    actions.showAgent(next.agent_id);
  },
  showTui() {
    store.centerView.value = "tui";
    tuiFollow();
    requestAnimationFrame(() => {
      tui.announce();
      tui.focus();
    });
  },
  showNative() {
    store.centerView.value = "native";
    tuiBlank();
    focusShown();
  },
  toggleShell() {
    const path = store.selected.value;
    if (!path) return;
    store.shellOpen.value = !store.shellOpen.value;
    if (store.shellOpen.value) {
      if (store.centerView.value === "tui") actions.showNative();
      ensureShell(path).then(focusShell);
    } else focusShown();
  },
  async toolbar(action) {
    const path = store.selected.value;
    if (action === "shell") return actions.toggleShell();
    if (action === "diff") {
      // Until U5 the diff is the TUI's.
      if (!path) return;
      store.centerView.value = "tui";
      if (!(await tuiFollow())) return;
      requestAnimationFrame(() => {
        tui.announce();
        tui.focus();
      });
      command("ui.show_diff", { worktree_path: path });
      return;
    }
    actions.openDialog(action);
  },
  openDialog(kind) {
    if (store.dialog.value || !store.engine.value) return;
    const path = store.selected.value;
    if (kind === "spawn") {
      if (path === undefined) return;
      if (path === null && store.orchestrator.value.length) {
        store.toast("The orchestrator agent is already running", "warning");
        return;
      }
    }
    if (kind === "stop" && !store.shownAgent()) {
      store.toast("No agent to stop here", "warning");
      return;
    }
    if (kind === "remove" && !path) {
      store.toast(path === null ? "The orchestrator is not a worktree" : "Select a worktree first", "warning");
      return;
    }
    store.dialog.value = { kind, path, agent: kind === "stop" ? store.shownAgent() : null };
  },
  closeDialog() {
    store.dialog.value = null;
  },
  /** A dialog that started something closes and the shown terminal gets
   * the keyboard (after the dialog gave it back to its opener). */
  dialogDone() {
    store.dialog.value = null;
    requestAnimationFrame(() => requestAnimationFrame(focusShown));
  },
  async spawn({ path, instruction, skipPermissions, resumeMode, label }) {
    const params =
      path === null
        ? { kind: "orchestrator", worktree_path: store.repoRoot.value }
        : { worktree_path: path };
    Object.assign(params, { skip_permissions: skipPermissions, resume_mode: resumeMode });
    if (instruction) params.instruction = instruction;
    if (label) params.label = label;
    if (store.centerView.value === "tui") actions.showNative();
    const result = await conn.request("spawn_agent", params);
    const key = path === null ? ORCHESTRATOR_KEY : result.worktree_path;
    expectAgent(key, result.agent_id);
    // Listed (so its tab exists) before the dialog closes and the
    // terminal takes the keyboard.
    await until(() => store.agentsOf(key).some((a) => a.agent_id === result.agent_id), 15000, "The new agent").catch(() => {});
    return result;
  },
  async create({ branch, base, extra, launch }) {
    const params = { branch, base_branch: base || undefined };
    if (extra) params.extra = extra;
    const created = await conn.request("create_worktree", params);
    if (created.job_id) {
      const exitCode = await jobFinished(created.job_id);
      if (exitCode !== 0) throw new Error(`The create command failed (${exitCode ?? "killed"}); see the job log`);
    }
    await worktreeListed(created.path);
    actions.select(created.path, { focus: !launch });
    store.toast(`Created worktree ${basename(created.path)}`, "success");
    if (launch) {
      try {
        await actions.spawn({ path: created.path, instruction: launch.instruction, skipPermissions: launch.skipPermissions, resumeMode: "new", label: "" });
      } catch (e) {
        store.toast(`Worktree created, but the agent did not start: ${e.message}`, "error", 10000);
      }
    }
  },
  async remove(path, force) {
    const result = await conn.request("remove_worktree", { worktree_path: path, force, custom: true });
    if (result.job_id) {
      const exitCode = await jobFinished(result.job_id);
      if (exitCode !== 0) throw new Error(`The remove command failed (${exitCode ?? "killed"}); see the job log`);
    }
    if (store.selected.value === path) store.selected.value = store.worktrees.value.find((w) => w.is_main)?.path;
    store.toast(`Removed worktree ${basename(path)}`, "success");
  },
  async stop(path, agentId) {
    await conn.request("stop_agent", { worktree_path: path === null ? ORCHESTRATOR_KEY : path, agent_id: agentId });
  },
  refreshGit() {
    const path = store.selected.value;
    if (!path) return;
    conn.request("refresh_git_status", { paths: [path] }).catch((e) => store.toast(e.message, "warning"));
    store.toast("Refreshing git status…", "information", 2000);
  },
  refreshList() {
    conn
      .request("refresh_worktrees")
      .then(() => store.toast("Refreshed worktrees", "information", 2000))
      .catch((e) => store.toast(e.message, "warning"));
  },

  // ------------------------------------------------ attention (U4)

  showAttention() {
    if (store.centerView.value === "tui") tuiBlank();
    store.centerView.value = "attention";
    requestAnimationFrame(() => document.getElementById("attention-list")?.focus());
  },
  pickAttention(item) {
    store.attentionPick.value = item.id;
  },
  queueKey(event) {
    const items = store.attention.value;
    if (items.length === 0) return;
    const at = Math.max(0, items.findIndex((i) => i.id === store.attentionPick.value));
    const move = { j: 1, ArrowDown: 1, k: -1, ArrowUp: -1 }[event.key];
    if (move && !event.ctrlKey && !event.altKey) {
      store.attentionPick.value = items[Math.min(items.length - 1, Math.max(0, at + move))].id;
      event.preventDefault();
      event.stopPropagation();
    } else if (event.key === "Enter") {
      const item = items[at];
      if (item.agent) focusTerminal(item.agent.session_id);
      else document.querySelector(".att-pane button.primary")?.focus();
      event.preventDefault();
      event.stopPropagation();
    }
  },
  openItem(item) {
    const path = item.key === ORCHESTRATOR_KEY ? null : item.key;
    if (item.agent) store.activeAgent.value = { ...store.activeAgent.value, [item.key]: item.agent.agent_id };
    store.centerView.value = "native";
    actions.select(path);
  },
  stopItem(item) {
    store.dialog.value = { kind: "stop", path: item.key === ORCHESTRATOR_KEY ? null : item.key, agent: item.agent };
  },
  dismiss(item) {
    store.dismiss(item);
  },
  snooze(item) {
    store.snooze(item, 15 * 60 * 1000);
    store.toast(`${item.label}: snoozed for 15 minutes`, "information", 3000);
  },
  /** send_agent_input: the text, then Enter, as the MCP tool does. */
  async reply(item, text) {
    const result = await conn.request("send_agent_input", {
      worktree_path: item.key,
      agent_id: item.agent.agent_id,
      text,
    });
    const note = result.delivery === "confirmed" ? "sent" : `sent (${result.delivery})`;
    store.toast(`${item.label}: ${note}`, "success", 3000);
    return result;
  },
  async sendFailures(item, agent) {
    try {
      await conn.request("send_agent_input", {
        worktree_path: item.path,
        agent_id: agent.agent_id,
        text: ciFailurePrompt(item),
      });
      store.toast(`Sent the CI failures to ${agentLabel(agent)}`, "success", 3000);
    } catch (e) {
      store.toast(e.message, "error");
    }
  },
  async respawn(item) {
    const path = item.key === ORCHESTRATOR_KEY ? null : item.key;
    try {
      await actions.spawn({ path, instruction: "", skipPermissions: false, resumeMode: "last", label: item.exit.label });
      store.dismiss(item);
    } catch (e) {
      store.toast(e.message, "error");
    }
  },

  // ------------------------------------------------ PR panel (U4)

  togglePrPanel() {
    const path = store.selected.value;
    store.prPanel.value = !store.prPanel.value;
    if (store.prPanel.value && path) actions.refreshPr(path, false);
  },
  async refreshPr(path, refresh = true) {
    try {
      const result = await conn.request("get_pr_info", { worktree_path: path, refresh });
      store.prFetched.value = { ...store.prFetched.value, [path]: result.fetched };
      store.prInfo.value = { ...store.prInfo.value, [path]: result.pr_info };
    } catch (e) {
      store.toast(`PR: ${e.message}`, "warning");
    }
  },
};

// ---------------------------------------------------------------- waits

const waiters = new Set();

/** Resolves once ``test()`` holds after a store update (or rejects after
 * ``timeout`` ms). */
function until(test, timeout, what) {
  if (test()) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const waiter = () => {
      if (!test()) return;
      waiters.delete(waiter);
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      waiters.delete(waiter);
      reject(new Error(`${what}: no answer in ${timeout / 1000} s`));
    }, timeout);
    waiters.add(waiter);
  });
}

async function jobFinished(jobId) {
  const done = () => store.jobs.value.find((j) => j.id === jobId && j.exitCode !== undefined);
  await until(() => Boolean(done()), 30 * 60 * 1000, "The job");
  return done().exitCode;
}

function worktreeListed(path) {
  if (store.worktrees.value.some((w) => w.path === path)) return Promise.resolve();
  conn.request("refresh_worktrees").catch(() => {});
  return until(() => store.worktrees.value.some((w) => w.path === path), 15000, "The new worktree");
}

/** A spawned agent's tab becomes the shown one as soon as it is listed. */
function expectAgent(key, agentId) {
  pendingAgent = { worktree_path: key, agent_id: agentId };
}

// ---------------------------------------------------------------- engine

let refreshTimer = null;
function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(loadSidebar, 100);
}

function afterSidebar() {
  const worktrees = store.worktrees.value;
  if (store.selected.value === undefined && worktrees.length) {
    const first = worktrees.find((w) => w.agents && w.agents.length) || worktrees.find((w) => w.is_main) || worktrees[0];
    store.selected.value = first.path;
  } else if (store.selected.value && !worktrees.some((w) => w.path === store.selected.value)) {
    store.selected.value = worktrees.find((w) => w.is_main)?.path;
  }
  // Terminals of sessions that are gone are dropped (shells stay).
  const alive = new Set(Object.values(store.shells.value));
  for (const w of worktrees) for (const a of w.agents || []) alive.add(a.session_id);
  for (const a of store.orchestrator.value) alive.add(a.session_id);
  terminals.retain(alive);
}

async function loadSidebar() {
  try {
    const sidebar = await conn.request("get_sidebar_state");
    store.enqueue(() => {
      store.applySidebar(sidebar);
      afterSidebar();
      // A quiet agent sends no event after it is listed: wake the waiters.
      for (const waiter of [...waiters]) waiter();
    });
  } catch (e) {
    store.toast(`Sidebar: ${e.message}`, "error");
  }
}

async function onEngineReady() {
  store.engine.value = true;
  conn.request("get_config").then((config) => (store.config.value = config), () => {});
  store.conn.value = { text: "connected", kind: "ok" };
  store.overlay.value = null;
  try {
    const sidebar = await conn.request("get_sidebar_state");
    store.enqueue(() => {
      store.applySidebar(sidebar);
      afterSidebar();
      if (store.centerView.value === "native") tuiBlank();
      else tuiFollow();
    });
    await conn.request("subscribe", { since: sidebar.cursor, events: SIDEBAR_EVENTS });
    terminals.resume();
  } catch (e) {
    store.toast(`Engine: ${e.message}`, "error");
  }
}

let pendingAgent = null;

function onEvent(name, data) {
  if (name.startsWith("stream.")) {
    terminals.onEvent(name, data);
    return;
  }
  store.enqueue(() => {
    if (name === "ui.view_changed") onViewChanged(data);
    if (name === "agent.spawned") {
      const selected = store.selected.value;
      if (data.worktree_path === (selected === null ? ORCHESTRATOR_KEY : selected)) pendingAgent = data;
    }
    if (store.applyEvent(name, data)) refreshSoon();
    if (name === "worktrees.changed") afterSidebar();
    if (pendingAgent && store.agentsOf(pendingAgent.worktree_path).some((a) => a.agent_id === pendingAgent.agent_id)) {
      store.activeAgent.value = { ...store.activeAgent.value, [pendingAgent.worktree_path]: pendingAgent.agent_id };
      const key = pendingAgent.worktree_path;
      pendingAgent = null;
      if (store.centerView.value === "native" && keyOf(store.selected.value) === key) focusShown();
    }
    for (const waiter of [...waiters]) waiter();
  });
}

function onViewChanged(view) {
  if (store.centerView.value !== "tui") return;
  // On the TUI tab the page follows what the TUI shows.
  if (view.orchestrator) store.selected.value = null;
  else if (view.worktree_path) store.selected.value = view.worktree_path;
  if (view.focus === "agent" && view.agent_id !== null) {
    const key = view.orchestrator ? ORCHESTRATOR_KEY : view.worktree_path;
    if (key) store.activeAgent.value = { ...store.activeAgent.value, [key]: view.agent_id };
  }
}

function onControl(message) {
  switch (message.type) {
    case "hello":
      store.repoRoot.value = message.repo_root;
      reconnectDelay = 250;
      tui.announce();
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
      onEvent(message.event, message.data);
      break;
    case "exit":
      store.exited.value = message.code;
      store.conn.value = { text: "exited", kind: "bad" };
      store.overlay.value = `lazyagent exited (code ${message.code}).\nThe host has stopped; run lazyagent host again to restart.`;
      break;
  }
}

let reconnectDelay = 250;

function onClose(event) {
  store.engine.value = false;
  if (store.exited.value !== null) return;
  if (event.code === 1008) {
    store.conn.value = { text: "disconnected", kind: "bad" };
    store.overlay.value = "The host refused this page (bad or stale token).\nOpen the URL lazyagent host printed.";
    return;
  }
  store.conn.value = { text: "reconnecting…", kind: "bad" };
  store.overlay.value = "Reconnecting to lazyagent host…";
  setTimeout(() => conn.open(token), reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 5000);
}

// ---------------------------------------------------------------- keys

const inside = (target, selector) => target instanceof Element && target.closest(selector) !== null;
const inField = (target) => inside(target, "input, textarea:not(.xterm-helper-textarea), select, [contenteditable]");

function focusSidebar() {
  focusRequest++;
  focusCurrentRow();
}

function focusCurrentRow() {
  const list = document.getElementById("worktrees");
  const current = list?.querySelector("li.wt.current") || list?.querySelector("li.wt");
  current?.focus();
}

function moveSelection(step) {
  const keys = [null, ...store.worktrees.value.map((w) => w.path)];
  const at = keys.indexOf(store.selected.value);
  const next = keys[Math.min(keys.length - 1, Math.max(0, at + step))];
  actions.select(next, { focus: false });
  // The row is re-rendered: keep the keyboard on it, unless something
  // asked for the keyboard since (Enter right after j).
  const request = focusRequest;
  requestAnimationFrame(() => request === focusRequest && focusCurrentRow());
}

/** Keys the page takes before a native terminal sees them (the TUI's
 * global set, §7a). In the TUI tab every key goes to the TUI. */
function globalKey(event) {
  const mod = event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey;
  const alt = event.altKey && !event.ctrlKey && !event.metaKey;
  const key = event.key.toLowerCase();
  if (mod && key === "k") return focusSidebar(), true;
  if (mod && key === "j") return actions.showNative(), true;
  if (mod && key === "l") {
    if (!store.selected.value) return true;
    if (!store.shellOpen.value) actions.toggleShell();
    else focusShell();
    return true;
  }
  if (mod && key === "d") return actions.toolbar("diff"), true;
  if (mod && key === ".") return actions.togglePrPanel(), true;
  if (alt && key === "g") return actions.refreshGit(), true;
  if (alt && (event.code === "BracketRight" || key === "n")) return actions.cycleAgent(1), true;
  if (alt && (event.code === "BracketLeft" || key === "p")) return actions.cycleAgent(-1), true;
  return false;
}

// "g" then a letter, outside a terminal: switch views (§3a).
let pendingG = 0;
const G_KEYS = {
  a: () => actions.showAttention(),
  w: () => actions.showNative(),
};

// Single keys outside a terminal, as in the TUI.
const OUTSIDE_KEYS = {
  s: () => actions.openDialog("spawn"),
  x: () => actions.openDialog("stop"),
  c: () => actions.openDialog("create"),
  d: () => actions.openDialog("remove"),
  r: () => actions.refreshList(),
};

function sidebarKey(event) {
  if (event.ctrlKey || event.altKey || event.metaKey) return false;
  if (event.key === "j" || event.key === "ArrowDown") return moveSelection(1), true;
  if (event.key === "k" || event.key === "ArrowUp") return moveSelection(-1), true;
  if (event.key === "Enter") return focusShown(), true;
  return false;
}

document.addEventListener(
  "keydown",
  (event) => {
    const target = event.target;
    if (event.key === "Escape" && store.help.value) {
      store.help.value = false;
      event.preventDefault();
      return;
    }
    if (event.key === "Escape" && store.dialog.value && !inside(target, "[role=dialog]")) {
      actions.closeDialog();
      event.preventDefault();
      return;
    }
    if (store.help.value || store.dialog.value || inField(target)) return;
    if (inside(target, "#terminal-pane")) return;
    let handled = globalKey(event);
    if (!handled && inside(target, "#worktrees")) handled = sidebarKey(event);
    const plain = !event.ctrlKey && !event.altKey && !event.metaKey;
    if (!handled && !inside(target, ".xterm") && plain) {
      const g = performance.now() - pendingG < 1000;
      pendingG = 0;
      if (g && G_KEYS[event.key]) {
        G_KEYS[event.key]();
        handled = true;
      } else if (event.key === "g") {
        pendingG = performance.now();
        handled = true;
      } else if (event.key === "?") {
        store.help.value = true;
        handled = true;
      } else if (OUTSIDE_KEYS[event.key]) {
        OUTSIDE_KEYS[event.key]();
        handled = true;
      }
    }
    if (handled) {
      event.preventDefault();
      event.stopPropagation();
    }
  },
  true,
);

// ---------------------------------------------------------------- page

const tuiPane = html`<div class="tui-wrap"><${TuiPane} tui=${tui} /></div>`;

function App() {
  return html`
    <${TopBar} store=${store} actions=${actions} />
    <main>
      <${Sidebar} store=${store} actions=${actions} />
      <div class="center-wrap">
        <${Center} store=${store} actions=${actions} manager=${terminals} tuiPane=${tuiPane} />
        ${store.centerView.value === "attention" && html`<${AttentionView} store=${store} actions=${actions} manager=${terminals} />`}
        ${store.centerView.value === "native" && html`<${PrPanel} store=${store} actions=${actions} />`}
        <${Overlay} store=${store} />
      </div>
    </main>
    <${JobDrawer} store=${store} />
    <${Toasts} store=${store} />
    ${store.help.value && html`<${HelpSheet} onClose=${() => (store.help.value = false)} />`}
    <${Dialogs} />
  `;
}

function Dialogs() {
  const dialog = store.dialog.value;
  if (!dialog) return null;
  const close = actions.closeDialog;
  const config = store.config.value;
  switch (dialog.kind) {
    case "spawn":
      return html`<${SpawnDialog} path=${dialog.path} provider=${config?.agent_provider} actions=${actions} onClose=${close} onDone=${actions.dialogDone} />`;
    case "create":
      return html`<${CreateDialog} config=${config} worktrees=${store.worktrees.value} actions=${actions} onClose=${close} onDone=${actions.dialogDone} />`;
    case "remove":
      return html`<${RemoveDialog}
        worktree=${store.worktrees.value.find((w) => w.path === dialog.path)}
        config=${config}
        actions=${actions}
        onClose=${close}
      />`;
    case "stop":
      return html`<${StopDialog} path=${dialog.path} agent=${dialog.agent} actions=${actions} onClose=${close} />`;
    default:
      return null;
  }
}

render(html`<${App} />`, document.body);

// ---------------------------------------------------------------- badge

// The attention count in the tab title and on the favicon (§3b).
setInterval(() => (store.clock.value = Date.now()), 15000);
const favicon = document.querySelector('link[rel="icon"]');
const plainIcon = favicon?.getAttribute("href");
let iconImage = null;
function badgeIcon(count) {
  if (!favicon) return;
  if (!count) {
    favicon.setAttribute("href", plainIcon);
    return;
  }
  const draw = () => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 32;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(iconImage, 0, 0, 32, 32);
    ctx.fillStyle = "#e5534b";
    ctx.beginPath();
    ctx.arc(22, 10, 10, 0, 2 * Math.PI);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.font = "bold 14px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(count > 9 ? "9+" : String(count), 22, 11);
    favicon.setAttribute("href", canvas.toDataURL("image/png"));
  };
  if (iconImage?.complete) draw();
  else {
    iconImage = new Image();
    iconImage.onload = draw;
    iconImage.src = plainIcon;
  }
}
let lastBadge = -1;
effect(() => {
  const count = store.attention.value.length;
  const repo = store.repoRoot.value;
  document.title = `${count ? `(${count}) ` : ""}lazyagent${repo ? ` — ${basename(repo)}` : ""}`;
  if (count !== lastBadge) {
    lastBadge = count;
    badgeIcon(count);
  }
});

const token = takeToken();
if (token) conn.open(token);
else {
  store.overlay.value = "No token: open the URL lazyagent host printed (it ends in #token=…).";
  store.conn.value = { text: "no token", kind: "bad" };
}

// For the browser tests.
window.lazyagent = { store, conn, tui, terminals, actions };
