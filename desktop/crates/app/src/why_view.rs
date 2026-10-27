//! "Why is this line here?" (proposal 0014, R6; feature `rules`): `w` on a
//! Home line, or Why? in the expanded line. `rules_explain {key}`: the rule
//! that matched (its id, where it comes from: built-in, or a file and
//! line), the facts it read, and the rules above it with the first
//! condition that was not true. A Merge line matched the queue: its top
//! item comes from the `merge` rule, shown first with its own facts and the
//! rules above it.
//!
//! I4f: the view answers the user first, in one sentence with the values
//! as people read them ("2 d 6 h", "24 h"), then the open items and which
//! one leads; the rules above that did not match are folded, the unknown
//! ones (the suspicious ones) first.
//!
//! R5 (feature `ci_attest`): the item's attestations, the standing ones
//! (pending, accepted, recorded) with Revoke.
//!
//! R3d (feature `owner_approve`): the user's approval of the PR's head, what
//! the count starts from, with Revoke.

use eframe::egui::{self, CornerRadius, Key, Modifiers, RichText, Stroke, Ui};
use lazyagent_proto::types::{Attestation, CatalogRule, ExplainAbove, ExplainFact, OwnerApproval, RulesExplain};
use serde_json::{Value, json};

use crate::app::{App, Severity, State, unix_now};
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

    /// Revoke a standing attestation from Why? (`ci_attestation_revoke`),
    /// then ask again why the line is what it is.
    pub fn revoke_attestation(&mut self, id: String) {
        self.conn.request("ci_attestation_revoke", json!({"id": id}), move |s, reply| match reply {
            Ok(_) => {
                s.toast(Severity::Info, "Revoked: the check counts against the branch again.");
                s.inbox_gen += 1;
                s.fetch_inbox();
                if let Some(view) = s.why.clone() {
                    s.refresh_why(view.key);
                }
            }
            Err(e) => s.toast(Severity::Error, format!("Not revoked: {e}")),
        });
    }

    /// Revoke the user's approval from Why? (`owner_approval_revoke`, only
    /// while it is still of that head), then ask again.
    pub fn revoke_owner_approval(&mut self, key: String, sha: String) {
        let params = json!({"key": key.clone(), "sha": sha});
        self.conn.request("owner_approval_revoke", params, move |s, reply| match reply {
            Ok(_) => {
                s.toast(Severity::Info, "Revoked: the commits count from the ready event again.");
                s.inbox_gen += 1;
                s.fetch_inbox();
                if let Some(view) = s.why.clone() {
                    s.refresh_why(view.key);
                }
            }
            Err(e) => s.toast(Severity::Error, format!("Not revoked: {e}")),
        });
    }

    fn refresh_why(&mut self, key: String) {
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
    if let Some(shown) = f.shown.as_deref().filter(|s| !s.is_empty()) {
        // The engine's words for people: "2 d 6 h", not 197132.43.
        return shown.to_string();
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
        let mut revoke = None;
        let mut unapprove = None;
        egui::ScrollArea::vertical().id_salt("why").auto_shrink([false, false]).show(ui, |ui| {
            ui.set_max_width(ui.available_width().min(980.0));
            match &view.result {
                None => {
                    ui.label(RichText::new("Asking the engine…").color(t.muted));
                }
                Some(Err(e)) => {
                    ui.label(RichText::new(format!("The engine could not say: {e}")).color(t.red));
                }
                Some(Ok(e)) => {
                    explained(ui, &t, e, &catalog);
                    if let Some(a) = &e.owner_approval
                        && owner_approval(ui, &t, a, unix_now())
                    {
                        unapprove = Some((a.key.clone(), a.sha.clone()));
                    }
                    revoke = attestations(ui, &t, &e.attestations, unix_now());
                }
            }
        });
        if let Some(id) = revoke {
            self.state.revoke_attestation(id);
        }
        if let Some((key, sha)) = unapprove {
            self.state.revoke_owner_approval(key, sha);
        }
    }
}

/// `home` → `in Home`: where the approval was given.
fn approval_source(source: &str) -> &str {
    match source {
        "home" => "in Home",
        "agent" => "to its agent",
        "cli" => "from the CLI",
        other => other,
    }
}

/// The user's approval of the PR's head (R3d), with Revoke. True: revoke.
fn owner_approval(ui: &mut Ui, t: &Theme, a: &OwnerApproval, now: f64) -> bool {
    let mut revoke = false;
    ui.add_space(8.0);
    ui.label(RichText::new("Your approval").small().strong().color(t.muted));
    egui::Frame::new()
        .fill(t.surface)
        .stroke(Stroke::new(1.0, if a.state.as_deref() == Some("lost") { t.border } else { t.cyan }))
        .corner_radius(CornerRadius::same(6))
        .inner_margin(egui::Margin::symmetric(10, 6))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(&a.sha[..a.sha.len().min(7)]).monospace().strong().color(t.text));
                ui.label(
                    RichText::new(format!(
                        "approved {} · {} ago",
                        approval_source(&a.source),
                        crate::home::age(now - a.at)
                    ))
                    .small()
                    .color(t.muted),
                );
                if ui
                    .small_button("Revoke")
                    .on_hover_text("The commits count from the PR's ready event again")
                    .clicked()
                {
                    revoke = true;
                }
            });
            if let Some(text) = a.text.as_deref().filter(|x| !x.is_empty()) {
                ui.label(RichText::new(text).small().color(t.text));
            }
            if let Some(note) = a.note.as_deref().filter(|x| !x.is_empty()) {
                ui.label(RichText::new(note).small().italics().color(t.muted));
            }
        });
    revoke
}

/// `accepted · confirmed by you`, `expired: head moved`.
pub fn attestation_state(a: &Attestation) -> String {
    let by = match a.confirmed_by.as_deref() {
        Some("user") => " · confirmed by you",
        Some("rule") => " · accepted by your config (confirm = \"none\")",
        _ => "",
    };
    match (a.state.as_str(), a.ended.as_deref()) {
        ("pending", _) => "waiting for your confirmation".into(),
        ("recorded", _) => "recorded: [ci] accept_attestation_for does not list it".into(),
        ("accepted", _) => format!("accepted{by}"),
        (state, Some(why)) => format!("{state}: {why}"),
        (state, None) => state.to_string(),
    }
}

/// The item's attestations (R5): the standing ones with Revoke, the
/// ended ones folded. Returns the one to revoke.
fn attestations(ui: &mut Ui, t: &Theme, list: &[Attestation], now: f64) -> Option<String> {
    if list.is_empty() {
        return None;
    }
    let mut revoke = None;
    let (standing, ended): (Vec<&Attestation>, Vec<&Attestation>) = list.iter().partition(|a| a.standing());
    ui.add_space(8.0);
    ui.label(RichText::new(format!("Attestations · {} standing", standing.len())).small().strong().color(t.muted));
    let row = |ui: &mut Ui, a: &Attestation, revoke: &mut Option<String>| {
        egui::Frame::new()
            .fill(t.surface)
            .stroke(Stroke::new(1.0, if a.standing() { t.cyan } else { t.border }))
            .corner_radius(CornerRadius::same(6))
            .inner_margin(egui::Margin::symmetric(10, 6))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new(&a.name).monospace().strong().color(t.text));
                    ui.label(
                        RichText::new(format!(
                            "attested by {} at {} · {} ago",
                            a.actor_text,
                            &a.sha[..a.sha.len().min(7)],
                            crate::home::age(now - a.at)
                        ))
                        .small()
                        .color(t.muted),
                    );
                    ui.label(RichText::new(attestation_state(a)).small().strong().color(t.cyan));
                    if a.standing()
                        && ui
                            .small_button("Revoke")
                            .on_hover_text("The check counts against the branch again (Fix CI comes back)")
                            .clicked()
                    {
                        *revoke = Some(a.id.clone());
                    }
                });
                ui.label(RichText::new(&a.text).small().color(t.text));
                if !a.range_text.is_empty() {
                    ui.label(RichText::new(&a.range_text).small().color(t.muted));
                }
            });
    };
    for a in standing {
        row(ui, a, &mut revoke);
    }
    if !ended.is_empty() {
        egui::CollapsingHeader::new(RichText::new(format!("Ended · {}", ended.len())).small().color(t.muted))
            .id_salt("why-attestations-ended")
            .default_open(false)
            .show(ui, |ui| {
                for a in ended {
                    row(ui, a, &mut revoke);
                }
            });
    }
    revoke
}

/// The open items of the line, top first, and which one leads.
fn items(ui: &mut Ui, t: &Theme, items: &[Value]) {
    if items.is_empty() {
        return;
    }
    ui.add_space(6.0);
    ui.label(RichText::new(format!("Its open inbox items · {}", items.len())).small().strong().color(t.muted));
    for (i, item) in items.iter().enumerate() {
        let state = item["state"].as_str().unwrap_or("").replace('_', " ");
        let kind = item["kind"].as_str().unwrap_or("?").replace('_', " ");
        let level = item["level"].as_str().unwrap_or("");
        let where_ = if level == "deep" {
            "To sit down with"
        } else if level == "now" {
            "Now"
        } else {
            "Round"
        };
        ui.horizontal_wrapped(|ui| {
            let lead = if i == 0 { "leads" } else { "" };
            ui.label(RichText::new(format!("{:>2}.", i + 1)).monospace().small().color(t.muted));
            ui.label(RichText::new(&state).color(t.text));
            ui.label(RichText::new(format!("· {kind} · {where_}")).small().color(t.muted));
            if !lead.is_empty() {
                ui.label(RichText::new(lead).small().strong().color(t.accent));
            }
        });
    }
}

/// The answer for people, on top: one sentence with its values.
fn answer(ui: &mut Ui, t: &Theme, e: &RulesExplain) {
    let Some(text) = e.answer.as_deref().filter(|a| !a.is_empty()) else { return };
    ui.add_space(6.0);
    egui::Frame::new()
        .fill(t.panel)
        .corner_radius(CornerRadius::same(6))
        .inner_margin(egui::Margin::symmetric(12, 10))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.add(egui::Label::new(RichText::new(text).size(15.0).color(t.text)).wrap());
        });
}

fn explained(ui: &mut Ui, t: &Theme, e: &RulesExplain, catalog: &lazyagent_proto::types::RulesCatalog) {
    answer(ui, t, e);
    items(ui, t, &e.items);
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

/// The rules above that did not match: the unknown ones (and the user's
/// own) first, as they are the suspicious ones.
pub fn above_order(rules: &[ExplainAbove]) -> Vec<&ExplainAbove> {
    let mut v: Vec<&ExplainAbove> = rules.iter().collect();
    v.sort_by_key(|a| match (a.value.as_str(), a.file.is_some()) {
        ("unknown", _) => 0,
        (_, true) => 1,
        ("item", _) => 2,
        _ => 3,
    });
    v
}

/// Folded by default (most questions never need them): the header says
/// how many, and how many of them are unknown.
fn above(ui: &mut Ui, t: &Theme, title: &str, rules: &[ExplainAbove]) {
    if rules.is_empty() {
        return;
    }
    ui.add_space(6.0);
    let unknown = rules.iter().filter(|a| a.value == "unknown").count();
    let head = if unknown > 0 {
        format!("{title} · {} ({unknown} unknown)", rules.len())
    } else {
        format!("{title} · {}", rules.len())
    };
    let colour = if unknown > 0 { t.yellow } else { t.muted };
    egui::CollapsingHeader::new(RichText::new(head).small().strong().color(colour))
        .id_salt(("why-above", title))
        .default_open(false)
        .show(ui, |ui| above_grid(ui, t, rules));
}

fn above_grid(ui: &mut Ui, t: &Theme, rules: &[ExplainAbove]) {
    egui::Grid::new(ui.next_auto_id()).num_columns(3).striped(true).spacing([14.0, 3.0]).show(ui, |ui| {
        for a in above_order(rules) {
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
