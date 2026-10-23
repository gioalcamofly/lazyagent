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
//! row; middle-ellipsis names with the full one on hover; `?` the Home guide.

use eframe::egui::{self, Align, Color32, Key, Layout, Modifiers, RichText, Sense, Ui, Vec2};
use lazyagent_proto::types::{
    ActionableResult, Decision, InboxConfig, Presence, StatusGestureResult, StatusTimeline, TimelineEntry,
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

/// The stalled kinds auto-delegation offers (Q-B33), with their labels.
pub const AUTO_DELEGATE: &[(&str, &str)] = &[
    ("conflict_with_base", "Conflict with the base branch"),
    ("agent_dead", "Agent died"),
    ("ci_red", "CI red (needs CI tracking, inactive)"),
];

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
    Pass(Line),
    TakeBack(Line),
    SendCi(Line),
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
        let folded = self.saved.home_fold;
        if take(Modifiers::SHIFT, Key::Questionmark) || take(Modifiers::NONE, Key::Questionmark) {
            self.open_dialog(crate::dialogs::Kind::HomeHelp);
            return true;
        }
        if let Some(key) = self.home.parking.clone()
            && let Some(line) = self.home.lines.get(&key).cloned()
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
        if take(Modifiers::SHIFT, Key::Z) {
            self.home.parked_open = !self.home.parked_open;
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
        if take(Modifiers::NONE, Key::Enter) {
            let asks = line.top.as_ref().is_some_and(|t| t.asks(line.brief.as_ref()));
            if (line.zone == Zone::Now || asks) && line.agent().is_some() {
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
        } else if take(Modifiers::SHIFT, Key::X) {
            self.close_menu(&line);
        } else if take(Modifiers::NONE, Key::X) {
            self.dismiss_line(&line);
        } else if take(Modifiers::NONE, Key::Z) {
            if line.top.is_none() && !self.can_gesture(&line) {
                self.toast(Severity::Info, "Nothing to park: it leaves once it is merged.");
            } else {
                self.home.expanded = Some(line.key.clone());
                self.home.parking = Some(line.key.clone());
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
        } else {
            return false;
        }
        true
    }

    fn toggle_line(&mut self, key: &str) {
        let open = self.home.expanded.as_deref() == Some(key);
        self.home.expanded = (!open).then(|| key.to_string());
        self.home.parking = None;
        self.home.closing = None;
        if !open && let Some(line) = self.home.lines.get(key).cloned() {
            self.look_at(&line);
        }
    }

    /// The user looked at a line: its timeline since the last look, its
    /// brief, and the item is seen (`status_seen {key}`).
    pub fn look_at(&mut self, line: &Line) {
        if let Some(id) = line.top.as_ref().and_then(|t| t.decision_id.clone())
            && !self.decisions.contains_key(&id)
            && self.has("decisions")
        {
            self.conn.request("decision_get", json!({"id": id}), |s, reply| {
                if let Ok(v) = reply
                    && let Ok(r) = serde_json::from_value::<lazyagent_proto::types::DecisionResult>(v)
                {
                    s.decisions.insert(r.decision.id.clone(), r.decision);
                    s.inbox_gen += 1;
                }
            });
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
            let hint = if self.can_gesture(line) { " Close the work item with Shift+X." } else { "" };
            self.toast(Severity::Info, format!("Nothing to dismiss: it leaves once it is merged.{hint}"));
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
                self.gesture(&line.key, params, "Not parked");
            } else {
                self.toast(Severity::Info, "Nothing to park: it leaves once it is merged.");
            }
            return;
        };
        if self.engine_inbox() {
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

    /// Row gestures (label, park, close) need the engine's `gestures` and a
    /// work item that is not done.
    pub fn can_gesture(&self, line: &Line) -> bool {
        self.gestures() && line.row.is_some() && !line.done()
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

    /// `l`: the label form (a ball and a short text, e.g. others · "QA").
    pub fn open_label(&mut self, line: &Line) {
        if !self.can_gesture(line) {
            self.toast(Severity::Info, "Labels need a work item and an engine with gestures.");
            return;
        }
        let g = line.gesture().filter(|g| g["kind"] == "label");
        self.home.expanded = Some(line.key.clone());
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
        self.home.expanded = Some(line.key.clone());
        self.home.parking = None;
        self.home.closing = Some(line.key.clone());
    }

    pub fn close_line(&mut self, line: &Line, how: &str) {
        self.home.closing = None;
        let params = json!({"key": line.key, "kind": "close", "how": how});
        self.gesture(&line.key, params, "Not closed");
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
        if matches!(top.kind.as_str(), "approval" | "question" | "decision" | "delegation") {
            self.toast(Severity::Info, "That one is answered, not delegated: open the agent's terminal (o).");
            return;
        }
        let mut params = json!({"key": line.key, "kind": "delegate", "to": "agent", "item": top.id});
        if top.kind == "ci"
            && let Some(place) = &line.place
            && let Some(pr) = self.store.pr_info.get(place).cloned().flatten()
        {
            params["text"] = json!(crate::attention::ci_failure_prompt(&pr));
        }
        self.gesture(&line.key, params, "Not delegated");
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

    /// `u`: take a pass back, or drop the user's label / park / close.
    pub fn take_back(&mut self, line: &Line) {
        if line.claim.is_some() {
            let params = json!({"key": line.key, "kind": "clear"});
            self.gesture(&line.key, params, "Could not take it back");
        } else if line.gesture().is_some() && self.gestures() {
            let params = json!({"key": line.key, "kind": "clear"});
            self.gesture(&line.key, params, "Could not clear it");
        }
    }

    /// `Shift+U`: the revert form of the orchestrator's decision on the line.
    pub fn open_revert(&mut self, line: &Line) {
        if !self.first_responder() {
            return;
        }
        match &line.decided {
            Some(d) if d.revertible == Some(true) => {
                self.home.expanded = Some(line.key.clone());
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
}

/// Settings → Auto-delegation (per repo, Q-B33): which stalled kinds the
/// engine delegates on its own, and to whom. `.lazyagent.toml` stays the
/// source of truth; the engine writes it.
pub fn auto_delegation_menu(ui: &mut Ui, s: &mut State) {
    let Some(config) = s.inbox_config.clone() else {
        ui.label(RichText::new("Reading…").small());
        return;
    };
    ui.label(RichText::new(format!("When one of these stalls in {}, delegate it:", s.inbox_repo())).small());
    let mut next = config.clone();
    for (kind, label) in AUTO_DELEGATE {
        let mut on = next.auto_delegate.iter().any(|k| k == kind);
        if ui.checkbox(&mut on, *label).changed() {
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
    if next != config {
        s.set_inbox_config(next);
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
fn name_cell(ui: &mut Ui, width: f32, name: &str) {
    ui.allocate_ui_with_layout(Vec2::new(width, 18.0), Layout::left_to_right(Align::Center), |ui| {
        ui.set_width(width);
        let shown = fit_name(ui, name, width - 2.0);
        let label = ui.add(egui::Label::new(RichText::new(&shown).strong()).sense(Sense::hover()));
        if shown != name {
            label.on_hover_text(name);
        }
    });
}

fn section(ui: &mut Ui, title: &str, color: Color32, extra: impl FnOnce(&mut Ui)) {
    ui.add_space(8.0);
    ui.horizontal(|ui| {
        ui.label(RichText::new(title).strong().small().color(color));
        extra(ui);
    });
    ui.add_space(2.0);
}

impl App {
    pub(crate) fn home_view(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let now = unix_now();
        let folded = self.state.saved.home_fold;
        let selected = self.state.home.current(folded).map(|l| l.key);
        let home = &self.state.home;
        let forms = Forms {
            expanded: home.expanded.clone(),
            parking: home.parking.clone(),
            park_whole: home.park_whole,
            closing: home.closing.clone(),
            labeling: home.labeling.clone(),
            reverting: home.reverting.clone(),
            focus: home.focus_form,
        };
        let lines: Vec<Vec<Line>> =
            home::ZONES.iter().map(|z| home.shown_in(*z).into_iter().cloned().collect()).collect();
        let (waiting, resolved, round_at) = (home.waiting(), home.resolved, home.round_at);
        let parked_open = home.parked_open;
        let deep_count = home.count(Zone::Deep);
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
            parks: s.parks(),
            selected: selected.as_deref(),
            timelines: &s.timelines,
        };
        let presence = s.presence.clone();
        let mut acts: Vec<Act> = vec![];
        let mut forms_out = forms.clone();
        egui::ScrollArea::vertical().id_salt("home").auto_shrink([false, false]).show(ui, |ui| {
            if ctx.first_responder {
                presence_bar(ui, &t, presence.as_ref(), counts, &mut acts);
            }
            let mut zone = |ui: &mut Ui, z: Zone, acts: &mut Vec<Act>| {
                for l in &lines[z as usize] {
                    line_ui(ui, &ctx, l, z, &mut forms_out, acts);
                }
            };
            if !lines[0].is_empty() {
                section(ui, "NOW", t.yellow, |_| {});
                zone(ui, Zone::Now, &mut acts);
            }
            section(ui, "ROUND", t.accent, |ui| {
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
            zone(ui, Zone::Round, &mut acts);
            if ctx.gestures || deep_count > 0 {
                // deep_max is shown, never enforced (§5b).
                let title = match counts.deep_max {
                    0 => format!("TO SIT DOWN WITH ({deep_count})"),
                    max => format!("TO SIT DOWN WITH ({deep_count} of {max})"),
                };
                section(ui, &title, t.purple, |ui| {
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        if counts.deep_max > 0 && deep_count > counts.deep_max as usize {
                            ui.label(
                                RichText::new(format!("over your limit of {}", counts.deep_max))
                                    .small()
                                    .color(t.yellow),
                            );
                        } else {
                            ui.label(RichText::new("for a focus block").small().color(t.muted));
                        }
                    });
                });
                if lines[2].is_empty() {
                    ui.label(RichText::new("Nothing to sit down with.").color(t.muted));
                }
                zone(ui, Zone::Deep, &mut acts);
            }
            ui.add_space(6.0);
            ui.separator();
            let flight = &lines[3];
            let live = flight.iter().filter(|l| !l.done()).count();
            let mut title = format!("IN FLIGHT ({live})");
            if counts.not_started > 0 {
                title.push_str(&format!(" · {} not started", counts.not_started));
            }
            let done = flight.len() - live;
            if done > 0 {
                title.push_str(&format!(" · {done} done today"));
            }
            section(ui, &title, t.muted, |ui| {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.button(RichText::new(if folded { "unfold (f)" } else { "fold (f)" }).small()).clicked() {
                        acts.push(Act::Fold);
                    }
                    if lines.iter().flatten().any(|l| l.unseen(ctx.look))
                        && ui
                            .button(RichText::new("mark all seen (M)").small())
                            .on_hover_text("Clear the • dots")
                            .clicked()
                    {
                        acts.push(Act::Seen);
                    }
                });
            });
            if !folded {
                if flight.is_empty() {
                    ui.label(RichText::new("No work in flight.").color(t.muted));
                }
                zone(ui, Zone::Flight, &mut acts);
            }
            let parked = &lines[Zone::Parked as usize];
            if !parked.is_empty() {
                ui.add_space(4.0);
                let need = parked.iter().filter(|l| l.parked_needs_you()).count();
                let mut title = format!("PARKED ({})", parked.len());
                if need > 0 {
                    title.push_str(&format!(" · {need} need you"));
                }
                let color = if need > 0 { t.yellow } else { t.muted };
                section(ui, &title, color, |ui| {
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        let text = if parked_open { "hide (Z)" } else { "show (Z)" };
                        if ui.button(RichText::new(text).small()).clicked() {
                            acts.push(Act::ParkedFold);
                        }
                    });
                });
                if parked_open {
                    zone(ui, Zone::Parked, &mut acts);
                }
            }
            ui.add_space(8.0);
            let mut keys = String::from(
                "• changed since your last look · Enter open/expand · o terminal · n notes · p PR · x dismiss · \
                 z park · O pass to orchestrator · u take back · f fold · ? Home guide",
            );
            if ctx.gestures {
                keys.push_str(" · l label · X close · m seen · M all seen");
            }
            if ctx.delegation {
                keys.push_str(" · d delegate to its agent");
            }
            if !parked.is_empty() {
                keys.push_str(" · Z show parked");
            }
            if ctx.first_responder {
                keys.push_str(" · U revert");
            }
            ui.label(RichText::new(keys).small().color(t.muted));
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
                    s.home.park_whole = s.home.lines.get(&k).is_some_and(|l| l.top.is_none());
                    s.home.parking = (!open).then_some(k);
                }
                Act::Park(l, p, whole) => s.park_line(&l, p, whole),
                Act::ParkWhole(on) => s.home.park_whole = on,
                Act::ParkedFold => s.home.parked_open = !s.home.parked_open,
                Act::Delegate(l) => s.delegate_line(&l),
                Act::Pass(l) => s.pass_line(&l),
                Act::TakeBack(l) => s.take_back(&l),
                Act::SendCi(l) => s.send_ci(&l),
                Act::Cut => s.home.cut(now),
                Act::Fold => {
                    s.saved.home_fold = !s.saved.home_fold;
                    s.touch_saved();
                }
                Act::Seen => s.mark_all_seen(),
                Act::SeenLine(l) => s.seen_line(&l),
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
                Act::Presence(mode) => s.set_presence(mode),
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
    parks: &'static [(&'static str, Park)],
    selected: Option<&'a str>,
    timelines: &'a std::collections::HashMap<String, StatusTimeline>,
}

/// Presence (auto / present / away), what the engine applies, and the
/// orchestrator's actions today (first responder only).
fn presence_bar(
    ui: &mut Ui,
    t: &Theme,
    presence: Option<&Presence>,
    counts: lazyagent_proto::types::StatusCounts,
    acts: &mut Vec<Act>,
) {
    ui.add_space(6.0);
    ui.horizontal(|ui| {
        ui.label(RichText::new("You are").small().color(t.muted));
        let mode = presence.and_then(|p| p.mode.as_deref()).unwrap_or("auto");
        for m in ["auto", "present", "away"] {
            if ui
                .selectable_label(mode == m, RichText::new(m).small())
                .on_hover_text("What the orchestrator may decide alone depends on it")
                .clicked()
                && mode != m
            {
                acts.push(Act::Presence(m));
            }
        }
        if let Some(p) = presence {
            ui.label(RichText::new(format!("· counted as {}", p.effective)).small().color(t.muted));
        }
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            if let Some(n) = counts.auto_actions {
                let reverted = counts.auto_reverted.unwrap_or(0);
                ui.label(
                    RichText::new(format!("orchestrator today: {n} actions · {reverted} reverted"))
                        .small()
                        .color(t.cyan),
                );
            }
        });
    });
}

fn line_ui(ui: &mut Ui, c: &LineCtx, l: &Line, shown: Zone, forms: &mut Forms, acts: &mut Vec<Act>) {
    let t = c.t;
    let selected = c.selected == Some(l.key.as_str());
    let expanded = forms.expanded.as_deref() == Some(&l.key);
    let fill = if selected { t.current_bg } else { Color32::TRANSPARENT };
    let resp = egui::Frame::new().fill(fill).inner_margin(egui::Margin { left: 10, right: 8, top: 3, bottom: 3 }).show(
        ui,
        |ui| {
            ui.set_width(ui.available_width());
            if l.done() {
                ui.multiply_opacity(0.45);
            }
            let head = ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                let name_w = (ui.available_width() * NAME_SHARE).clamp(NAME_MIN, NAME_MAX);
                if matches!(shown, Zone::Flight | Zone::Parked) {
                    let dot = if l.unseen(c.look) { "•" } else { " " };
                    cell(ui, 10.0, RichText::new(dot).strong().color(t.accent));
                    name_cell(ui, name_w, &l.name);
                    let (ball, state) = match (&l.claim, l.hold()) {
                        (Some(claim), _) => {
                            ("orchestrator".to_string(), format!("looking · {} left", left(claim.until - c.now)))
                        }
                        (None, Some(hold)) => {
                            ("orchestrator".to_string(), format!("{} · {} left", hold.note, left(hold.until - c.now)))
                        }
                        _ if shown == Zone::Parked && l.parked_needs_you() => {
                            ("needs you".to_string(), l.state.clone())
                        }
                        _ if shown == Zone::Parked => ("parked".to_string(), l.state.clone()),
                        _ if l.looking() && c.first_responder => ("orchestrator".to_string(), "looking".into()),
                        // In the inbox, but held for the next round.
                        _ if l.zone == Zone::Round => ("you · next round".to_string(), l.state.clone()),
                        _ => (l.ball.clone(), l.state.clone()),
                    };
                    let color = if l.claim.is_some() || l.hold().is_some() || l.looking() {
                        t.cyan
                    } else if shown == Zone::Parked {
                        if l.parked_needs_you() { t.yellow } else { t.muted }
                    } else {
                        ball_color(t, &l.ball)
                    };
                    cell(ui, 110.0, RichText::new(ball).small().color(color));
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(RichText::new(home::age(c.now - l.changed_at)).small().color(t.muted));
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
                    cell(ui, 10.0, RichText::new(if selected { "▸" } else { dot }).color(color));
                    cell(ui, 74.0, RichText::new(l.verb()).strong().color(color));
                    name_cell(ui, name_w, &l.name);
                    // "approve: npm test" after "Approve": the fact alone.
                    let text = if l.looking() && c.first_responder {
                        "orchestrator · looking".to_string()
                    } else if let Some(top) = l.top.as_ref().filter(|t| t.kind == "decision") {
                        // A brief's question (or "agent a2 asks: …") reads better than its state.
                        top.label.clone()
                    } else {
                        l.state.split_once(": ").map(|(_, rest)| rest).unwrap_or(&l.state).to_string()
                    };
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
            // A brief reads as one more line under its row (§5b wireframe).
            if let Some(d) = l.brief.as_ref().filter(|d| d.valid) {
                ui.horizontal(|ui| {
                    ui.add_space(102.0);
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
            if let Some(hint) = l.row.as_ref().and_then(|r| r.suggestion.as_deref()) {
                ui.horizontal(|ui| {
                    ui.add_space(18.0);
                    ui.add(
                        egui::Label::new(
                            RichText::new(format!("suggestion: {hint} (p opens the PR; closing it is yours)"))
                                .small()
                                .color(t.yellow),
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
                details(ui, c, l, forms, acts);
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
    let mut text = match &g["until"] {
        Value::Number(n) => format!("parked for {}", left(n.as_f64().unwrap_or(now) - now)),
        Value::String(e) if e == "indefinite" => "parked indefinitely".into(),
        Value::String(e) if e == "change" => "parked until something changes (CI, review, commit, agent)".into(),
        Value::String(e) => format!("parked until {}", e.replace('_', " ")),
        _ => "parked".into(),
    };
    if let Some(reason) = g["text"].as_str().filter(|r| !r.is_empty()) {
        text.push_str(&format!(" ({reason})"));
    }
    text
}

fn timeline_line(e: &TimelineEntry, now: f64) -> String {
    let actor = if e.actor == "engine" { String::new() } else { format!(" · {}", e.actor) };
    format!("{} ago  {}  {}{actor}", home::age(now - e.at), e.kind, e.text)
}

/// The brief, read-only (the decision view with answers is I5's).
fn brief_ui(ui: &mut Ui, t: &Theme, d: &Decision) {
    if !d.valid {
        ui.label(RichText::new(format!("agent {} asks: {}", d.agent, d.brief.question)).color(t.text));
        ui.label(RichText::new("Answer it in the agent's terminal (o).").small().color(t.muted));
        return;
    }
    ui.label(RichText::new(&d.brief.question).strong().color(t.text));
    let rec = d.brief.recommendation.as_deref().unwrap_or("");
    for o in &d.brief.options {
        let mut text = format!("{}  {}", o.key, o.label);
        if let Some(c) = o.consequence.as_deref().filter(|c| !c.is_empty()) {
            text.push_str(&format!(" — {c}"));
        }
        let recommended = o.key.eq_ignore_ascii_case(rec);
        if recommended {
            text.push_str("   (recommended)");
        }
        ui.label(RichText::new(text).small().color(if recommended { t.accent } else { t.text }));
    }
    if !d.brief.tried.is_empty() {
        ui.label(RichText::new(format!("Tried: {}", d.brief.tried)).small().color(t.muted));
    }
    let mut facts = vec![format!("Reversible: {}", d.brief.reversible.as_deref().unwrap_or("not given"))];
    if !d.brief.impact_if_wrong.is_empty() {
        facts.push(format!("If wrong: {}", d.brief.impact_if_wrong));
    }
    ui.label(RichText::new(facts.join(" · ")).small().color(t.muted));
    if !d.brief.evidence.is_empty() {
        let ev: Vec<String> = d.brief.evidence.iter().map(evidence_text).collect();
        ui.label(RichText::new(format!("Evidence: {}", ev.join(" · "))).small().color(t.muted));
    }
    ui.label(RichText::new("Answer it in the agent's terminal (o) for now.").small().color(t.muted));
}

/// The expanded line: who is on it, the agent's note, and the actions.
fn details(ui: &mut Ui, c: &LineCtx, l: &Line, forms: &mut Forms, acts: &mut Vec<Act>) {
    let t = c.t;
    let focus = std::mem::take(&mut forms.focus);
    ui.add_space(2.0);
    ui.indent(("home-details", &l.key), |ui| {
        // The full name: the line may show it cut in the middle.
        ui.label(RichText::new(&l.name).small().strong().color(t.text));
        let is_brief = l.top.as_ref().is_some_and(|t| t.kind == "decision");
        match (&l.top, &l.brief) {
            (Some(_), Some(d)) if is_brief => brief_ui(ui, t, d),
            (Some(top), _) => {
                let text = [top.label.as_str(), top.detail.as_str()]
                    .iter()
                    .filter(|x| !x.is_empty())
                    .cloned()
                    .collect::<Vec<_>>();
                ui.label(RichText::new(text.join(" · ")).small().color(t.muted));
            }
            _ => {}
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
            if let Some(g) = &row.gesture {
                ui.label(RichText::new(gesture_text(g, c.now)).small().color(t.purple));
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
        }
        if l.zone == Zone::Parked
            && let Some(g) = l.gesture()
        {
            let mut text = park_until_text(g, c.now);
            if l.parked_needs_you() {
                text.push_str(" · needs you: its items are not routed to the orchestrator");
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
        if c.first_responder && l.looking() {
            ui.label(RichText::new("The orchestrator is looking at this first.").small().color(t.cyan));
        }
        if c.first_responder
            && let Some(d) = &l.decided
        {
            ui.label(RichText::new(format!("{}: {}", d.brief.question, decided_text(d))).small().color(t.cyan));
            if d.revertible == Some(true) {
                match forms.reverting.as_mut().filter(|r| r.0 == d.id) {
                    Some((id, comment)) => {
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
                        if ui.button("Revert…  (U)").on_hover_text("Tell the agent your choice instead").clicked() {
                            acts.push(Act::RevertOpen(l.clone()));
                        }
                    }
                }
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
                    ui.label(RichText::new("Nothing new since you looked.").small().color(t.muted));
                }
                Some(tl) => {
                    for e in tl.entries.iter().rev().take(12) {
                        ui.label(RichText::new(timeline_line(e, c.now)).small().color(t.text));
                    }
                }
            }
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
            let ci = l.top.as_ref().is_some_and(|t| t.kind == "ci");
            let delegable = c.delegation
                && l.top.as_ref().is_some_and(|t| {
                    !matches!(t.kind.as_str(), "approval" | "question" | "decision" | "delegation")
                })
                && l.row.as_ref().is_some_and(|r| !r.agents.is_empty());
            if ci {
                // With the engine's delegation, the same thing, followed.
                let text = if delegable { "Send the failures to its agent  (d)" } else { "Send the failures to its agent" };
                if ui.button(text).clicked() {
                    acts.push(Act::SendCi(l.clone()));
                }
            } else if delegable
                && ui.button("Delegate to its agent  (d)").on_hover_text("It comes back if the fact still holds").clicked()
            {
                acts.push(Act::Delegate(l.clone()));
            }
            let row_gestures = c.gestures && l.row.is_some() && !l.done();
            if l.top.is_some()
                && ui.button("Dismiss  (x)").on_hover_text("Hide until it happens again, for every client").clicked()
            {
                acts.push(Act::Dismiss(l.clone()));
            }
            if (l.top.is_some() || row_gestures) && ui.button("Park until…  (z)").clicked() {
                acts.push(Act::ParkMenu(l.key.clone()));
            }
            if row_gestures {
                if ui.button("Label…  (l)").on_hover_text("Who has the ball, in your words (e.g. QA)").clicked() {
                    acts.push(Act::LabelOpen(l.clone()));
                }
                if ui.button("Close…  (X)").on_hover_text("Done or dropped: it leaves Home after a day").clicked() {
                    acts.push(Act::CloseMenu(l.key.clone()));
                }
            }
            let unpark = if l.zone == Zone::Parked { "Unpark  (u)" } else { "Clear  (u)" };
            if c.gestures && l.claim.is_none() && l.gesture().is_some() && ui.button(unpark).clicked() {
                acts.push(Act::TakeBack(l.clone()));
            }
            if l.claim.is_some() || l.hold().is_some() {
                if ui.button("Take back  (u)").clicked() {
                    acts.push(Act::TakeBack(l.clone()));
                }
            } else if (l.row.is_some() && !l.done()) || !c.engine {
                let ok = c.pass.is_ok() && l.row.is_some();
                let b = ui.add_enabled(ok, egui::Button::new("Pass to orchestrator  (O)"));
                if b.clicked() {
                    acts.push(Act::Pass(l.clone()));
                }
                if let Err(why) = c.pass {
                    ui.label(RichText::new(why).small().color(t.muted));
                }
            }
            if c.gestures && l.row.is_some() && l.unseen(c.look) && ui.button("Seen  (m)").clicked() {
                acts.push(Act::SeenLine(l.clone()));
            }
        });
        if forms.parking.as_deref() == Some(&l.key) {
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
        if forms.closing.as_deref() == Some(&l.key) {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new("Close the work item as").small().color(t.muted));
                if ui.button(RichText::new("done").small()).clicked() {
                    acts.push(Act::Close(l.clone(), "done"));
                }
                if ui.button(RichText::new("dropped").small()).clicked() {
                    acts.push(Act::Close(l.clone(), "dropped"));
                }
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
