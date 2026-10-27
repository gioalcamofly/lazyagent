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
    let mut app = App::new(state, rx, None);
    // Most tests are about the worktree view; Home's own start further down.
    app.state.view = crate::app::View::Worktrees;
    (app, tx)
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
    let (mut app, tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    app.state.shell_shown.insert("/r/repo".into());
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
    let mut state = State::new(conn.clone(), FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.view = crate::app::View::Worktrees;
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
    // No shell until asked for (Ctrl+L), then in the same worktree.
    assert!(!h.state().state.shells.contains_key(&world.repo));
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::L);
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
    let mut state = State::new(conn.clone(), FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.view = crate::app::View::Worktrees;
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
    step_until(&mut h, "the worktree's directory gone", |_| !std::path::Path::new(&path).exists());
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
    h.state_mut().state.dialog.as_mut().unwrap().skip = true;
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
fn home_without_an_engine_inbox_derives_its_lines_dismisses_and_parks() {
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
    // One line per worktree: f once (approval on top, +1), main in the Round.
    let now: Vec<String> =
        h.state().state.home.shown_in(crate::home::Zone::Now).iter().map(|l| l.name.clone()).collect();
    assert_eq!(now, ["f"]);
    assert_eq!(h.state().state.home.shown_in(crate::home::Zone::Now)[0].more, 1);
    assert!(h.state().title.starts_with("(1 now · 1) lazyagent"), "{}", h.state().title);
    // g h (outside a terminal: Ctrl+K first, then the keys).
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(2);
    h.key_press(egui::Key::G);
    h.key_press(egui::Key::H);
    h.run_steps(2);
    assert_eq!(h.state().state.view, View::Home);
    h.get_by_label("Approve");
    // x dismisses the selected line's top item: the question shows next.
    h.key_press(egui::Key::X);
    h.run_steps(2);
    assert_eq!(h.state().state.attention.len(), 2);
    assert_eq!(h.state().state.saved.marks.dismissed.len(), 1);
    h.get_by_label("Answer");
    // z: park it for an hour (a snooze, here).
    h.key_press(egui::Key::Z);
    h.run_steps(2);
    h.get_by_label("1  1 hour").click();
    h.run_steps(2);
    assert_eq!(h.state().state.saved.marks.snoozed.len(), 1);
    assert!(h.state().state.home.shown_in(crate::home::Zone::Now).is_empty());
    // The gate stops asking, then asks again: a new occurrence shows.
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
    assert_eq!(h.state().state.home.shown_in(crate::home::Zone::Now).len(), 1, "dismissing hid that occurrence only");
    // Pass to orchestrator needs the engine's claims: disabled, with why.
    h.key_press(egui::Key::K);
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    // g w: back to the worktrees.
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
    // On Home, the CI line offers to send the failures to the agent.
    h.state_mut().state.view = crate::app::View::Home;
    h.state_mut().state.home.selected = Some("/r/repo".into());
    h.run_steps(2);
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    h.get_by_label("Delegate to its agent  (d)").click();
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
    assert!(!state.home.done_open, "In flight's done group starts folded");
    // Unfolded, per repo: this repo's state file only.
    let mut saved = state.saved.clone();
    saved.home_done_open = true;
    saver.touch();
    saver.flush(&saved);
    let (tx, _rx) = mpsc::channel();
    let conn = Conn::new("/r/repo".into(), dir_env(&dir), tx, egui::Context::default());
    assert!(State::new(conn, FontSet::bundled_only(), None).home.done_open);
    let (tx, _rx) = mpsc::channel();
    let conn = Conn::new("/r/other".into(), dir_env(&dir), tx, egui::Context::default());
    assert!(!State::new(conn, FontSet::bundled_only(), None).home.done_open, "another repo keeps its own");
}

fn dir_env(dir: &tempfile::TempDir) -> Env {
    Env::process().with("XDG_STATE_HOME", Some(dir.path().to_str().unwrap()))
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
fn render_home_without_an_engine_inbox_and_the_pr_panel() {
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
    h.state_mut().state.view = crate::app::View::Home;
    h.run_steps(3);
    let out = std::env::temp_dir();
    match h.render() {
        Ok(image) => image.save(out.join("lazyagent-desktop-home-fallback.png")).unwrap(),
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
    // The engine writes the meta after the text, and the hook reads its rev.
    let meta_rev = || {
        std::fs::read_to_string(path.with_extension("json"))
            .ok()
            .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
            .and_then(|m| m["rev"].as_i64())
            .unwrap_or(0)
    };
    while meta_rev() < 1 && Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    assert!(meta_rev() >= 1, "the engine wrote the notes' meta");
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
    // The engine deletes the notes as the stop finishes, not before the
    // agent leaves the store: wait for it.
    step_until(&mut h, "copied, then deleted", |_| !path.exists());
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
    let mut state = State::new(conn.clone(), FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.view = crate::app::View::Worktrees;
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
        h.state_mut().state.shell_shown.insert(path.into());
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

// --- Home (proposal 0013, I3) ----------------------------------------------

fn status_row(key: &str, zone: &str, level: Option<&str>, ball: &str, state: &str, ago: f64) -> serde_json::Value {
    json!({"key": format!("repo:{key}"), "repo": "repo", "branch": key, "worktree_path": format!("/r/repo-{key}"),
           "title": key, "urls": [], "zone": zone, "level": level, "ball": ball, "priority": "medium",
           "state": state, "rule": if zone == "inbox" { 6 } else { 9 }, "blocks_agent": level == Some("now"),
           "needs_you": null, "agents": [], "pr": null, "note": null, "gesture": null, "claim": null,
           "stale": false, "changed_at": crate::app::unix_now() - ago, "fingerprint": "f"})
}

fn engine_home(caps: &[&str]) -> (App, mpsc::Sender<Msg>) {
    let (mut app, tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    app.state.view = crate::app::View::Home;
    app.state.capabilities = Some(caps.iter().map(|c| c.to_string()).collect());
    // As if status_list and actionables_list had answered (empty).
    app.state.inbox_loaded = 3;
    (app, tx)
}

fn home_names(h: &Harness<'static, App>, zone: crate::home::Zone) -> Vec<String> {
    h.state().state.home.shown_in(zone).iter().map(|l| l.name.clone()).collect()
}

#[test]
fn the_app_opens_on_home_not_a_terminal() {
    let (tx, rx) = mpsc::channel();
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/r/repo".into(), env, tx.clone(), egui::Context::default());
    let state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": [worktree("/r/repo-feat", "feat", vec![agent("a1", "Implementer", "running", "s1")])],
        "orchestrator": {"agents": []}, "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    let mut h = harness(App::new(state, rx, None));
    h.run_steps(4);
    assert_eq!(h.state().state.view, crate::app::View::Home);
    assert!(h.state().state.panes.is_empty(), "no terminal, and no shell, opened on start");
    h.get_by_label("In flight");
    assert_eq!(h.state().state.home.shown_in(crate::home::Zone::Flight).len(), 1);
    assert!(h.query_all_by_label("feat").count() >= 2, "the sidebar's branch and Home's line");
    // Noise cut: no "engine connected" pill, no version, no "0s".
    assert!(h.query_by_label("engine ● connected").is_none());
    assert!(h.query_all_by_label("<1m").count() >= 1);
    // o opens the line's agent in the worktree view.
    h.key_press(egui::Key::O);
    h.run_steps(2);
    let s = &h.state().state;
    assert_eq!((s.view, s.selected.as_deref()), (crate::app::View::Worktrees, Some("/r/repo-feat")));
}

#[test]
fn home_shows_the_engines_rows_once_and_never_reorders_under_the_cursor() {
    use crate::home::Zone;
    let (app, tx) = engine_home(&["status", "actionables"]);
    let mut h = harness(app);
    h.run_steps(2);
    for row in [
        status_row("cart", "in_flight", None, "agent", "Edit", 600.0),
        status_row("search", "in_flight", None, "others", "review requested", 300.0),
    ] {
        tx.send(event("status.updated", json!({"row": row}))).unwrap();
    }
    h.run_steps(2);
    assert_eq!(home_names(&h, Zone::Flight), ["cart", "search"]);
    h.key_press(egui::Key::J);
    h.key_press(egui::Key::J);
    h.run_steps(1);
    assert_eq!(h.state().state.home.selected.as_deref(), Some("repo:search"));
    // A new row that sorts first, an update that would move one: nothing
    // moves under the cursor; the new one comes last.
    tx.send(event("status.updated", json!({"row": status_row("wishlist", "in_flight", None, "agent", "Bash", 1.0)})))
        .unwrap();
    tx.send(event("status.updated", json!({"row": status_row("search", "in_flight", None, "agent", "Read", 2.0)})))
        .unwrap();
    h.run_steps(2);
    assert_eq!(home_names(&h, Zone::Flight), ["cart", "search", "wishlist"]);
    assert_eq!(h.state().state.home.selected.as_deref(), Some("repo:search"));
    // A Round item waits for the next round; a Now one shows at once.
    tx.send(event(
        "status.updated",
        json!({"row": status_row("cart", "inbox", Some("round"), "you", "review: agent finished", 0.0)}),
    ))
    .unwrap();
    let mut now = status_row("wishlist", "inbox", Some("now"), "you", "approve: npm test", 0.0);
    now["needs_you"] = json!({"count": 2, "top": "Approval:/r/repo-wishlist/a1"});
    let item = json!({"id": "Approval:/r/repo-wishlist/a1", "occurrence": "o@1", "kind": "approval", "level": "now",
                      "title": "Needs approval", "label": "Agent a1", "detail": "npm test", "state": "approve: npm test",
                      "work_item": "repo:wishlist", "worktree_path": "/r/repo-wishlist", "agent_id": "a1", "pr": null,
                      "job_id": null, "exit_code": null, "blocks_agent": true, "priority": "medium",
                      "since": crate::app::unix_now(), "park": null});
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": now}))).unwrap();
    h.run_steps(2);
    assert_eq!(home_names(&h, Zone::Now), ["wishlist"]);
    assert!(home_names(&h, Zone::Round).is_empty());
    assert_eq!(home_names(&h, Zone::Flight), ["search"], "each work item once, never in In flight");
    let next: Vec<String> = h.state().state.home.next_round().iter().map(|l| l.name.clone()).collect();
    assert_eq!(next, ["cart"], "dimmed under the Round");
    h.get_by_label("Round (+1 next)");
    h.get_by_label("next round");
    h.get_by_label("deliver now ▸");
    h.get_by_label("2 items");
    assert!(h.state().title.starts_with("(1 now · 1) "), "{}", h.state().title);
    // Back from a terminal is a cut point: the Round is delivered.
    h.key_press(egui::Key::G);
    h.key_press(egui::Key::W);
    h.run_steps(1);
    h.key_press(egui::Key::G);
    h.key_press(egui::Key::H);
    h.run_steps(2);
    assert_eq!(home_names(&h, Zone::Round), ["cart"]);
    assert!(h.query_by_label("deliver now ▸").is_none());
    h.get_by_label("Round");
    // It resolves on its own: out of the Round, counted.
    tx.send(event("status.updated", json!({"row": status_row("cart", "in_flight", None, "agent", "Edit", 0.0)})))
        .unwrap();
    h.run_steps(2);
    assert!(home_names(&h, Zone::Round).is_empty());
    h.get_by_label("1 resolved on their own");
    tx.send(event("status.removed", json!({"key": "repo:search"}))).unwrap();
    h.run_steps(2);
    assert_eq!(home_names(&h, Zone::Flight), ["cart"]);
}

#[test]
fn pass_to_orchestrator_is_offered_only_when_it_can_work() {
    let (app, tx) = engine_home(&["status", "actionables"]);
    let mut h = harness(app);
    h.run_steps(2);
    tx.send(event("status.updated", json!({"row": status_row("cart", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(2);
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    h.get_by_label("This engine is too old to pass items to the orchestrator: restart it (File → Restart…)");
    h.state_mut().state.capabilities.as_mut().unwrap().push("claims".into());
    h.run_steps(2);
    h.get_by_label("No orchestrator is running: start one (the ◆ Orchestrator row), then pass the item");
    tx.send(event(
        "agent.spawned",
        json!({"worktree_path": "__orchestrator__", "agent_id": "", "session_id": "so", "label": "Orchestrator",
               "session_name": "", "kind": "orchestrator"}),
    ))
    .unwrap();
    h.run_steps(2);
    assert!(
        h.query_by_label("No orchestrator is running: start one (the ◆ Orchestrator row), then pass the item")
            .is_none()
    );
    // No engine behind this one: the gesture fails, and says so.
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::O);
    h.run_steps(2);
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("Could not pass it to the orchestrator")));
    // A claimed row shows it, with the time left and Take back.
    let mut row = status_row("cart", "in_flight", None, "agent", "with orchestrator", 0.0);
    row["claim"] = json!({"key": "repo:cart", "holder": "orchestrator", "at": crate::app::unix_now(),
                          "until": crate::app::unix_now() + 7200.0, "by": "user", "allowed": ["relay"]});
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(2);
    h.get_by_label("Take back  (u)");
    h.get_by_label("orchestrator");
}

#[test]
fn home_against_a_real_engine_passes_an_item_to_the_orchestrator_and_takes_it_back() {
    let Some((world, mut h, conn)) = world_app() else { return };
    h.state_mut().state.spawn_agent(None);
    h.state_mut().state.spawn_agent(Some(world.repo.clone()));
    step_until(&mut h, "the orchestrator and an agent", |a| {
        !a.state.store.orchestrator.is_empty() && !a.state.store.agents(&world.repo).is_empty()
    });
    h.state_mut().state.go_home();
    step_until(&mut h, "the work item on Home", |a| a.state.home.lines.contains_key("repo:main"));
    h.state_mut().state.home.selected = Some("repo:main".into());
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::O);
    step_until(&mut h, "the claim on the row", |a| {
        a.state.status_rows.get("repo:main").is_some_and(|r| r.claim.is_some())
    });
    let (probe, _hello, _rx) = world.connect();
    let rows = probe.request("status_list", json!({}), lazyagent_testkit::TIMEOUT).unwrap();
    let row = rows["rows"].as_array().unwrap().iter().find(|r| r["key"] == "repo:main").unwrap().clone();
    assert_eq!(
        (row["state"].as_str(), row["claim"]["holder"].as_str()),
        (Some("orchestrator · looking"), Some("orchestrator"))
    );
    assert!(row["claim"]["until"].as_f64().unwrap() > row["claim"]["at"].as_f64().unwrap() + 7000.0);
    h.key_press(egui::Key::U);
    step_until(&mut h, "taken back", |a| a.state.status_rows.get("repo:main").is_some_and(|r| r.claim.is_none()));
    conn.shutdown();
}

/// Home in dark and light, from neutral demo data (a "shop" repo), for
/// docs/design/desktop-screenshots/i3-home-*.png.
#[test]
fn home_screenshots() {
    let now = crate::app::unix_now();
    let w = |b: &str| format!("/home/dev/shop-{b}");
    let (tx, rx) = mpsc::channel();
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/home/dev/shop".into(), env, tx.clone(), egui::Context::default());
    let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.capabilities = Some(vec!["status".into(), "actionables".into(), "claims".into()]);
    state.look_from = now - 1800.0;
    let branches = [
        ("cart-discount", "running"),
        ("checkout-copy", "completed"),
        ("product-images", ""),
        ("invoice-pdf", "waiting"),
        ("search-filters", "running"),
        ("payments-retry", "completed"),
        ("order-export", "waiting"),
        ("stock-sync", ""),
        ("shipping-rates", ""),
    ];
    let trees: Vec<_> = std::iter::once(worktree("/home/dev/shop", "main", vec![]))
        .chain(branches.iter().enumerate().map(|(i, (b, st))| {
            let agents = if st.is_empty() {
                vec![]
            } else {
                vec![agent(&format!("a{}", i + 1), &format!("Agent a{}", i + 1), st, &format!("s{i}"))]
            };
            worktree(&w(b), b, agents)
        }))
        .collect();
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": trees,
        "orchestrator": {"agents": [agent("", "Orchestrator", "waiting", "so")]},
        "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    let row = |b: &str, zone: &str, level: Option<&str>, ball: &str, state: &str, ago: f64, pr: Option<u64>| {
        let mut r = status_row(b, zone, level, ball, state, ago);
        r["key"] = json!(format!("shop:{b}"));
        r["repo"] = json!("shop");
        r["worktree_path"] = json!(w(b));
        if let Some(n) = pr {
            r["pr"] = json!({"number": n, "state": "OPEN", "review": "REVIEW_REQUIRED", "ci": "green", "mergeable": "MERGEABLE"});
            r["urls"] = json!([format!("https://example.com/shop/pull/{n}")]);
        }
        r
    };
    let item = |b: &str, kind: &str, level: &str, title: &str, detail: &str, state: &str, ago: f64| {
        json!({"id": format!("{kind}:{}", w(b)), "occurrence": format!("{kind}:{}@1", w(b)), "kind": kind,
               "level": level, "title": title, "label": "Agent", "detail": detail, "state": state,
               "work_item": format!("shop:{b}"), "worktree_path": w(b), "agent_id": "a1", "pr": null, "job_id": null,
               "exit_code": null, "blocks_agent": level == "now", "priority": "medium", "since": now - ago, "park": null})
    };
    let mut cart = row("cart-discount", "inbox", Some("now"), "you", "approve: npm test", 120.0, None);
    cart["needs_you"] = json!({"count": 1, "top": format!("approval:{}", w("cart-discount"))});
    cart["agents"] = json!([{"id": "a1", "status": "waiting_for_approval", "since": now - 120.0}]);
    let mut copy = row("checkout-copy", "inbox", Some("round"), "you", "review: agent finished", 900.0, None);
    copy["needs_you"] = json!({"count": 1, "top": format!("done:{}", w("checkout-copy"))});
    let mut images =
        row("product-images", "inbox", Some("round"), "you", "merge: approved, CI green", 3600.0, Some(14));
    images["rule"] = json!(4);
    images["pr"]["review"] = json!("APPROVED");
    let mut invoice = row("invoice-pdf", "inbox", Some("round"), "you", "CI red: tests", 1800.0, Some(15));
    invoice["needs_you"] = json!({"count": 2, "top": format!("ci:{}", w("invoice-pdf"))});
    invoice["pr"]["ci"] = json!("red");
    let mut payments =
        row("payments-retry", "in_flight", None, "others", "review requested, CI green", 600.0, Some(12));
    payments["agents"] = json!([{"id": "a6", "status": "completed", "since": now - 2400.0}]);
    payments["note"] = json!({"text": "retry holds 30/30 runs; waiting on review", "by": "notes", "at": now - 2400.0, "agent_id": "a6"});
    let mut export = row("order-export", "in_flight", None, "agent", "with orchestrator", 300.0, None);
    export["claim"] = json!({"key": "shop:order-export", "holder": "orchestrator", "at": now - 300.0,
                             "until": now + 6900.0, "by": "user", "allowed": ["relay", "restore_owner", "ci_rerun"]});
    let mut shipping = row("shipping-rates", "in_flight", None, "others", "in QA", 86400.0 * 2.0, Some(11));
    shipping["tracker"] = json!({"adapter": "github_issues", "key": "41", "url": "https://example.com/shop/issues/41",
                                 "status": "qa", "state": "in QA", "at": now - 300.0, "stale": false});
    let events = vec![
        event(
            "actionable.created",
            json!({"item": item("cart-discount", "approval", "now", "Needs approval", "npm test", "approve: npm test", 120.0)}),
        ),
        event(
            "actionable.created",
            json!({"item": item("checkout-copy", "done", "round", "Your turn", "completed", "review: agent finished", 900.0)}),
        ),
        event(
            "actionable.created",
            json!({"item": item("invoice-pdf", "ci", "round", "CI failing", "tests", "CI red: tests", 1800.0)}),
        ),
        event("status.updated", json!({"row": cart})),
        event("status.updated", json!({"row": copy})),
        event("status.updated", json!({"row": images})),
        event("status.updated", json!({"row": invoice})),
        event(
            "status.updated",
            json!({"row": row("search-filters", "in_flight", None, "agent", "Edit src/search/filters.py", 240.0, None)}),
        ),
        event("status.updated", json!({"row": payments})),
        event("status.updated", json!({"row": export})),
        event(
            "status.updated",
            json!({"row": row("stock-sync", "in_flight", None, "stalled", "no agent", 7200.0, Some(16))}),
        ),
        event("status.updated", json!({"row": shipping})),
    ];
    let mut h = Harness::builder()
        .with_size([1400.0, 880.0])
        .wgpu()
        .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
    h.run_steps(2);
    for e in events {
        tx.send(e).unwrap();
    }
    h.run_steps(3);
    // status_list and actionables_list answered: the first Round is delivered.
    h.state_mut().state.inbox_loaded = 3;
    h.state_mut().state.inbox_gen += 1;
    h.state_mut().state.status_counts.not_started = 3;
    h.run_steps(2);
    // One more for the next round, after this one was delivered.
    let mut wish = row("wishlist", "inbox", Some("round"), "you", "agent may hang", 60.0, None);
    wish["worktree_path"] = json!(null);
    tx.send(event("status.updated", json!({"row": wish}))).unwrap();
    h.state_mut().state.home.selected = Some("shop:payments-retry".into());
    h.state_mut().state.home.expanded = Some("shop:payments-retry".into());
    h.state_mut().state.toasts.clear();
    h.run_steps(4);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    assert_eq!(h.state().state.home.counts(), (1, 4));
    if !shot(&mut h, "i3-home-dark") {
        return;
    }
    let ctx = h.ctx.clone();
    h.state_mut().state.toggle_theme(&ctx);
    h.run_steps(4);
    shot(&mut h, "i3-home-light");
}

#[test]
fn tracker_rows_and_items_show_on_home() {
    use crate::home::Zone;
    let (app, tx) = engine_home(&["status", "actionables"]);
    let mut h = harness(app);
    h.run_steps(2);
    let mut r = status_row("fix-41", "inbox", Some("round"), "you", "returned by QA", 60.0);
    r["needs_you"] = json!({"count": 1, "top": "Tracker:github_issues:41"});
    r["tracker"] = json!({"adapter": "github_issues", "key": "41", "url": "https://example.com/issues/41",
                          "status": "qa-returned", "state": "returned by QA", "at": crate::app::unix_now() - 600.0,
                          "stale": true});
    let item = json!({"id": "Tracker:github_issues:41", "occurrence": "t@1", "kind": "tracker_status", "level": "round",
                      "title": "Tracker", "label": "Fix the cart total", "detail": "https://example.com/issues/41",
                      "state": "returned by QA", "work_item": "repo:fix-41", "worktree_path": "/r/repo-fix-41",
                      "agent_id": null, "pr": null, "job_id": null, "exit_code": null, "blocks_agent": false,
                      "priority": "medium", "since": crate::app::unix_now() - 60.0, "park": null});
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": r}))).unwrap();
    h.state_mut().state.status_counts.not_started = 2;
    h.run_steps(2);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    let line = h.state().state.home.shown_in(Zone::Round)[0].clone();
    assert_eq!((line.verb(), line.url.as_deref()), ("Update task", Some("https://example.com/issues/41")));
    h.get_by_label("In flight");
    h.get_by_label("· 2 not started");
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    h.get_by_label("github issues 41: returned by QA (10m ago)");
}

// --- Home, I4c: gestures, To sit down with, briefs, since you looked, and
// the orchestrator as first responder (against a fake engine) ---------------

type Calls = std::sync::Arc<std::sync::Mutex<Vec<(String, serde_json::Value)>>>;

/// A fake engine: `answer` gives each request's result (`None`: `{}`); every
/// request and notification is recorded.
fn fake_engine(
    h: &Harness<'static, App>,
    mut answer: impl FnMut(&str, &serde_json::Value) -> Option<serde_json::Value> + Send + 'static,
) -> Calls {
    let calls: Calls = Default::default();
    let seen = calls.clone();
    *h.state().state.conn.fake.lock().unwrap() = Some(Box::new(move |method, params| {
        seen.lock().unwrap().push((method.to_string(), params.clone()));
        Some(Ok(answer(method, params).unwrap_or_else(|| json!({}))))
    }));
    calls
}

fn calls_of(calls: &Calls, method: &str) -> Vec<serde_json::Value> {
    calls.lock().unwrap().iter().filter(|(m, _)| m == method).map(|(_, p)| p.clone()).collect()
}

const I4: &[&str] = &["status", "actionables", "claims", "gestures", "decisions"];

fn brief(id: &str, key: &str, valid: bool) -> serde_json::Value {
    json!({"id": id, "key": format!("repo:{key}"), "worktree_path": format!("/r/repo-{key}"), "agent": "a2",
           "holder": "user", "replaces": null, "state": "open", "valid": valid,
           "class": if valid { "deep" } else { "quick" }, "blocking": false,
           "brief": {"question": "Retries: fixed or exponential backoff?",
                     "options": [{"key": "A", "label": "Exponential, cap 30 s", "consequence": "slower failure on outages"},
                                 {"key": "B", "label": "Fixed 2 s, 5 tries", "consequence": "3/30 runs time out"}],
                     "recommendation": "A", "tried": "fixed 2 s: 3 of 30 runs hit the 10 s limit",
                     "evidence": [{"kind": "diff", "path": "src/retry.py", "hunk": "@@ -40,7 +40,12 @@"},
                                  {"kind": "log", "ref": "job:tests", "lines": "120-134"}],
                     "reversible": "yes", "impact_if_wrong": "slower failures under load"},
           "invalid": if valid { json!([]) } else { json!(["no evidence"]) }, "problems": [], "sha": "abc123",
           "created_at": crate::app::unix_now() - 600.0, "closed_at": null, "answer": null})
}

fn brief_item(id: &str, key: &str, level: &str, valid: bool) -> serde_json::Value {
    let (label, detail) = if valid {
        ("Retries: fixed or exponential backoff?".to_string(), "brief by agent a2 · 2 options · yes")
    } else {
        ("agent a2 asks: Retries: fixed or exponential backoff?".to_string(), "open terminal")
    };
    json!({"id": format!("Decision:{id}"), "occurrence": format!("{id}@1"), "kind": "decision", "level": level,
           "title": "Decide", "label": label, "detail": detail, "state": "decide: retries: fixed or exponential?",
           "work_item": format!("repo:{key}"), "worktree_path": format!("/r/repo-{key}"), "agent_id": "a2",
           "pr": null, "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - 600.0, "park": null, "class": if valid { "deep" } else { "quick" },
           "decision_id": id})
}

fn inbox_row(key: &str, level: &str, item: &str, state: &str) -> serde_json::Value {
    let mut r = status_row(key, "inbox", Some(level), "you", state, 600.0);
    r["needs_you"] = json!({"count": 1, "top": item});
    r["class"] = json!(if level == "deep" { "deep" } else { "quick" });
    r
}

#[test]
fn to_sit_down_with_shows_deep_items_at_once_with_deep_max() {
    use crate::home::Zone;
    let (app, tx) = engine_home(I4);
    let mut h = harness(app);
    h.state_mut().state.status_counts.deep_max = 2;
    h.run_steps(2);
    h.get_by_label("To sit down with");
    h.get_by_label("· of 2, for a focus block");
    h.get_by_label("Nothing to sit down with.");
    for (id, key) in [("d1", "billing"), ("d2", "export"), ("d3", "search")] {
        tx.send(event("decision.created", json!({"decision": brief(id, key, true)}))).unwrap();
        tx.send(event("actionable.created", json!({"item": brief_item(id, key, "deep", true)}))).unwrap();
        tx.send(event("status.updated", json!({"row": inbox_row(key, "deep", &format!("Decision:{id}"), "decide")})))
            .unwrap();
    }
    h.run_steps(3);
    // Deep is outside the rounds: no cut point needed, nothing waits.
    assert_eq!(home_names(&h, Zone::Deep), ["billing", "export", "search"]);
    assert!(h.query_by_label("deliver now ▸").is_none());
    // Shown, never enforced: three of two is a line, not a refusal.
    h.get_by_label("3");
    h.get_by_label("· over your limit of 2");
    assert_eq!(h.query_all_by_label("Decide").count(), 3);
    assert_eq!(h.state().state.home.counts(), (0, 0), "the badges count Now and Round only");
    // Without the engine's gestures (and no deep lines) the zone is not there.
    let (app, _tx) = engine_home(&["status", "actionables"]);
    let mut h = harness(app);
    h.run_steps(2);
    assert!(h.query_by_label("Nothing to sit down with.").is_none());
}

#[test]
fn briefs_show_compactly_read_only_and_invalid_ones_ask_in_the_terminal() {
    use crate::home::Zone;
    let (app, tx) = engine_home(I4);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "decision_get" => Some(json!({"decision": brief(p["id"].as_str().unwrap(), "billing", true)})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    // The brief is not known yet: Enter fetches it with the timeline.
    tx.send(event("actionable.created", json!({"item": brief_item("d1", "billing", "deep", true)}))).unwrap();
    tx.send(event("status.updated", json!({"row": inbox_row("billing", "deep", "Decision:d1", "decide")}))).unwrap();
    h.run_steps(2);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_get"), [json!({"id": "d1"})]);
    h.get_by_label("brief by agent a2 · 2 options · reversible: yes · recommends A");
    h.get_by_label("Retries: fixed or exponential backoff?");
    h.get_by_label("A  Exponential, cap 30 s — slower failure on outages   (recommended)");
    h.get_by_label("B  Fixed 2 s, 5 tries — 3/30 runs time out");
    h.get_by_label("Reversible: yes · If wrong: slower failures under load");
    h.get_by_label("Evidence: diff src/retry.py · log job:tests:120-134");
    assert!(h.query_by_label("Dismiss  (x)").is_some());
    // An invalid brief is a quick "agent a2 asks: …": Enter opens its terminal.
    tx.send(event("decision.created", json!({"decision": brief("d2", "export", false)}))).unwrap();
    tx.send(event("actionable.created", json!({"item": brief_item("d2", "export", "now", false)}))).unwrap();
    tx.send(event("status.updated", json!({"row": inbox_row("export", "now", "Decision:d2", "asks: retries")})))
        .unwrap();
    h.run_steps(2);
    let line = h.state().state.home.shown_in(Zone::Now)[0].clone();
    assert_eq!((line.name.as_str(), line.verb()), ("export", "Answer"));
    h.state_mut().state.home.selected = Some("repo:export".into());
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    let s = &h.state().state;
    assert_eq!((s.view, s.selected.as_deref()), (crate::app::View::Worktrees, Some("/r/repo-export")));
}

#[test]
fn row_gestures_label_park_close_and_clear_through_the_engine() {
    use crate::home::Zone;
    let (app, tx) = engine_home(I4);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_gesture" => {
            let mut row = status_row("cart", "in_flight", None, "others", "CI running", 60.0);
            if p["kind"] == "label" {
                row["gesture"] = json!({"kind": "label", "at": crate::app::unix_now(), "by": "user",
                                        "ball": p["ball"], "text": p["text"]});
                row["rule"] = json!(8);
            }
            Some(json!({"key": p["key"], "claim": null, "gesture": row["gesture"], "row": row}))
        }
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    tx.send(event(
        "status.updated",
        json!({"row": status_row("cart", "in_flight", None, "agent", "CI running", 60.0)}),
    ))
    .unwrap();
    h.run_steps(2);
    // l: the label form; a ball and a short text, Enter saves.
    h.key_press(egui::Key::L);
    h.run_steps(2);
    h.get_by_label("Ball with");
    h.event(egui::Event::Text("QA".into()));
    h.run_steps(1);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:cart", "kind": "label", "ball": "others", "text": "QA"})]
    );
    let line = h.state().state.home.shown_in(Zone::Flight)[0].clone();
    assert_eq!((line.ball.as_str(), line.state.as_str()), ("others", "QA · CI running"));
    h.get_by_label("QA · CI running");
    h.get_by_label_contains("labelled \"QA\" (others), by you");
    // z on a row with no inbox item parks the work item.
    h.key_press(egui::Key::Z);
    h.run_steps(2);
    h.get_by_label("Park the whole work item until");
    h.get_by_label("4  CI ends").click();
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "status_gesture")[1], json!({"key": "repo:cart", "kind": "park", "until": "ci_done"}));
    // Shift+X: close, as done or dropped.
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::X);
    h.run_steps(2);
    h.get_by_label(
        "Close this work item: it leaves Home after a day. The worktree, the branch and the PR are not touched.",
    );
    h.get_by_label("Close as dropped").click();
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "status_gesture")[2], json!({"key": "repo:cart", "kind": "close", "how": "dropped"}));
    // u clears the user's gesture (the row still says it has one).
    let mut labelled = status_row("cart", "in_flight", None, "others", "CI running", 60.0);
    labelled["gesture"] =
        json!({"kind": "label", "at": crate::app::unix_now(), "by": "user", "ball": "others", "text": "QA"});
    tx.send(event("status.updated", json!({"row": labelled}))).unwrap();
    h.run_steps(2);
    h.key_press(egui::Key::U);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "status_gesture")[3], json!({"key": "repo:cart", "kind": "clear"}));
    // A closed row is a dimmed tombstone for a day, then gone.
    let mut done = status_row("cart", "in_flight", None, "done", "dropped by you", 60.0);
    done["rule"] = json!(1);
    tx.send(event("status.updated", json!({"row": done}))).unwrap();
    let mut old = status_row("export", "in_flight", None, "done", "merged", 90000.0);
    old["rule"] = json!(1);
    tx.send(event("status.updated", json!({"row": old}))).unwrap();
    h.run_steps(2);
    assert!(home_names(&h, Zone::Flight).is_empty());
    let done: Vec<_> = h.state().state.home.done_group().iter().map(|l| l.name.clone()).collect();
    assert_eq!(done, ["cart"]);
    h.get_by_label("1 done today ▸");
    // No gestures on a done row.
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::F);
    h.run_steps(2);
    h.state_mut().state.home.selected = Some("repo:cart".into());
    h.key_press(egui::Key::L);
    h.run_steps(1);
    assert!(h.state().state.home.labeling.is_none());
}

#[test]
fn since_you_looked_fetches_the_timeline_and_marks_the_item_seen() {
    let (app, tx) = engine_home(I4);
    let mut h = harness(app);
    let now = crate::app::unix_now();
    let calls = fake_engine(&h, move |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": now - 3600.0, "seen_at": now - 3600.0, "entries": [
            {"at": now - 1800.0, "kind": "commit", "actor": "engine", "text": "try fixed backoff", "ref": "abc123"},
            {"at": now - 1200.0, "kind": "ci", "actor": "engine", "text": "CI red"}]})),
        _ => None,
    });
    h.run_steps(2);
    let mut row = status_row("billing", "in_flight", None, "agent", "Edit", 60.0);
    (row["seen_at"], row["last_event_at"]) = (json!(now - 3600.0), json!(now - 1200.0));
    let mut quiet = status_row("export", "in_flight", None, "agent", "Read", 30.0);
    (quiet["seen_at"], quiet["last_event_at"]) = (json!(now - 60.0), json!(now - 600.0));
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    tx.send(event("status.updated", json!({"row": quiet}))).unwrap();
    h.run_steps(2);
    let unseen = |h: &Harness<'static, App>| -> Vec<String> {
        let s = &h.state().state;
        s.home.lines.values().filter(|l| l.unseen(s.look_from)).map(|l| l.name.clone()).collect()
    };
    assert_eq!(unseen(&h), ["billing"], "the engine's per-item marks, not the client's last look");
    h.state_mut().state.home.selected = Some("repo:billing".into());
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "status_timeline"), [json!({"key": "repo:billing"})]);
    let seen = calls_of(&calls, "status_seen");
    assert_eq!((seen.len(), &seen[0]["key"]), (1, &json!("repo:billing")));
    h.get_by_label("Since you looked");
    h.get_by_label("30m ago  commit  try fixed backoff");
    h.get_by_label("20m ago  ci  CI red");
    assert!(unseen(&h).is_empty());
    // A new fact while it is open joins the list.
    tx.send(event(
        "status.timeline",
        json!({"key": "repo:billing", "entry": {"at": now, "kind": "decision", "actor": "agent:a2", "text": "asked: retries?", "ref": "d1"}}),
    ))
    .unwrap();
    h.run_steps(2);
    h.get_by_label("<1m ago  decision  asked: retries? · agent:a2");
    // Shift+M: all seen, in the engine too (no key).
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::M);
    h.run_steps(1);
    let all = calls_of(&calls, "status_seen").pop().unwrap();
    assert!(all.get("key").is_none() && all["at"].as_f64().is_some(), "{all}");
}

#[test]
fn first_responder_is_hidden_without_its_feature() {
    let (app, tx) = engine_home(I4);
    let mut h = harness(app);
    let calls = fake_engine(&h, |_, _| None);
    h.run_steps(2);
    let mut row = status_row("billing", "in_flight", None, "agent", "Edit", 60.0);
    row["holder"] = json!("orchestrator");
    let mut d = brief("d1", "billing", true);
    (d["answered_by"], d["revertible"], d["state"]) = (json!("orchestrator"), json!(true), json!("answered"));
    d["answer"] = json!({"choice": "B", "reason": "fixed is enough here"});
    d["closed_at"] = json!(crate::app::unix_now() - 60.0);
    tx.send(event("decision.created", json!({"decision": d}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.state_mut().state.status_counts.auto_actions = Some(3);
    h.state_mut().state.presence =
        Some(lazyagent_proto::types::Presence { mode: Some("auto".into()), effective: "away".into(), since: None });
    h.run_steps(2);
    h.key_press(egui::Key::Enter);
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::U);
    h.run_steps(2);
    for hidden in ["Orchestrator", "Presence: auto (away)", "looking", "Auto-delegation ▾", "Revert…  (⇧U)"] {
        assert!(h.query_by_label(hidden).is_none(), "{hidden}");
    }
    assert!(h.query_by_label_contains("decided by orchestrator").is_none());
    assert!(h.query_by_label_contains("Today:").is_none());
    assert!(h.state().state.home.reverting.is_none());
    assert!(!h.state_mut().state.user_input(crate::app::unix_now()));
    let called: Vec<String> = calls.lock().unwrap().iter().map(|(m, _)| m.clone()).collect();
    for m in ["presence_report", "presence_set", "decision_revert", "inbox_config_get"] {
        assert!(!called.iter().any(|c| c == m), "{m} called: {called:?}");
    }
}

#[test]
fn presence_is_reported_at_most_once_a_minute() {
    let (app, _tx) = engine_home(&["status", "actionables", "gestures", "first_responder"]);
    let mut h = harness(app);
    let calls = fake_engine(&h, |_, _| None);
    let s = &mut h.state_mut().state;
    assert!(s.user_input(1000.4));
    assert!(!s.user_input(1030.0));
    assert!(!s.user_input(1060.3));
    assert!(s.user_input(1060.4));
    assert_eq!(calls_of(&calls, "presence_report"), [json!({"input_at": 1000}), json!({"input_at": 1060})]);
    // Real input reaches it through the frame (a key press is activity).
    h.state_mut().state.presence_reported = f64::NEG_INFINITY;
    h.run_steps(1);
    h.key_press(egui::Key::J);
    h.run_steps(1);
    assert_eq!(calls_of(&calls, "presence_report").len(), 3);
}

#[test]
fn first_responder_presence_looking_decided_revert_and_counts() {
    use crate::home::Zone;
    let (app, tx) = engine_home(&["status", "actionables", "claims", "gestures", "decisions", "first_responder"]);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "presence_set" => Some(json!({"presence": {"mode": p["mode"], "effective": "present"}})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.state_mut().state.status_counts.auto_actions = Some(4);
    h.state_mut().state.status_counts.auto_reverted = Some(1);
    h.state_mut().state.presence =
        Some(lazyagent_proto::types::Presence { mode: Some("auto".into()), effective: "away".into(), since: None });
    h.run_steps(2);
    h.get_by_label("Orchestrator");
    h.get_by_label("Today: 4 actions · 1 reverted ▸  (t)");
    h.get_by_label("Presence: auto (away)").click();
    h.run_steps(2);
    h.get_by_label("present: it proposes, you decide").click();
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "presence_set"), [json!({"mode": "present"})]);
    assert_eq!(h.state().state.presence.as_ref().unwrap().effective, "present");
    // A routed question: "orchestrator · looking".
    let mut looking = inbox_row("search", "round", "Question:/r/repo-search/a3", "answer");
    looking["holder"] = json!("orchestrator");
    looking["zone"] = json!("in_flight");
    looking["level"] = json!(null);
    tx.send(event("status.updated", json!({"row": looking}))).unwrap();
    // The orchestrator answered a brief on billing, revertible.
    let mut d = brief("d1", "billing", true);
    (d["answered_by"], d["revertible"], d["state"]) = (json!("orchestrator"), json!(true), json!("answered"));
    d["answer"] = json!({"choice": "B", "reason": "fixed is enough here"});
    d["closed_at"] = json!(crate::app::unix_now() - 60.0);
    tx.send(event("decision.answered", json!({"decision": d}))).unwrap();
    tx.send(event("status.updated", json!({"row": status_row("billing", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(2);
    assert_eq!(home_names(&h, Zone::Flight), ["billing", "search"]);
    h.get_by_label("looking");
    h.get_by_label("decided by orchestrator: B (Fixed 2 s, 5 tries) — fixed is enough here");
    // Shift+U: Revert, with an optional comment for the agent.
    h.state_mut().state.home.selected = Some("repo:billing".into());
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::U);
    h.run_steps(2);
    h.get_by_label_contains("Revert the orchestrator's B: agent a2 is told your override");
    h.event(egui::Event::Text("use A, outages matter".into()));
    h.run_steps(1);
    h.get_by_label("Revert").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_revert"), [json!({"id": "d1", "comment": "use A, outages matter"})]);
    assert!(h.state().state.toasts.iter().any(|t| t.text == "Reverted: the agent is told."));
    h.get_by_label("decided by orchestrator: B (Fixed 2 s, 5 tries) — fixed is enough here (final)");
    assert!(h.query_by_label("Revert…  (⇧U)").is_none(), "reverted once");
}

#[test]
fn auto_delegation_is_set_per_repo_from_the_settings_menu() {
    let (app, tx) = engine_home(&["status", "actionables", "gestures", "first_responder"]);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, _| match m {
        "inbox_config_get" => Some(json!({"repo": "repo", "auto_delegate": ["conflict_with_base"],
                                          "auto_delegate_target": "owner"})),
        _ => None,
    });
    tx.send(event("status.updated", json!({"row": status_row("cart", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(2);
    h.get_by_label("Auto-delegation ▾").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "inbox_config_get"), [json!({"repo": "repo"})]);
    h.get_by_label("CI red (needs CI tracking, inactive)");
    h.get_by_label("Agent died").click();
    h.run_steps(2);
    h.get_by_label("the orchestrator").click();
    h.run_steps(2);
    let sets = calls_of(&calls, "inbox_config_set");
    assert_eq!(
        sets,
        [
            json!({"repo": "repo", "auto_delegate": ["conflict_with_base", "agent_dead"], "auto_delegate_target": "owner"}),
            json!({"repo": "repo", "auto_delegate": ["conflict_with_base", "agent_dead"],
                   "auto_delegate_target": "orchestrator"}),
        ]
    );
}

#[test]
fn a_restart_is_offered_only_when_the_app_could_be_rebuilt() {
    use crate::lifecycle::{ApplyResult, RestartWanted, Sheet, restart_after};
    let result = |steps: &[&str], app: bool, engine: bool| ApplyResult {
        ok: app,
        steps: steps.iter().map(|s| s.to_string()).collect(),
        restart: Some(RestartWanted { app, engine }),
        ..Default::default()
    };
    let after = |engine| Some(Sheet::Restart { engine, restore: true, after_update: true });
    assert_eq!(restart_after(&result(&["merge", "uv-sync", "build"], true, true)), after(true));
    assert_eq!(restart_after(&result(&["build"], true, false)), after(false));
    assert_eq!(restart_after(&result(&["upgrade"], true, true)), after(true));
    // Pulled, but the build failed: the app would come back as it is.
    assert_eq!(restart_after(&result(&["merge"], false, true)), None);
    assert_eq!(restart_after(&result(&[], false, false)), None);
    assert_eq!(restart_after(&ApplyResult { ok: true, ..Default::default() }), None, "already up to date");
}

#[test]
fn an_app_behind_the_checkout_is_rebuilt_before_its_restart() {
    use crate::lifecycle::{Phase, Sheet, UpdateInfo};
    // `lazyagent update apply` is a script that prints what `out` holds.
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("out");
    let fake = dir.path().join("python");
    std::fs::write(&fake, format!("#!/bin/sh\ncat '{}'\n", out.display())).unwrap();
    std::fs::set_permissions(&fake, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let (tx, rx) = mpsc::channel();
    let env = Env::process()
        .with("XDG_STATE_HOME", Some(dir.path().to_str().unwrap()))
        .with("LAZYAGENT_PYTHON", Some(fake.to_str().unwrap()));
    let conn = Conn::new("/r/repo".into(), env, tx, egui::Context::default());
    let mut h = harness(App::new(State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark)), rx, None));
    h.state_mut().state.life.info = Some(UpdateInfo {
        mode: "development".into(),
        upstream: Some("origin/develop".into()),
        app_behind: Some(true),
        ..Default::default()
    });
    h.state_mut().state.life.sheet = Some(Sheet::Update);
    h.run_steps(3);
    assert!(h.query_by_label("Restart…").is_none(), "a restart now would run this same binary");

    std::fs::write(
        &out,
        r#"{"step": "build", "state": "failed", "code": 101}
{"result": {"ok": false, "reason": "failed: app build", "steps": [], "restart": {"app": false, "engine": false}}}
"#,
    )
    .unwrap();
    h.get_by_label("Rebuild").click();
    step_until(&mut h, "the failed build", |a| a.state.life.phase == Phase::Idle && a.state.life.applied.is_some());
    h.run_steps(2);
    assert_eq!(h.state().state.life.sheet, Some(Sheet::Update), "no restart offered");
    assert!(h.query_by_label("failed: app build").is_some());

    std::fs::write(
        &out,
        r#"{"step": "build", "state": "done", "code": 0}
{"result": {"ok": true, "steps": ["build"], "restart": {"app": true, "engine": false}}}
"#,
    )
    .unwrap();
    h.get_by_label("Rebuild").click();
    step_until(&mut h, "the rebuild", |a| matches!(a.state.life.sheet, Some(Sheet::Restart { .. })));
    assert_eq!(h.state().state.life.sheet, Some(Sheet::Restart { engine: false, restore: true, after_update: true }));
    assert_eq!(h.state().state.life.info.as_ref().unwrap().app_behind, Some(false));
}

/// Home with I4's zones and gestures, in dark and light, from neutral demo
/// data, for docs/design/desktop-screenshots/i4c-home-*.png.
#[test]
fn home_i4c_screenshots() {
    let now = crate::app::unix_now();
    let w = |b: &str| format!("/home/dev/shop-{b}");
    let (tx, rx) = mpsc::channel();
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/home/dev/shop".into(), env, tx.clone(), egui::Context::default());
    let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.capabilities = Some(
        ["status", "actionables", "claims", "gestures", "decisions", "first_responder"]
            .iter()
            .map(|c| c.to_string())
            .collect(),
    );
    state.look_from = now - 1800.0;
    state.presence =
        Some(lazyagent_proto::types::Presence { mode: Some("auto".into()), effective: "present".into(), since: None });
    let branches = [
        "cart-discount",
        "billing-retries",
        "api-pagination",
        "search-filters",
        "invoice-pdf",
        "payments-hook",
        "order-export",
        "stock-sync",
        "docs-index",
    ];
    let trees: Vec<_> = std::iter::once(worktree("/home/dev/shop", "main", vec![]))
        .chain(branches.iter().map(|b| worktree(&w(b), b, vec![])))
        .collect();
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": trees, "orchestrator": {"agents": [agent("", "Orchestrator", "waiting", "so")]},
        "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    let row = |b: &str, zone: &str, level: Option<&str>, ball: &str, st: &str, ago: f64, pr: Option<u64>| {
        let mut r = status_row(b, zone, level, ball, st, ago);
        r["key"] = json!(format!("shop:{b}"));
        r["repo"] = json!("shop");
        r["worktree_path"] = json!(w(b));
        (r["seen_at"], r["last_event_at"]) = (json!(now - 1800.0), json!(now - ago.min(3600.0) * 2.0));
        if let Some(n) = pr {
            r["pr"] = json!({"number": n, "state": "OPEN", "review": "REVIEW_REQUIRED", "ci": "green", "mergeable": "MERGEABLE"});
            r["urls"] = json!([format!("https://example.com/shop/pull/{n}")]);
        }
        r
    };
    let mut d1 = brief("d1", "billing-retries", true);
    (d1["key"], d1["worktree_path"]) = (json!("shop:billing-retries"), json!(w("billing-retries")));
    let mut i1 = brief_item("d1", "billing-retries", "deep", true);
    (i1["work_item"], i1["worktree_path"]) = (json!("shop:billing-retries"), json!(w("billing-retries")));
    let mut billing = row("billing-retries", "inbox", Some("deep"), "you", "decide: retries", 720.0, Some(15));
    billing["needs_you"] = json!({"count": 1, "top": "Decision:d1"});
    let review = json!({"id": format!("review:{}", w("api-pagination")), "occurrence": "r@1", "kind": "review",
        "level": "deep", "title": "Changes requested", "label": "PR #21", "detail": "2 comments",
        "state": "changes requested by a reviewer", "work_item": "shop:api-pagination",
        "worktree_path": w("api-pagination"), "agent_id": null, "pr": null, "job_id": null, "exit_code": null,
        "blocks_agent": false, "priority": "medium", "since": now - 5400.0, "park": null, "class": "deep"});
    let mut api =
        row("api-pagination", "inbox", Some("deep"), "you", "changes requested by a reviewer", 5400.0, Some(21));
    api["needs_you"] = json!({"count": 1, "top": format!("review:{}", w("api-pagination"))});
    let approval = json!({"id": format!("approval:{}", w("cart-discount")), "occurrence": "a@1", "kind": "approval",
        "level": "now", "title": "Needs approval", "label": "Agent a1", "detail": "npm test", "state": "approve: npm test",
        "work_item": "shop:cart-discount", "worktree_path": w("cart-discount"), "agent_id": "a1", "pr": null,
        "job_id": null, "exit_code": null, "blocks_agent": true, "priority": "medium", "since": now - 120.0, "park": null});
    let mut cart = row("cart-discount", "inbox", Some("now"), "you", "approve: npm test", 120.0, None);
    cart["needs_you"] = json!({"count": 1, "top": format!("approval:{}", w("cart-discount"))});
    let mut docs = row("docs-index", "inbox", Some("round"), "you", "merge: approved, CI green", 3600.0, Some(14));
    docs["rule"] = json!(4);
    let mut qa = row("invoice-pdf", "in_flight", None, "others", "CI running", 2400.0, Some(16));
    qa["gesture"] = json!({"kind": "label", "at": now - 7200.0, "by": "user", "ball": "others", "text": "QA"});
    let mut looking = row("search-filters", "in_flight", None, "agent", "answer", 180.0, None);
    looking["holder"] = json!("orchestrator");
    let mut d2 = brief("d2", "payments-hook", true);
    (d2["key"], d2["worktree_path"]) = (json!("shop:payments-hook"), json!(w("payments-hook")));
    (d2["answered_by"], d2["revertible"], d2["state"]) = (json!("orchestrator"), json!(true), json!("answered"));
    d2["answer"] = json!({"choice": "A", "reason": "outages matter more than latency here"});
    d2["closed_at"] = json!(now - 900.0);
    let payments = row("payments-hook", "in_flight", None, "agent", "Edit src/payments/retry.py", 900.0, None);
    let mut parked = row("order-export", "in_flight", None, "others", "parked until CI ends", 4000.0, Some(12));
    parked["gesture"] = json!({"kind": "park", "at": now - 4000.0, "by": "user", "until": "ci_done"});
    let mut merged = row("stock-sync", "in_flight", None, "done", "merged", 7200.0, Some(9));
    merged["rule"] = json!(1);
    let events = vec![
        event("decision.created", json!({"decision": d1})),
        event("decision.answered", json!({"decision": d2})),
        event("actionable.created", json!({"item": i1})),
        event("actionable.created", json!({"item": review})),
        event("actionable.created", json!({"item": approval})),
        event("status.updated", json!({"row": cart})),
        event("status.updated", json!({"row": docs})),
        event("status.updated", json!({"row": billing})),
        event("status.updated", json!({"row": api})),
        event("status.updated", json!({"row": qa})),
        event("status.updated", json!({"row": looking})),
        event("status.updated", json!({"row": payments})),
        event("status.updated", json!({"row": parked})),
        event("status.updated", json!({"row": merged})),
    ];
    let mut h = Harness::builder()
        .with_size([1400.0, 960.0])
        .wgpu()
        .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
    h.run_steps(2);
    let calls = fake_engine(&h, move |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": now - 1800.0, "seen_at": now - 1800.0, "entries": [
            {"at": now - 1500.0, "kind": "commit", "actor": "engine", "text": "try fixed backoff", "ref": "abc123"},
            {"at": now - 1100.0, "kind": "ci", "actor": "engine", "text": "CI red"},
            {"at": now - 720.0, "kind": "decision", "actor": "agent:a2", "text": "asked: Retries: fixed or exponential backoff?", "ref": "d1"}]})),
        _ => None,
    });
    for e in events {
        tx.send(e).unwrap();
    }
    h.run_steps(3);
    h.state_mut().state.inbox_loaded = 3;
    h.state_mut().state.inbox_gen += 1;
    h.state_mut().state.status_counts.deep_max = 5;
    h.state_mut().state.status_counts.auto_actions = Some(3);
    h.state_mut().state.status_counts.auto_reverted = Some(0);
    h.run_steps(2);
    h.state_mut().state.home.selected = Some("shop:billing-retries".into());
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "status_timeline").len(), 1);
    h.state_mut().state.toasts.clear();
    h.run_steps(2);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    assert_eq!(h.state().state.home.count(crate::home::Zone::Deep), 2);
    if !shot(&mut h, "i4c-home-dark") {
        return;
    }
    let ctx = h.ctx.clone();
    h.state_mut().state.toggle_theme(&ctx);
    h.run_steps(4);
    shot(&mut h, "i4c-home-light");
}

// --- Home, I4d: park menu, Parked, names, holds, notes ---------------------

const I4D: &[&str] = &["status", "actionables", "claims", "gestures", "decisions", "holds", "delegation", "parked"];

/// A node's text (a Label keeps it in its value).
fn text_of<'t>(n: &impl egui_kittest::kittest::NodeT<'t>) -> String {
    let node = n.accesskit_node();
    node.value().or_else(|| node.label()).unwrap_or_default()
}

fn ci_item(key: &str) -> serde_json::Value {
    json!({"id": format!("Ci:/r/repo-{key}:7"), "occurrence": "c@1", "kind": "ci", "level": "round",
           "title": "CI failing", "label": "#7 Feat", "detail": "tests", "state": "CI red: tests",
           "work_item": format!("repo:{key}"), "worktree_path": format!("/r/repo-{key}"), "agent_id": null,
           "pr": null, "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - 300.0, "park": null, "class": "quick"})
}

#[test]
fn the_park_menu_offers_five_choices_and_the_whole_work_item() {
    let (app, tx) = engine_home(I4D);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        "actionables_mark" => Some(json!({"item": ci_item("cart")})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    let mut row = inbox_row("cart", "round", "Ci:/r/repo-cart:7", "CI red: tests");
    row["agents"] = json!([{"id": "a1", "status": "waiting", "since": crate::app::unix_now()}]);
    tx.send(event("actionable.created", json!({"item": ci_item("cart")}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(1);
    h.key_press(egui::Key::Z);
    h.run_steps(2);
    h.get_by_label("Park this item until");
    for label in ["1  1 hour", "2  tomorrow 08:00", "3  1 week", "4  something changes", "5  indefinitely"] {
        h.get_by_label(label);
    }
    // 2: this item, until tomorrow 08:00 (the engine works out the time).
    h.key_press(egui::Key::Num2);
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "actionables_mark"),
        [json!({"id": "Ci:/r/repo-cart:7", "action": "park", "until": "tomorrow"})]
    );
    // w: the whole work item and its items; 5: indefinitely.
    h.key_press(egui::Key::Z);
    h.run_steps(1);
    h.key_press(egui::Key::W);
    h.run_steps(2);
    h.get_by_label("Park the whole work item until");
    h.key_press(egui::Key::Num5);
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:cart", "kind": "park", "until": "indefinite"})]
    );
    // Esc closes it, nothing sent.
    h.key_press(egui::Key::Z);
    h.run_steps(1);
    h.key_press(egui::Key::Escape);
    h.run_steps(1);
    assert!(h.state().state.home.parking.is_none());
    // d hands the item to the branch's agent (the fake engine did not park it).
    tx.send(event("actionable.created", json!({"item": ci_item("cart")}))).unwrap();
    h.run_steps(2);
    h.key_press(egui::Key::D);
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "status_gesture")[1],
        json!({"key": "repo:cart", "kind": "delegate", "to": "agent", "item": "Ci:/r/repo-cart:7"})
    );
}

// --- Home, I7: CI on the head (GitHub Actions) -------------------------------

const I7: &[&str] =
    &["status", "actionables", "claims", "gestures", "decisions", "holds", "delegation", "parked", "actions"];

fn red_run(attempt: i64) -> serde_json::Value {
    json!({"id": 4242, "attempt": attempt, "workflow": "Tests", "head_sha": "abc1234def", "branch": "cart",
           "url": "https://github.com/o/r/actions/runs/4242", "conclusion": "failure",
           "failed_jobs": [{"name": "unit", "step": "Run pytest", "url": "https://github.com/o/r/actions/runs/4242/job/1"}]})
}

fn ci_log_summary(key: &str) -> serde_json::Value {
    json!({"key": key, "runs": [red_run(1)],
           "jobs": [{"name": "unit", "step": "Run pytest", "url": "https://github.com/o/r/actions/runs/4242/job/1",
                     "lines": ["E   assert 1 == 2", "FAILED tests/test_cart.py::test_total"], "total": 812}],
           "text": "CI failed on cart"})
}

/// A CI line on Home with its agent: `item` is the engine's item.
fn ci_home(caps: &[&str], item: serde_json::Value) -> (Harness<'static, App>, mpsc::Sender<Msg>, Calls) {
    let (app, tx) = engine_home(caps);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        "ci_log_summary" => Some(ci_log_summary(p["key"].as_str().unwrap())),
        _ => None,
    });
    h.run_steps(2);
    let mut row = inbox_row("cart", "round", item["id"].as_str().unwrap(), item["state"].as_str().unwrap());
    row["agents"] = json!([{"id": "a1", "status": "waiting", "since": crate::app::unix_now()}]);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    (h, tx, calls)
}

#[test]
fn a_red_run_shows_its_workflow_and_job_and_the_expanded_line_its_failed_log() {
    let mut item = ci_item("cart");
    item["run"] = red_run(1);
    let (mut h, tx, calls) = ci_home(I7, item.clone());
    assert_eq!(h.state().state.home.shown_in(crate::home::Zone::Round)[0].verb(), "Fix CI");
    h.get_by_label("· CI red · Tests / unit (step: Run pytest)");
    // Expanded: the failed log, read once per run.
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "ci_log_summary"), [json!({"key": "repo:cart"})]);
    h.get_by_label("Failed log");
    h.get_by_label("unit · Run pytest");
    h.get_by_label("E   assert 1 == 2\nFAILED tests/test_cart.py::test_total");
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "ci_log_summary").len(), 1, "not asked again for the same run");
    // A re-run that failed again (attempt 2): read again.
    item["run"] = red_run(2);
    tx.send(event("actionable.updated", json!({"item": item}))).unwrap();
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "ci_log_summary").len(), 2);
    // d: the engine writes the failures itself, so no text goes with it.
    h.key_press(egui::Key::D);
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:cart", "kind": "delegate", "to": "agent", "item": "Ci:/r/repo-cart:7"})]
    );
}

#[test]
fn a_branch_with_no_pr_gets_a_fix_ci_line_that_a_green_rerun_clears() {
    use crate::home::Zone;
    let mut item = ci_item("cart");
    item["id"] = json!("Ci_failed:/r/repo-cart:cart");
    item["kind"] = json!("ci_failed");
    item["title"] = json!("CI failed");
    item["label"] = json!("cart");
    item["run"] = red_run(1);
    let (mut h, tx, calls) = ci_home(I7, item);
    let line = h.state().state.home.shown_in(Zone::Round)[0].clone();
    assert_eq!((line.name.as_str(), line.verb()), ("cart", "Fix CI"));
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.get_by_label("Delegate to its agent  (d)");
    h.get_by_label("unit · Run pytest");
    h.key_press(egui::Key::D);
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:cart", "kind": "delegate", "to": "agent", "item": "Ci_failed:/r/repo-cart:cart"})]
    );
    // A green re-run: the item resolves and the line leaves at once, no
    // cut point needed.
    tx.send(event(
        "actionable.resolved",
        json!({"id": "Ci_failed:/r/repo-cart:cart", "occurrence": "c@1",
                                                 "reason": "cleared"}),
    ))
    .unwrap();
    tx.send(event(
        "status.updated",
        json!({"row": status_row("cart", "in_flight", None, "stalled", "agent idle", 0.0)}),
    ))
    .unwrap();
    h.run_steps(2);
    assert!(home_names(&h, Zone::Round).is_empty());
    assert_eq!(home_names(&h, Zone::Flight), ["cart"]);
    h.get_by_label("1 resolved on their own");
}

#[test]
fn without_ci_tracking_a_red_pr_is_delegated_with_the_failure_prompt() {
    let (mut h, tx, calls) = ci_home(I4D, ci_item("cart"));
    tx.send(event("pr_info.updated", json!({"path": "/r/repo-cart", "pr_info": pr_json()}))).unwrap();
    h.run_steps(2);
    // No run: the line keeps the engine's state.
    h.get_by_label("· tests");
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert!(h.query_by_label("Failed log").is_none());
    assert!(calls_of(&calls, "ci_log_summary").is_empty());
    h.key_press(egui::Key::D);
    h.run_steps(2);
    let sent = calls_of(&calls, "status_gesture");
    assert_eq!(sent.len(), 1);
    assert!(sent[0]["text"].as_str().unwrap().contains("tests"), "{sent:?}");
}

#[test]
fn with_ci_tracking_the_auto_delegation_menu_offers_ci_red_as_active() {
    let (app, tx) = engine_home(&["status", "actionables", "gestures", "first_responder", "actions"]);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, _| match m {
        "inbox_config_get" => Some(json!({"repo": "repo", "auto_delegate": [], "auto_delegate_target": "owner"})),
        _ => None,
    });
    tx.send(event("status.updated", json!({"row": status_row("cart", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(2);
    h.get_by_label("Auto-delegation ▾").click();
    h.run_steps(3);
    assert!(h.query_by_label("CI red (needs CI tracking, inactive)").is_none());
    h.get_by_label("CI red").click();
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "inbox_config_set"),
        [json!({"repo": "repo", "auto_delegate": ["ci_red"], "auto_delegate_target": "owner"})]
    );
}

#[test]
fn parked_work_items_wait_folded_and_say_when_they_need_you() {
    use crate::home::Zone;
    let (app, tx) = engine_home(I4D);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    let now = crate::app::unix_now();
    let mut quiet = status_row("export", "parked", None, "stalled", "parked: after the release", 600.0);
    quiet["gesture"] = json!({"kind": "park", "at": now - 600.0, "by": "user", "until": "indefinite",
                              "text": "after the release"});
    quiet["rule"] = json!(7);
    let mut asks = status_row("billing", "parked", None, "you", "parked until something changes", 300.0);
    asks["gesture"] = json!({"kind": "park", "at": now - 300.0, "by": "user", "until": "change"});
    asks["needs_you"] = json!({"count": 1, "top": "Question:/r/repo-billing/a1"});
    asks["rule"] = json!(7);
    tx.send(event("status.updated", json!({"row": quiet}))).unwrap();
    tx.send(event("status.updated", json!({"row": asks}))).unwrap();
    tx.send(event("status.updated", json!({"row": status_row("cart", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(2);
    assert_eq!(home_names(&h, Zone::Flight), ["cart"]);
    h.get_by_label("Parked");
    h.get_by_label("· 1 needs you");
    h.get_by_label("1 parked needs you ▸");
    assert!(h.query_by_label("parked: after the release").is_none(), "folded by default");
    assert_eq!(h.state().state.home.counts(), (0, 0));
    // Shift+Z shows them: a "needs you" line and a quiet one.
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::Z);
    h.run_steps(2);
    h.get_by_label("parked: after the release");
    h.get_by_label("needs you");
    h.get_by_label("parked");
    // Expanded: when it comes back, and Unpark.
    h.state_mut().state.home.selected = Some("repo:export".into());
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    h.get_by_label("parked indefinitely (after the release)");
    h.get_by_label("Unpark  (u)").click();
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "status_gesture"), [json!({"key": "repo:export", "kind": "clear"})]);
    h.state_mut().state.home.selected = Some("repo:billing".into());
    h.state_mut().state.home.expanded = Some("repo:billing".into());
    h.run_steps(2);
    h.get_by_label_contains("needs you: its items are not routed to the orchestrator");
}

#[test]
fn long_branch_names_keep_both_ends_with_the_full_name_on_hover_and_expanded() {
    let (app, tx) = engine_home(I4D);
    let mut h = harness(app);
    fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    let long = "feature-billing-retries-with-exponential-backoff-and-jitter-for-the-payments-hook";
    tx.send(event("status.updated", json!({"row": status_row(long, "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    let mut round = inbox_row(
        "search-filters-for-the-catalog-page-with-facets-and-sorting-v2-final",
        "round",
        "x",
        "CI red: tests",
    );
    round["needs_you"] = serde_json::Value::Null;
    tx.send(event("status.updated", json!({"row": round}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    for name in [long, "search-filters-for-the-catalog-page-with-facets-and-sorting-v2-final"] {
        let shown = h
            .query_all_by_label_contains("…")
            .map(|n| text_of(&n))
            .find(|l| l.starts_with(&name[..6]) && l.ends_with(&name[name.len() - 4..]))
            .unwrap_or_else(|| panic!("{name} is not cut in the middle"));
        assert!(shown.chars().count() < name.len());
    }
    h.ctx.all_styles_mut(|s| s.interaction.tooltip_delay = 0.0);
    h.get_by_label_contains("feature-").hover();
    h.run_steps(5);
    assert!(h.query_all_by_label(long).count() >= 1, "the full name on hover");
    h.state_mut().state.home.selected = Some(format!("repo:{long}"));
    h.state_mut().state.home.expanded = Some(format!("repo:{long}"));
    h.run_steps(2);
    assert!(h.query_all_by_label(long).count() >= 1, "and in the expanded row");
}

#[test]
fn held_passed_and_released_rows_say_what_the_orchestrator_does() {
    let (app, tx) = engine_home(I4D);
    let mut h = harness(app);
    h.run_steps(2);
    let now = crate::app::unix_now();
    let mut held = status_row("payments", "in_flight", None, "agent", "orchestrator · verifying tests", 60.0);
    held["hold"] = json!({"key": "repo:payments", "holder": "orchestrator", "at": now - 60.0, "until": now + 3540.0,
                          "by": "orchestrator", "note": "verifying tests", "auto": false});
    let mut passed = status_row("search", "in_flight", None, "agent", "orchestrator · looking", 30.0);
    passed["claim"] = json!({"key": "repo:search", "holder": "orchestrator", "at": now - 30.0,
                             "until": now + 7170.0, "by": "user", "allowed": ["relay"]});
    let mut back = inbox_row("export", "round", "Delegation:/r/repo-export", "needs a product call");
    back["note"] = json!({"text": "orchestrator: needs a product call", "by": "orchestrator", "at": now - 10.0});
    let mut merged = status_row("docs", "in_flight", None, "done", "already merged into develop", 120.0);
    merged["suggestion"] = json!("close PR #14: its head is in develop");
    merged["rule"] = json!(1);
    for r in [held, passed, back, merged] {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    h.run_steps(2);
    h.state_mut().state.home.cut(now);
    h.run_steps(2);
    h.get_by_label("verifying tests · 59m left");
    let looking = text_of(&h.get_by_label_contains("looking · "));
    assert!(looking.ends_with(" left") && looking.contains("h "), "{looking}");
    assert_eq!(h.query_all_by_label("orchestrator").count(), 2, "both in the ball column");
    h.get_by_label("orchestrator: needs a product call");
    h.get_by_label("· needs a product call");
    h.get_by_label("suggestion: close PR #14: its head is in develop (p opens the PR; closing it is yours)");
}

/// In flight's done rows: those with nothing to do fold into one "N done
/// today" group at its end; a done row with a suggestion stays in place.
fn done_fold_rows() -> Vec<serde_json::Value> {
    let mut rows = vec![
        status_row("cart", "in_flight", None, "agent", "working", 60.0),
        status_row("search", "in_flight", None, "others", "review requested", 120.0),
    ];
    let mut docs = status_row("docs", "in_flight", None, "done", "already merged into develop", 300.0);
    docs["suggestion"] = json!("close PR #47: its head is in develop");
    rows.push(docs);
    for (i, (b, st)) in [("gone-1", "merged"), ("gone-2", "closed"), ("gone-3", "worktree removed")].iter().enumerate()
    {
        rows.push(status_row(b, "in_flight", None, "done", st, 600.0 + i as f64 * 60.0));
    }
    for r in &mut rows {
        if r["ball"] == "done" {
            r["rule"] = json!(1);
        }
    }
    rows
}

#[test]
fn done_rows_fold_into_one_group_folded_by_default() {
    let (app, tx) = engine_home(I4D);
    let mut h = harness(app);
    h.run_steps(2);
    for r in done_fold_rows() {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    h.run_steps(2);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    // Folded by default: one header, none of its rows.
    h.get_by_label("3 done today ▸");
    assert!(h.query_by_label("gone-1").is_none());
    assert!(h.query_by_label("· 3 done today").is_none(), "the count moved into the group's header");
    // The count: cart, search and docs (a suggestion still asks something).
    h.get_by_label("3");
    assert!(h.query_by_label("6").is_none() && h.query_by_label("2").is_none());
    // A done row with a suggestion stays visible, in place.
    assert_eq!(home_names(&h, crate::home::Zone::Flight), ["cart", "search", "docs"]);
    h.get_by_label("docs");
    h.get_by_label("suggestion: close PR #47: its head is in develop (p opens the PR; closing it is yours)");
    // j/k skip the folded rows.
    h.key_press(egui::Key::Num4);
    h.run_steps(1);
    for _ in 0..5 {
        h.key_press(egui::Key::J);
        h.run_steps(1);
    }
    assert_eq!(h.state().state.home.selected.as_deref(), Some("repo:docs"));
    // ⇧F unfolds it, kept in the repo's state; j enters its rows.
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::F);
    h.run_steps(2);
    h.get_by_label("3 done today ▾");
    h.get_by_label("gone-1");
    assert!(h.state().state.saved.home_done_open);
    h.key_press(egui::Key::J);
    h.run_steps(1);
    let first = h.state().state.home.done_group()[0].key.clone();
    assert_eq!(h.state().state.home.selected.as_deref(), Some(first.as_str()));
    for _ in 0..5 {
        h.key_press(egui::Key::J);
        h.run_steps(1);
    }
    let last = h.state().state.home.done_group()[2].key.clone();
    assert_eq!(h.state().state.home.selected.as_deref(), Some(last.as_str()));
    // A click folds it again; the cursor leaves its rows.
    h.get_by_label("3 done today ▾").click();
    h.run_steps(2);
    h.get_by_label("3 done today ▸");
    assert!(h.query_by_label("gone-1").is_none());
    assert!(!h.state().state.saved.home_done_open);
    h.key_press(egui::Key::K);
    h.run_steps(1);
    let at = h.state().state.home.selected.clone().unwrap();
    assert!(!h.state().state.home.done_group().iter().any(|l| l.key == at), "{at}");
    // A click unfolds it too, and f (In flight folded) still hides it all.
    h.get_by_label("3 done today ▸").click();
    h.run_steps(2);
    h.get_by_label("gone-2");
    h.key_press(egui::Key::F);
    h.run_steps(2);
    assert!(h.query_by_label("3 done today ▾").is_none() && h.query_by_label("gone-2").is_none());
}

/// docs/design/desktop-screenshots/home-done-fold-*.png: In flight with its
/// done group folded (dark, light) and unfolded (light).
#[test]
fn home_done_fold_screenshots() {
    let (tx, rx) = mpsc::channel();
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/r/repo".into(), env, tx.clone(), egui::Context::default());
    let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.capabilities = Some(I4D.iter().map(|c| c.to_string()).collect());
    state.inbox_loaded = 3;
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": [worktree("/r/repo", "main", vec![])], "orchestrator": {"agents": []},
        "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    let mut rows = done_fold_rows();
    for (b, st) in [("gone-4", "merged"), ("gone-5", "merged"), ("gone-6", "closed by you")] {
        let mut r = status_row(b, "in_flight", None, "done", st, 4000.0);
        r["rule"] = json!(1);
        rows.push(r);
    }
    let mut round = status_row("checkout", "inbox", Some("round"), "you", "CI red: tests", 900.0);
    round["pr"] = json!({"number": 51, "state": "OPEN", "review": "", "ci": "red", "mergeable": "MERGEABLE"});
    rows.push(round);
    let mut h = Harness::builder()
        .with_size([1100.0, 620.0])
        .wgpu()
        .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
    h.run_steps(2);
    for r in rows {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.state_mut().state.home.selected = Some("repo:docs".into());
    h.run_steps(3);
    h.state_mut().state.toasts.clear();
    h.run_steps(2);
    h.get_by_label("6 done today ▸");
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    if !shot(&mut h, "home-done-fold-dark") {
        return;
    }
    let ctx = h.ctx.clone();
    h.state_mut().state.toggle_theme(&ctx);
    h.run_steps(4);
    shot(&mut h, "home-done-fold-light");
    h.state_mut().state.toggle_done_group();
    h.run_steps(4);
    shot(&mut h, "home-done-fold-open-light");
}

/// Home with I4d's park menu, Parked group, held and passed rows and a
/// release note, in dark and light, for docs/design/desktop-screenshots/i4d-*.png.
#[test]
fn home_i4d_screenshots() {
    let now = crate::app::unix_now();
    let w = |b: &str| format!("/home/dev/shop-{b}");
    let (tx, rx) = mpsc::channel();
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/home/dev/shop".into(), env, tx.clone(), egui::Context::default());
    let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    let mut caps: Vec<String> = I4D.iter().map(|c| c.to_string()).collect();
    caps.push("first_responder".into());
    state.capabilities = Some(caps);
    state.look_from = now - 1800.0;
    state.presence =
        Some(lazyagent_proto::types::Presence { mode: Some("auto".into()), effective: "present".into(), since: None });
    let branches = [
        "cart-discount",
        "billing-retries-with-exponential-backoff-and-jitter",
        "search-filters",
        "invoice-pdf",
        "order-export",
        "stock-sync",
        "docs-index",
        "release-notes",
    ];
    let trees: Vec<_> = std::iter::once(worktree("/home/dev/shop", "main", vec![]))
        .chain(branches.iter().map(|b| worktree(&w(b), b, vec![])))
        .collect();
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": trees, "orchestrator": {"agents": [agent("", "Orchestrator", "waiting", "so")]},
        "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    let row = |b: &str, zone: &str, level: Option<&str>, ball: &str, st: &str, ago: f64, pr: Option<u64>| {
        let mut r = status_row(b, zone, level, ball, st, ago);
        r["key"] = json!(format!("shop:{b}"));
        r["repo"] = json!("shop");
        r["worktree_path"] = json!(w(b));
        (r["seen_at"], r["last_event_at"]) = (json!(now - 1800.0), json!(now - ago.min(3600.0) * 2.0));
        if let Some(n) = pr {
            r["pr"] = json!({"number": n, "state": "OPEN", "review": "", "ci": "red", "mergeable": "MERGEABLE"});
            r["urls"] = json!([format!("https://example.com/shop/pull/{n}")]);
        }
        r
    };
    let ci = |b: &str| {
        let mut i = ci_item(b);
        (i["id"], i["work_item"], i["worktree_path"]) =
            (json!(format!("Ci:{}:7", w(b))), json!(format!("shop:{b}")), json!(w(b)));
        i
    };
    let long = "billing-retries-with-exponential-backoff-and-jitter";
    let mut billing = row(long, "inbox", Some("round"), "you", "CI red: tests", 900.0, Some(15));
    billing["needs_you"] = json!({"count": 1, "top": format!("Ci:{}:7", w(long))});
    billing["agents"] = json!([{"id": "a2", "status": "waiting", "since": now - 900.0}]);
    let mut back = row("order-export", "inbox", Some("round"), "you", "needs a product call", 300.0, None);
    back["note"] = json!({"text": "orchestrator: needs a product call", "by": "orchestrator", "at": now - 300.0});
    let mut nobody = row("invoice-pdf", "inbox", Some("round"), "you", "PR has no reviewer", 2400.0, Some(16));
    nobody["pr"]["ci"] = json!("green");
    let mut held = row("search-filters", "in_flight", None, "agent", "orchestrator · verifying tests", 600.0, None);
    held["hold"] = json!({"key": "shop:search-filters", "holder": "orchestrator", "at": now - 600.0,
                          "until": now + 3000.0, "by": "engine", "note": "checking agent a3's work", "auto": true});
    let mut passed = row("cart-discount", "in_flight", None, "agent", "orchestrator · looking", 120.0, None);
    passed["claim"] = json!({"key": "shop:cart-discount", "holder": "orchestrator", "at": now - 120.0,
                             "until": now + 7080.0, "by": "user", "allowed": ["relay"]});
    let mut delegated = row("stock-sync", "in_flight", None, "agent", "agent a4 · on it", 400.0, Some(18));
    delegated["delegation"] = json!({"agent": "a4", "item": "Ci:x:18", "occurrence": "o", "fact": "CI red: lint",
                                     "at": now - 400.0, "by": "user", "phase": "on_it"});
    let mut merged = row("docs-index", "in_flight", None, "done", "already merged into develop", 3600.0, Some(14));
    merged["suggestion"] = json!("close PR #14: its head is in develop");
    merged["rule"] = json!(1);
    let mut parked = row("release-notes", "parked", None, "stalled", "parked: after the release", 7200.0, None);
    parked["gesture"] = json!({"kind": "park", "at": now - 7200.0, "by": "user", "until": "indefinite",
                               "text": "after the release"});
    parked["rule"] = json!(7);
    let events = vec![
        event("actionable.created", json!({"item": ci(long)})),
        event("status.updated", json!({"row": billing})),
        event("status.updated", json!({"row": back})),
        event("status.updated", json!({"row": nobody})),
        event("status.updated", json!({"row": held})),
        event("status.updated", json!({"row": passed})),
        event("status.updated", json!({"row": delegated})),
        event("status.updated", json!({"row": merged})),
        event("status.updated", json!({"row": parked})),
    ];
    let mut h = Harness::builder()
        .with_size([1400.0, 960.0])
        .wgpu()
        .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
    h.run_steps(2);
    fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    for e in events {
        tx.send(e).unwrap();
    }
    h.run_steps(3);
    h.state_mut().state.inbox_loaded = 3;
    h.state_mut().state.inbox_gen += 1;
    h.run_steps(2);
    h.state_mut().state.home.cut(now);
    h.state_mut().state.home.parked_open = true;
    h.state_mut().state.home.selected = Some(format!("shop:{long}"));
    h.state_mut().state.home.expanded = Some(format!("shop:{long}"));
    h.state_mut().state.home.parking = Some(format!("shop:{long}"));
    h.run_steps(4);
    h.state_mut().state.toasts.clear();
    h.run_steps(2);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    h.get_by_label("Parked");
    if !shot(&mut h, "i4d-home-dark") {
        return;
    }
    let ctx = h.ctx.clone();
    h.state_mut().state.toggle_theme(&ctx);
    h.run_steps(4);
    shot(&mut h, "i4d-home-light");
}

// --- Home, I6: feeders' work items (projects, waits on, priority, waiting) ----

const I6: &[&str] =
    &["status", "actionables", "claims", "gestures", "decisions", "holds", "delegation", "parked", "work_items"];

/// A row linked to a feeder's work item: its project, what it waits on.
fn feeder_row(key: &str, ball: &str, state: &str, project: Option<(&str, &str)>, waits: &[&str]) -> serde_json::Value {
    let mut r = status_row(key, "in_flight", None, ball, state, 600.0);
    let url = format!("https://board.example.com/c/{key}");
    r["urls"] = json!([url]);
    r["project"] = match project {
        Some((id, title)) => json!({"id": id, "title": title, "policy": {"continue_while_review": true}}),
        None => json!(null),
    };
    r["depends_on"] = json!(waits.iter().map(|w| format!("https://board.example.com/c/{w}")).collect::<Vec<_>>());
    r["waits_on"] = json!(
        waits
            .iter()
            .map(|w| json!({"url": format!("https://board.example.com/c/{w}"), "title": w, "state": "review"}))
            .collect::<Vec<_>>()
    );
    r["feeder"] = json!({"url": url, "title": key, "source": "planner", "priority": "medium", "ticket": null,
                         "branch": null, "project": r["project"], "depends_on": r["depends_on"], "waiting": null,
                         "expires_at": null, "created_at": 1.0, "updated_at": 1.0});
    r
}

#[test]
fn in_flight_groups_by_project_with_waits_on_priority_and_not_started() {
    use crate::home::Zone;
    let (app, tx) = engine_home(I6);
    let mut h = harness(app);
    fake_engine(&h, |_, _| None);
    h.run_steps(2);
    let mut cart = feeder_row("cart", "others", "review requested", Some(("shop", "Shop")), &[]);
    cart["priority"] = json!("high");
    let mut refunds = feeder_row("refunds", "agent", "Edit", Some(("shop", "Shop")), &["cart"]);
    refunds["priority"] = json!("low");
    refunds["feeder"]["priority"] = json!("low");
    let docs = status_row("docs", "in_flight", None, "agent", "Write", 300.0);
    let search = feeder_row("search", "agent", "Bash", Some(("find", "Search revamp")), &[]);
    for r in [refunds, docs, search, cart] {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    h.state_mut().state.status_counts.not_started = 2;
    h.run_steps(2);
    // Lines with no project first, then each project's together.
    let names = home_names(&h, Zone::Flight);
    assert_eq!(names[0], "docs");
    let shop: Vec<usize> = ["cart", "refunds"].iter().map(|n| names.iter().position(|x| x == n).unwrap()).collect();
    assert_eq!((shop[0] as i32 - shop[1] as i32).abs(), 1, "{names:?}");
    h.get_by_label("▸ Shop");
    h.get_by_label("▸ Search revamp");
    assert!(h.query_all_by_label("· continues while in review").count() >= 1);
    // Keyboard order follows the groups.
    let visible: Vec<String> = h.state().state.home.visible(false).iter().map(|l| l.name.clone()).collect();
    assert_eq!(visible, names);
    h.get_by_label_contains("waits on cart");
    // The priority marker: high and low only.
    h.get_by_label("▴");
    h.get_by_label("▾");
    // The counter counts the feeders' items too (the engine's counts).
    h.get_by_label_contains("· 2 not started");
    // Expanded: the project, what it waits on (in review), the feeder.
    h.state_mut().state.home.expanded = Some("repo:refunds".into());
    h.run_steps(2);
    h.get_by_label("project Shop · waits on cart (in review)");
    h.get_by_label_contains("from planner · low priority · https://board.example.com/c/refunds");
}

#[test]
fn a_waiting_item_sits_in_parked_with_its_reason_and_unparks() {
    let (app, tx) = engine_home(I6);
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    let now = crate::app::unix_now();
    let mut held = feeder_row("refunds", "stalled", "waiting (board)", None, &[]);
    held["zone"] = json!("parked");
    held["rule"] = json!(7);
    held["gesture"] = json!({"kind": "park", "at": now - 600.0, "by": "feeder:board", "until": "indefinite",
                             "waiting": true, "source": "board"});
    let mut why = feeder_row("coupons", "stalled", "waiting: design review", None, &[]);
    why["zone"] = json!("parked");
    why["rule"] = json!(7);
    why["gesture"] = json!({"kind": "park", "at": now - 60.0, "by": "feeder:board", "until": now + 7200.0,
                            "waiting": true, "source": "board", "text": "design review"});
    tx.send(event("status.updated", json!({"row": held}))).unwrap();
    tx.send(event("status.updated", json!({"row": why}))).unwrap();
    h.run_steps(2);
    h.get_by_label("Parked");
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::Z);
    h.run_steps(2);
    h.get_by_label("waiting (board)");
    h.get_by_label("waiting: design review");
    h.state_mut().state.home.selected = Some("repo:refunds".into());
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    h.get_by_label_contains("waiting (board) · held in board's Waiting lane; u unparks it until board changes it");
    h.get_by_label("Unpark  (u)").click();
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "status_gesture"), [json!({"key": "repo:refunds", "kind": "clear"})]);
    h.state_mut().state.home.expanded = Some("repo:coupons".into());
    h.run_steps(2);
    h.get_by_label("waiting (board) for 2h 00m (design review), by feeder:board 1m ago");
}

/// Projects in In flight, "waits on", priority markers, the not-started
/// counter and a waiting item in Parked, in dark and light, for
/// docs/design/desktop-screenshots/i6-*.png.
#[test]
fn home_i6_screenshots() {
    let now = crate::app::unix_now();
    let w = |b: &str| format!("/home/dev/shop-{b}");
    let (tx, rx) = mpsc::channel();
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/home/dev/shop".into(), env, tx.clone(), egui::Context::default());
    let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.capabilities = Some(I6.iter().map(|c| c.to_string()).collect());
    state.look_from = now - 1800.0;
    let branches =
        ["SHOP-11-cart", "SHOP-12-checkout", "SHOP-14-refunds", "docs-index", "SHOP-20-search", "SHOP-9-tax"];
    let trees: Vec<_> = std::iter::once(worktree("/home/dev/shop", "main", vec![]))
        .chain(branches.iter().map(|b| worktree(&w(b), b, vec![])))
        .collect();
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": trees, "orchestrator": {"agents": []}, "pr_info": {}, "git_statuses": {}, "view": null,
        "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    let row = |b: &str, ball: &str, st: &str, ago: f64, project: Option<(&str, &str)>, waits: &[&str]| {
        let mut r = feeder_row(b, ball, st, project, waits);
        r["key"] = json!(format!("shop:{b}"));
        r["repo"] = json!("shop");
        r["worktree_path"] = json!(w(b));
        r["changed_at"] = json!(now - ago);
        (r["seen_at"], r["last_event_at"]) = (json!(now - 1800.0), json!(now - 3600.0));
        r
    };
    let shop = Some(("shop", "Checkout revamp"));
    let mut cart = row("SHOP-11-cart", "others", "review requested, CI green", 5400.0, shop, &[]);
    cart["priority"] = json!("high");
    cart["feeder"]["priority"] = json!("high");
    cart["title"] = json!("Cart totals");
    cart["pr"] =
        json!({"number": 21, "state": "OPEN", "review": "REVIEW_REQUIRED", "ci": "green", "mergeable": "MERGEABLE"});
    let mut checkout = row("SHOP-12-checkout", "agent", "Edit", 600.0, shop, &["Cart totals"]);
    checkout["agents"] = json!([{"id": "a2", "status": "running", "since": now - 600.0}]);
    let mut tax = row("SHOP-9-tax", "stalled", "no agent", 86400.0, None, &[]);
    tax["priority"] = json!("low");
    tax["feeder"]["priority"] = json!("low");
    let docs = {
        let mut r = status_row("docs-index", "in_flight", None, "agent", "Write", 300.0);
        r["key"] = json!("shop:docs-index");
        r["worktree_path"] = json!(w("docs-index"));
        r
    };
    let mut search = row("SHOP-20-search", "agent", "Bash", 1200.0, Some(("find", "Search")), &[]);
    search["agents"] = json!([{"id": "a3", "status": "running", "since": now - 1200.0}]);
    search["project"]["policy"] = json!({"continue_while_review": false});
    let mut refunds = row("SHOP-14-refunds", "stalled", "waiting: design review", 7200.0, shop, &[]);
    refunds["zone"] = json!("parked");
    refunds["rule"] = json!(7);
    refunds["gesture"] = json!({"kind": "park", "at": now - 7200.0, "by": "feeder:board", "until": "indefinite",
                                "waiting": true, "source": "board", "text": "design review"});
    let mut h = Harness::builder()
        .with_size([1400.0, 720.0])
        .wgpu()
        .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
    h.run_steps(2);
    fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    for r in [cart, checkout, tax, docs, search, refunds] {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    h.run_steps(3);
    h.state_mut().state.inbox_loaded = 3;
    h.state_mut().state.inbox_gen += 1;
    h.state_mut().state.status_counts.not_started = 3;
    h.run_steps(2);
    h.state_mut().state.home.cut(now);
    h.state_mut().state.home.parked_open = true;
    h.state_mut().state.home.selected = Some("shop:SHOP-12-checkout".into());
    h.state_mut().state.home.expanded = Some("shop:SHOP-12-checkout".into());
    h.run_steps(4);
    h.state_mut().state.toasts.clear();
    h.run_steps(2);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    h.get_by_label("▸ Checkout revamp");
    if !shot(&mut h, "i6-home-dark") {
        return;
    }
    let ctx = h.ctx.clone();
    h.state_mut().state.toggle_theme(&ctx);
    h.run_steps(4);
    shot(&mut h, "i6-home-light");
}

// --- I5: the decision view (brief | evidence | timeline), answers, Explain
// this and a new brief, against a fake engine --------------------------------

const I5: &[&str] = &["status", "actionables", "claims", "gestures", "decisions", "first_responder", "decision_view"];

const RETRY_DIFF: &str = "diff --git a/src/retry.py b/src/retry.py\n--- a/src/retry.py\n+++ b/src/retry.py\n\
@@ -10,2 +10,2 @@ import time\n ctx\n-a\n+b\n@@ -40,7 +40,12 @@ def retry():\n-    sleep(2)\n+    sleep(backoff(n))\n";

/// Home with a billing worktree (agent a2 when `alive`) and its brief d1 in
/// To sit down with; the fake engine answers the view's requests.
fn i5_home(alive: bool, caps: &[&str]) -> (Harness<'static, App>, mpsc::Sender<Msg>, Calls) {
    let agents = if alive { vec![agent("a2", "Implementer", "waiting", "s2")] } else { vec![] };
    let (mut app, tx) = app(vec![worktree("/r/repo", "main", vec![]), worktree("/r/repo-billing", "billing", agents)]);
    app.state.view = crate::app::View::Home;
    app.state.capabilities = Some(caps.iter().map(|c| c.to_string()).collect());
    app.state.inbox_loaded = 3;
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "decision_get" => Some(json!({"decision": brief("d1", "billing", true)})),
        "status_timeline" => Some(json!({"key": p["key"], "since": 0, "seen_at": null, "entries": [
            {"at": crate::app::unix_now() - 900.0, "kind": "commit", "actor": "engine", "text": "retry: backoff"},
            {"at": crate::app::unix_now() - 600.0, "kind": "decision", "actor": "agent:a2",
             "text": "asked: Retries: fixed or exponential backoff?", "ref": "d1"}]})),
        "get_diff" => Some(json!({"worktree_path": "/r/repo-billing", "diff": RETRY_DIFF, "truncated": false})),
        "decision_evidence" => Some(json!({"id": p["id"], "index": p["index"], "kind": "log", "start": 117,
                                           "text": "collected 30 items\nFAILED test_retry - timeout\n3 failed", "total": 400})),
        "decision_answer" => {
            let mut d = brief("d1", "billing", true);
            d["state"] = json!("answered");
            d["answered_by"] = json!("user");
            d["answer"] = json!({"choice": p["choice"], "comment": p["comment"], "at": crate::app::unix_now()});
            Some(json!({"decision": d}))
        }
        "decision_explain_request" | "decision_rebrief_request" => Some(json!({"explain": {
            "target": "d1", "id": "d1", "key": "repo:billing", "route": "owner", "to": "agent:a2", "agent": "a2",
            "kind": if m == "decision_explain_request" { "explain" } else { "rebrief" },
            "state": "asked", "asked_at": crate::app::unix_now()}})),
        _ => None,
    });
    h.run_steps(2);
    tx.send(event("decision.created", json!({"decision": brief("d1", "billing", true)}))).unwrap();
    tx.send(event("actionable.created", json!({"item": brief_item("d1", "billing", "deep", true)}))).unwrap();
    tx.send(event("status.updated", json!({"row": inbox_row("billing", "deep", "Decision:d1", "decide")}))).unwrap();
    h.run_steps(3);
    (h, tx, calls)
}

fn view(h: &Harness<'static, App>) -> crate::decision_view::DecisionView {
    h.state().state.decision.clone().expect("the decision view is open")
}

#[test]
fn enter_on_a_brief_opens_the_view_with_the_recommendation_preselected() {
    let (mut h, _tx, calls) = i5_home(true, I5);
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    assert_eq!(view(&h).choice.as_deref(), Some("A"), "the recommendation");
    assert_eq!(calls_of(&calls, "status_timeline"), [json!({"key": "repo:billing", "since": 0})]);
    h.get_by_label("Retries: fixed or exponential backoff?");
    h.get_by_label("A  Exponential, cap 30 s   (recommended)");
    h.get_by_label("fixed 2 s: 3 of 30 runs hit the 10 s limit");
    h.get_by_label("Reversible: yes");
    h.get_by_label_contains("asked: Retries: fixed or exponential backoff? · agent:a2");
    assert!(h.query_by_label_contains("Outdated").is_none(), "abc1234 is the brief's abc123");
    // The keys pick an option; Esc goes back to Home.
    h.key_press(egui::Key::Num2);
    h.run_steps(2);
    assert_eq!(view(&h).choice.as_deref(), Some("B"));
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert!(h.state().state.decision.is_none());
    h.get_by_label("To sit down with");
    // v opens it as well.
    h.key_press(egui::Key::V);
    h.run_steps(2);
    assert_eq!(view(&h).id, "d1");
}

#[test]
fn an_orchestrators_suggestion_is_preselected_over_the_recommendation() {
    let mut d: lazyagent_proto::types::Decision = serde_json::from_value(brief("d1", "billing", true)).unwrap();
    assert_eq!(crate::decision_view::preselected(&d).as_deref(), Some("A"));
    d.escalation = Some(json!({"by": "orchestrator", "note": "a product call", "suggestion": "b", "at": 1.0}));
    assert_eq!(crate::decision_view::preselected(&d).as_deref(), Some("B"));
}

#[test]
fn answering_sends_the_choice_and_the_comment() {
    let (mut h, _tx, calls) = i5_home(true, I5);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.get_by_label_contains("B  Fixed 2 s, 5 tries").click();
    h.run_steps(2);
    h.state_mut().state.decision.as_mut().unwrap().comment = "keep it simple".into();
    h.run_steps(1);
    h.get_by_label("Answer B  (Ctrl+Enter)").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_answer"), [json!({"id": "d1", "choice": "B", "comment": "keep it simple"})]);
    h.get_by_label("Answered: B · sent to agent a2 once its turn is over");
    assert!(h.query_by_label("Answer B  (Ctrl+Enter)").is_none(), "no second answer");
}

#[test]
fn the_outdated_banner_shows_when_the_branch_moved() {
    let (mut h, tx, _calls) = i5_home(true, I5);
    let mut moved = brief("d1", "billing", true);
    moved["sha"] = json!("0ddba11");
    tx.send(event("decision.created", json!({"decision": moved}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.conn.fake.lock().unwrap().take();
    h.state_mut().state.open_decision("d1", "repo:billing");
    h.run_steps(3);
    h.get_by_label(
        "Outdated: the branch moved since the brief (0ddba11 → abc1234). Its evidence may not match; ask for a new brief (b).",
    );
}

#[test]
fn evidence_tabs_show_the_hunk_and_the_log_lines() {
    use crate::decision_view::Ev;
    use crate::diff;
    let (mut h, _tx, calls) = i5_home(true, I5);
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    h.get_by_label("diff src/retry.py");
    h.get_by_label("log job:tests:120-134");
    assert_eq!(calls_of(&calls, "get_diff")[0]["worktree_path"], json!("/r/repo-billing"));
    match view(&h).evidence.get(&0) {
        Some(Ev::Hunk(rows, None)) => {
            assert!(rows[0].text.starts_with("@@ -40,7 +40,12 @@"), "only the hunk asked for");
            assert_eq!(rows.len(), 3);
        }
        other => panic!("{other:?}"),
    }
    h.key_press(egui::Key::CloseBracket);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_evidence"), [json!({"id": "d1", "index": 1})]);
    h.get_by_label_contains("  118  FAILED test_retry - timeout");
    // The hunk gone from the diff: the file's whole diff, said so.
    let d = diff::parse(RETRY_DIFF);
    match crate::decision_view::find_hunk(&d, "src/retry.py", Some("@@ -1,1 +1,1 @@")) {
        Ev::Hunk(rows, Some(note)) => {
            assert_eq!(rows.len(), d.files[0].rows.len());
            assert!(note.contains("not in the diff now"));
        }
        other => panic!("{other:?}"),
    }
    assert!(matches!(crate::decision_view::find_hunk(&d, "src/other.py", None), Ev::Failed(_)));
}

#[test]
fn explain_goes_to_the_owner_and_shows_who_explained() {
    let (mut h, tx, calls) = i5_home(true, I5);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.get_by_label("Explain this  (y)");
    h.key_press(egui::Key::Y);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_explain_request"), [json!({"id": "d1"})]);
    h.get_by_label("asked agent a2: sent once its turn is over");
    tx.send(event(
        "decision.explain",
        json!({"explain": {
        "target": "d1", "id": "d1", "key": "repo:billing", "kind": "explain", "route": "owner", "to": "agent:a2",
        "agent": "a2", "state": "answered", "asked_at": crate::app::unix_now() - 5.0, "by": "agent:a2",
        "text": "A backs off on outages.\nB keeps runs short.", "answered_at": crate::app::unix_now()}}),
    ))
    .unwrap();
    h.run_steps(3);
    h.get_by_label("explained by agent a2");
    h.get_by_label("A backs off on outages.\nB keeps runs short.");
}

#[test]
fn explain_offers_restore_and_is_disabled_when_nobody_can() {
    let (mut h, _tx, calls) = i5_home(false, I5);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    // Nobody: no agent, nothing restorable, no orchestrator.
    use egui_kittest::kittest::NodeT;
    let button = h.get_by_label("Explain this");
    assert!(button.accesskit_node().is_disabled());
    h.get_by_label("Open terminal");
    h.get_by_label("Pass to orchestrator");
    h.key_press(egui::Key::Y);
    h.run_steps(2);
    assert!(calls_of(&calls, "decision_explain_request").is_empty());
    // Restorable: offered, never automatic.
    let entry: lazyagent_proto::types::RestorableAgent = serde_json::from_value(json!({
        "id": "r1", "worktree_path": "/r/repo-billing", "agent_id": "a2", "kind": "agent", "provider": "claude",
        "label": "", "session_name": "", "skip_permissions": true, "conversation_id": null, "instruction": null,
        "started_at": 1.0, "engine_id": null, "ended_at": 2.0}))
    .unwrap();
    h.state_mut().state.restorable = vec![entry];
    h.run_steps(2);
    h.key_press(egui::Key::Y);
    h.run_steps(2);
    assert!(calls_of(&calls, "decision_explain_request").is_empty(), "y never restores");
    h.get_by_label("Restore agent and explain  (⇧Y)").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_explain_request"), [json!({"id": "d1", "restore": true})]);
    // An orchestrator, and nothing restorable: it explains.
    h.state_mut().state.restorable.clear();
    let orch: lazyagent_proto::types::Agent =
        serde_json::from_value(agent("", "Orchestrator", "waiting", "so")).unwrap();
    h.state_mut().state.store.orchestrator = vec![orch];
    h.run_steps(2);
    h.get_by_label("its agent is not running: the orchestrator answers");
}

#[test]
fn a_new_brief_is_asked_for_with_a_note() {
    let (mut h, _tx, calls) = i5_home(true, I5);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.key_press(egui::Key::B);
    h.run_steps(2);
    h.state_mut().state.decision.as_mut().unwrap().rebrief = Some("add the cost of each".into());
    h.run_steps(1);
    h.get_by_label("Ask for a new brief").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_rebrief_request"), [json!({"id": "d1", "note": "add the cost of each"})]);
    h.get_by_label("Request a new brief: asked agent a2: sent once its turn is over");
}

#[test]
fn an_orchestrators_decision_shows_with_revert() {
    let (mut h, tx, calls) = i5_home(true, I5);
    let mut d = brief("d1", "billing", true);
    d["state"] = json!("answered");
    d["answered_by"] = json!("orchestrator");
    d["revertible"] = json!(true);
    d["answer"] = json!({"choice": "A", "label": "Exponential, cap 30 s", "reason": "3/30 time out with fixed",
                         "presence": "away", "at": crate::app::unix_now()});
    tx.send(event("decision.answered", json!({"decision": d.clone()}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.conn.fake.lock().unwrap().take();
    let calls2 = fake_engine(&h, move |m, _| match m {
        "decision_get" => Some(json!({"decision": d.clone()})),
        _ => None,
    });
    drop(calls);
    h.state_mut().state.open_decision("d1", "repo:billing");
    h.run_steps(3);
    h.get_by_label("decided by orchestrator: A (Exponential, cap 30 s) — 3/30 time out with fixed");
    assert!(h.query_by_label("Answer A  (Ctrl+Enter)").is_none(), "answered: no answer form");
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::U);
    h.run_steps(2);
    h.get_by_label("Revert").click();
    h.run_steps(2);
    assert_eq!(calls_of(&calls2, "decision_revert"), [json!({"id": "d1"})]);
}

#[test]
fn without_the_decision_view_feature_enter_still_expands_the_brief() {
    let (mut h, _tx, calls) = i5_home(true, I4);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert!(h.state().state.decision.is_none());
    h.get_by_label("Answer it in the agent's terminal (o) for now.");
    h.key_press(egui::Key::Y);
    h.run_steps(2);
    assert!(calls_of(&calls, "decision_explain_request").is_empty());
}

#[test]
fn explain_works_on_any_home_line_by_its_work_item() {
    let (mut h, tx, calls) = i5_home(true, I5);
    let mut row = inbox_row("billing", "round", "Ci:/r/repo-billing:7", "CI red: tests");
    row["agents"] = json!([{"id": "a2", "status": "waiting", "since": crate::app::unix_now()}]);
    tx.send(event("actionable.resolved", json!({"id": "Decision:d1"}))).unwrap();
    tx.send(event("actionable.created", json!({"item": ci_item("billing")}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    h.key_press(egui::Key::Y);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "decision_explain_request"), [json!({"key": "repo:billing"})]);
    tx.send(event(
        "decision.explain",
        json!({"explain": {
        "target": "key:repo:billing", "id": null, "key": "repo:billing", "kind": "explain", "route": "owner",
        "to": "agent:a2", "agent": "a2", "state": "answered", "asked_at": crate::app::unix_now(), "by": "agent:a2",
        "text": "The tests time out: the retry waits 2 s five times."}}),
    ))
    .unwrap();
    h.run_steps(2);
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    h.get_by_label("The tests time out: the retry waits 2 s five times.");
}

#[test]
fn i5_decision_screenshots() {
    let now = crate::app::unix_now();
    let (mut h, tx, _calls) = {
        let (tx, rx) = mpsc::channel();
        let state_dir = tempfile::tempdir().unwrap().keep();
        let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
        let conn = Conn::new("/home/dev/shop".into(), env, tx.clone(), egui::Context::default());
        let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
        state.capabilities = Some(I5.iter().map(|c| c.to_string()).collect());
        state.inbox_loaded = 3;
        let mut wt = worktree("/r/repo-billing", "billing", vec![agent("a2", "Implementer", "running", "s2")]);
        wt["head"] = json!("f00d123");
        let seed: SidebarState = serde_json::from_value(json!({
            "worktrees": [worktree("/home/dev/shop", "main", vec![]), wt],
            "orchestrator": {"agents": []}, "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
        }))
        .unwrap();
        tx.send(Msg::Link(Link::Connected)).unwrap();
        tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
        let h = Harness::builder()
            .with_size([1400.0, 900.0])
            .wgpu()
            .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
        (h, tx, ())
    };
    h.run_steps(2);
    let mut d = brief("d1", "billing", true);
    d["brief"]["evidence"] = json!([
        {"kind": "diff", "path": "src/retry.py", "hunk": "@@ -40,7 +40,12 @@"},
        {"kind": "log", "ref": "job:tests", "lines": "120-134"},
        {"kind": "file", "path": "docs/retry.md", "lines": "10-24"},
        {"kind": "notes", "section": "Doubts"},
        {"kind": "url", "url": "https://example.com/shop/actions/runs/7"}]);
    d["brief"]["options"][1]["consequence"] = json!("3/30 runs time out at the 10 s limit");
    d["explain"] = json!({"id": "d1", "key": "repo:billing", "kind": "explain", "route": "owner", "to": "agent:a2",
        "agent": "a2", "state": "answered", "asked_at": now - 120.0, "by": "agent:a2", "answered_at": now - 60.0,
        "text": "Fixed 2 s retries five times, so a slow upstream fails the run in 10 s.\n\
                 Exponential backoff (cap 30 s) rides out short outages but a hard outage takes ~1 min to fail.\n\
                 I recommend A: the outages we saw last under 20 s."});
    let dd = d.clone();
    fake_engine(&h, move |m, p| match m {
        "decision_get" => Some(json!({"decision": dd.clone()})),
        "status_timeline" => Some(json!({"key": p["key"], "since": 0, "seen_at": null, "entries": [
            {"at": now - 3000.0, "kind": "commit", "actor": "engine", "text": "retry: exponential backoff"},
            {"at": now - 1500.0, "kind": "ci", "actor": "engine", "text": "CI red: tests"},
            {"at": now - 600.0, "kind": "decision", "actor": "agent:a2",
             "text": "asked: Retries: fixed or exponential backoff?", "ref": "d1"},
            {"at": now - 120.0, "kind": "decision", "actor": "user", "text": "asked agent:a2 for explain", "ref": "d1"},
            {"at": now - 60.0, "kind": "decision", "actor": "agent:a2", "text": "explained", "ref": "d1"}]})),
        "get_diff" => Some(
            json!({"worktree_path": "/r/repo-billing", "diff": "diff --git a/src/retry.py b/src/retry.py\n--- a/src/retry.py\n+++ b/src/retry.py\n\
@@ -40,7 +40,12 @@ def retry(call):\n     for n in range(TRIES):\n         try:\n             return call()\n\
         except Timeout:\n-            sleep(2)\n+            delay = min(30, 2 ** n)\n+            sleep(delay + jitter())\n\
+            log.info(\"retry %d in %.1fs\", n, delay)\n     raise GaveUp(TRIES)\n", "truncated": false}),
        ),
        _ => None,
    });
    tx.send(event("decision.created", json!({"decision": d}))).unwrap();
    tx.send(event("actionable.created", json!({"item": brief_item("d1", "billing", "deep", true)}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.open_decision("d1", "repo:billing");
    h.run_steps(5);
    h.state_mut().state.toasts.clear();
    h.run_steps(2);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    h.get_by_label_contains("Outdated: the branch moved since the brief (abc123 → f00d123)");
    if !shot(&mut h, "i5-decision-dark") {
        return;
    }
    let ctx = h.ctx.clone();
    h.state_mut().state.toggle_theme(&ctx);
    h.run_steps(4);
    shot(&mut h, "i5-decision-light");
}

// --- Home, I4e: answers on the line, ages, keys, undo, counts, the
// orchestrator strip, labels ------------------------------------------------

const I4E: &[&str] = &[
    "status",
    "actionables",
    "claims",
    "gestures",
    "decisions",
    "holds",
    "delegation",
    "parked",
    "first_responder",
    "decision_view",
    crate::home_view::F_APPROVAL,
    crate::home_view::F_QUESTION,
    crate::home_view::F_UNDO,
    crate::home_view::F_ORCHESTRATOR,
    crate::home_view::F_CLOSE_STOP,
];

/// An inbox item of `kind` on work item `key`, its agent a1.
fn ask_item(key: &str, kind: &str, level: &str, detail: &str) -> serde_json::Value {
    let id = format!("{}:/r/repo-{key}/a1", if kind == "approval" { "Approval" } else { "Question" });
    json!({"id": id, "occurrence": format!("{id}@1"), "kind": kind, "level": level,
           "title": if kind == "approval" { "Needs approval" } else { "Question" }, "label": "Agent a1",
           "detail": detail, "state": format!("{}: {detail}", if kind == "approval" { "approve" } else { "answer" }),
           "work_item": format!("repo:{key}"), "worktree_path": format!("/r/repo-{key}"), "agent_id": "a1",
           "pr": null, "job_id": null, "exit_code": null, "blocks_agent": true, "priority": "medium",
           "since": crate::app::unix_now() - 120.0, "park": null})
}

/// Home with the I4e engine features; `answer` gives the fake engine's
/// replies (`{}` when None).
fn i4e_home(
    answer: impl FnMut(&str, &serde_json::Value) -> Option<serde_json::Value> + Send + 'static,
) -> (Harness<'static, App>, mpsc::Sender<Msg>, Calls) {
    let (app, tx) = engine_home(I4E);
    let h = harness(app);
    let calls = fake_engine(&h, answer);
    (h, tx, calls)
}

fn send_ask(tx: &mpsc::Sender<Msg>, key: &str, kind: &str, detail: &str) {
    let item = ask_item(key, kind, "now", detail);
    let mut row = inbox_row(key, "now", item["id"].as_str().unwrap(), item["state"].as_str().unwrap());
    row["agents"] = json!([{"id": "a1", "status": format!("waiting_for_{}", if kind == "approval" { "approval" } else { "user" }),
                            "since": crate::app::unix_now() - 120.0}]);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
}

#[test]
fn an_approval_is_answered_on_its_line_with_a_and_shift_d() {
    let (mut h, tx, calls) = i4e_home(|m, _| match m {
        "status_timeline" => Some(json!({"key": "repo:cart", "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    send_ask(&tx, "cart", "approval", "rm -rf build/ && npm run build -- --prod");
    h.run_steps(3);
    // Enter shows the whole command, readable, with Approve and Deny.
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    assert_eq!(h.state().state.view, crate::app::View::Home, "answered here, not in the terminal");
    h.get_by_label("rm -rf build/ && npm run build -- --prod");
    h.get_by_label("Approve  (a)");
    h.get_by_label("Deny  (⇧D)");
    // The bar names this line's keys only.
    let bar = text_of(&h.get_by_label_contains("? all keys"));
    assert!(bar.starts_with("a approve · ⇧D deny"), "{bar}");
    h.key_press(egui::Key::A);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "approval_answer"), [json!({"id": "Approval:/r/repo-cart/a1", "allow": true})]);
    send_ask(&tx, "cart", "approval", "npm publish");
    h.run_steps(2);
    // ⇧D waits 5 s for its Undo before the agent gets the deny.
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::D);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "approval_answer").len(), 1, "not sent yet");
    h.get_by_label_contains("Denying cart");
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Z);
    h.run_steps(2);
    h.state_mut().state.tick_deny(crate::app::unix_now() + 10.0);
    assert_eq!(calls_of(&calls, "approval_answer").len(), 1, "undone: never sent");
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::D);
    h.run_steps(2);
    h.state_mut().state.tick_deny(crate::app::unix_now() + 6.0);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "approval_answer")[1], json!({"id": "Approval:/r/repo-cart/a1", "allow": false}));
    // y and n mean one thing on every line: explain and notes.
    h.key_press(egui::Key::Y);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "approval_answer").len(), 2, "y never approves: it explains");
    h.key_press(egui::Key::N);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "approval_answer").len(), 2, "n never denies");
    assert!(h.state().state.notes_open, "n opens the notes");
}

#[test]
fn an_approval_whose_prompt_is_gone_says_so() {
    let (app, tx) = engine_home(I4E);
    let mut h = harness(app);
    *h.state().state.conn.fake.lock().unwrap() = Some(Box::new(|method, _| {
        Some(match method {
            "approval_answer" => Err(lazyagent_proto::ClientError::Rpc(lazyagent_proto::RpcError {
                code: "VALIDATION_ERROR".into(),
                message: "The permission prompt is not on agent a1's screen: open its terminal (o)".into(),
                data: None,
            })),
            _ => Ok(json!({})),
        })
    }));
    h.run_steps(2);
    send_ask(&tx, "cart", "approval", "npm test");
    h.run_steps(3);
    h.key_press(egui::Key::A);
    h.run_steps(2);
    assert!(
        h.state().state.toasts.iter().any(|t| t.text.contains("not on agent a1's screen")),
        "{:?}",
        h.state().state.toasts.iter().map(|t| t.text.clone()).collect::<Vec<_>>()
    );
}

#[test]
fn without_the_engine_feature_an_approval_still_opens_its_terminal() {
    let caps: Vec<&str> = I4E.iter().copied().filter(|c| *c != crate::home_view::F_APPROVAL).collect();
    let (app, tx) = engine_home(&caps);
    let mut h = harness(app);
    let calls = fake_engine(&h, |_, _| None);
    h.run_steps(2);
    send_ask(&tx, "cart", "approval", "npm test");
    h.run_steps(3);
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    assert_eq!(h.state().state.view, crate::app::View::Worktrees);
    assert!(calls_of(&calls, "approval_answer").is_empty());
}

#[test]
fn a_question_is_answered_in_a_field_under_its_line() {
    let (mut h, tx, calls) = i4e_home(|m, _| match m {
        "status_timeline" => Some(json!({"key": "repo:search", "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    send_ask(&tx, "search", "question", "Should the index include archived products?");
    h.run_steps(3);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.get_by_label("Should the index include archived products?");
    h.event(egui::Event::Text("No, only live ones".into()));
    h.run_steps(1);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "question_answer"),
        [json!({"key": "repo:search", "id": "Question:/r/repo-search/a1", "text": "No, only live ones"})]
    );
    assert!(h.state().state.toasts.iter().any(|t| t.text == "Sent to agent a1 once its turn is over."));
}

#[test]
fn an_expanded_brief_takes_1_to_4_and_ctrl_enter() {
    let (mut h, tx, calls) = i4e_home(|m, p| match m {
        "decision_answer" => {
            let mut d = brief("d1", "billing", true);
            (d["state"], d["answered_by"]) = (json!("answered"), json!("user"));
            d["answer"] = json!({"choice": p["choice"], "at": crate::app::unix_now()});
            Some(json!({"decision": d}))
        }
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    tx.send(event("decision.created", json!({"decision": brief("d1", "billing", true)}))).unwrap();
    tx.send(event("actionable.created", json!({"item": brief_item("d1", "billing", "deep", true)}))).unwrap();
    tx.send(event("status.updated", json!({"row": inbox_row("billing", "deep", "Decision:d1", "decide")}))).unwrap();
    tx.send(event("status.updated", json!({"row": status_row("cart", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(3);
    h.state_mut().state.home.expanded = Some("repo:billing".into());
    h.state_mut().state.home.selected = Some("repo:billing".into());
    h.run_steps(2);
    h.get_by_label("Answer A  (Ctrl+Enter)");
    // 2 picks B (it does not jump to the Round).
    h.key_press(egui::Key::Num2);
    h.run_steps(2);
    assert_eq!(h.state().state.home.selected.as_deref(), Some("repo:billing"));
    h.get_by_label("Answer B  (Ctrl+Enter)");
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_answer"), [json!({"id": "d1", "choice": "B", "comment": ""})]);
    // Collapsed, 4 jumps to In flight again.
    h.key_press(egui::Key::Escape);
    h.run_steps(1);
    assert!(h.state().state.home.expanded.is_none(), "Esc collapses the expanded line");
    h.key_press(egui::Key::Num4);
    h.run_steps(1);
    assert_eq!(h.state().state.home.selected.as_deref(), Some("repo:cart"));
}

#[test]
fn zone_jumps_tab_and_esc() {
    let (mut h, tx, _calls) = i4e_home(|m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    send_ask(&tx, "cart", "approval", "npm test");
    let mut red = status_row("tax", "inbox", Some("round"), "you", "CI red: tests", 300.0);
    red["rule"] = json!(5);
    tx.send(event("status.updated", json!({"row": red}))).unwrap();
    tx.send(event("status.updated", json!({"row": status_row("search", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(1);
    let selected = |h: &Harness<'static, App>| h.state().state.home.selected.clone().unwrap_or_default();
    h.key_press(egui::Key::Num4);
    h.run_steps(1);
    assert_eq!(selected(&h), "repo:search");
    h.key_press(egui::Key::Num2);
    h.run_steps(1);
    assert_eq!(selected(&h), "repo:tax");
    h.key_press(egui::Key::Num3);
    h.run_steps(1);
    assert!(h.state().state.toasts.iter().any(|t| t.text == "Nothing in To sit down with."));
    h.key_press(egui::Key::Tab);
    h.run_steps(1);
    assert_eq!(selected(&h), "repo:cart", "Tab: the next that needs you, wrapping");
    h.key_press(egui::Key::Tab);
    h.run_steps(1);
    assert_eq!(selected(&h), "repo:tax");
    h.key_press(egui::Key::Enter);
    h.run_steps(1);
    assert_eq!(h.state().state.home.expanded.as_deref(), Some("repo:tax"));
    h.key_press(egui::Key::Escape);
    h.run_steps(1);
    assert!(h.state().state.home.expanded.is_none());
}

#[test]
fn the_hint_bar_is_pinned_to_the_bottom_with_shifted_keys() {
    let (mut h, tx, _calls) = i4e_home(|_, _| None);
    h.run_steps(2);
    for i in 0..60 {
        let key = format!("b{i:02}");
        tx.send(event("status.updated", json!({"row": status_row(&key, "in_flight", None, "agent", "Edit", 60.0)})))
            .unwrap();
    }
    h.run_steps(3);
    // The selected line's keys only, on one line, then "? all keys".
    let bar = h.get_by_label_contains("? all keys");
    let text = text_of(&bar);
    for key in ["⇧X close", "o terminal", "n notes"] {
        assert!(text.contains(key), "{key} in {text}");
    }
    assert!(text.split(" · ").count() <= crate::home_view::BAR_KEYS + 1, "{text}");
    assert!(!text.contains("⇧U") && !text.contains("⇧M"), "not this line's: {text}");
    let bottom = bar.rect().max.y;
    assert!(bottom > 880.0 - 60.0, "the bar stays at the bottom with a long list: {bottom}");
}

#[test]
fn undo_takes_back_a_dismiss_a_park_a_delegation_and_a_pass() {
    let (mut h, tx, calls) = i4e_home(|m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "kept": false, "row": null})),
        "actionables_mark" => Some(json!({"item": ask_item("cart", "approval", "now", "npm test")})),
        _ => None,
    });
    h.state_mut().state.store.orchestrator =
        vec![serde_json::from_value(agent("", "Orchestrator", "waiting", "so")).unwrap()];
    h.run_steps(2);
    send_ask(&tx, "cart", "approval", "npm test");
    h.run_steps(3);
    h.key_press(egui::Key::X);
    h.run_steps(2);
    h.get_by_label("Dismissed cart");
    h.get_by_label("Undo  (Ctrl+Z)").click();
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "actionables_mark")[1],
        json!({"id": "Approval:/r/repo-cart/a1", "action": "undismiss"})
    );
    // A park of the item: unpark. Ctrl+Z works as the button.
    let line = h.state().state.home.lines["repo:cart"].clone();
    h.state_mut().state.park_line(&line, crate::home_view::Park::For(3600.0), false);
    h.run_steps(2);
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Z);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "actionables_mark")[3], json!({"id": "Approval:/r/repo-cart/a1", "action": "unpark"}));
    // A delegation and a pass: the engine's undo of just that.
    let mut red = status_row("tax", "inbox", Some("round"), "you", "CI red: tests", 300.0);
    red["needs_you"] = json!({"count": 1, "top": "Ci:/r/repo-tax:7"});
    red["agents"] = json!([{"id": "a3", "status": "waiting", "since": 0.0}]);
    let mut ci = ci_item("tax");
    ci["id"] = json!("Ci:/r/repo-tax:7");
    tx.send(event("actionable.created", json!({"item": ci}))).unwrap();
    tx.send(event("status.updated", json!({"row": red}))).unwrap();
    h.run_steps(2);
    let line = h.state().state.home.lines["repo:tax"].clone();
    h.state_mut().state.delegate_line(&line);
    h.run_steps(2);
    h.get_by_label("Delegated tax to its agent");
    h.get_by_label("Undo  (Ctrl+Z)").click();
    h.run_steps(2);
    let gestures = calls_of(&calls, "status_gesture");
    assert_eq!(gestures.last().unwrap(), &json!({"key": "repo:tax", "kind": "undo", "what": "delegate"}));
    h.state_mut().state.pass_line(&line);
    h.run_steps(2);
    h.get_by_label("Passed tax to the orchestrator");
    // After 5 s the Undo is gone.
    h.state_mut().state.undo.as_mut().unwrap().at -= crate::home_view::UNDO_FOR;
    h.run_steps(2);
    assert!(h.query_by_label("Undo  (Ctrl+Z)").is_none());
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Z);
    h.run_steps(2);
    assert!(!calls_of(&calls, "status_gesture").iter().any(|p| p["what"] == "pass"), "too late to undo");
}

#[test]
fn shift_x_says_what_it_closes_and_stops_the_agent_only() {
    let (mut h, tx, calls) = i4e_home(|m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "kept": false, "row": null,
                                        "stopped": ["a1"]})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    let mut row = status_row("cart", "in_flight", None, "agent", "working", 60.0);
    row["agents"] = json!([{"id": "a1", "status": "running", "since": crate::app::unix_now() - 300.0}]);
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(2);
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::X);
    h.run_steps(2);
    h.get_by_label(
        "Close this work item: it leaves Home after a day. Agent a1 is stopped; its notes are kept. The worktree, the \
         branch and the PR are not touched.",
    );
    assert!(calls_of(&calls, "status_gesture").is_empty(), "nothing before the confirmation");
    h.get_by_label("Close as done and stop").click();
    h.run_steps(2);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:cart", "kind": "close", "how": "done", "stop_agents": true})]
    );
    assert_eq!(
        crate::home_view::close_text(&["a1".into()], false),
        "Close this work item: it leaves Home after a day. Its agents keep running (this engine does not stop them). \
         The worktree, the branch and the PR are not touched."
    );
}

#[test]
fn a_first_launch_shows_no_dots_and_marks_what_was_never_looked_at_once() {
    let (mut h, tx, calls) = i4e_home(|_, _| None);
    h.run_steps(2);
    let mut fresh = status_row("cart", "in_flight", None, "agent", "Edit", 60.0);
    fresh["last_event_at"] = json!(crate::app::unix_now() - 30.0);
    let mut looked = status_row("tax", "in_flight", None, "agent", "Edit", 60.0);
    (looked["last_event_at"], looked["seen_at"]) =
        (json!(crate::app::unix_now() - 30.0), json!(crate::app::unix_now() - 600.0));
    tx.send(event("status.updated", json!({"row": fresh.clone()}))).unwrap();
    tx.send(event("status.updated", json!({"row": looked}))).unwrap();
    h.run_steps(3);
    let seen = calls_of(&calls, "status_seen");
    assert_eq!(seen.len(), 1, "{seen:?}");
    assert_eq!(seen[0]["key"], "repo:cart");
    let home = &h.state().state.home;
    assert!(!home.lines["repo:cart"].unseen(0.0), "no dot for what was never looked at");
    assert!(home.lines["repo:tax"].unseen(0.0), "a real change since a real look keeps its dot");
    // Once: the engine's next row (still null, say) is not marked again.
    tx.send(event("status.updated", json!({"row": fresh}))).unwrap();
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "status_seen").len(), 1);
}

#[test]
fn a_working_line_reads_its_age_and_report_and_held_lines_their_last_step() {
    let (mut h, tx, _calls) = i4e_home(|_, _| None);
    h.run_steps(2);
    let now = crate::app::unix_now();
    let mut working = status_row("cart", "in_flight", None, "agent", "mcp__lazyagent_app__notes_append done", 60.0);
    working["agents"] = json!([{"id": "a1", "status": "running", "since": now - 720.0}]);
    working["note"] = json!({"text": "tests pass; opening the PR", "by": "agent:a1", "at": now - 60.0});
    let mut held = status_row("search", "in_flight", None, "agent", "orchestrator · verifying tests", 600.0);
    held["rule"] = json!(15);
    held["holder"] = json!("orchestrator");
    held["hold"] = json!({"key": "repo:search", "holder": "orchestrator", "at": now - 600.0, "until": now + 3000.0,
                          "by": "orchestrator", "note": "verifying tests", "auto": false});
    held["last_step"] = json!({"at": now - 240.0, "kind": "ci", "actor": "engine", "text": "CI green"});
    let mut delegated = status_row("tax", "in_flight", None, "agent", "agent a3 · fixing CI red: tests", 60.0);
    delegated["rule"] = json!(16);
    delegated["holder"] = json!("agent");
    let mut input = status_row("pdf", "in_flight", None, "agent", "waits: waiting_for_input", 60.0);
    input["rule"] = json!(12);
    for r in [working, held, delegated, input] {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    h.run_steps(3);
    // Its report, and its age once, in the age column.
    h.get_by_label("working · tests pass; opening the PR");
    assert!(h.query_by_label_contains("notes_append").is_none(), "never a tool name");
    h.get_by_label("verifying tests · 50m left");
    h.get_by_label("last step: CI green (4m ago)");
    h.get_by_label("agent a3 · fixing CI red: tests");
    h.get_by_label("waits: waiting for input");
    assert!(h.query_all_by_label("orchestrator").count() >= 1);
}

#[test]
fn the_orchestrator_strip_says_what_it_does_holds_and_did_today() {
    let now = crate::app::unix_now();
    let (mut h, tx, calls) = i4e_home(move |m, p| match m {
        "orchestrator_actions" => Some(json!({"actions": [
            {"key": "repo:billing", "at": now - 300.0, "kind": "decision", "action": "answer_reversible",
             "text": "decided by orchestrator (away): Retries? → B", "reason": "fixed is enough here",
             "mandate": "away", "decision_id": "d1", "revertible": true},
            {"key": "repo:cart", "at": now - 900.0, "kind": "action", "action": "ci_rerun", "text": "re-ran CI",
             "reason": null, "mandate": "present", "decision_id": null, "revertible": false}]})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    let s = &mut h.state_mut().state;
    s.status_counts.auto_actions = Some(2);
    s.status_counts.auto_reverted = Some(0);
    s.presence =
        Some(lazyagent_proto::types::Presence { mode: Some("auto".into()), effective: "present".into(), since: None });
    s.orchestrator = Some(serde_json::from_value(json!({"alive": true,
        "report": {"note": "verifying cart's tests", "at": now - 120.0},
        "queue": [{"key": "repo:search", "kind": "hold", "note": "waiting on your merge", "until": now + 1800.0,
                   "by": "orchestrator", "auto": false},
                  {"key": "repo:tax", "kind": "pass", "note": null, "until": now + 3600.0, "by": "user", "auto": false}]}))
    .unwrap());
    h.run_steps(2);
    let mut d = brief("d1", "billing", true);
    (d["answered_by"], d["revertible"], d["state"]) = (json!("orchestrator"), json!(true), json!("answered"));
    d["answer"] = json!({"choice": "B", "reason": "fixed is enough here"});
    d["closed_at"] = json!(now - 300.0);
    d["inverse"] = json!([{"kind": "git_reset", "sha": "abc1234def", "branch": "billing"}]);
    tx.send(event("decision.answered", json!({"decision": d}))).unwrap();
    tx.send(event("status.updated", json!({"row": status_row("billing", "in_flight", None, "agent", "Edit", 60.0)})))
        .unwrap();
    h.run_steps(3);
    h.get_by_label("verifying cart's tests · 2m ago");
    h.get_by_label("Queue: search · waiting on your merge · 30m left  ·  tax · passed · 1h 00m left");
    h.get_by_label("Presence: auto (present)");
    h.key_press(egui::Key::T);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "orchestrator_actions").len(), 1);
    h.get_by_label_contains("re-ran CI");
    h.get_by_label("Revert…").click();
    h.run_steps(2);
    assert_eq!(h.state().state.home.expanded.as_deref(), Some("repo:billing"));
    h.get_by_label(
        "Revert the orchestrator's B: agent a2 is told your override, and the branch is reset to abc1234 if nothing \
         was pushed since.",
    );
}

#[test]
fn labels_are_imperatives_items_are_counted_and_since_you_looked_says_nothing_new() {
    let (mut h, tx, _calls) = i4e_home(|m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    let item = ask_item("cart", "approval", "now", "npm test");
    let mut row = inbox_row("cart", "now", item["id"].as_str().unwrap(), "approve: npm test");
    row["needs_you"] = json!({"count": 2, "top": item["id"]});
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    let mut nobody = status_row("pdf", "inbox", Some("round"), "you", "PR has no reviewer", 600.0);
    nobody["rule"] = json!(10);
    nobody["urls"] = json!(["https://example.com/repo/pull/16"]);
    tx.send(event("status.updated", json!({"row": nobody}))).unwrap();
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    h.get_by_label("2 items");
    assert!(h.query_by_label("+1").is_none());
    h.get_by_label("Request reviewer");
    // Expanded: the short name is not repeated, nothing new, at most six
    // buttons and the rest under More….
    h.state_mut().state.home.selected = Some("repo:pdf".into());
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.get_by_label("nothing new");
    assert_eq!(h.query_all_by_label("pdf").count(), 1, "the branch once, on the line");
    h.get_by_label("More…").click();
    h.run_steps(2);
    h.get_by_label("Close…  (⇧X)");
}

#[test]
fn the_auto_delegation_menu_is_wide_enough_to_wrap_by_words() {
    let (mut h, _tx, _calls) = i4e_home(|m, _| match m {
        "inbox_config_get" => Some(json!({"config": {"repo": "repo", "auto_delegate": ["ci_red"],
                                                     "auto_delegate_target": "owner"}})),
        _ => None,
    });
    h.run_steps(2);
    h.get_by_label("Auto-delegation ▾").click();
    h.run_steps(3);
    let first = h.get_by_label_contains("When one of these stalls in");
    assert!(first.rect().width() >= 200.0, "{:?}", first.rect());
    let menu = h.get_by_label("Conflict with the base branch");
    assert!(menu.rect().width() > 120.0, "a checkbox label is not a column of letters: {:?}", menu.rect());
    // The offscreen render, for the record (no GPU: skipped).
    let _ = h.render();
}

/// Home after I4e, in dark and light at 1600 and 1100 points wide, for
/// docs/design/desktop-screenshots/i4e-*.png: the orchestrator strip with
/// its queue and today's actions, an approval answered on its line, a
/// question's field, the Round with the next one dimmed under it and the
/// parked chip, a working line with its report, a held line's last step.
#[test]
fn home_i4e_screenshots() {
    let now = crate::app::unix_now();
    let w = |b: &str| format!("/home/dev/shop-{b}");
    for (width, tag) in [(1600.0, "1600"), (1100.0, "1100")] {
        let (tx, rx) = mpsc::channel();
        let state_dir = tempfile::tempdir().unwrap().keep();
        let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
        let conn = Conn::new("/home/dev/shop".into(), env, tx.clone(), egui::Context::default());
        let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
        state.capabilities = Some(I4E.iter().map(|c| c.to_string()).collect());
        state.look_from = now - 1800.0;
        state.presence = Some(lazyagent_proto::types::Presence {
            mode: Some("auto".into()),
            effective: "present".into(),
            since: None,
        });
        state.status_counts.auto_actions = Some(3);
        state.status_counts.auto_reverted = Some(0);
        state.status_counts.not_started = 2;
        state.status_counts.deep_max = 5;
        state.orchestrator = Some(
            serde_json::from_value(json!({"alive": true,
                "report": {"note": "verifying search-filters: running the unit tests", "at": now - 120.0},
                "queue": [{"key": "shop:search-filters", "kind": "hold", "note": "verifying tests", "until": now + 3000.0,
                           "by": "orchestrator", "auto": false},
                          {"key": "shop:cart-discount", "kind": "pass", "note": null, "until": now + 6600.0, "by": "user",
                           "auto": false}]}))
            .unwrap(),
        );
        state.orchestrator_actions = Some(
            serde_json::from_value(json!([
                {"key": "shop:billing-retries", "at": now - 300.0, "kind": "decision", "action": "answer_quick",
                 "text": "decided: retries → B", "reason": "fixed is enough here", "mandate": "present",
                 "decision_id": "d9", "revertible": true},
                {"key": "shop:stock-sync", "at": now - 1500.0, "kind": "action", "action": "ci_rerun",
                 "text": "re-ran CI", "reason": null, "mandate": "present", "decision_id": null, "revertible": false}]))
            .unwrap(),
        );
        let branches = [
            "cart-discount",
            "billing-retries",
            "search-filters",
            "invoice-pdf",
            "order-export",
            "stock-sync",
            "docs-index",
            "tax-rules",
            "refunds",
        ];
        let trees: Vec<_> = std::iter::once(worktree("/home/dev/shop", "main", vec![]))
            .chain(branches.iter().map(|b| worktree(&w(b), b, vec![])))
            .collect();
        let seed: SidebarState = serde_json::from_value(json!({
            "worktrees": trees, "orchestrator": {"agents": [agent("", "Orchestrator", "waiting", "so")]},
            "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
        }))
        .unwrap();
        tx.send(Msg::Link(Link::Connected)).unwrap();
        tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
        let row = |b: &str, zone: &str, level: Option<&str>, ball: &str, st: &str, ago: f64| {
            let mut r = status_row(b, zone, level, ball, st, ago);
            r["key"] = json!(format!("shop:{b}"));
            r["repo"] = json!("shop");
            r["worktree_path"] = json!(w(b));
            (r["seen_at"], r["last_event_at"]) = (json!(now - 1800.0), json!(now - ago.min(3600.0) * 0.5));
            r
        };
        let item = |b: &str, kind: &str, detail: &str| {
            let mut i = ask_item(b, kind, "now", detail);
            let id = format!("{}:{}/a1", if kind == "approval" { "Approval" } else { "Question" }, w(b));
            (i["id"], i["work_item"], i["worktree_path"]) = (json!(id), json!(format!("shop:{b}")), json!(w(b)));
            i
        };
        let approval = item("invoice-pdf", "approval", "rm -rf build/ && npm run build -- --prod && npm test");
        let mut approve = row("invoice-pdf", "inbox", Some("now"), "you", "approve: rm -rf build/", 120.0);
        approve["needs_you"] = json!({"count": 1, "top": approval["id"]});
        approve["agents"] = json!([{"id": "a1", "status": "waiting_for_approval", "since": now - 120.0}]);
        let question = item("refunds", "question", "Refund the shipping fee too, or only the items?");
        let mut ask = row("refunds", "inbox", Some("now"), "you", "answer: refund shipping?", 300.0);
        ask["needs_you"] = json!({"count": 1, "top": question["id"]});
        let mut red = row("tax-rules", "inbox", Some("round"), "you", "CI red: tests", 900.0);
        red["rule"] = json!(5);
        let mut nobody = row("order-export", "inbox", Some("round"), "you", "PR has no reviewer", 2400.0);
        nobody["rule"] = json!(10);
        let mut working = row("stock-sync", "in_flight", None, "agent", "working", 700.0);
        working["agents"] = json!([{"id": "a4", "status": "running", "since": now - 720.0}]);
        working["note"] = json!({"text": "tests pass; opening the PR", "by": "agent:a4", "at": now - 60.0});
        let mut held = row("search-filters", "in_flight", None, "agent", "orchestrator · verifying tests", 600.0);
        (held["rule"], held["holder"]) = (json!(15), json!("orchestrator"));
        held["hold"] = json!({"key": "shop:search-filters", "holder": "orchestrator", "at": now - 600.0,
                              "until": now + 3000.0, "by": "orchestrator", "note": "verifying tests", "auto": false});
        held["last_step"] = json!({"at": now - 240.0, "kind": "ci", "actor": "engine", "text": "CI green"});
        let mut passed = row("cart-discount", "in_flight", None, "agent", "orchestrator · looking", 600.0);
        (passed["rule"], passed["holder"]) = (json!(13), json!("orchestrator"));
        passed["claim"] = json!({"key": "shop:cart-discount", "holder": "orchestrator", "at": now - 600.0,
                                 "until": now + 6600.0, "by": "user", "allowed": ["answer_quick", "ci_rerun"]});
        let mut delegated =
            row("billing-retries", "in_flight", None, "agent", "agent a2 · fixing conflict with base", 400.0);
        (delegated["rule"], delegated["holder"]) = (json!(16), json!("agent"));
        let mut stalled = row("docs-index", "in_flight", None, "stalled", "no activity", 3.0 * 86400.0);
        stalled["rule"] = json!(11);
        let mut parked = row("cart-coupons", "parked", None, "you", "parked until something changes", 900.0);
        parked["rule"] = json!(7);
        parked["gesture"] = json!({"kind": "park", "at": now - 900.0, "by": "user", "until": "change"});
        parked["needs_you"] = json!({"count": 1, "top": "Question:/home/dev/shop-cart-coupons/a5"});
        let events = vec![
            event("actionable.created", json!({"item": approval})),
            event("actionable.created", json!({"item": question})),
        ];
        let rows = vec![approve, ask, red, working, held, passed, delegated, stalled, parked];
        let mut h = Harness::builder()
            .with_size([width, 980.0])
            .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
        h.run_steps(2);
        fake_engine(&h, |m, p| match m {
            "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
            _ => None,
        });
        for e in events {
            tx.send(e).unwrap();
        }
        for r in rows {
            tx.send(event("status.updated", json!({"row": r}))).unwrap();
        }
        h.run_steps(3);
        h.state_mut().state.inbox_loaded = 3;
        h.state_mut().state.inbox_gen += 1;
        h.run_steps(2);
        h.state_mut().state.home.cut(now);
        // One more for the next round, after this one was delivered.
        tx.send(event("status.updated", json!({"row": nobody}))).unwrap();
        h.run_steps(2);
        let s = &mut h.state_mut().state;
        s.home.strip_open = true;
        s.home.selected = Some("shop:invoice-pdf".into());
        s.home.expanded = Some("shop:invoice-pdf".into());
        s.undo = Some(crate::home_view::Undo {
            text: "Dismissed tax-rules".into(),
            act: crate::home_view::UndoAct::Undismiss("x".into()),
            at: now - 1.0,
        });
        h.run_steps(4);
        h.state_mut().state.toasts.clear();
        h.run_steps(2);
        h.get_by_label("Round (+1 next)");
        h.get_by_label("1 parked needs you ▸");
        h.get_by_label("Approve  (a)");
        // The list stops at CONTENT_MAX: the age sits near its state.
        let left = h.get_by_label("In flight").rect().min.x;
        let age = h.get_all_by_label("11m").map(|n| n.rect().max.x).fold(0.0_f32, f32::max);
        assert!(age > left && age - left <= crate::home_view::CONTENT_MAX + 20.0, "{age} vs {left}");
        let out = std::env::temp_dir();
        let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
            Ok(image) => {
                image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
                true
            }
            Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
            Err(_) => false,
        };
        if !shot(&mut h, &format!("i4e-home-dark-{tag}")) {
            return;
        }
        let ctx = h.ctx.clone();
        h.state_mut().state.toggle_theme(&ctx);
        h.run_steps(4);
        shot(&mut h, &format!("i4e-home-light-{tag}"));
        if tag == "1600" {
            // The auto-delegation menu, open: wide enough for its sentences.
            fake_engine(&h, |m, _| match m {
                "inbox_config_get" => Some(json!({"config": {"repo": "shop", "auto_delegate": ["ci_red"],
                                                             "auto_delegate_target": "owner"}})),
                _ => None,
            });
            h.get_by_label("Auto-delegation ▾").click();
            h.run_steps(4);
            let label = h.get_by_label_contains("When one of these stalls in");
            assert!(label.rect().width() >= 200.0, "{:?}", label.rect());
            shot(&mut h, "i4e-auto-delegation-light");
        }
    }
}

// --- Home, rules (proposal 0014 R2): the error line -------------------------

#[test]
fn home_names_a_broken_rule_by_its_file_and_line() {
    let problem = json!({"scope": "global", "file": "/home/u/.config/lazyagent/rules.toml", "line": 12,
                         "rule": "merge", "message": "rule merge when: no fact 'tracker.qa'",
                         "fallback": "skipped; the built-in is in force"});
    let mut caps = I4E.to_vec();
    caps.push("rules");
    let (app, tx) = engine_home(&caps);
    let mut h = harness(app);
    let answer = problem.clone();
    let calls = fake_engine(&h, move |m, _| {
        (m == "rules_check").then(|| json!({"errors": [answer.clone()], "warnings": [], "files": []}))
    });
    h.state_mut().state.fetch_inbox();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "rules_check").len(), 1);
    let text = "Rules: 1 error · your global rules.toml, line 12: rule merge: when: no fact 'tracker.qa' — skipped; \
                the built-in is in force";
    h.get_by_label(text);
    // A second error in the repo's file: counted, the first still named.
    let repo = json!({"scope": "repo", "file": "/r/repo/.lazyagent/rules.toml", "line": null, "rule": null,
                      "message": "not allowed yet: check it, then lazyagent rules allow 0123456789ab",
                      "fallback": "not applied"});
    tx.send(event("rules.errors", json!({"errors": [problem, repo]}))).unwrap();
    h.run_steps(3);
    assert!(h.query_by_label(text).is_none());
    h.get_by_label(&text.replace("1 error", "2 errors"));
    // Fixed: the line goes.
    tx.send(event("rules.errors", json!({"errors": []}))).unwrap();
    h.run_steps(3);
    assert!(h.query_by_label_contains("Rules:").is_none());
}

#[test]
fn without_the_rules_feature_home_does_not_ask() {
    let (app, _tx) = engine_home(I4E);
    let h = harness(app);
    let calls = fake_engine(&h, |_, _| None);
    let mut h = h;
    h.state_mut().state.fetch_inbox();
    h.run_steps(2);
    assert!(calls_of(&calls, "rules_check").is_empty());
}

#[test]
fn a_rule_item_shows_its_rules_verb() {
    let (mut h, tx, _calls) = i4e_home(|_, _| None);
    h.run_steps(2);
    let item = json!({"id": "Rule:merge:repo:docs", "occurrence": "Rule:merge:repo:docs@1", "kind": "rule",
                      "level": "round", "title": "Merge", "label": "#14 Index the docs", "detail": "",
                      "state": "merge: approved, CI green", "work_item": "repo:docs", "worktree_path": "/r/repo-docs",
                      "agent_id": null, "pr": {"number": 14, "title": "Index the docs", "url": "https://example.com/pull/14"},
                      "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
                      "since": crate::app::unix_now() - 60.0, "park": null, "rule_id": "merge", "verb": "Merge"});
    let mut row = inbox_row("docs", "round", "Rule:merge:repo:docs", "merge: approved, CI green");
    row["rule"] = json!(2);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    let lines = h.state().state.home.shown_in(crate::home::Zone::Round);
    assert_eq!(lines.iter().map(|l| l.verb().to_string()).collect::<Vec<_>>(), ["Merge"]);
}

// "How lazyagent works", the Why? view and their screenshots (R6).
mod how;

// The permission mode agents are spawned in (A1).
mod a1;

// "Settings…": the default permission mode, global or this repo (D1).
mod d1;

// Home fixes from the second UX review (I4f).
mod i4f;

// I4g: ghost lines as keyboard stops, every brief answerable on its line,
// the auto-delegation skip list.
mod i4g;

// I4h: an agent's question at the end of its turn leads its line; Delegate
// and Pass only where an agent or the orchestrator can act.
mod i4h;

// I4i: Delegate all, a held letter and Send anyway, Why? and Notes under More….
mod i4i;

// "Prompts…": the agent prompts page and its screenshots.
mod prompts;

// CI red but unrelated to the branch: the ci_note chip (proposal 0014 R4).
mod r4;

// Attestations for nondeterministic checks: the Confirm item, the chip,
// Why? with Revoke (proposal 0014 R5).
mod r5;

// The owner's approval of a PR head outside GitHub: ⇧A, Undo, Why? with
// Revoke (proposal 0014 R3d).
mod r3d;

// Review requests: a teammate's PR that asks for the user's review.
mod review_requests;

// I4j: opt-in row fields kept across row updates, what to review on "new
// commits since your approval", Delegate through a restore or a spawn.
mod i4j;
