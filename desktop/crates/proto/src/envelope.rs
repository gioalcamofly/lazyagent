//! The JSON envelope (engine-split §4b): requests, responses, events.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Error codes are an open set: compare with these, never match exhaustively.
pub mod codes {
    pub const UNKNOWN_METHOD: &str = "UNKNOWN_METHOD";
    pub const WORKTREE_ERROR: &str = "WORKTREE_ERROR";
    pub const VALIDATION_ERROR: &str = "VALIDATION_ERROR";
    pub const TIMEOUT: &str = "TIMEOUT";
    pub const INTERNAL_ERROR: &str = "INTERNAL_ERROR";
    pub const UNAUTHORIZED: &str = "UNAUTHORIZED";
    pub const UNSUPPORTED_VERSION: &str = "UNSUPPORTED_VERSION";
    pub const NOT_SIZE_OWNER: &str = "NOT_SIZE_OWNER";
    pub const NO_HOSTED_UI: &str = "NO_HOSTED_UI";
    pub const PROTOCOL_ERROR: &str = "PROTOCOL_ERROR";
    /// A stale `if_rev` (session notes); `data` carries the current `{rev, text}`.
    pub const CONFLICT: &str = "CONFLICT";
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RpcError {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl std::fmt::Display for RpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Request<'a> {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub id: String,
    pub method: &'a str,
    pub params: &'a Value,
}

impl<'a> Request<'a> {
    pub fn new(id: String, method: &'a str, params: &'a Value) -> Self {
        Self { kind: "req", id, method, params }
    }
}

/// A pushed event. `data` stays untyped here; [`crate::types::EngineEvent`]
/// gives the known ones a shape.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Event {
    pub seq: u64,
    pub event: String,
    #[serde(default)]
    pub data: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Message {
    Response {
        id: String,
        result: Result<Value, RpcError>,
    },
    Event(Event),
    /// An engine never sends requests to a client; kept so a peer that does
    /// is reported instead of misread.
    Request {
        id: String,
        method: String,
    },
}

#[derive(Deserialize)]
struct Raw {
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    id: Option<Value>,
    #[serde(default)]
    method: Option<String>,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<RpcError>,
    #[serde(default)]
    seq: Option<u64>,
    #[serde(default)]
    event: Option<String>,
    #[serde(default)]
    data: Option<Value>,
}

fn id_string(id: Option<Value>) -> String {
    match id {
        Some(Value::String(s)) => s,
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

/// Parses one control payload. Unknown fields are ignored (§4c).
pub fn parse_message(payload: &[u8]) -> Result<Message, String> {
    let raw: Raw = serde_json::from_slice(payload).map_err(|e| format!("bad control message: {e}"))?;
    match raw.kind.as_str() {
        "res" => {
            let result = match (raw.error, raw.result) {
                (Some(e), _) => Err(e),
                (None, r) => Ok(r.unwrap_or(Value::Null)),
            };
            Ok(Message::Response { id: id_string(raw.id), result })
        }
        "evt" => Ok(Message::Event(Event {
            seq: raw.seq.unwrap_or(0),
            event: raw.event.ok_or("event without a name")?,
            data: raw.data.unwrap_or(Value::Null),
        })),
        "req" => Ok(Message::Request { id: id_string(raw.id), method: raw.method.unwrap_or_default() }),
        other => Err(format!("unknown message type {other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_responses_events_and_errors() {
        let ok = parse_message(br#"{"type":"res","id":"3","result":{"a":1},"extra":true}"#).unwrap();
        assert_eq!(ok, Message::Response { id: "3".into(), result: Ok(json!({"a":1})) });
        let err =
            parse_message(br#"{"type":"res","id":"","error":{"code":"PROTOCOL_ERROR","message":"bad"}}"#).unwrap();
        let Message::Response { id, result: Err(e) } = err else { panic!() };
        assert_eq!((id.as_str(), e.code.as_str()), ("", codes::PROTOCOL_ERROR));
        let evt = parse_message(br#"{"type":"evt","seq":12,"event":"agent.updated","data":{"x":1}}"#).unwrap();
        assert_eq!(evt, Message::Event(Event { seq: 12, event: "agent.updated".into(), data: json!({"x":1}) }));
        let null_result = parse_message(br#"{"type":"res","id":"1","result":null}"#).unwrap();
        assert_eq!(null_result, Message::Response { id: "1".into(), result: Ok(Value::Null) });
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse_message(b"[1]").is_err());
        assert!(parse_message(br#"{"type":"nope"}"#).is_err());
        assert!(parse_message(b"\xff").is_err());
    }

    #[test]
    fn request_shape() {
        let params = json!({"since": 0});
        let text = serde_json::to_string(&Request::new("7".into(), "subscribe", &params)).unwrap();
        assert_eq!(text, r#"{"type":"req","id":"7","method":"subscribe","params":{"since":0}}"#);
    }
}
