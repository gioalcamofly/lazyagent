//! "Prompts…" (Agents menu): lazyagent's guidance in the user's agent
//! CLIs' prompts (a `lazyagent` skill, a short pointer in the global
//! instructions file, optional lines in skills of the user's). The engine
//! plans and writes (`prompts_status`, `prompts_plan`, `prompts_apply`,
//! feature `prompts`), the same code as `lazyagent prompts`; this dialog
//! never touches a file itself. Each file's diff is shown and approved or
//! skipped one at a time; an approval sends the shas the diff was made
//! from, so a file changed meanwhile is refused, not overwritten.
//!
//! The engine's "your prompts are outdated" notice shows as a small button
//! on the menu bar's right, once per connection.

use std::collections::BTreeSet;

use eframe::egui::{self, Color32, RichText, Ui};
use lazyagent_proto::types::{PromptsChange as Change, PromptsPlan as Plan, PromptsStatus as Status};
use serde_json::json;

use crate::app::State;
use crate::theme::Theme;

pub const FEATURE: &str = "prompts";
const TITLE: &str = "Agent prompts";

#[derive(Debug, Clone, PartialEq)]
pub enum Outcome {
    Written,
    Skipped,
    Failed(String),
}

/// Install or uninstall, file by file.
#[derive(Debug, Clone)]
pub struct Review {
    pub action: String,
    pub changes: Vec<Change>,
    pub at: usize,
    pub outcomes: Vec<Option<Outcome>>,
}

impl Review {
    pub fn current(&self) -> Option<&Change> {
        self.changes.get(self.at)
    }

    fn count(&self, f: impl Fn(&Outcome) -> bool) -> usize {
        self.outcomes.iter().flatten().filter(|o| f(o)).count()
    }
}

#[derive(Debug, Default)]
pub struct Prompts {
    pub open: bool,
    pub status: Option<Status>,
    pub error: Option<String>,
    pub scope_repo: bool,
    pub skills: BTreeSet<String>,
    pub review: Option<Review>,
    pub busy: bool,
    /// The engine's outdated notice (menu bar), until the dialog is opened.
    pub notice: Option<String>,
}

impl Prompts {
    fn scope(&self) -> &'static str {
        if self.scope_repo { "repo" } else { "global" }
    }
}

fn err_text(e: lazyagent_proto::ClientError) -> String {
    match e {
        lazyagent_proto::ClientError::Rpc(r) => r.message,
        other => other.to_string(),
    }
}

impl State {
    /// On connect: the outdated notice, if the engine has one.
    pub fn fetch_prompts_notice(&mut self) {
        if !self.has(FEATURE) {
            return;
        }
        self.conn.request("prompts_status", json!({}), |s, reply| {
            if let Ok(st) = reply.and_then(|v| {
                serde_json::from_value::<Status>(v).map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                s.prompts.notice = st.notice.map(|n| n.text).filter(|t| !t.is_empty());
            }
        });
    }

    pub fn open_prompts(&mut self) {
        self.prompts.open = true;
        self.prompts.review = None;
        self.prompts.error = None;
        self.prompts.notice = None;
        self.fetch_prompts_status();
    }

    pub fn fetch_prompts_status(&mut self) {
        self.prompts.busy = true;
        let scope = self.prompts.scope();
        self.conn.request("prompts_status", json!({"scope": scope}), |s, reply| {
            s.prompts.busy = false;
            match reply.and_then(|v| {
                serde_json::from_value::<Status>(v).map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(st) => {
                    // Keep only the picks that still exist.
                    let have: BTreeSet<String> = st.targets.iter().flat_map(|t| t.skills.clone()).collect();
                    s.prompts.skills.retain(|k| have.contains(k));
                    s.prompts.status = Some(st);
                    s.prompts.error = None;
                }
                Err(e) => s.prompts.error = Some(err_text(e)),
            }
        });
    }

    /// Asks the engine what `action` would change; the review starts at
    /// the first file with something to do.
    pub fn prompts_review(&mut self, action: &str) {
        self.prompts.busy = true;
        self.prompts.error = None;
        let mut params = json!({"action": action, "scope": self.prompts.scope()});
        if action == "install" {
            params["skills"] = json!(self.prompts.skills.iter().collect::<Vec<_>>());
        }
        let action = action.to_string();
        self.conn.request("prompts_plan", params, move |s, reply| {
            s.prompts.busy = false;
            match reply.and_then(|v| {
                serde_json::from_value::<Plan>(v).map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(plan) => {
                    let changes: Vec<Change> = plan.changes.into_iter().filter(|c| c.action != "none").collect();
                    if changes.is_empty() {
                        s.prompts.error = Some(if action == "install" {
                            "Nothing to change: already installed.".into()
                        } else {
                            "Nothing to change: nothing installed.".into()
                        });
                        return;
                    }
                    let n = changes.len();
                    s.prompts.review = Some(Review { action, changes, at: 0, outcomes: vec![None; n] });
                }
                Err(e) => s.prompts.error = Some(err_text(e)),
            }
        });
    }

    /// Approve the file on screen: the engine writes it if it is still what
    /// the diff was made from.
    pub fn prompts_approve(&mut self) {
        let Some(r) = &self.prompts.review else { return };
        let Some(c) = r.current() else { return };
        let params = json!({
            "id": c.id, "action": r.action, "base_sha": c.base_sha, "new_sha": c.new_sha,
            "follow_symlinks": c.outside.is_some(), "force": c.needs_force,
        });
        let at = r.at;
        self.prompts.busy = true;
        self.conn.request("prompts_apply", params, move |s, reply| {
            s.prompts.busy = false;
            let Some(r) = s.prompts.review.as_mut() else { return };
            if r.at != at {
                return;
            }
            match reply {
                Ok(_) => {
                    r.outcomes[at] = Some(Outcome::Written);
                    r.at += 1;
                }
                Err(e) => r.outcomes[at] = Some(Outcome::Failed(err_text(e))),
            }
        });
    }

    pub fn prompts_skip(&mut self) {
        if let Some(r) = self.prompts.review.as_mut()
            && r.at < r.changes.len()
        {
            if r.outcomes[r.at].is_none() {
                r.outcomes[r.at] = Some(Outcome::Skipped);
            }
            r.at += 1;
        }
    }

    /// Back to the list, which is read again.
    pub fn prompts_back(&mut self) {
        self.prompts.review = None;
        self.fetch_prompts_status();
    }
}

fn state_color(t: &Theme, state: &str) -> Color32 {
    match state {
        "installed" => t.green,
        "outdated" => t.yellow,
        "modified" => t.red,
        _ => t.muted,
    }
}

fn diff_lines(ui: &mut Ui, t: &Theme, diff: &str) {
    egui::ScrollArea::both().max_height(360.0).auto_shrink([false, true]).show(ui, |ui| {
        for line in diff.lines() {
            let color = if line.starts_with("+++") || line.starts_with("---") {
                t.text
            } else if line.starts_with('+') {
                t.green
            } else if line.starts_with('-') {
                t.red
            } else if line.starts_with("@@") {
                t.cyan
            } else {
                t.muted
            };
            ui.label(RichText::new(line).monospace().color(color));
        }
    });
}

pub fn show(ctx: &egui::Context, state: &mut State) {
    if !state.prompts.open {
        return;
    }
    let t = state.theme;
    let mut close = false;
    let modal = egui::Modal::new(egui::Id::unique("lazyagent-prompts")).show(ctx, |ui| {
        ui.set_width(780.0);
        ui.label(RichText::new(TITLE).strong().size(15.0));
        ui.add_space(4.0);
        if state.prompts.review.is_some() {
            review(ui, state, &t, &mut close);
        } else {
            list(ui, state, &t, &mut close);
        }
    });
    if modal.should_close() && !state.prompts.busy {
        close = true;
    }
    if close {
        state.prompts.open = false;
        state.prompts.review = None;
    }
}

fn list(ui: &mut Ui, state: &mut State, t: &Theme, close: &mut bool) {
    let version = state.prompts.status.as_ref().map(|s| s.version.clone()).unwrap_or_default();
    ui.label(
        RichText::new(format!(
            "lazyagent can add its guidance to your agent CLIs: a lazyagent skill (written for lazyagent \
             {version}) and a short pointer to it in your instructions file. You see each file's diff; \
             nothing is written until you approve it, and a backup is kept."
        ))
        .color(t.muted),
    );
    ui.add_space(6.0);
    let mut scope_repo = state.prompts.scope_repo;
    ui.horizontal(|ui| {
        ui.label("Files:");
        ui.radio_value(&mut scope_repo, false, "Your global files");
        let repo = crate::store::basename(&state.conn.root).to_string();
        ui.radio_value(&mut scope_repo, true, format!("This repo's files ({repo})"));
    });
    if scope_repo != state.prompts.scope_repo {
        state.prompts.scope_repo = scope_repo;
        state.fetch_prompts_status();
    }
    ui.add_space(4.0);
    let targets = state.prompts.status.as_ref().map(|s| s.targets.clone()).unwrap_or_default();
    if targets.is_empty() && state.prompts.busy {
        ui.spinner();
    }
    egui::Grid::new("prompts-targets").num_columns(3).spacing([14.0, 4.0]).show(ui, |ui| {
        for target in &targets {
            ui.label(RichText::new(&target.title).strong());
            ui.label(RichText::new(&target.state).color(state_color(t, &target.state)));
            let found = if target.detected { "found" } else { "not found" };
            ui.label(RichText::new(format!("{found}: {}", target.reason)).small().color(t.muted));
            ui.end_row();
            for f in &target.files {
                ui.label("");
                ui.label(RichText::new(&f.state).small().color(state_color(t, &f.state)));
                let mut text = f.display.clone();
                if let Some(v) = &f.version {
                    text.push_str(&format!("  v{v}"));
                }
                if !f.note.is_empty() {
                    text.push_str(&format!("  · {}", f.note));
                }
                if let Some(o) = &f.outside {
                    text.push_str(&format!("  · symlink to {o}"));
                }
                ui.label(RichText::new(text).monospace().small());
                ui.end_row();
            }
            if !target.note.is_empty() {
                ui.label("");
                ui.label("");
                ui.label(RichText::new(&target.note).small().color(t.yellow));
                ui.end_row();
            }
        }
    });
    let skills: BTreeSet<String> =
        targets.iter().filter(|t| t.detected).flat_map(|t| t.skills.iter().cloned()).collect();
    if !skills.is_empty() {
        ui.add_space(6.0);
        ui.label("Also point these skills of yours to it (one line each, on install):");
        ui.horizontal_wrapped(|ui| {
            for s in &skills {
                let mut on = state.prompts.skills.contains(s);
                if ui.checkbox(&mut on, s).changed() {
                    if on {
                        state.prompts.skills.insert(s.clone());
                    } else {
                        state.prompts.skills.remove(s);
                    }
                }
            }
        });
    }
    if let Some(e) = &state.prompts.error {
        ui.add_space(4.0);
        ui.colored_label(t.yellow, e);
    }
    ui.add_space(8.0);
    let idle = !state.prompts.busy;
    let any = targets.iter().any(|t| t.detected) || state.prompts.scope_repo;
    ui.horizontal(|ui| {
        if ui.add_enabled(idle && any, egui::Button::new(RichText::new("Review install…").strong())).clicked() {
            state.prompts_review("install");
        }
        if ui.add_enabled(idle && any, egui::Button::new("Review uninstall…")).clicked() {
            state.prompts_review("uninstall");
        }
        if ui.add_enabled(idle, egui::Button::new("Close")).clicked() {
            *close = true;
        }
    });
    if !any && !targets.is_empty() {
        ui.label(
            RichText::new("No agent CLI found (no ~/.claude, ~/.codex or ~/.gemini). The CLI can install for one anyway: lazyagent prompts install --target <name>")
                .small()
                .color(t.muted),
        );
    }
}

fn review(ui: &mut Ui, state: &mut State, t: &Theme, close: &mut bool) {
    let r = state.prompts.review.clone().expect("a review");
    let n = r.changes.len();
    let idle = !state.prompts.busy;
    let Some(c) = r.current() else {
        let (w, s, f) = (
            r.count(|o| *o == Outcome::Written),
            r.count(|o| *o == Outcome::Skipped),
            r.count(|o| matches!(o, Outcome::Failed(_))),
        );
        ui.label(RichText::new(format!("{w} written, {s} skipped, {f} refused")).strong());
        for (c, o) in r.changes.iter().zip(&r.outcomes) {
            let (text, color) = match o {
                Some(Outcome::Written) if c.action == "remove" => ("deleted".to_string(), t.green),
                Some(Outcome::Written) => ("written".to_string(), t.green),
                Some(Outcome::Failed(e)) => (format!("refused: {e}"), t.red),
                _ => ("skipped".to_string(), t.muted),
            };
            ui.horizontal(|ui| {
                ui.label(RichText::new(&c.display).monospace());
                ui.label(RichText::new(text).color(color));
            });
        }
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            if ui.button("Back to the list").clicked() {
                state.prompts_back();
            }
            if ui.button("Close").clicked() {
                *close = true;
            }
        });
        return;
    };
    let verb = if r.action == "install" { "Install" } else { "Uninstall" };
    ui.label(RichText::new(format!("{verb} · file {} of {n}", r.at + 1)).color(t.muted));
    let was = c.version.as_ref().map(|v| format!(" (v{v})")).unwrap_or_default();
    ui.label(RichText::new(&c.display).monospace().strong());
    ui.label(format!("{} · {} · now {}{was}", c.action, c.kind.replace('_', " "), c.state));
    if let Some(o) = &c.outside {
        ui.colored_label(t.yellow, format!("It resolves outside its dir (a symlink): approving writes {o}"));
    }
    if c.needs_force {
        let why = if c.note.is_empty() { "edited by hand" } else { &c.note };
        ui.colored_label(t.yellow, format!("{why}: approving replaces your edits (a backup is kept)"));
    }
    if c.action == "blocked" {
        ui.colored_label(t.red, format!("Not touched: {}", c.note));
    } else {
        ui.add_space(4.0);
        egui::Frame::new()
            .fill(t.panel)
            .inner_margin(egui::Margin::same(6))
            .corner_radius(egui::CornerRadius::same(4))
            .show(ui, |ui| diff_lines(ui, t, &c.diff));
    }
    let failed = match r.outcomes.get(r.at) {
        Some(Some(Outcome::Failed(e))) => Some(e.clone()),
        _ => None,
    };
    if let Some(e) = &failed {
        ui.colored_label(t.red, format!("Refused: {e}"));
    }
    ui.add_space(8.0);
    ui.horizontal(|ui| {
        if failed.is_some() {
            if ui.add_enabled(idle, egui::Button::new(RichText::new("Preview again").strong())).clicked() {
                let action = r.action.clone();
                state.prompts_review(&action);
            }
        } else if c.action != "blocked" {
            let label = if c.needs_force {
                "Approve: replace my edits"
            } else if c.outside.is_some() {
                "Approve: write through the symlink"
            } else if c.action == "remove" {
                "Approve: delete"
            } else {
                "Approve"
            };
            if ui.add_enabled(idle, egui::Button::new(RichText::new(label).strong())).clicked() {
                state.prompts_approve();
            }
        }
        if ui.add_enabled(idle, egui::Button::new("Skip")).clicked() {
            state.prompts_skip();
        }
        if ui.add_enabled(idle, egui::Button::new("Stop")).clicked() {
            state.prompts_back();
        }
        if ui.add_enabled(idle, egui::Button::new("Close")).clicked() {
            *close = true;
        }
    });
}
