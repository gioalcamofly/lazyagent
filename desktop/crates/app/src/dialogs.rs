//! The action dialogs (the web page's dialogs.js, ui-plan U3): Spawn, New
//! worktree (and launch an agent in it), Remove, Stop, and the `?` sheet.
//! Same accelerators as the TUI's modals when no text field has the
//! keyboard: Spawn `n` / `d` / `r` / `l`, Remove `y` / `n` / `f`, Stop
//! `y` / `n`; Enter submits, Ctrl+Enter from the instruction, Esc closes.
//!
//! Session notes (plan N5): Stop offers a handoff when the agent's notes
//! are not empty (copy / keep / delete, `c` / `k` / `d`); Spawn warns when
//! a new conversation would delete the worktree's ended notes and can
//! carry them into the new agent.

use eframe::egui::{self, Align, Key, Layout, Modifiers, RichText, TextEdit, Ui};
use lazyagent_proto::types::{ConfigView, CreateWorktreeResult, Notes, RemoveWorktreeResult, StopResult, Worktree};
use serde_json::json;

use crate::app::{Focus, Severity, State, unix_now};
use crate::notes_view::{Earlier, ended_label, ended_summary};
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
    /// The permission mode to spawn in (PERMISSION_MODES); preselected
    /// from the engine's `[agents] permission_mode`.
    pub mode: String,
    pub resume: &'static str,
    pub launch: bool,
    pub force: bool,
    /// Spawn: carry the worktree's newest ended notes into the new agent.
    pub carry: bool,
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
            mode: "cli".into(),
            resume: "new",
            launch: false,
            force: false,
            carry: false,
            busy: false,
            error: None,
            focused: false,
        }
    }
}

/// The modes a spawn can name; Claude's own `--permission-mode` values
/// after cli and bypass.
pub const PERMISSION_MODES: [&str; 7] = ["cli", "bypass", "acceptEdits", "plan", "dontAsk", "auto", "default"];

/// The configured permission mode, "cli" without one (or from an older
/// engine).
pub fn configured_mode(config: Option<&ConfigView>) -> String {
    config.and_then(|c| c.permission_mode.clone()).unwrap_or_else(|| "cli".into())
}

/// The modes the picker offers: only cli and bypass for providers other
/// than Claude (the rest start them in their default anyway), plus the
/// configured one.
pub fn mode_choices(config: Option<&ConfigView>) -> Vec<String> {
    let claude = config.is_none_or(|c| c.agent_provider == "claude");
    let mut modes: Vec<String> =
        PERMISSION_MODES.iter().take(if claude { PERMISSION_MODES.len() } else { 2 }).map(|m| m.to_string()).collect();
    let configured = configured_mode(config);
    if !modes.contains(&configured) {
        modes.push(configured);
    }
    modes
}

/// `n` in the spawn dialog: the configured mode, unless that is bypass.
pub fn normal_mode(config: Option<&ConfigView>) -> String {
    match configured_mode(config) {
        m if m == "bypass" => "cli".into(),
        m => m,
    }
}

fn mode_picker(ui: &mut Ui, id: &str, mode: &mut String, config: Option<&ConfigView>) {
    ui.horizontal(|ui| {
        ui.label("Permissions");
        egui::ComboBox::from_id_salt(id).selected_text(mode.as_str()).show_ui(ui, |ui| {
            for m in mode_choices(config) {
                let text = if m == "cli" { "cli (the CLI's own default)".to_string() } else { m.clone() };
                ui.selectable_value(mode, m, text);
            }
        });
    });
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

/// What the stop dialog does with the agent's session notes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopNotes {
    /// To the clipboard, then deleted.
    Copy,
    /// An orphan, kept `[notes] orphan_days` and listed in the panel.
    Keep,
    Delete,
}

/// What the stop dialog knows of the agent's notes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Handoff {
    /// An engine without notes: a plain stop.
    Unsupported,
    /// Not read yet: a plain stop keeps them, to be safe.
    Checking,
    Empty,
    /// Not empty: the three choices. `dirty`: the panel has unsaved edits.
    Offer {
        dirty: bool,
    },
}

pub fn handoff(state: &State, target: &str, agent_id: Option<&str>) -> Handoff {
    if !state.notes_supported() {
        return Handoff::Unsupported;
    }
    let doc = agent_id.and_then(|a| state.notes.get(&(target.to_string(), a.to_string())));
    match doc.and_then(|d| d.meta.as_ref().map(|m| (d, m))) {
        // Unreadable notes (an error) are offered too: better a choice too many.
        None if doc.is_some_and(|d| d.error.is_some()) => Handoff::Offer { dirty: false },
        None => Handoff::Checking,
        Some((d, m)) if m.empty && !d.dirty() => Handoff::Empty,
        Some((d, _)) => Handoff::Offer { dirty: d.dirty() },
    }
}

/// Every key, for the `?` sheet (ui-plan §7a, keymap.js), in this app's terms.
pub const KEYMAP: &[(&str, &str, &str)] = &[
    ("F1", "How lazyagent works", "Anywhere"),
    ("Ctrl+K", "Focus the sidebar", "Anywhere"),
    ("Ctrl+J", "Focus the active agent (in the agent: a newline)", "Anywhere"),
    ("Ctrl+L", "Open and focus the worktree shell (in the shell: clear)", "Anywhere"),
    ("Alt+]  Alt+N", "Next agent tab", "Anywhere"),
    ("Alt+[  Alt+P", "Previous agent tab", "Anywhere"),
    ("Alt+G", "Refresh git status", "Anywhere"),
    ("Ctrl+Shift+D", "The worktree's diff (Ctrl+D outside a terminal)", "Anywhere"),
    ("Ctrl+.", "The worktree's PR and CI", "Anywhere"),
    ("G H  G W", "Home (what needs you, what is in flight) / the worktrees", "Outside a terminal"),
    ("Ctrl+=  Ctrl+−  Ctrl+0", "Terminal font size: larger, smaller, default", "Anywhere"),
    ("J  K  ↓  ↑", "Next / previous worktree; Enter: its agent", "Sidebar"),
    ("J  K  ↓  ↑  Enter", "Next / previous line; Enter: answer a Now item, else expand", "Home"),
    ("?", "How lazyagent works, at Home: zones, the ball, every state and key", "Home"),
    ("W", "Why is this line here? (the rule, its facts, the rules above)", "Home"),
    ("O  N  P", "The line's agent terminal / its notes / its PR", "Home"),
    ("X  Z", "Dismiss the line's item / park it (1-5; W the whole work item)", "Home"),
    ("Shift+Z  D", "Show / hide Parked / delegate the line's item to the branch's agent", "Home"),
    ("Shift+O  U", "Pass to the orchestrator / take it back (or clear your label, park, close)", "Home"),
    ("L  Shift+X", "Label who has the ball (e.g. QA) / close the work item as done or dropped", "Home"),
    ("M  Shift+M", "Mark the line seen / mark everything seen", "Home"),
    ("Shift+U", "Revert the orchestrator's decision on the line", "Home"),
    ("F", "Fold the in-flight list", "Home"),
    ("S", "Spawn an agent", "Outside a terminal"),
    ("X", "Stop the agent", "Worktrees"),
    ("C", "Create a worktree (and launch an agent)", "Outside a terminal"),
    ("D", "Remove the worktree", "Worktrees"),
    ("R", "Refresh the worktree list", "Outside a terminal"),
    ("Shift+R", "Restore the agents an earlier session ran", "Outside a terminal"),
    ("N", "The active agent's session notes", "Outside a terminal"),
    ("Ctrl+S", "Save the notes", "Notes panel"),
    ("?", "This help", "Outside a terminal"),
    ("Ctrl+C", "Copy the selection; without one, ^C to the program", "In a terminal"),
    ("Ctrl+Shift+C  Ctrl+Shift+V", "Copy, paste (also Shift+Insert, middle click)", "In a terminal"),
    ("Shift+Enter", "A newline for the agent (ESC CR)", "In a terminal"),
    ("Shift+drag  Shift+wheel", "Select and scroll even when the program uses the mouse", "In a terminal"),
    ("N  D  R  L", "Spawn: configured mode (not bypass), bypass, resume (pick / last)", "In a dialog"),
    ("Y  N  F", "Confirm: yes / no; Remove: toggle force", "In a dialog"),
    ("C  K  D", "Stop with notes: copy them / keep them / delete them", "In a dialog"),
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
    let mut open_notes = false;
    let mut submit = None::<Submit>;
    let handoff = if d.kind == Kind::Stop { handoff(state, &d.target, d.agent_id.as_deref()) } else { Handoff::Empty };
    let modal = egui::Modal::new(egui::Id::unique(("lazyagent-dialog", d.id))).show(ctx, |ui| {
        let help = matches!(d.kind, Kind::Help);
        ui.set_width(if help { 640.0 } else { 460.0 });
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
                mode_picker(ui, "spawn-mode", &mut d.mode, state.store.config.as_ref());
                ui.horizontal(|ui| {
                    ui.label("Session");
                    ui.radio_value(&mut d.resume, "new", "New");
                    ui.radio_value(&mut d.resume, "pick", "Resume (pick, r)");
                    ui.radio_value(&mut d.resume, "last", "Resume the last (l)");
                });
                let earlier = state.earlier.get(&d.target);
                if d.resume == "new"
                    && let Some(n) = earlier.and_then(Earlier::newest)
                {
                    let more = earlier.map_or(0, |e| e.items.len() - 1);
                    let more = if more > 0 { format!(" and {more} more") } else { String::new() };
                    ui.label(
                        RichText::new(format!(
                            "A new conversation deletes the earlier notes of {} ({}){more}.",
                            ended_label(&d.target, &n.agent_id),
                            ended_summary(n, unix_now())
                        ))
                        .small()
                        .color(t.yellow),
                    );
                    ui.horizontal(|ui| {
                        ui.checkbox(&mut d.carry, "Carry them into the new agent");
                        if ui.link("See them in the notes panel").clicked() {
                            open_notes = true;
                        }
                    });
                }
                if let Some(c) = &state.store.config {
                    ui.label(RichText::new(format!("Provider: {}", c.agent_provider)).small().color(t.muted));
                }
                if !typing {
                    if key(ui, Key::Enter) {
                        submit = Some(Submit::Spawn);
                    } else if key(ui, Key::N) {
                        (d.mode, d.resume) = (normal_mode(state.store.config.as_ref()), "new");
                        submit = Some(Submit::Spawn);
                    } else if key(ui, Key::D) {
                        (d.mode, d.resume) = ("bypass".into(), "new");
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
                    mode_picker(ui, "create-mode", &mut d.mode, state.store.config.as_ref());
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
                match handoff {
                    Handoff::Offer { dirty } => {
                        ui.add_space(6.0);
                        ui.label("Its session notes are not empty. Before they go:");
                        if dirty {
                            ui.label(
                                RichText::new("The notes panel has unsaved edits: they are not in them (save first).")
                                    .small()
                                    .color(t.yellow),
                            );
                        }
                        ui.add_space(4.0);
                        let wide = |ui: &mut Ui, text: &str| {
                            ui.add_enabled(!d.busy, egui::Button::new(text).min_size(egui::vec2(ui.available_width(), 0.0)))
                        };
                        if wide(ui, "Copy notes and stop (c)").clicked() {
                            submit = Some(Submit::Stop(StopNotes::Copy));
                        }
                        if wide(ui, "Stop, keep notes (k)")
                            .on_hover_text("Kept as orphan notes ([notes] orphan_days, 7 by default): the notes panel lists them to adopt")
                            .clicked()
                        {
                            submit = Some(Submit::Stop(StopNotes::Keep));
                        }
                        if wide(ui, "Stop and delete notes (d)").clicked() {
                            submit = Some(Submit::Stop(StopNotes::Delete));
                        }
                    }
                    Handoff::Checking => {
                        ui.label(RichText::new("Reading its session notes…").small().color(t.muted));
                    }
                    Handoff::Empty | Handoff::Unsupported => {}
                }
                if !typing {
                    let offer = matches!(handoff, Handoff::Offer { .. });
                    // A plain stop keeps notes it could not read yet (an empty one is pruned anyway).
                    let plain = if handoff == Handoff::Checking { StopNotes::Keep } else { StopNotes::Delete };
                    if key(ui, Key::Y) || key(ui, Key::Enter) {
                        submit = Some(Submit::Stop(if offer { StopNotes::Keep } else { plain }));
                    } else if key(ui, Key::N) {
                        close = true;
                    } else if offer && key(ui, Key::C) {
                        submit = Some(Submit::Stop(StopNotes::Copy));
                    } else if offer && key(ui, Key::K) {
                        submit = Some(Submit::Stop(StopNotes::Keep));
                    } else if offer && key(ui, Key::D) {
                        submit = Some(Submit::Stop(StopNotes::Delete));
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
            let plain = if handoff == Handoff::Checking { StopNotes::Keep } else { StopNotes::Delete };
            let (label, action) = match d.kind {
                Kind::Spawn => ("Spawn", Some(Submit::Spawn)),
                Kind::Create => ("Create", Some(Submit::Create)),
                Kind::Remove => ("Remove", Some(Submit::Remove)),
                Kind::Stop => ("Stop", Some(Submit::Stop(plain))),
                Kind::Help => ("Close", None),
            };
            let blocked = d.kind == Kind::Remove && remove_guard(state.store.worktree(&d.target)).is_some();
            let text = if d.busy { format!("{label}…") } else { label.to_string() };
            // With notes to hand off, the three choices above are the only ways to stop.
            let offer = d.kind == Kind::Stop && matches!(handoff, Handoff::Offer { .. });
            if !offer && ui.add_enabled(!d.busy && !blocked, egui::Button::new(RichText::new(text).strong())).clicked() {
                match action {
                    Some(a) => submit = Some(a),
                    None => close = true,
                }
            }
            if !help && ui.button("Cancel").clicked() {
                close = true;
            }
        });
    });
    if modal.should_close() {
        close = true;
    }
    if open_notes {
        state.notes_open = true;
        close = true;
    }
    if close {
        if !matches!(d.kind, Kind::Help) {
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
    Stop(StopNotes),
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
                "permission_mode": d.mode,
                "resume_mode": d.resume,
                "env": env,
            });
            if d.target == ORCHESTRATOR_KEY {
                params["kind"] = json!("orchestrator");
            } else {
                params["worktree_path"] = json!(d.target);
            }
            let target = d.target.clone();
            // A new conversation deletes the ended notes (rule 5): read them first.
            let carry = (d.carry && d.resume == "new")
                .then(|| state.earlier.get(&d.target).and_then(Earlier::newest).map(|n| n.notes_id.clone()))
                .flatten();
            match carry {
                None => spawn(state, params, target, id, None),
                Some(notes_id) => {
                    state.conn.clone().request("notes_get", json!({"notes_id": notes_id}), move |s, reply| match reply
                        .and_then(|v| {
                            serde_json::from_value::<Notes>(v)
                                .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
                        }) {
                        Ok(n) => spawn(s, params, target, id, (!n.empty).then_some(n.text)),
                        Err(e) => failed(s, id, format!("Could not read the earlier notes: {e}")),
                    });
                }
            }
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
            let launch = d.launch.then(|| (d.instruction.trim().to_string(), d.mode.clone()));
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
        Submit::Stop(notes) => {
            let mut params = json!({"worktree_path": d.target, "agent_id": d.agent_id});
            if state.notes_supported() {
                params["notes"] = json!(if notes == StopNotes::Keep { "keep" } else { "delete" });
            }
            state.conn.clone().request("stop_agent", params, move |s, reply| match reply {
                Ok(v) => {
                    close(s, id);
                    if notes == StopNotes::Copy {
                        // The engine's final text: what the agent wrote up to its last moment.
                        match serde_json::from_value::<StopResult>(v).ok().and_then(|r| r.notes) {
                            Some(text) => {
                                s.clipboard = Some(text);
                                s.toast(Severity::Info, "Copied the agent's notes to the clipboard.");
                            }
                            None => s.toast(Severity::Info, "Its notes were empty: nothing to copy."),
                        }
                    }
                }
                Err(e) => failed(s, id, format!("Could not stop the agent: {e}")),
            });
        }
    }
}

/// `spawn_agent`; `carry`: text for the new agent's notes.
fn spawn(state: &mut State, params: serde_json::Value, target: String, id: u64, carry: Option<String>) {
    state.conn.clone().request("spawn_agent", params, move |s, reply| match reply {
        Ok(v) => {
            close(s, id);
            s.spawned(&target, &v);
            if let Some(text) = carry
                && let Ok(r) = serde_json::from_value::<lazyagent_proto::types::SpawnResult>(v)
            {
                let place = if target.is_empty() { ORCHESTRATOR_KEY.to_string() } else { target.clone() };
                s.carry_notes((place, r.agent_id), text);
            }
        }
        Err(e) => failed(s, id, format!("Could not spawn: {e}")),
    });
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
            permission_mode: None,
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
