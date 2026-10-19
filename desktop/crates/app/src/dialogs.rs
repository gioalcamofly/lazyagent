//! The action dialogs (the web page's dialogs.js, ui-plan U3): Spawn, New
//! worktree (and launch an agent in it), Remove, Stop, and the `?` sheet.
//! Same accelerators as the TUI's modals when no text field has the
//! keyboard: Spawn `n` / `d` / `r` / `l`, Remove `y` / `n` / `f`, Stop
//! `y` / `n`; Enter submits, Ctrl+Enter from the instruction, Esc closes.

use eframe::egui::{self, Align, Key, Layout, Modifiers, RichText, TextEdit, Ui};
use lazyagent_proto::types::{ConfigView, CreateWorktreeResult, RemoveWorktreeResult, Worktree};
use serde_json::json;

use crate::app::{Focus, Severity, State};
use crate::store::{self, ORCHESTRATOR_KEY};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Spawn,
    Create,
    Remove,
    Stop,
    Help,
}

#[derive(Debug, Clone)]
pub struct Dialog {
    pub kind: Kind,
    /// Which dialog a late reply belongs to.
    pub id: u64,
    /// A worktree path, or the orchestrator key.
    pub target: String,
    pub agent_id: Option<String>,
    pub instruction: String,
    pub label: String,
    pub branch: String,
    pub base: String,
    pub extra: String,
    pub skip: bool,
    pub resume: &'static str,
    pub launch: bool,
    pub force: bool,
    pub busy: bool,
    pub error: Option<String>,
    focused: bool,
}

impl Dialog {
    pub fn new(kind: Kind, id: u64, target: String) -> Dialog {
        Dialog {
            kind,
            id,
            target,
            agent_id: None,
            instruction: String::new(),
            label: String::new(),
            branch: String::new(),
            base: String::new(),
            extra: String::new(),
            skip: false,
            resume: "new",
            launch: false,
            force: false,
            busy: false,
            error: None,
            focused: false,
        }
    }
}

/// The base a new worktree starts from: the configured default branch when
/// a worktree has it checked out, else the main worktree's branch (the
/// built-in default, `master`, is often no branch at all). dialogs.js.
pub fn default_base(config: Option<&ConfigView>, worktrees: &[Worktree]) -> String {
    let configured = config.map(|c| c.default_branch.as_str()).filter(|b| !b.is_empty());
    if let Some(c) = configured
        && worktrees.iter().any(|w| w.branch.as_deref() == Some(c))
    {
        return c.to_string();
    }
    worktrees
        .iter()
        .find(|w| w.is_main)
        .and_then(|w| w.branch.clone())
        .or(configured.map(str::to_string))
        .unwrap_or_else(|| "main".into())
}

/// Why a worktree cannot be removed, or `None`. Stricter than the TUI, as
/// on the web: any agent not completed, failed or stopped blocks it.
pub fn remove_guard(wt: Option<&Worktree>) -> Option<&'static str> {
    let wt = wt?;
    if wt.is_main {
        return Some("The main worktree cannot be removed.");
    }
    if wt.agents.iter().any(|a| !matches!(a.status.as_str(), "completed" | "failed" | "interrupted")) {
        return Some("An agent is running in this worktree: stop it first (x).");
    }
    None
}

/// Every key, for the `?` sheet (ui-plan §7a, keymap.js), in this app's terms.
pub const KEYMAP: &[(&str, &str, &str)] = &[
    ("Ctrl+K", "Focus the sidebar", "Anywhere"),
    ("Ctrl+J", "Focus the active agent (in the agent: a newline)", "Anywhere"),
    ("Ctrl+L", "Open and focus the worktree shell (in the shell: clear)", "Anywhere"),
    ("Alt+]  Alt+N", "Next agent tab", "Anywhere"),
    ("Alt+[  Alt+P", "Previous agent tab", "Anywhere"),
    ("Alt+G", "Refresh git status", "Anywhere"),
    ("Ctrl+Shift+D", "The worktree's diff (Ctrl+D outside a terminal)", "Anywhere"),
    ("Ctrl+.", "The worktree's PR and CI", "Anywhere"),
    ("G A  G W", "The attention queue / the worktrees", "Outside a terminal"),
    ("Ctrl+=  Ctrl+−  Ctrl+0", "Terminal font size: larger, smaller, default", "Anywhere"),
    ("J  K  ↓  ↑", "Next / previous worktree; Enter: its agent", "Sidebar"),
    ("S", "Spawn an agent", "Outside a terminal"),
    ("X", "Stop the agent", "Outside a terminal"),
    ("C", "Create a worktree (and launch an agent)", "Outside a terminal"),
    ("D", "Remove the worktree", "Outside a terminal"),
    ("R", "Refresh the worktree list", "Outside a terminal"),
    ("Shift+R", "Restore the agents an earlier session ran", "Outside a terminal"),
    ("N", "The active agent's session notes", "Outside a terminal"),
    ("Ctrl+S", "Save the notes", "Notes panel"),
    ("?", "This help", "Outside a terminal"),
    ("Ctrl+C", "Copy the selection; without one, ^C to the program", "In a terminal"),
    ("Ctrl+Shift+C  Ctrl+Shift+V", "Copy, paste (also Shift+Insert, middle click)", "In a terminal"),
    ("Shift+Enter", "A newline for the agent (ESC CR)", "In a terminal"),
    ("Shift+drag  Shift+wheel", "Select and scroll even when the program uses the mouse", "In a terminal"),
    ("N  D  R  L", "Spawn: normal, skip permissions, resume (pick / last)", "In a dialog"),
    ("Y  N  F", "Confirm: yes / no; Remove: toggle force", "In a dialog"),
    ("Enter  Ctrl+Enter  Esc", "Submit; submit from the instruction; close", "In a dialog"),
];

fn key(ui: &Ui, k: Key) -> bool {
    ui.input_mut(|i| i.consume_key(Modifiers::NONE, k))
}

/// Draws the open dialog, if any, and runs what it submits.
pub fn show(ctx: &egui::Context, state: &mut State) {
    let Some(mut d) = state.dialog.take() else { return };
    let t = state.theme;
    let mut close = false;
    let mut submit = None::<Submit>;
    let modal = egui::Modal::new(egui::Id::unique(("lazyagent-dialog", d.id))).show(ctx, |ui| {
        ui.set_width(if d.kind == Kind::Help { 640.0 } else { 460.0 });
        let typing = ui.ctx().text_edit_focused();
        let title = match d.kind {
            Kind::Spawn => format!("Spawn an agent in {}", state.place_name(&d.target)),
            Kind::Create => "New worktree".into(),
            Kind::Remove => format!("Remove {}", store::basename(&d.target)),
            Kind::Stop => "Stop the agent".into(),
            Kind::Help => "Keys".into(),
        };
        ui.label(RichText::new(title).strong().size(15.0));
        ui.add_space(6.0);
        match d.kind {
            Kind::Spawn => {
                ui.label("Instruction");
                let edit = ui.add(
                    TextEdit::multiline(&mut d.instruction)
                        .desired_rows(4)
                        .desired_width(f32::INFINITY)
                        .hint_text("What the agent should do (optional)"),
                );
                if !d.focused {
                    edit.request_focus();
                    d.focused = true;
                }
                if edit.has_focus() && ui.input_mut(|i| i.consume_key(Modifiers::CTRL, Key::Enter)) {
                    submit = Some(Submit::Spawn);
                }
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    ui.label("Label");
                    let label = ui.add(TextEdit::singleline(&mut d.label).hint_text("Agent N").desired_width(200.0));
                    // Enter in a one-line field submits, as on the web.
                    if label.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter)) {
                        submit = Some(Submit::Spawn);
                    }
                });
                ui.checkbox(&mut d.skip, "Skip permissions (d)");
                ui.horizontal(|ui| {
                    ui.label("Session");
                    ui.radio_value(&mut d.resume, "new", "New");
                    ui.radio_value(&mut d.resume, "pick", "Resume (pick, r)");
                    ui.radio_value(&mut d.resume, "last", "Resume the last (l)");
                });
                if let Some(c) = &state.store.config {
                    ui.label(RichText::new(format!("Provider: {}", c.agent_provider)).small().color(t.muted));
                }
                if !typing {
                    if key(ui, Key::Enter) {
                        submit = Some(Submit::Spawn);
                    } else if key(ui, Key::N) {
                        (d.skip, d.resume) = (false, "new");
                        submit = Some(Submit::Spawn);
                    } else if key(ui, Key::D) {
                        (d.skip, d.resume) = (true, "new");
                        submit = Some(Submit::Spawn);
                    } else if key(ui, Key::R) {
                        d.resume = "pick";
                        submit = Some(Submit::Spawn);
                    } else if key(ui, Key::L) {
                        d.resume = "last";
                        submit = Some(Submit::Spawn);
                    }
                }
            }
            Kind::Create => {
                egui::Grid::new("create-grid").num_columns(2).spacing([8.0, 6.0]).show(ui, |ui| {
                    ui.label("Branch");
                    let edit = ui.add(TextEdit::singleline(&mut d.branch).desired_width(300.0));
                    if !d.focused {
                        edit.request_focus();
                        d.focused = true;
                    }
                    ui.end_row();
                    ui.label("Base");
                    ui.add(TextEdit::singleline(&mut d.base).desired_width(300.0));
                    ui.end_row();
                    if state.store.config.as_ref().is_some_and(|c| c.has_custom_create) {
                        ui.label("Extra");
                        ui.add(
                            TextEdit::singleline(&mut d.extra)
                                .hint_text("for the custom create command")
                                .desired_width(300.0),
                        );
                        ui.end_row();
                    }
                });
                ui.checkbox(&mut d.launch, "and launch an agent in it");
                if d.launch {
                    let edit = ui.add(
                        TextEdit::multiline(&mut d.instruction)
                            .desired_rows(3)
                            .desired_width(f32::INFINITY)
                            .hint_text("Instruction (optional)"),
                    );
                    if edit.has_focus() && ui.input_mut(|i| i.consume_key(Modifiers::CTRL, Key::Enter)) {
                        submit = Some(Submit::Create);
                    }
                    ui.checkbox(&mut d.skip, "Skip permissions");
                }
                if state.store.config.as_ref().is_some_and(|c| c.has_custom_create) {
                    ui.label(
                        RichText::new("Runs the custom create command; its output shows in the job log.")
                            .small()
                            .color(t.muted),
                    );
                }
                if ui.input(|i| i.key_pressed(Key::Enter) && !i.modifiers.any()) && !d.launch {
                    submit = Some(Submit::Create);
                }
            }
            Kind::Remove => {
                let wt = state.store.worktree(&d.target);
                match remove_guard(wt) {
                    Some(why) => {
                        ui.label(RichText::new(why).color(t.red));
                    }
                    None => {
                        ui.label(format!(
                            "Removes the worktree {} (branch {}).",
                            d.target,
                            wt.and_then(|w| w.branch.clone()).unwrap_or_default()
                        ));
                        ui.checkbox(&mut d.force, "Force: even with uncommitted changes (f)");
                        if state.store.config.as_ref().is_some_and(|c| c.has_custom_remove) {
                            ui.label(
                                RichText::new("Runs the custom remove command; its output shows in the job log.")
                                    .small()
                                    .color(t.muted),
                            );
                        }
                        if !typing {
                            if key(ui, Key::Y) || key(ui, Key::Enter) {
                                submit = Some(Submit::Remove);
                            } else if key(ui, Key::F) {
                                d.force = !d.force;
                            }
                        }
                    }
                }
                if !typing && key(ui, Key::N) {
                    close = true;
                }
            }
            Kind::Stop => {
                let agent = d.agent_id.as_deref().and_then(|a| state.store.agent(&d.target, a));
                let name = agent.map(store::agent_label).unwrap_or_else(|| "the agent".into());
                ui.label(format!("Stop {name} in {}?", state.place_name(&d.target)));
                ui.label(
                    RichText::new("It can be brought back later only through its conversation (restore).")
                        .small()
                        .color(t.muted),
                );
                if !typing {
                    if key(ui, Key::Y) || key(ui, Key::Enter) {
                        submit = Some(Submit::Stop);
                    } else if key(ui, Key::N) {
                        close = true;
                    }
                }
            }
            Kind::Help => {
                egui::ScrollArea::vertical().max_height(520.0).show(ui, |ui| {
                    egui::Grid::new("help-grid").num_columns(3).striped(true).spacing([14.0, 4.0]).show(ui, |ui| {
                        for (keys, action, scope) in KEYMAP {
                            ui.label(RichText::new(*keys).monospace().color(t.accent));
                            ui.label(*action);
                            ui.label(RichText::new(*scope).small().color(t.muted));
                            ui.end_row();
                        }
                    });
                });
            }
        }
        if let Some(e) = &d.error {
            ui.add_space(4.0);
            ui.label(RichText::new(e).color(t.red));
        }
        ui.add_space(8.0);
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            let (label, action) = match d.kind {
                Kind::Spawn => ("Spawn", Some(Submit::Spawn)),
                Kind::Create => ("Create", Some(Submit::Create)),
                Kind::Remove => ("Remove", Some(Submit::Remove)),
                Kind::Stop => ("Stop", Some(Submit::Stop)),
                Kind::Help => ("Close", None),
            };
            let blocked = d.kind == Kind::Remove && remove_guard(state.store.worktree(&d.target)).is_some();
            let text = if d.busy { format!("{label}…") } else { label.to_string() };
            if ui.add_enabled(!d.busy && !blocked, egui::Button::new(RichText::new(text).strong())).clicked() {
                match action {
                    Some(a) => submit = Some(a),
                    None => close = true,
                }
            }
            if d.kind != Kind::Help && ui.button("Cancel").clicked() {
                close = true;
            }
        });
    });
    if modal.should_close() {
        close = true;
    }
    if close {
        if d.kind != Kind::Help {
            state.focus_request = Some(Focus::Agent);
        }
        return;
    }
    if let Some(s) = submit
        && !d.busy
    {
        d.busy = true;
        d.error = None;
        let id = d.id;
        state.dialog = Some(d);
        run(state, s, id);
        return;
    }
    state.dialog = Some(d);
}

#[derive(Debug, Clone, Copy)]
enum Submit {
    Spawn,
    Create,
    Remove,
    Stop,
}

/// A reply for dialog `id`: an error goes back into it (if still open).
fn failed(s: &mut State, id: u64, error: String) {
    match &mut s.dialog {
        Some(d) if d.id == id => {
            d.busy = false;
            d.error = Some(error);
        }
        _ => s.toast(Severity::Error, error),
    }
}

fn close(s: &mut State, id: u64) {
    if s.dialog.as_ref().is_some_and(|d| d.id == id) {
        s.dialog = None;
    }
}

fn run(state: &mut State, submit: Submit, id: u64) {
    let d = state.dialog.clone().unwrap();
    let env = state.env_json();
    match submit {
        Submit::Spawn => {
            let mut params = json!({
                "instruction": (!d.instruction.trim().is_empty()).then(|| d.instruction.trim().to_string()),
                "label": (!d.label.trim().is_empty()).then(|| d.label.trim().to_string()),
                "skip_permissions": d.skip,
                "resume_mode": d.resume,
                "env": env,
            });
            if d.target == ORCHESTRATOR_KEY {
                params["kind"] = json!("orchestrator");
            } else {
                params["worktree_path"] = json!(d.target);
            }
            let target = d.target.clone();
            state.conn.clone().request("spawn_agent", params, move |s, reply| match reply {
                Ok(v) => {
                    close(s, id);
                    s.spawned(&target, &v);
                }
                Err(e) => failed(s, id, format!("Could not spawn: {e}")),
            });
        }
        Submit::Create => {
            if d.branch.trim().is_empty() {
                return failed(state, id, "A branch name is needed.".into());
            }
            let mut params = json!({"branch": d.branch.trim(), "env": env});
            if !d.base.trim().is_empty() {
                params["base_branch"] = json!(d.base.trim());
            }
            if !d.extra.trim().is_empty() {
                params["extra"] = json!(d.extra.trim());
            }
            let launch = d.launch.then(|| (d.instruction.trim().to_string(), d.skip));
            state.conn.clone().request("create_worktree", params, move |s, reply| {
                match reply.and_then(|v| {
                    serde_json::from_value::<CreateWorktreeResult>(v)
                        .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
                }) {
                    Ok(r) => {
                        close(s, id);
                        s.after_create(r, launch);
                    }
                    Err(e) => failed(s, id, format!("Could not create the worktree: {e}")),
                }
            });
        }
        Submit::Remove => {
            let params = json!({"worktree_path": d.target, "force": d.force, "custom": true, "env": env});
            let path = d.target.clone();
            state.conn.clone().request("remove_worktree", params, move |s, reply| {
                match reply.and_then(|v| {
                    serde_json::from_value::<RemoveWorktreeResult>(v)
                        .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
                }) {
                    Ok(r) => {
                        close(s, id);
                        s.after_remove(&path, r.job_id);
                    }
                    Err(e) => failed(s, id, format!("Could not remove the worktree: {e}")),
                }
            });
        }
        Submit::Stop => {
            let params = json!({"worktree_path": d.target, "agent_id": d.agent_id});
            state.conn.clone().request("stop_agent", params, move |s, reply| match reply {
                Ok(_) => close(s, id),
                Err(e) => failed(s, id, format!("Could not stop the agent: {e}")),
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn wt(path: &str, branch: &str, main: bool, statuses: &[&str]) -> Worktree {
        let agents: Vec<_> = statuses
            .iter()
            .enumerate()
            .map(|(i, s)| {
                json!({"agent_id": format!("a{i}"), "label": "", "session_name": "", "status": s,
                       "confidence": "low", "detail": ""})
            })
            .collect();
        serde_json::from_value(json!({"path": path, "branch": branch, "head": "h", "is_main": main,
                                      "is_bare": false, "agent_status": "", "agents": agents}))
        .unwrap()
    }

    #[test]
    fn default_base_prefers_a_checked_out_configured_branch() {
        let cfg = |b: &str| ConfigView {
            agent_provider: "claude".into(),
            default_branch: b.into(),
            has_custom_create: false,
            has_custom_remove: false,
        };
        let trees = [wt("/r", "main", true, &[]), wt("/r-dev", "develop", false, &[])];
        assert_eq!(default_base(Some(&cfg("develop")), &trees), "develop");
        assert_eq!(default_base(Some(&cfg("master")), &trees), "main", "master is not checked out anywhere");
        assert_eq!(default_base(None, &trees), "main");
    }

    #[test]
    fn remove_guards() {
        assert_eq!(remove_guard(None), None);
        assert!(remove_guard(Some(&wt("/r", "main", true, &[]))).unwrap().contains("main worktree"));
        assert!(remove_guard(Some(&wt("/r-x", "x", false, &["waiting"]))).unwrap().contains("running"));
        assert_eq!(remove_guard(Some(&wt("/r-x", "x", false, &["completed", "failed", "interrupted"]))), None);
    }
}
