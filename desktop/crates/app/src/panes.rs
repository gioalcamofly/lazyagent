//! One pane per engine session (agent, orchestrator or shell): its emulator
//! and its stream. The stream's sink runs on the connection's reader
//! thread, so bytes are parsed there and the UI thread only draws.
//!
//! Rules (ui-plan §3b, U2, engine-split §3c):
//! - attach while shown; the first attach renders the snapshot, a re-attach
//!   resumes from the last byte (a tail when the engine still has it);
//! - the snapshot is applied in the attach callback, on the reader thread,
//!   before any frame of the stream (the frames carry the unparsed backlog);
//! - a gap in offsets, or `stream.reset`, re-attaches;
//! - the size follows the latest typist: a pane claims on focus and when
//!   its box changes while focused; a key typed while a claim is pending
//!   waits for it, so the program sees the resize first.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use eframe::egui;
use lazyagent_proto::client::{Client, StreamSink};
use lazyagent_proto::types::{AttachResult, SizeOwner, StreamClosed, StreamGeometry};
use lazyagent_term::{Palette, Terminal};
use serde_json::json;

/// `LAZYAGENT_DESKTOP_DEBUG=1`: stream events on stderr.
pub fn debug() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var("LAZYAGENT_DESKTOP_DEBUG").is_ok_and(|v| v == "1"))
}

macro_rules! dbg_log {
    ($($t:tt)*) => { if debug() { eprintln!("[desktop {:?}] {}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64(), format!($($t)*)); } };
}

/// Keys held while a claim or an attach is pending (the web holds 64 KiB).
const HELD_MAX: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaneKind {
    Agent,
    Shell,
}

pub struct Inner {
    pub term: Terminal,
    pub stream_id: Option<u32>,
    /// Offset after the last byte fed.
    pub received: Option<u64>,
    pub attaching: bool,
    /// Re-attach from `received` (a gap or a reset).
    pub stale: bool,
    /// `Some(exit_code)` once the session's PTY is gone.
    pub closed: Option<Option<i32>>,
    pub claim_pending: bool,
    pub claimed: Option<(u16, u16)>,
    pub(crate) held: Vec<u8>,
    client: Option<Client>,
    pub error: Option<String>,
    pub title: Option<String>,
    pub bell: bool,
    /// Bytes fed, and streams the engine dropped (slow client), for benches.
    pub bytes_in: u64,
    pub resets: u32,
    /// When the last output arrived (benches measure echo with it).
    pub last_output: Option<Instant>,
}

pub struct Pane {
    pub session_id: String,
    pub kind: PaneKind,
    pub inner: Mutex<Inner>,
    /// Shown this frame: output asks for a repaint only then.
    pub visible: AtomicBool,
    /// The keyboard is here: only this pane answers the program's queries.
    pub focused: AtomicBool,
    ctx: egui::Context,
    pacer: Arc<crate::pacer::Pacer>,
}

struct Sink(Arc<Pane>);

impl Pane {
    pub fn new(
        session_id: String,
        kind: PaneKind,
        palette: Palette,
        ctx: egui::Context,
        pacer: Arc<crate::pacer::Pacer>,
    ) -> Arc<Pane> {
        Arc::new(Pane {
            session_id,
            kind,
            inner: Mutex::new(Inner {
                term: Terminal::new(24, 80, palette),
                stream_id: None,
                received: None,
                attaching: false,
                stale: false,
                closed: None,
                claim_pending: false,
                claimed: None,
                held: vec![],
                client: None,
                error: None,
                title: None,
                bell: false,
                bytes_in: 0,
                resets: 0,
                last_output: None,
            }),
            visible: AtomicBool::new(false),
            focused: AtomicBool::new(false),
            ctx,
            pacer,
        })
    }

    pub fn attached(&self) -> bool {
        self.inner.lock().unwrap().stream_id.is_some()
    }

    /// Attaches unless attached, attaching or closed.
    pub fn attach(self: &Arc<Self>, client: &Client) {
        let from = {
            let mut i = self.inner.lock().unwrap();
            if i.attaching || (i.stream_id.is_some() && !i.stale) || i.closed.is_some() {
                return;
            }
            if let (Some(id), Some(c)) = (i.stream_id.take(), i.client.clone()) {
                c.detach(id);
            }
            i.attaching = true;
            i.stale = false;
            dbg_log!("attach {} from {:?}", self.session_id, i.received);
            i.received
        };
        let me = self.clone();
        let c = client.clone();
        client.attach_async(&self.session_id, from, Arc::new(Sink(self.clone())), move |r| {
            me.attached_with(r, c);
        });
    }

    fn attached_with(&self, result: Result<AttachResult, lazyagent_proto::ClientError>, client: Client) {
        let mut i = self.inner.lock().unwrap();
        i.attaching = false;
        match result {
            Ok(a) => {
                dbg_log!(
                    "attached {} stream {} mode {} offset {} size {:?}",
                    self.session_id,
                    a.stream_id,
                    a.mode,
                    a.offset,
                    a.size
                );
                let (rows, cols) = (a.size.rows as usize, a.size.cols as usize);
                if a.mode == "snapshot" {
                    i.term.reset(rows, cols);
                    if let Some(s) = &a.snapshot {
                        i.term.feed(s.as_bytes());
                    }
                } else {
                    i.term.resize(rows, cols);
                }
                i.stream_id = Some(a.stream_id);
                i.received = Some(a.offset);
                i.error = None;
                let held = std::mem::take(&mut i.held);
                if !held.is_empty() && !i.claim_pending {
                    let _ = client.send_input(a.stream_id, &held);
                } else {
                    i.held = held;
                }
                i.client = Some(client);
            }
            Err(e) => {
                dbg_log!("attach failed {}: {e}", self.session_id);
                // The session is gone (it exited between listing and attach).
                if e.code() == Some("VALIDATION_ERROR") {
                    i.closed = Some(None);
                }
                i.error = Some(e.to_string());
            }
        }
        drop(i);
        self.ctx.request_repaint();
    }

    pub fn detach(&self) {
        let mut i = self.inner.lock().unwrap();
        dbg_log!("detach {} stream {:?}", self.session_id, i.stream_id);
        if let (Some(id), Some(c)) = (i.stream_id.take(), i.client.clone()) {
            c.detach(id);
        }
    }

    /// Asks for the PTY to be `rows × cols` with this client as its owner.
    pub fn claim(self: &Arc<Self>, client: &Client, rows: u16, cols: u16) {
        {
            let mut i = self.inner.lock().unwrap();
            if i.claim_pending || i.closed.is_some() {
                return;
            }
            i.claim_pending = true;
            i.claimed = Some((rows, cols));
        }
        let me = self.clone();
        client.request_async(
            "claim_size",
            json!({"session_id": self.session_id, "rows": rows, "cols": cols}),
            move |reply| {
                let mut i = me.inner.lock().unwrap();
                i.claim_pending = false;
                if let Ok(v) = reply
                    && let Ok(owner) = serde_json::from_value::<SizeOwner>(v)
                {
                    // stream.resized normally did it already, in order.
                    i.term.resize(owner.rows as usize, owner.cols as usize);
                }
                let held = std::mem::take(&mut i.held);
                if let (Some(id), Some(c), false) = (i.stream_id, i.client.clone(), held.is_empty()) {
                    let _ = c.send_input(id, &held);
                }
                drop(i);
                me.ctx.request_repaint();
            },
        );
    }

    /// Writes typed bytes (held while a claim or the attach is pending).
    pub fn write(&self, bytes: &[u8]) {
        let mut i = self.inner.lock().unwrap();
        i.term.scroll_to_bottom();
        i.term.select_clear();
        match (i.stream_id, i.client.clone()) {
            (Some(id), Some(c)) if !i.claim_pending && i.held.is_empty() => {
                drop(i);
                let _ = c.send_input(id, bytes);
            }
            _ => {
                if i.held.len() + bytes.len() <= HELD_MAX {
                    i.held.extend_from_slice(bytes);
                }
            }
        }
    }
}

impl StreamSink for Sink {
    fn output(&self, offset: u64, data: &[u8]) {
        let pane = &self.0;
        let mut i = pane.inner.lock().unwrap();
        let mut data = data;
        if let Some(r) = i.received {
            if offset > r {
                dbg_log!("gap {}: have {r}, got {offset}", pane.session_id);
                // A gap: what we have is no longer the screen; start over.
                i.stale = true;
                drop(i);
                pane.ctx.request_repaint();
                return;
            }
            if offset + (data.len() as u64) <= r {
                return;
            }
            data = &data[(r - offset) as usize..];
        }
        i.term.feed(data);
        i.bytes_in += data.len() as u64;
        i.last_output = Some(Instant::now());
        i.received = Some(offset.max(i.received.unwrap_or(0)) + data.len() as u64);
        let requests = i.term.take_requests();
        if requests.bell {
            i.bell = true;
        }
        if let Some(t) = requests.title {
            i.title = Some(t);
        }
        if !requests.replies.is_empty()
            && pane.focused.load(Ordering::Relaxed)
            && let (Some(id), Some(c)) = (i.stream_id, i.client.clone())
        {
            let _ = c.send_input(id, &requests.replies);
        }
        drop(i);
        if pane.visible.load(Ordering::Relaxed) {
            pane.pacer.request(&pane.ctx);
        }
    }

    fn resized(&self, g: &StreamGeometry) {
        self.0.inner.lock().unwrap().term.resize(g.rows as usize, g.cols as usize);
        self.0.ctx.request_repaint();
    }

    fn reset(&self, g: &StreamGeometry) {
        dbg_log!("reset {} at {}", self.0.session_id, g.offset);
        let mut i = self.0.inner.lock().unwrap();
        i.stream_id = None;
        i.stale = true;
        i.resets += 1;
        drop(i);
        self.0.ctx.request_repaint();
    }

    fn closed(&self, c: &StreamClosed) {
        dbg_log!("closed {} at {}", self.0.session_id, c.offset);
        let mut i = self.0.inner.lock().unwrap();
        i.stream_id = None;
        i.closed = Some(c.exit_code);
        drop(i);
        self.0.ctx.request_repaint();
    }
}
