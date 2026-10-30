//! Home, drawn, and what its keys and buttons do (home.rs decides what is
//! shown where). One line per work item: the inbox (Now, then the current
//! Round, then To sit down with), then the in-flight list. Enter on a Now
//! line opens the agent's terminal; on any other it expands the line
//! (agents, the agent's note, the claim, the PR, a brief, "since you
//! looked", and the actions).
//!
//! With the engine's `gestures` (I4): label (`l`), park a row (`z`), close
//! (`Shift+X`), clear (`u`), and per-item seen marks (`m`; `Shift+M` for
//! all). With `first_responder` (I4b): the presence toggle, "orchestrator ·
//! looking", the orchestrator's decisions with Revert (`Shift+U`), its
//! counts, and the auto-delegation settings. Without those features none of
//! it shows.
//!
//! I4d: `z` opens a small park menu (1 h, tomorrow 08:00, 1 week, until
//! something changes, indefinitely; keys 1-5, `w` for the whole work item);
//! a folded **Parked (N)** group (`Shift+Z`); `d` delegates the line's item
//! to the branch's agent; the orchestrator's holds and release notes on the
//! row; middle-ellipsis names with the full one on hover; `?` "How lazyagent works" at Home; `w` Why? (R6).
//!
//! Done rows with nothing to do fold into one "N done today" group at the
//! end of In flight (`Shift+F`, kept per repo).
//!
//! I4f: `a` approves and `⇧D` denies (a deny waits [`UNDO_FOR`] for its
//! Undo before it is sent); `y` is always explain and `n` always notes. The
//! key bar names only the selected line's keys, and "? all keys". A deep
//! item under a quicker top one shows as a ghost line in To sit down with;
//! stale rows fold into "N stale" at the very end of In flight.

use eframe::egui::{self, Align, Color32, Key, Layout, Modifiers, RichText, Sense, Ui, Vec2};
use lazyagent_proto::types::{
    Actionable, ActionableResult, Attestation, CiLogSummary, Decision, Delegation, GithubHealth, InboxConfig, Presence,
    ReviewCommit, ReviewCommits, RulesProblem, StatusGestureResult, StatusRow, StatusTimeline, TimelineEntry,
};
use serde_json::{Value, json};

use crate::app::{App, Focus, Severity, State, unix_now};
use crate::home::{self, LABEL_BALLS, LABEL_CHARS, Label, Line, Zone};
use crate::store;
use crate::theme::Theme;

/// Park until…: a time from now, or an event the engine watches.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Park {
    For(f64),
    Event(&'static str),
}

/// The park menu (feature `parked`), keys 1-5. "tomorrow" is 08:00 local,
/// worked out by the engine.
pub const PARKS: &[(&str, Park)] = &[
    ("1 hour", Park::For(3600.0)),
    ("tomorrow 08:00", Park::Event("tomorrow")),
    ("1 week", Park::For(7.0 * 86400.0)),
    ("something changes", Park::Event("change")),
    ("indefinitely", Park::Event("indefinite")),
];

/// The park choices of engines without `parked`.
pub const LEGACY_PARKS: &[(&str, Park)] = &[
    ("1 hour", Park::For(3600.0)),
    ("4 hours", Park::For(4.0 * 3600.0)),
    ("24 hours", Park::For(86400.0)),
    ("CI ends", Park::Event("ci_done")),
    ("the agent stops", Park::Event("agent_stopped")),
    ("a human reviews", Park::Event("human_review")),
];

/// The name column: a share of the line's width, within these bounds.
pub const NAME_SHARE: f32 = 0.3;
pub const NAME_MIN: f32 = 150.0;
pub const NAME_MAX: f32 = 420.0;

/// User input is reported to the engine at most this often (seconds).
pub const PRESENCE_EVERY: f64 = 60.0;

/// The list stops growing past this width: ages stay next to their state.
pub const CONTENT_MAX: f32 = 1100.0;

/// An Undo is offered this long after a gesture (seconds).
pub const UNDO_FOR: f64 = 5.0;

/// At most this many buttons under an expanded line; the rest are under
/// "More…".
pub const PRIMARY_BUTTONS: usize = 6;

/// A gesture Home can take back for [`UNDO_FOR`] seconds.
#[derive(Debug, Clone, PartialEq)]
pub struct Undo {
    pub text: String,
    pub act: UndoAct,
    pub at: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum UndoAct {
    /// `actionables_mark undismiss`.
    Undismiss(String),
    /// `actionables_mark unpark` (an item's park).
    Unpark(String),
    /// `status_gesture undo {what}`: park (the work item), delegate, pass.
    Gesture(String, &'static str),
    /// A deny not sent yet (the approval item's id, its command): Undo
    /// keeps it from being sent; past [`UNDO_FOR`] it goes.
    Deny(String, String),
    /// `owner_approval_revoke {undo}` (the work item, the head approved):
    /// the approval it replaced, if any, stands again.
    Unapprove(String, String),
}

/// The stalled kinds auto-delegation offers (Q-B33), with their labels.
pub const AUTO_DELEGATE: &[(&str, &str)] =
    &[("conflict_with_base", "Conflict with the base branch"), ("agent_dead", "Agent died"), ("ci_red", "CI red")];
/// The auto-delegation menu's width (points).
pub const AUTO_DELEGATE_WIDTH: f32 = 280.0;
/// The `ci_red` label on an engine without CI tracking (feature `actions`).
pub const CI_RED_INACTIVE: &str = "CI red (needs CI tracking, inactive)";

enum Act {
    Select(String),
    Toggle(String),
    Open(Line),
    Notes(Line),
    Pr(String),
    Dismiss(Line),
    ParkMenu(String),
    ParkWhole(bool),
    Park(Line, Park, bool),
    ParkedFold,
    Delegate(Line),
    /// One item of the line alone (its id), from the expanded items.
    DelegateItem(Line, String),
    /// Send anyway: a delegation's letter held by a busy agent.
    SendAnyway(Line),
    Pass(Line),
    /// Approve the PR's head shown (R3d).
    OwnerApprove(Line),
    TakeBack(Line),
    SendCi(Line),
    CiLog(String, (i64, i64)),
    Cut,
    Fold,
    Seen,
    SeenLine(Line),
    LabelOpen(Line),
    LabelEdit(Label),
    LabelSave(Label),
    LabelCancel,
    CloseMenu(String),
    Close(Line, &'static str),
    RevertOpen(Line),
    RevertEdit(String),
    Revert(String, String),
    RevertCancel,
    Presence(&'static str),
    Approve(Line, bool),
    AnswerEdit(String, String),
    Answer(Line, String),
    AnswerCancel,
    BriefChoose(String, String),
    BriefAnswer(Decision),
    /// "Answer here" on a brief in an expanded line's items (its sel, the
    /// item's id).
    ItemOpen(String, String),
    Undo,
    StripToggle,
    RevertAction(String),
    ParkedShow,
    DoneFold,
    StaleFold,
    Why(Line),
    RestoreDelegate(Line),
    SpawnDelegate(Line),
    StartOrchestrator,
    /// An attestation's Confirm item (R5): Accept, or Reject with a comment.
    AttestAccept(String),
    RejectOpen(String),
    RejectEdit(String),
    Reject(String, String),
    RejectCancel,
}

/// `1h 40m`, `25m`: how long is left.
fn left(seconds: f64) -> String {
    let m = (seconds.max(0.0) / 60.0).ceil() as u64;
    if m >= 60 { format!("{}h {:02}m", m / 60, m % 60) } else { format!("{m}m") }
}

/// Input that says the user is at the keyboard (presence, I4b).
pub fn is_user_input(e: &egui::Event) -> bool {
    matches!(
        e,
        egui::Event::Key { pressed: true, .. }
            | egui::Event::Text(_)
            | egui::Event::PointerButton { pressed: true, .. }
            | egui::Event::PointerMoved(_)
            | egui::Event::MouseWheel { .. }
    )
}

/// The engine features of I4e's inline answers and undo.
pub const F_APPROVAL: &str = "approval_answer";
pub const F_QUESTION: &str = "question_answer";
pub const F_UNDO: &str = "undo";
pub const F_ORCHESTRATOR: &str = "orchestrator_activity";
pub const F_CLOSE_STOP: &str = "close_stop";
/// I4j: row updates carry the opt-in fields; Delegate restores or spawns
/// the agent in the engine's one step.
pub const F_ROW_FIELDS: &str = "row_fields_events";
pub const F_DELEGATE_START: &str = "delegate_start";

fn zone_name(zone: Zone) -> &'static str {
    match zone {
        Zone::Now => "Now",
        Zone::Round => "the Round",
        Zone::Deep => "To sit down with",
        Zone::Flight => "In flight",
        Zone::Parked => "Parked",
    }
}

/// Raw engine words, as people say them: `waiting_for_input` → "waiting
/// for input", `waiting_for_approval` → "waiting for approval".
pub fn humanize(text: &str) -> String {
    text.split(' ')
        .map(|w| {
            let bare = w.trim_matches(|c: char| !c.is_alphanumeric() && c != '_');
            if bare.contains('_') && bare.chars().all(|c| c.is_ascii_lowercase() || c == '_') {
                w.replace(bare, &bare.replace('_', " "))
            } else {
                w.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// "decided by orchestrator: B — reason".
pub fn decided_text(d: &Decision) -> String {
    let choice = d.choice().unwrap_or("?");
    let label = d.brief.options.iter().find(|o| o.key.eq_ignore_ascii_case(choice)).map(|o| o.label.as_str());
    let mut text = match label {
        Some(l) => format!("decided by orchestrator: {choice} ({l})"),
        None => format!("decided by orchestrator: {choice}"),
    };
    if let Some(reason) = d.reason().filter(|r| !r.is_empty()) {
        text.push_str(&format!(" — {reason}"));
    }
    text
}

/// "brief by agent a2 · 2 options · reversible: yes · recommends A".
fn brief_summary(d: &Decision) -> String {
    let mut parts = vec![format!("brief by agent {}", d.agent), format!("{} options", d.brief.options.len())];
    parts.push(match &d.brief.reversible {
        Some(r) => format!("reversible: {r}"),
        None => "reversibility not given".into(),
    });
    if let Some(rec) = &d.brief.recommendation {
        parts.push(format!("recommends {rec}"));
    }
    parts.join(" · ")
}

/// One piece of evidence, in a few words: `diff src/retry.py`.
fn evidence_text(e: &Value) -> String {
    let kind = e["kind"].as_str().unwrap_or("?");
    let what = ["path", "ref", "section", "url"].iter().find_map(|k| e[*k].as_str()).unwrap_or("");
    let lines = e["lines"].as_str().map(|l| format!(":{l}")).unwrap_or_default();
    format!("{kind} {what}{lines}").trim().to_string()
}

impl State {
    /// Home's single-letter keys; true when one was taken.
    pub fn home_key(&mut self, take: &impl Fn(Modifiers, Key) -> bool) -> bool {
        if self.decision.is_some() {
            // The decision view has the keys while it is open (decision_view.rs).
            return self.decision_key(take);
        }
        if self.why.is_some() {
            return self.why_key(take);
        }
        let folded = self.saved.home_fold;
        if take(Modifiers::SHIFT, Key::Questionmark) || take(Modifiers::NONE, Key::Questionmark) {
            self.open_guide(crate::how_it_works::HOME_SECTION);
            return true;
        }
        if let Some(key) = self.home.parking.clone()
            && let Some(line) = self.home.get(&key).cloned()
        {
            // The park menu has the keys while it is open.
            let parks = self.parks();
            const DIGITS: [Key; 6] = [Key::Num1, Key::Num2, Key::Num3, Key::Num4, Key::Num5, Key::Num6];
            if let Some(i) = DIGITS.iter().take(parks.len()).position(|k| take(Modifiers::NONE, *k)) {
                let whole = self.home.park_whole || line.top.is_none();
                self.park_line(&line, parks[i].1, whole);
                return true;
            }
            if take(Modifiers::NONE, Key::W) {
                if self.can_gesture(&line) && line.top.is_some() {
                    self.home.park_whole = !self.home.park_whole;
                }
                return true;
            }
            if take(Modifiers::NONE, Key::Escape) || take(Modifiers::NONE, Key::Z) {
                self.home.parking = None;
                return true;
            }
        }
        if take(Modifiers::CTRL, Key::Z) {
            self.undo_last();
            return true;
        }
        if take(Modifiers::NONE, Key::Escape) {
            // Esc closes the open form, else collapses the expanded line.
            let h = &mut self.home;
            if h.closing.is_some()
                || h.labeling.is_some()
                || h.reverting.is_some()
                || h.answering.is_some()
                || h.rejecting.is_some()
            {
                (h.closing, h.labeling, h.reverting, h.answering, h.rejecting) = (None, None, None, None, None);
            } else if h.item_open.is_some() {
                h.item_open = None;
            } else {
                h.expanded = None;
            }
            return true;
        }
        if take(Modifiers::SHIFT, Key::Z) {
            self.home.parked_open = !self.home.parked_open;
            return true;
        }
        if take(Modifiers::NONE, Key::Tab) {
            if !self.home.next_needing_you(folded) {
                self.toast(Severity::Info, "Nothing needs you.");
            }
            return true;
        }
        // An expanded brief takes 1-4 (its options) and Ctrl+Enter (answer);
        // otherwise 1-4 jump to Now, Round, To sit down with, In flight.
        const DIGITS: [Key; 4] = [Key::Num1, Key::Num2, Key::Num3, Key::Num4];
        let brief = self.expanded_brief();
        if let Some(d) = &brief {
            if let Some(i) = DIGITS.iter().take(d.brief.options.len()).position(|k| take(Modifiers::NONE, *k)) {
                self.home.brief_choice = Some((d.id.clone(), d.brief.options[i].key.clone()));
                return true;
            }
            if take(Modifiers::CTRL, Key::Enter) {
                self.answer_brief_inline(d);
                return true;
            }
        }
        if let Some(i) = DIGITS.iter().position(|k| take(Modifiers::NONE, *k)) {
            let zone = [Zone::Now, Zone::Round, Zone::Deep, Zone::Flight][i];
            if zone == Zone::Flight && folded {
                self.saved.home_fold = false;
                self.touch_saved();
            }
            if !self.home.jump(zone, false) {
                self.toast(Severity::Info, format!("Nothing in {}.", zone_name(zone)));
            }
            return true;
        }
        if take(Modifiers::NONE, Key::J) || take(Modifiers::NONE, Key::ArrowDown) {
            self.home.select_next(1, folded);
            return true;
        }
        if take(Modifiers::NONE, Key::K) || take(Modifiers::NONE, Key::ArrowUp) {
            self.home.select_next(-1, folded);
            return true;
        }
        // ⇧F before f: a shortcut without Shift also takes Shift.
        if take(Modifiers::SHIFT, Key::F) {
            self.toggle_done_group();
            return true;
        }
        if take(Modifiers::NONE, Key::F) {
            self.saved.home_fold = !folded;
            self.touch_saved();
            return true;
        }
        if take(Modifiers::SHIFT, Key::M) {
            self.mark_all_seen();
            return true;
        }
        let Some(line) = self.home.current(folded) else { return false };
        let kind = line.top.as_ref().map(|t| t.kind.clone()).unwrap_or_default();
        let approval = kind == "approval" && self.inline_approvals();
        let after_g = self.pending_g.is_some_and(|t| t.elapsed() < std::time::Duration::from_secs(1));
        if take(Modifiers::NONE, Key::Enter) {
            let asks = line.top.as_ref().is_some_and(|t| t.asks(line.brief.as_ref()));
            let brief = line.top.as_ref().is_some_and(|t| t.kind == "decision" && t.decision_id.is_some());
            if brief && !asks && self.decision_view_on() && line.ghost.is_none() {
                self.open_line_decision(&line);
            } else if approval || (kind == "question" && self.inline_questions()) {
                // Answered on the line: Enter shows the ask in full.
                self.toggle_line(&line.sel());
                if kind == "question" && self.home.expanded == Some(line.sel()) {
                    self.open_answer(&line);
                }
            } else if (line.zone == Zone::Now || asks) && line.agent().is_some() {
                self.open_line(&line);
            } else {
                self.toggle_line(&line.sel());
            }
        } else if !after_g && take(Modifiers::SHIFT, Key::A) {
            // ⇧A before a: a shortcut without Shift also takes Shift.
            self.approve_head(&line);
        } else if approval && !after_g && take(Modifiers::NONE, Key::A) {
            // (g a is Home: the g came first.)
            self.answer_approval(&line, true);
        } else if take(Modifiers::SHIFT, Key::D) {
            // ⇧D before d: a shortcut without Shift also takes Shift.
            if approval {
                self.answer_approval(&line, false);
            } else {
                self.toast(Severity::Info, "⇧D denies an approval: this line has none.");
            }
        } else if take(Modifiers::NONE, Key::V) {
            // A brief opened in the line's items: that one.
            let opened = self
                .home
                .item_open
                .as_ref()
                .filter(|(s, _)| *s == line.sel() && self.home.expanded.as_ref() == Some(s))
                .and_then(|(_, id)| line.under_item(id))
                .and_then(|u| u.top.as_ref()?.decision_id.clone());
            if let Some(id) = opened.filter(|_| self.decision_view_on()) {
                self.open_decision(&id, &line.key);
            } else if !self.decision_view_on() {
                self.toast(Severity::Info, "This engine has no decision view: expand the line (Enter).");
            } else if !self.open_line_decision(&line) {
                self.toast(Severity::Info, "No brief on this line.");
            }
        } else if take(Modifiers::SHIFT, Key::Y) {
            self.explain_line(&line, true);
        } else if take(Modifiers::NONE, Key::Y) {
            self.explain_line(&line, false);
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
        } else if take(Modifiers::SHIFT, Key::X) {
            self.close_menu(&line);
        } else if take(Modifiers::NONE, Key::X) {
            self.dismiss_line(&line);
        } else if take(Modifiers::NONE, Key::Z) {
            if line.top.is_none() && !self.can_gesture(&line) {
                self.toast(Severity::Info, "Nothing to park: it leaves once it is merged.");
            } else {
                self.home.expanded = Some(line.sel());
                self.home.parking = Some(line.sel());
                self.home.park_whole = line.top.is_none();
            }
        } else if take(Modifiers::NONE, Key::D) {
            self.delegate_line(&line);
        } else if take(Modifiers::NONE, Key::L) {
            self.open_label(&line);
        } else if take(Modifiers::SHIFT, Key::U) {
            self.open_revert(&line);
        } else if take(Modifiers::NONE, Key::U) {
            self.take_back(&line);
        } else if take(Modifiers::NONE, Key::M) {
            self.seen_line(&line);
        } else if !after_g && take(Modifiers::NONE, Key::W) {
            // (g w is the worktrees: the g came first.)
            self.open_why(&line);
        } else if self.first_responder() && take(Modifiers::NONE, Key::T) {
            self.toggle_strip();
        } else {
            return false;
        }
        true
    }

    /// Folds or unfolds In flight's "N done today" group (kept per repo);
    /// unfolding it unfolds In flight too.
    pub fn toggle_done_group(&mut self) {
        self.home.done_open = !self.home.done_open;
        self.saved.home_done_open = self.home.done_open;
        if self.home.done_open {
            self.saved.home_fold = false;
        }
        self.touch_saved();
    }

    /// Approvals are answered on the line (`approval_answer`).
    pub fn inline_approvals(&self) -> bool {
        self.engine_inbox() && self.has(F_APPROVAL)
    }

    /// Free-text questions are answered on the line (`question_answer`).
    pub fn inline_questions(&self) -> bool {
        self.engine_inbox() && self.has(F_QUESTION)
    }

    /// The open brief of the expanded line (or the one opened in its
    /// items), when it can be answered there: options to pick, or the
    /// engine's own brief and its free-text answer.
    pub fn expanded_brief(&self) -> Option<Decision> {
        let sel = self.home.expanded.as_ref()?;
        let line = self.home.get(sel)?;
        let brief = match self.home.item_open.as_ref().filter(|(s, _)| s == sel) {
            Some((_, id)) => line.under_item(id).and_then(|u| u.brief.clone()),
            None => line.brief.clone(),
        };
        let d = brief.filter(|d| d.state == "open" && answerable_here(d))?;
        (self.decision_view_on() || self.has("first_responder")).then_some(d)
    }

    /// `a` / `⇧D` on an approval line: the agent's permission prompt gets
    /// the answer, only while it is on screen (the engine checks). A deny
    /// waits [`UNDO_FOR`] for its Undo first: the agent cannot take it back.
    pub fn answer_approval(&mut self, line: &Line, allow: bool) {
        let Some(top) = line.top.clone().filter(|t| t.kind == "approval") else { return };
        if !self.inline_approvals() {
            self.toast(Severity::Info, "This engine cannot answer approvals from Home: open the terminal (o).");
            return;
        }
        if !allow {
            let text = format!("Denying {}: {}", line.name, top.detail);
            self.set_undo(text, UndoAct::Deny(top.id.clone(), top.detail.clone()));
            return;
        }
        self.send_approval(top.id.clone(), top.detail.clone(), true);
    }

    /// The deny whose Undo time is over goes to the agent now.
    pub fn tick_deny(&mut self, now: f64) {
        if let Some(Undo { act: UndoAct::Deny(id, detail), at, .. }) = self.undo.clone()
            && now - at >= UNDO_FOR
        {
            self.undo = None;
            self.send_approval(id, detail, false);
        }
    }

    fn send_approval(&mut self, id: String, detail: String, allow: bool) {
        let what = if allow { "Approved" } else { "Denied" };
        self.conn.request("approval_answer", json!({"id": id, "allow": allow}), move |s, reply| match reply {
            Ok(_) => {
                s.actionables.remove(&id);
                s.inbox_gen += 1;
                s.refresh_home(true);
                s.toast(Severity::Info, format!("{what}: {detail}"));
            }
            Err(e) => s.toast(Severity::Error, format!("Not sent: {e}")),
        });
    }

    /// The answer field of a question line, with the keyboard.
    pub fn open_answer(&mut self, line: &Line) {
        self.home.expanded = Some(line.sel());
        if self.home.answering.as_ref().is_none_or(|(k, _)| *k != line.key) {
            self.home.answering = Some((line.key.clone(), String::new()));
        }
        self.home.focus_form = true;
    }

    /// The typed answer goes to the agent once its turn is over (never in
    /// the middle of one), the way a review reply does.
    pub fn answer_question(&mut self, line: &Line, text: String) {
        let text = text.trim().to_string();
        if text.is_empty() {
            return;
        }
        self.home.answering = None;
        let mut params = json!({"key": line.key, "text": text});
        if let Some(top) = line.top.as_ref().filter(|t| t.kind == "question") {
            params["id"] = json!(top.id);
        }
        let agent = line.agent().unwrap_or_default();
        self.conn.request("question_answer", params, move |s, reply| match reply {
            Ok(_) => {
                s.toast(Severity::Info, format!("Sent to agent {agent} once its turn is over."));
                s.refresh_home(true);
            }
            Err(e) => s.toast(Severity::Error, format!("Not sent: {e}")),
        });
    }

    /// Ctrl+Enter on an expanded brief: the picked option (1-4), else its
    /// preselected one (with no options, the typed answer), through
    /// `decision_answer`.
    pub fn answer_brief_inline(&mut self, d: &Decision) {
        let choice = self
            .home
            .brief_choice
            .clone()
            .filter(|(id, c)| *id == d.id && !c.trim().is_empty())
            .map(|(_, c)| c.trim().to_string())
            .or_else(|| crate::decision_view::preselected(d));
        let Some(choice) = choice else {
            let how = if d.brief.options.is_empty() { "Type an answer first." } else { "Pick an option first (1-4)." };
            self.toast(Severity::Info, how);
            return;
        };
        self.home.brief_choice = None;
        let id = d.id.clone();
        let params = json!({"id": id, "choice": choice, "comment": ""});
        self.conn.request("decision_answer", params, move |s, reply| match reply {
            Ok(v) => {
                if let Ok(r) = serde_json::from_value::<lazyagent_proto::types::DecisionResult>(v) {
                    s.decisions.insert(r.decision.id.clone(), r.decision);
                }
                s.inbox_gen += 1;
                s.toast(Severity::Info, format!("Answered {choice}: sent once the agent's turn is over."));
            }
            Err(e) => s.toast(Severity::Error, format!("Not answered: {e}")),
        });
    }

    /// The orchestrator strip's list of today's actions (`t`).
    pub fn toggle_strip(&mut self) {
        self.home.strip_open = !self.home.strip_open;
        if !self.home.strip_open {
            return;
        }
        if !self.has(F_ORCHESTRATOR) {
            self.orchestrator_actions = Some(vec![]);
            return;
        }
        self.conn.request("orchestrator_actions", json!({}), |s, reply| {
            match reply.and_then(|v| {
                serde_json::from_value::<lazyagent_proto::types::OrchestratorActions>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(list) => s.orchestrator_actions = Some(list.actions),
                Err(e) => {
                    s.orchestrator_actions = Some(vec![]);
                    s.toast(Severity::Error, format!("Could not read the orchestrator's actions: {e}"));
                }
            }
        });
    }

    /// Revert… on one of today's actions: its line, with the confirmation.
    pub fn revert_from_strip(&mut self, decision_id: &str) {
        let line = self.home.lines.values().find(|l| l.decided.as_ref().is_some_and(|d| d.id == decision_id)).cloned();
        match line {
            Some(line) => {
                self.home.selected = Some(line.key.clone());
                self.open_revert(&line);
            }
            None => self.toast(Severity::Info, "That decision is no longer on Home."),
        }
    }

    /// Offers to take the gesture back for [`UNDO_FOR`] seconds. A deny
    /// still waiting goes first: a new Undo never drops it.
    fn set_undo(&mut self, text: String, act: UndoAct) {
        if let Some(Undo { act: UndoAct::Deny(id, detail), .. }) = self.undo.take() {
            self.send_approval(id, detail, false);
        }
        self.undo = Some(Undo { text, act, at: unix_now() });
    }

    /// The Undo of the toast (or Ctrl+Z) while it shows.
    pub fn undo_last(&mut self) {
        let Some(undo) = self.undo.take().filter(|u| unix_now() - u.at < UNDO_FOR) else { return };
        let (method, params) = match &undo.act {
            UndoAct::Undismiss(id) => ("actionables_mark", json!({"id": id, "action": "undismiss"})),
            UndoAct::Unpark(id) => ("actionables_mark", json!({"id": id, "action": "unpark"})),
            UndoAct::Gesture(key, what) => ("status_gesture", json!({"key": key, "kind": "undo", "what": what})),
            UndoAct::Unapprove(key, sha) => ("owner_approval_revoke", json!({"key": key, "sha": sha, "undo": true})),
            UndoAct::Deny(..) => {
                // Never sent: nothing to ask the engine.
                self.toast(Severity::Info, "Not denied: the command still waits for you.");
                return;
            }
        };
        self.conn.request(method, params, move |s, reply| match reply {
            Ok(v) => {
                if let Some(row) = v.get("row").and_then(|r| serde_json::from_value::<StatusRow>(r.clone()).ok()) {
                    s.status_rows.insert(row.key.clone(), row);
                }
                if let Some(item) = v.get("item").and_then(|i| serde_json::from_value::<Actionable>(i.clone()).ok()) {
                    s.actionables.insert(item.id.clone(), item);
                }
                s.inbox_gen += 1;
                s.refresh_home(true);
                s.home.cut(unix_now());
                let late = v.get("delegation_sent") == Some(&json!(true));
                s.toast(Severity::Info, if late { "Undone; the agent had already got the line." } else { "Undone." });
            }
            Err(e) => s.toast(Severity::Error, format!("Not undone: {e}")),
        });
    }

    /// Expands or collapses the line `sel` ([`Line::sel`]).
    fn toggle_line(&mut self, sel: &str) {
        let open = self.home.expanded.as_deref() == Some(sel);
        self.home.expanded = (!open).then(|| sel.to_string());
        self.home.parking = None;
        self.home.closing = None;
        self.home.answering = None;
        self.home.item_open = None;
        if !open && let Some(line) = self.home.get(sel).cloned() {
            self.look_at(&line);
        }
    }

    /// "Answer here" on a brief in an expanded line's items: it opens under
    /// them, and 1-4 / Ctrl+Enter go to it (again: it closes).
    fn open_item(&mut self, sel: String, id: String) {
        let open = self.home.item_open.as_ref() == Some(&(sel.clone(), id.clone()));
        self.home.expanded = Some(sel.clone());
        self.home.item_open = (!open).then_some((sel.clone(), id.clone()));
        if open {
            return;
        }
        let decision =
            self.home.get(&sel).and_then(|l| l.under_item(&id)).and_then(|u| u.top.as_ref()?.decision_id.clone());
        if let Some(did) = decision {
            self.fetch_decision(&did);
        }
    }

    /// The user looked at a line: its timeline since the last look, its
    /// brief, and the item is seen (`status_seen {key}`).
    pub fn look_at(&mut self, line: &Line) {
        if let Some(id) = line.top.as_ref().and_then(|t| t.decision_id.clone()) {
            self.fetch_decision(&id);
        }
        if !self.gestures() || line.row.is_none() {
            return;
        }
        let key = line.key.clone();
        self.conn.request("status_timeline", json!({"key": key}), move |s, reply| {
            if let Ok(v) = reply
                && let Ok(t) = serde_json::from_value::<StatusTimeline>(v)
            {
                s.timelines.insert(t.key.clone(), t);
            }
        });
        self.seen_line(line);
    }

    /// A brief the client does not have yet (`decision_get`).
    fn fetch_decision(&mut self, id: &str) {
        if self.decisions.contains_key(id) || !self.has("decisions") {
            return;
        }
        self.conn.request("decision_get", json!({"id": id}), |s, reply| {
            if let Ok(v) = reply
                && let Ok(r) = serde_json::from_value::<lazyagent_proto::types::DecisionResult>(v)
            {
                s.decisions.insert(r.decision.id.clone(), r.decision);
                s.inbox_gen += 1;
            }
        });
    }

    /// `m`: this item is seen (its dot goes, for every client of the user).
    pub fn seen_line(&mut self, line: &Line) {
        if !self.gestures() || line.row.is_none() {
            return;
        }
        let now = unix_now();
        self.conn.notify("status_seen", json!({"key": line.key, "at": now}));
        if let Some(row) = self.status_rows.get_mut(&line.key) {
            row.seen_at = Some(now);
            self.inbox_gen += 1;
        }
    }

    /// "mark all seen" (`Shift+M`): the client's last look, and the
    /// engine's marks when it keeps them.
    pub fn mark_all_seen(&mut self) {
        self.mark_seen();
        if !self.gestures() {
            return;
        }
        let now = self.look_from;
        self.conn.notify("status_seen", json!({"at": now}));
        for row in self.status_rows.values_mut() {
            row.seen_at = Some(now);
        }
        self.inbox_gen += 1;
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
            let hint = if self.can_gesture(line) { " Close the work item with ⇧X." } else { "" };
            self.toast(Severity::Info, format!("Nothing to dismiss: it leaves once it is merged.{hint}"));
            return;
        };
        if self.engine_inbox() {
            let id = top.id.clone();
            let name = line.name.clone();
            self.conn.request(
                "actionables_mark",
                json!({"id": id, "action": "dismiss"}),
                move |s, reply| match reply {
                    Ok(_) => {
                        s.actionables.remove(&id);
                        s.inbox_gen += 1;
                        s.refresh_home(true);
                        s.home.cut(unix_now());
                        if s.has(F_UNDO) {
                            s.set_undo(format!("Dismissed {name}"), UndoAct::Undismiss(id));
                        }
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

    /// The park menu's choices for this engine.
    pub fn parks(&self) -> &'static [(&'static str, Park)] {
        if self.has("parked") { PARKS } else { LEGACY_PARKS }
    }

    /// Parks the line's top inbox item, or with `whole` (or a line with no
    /// item) the work item itself and its items (a row gesture, rule 7).
    pub fn park_line(&mut self, line: &Line, park: Park, whole: bool) {
        self.home.parking = None;
        self.home.park_whole = false;
        let until = match park {
            Park::For(secs) => json!(unix_now() + secs),
            Park::Event(e) => json!(e),
        };
        let top = if whole { None } else { line.top.as_ref() };
        let Some(top) = top else {
            if self.can_gesture(line) {
                let params = json!({"key": line.key, "kind": "park", "until": until});
                let undo = (format!("Parked {}", line.name), UndoAct::Gesture(line.key.clone(), "park"));
                self.gesture_undo(&line.sel(), params, "Not parked", Some(undo));
            } else {
                self.toast(Severity::Info, "Nothing to park: it leaves once it is merged.");
            }
            return;
        };
        if self.engine_inbox() {
            let id = top.id.clone();
            let params = json!({"id": id, "action": "park", "until": until});
            let name = line.name.clone();
            self.conn.request("actionables_mark", params, move |s, reply| match reply {
                Ok(v) => {
                    if serde_json::from_value::<ActionableResult>(v).is_ok() {
                        s.actionables.remove(&id);
                        s.inbox_gen += 1;
                        s.refresh_home(true);
                        s.home.cut(unix_now());
                        s.set_undo(format!("Parked {name}'s item"), UndoAct::Unpark(id));
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

    /// Row gestures (label, park, close) need the engine's `gestures` and a
    /// work item that is not done.
    pub fn can_gesture(&self, line: &Line) -> bool {
        self.gestures() && line.row.is_some() && !line.done()
    }

    fn gesture(&mut self, sel: &str, params: Value, what: &'static str) {
        self.gesture_undo(sel, params, what, None);
    }

    /// A gesture, and once the engine took it, an Undo for it (feature
    /// `undo`); the line `sel` stays expanded.
    fn gesture_undo(&mut self, sel: &str, mut params: Value, what: &'static str, undo: Option<(String, UndoAct)>) {
        let undo = undo.filter(|_| self.has(F_UNDO));
        if let Some(fields) = self.gesture_fields() {
            // Its row with the opt-in fields, as status_list's (I4j).
            params["fields"] = fields;
        }
        self.conn.request("status_gesture", params, move |s, reply| {
            match reply.and_then(|v| {
                serde_json::from_value::<StatusGestureResult>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => {
                    if let Some(row) = r.row {
                        s.put_row(row);
                        s.refresh_home(true);
                    }
                    s.home.cut(unix_now());
                    if let Some((text, act)) = undo {
                        s.set_undo(text, act);
                    }
                }
                Err(e) => s.toast(Severity::Error, format!("{what}: {e}")),
            }
        });
        self.home.expanded = Some(sel.to_string());
    }

    /// `l`: the label form (a ball and a short text, e.g. others · "QA").
    pub fn open_label(&mut self, line: &Line) {
        if !self.can_gesture(line) {
            self.toast(Severity::Info, "Labels need a work item and an engine with gestures.");
            return;
        }
        let g = line.gesture().filter(|g| g["kind"] == "label");
        self.home.expanded = Some(line.sel());
        self.home.focus_form = true;
        self.home.labeling = Some(Label {
            key: line.key.clone(),
            ball: g.and_then(|g| g["ball"].as_str()).unwrap_or("others").into(),
            text: g.and_then(|g| g["text"].as_str()).unwrap_or("").into(),
        });
    }

    pub fn label_line(&mut self, label: Label) {
        self.home.labeling = None;
        let text: String = label.text.trim().chars().take(LABEL_CHARS).collect();
        let params = json!({"key": label.key, "kind": "label", "ball": label.ball, "text": text});
        self.gesture(&label.key, params, "Not labelled");
    }

    /// `Shift+X`: close the work item, as done or dropped.
    pub fn close_menu(&mut self, line: &Line) {
        if !self.can_gesture(line) {
            self.toast(Severity::Info, "Closing a work item needs an engine with gestures.");
            return;
        }
        self.home.expanded = Some(line.sel());
        self.home.parking = None;
        self.home.closing = Some(line.sel());
    }

    /// The confirmed close: the work item as done or dropped, and (with the
    /// engine's `close_stop`) its agents stopped. Never the worktree, the branch
    /// or the PR.
    pub fn close_line(&mut self, line: &Line, how: &str) {
        self.home.closing = None;
        let mut params = json!({"key": line.key, "kind": "close", "how": how});
        if self.has(F_CLOSE_STOP) {
            params["stop_agents"] = json!(true);
        }
        self.gesture(&line.sel(), params, "Not closed");
    }

    /// Who `d` hands the line's item to: its live agent, else a restorable
    /// one (restored first), else a new one (spawned first).
    pub fn delegate_target(&self, line: &Line) -> DelegateTo {
        if line.row.as_ref().is_some_and(|r| !r.agents.is_empty()) {
            return DelegateTo::Agent;
        }
        match line.place.as_deref().and_then(|p| self.restorable.iter().find(|r| r.worktree_path == p)) {
            Some(r) => DelegateTo::Restore(r.id.clone()),
            None => DelegateTo::Spawn,
        }
    }

    /// `d` on a line with no live agent: restore its agent, or start one,
    /// then delegate (the engine sends the line once its first turn ends).
    pub fn delegate_after_start(&mut self, line: &Line) {
        let Some(place) = line.place.clone() else {
            self.toast(Severity::Info, "Nothing to delegate to: this item has no worktree.");
            return;
        };
        if self.has(F_DELEGATE_START) && self.has("delegation") {
            // One step in the engine: the items read before the agent runs
            // (a running agent takes the row out of the inbox), the letter
            // sent once it is at its prompt (I4j).
            let mut params = json!({"key": line.key, "kind": "delegate", "to": "agent", "env": self.env_json()});
            match self.delegate_target(line) {
                DelegateTo::Agent => return self.delegate_line(line),
                DelegateTo::Restore(id) => params["restore"] = json!(id),
                // No mode: the configured one, else the CLI's own default.
                DelegateTo::Spawn => {
                    params["spawn"] = json!({"default_permission_mode": "cli", "env": self.env_json()})
                }
            }
            if line.delegates().is_none_or(|d| d.is_empty())
                && let Some(top) = &line.top
            {
                params["item"] = json!(top.id);
            }
            let undo =
                (format!("Delegated {} to its agent", line.name), UndoAct::Gesture(line.key.clone(), "delegate"));
            self.gesture_undo(&line.sel(), params, "Not delegated", Some(undo));
            return;
        }
        let key = line.key.clone();
        let then = move |s: &mut State, what: &str, reply: Result<Value, lazyagent_proto::ClientError>| match reply {
            Ok(_) => match s.home.lines.get(&key).cloned() {
                // The row may not list the new agent yet: the engine checks.
                Some(line) => s.send_delegate(&line),
                None => s.toast(Severity::Info, format!("{what}; the item is gone, nothing delegated.")),
            },
            Err(e) => s.toast(Severity::Error, format!("{what} failed: {e}")),
        };
        match self.delegate_target(line) {
            DelegateTo::Agent => self.delegate_line(line),
            DelegateTo::Restore(id) => {
                let params = json!({"ids": [id], "env": self.env_json()});
                self.conn.request("restore_agents", params, move |s, reply| then(s, "Agent restored", reply));
            }
            DelegateTo::Spawn => {
                // No mode: the configured one, else the CLI's own default.
                let params = json!({"worktree_path": place, "default_permission_mode": "cli", "env": self.env_json()});
                self.conn.request("spawn_agent", params, move |s, reply| {
                    if let Ok(v) = &reply {
                        s.spawned(&place, v);
                    }
                    then(s, "Agent started", reply)
                });
            }
        }
    }

    /// `d`: the line's inbox item to the branch's agent (`status_gesture
    /// delegate to: agent`); the engine keeps the delegation and the row.
    pub fn delegate_line(&mut self, line: &Line) {
        if !self.engine_inbox() || !self.has("delegation") {
            self.toast(Severity::Info, "Delegating needs a newer engine: restart it (File → Restart…)");
            return;
        }
        let Some(top) = line.top.clone() else {
            self.toast(Severity::Info, "Nothing to delegate: this line has no inbox item.");
            return;
        };
        // Delegate all: the engine says which items go; the top one may
        // be the user's (I4i).
        let all = line.delegates().is_some_and(|d| !d.is_empty());
        if !all && matches!(top.kind.as_str(), "approval" | "question" | "turn_question" | "decision" | "delegation") {
            self.toast(Severity::Info, "That one is answered, not delegated: open the agent's terminal (o).");
            return;
        }
        if !all && top.kind == "review_request" {
            self.toast(Severity::Info, "A teammate asked you for this review: open the PR (p) and review it there.");
            return;
        }
        if !all && !top.delegable() {
            // The engine's catalog: an agent cannot do the user's review or merge.
            let verb = line.verb().to_string();
            self.toast(Severity::Info, format!("{verb} is yours to do: an agent cannot do it for you."));
            return;
        }
        if self.delegate_target(line) != DelegateTo::Agent {
            // No agent runs there: the buttons offer to restore or start one.
            let how = match self.delegate_target(line) {
                DelegateTo::Restore(_) => "Restore agent and delegate",
                _ => "Spawn an agent for this",
            };
            self.toast(Severity::Info, format!("No agent runs there: use \"{how}\" in the expanded line."));
            self.home.expanded = Some(line.sel());
            return;
        }
        self.send_delegate(line);
    }

    /// The delegation itself (`status_gesture delegate`), with its Undo.
    fn send_delegate(&mut self, line: &Line) {
        let Some(top) = line.top.clone() else { return };
        let mut params = json!({"key": line.key, "kind": "delegate", "to": "agent", "item": top.id});
        if let Some(all) = line.delegates().filter(|d| !d.is_empty()) {
            // Every delegable item, in one letter (I4i).
            params.as_object_mut().expect("an object").remove("item");
            let undo = (
                format!(
                    "Delegated {} item{} of {} to its agent",
                    all.len(),
                    if all.len() == 1 { "" } else { "s" },
                    line.name
                ),
                UndoAct::Gesture(line.key.clone(), "delegate"),
            );
            self.gesture_undo(&line.sel(), params, "Not delegated", Some(undo));
            return;
        }
        // With CI tracking the engine writes the failures itself (failed
        // jobs, steps, the last lines of their logs).
        if top.kind == "ci"
            && !self.has("actions")
            && let Some(place) = &line.place
            && let Some(pr) = self.store.pr_info.get(place).cloned().flatten()
        {
            params["text"] = json!(crate::attention::ci_failure_prompt(&pr));
        }
        let undo = (format!("Delegated {} to its agent", line.name), UndoAct::Gesture(line.key.clone(), "delegate"));
        self.gesture_undo(&line.sel(), params, "Not delegated", Some(undo));
    }

    /// One item of the line alone, from its own line in the expanded
    /// items (feature `delegate_all`).
    pub fn delegate_item(&mut self, line: &Line, item: &str) {
        let params = json!({"key": line.key, "kind": "delegate", "to": "agent", "item": item});
        let undo = (
            format!("Delegated one item of {} to its agent", line.name),
            UndoAct::Gesture(line.key.clone(), "delegate"),
        );
        self.gesture_undo(&line.sel(), params, "Not delegated", Some(undo));
    }

    /// Send anyway: the delegation's letter, typed now whatever the
    /// agent's status (it waited for the agent to be free).
    pub fn send_anyway(&mut self, line: &Line) {
        let params = json!({"key": line.key, "kind": "delegate", "to": "agent", "send_now": true});
        self.gesture_undo(&line.sel(), params, "Not sent", None);
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
        if !line.passable() {
            let verb = line.verb().to_string();
            self.toast(Severity::Info, format!("{verb} is yours: the orchestrator cannot act on it."));
            return;
        }
        let params = json!({"key": line.key, "kind": "delegate", "to": "orchestrator"});
        let undo = (format!("Passed {} to the orchestrator", line.name), UndoAct::Gesture(line.key.clone(), "pass"));
        self.gesture_undo(&line.sel(), params, "Could not pass it to the orchestrator", Some(undo));
    }

    /// `⇧A`: record the user's approval of the line's PR at the head shown
    /// (`owner_approve`, R3d); the engine refuses it when the head moved.
    /// Undo brings back the approval it replaced, if any.
    pub fn approve_head(&mut self, line: &Line) {
        let Some(a) = line.approvable().cloned() else {
            self.toast(Severity::Info, "⇧A approves your own PR while it waits for your review: not this line.");
            return;
        };
        let short = a.sha[..a.sha.len().min(7)].to_string();
        if a.approved {
            self.toast(Severity::Info, format!("You approved #{} at {short} already.", a.pr));
            return;
        }
        let (key, name, sel) = (line.key.clone(), line.name.clone(), line.sel());
        let params = json!({"key": key, "sha": a.sha, "source": "home"});
        self.conn.request("owner_approve", params, move |s, reply| match reply {
            Ok(_) => {
                s.inbox_gen += 1;
                s.fetch_inbox();
                s.home.cut(unix_now());
                let text = format!("Approved {name} at {short}");
                if s.has(F_UNDO) {
                    s.set_undo(text, UndoAct::Unapprove(key, a.sha.clone()));
                } else {
                    s.toast(Severity::Info, text);
                }
            }
            Err(e) => {
                // The head moved (or the line changed): show the new one.
                s.fetch_inbox();
                s.toast(Severity::Error, format!("Not approved: {e}"));
            }
        });
        self.home.expanded = Some(sel);
    }

    /// `u`: take a pass back, or drop the user's label / park / close.
    pub fn take_back(&mut self, line: &Line) {
        if line.claim.is_some() {
            let params = json!({"key": line.key, "kind": "clear"});
            self.gesture(&line.sel(), params, "Could not take it back");
        } else if line.gesture().is_some() && self.gestures() {
            let params = json!({"key": line.key, "kind": "clear"});
            self.gesture(&line.sel(), params, "Could not clear it");
        }
    }

    /// `Shift+U`: the revert form of the orchestrator's decision on the line.
    pub fn open_revert(&mut self, line: &Line) {
        if !self.first_responder() {
            return;
        }
        match &line.decided {
            Some(d) if d.revertible == Some(true) => {
                self.home.expanded = Some(line.sel());
                self.home.reverting = Some((d.id.clone(), String::new()));
                self.home.focus_form = true;
            }
            Some(_) => self.toast(Severity::Info, "This decision cannot be reverted any more."),
            None => self.toast(Severity::Info, "The orchestrator has decided nothing here."),
        }
    }

    /// `decision_revert`: the worker is told the user's override.
    pub fn revert(&mut self, id: String, comment: String) {
        self.home.reverting = None;
        let mut params = json!({"id": id});
        if !comment.trim().is_empty() {
            params["comment"] = json!(comment.trim());
        }
        self.conn.request("decision_revert", params, move |s, reply| match reply {
            Ok(_) => {
                if let Some(d) = s.decisions.get_mut(&id) {
                    d.revertible = Some(false);
                }
                s.inbox_gen += 1;
                s.toast(Severity::Info, "Reverted: the agent is told.");
                s.fetch_counts();
            }
            Err(e) => s.toast(Severity::Error, format!("Not reverted: {e}")),
        });
    }

    /// Accept or reject an agent's attestation (R5, `ci_attestation_confirm`):
    /// Reject sends the comment to the agent and brings Fix CI back.
    pub fn confirm_attestation(&mut self, id: String, accept: bool, comment: String) {
        self.home.rejecting = None;
        let mut params = json!({"id": id, "accept": accept});
        if !comment.trim().is_empty() {
            params["comment"] = json!(comment.trim());
        }
        self.conn.request("ci_attestation_confirm", params, move |s, reply| match reply {
            Ok(_) => {
                s.inbox_gen += 1;
                let what = if accept {
                    "Accepted: the check no longer counts against the branch."
                } else {
                    "Rejected: the agent gets your comment, and Fix CI is back."
                };
                s.toast(Severity::Info, what);
                s.fetch_inbox();
            }
            Err(e) => s.toast(Severity::Error, format!("Not recorded: {e}")),
        });
    }

    /// Reject…: the comment form, focused.
    pub fn open_reject(&mut self, id: String) {
        self.home.rejecting = Some((id, String::new()));
        self.home.focus_form = true;
    }

    /// The Home toggle: `auto` · `present` · `away`.
    pub fn set_presence(&mut self, mode: &str) {
        let mode = mode.to_string();
        self.conn.request("presence_set", json!({"mode": mode}), move |s, reply| match reply {
            Ok(v) => {
                let got = serde_json::from_value::<Presence>(v.get("presence").cloned().unwrap_or(v)).ok();
                s.presence = got.or_else(|| {
                    Some(Presence { mode: Some(mode), effective: s.presence.as_ref()?.effective.clone(), since: None })
                });
                s.fetch_counts();
            }
            Err(e) => s.toast(Severity::Error, format!("Presence not changed: {e}")),
        });
    }

    /// Keyboard or mouse activity: told to the engine at most once every
    /// [`PRESENCE_EVERY`]; true when this one was.
    pub fn user_input(&mut self, now: f64) -> bool {
        if !self.first_responder() || now - self.presence_reported < PRESENCE_EVERY {
            return false;
        }
        self.presence_reported = now;
        self.conn.notify("presence_report", json!({"input_at": now.floor() as i64}));
        true
    }

    /// The repo the settings apply to: the rows' repo, else the checkout's
    /// name.
    pub fn inbox_repo(&self) -> String {
        self.status_rows
            .values()
            .next()
            .map(|r| r.repo.clone())
            .unwrap_or_else(|| store::basename(&self.conn.root).into())
    }

    pub fn fetch_inbox_config(&mut self) {
        let repo = self.inbox_repo();
        self.conn.request("inbox_config_get", json!({"repo": repo}), |s, reply| match reply {
            Ok(v) => match serde_json::from_value::<InboxConfig>(v.get("config").cloned().unwrap_or(v)) {
                Ok(c) => s.inbox_config = Some(c),
                Err(e) => s.toast(Severity::Error, format!("Could not read auto-delegation: {e}")),
            },
            Err(e) => s.toast(Severity::Error, format!("Could not read auto-delegation: {e}")),
        });
    }

    pub fn set_inbox_config(&mut self, config: InboxConfig) {
        let repo = self.inbox_repo();
        let target = config.auto_delegate_target.clone().unwrap_or_else(|| "owner".into());
        let params = json!({"repo": repo, "auto_delegate": config.auto_delegate, "auto_delegate_target": target});
        self.inbox_config = Some(config);
        self.conn.request("inbox_config_set", params, |s, reply| {
            if let Err(e) = reply {
                s.toast(Severity::Error, format!("Auto-delegation not saved: {e}"));
                s.fetch_inbox_config();
            }
        });
    }

    /// A red PR: its failed checks to the worktree's agent (a delegation the
    /// engine follows, when it can).
    fn send_ci(&mut self, line: &Line) {
        if self.engine_inbox() && self.has("delegation") {
            self.delegate_line(line);
            return;
        }
        let Some(place) = line.place.clone() else { return };
        let pr = self.store.pr_info.get(&place).cloned().flatten();
        let agent = self.store.agents(&place).first().map(|a| a.agent_id.clone());
        if let (Some(pr), Some(agent)) = (pr, agent) {
            self.send_reply(&place, &agent, crate::attention::ci_failure_prompt(&pr));
        }
    }

    /// The failed log of a CI line's red run, once per run (id, attempt).
    fn fetch_ci_log(&mut self, key: String, run: (i64, i64)) {
        if self.ci_logs.get(&key).is_some_and(|(r, _)| *r == run) {
            return;
        }
        self.ci_logs.insert(key.clone(), (run, None));
        self.conn.request("ci_log_summary", json!({"key": key}), move |s, reply| {
            let summary = match reply {
                Ok(v) => serde_json::from_value::<CiLogSummary>(v)
                    .unwrap_or_else(|e| CiLogSummary { error: Some(e.to_string()), ..Default::default() }),
                Err(e) => CiLogSummary { error: Some(e.to_string()), ..Default::default() },
            };
            if let Some(entry) = s.ci_logs.get_mut(&key).filter(|(r, _)| *r == run) {
                entry.1 = Some(summary);
            }
        });
    }
}

/// Who a delegation goes to (`State::delegate_target`).
#[derive(Debug, Clone, PartialEq)]
pub enum DelegateTo {
    /// Its live agent.
    Agent,
    /// No live agent: this restorable entry, restored first.
    Restore(String),
    /// Nobody: a new agent, started first.
    Spawn,
}

/// Settings → Auto-delegation (per repo, Q-B33): which stalled kinds the
/// engine delegates on its own, and to whom. `.lazyagent.toml` stays the
/// source of truth; the engine writes it.
pub fn auto_delegation_menu(ui: &mut Ui, s: &mut State) {
    // Wide enough that its sentences wrap by words, never letter by letter.
    ui.set_min_width(AUTO_DELEGATE_WIDTH);
    let Some(config) = s.inbox_config.clone() else {
        ui.label(RichText::new("Reading…").small());
        return;
    };
    ui.label(RichText::new(format!("When one of these stalls in {}, delegate it:", s.inbox_repo())).small());
    let mut next = config.clone();
    let tracking = s.has("actions");
    for (kind, label) in AUTO_DELEGATE {
        let label = if *kind == "ci_red" && !tracking { CI_RED_INACTIVE } else { label };
        let mut on = next.auto_delegate.iter().any(|k| k == kind);
        if ui.checkbox(&mut on, label).changed() {
            next.auto_delegate.retain(|k| k != kind);
            if on {
                next.auto_delegate.push(kind.to_string());
            }
        }
    }
    ui.separator();
    ui.label(RichText::new("to").small());
    let target = next.auto_delegate_target.clone().unwrap_or_else(|| "owner".into());
    for (value, label) in [("owner", "the branch's owner agent"), ("orchestrator", "the orchestrator")] {
        if ui.radio(target == value, label).clicked() {
            next.auto_delegate_target = Some(value.into());
        }
    }
    // The skip list is the file's (shown, not edited here; the set above
    // leaves it as it is).
    ui.separator();
    let skip = &config.auto_delegate_skip_checks;
    let text = if skip.is_empty() {
        "Failing checks it leaves alone: none".to_string()
    } else {
        format!("Failing checks it leaves alone: {}", skip.join(", "))
    };
    ui.label(RichText::new(text).small());
    ui.label(
        RichText::new(
            "A red made only of these stays yours. Edit [inbox] auto_delegate_skip_checks in .lazyagent.toml.",
        )
        .small()
        .weak(),
    );
    if next != config {
        s.set_inbox_config(next);
    }
}

/// A red run's failed jobs: "job · step", then the last lines of its log;
/// a red check that is not Actions' by its name, provider and link.
fn ci_log(ui: &mut Ui, t: &Theme, summary: &CiLogSummary, acts: &mut Vec<Act>) {
    if let Some(e) = &summary.error {
        ui.label(RichText::new(e).small().color(t.yellow));
    }
    for check in summary.checks.iter().flatten() {
        ui.horizontal(|ui| {
            ui.label(RichText::new(format!("{} · {}", check.name, check.provider)).small().strong().color(t.text));
            if !check.url.is_empty() && ui.small_button("open the check").on_hover_text(&check.url).clicked() {
                acts.push(Act::Pr(check.url.clone()));
            }
        });
    }
    for job in &summary.jobs {
        let head = if job.step.is_empty() { job.name.clone() } else { format!("{} · {}", job.name, job.step) };
        ui.label(RichText::new(head).small().strong().color(t.text));
        if !job.lines.is_empty() {
            ui.label(RichText::new(job.lines.join("\n")).small().monospace().color(t.text));
        }
    }
    if summary.jobs.is_empty() && summary.error.is_none() && summary.checks.as_ref().is_none_or(|c| c.is_empty()) {
        ui.label(RichText::new("No failed job log to show.").small().color(t.muted));
    }
}

/// A rule file as the error line names it: the repo's by its path in the
/// repo, the global one as such.
fn rules_file_name(p: &RulesProblem) -> String {
    match (p.scope.as_deref(), p.file.as_deref()) {
        (Some("repo"), _) => ".lazyagent/rules.toml".to_string(),
        (Some("global"), _) => "your global rules.toml".to_string(),
        (_, Some(f)) => f.rsplit('/').next().unwrap_or(f).to_string(),
        _ => "rules.toml".to_string(),
    }
}

/// One rule file error, in full: file, line, rule, what happened, what is
/// in force instead.
pub fn rules_problem_text(p: &RulesProblem) -> String {
    let mut text = rules_file_name(p);
    if let Some(line) = p.line {
        text.push_str(&format!(", line {line}"));
    }
    text.push_str(": ");
    let mut message = p.message.as_str();
    if let Some(rule) = &p.rule {
        text.push_str(&format!("rule {rule}: "));
        message = message.strip_prefix(&format!("rule {rule}")).unwrap_or(message).trim_start_matches([':', ' ']);
    }
    text.push_str(message);
    if !p.fallback.is_empty() {
        text.push_str(&format!(" — {}", p.fallback));
    }
    text
}

/// Home's line while the rule files have errors (`rules.errors`): how many,
/// and the first one named by its rule, file and line.
/// Home's line while GitHub does not answer (`status_list.github`): since
/// when, and when the PR data the rows still show was fetched. None while
/// it answers.
pub fn github_line(g: Option<&GithubHealth>, offset: i64) -> Option<String> {
    let g = g.filter(|g| !g.available)?;
    if g.installed == Some(false) {
        return Some("GitHub: gh is not installed (no PR, CI or review data)".into());
    }
    let mut text = match g.since {
        Some(s) => format!("GitHub unavailable since {}", home::hhmm(s, offset)),
        None => "GitHub unavailable".to_string(),
    };
    if let Some(at) = g.pr_data_at {
        text.push_str(&format!(" · PR data from {}", home::hhmm(at, offset)));
    }
    Some(text)
}

pub fn rules_error_line(errors: &[RulesProblem]) -> Option<String> {
    let first = errors.first()?;
    let n = errors.len();
    let count = if n == 1 { "1 error".to_string() } else { format!("{n} errors") };
    Some(format!("Rules: {count} · {}", rules_problem_text(first)))
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

/// The longest middle-ellipsis form of `name` that fits `width` points.
pub fn fit_name(ui: &Ui, name: &str, width: f32) -> String {
    let font = egui::TextStyle::Body.resolve(ui.style());
    let fits = |text: &str| {
        ui.fonts_mut(|f| f.layout_no_wrap(text.to_string(), font.clone(), Color32::WHITE).size().x) <= width
    };
    if fits(name) {
        return name.to_string();
    }
    let (mut lo, mut hi) = (1, name.chars().count());
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        if fits(&home::middle_ellipsis(name, mid)) { lo = mid } else { hi = mid - 1 }
    }
    home::middle_ellipsis(name, lo)
}

/// The name cell: start and end of the branch kept, the full name on hover.
/// True when the name had to be cut.
fn name_cell(ui: &mut Ui, width: f32, name: &str) -> bool {
    ui.allocate_ui_with_layout(Vec2::new(width, 18.0), Layout::left_to_right(Align::Center), |ui| {
        ui.set_width(width);
        let shown = fit_name(ui, name, width - 2.0);
        let label = ui.add(egui::Label::new(RichText::new(&shown).strong()).sense(Sense::hover()));
        let cut = shown != name;
        if cut {
            label.on_hover_text(name);
        }
        cut
    })
    .inner
}

/// A project's header inside In flight: its title and how many lines.
fn project_header(ui: &mut Ui, t: &Theme, title: &str, n: usize, continues: bool) {
    ui.add_space(4.0);
    ui.horizontal(|ui| {
        ui.add_space(10.0);
        ui.label(RichText::new(format!("▸ {title}")).small().strong().color(t.text));
        ui.label(RichText::new(format!("· {n}")).small().color(t.muted));
        if continues {
            ui.label(RichText::new("· continues while in review").small().color(t.muted)).on_hover_text(
                "The orchestrator may start the next task once its dependencies are merged or in review",
            );
        }
    });
}

/// High and low priority, before the name ("▴" / "▾"); medium shows none.
fn priority_cell(ui: &mut Ui, t: &Theme, priority: &str) {
    let marker = home::priority_marker(priority);
    let color = if priority == "high" { t.yellow } else { t.muted };
    let r = ui
        .allocate_ui_with_layout(Vec2::new(8.0, 18.0), Layout::left_to_right(Align::Center), |ui| {
            ui.set_width(8.0);
            ui.label(RichText::new(marker).small().color(color));
        })
        .response;
    if !marker.is_empty() {
        r.on_hover_text(format!("{priority} priority"));
    }
}

/// A zone's header: a large count, then its name at body size, bold.
fn zone_header(ui: &mut Ui, t: &Theme, count: usize, title: &str, color: Color32, extra: impl FnOnce(&mut Ui)) {
    ui.add_space(10.0);
    ui.horizontal(|ui| {
        ui.label(RichText::new(count.to_string()).size(20.0).strong().color(color));
        ui.label(RichText::new(title).strong().color(t.text));
        extra(ui);
    });
    ui.add_space(2.0);
}

/// Every key the key bar may name (the page lists each one; a test checks).
pub fn hint_keys(
    gestures: bool,
    delegation: bool,
    first_responder: bool,
    decision_view: bool,
    approval: bool,
) -> String {
    let mut keys = vec!["Enter open", "1-4 zones", "Tab next for you", "Esc collapse"];
    if approval {
        keys.extend(["a approve", "⇧D deny"]);
    }
    keys.extend(["o terminal", "n notes", "p PR", "x dismiss", "z park", "⇧O pass", "u take back"]);
    if delegation {
        keys.push("d delegate to its agent");
    }
    if gestures {
        keys.extend(["l label", "⇧X close", "m seen", "⇧M all seen"]);
    }
    keys.extend(["⇧Z parked", "⇧F done today"]);
    if first_responder {
        keys.extend(["⇧U revert", "t today's actions"]);
    }
    if decision_view {
        keys.extend(["v decision view", "y explain", "⇧Y restore and explain"]);
    }
    keys.extend(["w why", "f fold", "Ctrl+Z undo", "? how it works"]);
    keys.join(" · ")
}

/// The most keys the bar names for one line (then "? all keys").
pub const BAR_KEYS: usize = 8;

/// The key bar for the selected line: only the keys that do something on
/// it, at most [`BAR_KEYS`], then "? all keys" (the page lists them all).
pub fn line_keys(c: &KeysCtx, line: Option<&Line>) -> String {
    let mut keys: Vec<&str> = vec![];
    match line {
        None => keys.extend(["1-4 zones", "Tab next for you"]),
        Some(l) => {
            let kind = l.top.as_ref().map(|t| t.kind.as_str()).unwrap_or("");
            let has_row = l.row.is_some() && !l.done();
            match (l.zone, kind) {
                (_, "approval") if c.approvals => keys.extend(["a approve", "⇧D deny", "Enter expand", "o terminal"]),
                (_, "question") => keys.extend(["Enter answer", "o terminal"]),
                // A question in prose: answered in the agent's terminal (I4h).
                (_, "turn_question") => keys.extend(["Enter open", "o terminal"]),
                // A ghost's brief is answered on it: Enter expands it (I4g).
                (_, "decision") if c.decision_view && l.ghost.is_some() => {
                    let pick = l.brief.as_ref().is_some_and(|d| !d.brief.options.is_empty());
                    keys.extend(if pick {
                        ["Enter expand", "1-4 pick", "v decision view"].as_slice()
                    } else {
                        ["Enter expand", "v decision view"].as_slice()
                    })
                }
                (_, "decision") if c.decision_view => keys.extend(["Enter open", "v decision view"]),
                (Zone::Parked, _) => keys.extend(["Enter expand", "u unpark", "o terminal"]),
                (Zone::Flight, _) => keys.extend(["Enter expand", "o terminal", "n notes"]),
                _ if l.verb() == "Merge" || kind == "review_request" => keys.extend(["p PR", "Enter expand"]),
                _ => keys.extend(["Enter expand", "o terminal"]),
            }
            if l.approvable().is_some_and(|a| !a.approved) {
                keys.push("⇧A approve");
            }
            if l.top.as_ref().is_some_and(|t| t.is_ci()) && l.url.is_some() {
                keys.push("p PR");
            }
            if c.delegation && l.delegable() {
                keys.push("d delegate");
            }
            if l.top.is_some() {
                keys.extend(["x dismiss", "z park"]);
            } else if c.gestures && has_row && l.zone == Zone::Flight {
                keys.extend(["l label", "z park"]);
            }
            if c.decision_view && l.zone != Zone::Flight {
                keys.push("y explain");
            }
            if l.zone == Zone::Flight && has_row && c.pass && l.passable() {
                keys.push("⇧O pass");
            }
            if c.gestures && has_row {
                keys.push("⇧X close");
            }
            if c.why && l.row.is_some() {
                keys.push("w why");
            }
        }
    }
    let mut seen = std::collections::HashSet::new();
    keys.retain(|k| seen.insert(*k));
    keys.truncate(BAR_KEYS);
    keys.push("? all keys");
    keys.join(" · ")
}

/// What the key bar knows of the engine.
#[derive(Clone, Copy, Default)]
pub struct KeysCtx {
    pub approvals: bool,
    pub decision_view: bool,
    pub delegation: bool,
    pub gestures: bool,
    pub pass: bool,
    pub why: bool,
}

impl App {
    pub(crate) fn home_view(&mut self, ui: &mut Ui) {
        if self.state.decision.is_some() {
            self.decision_view(ui);
            return;
        }
        if self.state.why.is_some() {
            self.why_view(ui);
            return;
        }
        let t = self.state.theme;
        let now = unix_now();
        let folded = self.state.saved.home_fold;
        let selected = self.state.home.current(folded).map(|l| l.sel());
        let home = &self.state.home;
        let forms = Forms {
            expanded: home.expanded.clone(),
            parking: home.parking.clone(),
            park_whole: home.park_whole,
            closing: home.closing.clone(),
            labeling: home.labeling.clone(),
            reverting: home.reverting.clone(),
            rejecting: home.rejecting.clone(),
            answering: home.answering.clone(),
            brief_choice: home.brief_choice.clone(),
            item_open: home.item_open.clone(),
            focus: home.focus_form,
        };
        let lines: Vec<Vec<Line>> =
            home::ZONES.iter().map(|z| home.shown_in(*z).into_iter().cloned().collect()).collect();
        let next_round: Vec<Line> = home.next_round().into_iter().cloned().collect();
        let (waiting, resolved, round_at) = (home.waiting(), home.resolved, home.round_at);
        let parked_open = home.parked_open;
        let done_group: Vec<Line> = home.done_group().into_iter().cloned().collect();
        let done_open = home.done_open;
        let strip_open = home.strip_open;
        let deep_count = home.deep_count();
        let ghosts: Vec<Line> = home.deep_ghosts().into_iter().cloned().collect();
        let stale_group: Vec<Line> = home.stale_group().into_iter().cloned().collect();
        let stale_open = home.stale_open;
        let s = &self.state;
        let counts = s.status_counts;
        let ctx = LineCtx {
            t: &t,
            now,
            look: s.look_from,
            pass: s.can_pass(),
            engine: s.engine_inbox(),
            gestures: s.gestures(),
            first_responder: s.first_responder(),
            delegation: s.engine_inbox() && s.has("delegation"),
            delegate_all: s.engine_inbox() && s.has("delegate_all"),
            parks: s.parks(),
            selected: selected.as_deref(),
            timelines: &s.timelines,
            actions: s.engine_inbox() && s.has("actions"),
            ci_logs: &s.ci_logs,
            decision_view: s.decision_view_on(),
            explains: &s.explains,
            approvals: s.inline_approvals(),
            questions: s.inline_questions(),
            close_stop: s.has(F_CLOSE_STOP),
            why: s.has("rules"),
            restorable: s.restorable.iter().map(|r| r.worktree_path.clone()).collect(),
        };
        let keys_ctx = KeysCtx {
            approvals: ctx.approvals,
            decision_view: ctx.decision_view,
            delegation: ctx.delegation,
            gestures: ctx.gestures,
            pass: ctx.pass.is_ok(),
            why: ctx.why,
        };
        let selected_line = selected.as_ref().and_then(|k| s.home.get(k)).cloned();
        let rules_line = rules_error_line(&s.rules_errors);
        let rules_all: Vec<String> = s.rules_errors.iter().map(rules_problem_text).collect();
        let github = github_line(s.github.as_ref(), home::local_offset(now));
        let github_why = s.github.as_ref().and_then(|g| g.error.clone()).unwrap_or_default();
        let presence = s.presence.clone();
        let orchestrator = s.orchestrator.clone();
        let actions = s.orchestrator_actions.clone();
        let undo = s.undo.clone().filter(|u| now - u.at < UNDO_FOR);
        let mut acts: Vec<Act> = vec![];
        let mut forms_out = forms.clone();
        // The keys and the Undo stay at the bottom of Home, whatever scrolls.
        egui::Panel::bottom("home-keys").frame(egui::Frame::new().inner_margin(6)).show(ui, |ui| {
            if let Some(u) = &undo {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&u.text).color(t.text));
                    if ui.button("Undo  (Ctrl+Z)").clicked() {
                        acts.push(Act::Undo);
                    }
                    let left = (UNDO_FOR - (now - u.at)).max(0.0).ceil();
                    ui.label(RichText::new(format!("{left:.0} s")).small().color(t.muted));
                });
                ui.ctx().request_repaint_after(std::time::Duration::from_millis(250));
            }
            // One line of its own: the selected line's keys, never a wall.
            let keys = line_keys(&keys_ctx, selected_line.as_ref());
            let all = hint_keys(ctx.gestures, ctx.delegation, ctx.first_responder, ctx.decision_view, ctx.approvals);
            ui.add(egui::Label::new(RichText::new(keys).small().color(t.muted)).truncate())
                .on_hover_text(all.replace(" · ", "\n"));
        });
        egui::ScrollArea::vertical().id_salt("home").auto_shrink([false, false]).show(ui, |ui| {
            ui.set_max_width(ui.available_width().min(CONTENT_MAX));
            if let Some(text) = &github {
                // gh's error on hover; the rows keep their last PR data.
                ui.add(egui::Label::new(RichText::new(text).color(t.yellow)).truncate())
                    .on_hover_text(format!("{github_why}\nRetrying with backoff (at most every 10 minutes)."));
            }
            if let Some(text) = &rules_line {
                // Under the top bar while a rule file has errors; each one in
                // full on hover (the page that lists them is R6's).
                ui.add(egui::Label::new(RichText::new(text).color(t.red)).truncate())
                    .on_hover_text(rules_all.join("\n"));
            }
            if ctx.first_responder {
                orchestrator_strip(ui, &ctx, presence.as_ref(), counts, orchestrator.as_ref(), actions.as_deref(), strip_open, &mut acts);
            }
            let zone = |ui: &mut Ui, z: Zone, forms: &mut Forms, acts: &mut Vec<Act>| {
                let mut group: Option<&str> = None;
                for (i, l) in lines[z as usize].iter().enumerate() {
                    // In flight is grouped by project (home::group_by_project).
                    let project = home::project_of(l).filter(|_| z == Zone::Flight);
                    if let Some(p) = project
                        && group != Some(p.id.as_str())
                    {
                        let n = lines[z as usize][i..]
                            .iter()
                            .take_while(|m| home::project_of(m).is_some_and(|q| q.id == p.id))
                            .count();
                        project_header(ui, &t, &p.title, n, home::continues_while_review(p));
                    }
                    group = project.map(|p| p.id.as_str());
                    line_ui(ui, &ctx, l, z, false, forms, acts);
                }
            };
            if !lines[0].is_empty() {
                zone_header(ui, &t, lines[0].len(), "Now", t.yellow, |_| {});
                zone(ui, Zone::Now, &mut forms_out, &mut acts);
            }
            let parked = &lines[Zone::Parked as usize];
            let parked_need = parked.iter().filter(|l| l.parked_needs_you()).count();
            let round_title = if waiting > 0 { format!("Round (+{waiting} next)") } else { "Round".to_string() };
            zone_header(ui, &t, lines[1].len(), &round_title, t.accent, |ui| {
                ui.label(RichText::new(format!("· delivered {} ago", home::age(now - round_at))).small().color(t.muted));
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if waiting > 0
                        && ui
                            .button(RichText::new("deliver now ▸").small())
                            .on_hover_text(
                                "The next round comes when you close an item, come back from a terminal, or every 30 min",
                            )
                            .clicked()
                    {
                        acts.push(Act::Cut);
                    }
                    if parked_need > 0
                        && ui
                            .button(RichText::new(format!("{parked_need} parked needs you ▸")).small().color(t.yellow))
                            .on_hover_text("A parked work item has an item that is still yours")
                            .clicked()
                    {
                        acts.push(Act::ParkedShow);
                    }
                    if resolved > 0 {
                        ui.label(RichText::new(format!("{resolved} resolved on their own")).small().color(t.muted));
                    }
                });
            });
            if lines[1].is_empty() && next_round.is_empty() {
                let text = if lines[0].is_empty() { "Nothing needs you." } else { "Nothing else in this round." };
                ui.label(RichText::new(text).color(t.muted));
            }
            zone(ui, Zone::Round, &mut forms_out, &mut acts);
            for l in &next_round {
                line_ui(ui, &ctx, l, Zone::Round, true, &mut forms_out, &mut acts);
            }
            if ctx.gestures || deep_count > 0 {
                zone_header(ui, &t, deep_count, "To sit down with", t.purple, |ui| {
                    // deep_max is shown, never enforced (§5b).
                    let max = counts.deep_max as usize;
                    if max > 0 && deep_count > max {
                        ui.label(RichText::new(format!("· over your limit of {max}")).small().color(t.yellow));
                    } else if max > 0 {
                        ui.label(RichText::new(format!("· of {max}, for a focus block")).small().color(t.muted));
                    }
                });
                if lines[2].is_empty() && ghosts.is_empty() {
                    ui.label(RichText::new("Nothing to sit down with.").color(t.muted));
                }
                zone(ui, Zone::Deep, &mut forms_out, &mut acts);
                for l in &ghosts {
                    ghost_ui(ui, &ctx, l, &mut forms_out, &mut acts);
                }
            }
            ui.add_space(6.0);
            ui.separator();
            let flight = &lines[3];
            // Done rows with a suggestion count: they still ask something.
            zone_header(ui, &t, flight.len(), "In flight", t.muted, |ui| {
                if counts.not_started > 0 {
                    ui.label(RichText::new(format!("· {} not started", counts.not_started)).small().color(t.muted))
                        .on_hover_text("Tasks a planner or a tracker gave Home that have no branch yet");
                }
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.button(RichText::new(if folded { "unfold (f)" } else { "fold (f)" }).small()).clicked() {
                        acts.push(Act::Fold);
                    }
                    if lines.iter().flatten().any(|l| l.unseen(ctx.look))
                        && ui
                            .button(RichText::new("mark all seen (⇧M)").small())
                            .on_hover_text("Clear the • dots")
                            .clicked()
                    {
                        acts.push(Act::Seen);
                    }
                });
            });
            if !folded {
                if flight.is_empty() && done_group.is_empty() {
                    ui.label(RichText::new("No work in flight.").color(t.muted));
                }
                zone(ui, Zone::Flight, &mut forms_out, &mut acts);
                if !done_group.is_empty() {
                    let arrow = if done_open { "▾" } else { "▸" };
                    ui.add_space(4.0);
                    ui.horizontal(|ui| {
                        ui.add_space(10.0);
                        let text = RichText::new(format!("{} done today {arrow}", done_group.len())).small();
                        if ui
                            .add(egui::Button::new(text.color(t.muted)).frame(false))
                            .on_hover_text("Merged, closed or removed in the last day; nothing to do (⇧F)")
                            .clicked()
                        {
                            acts.push(Act::DoneFold);
                        }
                    });
                    if done_open {
                        for l in &done_group {
                            line_ui(ui, &ctx, l, Zone::Flight, false, &mut forms_out, &mut acts);
                        }
                    }
                }
                if !stale_group.is_empty() {
                    // Never gone without a trace: worktrees nothing moved in
                    // for days, with what they would lose.
                    let arrow = if stale_open { "▾" } else { "▸" };
                    let dirty: u32 = stale_group.iter().filter_map(|l| l.row.as_ref()?.dirty).sum();
                    let mut text = format!("{} stale {arrow}", stale_group.len());
                    if dirty > 0 {
                        text = format!("{} stale · {dirty} uncommitted files {arrow}", stale_group.len());
                    }
                    ui.add_space(4.0);
                    ui.horizontal(|ui| {
                        ui.add_space(10.0);
                        let color = if dirty > 0 { t.yellow } else { t.muted };
                        if ui
                            .add(egui::Button::new(RichText::new(text).small().color(color)).frame(false))
                            .on_hover_text("Only a branch, unchanged for [status] stale_days: no agent, no PR")
                            .clicked()
                        {
                            acts.push(Act::StaleFold);
                        }
                    });
                    if stale_open {
                        for l in &stale_group {
                            line_ui(ui, &ctx, l, Zone::Flight, false, &mut forms_out, &mut acts);
                        }
                    }
                }
            }
            if !parked.is_empty() {
                let color = if parked_need > 0 { t.yellow } else { t.muted };
                zone_header(ui, &t, parked.len(), "Parked", color, |ui| {
                    if parked_need > 0 {
                        ui.label(RichText::new(format!("· {parked_need} needs you")).small().color(t.yellow));
                    }
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        let text = if parked_open { "hide (⇧Z)" } else { "show (⇧Z)" };
                        if ui.button(RichText::new(text).small()).clicked() {
                            acts.push(Act::ParkedFold);
                        }
                    });
                });
                if parked_open {
                    zone(ui, Zone::Parked, &mut forms_out, &mut acts);
                }
            }
            ui.add_space(8.0);
        });
        let s = &mut self.state;
        if !forms_out.focus {
            s.home.focus_form = false;
        }
        // What the forms' text fields typed this frame.
        if forms_out.labeling != forms.labeling
            && let Some(l) = forms_out.labeling.clone()
        {
            acts.insert(0, Act::LabelEdit(l));
        }
        if forms_out.reverting != forms.reverting
            && let Some((_, text)) = forms_out.reverting.clone()
        {
            acts.insert(0, Act::RevertEdit(text));
        }
        if forms_out.answering != forms.answering
            && let Some((key, text)) = forms_out.answering.clone()
        {
            acts.insert(0, Act::AnswerEdit(key, text));
        }
        if forms_out.rejecting != forms.rejecting
            && let Some((_, text)) = forms_out.rejecting.clone()
        {
            acts.insert(0, Act::RejectEdit(text));
        }
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
                    s.home.park_whole = s.home.get(&k).is_some_and(|l| l.top.is_none());
                    s.home.parking = (!open).then_some(k);
                }
                Act::Park(l, p, whole) => s.park_line(&l, p, whole),
                Act::ParkWhole(on) => s.home.park_whole = on,
                Act::ParkedFold => s.home.parked_open = !s.home.parked_open,
                Act::ParkedShow => s.home.parked_open = true,
                Act::DoneFold => s.toggle_done_group(),
                Act::StaleFold => {
                    s.home.stale_open = !s.home.stale_open;
                    if s.home.stale_open {
                        s.saved.home_fold = false;
                        s.touch_saved();
                    }
                }
                Act::RestoreDelegate(l) | Act::SpawnDelegate(l) => s.delegate_after_start(&l),
                Act::StartOrchestrator => s.spawn_agent(None),
                Act::Delegate(l) => s.delegate_line(&l),
                Act::DelegateItem(l, id) => s.delegate_item(&l, &id),
                Act::SendAnyway(l) => s.send_anyway(&l),
                Act::Pass(l) => s.pass_line(&l),
                Act::OwnerApprove(l) => s.approve_head(&l),
                Act::TakeBack(l) => s.take_back(&l),
                Act::SendCi(l) => s.send_ci(&l),
                Act::CiLog(key, run) => s.fetch_ci_log(key, run),
                Act::Cut => s.home.cut(now),
                Act::Fold => {
                    s.saved.home_fold = !s.saved.home_fold;
                    s.touch_saved();
                }
                Act::Seen => s.mark_all_seen(),
                Act::SeenLine(l) => s.seen_line(&l),
                Act::Why(l) => s.open_why(&l),
                Act::LabelOpen(l) => s.open_label(&l),
                Act::LabelEdit(l) => s.home.labeling = Some(l),
                Act::LabelSave(l) => s.label_line(l),
                Act::LabelCancel => s.home.labeling = None,
                Act::CloseMenu(k) => {
                    let open = s.home.closing.as_deref() == Some(k.as_str());
                    s.home.closing = (!open).then_some(k);
                }
                Act::Close(l, how) => s.close_line(&l, how),
                Act::RevertOpen(l) => s.open_revert(&l),
                Act::RevertEdit(text) => {
                    if let Some(r) = s.home.reverting.as_mut() {
                        r.1 = text;
                    }
                }
                Act::Revert(id, comment) => s.revert(id, comment),
                Act::RevertCancel => s.home.reverting = None,
                Act::AttestAccept(id) => s.confirm_attestation(id, true, String::new()),
                Act::RejectOpen(id) => s.open_reject(id),
                Act::RejectEdit(text) => {
                    if let Some(r) = s.home.rejecting.as_mut() {
                        r.1 = text;
                    }
                }
                Act::Reject(id, comment) => s.confirm_attestation(id, false, comment),
                Act::RejectCancel => s.home.rejecting = None,
                Act::Presence(mode) => s.set_presence(mode),
                Act::Approve(l, allow) => s.answer_approval(&l, allow),
                Act::AnswerEdit(key, text) => s.home.answering = Some((key, text)),
                Act::Answer(l, text) => s.answer_question(&l, text),
                Act::AnswerCancel => s.home.answering = None,
                Act::BriefChoose(id, choice) => s.home.brief_choice = Some((id, choice)),
                Act::BriefAnswer(d) => s.answer_brief_inline(&d),
                Act::ItemOpen(sel, id) => s.open_item(sel, id),
                Act::Undo => s.undo_last(),
                Act::StripToggle => s.toggle_strip(),
                Act::RevertAction(id) => s.revert_from_strip(&id),
            }
        }
    }
}

/// The open forms of Home's lines (a copy the text fields edit).
#[derive(Clone, PartialEq)]
struct Forms {
    expanded: Option<String>,
    parking: Option<String>,
    park_whole: bool,
    closing: Option<String>,
    labeling: Option<Label>,
    reverting: Option<(String, String)>,
    rejecting: Option<(String, String)>,
    answering: Option<(String, String)>,
    brief_choice: Option<(String, String)>,
    item_open: Option<(String, String)>,
    focus: bool,
}

struct LineCtx<'a> {
    t: &'a Theme,
    now: f64,
    look: f64,
    pass: Result<(), &'static str>,
    engine: bool,
    gestures: bool,
    first_responder: bool,
    delegation: bool,
    /// Delegate all, held letters and Send anyway (feature `delegate_all`).
    delegate_all: bool,
    parks: &'static [(&'static str, Park)],
    selected: Option<&'a str>,
    timelines: &'a std::collections::HashMap<String, StatusTimeline>,
    /// CI tracking (feature `actions`): the failed log of a CI line.
    actions: bool,
    ci_logs: &'a std::collections::HashMap<String, ((i64, i64), Option<CiLogSummary>)>,
    decision_view: bool,
    explains: &'a std::collections::HashMap<String, lazyagent_proto::types::DecisionExplain>,
    /// Approvals and questions are answered on the line (I4e).
    approvals: bool,
    questions: bool,
    /// The engine stops a closed item's agents (`close_stop`).
    close_stop: bool,
    /// The engine explains its lines (feature `rules`): Why?.
    why: bool,
    /// The worktrees with a restorable agent: "Restore agent and delegate".
    restorable: std::collections::HashSet<String>,
}

/// "Presence: auto (present)": the toggle, as one menu.
fn presence_menu(ui: &mut Ui, t: &Theme, presence: Option<&Presence>, acts: &mut Vec<Act>) {
    let mode = presence.and_then(|p| p.mode.as_deref()).unwrap_or("auto");
    let effective = presence.map(|p| p.effective.as_str()).unwrap_or("present");
    let text = if mode == "auto" { format!("Presence: auto ({effective})") } else { format!("Presence: {mode}") };
    ui.menu_button(RichText::new(text).small().color(t.text), |ui| {
        ui.set_min_width(240.0);
        ui.label(RichText::new("What the orchestrator may decide alone depends on it.").small().color(t.muted));
        for (m, what) in [
            ("auto", "auto: present while you use the app"),
            ("present", "present: it proposes, you decide"),
            ("away", "away: it may decide more alone"),
        ] {
            if ui.selectable_label(mode == m, RichText::new(what).small()).clicked() && mode != m {
                acts.push(Act::Presence(m));
                ui.close();
            }
        }
    });
}

/// The orchestrator on Home: what it says it does, what it holds, and its
/// actions today (a click, or `t`, lists them with Revert).
#[allow(clippy::too_many_arguments)]
fn orchestrator_strip(
    ui: &mut Ui,
    c: &LineCtx,
    presence: Option<&Presence>,
    counts: lazyagent_proto::types::StatusCounts,
    orch: Option<&lazyagent_proto::types::OrchestratorStatus>,
    actions: Option<&[lazyagent_proto::types::OrchestratorAction]>,
    open: bool,
    acts: &mut Vec<Act>,
) {
    let t = c.t;
    ui.add_space(6.0);
    egui::Frame::new().stroke(egui::Stroke::new(1.0, t.border)).corner_radius(4).inner_margin(8).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new("Orchestrator").strong().color(t.cyan));
            if orch.is_some_and(|o| !o.alive)
                && ui
                    .button(RichText::new("Start the orchestrator").small())
                    .on_hover_text("Spawn it in the main checkout")
                    .clicked()
            {
                acts.push(Act::StartOrchestrator);
            }
            let doing = match orch {
                Some(o) if !o.alive => "not running".to_string(),
                Some(o) => match &o.report {
                    Some(r) => format!("{} · {} ago", r.note, home::age(c.now - r.at)),
                    None => "running; it has said nothing yet".to_string(),
                },
                None => String::new(),
            };
            if !doing.is_empty() {
                ui.add(egui::Label::new(RichText::new(doing).color(t.text)).truncate());
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| presence_menu(ui, t, presence, acts));
        });
        if let Some(o) = orch.filter(|o| !o.queue.is_empty()) {
            let items: Vec<String> = o
                .queue
                .iter()
                .map(|q| {
                    let name = q.key.split_once(':').map(|x| x.1).unwrap_or(&q.key);
                    let what = match (q.kind.as_str(), q.note.as_deref()) {
                        ("pass", _) => "passed".to_string(),
                        (_, Some(n)) if !n.is_empty() => n.to_string(),
                        _ => "held".to_string(),
                    };
                    format!("{name} · {what} · {} left", left(q.until - c.now))
                })
                .collect();
            ui.add(
                egui::Label::new(RichText::new(format!("Queue: {}", items.join("  ·  "))).small().color(t.text))
                    .truncate(),
            );
        }
        if let Some(n) = counts.auto_actions {
            let reverted = counts.auto_reverted.unwrap_or(0);
            let arrow = if open { "▾" } else { "▸" };
            let text = format!("Today: {n} actions · {reverted} reverted {arrow}  (t)");
            if ui.add(egui::Button::new(RichText::new(text).small()).frame(false)).clicked() {
                acts.push(Act::StripToggle);
            }
        }
        if open {
            match actions {
                None => {
                    ui.label(RichText::new("…").small().color(t.muted));
                }
                Some([]) => {
                    ui.label(RichText::new("No actions today.").small().color(t.muted));
                }
                Some(list) => {
                    for a in list {
                        ui.horizontal(|ui| {
                            let name = a.key.split_once(':').map(|x| x.1).unwrap_or(&a.key);
                            let mut text = format!("{} ago  {name}  {}", home::age(c.now - a.at), a.text);
                            if let Some(r) = a.reason.as_deref().filter(|r| !r.is_empty()) {
                                text.push_str(&format!(" — {r}"));
                            }
                            ui.add(egui::Label::new(RichText::new(text).small().color(t.text)).truncate());
                            if a.revertible
                                && let Some(id) = &a.decision_id
                                && ui.small_button("Revert…").clicked()
                            {
                                acts.push(Act::RevertAction(id.clone()));
                            }
                        });
                    }
                }
            }
        }
    });
}

/// The ball cell of an In flight line: who has it (owner decision): the
/// orchestrator while it holds the item (a pass, a hold, a routed brief or
/// question), the agent when it works on it (directly, or handed by you or
/// the orchestrator).
fn ball_text(c: &LineCtx, l: &Line, shown: Zone) -> (String, String, Color32) {
    let t = c.t;
    let state = l.working().unwrap_or_else(|| humanize(&l.state));
    match (&l.claim, l.hold()) {
        (Some(claim), _) => ("orchestrator".into(), format!("looking · {} left", left(claim.until - c.now)), t.cyan),
        (None, Some(hold)) => {
            ("orchestrator".into(), format!("{} · {} left", hold.note, left(hold.until - c.now)), t.cyan)
        }
        _ if shown == Zone::Parked && l.parked_needs_you() => ("needs you".into(), state, t.yellow),
        _ if shown == Zone::Parked => ("parked".into(), state, t.muted),
        _ if l.looking() && c.first_responder => ("orchestrator".into(), "looking".into(), t.cyan),
        _ => (l.ball.clone(), state, ball_color(t, &l.ball)),
    }
}

/// `CI red · unrelated (base also red: 2 tests)` (feature ci_tests, R4):
/// an informational chip, not an item; the full text on hover.
/// The chip's words: the note, or when it is too long and an attestation
/// explains part of the red, that part first: `CI red · eval-suite attested
/// by agent a2 +1` (the full note on hover).
pub fn chip_text(note: &str) -> String {
    const MAX: usize = 48;
    if note.chars().count() <= MAX {
        return note.to_string();
    }
    let inner = note.split_once('(').map(|(_, r)| r.trim_end_matches(')')).unwrap_or(note);
    let parts: Vec<&str> = inner.split("; ").collect();
    match parts.iter().position(|p| p.contains(" attested by ")) {
        Some(i) => {
            let more = if parts.len() > 1 { format!(" +{}", parts.len() - 1) } else { String::new() };
            home::middle_ellipsis(&format!("CI red · {}{more}", parts[i]), MAX)
        }
        None => home::middle_ellipsis(note, MAX),
    }
}

fn ci_chip(ui: &mut Ui, t: &Theme, l: &Line) {
    let Some(note) = l.ci_note() else { return };
    let short = chip_text(note);
    let r = egui::Frame::new()
        .fill(t.panel)
        .stroke(egui::Stroke::new(1.0, t.muted))
        .corner_radius(egui::CornerRadius::same(8))
        .inner_margin(egui::Margin::symmetric(6, 0))
        .show(ui, |ui| ui.label(RichText::new(short).small().color(t.muted)))
        .response;
    r.on_hover_text(note);
}

fn line_ui(ui: &mut Ui, c: &LineCtx, l: &Line, shown: Zone, next: bool, forms: &mut Forms, acts: &mut Vec<Act>) {
    let t = c.t;
    let sel = l.sel();
    let selected = c.selected == Some(sel.as_str());
    let expanded = forms.expanded.as_deref() == Some(sel.as_str());
    let fill = if selected { t.select_bg } else { Color32::TRANSPARENT };
    let mut cut = false;
    let resp = egui::Frame::new().fill(fill).inner_margin(egui::Margin { left: 10, right: 8, top: 3, bottom: 3 }).show(
        ui,
        |ui| {
            ui.set_width(ui.available_width());
            if l.done() {
                ui.multiply_opacity(0.45);
            } else if next {
                // The next round, not this one yet.
                ui.multiply_opacity(0.55);
            }
            let head = ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                let name_w = (ui.available_width() * NAME_SHARE).clamp(NAME_MIN, NAME_MAX);
                if matches!(shown, Zone::Flight | Zone::Parked) {
                    let dot = if l.unseen(c.look) { "•" } else { " " };
                    cell(ui, 10.0, RichText::new(dot).strong().color(t.primary));
                    priority_cell(ui, t, &l.priority);
                    cut = name_cell(ui, name_w, &l.name);
                    let (ball, state, color) = ball_text(c, l, shown);
                    cell(ui, 110.0, RichText::new(ball).small().color(color));
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(RichText::new(home::age(c.now - l.changed_at)).small().color(t.muted));
                        ci_chip(ui, t, l);
                        ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                            ui.add(egui::Label::new(RichText::new(state).color(t.muted)).truncate());
                        });
                    });
                } else {
                    let color = match shown {
                        Zone::Now => t.yellow,
                        Zone::Deep => t.purple,
                        _ => t.accent,
                    };
                    let dot = if c.gestures && l.unseen(c.look) { "•" } else { " " };
                    let mark = if selected {
                        RichText::new("▸").color(t.primary)
                    } else {
                        RichText::new(dot).color(t.primary)
                    };
                    cell(ui, 10.0, mark);
                    cell(ui, 112.0, RichText::new(l.verb()).strong().color(color));
                    priority_cell(ui, t, &l.priority);
                    cut = name_cell(ui, name_w, &l.name);
                    // "approve: npm test" after "Approve": the fact alone.
                    let text = if l.looking() && c.first_responder {
                        "orchestrator · looking".to_string()
                    } else if let Some(top) = l.top.as_ref().filter(|t| t.kind == "decision") {
                        // A brief's question (or "agent a2 asks: …") reads better than its state.
                        top.label.clone()
                    } else if let Some(ci) = l.top.as_ref().and_then(|t| t.ci_text()) {
                        // The red workflow and its failed job (feature actions).
                        ci
                    } else {
                        humanize(l.state.split_once(": ").map(|(_, rest)| rest).unwrap_or(&l.state))
                    };
                    let age = c.now - l.top.as_ref().map(|t| t.since).unwrap_or(l.changed_at);
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(RichText::new(home::age(age)).small().color(t.muted));
                        ci_chip(ui, t, l);
                        // "+ changes requested": what is behind the top item.
                        if let Some(more) = l.more_text() {
                            let r = ui.add(
                                egui::Label::new(
                                    RichText::new(home::middle_ellipsis(&more, 40)).small().color(t.muted),
                                )
                                .sense(Sense::hover()),
                            );
                            if more.chars().count() > 40 {
                                r.on_hover_text(more);
                            }
                        }
                        if next {
                            ui.label(RichText::new("next round").small().color(t.muted));
                        }
                        ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                            ui.add(egui::Label::new(RichText::new(format!("· {text}"))).truncate());
                        });
                    });
                }
            });
            // A brief reads as one more line under its row (§5b wireframe).
            if let Some(d) = l.brief.as_ref().filter(|d| d.valid) {
                ui.horizontal(|ui| {
                    ui.add_space(140.0);
                    ui.add(egui::Label::new(RichText::new(brief_summary(d)).small().color(t.muted)).truncate());
                });
            }
            // What the orchestrator said when it released a pass or a hold.
            if let Some(note) = l.released_note() {
                ui.horizontal(|ui| {
                    ui.add_space(18.0);
                    ui.add(egui::Label::new(RichText::new(note).small().color(t.cyan)).truncate());
                });
            }
            // A held line: the last thing that really happened there.
            if (l.claim.is_some() || l.hold().is_some() || l.looking())
                && let Some(step) = l.row.as_ref().and_then(|r| r.last_step.as_ref())
            {
                ui.horizontal(|ui| {
                    ui.add_space(18.0);
                    let actor = if step.actor == "engine" { String::new() } else { format!(" · {}", step.actor) };
                    let text = format!("last step: {} ({} ago{actor})", step.text, home::age(c.now - step.at));
                    ui.add(egui::Label::new(RichText::new(text).small().color(t.muted)).truncate());
                });
            }
            if let Some(hint) = l.row.as_ref().and_then(|r| r.suggestion.as_deref()) {
                ui.horizontal(|ui| {
                    ui.add_space(18.0);
                    ui.add(
                        egui::Label::new(
                            RichText::new(format!("suggestion: {hint} (p opens the PR; closing it is yours)"))
                                .small()
                                .color(t.muted),
                        )
                        .truncate(),
                    );
                });
            }
            if c.first_responder
                && let Some(d) = &l.decided
            {
                ui.horizontal(|ui| {
                    ui.add_space(18.0);
                    let undone = if d.revertible == Some(true) { "" } else { " (final)" };
                    ui.add(
                        egui::Label::new(RichText::new(format!("{}{undone}", decided_text(d))).small().color(t.cyan))
                            .truncate(),
                    );
                });
            }
            if expanded {
                details(ui, c, l, cut, forms, acts);
            }
            head.response
        },
    );
    if selected {
        let r = resp.response.rect;
        ui.painter().rect_filled(egui::Rect::from_min_size(r.min, Vec2::new(3.0, r.height())), 0.0, t.primary);
    }
    if resp.inner.interact(Sense::click()).clicked() {
        acts.push(Act::Select(sel.clone()));
        acts.push(Act::Toggle(sel));
    }
}

/// The user's gesture, as words: "label QA (others), by you 5m ago".
fn gesture_text(g: &Value, now: f64) -> String {
    let age = home::age(now - g["at"].as_f64().unwrap_or(now));
    let by = match g["by"].as_str() {
        Some("user") | None => "you".to_string(),
        Some(other) => other.to_string(),
    };
    let what = match g["kind"].as_str() {
        Some("label") => {
            format!("labelled \"{}\" ({})", g["text"].as_str().unwrap_or(""), g["ball"].as_str().unwrap_or(""))
        }
        Some("park") => park_until_text(g, now),
        Some("close") => format!("closed as {}", g["how"].as_str().unwrap_or("done")),
        Some(k) => k.to_string(),
        None => String::new(),
    };
    format!("{what}, by {by} {age} ago")
}

/// A park, as words: "parked for 1h 40m (after the release)".
fn park_until_text(g: &Value, now: f64) -> String {
    // A feeder's Waiting lane parks the same way (`waiting: true`).
    let word = if g["waiting"] == true {
        format!("waiting ({})", g["source"].as_str().unwrap_or("feeder"))
    } else {
        "parked".to_string()
    };
    let mut text = match &g["until"] {
        Value::Number(n) => format!("{word} for {}", left(n.as_f64().unwrap_or(now) - now)),
        Value::String(e) if e == "indefinite" && g["waiting"] == true => word,
        Value::String(e) if e == "indefinite" => "parked indefinitely".into(),
        Value::String(e) if e == "change" => format!("{word} until something changes (CI, review, commit, agent)"),
        Value::String(e) => format!("{word} until {}", e.replace('_', " ")),
        _ => word,
    };
    if let Some(reason) = g["text"].as_str().filter(|r| !r.is_empty()) {
        text.push_str(&format!(" ({reason})"));
    }
    text
}

fn timeline_line(e: &TimelineEntry, now: f64) -> String {
    let actor = if e.actor == "engine" { String::new() } else { format!(" · {}", e.actor) };
    let text = if e.kind == "ball" { ball_words(&e.text) } else { e.text.clone() };
    let kind = match e.kind.as_str() {
        "ball" => "now",
        // "you approved abc1234" (R3d).
        "owner_approval" => "review",
        other => other,
    };
    let times = match e.count {
        Some(n) if n > 1 => format!(" ×{n}"),
        _ => String::new(),
    };
    format!("{} ago  {kind}  {text}{times}{actor}", home::age(now - e.at),)
}

/// A ball change in words: "stalled → agent: working" → "with the agent
/// (was stalled): working".
pub fn ball_words(text: &str) -> String {
    let Some((move_, state)) = text.split_once(": ") else { return text.to_string() };
    let Some((from, to)) = move_.split_once(" → ") else { return text.to_string() };
    let who = |b: &str| match b {
        "you" => "yours".to_string(),
        "agent" => "with the agent".to_string(),
        "others" => "with others".to_string(),
        "stalled" => "stalled".to_string(),
        "done" => "done".to_string(),
        other => other.to_string(),
    };
    format!("{} (was {}): {state}", who(to), who(from))
}

/// A ghost line in To sit down with (I4g): a deep item under a quicker
/// line, dimmed, its verb and state, and where its line is. A line of its
/// own: selected by keys or a click, Enter expands it on that item.
fn ghost_ui(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, acts: &mut Vec<Act>) {
    let t = c.t;
    let Some(ghost) = &l.ghost else { return };
    let sel = l.sel();
    let selected = c.selected == Some(sel.as_str());
    let expanded = forms.expanded.as_deref() == Some(sel.as_str());
    let fill = if selected { t.select_bg } else { Color32::TRANSPARENT };
    let mut cut = false;
    let r = egui::Frame::new().fill(fill).inner_margin(egui::Margin { left: 10, right: 8, top: 3, bottom: 3 }).show(
        ui,
        |ui| {
            ui.set_width(ui.available_width());
            let head = ui.scope(|ui| {
                if !selected && !expanded {
                    ui.multiply_opacity(0.7);
                }
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    let name_w = (ui.available_width() * NAME_SHARE).clamp(NAME_MIN, NAME_MAX);
                    let mark = if selected {
                        RichText::new("▸").color(t.primary)
                    } else {
                        RichText::new("↳").color(t.muted)
                    };
                    cell(ui, 10.0, mark);
                    cell(ui, 112.0, RichText::new(l.verb()).strong().color(t.purple));
                    priority_cell(ui, t, &l.priority);
                    cut = name_cell(ui, name_w, &l.name);
                    let where_ = if ghost.zone == Zone::Now { "Now" } else { "the Round" };
                    let text = match l.top.as_ref().filter(|t| t.kind == "decision") {
                        Some(top) => top.label.clone(),
                        None => l.state.clone(),
                    };
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(RichText::new(home::age(c.now - l.changed_at)).small().color(t.muted));
                        ui.label(RichText::new(format!("under its {} in {where_}", ghost.verb)).small().color(t.muted));
                        ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                            ui.add(egui::Label::new(RichText::new(format!("· {text}"))).truncate());
                        });
                    });
                })
            });
            if expanded {
                details(ui, c, l, cut, forms, acts);
            }
            head.response
        },
    );
    if selected {
        let rect = r.response.rect;
        ui.painter().rect_filled(egui::Rect::from_min_size(rect.min, Vec2::new(3.0, rect.height())), 0.0, t.primary);
    }
    // The head row as a whole takes the click (its labels only show).
    let click = ui.interact(r.inner.rect, ui.scope_id().with(("home-ghost", &sel)), Sense::click());
    if click.on_hover_text("Its item, on its own line: Enter or a click expands it").clicked() {
        acts.push(Act::Select(sel.clone()));
        acts.push(Act::Toggle(sel));
    }
}

/// A brief Home answers itself: one with options (1-4), or the engine's
/// own (auto-delegation failed), answered in words.
pub fn answerable_here(d: &Decision) -> bool {
    (d.valid && !d.brief.options.is_empty()) || engine_brief(d)
}

fn engine_brief(d: &Decision) -> bool {
    d.source.as_deref() == Some("auto_delegate")
}

/// What answering the engine's brief does: "sends this to agent a5".
pub fn engine_answer_text(d: &Decision) -> String {
    if d.agent.is_empty() {
        "sends this to the branch's agent".into()
    } else {
        format!("sends this to agent {}", d.agent)
    }
}

/// The brief: its options (1-4 pick one when it can be answered here,
/// Ctrl+Enter answers), what was tried, reversibility, evidence.
fn brief_ui(ui: &mut Ui, t: &Theme, d: &Decision, view: bool, picked: Option<&str>, acts: &mut Vec<Act>, l: &Line) {
    if !d.valid && !engine_brief(d) {
        ui.label(RichText::new(format!("agent {} asks: {}", d.agent, d.brief.question)).color(t.text));
        ui.label(RichText::new("Answer it in the agent's terminal (o).").small().color(t.muted));
        return;
    }
    ui.label(RichText::new(&d.brief.question).strong().color(t.text));
    let rec = d.brief.recommendation.as_deref().unwrap_or("");
    let answerable = view && d.state == "open" && answerable_here(d);
    let words = d.brief.options.is_empty();
    let chosen = picked.map(str::to_string).or_else(|| crate::decision_view::preselected(d));
    let mut send = false;
    if answerable && words {
        // The engine's brief: what the agent should do now, in words.
        let mut text = picked.unwrap_or("").to_string();
        let edit = ui.add(
            egui::TextEdit::singleline(&mut text)
                .hint_text("your answer (what to do now); it reaches the agent once its turn is over")
                .desired_width(f32::INFINITY),
        );
        if edit.changed() {
            acts.push(Act::BriefChoose(d.id.clone(), text.clone()));
        }
        send = (edit.lost_focus() || edit.has_focus()) && ui.input(|i| i.key_pressed(Key::Enter));
    }
    for (i, o) in d.brief.options.iter().enumerate() {
        let mut text = format!("{}  {}", o.key, o.label);
        if let Some(c) = o.consequence.as_deref().filter(|c| !c.is_empty()) {
            text.push_str(&format!(" — {c}"));
        }
        let recommended = o.key.eq_ignore_ascii_case(rec);
        if recommended {
            text.push_str("   (recommended)");
        }
        let color = if recommended { t.cyan } else { t.text };
        if answerable {
            let on = chosen.as_deref().is_some_and(|c| c.eq_ignore_ascii_case(&o.key));
            ui.horizontal(|ui| {
                ui.label(RichText::new(format!("{}", i + 1)).small().monospace().color(t.muted));
                if ui.selectable_label(on, RichText::new(text).color(color)).clicked() {
                    acts.push(Act::BriefChoose(d.id.clone(), o.key.clone()));
                }
            });
        } else {
            ui.label(RichText::new(text).color(color));
        }
    }
    if !d.brief.tried.is_empty() {
        ui.label(RichText::new(format!("Tried: {}", d.brief.tried)).small().color(t.muted));
    }
    let chosen = chosen.filter(|c| !c.trim().is_empty());
    // The engine's brief has no reversibility to give: what was tried is all.
    if !engine_brief(d) {
        let mut facts = vec![format!("Reversible: {}", d.brief.reversible.as_deref().unwrap_or("not given"))];
        if !d.brief.impact_if_wrong.is_empty() {
            facts.push(format!("If wrong: {}", d.brief.impact_if_wrong));
        }
        ui.label(RichText::new(facts.join(" · ")).small().color(t.muted));
    }
    if !d.brief.evidence.is_empty() {
        let ev: Vec<String> = d.brief.evidence.iter().map(evidence_text).collect();
        ui.label(RichText::new(format!("Evidence: {}", ev.join(" · "))).small().color(t.muted));
    }
    if answerable {
        ui.horizontal(|ui| {
            let what = match chosen.as_deref() {
                Some(c) if !words => format!("Answer {c}  (Ctrl+Enter)"),
                _ => "Answer  (Ctrl+Enter)".into(),
            };
            if engine_brief(d) {
                // Written by the engine, not the agent: the answer goes to it.
                ui.label(RichText::new(engine_answer_text(d)).small().color(t.cyan));
            }
            if (ui.add_enabled(chosen.is_some(), egui::Button::new(what)).clicked() || send) && chosen.is_some() {
                acts.push(Act::BriefAnswer(d.clone()));
            }
            let open = if l.ghost.is_some() || l.top.as_ref().is_none_or(|t| t.decision_id.as_deref() != Some(&d.id)) {
                "v opens the brief with its evidence"
            } else {
                "Enter or v opens the brief with its evidence"
            };
            let pick = if words { "" } else { "1-4 pick · " };
            ui.label(RichText::new(format!("{pick}{open}")).small().color(t.muted));
        });
    } else {
        let how =
            if view { "Open it to answer (Enter or v)." } else { "Answer it in the agent's terminal (o) for now." };
        ui.label(RichText::new(how).small().color(t.muted));
    }
}

/// Send anyway is offered once a letter waited this long (seconds).
pub const SEND_ANYWAY_AFTER: f64 = 60.0;

/// "waiting for a1 to be free (possibly hanged) · 2m": a delegation whose
/// letter has not reached the agent yet.
pub fn hold_text(d: &Delegation, now: f64) -> Option<String> {
    if d.held.is_empty() || d.phase != "on_it" {
        return None;
    }
    Some(match d.held_since {
        Some(at) => format!("{} · {}", d.held, home::age(now - at)),
        None => d.held.clone(),
    })
}

/// Send anyway applies: the letter has waited [`SEND_ANYWAY_AFTER`].
pub fn send_anyway(d: &Delegation, now: f64) -> bool {
    hold_text(d, now).is_some() && d.held_since.is_some_and(|at| now - at >= SEND_ANYWAY_AFTER)
}

/// A button under an expanded line, and where it goes (see [`bar`]).
struct Button {
    text: String,
    hover: Option<&'static str>,
    enabled: bool,
    /// Always under "More…", never in the bar (Why?, Notes).
    more: bool,
    act: Act,
}

fn button(text: impl Into<String>, act: Act) -> Button {
    Button { text: text.into(), hover: None, enabled: true, more: false, act }
}

/// The bar under an expanded line and "More…": the bar keeps the first
/// [`PRIMARY_BUTTONS`] primary verbs, in order; the rest, and every button
/// marked `more`, go under "More…" (the owner's rule: a short bar).
fn bar<T>(buttons: Vec<T>, more: impl Fn(&T) -> bool) -> (Vec<T>, Vec<T>) {
    let (mut shown, mut rest): (Vec<T>, Vec<T>) = (vec![], vec![]);
    let mut forced = vec![];
    for b in buttons {
        if more(&b) {
            forced.push(b);
        } else if shown.len() < PRIMARY_BUTTONS {
            shown.push(b);
        } else {
            rest.push(b);
        }
    }
    rest.extend(forced);
    (shown, rest)
}

/// The texts of [`bar`]'s two halves, for tests.
#[cfg(test)]
pub fn bar_texts(buttons: &[(&str, bool)]) -> (Vec<String>, Vec<String>) {
    let (shown, rest) = bar(buttons.to_vec(), |b| b.1);
    let texts = |v: Vec<(&str, bool)>| v.into_iter().map(|b| b.0.to_string()).collect();
    (texts(shown), texts(rest))
}

/// The confirmation of ⇧X: exactly what closing does, and what it never
/// touches.
pub fn close_text(agents: &[String], stops: bool) -> String {
    let stop = match (agents, stops) {
        ([], _) => String::new(),
        (_, false) => " Its agents keep running (this engine does not stop them).".into(),
        ([a], true) => format!(" Agent {a} is stopped; its notes are kept."),
        (many, true) => format!(" Agents {} are stopped; their notes are kept.", many.join(", ")),
    };
    format!(
        "Close this work item: it leaves Home after a day.{stop} The worktree, the branch and the PR are not touched."
    )
}

/// `71 %`: a run's pass rate, as the engine rounds it.
fn rate(run: &Value) -> Option<i64> {
    let (passed, total) = (run["passed"].as_f64()?, run["total"].as_f64()?);
    (total > 0.0).then(|| (100.0 * passed / total).round() as i64)
}

/// `71 %, 75 %, 78 %` with each run's id (or `local`) on hover.
pub fn runs_text(runs: &[Value]) -> (String, String) {
    let shown: Vec<String> = runs
        .iter()
        .filter_map(|r| {
            let local = r["run_id"].is_null();
            rate(r).map(|p| if local { format!("{p} % (local)") } else { format!("{p} %") })
        })
        .collect();
    let hover: Vec<String> = runs
        .iter()
        .map(|r| match r["run_id"].as_i64() {
            Some(id) => format!(
                "run {id}{}",
                r["sha"].as_str().map(|s| format!(" at {}", &s[..s.len().min(7)])).unwrap_or_default()
            ),
            None => format!("{} (the agent's claim)", r["where"].as_str().unwrap_or("local")),
        })
        .collect();
    (shown.join(", "), hover.join("\n"))
}

/// The evidence lines of an attestation: what the agent read on the base,
/// what it ran on the head, and the engine's recomputed comparison.
pub fn evidence_rows(a: &Attestation) -> Vec<(&'static str, String, String)> {
    let ev = &a.evidence;
    let empty = vec![];
    let base = ev["base_runs"].as_array().unwrap_or(&empty);
    let own = ev["own_runs"].as_array().unwrap_or(&empty);
    let rng = &ev["range"];
    let pct = |k: &str| rng[k].as_f64().map(|x| (100.0 * x).round() as i64);
    let span = |lo: Option<i64>, hi: Option<i64>| match (lo, hi) {
        (Some(l), Some(h)) if l == h => format!("{l} %"),
        (Some(l), Some(h)) => format!("{l}-{h} %"),
        _ => "?".into(),
    };
    let (base_text, base_hover) = runs_text(base);
    let (own_text, own_hover) = runs_text(own);
    let sha = &a.sha[..a.sha.len().min(7)];
    let mut rows = vec![
        (
            "base",
            format!("{} runs: {base_text} · range {}", base.len(), span(pct("base_min"), pct("base_max"))),
            base_hover,
        ),
        (
            "branch",
            format!("{} runs at {sha}: {own_text} · range {}", own.len(), span(pct("own_min"), pct("own_max"))),
            own_hover,
        ),
    ];
    if let (Some(drop), Some(max)) = (pct("drop"), pct("max_drop")) {
        let within = if rng["within"].as_bool() == Some(true) { "within" } else { "outside" };
        rows.push(("drop", format!("{drop} points vs max_drop {max}: {within} the base's range"), String::new()));
    }
    if let Some(m) = ev["method"].as_str().filter(|m| *m != "range") {
        rows.push(("method", format!("{m}: the engine could not check it; read the reason"), String::new()));
    }
    rows
}

/// A Confirm item's evidence, and Accept / Reject (a comment for the agent).
fn attestation_ui(ui: &mut Ui, t: &Theme, a: &Attestation, forms: &mut Forms, focus: bool, acts: &mut Vec<Act>) {
    egui::Frame::new().fill(t.panel).corner_radius(4).inner_margin(8).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.label(
            RichText::new(format!("{} says {}'s failure is not the branch's", a.actor_text, a.name))
                .strong()
                .color(t.text),
        );
        if let Some(reason) = a.evidence["reason"].as_str().filter(|r| !r.is_empty()) {
            // The agent's words: shown as data.
            ui.add(egui::Label::new(RichText::new(format!("“{reason}”")).italics().color(t.text)).wrap());
        }
        egui::Grid::new(("attestation", &a.id)).num_columns(2).spacing([12.0, 3.0]).show(ui, |ui| {
            for (what, text, hover) in evidence_rows(a) {
                ui.label(RichText::new(what).small().color(t.muted));
                let colour = match what {
                    "drop" if text.contains("within") => t.green,
                    "drop" | "method" => t.yellow,
                    _ => t.text,
                };
                let r = ui.label(RichText::new(text).small().monospace().color(colour));
                if !hover.is_empty() {
                    r.on_hover_text(hover);
                }
                ui.end_row();
            }
        });
        match forms.rejecting.as_mut().filter(|r| r.0 == a.id) {
            Some((id, comment)) => {
                ui.horizontal(|ui| {
                    let edit = ui.add(
                        egui::TextEdit::singleline(comment)
                            .hint_text("why: the agent gets this comment")
                            .desired_width((ui.available_width() - 180.0).max(200.0)),
                    );
                    if focus {
                        edit.request_focus();
                    }
                    let enter = (edit.lost_focus() || edit.has_focus()) && ui.input(|i| i.key_pressed(Key::Enter));
                    let ok = !comment.trim().is_empty();
                    if (ui.add_enabled(ok, egui::Button::new(RichText::new("Reject").color(t.red))).clicked() || enter)
                        && ok
                    {
                        acts.push(Act::Reject(id.clone(), comment.clone()));
                    }
                    if ui.button("Cancel").clicked() {
                        acts.push(Act::RejectCancel);
                    }
                });
            }
            None => {
                ui.horizontal(|ui| {
                    if ui
                        .button(RichText::new("Accept").color(t.green))
                        .on_hover_text("The check no longer counts against the branch, until a new commit")
                        .clicked()
                    {
                        acts.push(Act::AttestAccept(a.id.clone()));
                    }
                    if ui
                        .button(RichText::new("Reject…").color(t.red))
                        .on_hover_text("Your comment goes to the agent; Fix CI comes back")
                        .clicked()
                    {
                        acts.push(Act::RejectOpen(a.id.clone()));
                    }
                });
            }
        }
    });
}

/// The confirmation of ⇧U: what reverting the orchestrator's decision does.
pub fn revert_text(d: &Decision) -> String {
    let choice = d.choice().unwrap_or("?");
    let mut text = format!("Revert the orchestrator's {choice}: agent {} is told your override", d.agent);
    let inverse = d.inverse.as_ref().and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let stops = inverse.iter().filter(|i| i["kind"] == "stop_agent").count();
    if stops > 0 {
        text.push_str(&format!(", the {stops} agent(s) it spawned for this are stopped"));
    }
    if let Some(sha) = inverse.iter().find(|i| i["kind"] == "git_reset").and_then(|i| i["sha"].as_str()) {
        let short: String = sha.chars().take(7).collect();
        text.push_str(&format!(", and the branch is reset to {short} if nothing was pushed since"));
    }
    text.push('.');
    text
}

/// The expanded line: the ask in full, who is on it, the agent's note, and
/// the actions.
/// The heading of what to review (I4j): "3 commits since your approval
/// (1a2b3c4)", "since the PR was opened: 3 commits".
pub fn review_heading(rc: &ReviewCommits) -> String {
    let n = format!("{} commit{}", rc.count, if rc.count == 1 { "" } else { "s" });
    let from = &rc.from[..rc.from.len().min(7)];
    let merges = match rc.base_merges {
        0 => String::new(),
        1 => " · 1 base merge not counted".into(),
        m => format!(" · {m} base merges not counted"),
    };
    match rc.since.as_str() {
        "approval" => format!("To review: {n} since your approval ({from}){merges}"),
        "ready" => format!("To review: {n} since it was marked ready ({from}){merges}"),
        _ => format!("To review: since the PR was opened: {n}{merges}"),
    }
}

/// One commit of the list: `1a2b3c4  subject · author · 2026-10-27`; a
/// merge of the base reads "base merge, not counted".
pub fn review_commit_text(c: &ReviewCommit) -> String {
    let sha = &c.sha[..c.sha.len().min(7)];
    let date = c.date.get(..10).unwrap_or(&c.date);
    match &c.base_merge {
        Some(_) => format!("{sha}  {} · base merge, not counted", c.subject),
        None => format!("{sha}  {} · {} · {date}", c.subject, c.author),
    }
}

fn review_commits_ui(ui: &mut Ui, t: &Theme, rc: &ReviewCommits, acts: &mut Vec<Act>) {
    ui.label(RichText::new(review_heading(rc)).small().strong().color(t.muted));
    for c in &rc.commits {
        let color = if c.base_merge.is_some() { t.muted } else { t.text };
        let r =
            ui.add(egui::Label::new(RichText::new(review_commit_text(c)).small().monospace().color(color)).truncate());
        if let Some(why) = &c.base_merge {
            r.on_hover_text(why);
        }
    }
    if rc.more > 0 {
        ui.label(RichText::new(format!("and {} more", rc.more)).small().color(t.muted));
    }
    if let Some(url) = &rc.compare_url
        && ui.link(RichText::new("Compare on GitHub").small()).on_hover_text(url).clicked()
    {
        acts.push(Act::Pr(url.clone()));
    }
}

fn details(ui: &mut Ui, c: &LineCtx, l: &Line, cut: bool, forms: &mut Forms, acts: &mut Vec<Act>) {
    let t = c.t;
    let focus = std::mem::take(&mut forms.focus);
    ui.add_space(2.0);
    ui.indent(("home-details", l.sel()), |ui| {
        // The full name, only when the line cut it.
        if cut {
            ui.label(RichText::new(&l.name).small().strong().color(t.text));
        }
        let is_brief = l.top.as_ref().is_some_and(|t| t.kind == "decision");
        let kind = l.top.as_ref().map(|t| t.kind.as_str()).unwrap_or("");
        match (&l.top, &l.brief) {
            (Some(_), Some(d)) if is_brief => {
                let picked = forms.brief_choice.as_ref().filter(|(id, _)| *id == d.id).map(|(_, c)| c.as_str());
                brief_ui(ui, t, d, c.decision_view || c.first_responder, picked, acts, l);
            }
            (Some(top), _) => {
                // What it asks, in full and readable: the command, the question.
                if let Some(ask) = l.ask_text() {
                    let text = if kind == "approval" { RichText::new(ask).monospace() } else { RichText::new(ask) };
                    egui::Frame::new().fill(t.panel).corner_radius(4).inner_margin(6).show(ui, |ui| {
                        ui.add(egui::Label::new(text.color(t.text)).wrap());
                    });
                    ui.label(RichText::new(&top.label).small().color(t.muted));
                } else {
                    // A Confirm item's evidence has its own block below.
                    let detail = if top.attestation.is_some() { "" } else { top.detail.as_str() };
                    let text = [top.label.as_str(), detail]
                        .iter()
                        .filter(|x| !x.is_empty())
                        .map(|x| humanize(x))
                        .collect::<Vec<_>>();
                    ui.label(RichText::new(text.join(" · ")).small().color(t.muted));
                }
            }
            _ => {}
        }
        if let Some(a) = l.top.as_ref().filter(|t| t.kind == "attestation_confirm").and_then(|t| t.attestation.as_ref())
        {
            attestation_ui(ui, t, a, forms, focus, acts);
        }
        // Every open item, and which one leads the line. A brief behind
        // the top one is answered here too ("Answer here"): never a dead end.
        let sel = l.sel();
        let mine = l.ghost.as_ref().map(|g| g.item.as_str());
        if l.items().len() > 1 {
            ui.label(RichText::new("Its items").small().strong().color(t.muted));
            for (i, item) in l.items().iter().enumerate() {
                let mut text = format!("{}. {} · {}", i + 1, home::kind_verb(&item.kind), home::item_words(item));
                if mine == Some(item.id.as_str()) {
                    text.push_str(" · this line");
                } else if i == 0 {
                    text.push_str(" · leads the line");
                } else if item.level == "deep" && mine.is_none() {
                    text.push_str(" · also in To sit down with");
                }
                text.push_str(&format!(" · {}", home::age(c.now - item.since)));
                let open = forms.item_open.as_ref().is_some_and(|(s, id)| *s == sel && *id == item.id);
                let brief = (i > 0 && item.kind == "decision" && mine.is_none()).then(|| l.under_item(&item.id)).flatten();
                let alone = c.delegate_all
                    && l.delegates().is_some_and(|d| d.len() > 1 && d.contains(&item.id))
                    && l.row.as_ref().is_some_and(|r| !r.agents.is_empty());
                ui.horizontal(|ui| {
                    ui.label(RichText::new(text).small().color(if i == 0 { t.text } else { t.muted }));
                    if alone
                        && ui.small_button("delegate this one").on_hover_text("Only this item, to the branch's agent").clicked()
                    {
                        acts.push(Act::DelegateItem(l.clone(), item.id.clone()));
                    }
                    if brief.is_some() {
                        let what = if open { "hide" } else { "Answer here" };
                        if ui.small_button(what).clicked() {
                            acts.push(Act::ItemOpen(sel.clone(), item.id.clone()));
                        }
                    }
                });
                if open && let Some(u) = brief {
                    ui.indent(("home-item", &item.id), |ui| match &u.brief {
                        Some(d) => {
                            let picked = forms.brief_choice.as_ref().filter(|(id, _)| *id == d.id).map(|(_, c)| c.as_str());
                            brief_ui(ui, t, d, c.decision_view || c.first_responder, picked, acts, l);
                        }
                        None => {
                            ui.label(RichText::new("…").small().color(t.muted));
                        }
                    });
                }
            }
        }
        if let Some(rc) = l.review_commits() {
            review_commits_ui(ui, t, rc, acts);
        }
        if kind == "approval" && c.approvals {
            ui.horizontal(|ui| {
                if ui.button(RichText::new("Approve  (a)").color(t.green)).clicked() {
                    acts.push(Act::Approve(l.clone(), true));
                }
                if ui
                    .button(RichText::new("Deny  (⇧D)").color(t.red))
                    .on_hover_text("Sent after 5 s, unless you undo it")
                    .clicked()
                {
                    acts.push(Act::Approve(l.clone(), false));
                }
                ui.label(
                    RichText::new("sent to the agent's prompt, only while it is on its screen").small().color(t.muted),
                );
            });
        }
        if kind == "question" && c.questions {
            if forms.answering.as_ref().is_none_or(|(k, _)| *k != l.key) {
                forms.answering = Some((l.key.clone(), String::new()));
            }
            let text = &mut forms.answering.as_mut().expect("just set").1;
            ui.horizontal(|ui| {
                let edit = ui.add(
                    egui::TextEdit::singleline(text)
                        .hint_text("your answer; it reaches the agent once its turn is over")
                        .desired_width((ui.available_width() - 160.0).max(200.0)),
                );
                if focus {
                    edit.request_focus();
                }
                let enter = (edit.lost_focus() || edit.has_focus()) && ui.input(|i| i.key_pressed(Key::Enter));
                if (ui.add_enabled(!text.trim().is_empty(), egui::Button::new("Send  (Enter)")).clicked() || enter)
                    && !text.trim().is_empty()
                {
                    acts.push(Act::Answer(l.clone(), text.clone()));
                }
                if edit.has_focus() && ui.input(|i| i.key_pressed(Key::Escape)) {
                    acts.push(Act::AnswerCancel);
                }
            });
        }
        if let Some(row) = &l.row {
            for a in &row.agents {
                ui.label(
                    RichText::new(format!(
                        "agent {}  {} {}",
                        a.id,
                        humanize(store::status_text(&a.status)),
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
            if let Some(rr) = &row.review_request {
                // The item line names the PR and who asked: when, and through
                // which team, is what it does not say.
                let mut text = format!("review requested {} ago", home::age(c.now - rr.requested_at));
                if let Some(team) = &rr.team {
                    text.push_str(&format!(" · through team {team}"));
                }
                text.push_str(" · it goes once you review it on the PR");
                ui.label(RichText::new(text).small().color(t.muted));
            }
            if let Some(p) = &row.project {
                let mut text = format!("project {}", p.title);
                for w in &row.waits_on {
                    let review = if w.state == "review" { " (in review)" } else { "" };
                    text.push_str(&format!(" · waits on {}{review}", w.title));
                }
                ui.label(RichText::new(text).small().color(t.muted));
            } else if !row.waits_on.is_empty() {
                let titles: Vec<&str> = row.waits_on.iter().map(|w| w.title.as_str()).collect();
                ui.label(RichText::new(format!("waits on {}", titles.join(", "))).small().color(t.muted));
            }
            if let Some(f) = &row.feeder {
                let mut text = format!("from {}", f["source"].as_str().unwrap_or("a feeder"));
                if let Some(p) = f["priority"].as_str().filter(|p| *p != "medium") {
                    text.push_str(&format!(" · {p} priority"));
                }
                if let Some(url) = f["url"].as_str() {
                    text.push_str(&format!(" · {url}"));
                }
                ui.label(RichText::new(text).small().color(t.muted));
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
            if let Some(g) = &row.gesture {
                ui.label(RichText::new(gesture_text(g, c.now)).small().color(t.purple));
            }
            // The skip list: why no agent was sent ([inbox] auto_delegate_skip_checks).
            if let Some(text) = &row.auto_delegate {
                ui.label(RichText::new(text).small().color(t.yellow));
            }
        }
        let target = match l.top.as_ref().and_then(|t| t.decision_id.clone()) {
            Some(id) => id,
            None => format!("key:{}", l.key),
        };
        if c.decision_view
            && let Some(e) = c.explains.get(&format!("explain:{target}"))
        {
            match &e.text {
                Some(text) if e.state == "answered" => {
                    ui.label(RichText::new(crate::decision_view::explained_by(e)).small().strong().color(t.cyan));
                    ui.label(RichText::new(text).small().color(t.text));
                }
                _ => {
                    ui.label(RichText::new(format!("explain: asked {}", e.to.replace(':', " "))).small().color(t.muted));
                }
            }
        }
        if let Some(hold) = l.hold() {
            let who = if hold.auto { "held for the orchestrator (its worker's work)" } else { "the orchestrator holds it" };
            ui.label(
                RichText::new(format!("{who}: {} · {} more", hold.note, left(hold.until - c.now)))
                    .small()
                    .color(t.cyan),
            );
        }
        if let Some(d) = l.row.as_ref().and_then(|r| r.delegation.as_ref()) {
            let text = match d.phase.as_str() {
                "on_it" => format!("delegated to agent {} {} ago: {}", d.agent, home::age(c.now - d.at), d.fact),
                _ => d.state.clone(),
            };
            ui.label(RichText::new(text).small().color(t.cyan));
            if let Some(held) = hold_text(d, c.now) {
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new(held).small().color(t.yellow));
                    if send_anyway(d, c.now) {
                        if ui
                            .small_button("Send anyway")
                            .on_hover_text("Type it into the agent's terminal now, whatever it is doing")
                            .clicked()
                        {
                            acts.push(Act::SendAnyway(l.clone()));
                        }
                        ui.label(RichText::new("or open its terminal (o)").small().color(t.muted));
                    }
                });
            }
        }
        if l.zone == Zone::Parked
            && let Some(g) = l.gesture()
        {
            let mut text = park_until_text(g, c.now);
            if l.parked_needs_you() {
                text.push_str(" · needs you: its items are not routed to the orchestrator");
            }
            if g["waiting"] == true {
                let source = g["source"].as_str().unwrap_or("the feeder");
                text.push_str(&format!(" · held in {source}'s Waiting lane; u unparks it until {source} changes it"));
            }
            ui.label(RichText::new(text).small().color(if l.parked_needs_you() { t.yellow } else { t.muted }));
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
        if c.first_responder && l.looking() && l.claim.is_none() && l.hold().is_none() {
            ui.label(RichText::new("The orchestrator is looking at this first.").small().color(t.cyan));
        }
        if c.first_responder
            && let Some(d) = &l.decided
        {
            ui.label(RichText::new(format!("{}: {}", d.brief.question, decided_text(d))).small().color(t.cyan));
            if d.revertible == Some(true) {
                match forms.reverting.as_mut().filter(|r| r.0 == d.id) {
                    Some((id, comment)) => {
                        // A real confirmation: what reverting does.
                        ui.label(RichText::new(revert_text(d)).small().color(t.text));
                        ui.horizontal(|ui| {
                            let edit = ui.add(
                                egui::TextEdit::singleline(comment)
                                    .hint_text("comment for the agent (optional)")
                                    .desired_width(320.0),
                            );
                            if focus {
                                edit.request_focus();
                            }
                            let enter =
                                (edit.lost_focus() || edit.has_focus()) && ui.input(|i| i.key_pressed(Key::Enter));
                            if ui.button("Revert").clicked() || enter {
                                acts.push(Act::Revert(id.clone(), comment.clone()));
                            }
                            if ui.button("Cancel").clicked() || ui.input(|i| i.key_pressed(Key::Escape)) {
                                acts.push(Act::RevertCancel);
                            }
                        });
                    }
                    None => {
                        if ui.button("Revert…  (⇧U)").on_hover_text("Tell the agent your choice instead").clicked() {
                            acts.push(Act::RevertOpen(l.clone()));
                        }
                    }
                }
            }
        }
        if c.actions
            && let Some(top) = l.top.as_ref().filter(|t| t.is_ci())
        {
            ui.add_space(2.0);
            ui.label(RichText::new("Failed log").small().strong().color(t.muted));
            // With no Actions run the engine says whose red it is (0, 0).
            let wanted = top.run.as_ref().map(|r| (r.id, r.attempt)).unwrap_or((0, 0));
            match c.ci_logs.get(&l.key) {
                Some((r, Some(summary))) if *r == wanted => ci_log(ui, t, summary, acts),
                Some((r, None)) if *r == wanted => {
                    ui.label(RichText::new("…").small().color(t.muted));
                }
                // Not read yet, or for an older run: ask (once per run).
                _ => acts.push(Act::CiLog(l.key.clone(), wanted)),
            }
        }
        if c.gestures && l.row.is_some() {
            ui.add_space(2.0);
            ui.label(RichText::new("Since you looked").small().strong().color(t.muted));
            match c.timelines.get(&l.key) {
                None => {
                    ui.label(RichText::new("…").small().color(t.muted));
                }
                Some(tl) if tl.entries.is_empty() => {
                    ui.label(RichText::new("nothing new").small().color(t.muted));
                }
                Some(tl) => {
                    for e in tl.entries.iter().rev().take(12) {
                        ui.label(RichText::new(timeline_line(e, c.now)).small().color(t.text));
                    }
                }
            }
        }
        // The actions: the first few as buttons, the rest under "More…".
        let mut buttons: Vec<Button> = vec![];
        if l.verb() == "Merge"
            && let Some(url) = &l.url
        {
            // Merging is done on the PR, by you: its button comes first.
            let mut b = button("Open PR to merge  (p)", Act::Pr(url.clone()));
            b.hover = Some("Merging happens on the PR page");
            buttons.push(b);
        }
        let reviewing = l.top.as_ref().is_some_and(|t| t.kind == "review_request");
        if reviewing && let Some(url) = &l.url {
            // A teammate's PR: the review is done on the PR page.
            let mut b = button("Open PR to review  (p)", Act::Pr(url.clone()));
            b.hover = Some("Approve, request changes or comment there: the line goes once you do");
            buttons.push(b);
        }
        if l.verb() == "Review" && l.approvable().is_some_and(|a| !a.approved) {
            // The user's own review, done outside GitHub (R3d): a button only
            // where Review is the verb (it replaces nothing there); ⇧A
            // works on every approvable line.
            let mut b = button("Approve  (⇧A)", Act::OwnerApprove(l.clone()));
            b.hover = Some("You reviewed this head: only commits after it bring the review back");
            buttons.push(b);
        }
        if l.place.is_some() {
            buttons.push(button("Open terminal  (o)", Act::Open(l.clone())));
        }
        let ci = l.top.as_ref().is_some_and(|t| t.is_ci());
        // The engine's catalog: only what the branch's agent can resolve (I4h).
        let delegable_kind = l.delegable();
        let live = match &l.row {
            Some(r) => !r.agents.is_empty(),
            // The client's own queue: its CI line sends the failures to
            // the worktree's agent, and says so when there is none.
            None => true,
        };
        if c.delegation && delegable_kind && l.row.is_some() && !l.done() {
            // Only an agent that runs can be delegated to; else restore it,
            // or start one, first.
            let restorable = l.place.as_ref().is_some_and(|p| c.restorable.contains(p));
            let mut b = match (live, restorable) {
                (true, _) => button(
                    format!("{}  (d)", l.delegate_label()),
                    if ci { Act::SendCi(l.clone()) } else { Act::Delegate(l.clone()) },
                ),
                (false, true) => button("Restore agent and delegate", Act::RestoreDelegate(l.clone())),
                (false, false) => button("Spawn an agent for this", Act::SpawnDelegate(l.clone())),
            };
            b.hover = Some(if live {
                "It comes back if the fact still holds"
            } else {
                "No agent runs here: it starts one, then hands it the item once its first turn ends"
            });
            buttons.push(b);
        } else if ci && !c.delegation && live && delegable_kind {
            // Older engines: the failures go to the agent as a prompt.
            let mut b = button("Delegate to its agent  (d)", Act::SendCi(l.clone()));
            b.hover = Some("It comes back if the fact still holds");
            buttons.push(b);
        }
        let row_gestures = c.gestures && l.row.is_some() && !l.done();
        if l.top.is_some() {
            let mut b = button("Dismiss  (x)", Act::Dismiss(l.clone()));
            b.hover = Some("Hide until it happens again, for every client");
            buttons.push(b);
        }
        if l.top.is_some() || row_gestures {
            buttons.push(button("Park until…  (z)", Act::ParkMenu(l.sel())));
        }
        if l.claim.is_some() || l.hold().is_some() {
            buttons.push(button("Take back  (u)", Act::TakeBack(l.clone())));
        } else if ((l.row.is_some() && !l.done()) || !c.engine) && l.passable() {
            // Not where only the user may act: approve, answer, review, merge (I4h).
            let mut b = button("Pass to orchestrator  (⇧O)", Act::Pass(l.clone()));
            b.enabled = c.pass.is_ok() && l.row.is_some();
            buttons.push(b);
        }
        let unpark = if l.zone == Zone::Parked { "Unpark  (u)" } else { "Clear  (u)" };
        if c.gestures && l.claim.is_none() && l.gesture().is_some() {
            buttons.push(button(unpark, Act::TakeBack(l.clone())));
        }
        if l.place.is_some() {
            let mut b = button("Notes  (n)", Act::Notes(l.clone()));
            b.more = true;
            buttons.push(b);
        }
        if let Some(url) = &l.url
            && l.verb() != "Merge"
            && !reviewing
        {
            buttons.push(button("PR  (p)", Act::Pr(url.clone())));
        }
        if row_gestures {
            let mut b = button("Label…  (l)", Act::LabelOpen(l.clone()));
            b.hover = Some("Who has the ball, in your words (e.g. QA)");
            buttons.push(b);
            let mut b = button("Close…  (⇧X)", Act::CloseMenu(l.sel()));
            b.hover = Some("Close the work item and stop its agent; the worktree, branch and PR stay");
            buttons.push(b);
        }
        if c.gestures && l.row.is_some() && l.unseen(c.look) {
            buttons.push(button("Seen  (m)", Act::SeenLine(l.clone())));
        }
        if c.why && l.row.is_some() {
            let mut b = button("Why?  (w)", Act::Why(l.clone()));
            b.hover = Some("The rule that put it here, and the facts it saw");
            b.more = true;
            buttons.push(b);
        }
        let (buttons, mut rest) = bar(buttons, |b| b.more);
        ui.horizontal_wrapped(|ui| {
            for b in buttons {
                let mut r = ui.add_enabled(b.enabled, egui::Button::new(b.text));
                if let Some(h) = b.hover {
                    r = r.on_hover_text(h);
                }
                if r.clicked() {
                    acts.push(b.act);
                }
            }
            if !rest.is_empty() {
                ui.menu_button("More…", |ui| {
                    ui.set_min_width(220.0);
                    for b in rest.drain(..) {
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
        });
        if let Err(why) = c.pass
            && l.row.is_some()
            && !l.done()
            && l.passable()
        {
            // On a line of its own: wrapped, never cut.
            ui.add(egui::Label::new(RichText::new(why).small().color(t.muted)).wrap());
        }
        if forms.parking == Some(l.sel()) {
            let whole = forms.park_whole || l.top.is_none();
            egui::Frame::new().stroke(egui::Stroke::new(1.0, t.border)).inner_margin(6).show(ui, |ui| {
                ui.horizontal_wrapped(|ui| {
                    let what = if whole { "Park the whole work item until" } else { "Park this item until" };
                    ui.label(RichText::new(what).small().color(t.muted));
                    for (i, (label, park)) in c.parks.iter().enumerate() {
                        let ok = c.engine || matches!(park, Park::For(_));
                        let text = RichText::new(format!("{}  {label}", i + 1)).small();
                        if ui.add_enabled(ok, egui::Button::new(text)).clicked() {
                            acts.push(Act::Park(l.clone(), *park, whole));
                        }
                    }
                });
                if l.top.is_some() && c.gestures && !l.done() {
                    let mut all = whole;
                    if ui
                        .checkbox(&mut all, RichText::new("the whole work item and all its items  (w)").small())
                        .on_hover_text("It leaves Now, Round and In flight for Parked; its questions go to the orchestrator if it may answer them")
                        .changed()
                    {
                        acts.push(Act::ParkWhole(all));
                    }
                }
                ui.label(RichText::new("1-5 choose · w whole work item · Esc close").small().color(t.muted));
            });
        }
        if forms.closing == Some(l.sel()) {
            let agents: Vec<String> = l.row.as_ref().map(|r| r.agents.iter().map(|a| a.id.clone()).collect()).unwrap_or_default();
            egui::Frame::new().stroke(egui::Stroke::new(1.0, t.border)).inner_margin(6).show(ui, |ui| {
                ui.label(RichText::new(close_text(&agents, c.close_stop)).small().color(t.text));
                ui.horizontal_wrapped(|ui| {
                    let stop = if c.close_stop && !agents.is_empty() { " and stop" } else { "" };
                    if ui.button(RichText::new(format!("Close as done{stop}")).small()).clicked() {
                        acts.push(Act::Close(l.clone(), "done"));
                    }
                    if ui.button(RichText::new(format!("Close as dropped{stop}")).small()).clicked() {
                        acts.push(Act::Close(l.clone(), "dropped"));
                    }
                    if ui.button(RichText::new("Cancel  (Esc)").small()).clicked() {
                        acts.push(Act::CloseMenu(l.sel()));
                    }
                });
            });
        }
        if let Some(label) = forms.labeling.as_mut().filter(|f| f.key == l.key) {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new("Ball with").small().color(t.muted));
                for b in LABEL_BALLS {
                    if ui.selectable_label(label.ball == b, RichText::new(b).small()).clicked() {
                        label.ball = b.to_string();
                    }
                }
                let edit = ui.add(
                    egui::TextEdit::singleline(&mut label.text)
                        .char_limit(LABEL_CHARS)
                        .hint_text("e.g. QA")
                        .desired_width(200.0),
                );
                if focus {
                    edit.request_focus();
                }
                let enter = (edit.lost_focus() || edit.has_focus()) && ui.input(|i| i.key_pressed(Key::Enter));
                if ui.button("Save").clicked() || enter {
                    acts.push(Act::LabelSave(label.clone()));
                }
                if ui.button("Cancel").clicked() || ui.input(|i| i.key_pressed(Key::Escape)) {
                    acts.push(Act::LabelCancel);
                }
            });
        }
    });
    ui.add_space(2.0);
}
