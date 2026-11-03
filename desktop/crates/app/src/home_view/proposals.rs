//! H2: simpler Home item layouts, behind a temporary dev-only switch
//! (`LAZYAGENT_HOME_ITEM_STYLE`) so the proposals in
//! docs/design/home-item-proposals.md render on the same data. Only the
//! line's head changes: the frame, the selection, the click and the
//! expanded details stay line_ui's. The one picked replaces `Current`; the
//! others go.

use super::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ItemStyle {
    /// The shipped layout.
    #[default]
    Current,
    /// (a) One line: verb or owner, title, age; the fact on selection.
    OneLine,
    /// (b) A two-line card: title and age; verb, fact and chips.
    Card,
    /// (c) In flight grouped by who has the ball; very short rows.
    ByBall,
    /// (d) One grid for every zone, title first, status as glyph columns.
    Grid,
}

impl ItemStyle {
    pub const ALL: [ItemStyle; 5] =
        [ItemStyle::Current, ItemStyle::OneLine, ItemStyle::Card, ItemStyle::ByBall, ItemStyle::Grid];

    pub fn name(self) -> &'static str {
        match self {
            ItemStyle::Current => "current",
            ItemStyle::OneLine => "a-oneline",
            ItemStyle::Card => "b-card",
            ItemStyle::ByBall => "c-ball",
            ItemStyle::Grid => "d-grid",
        }
    }

    /// `LAZYAGENT_HOME_ITEM_STYLE=<name>`; anything else is the current one.
    pub fn from_env() -> ItemStyle {
        let v = std::env::var("LAZYAGENT_HOME_ITEM_STYLE").unwrap_or_default();
        ItemStyle::ALL.into_iter().find(|s| s.name() == v).unwrap_or_default()
    }
}

/// The line's head in a proposal's layout; `cut` as name_cell's.
pub(super) fn head(
    ui: &mut Ui,
    c: &LineCtx,
    l: &Line,
    shown: Zone,
    selected: bool,
    next: bool,
    cut: &mut bool,
) -> egui::Response {
    match c.style {
        ItemStyle::Card => card(ui, c, l, shown, cut),
        ItemStyle::ByBall => short_row(ui, c, l, shown, cut),
        ItemStyle::Grid => grid_row(ui, c, l, shown, selected, cut),
        _ => one_line(ui, c, l, shown, selected, next, cut),
    }
}

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

/// Who has an In flight line, in one word, and its group in (c).
fn owner(c: &LineCtx, l: &Line, shown: Zone) -> (&'static str, Color32) {
    let t = c.t;
    if l.claim.is_some() || l.hold().is_some() || (l.looking() && c.first_responder) {
        ("orchestrator", t.cyan)
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
/// label (a chip), no agent's tool calls.
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
    if let Some(d) = delegated(l) {
        return format!("{} · agent {}", d.fact, d.agent);
    }
    if let Some(s) = l.row.as_ref().and_then(|r| r.suggestion.as_deref()) {
        return s.to_string();
    }
    if l.running_agent().is_some()
        && let Some(said) = l.report()
    {
        return home::middle_ellipsis(said, 80);
    }
    humanize(l.row.as_ref().map(|r| r.state.as_str()).unwrap_or(&l.state))
}

struct Chip {
    text: String,
    color: Color32,
    hover: String,
}

fn chip(text: impl Into<String>, color: Color32, hover: impl Into<String>) -> Chip {
    Chip { text: text.into(), color, hover: hover.into() }
}

/// The line's facts as short chips: label, PR, CI, review, agent, more.
fn chips(c: &LineCtx, l: &Line) -> Vec<Chip> {
    let t = c.t;
    let mut out = vec![];
    let Some(row) = &l.row else { return out };
    if let Some(label) = row.gesture.as_ref().filter(|g| g["kind"] == "label").and_then(|g| g["text"].as_str()) {
        out.push(chip(label, t.purple, "your label"));
    }
    if let Some(pr) = &row.pr {
        out.push(chip(format!("#{}", pr.number), t.muted, format!("PR #{}", pr.number)));
        match (pr.ci.as_str(), l.ci_note()) {
            ("red", Some(note)) => out.push(chip("CI ✖ unrelated", t.muted, note)),
            ("red", None) => out.push(chip("CI ✖", t.red, "CI red")),
            ("green", _) => out.push(chip("CI ✔", t.green, "CI green")),
            ("running", _) => out.push(chip("CI …", t.muted, "CI running")),
            _ => {}
        }
        match pr.review.as_str() {
            "CHANGES_REQUESTED" => out.push(chip("changes", t.yellow, "changes requested")),
            "APPROVED" => out.push(chip("approved", t.green, "approved")),
            "REVIEW_REQUIRED" => out.push(chip("in review", t.muted, "waiting for a review")),
            _ => {}
        }
    }
    if let Some(a) = l.running_agent() {
        out.push(chip(format!("{} ●", a.id), t.green, format!("agent {} running", a.id)));
    }
    if let Some(more) = l.more_text() {
        out.push(chip(format!("+{}", l.more), t.muted, more));
    }
    out
}

fn chip_ui(ui: &mut Ui, t: &Theme, c: &Chip, framed: bool) {
    let text = RichText::new(&c.text).small().color(c.color);
    let r = if framed {
        egui::Frame::new()
            .stroke(egui::Stroke::new(1.0, t.border))
            .corner_radius(egui::CornerRadius::same(8))
            .inner_margin(egui::Margin::symmetric(6, 0))
            .show(ui, |ui| ui.label(text))
            .response
    } else {
        ui.label(text)
    };
    r.on_hover_text(&c.hover);
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

/// (a) One line: the verb (or the owner), the title, the age. Selected,
/// a second line says the fact and the chips.
fn one_line(
    ui: &mut Ui,
    c: &LineCtx,
    l: &Line,
    shown: Zone,
    selected: bool,
    next: bool,
    cut: &mut bool,
) -> egui::Response {
    let t = c.t;
    ui.vertical(|ui| {
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            mark(ui, c, l, selected);
            let (word, color) = status_word(c, l, shown);
            let word = if inbox(shown) { RichText::new(word).strong() } else { RichText::new(word).small() };
            cell(ui, 104.0, word.color(color));
            priority_cell(ui, t, &l.priority);
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.label(RichText::new(age_of(c, l, shown)).small().color(t.muted));
                if next {
                    ui.label(RichText::new("next round").small().color(t.muted));
                }
                ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                    let w = (ui.available_width() - 16.0).max(NAME_MIN);
                    *cut = name_cell(ui, w, &title(l));
                });
            });
        });
        if selected {
            ui.horizontal(|ui| {
                ui.add_space(138.0);
                ui.spacing_mut().item_spacing.x = 6.0;
                for ch in chips(c, l) {
                    chip_ui(ui, t, &ch, false);
                }
                ui.add(egui::Label::new(RichText::new(fact(c, l, shown)).small().color(t.muted)).truncate());
            });
        }
    })
    .response
}

/// (b) A two-line card: the title and its age; then the verb (or owner),
/// the fact and the chips.
fn card(ui: &mut Ui, c: &LineCtx, l: &Line, shown: Zone, cut: &mut bool) -> egui::Response {
    let t = c.t;
    let stroke = if inbox(shown) { zone_color(t, shown).gamma_multiply(0.5) } else { t.border };
    egui::Frame::new()
        .stroke(egui::Stroke::new(1.0, stroke))
        .corner_radius(egui::CornerRadius::same(6))
        .inner_margin(egui::Margin::symmetric(10, 5))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                if !home::priority_marker(&l.priority).is_empty() {
                    priority_cell(ui, t, &l.priority);
                }
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    ui.label(RichText::new(age_of(c, l, shown)).small().color(t.muted));
                    if l.unseen(c.look) {
                        ui.label(RichText::new("•").strong().color(t.primary))
                            .on_hover_text("changed since you looked");
                    }
                    ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                        *cut = name_cell(ui, ui.available_width() - 8.0, &title(l));
                    });
                });
            });
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 6.0;
                let (word, color) = status_word(c, l, shown);
                ui.label(RichText::new(word).small().strong().color(color));
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    for ch in chips(c, l).iter().rev() {
                        chip_ui(ui, t, ch, true);
                    }
                    ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                        ui.add(egui::Label::new(RichText::new(fact(c, l, shown)).small().color(t.muted)).truncate());
                    });
                });
            });
        })
        .response
}

/// (c) A short row: the verb in the inbox (the group says who has an In
/// flight line), the title, the fact, the age.
fn short_row(ui: &mut Ui, c: &LineCtx, l: &Line, shown: Zone, cut: &mut bool) -> egui::Response {
    let t = c.t;
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 8.0;
        mark(ui, c, l, c.selected == Some(l.sel().as_str()));
        if inbox(shown) {
            cell(ui, 96.0, RichText::new(l.verb()).strong().color(zone_color(t, shown)));
        } else {
            // by_ball paints who has it there; the titles stay in one column.
            ui.add_space(96.0 + 8.0);
        }
        let name_w = (ui.available_width() * 0.36).clamp(NAME_MIN, 340.0);
        *cut = name_cell(ui, name_w, &title(l));
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            ui.label(RichText::new(age_of(c, l, shown)).small().color(t.muted));
            ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                ui.add(egui::Label::new(RichText::new(fact(c, l, shown)).color(t.muted)).truncate());
            });
        });
    })
    .response
}

/// (c) The In flight groups, in this order: who has the ball.
const GROUPS: [(&str, &str); 6] = [
    ("working", "Agents"),
    ("orchestrator", "Orchestrator"),
    ("waiting", "Waiting on others"),
    ("stalled", "Stalled"),
    ("done", "Done"),
    ("", "Other"),
];

fn group_of(c: &LineCtx, l: &Line) -> usize {
    let word = match owner(c, l, Zone::Flight).0 {
        "agent" | "delegated" => "working",
        w => w,
    };
    GROUPS.iter().position(|(w, _)| *w == word).unwrap_or(GROUPS.len() - 1)
}

/// (c) In flight as groups by who has the ball: the group's name in the
/// verb column of its first line (the inbox's verbs are the user's).
pub(super) fn by_ball(ui: &mut Ui, c: &LineCtx, lines: &[Line], forms: &mut Forms, acts: &mut Vec<Act>) {
    for (g, (_, name)) in GROUPS.iter().enumerate() {
        let group: Vec<&Line> = lines.iter().filter(|l| group_of(c, l) == g).collect();
        let Some(first) = group.first() else { continue };
        ui.add_space(4.0);
        let top = ui.cursor().min;
        for l in &group {
            line_ui(ui, c, l, Zone::Flight, false, forms, acts);
        }
        // Painted, not laid out: the line's own frame stays one surface.
        let color = owner(c, first, Zone::Flight).1;
        let text = format!("{name} · {}", group.len());
        ui.painter().text(
            top + Vec2::new(28.0, 12.0),
            egui::Align2::LEFT_CENTER,
            text,
            egui::TextStyle::Small.resolve(ui.style()),
            color,
        );
    }
}

const GRID_NEXT: f32 = 92.0;
const GRID_PR: f32 = 40.0;
const GRID_CI: f32 = 34.0;
const GRID_AGENT: f32 = 40.0;
const GRID_AGE: f32 = 30.0;

fn grid_title_w(ui: &Ui) -> f32 {
    (ui.available_width() * 0.32).clamp(NAME_MIN, 340.0)
}

/// (d) The grid's column names, once above Home's zones.
pub(super) fn grid_header(ui: &mut Ui, c: &LineCtx) {
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

/// (d) One grid for every zone: title, next step, state, then PR, CI and
/// agent as glyph columns, then the age.
fn grid_row(ui: &mut Ui, c: &LineCtx, l: &Line, shown: Zone, selected: bool, cut: &mut bool) -> egui::Response {
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
