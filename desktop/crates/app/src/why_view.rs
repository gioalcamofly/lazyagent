//! "Why is this line here?" (proposal 0014, R6; feature `rules`): `w` on a
//! Home line, or Why? in the expanded line. `rules_explain {key}`: the rule
//! that matched (its id, where it comes from: built-in, or a file and
//! line), the facts it read, and the rules above it with the first
//! condition that was not true. A Merge line matched the queue: its top
//! item comes from the `merge` rule, shown first with its own facts and the
//! rules above it.

use eframe::egui::{self, CornerRadius, Key, Modifiers, RichText, Stroke, Ui};
use lazyagent_proto::types::{CatalogRule, ExplainAbove, ExplainFact, RulesExplain};
use serde_json::{Value, json};

use crate::app::{App, Severity, State};
use crate::home::Line;
use crate::theme::Theme;

#[derive(Clone)]
pub struct WhyView {
    pub key: String,
    pub name: String,
    /// `None` until the engine answers.
    pub result: Option<Result<RulesExplain, String>>,
}

impl State {
    /// `w`: why `line` is what it is.
    pub fn open_why(&mut self, line: &Line) {
        if !self.has("rules") {
            self.toast(Severity::Info, "This engine cannot say why: restart it (File → Restart…).");
            return;
        }
        if line.row.is_none() {
            self.toast(Severity::Info, "No work item behind this line: nothing to explain.");
            return;
        }
        let key = line.key.clone();
        self.why = Some(WhyView { key: key.clone(), name: line.name.clone(), result: None });
        self.conn.request("rules_explain", json!({"key": key.clone()}), move |s, reply| {
            let result = reply
                .and_then(|v| {
                    serde_json::from_value::<RulesExplain>(v)
                        .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
                })
                .map_err(|e| e.to_string());
            if let Some(view) = s.why.as_mut().filter(|v| v.key == key) {
                view.result = Some(result);
            }
        });
    }

    /// The view's keys while it is open: Esc (or `w` again) goes back.
    pub fn why_key(&mut self, take: &impl Fn(Modifiers, Key) -> bool) -> bool {
        if take(Modifiers::NONE, Key::Escape) || take(Modifiers::NONE, Key::W) {
            self.why = None;
            return true;
        }
        false
    }
}

/// "built-in", "built-in, locked", "your global rules.toml · /path:12".
pub fn origin_text(origin: &str, file: Option<&str>, line: Option<u32>, locked: bool) -> String {
    match file {
        Some(f) if origin != "builtin" => {
            let at = line.map(|l| format!(":{l}")).unwrap_or_default();
            format!("your {origin} rules.toml · {f}{at}")
        }
        _ if locked => "built-in, locked".into(),
        _ => "built-in".into(),
    }
}

fn value_text(f: &ExplainFact) -> String {
    if f.unknown {
        return "unknown".into();
    }
    match &f.value {
        Value::String(s) if s.is_empty() => "\"\"".into(),
        Value::String(s) => s.clone(),
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(|v| v.as_str().map(str::to_string).unwrap_or(v.to_string())).collect::<Vec<_>>().join(", ")
        ),
        v => v.to_string(),
    }
}

impl App {
    pub(crate) fn why_view(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let Some(view) = self.state.why.clone() else { return };
        let catalog = self.state.guide.catalog().clone();
        let mut back = false;
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            if ui.button("←  Home  (Esc)").clicked() {
                back = true;
            }
            ui.add_space(8.0);
            ui.label(RichText::new("Why is this line here?").heading().strong().color(t.text));
            ui.label(RichText::new(&view.name).monospace().color(t.muted));
        });
        ui.separator();
        if back {
            self.state.why = None;
            return;
        }
        egui::ScrollArea::vertical().id_salt("why").auto_shrink([false, false]).show(ui, |ui| {
            ui.set_max_width(ui.available_width().min(980.0));
            match &view.result {
                None => {
                    ui.label(RichText::new("Asking the engine…").color(t.muted));
                }
                Some(Err(e)) => {
                    ui.label(RichText::new(format!("The engine could not say: {e}")).color(t.red));
                }
                Some(Ok(e)) => explained(ui, &t, e, &catalog),
            }
        });
    }
}

fn explained(ui: &mut Ui, t: &Theme, e: &RulesExplain, catalog: &lazyagent_proto::types::RulesCatalog) {
    let Some(rule) = &e.rule else {
        ui.label(RichText::new("No rule matched: the line is stalled.").color(t.yellow));
        above(ui, t, "Rules that did not match", &e.above);
        return;
    };
    let sentence = |r: &CatalogRule| {
        crate::how_it_works::rule_text(catalog, &r.id)
            .and_then(|c| c.sentence.clone())
            .unwrap_or_else(|| r.when.clone())
    };
    if let Some(item) = &e.item_rule {
        // A Merge line: its item's rule is the answer; the queue is how it got there.
        card(ui, t, "Its inbox item comes from", &item.rule, &sentence(&item.rule), t.accent, |ui| {
            if let Some(v) = &item.rule.verb {
                chips(ui, t, &[("verb", v.as_str())]);
            }
        });
        facts(ui, t, "Facts it read", &item.facts);
        above(ui, t, &format!("Rules above {} that did not match", item.rule.id), &item.above);
        ui.add_space(10.0);
    }
    let then = e.then.clone().unwrap_or(Value::Null);
    let field = |k: &str| then.get(k).and_then(|v| v.as_str()).map(str::to_string);
    let title = if e.item_rule.is_some() { "The line shows its top item through" } else { "Matched rule" };
    card(ui, t, title, rule, &sentence(rule), t.accent, |ui| {
        let (ball, level, state, verb) = (field("ball"), field("level"), field("state"), field("verb"));
        let mut list: Vec<(&str, &str)> = vec![];
        if let Some(b) = ball.as_deref() {
            list.push(("ball", b));
        }
        if let Some(l) = level.as_deref() {
            list.push(("level", l));
        }
        if let Some(s) = state.as_deref() {
            list.push(("state", s));
        }
        if let Some(v) = verb.as_deref() {
            list.push(("verb", v));
        }
        chips(ui, t, &list);
        if let Some(from) = &e.inherited_from {
            ui.label(RichText::new(format!("inherited from rule {from}")).small().color(t.muted));
        }
    });
    facts(ui, t, "Facts it read", &e.facts);
    above(ui, t, &format!("Rules above {} that did not match", rule.id), &e.above);
}

fn card(
    ui: &mut Ui,
    t: &Theme,
    title: &str,
    r: &CatalogRule,
    sentence: &str,
    colour: egui::Color32,
    extra: impl FnOnce(&mut Ui),
) {
    ui.add_space(6.0);
    ui.label(RichText::new(title).small().strong().color(t.muted));
    egui::Frame::new()
        .fill(t.surface)
        .stroke(Stroke::new(1.0, colour))
        .corner_radius(CornerRadius::same(6))
        .inner_margin(egui::Margin::symmetric(12, 8))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(&r.id).monospace().strong().size(16.0).color(colour));
                ui.label(
                    RichText::new(origin_text(&r.origin, r.file.as_deref(), r.line, r.locked)).small().color(t.muted),
                );
            });
            if !r.says.is_empty() {
                ui.add(egui::Label::new(RichText::new(&r.says).color(t.text)).wrap());
            }
            ui.add(egui::Label::new(RichText::new(format!("when: {sentence}")).small().color(t.text)).wrap());
            ui.add(egui::Label::new(RichText::new(&r.when).monospace().small().color(t.muted)).wrap());
            if let Some(a) = &r.and_when {
                ui.add(egui::Label::new(RichText::new(format!("and {a}")).monospace().small().color(t.muted)).wrap());
            }
            extra(ui);
        });
}

fn chips(ui: &mut Ui, t: &Theme, list: &[(&str, &str)]) {
    ui.horizontal_wrapped(|ui| {
        for (k, v) in list {
            egui::Frame::new()
                .fill(t.panel)
                .corner_radius(CornerRadius::same(8))
                .inner_margin(egui::Margin::symmetric(8, 2))
                .show(ui, |ui| {
                    ui.label(RichText::new(format!("{k}  ")).small().color(t.muted));
                    ui.label(RichText::new(*v).small().strong().color(t.text));
                });
        }
    });
}

fn facts(ui: &mut Ui, t: &Theme, title: &str, facts: &[ExplainFact]) {
    if facts.is_empty() {
        return;
    }
    ui.add_space(6.0);
    ui.label(RichText::new(title).small().strong().color(t.muted));
    egui::Grid::new(ui.next_auto_id()).num_columns(3).striped(true).spacing([14.0, 3.0]).show(ui, |ui| {
        for f in facts {
            ui.label(RichText::new(&f.name).monospace().small().color(t.text));
            let colour = if f.unknown { t.yellow } else { t.text };
            ui.label(RichText::new(value_text(f)).monospace().small().strong().color(colour));
            ui.label(RichText::new(format!("{} · {}", f.source, f.freshness)).small().color(t.muted));
            ui.end_row();
        }
    });
}

fn above(ui: &mut Ui, t: &Theme, title: &str, rules: &[ExplainAbove]) {
    if rules.is_empty() {
        return;
    }
    ui.add_space(6.0);
    ui.label(RichText::new(format!("{title} · {}", rules.len())).small().strong().color(t.muted));
    egui::Grid::new(ui.next_auto_id()).num_columns(3).striped(true).spacing([14.0, 3.0]).show(ui, |ui| {
        for a in rules {
            let mut id = RichText::new(&a.id).monospace().small().color(t.text);
            if a.file.is_some() {
                id = id.color(t.accent);
            }
            ui.label(id);
            let colour = match a.value.as_str() {
                "unknown" => t.yellow,
                "item" => t.cyan,
                _ => t.muted,
            };
            ui.label(RichText::new(&a.value).small().strong().color(colour));
            let said = a.said.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| a.failed.clone());
            let r = ui.add(egui::Label::new(RichText::new(said).small().color(t.text)).wrap());
            r.on_hover_text(&a.failed);
            ui.end_row();
        }
    });
}
