//! The engine's state as the app mirrors it: seeded by `get_sidebar_state`,
//! kept current by events applied in order (the web page's store.js).

use std::collections::BTreeMap;

use lazyagent_proto::EngineEvent;
use lazyagent_proto::types::{Agent, ConfigView, GitStatus, PrInfo, SidebarState, Worktree};

/// The engine's key for the orchestrator (registry.py).
pub const ORCHESTRATOR_KEY: &str = "__orchestrator__";

#[derive(Debug, Default, Clone)]
pub struct Store {
    pub worktrees: Vec<Worktree>,
    pub orchestrator: Vec<Agent>,
    pub pr_info: BTreeMap<String, Option<PrInfo>>,
    pub git: BTreeMap<String, GitStatus>,
    pub config: Option<ConfigView>,
    pub cursor: u64,
    /// Bumped on every change, so views can cache what they derive.
    pub version: u64,
}

impl Store {
    pub fn seed(&mut self, state: SidebarState) {
        self.worktrees = state.worktrees;
        self.orchestrator = state.orchestrator.agents;
        self.pr_info = state.pr_info;
        self.git = state.git_statuses;
        self.cursor = state.cursor;
        self.version += 1;
    }

    pub fn worktree(&self, path: &str) -> Option<&Worktree> {
        self.worktrees.iter().find(|w| w.path == path)
    }

    /// The agents of a worktree, or of the orchestrator.
    pub fn agents(&self, path: &str) -> &[Agent] {
        if path == ORCHESTRATOR_KEY {
            return &self.orchestrator;
        }
        self.worktree(path).map(|w| w.agents.as_slice()).unwrap_or(&[])
    }

    fn agents_mut(&mut self, path: &str) -> Option<&mut Vec<Agent>> {
        if path == ORCHESTRATOR_KEY {
            return Some(&mut self.orchestrator);
        }
        self.worktrees.iter_mut().find(|w| w.path == path).map(|w| &mut w.agents)
    }

    pub fn agent(&self, path: &str, agent_id: &str) -> Option<&Agent> {
        self.agents(path).iter().find(|a| a.agent_id == agent_id)
    }

    pub fn agent_count(&self) -> usize {
        self.orchestrator.len() + self.worktrees.iter().map(|w| w.agents.len()).sum::<usize>()
    }

    /// Applies one event; returns whether anything changed.
    pub fn apply(&mut self, event: &EngineEvent) -> bool {
        let changed = match event {
            EngineEvent::AgentUpdated(u) => match self.agents_mut(&u.worktree_path) {
                Some(agents) => match agents.iter_mut().find(|a| a.agent_id == u.agent_id) {
                    Some(a) => {
                        a.status.clone_from(&u.status);
                        a.confidence.clone_from(&u.confidence);
                        a.detail.clone_from(&u.detail);
                        true
                    }
                    None => false,
                },
                None => false,
            },
            EngineEvent::AgentSpawned(s) if s.kind != "shell" => {
                let agent = Agent {
                    agent_id: s.agent_id.clone(),
                    label: s.label.clone(),
                    session_name: s.session_name.clone(),
                    status: "starting".into(),
                    confidence: "low".into(),
                    detail: String::new(),
                    session_id: Some(s.session_id.clone()),
                    kind: Some(s.kind.clone()),
                    size: None,
                    attached_clients: None,
                };
                match self.agents_mut(&s.worktree_path) {
                    Some(agents) => {
                        match agents.iter_mut().find(|a| a.agent_id == s.agent_id) {
                            Some(a) => a.session_id = Some(s.session_id.clone()),
                            None => agents.push(agent),
                        }
                        true
                    }
                    None => false,
                }
            }
            EngineEvent::AgentExited(e) => match self.agents_mut(&e.worktree_path) {
                Some(agents) => {
                    let before = agents.len();
                    agents.retain(|a| a.agent_id != e.agent_id || a.session_id.as_deref() != Some(&e.session_id));
                    agents.len() != before
                }
                None => false,
            },
            EngineEvent::WorktreesChanged(list) => {
                self.worktrees = list.clone();
                let paths: Vec<&str> = list.iter().map(|w| w.path.as_str()).collect();
                self.pr_info.retain(|p, _| paths.contains(&p.as_str()));
                self.git.retain(|p, _| paths.contains(&p.as_str()));
                true
            }
            EngineEvent::GitStatusUpdated(map) => {
                for (path, status) in map {
                    if let Some(w) = self.worktrees.iter_mut().find(|w| &w.path == path) {
                        w.git_status = Some(lazyagent_proto::types::GitCounts {
                            dirty_count: status.dirty_count,
                            ahead: status.ahead,
                            behind: status.behind,
                        });
                    }
                    self.git.insert(path.clone(), status.clone());
                }
                true
            }
            EngineEvent::PrInfoUpdated(p) => {
                self.pr_info.insert(p.path.clone(), p.pr_info.clone());
                true
            }
            _ => false,
        };
        if changed {
            self.version += 1;
        }
        changed
    }
}

/// The colour role of a status (app.css `.s-*`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Running,
    Waiting,
    Done,
    Bad,
    Idle,
}

pub fn tone(status: &str) -> Tone {
    match status {
        "running" | "starting" => Tone::Running,
        "waiting" | "waiting_for_user" | "waiting_for_approval" => Tone::Waiting,
        "completed" => Tone::Done,
        "failed" | "possibly_hanged" => Tone::Bad,
        _ => Tone::Idle,
    }
}

/// Short status words (format.js STATUS_TEXT); unknown ones shown raw.
pub fn status_text(status: &str) -> &str {
    match status {
        "waiting_for_user" => "input",
        "waiting_for_approval" => "approval",
        "completed" => "done",
        "interrupted" => "stopped",
        "possibly_hanged" => "hanged?",
        other => other,
    }
}

pub fn basename(path: &str) -> &str {
    path.trim_end_matches('/').rsplit('/').next().unwrap_or(path)
}

pub fn agent_label(agent: &Agent) -> String {
    if agent.label.is_empty() { format!("Agent {}", agent.agent_id) } else { agent.label.clone() }
}

/// "fail" / "pending" / "pass" for a PR's checks (format.js ciSummary).
pub fn ci_summary(pr: &PrInfo) -> Option<&'static str> {
    if pr.checks.is_empty() {
        return None;
    }
    let up = |s: &str| s.to_ascii_uppercase();
    if pr.checks.iter().any(|c| up(&c.conclusion) == "FAILURE") {
        return Some("fail");
    }
    if pr.checks.iter().any(|c| up(&c.status) != "COMPLETED" && up(&c.conclusion) != "SUCCESS") {
        return Some("pending");
    }
    Some("pass")
}

#[cfg(test)]
mod tests {
    use super::*;
    use lazyagent_proto::Event;
    use serde_json::json;

    fn ev(name: &str, data: serde_json::Value) -> EngineEvent {
        EngineEvent::parse(Event { seq: 1, event: name.into(), data })
    }

    fn seeded() -> Store {
        let mut s = Store::default();
        s.seed(
            serde_json::from_value(json!({
                "worktrees": [{"path": "/r", "branch": "main", "head": "a", "is_main": true, "is_bare": false,
                               "agent_status": "no_agent", "agents": []}],
                "orchestrator": {"agents": []}, "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 7
            }))
            .unwrap(),
        );
        s
    }

    #[test]
    fn spawn_update_exit() {
        let mut s = seeded();
        assert_eq!(s.cursor, 7);
        s.apply(&ev(
            "agent.spawned",
            json!({"worktree_path": "/r", "agent_id": "a1", "session_id": "s1", "label": "", "session_name": "r-a1", "kind": "agent"}),
        ));
        assert_eq!(agent_label(&s.agents("/r")[0]), "Agent a1");
        s.apply(&ev(
            "agent.updated",
            json!({"worktree_path": "/r", "agent_id": "a1", "status": "waiting_for_user", "confidence": "high", "detail": "?"}),
        ));
        assert_eq!(status_text(&s.agent("/r", "a1").unwrap().status), "input");
        // A shell is not an agent.
        assert!(!s.apply(&ev(
            "agent.spawned",
            json!({"worktree_path": "/r", "agent_id": "", "session_id": "sh", "label": "", "session_name": "", "kind": "shell"})
        )));
        s.apply(&ev(
            "agent.exited",
            json!({"worktree_path": "/r", "agent_id": "a1", "session_id": "s1", "stopped": true}),
        ));
        assert!(s.agents("/r").is_empty());
    }

    #[test]
    fn orchestrator_git_and_pr() {
        let mut s = seeded();
        s.apply(&ev(
            "agent.spawned",
            json!({"worktree_path": ORCHESTRATOR_KEY, "agent_id": "", "session_id": "o", "label": "Orchestrator", "session_name": "o", "kind": "orchestrator"}),
        ));
        assert_eq!(s.orchestrator.len(), 1);
        s.apply(&ev("git_status.updated", json!({"/r": {"dirty_count": 3, "ahead": 1, "behind": 0}})));
        assert_eq!(s.worktrees[0].git_status.unwrap().dirty_count, 3);
        s.apply(&ev(
            "pr_info.updated",
            json!({"path": "/r", "pr_info": {"number": 4, "title": "t", "state": "OPEN",
                    "checks": [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]}}),
        ));
        assert_eq!(ci_summary(s.pr_info["/r"].as_ref().unwrap()), Some("fail"));
        let v = s.version;
        assert!(!s.apply(&ev("future.event", json!({}))));
        assert_eq!(s.version, v);
    }
}
