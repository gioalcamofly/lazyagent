//! The permission mode agents are spawned in (`[agents] permission_mode`):
//! the spawn and create dialogs' Skip permissions starts checked when the
//! configured mode is bypass and is sent as skip_permissions, quick spawns
//! send no mode (the engine's config applies), and a tab names a mode other
//! than the CLI's own default.

use super::*;
use crate::dialogs::{Kind, configured_mode, skip_by_default};
use lazyagent_proto::types::ConfigView;

const FEAT: &str = "/r/repo-f";

fn config(provider: &str, mode: Option<&str>) -> ConfigView {
    ConfigView {
        agent_provider: provider.into(),
        default_branch: "main".into(),
        has_custom_create: false,
        has_custom_remove: false,
        permission_mode: mode.map(str::to_string),
    }
}

fn with_config(mode: Option<&str>) -> Harness<'static, App> {
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![]), worktree(FEAT, "f", vec![])]);
    let mut h = harness(app);
    h.run_steps(2);
    h.state_mut().state.store.config = Some(config("claude", mode));
    h.state_mut().state.selected = Some(FEAT.into());
    h.run_steps(1);
    h
}

#[test]
fn skip_permissions_starts_checked_only_when_bypass_is_configured() {
    assert_eq!(configured_mode(None), "cli");
    assert_eq!(configured_mode(Some(&config("claude", None))), "cli", "an older engine, or none set");
    assert_eq!(configured_mode(Some(&config("claude", Some("plan")))), "plan");
    assert!(!skip_by_default(None));
    assert!(!skip_by_default(Some(&config("claude", Some("plan")))));
    assert!(skip_by_default(Some(&config("codex", Some("bypass")))));
}

#[test]
fn the_spawn_dialog_sends_its_checkbox() {
    for (configured, click, sent) in [
        (Some("bypass"), false, true),
        (Some("bypass"), true, false),
        (Some("acceptEdits"), false, false),
        (None, true, true),
    ] {
        let mut h = with_config(configured);
        let calls =
            fake_engine(&h, |m, _| (m == "spawn_agent").then(|| json!({"agent_id": "a1", "worktree_path": FEAT})));
        h.state_mut().state.open_dialog(Kind::Spawn);
        h.run_steps(3);
        assert_eq!(h.state().state.dialog.as_ref().unwrap().skip, configured == Some("bypass"), "{configured:?}");
        if click {
            h.get_by_label("Skip permissions (d)").click();
            h.run_steps(2);
        }
        h.get_by_label("Spawn").click();
        h.run_steps(3);
        let spawn = calls_of(&calls, "spawn_agent");
        assert_eq!(spawn.len(), 1);
        assert_eq!(spawn[0]["worktree_path"], FEAT);
        assert_eq!(spawn[0]["skip_permissions"], sent, "{configured:?} click {click}");
        assert!(spawn[0].get("permission_mode").is_none(), "{}", spawn[0]);
    }
}

#[test]
fn the_spawn_dialogs_keys_pick_skip_or_not() {
    for (key, configured, sent) in
        [(egui::Key::D, Some("plan"), true), (egui::Key::N, Some("bypass"), false), (egui::Key::N, None, false)]
    {
        let mut h = with_config(configured);
        let calls = fake_engine(&h, |_, _| None);
        h.state_mut().state.open_dialog(Kind::Spawn);
        h.run_steps(3);
        // Out of the instruction field: the keys are the dialog's.
        h.ctx.memory_mut(|m| m.stop_text_input());
        h.run_steps(1);
        h.key_press(key);
        h.run_steps(3);
        let spawn = calls_of(&calls, "spawn_agent");
        assert_eq!(spawn.len(), 1, "{key:?} with {configured:?}");
        assert_eq!(spawn[0]["skip_permissions"], sent, "{key:?} with {configured:?}");
    }
}

#[test]
fn the_create_dialogs_launch_sends_its_checkbox() {
    let mut h = with_config(Some("bypass"));
    h.state_mut().state.open_dialog(Kind::Create);
    h.run_steps(3);
    assert!(h.state().state.dialog.as_ref().unwrap().skip, "checked: bypass is configured");
    h.state_mut().state.after_create(
        lazyagent_proto::types::CreateWorktreeResult {
            path: FEAT.into(),
            branch: "f".into(),
            custom_command: None,
            warning: None,
            job_id: None,
        },
        Some(("go".into(), false)),
    );
    let calls = fake_engine(&h, |_, _| None);
    h.run_steps(4);
    let spawn = calls_of(&calls, "spawn_agent");
    assert_eq!(spawn.len(), 1);
    assert_eq!(spawn[0]["skip_permissions"], false);
    assert!(spawn[0].get("permission_mode").is_none(), "{}", spawn[0]);
}

#[test]
fn a_quick_spawn_sends_no_mode() {
    let mut h = with_config(Some("plan"));
    let calls = fake_engine(&h, |_, _| None);
    h.state_mut().state.spawn_agent(Some(FEAT.into()));
    h.state_mut().state.spawn_agent(None);
    h.run_steps(2);
    let spawn = calls_of(&calls, "spawn_agent");
    assert_eq!(spawn.len(), 2);
    for params in &spawn {
        assert!(params.get("skip_permissions").is_none() && params.get("permission_mode").is_none(), "{params}");
        assert_eq!(params["default_permission_mode"], "cli", "with none configured: the CLI's own default");
    }
    assert_eq!(spawn[1]["kind"], "orchestrator");
}

#[test]
fn a_tab_names_a_mode_other_than_the_cli_default() {
    let mut plan = agent("a1", "", "running", "s1");
    plan["permission_mode"] = json!("plan");
    let mut cli = agent("a2", "Reviewer", "running", "s2");
    cli["permission_mode"] = json!("cli");
    let older = agent("a3", "", "running", "s3");
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![]), worktree(FEAT, "f", vec![plan, cli, older])]);
    let mut h = harness(app);
    h.run_steps(2);
    h.state_mut().state.selected = Some(FEAT.into());
    h.run_steps(3);
    assert!(h.query_all_by_label_contains("Agent a1 · plan").count() >= 1);
    assert!(h.query_all_by_label_contains("Reviewer").count() >= 1);
    assert_eq!(h.query_all_by_label_contains("Reviewer ·").count(), 0);
    assert!(h.query_all_by_label_contains("Agent a3").count() >= 1);
    let a1 = h.state().state.store.agents(FEAT)[0].clone();
    assert_eq!(crate::store::tab_label(&a1), "Agent a1 · plan");
}

#[test]
fn a_spawned_agent_carries_its_mode() {
    let (app, tx) = app(vec![worktree("/r/repo", "main", vec![]), worktree(FEAT, "f", vec![])]);
    let mut h = harness(app);
    h.run_steps(2);
    tx.send(event(
        "agent.spawned",
        json!({"worktree_path": FEAT, "agent_id": "a1", "session_id": "s1", "label": "", "session_name": "",
               "kind": "agent", "permission_mode": "dontAsk"}),
    ))
    .unwrap();
    h.run_steps(2);
    assert_eq!(h.state().state.store.agents(FEAT)[0].permission_mode.as_deref(), Some("dontAsk"));
}
