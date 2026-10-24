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
    /// The PR's head commit, and how many reviewers were asked / reviewed
    /// (newer engines; `None` when not fetched).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub head_sha: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub review_requests: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reviews: Option<u32>,
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

/// A work item (proposal 0013): `status_list` rows and `status.updated`.
/// Open sets (zone, level, ball, priority) stay strings.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusRow {
    pub key: String,
    pub repo: String,
    pub branch: Option<String>,
    pub worktree_path: Option<String>,
    pub title: String,
    pub urls: Vec<String>,
    /// `inbox` · `in_flight` · `parked` (the whole work item, feature `parked`).
    pub zone: String,
    /// `now` · `round` · `deep` (inbox rows only).
    pub level: Option<String>,
    /// `you` · `agent` · `others` · `stalled` · `done`.
    pub ball: String,
    pub priority: String,
    pub state: String,
    pub rule: i64,
    pub blocks_agent: bool,
    pub needs_you: Option<NeedsYou>,
    pub agents: Vec<RowAgent>,
    pub pr: Option<RowPr>,
    pub note: Option<RowNote>,
    /// The user's label, park or close (feature `gestures`).
    pub gesture: Option<Value>,
    /// Older engines (before Pass to orchestrator) do not send it.
    #[serde(default)]
    pub claim: Option<Claim>,
    /// The issue-tracker task the branch links to (optional adapters).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tracker: Option<RowTracker>,
    pub stale: bool,
    pub changed_at: f64,
    pub fingerprint: String,
    /// `quick` · `deep` for inbox rows (feature `gestures`; older engines
    /// do not send it, nor the two below).
    #[serde(default)]
    pub class: Option<String>,
    /// The user's last look at the row (`status_seen`).
    #[serde(default)]
    pub seen_at: Option<f64>,
    /// Its latest timeline entry (`status_timeline`).
    #[serde(default)]
    pub last_event_at: Option<f64>,
    /// `orchestrator` while a routed question waits on it, `user` for inbox
    /// rows, else null (feature `first_responder`; older engines: absent).
    #[serde(default)]
    pub holder: Option<String>,
    /// The orchestrator's own hold (feature `holds`; older engines: absent).
    #[serde(default)]
    pub hold: Option<Hold>,
    /// An inbox item delegated to the branch's agent (feature `delegation`).
    #[serde(default)]
    pub delegation: Option<Delegation>,
    /// A hint for the user, e.g. "close PR #12: its head is in develop".
    #[serde(default)]
    pub suggestion: Option<String>,
    /// The linked feeder work item's project (feature `work_items`; absent
    /// when no feeder item is linked, and on older engines).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project: Option<RowProject>,
    /// Its dependencies (URLs).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub depends_on: Vec<String>,
    /// Of those, the ones still open or in review: "waits on <title>".
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub waits_on: Vec<WaitsOn>,
    /// The feeder's work item (`work_items_upsert`) the branch links to.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feeder: Option<Value>,
    /// The last real activity there (a commit, notes, a report, a step by
    /// someone other than the engine): stalled rows age from it (I4e).
    #[serde(default)]
    pub activity_at: Option<f64>,
    /// The item's newest timeline entry that is not a ball or state change.
    #[serde(default)]
    pub last_step: Option<LastStep>,
}

/// A feeder's project (`work_items_upsert {project}`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RowProject {
    pub id: String,
    pub title: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub policy: Option<Value>,
}

/// An open dependency of a work item.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WaitsOn {
    pub url: String,
    pub title: String,
    /// `open` · `review` (its row has an open PR).
    pub state: String,
}

/// The orchestrator holds a work item itself while it verifies or waits.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Hold {
    pub key: String,
    pub holder: String,
    pub at: f64,
    pub until: f64,
    pub by: String,
    pub note: String,
    /// The engine's hold on the orchestrator's worker's finished work.
    pub auto: bool,
}

/// One inbox item the user handed to the branch's agent.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Delegation {
    pub agent: String,
    pub item: String,
    pub occurrence: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    pub fact: String,
    pub at: f64,
    pub by: String,
    /// `on_it` · `returned`.
    pub phase: String,
    #[serde(default)]
    pub sent_at: Option<f64>,
    #[serde(default)]
    pub ran: bool,
    #[serde(default)]
    pub state: String,
}

/// A row's tracker task: read-only, the last good value when `stale`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RowTracker {
    pub adapter: String,
    pub key: String,
    pub url: String,
    pub status: String,
    /// The mapped text, else the raw status.
    pub state: String,
    pub at: f64,
    pub stale: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NeedsYou {
    pub count: u32,
    /// The top actionable's id.
    pub top: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RowAgent {
    pub id: String,
    pub status: String,
    pub since: f64,
    /// `orchestrator` for its workers (newer engines).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub spawned_by: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RowPr {
    pub number: u64,
    pub state: String,
    pub review: String,
    /// `green` · `red` · `running` · `none`.
    pub ci: String,
    pub mergeable: String,
}

/// The agent's own word on the row: a notes bullet, later a report.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RowNote {
    pub text: String,
    pub by: String,
    pub at: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
}

/// A work item passed to the orchestrator until a time (feature `claims`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Claim {
    pub key: String,
    pub holder: String,
    pub at: f64,
    pub until: f64,
    pub by: String,
    pub allowed: Vec<String>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct StatusCounts {
    pub now: u32,
    pub round: u32,
    pub in_flight: u32,
    pub done: u32,
    pub stale: u32,
    pub not_started: u32,
    /// "To sit down with" rows, and `[inbox] deep_max` (older engines: 0).
    #[serde(default)]
    pub deep: u32,
    #[serde(default)]
    pub deep_max: u32,
    /// The orchestrator's actions today, and how many the user reverted
    /// (feature `first_responder`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auto_actions: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auto_reverted: Option<u32>,
    /// Parked work items, and those whose items still need the user
    /// (feature `parked`).
    #[serde(default)]
    pub parked: u32,
    #[serde(default)]
    pub parked_needs_you: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusList {
    pub rows: Vec<StatusRow>,
    pub counts: StatusCounts,
    pub generated_at: f64,
    /// Whether the user counts as present (feature `first_responder`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub presence: Option<Presence>,
    /// What `counts.not_started` counts (feature `work_items`, when a
    /// feeder has items).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub not_started_items: Vec<Value>,
    /// The orchestrator's report and queue (I4e; older engines: absent).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub orchestrator: Option<OrchestratorStatus>,
}

/// A row's last real step (`status_row.last_step`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LastStep {
    pub at: f64,
    pub kind: String,
    pub actor: String,
    pub text: String,
}

/// `status_list.orchestrator` (I4e): what it says it is doing, and the
/// items it holds or was passed.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct OrchestratorStatus {
    pub alive: bool,
    #[serde(default)]
    pub report: Option<OrchestratorReport>,
    #[serde(default)]
    pub queue: Vec<OrchestratorQueued>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrchestratorReport {
    pub note: String,
    pub at: f64,
}

/// A pass (`pass`) or a hold (`hold`) in force.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrchestratorQueued {
    pub key: String,
    pub kind: String,
    #[serde(default)]
    pub note: Option<String>,
    pub until: f64,
    #[serde(default)]
    pub by: Option<String>,
    #[serde(default)]
    pub auto: bool,
}

/// One of the orchestrator's actions (`orchestrator_actions`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrchestratorAction {
    pub key: String,
    pub at: f64,
    pub kind: String,
    #[serde(default)]
    pub action: Option<String>,
    pub text: String,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub mandate: Option<String>,
    #[serde(default)]
    pub decision_id: Option<String>,
    #[serde(default)]
    pub revertible: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrchestratorActions {
    pub actions: Vec<OrchestratorAction>,
}

/// `mode` is the Home toggle (`auto` · `present` · `away`); `effective`
/// what the engine applies.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Presence {
    #[serde(default)]
    pub mode: Option<String>,
    pub effective: String,
    /// When `effective` last changed.
    #[serde(default)]
    pub since: Option<f64>,
}

/// `presence_report` / `presence_set`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PresenceResult {
    pub presence: Presence,
}

/// A fact on a work item's timeline (`status_timeline`, `status.timeline`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TimelineEntry {
    pub at: f64,
    pub kind: String,
    /// `user`, `agent:<id>`, `orchestrator` or `engine`.
    pub actor: String,
    pub text: String,
    #[serde(default, rename = "ref", skip_serializing_if = "Option::is_none")]
    pub reference: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusTimeline {
    pub key: String,
    pub since: Option<f64>,
    pub seen_at: Option<f64>,
    pub entries: Vec<TimelineEntry>,
}

/// `status.timeline`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TimelineAdded {
    pub key: String,
    pub entry: TimelineEntry,
}

/// `status_seen`: the client's marks, `*` for "all seen".
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusSeen {
    pub client: String,
    pub seen: BTreeMap<String, f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionOption {
    pub key: String,
    pub label: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub consequence: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Brief {
    pub question: String,
    pub options: Vec<DecisionOption>,
    pub recommendation: Option<String>,
    pub tried: String,
    /// Open shapes (diff, log, file, notes, url): the decision view's (I5).
    pub evidence: Vec<Value>,
    pub reversible: Option<String>,
    pub impact_if_wrong: String,
}

/// A decision brief (feature `decisions`); the orchestrator's answer and
/// whether it can be reverted come with `first_responder`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Decision {
    pub id: String,
    pub key: String,
    pub worktree_path: String,
    pub agent: String,
    pub holder: String,
    pub replaces: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub superseded_by: Option<String>,
    pub state: String,
    pub valid: bool,
    pub class: String,
    pub blocking: bool,
    pub brief: Brief,
    pub invalid: Vec<String>,
    pub problems: Vec<String>,
    pub sha: Option<String>,
    pub created_at: f64,
    pub closed_at: Option<f64>,
    /// `{choice, reason?, comment?}` once answered.
    pub answer: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub answered_by: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revertible: Option<bool>,
    /// `question` (a routed plain question) · `auto_delegate` (the
    /// engine's); absent for a brief.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    /// `{at, mandate}`: when it went to the orchestrator first.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub routed: Option<Value>,
    /// `{by, note, suggestion, at}`: handed to the user ("orchestrator
    /// suggests B", "orchestrator did not answer").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub escalation: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capped: Option<String>,
    /// What Revert runs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inverse: Option<Value>,
    /// `{at, choice, comment, results}` once the user reverted it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reverted: Option<Value>,
    /// The last Explain this on it (feature `decision_view`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub explain: Option<DecisionExplain>,
    /// The last Request a new brief on it (feature `decision_view`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rebrief: Option<DecisionExplain>,
}

impl Decision {
    fn answer_field(&self, name: &str) -> Option<&str> {
        self.answer.as_ref()?.get(name)?.as_str()
    }

    pub fn choice(&self) -> Option<&str> {
        self.answer_field("choice")
    }

    pub fn reason(&self) -> Option<&str> {
        self.answer_field("reason")
    }
}

/// An Explain this or Request a new brief, and its answer (feature
/// `decision_view`): who was asked (`route`: owner · restore ·
/// orchestrator), and the reply with who wrote it (`by`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionExplain {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
    pub id: Option<String>,
    pub key: String,
    /// `explain` · `rebrief`.
    pub kind: String,
    pub route: String,
    pub to: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub worktree_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub question: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    /// `asked` · `answered`.
    pub state: String,
    pub asked_at: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sent_at: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub by: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub answered_at: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub truncated: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replaced_by: Option<String>,
}

/// `decision_explain_request` / `decision_rebrief_request` /
/// `decision_explain`; `decision.explain`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionExplainResult {
    pub explain: DecisionExplain,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

/// `decision_evidence`: one piece of a brief's evidence as text.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionEvidence {
    pub id: String,
    pub index: u32,
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// `decision_get`; `decision.created` / `.answered` / `.resolved`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionResult {
    pub decision: Decision,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionsList {
    pub decisions: Vec<Decision>,
}

/// `inbox_config_get` / `inbox_config_set` (feature `first_responder`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InboxConfig {
    #[serde(default)]
    pub repo: Option<String>,
    #[serde(default)]
    pub auto_delegate: Vec<String>,
    #[serde(default)]
    pub auto_delegate_target: Option<String>,
}

/// `status_gesture`: the claim made (none after `clear`) and the row now.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusGestureResult {
    pub key: String,
    pub claim: Option<Claim>,
    /// The row's gesture now (feature `gestures`).
    #[serde(default)]
    pub gesture: Option<Value>,
    /// `keep_for_me` holds (feature `first_responder`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kept: Option<bool>,
    pub row: Option<StatusRow>,
    /// `delegate` to the branch's agent (feature `delegation`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delegation: Option<Delegation>,
    /// The agents a close with `stop_agents` stopped (I4e).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stopped: Option<Vec<String>>,
    /// An undone delegation's line had already reached the agent (I4e).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delegation_sent: Option<bool>,
    /// What `kind: "undo"` took back: park, delegate or pass (I4e).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub undone: Option<String>,
}

/// An inbox item (feature `actionables`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Actionable {
    pub id: String,
    pub occurrence: String,
    /// `approval`, `question`, `ci`, … (an open set).
    pub kind: String,
    pub level: String,
    pub title: String,
    pub label: String,
    pub detail: String,
    pub state: String,
    pub work_item: Option<String>,
    pub worktree_path: Option<String>,
    pub agent_id: Option<String>,
    pub pr: Option<ActionablePr>,
    pub job_id: Option<String>,
    pub exit_code: Option<i64>,
    pub blocks_agent: bool,
    pub priority: String,
    pub since: f64,
    pub park: Option<Value>,
    /// `quick` · `deep` (feature `gestures`; older engines: absent).
    #[serde(default)]
    pub class: Option<String>,
    /// A `decision` item's brief (feature `decisions`).
    #[serde(default)]
    pub decision_id: Option<String>,
    /// A `ci` / `ci_failed` item's red run on the branch's head (feature
    /// `actions`).
    #[serde(default)]
    pub run: Option<CiRun>,
}

/// A GitHub Actions run that went red on a branch's head (feature `actions`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CiRun {
    pub id: i64,
    #[serde(default)]
    pub attempt: i64,
    pub workflow: String,
    #[serde(default)]
    pub head_sha: String,
    #[serde(default)]
    pub branch: String,
    #[serde(default)]
    pub url: String,
    /// `failure` · `timed_out`.
    #[serde(default)]
    pub conclusion: String,
    /// Fetched on demand: None until the engine has read the run's jobs.
    #[serde(default)]
    pub failed_jobs: Option<Vec<CiJob>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CiJob {
    pub name: String,
    #[serde(default)]
    pub step: String,
    #[serde(default)]
    pub url: String,
}

/// `ci_log_summary`: the failed jobs of a work item's red run, with the last
/// lines of each failed log.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CiLogSummary {
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub runs: Vec<CiRun>,
    #[serde(default)]
    pub jobs: Vec<CiLogJob>,
    #[serde(default)]
    pub text: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CiLogJob {
    pub name: String,
    #[serde(default)]
    pub step: String,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub lines: Vec<String>,
    #[serde(default)]
    pub total: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionablePr {
    pub number: u64,
    pub title: String,
    pub url: Option<String>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActionableCounts {
    pub now: u32,
    pub round: u32,
    pub parked: u32,
    #[serde(default)]
    pub deep: u32,
    #[serde(default)]
    pub deep_max: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionablesList {
    pub items: Vec<Actionable>,
    pub counts: ActionableCounts,
    pub generated_at: f64,
}

/// `actionables_get` / `actionables_mark`; `actionable.created` / `.updated`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionableResult {
    pub item: Actionable,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusUpdated {
    pub row: StatusRow,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusRemoved {
    pub key: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionableResolved {
    pub id: String,
    pub occurrence: String,
    /// `cleared` · `dismissed` · `parked`.
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
    /// The restorable list as it is now (an entry became live, was
    /// dismissed, or was shown again), without a restore.
    RestorableChanged(RestorableList),
    NotesUpdated(NotesUpdated),
    NotesRemoved(NotesRemoved),
    ReviewReplied(ReviewReplied),
    StatusUpdated(Box<StatusUpdated>),
    StatusRemoved(StatusRemoved),
    ActionableCreated(Box<ActionableResult>),
    ActionableUpdated(Box<ActionableResult>),
    ActionableResolved(ActionableResolved),
    StatusTimeline(Box<TimelineAdded>),
    DecisionCreated(Box<DecisionResult>),
    DecisionResolved(Box<DecisionResult>),
    DecisionExplain(Box<DecisionExplainResult>),
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
    "status.updated",
    "status.removed",
    "actionable.created",
    "actionable.updated",
    "actionable.resolved",
    "status.timeline",
    "decision.created",
    "decision.resolved",
    "decision.answered",
    "decision.explain",
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
            "status.updated" => de(&event).map(|u| EngineEvent::StatusUpdated(Box::new(u))),
            "status.removed" => de(&event).map(EngineEvent::StatusRemoved),
            "actionable.created" => de(&event).map(|a| EngineEvent::ActionableCreated(Box::new(a))),
            "actionable.updated" => de(&event).map(|a| EngineEvent::ActionableUpdated(Box::new(a))),
            "actionable.resolved" => de(&event).map(EngineEvent::ActionableResolved),
            "status.timeline" => de(&event).map(|t| EngineEvent::StatusTimeline(Box::new(t))),
            "decision.created" => de(&event).map(|d| EngineEvent::DecisionCreated(Box::new(d))),
            // The orchestrator's answer (I4b) carries the same shape.
            "decision.resolved" | "decision.answered" => de(&event).map(|d| EngineEvent::DecisionResolved(Box::new(d))),
            "decision.explain" => de(&event).map(|d| EngineEvent::DecisionExplain(Box::new(d))),
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
