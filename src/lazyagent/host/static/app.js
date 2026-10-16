// lazyagent host page: the TUI in xterm.js, a sidebar fed by engine events.
// The host serves this file and proxies a whitelist of v1 methods; see
// lazyagent/host/server.py for the message format.

import { Terminal } from "./vendor/xterm/xterm.mjs";
import { FitAddon } from "./vendor/xterm/addon-fit.mjs";

const ORCHESTRATOR_KEY = "__orchestrator__";
const SIDEBAR_EVENTS = [
  "worktrees.changed",
  "git_status.updated",
  "pr_info.updated",
  "agent.status",
  "agent.updated",
  "agent.spawned",
  "agent.exited",
  "ui.view_changed",
  "engine.notice",
  "engine.shutdown",
];
// Transitions worth interrupting the user for.
const ALERT_STATUSES = {
  waiting: "is waiting",
  waiting_for_user: "is waiting for input",
  waiting_for_approval: "needs approval",
  completed: "completed",
  failed: "failed",
  possibly_hanged: "may be hanged",
};
const STATUS_TEXT = {
  running: "running",
  waiting: "waiting",
  waiting_for_user: "input",
  waiting_for_approval: "approval",
  completed: "done",
  failed: "failed",
  interrupted: "stopped",
  possibly_hanged: "hanged?",
};

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------- token

function takeToken() {
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

// ---------------------------------------------------------------- state

const state = {
  worktrees: [],
  orchestrator: [],
  prInfo: {},
  view: null,
  engine: false,
  exited: null,
};

// ---------------------------------------------------------------- terminal

const term = new Terminal({
  fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono"),
  fontSize: 13,
  cursorBlink: false,
  scrollback: 1000,
  theme: {
    background: "#121212",
    foreground: "#e0e0e0",
    cursor: "#e0e0e0",
    selectionBackground: "#0178d466",
  },
});
const fit = new FitAddon();
term.loadAddon(fit);
term.open($("#terminal"));

const encoder = new TextEncoder();
let socket = null;

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(message);
}

term.onData((data) => send(encoder.encode(data)));
term.onBinary((data) => {
  const bytes = new Uint8Array(data.length);
  for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 0xff;
  send(bytes);
});
term.onResize(({ rows, cols }) => send(JSON.stringify({ type: "resize", rows, cols })));

function refit() {
  try {
    fit.fit();
  } catch {
    // Not laid out yet.
  }
}
new ResizeObserver(refit).observe($("#terminal-pane"));

// ---------------------------------------------------------------- requests

let nextId = 1;
const pending = new Map();

function request(method, params = {}) {
  const id = String(nextId++);
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send(JSON.stringify({ type: "req", id, method, params }));
  });
}

async function command(method, params = {}) {
  try {
    await request(method, params);
  } catch (e) {
    toast(e.message, "warning");
  }
  term.focus();
}

// ---------------------------------------------------------------- toasts

function toast(text, severity = "information", timeout = 6000) {
  const el = document.createElement("div");
  el.className = `toast ${severity}`;
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), timeout);
}

function notify(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (document.hasFocus()) return;
  try {
    new Notification(title, { body, tag: `lazyagent-${title}` });
  } catch {
    // Some browsers only allow notifications from a service worker.
  }
}

function setupNotifyButton() {
  const button = $("#notify");
  if (!("Notification" in window) || Notification.permission !== "default") return;
  button.hidden = false;
  button.addEventListener("click", async () => {
    await Notification.requestPermission();
    button.hidden = Notification.permission !== "default";
  });
}

// ---------------------------------------------------------------- sidebar

const basename = (path) => path.replace(/\/+$/, "").split("/").pop();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function ciSummary(checks) {
  if (!checks || checks.length === 0) return null;
  const up = (s) => (s || "").toUpperCase();
  if (checks.some((c) => up(c.conclusion) === "FAILURE")) return "fail";
  if (checks.some((c) => up(c.status) !== "COMPLETED" && up(c.conclusion) !== "SUCCESS")) {
    return "pending";
  }
  return "pass";
}

function agentLabel(agent) {
  return agent.label || `Agent ${agent.agent_id}`;
}

function isCurrent(path) {
  const view = state.view;
  if (!view) return false;
  return path === null ? Boolean(view.orchestrator) : !view.orchestrator && view.worktree_path === path;
}

function renderAgents(agents, path) {
  const list = el("ul", "agents");
  for (const agent of agents) {
    const status = agent.status || "no_agent";
    const item = el("li", `agent s-${status}`);
    item.dataset.agent = agent.agent_id;
    item.title = agent.detail || status;
    if (isCurrent(path) && state.view.agent_id === agent.agent_id && state.view.focus === "agent") {
      item.classList.add("current");
    }
    item.append(el("span", "dot"), el("span", "label", agentLabel(agent)));
    item.append(el("span", "status", STATUS_TEXT[status] || status));
    item.addEventListener("click", (event) => {
      event.stopPropagation();
      const target = path === null ? { orchestrator: true } : { worktree_path: path };
      command("ui.focus_agent", { ...target, agent_id: agent.agent_id });
    });
    list.append(item);
  }
  return list;
}

function renderWorktree(wt) {
  const item = el("li", "wt");
  item.dataset.path = wt.path;
  if (isCurrent(wt.path)) item.classList.add("current");
  const head = el("div", "wt-head");
  head.append(el("span", "wt-name", basename(wt.path)));
  if (wt.is_main) head.append(el("span", "tag", "main"));
  item.append(head);
  item.append(el("div", "wt-branch", wt.branch || `(detached ${wt.head.slice(0, 7)})`));

  const meta = el("div", "wt-meta");
  const git = wt.git_status;
  if (git) {
    if (git.dirty_count) meta.append(el("span", "dirty", `●${git.dirty_count}`));
    if (git.ahead) meta.append(el("span", "ahead", `↑${git.ahead}`));
    if (git.behind) meta.append(el("span", "behind", `↓${git.behind}`));
    if (!git.dirty_count && !git.ahead && !git.behind) meta.append(el("span", "tag", "clean"));
  }
  const pr = state.prInfo[wt.path];
  if (pr) {
    const badge = el("span", `pr ${(pr.state || "").toLowerCase()}`, `#${pr.number} ${(pr.state || "").toLowerCase()}`);
    badge.title = pr.title;
    meta.append(badge);
    const ci = ciSummary(pr.checks);
    if (ci) meta.append(el("span", `ci ${ci}`, { pass: "✓ CI", fail: "✗ CI", pending: "… CI" }[ci]));
  }
  if (meta.childElementCount) item.append(meta);
  if (wt.agents && wt.agents.length) item.append(renderAgents(wt.agents, wt.path));
  item.addEventListener("click", () => command("ui.select_worktree", { worktree_path: wt.path }));
  return item;
}

function renderOrchestrator() {
  const item = el("li", "wt orchestrator");
  item.dataset.path = ORCHESTRATOR_KEY;
  if (isCurrent(null)) item.classList.add("current");
  const head = el("div", "wt-head");
  head.append(el("span", "wt-name", "Orchestrator"));
  item.append(head);
  if (state.orchestrator.length) item.append(renderAgents(state.orchestrator, null));
  item.addEventListener("click", () => command("ui.select_orchestrator"));
  return item;
}

function render() {
  const list = $("#worktrees");
  list.replaceChildren(renderOrchestrator(), ...state.worktrees.map(renderWorktree));
  const view = state.view;
  const enabled = state.engine && state.exited === null;
  const onWorktree = Boolean(view && !view.orchestrator && view.worktree_path);
  const wt = onWorktree ? state.worktrees.find((w) => w.path === view.worktree_path) : null;
  const modalOpen = Boolean(view && view.focus === "modal");
  const buttons = {
    spawn: enabled && !modalOpen,
    create: enabled && !modalOpen,
    remove: enabled && !modalOpen && Boolean(wt) && !wt.is_main,
    shell: enabled && onWorktree,
    diff: enabled && onWorktree,
  };
  for (const button of document.querySelectorAll("#toolbar button")) {
    button.disabled = !buttons[button.dataset.action];
  }
  $("#engine-state").textContent = state.engine
    ? `${state.worktrees.length} worktree(s)`
    : "waiting for the engine…";
}

const ACTIONS = {
  spawn: () => command("ui.open_modal", { modal: "spawn" }),
  create: () => command("ui.open_modal", { modal: "create" }),
  remove: () => command("ui.open_modal", { modal: "remove" }),
  shell: () => command("ui.open_shell"),
  diff: () => command("ui.show_diff"),
};
for (const button of document.querySelectorAll("#toolbar button")) {
  button.addEventListener("click", () => ACTIONS[button.dataset.action]());
}

// ---------------------------------------------------------------- events

function agentsOf(path) {
  if (path === ORCHESTRATOR_KEY) return state.orchestrator;
  const wt = state.worktrees.find((w) => w.path === path);
  return wt ? wt.agents : [];
}

let refreshTimer = null;
function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(loadSidebar, 100);
}

function applySidebar(sidebar) {
  state.worktrees = sidebar.worktrees;
  state.orchestrator = sidebar.orchestrator.agents;
  state.prInfo = { ...state.prInfo, ...sidebar.pr_info };
  state.view = sidebar.view;
  render();
}

async function loadSidebar() {
  try {
    applySidebar(await request("get_sidebar_state"));
  } catch (e) {
    toast(`Sidebar: ${e.message}`, "error");
  }
}

function onEvent(name, data) {
  switch (name) {
    case "worktrees.changed":
      state.worktrees = data;
      break;
    case "git_status.updated":
      for (const [path, git] of Object.entries(data)) {
        const wt = state.worktrees.find((w) => w.path === path);
        if (wt) wt.git_status = git;
      }
      break;
    case "pr_info.updated":
      state.prInfo[data.path] = data.pr_info;
      break;
    case "agent.updated": {
      const agent = agentsOf(data.worktree_path).find((a) => a.agent_id === data.agent_id);
      if (agent) Object.assign(agent, { status: data.status, detail: data.detail });
      else refreshSoon();
      break;
    }
    case "agent.status": {
      const message = ALERT_STATUSES[data.status];
      if (message && data.status !== data.previous_status) {
        const where = data.worktree_path === ORCHESTRATOR_KEY ? "orchestrator" : basename(data.worktree_path);
        const text = `${data.label || `Agent ${data.agent_id}`} (${where}) ${message}`;
        const severity = data.status === "completed" ? "success" : data.status.startsWith("waiting") ? "warning" : "error";
        toast(text, severity);
        notify("lazyagent", text);
      }
      break;
    }
    case "agent.spawned":
    case "agent.exited":
      refreshSoon();
      return;
    case "ui.view_changed":
      state.view = data;
      break;
    case "engine.notice":
      toast(data.message, data.severity);
      return;
    case "engine.shutdown":
      toast("The engine is shutting down", "warning");
      return;
    default:
      return;
  }
  render();
}

// ---------------------------------------------------------------- socket

function setConn(text, kind) {
  const pill = $("#conn");
  pill.textContent = text;
  pill.className = `pill ${kind || ""}`;
}

function showOverlay(text) {
  const overlay = $("#overlay");
  overlay.textContent = text;
  overlay.hidden = false;
}

async function onEngineReady() {
  state.engine = true;
  setConn("connected", "ok");
  try {
    const sidebar = await request("get_sidebar_state");
    applySidebar(sidebar);
    await request("subscribe", { since: sidebar.cursor, events: SIDEBAR_EVENTS });
  } catch (e) {
    toast(`Engine: ${e.message}`, "error");
  }
}

function onControl(message) {
  switch (message.type) {
    case "hello":
      $("#repo").textContent = message.repo_root;
      document.title = `lazyagent — ${basename(message.repo_root)}`;
      refit();
      send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols, redraw: true }));
      term.focus();
      break;
    case "engine":
      if (message.ready) onEngineReady();
      else {
        state.engine = false;
        setConn("engine gone", "bad");
        render();
      }
      break;
    case "res": {
      const waiter = pending.get(message.id);
      if (!waiter) {
        if (message.error) toast(message.error.message, "error");
        return;
      }
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(message.error.message));
      else waiter.resolve(message.result);
      break;
    }
    case "evt":
      onEvent(message.event, message.data);
      break;
    case "exit":
      state.exited = message.code;
      render();
      setConn("exited", "bad");
      showOverlay(`lazyagent exited (code ${message.code}).\nThe host has stopped; run lazyagent host again to restart.`);
      break;
  }
}

function connect() {
  const token = takeToken();
  if (!token) {
    showOverlay("No token: open the URL lazyagent host printed (it ends in #token=…).");
    setConn("no token", "bad");
    return;
  }
  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.binaryType = "arraybuffer";
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ type: "auth", token }));
    setConn("terminal only", "");
  });
  socket.addEventListener("message", (event) => {
    if (typeof event.data === "string") onControl(JSON.parse(event.data));
    else term.write(new Uint8Array(event.data));
  });
  socket.addEventListener("close", (event) => {
    for (const waiter of pending.values()) waiter.reject(new Error("disconnected"));
    pending.clear();
    state.engine = false;
    render();
    if (state.exited !== null) return;
    setConn("disconnected", "bad");
    showOverlay(
      event.code === 1008
        ? "The host refused this page (bad or stale token).\nOpen the URL lazyagent host printed."
        : "Disconnected from lazyagent host.\nReload the page if it is still running.",
    );
  });
}

setupNotifyButton();
render();
connect();
