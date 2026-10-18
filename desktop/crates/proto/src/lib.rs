//! Client side of the lazyagent engine's protocol v1
//! (`docs/design/engine-split.md` §4): framing, envelope, typed messages,
//! a thread-based client, and engine discovery / auto-start.

pub mod client;
pub mod discovery;
pub mod envelope;
pub mod framing;
pub mod types;

pub use client::{Client, ClientError, ClientEvent, StreamSink};
pub use envelope::{Event, RpcError};
pub use types::EngineEvent;
