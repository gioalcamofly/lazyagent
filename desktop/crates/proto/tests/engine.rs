//! The client against a real Python engine (`python -m lazyagent.engine`)
//! running the fake agent. Skipped without a Python that imports lazyagent
//! (see lazyagent-testkit); required in CI.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use lazyagent_proto::client::{Client, ClientError, ClientEvent};
use lazyagent_proto::discovery::{self, Starter};
use lazyagent_proto::types::*;
use lazyagent_proto::{EngineEvent, envelope::codes};
use lazyagent_testkit::{Recorder, TIMEOUT, World, next_event, wait_for};
use serde_json::{Value, json};

fn env_map(world: &World) -> BTreeMap<String, String> {
    world.env.vars().into_iter().collect()
}

fn event_named(name: &'static str) -> impl FnMut(&ClientEvent) -> Option<EngineEvent> {
    move |ev| match ev {
        ClientEvent::Event(e) if e.event == name => Some(EngineEvent::parse(e.clone())),
        _ => None,
    }
}

/// Subscribes, spawns an agent in the main worktree and returns its
/// session id once `agent.spawned` arrives.
fn spawn(world: &World, client: &Client, rx: &std::sync::mpsc::Receiver<ClientEvent>) -> SpawnResult {
    let _: SubscribeResult = client.call("subscribe", json!({}), TIMEOUT).unwrap();
    let spawned: SpawnResult = client
        .call(
            "spawn_agent",
            json!({"worktree_path": world.repo, "skip_permissions": false, "env": env_map(world)}),
            TIMEOUT,
        )
        .unwrap();
    let ev = next_event(rx, event_named("agent.spawned"));
    let EngineEvent::AgentSpawned(s) = ev else { panic!("{ev:?}") };
    assert_eq!(Some(&s.session_id), spawned.session_id.as_ref());
    spawned
}

#[test]
fn hello_and_sidebar_state() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, hello, _rx) = world.connect();
    assert_eq!(hello.protocol, 1);
    assert_eq!(hello.repo_root, world.repo);
    for cap in ["attach_stream", "claim_size", "get_sidebar_state", "stream.snapshot", "stream.tail"] {
        assert!(hello.capabilities.iter().any(|c| c == cap), "{cap} missing from {:?}", hello.capabilities);
    }
    let state: SidebarState = client.call("get_sidebar_state", json!({}), TIMEOUT).unwrap();
    assert_eq!(state.worktrees.len(), 1);
    assert!(state.worktrees[0].is_main);
    assert_eq!(state.worktrees[0].branch.as_deref(), Some("main"));
    assert!(state.orchestrator.agents.is_empty());
    let config: ConfigView = client.call("get_config", json!({}), TIMEOUT).unwrap();
    assert_eq!(config.agent_provider, "claude");
}

#[test]
fn errors_come_back_as_rpc_errors() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, _, _rx) = world.connect();
    let err = client.request("no_such_method", json!({}), TIMEOUT).unwrap_err();
    assert_eq!(err.code(), Some(codes::UNKNOWN_METHOD));
    let err = client.request("stop_agent", json!({"worktree_path": "/nowhere"}), TIMEOUT).unwrap_err();
    assert!(matches!(err, ClientError::Rpc(_)), "{err:?}");
}

#[test]
fn spawn_attach_type_resize_and_stop() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, _, rx) = world.connect();
    let spawned = spawn(&world, &client, &rx);
    let session = spawned.session_id.unwrap();

    let rec = Recorder::new();
    let attached = client.attach(&session, None, rec.clone(), TIMEOUT).unwrap();
    assert_eq!(attached.mode, "snapshot");
    wait_prompt(&attached, &rec);

    client.send_input(attached.stream_id, b"hello\r").unwrap();
    rec.wait_text("got: 'hello' paste=False");

    let owner: SizeOwner =
        client.call("claim_size", json!({"session_id": session, "rows": 30, "cols": 100}), TIMEOUT).unwrap();
    assert!(owner.owner);
    assert_eq!((owner.rows, owner.cols), (30, 100));
    rec.wait("stream.resized", |s| s.resized.iter().any(|g| g.rows == 30 && g.cols == 100));
    client.send_input(attached.stream_id, b"size\r").unwrap();
    rec.wait_text("size 30x100");

    // A resize from the owner works; offsets never had a gap.
    let r: SizeOwner = client.call("resize", json!({"session_id": session, "rows": 24, "cols": 80}), TIMEOUT).unwrap();
    assert_eq!((r.rows, r.cols), (24, 80));
    assert!(rec.state.lock().unwrap().gaps.is_empty());

    let _: StopResult =
        client.call("stop_agent", json!({"worktree_path": world.repo, "agent_id": spawned.agent_id}), TIMEOUT).unwrap();
    rec.wait("stream.closed", |s| s.closed.is_some());
    let exited = next_event(&rx, event_named("agent.exited"));
    let EngineEvent::AgentExited(e) = exited else { panic!("{exited:?}") };
    assert_eq!(e.stopped, Some(true));
    assert!(!client.has_stream(attached.stream_id));
}

#[test]
fn session_notes_get_put_event_and_conflict() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, hello, rx) = world.connect();
    for cap in ["notes_get", "notes_put"] {
        assert!(hello.capabilities.iter().any(|c| c == cap), "{cap} missing from {:?}", hello.capabilities);
    }
    let spawned = spawn(&world, &client, &rx);
    let target = json!({"worktree_path": world.repo, "agent_id": spawned.agent_id});
    let notes: Notes = client.call("notes_get", target.clone(), TIMEOUT).unwrap();
    assert!(notes.empty);
    assert_eq!((notes.rev, notes.by.as_deref(), notes.state.as_str()), (0, None, "live"));
    assert!(notes.text.starts_with("## Goal\n"), "{:?}", notes.text);
    assert_eq!(notes.size, notes.text.len() as u64);

    let mut put = target.clone();
    put["text"] = json!("## Goal\n- ship N4\n");
    put["if_rev"] = json!(0);
    let written: NotesWritten = client.call("notes_put", put.clone(), TIMEOUT).unwrap();
    assert_eq!((written.rev, written.by.as_deref()), (1, Some("user")));
    let ev = next_event(&rx, event_named("notes.updated"));
    let EngineEvent::NotesUpdated(u) = ev else { panic!("{ev:?}") };
    assert_eq!((u.rev, u.by.as_str(), u.notes_id.as_str()), (1, "user", notes.notes_id.as_str()));
    assert_eq!(u.agent_id, spawned.agent_id);

    // The same if_rev again: refused, with what is there now.
    put["text"] = json!("## Goal\n- mine\n");
    let err = client.request("notes_put", put, TIMEOUT).unwrap_err();
    assert_eq!(err.code(), Some(codes::CONFLICT));
    let current = NotesConflict::of(&err).expect("CONFLICT carries {rev, text}");
    assert_eq!((current.rev, current.text.as_str()), (1, "## Goal\n- ship N4\n"));
    let after: Notes = client.call("notes_get", target, TIMEOUT).unwrap();
    assert_eq!(after.text, "## Goal\n- ship N4\n", "a conflict never overwrites");
}

#[test]
fn reattach_resumes_with_a_tail() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, _, rx) = world.connect();
    let session = spawn(&world, &client, &rx).session_id.unwrap();
    let rec = Recorder::new();
    let first = client.attach(&session, None, rec.clone(), TIMEOUT).unwrap();
    wait_prompt(&first, &rec);
    client.send_input(first.stream_id, b"count 5\r").unwrap();
    rec.wait_text("done");
    let end = rec.state.lock().unwrap().end.unwrap();
    client.detach(first.stream_id);

    let rec2 = Recorder::new();
    let second = client.attach(&session, Some(end), rec2.clone(), TIMEOUT).unwrap();
    assert_eq!(second.mode, "tail");
    assert_eq!(second.offset, end);
    client.send_input(second.stream_id, b"x").unwrap();
    rec2.wait_text("your turn> x");
    assert!(!rec2.text().contains("line 1"), "a tail resends nothing old");
}

#[test]
fn shell_worktrees_and_diff() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, _, rx) = world.connect();
    let _: SubscribeResult = client.call("subscribe", json!({}), TIMEOUT).unwrap();

    let shell: OpenShellResult =
        client.call("open_shell", json!({"worktree_path": world.repo, "env": env_map(&world)}), TIMEOUT).unwrap();
    let rec = Recorder::new();
    let att = client.attach(&shell.session_id, None, rec.clone(), TIMEOUT).unwrap();
    client.send_input(att.stream_id, b"echo shell-$((6*7))\r").unwrap();
    rec.wait_text("shell-42");

    let created: CreateWorktreeResult = client.call("create_worktree", json!({"branch": "feat"}), TIMEOUT).unwrap();
    assert!(created.path.ends_with("repo-feat"), "{created:?}");
    let ev = next_event(&rx, |e| match e {
        ClientEvent::Event(e) if e.event == "worktrees.changed" => match EngineEvent::parse(e.clone()) {
            EngineEvent::WorktreesChanged(w) if w.len() == 2 => Some(w),
            _ => None,
        },
        _ => None,
    });
    assert!(ev.iter().any(|w| w.branch.as_deref() == Some("feat")));

    std::fs::write(std::path::Path::new(&created.path).join("new.txt"), "hello\n").unwrap();
    let diff: DiffResult = client.call("get_diff", json!({"worktree_path": created.path}), TIMEOUT).unwrap();
    assert!(diff.diff.contains("new.txt"), "{}", diff.diff);

    let removed: RemoveWorktreeResult =
        client.call("remove_worktree", json!({"worktree_path": created.path, "force": true}), TIMEOUT).unwrap();
    assert_eq!(removed.removed, created.path);
}

#[test]
fn a_closed_connection_fails_what_is_pending() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, _, rx) = world.connect();
    world.kill_engines();
    let why = next_event(&rx, |e| match e {
        ClientEvent::Closed(why) => Some(why.clone()),
        _ => None,
    });
    assert!(!why.is_empty());
    let err = client.request("get_config", json!({}), TIMEOUT).unwrap_err();
    assert!(matches!(err, ClientError::Closed(_)), "{err:?}");
}

#[test]
fn auto_start_through_lazyagent_engine_start() {
    let Some(world) = World::new() else { return };
    assert!(discovery::find(&world.repo, &world.env).is_none());
    let starter = Starter::from_env(&world.env).expect("LAZYAGENT_PYTHON is set by the world");
    let t = Instant::now();
    let socket = discovery::start_engine(&world.repo, &starter, &world.env, Duration::from_secs(30)).unwrap();
    let cold = t.elapsed();
    assert_eq!(discovery::find(&world.repo, &world.env), Some(socket.clone()));
    // Idempotent: a second start finds the running one.
    let again = discovery::start_engine(&world.repo, &starter, &world.env, Duration::from_secs(30)).unwrap();
    assert_eq!(again, socket);
    let (client, hello, _rx) = Client::connect(&socket, "desktop", "test", TIMEOUT).unwrap();
    assert_eq!(hello.repo_root, world.repo);
    eprintln!("cold engine start through `lazyagent engine start`: {:.0} ms", cold.as_secs_f64() * 1e3);
    let r: Value = client.request("shutdown", json!({}), TIMEOUT).unwrap();
    assert_eq!(r["stopping"], json!(true));
    wait_for(TIMEOUT, "the engine to go", || !discovery::alive(&socket));
}

/// The fake agent is in raw mode once its prompt is out: before that, what
/// we type is eaten by the PTY's line discipline.
fn wait_prompt(attached: &AttachResult, rec: &Recorder) {
    let snapshot = attached.snapshot.clone().unwrap_or_default();
    let deadline = Instant::now() + TIMEOUT;
    while !(snapshot.contains("your turn>") || rec.text().contains("your turn>")) {
        assert!(Instant::now() < deadline, "no prompt; snapshot {snapshot:?}, output {:?}", rec.text());
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    sorted[((sorted.len() - 1) as f64 * p).round() as usize]
}

/// B3: an input frame to the fake agent until the output frame that redraws
/// it, as tests/perf/test_echo.py measures it. `cargo test -- --ignored`.
#[test]
#[ignore]
fn bench_engine_echo() {
    let Some(mut world) = World::new() else { return };
    world.start_engine();
    let (client, _, rx) = world.connect();
    let session = spawn(&world, &client, &rx).session_id.unwrap();
    let rec = Recorder::new();
    let att = client.attach(&session, None, rec.clone(), TIMEOUT).unwrap();
    wait_prompt(&att, &rec);
    let mut samples = vec![];
    for i in 0..330 {
        let key = [b'a' + (i % 26) as u8];
        let before = rec.state.lock().unwrap().bytes.len();
        let t = Instant::now();
        client.send_input(att.stream_id, &key).unwrap();
        rec.wait("echo", |s| s.bytes.len() > before && s.bytes.ends_with(&key));
        if i >= 30 {
            samples.push(t.elapsed().as_secs_f64() * 1e3);
        }
        std::thread::sleep(Duration::from_millis(2));
    }
    samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let (p50, p95, p99) = (percentile(&samples, 0.5), percentile(&samples, 0.95), percentile(&samples, 0.99));
    println!(
        "B3 echo from Rust, idle: p50 {p50:.2} / p95 {p95:.2} / p99 {p99:.2} / max {:.2} ms",
        samples[samples.len() - 1]
    );
    assert!(p95 <= 6.0, "p95 {p95}");
}
