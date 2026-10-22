//! Headless UI tests (egui_kittest): no window, no engine. The store is
//! seeded through the same messages the connection sends, terminals are
//! fed directly, and keys go through egui like real ones. The render test
//! draws through wgpu (lavapipe in CI).

use std::sync::mpsc;
use std::time::Instant;

use eframe::egui;
use egui_kittest::Harness;
use egui_kittest::kittest::Queryable;
use lazyagent_proto::discovery::Env;
use lazyagent_proto::types::SidebarState;
use lazyagent_proto::{EngineEvent, Event};
use serde_json::json;

use crate::app::{App, State};
use crate::conn::{Conn, Link, Msg};
use crate::fonts::FontSet;
use crate::panes::PaneKind;
use crate::theme::ThemeKind;

fn agent(id: &str, label: &str, status: &str, session: &str) -> serde_json::Value {
    json!({"agent_id": id, "label": label, "session_name": "", "status": status, "confidence": "high",
           "detail": "", "session_id": session, "kind": "agent"})
}

fn worktree(path: &str, branch: &str, agents: Vec<serde_json::Value>) -> serde_json::Value {
    json!({"path": path, "branch": branch, "head": "abc1234", "is_main": path == "/r/repo", "is_bare": false,
           "agent_status": "running", "agents": agents, "git_status": {"dirty_count": 2, "ahead": 1, "behind": 0}})
}

fn app(worktrees: Vec<serde_json::Value>) -> (App, mpsc::Sender<Msg>) {
    let (tx, rx) = mpsc::channel();
    // The app's state file goes to a temporary dir, never the user's.
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/r/repo".into(), env, tx.clone(), egui::Context::default());
    let state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": worktrees, "orchestrator": {"agents": []}, "pr_info": {}, "git_statuses": {},
        "view": null, "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    (App::new(state, rx, None), tx)
}

fn harness(app: App) -> Harness<'static, App> {
    Harness::builder().with_size([1400.0, 880.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app)
}

fn event(name: &str, data: serde_json::Value) -> Msg {
    Msg::Event(EngineEvent::parse(Event { seq: 2, event: name.into(), data }))
}

#[test]
fn sidebar_shows_worktrees_agents_and_follows_events() {
    let (app, tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-feat", "feat", vec![agent("a1", "Implementer", "running", "s1")]),
    ]);
    let mut h = harness(app);
    h.run_steps(3);
    assert!(h.query_all_by_label("Implementer").count() >= 1);
    assert!(h.query_all_by_label("repo-feat").count() >= 1);
    assert!(h.query_all_by_label("●2").count() >= 1);
    tx.send(event(
        "agent.updated",
        json!({"worktree_path": "/r/repo-feat", "agent_id": "a1", "status": "waiting_for_user", "confidence": "high", "detail": ""}),
    ))
    .unwrap();
    tx.send(event(
        "agent.spawned",
        json!({"worktree_path": "/r/repo", "agent_id": "a2", "session_id": "s2", "label": "Reviewer", "session_name": "", "kind": "agent"}),
    ))
    .unwrap();
    h.run_steps(2);
    assert!(h.query_all_by_label("input").count() >= 1, "the waiting agent shows its status word");
    assert!(h.query_all_by_label("Reviewer").count() >= 1);
    h.get_by_label("2 worktrees · 2 agents");
}

#[test]
fn typing_into_the_focused_terminal_writes_bytes() {
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![agent("a1", "Agent", "running", "s1")])]);
    let mut h = harness(app);
    h.run_steps(3);
    let pane = h.state().state.panes.get("s1").cloned().expect("the agent's pane is shown");
    h.event(egui::Event::Text("ls -l".into()));
    h.key_press(egui::Key::Enter);
    h.key_press_modifiers(egui::Modifiers::ALT, egui::Key::B);
    // egui turns Ctrl+C into Copy: with no selection it is ^C.
    h.event(egui::Event::Copy);
    h.key_press(egui::Key::ArrowUp);
    h.run_steps(2);
    // Not attached (no engine): the bytes wait in the pane, in order.
    assert_eq!(pane.inner.lock().unwrap().held, b"ls -l\r\x1bb\x03\x1b[A");
}

#[test]
fn ctrl_c_with_a_selection_copies_instead() {
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![agent("a1", "Agent", "running", "s1")])]);
    let mut h = harness(app);
    h.run_steps(2);
    let pane = h.state().state.panes.get("s1").cloned().unwrap();
    {
        let mut i = pane.inner.lock().unwrap();
        i.term.feed(b"copy me");
        i.term.select_start(0, 0, false, 2);
    }
    h.event(egui::Event::Copy);
    h.run_steps(1);
    assert!(pane.inner.lock().unwrap().held.is_empty(), "no ^C sent");
    assert!(pane.inner.lock().unwrap().term.selection_text().is_none(), "the selection is cleared once copied");
}

#[test]
fn sidebar_with_100_worktrees_stays_cheap() {
    let trees: Vec<_> = (0..100)
        .map(|i| {
            worktree(
                &format!("/r/repo-{i}"),
                &format!("branch-{i}"),
                vec![
                    agent("a1", "Agent", "running", &format!("s{i}a")),
                    agent("a2", "Agent 2", "waiting", &format!("s{i}b")),
                ],
            )
        })
        .collect();
    let (app, tx) = app(trees);
    let mut h = harness(app);
    h.run_steps(3);
    let mut times = vec![];
    for n in 0..40 {
        // Every row changes.
        for i in 0..100 {
            let status = if n % 2 == 0 { "waiting_for_user" } else { "running" };
            tx.send(event(
                "agent.updated",
                json!({"worktree_path": format!("/r/repo-{i}"), "agent_id": "a1", "status": status, "confidence": "high", "detail": ""}),
            ))
            .unwrap();
        }
        let t = Instant::now();
        h.step();
        times.push(t.elapsed().as_secs_f64() * 1e3);
    }
    times.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let (p50, p95) = (times[times.len() / 2], times[times.len() * 95 / 100]);
    println!("B12 sidebar, 100 worktrees, every row changed: frame p50 {p50:.2} / p95 {p95:.2} ms");
    // A budget only means something in a release build on a quiet machine:
    // debug builds on a shared CI runner, next to the real-engine tests,
    // measured 40-65 ms. `cargo test --release sidebar_with_100` checks it.
    if !cfg!(debug_assertions) {
        assert!(p95 < 4.0, "p95 {p95} ms");
    }
    assert_eq!(h.state().state.store.worktrees.len(), 100);
}

#[test]
fn terminal_cells_render_on_the_gpu() {
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![agent("a1", "Agent", "running", "s1")])]);
    let mut h =
        Harness::builder().with_size([900.0, 500.0]).wgpu().build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    let ctx = h.ctx.clone();
    let pane = h.state_mut().state.pane("s1", PaneKind::Agent, &ctx);
    pane.inner.lock().unwrap().term.feed(b"\x1b[41m          \x1b[0m \x1b[32mgreen text\x1b[0m");
    h.run_steps(2);
    let image = match h.render() {
        Ok(i) => i,
        Err(e) => {
            // No GPU and no lavapipe: say so instead of failing obscurely.
            if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") {
                panic!("render: {e}");
            }
            eprintln!("skipped: no wgpu adapter ({e})");
            return;
        }
    };
    let out = std::env::temp_dir().join("lazyagent-desktop-terminal-render.png");
    image.save(&out).unwrap();
    // The red background run (Tango red, cc0000) and green glyph pixels.
    let near = |p: &image::Rgba<u8>, r: u8, g: u8, b: u8| {
        (p[0] as i32 - r as i32).abs() < 12
            && (p[1] as i32 - g as i32).abs() < 12
            && (p[2] as i32 - b as i32).abs() < 12
    };
    let red = image.pixels().filter(|p| near(p, 0xcc, 0, 0)).count();
    let green = image.pixels().filter(|p| p[1] > 120 && p[0] < 110 && p[2] < 60).count();
    println!("render: {} red px, {green} green px ({})", red, out.display());
    assert!(red > 500, "the red cells are drawn ({red} px)");
    assert!(green > 50, "the green glyphs are drawn ({green} px)");
}

#[test]
fn a_shell_that_cannot_open_is_not_retried_every_frame() {
    let (app, tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    let mut h = harness(app);
    h.run_steps(5);
    // Connected per the link, but no client: the request fails once.
    assert_eq!(
        h.state().state.toasts.len(),
        1,
        "{:?}",
        h.state().state.toasts.iter().map(|t| &t.text).collect::<Vec<_>>()
    );
    tx.send(Msg::Link(Link::Reconnecting("lost".into()))).unwrap();
    h.run_steps(5);
    assert_eq!(h.state().state.toasts.len(), 1, "nothing asked while disconnected");
}

/// Steps the harness until `ok` holds (the engine answers on other threads).
fn step_until(h: &mut Harness<'static, App>, what: &str, mut ok: impl FnMut(&App) -> bool) {
    let deadline = Instant::now() + std::time::Duration::from_secs(20);
    while !ok(h.state()) {
        if Instant::now() > deadline {
            let s = &h.state().state;
            panic!(
                "{what}: not reached\nlink {:?}\ndialog {:?}\ntoasts {:?}\nworktrees {:?}\njobs {:?}\nrestorable {}",
                s.link,
                s.dialog.as_ref().map(|d| (d.kind, d.busy, d.error.clone())),
                s.toasts.iter().map(|t| t.text.clone()).collect::<Vec<_>>(),
                s.store.worktrees.iter().map(|w| (w.path.clone(), w.agents.len())).collect::<Vec<_>>(),
                s.jobs.iter().map(|j| (j.id.clone(), j.finished)).collect::<Vec<_>>(),
                s.restorable.len(),
            );
        }
        h.step();
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
}

#[test]
fn against_a_real_engine_spawn_type_and_see_the_echo() {
    let Some(mut world) = lazyagent_testkit::World::new() else { return };
    world.start_engine();
    let (tx, rx) = mpsc::channel();
    let ctx = egui::Context::default();
    let conn = Conn::new(world.repo.clone(), world.env.clone(), tx, ctx);
    conn.spawn_supervisor();
    let state = State::new(conn.clone(), FontSet::bundled_only(), Some(ThemeKind::Dark));
    let mut h = harness(App::new(state, rx, None));
    step_until(&mut h, "connected and seeded", |a| a.state.seeded && a.state.link == Link::Connected);
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    // The dialog: an instruction, then Spawn.
    h.event(egui::Event::Text("fix the bug".into()));
    h.get_by_label("Spawn").click();
    step_until(&mut h, "the agent's prompt on screen", |a| {
        a.state.panes.values().any(|p| p.inner.lock().unwrap().term.frame().text().contains("your turn>"))
    });
    h.event(egui::Event::Text("hello desktop".into()));
    h.key_press(egui::Key::Enter);
    step_until(&mut h, "the fake agent's answer", |a| {
        a.state.panes.values().any(|p| p.inner.lock().unwrap().term.frame().text().contains("got: 'hello desktop'"))
    });
    // The shell pane opened too, in the same worktree.
    step_until(&mut h, "the shell", |a| a.state.shells.contains_key(&world.repo));
    conn.shutdown();
}

/// The app on a real engine in a fresh world (None: no Python engine here).
fn world_app() -> Option<(lazyagent_testkit::World, Harness<'static, App>, std::sync::Arc<Conn>)> {
    let mut world = lazyagent_testkit::World::new()?;
    world.start_engine();
    let (tx, rx) = mpsc::channel();
    let conn = Conn::new(world.repo.clone(), world.env.clone(), tx, egui::Context::default());
    conn.spawn_supervisor();
    let state = State::new(conn.clone(), FontSet::bundled_only(), Some(ThemeKind::Dark));
    let mut h = harness(App::new(state, rx, None));
    step_until(&mut h, "connected and seeded", |a| a.state.seeded && a.state.link == Link::Connected);
    Some((world, h, conn))
}

#[test]
fn create_and_launch_then_stop_then_remove() {
    let Some((world, mut h, conn)) = world_app() else { return };
    h.get_by_label("+ New").click();
    h.run_steps(2);
    h.event(egui::Event::Text("feat-x".into()));
    h.run_steps(2);
    h.get_by_label("and launch an agent in it").click();
    // The dialog grows; the modal takes its new size a frame later.
    h.run_steps(3);
    h.get_by_label("Create").click();
    let path = format!("{}-feat-x", world.repo);
    step_until(&mut h, "the new worktree selected, with an agent", |a| {
        a.state.selected.as_deref() == Some(path.as_str()) && !a.state.store.agents(&path).is_empty()
    });
    // Remove is refused while the agent runs.
    h.get_by_label("Remove").click();
    h.run_steps(2);
    assert!(h.query_all_by_label("An agent is running in this worktree: stop it first (x).").count() >= 1);
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert!(h.state().state.dialog.is_none());
    // Stop it through the confirm dialog.
    h.get_by_label("Stop").click();
    h.run_steps(2);
    h.key_press(egui::Key::Y);
    step_until(&mut h, "the agent gone", |a| a.state.store.agents(&path).is_empty());
    // Now the remove goes through, and the selection falls back to main.
    h.get_by_label("Remove").click();
    h.run_steps(2);
    h.key_press(egui::Key::Y);
    step_until(&mut h, "the worktree removed", |a| {
        a.state.store.worktree(&path).is_none() && a.state.selected.as_deref() == Some(world.repo.as_str())
    });
    assert!(!std::path::Path::new(&path).exists());
    conn.shutdown();
}

#[test]
fn a_failing_custom_create_shows_its_output_and_launches_nothing() {
    let Some(world) = lazyagent_testkit::World::new() else { return };
    std::fs::write(
        std::path::Path::new(&world.repo).join(".lazyagent.toml"),
        "[worktree]\ncreate = \"echo boom-{branch}; exit 3\"\n",
    )
    .unwrap();
    let mut world = world;
    world.start_engine();
    let (tx, rx) = mpsc::channel();
    let conn = Conn::new(world.repo.clone(), world.env.clone(), tx, egui::Context::default());
    conn.spawn_supervisor();
    let mut h = harness(App::new(State::new(conn.clone(), FontSet::bundled_only(), Some(ThemeKind::Dark)), rx, None));
    step_until(&mut h, "seeded with the config", |a| a.state.seeded && a.state.store.config.is_some());
    h.get_by_label("+ New").click();
    h.run_steps(2);
    h.event(egui::Event::Text("broken".into()));
    h.run_steps(2);
    h.get_by_label("and launch an agent in it").click();
    // The dialog grows; the modal takes its new size a frame later.
    h.run_steps(3);
    h.get_by_label("Create").click();
    // The engine waits for the job and answers with its failure: the dialog
    // keeps it, the job log has the output, nothing is launched.
    step_until(&mut h, "the failure in the dialog", |a| {
        a.state.dialog.as_ref().and_then(|d| d.error.as_deref()).is_some_and(|e| e.contains("boom-broken"))
    });
    // The reply can come before the job's events: wait for its end too.
    step_until(&mut h, "the finished job", |a| a.state.jobs.first().is_some_and(|j| j.finished.is_some()));
    let app = h.state();
    let job = &app.state.jobs[0];
    assert_eq!(job.finished, Some(Some(3)));
    assert!(job.output.contains("boom-broken"), "{:?}", job.output);
    assert!(app.state.jobs_open);
    assert_eq!(app.state.store.worktrees.len(), 1);
    assert_eq!(app.state.store.agent_count(), 0);
    conn.shutdown();
}

#[test]
fn agents_of_a_stopped_engine_are_offered_back_and_restored() {
    let Some((mut world, mut h, conn)) = world_app() else { return };
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    // The engine goes away (its agents with it) and a new one starts.
    world.terminate_engines();
    world.start_engine();
    let (probe, _, _rx) = world.connect();
    let offered = probe.request("list_restorable", json!({}), lazyagent_testkit::TIMEOUT);
    eprintln!("engine offers: {offered:?}");
    step_until(&mut h, "the restore offer", |a| a.state.link == Link::Connected && a.state.restorable.len() == 1);
    h.get_by_label("Restore all (Shift+R)").click();
    step_until(&mut h, "the agent back", |a| a.state.store.agent_count() == 1 && a.state.restorable.is_empty());
    conn.shutdown();
}

fn restorable(id: &str, worktree_path: &str, agent_id: &str, kind: &str) -> serde_json::Value {
    json!({"id": id, "worktree_path": worktree_path, "agent_id": agent_id, "kind": kind, "provider": "claude",
           "label": "", "session_name": "", "skip_permissions": false, "conversation_id": null,
           "instruction": null, "started_at": 1.0, "engine_id": null, "ended_at": 2.0})
}

#[test]
fn the_restore_banner_follows_restorable_changed() {
    let (app, tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    let mut h = harness(app);
    h.run_steps(3);
    tx.send(event("restorable.changed", json!({"agents": [restorable("e1", "__orchestrator__", "", "orchestrator")]})))
        .unwrap();
    h.run_steps(3);
    let banner = "1 agent(s) from an earlier session can be brought back: Orchestrator (the orchestrator)";
    assert_eq!(h.query_all_by_label(banner).count(), 1);
    // The orchestrator started again in this engine: its entry is consumed.
    tx.send(event("restorable.changed", json!({"agents": []}))).unwrap();
    h.run_steps(3);
    assert!(h.state().state.restorable.is_empty());
    assert_eq!(h.query_all_by_label(banner).count(), 0);
    assert_eq!(h.query_all_by_label("Restore all (Shift+R)").count(), 0);
}

#[test]
fn an_offered_agent_started_again_leaves_the_banner() {
    let Some((mut world, mut h, conn)) = world_app() else { return };
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    world.terminate_engines();
    world.start_engine();
    step_until(&mut h, "the restore offer", |a| a.state.link == Link::Connected && a.state.restorable.len() == 1);
    // Started again with --continue in its worktree, not through the banner:
    // the engine consumes the entry and says so.
    let worktree = h.state().state.restorable[0].worktree_path.clone();
    let (probe, _, _rx) = world.connect();
    let spawned = probe.request(
        "spawn_agent",
        json!({"worktree_path": worktree, "resume_mode": "last", "skip_permissions": false}),
        lazyagent_testkit::TIMEOUT,
    );
    eprintln!("spawned: {spawned:?}");
    step_until(&mut h, "the banner gone", |a| a.state.restorable.is_empty() && a.state.store.agent_count() == 1);
    conn.shutdown();
}

#[test]
fn single_letter_keys_open_the_dialogs_outside_a_terminal() {
    use crate::dialogs::Kind;
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![]), worktree("/r/repo-f", "f", vec![])]);
    let mut h = harness(app);
    h.run_steps(3);
    let open = |h: &mut Harness<'static, App>, key: egui::Key, mods: egui::Modifiers| {
        h.key_press_modifiers(mods, key);
        h.run_steps(2);
        let kind = h.state().state.dialog.as_ref().map(|d| d.kind);
        h.key_press(egui::Key::Escape);
        h.run_steps(2);
        assert!(h.state().state.dialog.is_none(), "Esc closes {kind:?}");
        kind
    };
    assert_eq!(open(&mut h, egui::Key::S, egui::Modifiers::NONE), Some(Kind::Spawn));
    assert_eq!(open(&mut h, egui::Key::C, egui::Modifiers::NONE), Some(Kind::Create));
    assert_eq!(open(&mut h, egui::Key::D, egui::Modifiers::NONE), Some(Kind::Remove));
    assert_eq!(open(&mut h, egui::Key::Questionmark, egui::Modifiers::SHIFT), Some(Kind::Help));
    // With the sidebar focused (Ctrl+K) too.
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    assert_eq!(open(&mut h, egui::Key::S, egui::Modifiers::NONE), Some(Kind::Spawn));
    // x with no agent: nothing to stop, a toast says so.
    assert_eq!(open(&mut h, egui::Key::X, egui::Modifiers::NONE), None);
    assert!(h.state().state.toasts.iter().any(|t| t.text.contains("No agent to stop")));
}

#[test]
fn the_help_sheet_lists_every_key() {
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    let mut h = harness(app);
    h.run_steps(2);
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Help);
    h.run_steps(3);
    for (keys, action, _) in crate::dialogs::KEYMAP {
        assert!(h.query_all_by_label(keys).count() >= 1, "{keys}");
        assert!(h.query_all_by_label(action).count() >= 1, "{action}");
    }
}

/// The command line of the fake agent running in `cwd`.
fn agent_cmdline(cwd: &str) -> Option<String> {
    std::fs::read_dir("/proc").ok()?.filter_map(|e| e.ok()).find_map(|e| {
        let dir = e.path();
        let cmd = std::fs::read(dir.join("cmdline")).ok()?;
        let cmd = String::from_utf8_lossy(&cmd).replace('\0', " ");
        let here = std::fs::read_link(dir.join("cwd")).ok()?;
        (cmd.contains("fake_agent.py") && here.to_string_lossy() == cwd).then_some(cmd)
    })
}

#[test]
fn the_spawn_dialog_passes_instruction_and_permissions() {
    let Some((world, mut h, conn)) = world_app() else { return };
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.event(egui::Event::Text("write the tests".into()));
    h.run_steps(2);
    h.get_by_label("Skip permissions (d)").click();
    h.run_steps(3);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    let deadline = Instant::now() + std::time::Duration::from_secs(10);
    let cmd = loop {
        if let Some(c) = agent_cmdline(&world.repo) {
            break c;
        }
        assert!(Instant::now() < deadline, "no agent process in {}", world.repo);
        std::thread::sleep(std::time::Duration::from_millis(50));
    };
    assert!(cmd.contains("write the tests"), "{cmd}");
    assert!(cmd.contains("--dangerously-skip-permissions"), "{cmd}");
    conn.shutdown();
}

fn pr_json() -> serde_json::Value {
    json!({"number": 7, "title": "Login", "state": "OPEN", "url": "https://github.com/o/r/pull/7",
           "review_decision": "REVIEW_REQUIRED", "mergeable": "MERGEABLE", "checks": [
               {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
               {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE", "details_url": "https://ci/1"}]})
}

#[test]
fn the_attention_queue_ranks_badges_dismisses_and_snoozes() {
    use crate::app::View;
    let (app, tx) = app(vec![
        worktree("/r/repo", "main", vec![agent("a1", "Done one", "completed", "s1")]),
        worktree(
            "/r/repo-f",
            "f",
            vec![agent("a2", "Asker", "waiting_for_user", "s2"), agent("a3", "Gate", "waiting_for_approval", "s3")],
        ),
    ]);
    let mut h = harness(app);
    h.run_steps(3);
    let order: Vec<String> = h.state().state.attention.iter().map(|i| i.label.clone()).collect();
    assert_eq!(order, ["Gate", "Asker", "Done one"]);
    assert!(h.state().title.starts_with("(3) lazyagent"), "{}", h.state().title);
    // g a (outside a terminal: the agent's terminal has the keyboard, so
    // Ctrl+K first): the attention view, with the most urgent item picked.
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::G);
    h.key_press(egui::Key::A);
    h.run_steps(2);
    assert_eq!(h.state().state.view, View::Attention);
    assert!(h.query_all_by_label("Needs approval").count() >= 1);
    h.get_by_label("Dismiss").click();
    h.run_steps(2);
    assert_eq!(h.state().state.attention.len(), 2);
    h.get_by_label("Snooze 1h").click();
    h.run_steps(2);
    assert_eq!(h.state().state.attention.len(), 1);
    assert_eq!(h.state().state.saved.marks.dismissed.len(), 1);
    assert_eq!(h.state().state.saved.marks.snoozed.len(), 1);
    // The asker stops asking, then asks again: a new occurrence shows.
    let set = |status: &str| {
        event(
            "agent.updated",
            json!({"worktree_path": "/r/repo-f", "agent_id": "a3", "status": status, "confidence": "high", "detail": ""}),
        )
    };
    tx.send(set("running")).unwrap();
    h.run_steps(2);
    tx.send(set("waiting_for_approval")).unwrap();
    h.run_steps(2);
    assert_eq!(h.state().state.attention.len(), 2, "dismissing hid that occurrence only");
    // g w: back.
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::G);
    h.key_press(egui::Key::W);
    h.run_steps(2);
    assert_eq!(h.state().state.view, View::Worktrees);
}

#[test]
fn a_failing_pr_is_queued_toasted_and_shown_in_the_panel() {
    let (app, tx) = app(vec![worktree("/r/repo", "main", vec![agent("a1", "Fixer", "running", "s1")])]);
    let mut h = harness(app);
    h.run_steps(3);
    tx.send(event("pr_info.updated", json!({"path": "/r/repo", "pr_info": pr_json()}))).unwrap();
    h.run_steps(2);
    let s = &h.state().state;
    assert!(s.toasts.iter().any(|t| t.text.contains("CI is failing on repo (#7 Login)")));
    let ci = s.attention.iter().find(|i| i.kind == crate::attention::Kind::Ci).expect("a CI item");
    assert_eq!(ci.detail, "tests");
    // The worktree's PR panel (Ctrl+.): the PR linked, every check.
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Period);
    h.run_steps(2);
    assert!(h.state().state.pr_panel);
    h.get_by_label("#7 Login");
    h.get_by_label("tests");
    h.get_by_label("lint");
    // In the queue, the CI item offers to send the failures to the agent.
    h.state_mut().state.view = crate::app::View::Attention;
    h.state_mut().state.att_selected = Some("Ci:/r/repo:7".into());
    h.run_steps(2);
    h.get_by_label("Send the failures to Fixer").click();
    h.run_steps(2);
    // No engine here: the send fails, and says so.
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("Not sent")));
}

#[test]
fn marks_and_settings_survive_a_restart() {
    let dir = tempfile::tempdir().unwrap();
    let env = Env::process().with("XDG_STATE_HOME", Some(dir.path().to_str().unwrap()));
    let path = crate::persist::path("/r/repo", &env);
    let mut saved = crate::persist::Saved::default();
    saved.notify.insert("done".into(), false);
    saved.marks.dismissed.insert("x@1".into(), crate::app::unix_now());
    saved.theme = Some("light".into());
    let mut saver = crate::persist::Saver::new(path.clone());
    saver.touch();
    saver.flush(&saved);
    let (tx, _rx) = mpsc::channel();
    let conn = Conn::new("/r/repo".into(), env, tx, egui::Context::default());
    let state = State::new(conn, FontSet::bundled_only(), None);
    assert!(!state.saved.notify_on("done"));
    assert_eq!(state.saved.marks.dismissed.len(), 1);
    assert_eq!(state.theme.kind, ThemeKind::Light, "the saved theme, since no --theme was given");
}

#[test]
fn a_reply_reaches_a_real_agent() {
    let Some((world, mut h, conn)) = world_app() else { return };
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "the prompt", |a| {
        a.state.panes.values().any(|p| p.inner.lock().unwrap().term.frame().text().contains("your turn>"))
    });
    let agent = h.state().state.store.agents(&world.repo)[0].agent_id.clone();
    h.state_mut().state.send_reply(&world.repo, &agent, "from the queue".into());
    step_until(&mut h, "the agent's answer", |a| {
        a.state.panes.values().any(|p| p.inner.lock().unwrap().term.frame().text().contains("got: 'from the queue'"))
    });
    step_until(&mut h, "the delivery toast", |a| a.state.toasts.iter().any(|t| t.text.starts_with("Sent")));
    conn.shutdown();
}

#[test]
fn deriving_the_queue_for_200_agents_is_cheap() {
    let statuses = ["running", "waiting_for_user", "completed", "waiting_for_approval", "failed"];
    let trees: Vec<_> = (0..40)
        .map(|w| {
            let agents =
                (0..5).map(|a| agent(&format!("a{a}"), "x", statuses[(w + a) % 5], &format!("s{w}{a}"))).collect();
            worktree(&format!("/r/repo-{w}"), &format!("b{w}"), agents)
        })
        .collect();
    let mut store = crate::store::Store::default();
    store.seed(
        serde_json::from_value(json!({"worktrees": trees, "orchestrator": {"agents": []}, "pr_info": {},
                                      "git_statuses": {}, "view": null, "cursor": 0}))
        .unwrap(),
    );
    let mut since = std::collections::HashMap::new();
    let marks = crate::attention::Marks::default();
    let mut times = vec![];
    for i in 0..50 {
        let t = Instant::now();
        let items = crate::attention::items(&store, &[], &[], &mut since, &marks, i as f64);
        times.push(t.elapsed().as_secs_f64() * 1e3);
        assert_eq!(items.len(), 160);
    }
    times.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p95 = times[47];
    println!("queue of 200 agents re-derived: p50 {:.3} / p95 {p95:.3} ms", times[25]);
    if !cfg!(debug_assertions) {
        assert!(p95 < 2.0, "p95 {p95} ms");
    }
}

#[test]
fn render_the_attention_view_and_the_pr_panel() {
    let (app, tx) = app(vec![
        worktree("/r/repo", "main", vec![agent("a1", "Fixer", "completed", "s1")]),
        worktree("/r/repo-login", "feat-login", vec![agent("a2", "Implementer", "waiting_for_approval", "s2")]),
        worktree("/r/repo-docs", "docs", vec![agent("a3", "Writer", "waiting_for_user", "s3")]),
    ]);
    let mut h =
        Harness::builder().with_size([1300.0, 760.0]).wgpu().build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    tx.send(event("pr_info.updated", json!({"path": "/r/repo", "pr_info": pr_json()}))).unwrap();
    h.run_steps(2);
    let ctx = h.ctx.clone();
    let pane = h.state_mut().state.pane("s2", PaneKind::Agent, &ctx);
    pane.inner.lock().unwrap().term.feed(
        "\x1b[1mBash command\x1b[0m\r\n\r\n  \x1b[33mnpm test -- --watch=false\x1b[0m\r\n\r\nDo you want to proceed?\r\n\x1b[36m❯ 1. Yes\x1b[0m\r\n  2. No, and tell Claude what to do differently\r\n"
            .as_bytes(),
    );
    h.state_mut().state.view = crate::app::View::Attention;
    h.run_steps(3);
    let out = std::env::temp_dir();
    match h.render() {
        Ok(image) => image.save(out.join("lazyagent-desktop-attention.png")).unwrap(),
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => return,
    }
    h.state_mut().state.view = crate::app::View::Worktrees;
    h.state_mut().state.selected = Some("/r/repo".into());
    h.state_mut().state.pr_panel = true;
    h.run_steps(3);
    h.render().unwrap().save(out.join("lazyagent-desktop-pr-panel.png")).unwrap();
}

fn sample_diff(files: usize, lines: usize) -> String {
    let mut s = String::new();
    for f in 0..files {
        s.push_str(&format!("diff --git a/src/f{f}.rs b/src/f{f}.rs\n--- a/src/f{f}.rs\n+++ b/src/f{f}.rs\n@@ -1,{lines} +1,{lines} @@\n"));
        for l in 0..lines {
            match l % 4 {
                0 => s.push_str(&format!("-    let old_{l} = compute({l});\n")),
                1 => s.push_str(&format!("+    let new_{l} = compute_better({l}, &context);\n")),
                _ => s.push_str(&format!("     // unchanged line {l} of file {f}\n")),
            }
        }
    }
    s
}

#[test]
fn the_diff_view_comments_and_submits_a_review() {
    let (app, tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-f", "f", vec![agent("a1", "Implementer", "running", "s1")]),
    ]);
    let mut h = harness(app);
    h.run_steps(2);
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo-f".into());
        s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo-f", "main", crate::diff::parse(&sample_diff(2, 8))));
        s.diff_open = true;
    }
    h.run_steps(2);
    h.get_by_label("2 files");
    assert!(h.query_by_label("Submit review (0)").is_some());
    // A comment on a removed line (from the click handler's anchor).
    let rows = h.state().state.diff.as_ref().unwrap().diff.files[0].rows.clone();
    let mut c = crate::diff::anchor("src/f0.rs", &rows[1]).unwrap();
    c.text = "keep the old name".into();
    h.state_mut().state.set_comments("/r/repo-f", &[c.clone()]);
    h.run_steps(2);
    let rows = &h.state().state.diff.as_ref().unwrap().rows;
    assert!(rows.contains(&crate::diff_view::DRow::Comment(0, c.id())), "the comment sits under its line");
    // Split keeps it, once.
    h.get_by_label("Split").click();
    h.run_steps(2);
    assert!(h.state().state.saved.diff_split);
    let rows = &h.state().state.diff.as_ref().unwrap().rows;
    assert_eq!(rows.iter().filter(|r| matches!(r, crate::diff_view::DRow::Comment(..))).count(), 1);
    // Submit: the agent runs, so "Send when idle" queues it…
    h.get_by_label("Submit review (1)").click();
    h.run_steps(2);
    h.get_by_label("Send when idle").click();
    h.run_steps(2);
    assert_eq!(h.state().state.pending_reviews.len(), 1);
    assert!(h.state().state.pending_reviews[0].text.contains("src/f0.rs:1 (removed line)"));
    // …and it goes out when the agent finishes its turn (no engine here: it fails, and says so).
    tx.send(event(
        "agent.updated",
        json!({"worktree_path": "/r/repo-f", "agent_id": "a1", "status": "waiting", "confidence": "high", "detail": ""}),
    ))
    .unwrap();
    h.run_steps(3);
    assert!(h.state().state.pending_reviews.is_empty());
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("The review was not sent")));
    assert_eq!(h.state().state.comments("/r/repo-f").len(), 1, "not cleared when it was not sent");
}

/// A diff view on `/r/repo-f` (agents as given) with a one-line comment
/// on a removed line and one on a range of three lines.
fn review_app(agents: Vec<serde_json::Value>) -> (Harness<'static, App>, mpsc::Sender<Msg>, Vec<crate::diff::Comment>) {
    let (app, tx) = app(vec![worktree("/r/repo", "main", vec![]), worktree("/r/repo-f", "f", agents)]);
    let mut h = harness(app);
    h.run_steps(2);
    let parsed = crate::diff::parse(&sample_diff(2, 8));
    let rows = parsed.files[0].rows.clone();
    let mut one = crate::diff::anchor("src/f0.rs", &rows[1]).unwrap();
    one.text = "keep the old name".into();
    let mut range = crate::diff::anchor_range("src/f0.rs", &rows[2..5]).unwrap();
    range.text = "What is this?".into();
    let comments = vec![one, range];
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo-f".into());
        s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo-f", "main", parsed));
        s.diff_open = true;
        s.set_comments("/r/repo-f", &comments);
    }
    h.run_steps(2);
    (h, tx, comments)
}

#[test]
fn the_review_form_lists_comments_and_shows_the_prompt_on_demand() {
    let (mut h, _tx, comments) = review_app(vec![agent("a1", "Implementer", "waiting", "s1")]);
    h.get_by_label("Submit review (2)").click();
    h.run_steps(3);
    // The target, then each comment: where, the comment, the code folded.
    assert!(h.query_by_label("To").is_some());
    assert!(h.query_by(|n| n.value().as_deref() == Some("Implementer · waiting")).is_some(), "the target");
    assert!(h.query_by_label("src/f0.rs:1 (removed line)").is_some());
    assert!(h.query_by_label("keep the old name").is_some());
    assert!(h.query_by_label("What is this?").is_some());
    assert!(h.query_by_label(&format!("src/f0.rs:{}", comments[1].place())).is_some());
    let code: Vec<&str> = comments[1].code.lines().collect();
    assert_eq!(code.len(), 3);
    assert!(h.query_by_label(code[0]).is_some(), "the first line of the code");
    assert!(h.query_by_label(code[2]).is_none(), "the rest folded");
    h.get_by_label("▸ 2 more lines").click();
    h.run_steps(3);
    assert!(h.query_by_label(code[2]).is_some(), "unfolded");
    assert!(h.query_by_label("▾ less").is_some());
    // The raw prompt only on demand.
    let prompt = crate::diff::review_prompt(&comments, "");
    assert!(h.query_by_label(&prompt).is_none());
    assert!(h.query_by_label_contains("Clear the comments").is_none(), "no clear-once-sent option");
    h.get_by_label("▸ Show prompt").click();
    h.run_steps(3);
    assert!(h.query_by_label(&prompt).is_some());
    // Idle agent: Send; no engine here, so it is not sent and nothing changes.
    h.get_by_label("Send").click();
    h.run_steps(3);
    assert!(h.state().state.review.is_none());
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("The review was not sent")));
    assert!(h.state().state.comments("/r/repo-f").iter().all(|c| !c.sent));
}

#[test]
fn review_reply_answers_land_in_their_threads() {
    let (mut h, tx, comments) = review_app(vec![agent("a1", "Implementer", "waiting", "s1")]);
    // An engine without review_register: no ids, the old prompt.
    h.state_mut().state.give_cids("/r/repo-f");
    assert!(h.state().state.comments("/r/repo-f").iter().all(|c| c.cid.is_empty()));
    h.state_mut().state.capabilities = Some(vec!["review_register".into()]);
    h.get_by_label("Submit review (2)").click();
    h.run_steps(3);
    let all = h.state().state.comments("/r/repo-f");
    assert!(all.iter().all(|c| c.cid.starts_with('c') && c.cid.len() > 1), "ids given: {all:?}");
    assert_ne!(all[0].cid, all[1].cid);
    h.get_by_label("▸ Show prompt").click();
    h.run_steps(3);
    let prompt = crate::diff::review_prompt(&all, "");
    assert!(prompt.contains(&format!("[comment_id: {}]", all[0].cid)), "{prompt}");
    assert!(h.query_by_label(&prompt).is_some(), "the prompt shown is the one with ids");
    h.get_by_label("Cancel").click();
    h.run_steps(2);

    // Sent (no engine here: as the reply to send_agent_input would).
    let ids: Vec<String> = comments.iter().map(|c| c.id()).collect();
    {
        let s = &mut h.state_mut().state;
        s.mark_sent("/r/repo-f", &ids, "Implementer");
        s.awaiting.push(crate::diff_view::Awaiting {
            worktree: "/r/repo-f".into(),
            agent_id: "a1".into(),
            ids: ids.clone(),
            ran: false,
        });
        s.comments_gen += 1;
    }
    h.run_steps(2);
    let thread = crate::diff_view::thread_lines(&h.state().state.comments("/r/repo-f")[0], true);
    assert_eq!(thread[1], ("note", "· Sent with the review to Implementer.".to_string()));
    assert_eq!(thread.last().unwrap().1, "  ⋯ waiting for the agent's answer");
    let rows = h.state().state.diff.as_ref().unwrap().rows.clone();
    assert!(rows.contains(&crate::diff_view::DRow::Reply(0, ids[0].clone(), 2)), "the waiting line: {rows:?}");

    // The agent answers the first: under it, once, with a toast.
    let replied = |reply_id: &str, cid: &str| {
        event(
            "review.replied",
            json!({"reply_id": reply_id, "comment_id": cid, "worktree_path": "/r/repo-f", "agent_id": "a1",
                   "text": "Kept it: callers use it.", "at": 1.0}),
        )
    };
    tx.send(replied("r1", &all[0].cid)).unwrap();
    tx.send(replied("r1", &all[0].cid)).unwrap();
    tx.send(replied("r2", "c-unknown")).unwrap();
    h.run_steps(3);
    let now = h.state().state.comments("/r/repo-f");
    let texts: Vec<&str> = now[0].replies.iter().map(|r| r.text.as_str()).collect();
    assert_eq!(texts, ["Sent with the review to Implementer.", "Kept it: callers use it."]);
    assert_eq!(now[0].replies[1].who, "agent");
    assert_eq!(now[1].replies.len(), 1, "the other is not answered yet");
    assert_eq!(h.state().state.awaiting[0].ids, [ids[1].clone()]);
    assert!(h.state().state.toasts.iter().any(|t| t.text == "The agent answered on src/f0.rs:1 (removed line)"));
    let thread = crate::diff_view::thread_lines(&now[0], false);
    assert_eq!(thread[2], ("agent", "agent: Kept it: callers use it.".to_string()));

    // Its turn ends without answering the second: the thread says so.
    for status in ["running", "waiting"] {
        tx.send(event(
            "agent.updated",
            json!({"worktree_path": "/r/repo-f", "agent_id": "a1", "status": status, "confidence": "high", "detail": ""}),
        ))
        .unwrap();
        h.run_steps(2);
    }
    assert!(h.state().state.awaiting.is_empty());
    let last = h.state().state.comments("/r/repo-f")[1].replies.last().cloned().unwrap();
    assert_eq!(
        (last.who.as_str(), last.text.as_str()),
        ("note", "The agent's turn ended without an answer here; see its terminal.")
    );
}

#[test]
fn sent_review_comments_stay_as_threads() {
    let (mut h, _tx, comments) = review_app(vec![agent("a1", "Implementer", "running", "s1")]);
    // Send when idle: queued, so not offered again meanwhile.
    h.get_by_label("Submit review (2)").click();
    h.run_steps(3);
    h.get_by_label("Send when idle").click();
    h.run_steps(3);
    assert_eq!(h.state().state.pending_reviews[0].ids, comments.iter().map(|c| c.id()).collect::<Vec<_>>());
    assert!(h.query_by_label("Submit review (0)").is_some(), "queued comments are not in the next review");
    h.state_mut().state.pending_reviews.clear();
    // Delivered: every comment stays, marked sent, with where its answer is.
    let ids: Vec<String> = comments.iter().map(|c| c.id()).collect();
    h.state_mut().state.mark_sent("/r/repo-f", &ids, "Implementer");
    h.run_steps(3);
    let all = h.state().state.comments("/r/repo-f");
    assert_eq!(all.len(), 2);
    assert!(all.iter().all(|c| c.sent && !c.asked));
    let thread = crate::diff_view::thread_lines(&all[0], false);
    assert_eq!(thread[0], ("you", "sent ▸ keep the old name".to_string()));
    assert_eq!(
        thread[1],
        ("note", "· Sent with the review to Implementer; its answer is in the agent's terminal.".to_string())
    );
    let rows = h.state().state.diff.as_ref().unwrap().rows.clone();
    for c in &all {
        assert!(rows.contains(&crate::diff_view::DRow::Comment(0, c.id())), "the thread under its line");
    }
    assert!(h.query_by_label("Submit review (0)").is_some());
    // Marking twice adds no second note.
    h.state_mut().state.mark_sent("/r/repo-f", &ids, "Implementer");
    assert_eq!(h.state().state.comments("/r/repo-f")[0].replies.len(), 1);
    // A sent thread opens as a conversation with a follow-up, not an editor.
    let idx = rows.iter().position(|r| *r == crate::diff_view::DRow::Comment(0, all[0].id())).unwrap();
    press(&mut h, idx, 200.0, true, egui::Modifiers::NONE);
    press(&mut h, idx, 200.0, false, egui::Modifiers::NONE);
    assert!(h.state().state.comment_edit.is_some());
    assert!(h.query_by_label("You (sent with a review): keep the old name").is_some());
    h.get_by_label("Close").click();
    h.run_steps(2);
    // A new comment is the next review; Clear sent removes the sent ones.
    let mut more = comments[0].clone();
    more.line = 3;
    more.text = "and this".into();
    more.sent = false;
    more.replies.clear();
    h.state_mut().state.upsert_comment("/r/repo-f", more);
    h.run_steps(2);
    assert!(h.query_by_label("Submit review (1)").is_some());
    h.get_by_label("Clear sent (2)").click();
    h.run_steps(2);
    let left = h.state().state.comments("/r/repo-f");
    assert_eq!((left.len(), left[0].text.as_str()), (1, "and this"));
}

#[test]
fn with_no_agent_the_review_and_ask_now_say_one_will_start() {
    let (mut h, _tx, _) = review_app(vec![]);
    h.get_by_label("Submit review (2)").click();
    h.run_steps(3);
    assert!(
        h.query_by(|n| n.value().as_deref() == Some("New agent (a new default agent (skip permissions))")).is_some()
    );
    assert!(
        h.query_by_label(
            "No agent in this worktree: Send starts a new default agent (skip permissions) and gives it this."
        )
        .is_some()
    );
    h.get_by_label("Start agent and send").click();
    h.run_steps(3);
    // No engine: not sent, said so, and the worktree is free to try again.
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("The review was not sent")));
    assert!(h.state().state.starting.is_empty());
    assert!(h.state().state.comments("/r/repo-f").iter().all(|c| !c.sent));
    // The popup on selected lines says the same for Ask now.
    // Rows: 0 file, 1 hunk, 2 a line, 3 its comment, 4 and 5 lines.
    press(&mut h, 5, 30.0, true, egui::Modifiers::NONE);
    press(&mut h, 5, 30.0, false, egui::Modifiers::NONE);
    assert!(h.state().state.ask.is_some());
    assert!(
        h.query_by_label(
            "No agent in this worktree: Ask now starts a new default agent (skip permissions) and gives it this."
        )
        .is_some()
    );
    h.event(egui::Event::Text("Why?".into()));
    h.run_steps(2);
    h.get_by_label("Ask now").click();
    h.run_steps(3);
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("The question was not sent")));
    assert!(h.state().state.asks.is_empty() && h.state().state.starting.is_empty());
}

#[test]
fn the_diff_view_keeps_long_names_and_lines_in_their_columns() {
    // The shape that broke: long file names, a long branch and subject,
    // lines wider than a split pane, in a narrow window.
    let branch = "TASK-123-dashboard-for-billing-and-shipping-in-the-checkout-flow";
    let (app, _tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-f", branch, vec![agent("a1", "Implementer", "running", "s1")]),
    ]);
    let mut h = Harness::builder().with_size([1000.0, 700.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    let long = "checkout-renders-the-payment-sections-and-keeps-them-in-order.spec.ts";
    let mut text = sample_diff(3, 8);
    text.push_str(&format!(
        "diff --git a/src/{long} b/src/{long}\n--- a/src/{long}\n+++ b/src/{long}\n@@ -1,1 +1,1 @@\n-{}\n+{}\n",
        "old ".repeat(60),
        "new ".repeat(60)
    ));
    {
        let s = &mut h.state_mut().state;
        s.store.git.insert(
            "/r/repo-f".into(),
            lazyagent_proto::types::GitStatus {
                last_commit_subject: Some(format!("Merge remote-tracking branch 'origin/{branch}' into {branch}")),
                ..Default::default()
            },
        );
        s.selected = Some("/r/repo-f".into());
        s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo-f", "main", crate::diff::parse(&text)));
        s.diff_open = true;
        s.saved.diff_split = true;
    }
    h.run_steps(3);
    // The header's names end before its buttons start.
    let stop = h.get_by_label("Stop").rect();
    let buttons = stop.min.x.min(h.get_by_label("Remove").rect().min.x);
    let in_header = |r: egui::Rect| stop.y_range().contains(r.center().y);
    let names: Vec<egui::Rect> = h
        .query_all_by_label(branch)
        .chain(h.query_all_by_label_contains("last: "))
        .map(|n| n.rect())
        .filter(|r| in_header(*r))
        .collect();
    assert_eq!(names.len(), 2, "the branch and the subject in the header");
    for r in names {
        assert!(r.max.x <= buttons, "{r:?} runs under the buttons at {buttons}");
    }
    // Every file entry stays in the list, left of the rows.
    let rows = h.state().state.diff.as_ref().unwrap().rows_rect;
    assert!(rows.width() > 300.0, "{rows:?}");
    let entry = h.get_by_label(&format!("src/{long}")).rect();
    assert!(entry.max.x <= rows.min.x, "the file entry {entry:?} overlaps the rows at {rows:?}");
    for f in 0..3 {
        assert!(h.get_by_label(&format!("src/f{f}.rs")).rect().max.x <= rows.min.x);
    }
    // The lines are wider than a pane: shift+wheel scrolls them sideways,
    // up to their end and no further.
    let wheel = |h: &mut Harness<'static, App>, dx: f32| {
        h.event(egui::Event::PointerMoved(rows.center()));
        h.event(egui::Event::MouseWheel {
            unit: egui::MouseWheelUnit::Point,
            delta: egui::vec2(dx, 0.0),
            phase: egui::TouchPhase::Move,
            source: egui::MouseWheelSource::Unknown,
            modifiers: egui::Modifiers::NONE,
        });
        h.run_steps(4);
        h.state().state.diff.as_ref().unwrap().hoff
    };
    assert_eq!(h.state().state.diff.as_ref().unwrap().hoff, 0.0);
    let a = wheel(&mut h, -80.0);
    assert!(a > 0.0, "scrolled sideways: {a}");
    let end = wheel(&mut h, -100_000.0);
    // 240 columns of 7-ish px against a pane of ~(rows/2 - gutter).
    assert!(end > a && end < 240.0 * 9.0, "clamped at the end: {end}");
    assert_eq!(wheel(&mut h, 100_000.0), 0.0);
    // Unified scrolls the same way, by less (one wider column).
    h.state_mut().state.saved.diff_split = false;
    h.run_steps(2);
    let unified = wheel(&mut h, -100_000.0);
    assert!(unified > 0.0 && unified < end, "unified {unified} vs split {end}");
}

/// The centre of row `idx` of the diff, at `x` from the rows' left.
fn diff_row_pos(h: &Harness<'static, App>, idx: usize, x: f32) -> egui::Pos2 {
    let r = h.state().state.diff.as_ref().unwrap().rows_rect;
    egui::pos2(r.left() + x, r.top() + (idx as f32 + 0.5) * crate::diff_view::ROW_H)
}

fn press(h: &mut Harness<'static, App>, idx: usize, x: f32, pressed: bool, modifiers: egui::Modifiers) {
    let pos = diff_row_pos(h, idx, x);
    h.event(egui::Event::PointerMoved(pos));
    h.event_modifiers(
        egui::Event::PointerButton { pos, button: egui::PointerButton::Primary, pressed, modifiers },
        modifiers,
    );
    h.run_steps(2);
}

#[test]
fn select_lines_then_add_to_review_or_ask_now() {
    let (app, tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-f", "f", vec![agent("a1", "Implementer", "running", "s1")]),
    ]);
    let mut h = harness(app);
    h.run_steps(2);
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo-f".into());
        s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo-f", "main", crate::diff::parse(&sample_diff(2, 8))));
        s.diff_open = true;
    }
    h.run_steps(3);
    // Unified: rows 0 file, 1 hunk, 2 "-old_0" (old 1), 3 "+new_1" (new 1),
    // 4 context (new 2). Drag over the code from row 2 to row 4.
    let none = egui::Modifiers::NONE;
    press(&mut h, 2, 260.0, true, none);
    for y in [3, 4] {
        let pos = diff_row_pos(&h, y, 262.0);
        h.event(egui::Event::PointerMoved(pos));
        h.run_steps(2);
    }
    press(&mut h, 4, 262.0, false, none);
    let draft = &h.state().state.ask.as_ref().expect("the popup").comment;
    assert_eq!(
        (draft.start_line, draft.start_side.as_deref(), draft.side.as_str(), draft.line),
        (Some(1), Some("old"), "new", 2)
    );
    assert_eq!(draft.code.lines().count(), 3);
    h.get_by_label("src/f0.rs:-1…+2");
    h.event(egui::Event::Text("Why both?".into()));
    h.run_steps(2);
    h.get_by_label("Add to review").click();
    h.run_steps(3);
    let all = h.state().state.comments("/r/repo-f");
    assert_eq!((all.len(), all[0].text.as_str(), all[0].asked), (1, "Why both?", false));
    assert!(h.state().state.ask.is_none() && h.state().state.diff.as_ref().unwrap().sel.is_none());
    // The thread shows under its last line (row 4).
    assert_eq!(h.state().state.diff.as_ref().unwrap().rows[5], crate::diff_view::DRow::Comment(0, all[0].id()));
    assert!(h.query_by_label("Submit review (1)").is_some());

    // Split: click a line on the right, shift-click another → the popup.
    h.get_by_label("Split").click();
    h.run_steps(3);
    let right = h.state().state.diff.as_ref().unwrap().rows_rect.width() / 2.0 + 120.0;
    // Rows: 2 (-old_0 | +new_1), 3 context (new 2), 4 the thread, 5 context
    // (new 3), 6 (-old_4 | +new_5, new 4).
    press(&mut h, 3, right, true, none);
    press(&mut h, 3, right, false, none);
    assert!(h.state().state.ask.is_none(), "a click on the code only selects");
    press(&mut h, 6, right, true, egui::Modifiers::SHIFT);
    press(&mut h, 6, right, false, egui::Modifiers::SHIFT);
    let draft = &h.state().state.ask.as_ref().expect("the popup").comment;
    // The right side only: context 2, 3, then +new_5 (new 4), no removed line.
    assert_eq!((draft.start_line, draft.line, draft.side.as_str()), (Some(2), 4, "new"));
    assert!(!draft.code.contains("old_4"), "{}", draft.code);
    h.event(egui::Event::Text("Is this tested?".into()));
    h.run_steps(2);
    h.get_by_label("Ask now").click();
    h.run_steps(3);
    // No engine here: it says it was not sent, and the thread stays asked.
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("The question was not sent")));
    assert!(h.state().state.asks.is_empty());
    let asked = h.state().state.comments("/r/repo-f").into_iter().find(|c| c.asked).expect("the question");
    assert!(h.query_by_label("Submit review (1)").is_some(), "a question is not part of the review");

    // An answer: the agent runs, then is idle; its output is read (here it
    // fails: the thread says where the answer is), then a real one.
    h.state_mut().state.asks.push(crate::diff_view::PendingAsk {
        worktree: "/r/repo-f".into(),
        agent_id: "a1".into(),
        id: asked.id(),
        marker: "m1".into(),
        by_reply: false,
        sent: true,
        ran: false,
        reading: false,
    });
    h.state_mut().state.comments_gen += 1;
    h.run_steps(2);
    let rows = |h: &Harness<'static, App>| h.state().state.diff.as_ref().unwrap().rows.clone();
    assert!(
        rows(&h).iter().any(|r| matches!(r, crate::diff_view::DRow::Reply(_, id, 1) if *id == asked.id())),
        "waiting line"
    );
    tx.send(event(
        "agent.updated",
        json!({"worktree_path": "/r/repo-f", "agent_id": "a1", "status": "waiting", "confidence": "high", "detail": ""}),
    ))
    .unwrap();
    h.run_steps(3);
    assert!(h.state().state.asks.is_empty());
    let c = h.state().state.comments("/r/repo-f").into_iter().find(|c| c.asked).unwrap();
    assert_eq!(
        c.replies.last().map(|r| (r.who.as_str(), r.text.as_str())),
        Some(("note", "Answered in the agent's terminal."))
    );
    let a = crate::diff_view::PendingAsk {
        worktree: "/r/repo-f".into(),
        agent_id: "a1".into(),
        id: asked.id(),
        marker: "m2".into(),
        by_reply: false,
        sent: true,
        ran: true,
        reading: true,
    };
    let out: Vec<String> = ["⏺ ⟦answer m2⟧", "  Yes: test_f0 covers it.", "  ⟦end m2⟧"].map(String::from).to_vec();
    h.state_mut().state.answer(&a, Ok(&out));
    h.run_steps(2);
    let c = h.state().state.comments("/r/repo-f").into_iter().find(|c| c.asked).unwrap();
    assert_eq!(c.replies.last().unwrap().text, "Yes: test_f0 covers it.");
    let thread = crate::diff_view::thread_lines(&c, false);
    assert_eq!(thread.last().unwrap(), &("agent", "agent: Yes: test_f0 covers it.".to_string()));
    let n = rows(&h).iter().filter(|r| matches!(r, crate::diff_view::DRow::Reply(_, id, _) | crate::diff_view::DRow::Comment(_, id) if *id == c.id())).count();
    assert_eq!(n, 3, "question, note, answer");
    // Clicking it opens the thread, with a follow-up box.
    let idx = rows(&h).iter().position(|r| *r == crate::diff_view::DRow::Comment(0, c.id())).unwrap();
    press(&mut h, idx, 200.0, true, none);
    press(&mut h, idx, 200.0, false, none);
    assert!(h.state().state.comment_edit.is_some());
    assert!(h.query_by_label("Agent: Yes: test_f0 covers it.").is_some());
}

#[test]
fn scrolling_a_5000_line_diff_is_cheap() {
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    let mut h = Harness::builder().with_size([1400.0, 860.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    let t = Instant::now();
    let parsed = crate::diff::parse(&sample_diff(25, 200));
    let parse_ms = t.elapsed().as_secs_f64() * 1e3;
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo".into());
        s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo", "", parsed));
        s.diff_open = true;
    }
    let t = Instant::now();
    h.step();
    let first = t.elapsed().as_secs_f64() * 1e3;
    h.run_steps(2);
    let pos = egui::pos2(900.0, 500.0);
    h.event(egui::Event::PointerMoved(pos));
    let mut frames = vec![];
    for _ in 0..120 {
        h.event(egui::Event::MouseWheel {
            unit: egui::MouseWheelUnit::Point,
            delta: egui::vec2(0.0, -60.0),
            phase: egui::TouchPhase::Move,
            source: egui::MouseWheelSource::Unknown,
            modifiers: egui::Modifiers::NONE,
        });
        let t = Instant::now();
        h.step();
        frames.push(t.elapsed().as_secs_f64() * 1e3);
    }
    frames.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let (p50, p95) = (frames[60], frames[114]);
    println!(
        "5,200-line diff: parse {parse_ms:.2} ms, first frame {first:.2} ms, scroll frames p50 {p50:.2} / p95 {p95:.2} ms"
    );
    if !cfg!(debug_assertions) {
        assert!(first < 20.0 && p95 < 2.0, "first {first} p95 {p95}");
    }
}

#[test]
fn a_review_reaches_a_real_agent_from_a_real_diff() {
    let Some((world, mut h, conn)) = world_app() else { return };
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "the prompt", |a| {
        a.state.panes.values().any(|p| p.inner.lock().unwrap().term.frame().text().contains("your turn>"))
    });
    std::fs::write(std::path::Path::new(&world.repo).join("app.py"), "print('hello')\n").unwrap();
    // Ctrl+Shift+D works with the agent's terminal focused.
    h.key_press_modifiers(egui::Modifiers::CTRL | egui::Modifiers::SHIFT, egui::Key::D);
    step_until(&mut h, "the diff", |a| a.state.diff.as_ref().is_some_and(|d| !d.loading && !d.diff.files.is_empty()));
    let file = h.state().state.diff.as_ref().unwrap().diff.files[0].clone();
    assert_eq!((file.path.as_str(), file.status), ("app.py", "added"));
    let mut c = crate::diff::anchor("app.py", &file.rows[0]).unwrap();
    c.text = "use logging".into();
    h.state_mut().state.set_comments(&world.repo, &[c]);
    h.run_steps(2);
    h.get_by_label("Submit review (1)").click();
    h.run_steps(2);
    h.get_by_label("Send now").click();
    step_until(&mut h, "the review in the agent", |a| {
        a.state.panes.values().any(|p| p.inner.lock().unwrap().term.frame().text().contains("Review comments"))
    });
    step_until(&mut h, "the comment sent, kept as a thread", |a| a.state.comments(&world.repo).iter().all(|c| c.sent));
    // The agent answers it with review_reply: under the comment, and in its terminal.
    step_until(&mut h, "the answer in the thread", |a| {
        a.state.comments(&world.repo).iter().all(|c| c.replies.iter().any(|r| r.who == "agent"))
    });
    let c = h.state().state.comments(&world.repo).remove(0);
    let texts: Vec<(&str, &str)> = c.replies.iter().map(|r| (r.who.as_str(), r.text.as_str())).collect();
    let agent = h.state().state.store.agents(&world.repo)[0].label.clone();
    let answer = format!("A fake answer to {}.", c.cid);
    assert_eq!(texts, [("note", format!("Sent with the review to {agent}.").as_str()), ("agent", answer.as_str())]);
    assert!(h.state().state.awaiting.is_empty(), "nothing left to wait for");
    let term =
        |a: &App| a.state.panes.values().map(|p| p.inner.lock().unwrap().term.frame().text()).collect::<String>();
    step_until(&mut h, "the answer in the terminal", |a| term(a).contains(&format!("replied {}", c.cid)));
    conn.shutdown();
}

/// The fake agent's command line once it runs in `cwd`.
fn wait_cmdline(cwd: &str) -> String {
    let deadline = Instant::now() + std::time::Duration::from_secs(10);
    loop {
        if let Some(c) = agent_cmdline(cwd) {
            return c;
        }
        assert!(Instant::now() < deadline, "no agent process in {cwd}");
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
}

/// A real engine's main worktree with a new file in it, its diff open.
fn world_with_diff() -> Option<(lazyagent_testkit::World, Harness<'static, App>, std::sync::Arc<Conn>)> {
    let (world, mut h, conn) = world_app()?;
    std::fs::write(std::path::Path::new(&world.repo).join("app.py"), "a = 1\nb = 2\n").unwrap();
    h.state_mut().state.selected = Some(world.repo.clone());
    h.state_mut().state.toggle_diff();
    step_until(&mut h, "the diff", |a| a.state.diff.as_ref().is_some_and(|d| !d.loading && !d.diff.files.is_empty()));
    assert_eq!(h.state().state.store.agent_count(), 0);
    Some((world, h, conn))
}

#[test]
fn a_review_with_no_agent_starts_one_that_gets_it() {
    let Some((world, mut h, conn)) = world_with_diff() else { return };
    let file = h.state().state.diff.as_ref().unwrap().diff.files[0].clone();
    let mut c = crate::diff::anchor("app.py", &file.rows[0]).unwrap();
    c.text = "use logging".into();
    h.state_mut().state.set_comments(&world.repo, &[c]);
    h.run_steps(2);
    h.get_by_label("Submit review (1)").click();
    h.run_steps(3);
    h.get_by_label("Start agent and send").click();
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    // The review is its first prompt, and it runs with permissions skipped.
    let cmd = wait_cmdline(&world.repo);
    assert!(cmd.contains("Review comments on your changes"), "{cmd}");
    assert!(cmd.contains("use logging"), "{cmd}");
    assert!(cmd.contains("--dangerously-skip-permissions"), "{cmd}");
    step_until(&mut h, "the comment sent", |a| a.state.comments(&world.repo).iter().all(|c| c.sent));
    assert!(h.state().state.diff_open, "still in the diff");
    // Its id was registered with the spawn: the answer it gives as it starts lands.
    let cid = h.state().state.comments(&world.repo)[0].cid.clone();
    assert!(cmd.contains(&format!("[comment_id: {cid}]")), "{cmd}");
    step_until(&mut h, "the answer in the thread", |a| {
        a.state.comments(&world.repo)[0].replies.iter().any(|r| r.who == "agent" && r.text.contains(&cid))
    });
    let c = h.state().state.comments(&world.repo).remove(0);
    assert_eq!(c.replies[0].who, "note", "the sent note first: {:?}", c.replies);
    assert!(h.state().state.awaiting.is_empty());
    conn.shutdown();
}

#[test]
fn ask_now_with_no_agent_starts_one_and_waits_for_its_answer() {
    let Some((world, mut h, conn)) = world_with_diff() else { return };
    let file = h.state().state.diff.as_ref().unwrap().diff.files[0].clone();
    let mut c = crate::diff::anchor_range("app.py", &file.rows).unwrap();
    c.text = "Why two names?".into();
    c.asked = true;
    let id = c.id();
    h.state_mut().state.upsert_comment(&world.repo, c);
    h.state_mut().state.ask_agent(&world.repo, &id);
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    let cmd = wait_cmdline(&world.repo);
    assert!(cmd.contains("A question about app.py:1-2"), "{cmd}");
    assert!(cmd.contains("--dangerously-skip-permissions"), "{cmd}");
    step_until(&mut h, "the answer in the thread", |a| {
        a.state.comments(&world.repo)[0].replies.iter().any(|r| r.who == "agent")
    });
    let c = h.state().state.comments(&world.repo).remove(0);
    assert!(cmd.contains(&format!("[comment_id: {}]", c.cid)), "{cmd}");
    assert_eq!(c.replies.last().unwrap().text, format!("A fake answer to {}.", c.cid));
    assert!(h.state().state.asks.is_empty(), "no longer waiting");
    conn.shutdown();
}

#[test]
fn a_question_reaches_a_real_agent_and_its_answer_comes_back() {
    let Some((world, mut h, conn)) = world_app() else { return };
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    let term =
        |a: &App| a.state.panes.values().map(|p| p.inner.lock().unwrap().term.frame().text()).collect::<String>();
    step_until(&mut h, "the prompt", |a| term(a).contains("your turn>"));
    std::fs::write(std::path::Path::new(&world.repo).join("app.py"), "a = 1\nb = 2\n").unwrap();
    h.key_press_modifiers(egui::Modifiers::CTRL | egui::Modifiers::SHIFT, egui::Key::D);
    step_until(&mut h, "the diff", |a| a.state.diff.as_ref().is_some_and(|d| !d.loading && !d.diff.files.is_empty()));
    let file = h.state().state.diff.as_ref().unwrap().diff.files[0].clone();
    let mut c = crate::diff::anchor_range("app.py", &file.rows).unwrap();
    c.text = "Why two names?".into();
    c.asked = true;
    let id = c.id();
    h.state_mut().state.upsert_comment(&world.repo, c);
    h.state_mut().state.ask_agent(&world.repo, &id);
    step_until(&mut h, "the question in the agent", |a| term(a).contains("A question about app.py:1-2 [comment_id: c"));
    // It answers with review_reply, mid-turn: no output to read, no turn end needed.
    step_until(&mut h, "the answer in the thread", |a| {
        a.state.comments(&world.repo).iter().any(|c| c.replies.iter().any(|r| r.who == "agent"))
    });
    let c = h.state().state.comments(&world.repo).remove(0);
    assert_eq!(c.replies[0].text, format!("A fake answer to {}.", c.cid));
    assert!(c.replies[0].id.is_some());
    assert!(h.state().state.asks.is_empty(), "no longer waiting");
    assert!(term(h.state()).contains(&format!("replied {}", c.cid)), "and in the terminal");

    // Shown again after a restart: the engine kept it, the app does not repeat it.
    h.state_mut().state.fetch_review_replies();
    h.run_steps(5);
    std::thread::sleep(std::time::Duration::from_millis(200));
    h.run_steps(5);
    assert_eq!(h.state().state.comments(&world.repo)[0].replies.len(), 1);
    conn.shutdown();
}

#[test]
fn render_the_diff_view() {
    let (app, _tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-login", "feat-login", vec![agent("a1", "Implementer", "waiting", "s1")]),
    ]);
    let mut h =
        Harness::builder().with_size([1300.0, 700.0]).wgpu().build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    let text = "diff --git a/src/login.py b/src/login.py\n--- a/src/login.py\n+++ b/src/login.py\n@@ -10,7 +10,9 @@ def login(user):\n     if not user:\n         return None\n-    token = make_token(user)\n-    return token\n+    token = make_token(user, ttl=3600)\n+    audit.log(\"login\", user.id)\n+    return token\n \n def logout(user):\n     session.clear()\ndiff --git a/tests/test_login.py b/tests/test_login.py\nnew file\ndef test_login():\n    assert login(User(1))\n";
    // A long name and a line wider than a pane, which stay in their columns.
    let long = "checkout-renders-the-payment-sections-and-keeps-them.spec.ts";
    let text = format!(
        "{text}diff --git a/{long} b/{long}\n--- a/{long}\n+++ b/{long}\n@@ -1,1 +1,1 @@\n-{}\n+{}\n",
        "expect(screen.getByRole('option', { name: 'Credit or Debit Card' })).toBeVisible(); ".repeat(2),
        "expect(screen.getByRole('option', { name: 'Bank Transfer' })).toBeVisible(); ".repeat(2),
    );
    let parsed = crate::diff::parse(&text);
    let mut c = crate::diff::anchor("src/login.py", &parsed.files[0].rows[5]).unwrap();
    c.text = "the TTL should come from settings".into();
    // A question on a range, answered, and lines being selected.
    let mut q = crate::diff::anchor_range("src/login.py", &parsed.files[0].rows[9..11]).unwrap();
    q.text = "Does logout need the audit log too?".into();
    q.asked = true;
    q.replies.push(crate::diff::Reply::new(
        "agent",
        "Yes: audit.log(\"logout\", user.id) before the clear,\nso both ends of a session are recorded.",
    ));
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo-login".into());
        let mut d = crate::diff_view::DiffState::loaded("/r/repo-login", "main", parsed);
        d.sel = Some(crate::diff_view::Sel { file: 0, side: None, from: 1, to: 2 });
        s.diff = Some(d);
        s.diff_open = true;
        s.set_comments("/r/repo-login", &[c, q]);
    }
    h.run_steps(3);
    let dir = std::env::temp_dir();
    match h.render() {
        Ok(i) => i.save(dir.join("lazyagent-desktop-diff.png")).unwrap(),
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => return,
    }
    h.state_mut().state.saved.diff_split = true;
    h.run_steps(3);
    h.render().unwrap().save(dir.join("lazyagent-desktop-diff-split.png")).unwrap();
}

#[test]
fn render_the_review_form() {
    let (app, _tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-f", "f", vec![agent("a1", "Agent 1", "waiting", "s1")]),
    ]);
    let mut h =
        Harness::builder().with_size([1300.0, 800.0]).wgpu().build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    let parsed = crate::diff::parse(&sample_diff(2, 12));
    let rows = parsed.files[0].rows.clone();
    let mut a = crate::diff::anchor_range("src/f0.rs", &rows[2..9]).unwrap();
    a.text = "What is this?".into();
    let mut b = crate::diff::anchor_range("src/f0.rs", &rows[10..13]).unwrap();
    b.text = "And this? Should it not reuse compute() instead of a second helper with the same body?".into();
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo-f".into());
        s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo-f", "main", parsed));
        s.diff_open = true;
        s.set_comments("/r/repo-f", &[a, b]);
    }
    h.run_steps(2);
    h.get_by_label("Submit review (2)").click();
    h.run_steps(3);
    let dir = std::env::temp_dir();
    match h.render() {
        Ok(i) => i.save(dir.join("lazyagent-desktop-review.png")).unwrap(),
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => return,
    }
    h.get_by_label("▸ Show prompt").click();
    h.run_steps(3);
    h.render().unwrap().save(dir.join("lazyagent-desktop-review-open.png")).unwrap();
}

#[test]
fn sidebar_keys_walk_a_long_list() {
    // A repo shaped like the one that broke: many worktrees (rows below the
    // sidebar's middle are candidates for egui's arrow-key focus moves),
    // long branch names, detached ones.
    let mut wts = vec![worktree("/r/repo", "master", vec![])];
    for i in 0..26 {
        let mut w = worktree(
            &format!("/r/repo-{i:02}"),
            &format!("TASK-{i:05}-a-very-long-branch-name-{}", "x".repeat(40)),
            vec![],
        );
        if i % 9 == 8 {
            w["branch"] = serde_json::Value::Null;
        }
        wts.push(w);
    }
    let (app, _tx) = app(wts);
    let mut h = harness(app);
    h.run_steps(3);
    let sel = |h: &Harness<'static, App>| h.state().state.selected.clone().unwrap();
    let press = |h: &mut Harness<'static, App>, mods: egui::Modifiers, key: egui::Key| {
        h.key_press_modifiers(mods, key);
        h.run_steps(2);
    };
    press(&mut h, egui::Modifiers::CTRL, egui::Key::K);
    assert_eq!(sel(&h), "/r/repo");
    for i in 0..5 {
        press(&mut h, egui::Modifiers::NONE, egui::Key::ArrowDown);
        assert_eq!(sel(&h), format!("/r/repo-{i:02}"), "↓ #{}", i + 1);
    }
    for i in 0..3 {
        press(&mut h, egui::Modifiers::NONE, egui::Key::J);
        assert_eq!(sel(&h), format!("/r/repo-{:02}", i + 5), "j #{}", i + 1);
    }
    for _ in 0..3 {
        press(&mut h, egui::Modifiers::NONE, egui::Key::ArrowUp);
    }
    assert_eq!(sel(&h), "/r/repo-04");
    press(&mut h, egui::Modifiers::NONE, egui::Key::K);
    assert_eq!(sel(&h), "/r/repo-03");
    // Nor do the side arrows.
    press(&mut h, egui::Modifiers::NONE, egui::Key::ArrowRight);
    press(&mut h, egui::Modifiers::NONE, egui::Key::ArrowLeft);
    press(&mut h, egui::Modifiers::NONE, egui::Key::ArrowDown);
    assert_eq!(sel(&h), "/r/repo-04");
}

fn notes_json(rev: u64, text: &str, by: Option<&str>, max: u64) -> lazyagent_proto::types::Notes {
    serde_json::from_value(json!({
        "worktree_path": "/r/repo", "agent_id": "a1", "notes_id": "n1", "rev": rev, "size": text.len(),
        "max_bytes": max, "by": by, "updated_at": crate::app::unix_now() - 120.0, "text": text, "state": "live",
        "kind": "agent", "mode": "manual", "conversation_id": null, "instruction": "fix the login bug",
        "created_at": 1.0, "ended_at": null, "empty": false
    }))
    .unwrap()
}

/// An app with one agent whose notes are already loaded (no engine).
fn notes_app(text: &str, max: u64) -> (Harness<'static, App>, mpsc::Sender<Msg>) {
    let (app, tx) = app(vec![worktree("/r/repo", "main", vec![agent("a1", "Implementer", "running", "s1")])]);
    let mut h = harness(app);
    h.run_steps(2);
    let mut doc = crate::notes_view::NotesDoc::default();
    doc.loaded(notes_json(1, text, Some("agent"), max));
    h.state_mut().state.notes.insert(("/r/repo".into(), "a1".into()), doc);
    (h, tx)
}

fn notes_doc<'a>(h: &'a Harness<'static, App>) -> &'a crate::notes_view::NotesDoc {
    &h.state().state.notes[&("/r/repo".to_string(), "a1".to_string())]
}

#[test]
fn n_toggles_the_notes_panel_outside_a_terminal_only() {
    let (mut h, _tx) = notes_app("## Goal\n- ship\n", 8192);
    h.run_steps(2);
    let pane = h.state().state.panes.get("s1").cloned().expect("the agent's terminal has the keyboard");
    // A real key press is a key event and its text.
    h.key_press(egui::Key::N);
    h.event(egui::Event::Text("n".into()));
    h.run_steps(2);
    assert!(!h.state().state.notes_open, "inside a terminal n is the agent's");
    assert_eq!(pane.inner.lock().unwrap().held, b"n");
    // Outside a terminal it opens the panel, with the editor focused.
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::N);
    h.run_steps(3);
    assert!(h.state().state.notes_open);
    assert!(h.query_all_by_label("Session notes").count() >= 1);
    assert!(h.query_all_by_label("fix the login bug").count() >= 1, "the pinned instruction");
    assert!(h.query_all_by_label("last edit 2m ago by agent · rev 1 · mode manual").count() >= 1);
    assert!(h.ctx.text_edit_focused(), "n puts the keyboard in the notes");
    // Typing there is text: s and x open no dialog, n does not close it.
    h.event(egui::Event::Text("sxn".into()));
    h.run_steps(2);
    assert!(h.state().state.dialog.is_none());
    assert!(h.state().state.notes_open);
    assert!(notes_doc(&h).draft.contains("sxn"), "{:?}", notes_doc(&h).draft);
    assert!(notes_doc(&h).dirty());
    assert!(h.query_all_by_label("unsaved").count() >= 1);
    // Esc leaves the editor; n then closes the panel. The header button too.
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    h.key_press(egui::Key::N);
    h.run_steps(2);
    assert!(!h.state().state.notes_open);
    h.get_by_label("Notes").click();
    h.run_steps(2);
    assert!(h.state().state.notes_open);
    // Revert drops the edit.
    h.get_by_label("Revert").click();
    h.run_steps(2);
    assert!(!notes_doc(&h).dirty());
}

#[test]
fn notes_panel_hidden_against_an_engine_without_notes() {
    let (mut h, _tx) = notes_app("x", 8192);
    h.state_mut().state.capabilities = Some(vec!["get_sidebar_state".into()]);
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::N);
    h.run_steps(2);
    assert!(!h.state().state.notes_open);
    assert_eq!(h.query_all_by_label("Notes").count(), 0, "no header button");
    assert!(h.state().state.toasts.iter().any(|t| t.text.contains("no session notes")));
}

#[test]
fn a_dirty_draft_gets_a_banner_instead_of_a_reload() {
    let (mut h, tx) = notes_app("## Goal\n- ship\n", 8192);
    h.state_mut().state.notes_open = true;
    h.run_steps(2);
    // Clean: a newer text replaces it silently.
    let key = ("/r/repo".to_string(), "a1".to_string());
    h.state_mut().state.notes.get_mut(&key).unwrap().loaded(notes_json(2, "## Goal\n- ship it\n", Some("agent"), 8192));
    h.run_steps(2);
    assert_eq!(notes_doc(&h).draft, "## Goal\n- ship it\n");
    assert_eq!(h.query_all_by_label("Reload (lose mine)").count(), 0);
    // Dirty: the event marks a newer rev; its text (fetched) waits behind a banner.
    h.state_mut().state.notes.get_mut(&key).unwrap().draft.push_str("- mine\n");
    tx.send(event(
        "notes.updated",
        json!({"worktree_path": "/r/repo", "agent_id": "a1", "notes_id": "n1", "rev": 3, "size": 10, "by": "orchestrator"}),
    ))
    .unwrap();
    h.run_steps(2);
    assert_eq!(notes_doc(&h).latest, 3);
    {
        let d = h.state_mut().state.notes.get_mut(&key).unwrap();
        // The fetch failed (no engine here); answer it as the engine would.
        d.error = None;
        d.loaded(notes_json(3, "## Goal\n- theirs\n", Some("orchestrator"), 8192));
    }
    h.run_steps(2);
    assert_eq!(notes_doc(&h).draft, "## Goal\n- ship it\n- mine\n", "the user's draft is kept");
    assert!(h.query_all_by_label("Changed by orchestrator while you edit (rev 3)").count() >= 1);
    h.get_by_label("Keep editing (save will conflict)").click();
    h.run_steps(2);
    assert_eq!(h.query_all_by_label("Reload (lose mine)").count(), 0);
    assert_eq!(notes_doc(&h).base_rev, 2, "a save will still send if_rev 2");
    // Reload takes theirs.
    h.state_mut().state.notes.get_mut(&key).unwrap().incoming.as_mut().unwrap().dismissed = false;
    h.run_steps(2);
    h.get_by_label("Reload (lose mine)").click();
    h.run_steps(2);
    assert_eq!((notes_doc(&h).draft.as_str(), notes_doc(&h).base_rev), ("## Goal\n- theirs\n", 3));
}

#[test]
fn a_conflict_keeps_the_draft_and_shows_both() {
    let (mut h, _tx) = notes_app("## Goal\n", 8192);
    h.state_mut().state.notes_open = true;
    h.run_steps(2);
    let key = ("/r/repo".to_string(), "a1".to_string());
    {
        let d = h.state_mut().state.notes.get_mut(&key).unwrap();
        d.draft = "## Goal\n- MY DRAFT\n".into();
        d.save_failed(&lazyagent_proto::ClientError::Rpc(lazyagent_proto::RpcError {
            code: "CONFLICT".into(),
            message: "notes changed (rev 5): read them again".into(),
            data: Some(json!({"rev": 5, "text": "## Goal\n- THEIR TEXT\n"})),
        }));
    }
    h.run_steps(3);
    assert!(h.query_all_by_label("Not saved: the notes changed (rev 5). Yours are kept above theirs.").count() >= 1);
    assert!(h.query_all_by_label("Theirs (rev 5)").count() >= 1);
    assert!(h.query_all_by_label("## Goal\n- THEIR TEXT\n").count() >= 1, "their text is shown");
    assert_eq!(notes_doc(&h).draft, "## Goal\n- MY DRAFT\n");
    // Overwrite re-puts with their rev (no engine: it fails and says so, the draft stays).
    h.get_by_label("Overwrite with mine").click();
    h.run_steps(3);
    let d = notes_doc(&h);
    assert_eq!(d.draft, "## Goal\n- MY DRAFT\n");
    assert!(d.error.as_deref().is_some_and(|e| e.contains("not connected")), "{:?}", d.error);
    h.state_mut().state.notes.get_mut(&key).unwrap().error = None;
    h.run_steps(2);
    h.get_by_label("Take theirs").click();
    h.run_steps(2);
    let d = notes_doc(&h);
    assert_eq!((d.draft.as_str(), d.base_rev, d.conflict.is_none()), ("## Goal\n- THEIR TEXT\n", 5, true));
}

#[test]
fn the_size_meter_turns_amber_then_red() {
    use crate::notes_view::{Meter, meter, meter_color};
    let (mut h, _tx) = notes_app(&"x".repeat(79), 100);
    h.state_mut().state.notes_open = true;
    h.run_steps(2);
    let t = h.state().state.theme;
    let key = ("/r/repo".to_string(), "a1".to_string());
    let level = |h: &mut Harness<'static, App>, n: usize| {
        h.state_mut().state.notes.get_mut(&key).unwrap().draft = "x".repeat(n);
        h.run_steps(2);
        let label = format!("{n} / 100 bytes");
        assert!(h.query_all_by_label(&label).count() >= 1, "{label}");
        meter(n, 100)
    };
    assert_eq!(level(&mut h, 79), Meter::Ok);
    assert_eq!(level(&mut h, 80), Meter::Warn);
    assert_eq!(level(&mut h, 100), Meter::Full);
    assert_eq!(h.query_all_by_label("over the cap: condense them").count(), 0, "at the cap it still saves");
    assert_eq!(level(&mut h, 101), Meter::Full);
    assert!(h.query_all_by_label("over the cap: condense them").count() >= 1);
    // Over the cap Save is off: a click sends nothing (here it would fail: no engine).
    h.get_by_label("Save").click();
    h.run_steps(2);
    assert!(notes_doc(&h).error.is_none() && !notes_doc(&h).saving);
    assert_eq!(meter_color(&t, Meter::Warn), t.yellow);
    assert_eq!(meter_color(&t, Meter::Full), t.red);
}

/// The notes file the engine of `world` keeps for `notes_id`, once it is
/// there (the engine writes it from a worker thread, after the reply).
fn notes_file(world: &lazyagent_testkit::World, notes_id: &str) -> std::path::PathBuf {
    let find = || {
        let mut dirs = vec![world.tmp.path().join("state/lazyagent/notes")];
        while let Some(d) = dirs.pop() {
            for e in std::fs::read_dir(&d).into_iter().flatten().flatten() {
                let p = e.path();
                if p.is_dir() {
                    dirs.push(p);
                } else if p.file_name().is_some_and(|n| n.to_string_lossy() == format!("{notes_id}.md")) {
                    return Some(p);
                }
            }
        }
        None
    };
    let deadline = Instant::now() + std::time::Duration::from_secs(10);
    loop {
        if let Some(p) = find() {
            return p;
        }
        assert!(Instant::now() < deadline, "no {notes_id}.md under the world's state dir");
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
}

#[test]
fn notes_edited_in_the_panel_reach_the_file_and_the_agents_next_turn() {
    let Some((world, mut h, conn)) = world_app() else { return };
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    let key = (world.repo.clone(), h.state().state.store.agents(&world.repo)[0].agent_id.clone());
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::N);
    step_until(&mut h, "the notes loaded", |a| a.state.notes.get(&key).is_some_and(|d| d.meta.is_some()));
    assert_eq!(h.state().state.notes[&key].base_rev, 0);
    h.run_steps(3);
    assert!(h.ctx.text_edit_focused());
    h.event(egui::Event::Text("from the panel".into()));
    h.run_steps(2);
    h.key_press_modifiers(egui::Modifiers::COMMAND, egui::Key::S);
    step_until(&mut h, "saved", |a| a.state.notes[&key].base_rev == 1 && !a.state.notes[&key].dirty());
    let d = &h.state().state.notes[&key];
    assert_eq!(d.meta.as_ref().unwrap().by.as_deref(), Some("user"));
    let notes_id = d.meta.as_ref().unwrap().notes_id.clone();
    let draft = d.draft.clone();
    assert!(draft.contains("from the panel"));
    let path = notes_file(&world, &notes_id);
    let on_disk = || std::fs::read_to_string(&path).unwrap_or_default();
    let deadline = Instant::now() + std::time::Duration::from_secs(10);
    while on_disk() != draft && Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    assert_eq!(on_disk(), draft, "the engine wrote the panel's text");
    // The agent's next turn: N3's hook (as Claude runs it) prints the notice.
    let hook = lazyagent_testkit::source_root().join("src/lazyagent/notes_hook.py");
    let mut child = std::process::Command::new(&world.python)
        .arg(&hook)
        .env("LAZYAGENT_NOTES_PATH", &path)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    child
        .stdin
        .take()
        .unwrap()
        .write_all(br#"{"hook_event_name": "UserPromptSubmit", "prompt": "go on", "session_id": "c1"}"#)
        .unwrap();
    let out = child.wait_with_output().unwrap();
    let out = String::from_utf8_lossy(&out.stdout);
    assert!(out.contains("Your session notes were updated by user"), "{out}");
    assert!(out.contains("from the panel"), "{out}");
    // An edit by someone else meanwhile: the panel's next save conflicts, nothing is overwritten.
    let (probe, _, _rx) = world.connect();
    let mut put = json!({"worktree_path": key.0, "agent_id": key.1, "text": "## Goal\n- by the orchestrator\n"});
    put["if_rev"] = json!(1);
    probe.request("notes_put", put, lazyagent_testkit::TIMEOUT).unwrap();
    h.state_mut().state.notes.get_mut(&key).unwrap().draft.push_str("\nmore of mine");
    step_until(&mut h, "the banner", |a| a.state.notes[&key].incoming.is_some());
    h.state_mut().state.save_notes(&key, None);
    step_until(&mut h, "the conflict", |a| a.state.notes[&key].conflict.is_some());
    let d = &h.state().state.notes[&key];
    assert!(d.draft.ends_with("more of mine"));
    assert_eq!(d.conflict.as_ref().unwrap().text, "## Goal\n- by the orchestrator\n");
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "## Goal\n- by the orchestrator\n");
    conn.shutdown();
}

fn ended_item(notes_id: &str, agent_id: &str, state: &str) -> lazyagent_proto::types::NotesListItem {
    let now = crate::app::unix_now();
    serde_json::from_value(json!({
        "notes_id": notes_id, "state": state, "worktree_path": "/r/repo", "agent_id": agent_id, "kind": "agent",
        "size": 2048, "empty": false, "by": "agent", "updated_at": now - 3.0 * 3600.0,
        "ended_at": now - 3.0 * 3600.0, "expires_at": (state == "orphan").then_some(now + 7.0 * 86_400.0 - 3.0 * 3600.0)
    }))
    .unwrap()
}

/// The worktree's ended notes, as a `notes_list` would have left them.
fn set_earlier(h: &mut Harness<'static, App>, items: Vec<lazyagent_proto::types::NotesListItem>) {
    let e = crate::notes_view::Earlier { items, loaded: true, ..Default::default() };
    h.state_mut().state.earlier.insert("/r/repo".into(), e);
}

#[test]
fn the_stop_dialog_offers_the_handoff_only_with_notes() {
    let (mut h, _tx) = notes_app("## Goal\n- ship\n", 8192);
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::X);
    h.run_steps(3);
    // Not empty: three ways to stop, no plain Stop button.
    for label in ["Copy notes and stop (c)", "Stop, keep notes (k)", "Stop and delete notes (d)"] {
        assert_eq!(h.query_all_by_label(label).count(), 1, "{label}");
    }
    assert_eq!(h.query_all_by_label("Stop").count(), 1, "only the header's: the dialog has no plain Stop");
    assert!(h.query_all_by_label("Its session notes are not empty. Before they go:").count() >= 1);
    // Unsaved edits in the panel are called out.
    let key = ("/r/repo".to_string(), "a1".to_string());
    h.state_mut().state.notes.get_mut(&key).unwrap().draft.push_str("- unsaved\n");
    h.run_steps(2);
    assert!(h.query_all_by_label("The notes panel has unsaved edits: they are not in them (save first).").count() >= 1);
    // c submits the copy (no engine here: the stop fails and says so in the dialog).
    h.key_press(egui::Key::C);
    h.run_steps(3);
    let d = h.state().state.dialog.as_ref().unwrap();
    assert!(d.error.as_deref().is_some_and(|e| e.contains("Could not stop")), "{:?}", d.error);
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    // Only the template: a plain stop.
    {
        let d = h.state_mut().state.notes.get_mut(&key).unwrap();
        d.revert();
        d.meta.as_mut().unwrap().empty = true;
    }
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Stop);
    h.run_steps(3);
    assert_eq!(h.query_all_by_label("Copy notes and stop (c)").count(), 0);
    assert_eq!(h.query_all_by_label("Stop").count(), 2, "the header's and the dialog's");
    // Not read yet: a plain stop too (it keeps them, to be safe).
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    h.state_mut().state.notes.clear();
    let s = &h.state().state;
    assert_eq!(crate::dialogs::handoff(s, "/r/repo", Some("a1")), crate::dialogs::Handoff::Checking);
    // An engine without notes: as before N5.
    h.state_mut().state.capabilities = Some(vec!["stop_agent".into()]);
    let s = &h.state().state;
    assert_eq!(crate::dialogs::handoff(s, "/r/repo", Some("a1")), crate::dialogs::Handoff::Unsupported);
}

#[test]
fn text_for_the_clipboard_goes_out_with_the_next_frame() {
    let (mut h, _tx) = notes_app("x", 8192);
    h.state_mut().state.clipboard = Some("## Goal\n- handed off\n".into());
    h.run();
    h.step();
    assert_eq!(h.state().state.last_copied.as_deref(), Some("## Goal\n- handed off\n"));
    assert!(h.state().state.clipboard.is_none());
}

#[test]
fn the_spawn_dialog_warns_only_with_ended_notes() {
    let (mut h, _tx) = notes_app("x", 8192);
    let warning = |h: &Harness<'static, App>| {
        h.query_all_by_label_contains(
            "A new conversation deletes the earlier notes of Agent a2 (orphan, 2.0 KiB, ended 3h ago, deleted in 7d)",
        )
        .count()
    };
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::S);
    h.run_steps(3);
    assert!(h.state().state.dialog.is_some());
    assert_eq!(warning(&h), 0, "no ended notes: no warning");
    assert_eq!(h.query_all_by_label("Carry them into the new agent").count(), 0);
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    set_earlier(&mut h, vec![ended_item("o2", "a2", "orphan")]);
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Spawn);
    h.run_steps(3);
    assert_eq!(warning(&h), 1);
    assert_eq!(h.state().state.earlier["/r/repo"].items.len(), 1, "a failed refresh keeps what was listed");
    h.get_by_label("Carry them into the new agent").click();
    h.run_steps(2);
    assert!(h.state().state.dialog.as_ref().unwrap().carry);
    // Resuming deletes nothing (no rule 5): no warning.
    h.get_by_label("Resume the last (l)").click();
    h.run_steps(2);
    assert_eq!(warning(&h), 0);
    h.get_by_label("New").click();
    h.run_steps(2);
    // The link opens the panel's list.
    h.get_by_label("See them in the notes panel").click();
    h.run_steps(3);
    assert!(h.state().state.dialog.is_none());
    assert!(h.state().state.notes_open);
    assert!(h.query_all_by_label("Earlier notes in this worktree (1)").count() >= 1);
}

#[test]
fn the_panel_lists_earlier_notes_to_view_adopt_or_delete() {
    let (mut h, _tx) = notes_app("## Goal\n- mine\n", 8192);
    set_earlier(&mut h, vec![ended_item("o2", "a2", "orphan"), ended_item("r1", "a0", "restorable")]);
    h.state_mut().state.notes_open = true;
    h.run_steps(3);
    assert!(h.query_all_by_label("Earlier notes in this worktree (2)").count() >= 1);
    assert!(h.query_all_by_label("Agent a2").count() >= 1);
    assert!(h.query_all_by_label("orphan, 2.0 KiB, ended 3h ago, deleted in 7d").count() >= 1);
    assert!(h.query_all_by_label("restorable, 2.0 KiB, ended 3h ago").count() >= 1);
    // View fetches the text (no engine: says it could not).
    // The first "View" is the menu bar's.
    h.get_all_by_label("View").nth(1).unwrap().click();
    h.run_steps(3);
    let e = &h.state().state.earlier["/r/repo"];
    assert_eq!(e.viewing.as_ref().map(|(id, _)| id.as_str()), Some("o2"));
    assert!(e.viewing.as_ref().unwrap().1.as_deref().is_some_and(|t| t.contains("could not read")));
    h.get_by_label("Hide").click();
    h.run_steps(2);
    assert!(h.state().state.earlier["/r/repo"].viewing.is_none());
    // This agent's notes are not empty: Adopt asks first.
    h.get_all_by_label("Adopt into this agent").next().unwrap().click();
    h.run_steps(2);
    assert!(h.query_all_by_label("Replace this agent's notes with them?").count() >= 1);
    h.get_by_label("Cancel").click();
    h.run_steps(2);
    assert!(h.state().state.earlier["/r/repo"].confirm.is_none());
    h.get_all_by_label("Delete").nth(1).unwrap().click();
    h.run_steps(2);
    let e = &h.state().state.earlier["/r/repo"];
    assert_eq!(e.confirm, Some((crate::notes_view::Confirm::Delete, "r1".to_string())));
    assert!(h.query_all_by_label("Delete them for good?").count() >= 1);
    h.get_by_label("Delete them").click();
    h.run_steps(3);
    assert!(h.state().state.toasts.iter().any(|t| t.text.contains("Could not delete the notes")), "no engine");
    // A worktree with no agent still lists them; Adopt waits for one.
    h.state_mut().state.store.worktrees[0].agents.clear();
    h.run_steps(3);
    assert!(h.query_all_by_label("No agent here: notes belong to an agent.").count() >= 1);
    assert!(h.query_all_by_label("Earlier notes in this worktree (2)").count() >= 1);
    use egui_kittest::kittest::NodeT;
    assert!(h.get_all_by_label("Adopt into this agent").next().unwrap().accesskit_node().is_disabled());
}

#[test]
fn a_crashed_agents_notes_are_offered_adopted_and_handed_off() {
    let Some((world, mut h, conn)) = world_app() else { return };
    let (probe, _, _rx) = world.connect();
    let repo = world.repo.clone();
    let notes_of = |agent: &str| {
        probe.request("notes_get", json!({"worktree_path": repo, "agent_id": agent}), lazyagent_testkit::TIMEOUT)
    };
    let ended = || {
        let v = probe.request("notes_list", json!({"worktree_path": repo}), lazyagent_testkit::TIMEOUT).unwrap();
        v["notes"].as_array().unwrap().iter().filter(|n| n["state"] != "live").cloned().collect::<Vec<_>>()
    };
    let only_agent =
        |h: &Harness<'static, App>| h.state().state.store.agents(&repo).first().map(|a| a.agent_id.clone());
    // a1 writes its notes, then exits on its own: an orphan, announced.
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "the agent's prompt", |a| {
        a.state.panes.values().any(|p| p.inner.lock().unwrap().term.frame().text().contains("your turn>"))
    });
    let a1 = only_agent(&h).unwrap();
    let text = "## Goal\n- carried from a1\n";
    let put = json!({"worktree_path": repo, "agent_id": a1, "text": text, "if_rev": 0});
    probe.request("notes_put", put, lazyagent_testkit::TIMEOUT).unwrap();
    h.event(egui::Event::Text("exit 3".into()));
    h.key_press(egui::Key::Enter);
    step_until(&mut h, "the orphan announced", |a| {
        a.state
            .toasts
            .iter()
            .any(|t| t.text == format!("Agent {a1} exited; its notes are kept 7 days (Notes, n: Earlier notes)."))
    });
    let orphan = ended();
    assert_eq!((orphan.len(), orphan[0]["state"].as_str()), (1, Some("orphan")));
    // The panel lists it even with no agent here.
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::N);
    step_until(&mut h, "the list", |a| a.state.earlier.get(&repo).is_some_and(|e| e.items.len() == 1));
    h.run_steps(2);
    assert!(h.query_all_by_label("Earlier notes in this worktree (1)").count() >= 1);
    // Resuming deletes nothing: a2 starts and adopts them from the panel.
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Spawn);
    h.run_steps(3);
    h.get_by_label("Resume the last (l)").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "a2", |a| a.state.store.agents(&repo).len() == 1);
    let a2 = only_agent(&h).unwrap();
    let k2 = (repo.clone(), a2.clone());
    step_until(&mut h, "a2's notes in the panel", |a| a.state.notes.get(&k2).is_some_and(|d| d.meta.is_some()));
    h.run_steps(3);
    h.get_by_label("Adopt into this agent").click();
    step_until(&mut h, "adopted", |a| {
        a.state.notes.get(&k2).is_some_and(|d| d.draft == text && !d.dirty())
            && a.state.earlier.get(&repo).is_some_and(|e| e.loaded && e.items.is_empty())
    });
    assert_eq!(notes_of(&a2).unwrap()["text"], text);
    assert!(ended().is_empty(), "the orphan is gone");
    // Stop a2 keeping its notes: an orphan again.
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Stop);
    step_until(&mut h, "the handoff", |a| {
        matches!(crate::dialogs::handoff(&a.state, &repo, Some(&a2)), crate::dialogs::Handoff::Offer { .. })
    });
    h.run_steps(2);
    h.get_by_label("Stop, keep notes (k)").click();
    step_until(&mut h, "a2 stopped, notes kept", |a| {
        a.state.store.agents(&repo).is_empty()
            && a.state
                .toasts
                .iter()
                .any(|t| t.text.starts_with(&format!("Agent {a2} stopped; its notes are kept 7 days")))
    });
    assert_eq!(ended().len(), 1);
    // A new conversation would delete them: the dialog says so and carries them.
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Spawn);
    step_until(&mut h, "the warning", |a| {
        a.state.earlier.get(&repo).is_some_and(|e| e.loaded && !e.stale && e.items.len() == 1)
    });
    h.run_steps(2);
    assert!(
        h.query_all_by_label_contains(&format!("A new conversation deletes the earlier notes of Agent {a2}")).count()
            >= 1
    );
    h.get_by_label("Carry them into the new agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "a3", |a| a.state.store.agents(&repo).len() == 1);
    let a3 = only_agent(&h).unwrap();
    let deadline = Instant::now() + std::time::Duration::from_secs(10);
    while notes_of(&a3).unwrap()["text"] != text && Instant::now() < deadline {
        h.step();
    }
    assert_eq!(notes_of(&a3).unwrap()["text"], text, "carried into the new agent");
    assert!(ended().is_empty(), "the new conversation pruned the old ones");
    // Stop a3 copying them: on the clipboard, and gone.
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Stop);
    step_until(&mut h, "the handoff", |a| {
        matches!(crate::dialogs::handoff(&a.state, &repo, Some(&a3)), crate::dialogs::Handoff::Offer { .. })
    });
    h.run_steps(2);
    h.get_by_label("Copy notes and stop (c)").click();
    step_until(&mut h, "copied", |a| a.state.last_copied.as_deref() == Some(text));
    step_until(&mut h, "a3 stopped", |a| a.state.store.agents(&repo).is_empty());
    assert!(ended().is_empty(), "copy then delete");
    conn.shutdown();
}

/// Runs N3's hook on `path` with `payload`, as Claude would; its stdout.
fn run_notes_hook(world: &lazyagent_testkit::World, path: &std::path::Path, payload: &str) -> String {
    use std::io::Write;
    let hook = lazyagent_testkit::source_root().join("src/lazyagent/notes_hook.py");
    let mut child = std::process::Command::new(&world.python)
        .arg(&hook)
        .env("LAZYAGENT_NOTES_PATH", path)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(payload.as_bytes()).unwrap();
    String::from_utf8_lossy(&child.wait_with_output().unwrap().stdout).into_owned()
}

/// The MVP demo (plan N5's acceptance), with the fake agent: notes written
/// in the panel, a compaction re-injects them, the engine restarts, the
/// agent is restored with the same notes, then stopped with the handoff.
#[test]
fn the_mvp_demo_notes_survive_compaction_and_restore_then_hand_off() {
    let Some((mut world, mut h, conn)) = world_app() else { return };
    let repo = world.repo.clone();
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.get_by_label("Spawn").click();
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    let key = (repo.clone(), h.state().state.store.agents(&repo)[0].agent_id.clone());
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::N);
    step_until(&mut h, "the notes loaded", |a| a.state.notes.get(&key).is_some_and(|d| d.meta.is_some()));
    h.run_steps(3);
    h.event(egui::Event::Text("- [ ] keep the API stable\n".into()));
    h.run_steps(2);
    h.key_press_modifiers(egui::Modifiers::COMMAND, egui::Key::S);
    step_until(&mut h, "saved", |a| a.state.notes[&key].base_rev == 1 && !a.state.notes[&key].dirty());
    let text = h.state().state.notes[&key].draft.clone();
    let notes_id = h.state().state.notes[&key].meta.as_ref().unwrap().notes_id.clone();
    let path = notes_file(&world, &notes_id);
    let deadline = Instant::now() + std::time::Duration::from_secs(10);
    while std::fs::read_to_string(&path).unwrap_or_default() != text && Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    // Compaction: the hook gives the notes back as context.
    let out = run_notes_hook(
        &world,
        &path,
        r#"{"hook_event_name": "SessionStart", "source": "compact", "session_id": "c1"}"#,
    );
    assert!(out.contains("additionalContext") && out.contains("keep the API stable"), "{out}");
    // The engine restarts: the agent's notes are a restorable agent's now.
    world.terminate_engines();
    world.start_engine();
    step_until(&mut h, "the restore offer", |a| a.state.link == Link::Connected && a.state.restorable.len() == 1);
    assert_eq!(h.state().state.restorable[0].notes_id.as_deref(), Some(notes_id.as_str()));
    step_until(&mut h, "listed as earlier notes", |a| {
        a.state
            .earlier
            .get(&repo)
            .is_some_and(|e| e.items.iter().any(|n| n.notes_id == notes_id && n.state == "restorable"))
    });
    h.get_by_label("Restore all (Shift+R)").click();
    step_until(&mut h, "the agent back", |a| a.state.store.agent_count() == 1 && a.state.restorable.is_empty());
    let back = (repo.clone(), h.state().state.store.agents(&repo)[0].agent_id.clone());
    step_until(&mut h, "its notes in the panel", |a| {
        a.state.notes.get(&back).and_then(|d| d.meta.as_ref()).is_some_and(|m| m.notes_id == notes_id && m.text == text)
    });
    step_until(&mut h, "no earlier notes left", |a| {
        a.state.earlier.get(&repo).is_some_and(|e| e.loaded && e.items.is_empty())
    });
    let out =
        run_notes_hook(&world, &path, r#"{"hook_event_name": "SessionStart", "source": "resume", "session_id": "c1"}"#);
    assert!(out.contains("keep the API stable"), "{out}");
    // Stop with the handoff.
    h.state_mut().state.open_dialog(crate::dialogs::Kind::Stop);
    step_until(&mut h, "the handoff", |a| {
        matches!(crate::dialogs::handoff(&a.state, &back.0, Some(&back.1)), crate::dialogs::Handoff::Offer { .. })
    });
    h.run_steps(2);
    h.key_press(egui::Key::C);
    step_until(&mut h, "copied", |a| a.state.last_copied.as_deref() == Some(text.as_str()));
    step_until(&mut h, "stopped", |a| a.state.store.agent_count() == 0);
    assert!(!path.exists(), "copied, then deleted");
    conn.shutdown();
}

#[test]
fn the_diff_base_shows_the_pr_base_and_takes_an_override() {
    let (app, _tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-f", "TASK-123-f", vec![agent("a1", "Implementer", "running", "s1")]),
    ]);
    let mut h = harness(app);
    h.run_steps(2);
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo-f".into());
        let committed = sample_diff(1, 4);
        let uncommitted = sample_diff(2, 4).replace("src/f0.rs", "src/wip.rs");
        let mut d =
            crate::diff_view::DiffState::loaded("/r/repo-f", "", crate::diff::parse_pr(&committed, &uncommitted));
        d.applied(
            Some(lazyagent_proto::types::DiffBase {
                ref_: "origin/TASK-456-checkout".into(),
                branch: Some("TASK-456-checkout".into()),
                source: "pr".into(),
                merge_base: "0123456789abcdef".into(),
                pr_number: Some(4242),
            }),
            None,
            None,
        );
        s.diff = Some(d);
        s.diff_open = true;
    }
    h.run_steps(2);
    // What is used, and where it came from; the uncommitted files apart.
    assert_eq!(h.state().state.diff.as_ref().unwrap().base, "origin/TASK-456-checkout");
    h.get_by_label("(PR #4242 base)");
    h.get_by_label("3 files");
    h.get_by_label("2 uncommitted");
    assert!(h.state().state.diff.as_ref().unwrap().rows.contains(&crate::diff_view::DRow::Uncommitted));
    assert!(h.query_by_label("PR base").is_none(), "no way back needed: it is the PR base");
    // Hidden, then shown again.
    h.get_by_role_and_label(egui::accesskit::Role::CheckBox, "Uncommitted").click();
    h.run_steps(2);
    assert!(h.state().state.saved.diff_hide_uncommitted);
    h.get_by_label("1 files");
    assert!(!h.state().state.diff.as_ref().unwrap().rows.contains(&crate::diff_view::DRow::Uncommitted));
    h.get_by_role_and_label(egui::accesskit::Role::CheckBox, "Uncommitted").click();
    h.run_steps(2);
    assert!(!h.state().state.saved.diff_hide_uncommitted);
    // An override: any ref, diffed again (fetching) from the refresh button.
    h.state_mut().state.diff.as_mut().unwrap().base = "origin/master".into();
    h.get_by_label("↻ Fetch").click();
    h.run_steps(2);
    let d = h.state().state.diff.as_ref().unwrap();
    assert_eq!((d.requested.as_str(), d.base.as_str()), ("origin/master", "origin/master"));
    // Back to the engine's pick.
    h.get_by_label("PR base").click();
    h.run_steps(2);
    let d = h.state().state.diff.as_ref().unwrap();
    assert_eq!(d.requested, "");
}

// ----------------------------------------------------------------------
// Updates and restarts (lifecycle.rs)
// ----------------------------------------------------------------------

fn start(env: &[(&str, &str)], args: &[&str]) -> crate::lifecycle::Start {
    crate::lifecycle::Start {
        env: env.iter().map(|(k, v)| ((*k).into(), (*v).into())).collect(),
        args: args.iter().map(|a| a.to_string()).collect(),
    }
}

#[test]
fn a_relaunch_runs_a_fresh_launch_with_the_start_environment() {
    use crate::lifecycle::{REPORT_ENV, relaunch_command};
    let report = std::path::Path::new("/run/x/report.json");
    // `lazyagent desktop` (uv run): through the same Python.
    let s = start(
        &[("LAZYAGENT_PYTHON", "/v/bin/python"), (REPORT_ENV, "/old.json"), ("PATH", "/usr/bin")],
        &["/r/repo", "--theme", "light", "--screenshot", "/tmp/x.png"],
    );
    let (program, args, env) = relaunch_command(&s, "/r/repo", None, Some(report));
    assert_eq!(program, std::path::PathBuf::from("/v/bin/python"));
    assert_eq!(args, ["-m", "lazyagent", "desktop", "/r/repo", "--theme", "light"]);
    let reports: Vec<_> = env.iter().filter(|(k, _)| k == REPORT_ENV).collect();
    assert_eq!(reports.len(), 1, "the previous report is not passed on");
    assert_eq!(reports[0].1, report.as_os_str());

    // No Python: `lazyagent` on the start PATH.
    let dir = tempfile::tempdir().unwrap();
    let bin = dir.path().join("lazyagent");
    std::fs::write(&bin, "#!/bin/sh\n").unwrap();
    std::fs::set_permissions(&bin, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let s = start(&[("PATH", dir.path().to_str().unwrap())], &[]);
    let (program, args, _) = relaunch_command(&s, "/r/repo", None, None);
    assert_eq!((program, args), (bin, vec!["desktop".to_string(), "/r/repo".to_string()]));

    // Neither: the binary itself, even once replaced on disk.
    let s = start(&[("PATH", "/nonexistent")], &[]);
    let exe = std::path::PathBuf::from("/home/u/.cargo/bin/lazyagent-desktop (deleted)");
    let (program, args, _) = relaunch_command(&s, "/r/repo", Some(exe), None);
    assert_eq!(program, std::path::PathBuf::from("/home/u/.cargo/bin/lazyagent-desktop"));
    assert_eq!(args, ["/r/repo"]);
}

#[test]
fn the_restart_report_becomes_a_toast() {
    use crate::app::Severity;
    use crate::lifecycle::report_text;
    assert_eq!(report_text(&json!({"engine": false})), Some((Severity::Info, "Restarted.".into())));
    let ok = json!({"engine": true, "result": {"restored": [{}, {}], "failed": [], "error": null}});
    assert_eq!(report_text(&ok), Some((Severity::Info, "Engine restarted · 2 agent(s) restored".into())));
    let partly = json!({"engine": true, "result": {"restored": [{}], "failed": [{"id": "e1", "reason": "gone"}], "error": null}});
    assert_eq!(
        report_text(&partly),
        Some((Severity::Warning, "Engine restarted · 1 agent(s) restored · 1 failed (e1: gone)".into()))
    );
    let failed = json!({"engine": true, "result": null, "error": "no python"});
    assert_eq!(report_text(&failed), Some((Severity::Error, "The engine restart failed: no python".into())));
}

#[test]
fn the_menu_bar_opens_restart_and_flags_busy_agents() {
    let (app, tx) = app(vec![
        worktree("/r/repo", "main", vec![agent("a1", "Idle one", "waiting_for_user", "s1")]),
        worktree("/r/repo-feat", "feat", vec![agent("a2", "Busy one", "running", "s2")]),
    ]);
    let mut h = harness(app);
    h.run_steps(3);
    for menu in ["File", "View", "Agents", "Help"] {
        assert_eq!(h.query_all_by_label(menu).count(), 1, "{menu}");
    }
    h.get_by_label("File").click();
    h.run_steps(2);
    h.get_by_label("Restart…").click();
    h.run_steps(3);
    assert!(h.query_all_by_label("Restart lazyagent").count() >= 1);
    assert!(h.query_all_by_label("Only the app restarts; the agents keep running.").count() >= 1);
    h.get_by_label("Restart the engine too").click();
    h.run_steps(3);
    let busy = h.query_all_by_label_contains("⚠ Busy one").count();
    assert_eq!(busy, 1, "the running agent is flagged");
    assert_eq!(h.query_all_by_label("Restart anyway").count(), 1);
    // Rows: busy first.
    let rows = crate::lifecycle::stopping(&h.state().state.store);
    assert_eq!(
        rows.iter().map(|r| (r.name.as_str(), r.busy)).collect::<Vec<_>>(),
        [("Busy one", true), ("Idle one", false)]
    );
    // An engine inside a TUI cannot be restarted from here.
    h.state_mut().state.engine_detached = Some(false);
    h.run_steps(3);
    assert!(h.query_all_by_label("Only the app restarts; the agents keep running.").count() >= 1);
    // The app alone: relaunched at once (the exec is recorded in tests).
    h.get_by_label("Restart").click();
    h.run_steps(2);
    let (_, args) = h.state().state.life.relaunched.clone().expect("relaunched");
    assert_eq!(args.last().map(String::as_str), Some("/r/repo"));
    drop(tx);
}

#[test]
fn exits_of_a_previous_engine_are_dropped() {
    let (app, tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    let mut h = harness(app);
    h.run_steps(2);
    h.state_mut().state.exits.push(crate::attention::Exit {
        key: "/r/repo".into(),
        agent_id: "a1".into(),
        label: "A".into(),
        exit_code: None,
        at: 1.0,
    });
    let hello = |id: &str| lazyagent_proto::types::HelloResult {
        protocol: 1,
        engine_version: "0".into(),
        capabilities: vec![],
        repo_root: "/r/repo".into(),
        engine_id: id.into(),
        detached: Some(true),
        source_commit: Some("abc".into()),
    };
    tx.send(Msg::Connected { hello: hello("e1"), engine_changed: false, started: false }).unwrap();
    h.run_steps(2);
    assert_eq!(h.state().state.exits.len(), 1, "same engine: kept");
    assert_eq!(h.state().state.engine_commit.as_deref(), Some("abc"));
    tx.send(Msg::Connected { hello: hello("e2"), engine_changed: true, started: false }).unwrap();
    h.run_steps(2);
    assert!(h.state().state.exits.is_empty(), "the restore banner offers them instead");
}

/// Stops the world's detached engine (not one of its own children) at the end.
struct StopEngine<'a>(&'a lazyagent_testkit::World);

impl Drop for StopEngine<'_> {
    fn drop(&mut self) {
        let mut cmd = std::process::Command::new(&self.0.python);
        cmd.args(["-m", "lazyagent", "engine", "stop", &self.0.repo]).env_clear().envs(self.0.env.vars());
        let _ = cmd.output();
    }
}

#[test]
fn restarting_the_engine_restores_its_agents_before_the_app_comes_back() {
    let Some(world) = lazyagent_testkit::World::new() else { return };
    let _stop = StopEngine(&world);
    // A detached engine (what `lazyagent desktop` starts): the app starts it.
    let (tx, rx) = mpsc::channel();
    let conn = Conn::new(world.repo.clone(), world.env.clone(), tx, egui::Context::default());
    conn.spawn_supervisor();
    let state = State::new(conn.clone(), FontSet::bundled_only(), Some(ThemeKind::Dark));
    let mut h = harness(App::new(state, rx, None));
    step_until(&mut h, "connected and seeded", |a| a.state.seeded && a.state.link == Link::Connected);
    assert_eq!(h.state().state.engine_detached, Some(true));
    h.state_mut().state.spawn_agent(Some(world.repo.clone()));
    step_until(&mut h, "an agent", |a| a.state.store.agent_count() == 1);
    let old = lazyagent_proto::discovery::engine_info(&world.repo, &world.env).unwrap()["pid"].clone();

    h.get_by_label("File").click();
    h.run_steps(2);
    h.get_by_label("Restart…").click();
    h.run_steps(3);
    h.get_by_label("Restart the engine too").click();
    h.run_steps(3);
    // "Restart anyway" while the fake agent's turn runs.
    h.query_by_label("Restart").or_else(|| h.query_by_label("Restart anyway")).expect("the button").click();
    h.run_steps(1);
    assert!(conn.client().is_none(), "the app let go of the engine first");
    step_until(&mut h, "the relaunch", |a| a.state.life.relaunched.is_some());
    // By then the new engine runs the agent again.
    let info = lazyagent_proto::discovery::engine_info(&world.repo, &world.env).unwrap();
    assert_ne!(info["pid"], old);
    let (probe, hello, _rx) = world.connect();
    assert_eq!(hello.detached, Some(true));
    let sidebar = probe.request("get_sidebar_state", json!({}), lazyagent_testkit::TIMEOUT).unwrap();
    let agents: usize =
        sidebar["worktrees"].as_array().unwrap().iter().map(|w| w["agents"].as_array().unwrap().len()).sum();
    assert_eq!(agents, 1);
    // What the relaunched app will say.
    let report = lazyagent_proto::discovery::runtime_dir(&world.repo, &world.env)
        .join(format!("desktop-restart-{}.json", std::process::id()));
    let report: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(report).unwrap()).unwrap();
    assert_eq!(
        crate::lifecycle::report_text(&report).map(|(_, t)| t).as_deref(),
        Some("Engine restarted · 1 agent(s) restored")
    );
}

#[test]
fn update_and_restart_screenshots() {
    use crate::lifecycle::{Commit, Sheet, UpdateInfo};
    let (app, _tx) = app(vec![
        worktree("/r/repo", "develop", vec![agent("a1", "Docs", "waiting_for_user", "s1")]),
        worktree("/r/repo-feat", "feat-x", vec![agent("a2", "Implementer", "running", "s2")]),
    ]);
    let mut h =
        Harness::builder().with_size([1400.0, 880.0]).wgpu().build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(3);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    h.state_mut().state.life.info = Some(UpdateInfo {
        mode: "development".into(),
        version: "0.8.0".into(),
        root: Some("/home/u/src/lazyagent".into()),
        branch: Some("develop".into()),
        upstream: Some("origin/develop".into()),
        behind: 3,
        commits: vec![
            Commit { sha: "a1b2c3d".into(), subject: "feat(desktop): update and restart from the app".into() },
            Commit { sha: "9f8e7d6".into(), subject: "fix(engine): keep the agent id on restore".into() },
            Commit { sha: "5c4b3a2".into(), subject: "docs: plan for the actionables".into() },
        ],
        update_available: true,
        can_apply: true,
        ..Default::default()
    });
    // No engine here: the shell's error toast is noise in a picture.
    h.state_mut().state.toasts.clear();
    h.get_by_label("Help").click();
    h.run_steps(3);
    if !shot(&mut h, "menu-help") {
        return;
    }
    h.key_press(egui::Key::Escape);
    h.state_mut().state.life.sheet = Some(Sheet::Update);
    h.run_steps(4);
    shot(&mut h, "update");
    h.state_mut().state.life.sheet = Some(Sheet::Restart { engine: true, restore: true, after_update: true });
    h.run_steps(4);
    shot(&mut h, "restart");
}

#[test]
fn sidebar_terminals_and_spawn_screenshots() {
    let (app, _tx) = app(vec![
        worktree("/r/repo", "main", vec![agent("a2", "Agent 2", "running", "s2")]),
        worktree(
            "/r/repo-feat-login",
            "feat-login",
            vec![agent("a1", "Implementer", "running", "s1"), agent("a3", "Reviewer", "running", "s3")],
        ),
        worktree("/r/repo-fix-typo", "fix-typo", vec![]),
    ]);
    let mut h =
        Harness::builder().with_size([1400.0, 880.0]).wgpu().build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(3);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    let ctx = h.ctx.clone();
    let feed = |h: &mut Harness<'static, App>, session: &str, kind: PaneKind, text: &str| {
        let pane = h.state_mut().state.pane(session, kind, &ctx);
        pane.inner.lock().unwrap().term.feed(text.replace('\n', "\r\n").as_bytes());
    };
    feed(&mut h, "s2", PaneKind::Agent, "fake agent ready\nyour turn> \nline 1\nline 2\nline 3\ndone\nyour turn> ");
    feed(&mut h, "s3", PaneKind::Agent, "working\ngot: 'second agent' paste=False\nyour turn> ");
    for (path, shell) in [("/r/repo", "sh1"), ("/r/repo-feat-login", "sh2"), ("/r/repo-fix-typo", "sh3")] {
        h.state_mut().state.shells.insert(path.into(), shell.into());
    }
    feed(
        &mut h,
        "sh1",
        PaneKind::Shell,
        "\x1b[32m●\x1b[0m Read(app.py)  \x1b[31m●\x1b[0m Error  ⎿ done\ndev@laptop:~/code/repo$ ",
    );
    feed(&mut h, "sh2", PaneKind::Shell, &(1..=40).map(|i| format!("{i}\n")).collect::<String>());
    feed(&mut h, "sh3", PaneKind::Shell, "dev@laptop:~/code/repo-fix-typo$ ");
    h.state_mut().state.selected = Some("/r/repo-feat-login".into());
    h.state_mut().state.tabs.insert("/r/repo-feat-login".into(), "a3".into());
    h.run_steps(4);
    if !shot(&mut h, "d2-dark") {
        return;
    }
    h.state_mut().state.toggle_theme(&ctx);
    h.state_mut().state.selected = Some("/r/repo".into());
    h.run_steps(4);
    shot(&mut h, "d2-light");
    h.state_mut().state.toggle_theme(&ctx);
    h.state_mut().state.selected = Some("/r/repo-fix-typo".into());
    h.run_steps(3);
    h.get_by_label("Spawn an agent").click();
    h.run_steps(2);
    h.event(egui::Event::Text("implement the login form".into()));
    h.run_steps(3);
    shot(&mut h, "d3-spawn");
}
