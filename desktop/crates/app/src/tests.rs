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
    step_until(&mut h, "comments cleared", |a| a.state.comments(&world.repo).is_empty());
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
    let parsed = crate::diff::parse(text);
    let mut c = crate::diff::anchor("src/login.py", &parsed.files[0].rows[5]).unwrap();
    c.text = "the TTL should come from settings".into();
    {
        let s = &mut h.state_mut().state;
        s.selected = Some("/r/repo-login".into());
        s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo-login", "main", parsed));
        s.diff_open = true;
        s.set_comments("/r/repo-login", &[c]);
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
