// The centre of the page: the selected worktree's agents as tabs of native
// terminals, its shell in a bottom pane, and the hosted TUI as one more tab
// (until U7). Terminal elements are owned by lib/terminals.js; the slots
// below only tell it where to show which session.

import { html, Component } from "../lib/html.js";
import { ORCHESTRATOR_KEY, STATUS_TEXT, agentLabel, basename } from "../lib/format.js";

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
    this.terminal()?.hide();
  }
  terminal(id = this.props.sessionId) {
    return id ? this.props.manager.sessions.get(id) : undefined;
  }
  sync(previousId) {
    if (previousId && previousId !== this.props.sessionId) this.terminal(previousId)?.hide();
    const { sessionId, active, manager } = this.props;
    if (!sessionId) return;
    if (active) manager.get(sessionId).show(this.base);
    else this.terminal()?.hide();
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
        Spawn an agent
      </button>
    </p>
  </div>`;
}

export function Center({ store, actions, manager, tuiPane }) {
  const agents = store.selectedAgents();
  const shown = store.shownAgent();
  const tui = store.centerView.value === "tui";
  const path = store.selected.value;
  const shellId = path ? store.shells.value[path] : undefined;
  const shellOpen = store.shellOpen.value && Boolean(path);
  return html`<div class=${`center${tui ? " tui-shown" : ""}`}>
    <${AgentTabs} store=${store} actions=${actions} agents=${agents} shown=${shown} />
    <div class="views">
      <${TerminalSlot}
        className="agent-slot"
        manager=${manager}
        sessionId=${shown?.session_id}
        active=${!tui && Boolean(shown)}
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
      <${TerminalSlot} className="shell-slot" manager=${manager} sessionId=${shellId} active=${Boolean(shellId)} />
    </div>`}
  </div>`;
}

export { ORCHESTRATOR_KEY };
