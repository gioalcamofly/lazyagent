//! Home, drawn, and what its keys and buttons do (home.rs decides what is
//! shown where). One line per work item: the inbox (Now, then the current
//! Round), then the in-flight list. Enter on a Now line opens the agent's
//! terminal; on any other it expands the line (agents, the agent's note,
//! the claim, the PR, and the actions).

use eframe::egui::{self, Align, Color32, Key, Layout, Modifiers, RichText, Sense, Ui, Vec2};
use lazyagent_proto::types::{ActionableResult, StatusGestureResult};
use serde_json::{Value, json};

use crate::app::{App, Focus, Severity, State, unix_now};
use crate::home::{self, Line, Zone};
use crate::store;
use crate::theme::Theme;

/// Park until…: a time from now, or an event the engine watches.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Park {
    For(f64),
    Event(&'static str),
}

pub const PARKS: &[(&str, Park)] = &[
    ("1 hour", Park::For(3600.0)),
    ("4 hours", Park::For(4.0 * 3600.0)),
    ("24 hours", Park::For(86400.0)),
    ("CI ends", Park::Event("ci_done")),
    ("the agent stops", Park::Event("agent_stopped")),
    ("a human reviews", Park::Event("human_review")),
];

enum Act {
    Select(String),
    Toggle(String),
    Open(Line),
    Notes(Line),
    Pr(String),
    Dismiss(Line),
    ParkMenu(String),
    Park(Line, Park),
    Pass(Line),
    TakeBack(Line),
    SendCi(Line),
    Cut,
    Fold,
    Seen,
}

/// `1h 40m`, `25m`: how long is left.
fn left(seconds: f64) -> String {
    let m = (seconds.max(0.0) / 60.0).ceil() as u64;
    if m >= 60 { format!("{}h {:02}m", m / 60, m % 60) } else { format!("{m}m") }
}

impl State {
    /// Home's single-letter keys; true when one was taken.
    pub fn home_key(&mut self, take: &impl Fn(Modifiers, Key) -> bool) -> bool {
        let folded = self.saved.home_fold;
        if take(Modifiers::NONE, Key::J) || take(Modifiers::NONE, Key::ArrowDown) {
            self.home.select_next(1, folded);
            return true;
        }
        if take(Modifiers::NONE, Key::K) || take(Modifiers::NONE, Key::ArrowUp) {
            self.home.select_next(-1, folded);
            return true;
        }
        if take(Modifiers::NONE, Key::F) {
            self.saved.home_fold = !folded;
            self.touch_saved();
            return true;
        }
        let Some(line) = self.home.current(folded) else { return false };
        if take(Modifiers::NONE, Key::Enter) {
            if line.zone == Zone::Now && line.agent().is_some() {
                self.open_line(&line);
            } else {
                self.toggle_line(&line.key);
            }
        } else if take(Modifiers::SHIFT, Key::O) {
            self.pass_line(&line);
        } else if take(Modifiers::NONE, Key::O) {
            self.open_line(&line);
        } else if take(Modifiers::NONE, Key::N) {
            self.open_line(&line);
            self.notes_open = true;
        } else if take(Modifiers::NONE, Key::P) {
            match line.url.clone() {
                Some(url) => self.url_to_open = Some(url),
                None => self.toast(Severity::Info, "No PR for this item yet."),
            }
        } else if take(Modifiers::NONE, Key::X) {
            self.dismiss_line(&line);
        } else if take(Modifiers::NONE, Key::Z) {
            self.home.expanded = Some(line.key.clone());
            self.home.parking = Some(line.key.clone());
        } else if take(Modifiers::NONE, Key::U) {
            self.take_back(&line);
        } else {
            return false;
        }
        true
    }

    fn toggle_line(&mut self, key: &str) {
        let open = self.home.expanded.as_deref() == Some(key);
        self.home.expanded = (!open).then(|| key.to_string());
        self.home.parking = None;
    }

    /// The line's agent terminal (or its worktree, with none).
    pub fn open_line(&mut self, line: &Line) {
        let Some(place) = line.place.clone() else {
            self.toast(Severity::Info, "Nothing to open: this item has no worktree.");
            return;
        };
        if let Some(agent) = line.agent() {
            self.tabs.insert(place.clone(), agent);
        }
        self.selected = Some(place);
        self.leave_home();
        self.focus_request = Some(Focus::Agent);
    }

    /// The user closed an item: the engine forgets this occurrence (for
    /// every client), and that is a cut point.
    pub fn dismiss_line(&mut self, line: &Line) {
        let Some(top) = &line.top else {
            self.toast(Severity::Info, "Nothing to dismiss: it leaves once it is merged.");
            return;
        };
        if self.engine_inbox() {
            let id = top.id.clone();
            self.conn.request(
                "actionables_mark",
                json!({"id": id, "action": "dismiss"}),
                move |s, reply| match reply {
                    Ok(_) => {
                        s.actionables.remove(&id);
                        s.inbox_gen += 1;
                        s.refresh_home(true);
                        s.home.cut(unix_now());
                    }
                    Err(e) => s.toast(Severity::Error, format!("Not dismissed: {e}")),
                },
            );
        } else if let Some(item) = self.attention.iter().find(|i| i.id == top.id).cloned() {
            self.dismiss(&item);
            self.refresh_attention(true);
            self.refresh_home(true);
            self.home.cut(unix_now());
        }
    }

    pub fn park_line(&mut self, line: &Line, park: Park) {
        self.home.parking = None;
        let Some(top) = &line.top else {
            self.toast(Severity::Info, "Nothing to park: it leaves once it is merged.");
            return;
        };
        if self.engine_inbox() {
            let until = match park {
                Park::For(secs) => json!(unix_now() + secs),
                Park::Event(e) => json!(e),
            };
            let id = top.id.clone();
            let params = json!({"id": id, "action": "park", "until": until});
            self.conn.request("actionables_mark", params, move |s, reply| match reply {
                Ok(v) => {
                    if serde_json::from_value::<ActionableResult>(v).is_ok() {
                        s.actionables.remove(&id);
                        s.inbox_gen += 1;
                        s.refresh_home(true);
                        s.home.cut(unix_now());
                    }
                }
                Err(e) => s.toast(Severity::Error, format!("Not parked: {e}")),
            });
        } else if let (Park::For(secs), Some(item)) = (park, self.attention.iter().find(|i| i.id == top.id).cloned()) {
            self.snooze(&item, secs);
            self.refresh_attention(true);
            self.refresh_home(true);
            self.home.cut(unix_now());
        } else {
            self.toast(Severity::Info, "Parking until an event needs a newer engine.");
        }
    }

    fn gesture(&mut self, key: &str, params: Value, what: &'static str) {
        self.conn.request("status_gesture", params, move |s, reply| {
            match reply.and_then(|v| {
                serde_json::from_value::<StatusGestureResult>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => {
                    if let Some(row) = r.row {
                        s.status_rows.insert(row.key.clone(), row);
                        s.inbox_gen += 1;
                        s.refresh_home(true);
                    }
                    s.home.cut(unix_now());
                }
                Err(e) => s.toast(Severity::Error, format!("{what}: {e}")),
            }
        });
        self.home.expanded = Some(key.to_string());
    }

    /// Pass to orchestrator: a claim with a timeout (the engine's).
    pub fn pass_line(&mut self, line: &Line) {
        if let Err(why) = self.can_pass() {
            self.toast(Severity::Info, why);
            return;
        }
        if line.row.is_none() || line.claim.is_some() {
            return;
        }
        let params = json!({"key": line.key, "kind": "delegate", "to": "orchestrator"});
        self.gesture(&line.key, params, "Could not pass it to the orchestrator");
    }

    pub fn take_back(&mut self, line: &Line) {
        if line.claim.is_none() {
            return;
        }
        let params = json!({"key": line.key, "kind": "clear"});
        self.gesture(&line.key, params, "Could not take it back");
    }

    /// A red PR: its failed checks to the worktree's agent.
    fn send_ci(&mut self, line: &Line) {
        let Some(place) = line.place.clone() else { return };
        let pr = self.store.pr_info.get(&place).cloned().flatten();
        let agent = self.store.agents(&place).first().map(|a| a.agent_id.clone());
        if let (Some(pr), Some(agent)) = (pr, agent) {
            self.send_reply(&place, &agent, crate::attention::ci_failure_prompt(&pr));
        }
    }
}

fn ball_color(t: &Theme, ball: &str) -> Color32 {
    // No red outside Now: the in-flight list stays calm.
    match ball {
        "agent" => t.cyan,
        "others" => t.purple,
        "stalled" => t.yellow,
        "you" => t.accent,
        _ => t.muted,
    }
}

/// A fixed-width cell with truncated text.
fn cell(ui: &mut Ui, width: f32, text: RichText) {
    ui.allocate_ui_with_layout(Vec2::new(width, 18.0), Layout::left_to_right(Align::Center), |ui| {
        ui.set_width(width);
        ui.add(egui::Label::new(text).truncate());
    });
}

fn section(ui: &mut Ui, t: &Theme, title: &str, color: Color32, extra: impl FnOnce(&mut Ui)) {
    ui.add_space(8.0);
    ui.horizontal(|ui| {
        ui.label(RichText::new(title).strong().small().color(color));
        extra(ui);
    });
    ui.add_space(2.0);
    let _ = t;
}

impl App {
    pub(crate) fn home_view(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let now = unix_now();
        let folded = self.state.saved.home_fold;
        let selected = self.state.home.current(folded).map(|l| l.key);
        let home = &self.state.home;
        let (expanded, parking) = (home.expanded.clone(), home.parking.clone());
        let lines: [Vec<Line>; 3] =
            [Zone::Now, Zone::Round, Zone::Flight].map(|z| home.shown_in(z).into_iter().cloned().collect());
        let (waiting, resolved, round_at) = (home.waiting(), home.resolved, home.round_at);
        let look = self.state.look_from;
        let pass = self.state.can_pass();
        let engine = self.state.engine_inbox();
        let not_started = self.state.status_counts.not_started;
        let mut acts: Vec<Act> = vec![];
        let ctx = LineCtx { t: &t, now, look, pass, engine, selected: selected.as_deref() };
        egui::ScrollArea::vertical().id_salt("home").auto_shrink([false, false]).show(ui, |ui| {
            if !lines[0].is_empty() {
                section(ui, &t, "NOW", t.yellow, |_| {});
                for l in &lines[0] {
                    line_ui(
                        ui,
                        &ctx,
                        l,
                        Zone::Now,
                        expanded.as_deref() == Some(&l.key),
                        parking.as_deref() == Some(&l.key),
                        &mut acts,
                    );
                }
            }
            section(ui, &t, "ROUND", t.accent, |ui| {
                ui.label(RichText::new(format!("· {} ago", home::age(now - round_at))).small().color(t.muted));
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if waiting > 0
                        && ui
                            .button(RichText::new(format!("next round: {waiting} ▸")).small())
                            .on_hover_text(
                                "Delivered when you close an item, come back from a terminal, or every 30 min",
                            )
                            .clicked()
                    {
                        acts.push(Act::Cut);
                    }
                    if resolved > 0 {
                        ui.label(RichText::new(format!("{resolved} resolved on their own")).small().color(t.muted));
                    }
                });
            });
            if lines[1].is_empty() {
                let text = if lines[0].is_empty() { "Nothing needs you." } else { "Nothing else in this round." };
                ui.label(RichText::new(text).color(t.muted));
            }
            for l in &lines[1] {
                line_ui(
                    ui,
                    &ctx,
                    l,
                    Zone::Round,
                    expanded.as_deref() == Some(&l.key),
                    parking.as_deref() == Some(&l.key),
                    &mut acts,
                );
            }
            ui.add_space(6.0);
            ui.separator();
            let flight = match not_started {
                0 => format!("IN FLIGHT ({})", lines[2].len()),
                n => format!("IN FLIGHT ({}) · {n} not started", lines[2].len()),
            };
            section(ui, &t, &flight, t.muted, |ui| {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.button(RichText::new(if folded { "unfold (f)" } else { "fold (f)" }).small()).clicked() {
                        acts.push(Act::Fold);
                    }
                    if lines[2].iter().any(|l| l.changed_at > look)
                        && ui.button(RichText::new("mark seen").small()).on_hover_text("Clear the • dots").clicked()
                    {
                        acts.push(Act::Seen);
                    }
                });
            });
            if !folded {
                if lines[2].is_empty() {
                    ui.label(RichText::new("No work in flight.").color(t.muted));
                }
                for l in &lines[2] {
                    line_ui(
                        ui,
                        &ctx,
                        l,
                        Zone::Flight,
                        expanded.as_deref() == Some(&l.key),
                        parking.as_deref() == Some(&l.key),
                        &mut acts,
                    );
                }
            }
            ui.add_space(8.0);
            ui.label(
                RichText::new(
                    "• changed since your last look · Enter open/expand · o terminal · n notes · p PR · x dismiss · \
                     z park · O pass to orchestrator · u take back · f fold",
                )
                .small()
                .color(t.muted),
            );
        });
        let s = &mut self.state;
        for act in acts {
            match act {
                Act::Select(k) => s.home.selected = Some(k),
                Act::Toggle(k) => s.toggle_line(&k),
                Act::Open(l) => s.open_line(&l),
                Act::Notes(l) => {
                    s.open_line(&l);
                    s.notes_open = true;
                }
                Act::Pr(url) => s.url_to_open = Some(url),
                Act::Dismiss(l) => s.dismiss_line(&l),
                Act::ParkMenu(k) => {
                    let open = s.home.parking.as_deref() == Some(k.as_str());
                    s.home.parking = (!open).then_some(k);
                }
                Act::Park(l, p) => s.park_line(&l, p),
                Act::Pass(l) => s.pass_line(&l),
                Act::TakeBack(l) => s.take_back(&l),
                Act::SendCi(l) => s.send_ci(&l),
                Act::Cut => s.home.cut(now),
                Act::Fold => {
                    s.saved.home_fold = !s.saved.home_fold;
                    s.touch_saved();
                }
                Act::Seen => s.mark_seen(),
            }
        }
    }
}

struct LineCtx<'a> {
    t: &'a Theme,
    now: f64,
    look: f64,
    pass: Result<(), &'static str>,
    engine: bool,
    selected: Option<&'a str>,
}

fn line_ui(ui: &mut Ui, c: &LineCtx, l: &Line, shown: Zone, expanded: bool, parking: bool, acts: &mut Vec<Act>) {
    let t = c.t;
    let selected = c.selected == Some(l.key.as_str());
    let fill = if selected { t.current_bg } else { Color32::TRANSPARENT };
    let resp = egui::Frame::new().fill(fill).inner_margin(egui::Margin { left: 10, right: 8, top: 3, bottom: 3 }).show(
        ui,
        |ui| {
            ui.set_width(ui.available_width());
            let head = ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                if shown == Zone::Flight {
                    let dot = if l.changed_at > c.look { "•" } else { " " };
                    cell(ui, 10.0, RichText::new(dot).strong().color(t.accent));
                    cell(ui, 220.0, RichText::new(&l.name).strong());
                    let (ball, state) = match &l.claim {
                        Some(claim) => {
                            ("orchestrator".to_string(), format!("{} · {} left", l.state, left(claim.until - c.now)))
                        }
                        // In the inbox, but held for the next round.
                        None if l.zone == Zone::Round => ("you · next round".to_string(), l.state.clone()),
                        None => (l.ball.clone(), l.state.clone()),
                    };
                    cell(ui, 110.0, RichText::new(ball).small().color(ball_color(t, &l.ball)));
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(RichText::new(home::age(c.now - l.changed_at)).small().color(t.muted));
                        ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                            ui.add(egui::Label::new(RichText::new(state).color(t.muted)).truncate());
                        });
                    });
                } else {
                    let color = if shown == Zone::Now { t.yellow } else { t.accent };
                    cell(ui, 10.0, RichText::new(if selected { "▸" } else { " " }).color(color));
                    cell(ui, 74.0, RichText::new(l.verb()).strong().color(color));
                    cell(ui, 220.0, RichText::new(&l.name).strong());
                    // "approve: npm test" after "Approve": the fact alone.
                    let text = l.state.split_once(": ").map(|(_, rest)| rest).unwrap_or(&l.state).to_string();
                    let age = c.now - l.top.as_ref().map(|t| t.since).unwrap_or(l.changed_at);
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(RichText::new(home::age(age)).small().color(t.muted));
                        if l.more > 0 {
                            ui.label(RichText::new(format!("+{}", l.more)).small().color(t.muted));
                        }
                        ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                            ui.add(egui::Label::new(RichText::new(format!("· {text}"))).truncate());
                        });
                    });
                }
            });
            if expanded {
                details(ui, c, l, parking, acts);
            }
            head.response
        },
    );
    if selected {
        let r = resp.response.rect;
        ui.painter().rect_filled(egui::Rect::from_min_size(r.min, Vec2::new(3.0, r.height())), 0.0, t.accent);
    }
    if resp.inner.interact(Sense::click()).clicked() {
        acts.push(Act::Select(l.key.clone()));
        acts.push(Act::Toggle(l.key.clone()));
    }
}

/// The expanded line: who is on it, the agent's note, and the actions.
fn details(ui: &mut Ui, c: &LineCtx, l: &Line, parking: bool, acts: &mut Vec<Act>) {
    let t = c.t;
    ui.add_space(2.0);
    ui.indent(("home-details", &l.key), |ui| {
        if let Some(top) = &l.top {
            let text =
                [top.label.as_str(), top.detail.as_str()].iter().filter(|x| !x.is_empty()).cloned().collect::<Vec<_>>();
            ui.label(RichText::new(text.join(" · ")).small().color(t.muted));
        }
        if let Some(row) = &l.row {
            for a in &row.agents {
                ui.label(
                    RichText::new(format!(
                        "agent {}  {} {}",
                        a.id,
                        store::status_text(&a.status),
                        home::age(c.now - a.since)
                    ))
                    .small()
                    .color(t.muted),
                );
            }
            if let Some(pr) = &row.pr {
                let review = pr.review.replace('_', " ").to_lowercase();
                let facts: Vec<String> = [format!("PR #{}", pr.number), review, format!("CI {}", pr.ci)]
                    .into_iter()
                    .filter(|x| !x.is_empty())
                    .collect();
                ui.label(RichText::new(facts.join(" · ")).small().color(t.muted));
            }
            if let Some(tr) = &row.tracker {
                let stale = if tr.stale { format!(" ({} ago)", home::age(c.now - tr.at)) } else { String::new() };
                ui.label(
                    RichText::new(format!("{} {}: {}{stale}", tr.adapter.replace('_', " "), tr.key, tr.state))
                        .small()
                        .color(t.muted),
                );
            }
            if let Some(note) = &row.note {
                ui.label(
                    RichText::new(format!("notes: \"{}\" ({})", note.text, home::age(c.now - note.at)))
                        .small()
                        .color(t.text),
                );
            }
        }
        if let Some(claim) = &l.claim {
            ui.label(
                RichText::new(format!(
                    "with the orchestrator for {} more · it may: {}",
                    left(claim.until - c.now),
                    claim.allowed.join(", ").replace('_', " ")
                ))
                .small()
                .color(t.cyan),
            );
        }
        ui.horizontal_wrapped(|ui| {
            if l.place.is_some() && ui.button("Open terminal  (o)").clicked() {
                acts.push(Act::Open(l.clone()));
            }
            if l.place.is_some() && ui.button("Notes  (n)").clicked() {
                acts.push(Act::Notes(l.clone()));
            }
            if let Some(url) = &l.url
                && ui.button("PR  (p)").clicked()
            {
                acts.push(Act::Pr(url.clone()));
            }
            if l.top.as_ref().is_some_and(|t| t.kind == "ci") && ui.button("Send the failures to its agent").clicked() {
                acts.push(Act::SendCi(l.clone()));
            }
            if l.top.is_some() {
                if ui.button("Dismiss  (x)").on_hover_text("Hide until it happens again, for every client").clicked() {
                    acts.push(Act::Dismiss(l.clone()));
                }
                if ui.button("Park until…  (z)").clicked() {
                    acts.push(Act::ParkMenu(l.key.clone()));
                }
            }
            if l.claim.is_some() {
                if ui.button("Take back  (u)").clicked() {
                    acts.push(Act::TakeBack(l.clone()));
                }
            } else if l.row.is_some() || !c.engine {
                let ok = c.pass.is_ok() && l.row.is_some();
                let b = ui.add_enabled(ok, egui::Button::new("Pass to orchestrator  (O)"));
                if b.clicked() {
                    acts.push(Act::Pass(l.clone()));
                }
                if let Err(why) = c.pass {
                    ui.label(RichText::new(why).small().color(t.muted));
                }
            }
        });
        if parking {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new("Park until").small().color(t.muted));
                for (label, park) in PARKS {
                    let ok = c.engine || matches!(park, Park::For(_));
                    if ui.add_enabled(ok, egui::Button::new(RichText::new(*label).small())).clicked() {
                        acts.push(Act::Park(l.clone(), *park));
                    }
                }
            });
        }
    });
    ui.add_space(2.0);
}
