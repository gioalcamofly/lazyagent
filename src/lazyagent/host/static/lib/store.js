// The page's state, as signals, built from get_sidebar_state and kept
// current by the engine's events.
//
// Everything that changes the state goes through enqueue(): the queue is
// applied once per animation frame (or after 50 ms when frames do not run,
// as in a hidden tab), inside one batch, in arrival order. A burst of events
// is then one re-render, and a get_sidebar_state answer can never be applied
// on top of events that came after it.

import { signal, computed, batch } from "./html.js";
import { ORCHESTRATOR_KEY, agentLabel, ciSummary, placeName } from "./format.js";
import { attentionItems, kindOfStatus } from "./attention.js";

function loadJson(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key));
    return value && typeof value === "object" ? value : fallback;
  } catch {
    return fallback;
  }
}

function saveJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Not persisted: it holds for this page load.
  }
}

// Desktop notifications per queue kind (settings; on unless turned off).
export const NOTIFY_KINDS = {
  approval: "Needs approval",
  question: "Asks a question",
  hanged: "May be hanged",
  failed: "Failed or exited",
  done: "Finished its turn",
  ci: "CI failed",
};

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
  "job.started",
  "job.output",
  "job.finished",
];

// Output kept per job in the drawer (the tail).
export const JOB_OUTPUT_LIMIT = 256 * 1024;

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
    // The page's own selection (not the hosted TUI's): a worktree path,
    // null for the orchestrator, undefined before the first sidebar.
    selected: signal(undefined),
    // The agent tab shown per worktree path (or ORCHESTRATOR_KEY).
    activeAgent: signal({}),
    // What the centre shows: "native" terminals, or the hosted "tui".
    centerView: signal("native"),
    shellOpen: signal(false),
    // Worktree path → its shell's session_id, once open_shell answered.
    shells: signal({}),
    // get_config, once known.
    config: signal(null),
    // The open dialog: {kind: "spawn" | "create" | "remove" | "stop", ...}.
    dialog: signal(null),
    // Engine jobs (custom create/remove), oldest first:
    // {id, title, command, text, exitCode (undefined while running)}.
    jobs: signal([]),
    drawerOpen: signal(false),
    // Unexpected exits seen by this page: {key, agent_id, label, exit_code, at}.
    exits: signal([]),
    // Queue items dismissed (by occurrence) or snoozed (by id, until a time).
    dismissed: signal(loadJson("lazyagent-dismissed", {})),
    snoozed: signal(loadJson("lazyagent-snoozed", {})),
    // The queue item shown on the right of the attention view.
    attentionPick: signal(null),
    notifySettings: signal(loadJson("lazyagent-notify", {})),
    // Bumped by a timer so snoozes run out without an event.
    clock: signal(Date.now()),
    // The PR/CI panel of the worktree view, and get_pr_info's "fetched".
    prPanel: signal(false),
    prFetched: signal({}),
    // True once a sidebar state has been applied.
    loaded: signal(false),
  };

  // When each queue condition was first seen: kept while it holds, dropped
  // when it clears, so a later return is a new occurrence (a dismiss of the
  // old one does not hide it).
  // Kept in this browser too, so a dismissed item stays dismissed after a
  // reload (its occurrence is the same).
  const since = loadJson("lazyagent-since", {});
  let saveTimer = null;
  // At most once a second: localStorage writes are synchronous.
  const scheduleSaveSince = () => {
    if (saveTimer !== null) return;
    saveTimer = setTimeout(() => {
      saveTimer = null;
      saveJson("lazyagent-since", since);
    }, 1000);
  };
  store.attention = computed(() => {
    // Nothing is known before the first sidebar (and an empty world would
    // forget every first-seen time).
    if (!store.loaded.value) return [];
    store.clock.value; // re-derived on its tick, for snoozes running out
    const now = Date.now();
    const items = attentionItems({
      worktrees: store.worktrees.value,
      orchestrator: store.orchestrator.value,
      prInfo: store.prInfo.value,
      exits: store.exits.value,
      jobs: store.jobs.value,
      since,
      dismissed: store.dismissed.value,
      snoozed: store.snoozed.value,
      now,
    });
    scheduleSaveSince();
    return items;
  });
  store.attentionSince = since;

  store.dismiss = (item) => {
    const dismissed = { ...store.dismissed.value, [item.occurrence]: Date.now() };
    // Keep the newest 500.
    const keys = Object.keys(dismissed);
    if (keys.length > 500) for (const k of keys.slice(0, keys.length - 500)) delete dismissed[k];
    store.dismissed.value = dismissed;
    saveJson("lazyagent-dismissed", dismissed);
    if (item.kind === "exited") store.exits.value = store.exits.value.filter((e) => e !== item.exit);
  };
  store.snooze = (item, ms) => {
    const now = Date.now();
    const snoozed = Object.fromEntries(Object.entries(store.snoozed.value).filter(([, until]) => until > now));
    snoozed[item.id] = now + ms;
    store.snoozed.value = snoozed;
    saveJson("lazyagent-snoozed", snoozed);
  };
  store.setNotify = (kind, on) => {
    store.notifySettings.value = { ...store.notifySettings.value, [kind]: on };
    saveJson("lazyagent-notify", store.notifySettings.value);
  };
  store.notifies = (kind) => store.notifySettings.value[kind] !== false;

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
    store.loaded.value = true;
  };

  /** The agents of the selected worktree (or the orchestrator). */
  store.selectedAgents = () => {
    const path = store.selected.value;
    if (path === undefined) return [];
    return store.agentsOf(path === null ? ORCHESTRATOR_KEY : path);
  };

  /** The agent whose tab is shown for the selection: the chosen one while
   * it runs, else the last one. */
  store.shownAgent = () => {
    const agents = store.selectedAgents();
    if (agents.length === 0) return null;
    const path = store.selected.value;
    const key = path === null ? ORCHESTRATOR_KEY : path;
    const chosen = store.activeAgent.value[key];
    return agents.find((a) => a.agent_id === chosen) || agents[agents.length - 1];
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

  function alert(agent, path, message, severity, kind) {
    const text = `${agentLabel(agent)} (${placeName(path)}) ${message}`;
    store.toast(text, severity);
    if (store.notifies(kind)) store.notify("lazyagent", text);
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
      case "pr_info.updated": {
        const before = store.prInfo.value[data.path];
        store.prInfo.value = { ...store.prInfo.value, [data.path]: data.pr_info };
        // Green (or pending) to red is worth an interruption.
        if (data.pr_info && ciSummary(data.pr_info.checks) === "fail" && before && ciSummary(before.checks) !== "fail") {
          const text = `CI failed on #${data.pr_info.number} (${placeName(data.path)})`;
          store.toast(text, "error");
          if (store.notifies("ci")) store.notify("lazyagent", text);
        }
        return false;
      }
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
          alert(data, data.worktree_path, message, severity, kindOfStatus(data.status));
        }
        return false;
      }
      case "agent.exited": {
        if (!data.stopped) {
          const agent = findAgent(data.worktree_path, data.agent_id);
          const code = data.exit_code ?? "?";
          alert(agent || data, data.worktree_path, `exited (code ${code})`, code === 0 ? "information" : "error", "failed");
          if (data.exit_code !== 0) {
            const exit = {
              key: data.worktree_path,
              agent_id: data.agent_id,
              label: agentLabel(agent || data),
              exit_code: data.exit_code ?? null,
              at: Date.now(),
            };
            store.exits.value = [...store.exits.value.slice(-19), exit];
          }
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
      case "job.started":
        store.jobs.value = [
          ...store.jobs.value.filter((j) => j.id !== data.job_id),
          { id: data.job_id, title: data.title, command: data.command, text: "", exitCode: undefined },
        ];
        store.drawerOpen.value = true;
        return false;
      case "job.output":
        store.jobs.value = store.jobs.value.map((j) => {
          if (j.id !== data.job_id) return j;
          const text = j.text + data.text;
          return { ...j, text: text.length > JOB_OUTPUT_LIMIT ? text.slice(-JOB_OUTPUT_LIMIT) : text };
        });
        return false;
      case "job.finished": {
        const job = store.jobs.value.find((j) => j.id === data.job_id);
        store.jobs.value = store.jobs.value.map((j) =>
          j.id === data.job_id ? { ...j, exitCode: data.exit_code } : j,
        );
        if (job) {
          const ok = data.exit_code === 0;
          store.toast(`${job.title}: ${ok ? "done" : `failed (${data.exit_code ?? "killed"})`}`, ok ? "success" : "error");
        }
        return false;
      }
      default:
        return false;
    }
  };

  return store;
}
