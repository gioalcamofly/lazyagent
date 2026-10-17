// The centre of the page: the selected worktree's agents as tabs of native
// terminals, its shell in a bottom pane, and the hosted TUI as one more tab
// (until U7). Terminal elements are owned by lib/terminals.js; the slots
// below only tell it where to show which session.

import { html, Component } from "../lib/html.js";
import { ORCHESTRATOR_KEY, STATUS_TEXT, agentLabel, basename } from "../lib/format.js";
import { GitBadges } from "./sidebar.js";

/** Shows one session's terminal in its box; hides it when another session
 * takes the slot or the slot goes away. */
export class TerminalSlot extends Component {
  shouldComponentUpdate(next) {
    return next.sessionId !== this.props.sessionId || next.active !== this.props.active;
  }
  componentDidMount() {
    this.sync(null);
  }
  componentDidUpdate(previous) {
    this.sync(previous.sessionId);
  }
  componentWillUnmount() {
    this.release(this.props.sessionId);
  }
  /** Hide a session's terminal, unless another slot has taken it since. */
  release(id) {
    const terminal = this.terminal(id);
    if (terminal && this.base && this.base.contains(terminal.element)) terminal.hide();
  }
  terminal(id = this.props.sessionId) {
    return id ? this.props.manager.sessions.get(id) : undefined;
  }
  sync(previousId) {
    if (previousId && previousId !== this.props.sessionId) this.release(previousId);
    const { sessionId, active, manager } = this.props;
    if (!sessionId) return;
    if (active) manager.get(sessionId).show(this.base);
    else this.release(sessionId);
  }
  render({ className }) {
    return html`<div class=${`term-slot ${className || ""}`}></div>`;
  }
}

function AgentTabs({ store, actions, agents, shown }) {
  const tui = store.centerView.value === "tui";
  const path = store.selected.value;
  const shellable = path !== null && path !== undefined;
  return html`<div class="tabs" role="tablist" aria-label="Agents">
    ${agents.map((agent) => {
      const status = agent.status || "no_agent";
      const active = !tui && shown && shown.agent_id === agent.agent_id;
      return html`<button
        key=${agent.agent_id}
        type="button"
        role="tab"
        aria-selected=${active}
        class=${`tab s-${status}${active ? " active" : ""}`}
        data-agent=${agent.agent_id}
        title=${agent.detail || status}
        onClick=${() => actions.showAgent(agent.agent_id)}
      >
        <span class="dot"></span>${agentLabel(agent)}
        <span class="status">${STATUS_TEXT[status] || status}</span>
      </button>`;
    })}
    <span class="spacer"></span>
    ${shellable &&
    html`<button
      type="button"
      class=${`tab-tool${store.shellOpen.value ? " on" : ""}`}
      data-tool="shell"
      title="Worktree shell (Ctrl+L)"
      aria-pressed=${store.shellOpen.value}
      onClick=${actions.toggleShell}
    >
      Shell
    </button>`}
    <button
      type="button"
      role="tab"
      aria-selected=${tui}
      class=${`tab tui-tab${tui ? " active" : ""}`}
      title="The TUI, hosted (for what the page does not do yet)"
      onClick=${() => (tui ? actions.showNative() : actions.showTui())}
    >
      TUI
    </button>
  </div>`;
}

function EmptyState({ store, actions }) {
  const path = store.selected.value;
  if (path === undefined) return html`<div class="empty">Select a worktree</div>`;
  const where = path === null ? "the orchestrator" : basename(path);
  return html`<div class="empty">
    <p>No agent in ${where}.</p>
    <p>
      <button type="button" class="primary" data-empty-action="spawn" onClick=${() => actions.toolbar("spawn")}>
        Spawn an agent <kbd>s</kbd>
      </button>
    </p>
  </div>`;
}

function WorktreeHeader({ store, actions }) {
  const path = store.selected.value;
  if (path === undefined) return null;
  if (path === null) {
    return html`<div class="wt-header"><strong>Orchestrator</strong><span class="muted">${store.repoRoot.value}</span></div>`;
  }
  const wt = store.worktrees.value.find((w) => w.path === path);
  if (!wt) return null;
  return html`<div class="wt-header">
    <strong>${basename(wt.path)}</strong>
    <span class="wt-branch">${wt.branch || `(detached ${wt.head.slice(0, 7)})`}</span>
    <${GitBadges} git=${wt.git_status} pr=${store.prInfo.value[wt.path]} />
    <span class="spacer"></span>
    <button type="button" class="link" title="Refresh git status (Alt+G)" onClick=${actions.refreshGit}>↻ git</button>
    <button type="button" class="link" data-tool="pr" title="Pull request and CI (Ctrl+.)" onClick=${actions.togglePrPanel}>PR</button>
  </div>`;
}

export function Center({ store, actions, manager, tuiPane }) {
  const agents = store.selectedAgents();
  const shown = store.shownAgent();
  const tui = store.centerView.value === "tui";
  // The attention view has the screen: the worktree view lets go of its
  // terminals (it stays mounted, for the TUI's terminal).
  const away = store.centerView.value === "attention" || store.centerView.value === "diff";
  const path = store.selected.value;
  const shellId = path ? store.shells.value[path] : undefined;
  const shellOpen = store.shellOpen.value && Boolean(path);
  return html`<div class=${`center${tui ? " tui-shown" : ""}${away ? " away" : ""}`}>
    <${WorktreeHeader} store=${store} actions=${actions} />
    <${AgentTabs} store=${store} actions=${actions} agents=${agents} shown=${shown} />
    <div class="views">
      <${TerminalSlot}
        className="agent-slot"
        manager=${manager}
        sessionId=${shown?.session_id}
        active=${!tui && !away && Boolean(shown)}
      />
      ${!tui && !shown && html`<${EmptyState} store=${store} actions=${actions} />`}
      ${tuiPane}
    </div>
    ${shellOpen &&
    !tui &&
    html`<div class="shell-pane">
      <div class="pane-head">
        <span>Shell · ${basename(path)}</span>
        <button type="button" class="link" onClick=${actions.toggleShell} title="Close (Ctrl+L)">×</button>
      </div>
      <${TerminalSlot} className="shell-slot" manager=${manager} sessionId=${shellId} active=${Boolean(shellId) && !away} />
    </div>`}
  </div>`;
}

export { ORCHESTRATOR_KEY };
