// The worktree list: the orchestrator row, then each worktree with its git
// and PR badges and its agents. Rows are memoised, so an event about one
// worktree re-renders that row only.

import { html, memo } from "../lib/html.js";
import { ORCHESTRATOR_KEY, STATUS_TEXT, agentLabel, basename, ciSummary } from "../lib/format.js";

const CI_TEXT = { pass: "✓ CI", fail: "✗ CI", pending: "… CI" };

function AgentList({ agents, path, currentAgent, onAgent }) {
  return html`<ul class="agents">
    ${agents.map((agent) => {
      const status = agent.status || "no_agent";
      const current = currentAgent === agent.agent_id;
      return html`<li
        key=${agent.agent_id}
        class=${`agent s-${status}${current ? " current" : ""}`}
        data-agent=${agent.agent_id}
        title=${agent.detail || status}
        onClick=${(event) => {
          event.stopPropagation();
          onAgent(path, agent.agent_id);
        }}
      >
        <span class="dot"></span>
        <span class="label">${agentLabel(agent)}</span>
        <span class="status">${STATUS_TEXT[status] || status}</span>
      </li>`;
    })}
  </ul>`;
}

function GitBadges({ git, pr }) {
  const badges = [];
  if (git) {
    if (git.dirty_count) badges.push(html`<span class="dirty">●${git.dirty_count}</span>`);
    if (git.ahead) badges.push(html`<span class="ahead">↑${git.ahead}</span>`);
    if (git.behind) badges.push(html`<span class="behind">↓${git.behind}</span>`);
    if (!git.dirty_count && !git.ahead && !git.behind) badges.push(html`<span class="tag">clean</span>`);
  }
  if (pr) {
    const state = (pr.state || "").toLowerCase();
    badges.push(html`<span class=${`pr ${state}`} title=${pr.title}>#${pr.number} ${state}</span>`);
    const ci = ciSummary(pr.checks);
    if (ci) badges.push(html`<span class=${`ci ${ci}`}>${CI_TEXT[ci]}</span>`);
  }
  return badges.length ? html`<div class="wt-meta">${badges}</div>` : null;
}

export const WorktreeRow = memo(function WorktreeRow({ wt, pr, current, currentAgent, onSelect, onAgent }) {
  return html`<li
    class=${`wt${current ? " current" : ""}`}
    data-path=${wt.path}
    tabindex=${current ? 0 : -1}
    aria-selected=${current}
    role="treeitem"
    onClick=${() => onSelect(wt.path)}
  >
    <div class="wt-head">
      <span class="wt-name">${basename(wt.path)}</span>
      ${wt.is_main && html`<span class="tag">main</span>`}
    </div>
    <div class="wt-branch">${wt.branch || `(detached ${wt.head.slice(0, 7)})`}</div>
    <${GitBadges} git=${wt.git_status} pr=${pr} />
    ${wt.agents && wt.agents.length > 0 &&
    html`<${AgentList} agents=${wt.agents} path=${wt.path} currentAgent=${currentAgent} onAgent=${onAgent} />`}
  </li>`;
});

const OrchestratorRow = memo(function OrchestratorRow({ agents, current, currentAgent, onSelect, onAgent }) {
  return html`<li
    class=${`wt orchestrator${current ? " current" : ""}`}
    data-path=${ORCHESTRATOR_KEY}
    tabindex=${current ? 0 : -1}
    aria-selected=${current}
    role="treeitem"
    onClick=${() => onSelect(null)}
  >
    <div class="wt-head"><span class="wt-name">Orchestrator</span></div>
    ${agents.length > 0 &&
    html`<${AgentList} agents=${agents} path=${ORCHESTRATOR_KEY} currentAgent=${currentAgent} onAgent=${onAgent} />`}
  </li>`;
});

/** What the sidebar highlights: the page's selection (null = the
 * orchestrator) and the agent whose tab it shows. */
export function selection(store) {
  const agent = store.shownAgent();
  return { path: store.selected.value, agent: agent ? agent.agent_id : null };
}

export function WorktreeList({ store, actions }) {
  store.activeAgent.value;
  const sel = selection(store);
  const prInfo = store.prInfo.value;
  return html`<ul id="worktrees" aria-label="Worktrees" role="tree">
    <${OrchestratorRow}
      agents=${store.orchestrator.value}
      current=${sel.path === null}
      currentAgent=${sel.path === null ? sel.agent : null}
      onSelect=${actions.select}
      onAgent=${actions.focusAgent}
    />
    ${store.worktrees.value.map(
      (wt) => html`<${WorktreeRow}
        key=${wt.path}
        wt=${wt}
        pr=${prInfo[wt.path]}
        current=${sel.path === wt.path}
        currentAgent=${sel.path === wt.path ? sel.agent : null}
        onSelect=${actions.select}
        onAgent=${actions.focusAgent}
      />`,
    )}
  </ul>`;
}

const TOOLBAR = [
  ["spawn", "Spawn", "Spawn an agent (s)"],
  ["create", "New", "New worktree (c)"],
  ["remove", "Remove", "Remove worktree (d)"],
  ["shell", "Shell", "Worktree shell"],
  ["diff", "Diff", "Diff"],
];

function Toolbar({ store, actions }) {
  const view = store.view.value;
  const enabled = store.engine.value && store.exited.value === null;
  const path = store.selected.value;
  const onWorktree = Boolean(path);
  const wt = onWorktree ? store.worktrees.value.find((w) => w.path === path) : null;
  const modalOpen = Boolean(view && view.focus === "modal");
  const allowed = {
    spawn: enabled && !modalOpen,
    create: enabled && !modalOpen,
    remove: enabled && !modalOpen && Boolean(wt) && !wt.is_main,
    shell: enabled && onWorktree,
    diff: enabled && onWorktree,
  };
  return html`<nav id="toolbar" aria-label="Actions">
    ${TOOLBAR.map(
      ([action, label, title]) => html`<button
        type="button"
        data-action=${action}
        title=${title}
        disabled=${!allowed[action]}
        onClick=${() => actions.toolbar(action)}
      >
        ${label}
      </button>`,
    )}
  </nav>`;
}

export function Sidebar({ store, actions }) {
  return html`<aside id="sidebar">
    <${Toolbar} store=${store} actions=${actions} />
    <${WorktreeList} store=${store} actions=${actions} />
    <p id="engine-state" class="muted">
      ${store.engine.value ? `${store.worktrees.value.length} worktree(s)` : "waiting for the engine…"}
    </p>
  </aside>`;
}
