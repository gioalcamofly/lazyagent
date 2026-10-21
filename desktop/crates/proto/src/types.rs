//! Typed shapes of the v1 methods and events the desktop app uses
//! (`src/lazyagent/protocol/schema/v1/`). Unknown fields are ignored and
//! open sets (statuses, confidence, delivery) stay strings (§4c).

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::client::ClientError;
use crate::envelope::{Event, codes};

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
    /// Whether `shutdown` can stop it (false: it lives inside a TUI). Older
    /// engines do not say.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detached: Option<bool>,
    /// The commit of the lazyagent source it runs (a checkout), if known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_commit: Option<String>,
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
    /// The branch it merges into.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub base_ref: Option<String>,
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
    /// The agent's final session notes, when they were not empty.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
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
    /// With `pr`: the committed part only (what the pull request shows).
    pub diff: String,
    /// With `pr`: staged, unstaged and untracked changes.
    #[serde(default)]
    pub uncommitted: Option<String>,
    /// With `pr`: what `diff` is against (`None` when nothing could be).
    #[serde(default)]
    pub base: Option<DiffBase>,
    #[serde(default)]
    pub base_error: Option<String>,
    #[serde(default)]
    pub fetch_error: Option<String>,
}

/// `get_diff`'s base, with `pr`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DiffBase {
    /// The ref diffed against: `origin/<branch>` when it exists.
    #[serde(rename = "ref")]
    pub ref_: String,
    #[serde(default)]
    pub branch: Option<String>,
    /// `pr`, `default` or `override` (an open set).
    pub source: String,
    pub merge_base: String,
    #[serde(default)]
    pub pr_number: Option<i64>,
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
    /// Its session notes (proposal 0011); absent from older engines.
    #[serde(default)]
    pub notes_id: Option<String>,
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

/// `notes_get`: one agent's session notes (proposal 0011).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Notes {
    pub worktree_path: String,
    pub agent_id: String,
    pub notes_id: String,
    pub rev: u64,
    /// UTF-8 bytes of `text`.
    pub size: u64,
    pub max_bytes: u64,
    /// `user`, `agent`, `orchestrator`; `None` for the untouched template.
    pub by: Option<String>,
    pub updated_at: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub warning: Option<String>,
    /// Markdown; sections are `## <name>` headings.
    pub text: String,
    /// `live`, `restorable`, `orphan`.
    pub state: String,
    pub kind: String,
    /// `manual`, `propose`, `free`.
    pub mode: String,
    pub conversation_id: Option<String>,
    /// The pinned first instruction.
    pub instruction: Option<String>,
    pub created_at: f64,
    pub ended_at: Option<f64>,
    /// Only the template.
    pub empty: bool,
}

/// What a notes write answers (`notes_put`, `notes_append`,
/// `notes_replace_section`, `notes_adopt`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NotesWritten {
    pub worktree_path: String,
    pub agent_id: String,
    pub notes_id: String,
    pub rev: u64,
    pub size: u64,
    pub max_bytes: u64,
    pub by: Option<String>,
    pub updated_at: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub warning: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NotesListItem {
    pub notes_id: String,
    pub state: String,
    pub worktree_path: String,
    pub agent_id: String,
    pub kind: String,
    pub size: u64,
    pub empty: bool,
    pub by: Option<String>,
    pub updated_at: f64,
    pub ended_at: Option<f64>,
    /// When an orphan is deleted.
    pub expires_at: Option<f64>,
}

/// `notes_list`, newest first.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NotesList {
    pub notes: Vec<NotesListItem>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NotesDeleted {
    pub deleted: String,
}

/// The `data` of a `CONFLICT` error: the notes as they are now.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NotesConflict {
    pub rev: u64,
    pub text: String,
}

impl NotesConflict {
    /// The current notes a stale `if_rev` was refused with; `None` for any
    /// other error (or a `CONFLICT` without its data).
    pub fn of(error: &ClientError) -> Option<NotesConflict> {
        match error {
            ClientError::Rpc(e) if e.code == codes::CONFLICT => {
                e.data.as_ref().and_then(|d| NotesConflict::deserialize(d).ok())
            }
            _ => None,
        }
    }
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

/// `notes.updated`: fetch the text with `notes_get` when it is shown.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NotesUpdated {
    pub worktree_path: String,
    pub agent_id: String,
    pub notes_id: String,
    pub rev: u64,
    pub size: u64,
    pub by: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NotesRemoved {
    pub notes_id: String,
    pub worktree_path: String,
    pub agent_id: String,
    /// `deleted`, `orphaned`, `adopted`, `pruned`.
    pub reason: String,
}

/// `review.replied`: an agent answered a review comment (`review_reply`).
/// Also an item of `review_replies`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReviewReplied {
    pub reply_id: String,
    pub comment_id: String,
    pub worktree_path: String,
    pub agent_id: String,
    pub text: String,
    pub at: f64,
}

/// `review_replies`: the kept answers, oldest first.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReviewReplies {
    pub replies: Vec<ReviewReplied>,
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
    /// The restorable list as it is now (an entry became live, was
    /// dismissed, or was shown again), without a restore.
    RestorableChanged(RestorableList),
    NotesUpdated(NotesUpdated),
    NotesRemoved(NotesRemoved),
    ReviewReplied(ReviewReplied),
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
    "restorable.changed",
    "notes.updated",
    "notes.removed",
    "review.replied",
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
            "restorable.changed" => de(&event).map(EngineEvent::RestorableChanged),
            "notes.updated" => de(&event).map(EngineEvent::NotesUpdated),
            "notes.removed" => de(&event).map(EngineEvent::NotesRemoved),
            "review.replied" => de(&event).map(EngineEvent::ReviewReplied),
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
    fn a_conflict_carries_the_current_notes() {
        let err = |code: &str, data| ClientError::Rpc(crate::RpcError { code: code.into(), message: "m".into(), data });
        let c = NotesConflict::of(&err(codes::CONFLICT, Some(json!({"rev": 3, "text": "theirs"})))).unwrap();
        assert_eq!((c.rev, c.text.as_str()), (3, "theirs"));
        assert_eq!(NotesConflict::of(&err(codes::CONFLICT, None)), None);
        assert_eq!(NotesConflict::of(&err(codes::VALIDATION_ERROR, Some(json!({"rev": 3, "text": ""})))), None);
        assert_eq!(NotesConflict::of(&ClientError::Timeout), None);
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
