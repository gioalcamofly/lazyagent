//! A protocol v1 client on plain threads: one reader thread per
//! connection, writes from the caller's thread under a mutex.
//!
//! - Requests can block ([`Client::request`]) or call back
//!   ([`Client::request_async`]); responses are matched by id, so requests
//!   overlap freely.
//! - Output frames go to the [`StreamSink`] of their stream **on the reader
//!   thread**, in wire order with the `stream.*` events of that stream, so a
//!   sink can parse the bytes and apply resizes exactly where they fall.
//! - Every other event, unknown ones included, goes to the
//!   [`ClientEvent`] channel in arrival order.

use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use crate::envelope::{self, Event, Message, Request, RpcError};
use crate::framing::{self, Frame, FrameDecoder};
use crate::types::{AttachResult, HelloResult, StreamClosed, StreamGeometry};

pub const PROTOCOL_VERSION: u32 = 1;
const READ_SIZE: usize = 256 * 1024;
/// The engine keeps input frames ≤ 64 KiB apart from the 16 MiB frame cap;
/// we split bigger pastes so one never holds the socket for long.
const INPUT_CHUNK: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq)]
pub enum ClientError {
    /// An error response.
    Rpc(RpcError),
    /// The connection is gone (the message says how).
    Closed(String),
    Timeout,
    /// A result that does not have the expected shape.
    Decode(String),
    Io(String),
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::Rpc(e) => write!(f, "{e}"),
            ClientError::Closed(why) => write!(f, "connection closed: {why}"),
            ClientError::Timeout => f.write_str("request timed out"),
            ClientError::Decode(e) => write!(f, "unexpected result: {e}"),
            ClientError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ClientError {}

impl ClientError {
    /// The error code of an error response (`None` for transport errors).
    pub fn code(&self) -> Option<&str> {
        match self {
            ClientError::Rpc(e) => Some(&e.code),
            _ => None,
        }
    }
}

pub type Reply = Result<Value, ClientError>;

/// What a connection reports besides responses and stream output.
#[derive(Debug, Clone, PartialEq)]
pub enum ClientEvent {
    Event(Event),
    /// An error response nobody asked for (`id: ""`): a bad frame or input.
    ProtocolError(RpcError),
    /// The connection ended; nothing follows.
    Closed(String),
}

/// Receives one stream's output, on the reader thread.
pub trait StreamSink: Send + Sync {
    /// Bytes at `offset` in the session's output.
    fn output(&self, offset: u64, data: &[u8]);
    /// The PTY changed size at `geometry.offset`.
    fn resized(&self, _geometry: &StreamGeometry) {}
    /// The engine dropped the stream (slow client); re-attach from `offset`.
    fn reset(&self, _geometry: &StreamGeometry) {}
    /// The session's PTY is gone.
    fn closed(&self, _closed: &StreamClosed) {}
}

type Callback = Box<dyn FnOnce(Reply) + Send>;

enum Pending {
    Wait(Arc<(Mutex<Option<Reply>>, Condvar)>),
    Call(Callback),
}

struct Inner {
    writer: Mutex<UnixStream>,
    next_id: AtomicU64,
    pending: Mutex<HashMap<String, Pending>>,
    /// `attach_stream` requests in flight: their sink is registered under
    /// the stream id as soon as the reader sees the response, before any
    /// output frame of that stream.
    attaching: Mutex<HashMap<String, Arc<dyn StreamSink>>>,
    streams: Mutex<HashMap<u32, Arc<dyn StreamSink>>>,
    closed: Mutex<Option<String>>,
    events: Sender<ClientEvent>,
}

/// One connection to an engine's v1 socket. Cheap to clone.
#[derive(Clone)]
pub struct Client {
    inner: Arc<Inner>,
    stream: Arc<UnixStream>,
}

impl Client {
    /// Connects without the handshake; see [`Client::connect`].
    pub fn connect_raw(path: &Path) -> io::Result<(Client, Receiver<ClientEvent>)> {
        let stream = UnixStream::connect(path)?;
        let reader = stream.try_clone()?;
        let (tx, rx) = mpsc::channel();
        let inner = Arc::new(Inner {
            writer: Mutex::new(stream.try_clone()?),
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
            attaching: Mutex::new(HashMap::new()),
            streams: Mutex::new(HashMap::new()),
            closed: Mutex::new(None),
            events: tx,
        });
        let reader_inner = inner.clone();
        thread::Builder::new().name("lazyagent-proto-reader".into()).spawn(move || read_loop(reader_inner, reader))?;
        Ok((Client { inner, stream: Arc::new(stream) }, rx))
    }

    /// Connects and says `hello`.
    pub fn connect(
        path: &Path,
        client: &str,
        client_version: &str,
        timeout: Duration,
    ) -> Result<(Client, HelloResult, Receiver<ClientEvent>), ClientError> {
        let (c, rx) = Client::connect_raw(path).map_err(|e| ClientError::Io(format!("{}: {e}", path.display())))?;
        let hello = c.hello(client, client_version, timeout);
        match hello {
            Ok(h) => Ok((c, h, rx)),
            Err(e) => {
                c.close();
                Err(e)
            }
        }
    }

    pub fn hello(&self, client: &str, client_version: &str, timeout: Duration) -> Result<HelloResult, ClientError> {
        let params = json!({"protocol": PROTOCOL_VERSION, "client": client, "client_version": client_version});
        let hello: HelloResult = self.call("hello", params, timeout)?;
        if hello.protocol != PROTOCOL_VERSION {
            self.close();
            return Err(ClientError::Rpc(RpcError {
                code: envelope::codes::UNSUPPORTED_VERSION.into(),
                message: format!("the engine speaks protocol {}, this client {PROTOCOL_VERSION}", hello.protocol),
                data: None,
            }));
        }
        Ok(hello)
    }

    pub fn closed(&self) -> Option<String> {
        self.inner.closed.lock().unwrap().clone()
    }

    /// Ends the connection; the reader thread then fails what is pending.
    pub fn close(&self) {
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
    }

    fn next_id(&self) -> String {
        self.inner.next_id.fetch_add(1, Ordering::Relaxed).to_string()
    }

    /// Puts a request on the wire. `pending` is always completed: by the
    /// response, by the error that ended the connection, or here when the
    /// request cannot be sent.
    fn send_request(&self, id: String, method: &str, params: &Value, pending: Pending) {
        if let Some(why) = self.closed() {
            return complete(pending, Err(ClientError::Closed(why)));
        }
        let body = serde_json::to_vec(&Request::new(id.clone(), method, params)).expect("a request serialises");
        self.inner.pending.lock().unwrap().insert(id.clone(), pending);
        if let Err(e) = self.write(&framing::encode_control(&body))
            && let Some(p) = self.inner.pending.lock().unwrap().remove(&id)
        {
            complete(p, Err(e));
        }
    }

    fn write(&self, bytes: &[u8]) -> Result<(), ClientError> {
        let mut w = self.inner.writer.lock().unwrap();
        w.write_all(bytes).map_err(|e| ClientError::Closed(format!("write failed: {e}")))
    }

    /// Sends a request and waits for its result.
    pub fn request(&self, method: &str, params: Value, timeout: Duration) -> Reply {
        let slot = Arc::new((Mutex::new(None), Condvar::new()));
        let id = self.next_id();
        self.send_request(id.clone(), method, &params, Pending::Wait(slot.clone()));
        let (lock, cv) = &*slot;
        let (mut guard, res) = cv.wait_timeout_while(lock.lock().unwrap(), timeout, |r| r.is_none()).unwrap();
        if res.timed_out() && guard.is_none() {
            self.inner.pending.lock().unwrap().remove(&id);
            return Err(ClientError::Timeout);
        }
        guard.take().unwrap()
    }

    /// [`request`](Self::request) with the result decoded.
    pub fn call<T: DeserializeOwned>(&self, method: &str, params: Value, timeout: Duration) -> Result<T, ClientError> {
        decode(self.request(method, params, timeout)?)
    }

    /// Sends a request now; `done` runs on the reader thread with its
    /// result (or with the error that ended the connection). Requests sent
    /// one after the other are on the wire in that order.
    pub fn request_async(&self, method: &str, params: Value, done: impl FnOnce(Reply) + Send + 'static) {
        self.send_request(self.next_id(), method, &params, Pending::Call(Box::new(done)));
    }

    /// Attaches to a session's output. `sink` sees every byte from
    /// `AttachResult::offset` on; the result arrives in `done`.
    pub fn attach_async(
        &self,
        session_id: &str,
        from_offset: Option<u64>,
        sink: Arc<dyn StreamSink>,
        done: impl FnOnce(Result<AttachResult, ClientError>) + Send + 'static,
    ) {
        let mut params = json!({"session_id": session_id});
        if let Some(off) = from_offset {
            params["from_offset"] = json!(off);
        }
        let id = self.next_id();
        self.inner.attaching.lock().unwrap().insert(id.clone(), sink);
        let inner = self.inner.clone();
        let rid = id.clone();
        let cb = Box::new(move |reply: Reply| {
            inner.attaching.lock().unwrap().remove(&rid);
            done(reply.and_then(decode));
        });
        self.send_request(id, "attach_stream", &params, Pending::Call(cb));
    }

    /// Blocking [`attach_async`](Self::attach_async).
    pub fn attach(
        &self,
        session_id: &str,
        from_offset: Option<u64>,
        sink: Arc<dyn StreamSink>,
        timeout: Duration,
    ) -> Result<AttachResult, ClientError> {
        let (tx, rx) = mpsc::channel();
        self.attach_async(session_id, from_offset, sink, move |r| {
            let _ = tx.send(r);
        });
        match rx.recv_timeout(timeout) {
            Ok(r) => r,
            Err(mpsc::RecvTimeoutError::Timeout) => Err(ClientError::Timeout),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                Err(ClientError::Closed(self.closed().unwrap_or_else(|| "connection closed".into())))
            }
        }
    }

    /// Forgets a stream locally and tells the engine (fire and forget).
    pub fn detach(&self, stream_id: u32) {
        self.inner.streams.lock().unwrap().remove(&stream_id);
        self.request_async("detach_stream", json!({"stream_id": stream_id}), |_| {});
    }

    /// Writes input bytes to a stream, split into frames of ≤ 64 KiB.
    pub fn send_input(&self, stream_id: u32, data: &[u8]) -> Result<(), ClientError> {
        if data.len() <= INPUT_CHUNK {
            return self.write(&framing::encode_input(stream_id, data));
        }
        let mut wire = Vec::with_capacity(data.len() + data.len() / INPUT_CHUNK * 10 + 10);
        for chunk in data.chunks(INPUT_CHUNK) {
            wire.extend(framing::encode_input(stream_id, chunk));
        }
        self.write(&wire)
    }

    /// Writes raw bytes (tests use it for malformed frames).
    pub fn send_raw(&self, bytes: &[u8]) -> Result<(), ClientError> {
        self.write(bytes)
    }

    pub fn has_stream(&self, stream_id: u32) -> bool {
        self.inner.streams.lock().unwrap().contains_key(&stream_id)
    }
}

fn decode<T: DeserializeOwned>(v: Value) -> Result<T, ClientError> {
    serde_json::from_value(v).map_err(|e| ClientError::Decode(e.to_string()))
}

fn read_loop(inner: Arc<Inner>, mut sock: UnixStream) {
    let mut decoder = FrameDecoder::default();
    let mut buf = vec![0u8; READ_SIZE];
    let why = loop {
        let n = match sock.read(&mut buf) {
            Ok(0) => break "closed by the engine".to_string(),
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => break format!("read failed: {e}"),
        };
        if let Err(e) = decoder.feed(&buf[..n]) {
            break e.0;
        }
        let fatal = loop {
            match decoder.next_frame() {
                Ok(None) => break None,
                Ok(Some(frame)) => dispatch(&inner, frame),
                Err(e) if decoder.failed() => break Some(e.0),
                Err(e) => {
                    let _ = inner.events.send(ClientEvent::ProtocolError(RpcError {
                        code: envelope::codes::PROTOCOL_ERROR.into(),
                        message: e.0,
                        data: None,
                    }));
                }
            }
        };
        if let Some(why) = fatal {
            break why;
        }
    };
    *inner.closed.lock().unwrap() = Some(why.clone());
    let _ = sock.shutdown(std::net::Shutdown::Both);
    let pending: Vec<Pending> = inner.pending.lock().unwrap().drain().map(|(_, p)| p).collect();
    for p in pending {
        complete(p, Err(ClientError::Closed(why.clone())));
    }
    inner.attaching.lock().unwrap().clear();
    inner.streams.lock().unwrap().clear();
    let _ = inner.events.send(ClientEvent::Closed(why));
}

fn complete(pending: Pending, reply: Reply) {
    match pending {
        Pending::Wait(slot) => {
            let (lock, cv) = &*slot;
            *lock.lock().unwrap() = Some(reply);
            cv.notify_all();
        }
        Pending::Call(cb) => cb(reply),
    }
}

fn dispatch(inner: &Inner, frame: Frame) {
    match frame {
        Frame::Output { stream_id, offset, data } => {
            let sink = inner.streams.lock().unwrap().get(&stream_id).cloned();
            if let Some(sink) = sink {
                sink.output(offset, &data);
            }
        }
        Frame::Input { .. } => {}
        Frame::Control(payload) => match envelope::parse_message(&payload) {
            Ok(Message::Response { id, result }) => {
                if let Ok(v) = &result {
                    let sink = inner.attaching.lock().unwrap().remove(&id);
                    if let (Some(sink), Some(sid)) = (sink, v.get("stream_id").and_then(Value::as_u64)) {
                        inner.streams.lock().unwrap().insert(sid as u32, sink);
                    }
                }
                let pending = inner.pending.lock().unwrap().remove(&id);
                match (pending, result) {
                    (Some(p), r) => complete(p, r.map_err(ClientError::Rpc)),
                    (None, Err(e)) => {
                        let _ = inner.events.send(ClientEvent::ProtocolError(e));
                    }
                    (None, Ok(_)) => {}
                }
            }
            Ok(Message::Event(event)) => {
                stream_event(inner, &event);
                let _ = inner.events.send(ClientEvent::Event(event));
            }
            Ok(Message::Request { .. }) => {}
            Err(e) => {
                let _ = inner.events.send(ClientEvent::ProtocolError(RpcError {
                    code: envelope::codes::PROTOCOL_ERROR.into(),
                    message: e,
                    data: None,
                }));
            }
        },
    }
}

fn stream_event(inner: &Inner, event: &Event) {
    let sid = || event.data.get("stream_id").and_then(Value::as_u64).map(|s| s as u32);
    match event.event.as_str() {
        "stream.resized" => {
            if let (Some(sink), Ok(g)) = (
                sid().and_then(|s| inner.streams.lock().unwrap().get(&s).cloned()),
                serde_json::from_value::<StreamGeometry>(event.data.clone()),
            ) {
                sink.resized(&g);
            }
        }
        "stream.reset" => {
            if let (Some(sink), Ok(g)) = (
                sid().and_then(|s| inner.streams.lock().unwrap().remove(&s)),
                serde_json::from_value::<StreamGeometry>(event.data.clone()),
            ) {
                sink.reset(&g);
            }
        }
        "stream.closed" => {
            if let (Some(sink), Ok(c)) = (
                sid().and_then(|s| inner.streams.lock().unwrap().remove(&s)),
                serde_json::from_value::<StreamClosed>(event.data.clone()),
            ) {
                sink.closed(&c);
            }
        }
        _ => {}
    }
}
