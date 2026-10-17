// The page's frame: top bar, toasts, the help sheet and the hosted TUI pane.

import { html, Component } from "../lib/html.js";
import { KEYMAP, SCOPES } from "../lib/keymap.js";
import { theme, cycleTheme } from "../lib/theme.js";
import { NOTIFY_KINDS } from "../lib/store.js";
import { JobsButton } from "./jobs.js";

const THEME_TEXT = { dark: "Dark", light: "Light", system: "System" };

function NotifyButton() {
  if (!("Notification" in window) || Notification.permission !== "default") return null;
  return html`<button
    id="notify"
    type="button"
    title="Enable desktop notifications"
    onClick=${async (event) => {
      await Notification.requestPermission();
      event.target.hidden = Notification.permission !== "default";
    }}
  >
    Notifications
  </button>`;
}

function ViewSwitch({ store, actions }) {
  const view = store.centerView.value;
  const count = store.attention.value.length;
  return html`<div class="views-switch" role="tablist" aria-label="Views">
    <button
      type="button"
      role="tab"
      id="view-worktrees"
      aria-selected=${view !== "attention"}
      class=${view !== "attention" ? "on" : ""}
      title="Worktrees (g w)"
      onClick=${actions.showNative}
    >
      Worktrees
    </button>
    <button
      type="button"
      role="tab"
      id="view-attention"
      aria-selected=${view === "attention"}
      class=${`${view === "attention" ? "on" : ""}${count ? " has" : ""}`}
      title="Needs attention (g a)"
      onClick=${actions.showAttention}
    >
      Attention${count ? html` <span class="count">${count}</span>` : ""}
    </button>
  </div>`;
}

class Settings extends Component {
  constructor(props) {
    super(props);
    this.state = { open: false };
  }
  render({ store }) {
    const settings = store.notifySettings.value;
    return html`<span class="settings">
      <button
        id="settings-button"
        type="button"
        title="Notifications"
        aria-expanded=${this.state.open}
        onClick=${() => this.setState({ open: !this.state.open })}
      >
        ⚙
      </button>
      ${this.state.open &&
      html`<div id="settings" class="popover" role="dialog" aria-label="Notification settings">
        <strong>Desktop notifications</strong>
        <p class="muted">When the page is not focused. Kept in this browser.</p>
        ${Object.entries(NOTIFY_KINDS).map(
          ([kind, text]) => html`<label class="inline">
            <input
              type="checkbox"
              data-kind=${kind}
              checked=${settings[kind] !== false}
              onChange=${(e) => store.setNotify(kind, e.target.checked)}
            />
            ${text}
          </label>`,
        )}
      </div>`}
    </span>`;
  }
}

export function TopBar({ store, actions }) {
  const conn = store.conn.value;
  return html`<header id="topbar">
    <span class="brand">lazyagent</span>
    <${ViewSwitch} store=${store} actions=${actions} />
    <span id="repo" class="muted">${store.repoRoot.value}</span>
    <span class="spacer"></span>
    <${JobsButton} store=${store} />
    <span id="conn" class=${`pill ${conn.kind}`}>${conn.text}</span>
    <${NotifyButton} />
    <${Settings} store=${store} />
    <button
      id="theme"
      type="button"
      title="Theme: dark, light or the system's"
      onClick=${cycleTheme}
    >
      ${THEME_TEXT[theme.value]}
    </button>
    <button
      id="help-button"
      type="button"
      title="Keyboard shortcuts (?)"
      aria-label="Keyboard shortcuts"
      onClick=${() => (store.help.value = true)}
    >
      ?
    </button>
  </header>`;
}

export function Toasts({ store }) {
  return html`<div id="toasts" aria-live="polite">
    ${store.toasts.value.map(
      (t) => html`<div key=${t.id} class=${`toast ${t.severity}`}>${t.text}</div>`,
    )}
  </div>`;
}

export class HelpSheet extends Component {
  componentDidMount() {
    this.opener = document.activeElement;
    this.base.querySelector(".close")?.focus();
  }
  componentWillUnmount() {
    if (this.opener && this.opener.isConnected) this.opener.focus();
  }
  render({ onClose }) {
    const groups = Object.entries(SCOPES).map(([scope, title]) => [
      title,
      KEYMAP.filter((k) => k.scope === scope),
    ]);
    return html`<div class="backdrop" onClick=${(e) => e.target === e.currentTarget && onClose()}>
      <div id="help" class="sheet" role="dialog" aria-modal="true" aria-labelledby="help-title">
        <div class="sheet-head">
          <h2 id="help-title">Keyboard</h2>
          <button type="button" class="close" onClick=${onClose} aria-label="Close">Esc</button>
        </div>
        <p class="muted">
          Keys marked “TUI” act in the TUI tab, exactly as in a terminal; there every key goes to the TUI.
        </p>
        ${groups.map(
          ([title, rows]) => html`<section>
            <h3>${title}</h3>
            <table>
              ${rows.map(
                (row) => html`<tr data-tui-key=${row.tui.join(" ")}>
                  <td class="keys">${row.keys.map((k) => html`<kbd>${k}</kbd>`)}</td>
                  <td>${row.action}</td>
                  <td class="where">${row.where === "tui" ? "TUI" : "page"}</td>
                </tr>`,
              )}
            </table>
          </section>`,
        )}
      </div>
    </div>`;
  }
}

/** The hosted TUI's terminal. Rendered once: xterm.js owns its subtree. */
export class TuiPane extends Component {
  shouldComponentUpdate() {
    return false;
  }
  componentDidMount() {
    this.props.tui.mount(this.base.querySelector("#terminal"), this.base);
  }
  render() {
    return html`<section id="terminal-pane"><div id="terminal"></div></section>`;
  }
}

export function Overlay({ store }) {
  const text = store.overlay.value;
  return html`<div id="overlay" hidden=${text === null}>${text}</div>`;
}
