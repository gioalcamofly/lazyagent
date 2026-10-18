//! Byte framing of protocol v1 over a stream socket (engine-split §4a).
//!
//! Every frame is `u32 length` · `u8 kind` · payload, big-endian, where
//! `length` counts the payload only. Kind 1 is a UTF-8 JSON control
//! message, kind 2 a binary stream frame: `u8 type` · `u32 stream_id` ·
//! `u64 offset` (output only) · data.

use std::fmt;

pub const KIND_CONTROL: u8 = 1;
pub const KIND_STREAM: u8 = 2;
pub const STREAM_OUTPUT: u8 = 1;
pub const STREAM_INPUT: u8 = 2;
pub const HEADER_SIZE: usize = 5;
const OUTPUT_HEADER: usize = 13;
const INPUT_HEADER: usize = 5;
/// A larger length prefix almost always means the peer is not speaking
/// this protocol, so we fail instead of buffering it.
pub const MAX_FRAME_SIZE: usize = 16 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameError(pub String);

impl fmt::Display for FrameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for FrameError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Frame {
    /// The JSON payload, not parsed yet.
    Control(Vec<u8>),
    Output {
        stream_id: u32,
        offset: u64,
        data: Vec<u8>,
    },
    Input {
        stream_id: u32,
        data: Vec<u8>,
    },
}

fn wrap(kind: u8, payload_len: usize, out: &mut Vec<u8>) {
    assert!(payload_len <= MAX_FRAME_SIZE, "frame payload of {payload_len} bytes exceeds {MAX_FRAME_SIZE}");
    out.extend_from_slice(&(payload_len as u32).to_be_bytes());
    out.push(kind);
}

/// A complete control frame for a JSON payload.
pub fn encode_control(json: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER_SIZE + json.len());
    wrap(KIND_CONTROL, json.len(), &mut out);
    out.extend_from_slice(json);
    out
}

/// A complete input stream frame (client → engine).
pub fn encode_input(stream_id: u32, data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER_SIZE + INPUT_HEADER + data.len());
    wrap(KIND_STREAM, INPUT_HEADER + data.len(), &mut out);
    out.push(STREAM_INPUT);
    out.extend_from_slice(&stream_id.to_be_bytes());
    out.extend_from_slice(data);
    out
}

/// A complete output stream frame (engine → client); tests and fakes use it.
pub fn encode_output(stream_id: u32, offset: u64, data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER_SIZE + OUTPUT_HEADER + data.len());
    wrap(KIND_STREAM, OUTPUT_HEADER + data.len(), &mut out);
    out.push(STREAM_OUTPUT);
    out.extend_from_slice(&stream_id.to_be_bytes());
    out.extend_from_slice(&offset.to_be_bytes());
    out.extend_from_slice(data);
    out
}

/// Stream frame payload → frame; the inverse of the stream encoders minus
/// the outer header.
pub fn decode_stream(payload: &[u8]) -> Result<Frame, FrameError> {
    let Some(&kind) = payload.first() else {
        return Err(FrameError("empty stream frame".into()));
    };
    let u32_at = |i: usize| u32::from_be_bytes(payload[i..i + 4].try_into().unwrap());
    match kind {
        STREAM_OUTPUT => {
            if payload.len() < OUTPUT_HEADER {
                return Err(FrameError(format!(
                    "output stream frame too short: {} bytes, need at least {OUTPUT_HEADER}",
                    payload.len()
                )));
            }
            let offset = u64::from_be_bytes(payload[5..13].try_into().unwrap());
            Ok(Frame::Output { stream_id: u32_at(1), offset, data: payload[OUTPUT_HEADER..].to_vec() })
        }
        STREAM_INPUT => {
            if payload.len() < INPUT_HEADER {
                return Err(FrameError(format!(
                    "input stream frame too short: {} bytes, need at least {INPUT_HEADER}",
                    payload.len()
                )));
            }
            Ok(Frame::Input { stream_id: u32_at(1), data: payload[INPUT_HEADER..].to_vec() })
        }
        other => Err(FrameError(format!("unknown stream frame type {other}; expected 1 (output) or 2 (input)"))),
    }
}

/// Incremental decoder: [`feed`](Self::feed) bytes as they arrive and pull
/// frames with [`next_frame`](Self::next_frame).
///
/// A bad header (unknown kind, oversized length) makes the byte stream
/// unrecoverable, so the decoder stays failed. A bad payload inside a
/// well-delimited frame is consumed before the error is returned, so the
/// caller may report it and carry on.
#[derive(Debug)]
pub struct FrameDecoder {
    buf: Vec<u8>,
    start: usize,
    max: usize,
    failed: Option<FrameError>,
}

impl Default for FrameDecoder {
    fn default() -> Self {
        Self::new(MAX_FRAME_SIZE)
    }
}

impl FrameDecoder {
    pub fn new(max_frame_size: usize) -> Self {
        Self { buf: Vec::with_capacity(256 * 1024), start: 0, max: max_frame_size, failed: None }
    }

    /// Bytes received but not yet returned as a frame.
    pub fn buffered(&self) -> usize {
        self.buf.len() - self.start
    }

    pub fn failed(&self) -> bool {
        self.failed.is_some()
    }

    pub fn feed(&mut self, data: &[u8]) -> Result<(), FrameError> {
        if let Some(e) = &self.failed {
            return Err(e.clone());
        }
        // Drop consumed bytes only when they dominate, so a stream of small
        // frames does not memmove the buffer on every read.
        if self.start > 0 && self.start >= self.buf.len() / 2 {
            self.buf.drain(..self.start);
            self.start = 0;
        }
        self.buf.extend_from_slice(data);
        Ok(())
    }

    /// The next complete frame, or `None` when more bytes are needed.
    pub fn next_frame(&mut self) -> Result<Option<Frame>, FrameError> {
        if let Some(e) = &self.failed {
            return Err(e.clone());
        }
        let avail = &self.buf[self.start..];
        if avail.len() < HEADER_SIZE {
            return Ok(None);
        }
        let length = u32::from_be_bytes(avail[..4].try_into().unwrap()) as usize;
        let kind = avail[4];
        if kind != KIND_CONTROL && kind != KIND_STREAM {
            return Err(self.fail(format!("unknown frame kind {kind}; expected 1 (control) or 2 (stream)")));
        }
        if length > self.max {
            return Err(self.fail(format!("frame length {length} exceeds the maximum of {} bytes", self.max)));
        }
        if avail.len() < HEADER_SIZE + length {
            return Ok(None);
        }
        let payload = &avail[HEADER_SIZE..HEADER_SIZE + length];
        let frame = if kind == KIND_CONTROL { Ok(Frame::Control(payload.to_vec())) } else { decode_stream(payload) };
        self.start += HEADER_SIZE + length;
        if self.start == self.buf.len() {
            self.buf.clear();
            self.start = 0;
        }
        frame.map(Some)
    }

    fn fail(&mut self, msg: String) -> FrameError {
        let e = FrameError(msg);
        self.failed = Some(e.clone());
        e
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode_all(bytes: &[u8]) -> Vec<Frame> {
        let mut d = FrameDecoder::default();
        d.feed(bytes).unwrap();
        let mut out = vec![];
        while let Some(f) = d.next_frame().unwrap() {
            out.push(f);
        }
        out
    }

    #[test]
    fn wire_layout_matches_the_python_framing() {
        // Python: encode_control({"a":1}) and encode_stream(output(7, 2**40, b"hi"))
        assert_eq!(encode_control(br#"{"a":1}"#), b"\x00\x00\x00\x07\x01{\"a\":1}");
        assert_eq!(
            encode_output(7, 1 << 40, b"hi"),
            b"\x00\x00\x00\x0f\x02\x01\x00\x00\x00\x07\x00\x00\x01\x00\x00\x00\x00\x00hi"
        );
        assert_eq!(encode_input(7, b"x"), b"\x00\x00\x00\x06\x02\x02\x00\x00\x00\x07x");
    }

    #[test]
    fn round_trip_in_one_byte_pieces() {
        let mut wire = encode_control(b"{}");
        wire.extend(encode_output(3, 9, b"out"));
        wire.extend(encode_input(4, b""));
        let mut d = FrameDecoder::default();
        let mut got = vec![];
        for b in &wire {
            d.feed(std::slice::from_ref(b)).unwrap();
            while let Some(f) = d.next_frame().unwrap() {
                got.push(f);
            }
        }
        assert_eq!(
            got,
            vec![
                Frame::Control(b"{}".to_vec()),
                Frame::Output { stream_id: 3, offset: 9, data: b"out".to_vec() },
                Frame::Input { stream_id: 4, data: vec![] },
            ]
        );
        assert_eq!(d.buffered(), 0);
        assert_eq!(decode_all(&wire).len(), 3);
    }

    #[test]
    fn unknown_kind_is_fatal() {
        let mut d = FrameDecoder::default();
        d.feed(b"\x00\x00\x00\x00\x09").unwrap();
        assert!(d.next_frame().is_err());
        assert!(d.failed());
        assert!(d.feed(b"more").is_err());
    }

    #[test]
    fn oversized_length_is_fatal() {
        let mut d = FrameDecoder::new(10);
        d.feed(b"\x00\x00\x00\x0b\x01").unwrap();
        assert!(d.next_frame().unwrap_err().0.contains("exceeds"));
        assert!(d.failed());
    }

    #[test]
    fn bad_stream_payload_is_skipped() {
        let mut wire = b"\x00\x00\x00\x03\x02\x01\x00\x00".to_vec();
        wire.extend(encode_control(b"{}"));
        let mut d = FrameDecoder::default();
        d.feed(&wire).unwrap();
        assert!(d.next_frame().unwrap_err().0.contains("too short"));
        assert!(!d.failed());
        assert_eq!(d.next_frame().unwrap(), Some(Frame::Control(b"{}".to_vec())));
        let mut d = FrameDecoder::default();
        d.feed(b"\x00\x00\x00\x01\x02\x07").unwrap();
        assert!(d.next_frame().unwrap_err().0.contains("unknown stream frame type 7"));
    }

    #[test]
    fn many_small_frames_keep_the_buffer_bounded() {
        let mut d = FrameDecoder::default();
        let frame = encode_output(1, 0, &[b'x'; 100]);
        for _ in 0..10_000 {
            d.feed(&frame).unwrap();
            assert!(d.next_frame().unwrap().is_some());
        }
        assert!(d.buf.capacity() < 1024 * 1024);
    }
}
