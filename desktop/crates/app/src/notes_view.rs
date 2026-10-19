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
//!
//! Below the editor, "Earlier notes in this worktree" lists the notes of
//! its ended agents (`notes_list`: orphans of an agent that exited, a
//! restorable agent's): View, Adopt into the shown agent (`notes_adopt`),
//! Delete (plan N5). The stop dialog's handoff and the spawn dialog's
//! warning are in dialogs.rs.

use eframe::egui::{self, Align, Color32, CornerRadius, Key, Layout, Modifiers, RichText, Stroke, Ui};
use lazyagent_proto::ClientError;
use lazyagent_proto::types::{
    Notes, NotesConflict, NotesList, NotesListItem, NotesRemoved, NotesUpdated, NotesWritten,
};
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
    /// Text that could not be written (a carry from the spawn dialog): the
    /// first load makes it the draft, unsaved, so it is not lost.
    pub carried: Option<String>,
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
            self.draft = self.carried.take().unwrap_or_else(|| n.text.clone());
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

/// A worktree's ended notes: orphans (their agent exited, or was stopped
/// with "keep") and restorable agents' (an earlier engine's).
#[derive(Debug, Clone, Default)]
pub struct Earlier {
    /// Newest first, as `notes_list` answers; never live notes.
    pub items: Vec<NotesListItem>,
    pub loaded: bool,
    pub loading: bool,
    /// Fetch again (a notes or restorable event, a reconnect).
    pub stale: bool,
    pub error: Option<String>,
    /// View: the notes shown read-only, and their text once fetched.
    pub viewing: Option<(String, Option<String>)>,
    /// An Adopt or Delete waiting for its confirmation.
    pub confirm: Option<(Confirm, String)>,
    /// Orphans to announce once listed (their expiry is in the list).
    pub announce: Vec<String>,
    /// The request whose reply counts (a reconnect drops older ones).
    pub seq: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Confirm {
    Adopt,
    Delete,
}

impl Earlier {
    pub fn wants_fetch(&self) -> bool {
        !self.loading && self.error.is_none() && (!self.loaded || self.stale)
    }

    /// The newest ended notes: what a new conversation would delete.
    pub fn newest(&self) -> Option<&NotesListItem> {
        self.items.first()
    }
}

/// "Agent a2", or "the orchestrator".
pub fn ended_label(place: &str, agent_id: &str) -> String {
    if place == ORCHESTRATOR_KEY { "the orchestrator".into() } else { format!("Agent {agent_id}") }
}

/// "orphan, 1.2 KiB, ended 3h ago" for one listed item.
pub fn ended_summary(item: &NotesListItem, now: f64) -> String {
    let size =
        if item.size >= 1024 { format!("{:.1} KiB", item.size as f64 / 1024.0) } else { format!("{} B", item.size) };
    let ended = item.ended_at.unwrap_or(item.updated_at);
    let mut s = format!("{}, {size}, ended {} ago", item.state, span(ended, now));
    if let Some(at) = item.expires_at {
        s.push_str(&format!(", deleted in {}", span(now, at.max(now))));
    }
    s
}

/// `age`, in days from two days up (an orphan lives a week).
fn span(from: f64, to: f64) -> String {
    let secs = to - from;
    if secs >= 2.0 * 86_400.0 {
        format!("{}d", (secs / 86_400.0).round())
    } else {
        crate::attention_view::age(from, to)
    }
}

/// Whole days left before `expires_at` (at least 1 while it is ahead).
pub fn days_left(expires_at: f64, now: f64) -> u64 {
    ((expires_at - now) / 86_400.0).round().max(1.0) as u64
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
        // Any reason changes the worktree's ended notes: one more (orphaned)
        // or one fewer (adopted, deleted, pruned).
        let visible = self.view == crate::app::View::Worktrees && self.selected.as_ref() == Some(&r.worktree_path);
        let announce = r.reason == "orphaned" && visible && self.notes_supported();
        let earlier = self.earlier.entry(r.worktree_path.clone()).or_default();
        earlier.stale = true;
        if announce {
            earlier.announce.push(r.notes_id.clone());
            let place = r.worktree_path.clone();
            self.fetch_earlier(&place);
        }
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
        for e in self.earlier.values_mut() {
            // Replies of the old connection no longer count.
            e.seq += 1;
            (e.stale, e.loading, e.error) = (true, false, None);
        }
    }

    /// Every worktree's ended notes may have changed (restorable agents
    /// came or went).
    pub fn earlier_changed(&mut self) {
        for e in self.earlier.values_mut() {
            (e.stale, e.error) = (true, None);
        }
    }

    /// One worktree's ended notes may have changed (a spawn there may have
    /// revived or pruned some).
    pub fn earlier_changed_in(&mut self, place: &str) {
        if let Some(e) = self.earlier.get_mut(place) {
            (e.stale, e.error) = (true, None);
        }
    }

    /// `notes_list` of one worktree (or the orchestrator), without live notes.
    pub fn fetch_earlier(&mut self, place: &str) {
        let e = self.earlier.entry(place.to_string()).or_default();
        if e.loading {
            // A change meanwhile stays `stale`: fetched again after this one.
            return;
        }
        e.seq += 1;
        (e.loading, e.stale) = (true, false);
        let (p, seq) = (place.to_string(), e.seq);
        self.conn.request("notes_list", json!({"worktree_path": place}), move |s, reply| {
            let Some(e) = s.earlier.get_mut(&p).filter(|e| e.seq == seq) else { return };
            e.loading = false;
            match reply
                .and_then(|v| serde_json::from_value::<NotesList>(v).map_err(|e| ClientError::Decode(e.to_string())))
            {
                Ok(list) => {
                    e.items = list.notes.into_iter().filter(|n| n.state != "live").collect();
                    (e.loaded, e.error) = (true, None);
                    if e.viewing.as_ref().is_some_and(|(id, _)| !e.items.iter().any(|n| &n.notes_id == id)) {
                        e.viewing = None;
                    }
                    if e.confirm.as_ref().is_some_and(|(_, id)| !e.items.iter().any(|n| &n.notes_id == id)) {
                        e.confirm = None;
                    }
                    let now = unix_now();
                    let announced: Vec<NotesListItem> = std::mem::take(&mut e.announce)
                        .iter()
                        .filter_map(|id| e.items.iter().find(|n| &n.notes_id == id).cloned())
                        .collect();
                    for n in announced {
                        s.announce_orphan(&p, &n, now);
                    }
                }
                Err(err) => {
                    e.error = Some(err.to_string());
                    e.announce.clear();
                }
            }
        });
    }

    /// "Agent a2 exited; its notes are kept 7 days" (a template-only one is
    /// pruned at once and never listed, so never announced).
    fn announce_orphan(&mut self, place: &str, n: &NotesListItem, now: f64) {
        let exited = self.exits.iter().any(|x| x.key == place && x.agent_id == n.agent_id);
        let who = if place == ORCHESTRATOR_KEY { "The orchestrator".into() } else { ended_label(place, &n.agent_id) };
        let how = if exited { "exited" } else { "stopped" };
        let kept = match n.expires_at {
            Some(at) => {
                let days = days_left(at, now);
                format!("kept {days} day{}", if days == 1 { "" } else { "s" })
            }
            None => "kept".into(),
        };
        self.toast(Severity::Info, format!("{who} {how}; its notes are {kept} (Notes, n: Earlier notes)."));
    }

    /// The text of ended notes, for View.
    pub fn view_earlier(&mut self, place: &str, notes_id: &str) {
        let Some(e) = self.earlier.get_mut(place) else { return };
        if e.viewing.as_ref().is_some_and(|(id, _)| id == notes_id) {
            e.viewing = None;
            return;
        }
        e.viewing = Some((notes_id.to_string(), None));
        let (p, id) = (place.to_string(), notes_id.to_string());
        self.conn.request("notes_get", json!({"notes_id": notes_id}), move |s, reply| {
            let text = reply
                .and_then(|v| serde_json::from_value::<Notes>(v).map_err(|e| ClientError::Decode(e.to_string())))
                .map(|n| n.text)
                .unwrap_or_else(|e| format!("(could not read them: {e})"));
            if let Some(e) = s.earlier.get_mut(&p)
                && let Some((viewing, t)) = &mut e.viewing
                && *viewing == id
            {
                *t = Some(text);
            }
        });
    }

    /// Adopt: the ended notes replace the live agent's (its notes id and
    /// path stay, so its hooks follow) and go from the list.
    pub fn adopt_earlier(&mut self, key: &NotesKey, notes_id: &str) {
        let mut params = json!({"notes_id": notes_id, "worktree_path": key.0, "agent_id": key.1});
        if let Some(d) = self.notes.get(key).filter(|d| d.meta.is_some()) {
            params["if_rev"] = json!(d.base_rev);
        }
        let k = key.clone();
        self.conn.request("notes_adopt", params, move |s, reply| {
            match reply
                .and_then(|v| serde_json::from_value::<NotesWritten>(v).map_err(|e| ClientError::Decode(e.to_string())))
            {
                Ok(w) => {
                    // Confirmed "replace": a draft goes, the adopted text loads.
                    if let Some(d) = s.notes.get_mut(&k) {
                        d.revert();
                        d.incoming = None;
                        d.latest = d.latest.max(w.rev);
                    }
                    s.toast(Severity::Info, "Adopted the earlier notes.");
                }
                Err(e) => {
                    if let Some(d) = s.notes.get_mut(&k) {
                        d.stale = true;
                    }
                    let why = if NotesConflict::of(&e).is_some() {
                        "this agent's notes changed meanwhile: look at them and try again".to_string()
                    } else {
                        e.to_string()
                    };
                    s.toast(Severity::Error, format!("Could not adopt the notes: {why}"));
                }
            }
            if let Some(e) = s.earlier.get_mut(&k.0) {
                e.stale = true;
            }
        });
    }

    pub fn delete_earlier(&mut self, place: &str, notes_id: &str) {
        let p = place.to_string();
        self.conn.request("notes_delete", json!({"notes_id": notes_id}), move |s, reply| {
            if let Err(e) = reply {
                s.toast(Severity::Error, format!("Could not delete the notes: {e}"));
            }
            if let Some(e) = s.earlier.get_mut(&p) {
                e.stale = true;
            }
        });
    }

    /// The spawn dialog's carry: the earlier notes, read before the spawn
    /// deleted them (rule 5), go into the new agent's fresh notes (rev 0).
    /// If that fails, they wait in the panel as an unsaved draft.
    pub fn carry_notes(&mut self, key: NotesKey, text: String) {
        let params = json!({"worktree_path": key.0, "agent_id": key.1, "text": text, "if_rev": 0});
        self.conn.request("notes_put", params, move |s, reply| match reply {
            Ok(_) => s.toast(Severity::Info, "Carried the earlier notes into the new agent."),
            Err(e) => {
                let d = s.notes.entry(key).or_default();
                if d.meta.is_some() {
                    d.draft = text;
                } else {
                    d.carried = Some(text);
                }
                s.notes_open = true;
                s.toast(
                    Severity::Warning,
                    format!("Could not carry the earlier notes ({e}); they are in the notes panel, unsaved."),
                );
            }
        });
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
        let place = self.state.selected.clone().unwrap_or_default();
        if !place.is_empty() && self.state.earlier.get(&place).is_none_or(Earlier::wants_fetch) {
            self.state.fetch_earlier(&place);
        }
        let Some(key) = key else {
            ui.label(RichText::new("No agent here: notes belong to an agent.").color(t.muted));
            ui.add_space(6.0);
            self.earlier_section(ui, &place, None);
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
        // The earlier notes get a fixed share at the bottom: their list scrolls.
        let has_earlier = self.state.earlier.get(&place).is_some_and(|e| !e.items.is_empty());
        let earlier_height = if has_earlier { (ui.available_height() * 0.4).min(280.0) } else { 0.0 };
        let rest = ui.available_height() - earlier_height;
        let editor_height = if doc.conflict.is_some() { rest * 0.55 } else { rest };
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
                let theirs_height = (ui.available_height() - earlier_height).max(40.0);
                egui::ScrollArea::vertical().id_salt(("notes-theirs", &key)).max_height(theirs_height).show(ui, |ui| {
                    ui.add(egui::Label::new(RichText::new(&c.text).monospace().color(t.text)).wrap().selectable(true));
                });
            });
        }
        if has_earlier {
            ui.separator();
            self.earlier_section(ui, &place, Some(&key));
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

impl App {
    /// "Earlier notes in this worktree": View, Adopt into `key`, Delete.
    fn earlier_section(&mut self, ui: &mut Ui, place: &str, key: Option<&NotesKey>) {
        let t = self.state.theme;
        let Some(e) = self.state.earlier.get(place).cloned() else { return };
        if e.items.is_empty() {
            if let Some(err) = &e.error {
                ui.label(RichText::new(format!("Earlier notes: {err}")).small().color(t.red));
            }
            return;
        }
        // Adopting replaces what the agent has: ask first unless it is only the template.
        let has_notes = key
            .and_then(|k| self.state.notes.get(k))
            .is_none_or(|d| d.dirty() || d.meta.as_ref().is_none_or(|m| !m.empty));
        let now = unix_now();
        let mut act = None::<(EarlierClick, String)>;
        egui::CollapsingHeader::new(
            RichText::new(format!("Earlier notes in this worktree ({})", e.items.len())).strong(),
        )
        .id_salt(("notes-earlier-head", place))
        .default_open(true)
        .show(ui, |ui| {
            egui::ScrollArea::vertical()
                .id_salt(("notes-earlier-list", place))
                .max_height(ui.available_height().clamp(80.0, 250.0))
                .show(ui, |ui| {
                    for n in &e.items {
                        ui.label(RichText::new(ended_label(place, &n.agent_id)).color(t.text));
                        ui.label(RichText::new(ended_summary(n, now)).small().color(t.muted));
                        let id = n.notes_id.clone();
                        let confirming = e.confirm.as_ref().filter(|(_, c)| *c == id).map(|(c, _)| *c);
                        match confirming {
                            Some(Confirm::Adopt) => {
                                ui.label(RichText::new("Replace this agent's notes with them?").color(t.yellow));
                                ui.horizontal(|ui| {
                                    if ui.button("Adopt, replacing mine").clicked() {
                                        act = Some((EarlierClick::Adopt, id.clone()));
                                    }
                                    if ui.button("Cancel").clicked() {
                                        act = Some((EarlierClick::Cancel, id.clone()));
                                    }
                                });
                            }
                            Some(Confirm::Delete) => {
                                ui.label(RichText::new("Delete them for good?").color(t.yellow));
                                ui.horizontal(|ui| {
                                    if ui.button("Delete them").clicked() {
                                        act = Some((EarlierClick::Delete, id.clone()));
                                    }
                                    if ui.button("Cancel").clicked() {
                                        act = Some((EarlierClick::Cancel, id.clone()));
                                    }
                                });
                            }
                            None => {
                                ui.horizontal(|ui| {
                                    let shown = e.viewing.as_ref().is_some_and(|(v, _)| *v == id);
                                    if ui.button(if shown { "Hide" } else { "View" }).clicked() {
                                        act = Some((EarlierClick::View, id.clone()));
                                    }
                                    let adopt = ui
                                        .add_enabled(key.is_some(), egui::Button::new("Adopt into this agent"))
                                        .on_hover_text("Their text replaces the agent's notes")
                                        .on_disabled_hover_text("Spawn an agent here first (s)");
                                    if adopt.clicked() {
                                        let ask = if has_notes { EarlierClick::AskAdopt } else { EarlierClick::Adopt };
                                        act = Some((ask, id.clone()));
                                    }
                                    if ui.button("Delete").clicked() {
                                        act = Some((EarlierClick::AskDelete, id.clone()));
                                    }
                                });
                            }
                        }
                        if let Some((_, text)) = e.viewing.as_ref().filter(|(v, _)| *v == id) {
                            egui::Frame::new().fill(t.panel).stroke(Stroke::new(1.0, t.border)).show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                match text {
                                    Some(text) => {
                                        ui.add(
                                            egui::Label::new(RichText::new(text).monospace().color(t.text))
                                                .wrap()
                                                .selectable(true),
                                        );
                                    }
                                    None => {
                                        ui.spinner();
                                    }
                                }
                            });
                        }
                        ui.add_space(6.0);
                    }
                });
        });
        let Some((what, id)) = act else { return };
        let confirm = |s: &mut State, c: Option<Confirm>| {
            if let Some(e) = s.earlier.get_mut(place) {
                e.confirm = c.map(|c| (c, id.clone()));
            }
        };
        match what {
            EarlierClick::View => self.state.view_earlier(place, &id),
            EarlierClick::AskAdopt => confirm(&mut self.state, Some(Confirm::Adopt)),
            EarlierClick::AskDelete => confirm(&mut self.state, Some(Confirm::Delete)),
            EarlierClick::Cancel => confirm(&mut self.state, None),
            EarlierClick::Adopt => {
                confirm(&mut self.state, None);
                if let Some(k) = key {
                    self.state.adopt_earlier(k, &id);
                }
            }
            EarlierClick::Delete => {
                confirm(&mut self.state, None);
                self.state.delete_earlier(place, &id);
            }
        }
    }
}

/// A click in the earlier notes list.
enum EarlierClick {
    View,
    AskAdopt,
    Adopt,
    AskDelete,
    Delete,
    Cancel,
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
    fn earlier_notes_helpers() {
        let mut e = Earlier::default();
        assert!(e.wants_fetch() && e.newest().is_none());
        (e.loaded, e.loading) = (true, false);
        assert!(!e.wants_fetch());
        e.stale = true;
        assert!(e.wants_fetch());
        e.loading = true;
        assert!(!e.wants_fetch(), "one request at a time; stale stays for the next");
        let now = 1_000_000.0;
        let item: NotesListItem = serde_json::from_value(json!({
            "notes_id": "o1", "state": "orphan", "worktree_path": "/r", "agent_id": "a2", "kind": "agent",
            "size": 300, "empty": false, "by": "agent", "updated_at": now - 90.0, "ended_at": now - 60.0,
            "expires_at": now + 7.0 * 86_400.0 - 60.0
        }))
        .unwrap();
        assert_eq!(ended_summary(&item, now), "orphan, 300 B, ended 1m ago, deleted in 7d");
        assert_eq!(days_left(now + 7.0 * 86_400.0 - 60.0, now), 7);
        assert_eq!(days_left(now + 60.0, now), 1, "never 0 while ahead");
        assert_eq!(ended_label("/r", "a2"), "Agent a2");
        assert_eq!(ended_label(ORCHESTRATOR_KEY, ""), "the orchestrator");
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
