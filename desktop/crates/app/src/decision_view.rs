//! The decision view (I5, feature `decision_view`): a brief opened from
//! Home (Enter on a brief's line, or `v`). The brief on the left (question,
//! options as radio buttons with the recommendation preselected, what was
//! tried, reversibility, impact, a comment box, Answer); the evidence on the
//! right, one tab per piece (a diff hunk drawn as the diff view draws it, a
//! log's or a file's lines, a notes section, a link), and the item's
//! timeline under it. An "Outdated" banner when the branch head moved since
//! the brief; "decided by orchestrator" with Revert when it answered.
//!
//! **Explain this** and **Request a new brief** go to whoever can answer,
//! first match: the owner agent when it runs; when it can only be restored,
//! "Restore agent and explain" (never automatic); the orchestrator; else
//! the button is disabled, with Open terminal and Pass to orchestrator.
//! `y` explains (also any Home line, by its work item), `Shift+Y` restores
//! and explains.

use std::collections::HashMap;

use eframe::egui::{self, Align, Color32, FontId, Key, Layout, Modifiers, RichText, Sense, Ui, Vec2};
use lazyagent_proto::types::{
    Decision, DecisionEvidence, DecisionExplain, DecisionExplainResult, DecisionResult, DiffResult, StatusTimeline,
    TimelineEntry,
};
use serde_json::{Value, json};

use crate::app::{App, Severity, State, unix_now};
use crate::diff::{self, RowKind};
use crate::home::{self, Line};
use crate::store::ORCHESTRATOR_KEY;
use crate::theme::Theme;

/// What one evidence tab shows, once read.
#[derive(Debug, Clone, PartialEq)]
pub enum Ev {
    Loading,
    /// A diff hunk (or the file's whole diff), and a note when it is not
    /// the hunk as asked.
    Hunk(Vec<diff::Row>, Option<String>),
    Text(DecisionEvidence),
    Failed(String),
}

/// The open decision view.
#[derive(Debug, Clone, PartialEq)]
pub struct DecisionView {
    pub id: String,
    /// The work item (Home's line key).
    pub key: String,
    /// The chosen option's key (or, with no options, the typed answer).
    pub choice: Option<String>,
    pub comment: String,
    pub tab: usize,
    pub evidence: HashMap<usize, Ev>,
    pub timeline: Vec<TimelineEntry>,
    /// The user's own question for Explain this.
    pub question: String,
    /// Request a new brief: the form is open, with its note.
    pub rebrief: Option<String>,
    /// The revert form: its comment.
    pub reverting: Option<String>,
    pub sending: bool,
}

/// Who answers Explain this / Request a new brief (§5b, first match).
#[derive(Debug, Clone, PartialEq)]
pub enum Route {
    /// The owner agent runs.
    Owner(String),
    /// It does not run but can be restored: only on the user's click.
    Restore(String),
    Orchestrator,
    /// Nobody: the button is disabled.
    Nobody,
}

/// The option preselected: the orchestrator's suggestion, else the
/// recommendation.
pub fn preselected(d: &Decision) -> Option<String> {
    let suggested = d.escalation.as_ref().and_then(|e| e["suggestion"].as_str());
    let pick = suggested.or(d.brief.recommendation.as_deref())?;
    d.brief.options.iter().find(|o| o.key.eq_ignore_ascii_case(pick)).map(|o| o.key.clone())
}

/// The brief was asked at another commit than the branch's head now.
pub fn outdated(d: &Decision, head: Option<&str>) -> Option<String> {
    let (sha, head) = (d.sha.as_deref()?, head?);
    if sha.is_empty() || head.is_empty() || sha.starts_with(head) || head.starts_with(sha) {
        return None;
    }
    Some(format!(
        "Outdated: the branch moved since the brief ({} → {}). Its evidence may not match; ask for a new brief (b).",
        short(sha),
        short(head)
    ))
}

fn short(sha: &str) -> &str {
    sha.get(..7).unwrap_or(sha)
}

/// `e.lines` ("120-134") as an inclusive range.
fn marked(e: &Value) -> Option<(u32, u32)> {
    let lines = e["lines"].as_str()?;
    let (a, b) = lines.split_once('-').unwrap_or((lines, lines));
    Some((a.trim().parse().ok()?, b.trim().parse().ok()?))
}

/// A tab's title: `diff src/retry.py`, `log job:tests:120-134`.
pub fn tab_title(e: &Value) -> String {
    let kind = e["kind"].as_str().unwrap_or("?");
    let what = ["path", "ref", "section", "url"].iter().find_map(|k| e[*k].as_str()).unwrap_or("");
    let what = if kind == "url" { what.split("://").nth(1).unwrap_or(what) } else { what };
    let lines = e["lines"].as_str().map(|l| format!(":{l}")).unwrap_or_default();
    let mut title = format!("{kind} {what}{lines}");
    if title.chars().count() > 40 {
        title = format!("{}…", title.chars().take(39).collect::<String>());
    }
    title
}

/// The hunk `header` ("@@ -40,7 +40,12 @@") of `path` in a parsed diff;
/// the file's whole diff (with a note) when that hunk is not there.
pub fn find_hunk(d: &diff::Diff, path: &str, header: Option<&str>) -> Ev {
    let Some(file) =
        d.files.iter().find(|f| f.path == path && !f.uncommitted).or_else(|| d.files.iter().find(|f| f.path == path))
    else {
        return Ev::Failed(format!("{path} is not in the branch's diff now."));
    };
    let Some(header) = header.map(str::trim).filter(|h| !h.is_empty()) else {
        return Ev::Hunk(file.rows.clone(), None);
    };
    let at = file.rows.iter().position(|r| r.kind == RowKind::Hunk && r.text.starts_with(header));
    match at {
        Some(i) => {
            let end =
                file.rows[i + 1..].iter().position(|r| r.kind == RowKind::Hunk).map_or(file.rows.len(), |n| i + 1 + n);
            Ev::Hunk(file.rows[i..end].to_vec(), None)
        }
        None => Ev::Hunk(
            file.rows.clone(),
            Some(format!("The hunk {header} is not in the diff now (the branch moved): the file's whole diff.")),
        ),
    }
}

/// "explained by agent a2" · "explained by orchestrator".
pub fn explained_by(e: &DecisionExplain) -> String {
    match e.by.as_deref() {
        Some("orchestrator") => "explained by orchestrator".into(),
        Some(by) => format!("explained by {}", by.replace(':', " ")),
        None => "explained".into(),
    }
}

/// Where a request stands, in a few words.
fn asked_text(e: &DecisionExplain) -> String {
    let who = if e.to == "orchestrator" { "the orchestrator".to_string() } else { e.to.replace(':', " ") };
    if e.sent_at.is_some() {
        format!("asked {who}: it is on it")
    } else {
        format!("asked {who}: sent once its turn is over")
    }
}

impl State {
    /// The engine opens briefs in a decision view (I5).
    pub fn decision_view_on(&self) -> bool {
        self.has("decision_view") && self.has("decisions")
    }

    /// Who would answer Explain this for an agent's work in `path`.
    pub fn explain_route(&self, path: Option<&str>, agent: Option<&str>) -> Route {
        let alive = |a: &lazyagent_proto::types::Agent| a.status != "exited";
        if let Some(path) = path.filter(|p| *p != ORCHESTRATOR_KEY) {
            if let Some(wt) = self.store.worktree(path) {
                let owner = wt
                    .agents
                    .iter()
                    .filter(|a| alive(a) && a.kind.as_deref() != Some("orchestrator"))
                    .find(|a| agent.is_none_or(|id| a.agent_id == id));
                if let Some(a) = owner {
                    return Route::Owner(a.agent_id.clone());
                }
            }
            let entry = self.restorable.iter().find(|e| {
                e.worktree_path == path && e.kind != "orchestrator" && agent.is_none_or(|id| e.agent_id == id)
            });
            if let Some(e) = entry {
                return Route::Restore(e.agent_id.clone());
            }
        }
        if self.store.orchestrator.iter().any(alive) {
            return Route::Orchestrator;
        }
        Route::Nobody
    }

    /// The request (Explain or a new brief) of `target` (a decision id, or
    /// `key:<work item>`), as last heard.
    pub fn explain_of(&self, kind: &str, target: &str) -> Option<&DecisionExplain> {
        let from_decision = self
            .decisions
            .get(target)
            .and_then(|d| if kind == "explain" { d.explain.as_ref() } else { d.rebrief.as_ref() });
        let event = self.explains.get(&format!("{kind}:{target}"));
        match (from_decision, event) {
            (Some(a), Some(b)) => {
                Some(if b.asked_at >= a.asked_at && (b.state == "answered" || a.state != "answered") { b } else { a })
            }
            (a, b) => a.or(b),
        }
    }

    pub fn on_explain(&mut self, e: DecisionExplain) {
        let target = e.id.clone().unwrap_or_else(|| format!("key:{}", e.key));
        if let Some(d) = e.id.as_ref().and_then(|id| self.decisions.get_mut(id)) {
            if e.kind == "explain" {
                d.explain = Some(e.clone());
            } else {
                d.rebrief = Some(e.clone());
            }
        }
        self.explains.insert(format!("{}:{target}", e.kind), e);
        self.inbox_gen += 1;
    }

    /// Opens the decision view on brief `id` of work item `key`.
    pub fn open_decision(&mut self, id: &str, key: &str) {
        let known = self.decisions.get(id).cloned();
        self.decision = Some(DecisionView {
            id: id.to_string(),
            key: key.to_string(),
            choice: known.as_ref().and_then(preselected),
            comment: String::new(),
            tab: 0,
            evidence: HashMap::new(),
            timeline: vec![],
            question: String::new(),
            rebrief: None,
            reverting: None,
            sending: false,
        });
        let id_owned = id.to_string();
        self.conn.request("decision_get", json!({"id": id}), move |s, reply| {
            if let Ok(r) = reply.and_then(|v| {
                serde_json::from_value::<DecisionResult>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                let d = r.decision;
                if let Some(view) = s.decision.as_mut().filter(|v| v.id == id_owned)
                    && view.choice.is_none()
                {
                    view.choice = preselected(&d);
                }
                s.decisions.insert(d.id.clone(), d);
                s.inbox_gen += 1;
                s.load_evidence(0);
            }
        });
        if self.gestures() {
            let id_owned = id.to_string();
            // The whole timeline (not only since the last look), and seen.
            self.conn.request("status_timeline", json!({"key": key, "since": 0}), move |s, reply| {
                if let Ok(v) = reply
                    && let Ok(t) = serde_json::from_value::<StatusTimeline>(v)
                    && let Some(view) = s.decision.as_mut().filter(|v| v.id == id_owned)
                {
                    view.timeline = t.entries;
                }
            });
            if let Some(line) = self.home.lines.get(key).cloned() {
                self.seen_line(&line);
            }
        }
        if known.is_some() {
            self.load_evidence(0);
        }
    }

    pub fn close_decision(&mut self) {
        self.decision = None;
    }

    /// `v` / Enter: the line's brief (or the orchestrator's decision there).
    pub fn open_line_decision(&mut self, line: &Line) -> bool {
        let id = line
            .top
            .as_ref()
            .and_then(|t| t.decision_id.clone())
            .or_else(|| line.decided.as_ref().map(|d| d.id.clone()));
        match id {
            Some(id) => {
                self.open_decision(&id, &line.key);
                true
            }
            None => false,
        }
    }

    /// Reads evidence tab `index` (once).
    pub fn load_evidence(&mut self, index: usize) {
        let Some(view) = self.decision.as_mut() else { return };
        view.tab = index;
        if view.evidence.contains_key(&index) {
            return;
        }
        let id = view.id.clone();
        let Some(d) = self.decisions.get(&id) else { return };
        let Some(e) = d.brief.evidence.get(index).cloned() else { return };
        let path = d.worktree_path.clone();
        let kind = e["kind"].as_str().unwrap_or("").to_string();
        if kind == "url" {
            return;
        }
        view.evidence.insert(index, Ev::Loading);
        let put = move |s: &mut State, ev: Ev| {
            if let Some(view) = s.decision.as_mut().filter(|v| v.id == id) {
                view.evidence.insert(index, ev);
            }
        };
        if kind == "diff" {
            let file = e["path"].as_str().unwrap_or("").to_string();
            let hunk = e["hunk"].as_str().map(str::to_string);
            let params = json!({"worktree_path": path, "max_bytes": crate::diff_view::MAX_BYTES, "pr": true});
            self.conn.request("get_diff", params, move |s, reply| {
                let ev = match reply.and_then(|v| {
                    serde_json::from_value::<DiffResult>(v)
                        .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
                }) {
                    Ok(r) => find_hunk(
                        &diff::parse_pr(&r.diff, r.uncommitted.as_deref().unwrap_or("")),
                        &file,
                        hunk.as_deref(),
                    ),
                    Err(e) => Ev::Failed(format!("Could not read the diff: {e}")),
                };
                put(s, ev);
            });
            return;
        }
        let id = self.decision.as_ref().map(|v| v.id.clone()).unwrap_or_default();
        self.conn.request("decision_evidence", json!({"id": id, "index": index}), move |s, reply| {
            let ev = match reply.and_then(|v| {
                serde_json::from_value::<DecisionEvidence>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => match r.error.clone() {
                    Some(err) => Ev::Failed(err),
                    None => Ev::Text(r),
                },
                Err(e) => Ev::Failed(format!("Could not read it: {e}")),
            };
            put(s, ev);
        });
    }

    /// Answer: `decision_answer {id, choice, comment}`; the agent gets it
    /// when its turn is over.
    pub fn answer_decision(&mut self) {
        let Some(view) = self.decision.as_mut() else { return };
        let Some(choice) = view.choice.clone().filter(|c| !c.trim().is_empty()) else {
            self.toast(Severity::Info, "Pick an option first.");
            return;
        };
        if view.sending {
            return;
        }
        view.sending = true;
        let (id, comment) = (view.id.clone(), view.comment.trim().to_string());
        let params = json!({"id": id, "choice": choice.trim(), "comment": comment});
        self.conn.request("decision_answer", params, move |s, reply| {
            if let Some(view) = s.decision.as_mut() {
                view.sending = false;
            }
            match reply.and_then(|v| {
                serde_json::from_value::<DecisionResult>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => {
                    let d = r.decision;
                    s.toast(
                        Severity::Info,
                        format!(
                            "Answered {}: agent {} gets it once its turn is over.",
                            d.choice().unwrap_or("?"),
                            d.agent
                        ),
                    );
                    s.decisions.insert(d.id.clone(), d);
                    s.inbox_gen += 1;
                    s.refresh_home(true);
                }
                Err(e) => s.toast(Severity::Error, format!("Not answered: {e}")),
            }
        });
    }

    /// Explain this (or Request a new brief): `id` for a brief, else the
    /// work item `key`. `restore`: the user's explicit "Restore agent and …".
    pub fn ask_explain(&mut self, kind: &'static str, id: Option<String>, key: &str, text: String, restore: bool) {
        let method = if kind == "explain" { "decision_explain_request" } else { "decision_rebrief_request" };
        let mut params = match &id {
            Some(id) => json!({"id": id}),
            None => json!({"key": key}),
        };
        let text = text.trim().to_string();
        if !text.is_empty() {
            params[if kind == "explain" { "question" } else { "note" }] = json!(text);
        }
        if restore {
            params["restore"] = json!(true);
        }
        self.conn.request(method, params, move |s, reply| {
            match reply.and_then(|v| {
                serde_json::from_value::<DecisionExplainResult>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => {
                    if let Some(view) = s.decision.as_mut() {
                        view.rebrief = None;
                    }
                    s.on_explain(r.explain);
                }
                Err(e) => s.toast(Severity::Error, format!("Not asked: {e}")),
            }
        });
    }

    /// `y` / `Shift+Y` on a Home line: explain its brief, or its work item.
    pub fn explain_line(&mut self, line: &Line, restore: bool) {
        if !self.decision_view_on() {
            self.toast(Severity::Info, "This engine cannot explain items (no decision view).");
            return;
        }
        let brief =
            line.top.as_ref().and_then(|t| t.decision_id.clone()).and_then(|id| self.decisions.get(&id).cloned());
        let (path, agent) = match &brief {
            Some(d) => (Some(d.worktree_path.clone()), Some(d.agent.clone())),
            None => (line.place.clone(), line.top.as_ref().and_then(|t| t.agent_id.clone())),
        };
        if line.row.is_none() && brief.is_none() {
            self.toast(Severity::Info, "Nothing to explain: it is not a work item.");
            return;
        }
        match self.explain_route(path.as_deref(), agent.as_deref()) {
            Route::Restore(a) if !restore => {
                self.home.expanded = Some(line.key.clone());
                self.toast(Severity::Info, format!("Agent {a} is not running: ⇧Y restores it and asks."));
            }
            Route::Nobody => self.toast(
                Severity::Info,
                "Nobody can explain it: its agent is not running and no orchestrator runs. Open its terminal (o) or pass it (O).",
            ),
            route => {
                let restore = matches!(route, Route::Restore(_));
                self.ask_explain("explain", brief.map(|d| d.id), &line.key, String::new(), restore);
            }
        }
    }

    /// The decision view's keys (no text field has the keyboard).
    pub fn decision_key(&mut self, take: &impl Fn(Modifiers, Key) -> bool) -> bool {
        let Some(view) = self.decision.clone() else { return false };
        let d = self.decisions.get(&view.id).cloned();
        if take(Modifiers::NONE, Key::Escape) {
            if view.rebrief.is_some() || view.reverting.is_some() {
                if let Some(v) = self.decision.as_mut() {
                    v.rebrief = None;
                    v.reverting = None;
                }
            } else {
                self.close_decision();
            }
            return true;
        }
        const DIGITS: [Key; 4] = [Key::Num1, Key::Num2, Key::Num3, Key::Num4];
        if let Some(d) = &d
            && d.state == "open"
            && let Some(i) = DIGITS.iter().take(d.brief.options.len()).position(|k| take(Modifiers::NONE, *k))
        {
            let key = d.brief.options[i].key.clone();
            if let Some(v) = self.decision.as_mut() {
                v.choice = Some(key);
            }
            return true;
        }
        let tabs = d.as_ref().map_or(0, |d| d.brief.evidence.len());
        if tabs > 0 && take(Modifiers::NONE, Key::CloseBracket) {
            self.load_evidence((view.tab + 1) % tabs);
            return true;
        }
        if tabs > 0 && take(Modifiers::NONE, Key::OpenBracket) {
            self.load_evidence((view.tab + tabs - 1) % tabs);
            return true;
        }
        if take(Modifiers::CTRL, Key::Enter) {
            self.answer_decision();
            return true;
        }
        let Some(d) = d else { return take(Modifiers::NONE, Key::Questionmark) };
        let shift = take(Modifiers::SHIFT, Key::Y);
        if shift || take(Modifiers::NONE, Key::Y) {
            match self.explain_route(Some(&d.worktree_path), Some(&d.agent)) {
                Route::Restore(a) if !shift => {
                    self.toast(Severity::Info, format!("Agent {a} is not running: ⇧Y restores it and asks."))
                }
                Route::Nobody => {
                    self.toast(Severity::Info, "Nobody can explain it: open its terminal (o) or pass it (O).")
                }
                route => {
                    let restore = matches!(route, Route::Restore(_));
                    self.ask_explain("explain", Some(d.id.clone()), &d.key, view.question.clone(), restore);
                }
            }
            return true;
        }
        if take(Modifiers::NONE, Key::B) {
            if d.state == "open"
                && d.source.is_none()
                && let Some(v) = self.decision.as_mut()
            {
                v.rebrief = Some(v.rebrief.take().unwrap_or_default());
            }
            return true;
        }
        if take(Modifiers::NONE, Key::O) {
            self.open_decision_terminal(&d);
            return true;
        }
        if take(Modifiers::SHIFT, Key::U) {
            if d.revertible == Some(true)
                && let Some(v) = self.decision.as_mut()
            {
                v.reverting = Some(String::new());
            }
            return true;
        }
        false
    }

    fn open_decision_terminal(&mut self, d: &Decision) {
        self.decision = None;
        self.tabs.insert(d.worktree_path.clone(), d.agent.clone());
        self.selected = Some(d.worktree_path.clone());
        self.leave_home();
        self.focus_request = Some(crate::app::Focus::Agent);
    }
}

enum Do {
    Back,
    Choose(String),
    Comment(String),
    Answer,
    Tab(usize),
    Question(String),
    Explain(bool),
    RebriefOpen,
    RebriefNote(String),
    Rebrief(bool),
    RebriefCancel,
    Open(String),
    Pass,
    Terminal,
    Url(String),
    OpenDiff,
    RevertOpen,
    RevertEdit(String),
    Revert(String),
    RevertCancel,
}

impl App {
    pub(crate) fn decision_view(&mut self, ui: &mut Ui) {
        let s = &self.state;
        let t = s.theme;
        let now = unix_now();
        let Some(view) = s.decision.clone() else { return };
        let mut acts: Vec<Do> = vec![];
        let d = s.decisions.get(&view.id).cloned();
        let line = s.home.lines.get(&view.key).cloned();
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            if ui.button("← Home  (Esc)").clicked() {
                acts.push(Do::Back);
            }
            let name = line.as_ref().map_or(view.key.clone(), |l| l.name.clone());
            ui.label(RichText::new(format!("Decision {} · {name}", view.id)).strong().color(t.text));
            if let Some(d) = &d {
                let by = match d.source.as_deref() {
                    Some("auto_delegate") => "by the engine".to_string(),
                    _ => format!("brief by agent {}", d.agent),
                };
                ui.label(RichText::new(format!("{by} · {} ago", home::age(now - d.created_at))).small().color(t.muted));
            }
        });
        let Some(d) = d else {
            ui.label(RichText::new("Loading the brief…").color(t.muted));
            self.apply(acts);
            return;
        };
        let head = s.store.worktree(&d.worktree_path).map(|w| w.head.clone());
        if d.state == "open"
            && let Some(text) = outdated(&d, head.as_deref())
        {
            banner(ui, &t, t.yellow, &text);
        }
        match d.state.as_str() {
            "open" => {}
            "superseded" => {
                let by = d.superseded_by.clone().unwrap_or_default();
                ui.horizontal(|ui| {
                    banner(ui, &t, t.muted, &format!("Superseded by a new brief ({by})."));
                    if !by.is_empty() && ui.button(format!("Open {by}")).clicked() {
                        acts.push(Do::Open(by.clone()));
                    }
                });
            }
            "answered" | "resolved" if d.answered_by.as_deref() != Some("orchestrator") => {
                let mut text = format!("Answered: {}", d.choice().unwrap_or("?"));
                if let Some(answer) = &d.answer {
                    if answer.get("delivered_at").and_then(Value::as_f64).is_some() {
                        text.push_str(&format!(" · agent {} has it", d.agent));
                    } else if answer.get("delivery").is_some() {
                        text.push_str(&format!(
                            " · agent {} is gone: it gets it when restored (its notes have it)",
                            d.agent
                        ));
                    } else {
                        text.push_str(&format!(" · sent to agent {} once its turn is over", d.agent));
                    }
                }
                banner(ui, &t, t.green, &text);
            }
            "answered" | "resolved" => {}
            other => banner(ui, &t, t.muted, &format!("This brief is {other}.")),
        }
        if d.answered_by.as_deref() == Some("orchestrator") {
            ui.horizontal(|ui| {
                banner(ui, &t, t.cyan, &crate::home_view::decided_text(&d));
                if d.revertible == Some(true) && view.reverting.is_none() && ui.button("Revert…  (⇧U)").clicked() {
                    acts.push(Do::RevertOpen);
                }
                if d.reverted.is_some() {
                    ui.label(RichText::new("(final: reverted by you)").small().color(t.muted));
                }
            });
            if let Some(comment) = &view.reverting {
                let mut comment = comment.clone();
                ui.horizontal(|ui| {
                    let edit = ui.add(
                        egui::TextEdit::singleline(&mut comment)
                            .hint_text("comment for the agent (optional)")
                            .desired_width(360.0),
                    );
                    if edit.changed() {
                        acts.push(Do::RevertEdit(comment.clone()));
                    }
                    if ui.button("Revert").clicked() {
                        acts.push(Do::Revert(comment.clone()));
                    }
                    if ui.button("Cancel").clicked() {
                        acts.push(Do::RevertCancel);
                    }
                });
            }
        }
        ui.add_space(4.0);
        let route = s.explain_route(Some(&d.worktree_path), Some(&d.agent));
        let explain = s.explain_of("explain", &d.id).cloned();
        let rebrief = s.explain_of("rebrief", &d.id).cloned();
        let avail = ui.available_size();
        ui.horizontal_top(|ui| {
            let left_w = (avail.x * 0.42).clamp(340.0, 620.0);
            ui.allocate_ui_with_layout(Vec2::new(left_w, avail.y), Layout::top_down(Align::Min), |ui| {
                egui::ScrollArea::vertical().id_salt("decision-brief").auto_shrink([false, false]).show(ui, |ui| {
                    brief_ui(ui, &t, &d, &view, &mut acts);
                    ui.add_space(10.0);
                    ui.separator();
                    explain_ui(ui, &t, &d, &view, &route, explain.as_ref(), rebrief.as_ref(), &mut acts);
                });
            });
            ui.separator();
            ui.allocate_ui_with_layout(Vec2::new(ui.available_width(), avail.y), Layout::top_down(Align::Min), |ui| {
                evidence_ui(ui, &t, &d, &view, &mut acts);
                ui.add_space(8.0);
                ui.separator();
                ui.label(RichText::new("TIMELINE").small().strong().color(t.muted));
                egui::ScrollArea::vertical().id_salt("decision-timeline").auto_shrink([false, false]).show(ui, |ui| {
                    if view.timeline.is_empty() {
                        ui.label(RichText::new("Nothing on its timeline yet.").small().color(t.muted));
                    }
                    for e in view.timeline.iter().rev() {
                        let actor = if e.actor == "engine" { String::new() } else { format!(" · {}", e.actor) };
                        ui.label(
                            RichText::new(format!("{} ago  {}  {}{actor}", home::age(now - e.at), e.kind, e.text))
                                .small()
                                .color(if e.reference.as_deref() == Some(d.id.as_str()) { t.text } else { t.muted }),
                        );
                    }
                });
            });
        });
        self.apply(acts);
    }

    fn apply(&mut self, acts: Vec<Do>) {
        let s = &mut self.state;
        for act in acts {
            let Some(view) = s.decision.clone() else { return };
            let d = s.decisions.get(&view.id).cloned();
            match act {
                Do::Back => s.close_decision(),
                Do::Choose(k) => s.decision.as_mut().unwrap().choice = Some(k),
                Do::Comment(c) => s.decision.as_mut().unwrap().comment = c,
                Do::Answer => s.answer_decision(),
                Do::Tab(i) => s.load_evidence(i),
                Do::Question(q) => s.decision.as_mut().unwrap().question = q,
                Do::Explain(restore) => {
                    if let Some(d) = d {
                        s.ask_explain("explain", Some(d.id), &d.key, view.question.clone(), restore);
                    }
                }
                Do::RebriefOpen => s.decision.as_mut().unwrap().rebrief = Some(String::new()),
                Do::RebriefNote(n) => s.decision.as_mut().unwrap().rebrief = Some(n),
                Do::Rebrief(restore) => {
                    if let Some(d) = d {
                        s.ask_explain("rebrief", Some(d.id), &d.key, view.rebrief.clone().unwrap_or_default(), restore);
                    }
                }
                Do::RebriefCancel => s.decision.as_mut().unwrap().rebrief = None,
                Do::Open(id) => s.open_decision(&id, &view.key),
                Do::Pass => {
                    if let Some(line) = s.home.lines.get(&view.key).cloned() {
                        s.pass_line(&line);
                    }
                }
                Do::Terminal => {
                    if let Some(d) = d {
                        s.open_decision_terminal(&d);
                    }
                }
                Do::Url(url) => s.url_to_open = Some(url),
                Do::OpenDiff => {
                    if let Some(d) = d {
                        s.decision = None;
                        s.selected = Some(d.worktree_path.clone());
                        s.leave_home();
                        if !s.diff_open {
                            s.toggle_diff();
                        }
                    }
                }
                Do::RevertOpen => s.decision.as_mut().unwrap().reverting = Some(String::new()),
                Do::RevertEdit(c) => s.decision.as_mut().unwrap().reverting = Some(c),
                Do::Revert(c) => {
                    s.decision.as_mut().unwrap().reverting = None;
                    s.revert(view.id.clone(), c);
                }
                Do::RevertCancel => s.decision.as_mut().unwrap().reverting = None,
            }
        }
    }
}

fn banner(ui: &mut Ui, t: &Theme, color: Color32, text: &str) {
    egui::Frame::new()
        .fill(color.gamma_multiply(0.14))
        .stroke(egui::Stroke::new(1.0, color.gamma_multiply(0.6)))
        .corner_radius(4.0)
        .inner_margin(egui::Margin::symmetric(8, 4))
        .show(ui, |ui| {
            ui.label(RichText::new(text).color(t.text));
        });
}

fn heading(ui: &mut Ui, t: &Theme, text: &str) {
    ui.add_space(6.0);
    ui.label(RichText::new(text).small().strong().color(t.muted));
}

fn brief_ui(ui: &mut Ui, t: &Theme, d: &Decision, view: &DecisionView, acts: &mut Vec<Do>) {
    ui.label(RichText::new(&d.brief.question).size(16.0).strong().color(t.text));
    let open = d.state == "open";
    let rec = d.brief.recommendation.as_deref().unwrap_or("");
    let suggested = d.escalation.as_ref().and_then(|e| e["suggestion"].as_str());
    if let Some(e) = &d.escalation {
        let note = e["note"].as_str().unwrap_or("");
        let text = match suggested {
            Some(k) => format!("orchestrator suggests {k}: {note}"),
            None => note.to_string(),
        };
        ui.label(RichText::new(text).small().color(t.cyan));
    }
    heading(ui, t, "OPTIONS");
    if d.brief.options.is_empty() {
        // A plain question: the answer is free text.
        let mut answer = view.choice.clone().unwrap_or_default();
        let edit = ui.add_enabled(
            open,
            egui::TextEdit::singleline(&mut answer).hint_text("your answer").desired_width(f32::INFINITY),
        );
        if edit.changed() {
            acts.push(Do::Choose(answer));
        }
    }
    for (i, o) in d.brief.options.iter().enumerate() {
        let chosen = view.choice.as_deref().is_some_and(|c| c.eq_ignore_ascii_case(&o.key));
        let mut tags = vec![];
        if o.key.eq_ignore_ascii_case(rec) {
            tags.push("recommended");
        }
        if suggested.is_some_and(|s| s.eq_ignore_ascii_case(&o.key)) {
            tags.push("orchestrator suggests");
        }
        let mut text = format!("{}  {}", o.key, o.label);
        if !tags.is_empty() {
            text.push_str(&format!("   ({})", tags.join(", ")));
        }
        ui.horizontal(|ui| {
            let r = ui.add_enabled(open, egui::RadioButton::new(chosen, RichText::new(text).color(t.text)));
            if r.clicked() {
                acts.push(Do::Choose(o.key.clone()));
            }
            r.on_hover_text(format!("{}", i + 1));
        });
        if let Some(c) = o.consequence.as_deref().filter(|c| !c.is_empty()) {
            ui.indent(("consequence", i), |ui| {
                ui.label(RichText::new(c).small().color(t.muted));
            });
        }
    }
    if !d.brief.tried.is_empty() {
        heading(ui, t, "TRIED");
        ui.label(RichText::new(&d.brief.tried).color(t.text));
    }
    heading(ui, t, "IF WRONG");
    let reversible = d.brief.reversible.as_deref().unwrap_or("not given");
    let color = match reversible {
        "no" => t.red,
        "partly" => t.yellow,
        _ => t.text,
    };
    ui.label(RichText::new(format!("Reversible: {reversible}")).color(color));
    if !d.brief.impact_if_wrong.is_empty() {
        ui.label(RichText::new(&d.brief.impact_if_wrong).color(t.text));
    }
    if open {
        heading(ui, t, "COMMENT");
        let mut comment = view.comment.clone();
        let edit = ui.add(
            egui::TextEdit::multiline(&mut comment)
                .hint_text("for the agent, with your choice (optional)")
                .desired_rows(2)
                .desired_width(f32::INFINITY),
        );
        if edit.changed() {
            acts.push(Do::Comment(comment));
        }
        let ctrl_enter = edit.has_focus() && ui.input(|i| i.modifiers.ctrl && i.key_pressed(Key::Enter));
        ui.add_space(4.0);
        ui.horizontal(|ui| {
            let label = match &view.choice {
                Some(c) if !c.is_empty() => format!("Answer {c}  (Ctrl+Enter)"),
                _ => "Answer  (Ctrl+Enter)".into(),
            };
            let enabled = view.choice.as_deref().is_some_and(|c| !c.trim().is_empty()) && !view.sending;
            if ui.add_enabled(enabled, egui::Button::new(RichText::new(label).strong())).clicked()
                || (ctrl_enter && enabled)
            {
                acts.push(Do::Answer);
            }
            ui.label(RichText::new("sent when the agent's turn is over").small().color(t.muted));
        });
    }
}

#[allow(clippy::too_many_arguments)]
fn explain_ui(
    ui: &mut Ui,
    t: &Theme,
    d: &Decision,
    view: &DecisionView,
    route: &Route,
    explain: Option<&DecisionExplain>,
    rebrief: Option<&DecisionExplain>,
    acts: &mut Vec<Do>,
) {
    heading(ui, t, "EXPLAIN THIS");
    let mut question = view.question.clone();
    if ui
        .add(
            egui::TextEdit::singleline(&mut question)
                .hint_text("your question (optional)")
                .desired_width(f32::INFINITY),
        )
        .changed()
    {
        acts.push(Do::Question(question));
    }
    explain_buttons(ui, t, route, "explain", acts);
    if let Some(e) = explain {
        match (e.state.as_str(), &e.text) {
            ("answered", Some(text)) => {
                ui.label(RichText::new(explained_by(e)).small().strong().color(t.cyan));
                egui::Frame::new().fill(t.panel).inner_margin(egui::Margin::symmetric(8, 6)).corner_radius(4.0).show(
                    ui,
                    |ui| {
                        ui.set_width(ui.available_width());
                        ui.label(RichText::new(text).color(t.text));
                    },
                );
                if e.truncated == Some(true) {
                    ui.label(RichText::new("cut to 10 lines").small().color(t.muted));
                }
            }
            _ => {
                ui.label(RichText::new(asked_text(e)).small().color(t.muted));
            }
        }
    }
    if d.state != "open" || d.source.is_some() {
        return;
    }
    heading(ui, t, "NOT THE RIGHT QUESTION?");
    match &view.rebrief {
        None => {
            if let Some(r) = rebrief {
                let text = match &r.replaced_by {
                    Some(id) => format!("new brief {id}"),
                    None => asked_text(r),
                };
                ui.label(RichText::new(format!("Request a new brief: {text}")).small().color(t.muted));
            }
            if ui.add_enabled(*route != Route::Nobody, egui::Button::new("Request a new brief…  (b)")).clicked() {
                acts.push(Do::RebriefOpen);
            }
        }
        Some(note) => {
            let mut note = note.clone();
            if ui
                .add(
                    egui::TextEdit::multiline(&mut note)
                        .hint_text("what the new brief should cover (optional)")
                        .desired_rows(2)
                        .desired_width(f32::INFINITY),
                )
                .changed()
            {
                acts.push(Do::RebriefNote(note));
            }
            ui.horizontal(|ui| {
                explain_buttons(ui, t, route, "rebrief", acts);
                if ui.button("Cancel").clicked() {
                    acts.push(Do::RebriefCancel);
                }
            });
        }
    }
}

/// The chain's button(s): who answers decides the label; nobody disables it.
fn explain_buttons(ui: &mut Ui, t: &Theme, route: &Route, kind: &str, acts: &mut Vec<Do>) {
    let verb = if kind == "explain" { "Explain this" } else { "Ask for a new brief" };
    let act = |restore: bool| if kind == "explain" { Do::Explain(restore) } else { Do::Rebrief(restore) };
    ui.horizontal_wrapped(|ui| match route {
        Route::Owner(a) => {
            let label = if kind == "explain" { format!("{verb}  (y)") } else { verb.to_string() };
            if ui.button(label).on_hover_text(format!("agent {a} answers when its turn is over")).clicked() {
                acts.push(act(false));
            }
            ui.label(RichText::new(format!("agent {a}")).small().color(t.muted));
        }
        Route::Restore(a) => {
            let label = if kind == "explain" { "Restore agent and explain  (⇧Y)" } else { "Restore agent and ask" };
            if ui.button(label).on_hover_text("Starts its process again, with its notes").clicked() {
                acts.push(act(true));
            }
            ui.label(RichText::new(format!("agent {a} is not running")).small().color(t.muted));
        }
        Route::Orchestrator => {
            if ui.button(format!("{verb}  ({})", if kind == "explain" { "y" } else { "orchestrator" })).clicked() {
                acts.push(act(false));
            }
            ui.label(RichText::new("its agent is not running: the orchestrator answers").small().color(t.muted));
        }
        Route::Nobody => {
            ui.add_enabled(false, egui::Button::new(verb))
                .on_disabled_hover_text("Its agent is not running and cannot be restored, and no orchestrator runs");
            if ui.button("Open terminal").clicked() {
                acts.push(Do::Terminal);
            }
            if ui.button("Pass to orchestrator").clicked() {
                acts.push(Do::Pass);
            }
        }
    });
}

fn evidence_ui(ui: &mut Ui, t: &Theme, d: &Decision, view: &DecisionView, acts: &mut Vec<Do>) {
    ui.label(RichText::new("EVIDENCE").small().strong().color(t.muted));
    if d.brief.evidence.is_empty() {
        ui.label(RichText::new("No evidence with this brief.").color(t.muted));
        return;
    }
    ui.horizontal_wrapped(|ui| {
        for (i, e) in d.brief.evidence.iter().enumerate() {
            if ui.selectable_label(view.tab == i, tab_title(e)).clicked() {
                acts.push(Do::Tab(i));
            }
        }
        ui.label(RichText::new("] [ next / previous").small().color(t.muted));
    });
    let Some(e) = d.brief.evidence.get(view.tab) else { return };
    let height = (ui.available_height() * 0.62).max(160.0);
    egui::Frame::new().fill(t.surface).stroke(egui::Stroke::new(1.0, t.border)).inner_margin(6).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.set_height(height);
        let kind = e["kind"].as_str().unwrap_or("");
        if kind == "url" {
            let url = e["url"].as_str().unwrap_or("").to_string();
            ui.horizontal(|ui| {
                ui.label(RichText::new(&url).monospace().color(t.text));
                if ui.button("open").clicked() {
                    acts.push(Do::Url(url.clone()));
                }
            });
            return;
        }
        match view.evidence.get(&view.tab) {
            None | Some(Ev::Loading) => {
                ui.label(RichText::new("Reading…").color(t.muted));
            }
            Some(Ev::Failed(why)) => {
                ui.label(RichText::new(why).color(t.muted));
            }
            Some(Ev::Hunk(rows, note)) => {
                ui.horizontal(|ui| {
                    if let Some(note) = note {
                        ui.label(RichText::new(note).small().color(t.yellow));
                    }
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        if ui.small_button("Open in the diff view").clicked() {
                            acts.push(Do::OpenDiff);
                        }
                    });
                });
                hunk_ui(ui, t, rows);
            }
            Some(Ev::Text(r)) => {
                if let Some(note) = &r.note {
                    ui.label(RichText::new(note).small().color(t.muted));
                }
                lines_ui(ui, t, r, marked(e));
            }
        }
    });
}

/// The hunk's rows, drawn by the diff view's own cell painter.
fn hunk_ui(ui: &mut Ui, t: &Theme, rows: &[diff::Row]) {
    let font = FontId::monospace(12.0);
    egui::ScrollArea::both().id_salt("decision-hunk").auto_shrink([false, false]).show_rows(
        ui,
        crate::diff_view::ROW_H,
        rows.len(),
        |ui, range| {
            for row in &rows[range] {
                let (rect, _) = ui.allocate_exact_size(
                    Vec2::new(ui.available_width().max(600.0), crate::diff_view::ROW_H),
                    Sense::hover(),
                );
                crate::diff_view::draw_cell(ui.painter(), t, &font, rect, row, true, 0.0);
            }
        },
    );
}

fn lines_ui(ui: &mut Ui, t: &Theme, r: &DecisionEvidence, marked: Option<(u32, u32)>) {
    let start = r.start.unwrap_or(1);
    let text = r.text.clone().unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    egui::ScrollArea::both().id_salt("decision-lines").auto_shrink([false, false]).show_rows(
        ui,
        crate::diff_view::ROW_H,
        lines.len(),
        |ui, range| {
            for i in range {
                let n = start + i as u32;
                let hit = marked.is_some_and(|(a, b)| a <= n && n <= b);
                let mut rich =
                    RichText::new(format!("{n:>5}  {}", lines[i].replace('\t', "    "))).monospace().color(t.text);
                if hit {
                    rich = rich.background_color(t.accent.gamma_multiply(0.18));
                }
                ui.label(rich);
            }
        },
    );
}
