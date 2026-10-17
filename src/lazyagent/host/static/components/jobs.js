// The job drawer: output of the custom create/remove commands the engine
// runs as jobs (job.started / job.output / job.finished), newest first.

import { html, Component } from "../lib/html.js";

function jobState(job) {
  if (job.exitCode === undefined) return ["running", "running…"];
  if (job.exitCode === 0) return ["ok", "done"];
  return ["failed", job.exitCode === null ? "killed" : `exit ${job.exitCode}`];
}

class JobOutput extends Component {
  componentDidUpdate() {
    // Follow the output while it is at the bottom.
    const el = this.base;
    if (el && this.atBottom !== false) el.scrollTop = el.scrollHeight;
  }
  render({ text }) {
    return html`<pre
      class="job-output"
      onScroll=${(e) => {
        const el = e.currentTarget;
        this.atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
      }}
    >${text}</pre>`;
  }
}

export function JobDrawer({ store }) {
  const jobs = store.jobs.value;
  if (!store.drawerOpen.value || jobs.length === 0) return null;
  return html`<section id="jobs" class="drawer" aria-label="Jobs">
    <div class="pane-head">
      <span>Jobs</span>
      <button type="button" class="link" title="Close" onClick=${() => (store.drawerOpen.value = false)}>×</button>
    </div>
    <div class="drawer-body">
      ${[...jobs].reverse().map((job) => {
        const [state, text] = jobState(job);
        return html`<article key=${job.id} class=${`job ${state}`} data-job=${job.id}>
          <header>
            <strong>${job.title}</strong>
            <code>${job.command}</code>
            <span class=${`job-state ${state}`}>${text}</span>
          </header>
          <${JobOutput} text=${job.text} />
        </article>`;
      })}
    </div>
  </section>`;
}

export function JobsButton({ store }) {
  const jobs = store.jobs.value;
  if (jobs.length === 0) return null;
  const running = jobs.filter((j) => j.exitCode === undefined).length;
  return html`<button
    id="jobs-button"
    type="button"
    class=${running ? "busy" : ""}
    title="Job log"
    onClick=${() => (store.drawerOpen.value = !store.drawerOpen.value)}
  >
    Jobs${running ? ` (${running})` : ""}
  </button>`;
}
