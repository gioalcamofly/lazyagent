// The page's state, as signals, built from get_sidebar_state and kept
// current by the engine's events.
//
// Everything that changes the state goes through enqueue(): the queue is
// applied once per animation frame (or after 50 ms when frames do not run,
// as in a hidden tab), inside one batch, in arrival order. A burst of events
// is then one re-render, and a get_sidebar_state answer can never be applied
// on top of events that came after it.

import { signal, batch } from "./html.js";
import { ORCHESTRATOR_KEY, agentLabel, placeName } from "./format.js";

export const SIDEBAR_EVENTS = [
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
export const ALERT_STATUSES = {
  waiting: "is waiting",
  waiting_for_user: "is waiting for input",
  waiting_for_approval: "needs approval",
  completed: "completed",
  failed: "failed",
  possibly_hanged: "may be hanged",
};

/** Structural equality of JSON values. */
export function same(a, b) {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((k) => same(a[k], b[k]));
}

/** The new worktree list, reusing the old object of every worktree that did
 * not change, so its memoised row does not re-render. */
export function reuseRows(old, rows) {
  const byPath = new Map(old.map((w) => [w.path, w]));
  const out = rows.map((w) => {
    const prev = byPath.get(w.path);
    return prev && same(prev, w) ? prev : w;
  });
  return out.length === old.length && out.every((w, i) => w === old[i]) ? old : out;
}

export function createStore() {
  const store = {
    repoRoot: signal(""),
    /** The connection pill: {text, kind} with kind "" / "ok" / "bad". */
    conn: signal({ text: "connecting…", kind: "" }),
    engine: signal(false),
    exited: signal(null),
    overlay: signal(null),
    worktrees: signal([]),
    orchestrator: signal([]),
    prInfo: signal({}),
    view: signal(null),
    toasts: signal([]),
    help: signal(false),
  };

  // ------------------------------------------------------------ queue

  let queue = [];
  let scheduled = false;
  let timer = null;

  function flush() {
    if (!scheduled) return;
    scheduled = false;
    clearTimeout(timer);
    const work = queue;
    queue = [];
    batch(() => {
      for (const fn of work) fn();
    });
  }

  store.enqueue = (fn) => {
    queue.push(fn);
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(flush);
    timer = setTimeout(flush, 50);
  };
  store.flush = flush;

  // ------------------------------------------------------------ toasts

  let toastIds = 0;
  store.toast = (text, severity = "information", timeout = 6000) => {
    const id = ++toastIds;
    store.toasts.value = [...store.toasts.value, { id, text, severity }];
    setTimeout(() => {
      store.toasts.value = store.toasts.value.filter((t) => t.id !== id);
    }, timeout);
  };
  /** Called with (title, body) for alerts the user should see even when
   * the page is not focused; app.js plugs desktop notifications in. */
  store.notify = () => {};

  // ------------------------------------------------------------ state

  store.applySidebar = (sidebar) => {
    store.worktrees.value = reuseRows(store.worktrees.value, sidebar.worktrees);
    store.orchestrator.value = sidebar.orchestrator.agents;
    store.prInfo.value = { ...store.prInfo.value, ...sidebar.pr_info };
    store.view.value = sidebar.view;
  };

  store.agentsOf = (path) => {
    if (path === ORCHESTRATOR_KEY) return store.orchestrator.value;
    const wt = store.worktrees.value.find((w) => w.path === path);
    return wt ? wt.agents : [];
  };

  function findAgent(path, agentId) {
    return store.agentsOf(path).find((a) => a.agent_id === agentId);
  }

  // Replace one agent, copying only the arrays and objects on its path, so
  // memoised rows of the other worktrees keep their props.
  function patchAgent(path, agentId, fields) {
    const patch = (agents) =>
      agents.map((a) => (a.agent_id === agentId ? { ...a, ...fields } : a));
    if (path === ORCHESTRATOR_KEY) {
      store.orchestrator.value = patch(store.orchestrator.value);
      return;
    }
    store.worktrees.value = store.worktrees.value.map((w) =>
      w.path === path ? { ...w, agents: patch(w.agents) } : w,
    );
  }

  function alert(agent, path, message, severity) {
    const text = `${agentLabel(agent)} (${placeName(path)}) ${message}`;
    store.toast(text, severity);
    store.notify("lazyagent", text);
  }

  /** Apply one engine event. Returns true when the page should reload the
   * sidebar (an agent it does not know about yet). */
  store.applyEvent = (name, data) => {
    switch (name) {
      case "worktrees.changed":
        store.worktrees.value = reuseRows(store.worktrees.value, data);
        return false;
      case "git_status.updated":
        store.worktrees.value = store.worktrees.value.map((w) =>
          w.path in data ? { ...w, git_status: data[w.path] } : w,
        );
        return false;
      case "pr_info.updated":
        store.prInfo.value = { ...store.prInfo.value, [data.path]: data.pr_info };
        return false;
      case "agent.updated":
        if (!findAgent(data.worktree_path, data.agent_id)) return true;
        patchAgent(data.worktree_path, data.agent_id, {
          status: data.status,
          detail: data.detail,
        });
        return false;
      case "agent.status": {
        const message = ALERT_STATUSES[data.status];
        if (message && data.status !== data.previous_status) {
          const severity =
            data.status === "completed"
              ? "success"
              : data.status.startsWith("waiting")
                ? "warning"
                : "error";
          alert(data, data.worktree_path, message, severity);
        }
        return false;
      }
      case "agent.exited": {
        if (!data.stopped) {
          const agent = findAgent(data.worktree_path, data.agent_id);
          const code = data.exit_code ?? "?";
          alert(agent || data, data.worktree_path, `exited (code ${code})`, code === 0 ? "information" : "error");
        }
        return true;
      }
      case "agent.spawned":
        return true;
      case "ui.view_changed":
        store.view.value = data;
        return false;
      case "engine.notice":
        store.toast(data.message, data.severity);
        return false;
      case "engine.shutdown":
        store.toast("The engine is shutting down", "warning");
        return false;
      default:
        return false;
    }
  };

  return store;
}
