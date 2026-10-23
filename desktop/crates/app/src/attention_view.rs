//! The PR/CI panel (ui-plan §3a) and the PR details Home's lines show; the
//! attention list itself became Home (home.rs, home_view.rs).

use eframe::egui::{self, Align, Color32, Layout, RichText, Ui};
use lazyagent_proto::types::{PrInfo, PrInfoResult, SendInputResult};

use crate::app::App;
use crate::store::ORCHESTRATOR_KEY;
use crate::theme::Theme;

/// Minute-level, never "0s" (home.rs `age`).
pub(crate) fn age(since: f64, now: f64) -> String {
    crate::home::age(now - since)
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
