//! The diff viewer and review (ui-plan §3d / §4c, the web's U5): a base,
//! unified or split rows in one virtualised list of fixed-height rows, a
//! file list that jumps, comments on lines or ranges (kept in the app's
//! state file), "Submit review" to an agent, now or when it is idle, and
//! questions asked now. The agent answers each comment with the
//! `review_reply` MCP tool (an engine with `review_register`), and the
//! answer shows under the code; an older engine's Ask now answer is read
//! back from the agent's output between markers.

use std::sync::Arc;

use eframe::egui::scroll_area::{DragScroll, ScrollSource};
use eframe::egui::text::{LayoutJob, TextWrapping};
use eframe::egui::{
    self, Align, Align2, Color32, FontId, Key, Layout, Modifiers, Pos2, Rect, RichText, Sense, TextEdit, TextFormat,
    Ui, Vec2,
};
use lazyagent_proto::types::{DiffBase, DiffResult, ReviewReplied, ReviewReplies, SpawnResult};
use serde_json::json;

use crate::app::{App, Severity, State};
use crate::diff::{self, Comment, Diff, Pair, RowKind};
use crate::store::{self, ORCHESTRATOR_KEY};

pub const ROW_H: f32 = 18.0;
pub(crate) const MAX_BYTES: u64 = 4 * 1024 * 1024;
const NUM_W: f32 = 44.0;
const HBAR_H: f32 = 10.0;

#[derive(Debug, Clone, PartialEq)]
pub enum DRow {
    File(usize),
    Row(usize, usize),
    Pair(usize, Pair),
    /// A wrapped line's `k`-th piece after its first (`Row` draws the
    /// first): no number, the line's colour.
    More(usize, usize, usize),
    /// A split pair's `k`-th piece row: each side its piece, or only its
    /// colour once its line has ended (the row is as tall as its taller side).
    PairMore(usize, Pair, usize),
    /// A thread's first line; `Reply` its next ones (fixed-height rows).
    Comment(usize, String),
    Reply(usize, String, usize),
    /// The heading over the uncommitted files.
    Uncommitted,
}

/// Selected lines: one file, `from`..=`to` in its rows (either order); in
/// split, one side (`Some(true)`: the old, left one).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sel {
    pub file: usize,
    pub side: Option<bool>,
    pub from: usize,
    pub to: usize,
}

impl Sel {
    fn has(&self, fi: usize, ri: usize, side: Option<bool>) -> bool {
        let (a, b) = (self.from.min(self.to), self.from.max(self.to));
        self.file == fi && self.side == side && (a..=b).contains(&ri)
    }
}

/// The comment on a selection: its side's lines only, in split.
pub fn selected_comment(d: &Diff, sel: &Sel) -> Option<Comment> {
    let f = d.files.get(sel.file)?;
    let rows = f.rows.get(sel.from.min(sel.to)..=sel.from.max(sel.to))?;
    diff::anchor_range(
        &f.path,
        rows.iter().filter(|r| match sel.side {
            Some(true) => r.kind != RowKind::Add,
            Some(false) => r.kind != RowKind::Del,
            None => true,
        }),
    )
    .map(|c| Comment { uncommitted: f.uncommitted, ..c })
}

fn code_row(row: &diff::Row) -> bool {
    matches!(row.kind, RowKind::Ctx | RowKind::Add | RowKind::Del)
}

pub struct DiffState {
    pub path: String,
    /// The Base field: the resolved base once loaded, or what was typed.
    pub base: String,
    /// The base asked for; empty lets the engine pick (the PR's base).
    pub requested: String,
    /// What the diff is against, once loaded.
    pub resolved: Option<DiffBase>,
    /// Why there is no base, or why the fetch failed.
    pub note: Option<String>,
    pub loading: bool,
    pub error: Option<String>,
    pub diff: Arc<Diff>,
    pub rows: Vec<DRow>,
    /// Rebuild `rows` when the layout, the comments, what is shown or the
    /// wrap columns change.
    built_for: Option<(bool, u64, bool, Option<usize>)>,
    /// The first row on screen last frame (next / previous file, and the
    /// line kept on top when the rows wrap again).
    pub top: usize,
    /// Which load a reply belongs to.
    seq: u64,
    pub scroll_to: Option<usize>,
    /// How far the code is scrolled sideways (px); line numbers stay put.
    pub hoff: f32,
    /// The longest code line, in columns (tabs as four).
    cols: usize,
    /// Where the rows were drawn last frame (tests, hit-testing).
    pub rows_rect: Rect,
    pub sel: Option<Sel>,
    dragging: bool,
    /// The drag began on the line numbers (or with shift): its release
    /// opens the popup even on one line.
    drag_pops: bool,
}

impl DiffState {
    /// What the engine said the diff is against.
    pub fn applied(&mut self, base: Option<DiffBase>, base_error: Option<String>, fetch_error: Option<String>) {
        if self.requested.is_empty() {
            self.base = base.as_ref().map(|b| b.ref_.clone()).unwrap_or_default();
        }
        self.note = match (fetch_error, base_error) {
            (Some(f), _) => Some(format!("Fetch failed: {f}")),
            (None, Some(b)) if base.is_none() => Some(format!("Only uncommitted changes: {b}")),
            _ => None,
        };
        self.resolved = base;
    }

    /// The base the Base field asks for: empty (let the engine pick)
    /// when it is cleared, or still shows the base the engine picked.
    pub fn base_request(&self) -> String {
        let field = self.base.trim();
        let picked = self.requested.is_empty() && self.resolved.as_ref().is_some_and(|b| b.ref_ == field);
        if picked { String::new() } else { field.to_string() }
    }

    /// A diff already parsed (tests, benches).
    #[cfg(test)]
    pub fn loaded(path: &str, base: &str, diff: Diff) -> DiffState {
        DiffState {
            path: path.into(),
            base: base.into(),
            requested: base.into(),
            resolved: None,
            note: None,
            loading: false,
            error: None,
            diff: Arc::new(diff),
            rows: vec![],
            built_for: None,
            top: 0,
            seq: 1,
            scroll_to: None,
            hoff: 0.0,
            cols: 0,
            rows_rect: Rect::NOTHING,
            sel: None,
            dragging: false,
            drag_pops: false,
        }
    }
}

/// A thread opened from its rows: edit, delete, ask, follow up.
pub struct CommentEdit {
    pub worktree: String,
    pub comment: Comment,
    pub existing: bool,
    pub follow: String,
}

/// The popup on selected lines: a comment for the review, or a question now.
pub struct AskDraft {
    pub worktree: String,
    pub comment: Comment,
    pub pos: Pos2,
    focus: bool,
}

/// A question sent to an agent. Its answer comes as `review.replied`
/// (`by_reply`), or is read back from the agent's output between `marker`
/// lines once the agent has run and is idle.
pub struct PendingAsk {
    pub worktree: String,
    pub agent_id: String,
    /// The comment's id.
    pub id: String,
    pub marker: String,
    pub by_reply: bool,
    pub sent: bool,
    pub ran: bool,
    pub reading: bool,
}

/// Review comments sent to an agent: their answers (`review.replied`) are
/// awaited until its turn ends.
pub struct Awaiting {
    pub worktree: String,
    pub agent_id: String,
    /// The comments' ids.
    pub ids: Vec<String>,
    pub ran: bool,
}

pub struct ReviewForm {
    pub worktree: String,
    /// `None`: start a new agent for it (the worktree has none).
    pub agent_id: Option<String>,
    pub summary: String,
    pub show_prompt: bool,
    /// The comments whose code is shown in full.
    pub expanded: Vec<String>,
}

/// A review waiting for its agent to finish its turn.
pub struct PendingReview {
    pub worktree: String,
    pub agent_id: String,
    pub text: String,
    /// The comments it carries, marked sent once it is delivered.
    pub ids: Vec<String>,
}

fn idle(status: &str) -> bool {
    matches!(status, "waiting" | "waiting_for_user" | "completed")
}

impl State {
    pub fn comments(&self, worktree: &str) -> Vec<Comment> {
        self.saved.comments.get(worktree).and_then(|v| serde_json::from_value(v.clone()).ok()).unwrap_or_default()
    }

    pub fn set_comments(&mut self, worktree: &str, comments: &[Comment]) {
        if comments.is_empty() {
            self.saved.comments.remove(worktree);
        } else {
            self.saved.comments.insert(worktree.to_string(), json!(comments));
        }
        self.comments_gen += 1;
        self.touch_saved();
    }

    /// Opens (or closes) the selected worktree's diff.
    pub fn toggle_diff(&mut self) {
        let Some(path) = self.selected.clone().filter(|p| p != ORCHESTRATOR_KEY) else { return };
        if self.diff_open {
            self.diff_open = false;
            self.focus_request = Some(crate::app::Focus::Agent);
            return;
        }
        self.diff_open = true;
        if self.diff.as_ref().is_none_or(|d| d.path != path) {
            // The engine picks the base: the PR's, as GitHub shows it.
            self.load_diff(&path, String::new());
        }
    }

    /// Wraps the diff's long lines, or keeps each on one row (Alt+Z).
    pub fn toggle_wrap(&mut self) {
        self.saved.diff_nowrap = !self.saved.diff_nowrap;
        self.touch_saved();
    }

    /// Hides the diff's file list (a rail stays), or shows it again (f).
    pub fn toggle_diff_files(&mut self) {
        self.saved.diff_files_hidden = !self.saved.diff_files_hidden;
        self.touch_saved();
    }

    /// Scrolls the diff to the next (`delta` > 0) or previous file (] / [).
    pub fn step_file(&mut self, delta: i32) {
        if let Some(d) = self.diff.as_mut()
            && let Some(j) = file_step(&d.rows, d.top, delta)
        {
            d.scroll_to = Some(j);
        }
    }

    pub fn load_diff(&mut self, path: &str, base: String) {
        self.load_diff_with(path, base, false);
    }

    /// Diffs `path` like its pull request, against `base` (empty: the one
    /// the engine resolves); `fetch` updates the base from origin first.
    pub fn load_diff_with(&mut self, path: &str, base: String, fetch: bool) {
        let seq = self.diff.as_ref().map_or(1, |d| d.seq + 1);
        let base = base.trim().to_string();
        // A reload keeps showing the base until the new one is known.
        let shown = match self.diff.as_ref() {
            Some(d) if base.is_empty() && d.path == path && d.requested.is_empty() => d.base.clone(),
            _ => base.clone(),
        };
        self.diff = Some(DiffState {
            path: path.to_string(),
            base: shown,
            requested: base.clone(),
            resolved: None,
            note: None,
            loading: true,
            error: None,
            diff: Arc::new(Diff::default()),
            rows: vec![],
            built_for: None,
            top: 0,
            seq,
            scroll_to: None,
            hoff: 0.0,
            cols: 0,
            rows_rect: Rect::NOTHING,
            sel: None,
            dragging: false,
            drag_pops: false,
        });
        let mut params = json!({"worktree_path": path, "max_bytes": MAX_BYTES, "pr": true});
        if !base.is_empty() {
            params["base"] = json!(base);
        }
        if fetch {
            params["fetch"] = json!(true);
        }
        let conn = self.conn.clone();
        self.conn.request("get_diff", params, move |s, reply| {
            let r = match reply.and_then(|v| {
                serde_json::from_value::<DiffResult>(v).map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => r,
                Err(e) => {
                    if let Some(d) = s.diff.as_mut().filter(|d| d.seq == seq) {
                        d.loading = false;
                        d.error = Some(e.to_string());
                    }
                    return;
                }
            };
            // Parsed off the UI thread (2-5 ms for 5,000 lines on the web).
            let conn2 = conn.clone();
            std::thread::spawn(move || {
                let parsed = Arc::new(diff::parse_pr(&r.diff, r.uncommitted.as_deref().unwrap_or("")));
                conn2.send(crate::conn::Msg::Apply(Box::new(move |s: &mut State| {
                    if let Some(d) = s.diff.as_mut().filter(|d| d.seq == seq) {
                        d.loading = false;
                        d.diff = parsed;
                        d.built_for = None;
                        d.applied(r.base, r.base_error, r.fetch_error);
                    }
                })));
            });
        });
    }

    /// Sends the reviews whose agent became idle.
    pub fn pending_reviews(&mut self) {
        let mut i = 0;
        while i < self.pending_reviews.len() {
            let r = &self.pending_reviews[i];
            match self.store.agent(&r.worktree, &r.agent_id).map(|a| a.status.clone()) {
                Some(status) if idle(&status) => {
                    let r = self.pending_reviews.remove(i);
                    self.send_review(r);
                }
                Some(_) => i += 1,
                None => {
                    let r = self.pending_reviews.remove(i);
                    self.toast(
                        Severity::Warning,
                        format!("The review for agent {} was not sent: it is gone.", r.agent_id),
                    );
                }
            }
        }
    }

    pub fn send_review(&mut self, r: PendingReview) {
        let to = self.store.agent(&r.worktree, &r.agent_id).map_or(r.agent_id.clone(), store::agent_label);
        let cids = self.cids(&r.worktree, &r.ids);
        let (worktree, agent_id) = (r.worktree.clone(), r.agent_id.clone());
        self.registered(&worktree, &agent_id, cids, move |s| {
            let params = json!({"worktree_path": r.worktree, "agent_id": r.agent_id, "text": r.text});
            s.conn.request("send_agent_input", params, move |s, reply| match reply {
                Ok(v) => {
                    // Confirmed or busy: it has run (or runs) since.
                    let ran = matches!(v.get("delivery").and_then(|d| d.as_str()), Some("confirmed" | "busy"));
                    s.toast(Severity::Info, format!("Review: {}", crate::attention_view::delivery_text(v)));
                    s.mark_sent(&r.worktree, &r.ids, &to);
                    s.await_replies(&r.worktree, &r.agent_id, &r.ids, ran);
                }
                Err(e) => s.toast(Severity::Error, format!("The review was not sent: {e}")),
            });
        });
    }

    /// Starts a new agent in `worktree` with the review as its first prompt.
    pub fn review_new_agent(&mut self, worktree: &str, text: String, ids: Vec<String>) {
        let worktree = worktree.to_string();
        let cids = self.cids(&worktree, &ids);
        self.start_agent(&worktree.clone(), text, cids, move |s, started| match started {
            Ok(agent_id) => {
                s.toast(Severity::Info, format!("Started agent {agent_id} with the review."));
                s.mark_sent(&worktree, &ids, &agent_id);
                s.await_replies(&worktree, &agent_id, &ids, false);
            }
            Err(e) => s.toast(Severity::Error, format!("The review was not sent: {e}")),
        });
    }

    /// Whether the engine takes answers through `review_reply`.
    pub fn replies_supported(&self) -> bool {
        self.capabilities.as_ref().is_some_and(|c| c.iter().any(|m| m == "review_register"))
    }

    /// Gives the comments of `worktree` that have none an id for the agent
    /// (`Comment::cid`), when the engine takes replies.
    pub fn give_cids(&mut self, worktree: &str) {
        if !self.replies_supported() {
            return;
        }
        let mut all = self.comments(worktree);
        let mut changed = false;
        for c in all.iter_mut().filter(|c| c.cid.is_empty()) {
            c.cid = format!("c{}", marker_id());
            changed = true;
        }
        if changed {
            self.set_comments(worktree, &all);
        }
    }

    /// The agent ids (`cid`) of the comments `ids`, those that have one.
    fn cids(&self, worktree: &str, ids: &[String]) -> Vec<String> {
        self.comments(worktree)
            .into_iter()
            .filter(|c| ids.contains(&c.id()) && !c.cid.is_empty())
            .map(|c| c.cid)
            .collect()
    }

    /// Registers `cids` for the agent (`review_register`), then runs `then`
    /// whatever the outcome: without it the answers stay in the terminal.
    fn registered(
        &mut self,
        worktree: &str,
        agent_id: &str,
        cids: Vec<String>,
        then: impl FnOnce(&mut State) + Send + 'static,
    ) {
        if cids.is_empty() {
            then(self);
            return;
        }
        let params = json!({"worktree_path": worktree, "agent_id": agent_id, "comment_ids": cids});
        self.conn.request("review_register", params, move |s, reply| {
            if let Err(e) = reply {
                s.toast(Severity::Warning, format!("Its answers will show in the agent's terminal only: {e}"));
            }
            then(s);
        });
    }

    /// Awaits the answers to the sent comments `ids` until the agent's turn
    /// ends; `ran`: it is known to have started on them.
    fn await_replies(&mut self, worktree: &str, agent_id: &str, ids: &[String], ran: bool) {
        let ids: Vec<String> = self
            .comments(worktree)
            .into_iter()
            .filter(|c| ids.contains(&c.id()) && !c.cid.is_empty())
            // Not those answered already (an agent started with them).
            .filter(|c| !c.replies.iter().any(|r| r.id.is_some()))
            .map(|c| c.id())
            .collect();
        if ids.is_empty() {
            return;
        }
        self.awaiting.push(Awaiting { worktree: worktree.to_string(), agent_id: agent_id.to_string(), ids, ran });
        self.comments_gen += 1;
    }

    /// Puts an agent's `review_reply` answer under its comment (once, by
    /// its reply id); `tell`: with a toast (not for answers caught up on).
    pub fn on_review_replied(&mut self, r: &ReviewReplied, tell: bool) {
        let mut all = self.comments(&r.worktree_path);
        let Some(c) = all.iter_mut().find(|c| c.cid == r.comment_id) else { return };
        if c.replies.iter().any(|x| x.id.as_deref() == Some(r.reply_id.as_str())) {
            return;
        }
        c.replies.push(diff::Reply { who: "agent".into(), text: r.text.clone(), id: Some(r.reply_id.clone()) });
        let (id, place) = (c.id(), format!("{}:{}", c.path, c.place()));
        self.set_comments(&r.worktree_path, &all);
        self.asks.retain(|a| !(a.worktree == r.worktree_path && a.id == id));
        for w in self.awaiting.iter_mut().filter(|w| w.worktree == r.worktree_path) {
            w.ids.retain(|x| *x != id);
        }
        self.awaiting.retain(|w| !w.ids.is_empty());
        if tell {
            self.toast(Severity::Info, format!("The agent answered on {place}"));
        }
    }

    /// The answers given while the app was away (the engine keeps them a
    /// few days).
    pub fn fetch_review_replies(&mut self) {
        if !self.replies_supported() {
            return;
        }
        self.conn.request("review_replies", json!({}), |s, reply| {
            let Some(kept) = reply.ok().and_then(|v| serde_json::from_value::<ReviewReplies>(v).ok()) else { return };
            for r in &kept.replies {
                s.on_review_replied(r, false);
            }
        });
    }

    /// Adds a note to the threads `ids`.
    fn note(&mut self, worktree: &str, ids: &[String], text: &str) {
        let mut all = self.comments(worktree);
        for c in all.iter_mut().filter(|c| ids.contains(&c.id())) {
            c.replies.push(diff::Reply::new("note", text));
        }
        self.set_comments(worktree, &all);
    }

    /// What a new agent started from the diff view is: the configured
    /// provider, permissions skipped (no one is there to approve them).
    pub fn new_agent_text(&self) -> String {
        let provider = self.store.config.as_ref().map_or("default", |c| c.agent_provider.as_str());
        format!("a new {provider} agent (skip permissions)")
    }

    /// Spawns an agent in `worktree` whose first prompt is `text`; `then`
    /// gets its id, or why it did not start. One at a time per worktree.
    fn start_agent(
        &mut self,
        worktree: &str,
        text: String,
        cids: Vec<String>,
        then: impl FnOnce(&mut State, Result<String, String>) + Send + 'static,
    ) {
        if self.starting.iter().any(|w| w == worktree) {
            then(self, Err("an agent is already starting in this worktree; try again once it is up".into()));
            return;
        }
        self.starting.push(worktree.to_string());
        let mut params = json!({"worktree_path": worktree, "instruction": text, "skip_permissions": true,
                                "env": self.env_json()});
        if !cids.is_empty() {
            // Registered before it starts: it may answer at once.
            params["review_comment_ids"] = json!(cids);
        }
        let worktree = worktree.to_string();
        self.conn.request("spawn_agent", params, move |s, reply| {
            s.starting.retain(|w| *w != worktree);
            let started = reply
                .map_err(|e| e.to_string())
                .and_then(|v| serde_json::from_value::<SpawnResult>(v).map_err(|e| e.to_string()));
            match started {
                Ok(r) => {
                    // Its tab, without leaving the diff.
                    s.tabs.insert(worktree, r.agent_id.clone());
                    then(s, Ok(r.agent_id));
                }
                Err(e) => then(s, Err(e)),
            }
        });
    }

    /// Marks the comments `ids` as sent to `to`: they stay, as threads.
    pub fn mark_sent(&mut self, worktree: &str, ids: &[String], to: &str) {
        let mut all = self.comments(worktree);
        for c in all.iter_mut().filter(|c| ids.contains(&c.id()) && !c.sent) {
            c.sent = true;
            let text = if c.cid.is_empty() {
                format!("Sent with the review to {to}; its answer is in the agent's terminal.")
            } else {
                format!("Sent with the review to {to}.")
            };
            // Before an answer that came first (an agent started with it).
            let at = c.replies.iter().position(|r| r.id.is_some()).unwrap_or(c.replies.len());
            c.replies.insert(at, diff::Reply::new("note", text));
        }
        self.set_comments(worktree, &all);
    }

    /// The comments that go with Submit review: not asked, not sent, not
    /// in a review waiting for its agent.
    pub fn review_comments(&self, worktree: &str) -> Vec<Comment> {
        let queued: Vec<&String> =
            self.pending_reviews.iter().filter(|r| r.worktree == worktree).flat_map(|r| &r.ids).collect();
        self.comments(worktree).into_iter().filter(|c| !c.out() && !queued.contains(&&c.id())).collect()
    }

    /// Adds `c`, or replaces the comment with its id.
    pub fn upsert_comment(&mut self, worktree: &str, c: Comment) {
        let mut all = self.comments(worktree);
        match all.iter_mut().find(|x| x.id() == c.id()) {
            Some(x) => *x = c,
            None => all.push(c),
        }
        self.set_comments(worktree, &all);
    }

    /// The ids of the threads waiting for an answer.
    pub fn asking(&self, worktree: &str) -> Vec<String> {
        let asks = self.asks.iter().filter(|a| a.worktree == worktree).map(|a| a.id.clone());
        let sent = self.awaiting.iter().filter(|w| w.worktree == worktree).flat_map(|w| w.ids.iter().cloned());
        asks.chain(sent).collect()
    }

    /// Sends the thread `id` (its comment, or its last follow-up) to the
    /// worktree's agent (an idle one first) as a question, now.
    pub fn ask_agent(&mut self, worktree: &str, id: &str) {
        self.give_cids(worktree);
        let Some(c) = self.comments(worktree).into_iter().find(|c| c.id() == id) else { return };
        let cids: Vec<String> = (!c.cid.is_empty()).then(|| c.cid.clone()).into_iter().collect();
        let agents = self.store.agents(worktree);
        let pick = agents.iter().find(|a| idle(&a.status)).or(agents.first()).map(|a| a.agent_id.clone());
        let marker = marker_id();
        let base = self.diff.as_ref().filter(|d| d.path == worktree).map(|d| d.base.clone()).unwrap_or_default();
        let text = diff::ask_prompt(&c, &base, &marker);
        self.asks.retain(|a| !(a.worktree == worktree && a.id == id));
        self.asks.push(PendingAsk {
            worktree: worktree.to_string(),
            agent_id: pick.clone().unwrap_or_default(),
            id: id.to_string(),
            marker,
            by_reply: !cids.is_empty(),
            sent: false,
            ran: false,
            reading: false,
        });
        self.comments_gen += 1;
        let (worktree, id) = (worktree.to_string(), id.to_string());
        let Some(agent_id) = pick else {
            // No agent here: a new one, with the question as its first prompt.
            self.start_agent(&worktree.clone(), text, cids, move |s, started| {
                let Some(i) = s.asks.iter().position(|a| a.worktree == worktree && a.id == id) else { return };
                match started {
                    Ok(agent_id) => {
                        s.toast(Severity::Info, format!("Started agent {agent_id} with the question."));
                        s.asks[i].agent_id = agent_id;
                        s.asks[i].sent = true;
                    }
                    Err(e) => {
                        s.asks.remove(i);
                        s.comments_gen += 1;
                        s.toast(Severity::Error, format!("The question was not sent: {e}"));
                    }
                }
            });
            return;
        };
        self.registered(&worktree.clone(), &agent_id.clone(), cids, move |s| {
            let params = json!({"worktree_path": worktree, "agent_id": agent_id, "text": text});
            s.conn.request("send_agent_input", params, move |s, reply| {
                let Some(i) = s.asks.iter().position(|a| a.worktree == worktree && a.id == id) else { return };
                match reply {
                    Ok(v) => {
                        // Confirmed or busy: it has run (or runs) since.
                        let delivery = v.get("delivery").and_then(|d| d.as_str()).unwrap_or("");
                        s.asks[i].sent = true;
                        s.asks[i].ran = matches!(delivery, "confirmed" | "busy");
                        s.toast(Severity::Info, format!("Question: {}", crate::attention_view::delivery_text(v)));
                    }
                    Err(e) => {
                        s.asks.remove(i);
                        s.comments_gen += 1;
                        s.toast(Severity::Error, format!("The question was not sent: {e}"));
                    }
                }
            });
        });
    }

    /// Reads the answers of the agents that ran and are idle again; says
    /// so under the comments whose `review_reply` answer did not come.
    pub fn pending_asks(&mut self) {
        let mut i = 0;
        while i < self.awaiting.len() {
            let w = &self.awaiting[i];
            match self.store.agent(&w.worktree, &w.agent_id).map(|x| x.status.clone()) {
                None => {
                    let w = self.awaiting.remove(i);
                    self.note(&w.worktree, &w.ids, "The agent is gone; it did not answer here.");
                    continue;
                }
                Some(status) if !idle(&status) => self.awaiting[i].ran = true,
                Some(_) if w.ran => {
                    let w = self.awaiting.remove(i);
                    self.note(&w.worktree, &w.ids, NO_REPLY);
                    continue;
                }
                Some(_) => {}
            }
            i += 1;
        }
        let mut i = 0;
        while i < self.asks.len() {
            let a = &self.asks[i];
            if !a.sent || a.reading {
                i += 1;
                continue;
            }
            match self.store.agent(&a.worktree, &a.agent_id).map(|x| x.status.clone()) {
                None => {
                    let a = self.asks.remove(i);
                    self.answer(&a, Err("The agent is gone; it did not answer."));
                    continue;
                }
                Some(status) if !idle(&status) => self.asks[i].ran = true,
                Some(_) if a.ran && a.by_reply => {
                    let a = self.asks.remove(i);
                    self.note(&a.worktree, std::slice::from_ref(&a.id), NO_REPLY);
                    continue;
                }
                Some(_) if a.ran => {
                    let params = json!({"worktree_path": a.worktree, "agent_id": a.agent_id, "lines": ANSWER_LINES});
                    let (worktree, id) = (a.worktree.clone(), a.id.clone());
                    self.asks[i].reading = true;
                    self.conn.request("read_agent_output", params, move |s, reply| {
                        let Some(i) = s.asks.iter().position(|a| a.worktree == worktree && a.id == id) else { return };
                        let a = s.asks.remove(i);
                        let lines: Vec<String> = reply
                            .ok()
                            .and_then(|v| serde_json::from_value(v.get("lines")?.clone()).ok())
                            .unwrap_or_default();
                        s.answer(&a, Ok(&lines));
                    });
                }
                Some(_) => {}
            }
            i += 1;
        }
    }

    /// Puts what the agent printed between the question's markers under it
    /// (or says where the answer is).
    pub fn answer(&mut self, a: &PendingAsk, output: Result<&[String], &str>) {
        let reply = match output.map(|lines| diff::parse_answer(lines, &a.marker)) {
            Ok(Some(text)) => diff::Reply::new("agent", text),
            Ok(None) => diff::Reply::new("note", "Answered in the agent's terminal."),
            Err(why) => diff::Reply::new("note", why),
        };
        let mut all = self.comments(&a.worktree);
        if let Some(c) = all.iter_mut().find(|c| c.id() == a.id) {
            if reply.who == "agent" {
                self.toast(Severity::Info, format!("The agent answered on {}:{}", c.path, c.place()));
            }
            c.replies.push(reply);
        }
        self.set_comments(&a.worktree, &all);
    }
}

/// Under a comment whose `review_reply` answer did not come in its turn.
const NO_REPLY: &str = "The agent's turn ended without an answer here; see its terminal.";

/// How much of an agent's output is searched for an answer.
const ANSWER_LINES: usize = 2000;

/// A short id for an answer's markers.
fn marker_id() -> String {
    use std::sync::atomic::{AtomicU32, Ordering};
    static N: AtomicU32 = AtomicU32::new(0);
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.subsec_nanos());
    format!("{:x}", (nanos ^ N.fetch_add(1, Ordering::Relaxed).wrapping_mul(0x9e37_79b9)) & 0xff_ffff)
}

/// A thread as lines: the comment, each reply (a few lines each), and a
/// line while its answer is awaited. (who, text): "you", "agent", "note".
pub fn thread_lines(c: &Comment, asking: bool) -> Vec<(&'static str, String)> {
    const MAX: usize = 6;
    let mut out = vec![];
    let mut push = |who: &'static str, label: &str, text: &str| {
        let lines: Vec<&str> = text.trim().lines().collect();
        let pad = " ".repeat(label.chars().count() + 1);
        for (k, l) in lines.iter().take(MAX).enumerate() {
            out.push((who, if k == 0 { format!("{label} {l}") } else { format!("{pad}{l}") }));
        }
        if lines.len() > MAX {
            out.push((who, format!("{pad}… (click to read it all)")));
        }
    };
    push(
        "you",
        if c.asked {
            "?"
        } else if c.sent {
            "sent ▸"
        } else {
            "▸"
        },
        &c.text,
    );
    for r in &c.replies {
        match r.who.as_str() {
            "agent" => push("agent", "agent:", &r.text),
            "you" => push("you", "you:", &r.text),
            _ => push("note", "·", &r.text),
        }
    }
    if asking {
        out.push(("note", "  ⋯ waiting for the agent's answer".into()));
    }
    out
}

fn row_colors(kind: RowKind) -> (Color32, Color32) {
    match kind {
        RowKind::Add => (Color32::from_rgba_unmultiplied(78, 191, 113, 38), Color32::from_rgb(78, 191, 113)),
        RowKind::Del => (Color32::from_rgba_unmultiplied(229, 83, 75, 38), Color32::from_rgb(229, 83, 75)),
        _ => (Color32::TRANSPARENT, Color32::TRANSPARENT),
    }
}

impl App {
    pub(crate) fn diff_view(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let split = self.state.saved.diff_split;
        let Some(path) = self.state.diff.as_ref().map(|d| d.path.clone()) else { return };
        let comments = self.state.comments(&path);
        let asking = self.state.asking(&path);
        let to_review = self.state.review_comments(&path).len();
        let sent = comments.iter().filter(|c| c.sent).count();
        // Toolbar.
        let mut reload = None;
        ui.horizontal(|ui| {
            ui.label(RichText::new("Base").color(t.muted));
            let d = self.state.diff.as_mut().unwrap();
            let edit = ui.add(TextEdit::singleline(&mut d.base).desired_width(220.0).hint_text("PR base"));
            let edit = edit.on_hover_text("Any ref; empty: the pull request's base, else the default branch");
            if edit.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter)) {
                reload = Some((d.base_request(), false));
            }
            if let Some(b) = &d.resolved {
                ui.label(RichText::new(format!("({})", base_label(b))).color(t.muted))
                    .on_hover_text(format!("from merge-base {}", short(&b.merge_base)));
            }
            if !d.requested.is_empty()
                && ui.small_button("PR base").on_hover_text("Back to the base the engine picks").clicked()
            {
                reload = Some((String::new(), false));
            }
            if ui.button("↻ Fetch").on_hover_text("Fetch the base from origin and diff again").clicked() {
                reload = Some((d.base_request(), true));
            }
            let mut show = !self.state.saved.diff_hide_uncommitted;
            if ui.checkbox(&mut show, "Uncommitted").on_hover_text("Staged, unstaged and untracked changes").changed() {
                self.state.saved.diff_hide_uncommitted = !show;
                self.state.touch_saved();
            }
            let mut s = split;
            ui.selectable_value(&mut s, false, "Unified");
            ui.selectable_value(&mut s, true, "Split");
            if s != split {
                self.state.saved.diff_split = s;
                self.state.touch_saved();
            }
            if ui
                .selectable_label(!self.state.saved.diff_nowrap, "Wrap")
                .on_hover_text("Wrap long lines at the pane's width (Alt+Z)")
                .clicked()
            {
                self.state.toggle_wrap();
            }
            let d = self.state.diff.as_ref().unwrap();
            let shown: Vec<&diff::File> = d.diff.files.iter().filter(|f| show || !f.uncommitted).collect();
            let (files, add, del) =
                (shown.len(), shown.iter().map(|f| f.added).sum::<u32>(), shown.iter().map(|f| f.removed).sum::<u32>());
            let open = shown.iter().filter(|f| f.uncommitted).count();
            ui.label(RichText::new(format!("{files} files")).color(t.muted));
            if open > 0 {
                ui.label(RichText::new(format!("{open} uncommitted")).color(t.yellow));
            }
            ui.label(RichText::new(format!("+{add}")).color(t.green));
            ui.label(RichText::new(format!("−{del}")).color(t.red));
            if d.diff.truncated {
                ui.label(RichText::new("Diff truncated at 4 MiB").color(t.yellow));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("Back (Ctrl+D)").clicked() {
                    self.state.toggle_diff();
                }
                let label = format!("Submit review ({to_review})");
                if ui.add_enabled(to_review > 0, egui::Button::new(label)).clicked() {
                    let agents = self.state.store.agents(&path);
                    let pick = agents.iter().find(|a| idle(&a.status)).or(agents.first()).map(|a| a.agent_id.clone());
                    self.state.review = Some(ReviewForm {
                        worktree: path.clone(),
                        agent_id: pick,
                        summary: String::new(),
                        show_prompt: false,
                        expanded: vec![],
                    });
                }
                if sent > 0
                    && ui
                        .small_button(format!("Clear sent ({sent})"))
                        .on_hover_text("Remove the threads already sent with a review")
                        .clicked()
                {
                    let kept: Vec<Comment> = self.state.comments(&path).into_iter().filter(|c| !c.sent).collect();
                    self.state.set_comments(&path, &kept);
                }
            });
        });
        if let Some((base, fetch)) = reload {
            self.state.load_diff_with(&path, base, fetch);
            return;
        }
        let d = self.state.diff.as_ref().unwrap();
        if let Some(note) = &d.note {
            ui.label(RichText::new(note).color(t.yellow));
        }
        if d.loading {
            ui.spinner();
            return;
        }
        if let Some(e) = &d.error {
            ui.label(RichText::new(e).color(t.red));
            return;
        }
        let show = !self.state.saved.diff_hide_uncommitted;
        if !d.diff.files.iter().any(|f| show || !f.uncommitted) {
            ui.label(RichText::new("No changes").color(t.muted));
            return;
        }
        let mut jump = None;
        if self.state.saved.diff_files_hidden {
            // A rail: the list back, and the previous / next file.
            egui::Panel::left("diff-files-rail")
                .resizable(false)
                .exact_size(RAIL_W)
                .frame(egui::Frame::new().inner_margin(egui::Margin { right: 4, ..Default::default() }))
                .show(ui, |ui| {
                    ui.vertical_centered(|ui| {
                        if ui.small_button("▸").on_hover_text("Show the file list (f)").clicked() {
                            self.state.toggle_diff_files();
                        }
                        if ui.small_button("▲").on_hover_text("Previous file ([)").clicked() {
                            self.state.step_file(-1);
                        }
                        if ui.small_button("▼").on_hover_text("Next file (])").clicked() {
                            self.state.step_file(1);
                        }
                    });
                });
        } else {
            egui::Panel::left("diff-files")
                .resizable(true)
                .default_size(260.0)
                .size_range(120.0..=640.0)
                .frame(egui::Frame::new().inner_margin(egui::Margin { right: 6, ..Default::default() }))
                .show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Files").color(t.muted).small());
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            if ui.small_button("◂").on_hover_text("Hide the file list (f)").clicked() {
                                self.state.toggle_diff_files();
                            }
                        });
                    });
                    egui::ScrollArea::vertical().id_salt("diff-file-list").auto_shrink([false, false]).show(ui, |ui| {
                        let d = self.state.diff.as_ref().unwrap();
                        for (fi, f) in d.diff.files.iter().enumerate() {
                            if f.uncommitted && !show {
                                break;
                            }
                            if f.uncommitted && d.diff.files.get(fi.wrapping_sub(1)).is_none_or(|p| !p.uncommitted) {
                                ui.label(RichText::new("Uncommitted").color(t.yellow).small());
                            }
                            if file_entry(ui, &t, f).clicked() {
                                jump = Some(fi);
                            }
                        }
                    });
                });
        }
        let font = FontId::monospace(12.0);
        let char_w = ui.painter().layout_no_wrap("x".into(), font.clone(), t.text).size().x;
        let mut rows_rect = ui.available_rect_before_wrap();
        // The code's own width, against the narrowest place it is shown in
        // (a split pane, or the unified column), minus the vertical bar.
        let gutter = if split { NUM_W } else { 2.0 * NUM_W + 12.0 };
        let view_w = if split { rows_rect.width() / 2.0 - 1.0 } else { rows_rect.width() } - gutter - 12.0;
        let wrap = (!self.state.saved.diff_nowrap).then(|| wrap_cols(view_w, char_w));
        // Rows for this layout, these comments, what is shown and the wrap.
        let key = (split, self.state.comments_gen, show, wrap);
        let d = self.state.diff.as_mut().unwrap();
        if d.built_for != Some(key) {
            // Wrapped again (a resize): the line on top stays on top.
            let rewrap = d.built_for.is_some_and(|b| (b.0, b.1, b.2) == (key.0, key.1, key.2));
            let top = d.rows.get(d.top).map(line_of);
            d.rows = build_rows(&d.diff, split, &comments, &asking, show, wrap);
            d.cols = max_cols(&d.diff);
            d.built_for = Some(key);
            if rewrap && d.scroll_to.is_none() {
                d.scroll_to = top.and_then(|top| d.rows.iter().position(|r| *r == top));
            }
        }
        if let Some(fi) = jump {
            d.scroll_to = d.rows.iter().position(|r| *r == DRow::File(fi));
        }
        let max_hoff = if wrap.is_some() { 0.0 } else { (d.cols as f32 * char_w + 8.0 - view_w).max(0.0) };
        if max_hoff > 0.0 {
            // Shift+wheel, or a sideways trackpad swipe, over the code.
            if ui.rect_contains_pointer(rows_rect) {
                d.hoff -= ui.input(|i| i.smooth_scroll_delta.x);
            }
            let bar = Rect::from_min_max(Pos2::new(rows_rect.left(), rows_rect.bottom() - HBAR_H), rows_rect.max);
            rows_rect.max.y = bar.top();
            hscrollbar(ui, &t, bar, view_w, max_hoff, &mut d.hoff);
        }
        d.hoff = d.hoff.clamp(0.0, max_hoff);
        d.rows_rect = rows_rect;
        let hoff = d.hoff;
        let mut rows_ui = ui.new_child(egui::UiBuilder::new().max_rect(rows_rect).id_salt("diff-rows"));
        rows_ui.set_clip_rect(rows_rect.intersect(ui.clip_rect()));
        // Rows touch: a selection or a thread reads as one block, and row
        // `j` is at `j * ROW_H` (the file list's jumps).
        rows_ui.spacing_mut().item_spacing.y = 0.0;
        // A drag selects lines; it does not move the list.
        let mut area = egui::ScrollArea::vertical()
            .id_salt(("diff-scroll", &path))
            .auto_shrink([false, false])
            .scroll_source(ScrollSource { drag: DragScroll::Never, ..Default::default() });
        if let Some(j) = d.scroll_to.take() {
            area = area.vertical_scroll_offset(j as f32 * ROW_H);
        }
        let mut opened: Option<Comment> = None;
        // Selection events, applied once the rows are drawn.
        let (sel, dragging) = (d.sel, d.dragging);
        let mut new_sel: Option<Sel> = None;
        let mut extend: Option<usize> = None;
        let (mut began_drag, mut on_numbers, mut shifted) = (false, false, false);
        let shift = ui.input(|i| i.modifiers.shift);
        let press = ui.input(|i| i.pointer.press_origin());
        let pointer = ui.input(|i| i.pointer.latest_pos());
        let n = d.rows.len();
        let mut top = 0;
        area.show_rows(&mut rows_ui, ROW_H, n, |ui, range| {
            top = range.start;
            let d = self.state.diff.as_ref().unwrap();
            let width = ui.available_width();
            // The selected side's first and last rows on screen, to extend a
            // drag that leaves the list.
            let mut edges: (Option<usize>, Option<usize>) = (None, None);
            for idx in range {
                let (rect, resp) = ui.allocate_exact_size(Vec2::new(width, ROW_H), Sense::click_and_drag());
                let p = ui.painter().with_clip_rect(rect.intersect(ui.clip_rect()));
                let p = &p;
                let text_y = rect.center().y;
                // What a pointer at `x` on this row selects: (file, row, side).
                let hit = |x: f32| -> Option<(usize, usize, Option<bool>)> {
                    match &d.rows[idx] {
                        DRow::Row(fi, ri) | DRow::More(fi, ri, _) => {
                            code_row(&d.diff.files[*fi].rows[*ri]).then_some((*fi, *ri, None))
                        }
                        DRow::Pair(fi, Pair::Sides(l, r)) | DRow::PairMore(fi, Pair::Sides(l, r), _) => {
                            let left = x < rect.center().x;
                            (if left { *l } else { *r }).map(|ri| (*fi, ri, Some(left)))
                        }
                        _ => None,
                    }
                };
                // This row's line on the selection's side.
                let on_sel = sel.and_then(|s| {
                    let x = match s.side {
                        Some(true) => rect.left(),
                        _ => rect.right() - 1.0,
                    };
                    hit(x).filter(|h| h.0 == s.file && h.2 == s.side).map(|h| h.1)
                });
                if on_sel.is_some() {
                    edges = (edges.0.or(on_sel), on_sel);
                }
                match &d.rows[idx] {
                    DRow::File(fi) => {
                        let f = &d.diff.files[*fi];
                        p.rect_filled(rect, 0.0, t.panel);
                        if f.uncommitted {
                            p.rect_filled(Rect::from_min_size(rect.min, Vec2::new(3.0, ROW_H)), 0.0, t.yellow);
                        }
                        let name = if f.status == "renamed" {
                            format!("{} → {}", f.old_path, f.path)
                        } else {
                            f.path.clone()
                        };
                        let part = if f.uncommitted { "  uncommitted" } else { "" };
                        p.text(
                            Pos2::new(rect.left() + 6.0, text_y),
                            Align2::LEFT_CENTER,
                            format!("{name}  ({}  +{} −{}){part}", f.status, f.added, f.removed),
                            FontId::monospace(12.0),
                            t.text,
                        );
                    }
                    DRow::Uncommitted => {
                        p.rect_filled(rect, 0.0, t.yellow.gamma_multiply(0.18));
                        p.text(
                            Pos2::new(rect.left() + 6.0, text_y),
                            Align2::LEFT_CENTER,
                            "Uncommitted: staged, unstaged and untracked (not in the pull request yet)",
                            FontId::proportional(12.5),
                            t.yellow,
                        );
                    }
                    DRow::Row(fi, ri) | DRow::More(fi, ri, _) => {
                        let k = if let DRow::More(.., k) = &d.rows[idx] { *k } else { 0 };
                        let row = &d.diff.files[*fi].rows[*ri];
                        draw_cell(p, &t, &font, rect, row, true, &Code::of(row, wrap, k, hoff));
                        if sel.is_some_and(|s| s.has(*fi, *ri, None)) {
                            p.rect_filled(rect, 0.0, t.accent.gamma_multiply(0.18));
                        }
                    }
                    DRow::Pair(fi, pair) | DRow::PairMore(fi, pair, _) => {
                        let k = if let DRow::PairMore(.., k) = &d.rows[idx] { *k } else { 0 };
                        let f = &d.diff.files[*fi];
                        let (left, right) = halves(rect);
                        match pair {
                            Pair::Wide(ri) => {
                                draw_cell(p, &t, &font, rect, &f.rows[*ri], false, &Code::whole(&f.rows[*ri], 0.0))
                            }
                            Pair::Sides(l, r) => {
                                for (ri, half, old) in [(l, left, true), (r, right, false)] {
                                    let Some(ri) = ri else { continue };
                                    let row = &f.rows[*ri];
                                    draw_side(p, &t, &font, half, row, old, &Code::of(row, wrap, k, hoff));
                                    if sel.is_some_and(|s| s.has(*fi, *ri, Some(old))) {
                                        p.rect_filled(half, 0.0, t.accent.gamma_multiply(0.18));
                                    }
                                }
                            }
                        }
                    }
                    DRow::Comment(_, id) | DRow::Reply(_, id, _) => {
                        let k = if let DRow::Reply(_, _, k) = &d.rows[idx] { *k } else { 0 };
                        let c = comments.iter().find(|c| &c.id() == id);
                        p.rect_filled(rect, 0.0, t.current_bg);
                        if let Some((who, text)) =
                            c.and_then(|c| thread_lines(c, asking.contains(id)).into_iter().nth(k))
                        {
                            let color = match who {
                                "agent" => t.text,
                                "you" => t.accent,
                                _ => t.muted,
                            };
                            p.text(
                                Pos2::new(rect.left() + 2.0 * NUM_W + 6.0, text_y),
                                Align2::LEFT_CENTER,
                                text,
                                FontId::proportional(12.5),
                                color,
                            );
                        }
                        // A press held still can come as a drag.
                        if resp.clicked() || (resp.drag_stopped() && resp.contains_pointer()) {
                            opened = c.cloned();
                        }
                    }
                }
                // Press and drag, shift-click, or a click on the numbers (a
                // press held still can come as a drag that does not move).
                let start = if resp.drag_started() {
                    press
                } else if resp.clicked() {
                    resp.interact_pointer_pos()
                } else {
                    None
                };
                if let Some(pos) = start
                    && let Some(h) = hit(pos.x)
                {
                    began_drag = resp.drag_started();
                    on_numbers = match h.2 {
                        None => pos.x < rect.left() + 2.0 * NUM_W,
                        Some(true) => pos.x < rect.left() + NUM_W,
                        Some(false) => pos.x > rect.center().x && pos.x < rect.center().x + NUM_W,
                    };
                    if shift && sel.is_some_and(|s| s.file == h.0 && s.side == h.2) {
                        extend = Some(h.1);
                        shifted = true;
                    } else {
                        new_sel = Some(Sel { file: h.0, side: h.2, from: h.1, to: h.1 });
                    }
                }
                if dragging && pointer.is_some_and(|p| rect.y_range().contains(p.y)) && on_sel.is_some() {
                    extend = on_sel;
                }
                if resp.hovered() {
                    let icon = if matches!(d.rows[idx], DRow::Comment(..) | DRow::Reply(..)) {
                        egui::CursorIcon::PointingHand
                    } else {
                        egui::CursorIcon::Text
                    };
                    ui.ctx().set_cursor_icon(icon);
                }
            }
            // A drag past the top or the bottom scrolls, and keeps selecting.
            if dragging && let Some(p) = pointer {
                let clip = ui.clip_rect();
                if p.y > clip.bottom() {
                    ui.scroll_with_delta(Vec2::new(0.0, -ROW_H / 2.0));
                    extend = extend.or(edges.1);
                } else if p.y < clip.top() {
                    ui.scroll_with_delta(Vec2::new(0.0, ROW_H / 2.0));
                    extend = extend.or(edges.0);
                }
            }
        });
        let d = self.state.diff.as_mut().unwrap();
        d.top = top;
        if let Some(s) = new_sel {
            d.sel = Some(s);
        }
        if let (Some(to), Some(s)) = (extend, d.sel.as_mut()) {
            s.to = to;
        }
        let mut popup = false;
        if began_drag {
            d.dragging = true;
            d.drag_pops = on_numbers || shifted;
        } else if new_sel.is_some() || shifted {
            popup = on_numbers || shifted;
        }
        if d.dragging && !ui.input(|i| i.pointer.primary_down()) {
            d.dragging = false;
            popup = d.drag_pops || d.sel.is_some_and(|s| s.from != s.to);
        }
        if popup && let Some(c) = d.sel.and_then(|s| selected_comment(&d.diff, &s)) {
            match comments.iter().find(|x| x.id() == c.id()) {
                // The same lines again: that thread.
                Some(existing) => opened = Some(existing.clone()),
                None => {
                    let pos = pointer.unwrap_or(rows_rect.center()) + Vec2::new(8.0, 10.0);
                    self.state.ask = Some(AskDraft { worktree: path.clone(), comment: c, pos, focus: true });
                }
            }
        }
        if self.state.ask.is_none() && opened.is_none() && ui.input(|i| i.key_pressed(Key::Escape)) {
            self.state.diff.as_mut().unwrap().sel = None;
        }
        if let Some(c) = opened {
            self.state.comment_edit =
                Some(CommentEdit { worktree: path, comment: c, existing: true, follow: String::new() });
        }
    }

    /// The popup on selected lines, the thread, and the review form.
    pub(crate) fn diff_dialogs(&mut self, ctx: &egui::Context) {
        let t = self.state.theme;
        if let Some(mut a) = self.state.ask.take() {
            // Some(false): add to the review; Some(true): ask now.
            let mut act = None::<bool>;
            let mut close = false;
            let starts = self.state.store.agents(&a.worktree).is_empty().then(|| self.state.new_agent_text());
            egui::Area::new(egui::Id::unique("diff-ask"))
                .order(egui::Order::Foreground)
                .fixed_pos(a.pos)
                .constrain(true)
                .show(ctx, |ui| {
                    egui::Frame::popup(ui.style()).show(ui, |ui| {
                        ui.set_width(400.0);
                        ui.label(RichText::new(format!("{}:{}", a.comment.path, a.comment.place())).strong());
                        let edit = ui.add(
                            TextEdit::multiline(&mut a.comment.text)
                                .desired_rows(3)
                                .desired_width(f32::INFINITY)
                                .hint_text("A comment, or a question for the agent"),
                        );
                        if std::mem::take(&mut a.focus) {
                            edit.request_focus();
                        }
                        if ui.input_mut(|i| i.consume_key(Modifiers::CTRL, Key::Enter)) {
                            act = Some(false);
                        }
                        if ui.input_mut(|i| i.consume_key(Modifiers::NONE, Key::Escape)) {
                            close = true;
                        }
                        ui.horizontal(|ui| {
                            let ok = !a.comment.text.trim().is_empty();
                            if ui
                                .add_enabled(ok, egui::Button::new("Add to review"))
                                .on_hover_text("Sent with Submit review (Ctrl+Enter)")
                                .clicked()
                            {
                                act = Some(false);
                            }
                            if ui
                                .add_enabled(ok, egui::Button::new(RichText::new("Ask now").strong()))
                                .on_hover_text("Send it to the agent now; its answer shows here")
                                .clicked()
                            {
                                act = Some(true);
                            }
                            if ui.button("Cancel").clicked() {
                                close = true;
                            }
                        });
                        if let Some(new) = &starts {
                            no_agent_note(ui, &t, new, "Ask now");
                        }
                    });
                });
            match act {
                Some(ask) if !a.comment.text.trim().is_empty() => {
                    a.comment.asked = ask;
                    let id = a.comment.id();
                    self.state.upsert_comment(&a.worktree, a.comment);
                    if let Some(d) = self.state.diff.as_mut() {
                        d.sel = None;
                    }
                    if ask {
                        self.state.ask_agent(&a.worktree, &id);
                    }
                }
                _ if close => {
                    if let Some(d) = self.state.diff.as_mut() {
                        d.sel = None;
                    }
                }
                _ => self.state.ask = Some(a),
            }
        }
        if let Some(mut e) = self.state.comment_edit.take() {
            #[derive(PartialEq)]
            enum Act {
                Save,
                Delete,
                Ask,
                FollowUp,
            }
            let mut act = None::<Act>;
            let mut cancel = false;
            let id = e.comment.id();
            let asking = self.state.asks.iter().any(|a| a.worktree == e.worktree && a.id == id);
            let starts = self.state.store.agents(&e.worktree).is_empty().then(|| self.state.new_agent_text());
            let m = egui::Modal::new(egui::Id::unique("diff-comment")).show(ctx, |ui| {
                ui.set_width(520.0);
                ui.label(RichText::new(format!("{}:{}", e.comment.path, e.comment.place())).strong());
                egui::ScrollArea::vertical().id_salt("thread-code").max_height(120.0).show(ui, |ui| {
                    ui.label(RichText::new(e.comment.code.trim_end()).size(11.5).monospace().color(t.muted));
                });
                if !e.comment.out() {
                    let edit = ui.add(
                        TextEdit::multiline(&mut e.comment.text)
                            .desired_rows(4)
                            .desired_width(f32::INFINITY)
                            .hint_text("Comment (Ctrl+Enter saves)"),
                    );
                    if !edit.has_focus() && e.comment.text.is_empty() {
                        edit.request_focus();
                    }
                    if ui.input_mut(|i| i.consume_key(Modifiers::CTRL, Key::Enter)) {
                        act = Some(Act::Save);
                    }
                } else {
                    egui::ScrollArea::vertical().id_salt("thread").max_height(300.0).show(ui, |ui| {
                        let you = if e.comment.sent { "You (sent with a review)" } else { "You" };
                        ui.label(RichText::new(format!("{you}: {}", e.comment.text.trim())).color(t.accent));
                        for r in &e.comment.replies {
                            let (who, color) = match r.who.as_str() {
                                "agent" => ("Agent: ", t.text),
                                "you" => ("You: ", t.accent),
                                _ => ("", t.muted),
                            };
                            ui.add_space(4.0);
                            ui.label(RichText::new(format!("{who}{}", r.text.trim())).color(color));
                        }
                        if asking {
                            ui.horizontal(|ui| {
                                ui.spinner();
                                ui.label(RichText::new("Waiting for the agent's answer").color(t.muted));
                            });
                        }
                    });
                    let edit = ui.add(
                        TextEdit::multiline(&mut e.follow)
                            .desired_rows(2)
                            .desired_width(f32::INFINITY)
                            .hint_text("A follow-up question (Ctrl+Enter asks)"),
                    );
                    if !asking && !edit.has_focus() && e.follow.is_empty() {
                        edit.request_focus();
                    }
                    if ui.input_mut(|i| i.consume_key(Modifiers::CTRL, Key::Enter)) && !asking {
                        act = Some(Act::FollowUp);
                    }
                }
                if let Some(new) = &starts {
                    no_agent_note(ui, &t, new, "Ask now");
                }
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if e.comment.out() {
                        let ok = !asking && !e.follow.trim().is_empty();
                        if ui.add_enabled(ok, egui::Button::new(RichText::new("Ask now").strong())).clicked() {
                            act = Some(Act::FollowUp);
                        }
                    } else {
                        if ui.button("Save").clicked() {
                            act = Some(Act::Save);
                        }
                        if ui
                            .add_enabled(!e.comment.text.trim().is_empty(), egui::Button::new("Ask now"))
                            .on_hover_text("Send it to the agent now instead of with the review")
                            .clicked()
                        {
                            act = Some(Act::Ask);
                        }
                    }
                    if e.existing && ui.button("Delete").clicked() {
                        act = Some(Act::Delete);
                    }
                    if ui.button(if e.comment.out() { "Close" } else { "Cancel" }).clicked() {
                        cancel = true;
                    }
                });
            });
            cancel |= m.should_close();
            match act {
                Some(Act::Delete) => {
                    let mut all = self.state.comments(&e.worktree);
                    all.retain(|c| c.id() != id);
                    self.state.asks.retain(|a| !(a.worktree == e.worktree && a.id == id));
                    self.state.set_comments(&e.worktree, &all);
                }
                Some(Act::Save) | Some(Act::Ask) if e.comment.text.trim().is_empty() => {
                    let mut all = self.state.comments(&e.worktree);
                    all.retain(|c| c.id() != id);
                    self.state.set_comments(&e.worktree, &all);
                }
                Some(a @ (Act::Save | Act::Ask)) => {
                    e.comment.asked = a == Act::Ask;
                    self.state.upsert_comment(&e.worktree, e.comment);
                    if a == Act::Ask {
                        self.state.ask_agent(&e.worktree, &id);
                    }
                }
                Some(Act::FollowUp) if !e.follow.trim().is_empty() => {
                    e.comment.replies.push(diff::Reply::new("you", e.follow.trim()));
                    self.state.upsert_comment(&e.worktree, e.comment);
                    self.state.ask_agent(&e.worktree, &id);
                }
                _ if !cancel => self.state.comment_edit = Some(e),
                _ => {}
            }
        }
        if let Some(mut r) = self.state.review.take() {
            self.state.give_cids(&r.worktree);
            let comments = self.state.review_comments(&r.worktree);
            let agents = self.state.store.agents(&r.worktree).to_vec();
            // The picked agent may be gone; with none left, a new one.
            if r.agent_id.as_ref().is_some_and(|id| !agents.iter().any(|a| &a.agent_id == id)) {
                r.agent_id = agents.first().map(|a| a.agent_id.clone());
            }
            let new_agent = self.state.new_agent_text();
            let prompt = diff::review_prompt(&comments, &r.summary);
            let screen = ctx.content_rect();
            let (width, list_h) =
                ((screen.width() - 80.0).clamp(360.0, 640.0), (screen.height() * 0.45).clamp(140.0, 380.0));
            let mut action = None::<bool>;
            let mut close = false;
            let m = egui::Modal::new(egui::Id::unique("diff-review")).show(ctx, |ui| {
                ui.set_width(width);
                ui.horizontal(|ui| {
                    ui.label(RichText::new("Submit review").strong().size(15.0));
                    let n = comments.len();
                    let noun = if n == 1 { "comment" } else { "comments" };
                    ui.label(RichText::new(format!("{n} {noun}")).color(t.muted));
                });
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    ui.label(RichText::new("To").color(t.muted));
                    let name = |a: &lazyagent_proto::types::Agent| {
                        format!("{} · {}", store::agent_label(a), store::status_text(&a.status))
                    };
                    let shown = match r.agent_id.as_ref().and_then(|id| agents.iter().find(|a| &a.agent_id == id)) {
                        Some(a) => name(a),
                        None => format!("New agent ({new_agent})"),
                    };
                    egui::ComboBox::from_id_salt("review-to").width(width - 40.0).selected_text(shown).show_ui(
                        ui,
                        |ui| {
                            for a in &agents {
                                ui.selectable_value(&mut r.agent_id, Some(a.agent_id.clone()), name(a));
                            }
                            ui.selectable_value(&mut r.agent_id, None, format!("New agent ({new_agent})"));
                        },
                    );
                });
                if r.agent_id.is_none() {
                    no_agent_note(ui, &t, &new_agent, "Send");
                }
                ui.add_space(4.0);
                ui.add(
                    TextEdit::multiline(&mut r.summary)
                        .desired_rows(2)
                        .desired_width(f32::INFINITY)
                        .hint_text("Summary (optional)"),
                );
                ui.add_space(4.0);
                egui::ScrollArea::vertical().id_salt("review-comments").max_height(list_h).show(ui, |ui| {
                    for c in &comments {
                        review_item(ui, &t, c, &mut r.expanded);
                    }
                });
                ui.add_space(2.0);
                let toggle = if r.show_prompt { "▾ Hide prompt" } else { "▸ Show prompt" };
                if ui
                    .add(egui::Button::new(RichText::new(toggle).color(t.muted)).frame(false))
                    .on_hover_text("Exactly what the agent receives")
                    .clicked()
                {
                    r.show_prompt = !r.show_prompt;
                }
                if r.show_prompt {
                    egui::Frame::new().fill(t.panel).inner_margin(6).corner_radius(4).show(ui, |ui| {
                        egui::ScrollArea::both().id_salt("review-prompt").max_height(220.0).show(ui, |ui| {
                            // size() then monospace(): small() would reset the font to proportional.
                            ui.add(egui::Label::new(RichText::new(&prompt).size(11.5).monospace()).extend());
                        });
                    });
                }
                ui.add_space(4.0);
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    let status = r
                        .agent_id
                        .as_ref()
                        .and_then(|id| agents.iter().find(|a| &a.agent_id == id))
                        .map(|a| a.status.clone());
                    match status {
                        Some(s) if idle(&s) => {
                            if ui.button(RichText::new("Send").strong()).clicked() {
                                action = Some(true);
                            }
                        }
                        Some(_) => {
                            if ui
                                .button(RichText::new("Send when idle").strong())
                                .on_hover_text("When the agent finishes its turn")
                                .clicked()
                            {
                                action = Some(false);
                            }
                            if ui.button("Send now").clicked() {
                                action = Some(true);
                            }
                        }
                        None => {
                            if ui
                                .button(RichText::new("Start agent and send").strong())
                                .on_hover_text(format!("Starts {new_agent} with the review as its first prompt"))
                                .clicked()
                            {
                                action = Some(true);
                            }
                        }
                    }
                    if ui.button("Cancel").clicked() {
                        close = true;
                    }
                });
            });
            if m.should_close() {
                close = true;
            }
            let ids: Vec<String> = comments.iter().map(Comment::id).collect();
            match (action, r.agent_id.clone()) {
                (Some(_), None) => self.state.review_new_agent(&r.worktree, prompt, ids),
                (Some(now), Some(agent_id)) => {
                    let pending = PendingReview { worktree: r.worktree.clone(), agent_id, text: prompt, ids };
                    if now {
                        self.state.send_review(pending);
                    } else {
                        self.state.toast(Severity::Info, "The review goes out when the agent finishes its turn.");
                        self.state.pending_reviews.push(pending);
                    }
                }
                _ if close => {}
                _ => self.state.review = Some(r),
            }
        }
    }
}

/// Says that no agent is here and what `button` starts instead.
fn no_agent_note(ui: &mut Ui, t: &crate::theme::Theme, new_agent: &str, button: &str) {
    ui.label(
        RichText::new(format!("No agent in this worktree: {button} starts {new_agent} and gives it this."))
            .small()
            .color(t.yellow),
    );
}

/// One comment in the review form: where, the comment, and its code (the
/// first line; all of it once expanded).
fn review_item(ui: &mut Ui, t: &crate::theme::Theme, c: &Comment, expanded: &mut Vec<String>) {
    let id = c.id();
    let open = expanded.contains(&id);
    egui::Frame::new().fill(t.panel).inner_margin(6).corner_radius(4).show(ui, |ui| {
        ui.set_width(ui.available_width());
        let part = if c.uncommitted { " · uncommitted" } else { "" };
        ui.label(RichText::new(format!("{}:{}{part}", c.path, c.place())).size(11.5).monospace().color(t.muted));
        ui.label(RichText::new(c.text.trim()).size(13.5).color(t.text));
        let lines: Vec<&str> = c.code.trim_end().lines().collect();
        let code = |ui: &mut Ui, text: &str| {
            ui.add(egui::Label::new(RichText::new(text).size(11.0).monospace().color(t.muted)).truncate());
        };
        if open {
            for l in &lines {
                code(ui, l);
            }
        } else if let Some(first) = lines.first() {
            code(ui, first);
        }
        if lines.len() > 1 {
            let label = if open { "▾ less".to_string() } else { format!("▸ {} more lines", lines.len() - 1) };
            if ui.add(egui::Button::new(RichText::new(label).small().color(t.accent)).frame(false)).clicked() {
                if open {
                    expanded.retain(|x| *x != id);
                } else {
                    expanded.push(id.clone());
                }
            }
        }
    });
    ui.add_space(4.0);
}

/// A split row's two panes.
fn halves(rect: Rect) -> (Rect, Rect) {
    let half = rect.width() / 2.0;
    let left = Rect::from_min_size(rect.min, Vec2::new(half - 1.0, rect.height()));
    let right =
        Rect::from_min_size(Pos2::new(rect.left() + half + 1.0, rect.top()), Vec2::new(half - 1.0, rect.height()));
    (left, right)
}

/// The file list's width when it is hidden: a rail with its buttons.
const RAIL_W: f32 = 26.0;

/// How many columns of code fit in `view_w` (never fewer than 8).
pub fn wrap_cols(view_w: f32, char_w: f32) -> usize {
    ((view_w - 8.0) / char_w.max(1.0)).floor().max(8.0) as usize
}

/// The row that starts `row`'s line (itself for any other row).
fn line_of(row: &DRow) -> DRow {
    match row {
        DRow::More(fi, ri, _) => DRow::Row(*fi, *ri),
        DRow::PairMore(fi, pair, _) => DRow::Pair(*fi, pair.clone()),
        other => other.clone(),
    }
}

/// The header row of the file after (`delta` > 0) or before the row on
/// top; before it means the top file's own header when it is scrolled into.
pub fn file_step(rows: &[DRow], top: usize, delta: i32) -> Option<usize> {
    let header = |r: &DRow| matches!(r, DRow::File(_));
    if delta > 0 {
        rows.iter().skip(top + 1).position(header).map(|i| top + 1 + i)
    } else {
        rows.get(..top.min(rows.len()))?.iter().rposition(header)
    }
}

/// The longest code line, in columns.
fn max_cols(d: &Diff) -> usize {
    d.files
        .iter()
        .flat_map(|f| &f.rows)
        .filter(|r| !matches!(r.kind, RowKind::Hunk | RowKind::Note))
        .map(|r| r.text.chars().map(|c| if c == '\t' { 4 } else { 1 }).sum::<usize>())
        .max()
        .unwrap_or(0)
}

/// One file in the list: its counts on the right, its name elided into
/// what is left (the full path on hover), all inside the list's width.
fn file_entry(ui: &mut Ui, t: &crate::theme::Theme, f: &diff::File) -> egui::Response {
    let font = FontId::monospace(11.0);
    let (rect, resp) = ui.allocate_exact_size(Vec2::new(ui.available_width(), ROW_H + 2.0), Sense::click());
    resp.widget_info(|| egui::WidgetInfo::labeled(egui::Role::Button, true, &f.path));
    let p = ui.painter().with_clip_rect(rect.intersect(ui.clip_rect()));
    if resp.hovered() {
        p.rect_filled(rect, 3.0, t.current_bg);
    }
    let y = rect.center().y;
    let del =
        p.text(Pos2::new(rect.right() - 2.0, y), Align2::RIGHT_CENTER, format!("−{}", f.removed), font.clone(), t.red);
    let add =
        p.text(Pos2::new(del.left() - 5.0, y), Align2::RIGHT_CENTER, format!("+{}", f.added), font.clone(), t.green);
    let mut job = LayoutJob::single_section(
        store::basename(&f.path).to_string(),
        TextFormat::simple(FontId::monospace(11.5), t.text),
    );
    job.wrap = TextWrapping::truncate_at_width((add.left() - 8.0 - rect.left() - 4.0).max(0.0));
    let name = ui.painter().layout_job(job);
    p.galley(Pos2::new(rect.left() + 4.0, y - name.size().y / 2.0), name, t.text);
    resp.on_hover_text(&f.path)
}

/// Where a resolved base came from, next to the Base field.
pub fn base_label(b: &DiffBase) -> String {
    match (b.source.as_str(), b.pr_number) {
        ("pr", Some(n)) => format!("PR #{n} base"),
        ("pr", None) => "PR base".into(),
        ("default", _) => "default branch".into(),
        ("override", _) => "override".into(),
        (other, _) => other.to_string(),
    }
}

fn short(sha: &str) -> &str {
    sha.get(..10).unwrap_or(sha)
}

/// A sideways scrollbar for the code: drag the thumb, or click the track.
fn hscrollbar(ui: &mut Ui, t: &crate::theme::Theme, bar: Rect, view_w: f32, max_hoff: f32, hoff: &mut f32) {
    let resp = ui.interact(bar, egui::Id::unique("diff-hscroll"), Sense::click_and_drag());
    let track = bar.shrink2(Vec2::new(2.0, 2.0));
    let thumb_w = (track.width() * view_w / (view_w + max_hoff)).max(24.0).min(track.width());
    let span = (track.width() - thumb_w).max(1.0);
    if resp.dragged() {
        *hoff += resp.drag_delta().x * max_hoff / span;
    } else if resp.clicked()
        && let Some(pos) = resp.interact_pointer_pos()
    {
        *hoff = (pos.x - track.left() - thumb_w / 2.0) / span * max_hoff;
    }
    *hoff = hoff.clamp(0.0, max_hoff);
    let x = track.left() + *hoff / max_hoff * span;
    let thumb = Rect::from_min_size(Pos2::new(x, track.top()), Vec2::new(thumb_w, track.height()));
    let color = if resp.hovered() || resp.dragged() { t.muted } else { t.border };
    ui.painter().rect_filled(thumb, 3.0, color);
}

fn draw_number(p: &egui::Painter, t: &crate::theme::Theme, rect: Rect, x: f32, n: Option<u32>) {
    if let Some(n) = n {
        p.text(Pos2::new(x, rect.center().y), Align2::RIGHT_CENTER, n.to_string(), FontId::monospace(11.0), t.muted);
    }
}

/// What a row draws of its line: the piece of code, whether it is the
/// line's first row (its numbers and +/−), and the sideways scroll.
pub(crate) struct Code {
    pub text: String,
    pub first: bool,
    pub hoff: f32,
}

impl Code {
    /// The whole line, on one row.
    pub(crate) fn whole(row: &diff::Row, hoff: f32) -> Code {
        Code { text: diff::expand_tabs(&row.text), first: true, hoff }
    }

    /// Piece `k` of the line wrapped at `cols` (empty past its end).
    pub(crate) fn piece(row: &diff::Row, cols: usize, k: usize) -> Code {
        let text = diff::expand_tabs(&row.text);
        let text = match diff::wrap_line(&text, cols).get(k) {
            Some(r) => text.chars().skip(r.start).take(r.len()).collect(),
            None => String::new(),
        };
        Code { text, first: k == 0, hoff: 0.0 }
    }

    /// What `row` draws on screen row `k` of it: wrapped at `wrap`, else whole.
    fn of(row: &diff::Row, wrap: Option<usize>, k: usize, hoff: f32) -> Code {
        match wrap {
            Some(cols) if code_row(row) => Code::piece(row, cols, k),
            _ if k == 0 => Code::whole(row, hoff),
            _ => Code { text: String::new(), first: false, hoff },
        }
    }
}

pub(crate) fn draw_cell(
    p: &egui::Painter,
    t: &crate::theme::Theme,
    font: &FontId,
    rect: Rect,
    row: &diff::Row,
    numbers: bool,
    code: &Code,
) {
    let (bg, sign) = row_colors(row.kind);
    if bg != Color32::TRANSPARENT {
        p.rect_filled(rect, 0.0, bg);
    }
    let y = rect.center().y;
    match row.kind {
        RowKind::Hunk | RowKind::Note => {
            p.text(Pos2::new(rect.left() + 6.0, y), Align2::LEFT_CENTER, &row.text, font.clone(), t.cyan);
        }
        _ => {
            if numbers && code.first {
                draw_number(p, t, rect, rect.left() + NUM_W - 6.0, row.old);
                draw_number(p, t, rect, rect.left() + 2.0 * NUM_W - 6.0, row.new);
            }
            let x = rect.left() + if numbers { 2.0 * NUM_W } else { NUM_W };
            let mark = match row.kind {
                RowKind::Add if code.first => "+",
                RowKind::Del if code.first => "-",
                _ => " ",
            };
            p.text(
                Pos2::new(x, y),
                Align2::LEFT_CENTER,
                mark,
                font.clone(),
                if sign == Color32::TRANSPARENT { t.muted } else { sign },
            );
            let rect = Rect::from_min_max(Pos2::new(x + 12.0, rect.top()), rect.max);
            draw_code(p, t, font, rect, code);
        }
    }
}

fn draw_side(
    p: &egui::Painter,
    t: &crate::theme::Theme,
    font: &FontId,
    rect: Rect,
    row: &diff::Row,
    old: bool,
    code: &Code,
) {
    let painter = p.with_clip_rect(rect.intersect(p.clip_rect()));
    let (bg, _) = row_colors(row.kind);
    if bg != Color32::TRANSPARENT {
        painter.rect_filled(rect, 0.0, bg);
    }
    if code.first {
        draw_number(&painter, t, rect, rect.left() + NUM_W - 6.0, if old { row.old } else { row.new });
    }
    let rect = Rect::from_min_max(Pos2::new(rect.left() + NUM_W, rect.top()), rect.max);
    draw_code(&painter, t, font, rect, code);
}

/// A piece of code, scrolled `hoff` to the left and clipped to `rect`, so
/// it never runs over the line numbers or the other pane.
fn draw_code(p: &egui::Painter, t: &crate::theme::Theme, font: &FontId, rect: Rect, code: &Code) {
    if code.text.is_empty() {
        return;
    }
    p.with_clip_rect(rect.intersect(p.clip_rect())).text(
        Pos2::new(rect.left() - code.hoff, rect.center().y),
        Align2::LEFT_CENTER,
        &code.text,
        font.clone(),
        t.text,
    );
}

/// The flat row list: a header per file, its rows (or pairs), and each
/// thread under its (last) line, a row per line of it (in split, under the
/// pair holding it). `asking`: the threads waiting for an answer.
/// The rows, with the uncommitted files (under their heading) when `uncommitted`.
/// `wrap`: the code's columns, a line longer than that takes a row per
/// piece (its thread under the last one); `None` keeps each line on one row.
pub fn build_rows(
    d: &Diff,
    split: bool,
    comments: &[Comment],
    asking: &[String],
    uncommitted: bool,
    wrap: Option<usize>,
) -> Vec<DRow> {
    let mut out = Vec::with_capacity(d.files.iter().map(|f| f.rows.len() + 1).sum::<usize>() + 1);
    for (fi, f) in d.files.iter().enumerate() {
        if f.uncommitted {
            if !uncommitted {
                break;
            }
            if fi == 0 || !d.files[fi - 1].uncommitted {
                out.push(DRow::Uncommitted);
            }
        }
        out.push(DRow::File(fi));
        let pieces = |ri: usize| match wrap {
            Some(cols) if code_row(&f.rows[ri]) => diff::wrap_count(&f.rows[ri].text, cols),
            _ => 1,
        };
        let on = |ri: usize| -> Vec<DRow> {
            let Some(a) = diff::anchor_in(f, &f.rows[ri]) else { return vec![] };
            let at = a.id();
            let mut rows = vec![];
            for c in comments.iter().filter(|c| c.anchor_id() == at) {
                let id = c.id();
                let n = thread_lines(c, asking.contains(&id)).len();
                rows.push(DRow::Comment(fi, id.clone()));
                rows.extend((1..n).map(|k| DRow::Reply(fi, id.clone(), k)));
            }
            rows
        };
        if split {
            for pair in diff::split_rows(&f.rows) {
                let threads: Vec<DRow> = match &pair {
                    Pair::Sides(l, r) => {
                        let mut threads: Vec<DRow> = l.map(on).unwrap_or_default();
                        // A context line is one row on both sides: one thread.
                        if l != r {
                            threads.extend(r.map(on).unwrap_or_default());
                        }
                        threads
                    }
                    Pair::Wide(_) => vec![],
                };
                let n = match &pair {
                    Pair::Sides(l, r) => l.map_or(1, pieces).max(r.map_or(1, pieces)),
                    Pair::Wide(_) => 1,
                };
                out.push(DRow::Pair(fi, pair.clone()));
                out.extend((1..n).map(|k| DRow::PairMore(fi, pair.clone(), k)));
                out.extend(threads);
            }
        } else {
            for ri in 0..f.rows.len() {
                out.push(DRow::Row(fi, ri));
                out.extend((1..pieces(ri)).map(|k| DRow::More(fi, ri, k)));
                out.extend(on(ri));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rows_carry_comments_in_both_layouts() {
        let d = diff::parse("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n");
        let rows = &d.files[0].rows;
        let mut c1 = diff::anchor("a.py", &rows[1]).unwrap();
        c1.text = "on ctx".into();
        let mut c2 = diff::anchor("a.py", &rows[2]).unwrap();
        c2.text = "on old".into();
        let unified = build_rows(&d, false, &[c1.clone(), c2.clone()], &[], true, None);
        assert_eq!(
            unified,
            [
                DRow::File(0),
                DRow::Row(0, 0),
                DRow::Row(0, 1),
                DRow::Comment(0, c1.id()),
                DRow::Row(0, 2),
                DRow::Comment(0, c2.id()),
                DRow::Row(0, 3)
            ]
        );
        let split = build_rows(&d, true, &[c1.clone(), c2.clone()], &[], true, None);
        assert_eq!(split.iter().filter(|r| matches!(r, DRow::Comment(..))).count(), 2, "the context comment once");
        assert_eq!(split.len(), 1 + 3 + 2);
    }

    /// A file whose line 2 is removed and replaced by a long one.
    const LONG: &str = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n ctx\n-short\n+aaaa bbbb cccc dddd eeee ffff gggg\n end\n";

    #[test]
    fn a_wrapped_line_takes_a_row_per_piece_and_its_thread_follows_the_last() {
        let d = diff::parse(LONG);
        let rows = &d.files[0].rows;
        let mut c = diff::anchor("a.py", &rows[3]).unwrap();
        c.text = "on the long line".into();
        // 34 columns at 10: three pieces.
        let unified = build_rows(&d, false, std::slice::from_ref(&c), &[], true, Some(10));
        assert_eq!(
            unified,
            [
                DRow::File(0),
                DRow::Row(0, 0),
                DRow::Row(0, 1),
                DRow::Row(0, 2),
                DRow::Row(0, 3),
                DRow::More(0, 3, 1),
                DRow::More(0, 3, 2),
                DRow::More(0, 3, 3),
                DRow::Comment(0, c.id()),
                DRow::Row(0, 4),
            ]
        );
        // Unwrapped (or wide enough): one row per line, as before.
        assert_eq!(build_rows(&d, false, &[], &[], true, None).len(), 6);
        assert_eq!(build_rows(&d, false, &[], &[], true, Some(80)).len(), 6);
    }

    #[test]
    fn split_rows_are_as_tall_as_their_taller_side() {
        let d = diff::parse(LONG);
        let mut c = diff::anchor("a.py", &d.files[0].rows[2]).unwrap();
        c.text = "on the short old line".into();
        let rows = build_rows(&d, true, std::slice::from_ref(&c), &[], true, Some(10));
        let pair = Pair::Sides(Some(2), Some(3));
        assert_eq!(
            rows[3..8],
            [
                DRow::Pair(0, pair.clone()),
                DRow::PairMore(0, pair.clone(), 1),
                DRow::PairMore(0, pair.clone(), 2),
                DRow::PairMore(0, pair, 3),
                // The short side's thread waits for the long side's end.
                DRow::Comment(0, c.id()),
            ]
        );
        // The short side has nothing past its first row; the long one has
        // each piece, and together they are the line.
        let (old, new) = (&d.files[0].rows[2], &d.files[0].rows[3]);
        assert_eq!(Code::of(old, Some(10), 1, 0.0).text, "");
        let joined: String = (0..4).map(|k| Code::of(new, Some(10), k, 0.0).text).collect();
        assert_eq!(joined, new.text);
    }

    /// The shapes `draw` paints.
    fn shapes(draw: impl Fn(&egui::Painter)) -> Vec<egui::Shape> {
        let ctx = egui::Context::default();
        let mut out = ctx.run_ui(egui::RawInput::default(), |ui| draw(ui.painter()));
        out.textures_delta.clear();
        out.shapes.into_iter().map(|s| s.shape).collect()
    }

    /// The texts `draw` paints.
    fn painted(draw: impl Fn(&egui::Painter)) -> Vec<String> {
        shapes(draw)
            .into_iter()
            .filter_map(|s| match s {
                egui::Shape::Text(t) => Some(t.galley.text().to_string()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn continuation_rows_have_no_number_or_sign_but_keep_the_colour() {
        let d = diff::parse(LONG);
        let row = &d.files[0].rows[3];
        let t = crate::theme::Theme::DARK;
        let font = FontId::monospace(12.0);
        let rect = Rect::from_min_size(Pos2::ZERO, Vec2::new(400.0, ROW_H));
        let first = painted(|p| draw_cell(p, &t, &font, rect, row, true, &Code::of(row, Some(10), 0, 0.0)));
        assert_eq!(first, ["2", "+", "aaaa bbbb "], "the number, the sign, the first piece");
        let more = painted(|p| draw_cell(p, &t, &font, rect, row, true, &Code::of(row, Some(10), 1, 0.0)));
        assert_eq!(more, [" ", "cccc dddd "], "no number, no sign");
        let side = painted(|p| draw_side(p, &t, &font, rect, row, false, &Code::of(row, Some(10), 2, 0.0)));
        assert_eq!(side, ["eeee ffff "]);
        // The added line's green runs under its continuation rows too.
        let green = row_colors(RowKind::Add).0;
        let more = shapes(|p| draw_cell(p, &t, &font, rect, row, true, &Code::of(row, Some(10), 1, 0.0)));
        assert!(more.iter().any(|s| matches!(s, egui::Shape::Rect(r) if r.fill == green)));
    }

    #[test]
    fn next_and_previous_file_from_the_row_on_top() {
        let rows = [DRow::File(0), DRow::Row(0, 0), DRow::Row(0, 1), DRow::Uncommitted, DRow::File(1), DRow::Row(1, 0)];
        assert_eq!(file_step(&rows, 0, 1), Some(4));
        assert_eq!(file_step(&rows, 2, 1), Some(4));
        assert_eq!(file_step(&rows, 4, 1), None, "the last file");
        assert_eq!(file_step(&rows, 5, -1), Some(4), "into a file: its own header");
        assert_eq!(file_step(&rows, 4, -1), Some(0), "on a header: the file before");
        assert_eq!(file_step(&rows, 0, -1), None);
    }

    #[test]
    fn the_wrap_columns_follow_the_pane() {
        assert_eq!(wrap_cols(708.0, 7.0), 100);
        assert_eq!(wrap_cols(20.0, 7.0), 8, "never fewer than 8");
    }

    fn pr_base() -> DiffBase {
        DiffBase {
            ref_: "origin/TASK-456-checkout".into(),
            branch: Some("TASK-456-checkout".into()),
            source: "pr".into(),
            merge_base: "0123456789abcdef".into(),
            pr_number: Some(4242),
        }
    }

    #[test]
    fn the_base_label_says_where_the_base_came_from() {
        assert_eq!(base_label(&pr_base()), "PR #4242 base");
        assert_eq!(base_label(&DiffBase { pr_number: None, ..pr_base() }), "PR base");
        assert_eq!(base_label(&DiffBase { source: "default".into(), ..pr_base() }), "default branch");
        assert_eq!(base_label(&DiffBase { source: "override".into(), ..pr_base() }), "override");
        assert_eq!(base_label(&DiffBase { source: "upstream".into(), ..pr_base() }), "upstream");
    }

    #[test]
    fn the_base_field_shows_the_resolved_base_and_takes_an_override() {
        let mut d = DiffState::loaded("/r/f", "", Diff::default());
        d.applied(Some(pr_base()), None, None);
        assert_eq!(d.base, "origin/TASK-456-checkout", "the field shows what is used");
        assert_eq!(d.base_request(), "", "unchanged: still the engine's pick");
        d.base = "  ".into();
        assert_eq!(d.base_request(), "", "cleared: the engine's pick");
        d.base = "origin/master".into();
        assert_eq!(d.base_request(), "origin/master", "any ref overrides it");
        // An override keeps the field as typed, even when it resolves.
        let mut o = DiffState::loaded("/r/f", "master", Diff::default());
        o.applied(Some(DiffBase { ref_: "master".into(), source: "override".into(), ..pr_base() }), None, None);
        assert_eq!((o.base.as_str(), o.base_request().as_str()), ("master", "master"));
    }

    #[test]
    fn the_notes_say_what_went_wrong() {
        let mut d = DiffState::loaded("/r/f", "", Diff::default());
        d.applied(None, Some("No base branch found".into()), None);
        assert_eq!(d.note.as_deref(), Some("Only uncommitted changes: No base branch found"));
        assert_eq!(d.base, "");
        d.applied(Some(pr_base()), None, Some("offline".into()));
        assert_eq!(d.note.as_deref(), Some("Fetch failed: offline"));
    }

    #[test]
    fn uncommitted_files_come_last_under_their_heading() {
        let file = |p: &str| format!("diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n");
        let d = diff::parse_pr(&file("a.py"), &format!("{}\n{}", file("a.py"), file("b.py")));
        let mut c = diff::anchor_in(&d.files[1], &d.files[1].rows[2]).unwrap();
        c.text = "here".into();
        let rows = build_rows(&d, false, std::slice::from_ref(&c), &[], true, None);
        let at = rows.iter().position(|r| *r == DRow::Uncommitted).unwrap();
        assert_eq!(rows[at - 1], DRow::Row(0, 2));
        assert_eq!(rows[at + 1], DRow::File(1));
        // The comment sits under the uncommitted copy of the line only.
        let under = rows.iter().position(|r| matches!(r, DRow::Comment(..))).unwrap();
        assert_eq!(rows[under - 1], DRow::Row(1, 2));
        let hidden = build_rows(&d, false, std::slice::from_ref(&c), &[], false, None);
        assert_eq!(hidden, rows[..at].to_vec());
    }
}
