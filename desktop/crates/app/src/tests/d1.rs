//! "Settings…" (File menu): the page shows each option with the value in
//! force and where it comes from, saves to the scope picked (the engine's
//! settings_set), reads the result back, and refreshes the config the spawn
//! dialogs start from.

use super::*;

const KEY: &str = "agents.permission_mode";
const SETTLE: usize = 6;

fn settings(global: Option<&str>, repo: Option<&str>) -> serde_json::Value {
    let (value, source) = match (repo, global) {
        (Some(r), _) => (r, "repo"),
        (None, Some(g)) => (g, "global"),
        _ => ("cli", "default"),
    };
    json!({"repo": "repo", "files": {"global": "/home/dev/.config/lazyagent/config.toml", "repo": "/r/repo/.lazyagent.toml"},
           "settings": [{"key": KEY, "value": value, "source": source, "global": global, "repo": repo, "default": "cli",
                         "choices": ["cli", "bypass", "acceptEdits", "plan", "dontAsk", "auto", "default"]}]})
}

/// The engine: settings_get answers `start`; settings_set answers as if it
/// wrote the value; get_config gives back the new mode in force.
fn settings_engine(h: &Harness<'static, App>, start: serde_json::Value) -> Calls {
    let current = std::sync::Arc::new(std::sync::Mutex::new(start));
    fake_engine(h, move |m, p| match m {
        "settings_get" => Some(current.lock().unwrap().clone()),
        "settings_set" => {
            let mut now = current.lock().unwrap();
            let s = &now["settings"][0];
            let (mut global, mut repo) = (s["global"].as_str().map(String::from), s["repo"].as_str().map(String::from));
            let value = p["value"].as_str().map(String::from);
            if p["scope"] == "repo" {
                repo = value;
            } else {
                global = value;
            }
            *now = settings(global.as_deref(), repo.as_deref());
            Some(now.clone())
        }
        "get_config" => {
            let mode = current.lock().unwrap()["settings"][0]["value"].clone();
            Some(json!({"agent_provider": "claude", "default_branch": "main", "has_custom_create": false,
                        "has_custom_remove": false, "permission_mode": mode}))
        }
        _ => None,
    })
}

fn opened(caps: &[&str]) -> Harness<'static, App> {
    let (app, _tx) = app(vec![worktree("/r/repo", "main", vec![])]);
    let mut h = harness(app);
    h.run_steps(2);
    h.state_mut().state.capabilities = Some(caps.iter().map(|c| c.to_string()).collect());
    h.run_steps(1);
    h
}

fn open_from_the_menu(h: &mut Harness<'static, App>) {
    h.get_by_label("File").click();
    h.run_steps(2);
    h.get_by_label("Settings…").click();
    h.run_steps(SETTLE);
}

#[test]
fn the_menu_entry_opens_the_page_with_the_value_in_force() {
    let mut h = opened(&[crate::settings_view::FEATURE]);
    let calls = settings_engine(&h, settings(Some("acceptEdits"), None));
    open_from_the_menu(&mut h);
    assert!(h.state().state.settings.open);
    assert_eq!(calls_of(&calls, "settings_get"), [json!({})]);
    h.get_by_label("Settings");
    assert!(h.query_all_by_label("Agents").count() >= 2, "the section, besides the menu");
    h.get_by_label("Default permission mode for agents the engine starts");
    h.get_by_label_contains("Used whenever a spawn names none");
    h.get_by_label("In force: acceptEdits, from your global /home/dev/.config/lazyagent/config.toml");
    let draft = h.state().state.settings.drafts[KEY].clone();
    assert_eq!(draft, crate::settings_view::Draft { repo: false, value: "acceptEdits".into() }, "the scope in force");
    h.get_by_label("Close").click();
    h.run_steps(2);
    assert!(!h.state().state.settings.open);
}

#[test]
fn an_older_engine_greys_the_entry_out() {
    let mut h = opened(&[]);
    let calls = settings_engine(&h, settings(None, None));
    h.get_by_label("File").click();
    h.run_steps(2);
    h.get_by_label("Settings…").click();
    h.run_steps(2);
    assert!(!h.state().state.settings.open);
    assert!(calls_of(&calls, "settings_get").is_empty());
}

#[test]
fn saving_in_the_repo_sends_the_scope_and_reads_back() {
    let mut h = opened(&[crate::settings_view::FEATURE]);
    let calls = settings_engine(&h, settings(Some("acceptEdits"), None));
    open_from_the_menu(&mut h);
    h.get_by_label("This repo (repo)").click();
    h.run_steps(2);
    h.get_by_label("Stored in /r/repo/.lazyagent.toml: not set there");
    h.state_mut().state.settings.drafts.get_mut(KEY).unwrap().value = "bypass".into();
    h.run_steps(2);
    h.get_by_label("Save").click();
    h.run_steps(SETTLE);
    assert_eq!(calls_of(&calls, "settings_set"), [json!({"key": KEY, "scope": "repo", "value": "bypass"})]);
    h.get_by_label("In force: bypass (skip permissions), from this repo's /r/repo/.lazyagent.toml");
    h.get_by_label("Saved in /r/repo/.lazyagent.toml");
    assert_eq!(calls_of(&calls, "get_config").len(), 1, "the dialogs' default read again");
    let config = h.state().state.store.config.clone();
    assert_eq!(config.and_then(|c| c.permission_mode).as_deref(), Some("bypass"));
    assert!(crate::dialogs::skip_by_default(h.state().state.store.config.as_ref()));

    // The global scope says the repo's value wins.
    h.get_by_label("Global (every repo)").click();
    h.run_steps(2);
    h.get_by_label("This repo sets its own value, which wins over the global one.");

    // Removing the repo's: the global one is in force again.
    h.get_by_label("This repo (repo)").click();
    h.run_steps(2);
    h.get_by_label("Remove from this file").click();
    h.run_steps(SETTLE);
    assert_eq!(calls_of(&calls, "settings_set")[1], json!({"key": KEY, "scope": "repo", "value": null}));
    h.get_by_label("In force: acceptEdits, from your global /home/dev/.config/lazyagent/config.toml");
    h.get_by_label("Removed from /r/repo/.lazyagent.toml");
}

#[test]
fn save_waits_for_a_change() {
    let mut h = opened(&[crate::settings_view::FEATURE]);
    let calls = settings_engine(&h, settings(Some("plan"), None));
    open_from_the_menu(&mut h);
    h.get_by_label("Save").click();
    h.run_steps(2);
    assert!(calls_of(&calls, "settings_set").is_empty(), "the file already has it");
}

#[test]
fn a_refusal_shows_on_the_page() {
    let mut h = opened(&[crate::settings_view::FEATURE]);
    *h.state().state.conn.fake.lock().unwrap() = Some(Box::new(|method, _| {
        Some(match method {
            "settings_get" => Ok(settings(None, None)),
            _ => Err(lazyagent_proto::ClientError::Rpc(lazyagent_proto::envelope::RpcError {
                code: "VALIDATION_ERROR".into(),
                message: "could not write [agents] permission_mode safely".into(),
                data: None,
            })),
        })
    }));
    open_from_the_menu(&mut h);
    h.state_mut().state.settings.drafts.get_mut(KEY).unwrap().value = "plan".into();
    h.run_steps(2);
    h.get_by_label("Save").click();
    h.run_steps(SETTLE);
    h.get_by_label("could not write [agents] permission_mode safely");
    assert!(h.state().state.settings.saved.is_none());
}

#[test]
fn the_cli_choice_reads_as_the_clis_own_setting() {
    assert_eq!(crate::settings_view::choice_text("cli"), "CLI default (your agent CLI's own setting)");
    assert_eq!(crate::settings_view::choice_text("plan"), "plan");
}
