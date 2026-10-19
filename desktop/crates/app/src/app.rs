//! The app: state on the UI thread, fed by [`Msg`]s from the connection,
//! drawn each frame by egui (only when something changed: egui repaints
//! on input, and the reader thread asks for a repaint when a visible
//! terminal or the store changed).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::sync::mpsc::Receiver;
use std::time::{Duration, Instant};

use eframe::egui::{
    self, Align, Color32, CornerRadius, Id, Key, KeyboardShortcut, Layout, Modifiers, RichText, Sense, Stroke, Ui, Vec2,
};
use lazyagent_proto::EngineEvent;
use lazyagent_proto::types::{
    CreateWorktreeResult, OpenShellResult, RestorableAgent, RestorableList, SpawnResult, Worktree,
};
use serde_json::json;

use crate::conn::{Conn, Link, Msg};
use crate::fonts::{self, FontSet};
use crate::panes::{Pane, PaneKind};
use crate::render::Atlas;
use crate::store::{self, ORCHESTRATOR_KEY, Store};
use crate::term_view::{self, ViewState};
use crate::theme::{Theme, ThemeKind};

/// Hidden panes that keep their stream (P7), and for how long.
const KEEP_HIDDEN: usize = 3;
const HIDDEN_DETACH_AFTER: Duration = Duration::from_secs(30);
const TOAST_FOR: Duration = Duration::from_secs(6);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Sidebar,
    Agent,
    Shell,
}

/// Which main view is shown.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum View {
    Worktrees,
    Attention,
}

/// Unix time in seconds.
pub fn unix_now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Info,
    Warning,
    Error,
}

pub struct Toast {
    pub text: String,
    pub severity: Severity,
    pub at: Instant,
}

/// A custom create/remove command the engine runs (`job.*` events).
pub struct Job {
    pub id: String,
    pub title: String,
    pub command: String,
    pub output: String,
    /// `Some(code)` once finished (`None` inside: killed by a signal).
    pub finished: Option<Option<i32>>,
}

struct PendingLaunch {
    path: String,
    instruction: String,
    skip: bool,
    job: Option<String>,
}

/// Hooks a bench (or a test) runs at the end of each frame.
pub trait FrameHook {
    fn frame(&mut self, state: &mut State, ctx: &egui::Context);
}

pub struct State {
    pub conn: Arc<Conn>,
    pub link: Link,
    pub engine_version: Option<String>,
    pub store: Store,
    pub seeded: bool,
    /// A worktree path, or the orchestrator key.
    pub selected: Option<String>,
    /// The active agent tab per worktree.
    pub tabs: HashMap<String, String>,
    /// The shell session per worktree.
    pub shells: HashMap<String, String>,
    shell_requested: HashSet<String>,
    /// Shells that failed to open: not retried until the next connection.
    shell_failed: HashSet<String>,
    pub shell_hidden: HashSet<String>,
    pub panes: HashMap<String, Arc<Pane>>,
    pub views: HashMap<String, ViewState>,
    shown_at: HashMap<String, Instant>,
    pub toasts: Vec<Toast>,
    pub theme: Theme,
    pub focus: Focus,
    /// A focus asked for (a key, a spawn): applied when its target is drawn.
    pub focus_request: Option<Focus>,
    /// Focus this session's tab once it shows up (our own spawn).
    focus_session: Option<String>,
    pub fonts: FontSet,
    pub font_size: f32,
    pub atlas: Option<Atlas>,
    pub shell_split: f32,
    pub view: View,
    /// The selected worktree's diff replaces its agents and shell.
    pub diff_open: bool,
    pub diff: Option<crate::diff_view::DiffState>,
    pub comment_edit: Option<crate::diff_view::CommentEdit>,
    pub review: Option<crate::diff_view::ReviewForm>,
    pub pending_reviews: Vec<crate::diff_view::PendingReview>,
    /// Bumped when comments change (the diff rows carry them).
    pub comments_gen: u64,
    pub saved: crate::persist::Saved,
    saver: crate::persist::Saver,
    /// The attention queue, re-derived when its inputs change.
    pub attention: Vec<crate::attention::Item>,
    att_since: HashMap<String, f64>,
    att_key: (u64, usize, usize, u64, u64),
    marks_gen: u64,
    /// Agents that exited on their own (for the queue).
    pub exits: Vec<crate::attention::Exit>,
    pub att_selected: Option<String>,
    pub reply: String,
    notified: HashSet<String>,
    pub pr_panel: bool,
    /// Worktree → whether `get_pr_info` could fetch (false: no gh).
    pub pr_fetched: HashMap<String, bool>,
    /// `g` was pressed (a view switch follows: a / w).
    pending_g: Option<Instant>,
    pub dialog: Option<crate::dialogs::Dialog>,
    dialog_seq: u64,
    pub jobs: Vec<Job>,
    pub jobs_open: bool,
    pub restorable: Vec<RestorableAgent>,
    restore_hidden: bool,
    /// A worktree being created to launch an agent in, once listed.
    pending_launch: Option<PendingLaunch>,
    /// Select this worktree once it is listed (a new one).
    pending_select: Option<String>,
    /// A terminal had the keyboard last frame (single-letter keys are its).
    pub terminal_focused: bool,
    pub frames: u64,
    pub frame_started: Instant,
    /// eframe's CPU time of the previous frame (ui + tessellation + paint), seconds.
    pub last_frame_cpu: Option<f32>,
    pub pacer: Arc<crate::pacer::Pacer>,
    /// Frames in which a terminal mesh was rebuilt (benches).
    pub terminal_draws: u64,
}

impl State {
    pub fn new(conn: Arc<Conn>, fonts: FontSet, theme: Option<ThemeKind>) -> State {
        let saved_path = crate::persist::path(&conn.root, &conn.env);
        let mut saved = crate::persist::load(&saved_path);
        saved.marks.prune(unix_now());
        let theme =
            theme.unwrap_or(if saved.theme.as_deref() == Some("light") { ThemeKind::Light } else { ThemeKind::Dark });
        State {
            view: View::Worktrees,
            diff_open: false,
            diff: None,
            comment_edit: None,
            review: None,
            pending_reviews: vec![],
            comments_gen: 0,
            saved,
            saver: crate::persist::Saver::new(saved_path),
            attention: vec![],
            att_since: HashMap::new(),
            att_key: (u64::MAX, 0, 0, 0, 0),
            marks_gen: 0,
            exits: vec![],
            att_selected: None,
            reply: String::new(),
            notified: HashSet::new(),
            pr_panel: false,
            pr_fetched: HashMap::new(),
            pending_g: None,
            conn,
            link: Link::Connecting("Looking for the engine…".into()),
            engine_version: None,
            store: Store::default(),
            seeded: false,
            selected: None,
            tabs: HashMap::new(),
            shells: HashMap::new(),
            shell_requested: HashSet::new(),
            shell_failed: HashSet::new(),
            shell_hidden: HashSet::new(),
            panes: HashMap::new(),
            views: HashMap::new(),
            shown_at: HashMap::new(),
            toasts: vec![],
            theme: Theme::of(theme),
            focus: Focus::Agent,
            focus_request: Some(Focus::Agent),
            focus_session: None,
            fonts,
            font_size: fonts::font_size(),
            atlas: None,
            shell_split: 0.34,
            dialog: None,
            dialog_seq: 0,
            jobs: vec![],
            jobs_open: false,
            restorable: vec![],
            restore_hidden: false,
            pending_launch: None,
            pending_select: None,
            terminal_focused: false,
            frames: 0,
            frame_started: Instant::now(),
            last_frame_cpu: None,
            pacer: Arc::new(crate::pacer::Pacer::new(Duration::from_micros(16_000))),
            terminal_draws: 0,
        }
    }

    pub fn toast(&mut self, severity: Severity, text: impl Into<String>) {
        self.toasts.push(Toast { text: text.into(), severity, at: Instant::now() });
        if self.toasts.len() > 5 {
            self.toasts.remove(0);
        }
    }

    fn env_map(&self) -> serde_json::Map<String, serde_json::Value> {
        self.conn.env.vars().into_iter().map(|(k, v)| (k, json!(v))).collect()
    }

    /// The app's environment, for what it starts (D5).
    pub fn env_json(&self) -> serde_json::Value {
        serde_json::Value::Object(self.env_map())
    }

    pub fn open_dialog(&mut self, kind: crate::dialogs::Kind) {
        use crate::dialogs::{Dialog, Kind};
        let target = self.selected.clone().unwrap_or_default();
        if kind != Kind::Create && kind != Kind::Help && target.is_empty() {
            return;
        }
        if kind == Kind::Remove && target == ORCHESTRATOR_KEY {
            return;
        }
        self.dialog_seq += 1;
        let mut d = Dialog::new(kind, self.dialog_seq, target.clone());
        match kind {
            Kind::Create => {
                d.target = String::new();
                d.base = crate::dialogs::default_base(self.store.config.as_ref(), &self.store.worktrees);
            }
            Kind::Stop => {
                let Some(agent) = self
                    .tabs
                    .get(&target)
                    .cloned()
                    .or_else(|| self.store.agents(&target).first().map(|a| a.agent_id.clone()))
                else {
                    self.toast(Severity::Info, "No agent to stop here.");
                    return;
                };
                d.agent_id = Some(agent);
            }
            _ => {}
        }
        self.dialog = Some(d);
    }

    /// After a spawn answered: show and focus the new agent.
    pub fn spawned(&mut self, target: &str, v: &serde_json::Value) {
        if let Ok(r) = serde_json::from_value::<SpawnResult>(v.clone()) {
            let path = if target.is_empty() { ORCHESTRATOR_KEY.to_string() } else { target.to_string() };
            self.tabs.insert(path.clone(), r.agent_id.clone());
            self.selected = Some(path);
            self.focus_request = Some(Focus::Agent);
            self.focus_session = r.session_id;
        }
    }

    pub fn after_create(&mut self, r: CreateWorktreeResult, launch: Option<(String, bool)>) {
        if let Some(w) = &r.warning
            && r.job_id.is_none()
        {
            self.toast(Severity::Warning, w.clone());
        }
        if r.job_id.is_some() {
            self.jobs_open = true;
        } else {
            self.toast(Severity::Info, format!("Created worktree {}", store::basename(&r.path)));
        }
        self.pending_select = Some(r.path.clone());
        if let Some((instruction, skip)) = launch {
            self.pending_launch = Some(PendingLaunch { path: r.path, instruction, skip, job: r.job_id });
        }
        self.conn.notify("refresh_worktrees", json!({}));
    }

    pub fn after_remove(&mut self, path: &str, job: Option<String>) {
        if job.is_some() {
            self.jobs_open = true;
        } else {
            self.toast(Severity::Info, format!("Removed worktree {}", store::basename(path)));
        }
        if self.selected.as_deref() == Some(path) {
            self.selected = self.store.worktrees.iter().find(|w| w.is_main).map(|w| w.path.clone());
        }
    }

    /// Runs what waits for a worktree to show up (select it, launch in it).
    fn pending(&mut self) {
        if let Some(path) = self.pending_select.clone()
            && self.store.worktree(&path).is_some()
        {
            self.selected = Some(path);
            self.pending_select = None;
        }
        let Some(p) = &self.pending_launch else { return };
        if let Some(job) = &p.job
            && let Some(j) = self.jobs.iter().find(|j| &j.id == job)
            && let Some(code) = j.finished
            && code != Some(0)
        {
            self.toast(Severity::Error, "The create command failed; no agent launched (see the job log).");
            self.pending_launch = None;
            return;
        }
        let job_done = p.job.as_ref().is_none_or(|job| self.jobs.iter().any(|j| &j.id == job && j.finished.is_some()));
        if !job_done || self.store.worktree(&p.path).is_none() {
            return;
        }
        let p = self.pending_launch.take().unwrap();
        let params = json!({
            "worktree_path": p.path,
            "instruction": (!p.instruction.is_empty()).then_some(p.instruction),
            "skip_permissions": p.skip,
            "env": self.env_json(),
        });
        let path = p.path.clone();
        self.conn.clone().request("spawn_agent", params, move |s, reply| match reply {
            Ok(v) => s.spawned(&path, &v),
            Err(e) => s.toast(
                Severity::Error,
                format!("The worktree is there, but the agent did not start: {e}. Spawn again with s."),
            ),
        });
    }

    /// Re-derives the queue when its inputs changed (or every 30 s, for
    /// snoozes running out), and notifies new items while the window is in
    /// the background.
    pub fn refresh_attention(&mut self, window_focused: bool) {
        let now = unix_now();
        let ended = self.jobs.iter().filter(|j| j.finished.is_some()).count();
        let key = (self.store.version, self.exits.len(), ended, self.marks_gen, (now / 30.0) as u64);
        if key == self.att_key || !self.seeded {
            return;
        }
        self.att_key = key;
        let jobs: Vec<crate::attention::JobEnd> = self
            .jobs
            .iter()
            .filter_map(|j| {
                j.finished.map(|code| crate::attention::JobEnd {
                    id: j.id.clone(),
                    title: j.title.clone(),
                    exit_code: code,
                })
            })
            .collect();
        self.attention =
            crate::attention::items(&self.store, &self.exits, &jobs, &mut self.att_since, &self.saved.marks, now);
        let current: HashSet<String> = self.attention.iter().map(|i| i.occurrence.clone()).collect();
        for item in &self.attention {
            if self.notified.contains(&item.occurrence) {
                continue;
            }
            self.notified.insert(item.occurrence.clone());
            if !window_focused && self.saved.notify_on(item.kind.setting()) {
                let place = if item.key.is_empty() { "a job".to_string() } else { self.place_name(&item.key) };
                let urgent = item.kind.rank() <= 2;
                crate::notify::send(
                    &format!("{}: {}", item.kind.title(), item.label),
                    &format!("{place} — {}", item.detail),
                    urgent,
                );
            }
        }
        self.notified.retain(|o| current.contains(o));
    }

    pub fn touch_saved(&mut self) {
        self.saver.touch();
    }

    pub fn move_attention(&mut self, delta: i32) {
        if self.attention.is_empty() {
            return;
        }
        let at = self.att_selected.as_ref().and_then(|id| self.attention.iter().position(|i| &i.id == id)).unwrap_or(0)
            as i32;
        let next = (at + delta).clamp(0, self.attention.len() as i32 - 1) as usize;
        self.att_selected = Some(self.attention[next].id.clone());
    }

    pub fn dismiss(&mut self, item: &crate::attention::Item) {
        self.saved.marks.dismissed.insert(item.occurrence.clone(), unix_now());
        if item.kind == crate::attention::Kind::Exited {
            self.exits.retain(|e| Some(e) != item.exit.as_ref());
        }
        self.marks_gen += 1;
        self.saver.touch();
    }

    pub fn snooze(&mut self, item: &crate::attention::Item, secs: f64) {
        self.saved.marks.snoozed.insert(item.id.clone(), unix_now() + secs);
        self.marks_gen += 1;
        self.saver.touch();
    }

    /// `send_agent_input` (the engine frames, submits and confirms it).
    pub fn send_reply(&mut self, key: &str, agent_id: &str, text: String) {
        let params = json!({"worktree_path": key, "agent_id": agent_id, "text": text});
        self.conn.request("send_agent_input", params, |s, reply| match reply {
            Ok(v) => s.toast(Severity::Info, crate::attention_view::delivery_text(v)),
            Err(e) => s.toast(Severity::Error, format!("Not sent: {e}")),
        });
    }

    pub fn fetch_pr(&mut self, path: &str, refresh: bool) {
        if !refresh && self.pr_fetched.contains_key(path) {
            return;
        }
        // Marked as asked, so a frame does not ask again while it is in flight.
        self.pr_fetched.entry(path.to_string()).or_insert(true);
        let p = path.to_string();
        self.conn.request(
            "get_pr_info",
            json!({"worktree_path": path, "refresh": refresh}),
            move |s, reply| match reply.ok().and_then(crate::attention_view::pr_result) {
                Some(r) => {
                    s.pr_fetched.insert(p.clone(), r.fetched);
                    if r.fetched {
                        s.store.pr_info.insert(p, r.pr_info);
                        s.store.version += 1;
                    }
                }
                None => {
                    s.pr_fetched.insert(p, false);
                }
            },
        );
    }

    fn fetch_restorable(&mut self) {
        self.conn.request("list_restorable", json!({}), |s, reply| {
            // VALIDATION_ERROR: an engine without a manifest (nothing to offer).
            if let Ok(list) = reply.and_then(|v| {
                serde_json::from_value::<RestorableList>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                s.restorable = list.agents;
            }
        });
    }

    pub fn restore(&mut self, all: bool) {
        let params = if all { json!({"env": self.env_json()}) } else { json!({"ids": [], "env": self.env_json()}) };
        self.restore_hidden = true;
        self.conn.request("restore_agents", params, |s, reply| {
            if let Err(e) = reply {
                s.toast(Severity::Error, format!("Restore failed: {e}"));
            }
            s.fetch_restorable();
        });
    }

    pub fn dismiss_restorable(&mut self) {
        self.restore_hidden = true;
        self.conn.request("dismiss_restorable", json!({}), |s, _| s.restorable.clear());
    }
    pub fn pane(&mut self, session_id: &str, kind: PaneKind, ctx: &egui::Context) -> Arc<Pane> {
        let palette = self.theme.palette();
        self.panes
            .entry(session_id.to_string())
            .or_insert_with(|| Pane::new(session_id.to_string(), kind, palette, ctx.clone(), self.pacer.clone()))
            .clone()
    }

    fn drop_pane(&mut self, session_id: &str) {
        if let Some(p) = self.panes.remove(session_id) {
            p.detach();
        }
        self.views.remove(session_id);
        self.shown_at.remove(session_id);
    }

    pub fn handle(&mut self, msg: Msg) {
        match msg {
            Msg::Link(l) => self.link = l,
            Msg::Connected { hello, engine_changed, started } => {
                self.shell_failed.clear();
                if engine_changed {
                    // A new engine process: the old sessions died with the old one.
                    for id in self.panes.keys().cloned().collect::<Vec<_>>() {
                        self.drop_pane(&id);
                    }
                    self.shells.clear();
                    self.shell_requested.clear();
                } else {
                    // Same engine: every stream resumes from its last byte.
                    for p in self.panes.values() {
                        let mut i = p.inner.lock().unwrap();
                        i.stream_id = None;
                        i.attaching = false;
                        i.claim_pending = false;
                    }
                }
                self.restore_hidden = false;
                self.fetch_restorable();
                if started {
                    self.toast(
                        Severity::Info,
                        "Started an engine for this repo. Agents keep running when you close the app.",
                    );
                }
                self.engine_version = Some(hello.engine_version);
            }
            Msg::Seed(state, config) => {
                self.store.seed(*state);
                self.store.config = config;
                self.seeded = true;
                self.after_worktrees_changed();
            }
            Msg::Event(ev) => self.on_event(ev),
            Msg::Apply(f) => f(self),
        }
    }

    fn after_worktrees_changed(&mut self) {
        let paths: Vec<String> = self.store.worktrees.iter().map(|w| w.path.clone()).collect();
        // Poll git/PR for every worktree, as the web host does (Q12).
        self.conn.notify("set_interest", json!({"worktree_paths": paths}));
        let valid = |s: &String| s == ORCHESTRATOR_KEY || paths.contains(s);
        if !self.selected.as_ref().is_some_and(valid) {
            self.selected = self
                .store
                .worktrees
                .iter()
                .find(|w| !w.agents.is_empty())
                .or_else(|| self.store.worktrees.iter().find(|w| w.is_main))
                .or(self.store.worktrees.first())
                .map(|w| w.path.clone());
        }
        let gone: Vec<String> = self.shells.keys().filter(|p| !paths.contains(p)).cloned().collect();
        for p in gone {
            if let Some(id) = self.shells.remove(&p) {
                self.drop_pane(&id);
            }
        }
    }

    fn on_event(&mut self, ev: EngineEvent) {
        // A PR going red is news (toast, notification), not just a badge.
        if let EngineEvent::PrInfoUpdated(p) = &ev {
            let before = self.store.pr_info.get(&p.path).cloned().flatten();
            let was_red = before.as_ref().and_then(store::ci_summary) == Some("fail");
            if let Some(pr) = &p.pr_info
                && !was_red
                && store::ci_summary(pr) == Some("fail")
                && pr.state.eq_ignore_ascii_case("OPEN")
            {
                let text = format!("CI is failing on {} (#{} {})", store::basename(&p.path), pr.number, pr.title);
                self.toast(Severity::Error, text.clone());
                if self.saved.notify_on("ci") {
                    crate::notify::send("CI failing", &text, false);
                }
            }
        }
        self.store.apply(&ev);
        match ev {
            EngineEvent::WorktreesChanged(_) => self.after_worktrees_changed(),
            EngineEvent::AgentSpawned(s) if s.kind != "shell" => {
                let ours = self.focus_session.as_deref() == Some(s.session_id.as_str());
                if ours || !self.tabs.contains_key(&s.worktree_path) {
                    self.tabs.insert(s.worktree_path.clone(), s.agent_id.clone());
                }
                if ours {
                    self.focus_session = None;
                    self.selected = Some(s.worktree_path.clone());
                    self.focus_request = Some(Focus::Agent);
                }
            }
            EngineEvent::AgentExited(e) => {
                let name = self.place_name(&e.worktree_path);
                if e.stopped != Some(true) {
                    let label = self
                        .store
                        .agents(&e.worktree_path)
                        .iter()
                        .find(|a| a.agent_id == e.agent_id)
                        .map(store::agent_label)
                        .unwrap_or_else(|| format!("Agent {}", e.agent_id));
                    self.exits.push(crate::attention::Exit {
                        key: e.worktree_path.clone(),
                        agent_id: e.agent_id.clone(),
                        label,
                        exit_code: e.exit_code,
                        at: unix_now(),
                    });
                    if self.exits.len() > 50 {
                        self.exits.remove(0);
                    }
                    let code = e.exit_code.map(|c| format!(" (exit code {c})")).unwrap_or_default();
                    self.toast(Severity::Warning, format!("Agent {} in {name} exited{code}", e.agent_id));
                }
                self.drop_pane(&e.session_id);
                if self.tabs.get(&e.worktree_path) == Some(&e.agent_id) {
                    let next = self.store.agents(&e.worktree_path).first().map(|a| a.agent_id.clone());
                    match next {
                        Some(n) => self.tabs.insert(e.worktree_path.clone(), n),
                        None => self.tabs.remove(&e.worktree_path),
                    };
                }
            }
            EngineEvent::EngineNotice(n) => {
                let sev = match n.severity.as_str() {
                    "error" => Severity::Error,
                    "warning" => Severity::Warning,
                    _ => Severity::Info,
                };
                self.toast(sev, n.message);
            }
            EngineEvent::EngineShutdown(s) => {
                self.toast(Severity::Warning, format!("The engine is shutting down ({}).", s.reason));
            }
            EngineEvent::JobStarted(j) => {
                self.jobs.push(Job {
                    id: j.job_id,
                    title: j.title,
                    command: j.command,
                    output: String::new(),
                    finished: None,
                });
                if self.jobs.len() > 20 {
                    self.jobs.remove(0);
                }
                self.jobs_open = true;
            }
            EngineEvent::JobOutput(o) => {
                if let Some(j) = self.jobs.iter_mut().find(|j| j.id == o.job_id) {
                    j.output.push_str(&o.text);
                    // Keep the tail: a job log is for the last lines.
                    if j.output.len() > 256 * 1024 {
                        let cut = j.output.len() - 192 * 1024;
                        let cut = (cut..j.output.len()).find(|i| j.output.is_char_boundary(*i)).unwrap_or(cut);
                        j.output.drain(..cut);
                    }
                }
            }
            EngineEvent::JobFinished(f) => {
                if let Some(j) = self.jobs.iter_mut().find(|j| j.id == f.job_id) {
                    j.finished = Some(f.exit_code);
                    let title = j.title.clone();
                    match f.exit_code {
                        Some(0) => self.toast(Severity::Info, format!("{title}: done")),
                        Some(c) => {
                            self.toast(Severity::Error, format!("{title}: failed (exit code {c}); see the job log"))
                        }
                        None => self.toast(Severity::Error, format!("{title}: killed")),
                    }
                }
            }
            EngineEvent::EngineRestored(r) => {
                if !r.restored.is_empty() {
                    self.toast(Severity::Info, format!("Restored {} agent(s)", r.restored.len()));
                }
                for f in &r.failed {
                    self.toast(Severity::Error, format!("Could not restore {}: {}", f.id, f.reason));
                }
                self.fetch_restorable();
            }
            // An offered agent started again here (or was dismissed): the
            // banner follows without a fetch.
            EngineEvent::RestorableChanged(list) => self.restorable = list.agents,
            _ => {}
        }
    }

    pub fn place_name(&self, path: &str) -> String {
        if path == ORCHESTRATOR_KEY { "the orchestrator".into() } else { store::basename(path).to_string() }
    }

    fn selected_worktree(&self) -> Option<&Worktree> {
        self.selected.as_deref().and_then(|p| self.store.worktree(p))
    }

    /// Moves the sidebar selection by `delta` rows (orchestrator first).
    fn move_selection(&mut self, delta: i32) {
        let mut rows: Vec<String> = vec![ORCHESTRATOR_KEY.to_string()];
        rows.extend(self.store.worktrees.iter().map(|w| w.path.clone()));
        let at = self.selected.as_ref().and_then(|s| rows.iter().position(|r| r == s)).unwrap_or(0) as i32;
        let next = (at + delta).clamp(0, rows.len() as i32 - 1) as usize;
        self.selected = Some(rows[next].clone());
    }

    fn cycle_tab(&mut self, delta: i32) {
        let Some(path) = self.selected.clone() else { return };
        let agents = self.store.agents(&path);
        if agents.is_empty() {
            return;
        }
        let at = self.tabs.get(&path).and_then(|t| agents.iter().position(|a| &a.agent_id == t)).unwrap_or(0) as i32;
        let next = (at + delta).rem_euclid(agents.len() as i32) as usize;
        let id = agents[next].agent_id.clone();
        self.tabs.insert(path, id);
        self.focus_request = Some(Focus::Agent);
    }

    pub fn spawn_agent(&mut self, worktree: Option<String>) {
        let mut params = json!({"skip_permissions": false, "env": self.env_map()});
        match &worktree {
            Some(p) => params["worktree_path"] = json!(p),
            None => params["kind"] = json!("orchestrator"),
        }
        let target = worktree.unwrap_or_default();
        self.conn.request("spawn_agent", params, move |s, reply| match reply {
            Ok(v) => s.spawned(&target, &v),
            Err(e) => s.toast(Severity::Error, format!("Could not spawn an agent: {e}")),
        });
    }

    fn ensure_shell(&mut self, path: &str) {
        if self.link != Link::Connected
            || self.shells.contains_key(path)
            || self.shell_failed.contains(path)
            || !self.shell_requested.insert(path.to_string())
        {
            return;
        }
        let p = path.to_string();
        self.conn.request("open_shell", json!({"worktree_path": path, "env": self.env_map()}), move |s, reply| {
            s.shell_requested.remove(&p);
            match reply.and_then(|v| {
                serde_json::from_value::<OpenShellResult>(v)
                    .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
            }) {
                Ok(r) => {
                    s.shells.insert(p, r.session_id);
                }
                Err(e) => {
                    s.shell_failed.insert(p);
                    s.toast(Severity::Error, format!("Could not open the shell: {e}"));
                }
            }
        });
    }

    /// Attach what is shown; keep the few most recently hidden streams
    /// (P7), detach the rest.
    fn stream_policy(&mut self) {
        let now = Instant::now();
        let client = self.conn.client();
        let mut hidden: Vec<(Instant, String)> = vec![];
        for (id, pane) in &self.panes {
            if pane.visible.load(Ordering::Relaxed) {
                self.shown_at.insert(id.clone(), now);
                if let Some(c) = &client {
                    pane.attach(c);
                }
            } else if pane.attached() {
                hidden.push((self.shown_at.get(id).copied().unwrap_or(now), id.clone()));
            }
        }
        hidden.sort_by_key(|h| std::cmp::Reverse(h.0));
        for (rank, (at, id)) in hidden.into_iter().enumerate() {
            if rank >= KEEP_HIDDEN || now.duration_since(at) > HIDDEN_DETACH_AFTER {
                self.panes[&id].detach();
            }
        }
    }
}

pub struct App {
    pub state: State,
    rx: Receiver<Msg>,
    hook: Option<Box<dyn FrameHook>>,
    sidebar_id: Id,
    pub(crate) title: String,
    fonts_set: bool,
}

impl App {
    pub fn new(state: State, rx: Receiver<Msg>, hook: Option<Box<dyn FrameHook>>) -> App {
        App { state, rx, hook, sidebar_id: Id::unique("lazyagent-sidebar"), title: String::new(), fonts_set: false }
    }

    fn global_keys(&mut self, ctx: &egui::Context) {
        let s = &mut self.state;
        let take = |mods: Modifiers, key: Key| ctx.input_mut(|i| i.consume_shortcut(&KeyboardShortcut::new(mods, key)));
        // Ctrl+D is EOF inside a terminal: there, Ctrl+Shift+D (Q6).
        if take(Modifiers::CTRL | Modifiers::SHIFT, Key::D)
            || (!s.terminal_focused && !ctx.text_edit_focused() && take(Modifiers::CTRL, Key::D))
        {
            s.view = View::Worktrees;
            s.toggle_diff();
        }
        if take(Modifiers::CTRL, Key::Period) {
            s.pr_panel = !s.pr_panel;
        }
        if take(Modifiers::CTRL, Key::K) {
            s.focus_request = Some(Focus::Sidebar);
        }
        // Ctrl+J / Ctrl+L go to the terminal when it already has the keyboard
        // (newline in Claude Code, clear in a shell).
        if s.focus != Focus::Agent && take(Modifiers::CTRL, Key::J) {
            s.focus_request = Some(Focus::Agent);
        }
        if s.focus != Focus::Shell && take(Modifiers::CTRL, Key::L) {
            if let Some(p) = s.selected.clone() {
                s.shell_hidden.remove(&p);
            }
            s.focus_request = Some(Focus::Shell);
        }
        if take(Modifiers::ALT, Key::CloseBracket) || take(Modifiers::ALT, Key::N) {
            s.cycle_tab(1);
        }
        if take(Modifiers::ALT, Key::OpenBracket) || take(Modifiers::ALT, Key::P) {
            s.cycle_tab(-1);
        }
        let zoom = |s: &mut State, size: f32| {
            s.font_size = size.clamp(7.0, 40.0);
            s.atlas = None;
        };
        if take(Modifiers::CTRL, Key::Equals)
            || take(Modifiers::CTRL | Modifiers::SHIFT, Key::Equals)
            || take(Modifiers::CTRL, Key::Plus)
        {
            zoom(s, s.font_size + 1.0);
        }
        if take(Modifiers::CTRL, Key::Minus) {
            zoom(s, s.font_size - 1.0);
        }
        if take(Modifiers::CTRL, Key::Num0) {
            zoom(s, fonts::font_size());
        }
        if take(Modifiers::ALT, Key::G)
            && let Some(p) = s.selected.clone().filter(|p| p != ORCHESTRATOR_KEY)
        {
            s.conn.notify("refresh_git_status", json!({"paths": [p]}));
        }
        // Single letters belong to a terminal that has the keyboard, and to
        // text fields; otherwise they are the TUI's action keys.
        let outside = s.dialog.is_none() && !s.terminal_focused && !ctx.text_edit_focused();
        let g_recent = s.pending_g.is_some_and(|t| t.elapsed() < Duration::from_secs(1));
        if outside {
            use crate::dialogs::Kind;
            if take(Modifiers::NONE, Key::S) {
                s.open_dialog(Kind::Spawn);
            } else if take(Modifiers::NONE, Key::X) {
                s.open_dialog(Kind::Stop);
            } else if take(Modifiers::NONE, Key::C) {
                s.open_dialog(Kind::Create);
            } else if take(Modifiers::NONE, Key::D) {
                s.open_dialog(Kind::Remove);
            } else if take(Modifiers::SHIFT, Key::R) {
                s.restore(true);
            } else if g_recent && take(Modifiers::NONE, Key::A) {
                (s.view, s.pending_g) = (View::Attention, None);
            } else if g_recent && take(Modifiers::NONE, Key::W) {
                (s.view, s.pending_g) = (View::Worktrees, None);
            } else if take(Modifiers::NONE, Key::G) {
                // Its second key may arrive in the same frame.
                if take(Modifiers::NONE, Key::A) {
                    s.view = View::Attention;
                } else if take(Modifiers::NONE, Key::W) {
                    s.view = View::Worktrees;
                } else {
                    s.pending_g = Some(Instant::now());
                }
            } else if s.view == View::Attention
                && (take(Modifiers::NONE, Key::J) || take(Modifiers::NONE, Key::ArrowDown))
            {
                s.move_attention(1);
            } else if s.view == View::Attention
                && (take(Modifiers::NONE, Key::K) || take(Modifiers::NONE, Key::ArrowUp))
            {
                s.move_attention(-1);
            } else if s.view == View::Attention && take(Modifiers::NONE, Key::Enter) {
                s.focus_request = Some(Focus::Agent);
            } else if take(Modifiers::NONE, Key::R) {
                s.conn.notify("refresh_worktrees", json!({}));
            } else if take(Modifiers::SHIFT, Key::Questionmark) || take(Modifiers::NONE, Key::Questionmark) {
                s.open_dialog(Kind::Help);
            }
        }
        if s.focus == Focus::Sidebar && ctx.memory(|m| m.has_focus(self.sidebar_id)) {
            if take(Modifiers::NONE, Key::J) || take(Modifiers::NONE, Key::ArrowDown) {
                s.move_selection(1);
            }
            if take(Modifiers::NONE, Key::K) || take(Modifiers::NONE, Key::ArrowUp) {
                s.move_selection(-1);
            }
            if take(Modifiers::NONE, Key::Enter) {
                s.focus_request = Some(Focus::Agent);
            }
        }
    }

    fn top_bar(&mut self, ui: &mut Ui) {
        let s = &mut self.state;
        let t = s.theme;
        ui.horizontal_centered(|ui| {
            ui.label(RichText::new("lazyagent").strong().color(t.accent));
            let home = std::env::var("HOME").unwrap_or_default();
            let root = s.conn.root.clone();
            let full =
                if !home.is_empty() && root.starts_with(&home) { format!("~{}", &root[home.len()..]) } else { root };
            // The repo's name; the path on hover (a long one pushed the engine pill away).
            ui.label(RichText::new(store::basename(&s.conn.root)).color(t.text).monospace()).on_hover_text(full);
            let (text, color) = match &s.link {
                Link::Connected => ("connected".to_string(), t.green),
                Link::Connecting(m) | Link::Reconnecting(m) => (m.clone(), t.yellow),
                Link::Stopped => ("stopped".to_string(), t.red),
                Link::Failed(_) => ("no engine".to_string(), t.red),
            };
            egui::Frame::new()
                .stroke(Stroke::new(1.0, color))
                .corner_radius(CornerRadius::same(9))
                .inner_margin(egui::Margin::symmetric(8, 1))
                .show(ui, |ui| {
                    ui.label(RichText::new(format!("engine ● {text}")).small().color(color));
                });
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let icon = "◐";
                if ui.button(icon).on_hover_text("Light / dark theme").clicked() {
                    let kind = if t.kind == ThemeKind::Dark { ThemeKind::Light } else { ThemeKind::Dark };
                    s.saved.theme = Some(if kind == ThemeKind::Light { "light" } else { "dark" }.into());
                    s.saver.touch();
                    s.theme = Theme::of(kind);
                    s.theme.apply(ui.ctx());
                    let palette = s.theme.palette();
                    for p in s.panes.values() {
                        p.inner.lock().unwrap().term.set_palette(palette.clone());
                    }
                }
                ui.menu_button("Notifications ▾", |ui| {
                    ui.label(RichText::new("Notify me (while the window is in the background) when an agent…").small());
                    for (key, label) in crate::attention::SETTINGS {
                        let mut on = s.saved.notify_on(key);
                        if ui.checkbox(&mut on, *label).changed() {
                            s.saved.notify.insert(key.to_string(), on);
                            s.saver.touch();
                        }
                    }
                });
                if let Some(v) = &s.engine_version {
                    ui.label(RichText::new(format!("engine {v}")).small().color(t.muted));
                }
                ui.add_space(12.0);
                let n = s.attention.len();
                let att = if n > 0 { format!("Attention ({n})") } else { "Attention".to_string() };
                if ui
                    .selectable_label(
                        s.view == View::Attention,
                        RichText::new(att).color(if n > 0 { t.yellow } else { t.text }),
                    )
                    .on_hover_text("g a")
                    .clicked()
                {
                    s.view = View::Attention;
                }
                if ui.selectable_label(s.view == View::Worktrees, "Worktrees").on_hover_text("g w").clicked() {
                    s.view = View::Worktrees;
                }
            });
        });
    }

    fn sidebar(&mut self, ui: &mut Ui) {
        let sidebar_id = self.sidebar_id;
        let s = &mut self.state;
        let t = s.theme;
        let focus_here = s.focus_request == Some(Focus::Sidebar);
        let area = ui.interact(ui.max_rect(), sidebar_id, Sense::focusable_noninteractive());
        if focus_here {
            area.request_focus();
            s.focus = Focus::Sidebar;
            s.focus_request = None;
        }
        // Otherwise egui moves the focus on ↑/↓ to the nearest focusable
        // widget that way (a row below the middle of a long list) and j/k/↑/↓
        // stop after one row.
        let arrows = egui::EventFilter { vertical_arrows: true, horizontal_arrows: true, ..Default::default() };
        ui.memory_mut(|m| m.set_focus_lock_filter(sidebar_id, arrows));
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.label(RichText::new("Worktrees").strong());
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("↻").on_hover_text("Refresh the worktree list (r)").clicked() {
                    s.conn.notify("refresh_worktrees", json!({}));
                }
                if ui.button("+ New").on_hover_text("Create a worktree (c)").clicked() {
                    s.open_dialog(crate::dialogs::Kind::Create);
                }
                if ui.button("?").on_hover_text("Keys").clicked() {
                    s.open_dialog(crate::dialogs::Kind::Help);
                }
            });
        });
        ui.separator();
        let mut clicked: Option<(String, Option<String>)> = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            let selected = s.selected.clone();
            // Orchestrator.
            let orch_selected = selected.as_deref() == Some(ORCHESTRATOR_KEY);
            let resp = row(ui, orch_selected, &t, |ui| {
                ui.label(RichText::new("◆ Orchestrator").strong().color(t.accent));
                for a in &s.store.orchestrator {
                    agent_line(ui, &t, &store::agent_label(a), &a.status, false);
                }
            });
            if resp.clicked() {
                clicked = Some((ORCHESTRATOR_KEY.to_string(), None));
            }
            for w in &s.store.worktrees {
                let is_sel = selected.as_deref() == Some(w.path.as_str());
                let active_tab = s.tabs.get(&w.path);
                let resp = row(ui, is_sel, &t, |ui| {
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(store::basename(&w.path)).strong());
                        let branch =
                            w.branch.clone().unwrap_or_else(|| format!("detached {}", &w.head[..w.head.len().min(7)]));
                        ui.add(egui::Label::new(RichText::new(branch).color(t.cyan).small()).truncate());
                    });
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 6.0;
                        let g = w.git_status.unwrap_or_default();
                        if w.git_status.is_none() {
                            ui.label(RichText::new("…").small().color(t.muted));
                        } else if g.dirty_count == 0 && g.ahead == 0 && g.behind == 0 {
                            ui.label(RichText::new("clean").small().color(t.muted));
                        }
                        if g.dirty_count > 0 {
                            ui.label(RichText::new(format!("●{}", g.dirty_count)).small().color(t.yellow));
                        }
                        if g.ahead > 0 {
                            ui.label(RichText::new(format!("↑{}", g.ahead)).small().color(t.green));
                        }
                        if g.behind > 0 {
                            ui.label(RichText::new(format!("↓{}", g.behind)).small().color(t.red));
                        }
                        if let Some(Some(pr)) = s.store.pr_info.get(&w.path) {
                            let (color, state) = match pr.state.as_str() {
                                "MERGED" => (t.purple, " merged"),
                                "CLOSED" => (t.muted, " closed"),
                                _ => (t.green, ""),
                            };
                            ui.label(RichText::new(format!("#{}{state}", pr.number)).small().color(color));
                            if let Some(ci) = store::ci_summary(pr) {
                                let (c, txt) = match ci {
                                    "fail" => (t.red, "CI ✖"),
                                    "pending" => (t.yellow, "CI …"),
                                    _ => (t.green, "CI ✔"),
                                };
                                ui.label(RichText::new(txt).small().color(c));
                            }
                        }
                    });
                    for a in &w.agents {
                        let active = is_sel && active_tab == Some(&a.agent_id);
                        let r = agent_line(ui, &t, &store::agent_label(a), &a.status, active);
                        if r.clicked() {
                            clicked = Some((w.path.clone(), Some(a.agent_id.clone())));
                        }
                    }
                });
                if resp.clicked() && clicked.is_none() {
                    clicked = Some((w.path.clone(), None));
                }
            }
        });
        if let Some((path, agent)) = clicked {
            s.selected = Some(path.clone());
            match agent {
                Some(a) => {
                    s.tabs.insert(path, a);
                    s.focus_request = Some(Focus::Agent);
                }
                None => {
                    s.focus_request = Some(Focus::Sidebar);
                }
            }
        }
    }

    fn footer(&mut self, ui: &mut Ui) {
        let s = &self.state;
        ui.horizontal(|ui| {
            ui.label(
                RichText::new(format!("{} worktrees · {} agents", s.store.worktrees.len(), s.store.agent_count()))
                    .small()
                    .color(s.theme.muted),
            );
        });
    }

    fn center(&mut self, ui: &mut Ui, ctx: &egui::Context) {
        let t = self.state.theme;
        if !self.state.seeded {
            self.link_overlay(ui);
            return;
        }
        let Some(selected) = self.state.selected.clone() else {
            ui.centered_and_justified(|ui| ui.label(RichText::new("No worktree").color(t.muted)));
            return;
        };
        let orchestrator = selected == ORCHESTRATOR_KEY;
        self.link_banner(ui);
        self.restore_banner(ui);
        // Header: the buttons first, right to left, so the names truncate
        // into what they leave instead of running under them.
        ui.add_space(4.0);
        ui.horizontal(|ui| {
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if !orchestrator {
                    let hidden = self.state.shell_hidden.contains(&selected);
                    if ui
                        .button(if hidden { "Shell ▸" } else { "Shell ▾" })
                        .on_hover_text("Show or hide the shell (Ctrl+L)")
                        .clicked()
                    {
                        if hidden {
                            self.state.shell_hidden.remove(&selected);
                        } else {
                            self.state.shell_hidden.insert(selected.clone());
                        }
                    }
                }
                if !orchestrator
                    && ui.selectable_label(self.state.pr_panel, "PR").on_hover_text("PR and CI (Ctrl+.)").clicked()
                {
                    self.state.pr_panel = !self.state.pr_panel;
                }
                if !orchestrator
                    && ui
                        .selectable_label(self.state.diff_open, "Diff")
                        .on_hover_text("The worktree's changes (Ctrl+D outside a terminal, Ctrl+Shift+D)")
                        .clicked()
                {
                    self.state.toggle_diff();
                }
                if ui.button("+ Agent").on_hover_text("Spawn an agent here (s)").clicked() {
                    self.state.open_dialog(crate::dialogs::Kind::Spawn);
                }
                if !orchestrator {
                    if ui.button("Remove").on_hover_text("Remove this worktree (d)").clicked() {
                        self.state.open_dialog(crate::dialogs::Kind::Remove);
                    }
                    if !self.state.store.agents(&selected).is_empty()
                        && ui.button("Stop").on_hover_text("Stop the agent of the active tab (x)").clicked()
                    {
                        self.state.open_dialog(crate::dialogs::Kind::Stop);
                    }
                }
                ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                    if orchestrator {
                        ui.label(RichText::new("Orchestrator").strong().size(15.0).color(t.accent));
                    } else if let Some(w) = self.state.selected_worktree() {
                        ui.add(
                            egui::Label::new(RichText::new(store::basename(&w.path)).strong().size(15.0)).truncate(),
                        );
                        if let Some(b) = &w.branch {
                            // Most of the room, not all: the subject keeps some.
                            let label = egui::Label::new(RichText::new(b).color(t.cyan).monospace()).truncate();
                            let room = ui.available_width() * 0.6;
                            ui.scope(|ui| {
                                ui.set_max_width(room);
                                ui.add(label);
                            });
                        }
                        if let Some(g) = self.state.store.git.get(&w.path)
                            && let Some(subject) = &g.last_commit_subject
                            && ui.available_width() > 60.0
                        {
                            ui.add(
                                egui::Label::new(RichText::new(format!("last: \"{subject}\"")).color(t.muted).small())
                                    .truncate(),
                            );
                        }
                    }
                });
            });
        });
        if self.state.diff_open {
            if self.state.diff.as_ref().is_some_and(|d| d.path == selected) {
                self.diff_view(ui);
                return;
            }
            // Another worktree was selected: its agents, not the old diff.
            self.state.diff_open = false;
        }
        // Tabs.
        let agents: Vec<(String, String, String, Option<String>)> = self
            .state
            .store
            .agents(&selected)
            .iter()
            .map(|a| (a.agent_id.clone(), store::agent_label(a), a.status.clone(), a.session_id.clone()))
            .collect();
        if agents.is_empty() {
            self.state.tabs.remove(&selected);
        } else if !self.state.tabs.get(&selected).is_some_and(|t| agents.iter().any(|a| &a.0 == t)) {
            self.state.tabs.insert(selected.clone(), agents[0].0.clone());
        }
        let active = self.state.tabs.get(&selected).cloned();
        ui.horizontal(|ui| {
            for (id, label, status, _) in &agents {
                let is_active = active.as_deref() == Some(id.as_str());
                let r = tab(ui, &t, label, status, is_active);
                if r.clicked() {
                    self.state.tabs.insert(selected.clone(), id.clone());
                    self.state.focus_request = Some(Focus::Agent);
                }
            }
        });
        ui.add_space(2.0);

        let body = ui.available_rect_before_wrap();
        let show_shell = !orchestrator && !self.state.shell_hidden.contains(&selected);
        let split = if show_shell { (body.height() * (1.0 - self.state.shell_split)).round() } else { body.height() };
        let top = egui::Rect::from_min_size(body.min, Vec2::new(body.width(), split));
        let agent_session = active.and_then(|a| agents.iter().find(|x| x.0 == a).and_then(|x| x.3.clone()));
        let mut top_ui = ui.new_child(egui::UiBuilder::new().max_rect(top).id_salt("agent-area"));
        match agent_session {
            Some(session) => self.terminal(&mut top_ui, &session, PaneKind::Agent, Focus::Agent, ctx),
            None => {
                top_ui.vertical_centered(|ui| {
                    ui.add_space(top.height() / 2.0 - 40.0);
                    ui.label(
                        RichText::new(if orchestrator { "No orchestrator running" } else { "No agent here" })
                            .color(t.muted),
                    );
                    ui.add_space(6.0);
                    if ui.button(if orchestrator { "Start the orchestrator" } else { "Spawn an agent" }).clicked() {
                        self.state.open_dialog(crate::dialogs::Kind::Spawn);
                    }
                });
                if self.state.focus_request == Some(Focus::Agent) && self.state.focus != Focus::Sidebar {
                    // Nothing to focus: the shell takes the keyboard instead.
                    self.state.focus_request = if show_shell { Some(Focus::Shell) } else { None };
                }
            }
        }
        if show_shell {
            let handle =
                egui::Rect::from_min_size(egui::pos2(body.min.x, body.min.y + split), Vec2::new(body.width(), 22.0));
            let resp = ui.interact(handle, Id::unique("shell-split"), Sense::drag());
            if resp.dragged() {
                let dy = resp.drag_delta().y / body.height().max(1.0);
                self.state.shell_split = (self.state.shell_split - dy).clamp(0.12, 0.85);
            }
            if resp.hovered() || resp.dragged() {
                ctx.set_cursor_icon(egui::CursorIcon::ResizeVertical);
            }
            let painter = ui.painter_at(handle);
            painter.rect_filled(handle, 0.0, t.panel);
            painter.text(
                handle.left_center() + Vec2::new(8.0, 0.0),
                egui::Align2::LEFT_CENTER,
                "Shell  bash -l",
                egui::FontId::proportional(11.5),
                t.muted,
            );
            let shell_rect = egui::Rect::from_min_max(egui::pos2(body.min.x, handle.max.y), body.max);
            let mut shell_ui = ui.new_child(egui::UiBuilder::new().max_rect(shell_rect).id_salt("shell-area"));
            self.state.ensure_shell(&selected);
            match self.state.shells.get(&selected).cloned() {
                Some(session) => {
                    let closed =
                        self.state.panes.get(&session).is_some_and(|p| p.inner.lock().unwrap().closed.is_some());
                    if closed {
                        shell_ui.vertical_centered(|ui| {
                            ui.add_space(20.0);
                            ui.label(RichText::new("The shell ended.").color(t.muted));
                            if ui.button("Open a new shell").clicked() {
                                self.state.shells.remove(&selected);
                                self.state.drop_pane(&session);
                            }
                        });
                    } else {
                        self.terminal(&mut shell_ui, &session, PaneKind::Shell, Focus::Shell, ctx);
                    }
                }
                None => {
                    shell_ui.label(RichText::new("Opening the shell…").color(t.muted));
                }
            }
        }
        ui.allocate_rect(body, Sense::hover());
    }

    pub(crate) fn terminal(&mut self, ui: &mut Ui, session: &str, kind: PaneKind, role: Focus, ctx: &egui::Context) {
        let pane = self.state.pane(session, kind, ctx);
        let ppp = ctx.pixels_per_point();
        let atlas_ok = self.state.atlas.as_ref().is_some_and(|a| a.px == self.state.font_size * ppp);
        if !atlas_ok {
            self.state.atlas = Some(Atlas::new(self.state.fonts.clone(), self.state.font_size * ppp));
        }
        let focus_now = self.state.focus_request == Some(role);
        if focus_now {
            self.state.focus_request = None;
        }
        let window_focused = ctx.input(|i| i.viewport().focused.unwrap_or(true));
        let client = self.state.conn.client();
        let view = self.state.views.entry(session.to_string()).or_default();
        let atlas = self.state.atlas.as_mut().unwrap();
        let out =
            term_view::show(ui, &pane, view, atlas, client.as_ref(), &self.state.theme, focus_now, window_focused);
        if out.response.has_focus() {
            self.state.focus = role;
            self.state.terminal_focused = true;
        }
        if out.drew {
            self.state.terminal_draws += 1;
        }
    }

    /// Agents an earlier engine ran (the restart manifest), offered back.
    fn restore_banner(&mut self, ui: &mut Ui) {
        let n = self.state.restorable.len();
        if n == 0 || self.state.restore_hidden || self.state.link != Link::Connected {
            return;
        }
        let t = self.state.theme;
        egui::Frame::new()
            .fill(t.panel)
            .stroke(Stroke::new(1.0, t.primary))
            .corner_radius(CornerRadius::same(4))
            .inner_margin(egui::Margin::symmetric(10, 6))
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    let names: Vec<String> = self
                        .state
                        .restorable
                        .iter()
                        .take(4)
                        .map(|a| {
                            let label = if !a.label.is_empty() {
                                a.label.clone()
                            } else if a.kind == "orchestrator" {
                                "Orchestrator".to_string()
                            } else {
                                format!("Agent {}", a.agent_id)
                            };
                            format!("{label} ({})", self.state.place_name(&a.worktree_path))
                        })
                        .collect();
                    ui.add(
                        egui::Label::new(
                            RichText::new(format!(
                                "{n} agent(s) from an earlier session can be brought back: {}",
                                names.join(", ")
                            ))
                            .color(t.text),
                        )
                        .truncate(),
                    );
                    if ui.button("Restore all (Shift+R)").clicked() {
                        self.state.restore(true);
                    }
                    if ui.button("Dismiss").clicked() {
                        self.state.dismiss_restorable();
                    }
                    if ui.button("Later").clicked() {
                        self.state.restore_hidden = true;
                    }
                });
            });
    }

    /// The output of custom create/remove commands (`job.*`).
    fn job_drawer(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let mut open = self.state.jobs_open;
        ui.horizontal(|ui| {
            ui.label(RichText::new("Jobs").strong());
            if let Some(j) = self.state.jobs.last() {
                let (txt, c) = match j.finished {
                    None => ("running".to_string(), t.yellow),
                    Some(Some(0)) => ("done".to_string(), t.green),
                    Some(Some(code)) => (format!("failed ({code})"), t.red),
                    Some(None) => ("killed".to_string(), t.red),
                };
                ui.label(RichText::new(format!("{}: {txt}", j.title)).color(c));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.button("✖").on_hover_text("Close the job log").clicked() {
                    open = false;
                }
            });
        });
        egui::ScrollArea::vertical().stick_to_bottom(true).auto_shrink([false, false]).show(ui, |ui| {
            for j in &self.state.jobs {
                ui.label(RichText::new(format!("$ {}", j.command)).monospace().color(t.muted));
                ui.label(RichText::new(&j.output).monospace());
            }
        });
        self.state.jobs_open = open;
    }

    /// While the engine is not reachable, say so above the (stale) view.
    fn link_banner(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let (text, color, start) = match &self.state.link {
            Link::Connected => return,
            Link::Stopped => (
                "The engine was stopped (lazyagent engine stop): what is shown is no longer live.".to_string(),
                t.red,
                true,
            ),
            Link::Failed(why) => (format!("No engine: {why}"), t.red, true),
            Link::Connecting(m) | Link::Reconnecting(m) => (m.clone(), t.yellow, false),
        };
        egui::Frame::new()
            .fill(t.panel)
            .stroke(Stroke::new(1.0, color))
            .corner_radius(CornerRadius::same(4))
            .inner_margin(egui::Margin::symmetric(10, 6))
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    if !start {
                        ui.spinner();
                    }
                    ui.add(egui::Label::new(RichText::new(text).color(color)).truncate());
                    if start && ui.button("Start the engine").clicked() {
                        self.state.conn.request_start();
                    }
                });
            });
    }

    fn link_overlay(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        ui.vertical_centered(|ui| {
            ui.add_space(ui.available_height() / 3.0);
            match self.state.link.clone() {
                Link::Connecting(m) | Link::Reconnecting(m) => {
                    ui.spinner();
                    ui.label(RichText::new(m).color(t.muted));
                }
                Link::Stopped => {
                    ui.label(RichText::new("The engine was stopped (lazyagent engine stop).").color(t.text));
                    if ui.button("Start the engine").clicked() {
                        self.state.conn.request_start();
                    }
                }
                Link::Failed(why) => {
                    ui.label(RichText::new("No engine for this repo").strong().color(t.red));
                    ui.label(RichText::new(why).color(t.muted));
                    if ui.button("Try again").clicked() {
                        self.state.conn.request_start();
                    }
                }
                Link::Connected => {
                    ui.spinner();
                }
            }
        });
    }

    fn toasts(&mut self, ctx: &egui::Context) {
        let now = Instant::now();
        self.state.toasts.retain(|t| now.duration_since(t.at) < TOAST_FOR);
        if let Some(first) = self.state.toasts.first() {
            ctx.request_repaint_after(TOAST_FOR.saturating_sub(now.duration_since(first.at)));
        }
        let t = self.state.theme;
        egui::Area::new(Id::unique("toasts"))
            .order(egui::Order::Foreground)
            // Bottom right, above the reply box: at the top they covered the
            // header's buttons and the PR panel.
            .anchor(egui::Align2::RIGHT_BOTTOM, egui::vec2(-16.0, -120.0))
            .show(ctx, |ui| {
                ui.set_width(360.0);
                let mut dismiss = None;
                for (i, toast) in self.state.toasts.iter().enumerate() {
                    let bar = match toast.severity {
                        Severity::Info => t.primary,
                        Severity::Warning => t.yellow,
                        Severity::Error => t.red,
                    };
                    let r = egui::Frame::new()
                        .fill(t.panel)
                        .stroke(Stroke::new(1.0, t.border))
                        .corner_radius(CornerRadius::same(6))
                        .inner_margin(egui::Margin::symmetric(10, 8))
                        .show(ui, |ui| {
                            ui.horizontal(|ui| {
                                let (rect, _) = ui.allocate_exact_size(Vec2::new(3.0, 18.0), Sense::hover());
                                ui.painter().rect_filled(rect, 1.0, bar);
                                ui.add(egui::Label::new(RichText::new(&toast.text).color(t.text)).wrap());
                            });
                        })
                        .response
                        .interact(Sense::click());
                    if r.clicked() {
                        dismiss = Some(i);
                    }
                    ui.add_space(6.0);
                }
                if let Some(i) = dismiss {
                    self.state.toasts.remove(i);
                }
            });
    }
}

fn row<R>(ui: &mut Ui, selected: bool, t: &Theme, body: impl FnOnce(&mut Ui) -> R) -> egui::Response {
    let fill = if selected { t.current_bg } else { Color32::TRANSPARENT };
    let resp = egui::Frame::new()
        .fill(fill)
        .inner_margin(egui::Margin { left: 10, right: 8, top: 5, bottom: 5 })
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            body(ui)
        })
        .response;
    if selected {
        let r = resp.rect;
        ui.painter().rect_filled(egui::Rect::from_min_size(r.min, Vec2::new(3.0, r.height())), 0.0, t.accent);
    }
    resp.interact(Sense::click())
}

fn agent_line(ui: &mut Ui, t: &Theme, label: &str, status: &str, active: bool) -> egui::Response {
    let resp = ui
        .horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(Vec2::new(10.0, 14.0), Sense::hover());
            ui.painter().circle_filled(rect.center(), 4.0, t.tone(store::tone(status)));
            ui.label(RichText::new(label).color(if active { t.text } else { t.text.gamma_multiply(0.85) }));
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.label(RichText::new(store::status_text(status)).small().color(t.tone(store::tone(status))));
            });
        })
        .response;
    if active {
        ui.painter().rect_stroke(resp.rect.expand(1.0), 4.0, Stroke::new(1.0, t.accent), egui::StrokeKind::Outside);
    }
    resp.interact(Sense::click())
}

fn tab(ui: &mut Ui, t: &Theme, label: &str, status: &str, active: bool) -> egui::Response {
    let resp = egui::Frame::new()
        .inner_margin(egui::Margin::symmetric(10, 5))
        .show(ui, |ui| {
            ui.horizontal(|ui| {
                let (rect, _) = ui.allocate_exact_size(Vec2::new(8.0, 12.0), Sense::hover());
                ui.painter().circle_filled(rect.center(), 3.5, t.tone(store::tone(status)));
                ui.label(RichText::new(label).color(if active { t.text } else { t.muted }));
            });
        })
        .response
        .interact(Sense::click());
    if active {
        let r = resp.rect;
        ui.painter().rect_filled(egui::Rect::from_min_max(egui::pos2(r.min.x, r.max.y - 2.0), r.max), 0.0, t.accent);
    }
    resp
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut Ui, frame: &mut eframe::Frame) {
        self.state.last_frame_cpu = frame.info().cpu_usage;
        self.draw(ui);
    }

    fn on_exit(&mut self) {
        self.state.saver.flush(&self.state.saved);
        self.state.conn.shutdown();
    }
}

impl App {
    /// One frame: drain the connection's messages, then lay out and draw.
    pub fn draw(&mut self, ui: &mut Ui) {
        let ctx = ui.ctx().clone();
        // Fonts apply from the next pass: install them, draw from then on.
        if !self.fonts_set {
            self.fonts_set = true;
            ctx.set_fonts(crate::fonts::egui_definitions(&self.state.fonts));
            ctx.request_repaint();
            return;
        }
        while let Ok(msg) = self.rx.try_recv() {
            self.state.handle(msg);
        }
        self.state.frame_started = Instant::now();
        self.state.frames += 1;
        if crate::panes::debug() {
            eprintln!("[desktop] frame {} causes {:?}", self.state.frames, ctx.repaint_causes());
        }
        for p in self.state.panes.values() {
            p.visible.store(false, Ordering::Relaxed);
        }
        self.global_keys(&ctx);
        // A viewport command asks for a repaint: send the title only on change.
        self.state.refresh_attention(ctx.input(|i| i.viewport().focused.unwrap_or(true)));
        if let Some(wait) = self.state.saver.tick(&self.state.saved) {
            ctx.request_repaint_after(wait);
        }
        let n = self.state.attention.len();
        let badge = if n > 0 { format!("({n}) ") } else { String::new() };
        let title = format!("{badge}lazyagent — {}", store::basename(&self.state.conn.root));
        if self.title != title {
            ctx.send_viewport_cmd(egui::ViewportCommand::Title(title.clone()));
            self.title = title;
        }
        let t = self.state.theme;
        egui::Panel::top("top")
            .exact_size(34.0)
            .frame(egui::Frame::new().fill(t.bg).inner_margin(egui::Margin::symmetric(10, 0)))
            .show(ui, |ui| self.top_bar(ui));
        egui::Panel::left("sidebar")
            .resizable(true)
            .default_size(290.0)
            .size_range(200.0..=520.0)
            .frame(egui::Frame::new().fill(t.surface).stroke(Stroke::new(1.0, t.border)))
            .show(ui, |ui| {
                egui::Panel::bottom("sidebar-footer")
                    .frame(egui::Frame::new().inner_margin(egui::Margin::symmetric(10, 4)))
                    .show(ui, |ui| self.footer(ui));
                self.sidebar(ui);
            });
        if self.state.jobs_open && !self.state.jobs.is_empty() {
            egui::Panel::bottom("jobs")
                .resizable(true)
                .default_size(160.0)
                .frame(egui::Frame::new().fill(t.surface).inner_margin(egui::Margin::symmetric(10, 6)))
                .show(ui, |ui| self.job_drawer(ui));
        }
        if self.state.pr_panel && self.state.view == View::Worktrees {
            egui::Panel::right("pr-panel")
                .resizable(true)
                .default_size(320.0)
                .frame(egui::Frame::new().fill(t.surface).inner_margin(egui::Margin::symmetric(10, 8)))
                .show(ui, |ui| self.pr_panel(ui));
        }
        self.state.terminal_focused = false;
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(t.bg).inner_margin(egui::Margin::symmetric(8, 0)))
            .show(ui, |ui| match self.state.view {
                View::Worktrees => self.center(ui, &ctx),
                View::Attention if !self.state.seeded => self.link_overlay(ui),
                View::Attention => {
                    self.link_banner(ui);
                    ui.add_space(4.0);
                    self.attention_view(ui, &ctx);
                }
            });
        crate::dialogs::show(&ctx, &mut self.state);
        self.diff_dialogs(&ctx);
        self.state.pending_reviews();
        self.state.pending();
        self.toasts(&ctx);
        self.state.stream_policy();
        if let Some(mut hook) = self.hook.take() {
            hook.frame(&mut self.state, &ctx);
            self.hook = Some(hook);
        }
    }
}
