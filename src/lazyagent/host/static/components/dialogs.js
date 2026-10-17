// The TUI's dialogs as forms (ui-plan §2b, §4a): spawn with an instruction,
// create a worktree (and launch an agent in it), remove a worktree, stop an
// agent. Each keeps the TUI's accelerators when the focus is not in a text
// field; Enter (Ctrl+Enter in the instruction) submits, Esc cancels.

import { html, Component } from "../lib/html.js";
import { agentLabel, basename } from "../lib/format.js";

const isText = (el) => el instanceof Element && el.matches("input[type=text], input:not([type]), textarea");

/** The frame of every dialog: modal, labelled, focus in on open and back
 * to the opener on close. */
export class Dialog extends Component {
  componentDidMount() {
    this.opener = document.activeElement;
    // In priority order: the close button comes first in the document.
    const first = [
      "[autofocus]",
      "textarea",
      "input",
      "button.primary:not(:disabled)",
      "button.danger:not(:disabled)",
      "button.close",
    ].reduce((found, selector) => found || this.base.querySelector(selector), null);
    first?.focus();
  }
  componentWillUnmount() {
    if (this.opener instanceof HTMLElement && this.opener.isConnected) this.opener.focus();
  }
  onKeyDown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.props.onClose();
      return;
    }
    if (this.props.onKey && !isText(event.target) && !event.ctrlKey && !event.altKey && !event.metaKey) {
      if (this.props.onKey(event.key)) {
        event.preventDefault();
        event.stopPropagation();
      }
    }
  }
  render({ id, title, children, onClose }) {
    return html`<div class="backdrop" onMouseDown=${(e) => e.target === e.currentTarget && onClose()}>
      <div
        id=${id}
        class="sheet dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby=${`${id}-title`}
        onKeyDown=${(e) => this.onKeyDown(e)}
      >
        <div class="sheet-head">
          <h2 id=${`${id}-title`}>${title}</h2>
          <button type="button" class="close" onClick=${onClose} aria-label="Close">Esc</button>
        </div>
        ${children}
      </div>
    </div>`;
  }
}

/** Submit state shared by the forms: busy flag and the inline error. */
class Form extends Component {
  async run(work) {
    if (this.state.busy) return;
    this.setState({ busy: true, error: null });
    try {
      await work();
      (this.props.onDone || this.props.onClose)();
    } catch (e) {
      this.setState({ busy: false, error: e.message || String(e) });
    }
  }
  errorLine() {
    return this.state.error ? html`<p class="form-error" role="alert">${this.state.error}</p>` : null;
  }
}

// ---------------------------------------------------------------- spawn

export class SpawnDialog extends Form {
  constructor(props) {
    super(props);
    this.state = { instruction: "", skip: false, resume: "new", label: "" };
  }
  submit(overrides = {}) {
    const { instruction, skip, resume, label } = { ...this.state, ...overrides };
    return this.run(() =>
      this.props.actions.spawn({
        path: this.props.path,
        instruction: instruction.trim(),
        skipPermissions: skip,
        resumeMode: resume,
        label: label.trim(),
      }),
    );
  }
  onKey(key) {
    // The TUI's spawn keys: n normal, d skip permissions, r / l resume.
    if (key === "n") return this.submit({ skip: false }), true;
    if (key === "d") return this.submit({ skip: true }), true;
    if (key === "r") return this.submit({ resume: "pick" }), true;
    if (key === "l") return this.submit({ resume: "last" }), true;
    return false;
  }
  render({ path, provider, onClose }) {
    const where = path === null ? "the orchestrator" : basename(path);
    const s = this.state;
    return html`<${Dialog} id="spawn-dialog" title=${`Spawn an agent in ${where}`} onClose=${onClose} onKey=${(k) => this.onKey(k)}>
      <form class="form" onSubmit=${(e) => (e.preventDefault(), this.submit())}>
        <label>
          <span class="label-head">Instruction <span class="muted">optional · Ctrl+Enter starts</span></span>
          <textarea
            name="instruction"
            rows="5"
            value=${s.instruction}
            placeholder="What should the agent do?"
            onInput=${(e) => this.setState({ instruction: e.target.value })}
            onKeyDown=${(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                this.submit();
              }
            }}
          ></textarea>
        </label>
        <fieldset>
          <legend>Permissions</legend>
          <label class="inline"><input type="radio" name="perm" checked=${!s.skip} onChange=${() => this.setState({ skip: false })} /> Ask as usual <kbd>n</kbd></label>
          <label class="inline"><input type="radio" name="perm" checked=${s.skip} onChange=${() => this.setState({ skip: true })} /> Skip permissions <kbd>d</kbd></label>
        </fieldset>
        <fieldset>
          <legend>Session</legend>
          ${[
            ["new", "New", ""],
            ["pick", "Resume (pick)", "r"],
            ["last", "Resume the last", "l"],
          ].map(
            ([value, text, key]) => html`<label class="inline">
              <input type="radio" name="resume" checked=${s.resume === value} onChange=${() => this.setState({ resume: value })} />
              ${text} ${key && html`<kbd>${key}</kbd>`}
            </label>`,
          )}
        </fieldset>
        <label>
          <span class="label-head">Label <span class="muted">optional</span></span>
          <input type="text" name="label" value=${s.label} onInput=${(e) => this.setState({ label: e.target.value })} />
        </label>
        ${this.errorLine()}
        <div class="form-actions">
          <span class="muted">${provider ? `Provider: ${provider}` : ""}</span>
          <button type="button" onClick=${onClose}>Cancel</button>
          <button type="submit" class="primary" disabled=${s.busy}>${s.busy ? "Starting…" : "Spawn"}</button>
        </div>
      </form>
    <//>`;
  }
}

// ---------------------------------------------------------------- create

/** The base a new worktree starts from: the configured default branch
 * when a worktree has it checked out, else the main worktree's branch (the
 * built-in default, "master", is often not a branch at all). */
export function defaultBase(config, worktrees) {
  const configured = config?.default_branch;
  if (configured && worktrees.some((w) => w.branch === configured)) return configured;
  return worktrees.find((w) => w.is_main)?.branch || configured || "main";
}

export class CreateDialog extends Form {
  constructor(props) {
    super(props);
    this.state = {
      branch: "",
      base: defaultBase(props.config, props.worktrees || []),
      extra: "",
      launch: false,
      instruction: "",
      skip: false,
    };
  }
  submit() {
    const s = this.state;
    if (!s.branch.trim()) {
      this.setState({ error: "A branch name is needed" });
      return;
    }
    return this.run(() =>
      this.props.actions.create({
        branch: s.branch.trim(),
        base: s.base.trim(),
        extra: s.extra.trim(),
        launch: s.launch ? { instruction: s.instruction.trim(), skipPermissions: s.skip } : null,
      }),
    );
  }
  render({ config, onClose }) {
    const s = this.state;
    return html`<${Dialog} id="create-dialog" title="New worktree" onClose=${onClose}>
      <form class="form" onSubmit=${(e) => (e.preventDefault(), this.submit())}>
        <label>
          Branch
          <input type="text" name="branch" autofocus value=${s.branch} onInput=${(e) => this.setState({ branch: e.target.value })} />
        </label>
        <label>
          Base
          <input type="text" name="base" value=${s.base} onInput=${(e) => this.setState({ base: e.target.value })} />
        </label>
        ${config?.has_custom_create &&
        html`<label>
          <span class="label-head">Extra options <span class="muted">for the custom create command</span></span>
          <input type="text" name="extra" value=${s.extra} onInput=${(e) => this.setState({ extra: e.target.value })} />
        </label>`}
        <label class="inline">
          <input type="checkbox" name="launch" checked=${s.launch} onChange=${(e) => this.setState({ launch: e.target.checked })} />
          and launch an agent in it
        </label>
        ${s.launch &&
        html`<label>
            Instruction
            <textarea
              name="instruction"
              rows="4"
              value=${s.instruction}
              onInput=${(e) => this.setState({ instruction: e.target.value })}
              onKeyDown=${(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  this.submit();
                }
              }}
            ></textarea>
          </label>
          <label class="inline">
            <input type="checkbox" name="skip" checked=${s.skip} onChange=${(e) => this.setState({ skip: e.target.checked })} />
            Skip permissions
          </label>`}
        ${this.errorLine()}
        <div class="form-actions">
          <span class="muted">${config?.has_custom_create ? "Runs the custom create command; its output shows in the job log" : ""}</span>
          <button type="button" onClick=${onClose}>Cancel</button>
          <button type="submit" class="primary" disabled=${s.busy}>${s.busy ? "Creating…" : "Create"}</button>
        </div>
      </form>
    <//>`;
  }
}

// ---------------------------------------------------------------- remove

/** Why a worktree cannot be removed, or null. */
export function removeGuard(wt) {
  if (!wt) return "Select a worktree first.";
  if (wt.is_main) return "The main worktree cannot be removed.";
  if ((wt.agents || []).some((a) => a.status !== "completed" && a.status !== "failed" && a.status !== "interrupted")) {
    return "An agent is running in this worktree: stop it first (x).";
  }
  return null;
}

export class RemoveDialog extends Form {
  constructor(props) {
    super(props);
    this.state = { force: false };
  }
  submit() {
    if (removeGuard(this.props.worktree)) return;
    return this.run(() => this.props.actions.remove(this.props.worktree.path, this.state.force));
  }
  onKey(key) {
    if (key === "y" || key === "Enter") return this.submit(), true;
    if (key === "n") return this.props.onClose(), true;
    if (key === "f") return this.setState({ force: !this.state.force }), true;
    return false;
  }
  render({ worktree, config, onClose }) {
    const guard = removeGuard(worktree);
    const s = this.state;
    return html`<${Dialog} id="remove-dialog" title="Remove worktree" onClose=${onClose} onKey=${(k) => this.onKey(k)}>
      ${worktree &&
      html`<p>Remove <strong>${basename(worktree.path)}</strong> (${worktree.branch || "detached"})?</p>`}
      ${guard
        ? html`<p class="form-error" role="alert">${guard}</p>`
        : html`<label class="inline">
              <input type="checkbox" name="force" checked=${s.force} onChange=${(e) => this.setState({ force: e.target.checked })} />
              Force (discard uncommitted changes) <kbd>f</kbd>
            </label>
            ${config?.has_custom_remove && html`<p class="muted">Runs the custom remove command; its output shows in the job log.</p>`}`}
      ${this.errorLine()}
      <div class="form-actions">
        <span></span>
        <button type="button" onClick=${onClose}>Cancel <kbd>n</kbd></button>
        <button type="button" class="danger" disabled=${Boolean(guard) || s.busy} onClick=${() => this.submit()}>
          ${s.busy ? "Removing…" : "Remove"} <kbd>y</kbd>
        </button>
      </div>
    <//>`;
  }
}

// ---------------------------------------------------------------- stop

export class StopDialog extends Form {
  constructor(props) {
    super(props);
    this.state = {};
  }
  submit() {
    return this.run(() => this.props.actions.stop(this.props.path, this.props.agent.agent_id));
  }
  onKey(key) {
    if (key === "y" || key === "Enter") return this.submit(), true;
    if (key === "n") return this.props.onClose(), true;
    return false;
  }
  render({ path, agent, onClose }) {
    const where = path === null ? "the orchestrator" : basename(path);
    return html`<${Dialog} id="stop-dialog" title="Stop agent" onClose=${onClose} onKey=${(k) => this.onKey(k)}>
      <p>Stop <strong>${agentLabel(agent)}</strong> in ${where}?</p>
      ${this.errorLine()}
      <div class="form-actions">
        <span></span>
        <button type="button" onClick=${onClose}>Cancel <kbd>n</kbd></button>
        <button type="button" class="danger" autofocus disabled=${this.state.busy} onClick=${() => this.submit()}>
          Stop <kbd>y</kbd>
        </button>
      </div>
    <//>`;
  }
}
