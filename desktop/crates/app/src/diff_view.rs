//! The diff viewer and review (ui-plan §3d / §4c, the web's U5): a base,
//! unified or split rows in one virtualised list of fixed-height rows, a
//! file list that jumps, comments on lines or ranges (kept in the app's
//! state file), "Submit review" to an agent, now or when it is idle, and
//! questions asked now, their answers read back under the code.

use std::sync::Arc;

use eframe::egui::scroll_area::{DragScroll, ScrollSource};
use eframe::egui::text::{LayoutJob, TextWrapping};
use eframe::egui::{
    self, Align, Align2, Color32, FontId, Key, Layout, Modifiers, Pos2, Rect, RichText, Sense, TextEdit, TextFormat,
    Ui, Vec2,
};
use lazyagent_proto::types::{DiffBase, DiffResult};
use serde_json::json;

use crate::app::{App, Severity, State};
use crate::diff::{self, Comment, Diff, Pair, RowKind};
use crate::store::{self, ORCHESTRATOR_KEY};

pub const ROW_H: f32 = 18.0;
const MAX_BYTES: u64 = 4 * 1024 * 1024;
const NUM_W: f32 = 44.0;
const HBAR_H: f32 = 10.0;

#[derive(Debug, Clone, PartialEq)]
pub enum DRow {
    File(usize),
    Row(usize, usize),
    Pair(usize, Pair),
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
    /// Rebuild `rows` when the layout, the comments or what is shown change.
    built_for: Option<(bool, u64, bool)>,
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

/// A question sent to an agent; its answer is read back from the agent's
/// output, between `marker` lines, once the agent has run and is idle.
pub struct PendingAsk {
    pub worktree: String,
    pub agent_id: String,
    /// The comment's id.
    pub id: String,
    pub marker: String,
    pub sent: bool,
    pub ran: bool,
    pub reading: bool,
}

pub struct ReviewForm {
    pub worktree: String,
    pub agent_id: Option<String>,
    pub summary: String,
    pub clear: bool,
}

/// A review waiting for its agent to finish its turn.
pub struct PendingReview {
    pub worktree: String,
    pub agent_id: String,
    pub text: String,
    pub clear: bool,
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
        let params = json!({"worktree_path": r.worktree, "agent_id": r.agent_id, "text": r.text});
        let (worktree, clear) = (r.worktree.clone(), r.clear);
        self.conn.request("send_agent_input", params, move |s, reply| match reply {
            Ok(v) => {
                s.toast(Severity::Info, format!("Review: {}", crate::attention_view::delivery_text(v)));
                if clear {
                    // The asked threads were not part of it.
                    let kept: Vec<Comment> = s.comments(&worktree).into_iter().filter(|c| c.asked).collect();
                    s.set_comments(&worktree, &kept);
                }
            }
            Err(e) => s.toast(Severity::Error, format!("The review was not sent: {e}")),
        });
    }

    /// The comments that go with Submit review (not the asked ones).
    pub fn review_comments(&self, worktree: &str) -> Vec<Comment> {
        self.comments(worktree).into_iter().filter(|c| !c.asked).collect()
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
        self.asks.iter().filter(|a| a.worktree == worktree).map(|a| a.id.clone()).collect()
    }

    /// Sends the thread `id` (its comment, or its last follow-up) to the
    /// worktree's agent (an idle one first) as a question, now.
    pub fn ask_agent(&mut self, worktree: &str, id: &str) {
        let Some(c) = self.comments(worktree).into_iter().find(|c| c.id() == id) else { return };
        let agents = self.store.agents(worktree);
        let Some(agent_id) = agents.iter().find(|a| idle(&a.status)).or(agents.first()).map(|a| a.agent_id.clone())
        else {
            self.toast(Severity::Error, "No agent in this worktree to ask.");
            return;
        };
        let marker = marker_id();
        let base = self.diff.as_ref().filter(|d| d.path == worktree).map(|d| d.base.clone()).unwrap_or_default();
        let params =
            json!({"worktree_path": worktree, "agent_id": agent_id, "text": diff::ask_prompt(&c, &base, &marker)});
        self.asks.retain(|a| !(a.worktree == worktree && a.id == id));
        self.asks.push(PendingAsk {
            worktree: worktree.to_string(),
            agent_id,
            id: id.to_string(),
            marker,
            sent: false,
            ran: false,
            reading: false,
        });
        self.comments_gen += 1;
        let (worktree, id) = (worktree.to_string(), id.to_string());
        self.conn.request("send_agent_input", params, move |s, reply| {
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
    }

    /// Reads the answers of the agents that ran and are idle again.
    pub fn pending_asks(&mut self) {
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
            Ok(Some(text)) => diff::Reply { who: "agent".into(), text },
            Ok(None) => diff::Reply { who: "note".into(), text: "Answered in the agent's terminal.".into() },
            Err(why) => diff::Reply { who: "note".into(), text: why.into() },
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
    push("you", if c.asked { "?" } else { "▸" }, &c.text);
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
        let to_review = comments.iter().filter(|c| !c.asked).count();
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
                        clear: true,
                    });
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
        // Rows for this layout, these comments and what is shown.
        let key = (split, self.state.comments_gen, show);
        if self.state.diff.as_ref().unwrap().built_for != Some(key) {
            let d = self.state.diff.as_mut().unwrap();
            d.rows = build_rows(&d.diff, split, &comments, &asking, show);
            d.cols = max_cols(&d.diff);
            d.built_for = Some(key);
        }
        let mut jump = None;
        egui::Panel::left("diff-files")
            .resizable(true)
            .default_size(260.0)
            .size_range(120.0..=640.0)
            .frame(egui::Frame::new().inner_margin(egui::Margin { right: 6, ..Default::default() }))
            .show(ui, |ui| {
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
                            jump = d.rows.iter().position(|r| *r == DRow::File(fi));
                        }
                    }
                });
            });
        if let Some(j) = jump {
            self.state.diff.as_mut().unwrap().scroll_to = Some(j);
        }
        let font = FontId::monospace(12.0);
        let char_w = ui.painter().layout_no_wrap("x".into(), font.clone(), t.text).size().x;
        let mut rows_rect = ui.available_rect_before_wrap();
        let d = self.state.diff.as_mut().unwrap();
        // The code's own width, against the narrowest place it is shown in
        // (a split pane, or the unified column), minus the vertical bar.
        let gutter = if split { NUM_W } else { 2.0 * NUM_W + 12.0 };
        let view_w = if split { rows_rect.width() / 2.0 - 1.0 } else { rows_rect.width() } - gutter - 12.0;
        let max_hoff = (d.cols as f32 * char_w + 8.0 - view_w).max(0.0);
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
        area.show_rows(&mut rows_ui, ROW_H, n, |ui, range| {
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
                        DRow::Row(fi, ri) => code_row(&d.diff.files[*fi].rows[*ri]).then_some((*fi, *ri, None)),
                        DRow::Pair(fi, Pair::Sides(l, r)) => {
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
                    DRow::Row(fi, ri) => {
                        draw_cell(p, &t, &font, rect, &d.diff.files[*fi].rows[*ri], true, hoff);
                        if sel.is_some_and(|s| s.has(*fi, *ri, None)) {
                            p.rect_filled(rect, 0.0, t.accent.gamma_multiply(0.18));
                        }
                    }
                    DRow::Pair(fi, pair) => {
                        let f = &d.diff.files[*fi];
                        let (left, right) = halves(rect);
                        match pair {
                            Pair::Wide(ri) => draw_cell(p, &t, &font, rect, &f.rows[*ri], false, 0.0),
                            Pair::Sides(l, r) => {
                                for (ri, half, old) in [(l, left, true), (r, right, false)] {
                                    let Some(ri) = ri else { continue };
                                    draw_side(p, &t, &font, half, &f.rows[*ri], old, hoff);
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
            let m = egui::Modal::new(egui::Id::unique("diff-comment")).show(ctx, |ui| {
                ui.set_width(520.0);
                ui.label(RichText::new(format!("{}:{}", e.comment.path, e.comment.place())).strong());
                egui::ScrollArea::vertical().id_salt("thread-code").max_height(120.0).show(ui, |ui| {
                    ui.label(RichText::new(e.comment.code.trim_end()).size(11.5).monospace().color(t.muted));
                });
                if !e.comment.asked {
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
                        ui.label(RichText::new(format!("You: {}", e.comment.text.trim())).color(t.accent));
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
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if e.comment.asked {
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
                    if ui.button(if e.comment.asked { "Close" } else { "Cancel" }).clicked() {
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
                    e.comment.replies.push(diff::Reply { who: "you".into(), text: e.follow.trim().to_string() });
                    self.state.upsert_comment(&e.worktree, e.comment);
                    self.state.ask_agent(&e.worktree, &id);
                }
                _ if !cancel => self.state.comment_edit = Some(e),
                _ => {}
            }
        }
        if let Some(mut r) = self.state.review.take() {
            let comments = self.state.review_comments(&r.worktree);
            let agents = self.state.store.agents(&r.worktree).to_vec();
            let mut action = None::<bool>;
            let mut close = false;
            let m = egui::Modal::new(egui::Id::unique("diff-review")).show(ctx, |ui| {
                ui.set_width(620.0);
                let n = comments.len();
                let noun = if n == 1 { "comment" } else { "comments" };
                ui.label(RichText::new(format!("Submit review ({n} {noun})")).strong().size(15.0));
                if agents.is_empty() {
                    ui.label(RichText::new("No agent in this worktree to send it to.").color(t.red));
                }
                egui::ComboBox::from_label("to")
                    .selected_text(
                        r.agent_id
                            .as_ref()
                            .and_then(|id| agents.iter().find(|a| &a.agent_id == id))
                            .map(|a| format!("{} ({})", store::agent_label(a), store::status_text(&a.status)))
                            .unwrap_or_else(|| "choose an agent".into()),
                    )
                    .show_ui(ui, |ui| {
                        for a in &agents {
                            let label = format!("{} ({})", store::agent_label(a), store::status_text(&a.status));
                            ui.selectable_value(&mut r.agent_id, Some(a.agent_id.clone()), label);
                        }
                    });
                ui.add(
                    TextEdit::multiline(&mut r.summary)
                        .desired_rows(2)
                        .desired_width(f32::INFINITY)
                        .hint_text("Summary (optional)"),
                );
                ui.label(RichText::new("What the agent receives:").small().color(t.muted));
                egui::ScrollArea::vertical().max_height(260.0).show(ui, |ui| {
                    // size() then monospace(): small() would reset the font to proportional.
                    ui.label(RichText::new(diff::review_prompt(&comments, &r.summary)).size(11.5).monospace());
                });
                ui.checkbox(&mut r.clear, "Clear the comments once sent");
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
                        None => {}
                    }
                    if ui.button("Cancel").clicked() {
                        close = true;
                    }
                });
            });
            if m.should_close() {
                close = true;
            }
            match (action, r.agent_id.clone()) {
                (Some(now), Some(agent_id)) => {
                    let pending = PendingReview {
                        worktree: r.worktree.clone(),
                        agent_id,
                        text: diff::review_prompt(&comments, &r.summary),
                        clear: r.clear,
                    };
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

/// A split row's two panes.
fn halves(rect: Rect) -> (Rect, Rect) {
    let half = rect.width() / 2.0;
    let left = Rect::from_min_size(rect.min, Vec2::new(half - 1.0, rect.height()));
    let right =
        Rect::from_min_size(Pos2::new(rect.left() + half + 1.0, rect.top()), Vec2::new(half - 1.0, rect.height()));
    (left, right)
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

fn draw_cell(
    p: &egui::Painter,
    t: &crate::theme::Theme,
    font: &FontId,
    rect: Rect,
    row: &diff::Row,
    numbers: bool,
    hoff: f32,
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
            if numbers {
                draw_number(p, t, rect, rect.left() + NUM_W - 6.0, row.old);
                draw_number(p, t, rect, rect.left() + 2.0 * NUM_W - 6.0, row.new);
            }
            let x = rect.left() + if numbers { 2.0 * NUM_W } else { NUM_W };
            let mark = match row.kind {
                RowKind::Add => "+",
                RowKind::Del => "-",
                _ => " ",
            };
            p.text(
                Pos2::new(x, y),
                Align2::LEFT_CENTER,
                mark,
                font.clone(),
                if sign == Color32::TRANSPARENT { t.muted } else { sign },
            );
            let code = Rect::from_min_max(Pos2::new(x + 12.0, rect.top()), rect.max);
            draw_code(p, t, font, code, row, hoff);
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
    hoff: f32,
) {
    let painter = p.with_clip_rect(rect.intersect(p.clip_rect()));
    let (bg, _) = row_colors(row.kind);
    if bg != Color32::TRANSPARENT {
        painter.rect_filled(rect, 0.0, bg);
    }
    draw_number(&painter, t, rect, rect.left() + NUM_W - 6.0, if old { row.old } else { row.new });
    let code = Rect::from_min_max(Pos2::new(rect.left() + NUM_W, rect.top()), rect.max);
    draw_code(&painter, t, font, code, row, hoff);
}

/// A line's code, scrolled `hoff` to the left and clipped to `code`, so
/// it never runs over the line numbers or the other pane.
fn draw_code(p: &egui::Painter, t: &crate::theme::Theme, font: &FontId, code: Rect, row: &diff::Row, hoff: f32) {
    p.with_clip_rect(code.intersect(p.clip_rect())).text(
        Pos2::new(code.left() - hoff, code.center().y),
        Align2::LEFT_CENTER,
        row.text.replace('\t', "    "),
        font.clone(),
        t.text,
    );
}

/// The flat row list: a header per file, its rows (or pairs), and each
/// thread under its (last) line, a row per line of it (in split, under the
/// pair holding it). `asking`: the threads waiting for an answer.
/// The rows, with the uncommitted files (under their heading) when `uncommitted`.
pub fn build_rows(d: &Diff, split: bool, comments: &[Comment], asking: &[String], uncommitted: bool) -> Vec<DRow> {
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
                out.push(DRow::Pair(fi, pair));
                out.extend(threads);
            }
        } else {
            for ri in 0..f.rows.len() {
                out.push(DRow::Row(fi, ri));
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
        let unified = build_rows(&d, false, &[c1.clone(), c2.clone()], &[], true);
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
        let split = build_rows(&d, true, &[c1.clone(), c2.clone()], &[], true);
        assert_eq!(split.iter().filter(|r| matches!(r, DRow::Comment(..))).count(), 2, "the context comment once");
        assert_eq!(split.len(), 1 + 3 + 2);
    }

    fn pr_base() -> DiffBase {
        DiffBase {
            ref_: "origin/PROJ-25996-intake".into(),
            branch: Some("PROJ-25996-intake".into()),
            source: "pr".into(),
            merge_base: "0123456789abcdef".into(),
            pr_number: Some(20163),
        }
    }

    #[test]
    fn the_base_label_says_where_the_base_came_from() {
        assert_eq!(base_label(&pr_base()), "PR #20163 base");
        assert_eq!(base_label(&DiffBase { pr_number: None, ..pr_base() }), "PR base");
        assert_eq!(base_label(&DiffBase { source: "default".into(), ..pr_base() }), "default branch");
        assert_eq!(base_label(&DiffBase { source: "override".into(), ..pr_base() }), "override");
        assert_eq!(base_label(&DiffBase { source: "upstream".into(), ..pr_base() }), "upstream");
    }

    #[test]
    fn the_base_field_shows_the_resolved_base_and_takes_an_override() {
        let mut d = DiffState::loaded("/r/f", "", Diff::default());
        d.applied(Some(pr_base()), None, None);
        assert_eq!(d.base, "origin/PROJ-25996-intake", "the field shows what is used");
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
        let rows = build_rows(&d, false, std::slice::from_ref(&c), &[], true);
        let at = rows.iter().position(|r| *r == DRow::Uncommitted).unwrap();
        assert_eq!(rows[at - 1], DRow::Row(0, 2));
        assert_eq!(rows[at + 1], DRow::File(1));
        // The comment sits under the uncommitted copy of the line only.
        let under = rows.iter().position(|r| matches!(r, DRow::Comment(..))).unwrap();
        assert_eq!(rows[under - 1], DRow::Row(1, 2));
        let hidden = build_rows(&d, false, std::slice::from_ref(&c), &[], false);
        assert_eq!(hidden, rows[..at].to_vec());
    }
}
