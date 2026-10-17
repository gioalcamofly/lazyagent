// Small pure helpers shared by the components.

export const ORCHESTRATOR_KEY = "__orchestrator__";

export const STATUS_TEXT = {
  running: "running",
  waiting: "waiting",
  waiting_for_user: "input",
  waiting_for_approval: "approval",
  completed: "done",
  failed: "failed",
  interrupted: "stopped",
  possibly_hanged: "hanged?",
};

/** @param {string} path */
export const basename = (path) => path.replace(/\/+$/, "").split("/").pop();

export function agentLabel(agent) {
  return agent.label || `Agent ${agent.agent_id}`;
}

/** "fail" / "pending" / "pass" for a PR's checks, null when there are none. */
export function ciSummary(checks) {
  if (!checks || checks.length === 0) return null;
  const up = (s) => (s || "").toUpperCase();
  if (checks.some((c) => up(c.conclusion) === "FAILURE")) return "fail";
  if (checks.some((c) => up(c.status) !== "COMPLETED" && up(c.conclusion) !== "SUCCESS")) {
    return "pending";
  }
  return "pass";
}

/** Where an agent lives, for messages: the worktree's folder or "orchestrator". */
export function placeName(path) {
  return path === ORCHESTRATOR_KEY || path === null ? "orchestrator" : basename(path);
}
