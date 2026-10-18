//! The connection to the engine, kept by a supervisor thread (the rules of
//! the remote TUI, engine-split §6c/§6d):
//!
//! - find the repo's engine, starting one through `lazyagent engine start`
//!   when none runs;
//! - hello, `get_sidebar_state` + `get_config`, then `subscribe {since}`, so
//!   no event falls between the seed and the live stream;
//! - on loss, retry with backoff (0.25 s doubling to 5 s); an engine that
//!   announced `engine.shutdown` is not restarted (it was stopped on
//!   purpose) until asked, one that vanished is.
//!
//! Everything for the UI goes through [`Msg`] plus a repaint request.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use eframe::egui;
use lazyagent_proto::EngineEvent;
use lazyagent_proto::client::{Client, ClientError, ClientEvent, Reply};
use lazyagent_proto::discovery::{self, Env, Starter};
use lazyagent_proto::types::{ConfigView, HelloResult, SidebarState};
use serde_json::{Value, json};

use crate::app::State;

pub const CLIENT_NAME: &str = "desktop";
const TIMEOUT: Duration = Duration::from_secs(10);
const BACKOFF_MIN: Duration = Duration::from_millis(250);
const BACKOFF_MAX: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, PartialEq)]
pub enum Link {
    /// Looking for the engine, or starting one (the text says which).
    Connecting(String),
    Connected,
    /// Lost it; trying again.
    Reconnecting(String),
    /// Stopped on purpose; not restarted until asked.
    Stopped,
    /// Cannot start one (the text says why); retrying.
    Failed(String),
}

pub type Apply = Box<dyn FnOnce(&mut State) + Send>;

pub enum Msg {
    Link(Link),
    Connected {
        hello: HelloResult,
        engine_changed: bool,
        started: bool,
    },
    Seed(Box<SidebarState>, Option<ConfigView>),
    Event(EngineEvent),
    /// A continuation to run on the UI thread (a request's reply).
    Apply(Apply),
}

pub struct Conn {
    pub root: String,
    pub env: Env,
    client: Mutex<Option<Client>>,
    tx: Mutex<Sender<Msg>>,
    ctx: egui::Context,
    start_requested: AtomicBool,
    quit: AtomicBool,
}

impl Conn {
    pub fn new(root: String, env: Env, tx: Sender<Msg>, ctx: egui::Context) -> Arc<Conn> {
        Arc::new(Conn {
            root,
            env,
            client: Mutex::new(None),
            tx: Mutex::new(tx),
            ctx,
            start_requested: AtomicBool::new(false),
            quit: AtomicBool::new(false),
        })
    }

    pub fn spawn_supervisor(self: &Arc<Self>) {
        let me = self.clone();
        std::thread::Builder::new().name("lazyagent-conn".into()).spawn(move || me.supervise()).unwrap();
    }

    pub fn send(&self, msg: Msg) {
        let _ = self.tx.lock().unwrap().send(msg);
        self.ctx.request_repaint();
    }

    pub fn client(&self) -> Option<Client> {
        self.client.lock().unwrap().clone()
    }

    /// Asks for an engine start after a deliberate stop.
    pub fn request_start(&self) {
        self.start_requested.store(true, Ordering::SeqCst);
    }

    pub fn shutdown(&self) {
        self.quit.store(true, Ordering::SeqCst);
        if let Some(c) = self.client() {
            c.close();
        }
    }

    /// Sends a request; `done` runs on the UI thread with the reply.
    pub fn request(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        done: impl FnOnce(&mut State, Reply) + Send + 'static,
    ) {
        match self.client() {
            Some(c) => {
                let me = self.clone();
                c.request_async(method, params, move |reply| me.send(Msg::Apply(Box::new(move |s| done(s, reply)))));
            }
            None => {
                let err = ClientError::Closed("not connected to the engine".into());
                self.send(Msg::Apply(Box::new(move |s| done(s, Err(err)))));
            }
        }
    }

    /// Fire and forget.
    pub fn notify(&self, method: &str, params: Value) {
        if let Some(c) = self.client() {
            c.request_async(method, params, |_| {});
        }
    }

    fn supervise(self: Arc<Self>) {
        let mut backoff = BACKOFF_MIN;
        let mut engine_id: Option<String> = None;
        let mut stopped = false;
        let mut first = true;
        while !self.quit.load(Ordering::SeqCst) {
            let wants_start = self.start_requested.swap(false, Ordering::SeqCst);
            let located = discovery::locate(&self.root, &self.env);
            let mut started = false;
            let socket = match located {
                Some(l) => l.socket,
                None if stopped && !wants_start => {
                    self.send(Msg::Link(Link::Stopped));
                    self.sleep(Duration::from_secs(1));
                    continue;
                }
                None if discovery::autostart_enabled(&self.env) || wants_start => {
                    let link = if first {
                        Link::Connecting("Starting the engine…".into())
                    } else {
                        Link::Reconnecting("Starting the engine…".into())
                    };
                    self.send(Msg::Link(link));
                    let Some(starter) = Starter::from_env(&self.env) else {
                        self.send(Msg::Link(Link::Failed(
                            "No engine runs for this repo, and neither LAZYAGENT_PYTHON nor `lazyagent` on PATH \
                             can start one. Run: lazyagent engine start"
                                .into(),
                        )));
                        self.sleep(BACKOFF_MAX);
                        continue;
                    };
                    match discovery::start_engine(&self.root, &starter, &self.env, discovery::START_TIMEOUT) {
                        Ok(s) => {
                            started = true;
                            s
                        }
                        Err(e) => {
                            self.send(Msg::Link(Link::Failed(e.0)));
                            self.sleep(BACKOFF_MAX);
                            continue;
                        }
                    }
                }
                None => {
                    self.send(Msg::Link(Link::Failed(
                        "No engine runs for this repo (auto-start is off): lazyagent engine start".into(),
                    )));
                    self.sleep(BACKOFF_MAX);
                    continue;
                }
            };
            stopped = false;
            match self.session(&socket, &mut engine_id, started) {
                Ok(deliberate) => {
                    stopped = deliberate;
                    backoff = BACKOFF_MIN;
                    first = false;
                    if !deliberate && !self.quit.load(Ordering::SeqCst) {
                        self.send(Msg::Link(Link::Reconnecting("Lost the engine; reconnecting…".into())));
                    }
                }
                Err(why) => {
                    let link = if first { Link::Connecting(why) } else { Link::Reconnecting(why) };
                    self.send(Msg::Link(link));
                    self.sleep(backoff);
                    backoff = (backoff * 2).min(BACKOFF_MAX);
                }
            }
        }
    }

    fn sleep(&self, d: Duration) {
        let end = Instant::now() + d;
        while Instant::now() < end && !self.quit.load(Ordering::SeqCst) && !self.start_requested.load(Ordering::SeqCst)
        {
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// One connection, until it ends: `Ok(true)` when the engine announced
    /// a deliberate shutdown, `Ok(false)` when it was lost, `Err` when it
    /// could not be set up.
    fn session(&self, socket: &std::path::Path, engine_id: &mut Option<String>, started: bool) -> Result<bool, String> {
        let (client, hello, rx) =
            Client::connect(socket, CLIENT_NAME, env!("CARGO_PKG_VERSION"), TIMEOUT).map_err(|e| e.to_string())?;
        let engine_changed = engine_id.as_ref().is_some_and(|id| *id != hello.engine_id);
        *engine_id = Some(hello.engine_id.clone());
        let state: SidebarState = client.call("get_sidebar_state", json!({}), TIMEOUT).map_err(|e| e.to_string())?;
        let config: Option<ConfigView> = client.call("get_config", json!({}), TIMEOUT).ok();
        let cursor = state.cursor;
        *self.client.lock().unwrap() = Some(client.clone());
        self.send(Msg::Connected { hello, engine_changed, started });
        self.send(Msg::Seed(Box::new(state), config));
        if let Err(e) = client.call::<Value>("subscribe", json!({"since": cursor}), TIMEOUT) {
            *self.client.lock().unwrap() = None;
            client.close();
            return Err(e.to_string());
        }
        self.send(Msg::Link(Link::Connected));
        let mut deliberate = false;
        loop {
            if self.quit.load(Ordering::SeqCst) {
                client.close();
            }
            match rx.recv_timeout(Duration::from_millis(250)) {
                Ok(ClientEvent::Event(e)) => {
                    let ev = EngineEvent::parse(e);
                    if matches!(ev, EngineEvent::EngineShutdown(_)) {
                        deliberate = true;
                    }
                    self.send(Msg::Event(ev));
                }
                Ok(ClientEvent::ProtocolError(_)) => {}
                Ok(ClientEvent::Closed(_)) | Err(RecvTimeoutError::Disconnected) => break,
                Err(RecvTimeoutError::Timeout) => {}
            }
        }
        *self.client.lock().unwrap() = None;
        Ok(deliberate)
    }
}
