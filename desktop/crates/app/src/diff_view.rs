//! The diff viewer and review (ui-plan §3d / §4c, the web's U5): a base,
//! unified or split rows in one virtualised list of fixed-height rows, a
//! file list that jumps, comments on lines (kept in the app's state file),
//! and "Submit review" to an agent, now or when it is idle.

use std::sync::Arc;

use eframe::egui::{
    self, Align, Align2, Color32, FontId, Key, Layout, Modifiers, Pos2, Rect, RichText, Sense, TextEdit, Ui, Vec2,
};
use lazyagent_proto::types::DiffResult;
use serde_json::json;

use crate::app::{App, Severity, State};
use crate::diff::{self, Comment, Diff, Pair, RowKind};
use crate::store::{self, ORCHESTRATOR_KEY};

pub const ROW_H: f32 = 18.0;
const MAX_BYTES: u64 = 4 * 1024 * 1024;
const NUM_W: f32 = 44.0;

#[derive(Debug, Clone, PartialEq)]
pub enum DRow {
    File(usize),
    Row(usize, usize),
    Pair(usize, Pair),
    Comment(usize, String),
}

pub struct DiffState {
    pub path: String,
    pub base: String,
    pub loading: bool,
    pub error: Option<String>,
    pub diff: Arc<Diff>,
    pub rows: Vec<DRow>,
    /// Rebuild `rows` when the layout or the comments change.
    built_for: Option<(bool, u64)>,
    /// Which load a reply belongs to.
    seq: u64,
    pub scroll_to: Option<usize>,
}

impl DiffState {
    /// A diff already parsed (tests, benches).
    #[cfg(test)]
    pub fn loaded(path: &str, base: &str, diff: Diff) -> DiffState {
        DiffState {
            path: path.into(),
            base: base.into(),
            loading: false,
            error: None,
            diff: Arc::new(diff),
            rows: vec![],
            built_for: None,
            seq: 1,
            scroll_to: None,
        }
    }
}

pub struct CommentEdit {
    pub worktree: String,
    pub comment: Comment,
    pub existing: bool,
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
            let main = self.store.worktree(&path).is_some_and(|w| w.is_main);
            // The main worktree diffs its working tree; a branch, its commits too.
            let base = if main {
                String::new()
            } else {
                crate::dialogs::default_base(self.store.config.as_ref(), &self.store.worktrees)
            };
            self.load_diff(&path, base);
        }
    }

    pub fn load_diff(&mut self, path: &str, base: String) {
        let seq = self.diff.as_ref().map_or(1, |d| d.seq + 1);
        self.diff = Some(DiffState {
            path: path.to_string(),
            base: base.clone(),
            loading: true,
            error: None,
            diff: Arc::new(Diff::default()),
            rows: vec![],
            built_for: None,
            seq,
            scroll_to: None,
        });
        let mut params = json!({"worktree_path": path, "max_bytes": MAX_BYTES});
        if !base.trim().is_empty() {
            params["base"] = json!(base.trim());
        }
        let conn = self.conn.clone();
        self.conn.request("get_diff", params, move |s, reply| {
            let text = match reply.and_then(|v| {
                serde_json::from_value::<DiffResult>(v).map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => r.diff,
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
                let parsed = Arc::new(diff::parse(&text));
                conn2.send(crate::conn::Msg::Apply(Box::new(move |s: &mut State| {
                    if let Some(d) = s.diff.as_mut().filter(|d| d.seq == seq) {
                        d.loading = false;
                        d.diff = parsed;
                        d.built_for = None;
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
                    s.set_comments(&worktree, &[]);
                }
            }
            Err(e) => s.toast(Severity::Error, format!("The review was not sent: {e}")),
        });
    }
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
        // Toolbar.
        let mut reload = None;
        ui.horizontal(|ui| {
            ui.label(RichText::new("Base").color(t.muted));
            let d = self.state.diff.as_mut().unwrap();
            let edit = ui.add(TextEdit::singleline(&mut d.base).desired_width(140.0).hint_text("working tree"));
            if edit.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter)) {
                reload = Some(d.base.clone());
            }
            if ui.button("↻").on_hover_text("Diff again").clicked() {
                reload = Some(d.base.clone());
            }
            let mut s = split;
            ui.selectable_value(&mut s, false, "Unified");
            ui.selectable_value(&mut s, true, "Split");
            if s != split {
                self.state.saved.diff_split = s;
                self.state.touch_saved();
            }
            let d = self.state.diff.as_ref().unwrap();
            let (files, add, del) = (
                d.diff.files.len(),
                d.diff.files.iter().map(|f| f.added).sum::<u32>(),
                d.diff.files.iter().map(|f| f.removed).sum::<u32>(),
            );
            ui.label(RichText::new(format!("{files} files")).color(t.muted));
            ui.label(RichText::new(format!("+{add}")).color(t.green));
            ui.label(RichText::new(format!("−{del}")).color(t.red));
            if d.diff.truncated {
                ui.label(RichText::new("Diff truncated at 4 MiB").color(t.yellow));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("Back (Ctrl+D)").clicked() {
                    self.state.toggle_diff();
                }
                let label = format!("Submit review ({})", comments.len());
                if ui.add_enabled(!comments.is_empty(), egui::Button::new(label)).clicked() {
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
        if let Some(base) = reload {
            self.state.load_diff(&path, base);
            return;
        }
        let d = self.state.diff.as_ref().unwrap();
        if d.loading {
            ui.spinner();
            return;
        }
        if let Some(e) = &d.error {
            ui.label(RichText::new(e).color(t.red));
            return;
        }
        if d.diff.files.is_empty() {
            ui.label(RichText::new("No changes").color(t.muted));
            return;
        }
        // Rows for this layout and these comments.
        let key = (split, self.state.comments_gen);
        if self.state.diff.as_ref().unwrap().built_for != Some(key) {
            let d = self.state.diff.as_mut().unwrap();
            d.rows = build_rows(&d.diff, split, &comments);
            d.built_for = Some(key);
        }
        let body = ui.available_rect_before_wrap();
        let list_w = 220.0f32.min(body.width() * 0.25);
        let list = Rect::from_min_size(body.min, Vec2::new(list_w, body.height()));
        let rows_rect = Rect::from_min_max(Pos2::new(list.max.x + 6.0, body.min.y), body.max);
        let mut list_ui = ui.new_child(egui::UiBuilder::new().max_rect(list).id_salt("diff-files"));
        let mut jump = None;
        egui::ScrollArea::vertical().id_salt("diff-file-list").show(&mut list_ui, |ui| {
            let d = self.state.diff.as_ref().unwrap();
            for (fi, f) in d.diff.files.iter().enumerate() {
                let resp = ui
                    .horizontal(|ui| {
                        ui.add(
                            egui::Label::new(RichText::new(store::basename(&f.path)).monospace().small()).truncate(),
                        );
                        ui.label(RichText::new(format!("+{}", f.added)).small().color(t.green));
                        ui.label(RichText::new(format!("−{}", f.removed)).small().color(t.red));
                    })
                    .response
                    .interact(Sense::click())
                    .on_hover_text(&f.path);
                if resp.clicked() {
                    jump = d.rows.iter().position(|r| *r == DRow::File(fi));
                }
            }
        });
        if let Some(j) = jump {
            self.state.diff.as_mut().unwrap().scroll_to = Some(j);
        }
        let mut rows_ui = ui.new_child(egui::UiBuilder::new().max_rect(rows_rect).id_salt("diff-rows"));
        let mut area = egui::ScrollArea::vertical().id_salt(("diff-scroll", &path)).auto_shrink([false, false]);
        if let Some(j) = self.state.diff.as_mut().unwrap().scroll_to.take() {
            area = area.vertical_scroll_offset(j as f32 * ROW_H);
        }
        let mut clicked: Option<Comment> = None;
        let n = self.state.diff.as_ref().unwrap().rows.len();
        area.show_rows(&mut rows_ui, ROW_H, n, |ui, range| {
            let d = self.state.diff.as_ref().unwrap();
            let font = FontId::monospace(12.0);
            let width = ui.available_width();
            for idx in range {
                let (rect, resp) = ui.allocate_exact_size(Vec2::new(width, ROW_H), Sense::click());
                let p = ui.painter();
                let text_y = rect.center().y;
                match &d.rows[idx] {
                    DRow::File(fi) => {
                        let f = &d.diff.files[*fi];
                        p.rect_filled(rect, 0.0, t.panel);
                        let name = if f.status == "renamed" {
                            format!("{} → {}", f.old_path, f.path)
                        } else {
                            f.path.clone()
                        };
                        p.text(
                            Pos2::new(rect.left() + 6.0, text_y),
                            Align2::LEFT_CENTER,
                            format!("{name}  ({}  +{} −{})", f.status, f.added, f.removed),
                            FontId::monospace(12.0),
                            t.text,
                        );
                    }
                    DRow::Row(fi, ri) => {
                        let f = &d.diff.files[*fi];
                        let row = &f.rows[*ri];
                        draw_cell(p, &t, &font, rect, row, true);
                        if resp.clicked()
                            && resp.interact_pointer_pos().is_some_and(|pos| pos.x < rect.left() + 2.0 * NUM_W)
                        {
                            clicked = diff::anchor(&f.path, row);
                        }
                    }
                    DRow::Pair(fi, pair) => {
                        let f = &d.diff.files[*fi];
                        let half = rect.width() / 2.0;
                        let left = Rect::from_min_size(rect.min, Vec2::new(half - 1.0, ROW_H));
                        let right = Rect::from_min_size(
                            Pos2::new(rect.left() + half + 1.0, rect.top()),
                            Vec2::new(half - 1.0, ROW_H),
                        );
                        match pair {
                            Pair::Wide(ri) => draw_cell(p, &t, &font, rect, &f.rows[*ri], false),
                            Pair::Sides(l, r) => {
                                if let Some(l) = l {
                                    draw_side(p, &t, &font, left, &f.rows[*l], true);
                                }
                                if let Some(r) = r {
                                    draw_side(p, &t, &font, right, &f.rows[*r], false);
                                }
                                if resp.clicked()
                                    && let Some(pos) = resp.interact_pointer_pos()
                                {
                                    let pick = if pos.x < rect.left() + half { l } else { r };
                                    clicked = pick.and_then(|i| diff::anchor(&f.path, &f.rows[i]));
                                }
                            }
                        }
                    }
                    DRow::Comment(_, id) => {
                        let c = comments.iter().find(|c| &c.id() == id);
                        p.rect_filled(rect, 3.0, t.current_bg);
                        let text = c.map(|c| c.text.lines().next().unwrap_or("").to_string()).unwrap_or_default();
                        p.text(
                            Pos2::new(rect.left() + 2.0 * NUM_W + 6.0, text_y),
                            Align2::LEFT_CENTER,
                            format!("▸ {text}"),
                            FontId::proportional(12.5),
                            t.accent,
                        );
                        if resp.clicked()
                            && let Some(c) = c
                        {
                            self.state.comment_edit =
                                Some(CommentEdit { worktree: path.clone(), comment: c.clone(), existing: true });
                        }
                    }
                }
                if resp.hovered() {
                    ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                }
            }
        });
        if let Some(c) = clicked {
            let existing = comments.iter().find(|x| x.id() == c.id()).cloned();
            self.state.comment_edit =
                Some(CommentEdit { worktree: path, existing: existing.is_some(), comment: existing.unwrap_or(c) });
        }
    }

    /// The comment editor and the review form (modals).
    pub(crate) fn diff_dialogs(&mut self, ctx: &egui::Context) {
        let t = self.state.theme;
        if let Some(mut e) = self.state.comment_edit.take() {
            // Some(true): save; Some(false): delete.
            let mut done = None::<bool>;
            let mut cancel = false;
            let m = egui::Modal::new(egui::Id::unique("diff-comment")).show(ctx, |ui| {
                ui.set_width(460.0);
                let side = if e.comment.side == "old" { " (removed line)" } else { "" };
                ui.label(RichText::new(format!("{}:{}{side}", e.comment.path, e.comment.line)).strong());
                ui.label(RichText::new(e.comment.code.trim()).monospace().color(t.muted));
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
                    done = Some(true);
                }
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.button("Save").clicked() {
                        done = Some(true);
                    }
                    if e.existing && ui.button("Delete").clicked() {
                        done = Some(false);
                    }
                    if ui.button("Cancel").clicked() {
                        cancel = true;
                    }
                });
            });
            cancel |= m.should_close();
            match done {
                Some(save) => {
                    let mut all = self.state.comments(&e.worktree);
                    all.retain(|c| c.id() != e.comment.id());
                    if save && !e.comment.text.trim().is_empty() {
                        all.push(e.comment.clone());
                    }
                    self.state.set_comments(&e.worktree, &all);
                }
                None if !cancel => self.state.comment_edit = Some(e),
                None => {}
            }
        }
        if let Some(mut r) = self.state.review.take() {
            let comments = self.state.comments(&r.worktree);
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

fn draw_number(p: &egui::Painter, t: &crate::theme::Theme, rect: Rect, x: f32, n: Option<u32>) {
    if let Some(n) = n {
        p.text(Pos2::new(x, rect.center().y), Align2::RIGHT_CENTER, n.to_string(), FontId::monospace(11.0), t.muted);
    }
}

fn draw_cell(p: &egui::Painter, t: &crate::theme::Theme, font: &FontId, rect: Rect, row: &diff::Row, numbers: bool) {
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
            p.text(Pos2::new(x + 12.0, y), Align2::LEFT_CENTER, row.text.replace('\t', "    "), font.clone(), t.text);
        }
    }
}

fn draw_side(p: &egui::Painter, t: &crate::theme::Theme, font: &FontId, rect: Rect, row: &diff::Row, old: bool) {
    let painter = p.with_clip_rect(rect);
    let (bg, _) = row_colors(row.kind);
    if bg != Color32::TRANSPARENT {
        painter.rect_filled(rect, 0.0, bg);
    }
    draw_number(&painter, t, rect, rect.left() + NUM_W - 6.0, if old { row.old } else { row.new });
    draw_cell_text(&painter, t, font, rect, row);
}

fn draw_cell_text(p: &egui::Painter, t: &crate::theme::Theme, font: &FontId, rect: Rect, row: &diff::Row) {
    p.text(
        Pos2::new(rect.left() + NUM_W, rect.center().y),
        Align2::LEFT_CENTER,
        row.text.replace('\t', "    "),
        font.clone(),
        t.text,
    );
}

/// The flat row list: a header per file, its rows (or pairs), and each
/// comment under its line (in split, under the pair holding it).
pub fn build_rows(d: &Diff, split: bool, comments: &[Comment]) -> Vec<DRow> {
    let mut out = Vec::with_capacity(d.files.iter().map(|f| f.rows.len() + 1).sum());
    for (fi, f) in d.files.iter().enumerate() {
        out.push(DRow::File(fi));
        let on = |ri: usize| -> Vec<String> {
            let Some(a) = diff::anchor(&f.path, &f.rows[ri]) else { return vec![] };
            let id = a.id();
            comments.iter().filter(|c| c.id() == id).map(|c| c.id()).collect()
        };
        if split {
            for pair in diff::split_rows(&f.rows) {
                let ids: Vec<String> = match &pair {
                    Pair::Sides(l, r) => {
                        let mut ids: Vec<String> = l.map(on).unwrap_or_default();
                        // A context line is one row on both sides: one comment.
                        if l != r {
                            ids.extend(r.map(on).unwrap_or_default());
                        }
                        ids
                    }
                    Pair::Wide(_) => vec![],
                };
                out.push(DRow::Pair(fi, pair));
                out.extend(ids.into_iter().map(|id| DRow::Comment(fi, id)));
            }
        } else {
            for ri in 0..f.rows.len() {
                out.push(DRow::Row(fi, ri));
                out.extend(on(ri).into_iter().map(|id| DRow::Comment(fi, id)));
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
        let unified = build_rows(&d, false, &[c1.clone(), c2.clone()]);
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
        let split = build_rows(&d, true, &[c1.clone(), c2.clone()]);
        assert_eq!(split.iter().filter(|r| matches!(r, DRow::Comment(..))).count(), 2, "the context comment once");
        assert_eq!(split.len(), 1 + 3 + 2);
    }
}
