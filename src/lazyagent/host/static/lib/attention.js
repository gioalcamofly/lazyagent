// The attention queue (ui-plan §3b), derived from the store: who needs the
// user, most urgent first. An item exists while its condition holds (an
// agent's status, a PR's state, a failed job, an unexpected exit) and clears
// itself when the condition does; dismissing or snoozing it is this page's
// own business (localStorage, Q9).

import { ORCHESTRATOR_KEY, agentLabel, ciSummary, placeName } from "./format.js";

/** Item kinds, by urgency (rank 1 first). */
export const KINDS = {
  approval: { rank: 1, title: "Needs approval" },
  question: { rank: 2, title: "Question" },
  hanged: { rank: 3, title: "Hanged?" },
  failed: { rank: 4, title: "Failed" },
  exited: { rank: 4, title: "Exited" },
  ci: { rank: 5, title: "CI failing" },
  review: { rank: 5, title: "Changes requested" },
  conflict: { rank: 5, title: "Merge conflict" },
  done: { rank: 6, title: "Your turn" },
  job: { rank: 7, title: "Job failed" },
};

const STATUS_KIND = {
  waiting_for_approval: "approval",
  waiting_for_user: "question",
  possibly_hanged: "hanged",
  failed: "failed",
  completed: "done",
  waiting: "done",
};

/** The queue kind an agent status puts the agent in, or undefined. */
export const kindOfStatus = (status) => STATUS_KIND[status];

const agentKey = (key, agentId) => `${key}/${agentId}`;

/**
 * Every item, most urgent first (rank, then newest).
 * @param {object} s
 * @param {any[]} s.worktrees
 * @param {any[]} s.orchestrator  the orchestrator's agents
 * @param {Record<string, any>} s.prInfo
 * @param {any[]} s.exits   unexpected exits the page saw: {key, agent_id, label, exit_code, at}
 * @param {any[]} s.jobs
 * @param {Record<string, number>} s.since  when each condition was first seen
 *   (ms); updated in place: set on first sight, dropped once it clears
 * @param {Record<string, number>} s.dismissed  occurrence → dismissed at
 * @param {Record<string, number>} s.snoozed  item id → until (ms)
 * @param {number} s.now
 */
export function attentionItems({ worktrees, orchestrator, prInfo, exits, jobs, since, dismissed, snoozed, now }) {
  const items = [];
  const seen = new Set();
  const add = (item) => {
    seen.add(item.id);
    if (!(item.id in since)) since[item.id] = now;
    const at = since[item.id];
    item.since = at;
    item.occurrence = `${item.id}@${at}`;
    item.rank = KINDS[item.kind].rank;
    item.title = KINDS[item.kind].title;
    if (dismissed[item.occurrence]) return;
    if ((snoozed[item.id] || 0) > now) return;
    items.push(item);
  };
  const agents = (key, path, list) => {
    for (const agent of list || []) {
      const kind = STATUS_KIND[agent.status];
      if (!kind) continue;
      add({
        id: `${kind}:${agentKey(key, agent.agent_id)}`,
        kind,
        key,
        path,
        agent,
        where: placeName(key),
        label: agentLabel(agent),
        detail: agent.detail || agent.status,
      });
    }
  };
  agents(ORCHESTRATOR_KEY, null, orchestrator);
  for (const wt of worktrees) {
    agents(wt.path, wt.path, wt.agents);
    const pr = prInfo[wt.path];
    if (!pr || (pr.state || "").toUpperCase() !== "OPEN") continue;
    const base = { key: wt.path, path: wt.path, pr, where: placeName(wt.path), label: `#${pr.number} ${pr.title}` };
    if (ciSummary(pr.checks) === "fail") {
      const failed = pr.checks.filter((c) => (c.conclusion || "").toUpperCase() === "FAILURE").map((c) => c.name);
      add({ ...base, id: `ci:${wt.path}:${pr.number}`, kind: "ci", detail: failed.join(", ") });
    }
    if ((pr.review_decision || "").toUpperCase() === "CHANGES_REQUESTED") {
      add({ ...base, id: `review:${wt.path}:${pr.number}`, kind: "review", detail: "a reviewer asked for changes" });
    }
    if ((pr.mergeable || "").toUpperCase() === "CONFLICTING") {
      add({ ...base, id: `conflict:${wt.path}:${pr.number}`, kind: "conflict", detail: "conflicts with the base branch" });
    }
  }
  for (const exit of exits) {
    add({
      id: `exited:${agentKey(exit.key, exit.agent_id)}:${exit.at}`,
      kind: "exited",
      key: exit.key,
      path: exit.key === ORCHESTRATOR_KEY ? null : exit.key,
      exit,
      where: placeName(exit.key),
      label: exit.label,
      detail: `exited with code ${exit.exit_code ?? "?"}`,
    });
  }
  for (const job of jobs) {
    if (job.exitCode === undefined || job.exitCode === 0) continue;
    add({
      id: `job:${job.id}`,
      kind: "job",
      key: null,
      path: null,
      job,
      where: "job",
      label: job.title,
      detail: job.exitCode === null ? "killed" : `exit ${job.exitCode}`,
    });
  }
  for (const id of Object.keys(since)) if (!seen.has(id)) delete since[id];
  items.sort((a, b) => a.rank - b.rank || b.since - a.since);
  return items;
}

/** The prompt sent to an agent for a failing PR (ui-plan §4d step 5). */
export function ciFailurePrompt(item) {
  const failed = (item.pr.checks || []).filter((c) => (c.conclusion || "").toUpperCase() === "FAILURE");
  const lines = failed.map((c) => `- ${c.name}${c.details_url ? `: ${c.details_url}` : ""}`);
  return `CI is failing on PR #${item.pr.number} (${item.pr.title}). Failed checks:\n${lines.join("\n")}\nPlease look into the failures and fix them.`;
}
