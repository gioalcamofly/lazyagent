//! Typed shapes of the v1 methods and events the desktop app uses
//! (`src/lazyagent/protocol/schema/v1/`). Unknown fields are ignored and
//! open sets (statuses, confidence, delivery) stay strings (§4c).

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::envelope::Event;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Size {
    pub rows: u16,
    pub cols: u16,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HelloResult {
    pub protocol: u32,
    pub engine_version: String,
    pub capabilities: Vec<String>,
    pub repo_root: String,
    pub engine_id: String,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GitCounts {
    pub dirty_count: u32,
    pub ahead: u32,
    pub behind: u32,
}

/// The full status (`git_status.updated`, `get_sidebar_state.git_statuses`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GitStatus {
    pub dirty_count: u32,
    pub ahead: u32,
    pub behind: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub has_upstream: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_commit_subject: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Agent {
    pub agent_id: String,
    pub label: String,
    pub session_name: String,
    pub status: String,
    pub confidence: String,
    pub detail: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<Size>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attached_clients: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Worktree {
    pub path: String,
    pub branch: Option<String>,
    pub head: String,
    pub is_main: bool,
    pub is_bare: bool,
    pub agent_status: String,
    pub agents: Vec<Agent>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub git_status: Option<GitCounts>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CiCheck {
    pub name: String,
    pub status: String,
    pub conclusion: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub details_url: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PrInfo {
    pub number: i64,
    pub title: String,
    pub state: String,
    pub checks: Vec<CiCheck>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub review_decision: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mergeable: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Orchestrator {
    pub agents: Vec<Agent>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SidebarState {
    pub worktrees: Vec<Worktree>,
    pub orchestrator: Orchestrator,
    pub pr_info: BTreeMap<String, Option<PrInfo>>,
    #[serde(default)]
    pub git_statuses: BTreeMap<String, GitStatus>,
    #[serde(default)]
    pub view: Option<Value>,
    pub cursor: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ListAgentsResult {
    pub worktree_path: String,
    pub agents: Vec<Agent>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubscribeResult {
    pub cursor: u64,
    pub replayed: u64,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttachResult {
    pub stream_id: u32,
    pub session_id: String,
    pub size: Size,
    /// `snapshot` or `tail`.
    pub mode: String,
    pub offset: u64,
    #[serde(default)]
    pub snapshot: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SizeOwner {
    pub session_id: String,
    pub rows: u16,
    pub cols: u16,
    pub owner: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OpenShellResult {
    pub worktree_path: String,
    pub session_id: String,
    pub size: Size,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SpawnResult {
    pub worktree_path: String,
    pub agent_id: String,
    pub status: String,
    pub cursor: u64,
    #[serde(default)]
    pub session_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StopResult {
    pub worktree_path: String,
    pub agent_id: String,
    pub status: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CreateWorktreeResult {
    pub path: String,
    pub branch: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub custom_command: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub warning: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RemoveWorktreeResult {
    pub removed: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SendInputResult {
    pub worktree_path: String,
    pub agent_id: String,
    pub status: String,
    pub status_before: String,
    pub cursor: u64,
    pub delivery: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub warnings: Option<Vec<String>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReadOutputResult {
    pub worktree_path: String,
    pub agent_id: String,
    pub lines: Vec<String>,
    pub total_lines: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConfigView {
    pub agent_provider: String,
    pub default_branch: String,
    pub has_custom_create: bool,
    pub has_custom_remove: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PrInfoResult {
    pub worktree_path: String,
    pub pr_info: Option<PrInfo>,
    pub fetched: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DiffResult {
    pub worktree_path: String,
    pub diff: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RestorableAgent {
    pub id: String,
    pub worktree_path: String,
    pub agent_id: String,
    pub kind: String,
    pub provider: String,
    pub label: String,
    pub session_name: String,
    pub skip_permissions: bool,
    pub conversation_id: Option<String>,
    pub instruction: Option<String>,
    pub started_at: f64,
    pub engine_id: Option<String>,
    pub ended_at: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RestorableList {
    pub agents: Vec<RestorableAgent>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RestoredAgent {
    pub id: String,
    pub worktree_path: String,
    pub agent_id: String,
    pub session_id: String,
    pub label: String,
    pub session_name: String,
    pub kind: String,
    pub resumed: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RestoreFailure {
    pub id: String,
    pub reason: String,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Restored {
    pub restored: Vec<RestoredAgent>,
    pub failed: Vec<RestoreFailure>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DismissResult {
    pub dismissed: Vec<String>,
}

// ---------------------------------------------------------------- events

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentStatusEvent {
    pub seq: u64,
    pub time: f64,
    pub worktree_path: String,
    pub agent_id: String,
    pub label: String,
    pub status: String,
    pub previous_status: String,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentUpdated {
    pub worktree_path: String,
    pub agent_id: String,
    pub status: String,
    pub confidence: String,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentSpawned {
    pub worktree_path: String,
    pub agent_id: String,
    pub session_id: String,
    pub label: String,
    pub session_name: String,
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentExited {
    pub worktree_path: String,
    pub agent_id: String,
    pub session_id: String,
    #[serde(default)]
    pub exit_code: Option<i32>,
    #[serde(default)]
    pub stopped: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PrInfoUpdated {
    pub path: String,
    pub pr_info: Option<PrInfo>,
}

/// `stream.resized` and `stream.reset`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StreamGeometry {
    pub stream_id: u32,
    #[serde(default)]
    pub session_id: Option<String>,
    pub rows: u16,
    pub cols: u16,
    pub offset: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StreamClosed {
    pub stream_id: u32,
    pub session_id: String,
    pub offset: u64,
    #[serde(default)]
    pub exit_code: Option<i32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EngineNotice {
    pub severity: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JobStarted {
    pub job_id: String,
    pub title: String,
    pub command: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JobOutput {
    pub job_id: String,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JobFinished {
    pub job_id: String,
    pub exit_code: Option<i32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EngineShutdown {
    pub reason: String,
}

/// The events the desktop app acts on; everything else is `Other` and
/// ignored by design (§4c: unknown events are tolerated).
#[derive(Debug, Clone, PartialEq)]
pub enum EngineEvent {
    AgentStatus(AgentStatusEvent),
    AgentUpdated(AgentUpdated),
    AgentSpawned(AgentSpawned),
    AgentExited(AgentExited),
    WorktreesChanged(Vec<Worktree>),
    GitStatusUpdated(BTreeMap<String, GitStatus>),
    PrInfoUpdated(PrInfoUpdated),
    StreamResized(StreamGeometry),
    StreamReset(StreamGeometry),
    StreamClosed(StreamClosed),
    EngineNotice(EngineNotice),
    JobStarted(JobStarted),
    JobOutput(JobOutput),
    JobFinished(JobFinished),
    EngineShutdown(EngineShutdown),
    EngineRestored(Restored),
    /// A known name with a payload we could not read, or an unknown event.
    Other(Event),
}

/// Every event name [`EngineEvent::parse`] gives a shape (tests check them
/// against `events.json`).
pub const KNOWN_EVENTS: &[&str] = &[
    "agent.status",
    "agent.updated",
    "agent.spawned",
    "agent.exited",
    "worktrees.changed",
    "git_status.updated",
    "pr_info.updated",
    "stream.resized",
    "stream.reset",
    "stream.closed",
    "engine.notice",
    "job.started",
    "job.output",
    "job.finished",
    "engine.shutdown",
    "engine.restored",
];

impl EngineEvent {
    pub fn parse(event: Event) -> EngineEvent {
        fn de<T: serde::de::DeserializeOwned>(event: &Event) -> Option<T> {
            T::deserialize(&event.data).ok()
        }
        let parsed = match event.event.as_str() {
            "agent.status" => de(&event).map(EngineEvent::AgentStatus),
            "agent.updated" => de(&event).map(EngineEvent::AgentUpdated),
            "agent.spawned" => de(&event).map(EngineEvent::AgentSpawned),
            "agent.exited" => de(&event).map(EngineEvent::AgentExited),
            "worktrees.changed" => de(&event).map(EngineEvent::WorktreesChanged),
            "git_status.updated" => de(&event).map(EngineEvent::GitStatusUpdated),
            "pr_info.updated" => de(&event).map(EngineEvent::PrInfoUpdated),
            "stream.resized" => de(&event).map(EngineEvent::StreamResized),
            "stream.reset" => de(&event).map(EngineEvent::StreamReset),
            "stream.closed" => de(&event).map(EngineEvent::StreamClosed),
            "engine.notice" => de(&event).map(EngineEvent::EngineNotice),
            "job.started" => de(&event).map(EngineEvent::JobStarted),
            "job.output" => de(&event).map(EngineEvent::JobOutput),
            "job.finished" => de(&event).map(EngineEvent::JobFinished),
            "engine.shutdown" => de(&event).map(EngineEvent::EngineShutdown),
            "engine.restored" => de(&event).map(EngineEvent::EngineRestored),
            _ => None,
        };
        parsed.unwrap_or(EngineEvent::Other(event))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn unknown_and_malformed_events_are_other() {
        let e = Event { seq: 1, event: "future.thing".into(), data: json!({}) };
        assert!(matches!(EngineEvent::parse(e), EngineEvent::Other(_)));
        let e = Event { seq: 1, event: "agent.updated".into(), data: json!({"nope": 1}) };
        assert!(matches!(EngineEvent::parse(e), EngineEvent::Other(_)));
    }

    #[test]
    fn open_sets_pass_through() {
        let e = Event {
            seq: 4,
            event: "agent.updated".into(),
            data: json!({"worktree_path": "/r", "agent_id": "a1", "status": "brand_new",
                         "confidence": "cosmic", "detail": "", "future": [1]}),
        };
        let EngineEvent::AgentUpdated(u) = EngineEvent::parse(e) else { panic!() };
        assert_eq!((u.status.as_str(), u.confidence.as_str()), ("brand_new", "cosmic"));
    }

    #[test]
    fn job_finished_by_signal_has_no_code() {
        let e = Event { seq: 1, event: "job.finished".into(), data: json!({"job_id": "j", "exit_code": null}) };
        assert_eq!(
            EngineEvent::parse(e),
            EngineEvent::JobFinished(JobFinished { job_id: "j".into(), exit_code: None })
        );
    }
}
