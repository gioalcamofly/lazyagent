//! A Home item, expanded (docs/design/home-items.md): what it asks, a
//! tinted section per problem (a CI failure, a conflict, a review), one line
//! of facts and every action that applies; the rest behind "Show more".

use super::*;

/// The expanded item, under the line (`details` adds the cut name and the
/// forms).
pub(super) fn body(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    let more = more_open(ui, l);
    ask_block(ui, c, l, forms, focus, acts);
    problems(ui, c, l, more, acts);
    answer_block(ui, c, l, forms, focus, acts);
    facts_line(ui, c, l);
    // The skip list: why no agent was sent ([inbox] auto_delegate_skip_checks).
    if let Some(text) = l.row.as_ref().and_then(|r| r.auto_delegate.as_ref()) {
        ui.label(RichText::new(text).small().color(c.t.yellow));
    }
    // Asked for (y): never folded.
    explain_block(ui, c, l);
    // Who else has it (a hold, a claim, a delegation, the orchestrator's
    // decision): it is who has the ball, never folded.
    owner_block(ui, c, l, forms, focus, acts);
    actions(ui, c, l, acts);
    if more_toggle(ui, c, l, more) {
        items_block(ui, c, l, forms, acts);
        timeline_block(ui, c, l);
        moved_lines(ui, c, l);
        facts_block(ui, c, l);
    }
}

/// What an item is a problem with, when it is one: each gets a section.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Problem {
    Ci,
    Conflict,
    Review,
    ReviewRequest,
}

impl Problem {
    pub fn of(kind: &str) -> Option<Problem> {
        match kind {
            "ci" | "ci_failed" | "ci_policy" => Some(Problem::Ci),
            "conflict" => Some(Problem::Conflict),
            "review" => Some(Problem::Review),
            "review_request" => Some(Problem::ReviewRequest),
            _ => None,
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Problem::Ci => "CI failed",
            Problem::Conflict => "Merge conflict",
            Problem::Review => "Changes requested",
            Problem::ReviewRequest => "Review requested",
        }
    }

    fn color(self, t: &Theme) -> Color32 {
        match self {
            Problem::Ci => t.red,
            Problem::Conflict => t.yellow,
            Problem::Review => t.purple,
            Problem::ReviewRequest => t.primary,
        }
    }
}

/// A section: a tint of `color` behind it and a bar of it on the left, so
/// sections tell apart in both themes (the tint is see-through).
fn section(ui: &mut Ui, t: &Theme, color: Color32, add: impl FnOnce(&mut Ui)) {
    let tint = color.gamma_multiply(if t.kind == crate::theme::ThemeKind::Dark { 0.16 } else { 0.09 });
    let r = egui::Frame::new()
        .fill(tint)
        .corner_radius(4)
        .inner_margin(egui::Margin { left: 12, right: 8, top: 5, bottom: 6 })
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui);
        });
    let rect = r.response.rect;
    let bar = egui::Rect::from_min_max(rect.min, egui::pos2(rect.min.x + 3.0, rect.max.y));
    ui.painter().rect_filled(bar, egui::CornerRadius { nw: 4, sw: 4, ne: 0, se: 0 }, color);
    ui.add_space(2.0);
}

/// Log lines in a framed monospace block of their own: cut to one line
/// each while folded, wrapped in full.
fn code(ui: &mut Ui, t: &Theme, lines: &[String], full: bool) {
    egui::Frame::new()
        .fill(t.bg)
        .stroke(egui::Stroke::new(1.0, t.border))
        .corner_radius(3)
        .inner_margin(egui::Margin::symmetric(8, 4))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            for line in lines {
                let label = egui::Label::new(RichText::new(line).small().monospace().color(t.text));
                ui.add(if full { label.wrap() } else { label.truncate() });
            }
        });
}

/// The line's items that are a problem, with what the line knows of each:
/// the top one's PR title and failed log, the others' state.
fn problems(ui: &mut Ui, c: &LineCtx, l: &Line, more: bool, acts: &mut Vec<Act>) {
    let t = c.t;
    let Some(top) = &l.top else { return };
    let items: Vec<(String, String, String)> = if l.items().is_empty() {
        vec![(top.id.clone(), top.kind.clone(), String::new())]
    } else {
        l.items().iter().map(|i| (i.id.clone(), i.kind.clone(), home::item_words(i))).collect()
    };
    let alone = c.delegate_all
        && l.delegates().is_some_and(|d| d.len() > 1)
        && l.row.as_ref().is_some_and(|r| !r.agents.is_empty());
    for (id, kind, words) in &items {
        let Some(p) = Problem::of(kind) else { continue };
        let is_top = *id == top.id;
        section(ui, t, p.color(t), |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(p.title()).strong().color(t.text));
                let what = if is_top && p == Problem::Ci {
                    ci_job(c, l).or_else(|| top.ci_text().map(|s| s.trim_start_matches("CI red · ").to_string()))
                } else {
                    None
                };
                let what = what.unwrap_or_else(|| {
                    // The top's own words when it has some ("ana asked you
                    // for a review"), else the item's state.
                    let mine = is_top && !top.detail.is_empty() && !top.detail.starts_with("http");
                    let w = if mine { humanize(&top.detail) } else { words.clone() };
                    let w = w.trim_start_matches("CI red: ").to_string();
                    if w.eq_ignore_ascii_case(p.title()) { String::new() } else { w }
                });
                if !what.is_empty() {
                    ui.label(RichText::new(what).color(t.text));
                }
                if is_top && !top.label.is_empty() && !top.label.starts_with("Agent ") {
                    ui.label(RichText::new(&top.label).small().color(t.muted));
                }
                if alone
                    && l.delegates().is_some_and(|d| d.contains(id))
                    && ui
                        .small_button("Delegate this one")
                        .on_hover_text("Only this item, to the branch's agent")
                        .clicked()
                {
                    acts.push(Act::DelegateItem(l.clone(), id.clone()));
                }
            });
            if is_top && top.is_ci() && c.actions {
                ci_body(ui, c, l, more, acts);
            }
        });
    }
}

/// The red run's first failed job, "unit-tests · Run tests", once read.
fn ci_job(c: &LineCtx, l: &Line) -> Option<String> {
    let (_, Some(log)) = c.ci_logs.get(&l.key)? else { return None };
    let job = log.jobs.first()?;
    Some(if job.step.is_empty() { job.name.clone() } else { format!("{} · {}", job.name, job.step) })
}

/// Lines of a failed job shown while the item is folded.
pub const LOG_HEAD: usize = 3;

/// A red CI's log: the first job's first lines, or every job in full
/// with "Show more"; a red check that is not Actions' by its name and link.
fn ci_body(ui: &mut Ui, c: &LineCtx, l: &Line, more: bool, acts: &mut Vec<Act>) {
    let t = c.t;
    let Some(top) = &l.top else { return };
    // With no Actions run the engine says whose red it is (0, 0).
    let wanted = top.run.as_ref().map(|r| (r.id, r.attempt)).unwrap_or((0, 0));
    let summary = match c.ci_logs.get(&l.key) {
        Some((r, Some(summary))) if *r == wanted => summary,
        Some((r, None)) if *r == wanted => {
            ui.label(RichText::new("reading the log…").small().color(t.muted));
            return;
        }
        // Not read yet, or for an older run: ask (once per run).
        _ => {
            acts.push(Act::CiLog(l.key.clone(), wanted));
            return;
        }
    };
    if let Some(e) = &summary.error {
        ui.label(RichText::new(e).small().color(t.yellow));
    }
    for check in summary.checks.iter().flatten() {
        ui.horizontal(|ui| {
            ui.label(RichText::new(format!("{} · {}", check.name, check.provider)).small().strong().color(t.text));
            if !check.url.is_empty() && ui.small_button("Open the check").on_hover_text(&check.url).clicked() {
                acts.push(Act::Pr(check.url.clone()));
            }
        });
    }
    for (i, job) in summary.jobs.iter().enumerate() {
        if !more && i > 0 {
            break;
        }
        // The first job names the section; the others name their block.
        if i > 0 {
            let head = if job.step.is_empty() { job.name.clone() } else { format!("{} · {}", job.name, job.step) };
            ui.label(RichText::new(head).small().strong().color(t.text));
        }
        if !job.lines.is_empty() {
            let n = if more { job.lines.len() } else { LOG_HEAD.min(job.lines.len()) };
            code(ui, t, &job.lines[..n], more);
        }
    }
    if summary.jobs.is_empty() && summary.error.is_none() && summary.checks.as_ref().is_none_or(|c| c.is_empty()) {
        ui.label(RichText::new("No failed job log to show.").small().color(t.muted));
    }
}

/// How much of a CI log "Show more" adds: "4 more log lines".
fn log_rest(c: &LineCtx, l: &Line) -> usize {
    if !c.actions || !l.top.as_ref().is_some_and(|t| t.is_ci()) {
        return 0;
    }
    let Some((_, Some(log))) = c.ci_logs.get(&l.key) else { return 0 };
    let first = log.jobs.first().map_or(0, |j| j.lines.len().saturating_sub(LOG_HEAD));
    first + log.jobs.iter().skip(1).map(|j| j.lines.len() + 1).sum::<usize>()
}

/// The facts the line does not show: the PR's review and CI. The agent and
/// the items are in the sections and "Show more"; who else has it, below.
pub fn facts(l: &Line) -> Vec<String> {
    let mut out = vec![];
    let Some(row) = &l.row else { return out };
    if let Some(pr) = &row.pr {
        let mut text = format!("PR #{}", pr.number);
        let review = pr.review.replace('_', " ").to_lowercase();
        if !review.is_empty() {
            text.push_str(&format!(" · {review}"));
        }
        text.push_str(&format!(" · CI {}", pr.ci));
        if let Some(note) = l.ci_note() {
            text.push_str(&format!(" ({note})"));
        }
        out.push(text);
    }
    out
}

/// The facts as one muted line, wrapped rather than cut.
fn facts_line(ui: &mut Ui, c: &LineCtx, l: &Line) {
    let text = facts(l);
    if !text.is_empty() {
        ui.add(egui::Label::new(RichText::new(text.join("  ·  ")).small().color(c.t.muted)).wrap());
    }
}

/// Rare or not usable now: under "More…" (Pass while it cannot be used,
/// its reason on hover; Notes, Why?, Label…).
fn rare(b: &Button) -> bool {
    b.more || !b.enabled || matches!(b.act, Act::LabelOpen(_))
}

/// The item's actions with the one its verb asks for first (Delegate on a
/// red CI); a Pass that cannot be used now says why on hover.
pub(super) fn ordered_buttons(c: &LineCtx, l: &Line) -> Vec<Button> {
    let mut buttons = detail_buttons(c, l);
    let delegate = |b: &Button| {
        matches!(b.act, Act::Delegate(_) | Act::SendCi(_) | Act::RestoreDelegate(_) | Act::SpawnDelegate(_))
    };
    if let Some(i) = buttons.iter().position(delegate) {
        let b = buttons.remove(i);
        buttons.insert(0, b);
    }
    for b in buttons.iter_mut().filter(|b| matches!(b.act, Act::Pass(_)) && !b.enabled) {
        b.hover = c.pass.err();
    }
    buttons
}

/// The texts of the inline actions and of "More…", for tests.
#[cfg(test)]
pub fn split_texts(buttons: &[(&str, bool, bool)]) -> (Vec<String>, Vec<String>) {
    let (mut shown, mut rest) = (vec![], vec![]);
    for (text, enabled, more) in buttons {
        let b = Button { text: text.to_string(), hover: None, enabled: *enabled, more: *more, act: Act::AnswerCancel };
        if rare(&b) { rest.push(b.text) } else { shown.push(b.text) }
    }
    (shown, rest)
}

/// Every action that applies, inline and wrapped (the first one stressed,
/// unless the answer row above is the primary action); the rare ones
/// under "More…".
fn actions(ui: &mut Ui, c: &LineCtx, l: &Line, acts: &mut Vec<Act>) {
    let t = c.t;
    let (shown, rest): (Vec<Button>, Vec<Button>) = ordered_buttons(c, l).into_iter().partition(|b| !rare(b));
    let answered = matches!(l.top.as_ref().map(|t| t.kind.as_str()), Some("approval" | "question" | "decision"));
    ui.horizontal_wrapped(|ui| {
        for (i, b) in shown.into_iter().enumerate() {
            let button = if i == 0 && !answered {
                primary(t, RichText::new(b.text))
            } else {
                egui::Button::new(RichText::new(b.text))
            };
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

/// The primary action's look: filled and outlined in the primary colour.
pub(super) fn primary(t: &Theme, text: RichText) -> egui::Button<'static> {
    egui::Button::new(text.strong().color(t.text)).fill(t.select_bg).stroke(egui::Stroke::new(1.0, t.primary))
}

fn more_menu(ui: &mut Ui, rest: Vec<Button>, acts: &mut Vec<Act>) {
    if rest.is_empty() {
        return;
    }
    ui.menu_button("More…", |ui| {
        ui.set_min_width(220.0);
        for b in rest {
            let mut r = ui.add_enabled(b.enabled, egui::Button::new(b.text));
            // A disabled button's reason shows on hover all the same.
            if let Some(h) = b.hover {
                r = if b.enabled { r.on_hover_text(h) } else { r.on_disabled_hover_text(h) };
            }
            if r.clicked() {
                acts.push(b.act);
                ui.close();
            }
        }
    });
}

fn more_id(l: &Line) -> egui::Id {
    egui::Id::unique(("home-more", l.sel()))
}

/// Whether the line's "Show more" is open (kept per line in egui memory).
fn more_open(ui: &Ui, l: &Line) -> bool {
    ui.data(|d| d.get_temp::<bool>(more_id(l))).unwrap_or(false)
}

/// "Show more ▸  2 items · 3 since you looked · facts", and its flip.
fn more_toggle(ui: &mut Ui, c: &LineCtx, l: &Line, open: bool) -> bool {
    let text = if open { "Show less ▾".to_string() } else { format!("Show more ▸  {}", rest_words(c, l)) };
    if ui.add(egui::Button::new(RichText::new(text).small().color(c.t.muted)).frame(false)).clicked() {
        ui.data_mut(|d| d.insert_temp(more_id(l), !open));
        return !open;
    }
    open
}

/// What "Show more" holds, in words.
fn rest_words(c: &LineCtx, l: &Line) -> String {
    let mut words = vec![];
    if l.items().len() > 1 {
        words.push(format!("{} items", l.items().len()));
    }
    match log_rest(c, l) {
        0 => {}
        1 => words.push("1 more log line".to_string()),
        n => words.push(format!("{n} more log lines")),
    }
    if let Some(tl) = c.timelines.get(&l.key).filter(|tl| !tl.entries.is_empty()) {
        words.push(format!("{} since you looked", tl.entries.len()));
    }
    words.push("agents and facts".to_string());
    words.join(" · ")
}
