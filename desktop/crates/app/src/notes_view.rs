//! The session notes panel (proposal 0011, plan N4): a right side panel
//! over the active agent's notes, edited as raw Markdown (egui has no
//! Markdown widget, V8).
//!
//! Per agent the panel keeps the text it last read (`base_text` at
//! `base_rev`) and the user's `draft`. A save is `notes_put {if_rev:
//! base_rev}`, so the engine refuses it when someone wrote in between: the
//! draft is kept and the engine's text shown next to it. Nothing the user
//! typed is ever replaced without a click.
//!
//! `notes.updated` only says that a newer rev exists; the text is fetched
//! (`notes_get`) when that agent's notes are shown. A clean draft takes it
//! silently, a dirty one gets a banner.

use eframe::egui::{self, Align, Color32, CornerRadius, Key, Layout, Modifiers, RichText, Stroke, Ui};
use lazyagent_proto::ClientError;
use lazyagent_proto::types::{Notes, NotesConflict, NotesRemoved, NotesUpdated, NotesWritten};
use serde_json::json;

use crate::app::{App, Severity, State, unix_now};
use crate::store::{self, ORCHESTRATOR_KEY};
use crate::theme::Theme;

/// A worktree path (or the orchestrator key) and an agent id.
pub type NotesKey = (String, String);

/// A newer text seen while the draft was dirty.
#[derive(Debug, Clone, PartialEq)]
pub struct Incoming {
    pub rev: u64,
    pub by: Option<String>,
    pub text: String,
    /// "Keep editing": the banner hides; a save will conflict.
    pub dismissed: bool,
}

#[derive(Debug, Clone, Default)]
pub struct NotesDoc {
    /// The last `notes_get` (metadata shown in the panel; its `text` may be
    /// newer than `base_text` while an `incoming` waits).
    pub meta: Option<Notes>,
    pub base_rev: u64,
    pub base_text: String,
    pub draft: String,
    /// The newest rev an event announced.
    pub latest: u64,
    pub incoming: Option<Incoming>,
    /// A save was refused: the engine's notes as they are now.
    pub conflict: Option<NotesConflict>,
    pub loading: bool,
    pub saving: bool,
    /// Fetch again even without a newer rev (a reconnect).
    pub stale: bool,
    pub error: Option<String>,
}

impl NotesDoc {
    pub fn dirty(&self) -> bool {
        self.meta.is_some() && self.draft != self.base_text
    }

    pub fn wants_fetch(&self) -> bool {
        !self.loading
            && !self.saving
            && self.error.is_none()
            && (self.stale || self.meta.as_ref().is_none_or(|m| self.latest > m.rev))
    }

    /// A `notes_get` answered.
    pub fn loaded(&mut self, n: Notes) {
        self.loading = false;
        self.stale = false;
        self.error = None;
        self.latest = self.latest.max(n.rev);
        let first = self.meta.is_none();
        if first || !self.dirty() || n.text == self.draft {
            // Nothing of the user's to lose (or the engine has exactly it).
            self.base_rev = n.rev;
            self.base_text = n.text.clone();
            self.draft = n.text.clone();
            self.incoming = None;
        } else if n.rev > self.base_rev && self.conflict.is_none() {
            let dismissed = self.incoming.as_ref().is_some_and(|i| i.dismissed);
            self.incoming = Some(Incoming { rev: n.rev, by: n.by.clone(), text: n.text.clone(), dismissed });
        }
        self.meta = Some(n);
    }

    /// A `notes_put` of `sent` answered.
    pub fn saved(&mut self, sent: String, w: NotesWritten) {
        self.saving = false;
        self.error = None;
        self.conflict = None;
        self.base_rev = w.rev;
        self.base_text = sent;
        self.latest = self.latest.max(w.rev);
        if self.incoming.as_ref().is_some_and(|i| i.rev <= w.rev) {
            self.incoming = None;
        }
        if let Some(m) = &mut self.meta {
            m.text = self.base_text.clone();
            m.empty = false;
            (m.rev, m.size, m.max_bytes, m.by, m.updated_at) = (w.rev, w.size, w.max_bytes, w.by, w.updated_at);
        }
    }

    /// A `notes_put` failed: a conflict keeps the draft and shows theirs.
    pub fn save_failed(&mut self, e: &ClientError) {
        self.saving = false;
        match NotesConflict::of(e) {
            Some(c) => {
                self.latest = self.latest.max(c.rev);
                self.incoming = None;
                self.conflict = Some(c);
            }
            None => self.error = Some(e.to_string()),
        }
    }

    /// "Take theirs" / "Reload (lose mine)".
    pub fn take(&mut self, rev: u64, text: String) {
        self.base_rev = rev;
        self.base_text = text.clone();
        self.draft = text;
        self.conflict = None;
        self.incoming = None;
    }

    pub fn revert(&mut self) {
        self.draft = self.base_text.clone();
    }
}

/// How full the notes are.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Meter {
    Ok,
    /// From 80 % of `max_bytes` (the engine warns there too).
    Warn,
    /// At the cap (one byte more and the engine refuses the save).
    Full,
}

pub fn meter(size: usize, max: u64) -> Meter {
    let max = max.max(1) as f64;
    match size as f64 / max {
        f if f >= 1.0 => Meter::Full,
        f if f >= 0.8 => Meter::Warn,
        _ => Meter::Ok,
    }
}

pub fn meter_color(t: &Theme, m: Meter) -> Color32 {
    match m {
        Meter::Ok => t.green,
        Meter::Warn => t.yellow,
        Meter::Full => t.red,
    }
}

impl State {
    /// Whether the engine serves the notes methods (R10: an older one
    /// does not, and the panel stays hidden).
    pub fn notes_supported(&self) -> bool {
        self.capabilities.as_ref().is_none_or(|c| c.iter().any(|m| m == "notes_get"))
    }

    /// The agent whose notes the panel shows: the active tab of the selection.
    pub fn notes_key(&self) -> Option<NotesKey> {
        let place = self.selected.clone()?;
        let agent = self.tabs.get(&place).cloned().or_else(|| {
            // The orchestrator's single agent has no tab until its view is drawn.
            (place == ORCHESTRATOR_KEY).then(|| self.store.agents(&place).first().map(|a| a.agent_id.clone()))?
        })?;
        self.store.agent(&place, &agent).is_some().then_some((place, agent))
    }

    pub fn toggle_notes(&mut self) {
        if !self.notes_supported() {
            self.toast(Severity::Info, "This engine has no session notes (update lazyagent).");
            return;
        }
        self.notes_open = !self.notes_open;
        self.notes_focus = self.notes_open;
    }

    pub fn fetch_notes(&mut self, key: &NotesKey) {
        let doc = self.notes.entry(key.clone()).or_default();
        doc.loading = true;
        let k = key.clone();
        let params = json!({"worktree_path": key.0, "agent_id": key.1});
        self.conn.request("notes_get", params, move |s, reply| {
            let Some(doc) = s.notes.get_mut(&k) else { return };
            match reply.and_then(|v| serde_json::from_value::<Notes>(v).map_err(|e| ClientError::Decode(e.to_string())))
            {
                Ok(n) => doc.loaded(n),
                Err(e) => {
                    doc.loading = false;
                    doc.error = Some(e.to_string());
                }
            }
        });
    }

    /// Saves the draft over `if_rev` (the base, or theirs to overwrite).
    pub fn save_notes(&mut self, key: &NotesKey, if_rev: Option<u64>) {
        let Some(doc) = self.notes.get_mut(key) else { return };
        if doc.saving || doc.meta.is_none() {
            return;
        }
        doc.saving = true;
        doc.error = None;
        let text = doc.draft.clone();
        let params = json!({"worktree_path": key.0, "agent_id": key.1, "text": text,
                            "if_rev": if_rev.unwrap_or(doc.base_rev)});
        let k = key.clone();
        self.conn.request("notes_put", params, move |s, reply| {
            let Some(doc) = s.notes.get_mut(&k) else { return };
            match reply
                .and_then(|v| serde_json::from_value::<NotesWritten>(v).map_err(|e| ClientError::Decode(e.to_string())))
            {
                Ok(w) => doc.saved(text, w),
                Err(e) => doc.save_failed(&e),
            }
        });
    }

    pub fn on_notes_updated(&mut self, u: &NotesUpdated) {
        if let Some(doc) = self.notes.get_mut(&(u.worktree_path.clone(), u.agent_id.clone())) {
            doc.latest = doc.latest.max(u.rev);
        }
    }

    pub fn on_notes_removed(&mut self, r: &NotesRemoved) {
        let key = (r.worktree_path.clone(), r.agent_id.clone());
        let Some(doc) = self.notes.get(&key) else { return };
        if doc.meta.as_ref().is_some_and(|m| m.notes_id != r.notes_id) {
            return;
        }
        if doc.dirty() {
            let place = self.place_name(&r.worktree_path);
            self.toast(
                Severity::Warning,
                format!("Agent {} in {place} ended with unsaved edits in its notes panel; they are gone.", r.agent_id),
            );
        }
        self.notes.remove(&key);
    }

    /// After a reconnect: what is cached may be old. A new engine process
    /// ran nothing of the old one; notes nobody edits are dropped.
    pub fn notes_reconnected(&mut self, engine_changed: bool) {
        if engine_changed {
            self.notes.retain(|_, d| d.dirty());
        }
        for d in self.notes.values_mut() {
            d.stale = true;
            d.error = None;
            d.loading = false;
            d.saving = false;
        }
    }
}

impl App {
    pub(crate) fn notes_panel(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let key = self.state.notes_key();
        let title = key.as_ref().and_then(|(p, a)| self.state.store.agent(p, a)).map(store::agent_label);
        ui.horizontal(|ui| {
            ui.label(RichText::new("Session notes").strong());
            if let Some(title) = &title {
                ui.add(egui::Label::new(RichText::new(title).color(t.muted)).truncate());
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("✖").on_hover_text("Close (n)").clicked() {
                    self.state.notes_open = false;
                }
            });
        });
        ui.separator();
        let Some(key) = key else {
            ui.label(RichText::new("No agent here: notes belong to an agent.").color(t.muted));
            return;
        };
        if self.state.notes.get(&key).is_none_or(NotesDoc::wants_fetch) {
            self.state.fetch_notes(&key);
        }
        let doc = self.state.notes.get(&key).cloned().unwrap_or_default();
        if let Some(e) = &doc.error {
            ui.label(RichText::new(e).color(t.red));
            if ui.button("Try again").clicked()
                && let Some(d) = self.state.notes.get_mut(&key)
            {
                d.error = None;
            }
            if doc.meta.is_none() {
                return;
            }
        }
        let Some(meta) = doc.meta.clone() else {
            ui.spinner();
            return;
        };
        let mut action = None::<Action>;
        // Bottom up: the meter and the buttons keep their room.
        egui::Panel::bottom(egui::Id::unique(("notes-footer", &key)))
            .frame(egui::Frame::new().inner_margin(egui::Margin::symmetric(0, 6)))
            .show(ui, |ui| footer(ui, &t, &doc, &meta, &mut action));
        if let Some(instruction) = meta.instruction.as_deref().filter(|i| !i.trim().is_empty()) {
            egui::CollapsingHeader::new(RichText::new("Original instruction (pinned)").small().color(t.muted))
                .id_salt(("notes-instruction", &key))
                .default_open(true)
                .show(ui, |ui| {
                    egui::ScrollArea::vertical().max_height(90.0).id_salt(("notes-instruction-scroll", &key)).show(
                        ui,
                        |ui| {
                            ui.add(egui::Label::new(RichText::new(instruction).color(t.text)).wrap().selectable(true));
                        },
                    );
                });
        }
        if let Some(i) = doc.incoming.as_ref().filter(|i| !i.dismissed) {
            banner(ui, t.yellow, &t, |ui| {
                let by = i.by.as_deref().unwrap_or("someone");
                ui.label(RichText::new(format!("Changed by {by} while you edit (rev {})", i.rev)).color(t.text));
                ui.horizontal(|ui| {
                    if ui.button("Reload (lose mine)").clicked() {
                        action = Some(Action::Take(i.rev, i.text.clone()));
                    }
                    if ui.button("Keep editing (save will conflict)").clicked() {
                        action = Some(Action::KeepEditing);
                    }
                });
            });
        }
        if let Some(c) = &doc.conflict {
            banner(ui, t.red, &t, |ui| {
                ui.label(
                    RichText::new(format!(
                        "Not saved: the notes changed (rev {}). Yours are kept above theirs.",
                        c.rev
                    ))
                    .color(t.text),
                );
                ui.horizontal(|ui| {
                    if ui.button("Overwrite with mine").clicked() {
                        action = Some(Action::Overwrite(c.rev));
                    }
                    if ui.button("Take theirs").clicked() {
                        action = Some(Action::Take(c.rev, c.text.clone()));
                    }
                });
            });
        }
        let editor_height = if doc.conflict.is_some() { ui.available_height() * 0.55 } else { ui.available_height() };
        let draft_id = egui::Id::unique(("notes-draft", &key));
        let focus = std::mem::take(&mut self.state.notes_focus);
        if let Some(d) = self.state.notes.get_mut(&key) {
            egui::ScrollArea::vertical().id_salt(("notes-editor", &key)).max_height(editor_height).show(ui, |ui| {
                let edit = egui::TextEdit::multiline(&mut d.draft)
                    .id(draft_id)
                    .code_editor()
                    .desired_width(f32::INFINITY)
                    .desired_rows(((editor_height / 15.0) as usize).max(4))
                    .hint_text("Goal, constraints, decisions, doubts, pending (Markdown)")
                    .show(ui);
                if focus {
                    edit.response.request_focus();
                }
            });
        }
        if let Some(c) = &doc.conflict {
            ui.add_space(4.0);
            ui.label(RichText::new(format!("Theirs (rev {})", c.rev)).small().color(t.muted));
            egui::Frame::new().fill(t.panel).stroke(Stroke::new(1.0, t.border)).show(ui, |ui| {
                egui::ScrollArea::vertical().id_salt(("notes-theirs", &key)).show(ui, |ui| {
                    ui.add(egui::Label::new(RichText::new(&c.text).monospace().color(t.text)).wrap().selectable(true));
                });
            });
        }
        if ui.input_mut(|i| i.consume_key(Modifiers::COMMAND, Key::S)) && doc.dirty() {
            action = Some(Action::Save);
        }
        match action {
            None => {}
            Some(Action::Save) => self.state.save_notes(&key, None),
            Some(Action::Overwrite(rev)) => self.state.save_notes(&key, Some(rev)),
            Some(Action::Revert) => {
                if let Some(d) = self.state.notes.get_mut(&key) {
                    d.revert();
                }
            }
            Some(Action::KeepEditing) => {
                if let Some(i) = self.state.notes.get_mut(&key).and_then(|d| d.incoming.as_mut()) {
                    i.dismissed = true;
                }
            }
            Some(Action::Take(rev, text)) => {
                if let Some(d) = self.state.notes.get_mut(&key) {
                    d.take(rev, text);
                }
            }
        }
    }
}

enum Action {
    Save,
    Revert,
    Overwrite(u64),
    Take(u64, String),
    KeepEditing,
}

fn banner(ui: &mut Ui, color: Color32, t: &Theme, body: impl FnOnce(&mut Ui)) {
    egui::Frame::new()
        .fill(t.panel)
        .stroke(Stroke::new(1.0, color))
        .corner_radius(CornerRadius::same(4))
        .inner_margin(egui::Margin::symmetric(8, 6))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            body(ui)
        });
    ui.add_space(4.0);
}

fn footer(ui: &mut Ui, t: &Theme, doc: &NotesDoc, meta: &Notes, action: &mut Option<Action>) {
    let size = doc.draft.len();
    let level = meter(size, meta.max_bytes);
    let color = meter_color(t, level);
    let over = size as u64 > meta.max_bytes;
    let frac = (size as f32 / meta.max_bytes.max(1) as f32).min(1.0);
    let (rect, _) = ui.allocate_exact_size(egui::vec2(ui.available_width(), 4.0), egui::Sense::hover());
    ui.painter().rect_filled(rect, 2.0, t.border);
    ui.painter().rect_filled(egui::Rect::from_min_size(rect.min, egui::vec2(rect.width() * frac, 4.0)), 2.0, color);
    ui.horizontal(|ui| {
        ui.label(RichText::new(format!("{size} / {} bytes", meta.max_bytes)).small().color(color));
        if over {
            ui.label(RichText::new("over the cap: condense them").small().color(t.red));
        }
    });
    let edited = match &meta.by {
        Some(by) => format!(
            "last edit {} ago by {by} · rev {}",
            crate::attention_view::age(meta.updated_at, unix_now()),
            meta.rev
        ),
        None => "not edited yet".to_string(),
    };
    ui.label(RichText::new(format!("{edited} · mode {}", meta.mode)).small().color(t.muted));
    ui.horizontal(|ui| {
        let dirty = doc.dirty();
        let save = ui.add_enabled(dirty && !doc.saving && !over, egui::Button::new("Save"));
        if save.on_hover_text("Ctrl+S").clicked() {
            *action = Some(Action::Save);
        }
        if ui.add_enabled(dirty, egui::Button::new("Revert")).on_hover_text("Back to the saved notes").clicked() {
            *action = Some(Action::Revert);
        }
        if doc.saving {
            ui.spinner();
        } else if dirty {
            ui.label(RichText::new("unsaved").small().color(t.yellow));
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn notes(rev: u64, text: &str, by: Option<&str>) -> Notes {
        serde_json::from_value(json!({
            "worktree_path": "/r", "agent_id": "a1", "notes_id": "n1", "rev": rev, "size": text.len(),
            "max_bytes": 100, "by": by, "updated_at": 1.0, "text": text, "state": "live", "kind": "agent",
            "mode": "manual", "conversation_id": null, "instruction": null, "created_at": 1.0, "ended_at": null,
            "empty": false
        }))
        .unwrap()
    }

    fn written(rev: u64) -> NotesWritten {
        serde_json::from_value(json!({"worktree_path": "/r", "agent_id": "a1", "notes_id": "n1", "rev": rev,
            "size": 1, "max_bytes": 100, "by": "user", "updated_at": 2.0}))
        .unwrap()
    }

    #[test]
    fn a_clean_draft_follows_and_a_dirty_one_is_kept() {
        let mut d = NotesDoc::default();
        assert!(d.wants_fetch());
        d.loaded(notes(0, "a", None));
        assert!(!d.dirty() && !d.wants_fetch());
        d.latest = 1;
        assert!(d.wants_fetch());
        d.loaded(notes(1, "b", Some("agent")));
        assert_eq!((d.draft.as_str(), d.base_rev), ("b", 1), "clean: reloaded silently");
        d.draft.push_str(" mine");
        d.loaded(notes(2, "c", Some("orchestrator")));
        assert_eq!(d.draft, "b mine", "dirty: kept");
        assert_eq!(d.base_rev, 1, "a save will still say if_rev 1");
        let i = d.incoming.clone().unwrap();
        assert_eq!((i.rev, i.by.as_deref(), i.text.as_str()), (2, Some("orchestrator"), "c"));
        // The engine already has exactly the draft (our own save's echo).
        d.loaded(notes(3, "b mine", Some("user")));
        assert!(!d.dirty() && d.incoming.is_none());
        assert_eq!(d.base_rev, 3);
    }

    #[test]
    fn save_conflict_and_the_ways_out() {
        let mut d = NotesDoc::default();
        d.loaded(notes(0, "base", None));
        d.draft = "mine".into();
        d.saving = true;
        assert!(!d.wants_fetch(), "no fetch while a save is in flight");
        d.saved("mine".into(), written(1));
        assert!(!d.dirty());
        assert_eq!((d.base_rev, d.meta.as_ref().unwrap().by.as_deref()), (1, Some("user")));
        d.draft = "mine 2".into();
        let conflict = ClientError::Rpc(lazyagent_proto::RpcError {
            code: "CONFLICT".into(),
            message: "notes changed (rev 4): read them again".into(),
            data: Some(json!({"rev": 4, "text": "theirs"})),
        });
        d.save_failed(&conflict);
        assert_eq!(d.draft, "mine 2", "a conflict never touches the draft");
        assert_eq!(d.conflict.as_ref().map(|c| (c.rev, c.text.as_str())), Some((4, "theirs")));
        // A fetch meanwhile does not add a banner over the conflict view.
        d.loaded(notes(4, "theirs", Some("agent")));
        assert!(d.incoming.is_none() && d.draft == "mine 2");
        d.take(4, "theirs".into());
        assert_eq!((d.draft.as_str(), d.base_rev, d.dirty()), ("theirs", 4, false));
        d.save_failed(&ClientError::Rpc(lazyagent_proto::RpcError {
            code: "VALIDATION_ERROR".into(),
            message: "notes would be 200 bytes (max 100): condense them".into(),
            data: None,
        }));
        assert!(d.error.as_deref().is_some_and(|e| e.contains("condense")));
    }

    #[test]
    fn meter_levels() {
        assert_eq!(meter(79, 100), Meter::Ok);
        assert_eq!(meter(80, 100), Meter::Warn);
        assert_eq!(meter(99, 100), Meter::Warn);
        assert_eq!(meter(100, 100), Meter::Full);
        assert_eq!(meter(5000, 100), Meter::Full);
        assert_eq!(meter(0, 0), Meter::Ok);
    }
}
