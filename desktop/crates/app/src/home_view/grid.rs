//! A Home line's head as one grid for every zone (docs/design/home-items.md):
//! title, next step, state, then PR, CI and a running agent as glyph
//! columns, then the age. The column names sit once above the zones.

use super::*;

/// The title without the PR number (a chip of its own here).
fn title(l: &Line) -> String {
    match l.row.as_ref().and_then(|r| r.pr.as_ref()) {
        Some(pr) => l.name.strip_suffix(&format!(" #{}", pr.number)).unwrap_or(&l.name).to_string(),
        None => l.name.clone(),
    }
}

fn zone_color(t: &Theme, shown: Zone) -> Color32 {
    match shown {
        Zone::Now => t.yellow,
        Zone::Deep => t.purple,
        _ => t.accent,
    }
}

fn inbox(shown: Zone) -> bool {
    matches!(shown, Zone::Now | Zone::Round | Zone::Deep)
}

fn delegated(l: &Line) -> Option<&lazyagent_proto::types::Delegation> {
    l.row.as_ref().and_then(|r| r.delegation.as_ref()).filter(|d| d.phase == "on_it")
}

/// Who has an In flight line, in one word.
fn owner(c: &LineCtx, l: &Line, shown: Zone) -> (&'static str, Color32) {
    let t = c.t;
    if l.claim.is_some() || l.hold().is_some() || (l.looking() && c.first_responder) {
        ("orchestrator", t.cyan)
    } else if shown == Zone::Parked && l.parked_needs_you() {
        ("needs you", t.yellow)
    } else if shown == Zone::Parked {
        ("parked", t.muted)
    } else if l.done() {
        ("done", t.muted)
    } else if delegated(l).is_some() {
        ("delegated", t.cyan)
    } else {
        match l.ball.as_str() {
            "agent" if l.running_agent().is_some() => ("working", t.cyan),
            "agent" => ("agent", t.cyan),
            "others" => ("waiting", t.purple),
            "stalled" => ("stalled", t.yellow),
            "you" => ("yours", t.accent),
            _ => ("", t.muted),
        }
    }
}

/// What the line asks of the user (its verb) or who has it.
fn status_word(c: &LineCtx, l: &Line, shown: Zone) -> (String, Color32) {
    if inbox(shown) {
        return (l.verb().to_string(), zone_color(c.t, shown));
    }
    let (word, color) = owner(c, l, shown);
    (word.to_string(), color)
}

/// The line's state in one short clause: the fact alone, no owner, no
/// agent's tool calls.
fn fact(c: &LineCtx, l: &Line, shown: Zone) -> String {
    if inbox(shown) {
        return inbox_text(c, l);
    }
    if let Some(claim) = &l.claim {
        return format!("looking · {} left", left(claim.until - c.now));
    }
    if let Some(hold) = l.hold() {
        return format!("{} · {} left", hold.note, left(hold.until - c.now));
    }
    if l.looking() && c.first_responder {
        return "looking".to_string();
    }
    if let Some(d) = delegated(l) {
        return format!("{} · agent {}", d.fact, d.agent);
    }
    if let Some(s) = l.row.as_ref().and_then(|r| r.suggestion.as_deref()) {
        return s.to_string();
    }
    // A running agent: its report, else its notes' line (the "next"
    // column already says "working").
    if let Some(w) = l.working() {
        return w.strip_prefix("working · ").map(str::to_string).unwrap_or_default();
    }
    humanize(&l.state)
}

/// The selection mark (and the seen dot) before a line.
fn mark(ui: &mut Ui, c: &LineCtx, l: &Line, selected: bool) {
    let t = c.t;
    let text = if selected {
        RichText::new("▸").color(t.primary)
    } else {
        RichText::new(if l.unseen(c.look) { "•" } else { " " }).strong().color(t.primary)
    };
    cell(ui, 10.0, text);
}

fn age_of(c: &LineCtx, l: &Line, shown: Zone) -> String {
    let since = if inbox(shown) { l.top.as_ref().map(|t| t.since).unwrap_or(l.changed_at) } else { l.changed_at };
    home::age(c.now - since)
}

const GRID_NEXT: f32 = 92.0;
const GRID_PR: f32 = 40.0;
const GRID_CI: f32 = 34.0;
const GRID_AGENT: f32 = 40.0;
const GRID_AGE: f32 = 30.0;

fn grid_title_w(ui: &Ui) -> f32 {
    (ui.available_width() * 0.32).clamp(NAME_MIN, 340.0)
}

/// The grid's column names, once above Home's zones.
pub(super) fn header(ui: &mut Ui, c: &LineCtx) {
    let t = c.t;
    ui.add_space(6.0);
    egui::Frame::new().inner_margin(egui::Margin { left: 10, right: 8, top: 0, bottom: 0 }).show(ui, |ui| {
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            let h = |s: &str| RichText::new(s).small().color(t.muted);
            cell(ui, 10.0 + 8.0 + 8.0, h(""));
            cell(ui, grid_title_w(ui), h("work item"));
            cell(ui, GRID_NEXT, h("next"));
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                cell(ui, GRID_AGE, h("age"));
                cell(ui, GRID_AGENT, h("agent"));
                cell(ui, GRID_CI, h("CI"));
                cell(ui, GRID_PR, h("PR"));
                ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                    ui.label(h("state"));
                });
            });
        });
    });
}

/// The line's head; `cut` as name_cell's.
pub(super) fn row(ui: &mut Ui, c: &LineCtx, l: &Line, shown: Zone, selected: bool, cut: &mut bool) -> egui::Response {
    let t = c.t;
    let row = l.row.as_ref();
    let pr = row.and_then(|r| r.pr.as_ref());
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 8.0;
        mark(ui, c, l, selected);
        priority_cell(ui, t, &l.priority);
        *cut = name_cell(ui, grid_title_w(ui), &title(l));
        let (word, color) = status_word(c, l, shown);
        let word = if inbox(shown) { RichText::new(word).strong() } else { RichText::new(word).small() };
        cell(ui, GRID_NEXT, word.color(color));
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            cell(ui, GRID_AGE, RichText::new(age_of(c, l, shown)).small().color(t.muted));
            // Only an agent that runs: an idle id says nothing at a glance.
            let agent = l.running_agent().map(|a| format!("{} ●", a.id)).unwrap_or_default();
            cell(ui, GRID_AGENT, RichText::new(agent).small().color(t.green));
            let ci = match (pr.map(|p| p.ci.as_str()), l.ci_note()) {
                (Some("red"), Some(_)) => ("✖", t.muted),
                (Some("red"), None) => ("✖", t.red),
                (Some("green"), _) => ("✔", t.green),
                (Some("running"), _) => ("…", t.muted),
                _ => ("", t.muted),
            };
            let r = ui
                .allocate_ui_with_layout(Vec2::new(GRID_CI, 18.0), Layout::left_to_right(Align::Center), |ui| {
                    ui.set_width(GRID_CI);
                    ui.label(RichText::new(ci.0).color(ci.1));
                })
                .response;
            if let Some(note) = l.ci_note() {
                r.on_hover_text(note);
            }
            let pr_text = pr.map(|p| format!("#{}", p.number)).unwrap_or_default();
            cell(ui, GRID_PR, RichText::new(pr_text).small().color(t.muted));
            ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                let mut text = fact(c, l, shown);
                if l.more > 0 {
                    text.push_str(&format!("  +{}", l.more));
                }
                let r = ui.add(egui::Label::new(RichText::new(text).color(t.muted)).truncate());
                if let Some(more) = l.more_text() {
                    r.on_hover_text(more);
                }
            });
        });
    })
    .response
}
