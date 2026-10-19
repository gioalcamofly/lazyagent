// The diff viewer (ui-plan §3d, U5 stage 1): get_diff's text parsed in the
// page (lib/diff.js), a file tree, unified or split rows, virtualised (only
// the rows on screen exist), comments kept in this browser, and "Submit
// review" composing them into one prompt for an agent (send_agent_input).

import { html, Component } from "../lib/html.js";
import { splitRows, anchorOf, reviewPrompt } from "../lib/diff.js";
import { agentLabel, basename } from "../lib/format.js";
import { Dialog } from "./dialogs.js";

export const ROW = 18;
const COMMENTABLE = new Set(["add", "del", "ctx"]);
const OVERSCAN = 30;

/**
 * The flat list the viewer scrolls: a header per file, then its rows (or
 * split pairs), and a row under every commented line.
 */
export function flatten(files, mode, comments, { uncommitted = true } = {}) {
  const byAnchor = new Map(comments.map((c) => [c.id, c]));
  const out = [];
  const starts = [];
  let width = 20;
  files.forEach((file, index) => {
    if (file.uncommitted && !uncommitted) return;
    if (file.uncommitted && !files[index - 1]?.uncommitted) out.push({ type: "section" });
    starts[index] = out.length;
    out.push({ type: "file", file, index });
    const rows = mode === "split" ? splitRows(file.rows) : file.rows;
    for (const row of rows) {
      if (mode === "split") {
        out.push({ type: "pair", file, pair: row });
        const len = Math.max(row.left?.text.length || 0, row.right?.text.length || 0, row.text?.length || 0);
        if (len > width) width = len;
        // A context line is one line on both sides: one anchor.
        const sides = row.left && row.left === row.right ? [row.right] : [row.left, row.right];
        for (const side of sides) {
          if (!side || !COMMENTABLE.has(side.kind)) continue;
          const anchor = anchorOf(file.path, side, file.uncommitted);
          if (byAnchor.has(anchor.id)) out.push({ type: "comment", comment: byAnchor.get(anchor.id) });
        }
      } else {
        out.push({ type: "row", file, row });
        if (row.text.length > width) width = row.text.length;
        if (COMMENTABLE.has(row.kind)) {
          const anchor = anchorOf(file.path, row, file.uncommitted);
          if (byAnchor.has(anchor.id)) out.push({ type: "comment", comment: byAnchor.get(anchor.id) });
        }
      }
    }
  });
  return { rows: out, starts, width };
}

function Gutter({ n }) {
  return html`<span class="ln">${n ?? ""}</span>`;
}

function Line({ row, onComment, file }) {
  if (!row) return html`<span class="half none"></span>`;
  const sign = { add: "+", del: "-", ctx: " " }[row.kind] ?? "";
  return html`<span class=${`half ${row.kind}`}>
    <button
      type="button"
      class="ln"
      tabindex="-1"
      title="Comment on this line"
      onClick=${() => onComment(file, row)}
    >${row.kind === "del" ? row.old : row.new}</button
    ><span class="sign">${sign}</span><span class="code">${row.text}</span>
  </span>`;
}

function renderRow(item, index, mode, onComment) {
  const style = `top:${index * ROW}px`;
  if (item.type === "file") {
    const f = item.file;
    return html`<div key=${index} class=${`drow file${f.uncommitted ? " uncommitted" : ""}`} style=${style} data-file=${f.path}>
      <span class=${`fstatus ${f.status}`}>${f.status}</span>
      <strong>${f.path}</strong>
      ${f.oldPath !== f.path && html`<span class="muted">from ${f.oldPath}</span>`}
      <span class="adds">+${f.added}</span><span class="dels">−${f.removed}</span>
      ${f.uncommitted && html`<span class="part">uncommitted</span>`}
    </div>`;
  }
  if (item.type === "section") {
    return html`<div key=${index} class="drow section" style=${style}>
      Uncommitted: staged, unstaged and untracked (not in the pull request yet)
    </div>`;
  }
  if (item.type === "comment") {
    const c = item.comment;
    return html`<div key=${index} class="drow comment" style=${style} title=${c.text}>
      <span class="ln"></span><span class="ln"></span>
      <span class="code">💬 ${c.text.split("\n")[0]}</span>
    </div>`;
  }
  if (item.type === "pair") {
    const p = item.pair;
    if (p.kind === "hunk" || p.kind === "note") {
      return html`<div key=${index} class=${`drow ${p.kind}`} style=${style}><span class="code">${p.text}</span></div>`;
    }
    return html`<div key=${index} class="drow split" style=${style}>
      <${Line} row=${p.left} file=${item.file} onComment=${onComment} />
      <${Line} row=${p.right} file=${item.file} onComment=${onComment} />
    </div>`;
  }
  const r = item.row;
  if (r.kind === "hunk" || r.kind === "note") {
    return html`<div key=${index} class=${`drow ${r.kind}`} style=${style}><span class="ln"></span><span class="ln"></span><span class="code">${r.text}</span></div>`;
  }
  return html`<div key=${index} class=${`drow ${r.kind}`} style=${style}>
    <${Gutter} n=${r.old} />
    <button type="button" class="ln" tabindex="-1" title="Comment on this line" onClick=${() => onComment(item.file, r)}>${r.new ?? ""}</button>
    <span class="sign">${{ add: "+", del: "-", ctx: " " }[r.kind]}</span><span class="code">${r.text}</span>
  </div>`;
}

/** The virtualised rows: only those in view (plus some) are in the DOM. */
export class DiffRows extends Component {
  constructor(props) {
    super(props);
    this.state = { top: 0, height: 600 };
    this.onScroll = this.onScroll.bind(this);
    this.frame = 0;
  }
  componentDidMount() {
    this.measure();
    this.observer = new ResizeObserver(() => this.measure());
    this.observer.observe(this.base);
  }
  componentWillUnmount() {
    this.observer?.disconnect();
    cancelAnimationFrame(this.frame);
  }
  measure() {
    if (this.base) this.setState({ height: this.base.clientHeight, top: this.base.scrollTop });
  }
  onScroll() {
    // At most one re-render per frame, whatever the scroll events.
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.setState({ top: this.base.scrollTop });
    });
  }
  scrollToRow(index) {
    this.base.scrollTop = index * ROW;
    this.setState({ top: this.base.scrollTop });
  }
  render({ flat, mode, onComment }) {
    const { top, height } = this.state;
    const first = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
    const last = Math.min(flat.rows.length, Math.ceil((top + height) / ROW) + OVERSCAN);
    const shown = [];
    for (let i = first; i < last; i++) shown.push(renderRow(flat.rows[i], i, mode, onComment));
    const width = mode === "split" ? `calc(2 * (${flat.width + 10}ch))` : `calc(${flat.width + 14}ch)`;
    return html`<div class="diff-scroll" onScroll=${this.onScroll} tabindex="0">
      <div class=${`diff-rows ${mode}`} style=${`height:${flat.rows.length * ROW}px;min-width:${width}`}>${shown}</div>
    </div>`;
  }
}

class CommentEditor extends Component {
  constructor(props) {
    super(props);
    this.state = { text: props.existing?.text || "" };
  }
  componentDidMount() {
    this.base.querySelector("textarea")?.focus();
  }
  save() {
    this.props.onSave(this.state.text.trim());
  }
  render({ anchor, existing, onCancel }) {
    return html`<div id="comment-editor" class="comment-editor" role="dialog" aria-label="Comment">
      <div class="muted">${anchor.path}:${anchor.line}${anchor.side === "old" ? " (removed)" : ""}</div>
      <pre class="quote">${anchor.code}</pre>
      <textarea
        rows="3"
        value=${this.state.text}
        placeholder="What should change here? (Ctrl+Enter saves)"
        onInput=${(e) => this.setState({ text: e.target.value })}
        onKeyDown=${(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            this.save();
          } else if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            onCancel();
          }
        }}
      ></textarea>
      <div class="form-actions">
        <span></span>
        ${existing && html`<button type="button" onClick=${() => this.props.onSave("")}>Delete</button>`}
        <button type="button" onClick=${onCancel}>Cancel</button>
        <button type="button" class="primary" onClick=${() => this.save()}>Save</button>
      </div>
    </div>`;
  }
}

/** Which agents are ready to read a review right away. */
export const READY = new Set(["waiting", "waiting_for_user", "completed"]);

export class SubmitDialog extends Component {
  constructor(props) {
    super(props);
    const agents = props.agents;
    const ready = agents.find((a) => READY.has(a.status));
    this.state = { agentId: (ready || agents[agents.length - 1])?.agent_id, summary: "", clear: true, busy: false, error: null };
  }
  async submit(whenIdle) {
    const agent = this.props.agents.find((a) => a.agent_id === this.state.agentId);
    if (!agent) return;
    this.setState({ busy: true, error: null });
    try {
      const text = reviewPrompt(this.props.comments, { summary: this.state.summary });
      await this.props.onSend(agent, text, { whenIdle, clear: this.state.clear });
      this.props.onClose();
    } catch (e) {
      this.setState({ busy: false, error: e.message });
    }
  }
  render({ agents, comments, onClose, path }) {
    const s = this.state;
    const agent = agents.find((a) => a.agent_id === s.agentId);
    const ready = agent && READY.has(agent.status);
    const prompt = reviewPrompt(comments, { summary: s.summary });
    return html`<${Dialog} id="review-dialog" title=${`Submit review of ${basename(path)}`} onClose=${onClose}>
      ${agents.length === 0
        ? html`<p class="form-error">No agent in this worktree to send it to.</p>`
        : html`<div class="form">
            <label>
              <span class="label-head">To</span>
              <select value=${s.agentId} onChange=${(e) => this.setState({ agentId: e.target.value })}>
                ${agents.map((a) => html`<option value=${a.agent_id}>${agentLabel(a)} · ${a.status}</option>`)}
              </select>
            </label>
            <label>
              <span class="label-head">Summary <span class="muted">optional</span></span>
              <textarea rows="2" value=${s.summary} onInput=${(e) => this.setState({ summary: e.target.value })}></textarea>
            </label>
            <label>
              <span class="label-head">What it receives</span>
              <pre class="prompt-preview">${prompt}</pre>
            </label>
            <label class="inline">
              <input type="checkbox" checked=${s.clear} onChange=${(e) => this.setState({ clear: e.target.checked })} />
              Clear the comments once sent
            </label>
          </div>`}
      ${s.error && html`<p class="form-error" role="alert">${s.error}</p>`}
      <div class="form-actions">
        <span class="muted">${agent && !ready ? `${agentLabel(agent)} is ${agent.status}: it would get this mid-task.` : ""}</span>
        <button type="button" onClick=${onClose}>Cancel</button>
        ${agent && !ready &&
        html`<button type="button" disabled=${s.busy} onClick=${() => this.submit(true)}>Send when idle</button>`}
        <button type="button" class="primary" disabled=${!agent || s.busy} onClick=${() => this.submit(false)}>
          ${ready ? "Send" : "Send now"}
        </button>
      </div>
    <//>`;
  }
}

/** Where a resolved base came from (get_diff's base.source). */
export function baseLabel(base) {
  if (base.source === "pr") return base.pr_number ? `PR #${base.pr_number} base` : "PR base";
  if (base.source === "default") return "default branch";
  return base.source;
}

/** The base a Base field edit asks for: "" (let the engine pick) when it is
 * cleared, or still shows the base the engine picked. */
export function baseRequest(value, requested, resolved) {
  const v = value.trim();
  return !requested && resolved && v === resolved.ref ? "" : v;
}

export class DiffView extends Component {
  constructor(props) {
    super(props);
    this.state = { editing: null };
    this.rows = null;
    this.onComment = this.onComment.bind(this);
  }
  onComment(file, row) {
    if (!COMMENTABLE.has(row.kind)) return;
    const anchor = anchorOf(file.path, row, file.uncommitted);
    this.setState({ editing: anchor });
  }
  saveComment(text) {
    const { store, actions } = this.props;
    actions.setComment(store.selected.value, this.state.editing, text);
    this.setState({ editing: null });
  }
  render({ store, actions }) {
    const path = store.selected.value;
    const diff = store.diff.value;
    const mode = store.diffMode.value;
    const comments = (path && store.comments.value[path]) || [];
    if (!path) return html`<div class="diff-view"><div class="empty">Select a worktree to see its diff.</div></div>`;
    const current = diff && diff.path === path ? diff : null;
    const showUncommitted = store.diffUncommitted.value;
    const flat = current?.parsed ? flatten(current.parsed.files, mode, comments, { uncommitted: showUncommitted }) : null;
    const requested = store.diffBase.value;
    const resolved = current?.base;
    const files = current?.parsed ? current.parsed.files.filter((f) => showUncommitted || !f.uncommitted) : [];
    const editing = this.state.editing;
    const existing = editing && comments.find((c) => c.id === editing.id);
    return html`<div class="diff-view">
      <div class="diff-head">
        <strong>Diff · ${basename(path)}</strong>
        <label class="inline muted">
          against
          <input
            type="text"
            class="base"
            size="24"
            value=${requested || resolved?.ref || ""}
            placeholder="PR base"
            title="Any ref (its merge-base with HEAD); empty: the pull request's base, else the default branch"
            onChange=${(e) => actions.loadDiff(path, baseRequest(e.target.value, requested, resolved))}
          />
        </label>
        ${resolved && html`<span class="muted base-source" title=${`from merge-base ${resolved.merge_base.slice(0, 10)}`}>(${baseLabel(resolved)})</span>`}
        ${requested && html`<button type="button" class="link" title="Back to the base the engine picks" onClick=${() => actions.loadDiff(path, "")}>PR base</button>`}
        <label class="inline muted" title="Staged, unstaged and untracked changes">
          <input type="checkbox" class="show-uncommitted" checked=${showUncommitted} onChange=${(e) => actions.diffUncommitted(e.target.checked)} />
          Uncommitted
        </label>
        <div class="seg" role="group" aria-label="Layout">
          <button type="button" class=${mode === "unified" ? "on" : ""} onClick=${() => actions.diffMode("unified")}>Unified</button>
          <button type="button" class=${mode === "split" ? "on" : ""} onClick=${() => actions.diffMode("split")}>Split</button>
        </div>
        <button type="button" class="link" title="Fetch the base from origin and diff again" onClick=${() => actions.loadDiff(path, requested, { fetch: true })}>↻</button>
        <span class="spacer"></span>
        <span class="muted" title="Comments are kept in this browser until the engine stores them (U8)">
          ${comments.length} comment${comments.length === 1 ? "" : "s"} · in this browser
        </span>
        <button
          type="button"
          class="primary"
          id="submit-review"
          disabled=${comments.length === 0}
          onClick=${() => actions.openDialog("review")}
        >
          Submit review
        </button>
      </div>
      ${current?.note && html`<div class="diff-note">${current.note}</div>`}
      ${current?.loading && !current.parsed && html`<div class="empty">Loading the diff…</div>`}
      ${current?.error && html`<div class="empty form-error">${current.error}</div>`}
      ${flat &&
      (flat.rows.length === 0
        ? html`<div class="empty">No changes${resolved ? ` against ${resolved.ref}` : ""}.</div>`
        : html`<div class="diff-body">
            <nav class="diff-files" aria-label="Files">
              ${current.parsed.truncated && html`<p class="form-error">Diff truncated: open the rest in your editor.</p>`}
              ${files.map(
                (f, i) => html`${f.uncommitted && !files[i - 1]?.uncommitted && html`<p class="dsection">Uncommitted</p>`}<button type="button" class=${`dfile${f.uncommitted ? " uncommitted" : ""}`} data-path=${f.path} onClick=${() => this.rows?.scrollToRow(flat.starts[i])}>
                  <span class=${`fstatus ${f.status}`}>${f.status[0].toUpperCase()}</span>
                  <span class="dname" title=${f.path}>${f.path}</span>
                  <span class="adds">+${f.added}</span><span class="dels">−${f.removed}</span>
                </button>`,
              )}
            </nav>
            <${DiffRows} ref=${(r) => (this.rows = r)} flat=${flat} mode=${mode} onComment=${this.onComment} />
          </div>`)}
      ${editing &&
      html`<${CommentEditor}
        key=${editing.id}
        anchor=${editing}
        existing=${existing}
        onSave=${(text) => this.saveComment(text)}
        onCancel=${() => this.setState({ editing: null })}
      />`}
    </div>`;
  }
}
