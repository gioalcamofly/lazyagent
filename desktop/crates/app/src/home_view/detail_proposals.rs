//! H2, second round: simpler expanded items, behind a temporary dev-only
//! switch (`LAZYAGENT_HOME_DETAIL_STYLE`) so the proposals in
//! docs/design/home-detail-proposals.md render on the same data. Every
//! variant is made of the blocks `details` draws today (home_view.rs), in
//! another order and with less shown at first. The one picked replaces
//! `Current`; the others go.

use super::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DetailStyle {
    /// The shipped details.
    #[default]
    Current,
    /// (a) The ask, its answer, one facts line and three actions; the
    /// rest behind "Show more".
    Summary,
    /// (b) Two columns: what it is on the left, what to do on the right.
    Columns,
    /// (c) The ask open; every other block a collapsed section.
    Sections,
    /// (d) The actions first, then the ask, then a facts table.
    ActionFirst,
}

impl DetailStyle {
    pub const ALL: [DetailStyle; 5] = [
        DetailStyle::Current,
        DetailStyle::Summary,
        DetailStyle::Columns,
        DetailStyle::Sections,
        DetailStyle::ActionFirst,
    ];

    pub fn name(self) -> &'static str {
        match self {
            DetailStyle::Current => "current",
            DetailStyle::Summary => "a-summary",
            DetailStyle::Columns => "b-columns",
            DetailStyle::Sections => "c-sections",
            DetailStyle::ActionFirst => "d-actions",
        }
    }

    /// `LAZYAGENT_HOME_DETAIL_STYLE=<name>`; anything else is the current one.
    pub fn from_env() -> DetailStyle {
        let v = std::env::var("LAZYAGENT_HOME_DETAIL_STYLE").unwrap_or_default();
        DetailStyle::ALL.into_iter().find(|s| s.name() == v).unwrap_or_default()
    }
}

/// The expanded item in a proposal's layout.
pub(super) fn details(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    match c.detail {
        DetailStyle::Columns => columns(ui, c, l, forms, focus, acts),
        DetailStyle::Sections => sections(ui, c, l, forms, focus, acts),
        DetailStyle::ActionFirst => action_first(ui, c, l, forms, focus, acts),
        _ => summary(ui, c, l, forms, focus, acts),
    }
    forms_block(ui, c, l, forms, focus, acts);
}

/// The row's facts as short (name, value) pairs: who works on it, its PR,
/// its items, who else has it, its notes, since you looked.
fn facts(c: &LineCtx, l: &Line) -> Vec<(&'static str, String)> {
    let mut out = vec![];
    let Some(row) = &l.row else { return out };
    if let Some(a) = row.agents.first() {
        let status = humanize(store::status_text(&a.status));
        out.push(("agent", format!("{} · {status} {}", a.id, home::age(c.now - a.since))));
    }
    if let Some(pr) = &row.pr {
        let mut text = format!("#{}", pr.number);
        let review = pr.review.replace('_', " ").to_lowercase();
        if !review.is_empty() {
            text.push_str(&format!(" · {review}"));
        }
        text.push_str(&format!(" · CI {}", pr.ci));
        if let Some(note) = l.ci_note() {
            text.push_str(&format!(" ({note})"));
        }
        out.push(("PR", text));
    }
    if l.items().len() > 1 {
        let words: Vec<String> = l.items().iter().map(|i| home::kind_verb(&i.kind).to_lowercase()).collect();
        out.push(("items", format!("{}: {}", words.len(), words.join(", "))));
    }
    if let Some(hold) = l.hold() {
        out.push(("held", format!("{} · {} more", hold.note, left(hold.until - c.now))));
    }
    if let Some(d) = row.delegation.as_ref().filter(|d| d.phase == "on_it") {
        out.push(("delegated", format!("to {} {} ago: {}", d.agent, home::age(c.now - d.at), d.fact)));
    }
    if let Some(note) = &row.note {
        out.push(("notes", format!("\"{}\" ({})", note.text, home::age(c.now - note.at))));
    }
    if let Some(tl) = c.timelines.get(&l.key).filter(|tl| !tl.entries.is_empty()) {
        let last = tl.entries.last().map(|e| timeline_line(e, c.now)).unwrap_or_default();
        out.push(("since you looked", format!("{} · last: {last}", tl.entries.len())));
    }
    out
}

/// The facts as one muted line.
fn facts_line(ui: &mut Ui, c: &LineCtx, l: &Line) {
    let text: Vec<String> = facts(c, l)
        .into_iter()
        .filter(|(k, _)| !matches!(*k, "since you looked" | "notes"))
        .map(|(k, v)| if k == "agent" || k == "PR" { v } else { format!("{k} {v}") })
        .collect();
    if !text.is_empty() {
        ui.add(egui::Label::new(RichText::new(text.join("  ·  ")).small().color(c.t.muted)).truncate());
    }
}

/// The facts as a two-column table.
fn facts_table(ui: &mut Ui, c: &LineCtx, l: &Line) {
    let t = c.t;
    egui::Grid::new(("home-facts", l.sel())).num_columns(2).spacing([12.0, 2.0]).show(ui, |ui| {
        for (k, v) in facts(c, l) {
            ui.label(RichText::new(k).small().color(t.muted));
            ui.add(egui::Label::new(RichText::new(v).small().color(t.text)).truncate());
            ui.end_row();
        }
    });
}

/// The item's actions with the one its verb asks for first (Delegate on
/// a red CI), and Pass, while it cannot be used, under "More…" with its
/// reason on hover instead of a line of its own.
fn ordered_buttons(c: &LineCtx, l: &Line) -> Vec<Button> {
    let mut buttons = detail_buttons(c, l);
    let delegate = |b: &Button| {
        matches!(b.act, Act::Delegate(_) | Act::SendCi(_) | Act::RestoreDelegate(_) | Act::SpawnDelegate(_))
    };
    if let Some(i) = buttons.iter().position(delegate) {
        let b = buttons.remove(i);
        buttons.insert(0, b);
    }
    for b in buttons.iter_mut().filter(|b| matches!(b.act, Act::Pass(_)) && !b.enabled) {
        b.more = true;
        b.hover = c.pass.err();
    }
    buttons
}

/// The first `n` actions as buttons (the first one stressed), the rest
/// and every "more" one under "More…".
fn actions(ui: &mut Ui, c: &LineCtx, l: &Line, n: usize, acts: &mut Vec<Act>) {
    let t = c.t;
    let (mut shown, mut rest): (Vec<Button>, Vec<Button>) = (vec![], vec![]);
    for b in ordered_buttons(c, l) {
        if !b.more && shown.len() < n { shown.push(b) } else { rest.push(b) }
    }
    // The answer row (Approve/Deny) is the primary action: no second one.
    let answered = matches!(l.top.as_ref().map(|t| t.kind.as_str()), Some("approval" | "question" | "decision"));
    ui.horizontal_wrapped(|ui| {
        for (i, b) in shown.into_iter().enumerate() {
            let mut text = RichText::new(b.text);
            let mut button = egui::Button::new(text.clone());
            if i == 0 && !answered {
                text = text.strong().color(t.text);
                button = egui::Button::new(text).fill(t.select_bg).stroke(egui::Stroke::new(1.0, t.primary));
            }
            let mut r = ui.add_enabled(b.enabled, button);
            if let Some(h) = b.hover {
                r = r.on_hover_text(h);
            }
            if r.clicked() {
                acts.push(b.act);
            }
        }
        more_menu(ui, rest, acts);
    });
}

fn more_menu(ui: &mut Ui, rest: Vec<Button>, acts: &mut Vec<Act>) {
    if rest.is_empty() {
        return;
    }
    ui.menu_button("More…", |ui| {
        ui.set_min_width(220.0);
        for b in rest {
            let mut r = ui.add_enabled(b.enabled, egui::Button::new(b.text));
            if let Some(h) = b.hover {
                r = r.on_hover_text(h);
            }
            if r.clicked() {
                acts.push(b.act);
                ui.close();
            }
        }
    });
}

/// The blocks a summary leaves for later: items, the failed log, since
/// you looked, every fact, who else has it.
fn the_rest(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    items_block(ui, c, l, forms, acts);
    ci_log_block(ui, c, l, acts);
    timeline_block(ui, c, l);
    moved_lines(ui, c, l);
    facts_block(ui, c, l);
    explain_block(ui, c, l);
    owner_block(ui, c, l, forms, focus, acts);
}

/// A "Show more ▸" toggle kept per line (egui memory: a proposal only).
fn more_toggle(ui: &mut Ui, c: &LineCtx, l: &Line, what: &str) -> bool {
    let id = egui::Id::unique(("home-more", l.sel()));
    let mut open = ui.data(|d| d.get_temp::<bool>(id)).unwrap_or(false);
    let arrow = if open { "▾" } else { "▸" };
    let text = RichText::new(format!("{} {arrow}  {what}", if open { "Show less" } else { "Show more" }));
    if ui.add(egui::Button::new(text.small().color(c.t.muted)).frame(false)).clicked() {
        open = !open;
        ui.data_mut(|d| d.insert_temp(id, open));
    }
    open
}

/// A red CI's ask: the failed job and its first two lines (the whole log
/// is one "Show more" away).
fn ci_head(ui: &mut Ui, c: &LineCtx, l: &Line) {
    let t = c.t;
    if !l.top.as_ref().is_some_and(|t| t.is_ci()) {
        return;
    }
    let Some((_, Some(log))) = c.ci_logs.get(&l.key) else { return };
    let Some(job) = log.jobs.first() else { return };
    let lines: Vec<&str> = job.lines.iter().take(2).map(String::as_str).collect();
    egui::Frame::new().fill(t.panel).corner_radius(4).inner_margin(6).show(ui, |ui| {
        ui.label(RichText::new(format!("{} · {}", job.name, job.step)).small().strong().color(t.text));
        for line in lines {
            ui.add(egui::Label::new(RichText::new(line).small().monospace().color(t.text)).truncate());
        }
    });
}

/// (a) Progressive disclosure.
fn summary(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    ask_block(ui, c, l, forms, focus, acts);
    ci_head(ui, c, l);
    answer_block(ui, c, l, forms, focus, acts);
    facts_line(ui, c, l);
    actions(ui, c, l, 3, acts);
    if more_toggle(ui, c, l, &rest_words(c, l)) {
        the_rest(ui, c, l, forms, focus, acts);
    }
}

/// What "Show more" holds, in words: "2 items · failed log · 3 since you looked".
fn rest_words(c: &LineCtx, l: &Line) -> String {
    let mut words = vec![];
    if l.items().len() > 1 {
        words.push(format!("{} items", l.items().len()));
    }
    if c.actions && l.top.as_ref().is_some_and(|t| t.is_ci()) {
        words.push("failed log".to_string());
    }
    if let Some(tl) = c.timelines.get(&l.key).filter(|tl| !tl.entries.is_empty()) {
        words.push(format!("{} since you looked", tl.entries.len()));
    }
    words.push("facts".to_string());
    words.join(" · ")
}

pub const COLUMN_RIGHT: f32 = 230.0;

/// (b) Two columns: the ask, the log and the timeline on the left; the
/// answer, the actions and the facts on the right.
fn columns(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    let t = c.t;
    let right = COLUMN_RIGHT.min(ui.available_width() * 0.4);
    let left = ui.available_width() - right - 24.0;
    ui.horizontal_top(|ui| {
        ui.allocate_ui_with_layout(Vec2::new(left, 0.0), Layout::top_down(Align::Min), |ui| {
            ui.set_width(left);
            ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Wrap);
            ask_block(ui, c, l, forms, focus, acts);
            items_block(ui, c, l, forms, acts);
            ci_log_block(ui, c, l, acts);
            timeline_block(ui, c, l);
            moved_lines(ui, c, l);
        });
        ui.add_space(8.0);
        ui.allocate_ui_with_layout(Vec2::new(right, 0.0), Layout::top_down(Align::Min), |ui| {
            ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Wrap);
            egui::Frame::new().fill(t.panel).corner_radius(4).inner_margin(8).show(ui, |ui| {
                let w = right - 16.0;
                ui.set_width(w);
                let wide = |text: RichText| egui::Button::new(text).min_size(Vec2::new(w, 0.0));
                if l.top.as_ref().is_some_and(|t| t.kind == "approval") && c.approvals {
                    // The answer is the primary action: one button each, full width.
                    if ui.add(wide(RichText::new("Approve  (a)").strong().color(t.green))).clicked() {
                        acts.push(Act::Approve(l.clone(), true));
                    }
                    if ui.add(wide(RichText::new("Deny  (⇧D)").color(t.red))).clicked() {
                        acts.push(Act::Approve(l.clone(), false));
                    }
                } else {
                    answer_block(ui, c, l, forms, focus, acts);
                }
                let (mut shown, mut rest): (Vec<Button>, Vec<Button>) = (vec![], vec![]);
                for b in ordered_buttons(c, l) {
                    if !b.more && shown.len() < 4 { shown.push(b) } else { rest.push(b) }
                }
                for b in shown {
                    if ui.add_enabled(b.enabled, wide(RichText::new(b.text))).clicked() {
                        acts.push(b.act);
                    }
                }
                more_menu(ui, rest, acts);
                ui.add_space(6.0);
                for (k, v) in facts(c, l).into_iter().filter(|(k, _)| *k != "since you looked") {
                    ui.label(RichText::new(format!("{k}: {v}")).small().color(t.muted));
                }
            });
        });
    });
}

/// (c) The ask open; the rest as sections, collapsed, their summary in
/// the header.
fn sections(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    let t = c.t;
    ask_block(ui, c, l, forms, focus, acts);
    answer_block(ui, c, l, forms, focus, acts);
    let sel = l.sel();
    let header = |s: String| RichText::new(s).small().strong().color(t.muted);
    if l.items().len() > 1 {
        let words: Vec<String> = l.items().iter().map(|i| home::kind_verb(&i.kind).to_lowercase()).collect();
        egui::CollapsingHeader::new(header(format!("Items · {}", words.join(", "))))
            .id_salt(("sec-items", &sel))
            .show(ui, |ui| items_block(ui, c, l, forms, acts));
    }
    if c.actions
        && let Some(top) = l.top.as_ref().filter(|t| t.is_ci())
    {
        egui::CollapsingHeader::new(header(format!("CI · {}", top.ci_text().unwrap_or_else(|| "failed log".into()))))
            .id_salt(("sec-ci", &sel))
            .show(ui, |ui| ci_log_block(ui, c, l, acts));
    }
    let pairs = facts(c, l);
    if let Some((_, pr)) = pairs.iter().find(|(k, _)| *k == "PR") {
        egui::CollapsingHeader::new(header(format!("PR {pr}")))
            .id_salt(("sec-pr", &sel))
            .show(ui, |ui| facts_block(ui, c, l));
    }
    if let Some(tl) = c.timelines.get(&l.key).filter(|tl| !tl.entries.is_empty()) {
        egui::CollapsingHeader::new(header(format!("Since you looked · {}", tl.entries.len())))
            .id_salt(("sec-tl", &sel))
            .show(ui, |ui| timeline_block(ui, c, l));
    }
    egui::CollapsingHeader::new(header("Notes, owner, why".to_string())).id_salt(("sec-more", &sel)).show(ui, |ui| {
        moved_lines(ui, c, l);
        explain_block(ui, c, l);
        owner_block(ui, c, l, forms, focus, acts);
    });
    buttons_bar(ui, ordered_buttons(c, l), acts);
}

/// (d) Actions first: the primary and two more, then the ask, then the
/// facts as a table; the log and the timeline behind "Show more".
fn action_first(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    // A brief's options are its actions: the brief comes first.
    let brief = l.top.as_ref().is_some_and(|t| t.kind == "decision");
    if brief {
        ask_block(ui, c, l, forms, focus, acts);
    }
    answer_block(ui, c, l, forms, focus, acts);
    actions(ui, c, l, 3, acts);
    ui.add_space(2.0);
    if !brief {
        ask_block(ui, c, l, forms, focus, acts);
        ci_head(ui, c, l);
    }
    facts_table(ui, c, l);
    if more_toggle(ui, c, l, &rest_words(c, l)) {
        the_rest(ui, c, l, forms, focus, acts);
    }
}
