// The attention view (ui-plan §3b): the queue on the left, the picked
// item on the right with the agent's live terminal and a reply box; and the
// PR/CI panel of a worktree (§3a right panel, §4d).

import { html, Component } from "../lib/html.js";
import { ciFailurePrompt } from "../lib/attention.js";
import { agentLabel, basename, ciSummary } from "../lib/format.js";
import { TerminalSlot } from "./center.js";

export function age(ms, now = Date.now()) {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function QueueList({ store, items, picked, actions }) {
  const now = store.clock.value;
  return html`<ul
    id="attention-list"
    role="listbox"
    aria-label="Needs attention"
    tabindex="0"
    aria-activedescendant=${picked ? `att-${items.indexOf(picked)}` : undefined}
    onKeyDown=${(e) => actions.queueKey(e)}
  >
    ${items.length === 0 && html`<li class="queue-empty">Nothing needs you right now.</li>`}
    ${items.map(
      (item, index) => html`<li
        key=${item.id}
        id=${`att-${index}`}
        role="option"
        aria-selected=${item === picked}
        class=${`att k-${item.kind}${item === picked ? " picked" : ""}`}
        data-kind=${item.kind}
        data-item=${item.id}
        onClick=${() => actions.pickAttention(item)}
      >
        <div class="att-head">
          <span class=${`att-kind k-${item.kind}`}>${item.title}</span>
          <span class="att-where">${item.where}</span>
          <span
            class="att-age"
            onMouseEnter=${(e) => (e.currentTarget.title = new Date(item.since).toLocaleString())}
            >${age(item.since, Math.max(now, item.since))}</span
          >
        </div>
        <div class="att-label">${item.label}</div>
        <div class="att-detail">${item.detail}</div>
      </li>`,
    )}
  </ul>`;
}

class ReplyBox extends Component {
  constructor(props) {
    super(props);
    this.state = { text: "", busy: false, error: null };
  }
  componentDidUpdate(previous) {
    if (previous.target !== this.props.target) this.setState({ text: "", error: null });
  }
  async send() {
    const text = this.state.text.trim();
    if (!text || this.state.busy) return;
    this.setState({ busy: true, error: null });
    try {
      await this.props.actions.reply(this.props.item, text);
      this.setState({ text: "", busy: false });
    } catch (e) {
      this.setState({ busy: false, error: e.message });
    }
  }
  render({ item }) {
    const s = this.state;
    return html`<form class="reply" onSubmit=${(e) => (e.preventDefault(), this.send())}>
      <textarea
        name="reply"
        rows="3"
        placeholder=${`Reply to ${item.label} (Ctrl+Enter sends)`}
        value=${s.text}
        onInput=${(e) => this.setState({ text: e.target.value })}
        onKeyDown=${(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            this.send();
          }
        }}
      ></textarea>
      <div class="reply-actions">
        ${s.error ? html`<span class="form-error" role="alert">${s.error}</span>` : html`<span></span>`}
        <button type="submit" class="primary" disabled=${s.busy || !s.text.trim()}>${s.busy ? "Sending…" : "Send"}</button>
      </div>
    </form>`;
  }
}

function Checks({ pr }) {
  if (!pr.checks || pr.checks.length === 0) return html`<p class="muted">No checks.</p>`;
  return html`<ul class="checks">
    ${pr.checks.map((c) => {
      const verdict = (c.conclusion || c.status || "").toLowerCase();
      const cls = verdict === "success" ? "pass" : verdict === "failure" ? "fail" : "pending";
      return html`<li class=${`check ${cls}`}>
        <span class="check-state">${{ pass: "✓", fail: "✗", pending: "…" }[cls]}</span>
        ${c.details_url
          ? html`<a href=${c.details_url} target="_blank" rel="noopener noreferrer">${c.name}</a>`
          : html`<span>${c.name}</span>`}
        <span class="muted">${verdict || "queued"}</span>
      </li>`;
    })}
  </ul>`;
}

function PrSummary({ pr }) {
  const state = (pr.state || "").toLowerCase();
  return html`<div class="pr-summary">
    <div>
      <span class=${`pr ${state}`}>#${pr.number} ${state}</span>
      ${pr.url
        ? html`<a class="pr-title" href=${pr.url} target="_blank" rel="noopener noreferrer">${pr.title}</a>`
        : html`<span class="pr-title">${pr.title}</span>`}
    </div>
    <dl>
      <dt>Review</dt><dd>${(pr.review_decision || "none").toLowerCase().replace(/_/g, " ")}</dd>
      <dt>Mergeable</dt><dd>${(pr.mergeable || "unknown").toLowerCase()}</dd>
      <dt>CI</dt><dd>${ciSummary(pr.checks) || "no checks"}</dd>
    </dl>
  </div>`;
}

function ItemPane({ store, item, actions, manager }) {
  if (!item) return html`<div class="att-pane empty">Pick an item on the left.</div>`;
  const session = item.agent?.session_id;
  const worktreeAgent = item.path ? store.agentsOf(item.path)[0] : null;
  return html`<div class="att-pane">
    <div class="att-pane-head">
      <span class=${`att-kind k-${item.kind}`}>${item.title}</span>
      <strong>${item.label}</strong>
      <span class="muted">${item.where}</span>
      <span class="spacer"></span>
      ${item.key &&
      html`<button type="button" onClick=${() => actions.openItem(item)} title="Show it in the worktree view">Open</button>`}
      ${item.agent && html`<button type="button" onClick=${() => actions.stopItem(item)}>Stop</button>`}
      <button type="button" title="Hide for 15 minutes" onClick=${() => actions.snooze(item)}>Snooze</button>
      <button type="button" class="dismiss" onClick=${() => actions.dismiss(item)}>Dismiss</button>
    </div>
    ${item.agent &&
    html`<div class="att-term"><${TerminalSlot} className="att-slot" manager=${manager} sessionId=${session} active=${Boolean(session)} /></div>
      <${ReplyBox} item=${item} target=${item.id} actions=${actions} />`}
    ${item.pr &&
    html`<div class="att-pr">
      <${PrSummary} pr=${item.pr} />
      <${Checks} pr=${item.pr} />
      ${item.kind === "ci" &&
      (worktreeAgent
        ? html`<button type="button" class="primary" onClick=${() => actions.sendFailures(item, worktreeAgent)}>
            Send the failures to ${agentLabel(worktreeAgent)}
          </button>`
        : html`<p class="muted">No agent in ${basename(item.path)} to send the failures to.</p>`)}
    </div>`}
    ${item.exit && html`<div class="att-exit"><p>${item.label} ${item.detail}.</p>
      <button type="button" class="primary" onClick=${() => actions.respawn(item)}>Spawn again (resume the last session)</button></div>`}
    ${item.job && html`<div class="att-job"><pre class="job-output">${item.job.text.slice(-4000)}</pre></div>`}
  </div>`;
}

export function AttentionView({ store, actions, manager }) {
  const items = store.attention.value;
  const pickedId = store.attentionPick.value;
  const picked = items.find((i) => i.id === pickedId) || items[0] || null;
  return html`<div class="attention">
    <${QueueList} store=${store} items=${items} picked=${picked} actions=${actions} />
    <${ItemPane} store=${store} item=${picked} actions=${actions} manager=${manager} />
  </div>`;
}

export function PrPanel({ store, actions }) {
  const path = store.selected.value;
  if (!store.prPanel.value || !path) return null;
  const pr = store.prInfo.value[path];
  const fetched = store.prFetched.value[path];
  let body;
  if (pr) body = html`<${PrSummary} pr=${pr} /><${Checks} pr=${pr} />`;
  else if (fetched === false) body = html`<p class="muted">GitHub CLI not available: no PR information.</p>`;
  else if (pr === null) {
    const wt = store.worktrees.value.find((w) => w.path === path);
    body = html`<p class="muted">No PR for <code>${wt?.branch || basename(path)}</code>.</p>
      <p class="muted">Open one with <code>gh pr create</code> in the shell.</p>`;
  } else body = html`<p class="muted">Looking for a PR…</p>`;
  return html`<aside id="pr-panel" aria-label="Pull request">
    <div class="pane-head">
      <span>Pull request · ${basename(path)}</span>
      <span>
        <button type="button" class="link" title="Refresh" onClick=${() => actions.refreshPr(path)}>↻</button>
        <button type="button" class="link" title="Close (Ctrl+.)" onClick=${() => (store.prPanel.value = false)}>×</button>
      </span>
    </div>
    <div class="pr-body">${body}</div>
  </aside>`;
}

export { ciFailurePrompt };
