//! "Settings…" (File menu): lazyagent's settings kept in files, by
//! section. Each option says what it does, its scope (your global
//! config.toml or this repo's .lazyagent.toml, where the repo's wins) and
//! which file holds it. The engine reads and writes the files
//! (`settings_get`, `settings_set`, feature `settings`) and applies a change
//! at once; this page never touches a file itself.

use eframe::egui::{self, RichText, Ui};
use lazyagent_proto::types::{Setting, SettingValue, Settings};
use serde_json::{Value, json};

use crate::app::State;
use crate::theme::Theme;

pub const FEATURE: &str = "settings";
const TITLE: &str = "Settings";

/// An option of the page: the engine's key, its label and one line on
/// what it does.
pub struct Opt {
    pub key: &'static str,
    pub label: &'static str,
    pub help: &'static str,
}

pub struct Section {
    pub title: &'static str,
    pub options: &'static [Opt],
}

pub const SECTIONS: &[Section] = &[
    Section {
        title: "Agents",
        options: &[Opt {
            key: "agents.permission_mode",
            label: "Default permission mode for agents the engine starts",
            help: "Used whenever a spawn names none: Home's quick spawn, Delegate → Spawn, auto-delegation \
                   (with no agent recorded in the worktree) and restores of agents with no recorded mode.",
        }],
    },
    Section {
        title: "Inbox",
        options: &[Opt {
            key: "inbox.hang_seconds",
            label: "Seconds without output before an agent counts as hung",
            help: "A working agent quiet this long (and, on Linux, using no CPU) shows as \"may hang\" in Home. \
                   With no file setting it: the engine's LAZYAGENT_HANG_SECONDS, else 600.",
        }],
    },
];

/// How a value reads in the page.
pub fn choice_text(value: &str) -> String {
    match value {
        "cli" => "CLI default (your agent CLI's own setting)".into(),
        "bypass" => "bypass (skip permissions)".into(),
        other => other.into(),
    }
}

/// How a setting's value reads: a choice by `choice_text`, a number with
/// its unit.
pub fn value_text(s: &Setting, value: &SettingValue) -> String {
    match value {
        SettingValue::Int(n) if s.kind == "int" => format!("{n} seconds"),
        other => choice_text(&other.to_string()),
    }
}

/// An int setting's draft as the number it saves (None: not a whole
/// number, or below its minimum).
pub fn parse_int(s: &Setting, text: &str) -> Option<i64> {
    let n = text.trim().parse::<i64>().ok()?;
    (n >= s.min.unwrap_or(i64::MIN)).then_some(n)
}

/// What an option will be saved as: in which file, and the value (an int
/// setting's as typed).
#[derive(Debug, Clone, PartialEq)]
pub struct Draft {
    pub repo: bool,
    pub value: String,
}

impl Draft {
    /// The scope in force (global when only the default applies), and the
    /// value that file has, else the one in force.
    fn of(s: &Setting) -> Self {
        let repo = s.source == "repo";
        Draft { repo, value: Self::value_at(s, repo) }
    }

    fn value_at(s: &Setting, repo: bool) -> String {
        let file = if repo { &s.repo } else { &s.global };
        file.as_ref().unwrap_or(&s.value).to_string()
    }

    pub fn scope(&self) -> &'static str {
        if self.repo { "repo" } else { "global" }
    }
}

#[derive(Debug, Default)]
pub struct SettingsPage {
    pub open: bool,
    pub settings: Option<Settings>,
    pub drafts: std::collections::BTreeMap<String, Draft>,
    pub error: Option<String>,
    /// The last write, e.g. "Saved in ~/.config/lazyagent/config.toml".
    pub saved: Option<String>,
    pub busy: bool,
}

fn err_text(e: lazyagent_proto::ClientError) -> String {
    match e {
        lazyagent_proto::ClientError::Rpc(r) => r.message,
        other => other.to_string(),
    }
}

/// `path` with the home folder as `~`.
fn tilde(path: &str) -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    match path.strip_prefix(&home) {
        Some(rest) if !home.is_empty() && rest.starts_with('/') => format!("~{rest}"),
        _ => path.to_string(),
    }
}

impl State {
    pub fn open_settings(&mut self) {
        self.settings.open = true;
        self.settings.error = None;
        self.settings.saved = None;
        self.fetch_settings();
    }

    pub fn fetch_settings(&mut self) {
        self.settings.busy = true;
        self.conn.request("settings_get", json!({}), |s, reply| {
            s.settings.busy = false;
            s.settings_reply(reply);
        });
    }

    fn settings_reply(&mut self, reply: Result<serde_json::Value, lazyagent_proto::ClientError>) {
        match reply.and_then(|v| {
            serde_json::from_value::<Settings>(v).map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
        }) {
            Ok(st) => {
                self.settings.drafts = st.settings.iter().map(|s| (s.key.clone(), Draft::of(s))).collect();
                self.settings.settings = Some(st);
                self.settings.error = None;
            }
            Err(e) => self.settings.error = Some(err_text(e)),
        }
    }

    /// Writes `key` in the draft's file (`value` None: removes it there),
    /// then reads back the settings and the engine's config (the spawn
    /// dialogs' defaults).
    pub fn save_setting(&mut self, key: &str, value: Option<Value>) {
        let Some(draft) = self.settings.drafts.get(key).cloned() else { return };
        let file = self
            .settings
            .settings
            .as_ref()
            .map(|s| if draft.repo { s.files.repo.clone() } else { s.files.global.clone() })
            .unwrap_or_default();
        let removed = value.is_none();
        self.settings.busy = true;
        self.settings.saved = None;
        let params = json!({"key": key, "scope": draft.scope(), "value": value});
        self.conn.request("settings_set", params, move |s, reply| {
            s.settings.busy = false;
            let ok = reply.is_ok();
            s.settings_reply(reply);
            if ok {
                let verb = if removed { "Removed from" } else { "Saved in" };
                s.settings.saved = Some(format!("{verb} {}", tilde(&file)));
                s.refresh_config();
            }
        });
    }

    /// `get_config` again, after a setting changed.
    pub fn refresh_config(&mut self) {
        self.conn.request("get_config", json!({}), |s, reply| {
            if let Ok(c) = reply.and_then(|v| {
                serde_json::from_value(v).map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                s.store.config = Some(c);
            }
        });
    }
}

pub fn show(ctx: &egui::Context, state: &mut State) {
    if !state.settings.open {
        return;
    }
    let t = state.theme;
    let mut close = false;
    let modal = egui::Modal::new(egui::Id::unique("lazyagent-settings")).show(ctx, |ui| {
        ui.set_width(640.0);
        ui.label(RichText::new(TITLE).strong().size(15.0));
        ui.add_space(4.0);
        page(ui, state, &t, &mut close);
    });
    if modal.should_close() && !state.settings.busy {
        close = true;
    }
    if close {
        state.settings.open = false;
    }
}

fn page(ui: &mut Ui, state: &mut State, t: &Theme, close: &mut bool) {
    let Some(st) = state.settings.settings.clone() else {
        if let Some(e) = &state.settings.error {
            ui.colored_label(t.yellow, e);
        } else {
            ui.spinner();
        }
        ui.add_space(8.0);
        if ui.button("Close").clicked() {
            *close = true;
        }
        return;
    };
    for section in SECTIONS {
        ui.add_space(4.0);
        ui.label(RichText::new(section.title).strong().color(t.accent));
        ui.separator();
        for opt in section.options {
            if let Some(setting) = st.get(opt.key) {
                option(ui, state, t, &st, opt, setting);
            }
        }
    }
    if let Some(e) = &state.settings.error {
        ui.add_space(4.0);
        ui.colored_label(t.yellow, e);
    }
    if let Some(saved) = &state.settings.saved {
        ui.add_space(4.0);
        ui.colored_label(t.green, saved);
    }
    ui.add_space(8.0);
    if ui.add_enabled(!state.settings.busy, egui::Button::new("Close")).clicked() {
        *close = true;
    }
}

fn option(ui: &mut Ui, state: &mut State, t: &Theme, st: &Settings, opt: &Opt, s: &Setting) {
    ui.label(RichText::new(opt.label).strong());
    ui.label(RichText::new(opt.help).small().color(t.muted));
    let from = match s.source.as_str() {
        "repo" => format!("this repo's {}", tilde(&st.files.repo)),
        "global" => format!("your global {}", tilde(&st.files.global)),
        _ => "the default (no file sets it)".into(),
    };
    ui.label(format!("In force: {}, from {from}", value_text(s, &s.value)));
    let Some(mut draft) = state.settings.drafts.get(opt.key).cloned() else { return };
    let repo_name = if st.repo.is_empty() { "this repo".to_string() } else { st.repo.clone() };
    ui.horizontal(|ui| {
        ui.label("Scope:");
        let before = draft.repo;
        ui.radio_value(&mut draft.repo, false, "Global (every repo)");
        ui.radio_value(&mut draft.repo, true, format!("This repo ({repo_name})"));
        if draft.repo != before {
            draft.value = Draft::value_at(s, draft.repo);
        }
    });
    let file = if draft.repo { &st.files.repo } else { &st.files.global };
    let here = if draft.repo { &s.repo } else { &s.global };
    let note = match here {
        Some(v) => format!("Stored in {}: {}", tilde(file), value_text(s, v)),
        None => format!("Stored in {}: not set there", tilde(file)),
    };
    ui.label(RichText::new(note).small().monospace().color(t.muted));
    if !draft.repo && s.repo.is_some() {
        ui.label(
            RichText::new("This repo sets its own value, which wins over the global one.").small().color(t.yellow),
        );
    }
    let idle = !state.settings.busy;
    let mut save = None;
    ui.horizontal(|ui| {
        ui.label("Value:");
        let new = if s.kind == "int" {
            ui.add(egui::TextEdit::singleline(&mut draft.value).desired_width(80.0).id_salt(("setting", opt.key)));
            ui.label("seconds");
            let n = parse_int(s, &draft.value);
            if n.is_none() {
                let min = s.min.map(|m| format!(", at least {m}")).unwrap_or_default();
                ui.label(RichText::new(format!("a whole number{min}")).small().color(t.yellow));
            }
            n.map(|n| (SettingValue::Int(n), json!(n)))
        } else {
            egui::ComboBox::from_id_salt(("setting", opt.key)).selected_text(choice_text(&draft.value)).show_ui(
                ui,
                |ui| {
                    for c in &s.choices {
                        ui.selectable_value(&mut draft.value, c.clone(), choice_text(c));
                    }
                },
            );
            Some((SettingValue::Text(draft.value.clone()), json!(draft.value)))
        };
        let changed = new.as_ref().is_some_and(|(v, _)| here.as_ref() != Some(v));
        if ui.add_enabled(idle && changed, egui::Button::new(RichText::new("Save").strong())).clicked() {
            save = new.map(|(_, json)| Some(json));
        }
        if here.is_some()
            && ui
                .add_enabled(idle, egui::Button::new("Remove from this file"))
                .on_hover_text("The other file's value, else the default, applies")
                .clicked()
        {
            save = Some(None);
        }
    });
    state.settings.drafts.insert(opt.key.to_string(), draft);
    if let Some(value) = save {
        state.save_setting(opt.key, value);
    }
    ui.add_space(6.0);
}
