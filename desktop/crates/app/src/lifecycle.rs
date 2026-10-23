//! Updating and restarting from the app (docs/design/desktop-update-restart-plan.md):
//! Help → Check for updates (`lazyagent update check/apply`), File → Restart
//! (the app; optionally the engine, through `lazyagent engine restart`,
//! restoring the agents it stopped), and About.
//!
//! The order of a restart is the point: the app saves its state, ends its
//! connection (so it neither reconnects to nor starts an engine), the CLI
//! stops and starts the engine and restores, and only then the app replaces
//! itself (`exec`) with a fresh launch, which reports the outcome in a toast.

use std::ffi::OsString;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use eframe::egui::{self, RichText};
use lazyagent_proto::discovery::{self, Starter};
use serde::Deserialize;
use serde_json::Value;

use crate::app::{Severity, State};
use crate::conn::Msg;
use crate::store;

/// Where a relaunched app finds what the restart did.
pub const REPORT_ENV: &str = "LAZYAGENT_DESKTOP_RESTART_REPORT";
/// The commit this binary was built from (build.rs), `+dirty` when
/// `desktop/` had changes; empty outside a checkout.
pub const BUILD_COMMIT: &str = env!("LAZYAGENT_BUILD_COMMIT");
const AUTO_FIRST: Duration = Duration::from_secs(10);
const AUTO_EVERY: Duration = Duration::from_secs(6 * 3600);
/// App options a relaunch keeps (with their value).
const KEPT_OPTIONS: &[&str] = &["--theme"];
/// Agent statuses mid-turn: a restart cuts the turn (the conversation
/// itself comes back).
const BUSY: &[&str] = &["running", "waiting_for_approval", "possibly_hanged"];

/// How the app was started, before it changed its own environment
/// (`gpu::hide_offload_nvidia`): what a relaunch runs with.
pub struct Start {
    pub env: Vec<(OsString, OsString)>,
    pub args: Vec<String>,
}

static START: OnceLock<Start> = OnceLock::new();

/// Call first in `main`, while single-threaded.
pub fn remember_start() {
    let _ = START.set(Start { env: std::env::vars_os().collect(), args: std::env::args().skip(1).collect() });
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
pub struct Commit {
    pub sha: String,
    #[serde(default)]
    pub subject: String,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Deserialize)]
pub struct Changes {
    #[serde(default)]
    pub python: bool,
    #[serde(default)]
    pub lock: bool,
    #[serde(default)]
    pub desktop: bool,
}

/// `lazyagent update check --json`.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
pub struct UpdateInfo {
    pub mode: String,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub root: Option<String>,
    #[serde(default)]
    pub branch: Option<String>,
    #[serde(default)]
    pub upstream: Option<String>,
    #[serde(default)]
    pub latest: Option<String>,
    #[serde(default)]
    pub command: Option<Vec<String>>,
    #[serde(default)]
    pub behind: u32,
    #[serde(default)]
    pub commits: Vec<Commit>,
    #[serde(default)]
    pub changes: Changes,
    #[serde(default)]
    pub collisions: Vec<String>,
    #[serde(default)]
    pub update_available: bool,
    #[serde(default)]
    pub can_apply: bool,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub fetch_error: Option<String>,
    #[serde(default)]
    pub engine_behind: Option<bool>,
    #[serde(default)]
    pub app_behind: Option<bool>,
}

impl UpdateInfo {
    /// Something to act on: new code upstream, or code already here that
    /// the engine or the app does not run yet.
    pub fn actionable(&self) -> bool {
        self.update_available || self.engine_behind == Some(true) || self.app_behind == Some(true)
    }
}

/// `{"result": …}` of `lazyagent update apply --json`.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
pub struct ApplyResult {
    pub ok: bool,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub files: Vec<String>,
    #[serde(default)]
    pub steps: Vec<String>,
    #[serde(default)]
    pub restart: Option<RestartWanted>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Deserialize)]
pub struct RestartWanted {
    #[serde(default)]
    pub app: bool,
    #[serde(default)]
    pub engine: bool,
}

/// After `lazyagent update apply`: the restart sheet to offer, if any. Only
/// a result that allows the app's restart offers one (a build that failed
/// would relaunch the binary that runs now).
pub fn restart_after(applied: &ApplyResult) -> Option<Sheet> {
    let changed = applied.steps.iter().any(|st| matches!(st.as_str(), "merge" | "upgrade" | "build"));
    let restart = applied.restart.filter(|r| r.app)?;
    changed.then_some(Sheet::Restart { engine: restart.engine, restore: true, after_update: true })
}

/// Development mode, nothing to pull, but this app was built from older
/// code: the update is a rebuild.
pub fn rebuild_wanted(i: &UpdateInfo) -> bool {
    i.mode == "development" && !i.update_available && i.app_behind == Some(true)
}

#[derive(Debug, Clone, PartialEq)]
pub enum Phase {
    Idle,
    /// `quiet`: an automatic check (no dialog unless it finds something).
    Checking {
        quiet: bool,
    },
    Applying {
        step: String,
        line: String,
    },
    Restarting {
        step: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sheet {
    Update,
    Restart { engine: bool, restore: bool, after_update: bool },
    About,
}

pub struct Lifecycle {
    pub info: Option<UpdateInfo>,
    pub check_error: Option<String>,
    pub applied: Option<ApplyResult>,
    pub phase: Phase,
    pub sheet: Option<Sheet>,
    next_auto: Option<Instant>,
    /// What the relaunch would have run (tests replace the exec).
    pub relaunched: Option<(PathBuf, Vec<String>)>,
}

impl Default for Lifecycle {
    fn default() -> Lifecycle {
        Lifecycle {
            info: None,
            check_error: None,
            applied: None,
            phase: Phase::Idle,
            sheet: None,
            next_auto: None,
            relaunched: None,
        }
    }
}

/// One agent the engine's restart would stop.
#[derive(Debug, Clone, PartialEq)]
pub struct Stopping {
    pub name: String,
    pub place: String,
    pub status: String,
    pub busy: bool,
}

/// Every agent the engine runs, busy ones first.
pub fn stopping(store: &crate::store::Store) -> Vec<Stopping> {
    let mut out: Vec<Stopping> =
        store
            .orchestrator
            .iter()
            .map(|a| ("orchestrator".to_string(), "comes back as the orchestrator".to_string(), a))
            .chain(store.worktrees.iter().flat_map(|w| {
                w.agents.iter().map(|a| (store::agent_label(a), store::basename(&w.path).to_string(), a))
            }))
            .map(|(name, place, a)| Stopping {
                name,
                place,
                status: a.status.clone(),
                busy: BUSY.contains(&a.status.as_str()),
            })
            .collect();
    out.sort_by_key(|s| !s.busy);
    out
}

/// The command a relaunch runs: the same lookup as a launch (`lazyagent
/// desktop`, so a freshly installed binary is the one that runs), with the
/// environment the app was started with.
pub fn relaunch_command(
    start: &Start,
    root: &str,
    current_exe: Option<PathBuf>,
    report: Option<&Path>,
) -> (PathBuf, Vec<String>, Vec<(OsString, OsString)>) {
    let get = |name: &str| {
        start
            .env
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.to_string_lossy().into_owned())
            .filter(|v| !v.is_empty())
    };
    let mut options = vec![];
    let mut it = start.args.iter();
    while let Some(a) = it.next() {
        if KEPT_OPTIONS.contains(&a.as_str())
            && let Some(v) = it.next()
        {
            options.extend([a.clone(), v.clone()]);
        }
    }
    let path_env = discovery::Env::default().with("PATH", get("PATH").as_deref());
    let (program, mut args) = if let Some(py) = get(discovery::PYTHON_ENV) {
        (PathBuf::from(py), vec!["-m".into(), "lazyagent".into(), "desktop".into()])
    } else if let Some(bin) = discovery::which("lazyagent", &path_env) {
        (bin, vec!["desktop".into()])
    } else {
        let exe = current_exe.unwrap_or_else(|| PathBuf::from("lazyagent-desktop"));
        // A replaced binary reads as "<path> (deleted)" in /proc/self/exe.
        let exe = exe.to_string_lossy().trim_end_matches(" (deleted)").to_string();
        (PathBuf::from(exe), vec![])
    };
    args.push(root.to_string());
    args.extend(options);
    let mut env: Vec<(OsString, OsString)> = start.env.iter().filter(|(k, _)| k != REPORT_ENV).cloned().collect();
    if let Some(r) = report {
        env.push((REPORT_ENV.into(), r.as_os_str().to_owned()));
    }
    (program, args, env)
}

/// What a relaunched app says about the restart that started it (once).
pub fn take_report() -> Option<(Severity, String)> {
    static TAKEN: OnceLock<()> = OnceLock::new();
    TAKEN.set(()).ok()?;
    let path = std::env::var_os(REPORT_ENV)?;
    let text = std::fs::read_to_string(&path).ok();
    let _ = std::fs::remove_file(&path);
    report_text(&serde_json::from_str(&text?).ok()?)
}

/// The toast for a restart report (`{"engine", "result", "error"}`).
pub fn report_text(report: &Value) -> Option<(Severity, String)> {
    if !report["engine"].as_bool().unwrap_or(false) {
        return Some((Severity::Info, "Restarted.".into()));
    }
    if let Some(e) = report["error"].as_str().or(report["result"]["error"].as_str()) {
        return Some((Severity::Error, format!("The engine restart failed: {e}")));
    }
    let result = &report["result"];
    let restored = result["restored"].as_array().map_or(0, Vec::len);
    let mut text = format!("Engine restarted · {restored} agent(s) restored");
    let failed = result["failed"].as_array().cloned().unwrap_or_default();
    if !failed.is_empty() {
        let why: Vec<String> = failed
            .iter()
            .map(|f| format!("{}: {}", f["id"].as_str().unwrap_or("?"), f["reason"].as_str().unwrap_or("")))
            .collect();
        text.push_str(&format!(" · {} failed ({})", failed.len(), why.join("; ")));
        return Some((Severity::Warning, text));
    }
    Some((Severity::Info, text))
}

fn report_path(state: &State) -> PathBuf {
    let dir = discovery::runtime_dir(&state.conn.root, &state.conn.env);
    let _ = std::fs::create_dir_all(&dir);
    dir.join(format!("desktop-restart-{}.json", std::process::id()))
}

impl State {
    fn lazyagent(&self, args: &[&str]) -> Option<Command> {
        let starter = Starter::from_env(&self.conn.env)?;
        let mut cmd = starter.command();
        cmd.env_clear().envs(self.conn.env.vars()).args(args);
        cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        Some(cmd)
    }

    fn update_args(&self, sub: &'static str) -> Vec<String> {
        let mut args: Vec<String> = vec!["update".into(), sub.into(), "--json".into()];
        if let Ok(exe) = std::env::current_exe() {
            args.extend(["--app-binary".into(), exe.to_string_lossy().trim_end_matches(" (deleted)").to_string()]);
        }
        if !BUILD_COMMIT.is_empty() {
            args.extend(["--app-commit".into(), BUILD_COMMIT.into()]);
        }
        if let Some(c) = &self.engine_commit {
            args.extend(["--engine-commit".into(), c.clone()]);
        }
        args
    }

    /// Runs `lazyagent <args>` off the UI thread; each stdout line, then
    /// the exit (stderr's tail), goes back to the UI thread.
    fn run_lines(
        &mut self,
        args: Vec<String>,
        on_line: impl Fn(&mut State, String) + Send + Clone + 'static,
        on_end: impl FnOnce(&mut State, Result<(), String>) + Send + 'static,
    ) {
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        let Some(mut cmd) = self.lazyagent(&refs) else {
            on_end(self, Err("neither LAZYAGENT_PYTHON nor `lazyagent` on PATH can run lazyagent".into()));
            return;
        };
        let conn = self.conn.clone();
        std::thread::spawn(move || {
            let mut child = match cmd.spawn() {
                Ok(c) => c,
                Err(e) => {
                    let e = format!("cannot run lazyagent: {e}");
                    conn.send(Msg::Apply(Box::new(move |s| on_end(s, Err(e)))));
                    return;
                }
            };
            let stderr = child.stderr.take();
            let tail = std::thread::spawn(move || {
                let mut text = String::new();
                if let Some(mut e) = stderr {
                    let _ = std::io::Read::read_to_string(&mut e, &mut text);
                }
                text
            });
            if let Some(out) = child.stdout.take() {
                for line in BufReader::new(out).lines().map_while(Result::ok) {
                    let f = on_line.clone();
                    conn.send(Msg::Apply(Box::new(move |s| f(s, line))));
                }
            }
            let status = child.wait();
            let err = tail.join().unwrap_or_default();
            let end = match status {
                Ok(st) if st.success() => Ok(()),
                Ok(_) => Err(err.lines().last().unwrap_or("failed").to_string()),
                Err(e) => Err(e.to_string()),
            };
            conn.send(Msg::Apply(Box::new(move |s| on_end(s, end))));
        });
    }

    /// Help → Check for updates (`quiet`: the automatic one).
    pub fn check_updates(&mut self, quiet: bool) {
        if !matches!(self.life.phase, Phase::Idle) {
            return;
        }
        self.life.phase = Phase::Checking { quiet };
        self.life.check_error = None;
        if !quiet {
            self.life.applied = None;
            self.life.sheet = Some(Sheet::Update);
        }
        let args = self.update_args("check");
        self.run_lines(
            args,
            |s, line| {
                if let Ok(info) = serde_json::from_str::<UpdateInfo>(&line) {
                    s.life.info = Some(info);
                }
            },
            move |s, end| {
                s.life.phase = Phase::Idle;
                s.life.next_auto = Some(Instant::now() + AUTO_EVERY);
                let fetch_error = s.life.info.as_ref().and_then(|i| i.fetch_error.clone());
                match (end, fetch_error) {
                    (Err(e), None) | (_, Some(e)) => {
                        if !quiet {
                            s.life.check_error = Some(e);
                        }
                    }
                    (Ok(()), None) => {}
                }
            },
        );
    }

    /// The update dialog's "Update".
    pub fn apply_update(&mut self) {
        if !matches!(self.life.phase, Phase::Idle) {
            return;
        }
        self.life.phase = Phase::Applying { step: "check".into(), line: String::new() };
        self.life.applied = None;
        let args = self.update_args("apply");
        self.run_lines(
            args,
            |s, line| {
                let Ok(v) = serde_json::from_str::<Value>(&line) else { return };
                if let Some(r) = v.get("result") {
                    s.life.applied = serde_json::from_value(r.clone()).ok();
                } else if let Phase::Applying { step, line } = &mut s.life.phase {
                    if let Some(st) = v["step"].as_str() {
                        *step = st.to_string();
                    }
                    if let Some(o) = v["output"].as_str().or(v["detail"].as_str()) {
                        *line = o.to_string();
                    }
                }
            },
            |s, end| {
                s.life.phase = Phase::Idle;
                let applied = s.life.applied.clone().unwrap_or_else(|| ApplyResult {
                    ok: false,
                    reason: Some(end.err().unwrap_or_else(|| "no result".into())),
                    ..Default::default()
                });
                if applied.steps.iter().any(|st| st == "merge" || st == "upgrade")
                    && let Some(info) = &mut s.life.info
                {
                    info.update_available = false;
                }
                if applied.steps.iter().any(|st| st == "build")
                    && let Some(info) = &mut s.life.info
                {
                    info.app_behind = Some(false);
                }
                // Offer the restart (Q-U7): the engine when its code changed.
                if let Some(sheet) = restart_after(&applied) {
                    s.life.sheet = Some(sheet);
                }
                s.life.applied = Some(applied);
            },
        );
    }

    /// File → Restart…
    pub fn open_restart(&mut self) {
        self.life.sheet = Some(Sheet::Restart { engine: false, restore: true, after_update: false });
    }

    /// Whether this engine can be restarted from here (not one inside a TUI).
    pub fn engine_restartable(&self) -> bool {
        self.engine_detached != Some(false)
    }

    /// Restarts the app, and first the engine when `engine` (§4).
    pub fn restart(&mut self, engine: bool, restore: bool) {
        self.flush_saved();
        if !engine || !self.engine_restartable() {
            self.relaunch(serde_json::json!({"engine": false}));
            return;
        }
        // From here the app sends nothing to any engine.
        self.conn.shutdown();
        self.life.phase = Phase::Restarting { step: "stopping".into() };
        let root = self.conn.root.clone();
        let mut args = vec!["engine".to_string(), "restart".into(), "--json".into()];
        if restore {
            args.push("--restore".into());
        }
        args.push(root);
        self.run_lines(
            args,
            |s, line| {
                let Ok(v) = serde_json::from_str::<Value>(&line) else { return };
                if let Some(r) = v.get("result") {
                    s.life.applied = None;
                    s.restart_result = Some(r.clone());
                } else if let (Phase::Restarting { step }, Some(st)) = (&mut s.life.phase, v["step"].as_str()) {
                    *step = st.to_string();
                }
            },
            |s, end| {
                let result = s.restart_result.take().unwrap_or(Value::Null);
                let error = if result.is_null() { end.err() } else { None };
                s.relaunch(serde_json::json!({"engine": true, "result": result, "error": error}));
            },
        );
    }

    /// Replaces this process with a fresh launch of the app.
    fn relaunch(&mut self, report: Value) {
        let path = report_path(self);
        let written = std::fs::write(&path, report.to_string()).is_ok();
        let fallback = Start { env: std::env::vars_os().collect(), args: vec![] };
        let start = START.get().unwrap_or(&fallback);
        let (program, args, env) =
            relaunch_command(start, &self.conn.root, std::env::current_exe().ok(), written.then_some(path.as_path()));
        if cfg!(test) {
            self.life.relaunched = Some((program, args));
            self.life.phase = Phase::Idle;
            return;
        }
        use std::os::unix::process::CommandExt;
        let err = Command::new(&program).args(&args).env_clear().envs(env).exec();
        // Only reached when the exec failed: stay open, say so.
        self.life.phase = Phase::Idle;
        self.toast(Severity::Error, format!("Could not restart the app ({}): {err}", program.display()));
    }

    /// Once per frame: the automatic check (10 s after the first
    /// connection, then every 6 h) unless turned off.
    pub fn lifecycle_tick(&mut self, ctx: &egui::Context) {
        if cfg!(test) || self.saved.update_check_off {
            return;
        }
        let Some(at) = self.life.next_auto else { return };
        let now = Instant::now();
        if now >= at {
            self.life.next_auto = Some(now + AUTO_EVERY);
            self.check_updates(true);
        } else {
            ctx.request_repaint_after(at - now);
        }
    }

    /// The first connection arms the automatic check, and a relaunched app
    /// shows what its restart did.
    pub fn lifecycle_connected(&mut self) {
        if self.life.next_auto.is_none() {
            self.life.next_auto = Some(Instant::now() + AUTO_FIRST);
        }
        if let Some((severity, text)) = take_report() {
            self.toast(severity, text);
        }
    }
}

/// The update, restart and about sheets.
pub fn show(ctx: &egui::Context, state: &mut State) {
    let Some(sheet) = state.life.sheet else { return };
    let t = state.theme;
    let mut close = false;
    let mut edited = None;
    let modal = egui::Modal::new(egui::Id::unique("lazyagent-lifecycle")).show(ctx, |ui| {
        ui.set_width(520.0);
        match sheet {
            Sheet::About => {
                ui.label(RichText::new("About lazyagent").strong().size(15.0));
                ui.add_space(6.0);
                let commit = if BUILD_COMMIT.is_empty() { "unknown".to_string() } else { short(BUILD_COMMIT) };
                ui.label(format!("Desktop app {} (build {commit})", env!("CARGO_PKG_VERSION")));
                let engine = state.engine_version.clone().unwrap_or_else(|| "not connected".into());
                let ecommit = state.engine_commit.as_deref().map(short).unwrap_or_else(|| "published".into());
                ui.label(format!("Engine {engine} ({ecommit})"));
                if let Some(i) = &state.life.info {
                    ui.label(mode_line(i));
                }
                ui.label(RichText::new(format!("Repo {}", state.conn.root)).color(t.muted));
                ui.add_space(8.0);
                if ui.button("Close").clicked() {
                    close = true;
                }
            }
            Sheet::Update => update_sheet(ui, state, &mut close),
            Sheet::Restart { engine, restore, after_update } => {
                let (mut engine, mut restore) = (engine, restore);
                restart_sheet(ui, state, &mut engine, &mut restore, after_update, &mut close);
                edited = Some(Sheet::Restart { engine, restore, after_update });
            }
        }
    });
    let busy = !matches!(state.life.phase, Phase::Idle | Phase::Checking { quiet: true });
    if modal.should_close() && !busy {
        close = true;
    }
    // A button may have opened another sheet; else this one stays, edited.
    state.life.sheet = match state.life.sheet {
        _ if close => None,
        Some(now) if now == sheet => edited.or(Some(sheet)),
        other => other,
    };
}

fn short(commit: &str) -> String {
    let (sha, dirty) = commit.split_once('+').map_or((commit, ""), |(s, d)| (s, d));
    let sha = &sha[..sha.len().min(9)];
    if dirty.is_empty() { sha.to_string() } else { format!("{sha}+{dirty}") }
}

fn mode_line(i: &UpdateInfo) -> String {
    if i.mode == "development" {
        format!(
            "Development checkout {} ({} → {})",
            i.root.as_deref().unwrap_or("?"),
            i.branch.as_deref().unwrap_or("detached"),
            i.upstream.as_deref().unwrap_or("no upstream")
        )
    } else {
        format!("Published release {}", i.version)
    }
}

fn update_sheet(ui: &mut egui::Ui, state: &mut State, close: &mut bool) {
    let t = state.theme;
    ui.label(RichText::new("Updates").strong().size(15.0));
    ui.add_space(6.0);
    let phase = state.life.phase.clone();
    match &phase {
        Phase::Checking { .. } => {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label("Checking…");
            });
        }
        Phase::Applying { step, line } => {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label(if step == "build" {
                    "Building the app (cargo)…".to_string()
                } else {
                    format!("Updating: {step}")
                });
            });
            if !line.is_empty() {
                ui.label(RichText::new(line).monospace().small().color(t.muted));
            }
        }
        _ => {}
    }
    if let Some(e) = &state.life.check_error {
        ui.colored_label(t.red, format!("Cannot check: {e}"));
    }
    if let Some(i) = state.life.info.clone() {
        ui.label(RichText::new(mode_line(&i)).color(t.muted));
        ui.add_space(4.0);
        if i.mode == "development" {
            if i.update_available {
                ui.label(format!("{} new commit(s) on {}:", i.behind, i.upstream.as_deref().unwrap_or("upstream")));
                egui::ScrollArea::vertical().max_height(180.0).show(ui, |ui| {
                    for c in &i.commits {
                        ui.label(RichText::new(format!("{} {}", c.sha, c.subject)).monospace());
                    }
                });
            } else if i.upstream.is_some() && i.fetch_error.is_none() {
                ui.label("Up to date.");
            }
        } else if let Some(latest) = &i.latest {
            if i.update_available {
                ui.label(format!("lazyagent {latest} is available (this is {}).", i.version));
            } else {
                ui.label(format!("Up to date ({}).", i.version));
            }
        }
        if let Some(r) = &i.reason {
            ui.colored_label(t.yellow, r);
        }
        if i.engine_behind == Some(true) {
            ui.colored_label(t.yellow, "The engine runs older code than the checkout: restart it.");
        }
        if i.app_behind == Some(true) {
            let text = if rebuild_wanted(&i) {
                "This app was built from older code than the checkout: rebuild it, then restart."
            } else {
                "This app was built from older code than the checkout."
            };
            ui.colored_label(t.yellow, text);
        }
    }
    if let Some(a) = &state.life.applied {
        ui.add_space(4.0);
        if a.ok {
            let built_only = a.steps == ["build"];
            ui.colored_label(t.green, if built_only { "Rebuilt." } else { "Updated." });
        } else {
            ui.colored_label(t.red, a.reason.clone().unwrap_or_else(|| "Not updated.".into()));
            for f in &a.files {
                ui.label(RichText::new(format!("  {f}")).monospace().small());
            }
        }
    }
    ui.add_space(8.0);
    let idle = matches!(phase, Phase::Idle);
    let info = state.life.info.clone();
    ui.horizontal(|ui| {
        if ui.add_enabled(idle, egui::Button::new("Close")).clicked() {
            *close = true;
        }
        if ui.add_enabled(idle, egui::Button::new("Check again")).clicked() {
            state.check_updates(false);
        }
        if let Some(i) = info {
            if i.update_available
                && ui.add_enabled(idle && i.can_apply, egui::Button::new(RichText::new("Update").strong())).clicked()
            {
                state.apply_update();
            }
            if rebuild_wanted(&i)
                && ui.add_enabled(idle, egui::Button::new(RichText::new("Rebuild").strong())).clicked()
            {
                state.apply_update();
            }
            if !i.update_available
                && !rebuild_wanted(&i)
                && (i.engine_behind == Some(true) || i.app_behind == Some(true))
                && idle
                && ui.button("Restart…").clicked()
            {
                let engine = i.engine_behind == Some(true);
                state.life.sheet = Some(Sheet::Restart { engine, restore: true, after_update: false });
            }
            if i.update_available
                && !i.can_apply
                && i.mode == "published"
                && let Some(c) = &i.command
                && ui.button("Copy the command").clicked()
            {
                state.clipboard = Some(c.join(" "));
            }
        }
    });
}

fn restart_sheet(
    ui: &mut egui::Ui,
    state: &mut State,
    engine: &mut bool,
    restore: &mut bool,
    after_update: bool,
    close: &mut bool,
) {
    let t = state.theme;
    ui.label(RichText::new("Restart lazyagent").strong().size(15.0));
    ui.add_space(6.0);
    if after_update {
        ui.label("Updated. Restart now to run the new code?");
        ui.add_space(4.0);
    }
    if let Phase::Restarting { step } = &state.life.phase {
        ui.horizontal(|ui| {
            ui.spinner();
            ui.label(format!("Restarting the engine: {step}…"));
        });
        return;
    }
    let restartable = state.engine_restartable();
    ui.add_enabled_ui(restartable, |ui| {
        ui.checkbox(engine, "Restart the engine too")
            .on_disabled_hover_text("This engine runs inside a lazyagent TUI: quit that TUI to restart it");
    });
    ui.add_enabled_ui(*engine && restartable, |ui| {
        ui.indent("restore", |ui| ui.checkbox(restore, "Restore its agents afterwards"));
    });
    let list = stopping(&state.store);
    let busy = *engine && restartable && list.iter().any(|s| s.busy);
    if *engine && restartable {
        if list.is_empty() {
            ui.label(RichText::new("The engine runs no agent.").color(t.muted));
        } else {
            ui.label(format!("The engine stops {} agent(s), and its shells:", list.len()));
            egui::ScrollArea::vertical().max_height(200.0).show(ui, |ui| {
                for s in &list {
                    let mut text = format!("{}  {}  {}", s.name, s.place, s.status.replace('_', " "));
                    if s.busy {
                        text = format!("⚠ {text} — its current turn is cut");
                    }
                    ui.label(RichText::new(text).color(if s.busy { t.yellow } else { t.text }));
                }
            });
            if !*restore {
                ui.label(
                    RichText::new("Without restore they stay offered (the banner) in the new app.").color(t.muted),
                );
            }
        }
    } else {
        ui.label(RichText::new("Only the app restarts; the agents keep running.").color(t.muted));
    }
    let unsaved: Vec<String> =
        state.notes.iter().filter(|(_, d)| d.dirty()).map(|(k, _)| state.place_name(&k.0)).collect();
    if !unsaved.is_empty() {
        ui.colored_label(t.yellow, format!("Unsaved notes edits are lost: {}", unsaved.join(", ")));
    }
    ui.add_space(8.0);
    ui.horizontal(|ui| {
        if ui.button(if after_update { "Later" } else { "Cancel" }).clicked() {
            *close = true;
        }
        let label = if busy || !unsaved.is_empty() { "Restart anyway" } else { "Restart" };
        if ui.button(RichText::new(label).strong()).clicked() {
            state.restart(*engine && restartable, *restore);
        }
    });
}
