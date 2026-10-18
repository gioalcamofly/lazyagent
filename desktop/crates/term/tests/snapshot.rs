//! The engine's attach snapshot (engine-split §3b) rebuilt in our emulator
//! shows what the engine's own screen shows (`read_agent_output`).

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use lazyagent_proto::client::StreamSink;
use lazyagent_proto::types::{ReadOutputResult, SpawnResult, SubscribeResult};
use lazyagent_term::{Palette, Terminal};
use lazyagent_testkit::{TIMEOUT, World};
use serde_json::json;

struct Sink(Arc<Mutex<Terminal>>);

impl StreamSink for Sink {
    fn output(&self, _offset: u64, data: &[u8]) {
        self.0.lock().unwrap().feed(data);
    }
}

#[test]
fn snapshot_then_live_matches_the_engine_screen() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, _, _rx) = world.connect();
    let _: SubscribeResult = client.call("subscribe", json!({}), TIMEOUT).unwrap();
    let env: std::collections::BTreeMap<String, String> = world.env.vars().into_iter().collect();
    let agent: SpawnResult = client
        .call("spawn_agent", json!({"worktree_path": world.repo, "skip_permissions": false, "env": env}), TIMEOUT)
        .unwrap();
    let session = agent.session_id.unwrap();

    // Some history first, from a stream we then drop.
    let warm = Arc::new(Mutex::new(Terminal::new(24, 80, Palette::dark())));
    let first = client.attach(&session, None, Arc::new(Sink(warm.clone())), TIMEOUT).unwrap();
    let wait_text = |term: &Arc<Mutex<Terminal>>, needle: &str| {
        let deadline = Instant::now() + TIMEOUT;
        loop {
            let text = term.lock().unwrap().frame().text();
            if text.contains(needle) {
                return;
            }
            assert!(Instant::now() < deadline, "{needle:?} not on screen:\n{text}");
            std::thread::sleep(Duration::from_millis(10));
        }
    };
    if !first.snapshot.as_deref().unwrap_or("").contains("your turn>") {
        wait_text(&warm, "your turn>");
    }
    client.send_input(first.stream_id, b"count 60\r").unwrap();
    wait_text(&warm, "done");
    client.detach(first.stream_id);

    // A new attach: snapshot into an empty terminal of its size, then live.
    let term = Arc::new(Mutex::new(Terminal::new(1, 1, Palette::dark())));
    let sink = Arc::new(Sink(term.clone()));
    // The snapshot is applied in the attach callback, on the reader thread,
    // before any output frame of the stream: since E1 the frames carry the
    // backlog the engine had not parsed yet.
    let (tx, rx) = std::sync::mpsc::channel();
    let t2 = term.clone();
    client.attach_async(&session, None, sink, move |r| {
        let attached = r.unwrap();
        let mut t = t2.lock().unwrap();
        t.reset(attached.size.rows as usize, attached.size.cols as usize);
        t.feed(attached.snapshot.as_deref().unwrap_or("").as_bytes());
        tx.send(attached).unwrap();
    });
    let attached = rx.recv_timeout(TIMEOUT).unwrap();
    assert_eq!(attached.mode, "snapshot");
    std::thread::sleep(Duration::from_millis(200));
    let rows = attached.size.rows as usize;
    let engine: ReadOutputResult = client
        .call(
            "read_agent_output",
            json!({"worktree_path": world.repo, "agent_id": agent.agent_id, "lines": rows}),
            TIMEOUT,
        )
        .unwrap();
    let ours = term.lock().unwrap().frame();
    let ours: Vec<String> = (0..ours.rows).map(|r| ours.row_text(r)).collect();
    let theirs: Vec<String> = engine.lines.iter().map(|l| l.trim_end().to_string()).collect();
    // read_agent_output returns the last `rows` lines of history + screen.
    let tail = |v: &[String]| v.iter().rev().skip_while(|l| l.is_empty()).cloned().collect::<Vec<_>>();
    assert_eq!(tail(&ours), tail(&theirs), "\nours:\n{}\ntheirs:\n{}", ours.join("\n"), theirs.join("\n"));
    assert!(ours.iter().any(|l| l == "line 60"));
}
