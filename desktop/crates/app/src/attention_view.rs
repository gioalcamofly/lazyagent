//! The attention view (ui-plan §3b) and the PR/CI panel (§3a), drawn by the
//! app. The queue is derived in `attention.rs`; here are its list, the
//! picked item (the agent's live terminal and a reply box, or the PR and
//! its checks, an exit, a job) and the panel of the selected worktree's PR.

use eframe::egui::{self, Align, Color32, CornerRadius, Layout, RichText, Sense, Stroke, TextEdit, Ui, Vec2};
use lazyagent_proto::types::{PrInfo, PrInfoResult, SendInputResult};
use serde_json::json;

use crate::app::{App, Focus, Severity, View};
use crate::attention::{self, Item, Kind};
use crate::panes::PaneKind;
use crate::store::{self, ORCHESTRATOR_KEY};
use crate::theme::Theme;

fn kind_color(t: &Theme, kind: Kind) -> Color32 {
    match kind.rank() {
        1 | 2 => t.yellow,
        3 | 4 => t.red,
        5 => t.purple,
        6 => t.cyan,
        _ => t.muted,
    }
}

pub(crate) fn age(since: f64, now: f64) -> String {
    let s = (now - since).max(0.0) as u64;
    match s {
        0..=59 => format!("{s}s"),
        60..=3599 => format!("{}m", s / 60),
        _ => format!("{}h", s / 3600),
    }
}

/// A check's mark and colour.
fn check_mark(t: &Theme, status: &str, conclusion: &str) -> (&'static str, Color32) {
    match (status.to_ascii_uppercase().as_str(), conclusion.to_ascii_uppercase().as_str()) {
        (_, "SUCCESS") => ("✔", t.green),
        (_, "FAILURE") | (_, "TIMED_OUT") | (_, "CANCELLED") => ("✖", t.red),
        (_, "SKIPPED") | (_, "NEUTRAL") => ("·", t.muted),
        _ => ("…", t.yellow),
    }
}

pub fn pr_details(ui: &mut Ui, t: &Theme, pr: &PrInfo) {
    match &pr.url {
        Some(url) if !url.is_empty() => {
            ui.hyperlink_to(RichText::new(format!("#{} {}", pr.number, pr.title)).strong(), url);
        }
        _ => {
            ui.label(RichText::new(format!("#{} {}", pr.number, pr.title)).strong());
        }
    }
    egui::Grid::new(("pr", pr.number)).num_columns(2).spacing([10.0, 3.0]).show(ui, |ui| {
        let state_color = match pr.state.as_str() {
            "OPEN" => t.green,
            "MERGED" => t.purple,
            _ => t.muted,
        };
        ui.label(RichText::new("State").color(t.muted));
        ui.label(RichText::new(&pr.state).color(state_color));
        ui.end_row();
        if let Some(r) = pr.review_decision.as_deref().filter(|r| !r.is_empty()) {
            ui.label(RichText::new("Review").color(t.muted));
            let c = if r == "APPROVED" {
                t.green
            } else if r == "CHANGES_REQUESTED" {
                t.red
            } else {
                t.yellow
            };
            ui.label(RichText::new(r.replace('_', " ").to_lowercase()).color(c));
            ui.end_row();
        }
        if let Some(m) = pr.mergeable.as_deref().filter(|m| !m.is_empty()) {
            ui.label(RichText::new("Mergeable").color(t.muted));
            let c = if m == "MERGEABLE" {
                t.green
            } else if m == "CONFLICTING" {
                t.red
            } else {
                t.muted
            };
            ui.label(RichText::new(m.to_lowercase()).color(c));
            ui.end_row();
        }
    });
    if pr.checks.is_empty() {
        ui.label(RichText::new("No checks").color(t.muted));
        return;
    }
    let passed = pr.checks.iter().filter(|c| c.conclusion.eq_ignore_ascii_case("SUCCESS")).count();
    let failed = pr.checks.iter().filter(|c| c.conclusion.eq_ignore_ascii_case("FAILURE")).count();
    ui.add_space(4.0);
    ui.label(
        RichText::new(format!("Checks: {passed} ✔ · {failed} ✖ · {} total", pr.checks.len())).small().color(t.muted),
    );
    for c in &pr.checks {
        ui.horizontal(|ui| {
            let (mark, color) = check_mark(t, &c.status, &c.conclusion);
            ui.label(RichText::new(mark).color(color));
            match c.details_url.as_deref().filter(|u| !u.is_empty()) {
                Some(url) => {
                    ui.hyperlink_to(&c.name, url);
                }
                None => {
                    ui.label(&c.name);
                }
            }
        });
    }
}

impl App {
    /// The queue on the left, the picked item on the right.
    pub(crate) fn attention_view(&mut self, ui: &mut Ui, ctx: &egui::Context) {
        let t = self.state.theme;
        let now = crate::app::unix_now();
        let items = self.state.attention.clone();
        if !self.state.att_selected.as_ref().is_some_and(|id| items.iter().any(|i| &i.id == id)) {
            self.state.att_selected = items.first().map(|i| i.id.clone());
        }
        let body = ui.available_rect_before_wrap();
        let list_w = 340.0f32.min(body.width() * 0.4);
        let list = egui::Rect::from_min_size(body.min, Vec2::new(list_w, body.height()));
        let detail = egui::Rect::from_min_max(egui::pos2(list.max.x + 8.0, body.min.y), body.max);
        let mut list_ui = ui.new_child(egui::UiBuilder::new().max_rect(list).id_salt("attention-list"));
        list_ui.label(RichText::new(format!("Needs you ({})", items.len())).strong());
        list_ui.add_space(4.0);
        if items.is_empty() {
            list_ui.label(RichText::new("Nothing needs you right now.").color(t.muted));
        }
        egui::ScrollArea::vertical().id_salt("attention-scroll").show(&mut list_ui, |ui| {
            for item in &items {
                let selected = self.state.att_selected.as_deref() == Some(item.id.as_str());
                let fill = if selected { t.current_bg } else { t.surface };
                let resp = egui::Frame::new()
                    .fill(fill)
                    .stroke(Stroke::new(1.0, if selected { t.accent } else { t.border }))
                    .corner_radius(CornerRadius::same(4))
                    .inner_margin(egui::Margin::symmetric(8, 5))
                    .show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.horizontal(|ui| {
                            ui.label(
                                RichText::new(item.kind.title()).small().strong().color(kind_color(&t, item.kind)),
                            );
                            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                                ui.label(RichText::new(age(item.since, now)).small().color(t.muted));
                            });
                        });
                        let place =
                            if item.key.is_empty() { "job".to_string() } else { self.state.place_name(&item.key) };
                        ui.add(egui::Label::new(RichText::new(format!("{} · {place}", item.label))).truncate());
                        ui.add(egui::Label::new(RichText::new(&item.detail).small().color(t.muted)).truncate());
                    })
                    .response
                    .interact(Sense::click());
                if resp.clicked() {
                    self.state.att_selected = Some(item.id.clone());
                }
                ui.add_space(4.0);
            }
        });
        let Some(item) = self.state.att_selected.clone().and_then(|id| items.into_iter().find(|i| i.id == id)) else {
            return;
        };
        let mut d = ui.new_child(egui::UiBuilder::new().max_rect(detail).id_salt(("attention-detail", &item.id)));
        self.attention_detail(&mut d, ctx, &item);
    }

    fn attention_detail(&mut self, ui: &mut Ui, ctx: &egui::Context, item: &Item) {
        let t = self.state.theme;
        let place = if item.key.is_empty() { "job".to_string() } else { self.state.place_name(&item.key) };
        ui.horizontal(|ui| {
            ui.label(RichText::new(item.kind.title()).strong().color(kind_color(&t, item.kind)));
            ui.label(RichText::new(format!("{} · {place}", item.label)).strong());
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("Dismiss").on_hover_text("Hide until it happens again").clicked() {
                    self.state.dismiss(item);
                }
                if ui.button("Snooze 1h").clicked() {
                    self.state.snooze(item, 3600.0);
                }
                if item.agent_id.is_some() && item.kind != Kind::Exited {
                    if ui.button("Stop").clicked() {
                        self.state.selected = Some(item.key.clone());
                        self.state.tabs.insert(item.key.clone(), item.agent_id.clone().unwrap());
                        self.state.open_dialog(crate::dialogs::Kind::Stop);
                    }
                    if ui.button("Open").on_hover_text("In the worktree view").clicked() {
                        self.state.selected = Some(item.key.clone());
                        self.state.tabs.insert(item.key.clone(), item.agent_id.clone().unwrap());
                        self.state.view = View::Worktrees;
                        self.state.focus_request = Some(Focus::Agent);
                    }
                }
            });
        });
        ui.label(RichText::new(&item.detail).color(t.muted));
        ui.add_space(4.0);
        match item.kind {
            Kind::Ci | Kind::Review | Kind::Conflict => {
                let pr = item.pr.clone().unwrap();
                pr_details(ui, &t, &pr);
                ui.add_space(6.0);
                let target = self.state.store.agents(&item.key).first().cloned();
                match (item.kind, target) {
                    (Kind::Ci, Some(agent)) => {
                        if ui.button(format!("Send the failures to {}", store::agent_label(&agent))).clicked() {
                            let text = attention::ci_failure_prompt(&pr);
                            self.state.send_reply(&item.key, &agent.agent_id, text);
                        }
                    }
                    (Kind::Ci, None) => {
                        ui.label(RichText::new("No agent in this worktree to send the failures to.").color(t.muted));
                    }
                    _ => {}
                }
            }
            Kind::Exited => {
                if ui.button("Spawn again (resume the last session)").clicked() {
                    let mut params =
                        json!({"resume_mode": "last", "skip_permissions": false, "env": self.state.env_json()});
                    if item.key == ORCHESTRATOR_KEY {
                        params["kind"] = json!("orchestrator");
                    } else {
                        params["worktree_path"] = json!(item.key);
                    }
                    let key = item.key.clone();
                    let exit = item.exit.clone();
                    self.state.conn.clone().request("spawn_agent", params, move |s, reply| match reply {
                        Ok(v) => {
                            s.exits.retain(|e| Some(e) != exit.as_ref());
                            s.spawned(&key, &v);
                        }
                        Err(e) => s.toast(Severity::Error, format!("Could not spawn: {e}")),
                    });
                }
            }
            Kind::Job => {
                if let Some(job) = item.job.as_ref().and_then(|id| self.state.jobs.iter().find(|j| &j.id == id)) {
                    ui.label(RichText::new(format!("$ {}", job.command)).monospace().color(t.muted));
                    egui::ScrollArea::vertical().stick_to_bottom(true).show(ui, |ui| {
                        ui.label(RichText::new(&job.output).monospace());
                    });
                }
            }
            _ => self.attention_agent(ui, ctx, item),
        }
    }

    /// An agent's item: its live terminal and a reply box.
    fn attention_agent(&mut self, ui: &mut Ui, ctx: &egui::Context, item: &Item) {
        let t = self.state.theme;
        let agent_id = item.agent_id.clone().unwrap_or_default();
        let session = self.state.store.agent(&item.key, &agent_id).and_then(|a| a.session_id.clone());
        let reply_h = 96.0;
        let body = ui.available_rect_before_wrap();
        let term = egui::Rect::from_min_max(body.min, egui::pos2(body.max.x, body.max.y - reply_h));
        let mut term_ui = ui.new_child(egui::UiBuilder::new().max_rect(term).id_salt("attention-terminal"));
        match session {
            Some(s) => self.terminal(&mut term_ui, &s, PaneKind::Agent, Focus::Agent, ctx),
            None => {
                term_ui.label(RichText::new("The agent's terminal is not available.").color(t.muted));
            }
        }
        let reply = egui::Rect::from_min_max(egui::pos2(body.min.x, body.max.y - reply_h + 6.0), body.max);
        let mut reply_ui = ui.new_child(egui::UiBuilder::new().max_rect(reply).id_salt("attention-reply"));
        reply_ui.horizontal(|ui| {
            let edit = ui.add(
                TextEdit::multiline(&mut self.state.reply)
                    .desired_rows(3)
                    .desired_width(ui.available_width() - 90.0)
                    .hint_text(format!("Reply to {} (Ctrl+Enter)", item.label)),
            );
            let ctrl_enter =
                edit.has_focus() && ui.input_mut(|i| i.consume_key(egui::Modifiers::CTRL, egui::Key::Enter));
            let send = ui.add_enabled(!self.state.reply.trim().is_empty(), egui::Button::new("Send"));
            if (send.clicked() || ctrl_enter) && !self.state.reply.trim().is_empty() {
                let text = std::mem::take(&mut self.state.reply).trim().to_string();
                self.state.send_reply(&item.key, &agent_id, text);
            }
        });
    }

    /// The selected worktree's PR and checks.
    pub(crate) fn pr_panel(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let Some(path) = self.state.selected.clone().filter(|p| p != ORCHESTRATOR_KEY) else {
            ui.label(RichText::new("Select a worktree").color(t.muted));
            return;
        };
        ui.horizontal(|ui| {
            ui.label(RichText::new("Pull request").strong());
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("✖").on_hover_text("Close (Ctrl+.)").clicked() {
                    self.state.pr_panel = false;
                }
                if ui.button("↻").on_hover_text("Fetch again").clicked() {
                    self.state.fetch_pr(&path, true);
                }
            });
        });
        ui.separator();
        match self.state.store.pr_info.get(&path).cloned() {
            Some(Some(pr)) => pr_details(ui, &t, &pr),
            Some(None) => {
                let branch = self.state.store.worktree(&path).and_then(|w| w.branch.clone()).unwrap_or_default();
                ui.label(RichText::new(format!("No PR for {branch}")).color(t.muted));
                ui.label(RichText::new("gh pr create opens one.").small().color(t.muted));
            }
            None => match self.state.pr_fetched.get(&path) {
                Some(false) => {
                    ui.label(RichText::new("GitHub CLI not available").color(t.muted));
                }
                Some(true) => {
                    ui.label(RichText::new("No PR").color(t.muted));
                }
                None => {
                    self.state.fetch_pr(&path, false);
                    ui.spinner();
                }
            },
        }
    }
}

pub fn pr_result(v: serde_json::Value) -> Option<PrInfoResult> {
    serde_json::from_value(v).ok()
}

pub fn delivery_text(v: serde_json::Value) -> String {
    match serde_json::from_value::<SendInputResult>(v) {
        Ok(r) => match r.delivery.as_str() {
            "confirmed" => "Sent".into(),
            "busy" => "Sent; the agent was busy, it will read it when it is free".into(),
            "unconfirmed" => "Sent (not confirmed by the agent)".into(),
            other => format!("Sent ({other})"),
        },
        Err(_) => "Sent".into(),
    }
}
